#!/usr/bin/env python3
"""Write work items (Feishu Project). For the scribe only (the guard hook enforces this). The commands
print what they would do first; nothing is written without --apply.

    workitem.py register <draft> [--task ID] [--source ID] [--set f=v ...] [--resume]
                                            需求包 / 决策包 / 研究任务单 / 反馈: work item + main document
    workitem.py reply <draft>               交付包: document under the request it answers, 交付包 field;
                                            handed in again after changes, its version one higher
    workitem.py confirm <ID> --by <会话名> [--set f=v ...] [--option 选项] [--executor 会话名]
                                            finish the step the item is at; on a decision's 决定 step,
                                            --option records the option picked and --executor hands the
                                            decision to a session in place of the package header's 执行方
    workitem.py confirm <ID> --by 到期默认  a reversible decision past its expiry time takes its default
    workitem.py rollback <ID> --to <节点> --reason R (--by <会话名> | --decision <ID>)
    workitem.py archive <ID>                move the main document subtree to 归档文档/<序列>, then finish the last step
    workitem.py update <ID> --set f=v [...] blocked_reason=<原因> also sets the date; an empty value clears a field
    workitem.py comment <ID> --text T

`confirm` asks the space what the current node still lacks, writes the --set values, confirms the
node and reads it back. A link field may be given a local file, --set 规格=@spec/spec.md: confirm and
update upload it as a document under the work item's main document and write its link.
Three checks are the scripts' own. A step whose form requires a signature field takes a
decision that is decided, approved and made for this task. Before the step that requires
the conclusion, the operation records are read: a frozen field changed after the signing
step stops the command. A roll-back on the main line needs an approved decision and
clears the required fields of the steps it undoes.

Package documents are written with docs.py, the ledger is read with ledger.py; what the three share is
in ledger_core.py.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import pool  # noqa: E402
import space  # noqa: E402
import ledger_core as core  # noqa: E402

NODE_STATUS = {"doing": "进行中", "finished": "已完成", "not_started": "未开始"}


def created_id(out, what):
    """The work item id in a `workitem create` answer; stop if there is none.

    Only work_item_id (anywhere), or an id at the top level or directly under data:
    a nested id may belong to the template or the project.
    """
    item_id = core._find(out, ("work_item_id",))
    if item_id in (None, "") and isinstance(out, dict):
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        item_id = out.get("id") or data.get("id")
    if item_id in (None, ""):
        raise core.LedgerError("%s建了，但输出里没有工作项 ID：%s；去网页上核对，不要重跑" % (
            what, json.dumps(out, ensure_ascii=False)[:300]))
    return str(item_id)
EXISTING_GONE = ("register --existing 停用了：研究任务单和包一样原位修订，文档链接不变。运行 "
                 "docs.py revise %s <修订稿> --note '<改了什么>' --apply")


STATUS_SECTION = "## 状态包\n\n### 简介\n\n%s### 当前状态\n\n### 近期变化\n\n### 风险\n"


def cognitive_layer(body):
    """What a new 状态包 starts from: the 认知层 of a package, the 目标 of a 研究任务单. '' when neither."""
    for heading in ("认知层", "目标"):
        m = re.search(r"^## %s[ \t]*\n(.*?)(?=^## |\Z)" % heading, body, re.M | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def with_status_sections(body):
    """Main documents end with 状态包 and then 交接记录; add whichever the draft lacks.
    A new 状态包 starts with the 认知层 as its 简介: the router matches against it."""
    has_status = re.search(r"^## 状态包\s*$", body, re.M)
    has_log = re.search(r"^## 交接记录\s*$", body, re.M)
    if has_status and has_log:
        return body
    intro = cognitive_layer(body)
    status = STATUS_SECTION % (intro + "\n\n" if intro else "")
    if has_log:
        return body[:has_log.start()] + status + "\n" + body[has_log.start():]
    if has_status:
        return body.rstrip("\n") + "\n\n## 交接记录\n"
    return body + "\n" + status + "\n## 交接记录\n"


def check_receipt(lg, path, resume, again=""):
    """The receipt to continue from with --resume (or --doc); None for a fresh registration."""
    rec = core.find_receipt(lg, path)
    if not rec:
        if resume:
            raise core.LedgerError("没有这份草稿的登记回执，不用 --resume")
        return None
    item_id = rec.get("work_item_id")
    if rec.get("status") == "registered":
        raise core.LedgerError("这份草稿已经登记成 #%s（回执 %s），不要重复登记%s" % (item_id, rec["_file"], again))
    if not resume:
        step = "写回主文档链接" if rec.get("doc") else ("查主文档的知识库节点" if rec.get("docx") else "建主文档")
        raise core.LedgerError("上次已经建了工作项 #%s，但没做完（停在：%s）。修好原因后加 --resume 接着做，不会再建一个" % (item_id, step))
    try:
        lg.item(item_id)
    except core.LedgerError as e:
        raise core.LedgerError("回执里的工作项 #%s 读不到了（%s）。如果它已经被删掉，删掉回执 %s 再重新登记" % (
            item_id, str(e)[:120], rec["_file"]))
    return rec
BY_DEFAULT = "到期默认"


def hours_text(hours):
    hours = round(float(hours), 1)
    return "%s 小时" % (int(hours) if hours.is_integer() else hours)


def decision_options(lg, item_id):
    """A decision's options as registered or revised: the local decision state first, then the receipt.
    None when neither has them (registered before 1.0.4)."""
    entry = core.load_gates(lg.root)["pending"].get(str(item_id))
    if entry and entry.get("options") is not None:
        return entry["options"]
    return core.receipt_of(lg, item_id).get("options")


def chosen_option(options, option, item_id):
    """The option --option names, checked against the decision's options table; {"name": "无"} when the
    researcher picked none of them; None when --option is left out. The option is a record only: it goes
    into the receipt and a comment, and nothing is released by it."""
    if option is None:
        return None
    option = option.strip()
    if option == orgspec.NOTHING:
        return {"name": orgspec.NOTHING, "approach": "", "irreversible": []}
    hit = next((o for o in options or [] if o.get("name") == option), None)
    if hit:
        return hit
    if options is None:
        raise core.LedgerError("#%s 没有记下选项表（包里的选项写的是“无”，或者是 1.0.4 以前登记的）：不加 --option，研究员定的内容写进 --set 决定内容"
                          % item_id)
    raise core.LedgerError("#%s 的选项里没有“%s”，有：%s；研究员没有选表里的任何一个，写 --option 无，或者不加 --option" % (
        item_id, option, "、".join(o.get("name", "") for o in options)))


def changed_executor(lg, value, item_id):
    """The session `confirm --executor` hands a decision to, under the package header's own check
    (orgspec.read_executor): a sequence's +2, the entry or the router. 研究员 is refused: what the researcher
    is to do by hand has to be in the package, so that takes a new version."""
    if value is None:
        return None
    who, why = orgspec.read_executor(lg.spec, value)
    if who == core.RESEARCHER:
        raise core.LedgerError("--executor 只收会话名：要把 #%s 的执行方改成研究员，请发出方写下一版（包头“%s”写“研究员”，“%s”写清楚要他做什么），"
                          "书记员用 docs.py revise 换主文档以后再确认" % (item_id, orgspec.EXECUTOR, orgspec.RESEARCHER_ACTION))
    if not who:
        raise core.LedgerError("--executor 写的是“%s”：%s" % (value, why))
    return who


def settle(lg, item_id, choice, by_default=None, executor=None):
    """After 决定 is confirmed: the decision leaves the pending decisions, and its receipt keeps how it was
    decided: the option picked (when --option named one, or 到期默认 took the default), who decided (研究员 or
    到期默认) and when. executor (--executor) becomes the receipt's executor, with researcher_action null;
    as_packaged keeps what the package header said, for a rollback to 待决定 to put back."""
    if lg.mode != "apply":
        return
    core.save_gates(lg, lambda gates: gates["pending"].pop(str(item_id), None), keep=item_id)
    values = {"option": (choice or {}).get("name"), "decided_by": BY_DEFAULT if by_default else core.RESEARCHER, "decided_at": core.now()}
    rec = core.receipt_of(lg, item_id)
    if executor and executor != rec.get("executor"):
        packaged = rec.get("as_packaged") or {"executor": rec.get("executor"), "researcher_action": rec.get("researcher_action")}
        values.update(executor=executor, researcher_action=None, as_packaged=packaged)
    core.update_receipt(lg, item_id, **values)


def drop_pending(lg, item_id):
    """Take a decision out of the pending decisions (archived, terminated); True when it was there."""
    if lg.mode != "apply" or str(item_id) not in core.load_gates(lg.root)["pending"]:
        return False
    core.save_gates(lg, lambda gates: gates["pending"].pop(str(item_id), None), keep=item_id)
    return True


def drop_owner(lg, item_id):
    """Take a finished work item out of the pool's table of who answers for it (pool.json owners), so that
    `pool.py show` stops listing it under a session; True when it was there. The table is written by the inbox
    hook when a session is handed the item and by nothing else, so until 1.0.8 it only grew. A project without
    a pool, or a pool.json that cannot be read, stops no archive: pool.py show --prune takes it out later."""
    if lg.mode != "apply":
        return False
    try:
        data, ok = pool.load(lg.root)
        if not ok or str(item_id) not in (data.get("owners") or {}):
            return False
        with pool.locked(lg.root) as held:
            return held.get("owners", {}).pop(str(item_id), None) is not None
    except (pool.PoolError, OSError):
        return False


def owner_note(lg, item_id):
    return "会话池的认领表里移除了 #%s。" % item_id if drop_owner(lg, item_id) else ""



def doc_from_link(lg, link, given=False):
    """/wiki/ link of a document known by a /wiki/ or /docx/ link or a document_id.
    given=True checks a link typed with --doc before using it."""
    link = (link or "").strip()
    if given and not re.match(r"https?://[^/\s]+/(?:wiki|docx)/[A-Za-z0-9]{6,}$", link):
        raise core.LedgerError("--doc 要给文档的 /wiki/ 或 /docx/ 链接，或者 new：%s" % link)
    if "/wiki/" in link:
        return link
    return lg.doc_link(link if link.startswith("http") else None, None if link.startswith("http") else link)


def main_document(lg, rec, doc, where, create, remember):
    """The /wiki/ link of the draft's document: given with --doc, kept in the receipt, or created now.

    create() makes the document and calls remember(docx=...) as soon as it exists;
    remember(**kw) updates the receipt. When docs +create timed out or answered
    without a link, the receipt keeps doc_unknown and the next run stops until --doc
    says what to do: --doc <链接> takes over the document found on the web, --doc new
    creates another one.
    """
    if doc and doc.strip().lower() != "new":
        url = doc_from_link(lg, doc, given=True)
        remember(doc=url, doc_unknown=False)
        return url
    if rec.get("doc"):
        return rec["doc"]
    if rec.get("docx"):
        url = doc_from_link(lg, rec["docx"])
        remember(doc=url)
        return url
    if rec.get("doc_unknown") and (doc or "").strip().lower() != "new":
        raise core.LedgerError("上次建文档没有拿到确定的结果，文档可能已经建在%s下了。先在网页上核对：建好了的，重跑时加 --doc <文档链接> 接着用它；确认没建成的，加 --doc new 重新建" % where)
    try:
        url = create()
    except core.WriteMaybeDone:
        remember(doc_unknown=True)
        raise
    remember(doc=url, doc_unknown=False)
    return url


def resolved_pairs(lg, pairs):
    """--set pairs with @file replaced by the file's text, as kept in a receipt: a --resume
    writes the same values even if the file has moved or changed."""
    out = []
    for pair in pairs or []:
        fid, eq, raw = pair.partition("=")
        if not eq:
            raise core.LedgerError("--set 要写成 字段标识=值：%s" % pair)
        value = set_value(lg, raw)
        out.append("%s=%s" % (fid, "@" + value if value.startswith("@") else value))
    return out


def merge_pairs(*lists):
    """Later --set pairs win over earlier ones for the same field."""
    merged = {}
    for pairs in lists:
        for pair in pairs or []:
            merged[pair.partition("=")[0]] = pair
    return list(merged.values())


def parent_node(lg, wid, seq, rel, source):
    if wid == "same_seq" and rel == "execute":
        if not source:
            raise core.LedgerError("执行关系的需求包要写来源工作项（它所属的下发需求包）：--source <ID>")
        node = lg.node_of(lg.doc_url(source))
        if not node:
            raise core.LedgerError("来源工作项 #%s 的主文档不是知识库文档，没法把执行需求包挂在它下面" % source)
        return node
    key = "same_seq/%s" % seq if wid == "same_seq" else wid
    n = lg.nodes.get(key)
    if not n or not n.get("node_token"):
        raise core.LedgerError("ids.json 里没有知识库节点 %s；先跑 ensure-node %s" % (key, key))
    return n["node_token"]


def ensure_roles(lg, wid, item_id):
    """After create, check that every role of the item has the researcher in it; add where it
    does not. Returns "ok", "added", "unknown" (could not tell) or None (not checked)."""
    if lg.mode != "apply":
        return None
    try:
        data = lg.get(item_id, ["_all"])
        members = ((data or {}).get("work_item_attribute") or {}).get("role_members")
        if not isinstance(members, list):
            print("提醒：读不到 #%s 的角色人员，没法确认各节点有没有负责人；请在网页上核对。" % item_id)
            return "unknown"
        me = lg.me()
        empty = [r.get("key") for r in members if isinstance(r, dict) and r.get("key")
                 and me not in json.dumps(r.get("members") or [], ensure_ascii=False)]
        if not empty:
            return "ok"
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--role-operate",
                  json.dumps([{"op": "add", "role_key": r, "user_keys": [me]} for r in empty]), write=True)
        print("建工作项时带的角色人员没全生效，已用 --role-operate 把研究员补进：%s。" % "、".join(empty))
        return "added"
    except core.LedgerError as e:
        print("提醒：没能核对 #%s 的角色人员：%s" % (item_id, str(e)[:200]))
        return "unknown"


def flow_entry(lg, wid, name=None):
    """The `template` field of workitem create: which flow the new item follows."""
    tid, flow = space.flow_of(lg.snap, (lg.types.get(wid) or {}).get("type_key"), name)
    if not tid:
        if lg.mode == "offline":
            return {"field_key": "template", "field_value": "<流程 ID>"}
        flows = "、".join(f.get("name") or "" for f in (lg.space_type(wid).get("flows") or {}).values())
        if not flows:
            raise core.LedgerError("feishu/space.json 里没有%s的流程；先跑 setup.py feishu --live --apply" % lg.spec.term("wi", wid))
        raise core.LedgerError("%s有这些流程：%s；%s" % (lg.spec.term("wi", wid), flows,
                                               "没有“%s”" % name if name else "要写明走哪一个"))
    return {"field_key": "template", "field_value": str(tid)}


def space_url(lg):
    host = (lg.ids.get("meegle") or {}).get("host") or "project.feishu.cn"
    return "https://%s/%s" % (host, lg.simple_name or lg.key)


def open_tasks(lg):
    """The research tasks that are not archived: [(id, name)]. [] offline."""
    main = lg.contract.main_line()
    if lg.mode == "offline" or main not in lg.types:
        return []
    rows = []
    for mql in core.mql_text(lg, "tasks"):
        rows += core.query_all(lg, mql)
    return [(str(r.get("work_item_id")), r.get("name") or "") for r in rows]


def task_of(lg, wid, given):
    """The research task a new item belongs to: what was given, else the only one that is open."""
    if given:
        return core.item_id_of(given)
    tasks = open_tasks(lg)
    if len(tasks) == 1:
        return tasks[0][0]
    if lg.mode == "offline":
        return "<所属研究任务 ID>"
    key = lg.key_of(wid, "task_ref")
    must = any(c.get("required") and c.get("field_key") == key for c in lg.space_type(wid).get("create_fields") or [])
    if not must:
        return None
    raise core.LedgerError("没法确定这份包属于哪条研究任务：空间里没归档的研究任务有 %d 条%s。包头加一行“- 所属研究任务：#<ID>”，或者登记时加 --task <ID>" % (
        len(tasks), "（%s）" % "、".join("#%s %s" % t for t in tasks[:6]) if tasks else ""))


def first_step_is_the_senders(lg, wid, nodes):
    """The first node only records that the sender put the item in (跨序列需求的“提出”): its
    role is the issuer and everything it requires is filled in when the item is created."""
    first = next((n for n in nodes or [] if n["status"] == "doing"), None)
    if not first or first["prev"] or len(nodes) < 3:
        return None
    created = {c["field_key"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required")}
    issuer = (lg.roles.get(wid) or {}).get("issuer")
    if first["roles"] == [issuer] and first["required"] and all(r["field_key"] in created for r in first["required"]):
        return first
    return None



# A research task sheet's 资源上限 names the conditions a paid run depends on: 执行环境 and 计算资源 say
# 待研究员确认, or name the decision that set them. Choosing them meets criterion 4 of the list of
# irreversible actions, so they go into the decision queue, exploratory tasks included.
RUN_CONDITIONS = ("执行环境", "计算资源")
NOT_YET = re.compile(r"(?:待研究员确认|待确认|待定|未定|无)(?:$|[\s（(，,。；;：:])")
DECISION_REF = re.compile(r"(?:决策\s*#?\s*|#)(\d{3,})")


def run_conditions_hints(d):
    """Lines to print when a research task sheet writes 执行环境 or 计算资源 without the decision that set them."""
    section = d.section("资源上限")
    hints = []
    for row in RUN_CONDITIONS:
        m = re.search(r"^\s*[-*]\s*%s\s*[：:][ \t]*(.*)$" % row, section, re.M)
        value = m.group(1).strip() if m else ""
        if not value or orgspec.placeholder(value) or NOT_YET.match(value) or DECISION_REF.search(value):
            continue
        hints.append("提示：研究任务单“资源上限”里的%s写了“%s”，没有定下它的决策 ID：%s要进决策队列。请发出方写决策包交研究员定，"
                     "试探任务也一样；定了以后把决策 ID 写进这一行，比如“%s（决策 #<ID>）”，没定之前写“待研究员确认”。" % (row, value, row, value))
    return hints


def register(lg, path, source=None, pairs=None, resume=False, doc=None, task=None):
    """Register a draft: a work item with the draft as its main document.

    需求包 → 同序列需求 or 跨序列需求; 决策包 → 决策; 研究任务单 → 研究任务; 反馈 → 反馈.
    Everything that can fail is checked before the work item exists: the header, the
    fields the new-item page requires, the parent node. The item is created with a
    link to the space as its main document, since the page requires one; the document
    is created next, with the item's ID in its title and header, and written back. A decision's
    执行方 and 研究员要做的事 go into the fields of those names where the space has them (header_fields).
    A sheet or a package that is registered already takes its next version through revise, which keeps the
    document and its link (until 1.0.9 a revised 研究任务单 became a new document: register --existing).
    """
    spec = lg.spec
    kind = core.sheet_kind(path)
    d = core.Sheet(spec, path, kind) if kind else core.Draft(spec, path)
    wid = kind or core.work_item_type(d)
    if not d.author:
        raise core.LedgerError("发出方要带会话名，比如“算法 +2（Alg-p2-01）”")
    if not kind and d.version != 1:
        raise core.LedgerError("登记的是新工作项，包头“版本”写第 1 版，现在是第 %s 版：已经登记过的包改了内容，用 docs.py revise <工作项 ID> <新草稿> 换主文档" % d.version)
    if kind == "research_task":
        for line in run_conditions_hints(d):
            print(line)
    rec = check_receipt(lg, path, resume or bool(doc) and bool(core.find_receipt(lg, path))) if lg.mode != "offline" else None
    if rec and rec.get("existing"):
        raise core.LedgerError(EXISTING_GONE % rec.get("work_item_id"))
    if rec:
        source, task = source or rec.get("source"), task or rec.get("task")
        pairs = merge_pairs(rec.get("pairs"), resolved_pairs(lg, pairs))   # rewritten with the main document
    source = core.item_id_of(source or d.source) if (source or d.source) else None
    core.link_line(d.text, "#0", "0")                      # a 关联工作项 line that is no placeholder stops here
    name, title = core.naming(spec, d, kind)

    fields = [{"field_key": "name", "field_value": name}, flow_entry(lg, wid, d.flow)]
    required = [("sender", d.author)]
    # a sheet the researcher or the mentor is the sender of belongs to the entry's sequence
    seq = d.receiver_seq if wid == "cross_request" else d.sender_seq or ("pmo" if kind else None)
    if not seq:
        raise core.LedgerError("从发出方、接收方认不出序列：写成“<序列名> +<职级>（<会话名>）”")
    if "sequence" in lg.contract.slots(wid, "fields"):
        required.append(("sequence", spec.term("seq", seq)))
    if wid == "same_seq":
        if not d.rel:
            raise core.LedgerError("同序列需求要写关系：下发或执行")
        rank = re.search(r"-p([0-2])-", d.author or "")
        # +2 发下发关系的包，+1 发执行关系的包；本序列没开 +1 时 +2 直接委托 +0，写的也是执行关系（1.0.12）
        expected = {"2": ("assign", "execute"), "1": ("execute",)}.get(rank.group(1), ()) if rank else ()
        if d.rel not in expected:
            raise core.LedgerError("关系“%s”和发出方 %s 的职级对不上：+2 发给 +1 的同序列需求是下发，本序列没开 +1、直接发给 +0 的是执行；+1 发的是执行；退回作者改包头" % (
                spec.term("rel", d.rel), d.author))
        required += [("relation", spec.term("rel", d.rel)), ("owner_session", d.receiver or "待委托")]
    if wid == "cross_request":
        if not (d.sender_seq and d.receiver_seq):
            raise core.LedgerError("跨序列需求要认得出发起序列和承接序列")
        required += [("from_sequence", spec.term("seq", d.sender_seq)), ("to_sequence", spec.term("seq", d.receiver_seq))]
    if source:
        required.append(("source_item", source))
    if "task_ref" in lg.contract.slots(wid, "fields"):
        task = task_of(lg, wid, task or d.task_ref)
        if task:
            required.append(("task_ref", task))
    for ref, raw in required:
        fields.append(lg.entry(wid, ref, raw))
    optional = []
    has_due = "due" in lg.contract.slots(wid, "fields")          # a decision has no deadline: it waits for the researcher
    if has_due and core.utc_noon_ms(d.header.get("回复期限", "")):
        optional.append(("due", d.header["回复期限"]))
    elif has_due and re.search(r"\d{4}-\d{1,2}-\d{1,2}", d.header.get("回复期限", "")):
        print("提醒：回复期限“%s”不是存在的日期，截止时间没写；改好后用 update 补" % d.header["回复期限"])
    if wid == "decision":
        optional.append(("urgency", d.header.get("紧急程度")))
    for ref, raw in optional:
        if not raw or "/" in str(raw) and ref == "urgency":       # the template's "插队/今天/本周/可延后" left as it was
            continue
        try:
            fields.append(lg.entry(wid, ref, raw))
        except (core.LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（之后可以用 update 补）：%s" % (ref, e))
    sets = parse_sets(lg, wid, pairs)                 # on --resume, all values are written again with the main document
    if any("upload" in v for _, v in sets):
        raise core.LedgerError("登记时还没有主文档，产出文档没处挂：先登记，再用 workitem.py update <工作项 ID> --set <字段>=@<文件> 上传")
    fields += core.header_fields(lg, wid, d, {v["field_key"] for _, v in sets})
    if not rec:
        fields += [v for _, v in sets]
    doc_key = lg.field(wid, "main_doc")["field_key"]
    fields = [f for f in fields if f["field_key"] != doc_key] + [{"field_key": doc_key, "field_value": space_url(lg)}]
    have = {f["field_key"] for f in fields if f.get("field_value") not in (None, "")}
    lacking = [c["name"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required") and c["field_key"] not in have]
    if lacking and not rec:
        raise core.LedgerError("新建%s时这些字段必填，草稿里读不出来：%s。在包头写上，或者登记时用 --set <字段名>=<值> 传" % (
            spec.term("wi", wid), "、".join(lacking)))
    owners = lg.role_owners(wid)
    if owners:
        fields.append(owners)
    parent = parent_node(lg, wid, seq, d.rel, source)

    kinds = core.receipt_kind(spec, wid, d, pairs)
    if rec:
        item_id = rec["work_item_id"]
        print("接着上次的 #%s 做。" % item_id)
        core.save_receipt(lg, path, item_id, "created", kinds, pairs=pairs)        # values given on --resume too
    else:
        created = lg.meegle("workitem", "create", "--work-item-type", lg.api(wid), "--fields",
                            json.dumps(fields, ensure_ascii=False), write=True)
        item_id = created_id(created, spec.term("wi", wid)) if created is not None else "<新工作项 ID>"
        core.save_receipt(lg, path, item_id, "created", kinds, source=source, task=task, pairs=resolved_pairs(lg, pairs),
                     registered_at=core.now())
    if wid == "decision" and kinds.get("queue"):      # frozen from the moment the work item exists
        core.hold(lg, item_id, kinds, (rec or {}).get("registered_at") or core.receipt_of(lg, item_id).get("registered_at"))
    title = title.replace("<工作项 ID>", item_id)

    state = {}

    def remember(**kw):
        state.update(kw)
        core.save_receipt(lg, path, item_id, "created", **kw)

    def create():
        link = lg.item_url(wid, item_id)
        body = core.link_line(d.body_without_title(), "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id)
        if kind != "feedback":
            body = with_status_sections(body)
        return lg.create_doc(parent, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        if owners and not rec:
            ensure_roles(lg, wid, item_id)
        url = main_document(lg, rec or {}, doc, "节点 %s " % parent, create, remember)
        updates = [{"field_key": doc_key, "field_value": url}]
        if rec:
            updates += [v for _, v in sets]
        if wid == "same_seq" and d.rel == "execute" and not d.receiver:
            updates.append(lg.entry(wid, "owner_session", "%s-p0-%s" % (spec.term("abbrev", d.sender_seq), item_id)))
        lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields",
                  json.dumps(updates, ensure_ascii=False), write=True)
        lg.meegle("comment", "add", "--work-item-id", item_id, "--content",
                  "%s 已登记：%s。主文档：%s" % (lg.tag, title, url), write=True)
    except core.LedgerError as e:
        if state.get("doc_unknown", (rec or {}).get("doc_unknown")) and not (state.get("docx") or state.get("doc")):
            raise core.WriteMaybeDone("%s\n工作项 #%s 已经建了，建主文档没有拿到确定的结果：先在网页上节点 %s 下核对。建好了的，运行 workitem.py register %s --resume --doc <文档链接> --apply；确认没建成的，运行 workitem.py register %s --resume --doc new --apply" % (
                e, item_id, parent, path, path))
        raise core.LedgerError("%s\n工作项 #%s 已经建了，主文档字段暂时指着空间首页；修好后运行 workitem.py register %s --resume --apply 接着做，不会再建一个工作项（来源工作项、所属研究任务和 --set 的值已记在回执里）" % (
            e, item_id, path))
    drifted = ""
    if lg.mode == "apply":
        drifted = core.write_back(path, d.text, item_id)
        core.save_receipt(lg, path, item_id, "registered", doc=url, draft_changed=True if drifted else None)
    print(("登记：%s #%s，主文档放在节点 %s 下。" if lg.mode == "apply" else "预览：会登记一条%s（#%s），主文档放在节点 %s 下。") % (
        spec.term("wi", wid), item_id, parent))
    if drifted:
        print("提醒：" + drifted)
    after_register(lg, wid, d, item_id)
    return item_id


def after_register(lg, wid, d, item_id):
    """What follows a registration: the sender's own first step is confirmed at once; for the
    rest, the command that comes next is printed."""
    spec = lg.spec
    if lg.mode == "apply":
        try:
            first = first_step_is_the_senders(lg, wid, lg.flow_nodes(item_id))
            if first:
                lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", first["key"],
                          "--action", "confirm", write=True)
                print("“%s”是发出方自己的一步，已随登记确认。" % first["name"])
        except core.LedgerError as e:
            print("提醒：登记好了，第一步没能确认：%s。接着运行 workitem.py confirm %s --by %s --apply" % (str(e)[:200], item_id, d.author))
    if wid == "decision" and d.header.get("渠道") == spec.term("channel", "researcher"):
        sec = d.section("决定")
        content = re.search(r"决定内容[：:]\s*(.+)", sec)
        decider = re.search(r"决定人[：:]\s*(.+)", sec)
        if content and decider:
            print("研究员主动发起的决策，接着确认：workitem.py confirm %s --by 研究员 --set 决定内容='%s' --set 决定人=研究员"
                  % (item_id, content.group(1).strip()))
        else:
            print("研究员主动发起的决策，但“决定”一节没写全决定内容和决定人：退回作者补上，再确认“决定”这一步。")
    elif wid == "same_seq":
        print("请求方说马上委托下一级的（登记请求写了“开工”），接着确认：workitem.py confirm %s --by <承接方的会话名>；排队还没委托的留在“%s”。" % (
            item_id, spec.term("state", "queued")))
    elif wid in (lg.contract.main_line(), "feedback"):
        print("接着看这一步要什么：ledger.py next %s" % item_id)


def delivered(lg, parent_id):
    """The deliveries a request has received so far, from the reply receipts that finished
    (packages/sent/reply-<ID>-*.json): [(version, receipt)], lowest first. A receipt from before 1.0.6 keeps
    no version: None, which sorts as the first."""
    out, folder = [], glob.escape(core.sent_dir(lg))
    for path in sorted(glob.glob(os.path.join(folder, "reply-%s-*.json" % parent_id))):
        rec = core.load_json(path, {})
        if isinstance(rec, dict) and rec.get("status") == "registered" and str(rec.get("parent", parent_id)) == str(parent_id):
            out.append((rec.get("version") if isinstance(rec.get("version"), int) else None, os.path.relpath(path, lg.root)))
    return sorted(out, key=lambda x: x[0] or 1)


def reply(lg, path, doc=None):
    """Hang a 交付包 under the main document of the request it answers and put its link into
    the request's 交付包 field.

    The target field and the parent node are worked out before anything is written; a
    receipt packages/sent/reply-<ID>-<file>.json lets a rerun reuse the document
    instead of creating a second one. The receipt keeps the version handed in. A delivery
    changed after one that was not accepted is handed in again, from the same file or another:
    its version is the last one plus one, it becomes a new document (the earlier one stays
    where it is) and the 交付包 field takes its link; the same version again, or a number that
    does not follow, stops with the number to write.
    """
    spec = lg.spec
    d = core.Draft(spec, path)
    if d.pid != "delivery":
        raise core.LedgerError("%s要用 register 登记" % d.header["包类型"])
    if not d.linked:
        raise core.LedgerError("包头的“关联工作项”要写它答复的工作项 ID，比如 #2003")
    parent_id = core.item_id_of(d.linked[0])
    rpath = core.receipt_file(lg, ["reply-%s-%s" % (parent_id, core.safe_name(path))], path)
    rec = (core.load_json(rpath, {}) or {}) if lg.mode != "offline" else {}
    rec = rec if isinstance(rec, dict) else {}
    earlier = delivered(lg, parent_id) if lg.mode != "offline" else []
    again = bool(earlier or rec.get("earlier"))        # not the first delivery of this request
    if earlier and rec.get("status") != "created":     # handed in again; an unfinished run of this one just goes on
        last, receipt = max(v or 1 for v, _ in earlier), earlier[-1][1]
        how = "验收不通过、改好后重新交的，包头“%s”写第 %d 版，再 reply" % (orgspec.VERSION, last + 1)
        if rec.get("status") == "registered" and d.version == (rec.get("version") or 1):
            raise core.LedgerError("这份%s（第 %d 版）已经挂到 #%s 下了（回执 %s），不要重复登记；%s" % (
                spec.pkg(d.pid), d.version, parent_id, os.path.relpath(rpath, lg.root), how))
        follows = d.version == last + 1 if all(v for v, _ in earlier) else d.version > last   # an old receipt: its number is not known
        if not follows:
            raise core.LedgerError("#%s 已经收过第 %d 版%s（回执 %s），这一份写的是第 %d 版：%s" % (
                parent_id, last, spec.pkg(d.pid), receipt, d.version, how))
    it = lg.item(parent_id)
    if it is not None and not it.get("type"):
        raise core.LedgerError("认不出 #%s 的类型（type_key %s）；确认工作项 ID 没写错，或者先跑 setup.py feishu --live --apply 重读类型" % (
            parent_id, it.get("type_key")))
    wid = (it and it.get("type")) or "same_seq"
    if wid not in lg.contract.types_with("fields", "delivery_link"):
        raise core.LedgerError("交付包答复的是同序列需求或跨序列需求，#%s 是%s" % (parent_id, spec.term("wi", wid)))
    link_key = lg.field(wid, "delivery_link")["field_key"]
    if again and it is not None and it["state"] in core.closed_states(lg):
        raise core.LedgerError("#%s 已经是“%s”：工作项关闭以后不再重新交%s" % (parent_id, it["state"], spec.pkg(d.pid)))
    node = lg.node_of(lg.doc_url(parent_id, it))
    if not node:
        raise core.LedgerError("#%s 的主文档不是知识库文档，答复没处挂" % parent_id)
    title = spec.rules["naming"]["reply_doc"].replace("<答复包名>", spec.pkg(d.pid)) \
        .replace("<作者会话名>", d.author or "?").replace("<同一个任务名>", d.task)
    if rec.get("status") == "registered":             # this file handed in again: a new document, the earlier one stays
        rec = {"earlier": list(rec.get("earlier") or []) + [{"version": rec.get("version") or 1, "doc": rec.get("doc")}]}

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=os.path.relpath(path, lg.root), parent=parent_id, version=d.version, updated_at=core.now())
            rec.setdefault("status", "created")
            os.makedirs(core.sent_dir(lg), exist_ok=True)
            core.save_json(rpath, rec)

    def create():
        return lg.create_doc(node, title, d.body_without_title(), created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        link = main_document(lg, dict(rec), doc, "#%s 的主文档下" % parent_id, create, remember)
        lg.meegle("workitem", "update", "--work-item-id", parent_id, "--fields", json.dumps(
            [{"field_key": link_key, "field_value": link}]), write=True)
        lg.meegle("comment", "add", "--work-item-id", parent_id, "--content",
                  "%s 收到%s%s：%s" % (lg.tag, spec.pkg("delivery"), "第 %d 版" % d.version if again else "", link), write=True)
    except core.LedgerError as e:
        receipt = os.path.relpath(rpath, lg.root)
        if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
            raise core.LedgerError("%s\n建文档没有拿到确定的结果（回执 %s）：先在网页上核对 #%s 的主文档下有没有这份%s；建好了的，重跑时加 --doc <文档链接>；确认没建成的，加 --doc new" % (
                e, receipt, parent_id, spec.pkg(d.pid)))
        if rec.get("doc") or rec.get("docx"):
            raise core.LedgerError("%s\n文档已经建好，回执记在 %s；修好后重跑同一条命令，会接着用这份文档，不会再建一个" % (e, receipt))
        raise core.LedgerError("%s\n文档还没建，飞书里没有写入；修好后重跑同一条命令即可" % e)
    remember(status="registered")
    print("交付包%s已挂到 #%s 下，“%s”字段已填%s。承接方接着请书记员确认这一步：workitem.py confirm %s --by <承接方的会话名>" % (
        "第 %d 版" % d.version if again else "", parent_id, lg.label(wid, link_key),
        "，上一版的文档留在原处" if again else "", parent_id))
    return link


def set_value(lg, raw):
    """--set key=@file reads the file (relative to the current directory, then the project);
    @@text keeps a literal @text.

    When no such file exists, the value stops the command if it could be a file name:
    one word, or anything with a /, a .ext ending or a <placeholder>. Only text with a
    space and none of those (@研究员 还没批预算) is kept as it is.
    """
    if raw.startswith("@@"):
        return raw[1:]
    if not raw.startswith("@") or len(raw) == 1:
        return raw
    name = raw[1:]
    full = find_file(lg, name)
    if full:
        return read_text_file(lg, full).strip()
    if not re.search(r"\s", name) or re.search(r"/|<|>|\.[A-Za-z0-9]{1,5}$", name):
        raise core.LedgerError("找不到文件 %s（--set 的 @文件 先按当前目录、再按项目目录找）；要写以 @ 开头的文字，用 @@" % name)
    return raw


def find_file(lg, name):
    """The file an @name of --set points at: looked for under the current directory, then the project."""
    for base in dict.fromkeys((os.getcwd(), lg.root)):
        full = name if os.path.isabs(name) else os.path.join(base, name)
        if os.path.isfile(full):
            return full
    return None


def read_text_file(lg, full):
    try:
        with open(full, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise core.LedgerError("文件 %s 不是 UTF-8 文本；转成 UTF-8 再传" % os.path.relpath(full, lg.root))


def parse_sets(lg, wid, pairs):
    out = []
    for pair in pairs or []:
        if "=" not in pair:
            raise core.LedgerError("--set 要写成 字段=值：%s" % pair)
        ref, raw = pair.split("=", 1)
        try:
            f = lg.field(wid, ref)
            if f.get("field_type") == "link" and raw.startswith("@") and not raw.startswith("@@") and len(raw) > 1:
                # a link field given a local file: the file becomes a document when the values are written
                full = find_file(lg, raw[1:])
                if not full:
                    raise core.LedgerError("找不到文件 %s（--set 的 @文件 先按当前目录、再按项目目录找）" % raw[1:])
                out.append((ref, {"field_key": f["field_key"], "field_value": "@" + os.path.relpath(full, lg.root), "upload": full}))
                continue
            out.append((ref, lg.entry(wid, ref, set_value(lg, raw))))
        except (orgspec.OrgSpecError, KeyError) as e:
            raise core.LedgerError("--set %s：%s（%s有哪些字段见 feishu/space.md）" % (ref, e, lg.spec.term("wi", wid)))
    return out


def attach(lg, item_id, it, wid, key, path):
    """Upload a local file as a document under the work item's main document; return its link.

    This is how a document written by the session that owns a step gets into Feishu without a
    request of its own: the step is the work item behind it. One receipt per file and content
    (packages/sent/attach-<ID>-<field>-<file>-<hash>.json): a rerun with the same content uses
    the document it made, a changed file becomes a new document with a new link, so a field
    frozen by a signature cannot change unnoticed.
    """
    text = read_text_file(lg, path)
    rel, label = os.path.relpath(path, lg.root), lg.label(wid, key)
    if not text.strip():
        raise core.LedgerError("文件 %s 是空的" % rel)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    rpath = core.receipt_file(lg, ["attach-%s-%s-%s-%s" % (item_id, core.safe_name(label), core.safe_name(path), digest)], path)
    rec = (core.load_json(rpath, {}) or {}) if lg.mode != "offline" else {}
    rec = rec if isinstance(rec, dict) else {}
    receipt = os.path.relpath(rpath, lg.root)
    if rec.get("status") == "registered" and rec.get("doc"):
        print("%s 没有改过，沿用上次上传的文档：%s" % (rel, rec["doc"]))
        return rec["doc"]
    if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
        raise core.LedgerError("上次上传 %s 没有拿到确定的结果（回执 %s）。先在网页上看 #%s 的主文档下有没有这份文档：有的话把它的链接直接写进 "
                          "--set %s=<文档链接>；确认没有，删掉这份回执再重跑" % (rel, receipt, item_id, label))
    node = lg.node_of(lg.doc_url(item_id, it))
    if not node:
        raise core.LedgerError("#%s 的主文档不是知识库文档，“%s”没处挂" % (item_id, label))
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
    title = "%s：%s" % (label, core.title_of(text) or (it or {}).get("name") or "#%s" % item_id)
    body = "\n".join(l for i, l in enumerate(lines) if i != head).strip() + "\n"

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=rel, parent=str(item_id), field=label, updated_at=core.now())
            rec.setdefault("status", "created")
            os.makedirs(core.sent_dir(lg), exist_ok=True)
            core.save_json(rpath, rec)

    def create():
        return lg.create_doc(node, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        link = main_document(lg, dict(rec), None, "#%s 的主文档下" % item_id, create, remember)
    except core.WriteMaybeDone as e:
        raise core.LedgerError("%s\n上传 %s 没有拿到确定的结果（回执 %s）。先在网页上看 #%s 的主文档下有没有这份文档：有的话把它的链接直接写进 "
                          "--set %s=<文档链接>；确认没有，删掉这份回执再重跑" % (e, rel, receipt, item_id, label))
    remember(status="registered")
    if lg.mode == "apply":
        print("已上传“%s”：%s" % (title, link))
    return link


def attach_documents(lg, item_id, it, wid, sets):
    """--set pairs with every local file given for a link field uploaded and replaced by its link."""
    return [(ref, {"field_key": v["field_key"], "field_value": attach(lg, item_id, it, wid, v["field_key"], v["upload"])}
             if "upload" in v else v) for ref, v in sets]


def current_node(nodes, item_id, wanted=None):
    """The node an item is at. Flows are straight lines, so one node is in progress."""
    doing = [n for n in nodes if n["status"] == "doing"]
    if wanted:
        hit = next((n for n in nodes if wanted in (n["key"], n["name"])), None)
        if not hit:
            raise core.LedgerError("#%s 的流程里没有节点“%s”；有：%s" % (item_id, wanted, "、".join("%s（%s）" % (n["name"], n["key"]) for n in nodes)))
        if hit["status"] != "doing":
            raise core.LedgerError("#%s 的“%s”%s，现在走到的是“%s”" % (
                item_id, hit["name"], NODE_STATUS.get(hit["status"], hit["status"]), "、".join(n["name"] for n in doing) or "流程已经走完"))
        return hit
    if not doing:
        raise core.LedgerError("#%s 没有进行中的节点：流程已经走完，或者工作项已终止" % item_id)
    if len(doing) > 1:
        raise core.LedgerError("#%s 有 %d 个节点同时在进行（%s）：用 --node 指明确认哪一个" % (
            item_id, len(doing), "、".join("%s（%s）" % (n["name"], n["key"]) for n in doing)))
    return doing[0]


def is_scribes(lg, wid, node):
    return lg.contract.role(core.role_slot(lg, wid, node))["who"] == "scribe"


def check_by(lg, wid, it, node, by, what):
    label, allowed = core.responsible(lg, wid, it, node)
    if is_scribes(lg, wid, node):
        return label
    if not by:
        raise core.LedgerError("“%s”这一步的负责方是%s：加 --by <提出请求的会话名>；研究员交代的写 --by 研究员" % (node["name"], label))
    if not allowed(by):
        raise core.LedgerError("“%s”这一步的负责方是%s，%s 不能请书记员%s；退回请求方，让负责的会话来提" % (node["name"], label, by, what))
    return label


def clock(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def check_sign(lg, task_id, decision_id, what, since=None):
    """A signature is a decision that is decided, approved and made for this task. since (ms)
    is when the step it is used on began: a decision made before that was made for an
    earlier step, so one decision passes one gate once."""
    gate = lg.contract.flow["sign_gate"]
    did = core.item_id_of(decision_id)
    it = lg.item(did)
    if it is None:
        return did
    dtype = lg.spec.term("wi", gate["type"])
    if it.get("type") != gate["type"]:
        raise core.LedgerError("%s要填一条%s工作项的 ID；#%s 是%s" % (what, dtype, did, lg.spec.term("wi", it["type"]) if it.get("type") else "别的类型"))
    problems = []
    states = [lg.state(gate["type"], s) for s in gate["states"]]
    if it["state"] not in states:
        problems.append("状态是“%s”，要是%s" % (it["state"], "或".join("“%s”" % s for s in states)))
    approved = lg.spec.term("sign", gate["approved"])
    result = lg.text_of(it, gate["result"])
    if result != approved:
        problems.append("%s是%s，要是“%s”" % (lg.spec.field_name(gate["result"]), "“%s”" % result if result else "空的", approved))
    task = (lg.text_of(it, gate["task"]) or "").strip().lstrip("#")
    if task != str(task_id):
        problems.append("%s是%s，不是 #%s" % (lg.spec.field_name(gate["task"]), " #" + task if task else "空的", task_id))
    if since and not problems:
        decided = (lg.flow_nodes(did) or [{}])[0].get("finished_at")
        if decided and decided < since:
            problems.append("它是 %s 决定的，#%s 现在这一步是 %s 开始的；一条决定只用在它决定时所在的那一步，请研究员就现在这一步重新决定" % (
                clock(decided), task_id, clock(since)))
    if problems:
        raise core.LedgerError("%s用的%s #%s 不合格：%s" % (what, dtype, did, "；".join(problems)))
    return did


def raw_text(value):
    """A field value read with `workitem get`, as the operation records write values: a list of strings."""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [x for v in value for x in raw_text(v)]
    if isinstance(value, dict):
        for k in ("value", "timestamp", "id", "user_key"):
            if value.get(k) not in (None, ""):
                return [str(value[k])]
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    return [str(value)]


def frozen_changes(lg, wid, item_id, it, nodes):
    """Changes to the frozen fields after the signing step was last finished, as lines.

    The signing step is the node whose form requires the contract's sealed_by field;
    what it and the nodes before it require is frozen from the moment it was finished.
    Two things are compared. The operation records name every change made since, on
    the web page as well; but a record takes a while to show up. So the value each
    field had at the signing, taken from the records before it, is also compared with
    the value the item has now.
    """
    freeze = lg.contract.flow["freeze"]
    seal = (lg.field_keys.get(wid) or {}).get(freeze["sealed_by"])
    idx = next((i for i, n in enumerate(nodes) if seal in [r["field_key"] for r in n["required"]]), None)
    if seal is None or idx is None or not nodes[idx].get("finished_at"):
        return []
    since = nodes[idx]["finished_at"]
    frozen = {r["field_key"]: r for n in nodes[:idx + 1] for r in n["required"]}
    history = {}
    for rec in sorted(lg.op_records(item_id, "field_mod") or [], key=lambda r: r.get("operation_time") or 0):
        for c in rec.get("record_contents") or []:
            key = (c.get("object") or {}).get("object_value") if isinstance(c, dict) else None
            if key in frozen:
                history.setdefault(key, []).append((rec.get("operation_time") or 0, c))

    def show(values):
        return "、".join(map(str, values or [])) or "空"

    lines = []
    for key, f in frozen.items():
        name = f.get("name") or key
        after = [(t, c) for t, c in history.get(key, []) if t > since]
        before = [(t, c) for t, c in history.get(key, []) if t <= since]
        for t, c in after:
            stamp = datetime.datetime.fromtimestamp(t / 1000).astimezone().isoformat(timespec="seconds")
            lines.append("%s：%s，%s → %s" % (name, stamp, show(c.get("old")), show(c.get("new"))))
        if not after and before and f.get("field_type") != "user":
            signed, current = [str(x) for x in before[-1][1].get("new") or []], raw_text(it["fields"].get(key))
            if signed != current:
                lines.append("%s：签字时是 %s，现在是 %s（操作记录里还没有这次改动）" % (name, show(signed), show(current)))
    return lines


def confirm(lg, item_id, by=None, pairs=None, node=None, decision=None, wid_hint=None, option=None, executor=None):
    """Finish the step a work item is at.

    The space says what the step still lacks; --set values are written first. The
    scripts' own checks come before anything is written: who asks, the signature a
    required field refers to, and, before the step that requires the conclusion,
    whether a frozen field was changed after it was signed.
    On a decision's 决定 step two things may be said, both optional. option is the option the
    researcher picked (无 for none of the table): a record, kept in the receipt and a comment;
    nothing is released by it. executor hands the decision to a session in place of the package
    header's 执行方, for when the header says 研究员 and the researcher picked an option that needs
    nothing done by hand: the receipt's executor becomes the session and its researcher_action null,
    the space's two fields follow where it has them, and from then on the decision goes as one a
    session carries out. by=到期默认 confirms a reversible decision that waited the expiry time with
    the option its package names (confirm_by_default).
    """
    if by == BY_DEFAULT:
        if pairs or option or decision or executor:
            raise core.LedgerError("--by 到期默认 的决定内容和决定人由脚本照包里写的到期默认动作写：去掉 --set、--option、--executor 和 --decision")
        return confirm_by_default(lg, item_id, node)
    return confirm_step(lg, item_id, by, pairs, node, decision, wid_hint, option, executor=executor)


def confirm_by_default(lg, item_id, node=None):
    """`confirm <ID> --by 到期默认`: record the decision a reversible decision's package names as its
    default once it has waited the expiry time since registration. Only when all of these hold: it is
    still 待决定, it is in the local decision state, a session carries it out with nothing for the
    researcher to do by hand, it is reversible, the researcher never decided it before (one rolled back
    to 待决定 waits for the researcher, however long), the expiry time has passed, and its 到期默认动作
    names an option. 决定人 is the researcher (the field takes a person); 决定内容 starts with 到期默认：."""
    it, wid = core.typed_item(lg, item_id, "decision")
    if it is None:
        raise core.LedgerError("到期默认要先读这条决策现在的状态，离线做不了")
    if wid != "decision":
        raise core.LedgerError("--by 到期默认 只用在决策上：#%s 是%s" % (item_id, lg.spec.term("wi", wid)))
    pending = lg.state("decision", "pending")
    if it["state"] != pending:
        raise core.LedgerError("#%s 现在是“%s”，不在“%s”：到期默认只给还没定的决策" % (item_id, it["state"], pending))
    entry = core.load_gates(lg.root)["pending"].get(str(item_id))
    if not entry:
        raise core.LedgerError("本地决策状态里没有 #%s：它不是决策队列来的，或者是 1.0.4 以前登记的。到期默认要照登记时写下的可逆性和到期默认动作办："
                          "请发出方按新模板写下一版，书记员 revise 以后再看" % item_id)
    if entry.get("executor") == core.RESEARCHER or entry.get("researcher_action"):
        raise core.LedgerError("#%s 的执行方是研究员（或者要研究员亲手做事），到期继续等研究员，不走到期默认" % item_id)
    if entry.get("reversible") is not True:
        raise core.LedgerError("#%s 是不可逆的决策（判据 %s），到期只能等研究员，不走到期默认" % (
            item_id, "、".join(str(n) for n in entry.get("criteria") or []) or "?"))
    back = pool.rolled_back(entry)
    if back:
        at = core.parse_stamp(back)
        raise core.LedgerError("#%s 定过一次，又回滚到了待决定（%s）：不再走到期默认，一直等研究员，他来谈时排在清单最前" % (
            item_id, at.strftime("%Y-%m-%d %H:%M") if at else back))
    hours, source, note = core.expiry_hours(lg.root)
    if note:
        print(note)
    due = pool.expires_at(entry, hours)
    if not due:
        raise core.LedgerError("本地决策状态里 #%s 的登记时间读不出来，没法算到期：请书记员 revise 一次重新记下" % item_id)
    if core.now_at() < due:
        waited = (core.now_at() - due).total_seconds() / 3600 + hours
        raise core.LedgerError("#%s 登记了 %s，还没满到期时长 %s（%s）：%s 以后才能按到期默认确认" % (
            item_id, hours_text(waited), hours_text(hours),
            "org/decisions.md" if source != "默认" else "默认值", due.strftime("%Y-%m-%d %H:%M")))
    option = entry.get("default_option")
    picked = next((o for o in entry.get("options") or [] if o.get("name") == option), None)
    if not picked:
        raise core.LedgerError("#%s 的到期默认动作是“%s”，没有指向选项表里的选项：到期继续等研究员" % (item_id, entry.get("default") or "空的"))
    content = "到期默认：%s%s（登记后 %s内研究员没有决定）" % (option, "：" + picked["approach"] if picked.get("approach") else "",
                                                 hours_text(hours))
    confirm_step(lg, item_id, core.RESEARCHER, ["决定内容=" + content, "决定人=" + core.RESEARCHER], node, None, "decision",
                 by_default=(content, picked))
    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, content), write=True)


def decided_note(choice, executor=None, packaged=None):
    """What a comment records about how 决定 was confirmed: the option picked with the irreversible actions it
    lists (choice, from --option), and the session the decision was handed to in place of the package header's
    执行方 (executor, from --executor; packaged is what the header said). "" when neither was said."""
    parts = []
    if choice and choice.get("name") == orgspec.NOTHING:
        parts.append("研究员没有选选项表里的选项")
    elif choice:
        listed = "；".join(a.get("text") or a.get("name") or "" for a in orgspec.option_actions(choice))
        parts.append("研究员选的选项：%s%s" % (choice["name"], "（%s：%s）" % (orgspec.IRREVERSIBLE_COLUMN, listed) if listed else ""))
    if executor:
        parts.append("%s改成 %s（包头写的是%s），“%s”记“%s”" % (
            orgspec.EXECUTOR, executor, packaged or "空的", orgspec.RESEARCHER_ACTION, orgspec.NOTHING))
    return "；".join(parts)


def confirm_step(lg, item_id, by=None, pairs=None, node=None, decision=None, wid_hint=None, option=None, by_default=None,
                 executor=None):
    it, wid = core.typed_item(lg, item_id, wid_hint)
    spec = lg.spec
    sets = parse_sets(lg, wid, pairs)
    pending = ((lg.states.get("decision") or {}).get("pending") or {}).get("name")
    deciding = wid == "decision" and it is not None and bool(pending) and it.get("state") == pending
    if it is not None and not deciding:                 # offline nothing can be read: commands only
        for flag, value, how in (("--option", option, "选的选项用 workitem.py comment 记一条评论"),
                                 ("--executor", executor, "要改执行方，先 workitem.py rollback %s --to 决定 --reason '<原因>' --by 研究员，"
                                                          "再带 --executor 确认“决定”" % item_id)):
            if value is not None:
                raise core.LedgerError("%s 只用在确认决策的“决定”这一步：#%s 是%s，现在是“%s”%s" % (
                    flag, item_id, spec.term("wi", wid), it["state"],
                    "；这一步已经确认过（比如研究员在网页上定的），" + how if wid == "decision" else ""))
    choice = chosen_option(decision_options(lg, item_id), option, item_id) if deciding and not by_default else None
    if by_default:
        choice = by_default[1]
    who = changed_executor(lg, executor, item_id)
    rec = core.receipt_of(lg, item_id) if deciding else {}
    packaged = core.carried_out_by(rec, lg.text_of(it, "sender")) if deciding else None
    if who and who == rec.get("executor"):
        who = None                                      # the package header's own 执行方: nothing changes
    handed = core.executor_fields(lg, wid, who, orgspec.NOTHING) if who else []
    if {f["field_key"] for f in handed} & {v["field_key"] for _, v in sets}:
        raise core.LedgerError("%s用 --executor 改：去掉 --set 里的“%s”和“%s”" % (orgspec.EXECUTOR, orgspec.EXECUTOR, orgspec.RESEARCHER_ACTION))
    note = decided_note(choice, who, packaged) if not by_default else ""
    if (lg.field_keys.get(wid) or {}).get("blocked_reason") in [v["field_key"] for _, v in sets]:
        raise core.LedgerError("阻塞原因用 update 写，不随确认节点写")
    nodes = lg.flow_nodes(item_id)
    if nodes is None:                                   # offline: nothing can be read, show the commands
        if sets or handed:
            sets = attach_documents(lg, item_id, it, wid, sets)
            lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                      json.dumps([v for _, v in sets] + handed, ensure_ascii=False), write=True)
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", node or "<当前节点>",
                  "--action", "confirm", write=True)
        return
    cur = current_node(nodes, item_id, node)
    if core.is_last(cur) and len(nodes) > 1:
        raise core.LedgerError("“%s”是最后一步，要先把主文档移到归档节点：用 workitem.py archive %s" % (cur["name"], item_id))
    check_by(lg, wid, it, cur, by, "确认这一步")
    given = {v["field_key"]: v["field_value"] for _, v in sets}
    lacking = lg.unfinished(item_id, cur["key"]) or []
    missing = [lg.label(wid, f.get("key")) for f in lacking if f.get("class") == "field" and given.get(f.get("key"), "") == ""]
    missing += [lg.label(wid, k) for k, v in given.items() if v == "" and k in {r["field_key"] for r in cur["required"]}
                and lg.label(wid, k) not in missing]
    other = sorted({str(f.get("class")) for f in lacking if f.get("class") != "field"})
    if missing or other:
        parts = []
        if missing:
            parts.append("要先填：%s（用 --set <字段名>=<值> 传）" % "、".join(missing))
        if other:
            parts.append("空间还要求别的东西（%s），命令行填不了：请研究员在网页上看这个节点" % "、".join(other))
        raise core.LedgerError("#%s 的“%s”还不能确认。%s" % (item_id, cur["name"], "；".join(parts)))
    for slot, key in core.sign_slots(lg, wid, cur):
        value = given.get(key) or core.plain(it["fields"].get(key))
        check_sign(lg, item_id, value, "“%s”" % spec.field_name(slot), cur.get("started_at"))
    freeze = lg.contract.flow["freeze"]
    if (lg.field_keys.get(wid) or {}).get(freeze["checked_before"]) in {r["field_key"] for r in cur["required"]}:
        changed = frozen_changes(lg, wid, item_id, it, nodes)
        if changed and not decision:
            raise core.LedgerError("#%s 签字以后，冻结的字段被改过：\n  %s\n这一步先不确认。请研究员决定：退回到签字那一步或之前，改完重新签字；"
                              "或者认可这些改动，写成决策包，批准后重跑并加 --decision <决策工作项 ID>" % (item_id, "\n  ".join(changed)))
        if changed:
            check_sign(lg, item_id, decision, "认可冻结字段的改动", cur.get("started_at"))
            print("提醒：冻结的字段在签字后改过（%d 处），研究员已用决策 #%s 认可。" % (len(changed), core.item_id_of(decision)))
    if sets or handed:
        sets = attach_documents(lg, item_id, it, wid, sets)     # every check has passed: local files become documents now
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([v for _, v in sets] + handed, ensure_ascii=False), write=True)
    lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", cur["key"], "--action", "confirm", write=True)
    if lg.mode == "apply":
        after = lg.flow_nodes(item_id)
        done = next(n for n in after if n["key"] == cur["key"])
        if done["status"] != "finished":
            raise core.LedgerError("确认后回读，“%s”还是%s" % (cur["name"], NODE_STATUS.get(done["status"], done["status"])))
        state = (lg.item(item_id) or {}).get("state")
        doing = [n for n in after if n["status"] == "doing"]
        print("确认：#%s “%s”完成，现在到“%s”，状态“%s”。" % (
            item_id, cur["name"], "、".join(n["name"] for n in doing) or "流程末尾", state))
        for n in doing:                                    # the scribe tells whoever the next step belongs to
            print("“%s”这一步的负责方：%s。" % (n["name"], core.responsible(lg, wid, it, n)[0]))
        if deciding:
            settle(lg, item_id, choice, by_default and by_default[0], who)
            if by_default:
                print("到期默认：照选项“%s”办，执行完请书记员归档。" % choice["name"])
            if note:                                       # the record of the choice: the receipt has it, a comment says it
                kept = "回执" if rec else ""                 # registered elsewhere: no receipt here
                try:
                    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, note), write=True)
                    print("%s。已记进%s评论。" % (note, kept + "和" if kept else ""))
                except core.LedgerError as e:
                    print("%s。%s评论没写上（%s），用 workitem.py comment %s --text '%s' 补。" % (
                        note, "已记进%s；" % kept if kept else "", str(e)[:160], item_id, note))
        if wid == "decision" and state == lg.state(wid, "decided"):   # and the decision's 执行方 what was decided
            carried = who or core.carried_out_by(core.receipt_of(lg, item_id), lg.text_of(it, "sender"))
            if carried:
                print("执行方：%s。" % carried)
    else:
        print("预览：会确认 #%s 的“%s”（%s）。" % (item_id, cur["name"], cur["key"]))
        if note:
            lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, note), write=True)


def rollback(lg, item_id, to, reason, by=None, decision=None, wid_hint=None):
    """Send a work item back to a step it has finished.

    Who decides: on the main line the researcher, with an approved decision; elsewhere
    whoever is responsible for the step the item is at (the sender who does not accept
    a delivery, the receiver who hands a request back). When the item only waits for the
    scribe to archive it, whoever is responsible for the step it goes back to. The space
    keeps the values of the undone steps, so their required fields are cleared: the
    steps have to be done again before they can be confirmed again. Fields the new-item
    page requires are the item's own and stay.
    """
    it, wid = core.typed_item(lg, item_id, wid_hint)
    if not (reason or "").strip():
        raise core.LedgerError("回滚要写原因：--reason <为什么退回>")
    nodes = lg.flow_nodes(item_id)
    if nodes is None:
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", to, "--action", "rollback",
                  "--rollback-reason", reason, write=True)
        return
    target = next((n for n in nodes if to in (n["key"], n["name"])), None)
    if not target:
        raise core.LedgerError("#%s 的流程里没有节点“%s”；有：%s" % (item_id, to, "、".join("%s（%s）" % (n["name"], n["key"]) for n in nodes)))
    if target["status"] != "finished":
        raise core.LedgerError("只能退回到已完成的节点；“%s”%s" % (target["name"], NODE_STATUS.get(target["status"], target["status"])))
    doing = [n for n in nodes if n["status"] == "doing"]
    if wid in lg.contract.flow.get("rollback_needs_decision", []):
        if not decision:
            raise core.LedgerError("%s的回退由研究员决定：退到哪一步、为什么，写成决策包，研究员批准后重跑并加 --decision <决策工作项 ID>" % lg.spec.term("wi", wid))
        since = max([n.get("started_at") or 0 for n in doing] or [n.get("finished_at") or 0 for n in nodes]) or None
        reason = "%s（研究员决定 #%s）" % (reason.strip(), check_sign(lg, item_id, decision, "回退", since))
    elif decision:
        raise core.LedgerError("%s的退回不走决策：由这一步的负责方提，加 --by <会话名>；研究员交代的写 --by 研究员" % lg.spec.term("wi", wid))
    elif doing:
        check_by(lg, wid, it, target if is_scribes(lg, wid, doing[0]) else doing[0], by, "回滚")
    elif by != core.RESEARCHER:
        raise core.LedgerError("#%s 的流程已经走完，重新打开要研究员交代：加 --by 研究员" % item_id)
    start = nodes.index(target)
    end = max([nodes.index(n) for n in doing] or [len(nodes) - 1])
    kept = {c["field_key"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required")}
    clear = {}
    for n in nodes[start:end + 1]:
        for r in n["required"]:
            old = core.plain(it["fields"].get(r["field_key"]))
            if r["field_key"] not in kept and old is not None:
                clear[r["field_key"]] = (r.get("name") or r["field_key"], old)
    lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", target["key"], "--action", "rollback",
              "--rollback-reason", reason, write=True)
    if clear:
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([{"field_key": k, "field_value": ""} for k in clear], ensure_ascii=False), write=True)
    applied = lg.mode == "apply"
    print(("回滚：#%s 退回到“%s”，原因：%s" if applied else "预览：会把 #%s 退回到“%s”，原因：%s") % (item_id, target["name"], reason))
    for name, old in clear.values():
        print("  %s %s，原值：%s" % ("清空" if applied else "会清空", name, old if len(old) <= 120 else old[:120] + "…"))
    if lg.mode == "apply":
        after = lg.flow_nodes(item_id)
        if next(n for n in after if n["key"] == target["key"])["status"] != "doing":
            raise core.LedgerError("回滚后回读，“%s”不在进行中" % target["name"])
        state = (lg.item(item_id) or {}).get("state")
        print("现在状态“%s”。被退回的各步重做后再确认。" % state)
        if wid == "decision" and state == ((lg.states.get("decision") or {}).get("pending") or {}).get("name"):
            reopen(lg, item_id)


def reopen(lg, item_id):
    """A decision back at 待决定. Its receipt forgets how it was decided and keeps when it came back
    (rolled_back_at); a 执行方 the confirmation had changed (confirm --executor) goes back to what the package
    header says, in the receipt and in the space's two fields. One from the decision queue waits again in
    the local decision state, with its options and frozen actions from the receipt, marked rolled_back_at:
    it was decided once, so it takes no default from now on and waits for the researcher (its expiry still
    counts from its registration, for the entry's list)."""
    rec = core.receipt_of(lg, item_id)
    stamp = core.now()
    values = {"rolled_back_at": stamp, "option": None, "decided_by": None, "decided_at": None}
    packaged = rec.get("as_packaged") if isinstance(rec.get("as_packaged"), dict) else None
    if packaged:
        values.update(executor=packaged.get("executor"), researcher_action=packaged.get("researcher_action"), as_packaged=None)
    queued = bool(rec.get("queue")) and rec.get("options") is not None
    if queued:
        def change(gates):
            old = gates["pending"].get(str(item_id)) or {}
            gates["pending"][str(item_id)] = core.pending_entry(
                item_id, dict(rec, **values), old.get("registered_at") or rec.get("registered_at") or stamp, stamp)
        core.save_gates(lg, change, keep=item_id)
    core.update_receipt(lg, item_id, **values)
    if queued:
        print("本地决策状态：#%s 回到待决定，冻结的动作重新生效；它定过一次，不再走到期默认，一直等研究员。" % item_id)
    if packaged:
        back = "%s改回包头写的%s" % (orgspec.EXECUTOR, packaged.get("executor") or "（照包头）")
        fields = core.executor_fields(lg, "decision", packaged.get("executor"), packaged.get("researcher_action") or orgspec.NOTHING)
        try:
            if fields:
                lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields", json.dumps(fields, ensure_ascii=False), write=True)
            print(back + "。")
        except core.LedgerError as e:
            print("提醒：回执里的%s，空间里的字段没写上（%s）：用 workitem.py update %s --set 补。" % (back, str(e)[:160], item_id))


def update(lg, item_id, pairs, wid_hint=None):
    """Write field values. 阻塞原因 carries its date: setting it sets 阻塞开始日期 to today
    unless given, clearing it clears both."""
    it, wid = core.typed_item(lg, item_id, wid_hint)
    sets = parse_sets(lg, wid, pairs)
    keys = lg.field_keys.get(wid) or {}
    by_key = {v["field_key"]: v for _, v in sets}
    reason, since = keys.get("blocked_reason"), keys.get("blocked_since")
    if reason in by_key and since and since not in by_key:
        if by_key[reason]["field_value"] == "":
            sets.append(("blocked_since", {"field_key": since, "field_value": ""}))
        else:
            sets += parse_sets(lg, wid, ["blocked_since=%s" % datetime.date.today().isoformat()])   # local date
    sets = attach_documents(lg, item_id, it, wid, sets)
    lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
              json.dumps([v for _, v in sets], ensure_ascii=False), write=True)


SESSION_TAG = re.compile(r"\s*\[[A-Za-z]+\+[0-2]\]")


def comment(lg, item_id, text):
    if not SESSION_TAG.match(text):
        text = "%s %s" % (lg.tag, text)
    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", text, write=True)


def archive(lg, item_id, moved=False):
    """Move the main document's subtree to 归档文档/<序列>, then finish the last step of the
    flow. With moved=True the document is already there and only the step is finished."""
    it = lg.item(item_id)
    url = lg.doc_url(item_id, it)
    wid = it.get("type") if it else None
    seq_cn = lg.text_of(it, "sequence")
    seq = next((s for s, cn, _ in lg.spec.sequences() if cn == seq_cn), None)
    if it is not None and not seq:
        raise core.LedgerError("认不出 #%s 属于哪个序列（“序列”字段是 %s），没法决定归档到哪" % (item_id, seq_cn or "空的"))
    last, archived = None, None
    if it is not None:
        if not wid:
            raise core.LedgerError("认不出 #%s 的类型（type_key %s）；先跑 setup.py feishu --live --apply 重读类型" % (item_id, it.get("type_key")))
        archived = lg.state(wid, lg.contract.flow["archive_node"]["state"])
        if wid == "decision" and it["state"] in core.system_states(lg.spec):     # terminated on the web page
            gone = drop_pending(lg, item_id)
            print("#%s 是“%s”，不用归档。%s%s" % (item_id, it["state"], "本地决策状态里移除了它：冻结解除。" if gone else "",
                                              owner_note(lg, item_id)))
            return
        if it["state"] == archived:
            if wid == "decision" and drop_pending(lg, item_id):
                print("本地决策状态里移除了 #%s：冻结解除。" % item_id)
            print("#%s 已经是“%s”，不用再归档。%s" % (item_id, archived, owner_note(lg, item_id)))
            return
        nodes = lg.flow_nodes(item_id)
        last = next((n for n in nodes if core.is_last(n)), None)
        if not last or last["status"] != "doing":
            # A work item made by mistake, or withdrawn before its work began, is not archived, and its steps are
            # not confirmed to get it there: the researcher terminates it on the web page (the researcher,
            # 2026-11-20). The message says how to ask, since this is where a session learns that it must.
            raise core.LedgerError("#%s 还没走到最后一步“%s”：现在在“%s”。前面的步骤确认完再归档。\n这条工作项要是建错了、或者不做了，不要为了归档去确认前面的步骤，"
                              "也不用写决策包：运行 python3.12 -I .claude/solo-lab/bin/todo.py add --for %s --terminate --text '<为什么不要它了>'，"
                              "记进研究员任务清单，由研究员在网页上终止" % (
                item_id, (last or {}).get("name") or "归档", "、".join(n["name"] for n in nodes if n["status"] == "doing") or "流程之外", item_id))
    key = "archive/%s" % (seq or "<序列>")
    if not moved:
        n = lg.nodes.get(key) if (lg.nodes.get(key) or {}).get("node_token") else None
        if not n and lg.mode != "offline":
            raise core.LedgerError("ids.json 里没有归档节点 %s；先跑 ensure-node %s" % (key, key))
        node = lg.node_of(url)
        if not node:
            raise core.LedgerError("#%s 的主文档不是知识库文档，没法移动" % item_id)
        lg.lark("wiki", "+move", "--node-token", node, "--target-parent-token", (n or {}).get("node_token") or "<%s 的节点>" % key,
                write=True)
    try:
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", last["key"] if last else "<最后一个节点>",
                  "--action", "confirm", write=True)
        if lg.mode == "apply":
            state = (lg.item(item_id) or {}).get("state")
            if state != archived:
                raise core.LedgerError("确认后回读状态是“%s”，不是“%s”" % (state, archived))
    except core.LedgerError as e:
        if lg.mode == "apply" and not moved:
            raise core.LedgerError("%s\n主文档已经移到 %s 下；修好后运行 workitem.py archive %s --moved --apply，只确认最后一步" % (e, key, item_id))
        raise
    if lg.mode != "apply":
        print("预览：会%s确认 #%s 的最后一步。" % ("" if moved else "把主文档整棵子树移到 %s 下，再" % key, item_id))
    elif moved:
        print("归档：#%s 的最后一步已确认（主文档此前已移到 %s 下）。" % (item_id, key))
    else:
        print("归档：#%s 的主文档整棵子树移到 %s 下（移动的是原节点，文档链接不变），最后一步已确认。" % (item_id, key))
    if wid == "decision" and drop_pending(lg, item_id):
        print("本地决策状态里移除了 #%s：冻结解除。" % item_id)
    note = owner_note(lg, item_id)
    if note:
        print(note)


def main(argv=None):
    ap, sub = core.parser("写智能体组织的工作项")
    for name in ("register", "reply"):
        p = sub.add_parser(name); p.add_argument("draft"); p.add_argument("--apply", action="store_true")
        p.add_argument("--doc", help="上次建文档没有确定结果时：已建好的文档链接，或 new 重新建")
        if name == "register":
            p.add_argument("--source", help="来源工作项的 ID")
            p.add_argument("--task", help="所属研究任务的 ID；空间里只有一条没归档的研究任务时不用写")
            p.add_argument("--set", action="append", help="字段=值；字段写名字、约定里的标识或 field_key")
            p.add_argument("--resume", action="store_true")
            p.add_argument("--existing", metavar="ID", help=argparse.SUPPRESS)      # gone with 1.0.10: EXISTING_GONE
    p = sub.add_parser("confirm"); p.add_argument("id")
    p.add_argument("--by", help="请书记员确认这一步的会话名；研究员交代的写 研究员；到期默认 表示照决策包的到期默认动作确认")
    p.add_argument("--set", action="append"); p.add_argument("--node", help="有几个节点同时进行时指明哪一个")
    p.add_argument("--decision", help="冻结的字段在签字后改过时：研究员认可这些改动的决策工作项 ID")
    p.add_argument("--option", help="决策的“决定”这一步：研究员选的选项名，记进回执和评论；表里的都没选写 无")
    p.add_argument("--executor", metavar="会话名", help="决策的“决定”这一步：把执行方改成这个会话")
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("rollback"); p.add_argument("id")
    p.add_argument("--to", required=True, help="退回到哪个已完成的节点：节点 ID 或名字")
    p.add_argument("--reason", required=True)
    p.add_argument("--by", help="请书记员回滚的会话名；研究员交代的写 研究员")
    p.add_argument("--decision", help="研究员批准回退的决策工作项 ID；研究任务的回退必填")
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("update"); p.add_argument("id"); p.add_argument("--set", action="append", required=True)
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("comment"); p.add_argument("id"); p.add_argument("--text", required=True); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("archive"); p.add_argument("id"); p.add_argument("--apply", action="store_true")
    p.add_argument("--moved", action="store_true", help="主文档已经移到归档节点下，只确认最后一步")
    return core.run(ap, argv, run_command)


def run_command(lg, args):
    if args.cmd == "register":
        if args.existing:
            raise core.LedgerError(EXISTING_GONE % core.item_id_of(args.existing))
        core.also_to_router(lg, register(lg, args.draft, args.source, pairs=args.set, resume=args.resume, doc=args.doc, task=args.task))
    elif args.cmd == "reply":
        reply(lg, args.draft, args.doc)
        core.also_to_router(lg, (core.Draft(lg.spec, args.draft).linked or [None])[0])
    elif args.cmd == "confirm":
        confirm(lg, args.id, args.by, args.set, args.node, args.decision, args.type, args.option, args.executor)
        core.also_to_router(lg, args.id)
    elif args.cmd == "rollback":
        rollback(lg, args.id, args.to, args.reason, args.by, args.decision, args.type)
        core.also_to_router(lg, args.id)
    elif args.cmd == "update":
        update(lg, args.id, args.set, args.type)
    elif args.cmd == "comment":
        comment(lg, args.id, args.text)
    elif args.cmd == "archive":
        archive(lg, args.id, args.moved)
        core.also_to_router(lg, args.id)


if __name__ == "__main__":
    sys.exit(main())
