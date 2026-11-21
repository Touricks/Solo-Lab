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

Package documents are written with docs.py, work items with workitem.py; what the three share is in
ledger_core.py.
"""
import json
import os
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


def query(lg, name, sequence=None, as_json=False):
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


def main(argv=None):
    ap, sub = core.parser("读智能体组织的台账")
    p = sub.add_parser("get"); p.add_argument("id"); p.add_argument("--raw", action="store_true")
    p = sub.add_parser("next"); p.add_argument("id")
    p = sub.add_parser("query"); p.add_argument("name"); p.add_argument("--sequence"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("mql"); p.add_argument("name"); p.add_argument("--sequence")
    p = sub.add_parser("trace"); p.add_argument("id")
    p = sub.add_parser("status"); p.add_argument("id")
    return core.run(ap, argv, run_command)


def run_command(lg, args):
    if args.cmd == "get":
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


if __name__ == "__main__":
    sys.exit(main())
