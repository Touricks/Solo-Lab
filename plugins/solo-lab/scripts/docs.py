#!/usr/bin/env python3
"""Write package documents (Feishu Docs). The commands print what they would do first; nothing is
written without --apply. Every session may run this script: a document is written by the session that
answers for it, and each command holds the session it runs in against the document's owner
(ledger_core.must_be). Nothing here writes a work item: what a new version changes on its work item is
printed as the request to send to the scribe.

    docs.py upload <draft>                  the document of a new work item, before the scribe registers it:
                                            created under its node, its link kept in an upload receipt
    docs.py relink <ID>                     once the item exists: its ID into the 关联工作项 line and the
                                            title of its main document
    docs.py revise <ID> <draft> --note N    the next version of a 需求包 or 决策包: the main document's
                                            opening up to 状态包 replaced in place, a line in 交接记录
    docs.py status-block <ID> --file F      replace the 状态包 section (block_replace)
    docs.py append <ID> --file F            append to 交接记录 (first line carries the session tag)
    docs.py ensure-node <key> | all         create knowledge-base nodes such as feedback or archive/alg

Work items are written with workitem.py, the ledger is read with ledger.py; what the three share is in
ledger_core.py.
"""
import datetime
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import project_paths  # noqa: E402
import space  # noqa: E402
import ledger_core as core  # noqa: E402

RUN = "python3.12 -I .claude/solo-lab/bin/"


def first_sheet_roles(draft):
    """The scribe uploads and relinks one document that is nobody's: the research-task sheet init writes
    (ledger_core.FIRST_TASK), which it registers while the pool opens, before any session has a say."""
    return (SCRIBE_ROLE,) if draft and os.path.normpath(draft) == os.path.normpath(core.FIRST_TASK) else ()


def upload(lg, path, source=None, doc=None):
    """Create the document of a new work item, before the work item exists (1.1.0: a document is written by
    the session that answers for it; the scribe registers the item afterwards and puts the link into it).

    The draft is checked as register checks it; the session is the draft's sender, or the +2 of its sequence
    for a sheet whose sender is no session. The document goes under the node its type has (a package in the
    执行 relation: under the main document of the item it comes from, --source), titled without a work item ID,
    its 关联工作项 line still a placeholder: `docs.py relink` fills both once the item exists. The receipt
    packages/sent/upload-<draft file>.json keeps the link and a digest of what was uploaded: the same command
    run again creates no second document; when the draft has changed since and is not registered yet, the
    document takes the draft's text and its title (overwrite, the link stays). When docs +create timed out or
    answered without a link, the next run stops until --doc says what to do: --doc <链接> takes over the
    document found on the web, --doc new creates another one. Returns the /wiki/ link."""
    spec = lg.spec
    kind = core.sheet_kind(path)
    d = core.Sheet(spec, path, kind) if kind else core.Draft(spec, path)
    wid = kind or core.work_item_type(d)
    core.check_new(d, kind)
    names, owner_seq, owner = core.doc_owner(lg, None, d)
    rel = os.path.relpath(path, lg.root)
    core.must_be(lg, "上传这份文档", names=names, roles=first_sheet_roles(rel), seq=owner_seq, why="这份草稿的发出方是 %s，请它来上传" % owner)
    registered = core.find_receipt(lg, path) if lg.mode != "offline" else None
    if registered and registered.get("work_item_id"):
        raise core.LedgerError("这份草稿已经登记成 #%s（回执 %s），不要重复登记；改了内容的，用 docs.py revise %s <新草稿> --note '<改了什么>' 换主文档" % (
            registered["work_item_id"], registered["_file"], registered["work_item_id"]))
    core.link_line(d.text, "#0", "0")                 # a 关联工作项 line that is no placeholder stops here
    seq = core.new_item_sequence(lg, d, kind, wid)
    source = core.item_id_of(source or d.source) if (source or d.source) else None
    parent = core.parent_node(lg, wid, seq, d.rel, source)
    if kind == "research_task":
        for line in core.run_conditions_hints(d):
            print(line)
    title, body = core.new_document(spec, d, kind)
    digest = core.text_digest(title, body)
    rpath, rec = core.upload_receipt(lg, path) if lg.mode != "offline" else (None, {})

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=rel, updated_at=core.now(), by=(lg.caller or {}).get("name") or core.RESEARCHER)
            rec.setdefault("status", "created")
            os.makedirs(core.sent_dir(lg), exist_ok=True)
            core.save_json(rpath, rec)

    then = "接着给书记员发【%s】，写上草稿 %s%s；登记回执到了以后运行 %sdocs.py relink <工作项 ID> --apply，把工作项 ID 写进文档。" % (
        spec.term("msg", "register_request"), rel, "和来源工作项 #%s" % source if source and not d.source else "", RUN)
    known = rec.get("doc") if not doc else None
    if known and rec.get("sha256") == digest:
        print("这份草稿已经上传过，没有再传：%s\n%s" % (known, then))
        return known
    if known:                                         # the draft changed after it was uploaded: the document follows it
        lg.lark("docs", "+update", "--doc", known, "--command", "overwrite", "--doc-format", "markdown", "--content", "-",
                write=True, stdin=body)
        if rec.get("title") != title:
            retitle(lg, known, title)
        remember(sha256=digest, title=title)
        print(("草稿在上传以后改过：文档已经换成现在的内容，链接不变：%s\n%s" if lg.mode == "apply"
               else "预览：草稿在上传以后改过，会把文档换成现在的内容，链接不变：%s\n%s") % (known, then))
        return known

    def create():
        return lg.create_doc(parent, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        url = core.main_document(lg, dict(rec), doc, "节点 %s " % parent, create, remember)
    except core.WriteMaybeDone as e:
        raise core.WriteMaybeDone("%s\n建文档没有拿到确定的结果：先在网页上节点 %s 下核对。建好了的，运行 docs.py upload %s --doc <文档链接> --apply；"
                                  "确认没建成的，运行 docs.py upload %s --doc new --apply" % (e, parent, rel, rel))
    remember(status="uploaded", sha256=digest, title=title, parent=parent, source=source, type=wid)
    print(("上传：文档建在节点 %s 下：%s\n%s" if lg.mode == "apply" else "预览：会把文档建在节点 %s 下（%s）。\n%s") % (parent, url, then))
    return url


def retitle(lg, url, title, whole=None):
    """Give a document, and with it its node in the knowledge space, this title: the title is a block of its
    own whose id is the document's. A refusal is no reason to stop what else is being written: it is printed
    with the title to set by hand. Returns True when the title was written or is this one already."""
    if whole is None:
        whole = lg.lark("docs", "+fetch", "--doc", url, "--detail", "with-ids")
    if whole is None:                                 # a preview, or offline
        return False
    doc_id, current = title_block(whole)
    if current is not None and current.strip() == title:
        return True
    if not doc_id or current is None:
        print("提醒：读不出文档的标题块，标题没有改；请在网页上把它改成“%s”。" % title)
        return False
    try:
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--block-id", doc_id,
                "--doc-format", "xml", "--content", "-", write=True, stdin="<title>%s</title>" % html.escape(title, quote=False))
    except core.LedgerError as e:
        print("提醒：主文档的标题没有改成，还是“%s”；请在网页上把它改成“%s”。原因：%s" % (current.strip(), title, str(e).splitlines()[-1]))
        return False
    print("改名：主文档和它的知识库节点的标题从“%s”改成“%s”。" % (current.strip(), title))
    return True


SCRIBE_ROLE = "pmo-scribe"                    # the skeleton of knowledge-base nodes is the scribe's: no session's document
STATUS_ROLES = ("pmo-router", "pmo-p2")       # who keeps a research task's 状态包, besides the document's owner


def last_block_before(xml, end_id):
    """Id of the last top-level block before end_id in a range fragment.

    --start-block-id and --end-block-id must be siblings. Lists come back as a
    <ul>/<ol> without an id around <li id=...>, so a list ends at its last <li>;
    an <excerpt top-block-id=...> wrapper stands for its top-level block.
    """
    root = ET.fromstring(xml)
    children = list(root)
    idx = next((i for i, c in enumerate(children) if c.get("id") == end_id or c.get("top-block-id") == end_id), None)
    if not idx:
        raise core.LedgerError("在“状态包”和“交接记录”之间找不到内容块")
    prev = children[idx - 1]
    if prev.get("top-block-id"):
        return prev.get("top-block-id")
    if prev.get("id"):
        return prev.get("id")
    items = [c for c in prev if c.get("id")]
    if prev.tag in ("ul", "ol") and items:
        return items[-1].get("id")
    raise core.LedgerError("“交接记录”前面那一块（<%s>）没有 block id；请在网页上替换状态包" % prev.tag)


def status_block(lg, item_id, path):
    it = lg.item(item_id)
    names, seq, owner = core.doc_owner(lg, it)
    core.must_be(lg, "换 #%s 的状态包" % item_id, names=names, roles=STATUS_ROLES, seq=seq, why="状态包归 %s、路由或入口写" % owner)
    url = lg.doc_url(item_id, it)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.lstrip().startswith("## 状态包"):
        content = "## 状态包\n\n" + content
    heads = core.heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    end_head = next((b for b, t in heads if t == "交接记录"), None)
    if lg.mode == "offline":
        start, end = start or "<状态包标题的 block_id>", "<交接记录前一块的 block_id>"
    else:
        if not start or not end_head:
            raise core.LedgerError("主文档里找不到“状态包”或“交接记录”标题")
        rng = lg.lark("docs", "+fetch", "--doc", url, "--scope", "range", "--start-block-id", start,
                      "--end-block-id", end_head, "--detail", "with-ids")
        end = last_block_before(core.doc_content(rng), end_head) if rng is not None else "<交接记录前一块的 block_id>"
    lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", start,
            "--end-block-id", end, "--doc-format", "markdown", "--content", "-", write=True, stdin=content)


def whole_document(content):
    """The answer to a read of a whole document as one element: lark-cli gives the title and the top-level
    blocks side by side, with nothing around them."""
    return content if content.lstrip().startswith("<fragment") else "<fragment>%s</fragment>" % content


def opening_blocks(xml, end_id):
    """(first block, last block before end_id) of a document read whole (whole_document): the blocks
    `docs.py revise` replaces. A list is its <li> blocks; an <excerpt> stands for its top block."""
    root = ET.fromstring(xml)
    children = [c for c in root if c.tag != "title"]
    if not children or children[0].get("id") == end_id or children[0].get("top-block-id") == end_id:
        raise core.LedgerError("主文档在“状态包”标题之前没有内容块")
    head = children[0]
    first = head.get("top-block-id") or head.get("id")
    if not first and head.tag in ("ul", "ol"):
        first = next((c.get("id") for c in head if c.get("id")), None)
    if not first:
        raise core.LedgerError("主文档开头那一块（<%s>）没有 block id；请在网页上替换主文档开头" % head.tag)
    return first, last_block_before(xml, end_id)


def title_block(whole):
    """(block id, text) of the title of a document read whole. The title is a block of its own whose id is the
    document's; replacing it renames the document and, with it, its node in the knowledge space."""
    m = re.search(r"<title(?:\s+[^>]*?\bid=\"([^\"]+)\")?[^>]*>(.*?)</title>", core.doc_content(whole), re.S)
    found = core._find(whole, ("document_id",)) if isinstance(whole, dict) else None
    return (m.group(1) if m and m.group(1) else found), (html.unescape(m.group(2)) if m else None)


def revision_body(d, item_id, link):
    """The new opening of a main document: the draft without its title, its 关联工作项 naming the item with
    the link to it (also when the draft carries the bare ID an earlier revise wrote back into it), and
    nothing from 状态包 on (the document keeps its own 状态包 and 交接记录)."""
    body = core.link_line(d.body_without_title(), "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id, relink=True)
    m = re.search(r"^## (?:状态包|交接记录)\s*$", body, re.M)
    return (body[:m.start()] if m else body).rstrip("\n") + "\n"


def revise(lg, item_id, path, note):
    """Put the next version of a package in place: the main document's opening, up to its 状态包 heading,
    becomes the draft (block_replace, the same write as status-block), and 交接记录 gets a line
    “<标签> 第 N 版（日期）：<改了什么>”. The document keeps its link and identity; earlier versions stay in
    the document's history. Checked first: the draft passes the package check, its type and relation are the
    item's, its version is the previous one plus one, a decision is still 待决定 and any other item is not
    closed; the session is the document's owner (doc_owner). A draft copied from the main document has no
    title line: the item keeps the title it has. Then the document's opening is replaced and the line is added
    to 交接记录, unless it is there already; the receipt and the local decision state follow last. A step that
    fails says what is already written; the same command run again goes on from there and adds no second
    line. The work item is not written here (1.1.0: only the scribe writes work items). What this version
    changes on it is printed as one `workitem.py update` for the scribe: the item's name, 紧急程度 (a
    request's 截止时间), a decision's 执行方 and 研究员要做的事 where the space has those fields, a sheet's
    发出方 and 序列, 来源工作项; a value the item holds already is left out, one the field does not take is
    skipped with a reminder. Nothing is sent to the researcher: the queue shows the new version when they
    come."""
    spec = lg.spec
    if not (note or "").strip():
        raise core.LedgerError("修订要写改了什么：--note '<改了什么>'，它会记进交接记录")
    item_id = core.item_id_of(item_id)
    sheet = core.sheet_kind(path)
    if sheet and sheet != "research_task":
        raise core.LedgerError("反馈的主文档没有状态包，不用 revise：要补充的写成新的反馈，或者在工作项下加评论")
    d = core.Sheet(spec, path, sheet) if sheet else core.Draft(spec, path)
    if d.pid == "delivery":
        raise core.LedgerError("交付包不是主文档，不用 revise：改好后用 reply 重新交，版本号加 1")
    wid = "research_task" if sheet else core.work_item_type(d)
    head = d.text.split("\n## ", 1)[0]
    m = core.LINK_LINE.search(head)
    value = m.group(2).strip() if m else ""
    if m and not core.is_placeholder(value) and not re.fullmatch(r"#?%s|\[#?%s\]\(\S+\)" % (item_id, item_id), value):
        raise core.LedgerError("修订稿的“关联工作项”写的是“%s”，要修订的是 #%s：写“#%s”，或者照新草稿写“待建”" % (value, item_id, item_id))
    it = lg.item(item_id)
    names, seq, owner = core.doc_owner(lg, it, d)
    core.must_be(lg, "修订 #%s 的主文档" % item_id, names=names, seq=seq, why="这份文档归 %s 写，请它来修订" % owner)
    rec = core.receipt_of(lg, item_id)
    if not core.title_of(d.text):                      # copied from the main document, which has no title line: the title stays
        d.task = rec.get("title") or re.split(r"[：:]", (it or {}).get("name") or "", 1)[-1].strip() or d.task
    if it is not None and sheet:
        cn = spec.term("wi", wid)
        if it.get("type") != wid:
            raise core.LedgerError("#%s 是%s，修订稿是%s单：类型不变才能修订" % (
                item_id, spec.term("wi", it["type"]) if it.get("type") else "别的类型", cn))
        if it.get("flow") and d.flow and it["flow"] != d.flow:
            raise core.LedgerError("#%s 走的是“%s”流程，修订稿写的是“%s”：流程在新建时选定，改不了；要换流程，另开一条%s" % (
                item_id, it["flow"], d.flow, cn))
        if it["state"] in core.closed_states(lg):
            raise core.LedgerError("#%s 已经是“%s”：工作项关闭以后不再修订" % (item_id, it["state"]))
        nodes = lg.flow_nodes(item_id) if lg.mode != "offline" else []
        if nodes and nodes[0]["status"] != "doing":          # allowed from 1.0.10 on; the researcher answers for it
            at = next((n["name"] for n in nodes if n["status"] == "doing"), None)
            print("提醒：#%s 已经过了第一步“%s”%s：后面各步已经定下的文档不会跟着%s单变。" % (
                item_id, nodes[0]["name"], "，现在在“%s”" % at if at else "", cn))
    elif it is not None:
        requests = ("same_seq", "cross_request")
        relation = "（关系“%s”）" % spec.term("rel", d.rel) if d.rel else ""
        if it.get("type") != wid and it.get("type") in requests and wid in requests:
            raise core.LedgerError("修订稿登记出来是%s%s，#%s 是%s：包类型和关系都不变才能修订；要改关系，另写一份登记" % (
                spec.term("wi", wid), relation, item_id, spec.term("wi", it["type"])))
        if it.get("type") != wid:
            raise core.LedgerError("#%s 是%s，修订稿是%s：包类型不变才能修订；要换成别的包，另写一份登记" % (
                item_id, spec.term("wi", it["type"]) if it.get("type") else "别的类型", spec.pkg(d.pid)))
        registered = lg.text_of(it, "relation") if wid == "same_seq" else None
        if registered and d.rel and registered != spec.term("rel", d.rel):
            raise core.LedgerError("修订稿的关系是“%s”，#%s 登记的是“%s”：包类型和关系都不变才能修订；要改关系，另写一份登记" % (
                spec.term("rel", d.rel), item_id, registered))
        if wid == "decision":
            pending = lg.state("decision", "pending")
            if it["state"] != pending:
                raise core.LedgerError("#%s 现在是“%s”：决策只在“%s”时能修订；研究员已经定了的，要改另写一条决策" % (item_id, it["state"], pending))
        elif it["state"] in core.closed_states(lg):
            raise core.LedgerError("#%s 已经是“%s”：工作项关闭以后不再修订" % (item_id, it["state"]))
    previous = int(rec.get("version") or 1)
    if sheet:                                     # a sheet has no 版本 line: its versions are counted here
        d.version, date = previous + 1, datetime.date.today().isoformat()
    else:
        if d.version != previous + 1:
            raise core.LedgerError("修订稿是第 %s 版，#%s 现在是第 %d 版：修订稿的“版本”写第 %d 版" % (d.version, item_id, previous, previous + 1))
        date = orgspec.read_version(d.header.get(orgspec.VERSION))[1]
    name, title = core.naming(spec, d, sheet) if core.title_of(d.text) else (None, None)
    title = title.replace("<工作项 ID>", str(item_id)) if title else None
    kinds = core.receipt_kind(spec, wid, d)
    asks = []                                     # (field name, value) for the scribe: what the item does not hold yet

    def ask(ref, raw):
        try:
            f = lg.field(wid, ref)
            lg.value(wid, ref, raw)               # a value the field does not take is not asked for
        except (core.LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（之后可以请书记员用 update 补）：%s" % (ref, e))
            return
        if it is None or not holds(it, f, raw):
            asks.append((f.get("name") or ref, str(raw).strip()))

    if wid == "decision" and kinds.get("urgency"):
        ask("urgency", kinds["urgency"])
    elif wid != "decision" and "due" in lg.contract.slots(wid, "fields") and core.utc_noon_ms(d.header.get("回复期限", "")):
        ask("due", d.header["回复期限"])
    if wid == "decision":                         # 执行方 and 研究员要做的事, where the space has fields of those names
        values = {orgspec.EXECUTOR: orgspec.executor_of(spec, d.header.get(orgspec.EXECUTOR)),
                  orgspec.RESEARCHER_ACTION: (d.header.get(orgspec.RESEARCHER_ACTION) or "").strip()}
        for field in core.HEADER_FIELDS:
            if lg.key_of(wid, field) and values[field]:
                ask(field, values[field])
    if sheet:                                     # the task takes the sender and the sequence of this version: the
        if d.author:                              # first sheet is the one init wrote
            ask("sender", d.author)
        if "sequence" in lg.contract.slots(wid, "fields"):
            ask("sequence", spec.term("seq", d.sender_seq or "pmo"))
    if name and it is not None and it.get("name") and it["name"] != name:  # the title changed: the item is renamed
        asks.append((lg.label(wid, "name"), name))                         # by the scribe, its document below
    if d.source and "source_item" in lg.contract.slots(wid, "fields"):   # the upstream item this version names
        ask("source_item", core.item_id_of(d.source))
    body = revision_body(d, item_id, lg.item_url(wid, item_id))
    url = lg.doc_url(item_id, it)
    heads = core.heading_ids(lg, url)
    status = next((b for b, t in heads if t == "状态包"), None)
    retitle_to, whole = None, None
    if lg.mode == "offline":
        status = status or "<状态包标题的 block_id>"
        first, last = "<主文档第一块的 block_id>", "<状态包前一块的 block_id>"
    else:
        if not status:
            raise core.LedgerError("#%s 的主文档里找不到“状态包”标题：revise 只换带状态包的主文档" % item_id)
        # The whole document, not a range that names only its end: lark-cli (1.0.98) answers such a range with the
        # end block alone, and the id of the first block is known from no smaller read.
        whole = lg.lark("docs", "+fetch", "--doc", url, "--detail", "with-ids")
        first, last = opening_blocks(whole_document(core.doc_content(whole)), status) if whole is not None else (
            "<主文档第一块的 block_id>", "<状态包前一块的 block_id>")
        doc_id, doc_title = title_block(whole) if whole is not None else (None, None)
        retitle_to = title if title and doc_id and doc_title is not None and doc_title.strip() != title else None
    done, logged = [], False                      # what is written already, should a later step fail
    try:
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", first, "--end-block-id", last,
                "--doc-format", "markdown", "--content", "-", write=True, stdin=body)
        done.append("主文档开头已经换成第 %d 版" % d.version)
        if retitle_to:                            # the document's title, and its node's: one block, the document's own
            retitle(lg, url, retitle_to, whole)
        logged = revision_logged(lg, url, heads, d.version, date)
        if not logged:
            lg.lark("docs", "+update", "--doc", url, "--command", "append", "--doc-format", "markdown", "--content", "-", write=True,
                    stdin="%s 第 %d 版（%s）：%s\n" % (lg.tag, d.version, date, note.strip()))
    except core.LedgerError as e:
        if lg.mode != "apply" or not done and isinstance(e, core.WriteMaybeDone):
            raise
        if not done:
            raise core.LedgerError("%s\n主文档还没改；修好后重跑同一条命令" % e)
        raise type(e)("%s\n%s；回执和本地决策状态还是第 %d 版。修好后重跑同一条命令接着做，交接记录不会多出一行" % (
            e, "，".join(done), previous))
    if lg.mode == "apply":
        rel = os.path.relpath(path, lg.root)
        revisions = list(rec.get("revisions") or []) + [{"version": d.version, "date": date, "note": note.strip(), "draft": rel, "at": core.now()}]
        queue = rec["queue"] if "queue" in rec else kinds.get("queue")
        values = dict(kinds, queue=queue, revisions=revisions)
        values.pop("type", None)
        if wid == "decision":
            values["as_packaged"] = None                       # the new version's header says who carries it out
        if not core.update_receipt(lg, item_id, **values):          # registered elsewhere: this draft's receipt holds it now
            core.save_receipt(lg, path, item_id, "registered", dict(kinds, revisions=revisions), doc=url)
        if wid == "decision" and queue:
            first_seen = rec.get("registered_at") or started_at(lg, item_id)
            core.hold(lg, item_id, dict(kinds, queue=queue), first_seen, rec.get("rolled_back_at") or rec.get("reopened_at"))
        drifted = core.write_back(path, d.text, item_id)             # the draft now names its item: it is not registered again
        if drifted:
            core.update_receipt(lg, item_id, draft_changed=True)
            print("提醒：" + drifted)
        print("修订：#%s 的主文档开头换成第 %d 版（文档链接不变），%s。%s" % (
            item_id, d.version, "交接记录里已经有这一版的一行，没有再加" if logged else "交接记录追加了一行",
            "本地决策状态已按新版本更新。" if wid == "decision" and queue else ""))
    else:
        print("预览：会把 #%s 的主文档开头换成第 %d 版，交接记录追加一行。" % (item_id, d.version))
    lg.field_asks = asks
    if asks:
        print(("这一版改了记在工作项上的内容，工作项只由书记员写：给书记员发【%s】，请它运行下面这条命令。\n%s" if lg.mode == "apply"
               else "预览：这一版改了记在工作项上的内容，修订以后要请书记员运行（发【%s】）：\n%s") % (
                   spec.term("msg", "register_request"), update_command(item_id, asks)))
    return d.version


def holds(it, f, raw):
    """Does the work item hold this value in this field already? Compared as the text `workitem get` shows;
    a date by its day."""
    current = it.get("name") if f["field_key"] == "name" else (it.get("fields") or {}).get(f["field_key"])
    if f.get("field_type") == "date" and isinstance(current, dict) and current.get("timestamp") is not None:
        return str(current["timestamp"]) == str(core.utc_noon_ms(str(raw)))
    return (core.plain(current) or "") == str(raw).strip()


def update_command(item_id, asks):
    """The `workitem.py update` that writes asks: [(field name, value)]."""
    return "python3.12 -I .claude/solo-lab/bin/workitem.py update %s %s --apply" % (
        item_id, " ".join("--set %s" % core._quote("%s=%s" % (name, value)) for name, value in asks))


def revision_logged(lg, url, heads, version, date):
    """Does 交接记录 already have the line of this revision (“<标签> 第 N 版（日期）：…”)? A run that failed
    after the line was added, or whose answer never came, is run again: the line is not added twice. Only the
    line revise writes counts: one that starts with the scribe's tag, at the start of a line or of a block
    (the answer may be Markdown or blocks in tags). Another session's note that names the version
    (“[Infra+2] 准备出第 2 版（日期）：…”) is no such line. False when the section cannot be read, offline included."""
    log = next((b for b, t in heads if t == "交接记录"), None)
    if not log or lg.mode == "offline":
        return False
    try:
        data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section", "--start-block-id", log)
    except core.LedgerError:
        return False
    tag = "".join(("\\\\?" if c in "[]+" else "") + re.escape(c) for c in lg.tag)      # a Markdown export may escape the brackets
    line = r"(?:^|>)[ \t>*-]*%s\s*第\s*%d\s*版\s*[（(]\s*%s\s*[）)]\s*[：:]" % (tag, version, re.escape(date or ""))
    return bool(re.search(line, core.doc_content(data), re.M))


def started_at(lg, item_id):
    """When a work item's first step began, as an ISO time: when it was created. None when unknown."""
    try:
        nodes = lg.flow_nodes(item_id) or []
    except core.LedgerError:
        return None
    ms = nodes[0].get("started_at") if nodes else None
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds") if ms else None


def append(lg, item_id, path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    first = next((l for l in content.splitlines() if l.strip()), "")
    if not re.search(r"\[[A-Za-z]+\+[0-2]\]", first):
        raise core.LedgerError("交接记录第一行要带会话标签，比如 [Alg+1]")
    core.must_be(lg, "追加 #%s 的交接记录" % item_id, any_claim=True)
    mine = (lg.caller or {}).get("tag")
    if mine and mine not in first:
        raise core.LedgerError("交接记录第一行的会话标签要是自己的 %s：每个会话只记自己的交接" % mine)
    url = lg.doc_url(item_id)
    lg.lark("docs", "+update", "--doc", url, "--command", "append", "--doc-format", "markdown",
            "--content", "-", write=True, stdin=content)


LIST_ITEM = re.compile(r'<li\b[^>]*?\bid="([^"]+)"[^>]*>(.*?)</li>', re.S)


def relink(lg, item_id):
    """Put the work item into its main document: its ID with the link to it into the 关联工作项 line, and
    “工作项 <ID>｜” in front of the title. The author runs it once the scribe has registered the item (the
    document is uploaded before the item exists, so it carries neither); the next revision does the same for a
    document nobody relinked. The block is replaced in place: the document keeps its identity and link."""
    item_id = core.item_id_of(item_id)
    it = lg.item(item_id)
    names, seq, owner = core.doc_owner(lg, it)
    core.must_be(lg, "改 #%s 的主文档" % item_id, names=names, roles=first_sheet_roles(core.receipt_of(lg, item_id).get("draft")),
                 seq=seq, why="这份文档归 %s 写" % owner)
    url = lg.doc_url(item_id, it)
    data = lg.lark("docs", "+fetch", "--doc", url, "--detail", "with-ids")
    if data is None:
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", "<“关联工作项”那一行的 block_id>",
                "--end-block-id", "<同一个 block_id>", "--doc-format", "markdown", "--content", "-", write=True,
                stdin="- 关联工作项：#%s\n" % item_id)
        return
    hit = None
    for m in LIST_ITEM.finditer(core.doc_content(data)):
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if re.match(r"关联工作项\s*[：:]", text):
            hit = (m.group(1), text)
            break
    if not hit:
        raise core.LedgerError("#%s 的主文档开头找不到“关联工作项”一行" % item_id)
    block, text = hit
    link = lg.item_url(it["type"], item_id) if it.get("type") else None
    line = "- " + text
    fixed = core.link_line(line, "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id)
    if fixed == line:
        print("#%s 的主文档已经写着它的 ID，不用改。" % item_id)
    else:
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", block, "--end-block-id", block,
                "--doc-format", "markdown", "--content", "-", write=True, stdin=fixed + "\n")
        print("已把 #%s 写进主文档的“关联工作项”一行。" % item_id)
    _, current = title_block(data)
    if current is not None and not re.match(r"工作项\s*#?%s\s*｜" % re.escape(str(item_id)), current.strip()):
        retitle(lg, url, "工作项 %s｜%s" % (item_id, core.ITEM_IN_TITLE.sub("", current.strip())), data)


def existing_node(lg, space, parent, key, title):
    """(node_token, title) of a node already in the space for this key: directly under parent
    (the space root when parent is None), titled with the node's name. None when there is
    none; offline it is not looked up."""
    args = ["wiki", "+node-list", "--space-id", space, "--page-all"]
    if parent:
        args += ["--parent-node-token", parent]
    cache = getattr(lg, "_node_listing_cache", None)
    cache_key = (space, parent)
    data = cache.get(cache_key) if cache is not None else None
    if data is None:
        data = lg.lark(*args)
    if data is None:
        return None
    nodes = (data.get("data") or {}).get("nodes") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
    if not isinstance(nodes, list):
        raise core.LedgerError("wiki +node-list 的输出读不懂，没法确认已有没有这个节点：%s" % json.dumps(data, ensure_ascii=False)[:300])

    def same(t):
        return (t or "").split("：")[0].strip() == title

    hits = [n for n in nodes if isinstance(n, dict) and n.get("node_token") and same(n.get("title"))]
    if len(hits) > 1:
        raise core.LedgerError("知识空间里已经有 %d 个“%s”节点（%s）；在 feishu/ids.json 的 wiki.nodes.%s 里写明用哪一个" % (
            len(hits), title, "、".join(n["node_token"] for n in hits), key))
    if cache is not None:
        cache[cache_key] = {"data": {"nodes": list(nodes)}}
    return (hits[0]["node_token"], hits[0].get("title")) if hits else None


def ensure_node(lg, key):
    core.must_be(lg, "建知识库节点", roles=(SCRIBE_ROLE,), why="知识库的节点由书记员建")
    if (lg.nodes.get(key) or {}).get("node_token"):
        print("节点 %s 已有：%s" % (key, lg.nodes[key].get("node_token")))
        return
    head, _, seq = key.partition("/")
    title = lg.spec.term("node", head) if not seq else lg.spec.term("seq", seq)
    space = (lg.ids.get("wiki") or {}).get("space_id")
    if not space and lg.mode != "offline":
        raise core.LedgerError("ids.json 里没有知识空间 space_id；先建知识空间，再跑 space.py feishu --live --apply")
    parent = None
    if seq:
        parent = (lg.nodes.get(head) or {}).get("node_token")
        if not parent:
            raise core.LedgerError("ids.json 里的上一级节点 %s 没有 node_token；先跑 ensure-node %s" % (head, head))
    found = existing_node(lg, space, parent, key, title) if space else None
    if found:                                          # the space already has it: record it, do not create a second
        token, current = found
        record = {"title": current or title, "node_token": token, "obj_token": None}
        out = None
        print("知识空间里已经有节点 %s（%s），直接用，不再建。" % (key, token))
    else:
        args = ["wiki", "+node-create", "--space-id", space or "<space_id>", "--obj-type", "docx",
                "--node-type", "origin", "--title", title]
        if parent:
            args += ["--parent-node-token", parent]
        out = lg.lark(*args, write=True)
        record = None
    if lg.mode == "apply":
        if record is None:
            token = core._find(out, ("node_token",))
            if not token:
                raise core.LedgerError("节点建了，但输出里没有 node_token：%s；去网页上核对，不要重跑" % json.dumps(out, ensure_ascii=False)[:300])
            record = {"title": title, "node_token": token, "obj_token": core._find(out, ("obj_token",))}
        path = os.path.join(lg.root, "feishu", "ids.json")
        ids = core.load_json(path, {})
        ids.setdefault("wiki", {}).setdefault("nodes", {})[key] = record
        core.save_json(path, ids)
        lg.nodes[key] = record                         # a child created next in the same run finds it
        cache = getattr(lg, "_node_listing_cache", None)
        if cache is not None and out is not None and (space, parent) in cache:
            cache[(space, parent)]["data"]["nodes"].append(record)
        if out is not None:
            print("已建节点 %s：%s，写进 feishu/ids.json。" % (key, record["node_token"]))
        else:
            print("写进 feishu/ids.json。")


def ensure_all_nodes(lg):
    """The whole node skeleton: the top-level nodes first, then the enabled sequences' nodes
    under them. Nodes already recorded, or already in the space under the same title, are kept."""
    manifest = core.load_json(os.path.join(project_paths.runtime_dir(lg.root), "manifest.json"), {})
    lg._node_listing_cache = {}  # One listing per parent, only within this locked transaction.
    try:
        for key in lg.spec.node_keys(manifest.get("sequences") or None):
            head, _, seq = key.partition("/")
            if seq and not (lg.nodes.get(head) or {}).get("node_token"):
                print("节点 %s 要等上一级 %s 建好：加 --apply 时先建上一级，再建它。" % (key, head))
                continue
            ensure_node(lg, key)
    finally:
        del lg._node_listing_cache


def main(argv=None):
    ap, sub = core.parser("写智能体组织的任务包文档")
    p = sub.add_parser("upload"); p.add_argument("draft")
    p.add_argument("--source", help="来源工作项 ID：执行关系的需求包挂在它的主文档下面")
    p.add_argument("--doc", help="上次建文档没有拿到确定的结果时：已经建好的文档的链接，或者 new 重新建")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("revise"); p.add_argument("id"); p.add_argument("draft")
    p.add_argument("--note", required=True, help="这一版改了什么，记进交接记录")
    p.add_argument("--apply", action="store_true")
    for name in ("status-block", "append"):
        p = sub.add_parser(name); p.add_argument("id"); p.add_argument("--file", required=True); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("relink"); p.add_argument("id"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("ensure-node"); p.add_argument("key"); p.add_argument("--apply", action="store_true")
    return core.run(ap, argv, run_command)


def run_command(lg, args):
    if args.cmd == "upload":
        upload(lg, args.draft, args.source, args.doc)
    elif args.cmd == "revise":
        revise(lg, args.id, args.draft, args.note)
        if lg.mode == "apply" and core.receipt_of(lg, str(args.id).lstrip("#")).get("type") == "cross_request":
            print("跨序列需求：把这次修订告诉路由 %s 一声（修订的就是它的不用再说）。" % lg.spec.rules["naming"]["fixed_sessions"]["pmo-router"])
    elif args.cmd == "status-block":
        status_block(lg, args.id, args.file)
    elif args.cmd == "append":
        append(lg, args.id, args.file)
    elif args.cmd == "relink":
        relink(lg, args.id)
    elif args.cmd == "ensure-node":
        if args.key == "all":
            ensure_all_nodes(lg)
        else:
            ensure_node(lg, args.key)


if __name__ == "__main__":
    sys.exit(main())
