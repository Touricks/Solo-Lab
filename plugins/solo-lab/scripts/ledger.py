#!/usr/bin/env python3
"""Read the task ledger (Feishu Project). For every session; nothing here writes.

    ledger.py get <ID> [--raw]              type, state, flow, name and the fields that have a value
    ledger.py next <ID>                     the step the item is at: who is responsible, what is still to fill in
    ledger.py query <name> [--sequence s]   queue | blocked | verifying | open | cross | tasks | recent | source:<ID>;
                                            queue: the decisions waiting on the researcher, each with its stage
                                            (待决定, or 待执行 while the researcher, its 执行方, has still to
                                            carry it out) and 紧急程度; blocked with 阻塞原因; both with the
                                            main document, and a decision's 回复期限, 执行方 and 研究员要做的事,
                                            kept in the registration receipt
    ledger.py mql <name> [--sequence s]     print the MQL only
    ledger.py trace <ID>                    follow 来源工作项 upwards
    ledger.py status <ID>                   print the 状态包 section of the main document
    ledger.py settled [--task ID] [--sequence s] [--keyword w] [--all]
                      [--cards ID,ID] [--into <草稿>]
                                            what is settled: the decisions the researcher made and the research
                                            tasks that have a conclusion, a line each; with --cards, a card for
                                            each ID named, cut from the work items and their documents; with
                                            --into, written into the draft's section 已有的决定和结论 (a local file:
                                            nothing is written to the space)

Package documents are written with docs.py, work items with workitem.py; what the three share is in
ledger_core.py.
"""
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import pool  # noqa: E402
import ledger_core as core  # noqa: E402

def step_lines(lg, wid, it, node, nodes):
    """What `next` shows for the node an item is at."""
    label, _ = core.responsible(lg, wid, it, node)
    lines = ["  这一步：%s（%s），负责方：%s" % (node["name"], node["key"], label)]
    for r in node["required"]:
        filled = core.plain(it["fields"].get(r["field_key"])) is not None or r.get("value") not in (None, "", [])
        slot = next((s for s, k in (lg.field_keys.get(wid) or {}).items() if k == r["field_key"]), None)
        lines.append("    %s %s%s" % ("已填" if filled else "待填", r.get("name") or r["field_key"], "（%s）" % slot if slot else ""))
    if not node["required"]:
        lines.append("    没有必填项")
    for slot, _ in core.sign_slots(lg, wid, node):
        lines.append("    “%s”填一条研究员批准的决策工作项的 ID，确认前由脚本核对" % lg.spec.field_name(slot))
    after = [n["name"] for n in nodes if n["key"] in node["next"]]
    lines.append("  下一步：%s" % ("、".join(after) if after else "没有了，这是最后一步"))
    return lines


def next_step(lg, item_id):
    """`ledger.py next`: where an item is and what its step still needs. Reads only."""
    it, wid = core.typed_item(lg, item_id)
    if it is None:
        return None
    nodes = lg.flow_nodes(item_id)
    lines = ["#%s  %s%s  %s  %s" % (item_id, lg.spec.term("wi", wid), "·" + it["flow"] if it.get("flow") and len(lg.space_type(wid).get("flows") or {}) > 1 else "",
                                  it["state"], it["name"])]
    doing = [n for n in nodes if n["status"] == "doing"]
    if not doing:
        lines.append("  流程已经走完。" if all(n["status"] == "finished" for n in nodes) else "  没有进行中的节点：工作项可能已终止。")
    for node in doing:
        lines += step_lines(lg, wid, it, node, nodes)
        cmd = "archive %s" % item_id if core.is_last(node) else "confirm %s --by <会话名>" % item_id
        lines.append("  书记员推进：workitem.py %s" % cmd)
    reason = lg.text_of(it, "blocked_reason") if "blocked_reason" in lg.contract.slots(wid, "fields") else None
    if reason:
        lines.append("  阻塞中（%s 起）：%s" % ((lg.text_of(it, "blocked_since") or "?")[:10], reason))
    return "\n".join(lines)
SHOWN = (("urgency", "紧急程度"), ("due", "回复期限"), ("executor", orgspec.EXECUTOR),
         ("researcher_action", orgspec.RESEARCHER_ACTION), ("blocked_reason", "阻塞原因"), ("doc", "主文档"))
# A decision goes through three stages (the researcher, 2026-11-16): 待决定; 待执行, called 已决定 in a space
# made from template 2.1.0 or earlier (the scribe confirms 决定 as soon as the researcher decides and tells
# the 执行方, so every session knows the choice); 归档, which means it was carried out. The package header's
# 执行方 carries it out: the researcher, or a session, which asks the scribe to archive it as soon as it is
# done. query queue shows the stage in place of the state: every decision 待决定, and a decided one only
# while the researcher has still to carry it out.
STAGES = {"pending": "待决定", "decided": "待执行"}
PAST_DUE = "待决定（已到期，继续等研究员）"     # past its expiry time and no 到期默认 for it: first in the list


def past_due_waiting(lg):
    """IDs of the pending decisions past their expiry time (their registration plus the expiry time in
    org/decisions.md, as the inbox hook works it out) that keep waiting for the researcher: irreversible,
    carried out by the researcher, or decided once and rolled back to 待决定."""
    at, hours, out = core.now_at(), core.expiry_hours(lg.root)[0], set()
    for item_id, entry in core.load_gates(lg.root)["pending"].items():
        due = pool.expires_at(entry, hours)
        if due and at >= due and not pool.takes_default(entry):
            out.add(str(item_id))
    return out


def shown_fields(lg, name, rows):
    """Rows of the named queries queue and blocked, with their extra cells under the slot's name and
    what the registration receipt kept: the main document's link, and a decision's 回复期限; queue
    also a decision's 执行方 (carried_out_by: on a receipt older than that line, 研究员 when the
    researcher has something to do, nothing otherwise), its 研究员要做的事 (无 when the receipt says
    null, nothing on an older receipt) and its stage (STAGES). A decided one that a session carries out
    is left out of queue; one past its expiry time that keeps waiting for the researcher comes first, its stage
    PAST_DUE."""
    if name not in ("queue", "blocked"):
        return rows
    slots = {}
    for wid in lg.types:
        for slot in ("urgency", "blocked_reason"):
            key = lg.key_of(wid, slot)
            if key:
                slots[key] = slot
    decided = lg.state("decision", "decided") if name == "queue" else None
    late = past_due_waiting(lg) if name == "queue" else set()
    out = []
    for r in rows:
        for key in [k for k in r if k in slots]:
            r[slots[key]] = r.pop(key)
        rec = core.receipt_of(lg, r.get("work_item_id"))
        for key in ("due", "doc"):
            if rec.get(key) and key not in r:
                r[key] = rec[key]
        if name == "queue":
            who = core.carried_out_by(rec)
            if r.get("work_item_status") == decided and who != core.RESEARCHER:
                continue
            r["stage"] = STAGES["decided" if r.get("work_item_status") == decided else "pending"]
            if r["stage"] == STAGES["pending"] and str(r.get("work_item_id")) in late:
                r["stage"] = PAST_DUE
            if rec.get("version"):                         # a revision is not pushed: the list shows its number
                r["version"] = rec["version"]
            if who:
                r["executor"] = who
            if "researcher_action" in rec:
                r["researcher_action"] = rec["researcher_action"] or orgspec.NOTHING
        out.append(r)
    return sorted(out, key=lambda r: r.get("stage") != PAST_DUE)          # stable: the space's order otherwise


def brief_lines(lg, pending=False):
    """One line for each 简版决策 this project uploaded, the oldest first: what the researcher is asked, who wrote
    it, since when, its link; a decided one with the first line of his answer and whether it was promoted to a
    work item. They are read from the upload receipts (packages/sent/brief-*.json): a 简版决策 is no work item
    and the space knows nothing of it. pending keeps the ones he has not decided."""
    out = []
    for rec in core.brief_receipts(lg):
        decided = rec.get("status") == "decided"
        if pending and decided:
            continue
        head = "%s  %s  %s（%s）" % ("已定" if decided else "待定", rec.get("day") or "", rec.get("heading") or rec.get("title") or "", rec.get("by") or "?")
        if decided:
            item = (core.find_receipt(lg, os.path.join(lg.root, rec["package"])) or {}).get("work_item_id") if rec.get("package") else None
            up = "已升级成决策 #%s" % item if item else "要升级，决策包 %s 还没登记" % rec["package"] if rec.get("package") else \
                "要升级，决策包还没生成" if rec.get("promote") else "不升级"
            head += "  决定（%s）：%s  %s" % ((rec.get("decided_at") or "")[:10], rec.get("gist") or "", up)
        else:
            head += "  作者%s" % ("建议升级" if rec.get("suggested") else "建议不升级")
        out.append("%s  %s" % (head, rec.get("doc")))
    return out


def query(lg, name, sequence=None, as_json=False):
    if name == "briefs":                               # no work items: the 简版决策, decided or not
        lines = brief_lines(lg)
        print("\n".join(lines + ["共 %d 份简版决策，%d 份待定。" % (len(lines), len(brief_lines(lg, pending=True)))]))
        return lines
    rows = []
    for mql in core.mql_text(lg, name, sequence):
        rows += core.query_all(lg, mql)
    rows = shown_fields(lg, name, rows)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            extra = "  版本：第 %s 版" % r["version"] if r.get("version") else ""
            extra += "".join("  %s：%s" % (label, r[key] if isinstance(r[key], str) else core.plain(r[key]))
                             for key, label in SHOWN if r.get(key) not in (None, "", [], {}))
            print("#%s  %s  %s%s" % (r.get("work_item_id"), r.get("stage") or r.get("work_item_status", ""), r.get("name", ""), extra))
        if lg.mode != "offline":
            print("共 %d 项。" % len(rows))
        waiting = brief_lines(lg, pending=True) if name == "queue" else []
        if waiting:                                    # 1.1.2: what waits for the researcher without being a work item
            print("\n还没定的简版决策（不是工作项，不进决策队列；研究员来谈时列在决策清单后面）：\n" + "\n".join(waiting))
    return rows


def digest(lg, item_id):
    """`ledger.py get`: type, state, flow, name and the fields that have a value, by their names
    in the space; a field the contract names shows its slot too."""
    it = lg.item(item_id)
    if it is None:
        return None
    lg.people(it, item_id)
    wid = it["type"]
    flows = lg.space_type(wid).get("flows") or {} if wid else {}
    lines = ["#%s  %s%s  %s  %s" % (item_id, lg.spec.term("wi", wid) if wid else "?",
                                  "·" + it["flow"] if it.get("flow") and len(flows) > 1 else "", it["state"], it["name"])]
    if wid:
        slots = {k: s for s, k in (lg.field_keys.get(wid) or {}).items()}
        fields = [f for f in lg.space_type(wid).get("fields") or [] if str(f.get("field_key")).startswith("field_")]
        if not fields:                                   # no snapshot: at least the slots
            fields = [{"field_key": k, "name": lg.spec.field_name(s)} for k, s in slots.items()]
        for f in fields:
            text = core.plain(it["fields"].get(f["field_key"]))
            if text:
                lines.append("  %s%s：%s" % (f["name"], "（%s）" % slots[f["field_key"]] if f["field_key"] in slots else "", text))
    return "\n".join(lines)


def trace(lg, item_id):
    seen = []
    cur = str(item_id)
    while cur and cur not in seen and len(seen) < 20:
        seen.append(cur)
        if core.is_local(cur):                         # a task without a work item: its receipt names the item above it
            rec = core.local_task(lg, cur)[1]
            print("#%s  没建工作项的任务  %s  %s" % (cur, rec.get("state") or core.LOCAL_OPEN, rec.get("title") or ""))
            cur = str(rec.get("source") or "") or None
            continue
        it = lg.item(cur)
        if it is None:
            print("#%s（离线，无法回溯）" % cur)
            return
        kind = lg.spec.term("wi", it["type"]) if it["type"] else "?"
        print("#%s  %s  %s  %s" % (cur, kind, it["state"], it["name"]))
        nxt = lg.text_of(it, "source_item")
        cur = nxt.strip() if nxt else None


def status(lg, item_id):
    """The 状态包 section, found by its heading in the outline. The router matches
    messages without a work item ID against it, the item's name and its fields."""
    url = lg.doc_url(item_id)
    heads = core.heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    if lg.mode == "offline":
        start = start or "<状态包标题的 block_id>"
    if not start:
        raise core.LedgerError("主文档里找不到“状态包”标题")
    data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section",
                   "--start-block-id", start)
    if data is not None:
        print(core.doc_content(data) or json.dumps(data, ensure_ascii=False, indent=2))


# ------------------------------------------------------------ what is settled
# 结论清单 (the researcher, 2026-11-21): what was decided and concluded before, for a session that writes a decision
# package or opens a research task, and for the entry when the researcher asks. Three layers. The index: a line for
# every settled decision and concluded research task of a range, from the work items' fields; it is for picking, and
# says nothing more. A card, for the few picked (CARDS_MAX at a time): a decision's 认知层 and 背景 as its package
# wrote them, its 决定内容 as the work item holds it, the options that were not taken, what came after; a research
# task's fields, the 结论 section of its 结论文档 and what the researcher decided at its last step. The document:
# the link every card ends with. Nothing here says what a settled thing means for the question at hand: the
# session that reads it does.
SETTLED_MAX, CARDS_MAX, CUT = 60, 5, orgspec.BACKGROUND_MAX
SUPERSEDED_BY = re.compile(r"被 #(\d+) 取代")
TASK_FACTS = ("探索问题", "成功标准", "评测集版本", "规格版本", "总预算", "已用预算", "波动估计", "候选假设")
DECIDED, CARRIED_OUT = STAGES["decided"], "已执行"


def cut(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "……（下略，见原文）"


def settled_rows(lg, task=None, sequence=None, keyword=None):
    """What is settled, the newest work item first: [{id, kind (decision or task), state, name, gist, text,
    date, superseded_by}]. A decision's text is its 决定内容, its gist the first line of it, its date the day it
    was decided (from its receipt, '' when this project has none); a research task's gist is its 结论 or 去向.
    keyword keeps the rows whose name or text has it, case aside."""
    decided = lg.state("decision", "decided") if "decision" in lg.types else None
    rows = {}
    for kind, mql, cols in core.settled_mql(lg, task, sequence):
        for r in core.query_all(lg, mql):
            item_id = str(r.get("work_item_id"))
            got = {what: r.get(key) for key, what in cols.items()}
            got = {k: v if isinstance(v, str) else core.plain(v) for k, v in got.items()}
            row = rows.setdefault(item_id, {"id": item_id, "kind": kind, "name": r.get("name") or "", "text": "", "date": ""})
            if kind == "decision":
                text = (got.get("text") or "").strip()
                gone = SUPERSEDED_BY.search(text)
                row.update(state=DECIDED if r.get("work_item_status") == decided else CARRIED_OUT, text=text,
                           gist=next((l.strip() for l in text.splitlines() if l.strip()), ""),
                           date=(core.receipt_of(lg, item_id).get("decided_at") or "")[:10],
                           superseded_by=gone.group(1) if gone else None)
            else:
                said = ["%s：%s" % (label, got[what]) for what, label in (("conclusion", "结论"), ("where_next", core.WHERE_NEXT)) if got.get(what)]
                row.update(state=r.get("work_item_status") or "", gist="；".join(filter(None, [row.get("gist")] + said)))
    out = [r for r in rows.values() if not (task and r["kind"] == "task" and r["id"] != str(task))]
    if keyword:
        out = [r for r in out if keyword.lower() in ("%s\n%s\n%s" % (r["name"], r["text"], r.get("gist") or "")).lower()]
    return sorted(out, key=lambda r: -int(r["id"]))


def index_line(lg, row):
    if row["kind"] == "decision":
        gone = "（已被 #%s 取代）" % row["superseded_by"] if row.get("superseded_by") else ""
        return "#%s  %s  %s%s  %s  →  %s%s" % (row["id"], lg.spec.term("wi", "decision"), row["state"],
                                              "  " + row["date"] if row["date"] else "", row["name"], gone, cut(row["gist"], 60))
    return "#%s  %s  %s  %s  →  %s" % (row["id"], lg.spec.term("wi", "research_task"), row["state"], row["name"], row["gist"])


def doc_sections(lg, url, names):
    """{name: text} of the level-2 sections of a document that it has, as they are written, without their headings."""
    heads = core.heading_ids(lg, url)
    out = {}
    for name in names:
        start = next((b for b, t in heads if t == name), None)
        if not start:
            continue
        data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section", "--start-block-id", start)
        lines = core.doc_content(data).strip().splitlines() if data is not None else []
        if lines and lines[0].lstrip("#").strip() == name:
            lines = lines[1:]
        out[name] = "\n".join(lines).strip()
    return out


def decision_card(lg, item_id, it):
    decided, archived = lg.state("decision", "decided"), lg.state("decision", "archived")
    if it["state"] not in (decided, archived):
        raise core.LedgerError("#%s 现在是“%s”，还没定：卡片只出定下来的决策" % (item_id, it["state"]))
    rec = core.receipt_of(lg, item_id)
    url = lg.doc_url(item_id, it)
    try:
        parts = doc_sections(lg, url, ("认知层", orgspec.BACKGROUND))
    except core.LedgerError as e:                      # said in the card, not left out: without it the decision has no premise
        parts = {orgspec.BACKGROUND: "主文档读不到（%s），这条决定的前提看原文" % " ".join(str(e).split())[:120]}
    text = (lg.text_of(it, core.DECIDED_TEXT) or "").strip()
    how = "；".join(filter(None, [DECIDED if it["state"] == decided else CARRIED_OUT,
                                 "%s %s定" % ((rec.get("decided_at") or "")[:10], rec.get("decided_by") or "") if rec.get("decided_at") else ""]))
    lines = ["### #%s %s（%s）" % (item_id, it["name"], how), ""]
    if parts.get("认知层"):
        lines += ["要定的事：%s" % cut(parts["认知层"], 200), ""]
    if parts.get(orgspec.BACKGROUND):
        lines += ["背景：%s" % cut(parts[orgspec.BACKGROUND], CUT), ""]
    lines += ["决定：%s" % (cut(text, CUT) or "工作项上没有写决定内容，见原文"), ""]
    names = [o.get("name") for o in rec.get("options") or [] if o.get("name")]
    if names and rec.get("option"):
        rest = [n for n in names if n != rec["option"]]
        lines.append("选项：定的是“%s”%s。" % (rec["option"], "；没选的有%s" % "、".join("“%s”" % n for n in rest) if rest else ""))
    elif names:
        lines.append("选项：包里列了%s，没记下选的是哪个，看“决定”。" % "、".join("“%s”" % n for n in names))
    after = []
    gone = SUPERSEDED_BY.search(text)
    if gone:
        after.append("已被 #%s 取代，现在算数的是 #%s" % (gone.group(1), gone.group(1)))
    if rec.get("rolled_back_at"):
        after.append("%s 回滚到待决定过，上面是回滚以后重新定的" % rec["rolled_back_at"][:10])
    if after:
        lines.append("后来：%s。" % "；".join(after))
    lines.append("原文：%s" % url)
    return "\n".join(lines)


def task_card(lg, item_id, it):
    wid = "research_task"
    said = [(label, lg.text_of(it, ref)) for ref, label in (("conclusion", "结论"), (core.WHERE_NEXT, core.WHERE_NEXT))]
    said = ["%s：%s" % pair for pair in said if pair[1]]
    if not said:
        raise core.LedgerError("#%s 还没有结论：卡片只出有结论（正式实验）或有去向（探索）的研究任务" % item_id)
    lines = ["### #%s %s：%s（%s）" % (item_id, lg.spec.term("wi", wid), it["name"], "；".join(([it["flow"]] if it.get("flow") else []) + said)), ""]
    for name in TASK_FACTS:
        value = lg.text_of(it, name) if lg.key_of(wid, name) else None
        if value:
            lines.append("%s：%s" % (name, cut(value, 200)))
    links = [("主文档", lg.doc_url(item_id, it))] + [(n, lg.text_of(it, n)) for n in ("结论文档", "探索记录") if lg.key_of(wid, n)]
    report = dict(links).get("结论文档")
    try:
        summary = doc_sections(lg, report, ("结论",)).get("结论") if report else None
    except core.LedgerError:                           # the fields above stand on their own
        summary = None
    if summary:
        lines += ["", "结论：%s" % cut(summary, CUT)]
    signed = (lg.text_of(it, "next_step_signoff") or "").strip().lstrip("#") if lg.key_of(wid, "next_step_signoff") else ""
    if signed.isdigit():
        choice = lg.item(signed)
        text = lg.text_of(choice, core.DECIDED_TEXT) if choice else None
        if text:
            lines += ["", "下一步（决策 #%s）：%s" % (signed, cut(text, CUT))]
    lines += ["", "原文：%s" % "；".join("%s %s" % pair for pair in links if pair[1])]
    return "\n".join(lines)


def card(lg, item_id):
    it, wid = core.typed_item(lg, item_id)
    if wid == "decision":
        return decision_card(lg, item_id, it)
    if wid == "research_task":
        return task_card(lg, item_id, it)
    raise core.LedgerError("#%s 是%s：卡片只出决策和研究任务" % (item_id, lg.spec.term("wi", wid)))


def write_section(lg, path, body):
    """Put body in place of what the draft's section 已有的决定和结论 holds. The draft is one under packages/drafts/."""
    drafts = os.path.realpath(os.path.join(lg.root, "packages", "drafts"))
    real = os.path.realpath(path)
    if not real.startswith(drafts + os.sep):
        raise core.LedgerError("--into 只写 packages/drafts/ 下面的草稿：%s" % path)
    with open(real, encoding="utf-8") as f:
        text = "\n" + f.read()
    m = re.search(r"(\n## %s[ \t]*\n)(.*?)(?=\n## |\Z)" % re.escape(orgspec.SETTLED), text, re.S)
    if not m:
        raise core.LedgerError("草稿 %s 里没有“## %s”一节：照决策包模板加上这一节的标题，再运行" % (path, orgspec.SETTLED))
    rest = text[m.end(2):]
    with open(real, "w", encoding="utf-8") as f:
        f.write((text[:m.end(1)] + body.strip() + "\n" + rest)[1:])


def settled(lg, task=None, sequence=None, keyword=None, cards=None, into=None, everything=False):
    task = core.item_id_of(task) if task else None
    ids = list(dict.fromkeys(core.item_id_of(i) for i in re.split(r"[,，、\s]+", cards or "") if i))
    if len(ids) > CARDS_MAX:
        raise core.LedgerError("一次最多出 %d 张卡片，这里写了 %d 张：只挑和这次要定的事直接有关的" % (CARDS_MAX, len(ids)))
    rows = settled_rows(lg, task, sequence, keyword)
    if lg.mode == "offline":
        return rows
    if len(rows) > SETTLED_MAX and not (task or sequence or keyword or everything):
        raise core.LedgerError("定下来的决定和有结论的研究任务有 %d 条，超过 %d 条：加 --task、--sequence 或 --keyword 缩小范围；"
                               "确实要全部列出，加 --all" % (len(rows), SETTLED_MAX))
    stray = [i for i in ids if i not in {r["id"] for r in rows}]
    if stray:
        raise core.LedgerError("--cards 里的 %s 不在这次的范围里：卡片只出清单里有的。先不带 --cards 看一遍清单，或者放宽范围" % (
            "、".join("#" + i for i in stray)))
    index = [index_line(lg, r) for r in rows]
    if not ids and not into:
        print("\n".join(index + ["共 %d 项。" % len(rows)]))
        return rows
    scope = "、".join(filter(None, ["研究任务 #%s" % task if task else "",
                                   "%s序列" % lg.spec.term("seq", core.sequence_id(lg, sequence)) if sequence else "",
                                   "名称或内容里有“%s”的" % keyword if keyword else ""])) or "整个项目"
    today = datetime.date.today().isoformat()
    if not rows:
        body = "无（范围：%s；%s 查，还没有定下来的决定和有结论的研究任务）" % (scope, today)
    else:
        body = "\n".join(["%s%s；定下来的决定和有结论的研究任务共 %d 条（%s 查）。清单只用来看有哪些，依据看卡片和原文。" % (
            orgspec.SETTLED_SCOPE, scope, len(rows), today), ""] + ["- " + line for line in index])
        for i in ids:
            body += "\n\n" + card(lg, i)
    if into:
        write_section(lg, into, body)
        print("已写进 %s 的“%s”一节：清单 %d 行，卡片 %d 张。写完跑一遍包检查。" % (into, orgspec.SETTLED, len(rows), len(ids)))
    else:
        print(body)
    return rows


def main(argv=None):
    ap, sub = core.parser("读智能体组织的台账")
    p = sub.add_parser("get"); p.add_argument("id"); p.add_argument("--raw", action="store_true")
    p = sub.add_parser("next"); p.add_argument("id")
    p = sub.add_parser("query"); p.add_argument("name"); p.add_argument("--sequence"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("mql"); p.add_argument("name"); p.add_argument("--sequence")
    p = sub.add_parser("trace"); p.add_argument("id")
    p = sub.add_parser("status"); p.add_argument("id")
    p = sub.add_parser("settled")
    p.add_argument("--task", help="只看这条研究任务下的：它自己和所属研究任务是它的决策")
    p.add_argument("--sequence", help="只看这个序列的，写标识或中文名")
    p.add_argument("--keyword", help="只看名称或内容里有这个词的")
    p.add_argument("--all", action="store_true", help="不带范围时超过 60 条也全部列出")
    p.add_argument("--cards", help="要出卡片的工作项 ID，用逗号隔开，最多 5 个")
    p.add_argument("--into", help="把范围、清单和卡片写进这份草稿的“已有的决定和结论”一节")
    return core.run(ap, argv, run_command)


def run_command(lg, args):
    if args.cmd in ("get", "next", "status") and core.is_local(args.id):
        rec = core.local_task(lg, core.item_id_of(args.id))[1]     # a closed task (执行): no work item, a local receipt
        print(core.local_summary(rec))
        if args.cmd == "next":
            ref, by = rec["work_item_id"], rec.get("by") or "派活的会话"
            print({core.LOCAL_OPEN: "下一步：承接的会话交付，运行 docs.py upload <交付包草稿>（包头“关联工作项”写 #%s），再给 %s 发【交付 #%s】。" % (ref, by, ref),
                   core.LOCAL_DELIVERED: "下一步：%s 验收，运行 docs.py accept %s --result 通过|不通过 --note '<一句话>'。" % (by, ref),
                   core.LOCAL_ACCEPTED: "这件活已经验收，了结了。"}.get(rec.get("state") or core.LOCAL_OPEN, ""))
        elif args.cmd == "status":
            print("没建工作项的任务没有自己的状态包：看它上级工作项的，ledger.py status %s" % rec.get("source"))
    elif args.cmd == "get":
        if args.raw:
            print(json.dumps(lg.get(args.id, ["_all"]), ensure_ascii=False, indent=2))
        else:
            print(digest(lg, args.id) or "")
    elif args.cmd == "next":
        print(next_step(lg, args.id) or "")
    elif args.cmd == "query":
        query(lg, args.name, args.sequence, args.json)
    elif args.cmd == "mql":
        print("\n".join(core.mql_text(lg, args.name, args.sequence)))
    elif args.cmd == "trace":
        trace(lg, args.id)
    elif args.cmd == "status":
        status(lg, args.id)
    elif args.cmd == "settled":
        settled(lg, args.task, args.sequence, args.keyword, args.cards, args.into, args.all)


if __name__ == "__main__":
    sys.exit(main())
