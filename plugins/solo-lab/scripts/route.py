#!/usr/bin/env python3
"""Mechanical part of the routing: where a message or a package goes, and what the sessions did.

    route.py classify --text '<message>'      rows R1/R3 by work item ID or session name; else needs_router
    route.py classify --draft <path>          rows R5/R6/R7 for a package handed back to the entry
    route.py audit                            the soft-rule hits, questions the hooks took out of turns
                                              other sessions woke, wrap-ups that asked by who started the
                                              turn, headers without the app's wrapper, stale work and
                                              claim anomalies
    route.py cross [<ID>] [--days N]          the cross-sequence requests with traffic in the last days and the
                                              latest line of each; with an ID, all the traffic of that request

classify walks the routing table in orgspec/rules.json and stops at the first row
it can decide without judgement. Matching a message to work by the work item's
name, fields and 状态包 (R2) is left to the router agent: the script answers
needs_router instead of guessing. --offline skips the ledger lookups.

The session pool is pool.py, inboxes and focus sessions are notify.py, the researcher's to-dos todo.py.
Standard library only.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import ledger_core  # noqa: E402
import pool  # noqa: E402

ID_RE = re.compile(r"#(\d{3,}(?:-\d{1,2}(?![\d-]))?)|(?<![\d-])(\d{9,12})(?!\d)")      # #2101; #2101-3, a task without a work item
SESSION_RE = re.compile(r"(?<![\w-])([A-Za-z]+-[pP][0-2]-\d+(?:-\d{1,2})?)(?![\w-])")
# guard.jsonl's agent_turn_gate: what guard.py did to a question in a turn another session woke
AGENT_TURN_GATES = ("stripped", "denied", "allowed_urgent")


def role_of_session(spec, session):
    m = re.match(r"([A-Za-z]+)-[pP]([0-2])-", session)
    if not m:
        return None
    seq = next((s for s, _, a in spec.sequences() if a.lower() == m.group(1).lower()), None)
    if not seq:
        return None
    rank = int(m.group(2))
    if seq == "pmo" and rank < 2:
        return "pmo-router" if rank == 1 else "pmo-scribe"
    return "%s-p%d" % (seq, rank)


# ------------------------------------------------------------- classify
def classify_text(spec, root, text, offline):
    rows = {r["id"]: r for r in spec.rules["routing"]}
    ids = [a or b for a, b in ID_RE.findall(text)]
    sessions = SESSION_RE.findall(text)
    for s in sessions:
        m = re.match(r"[A-Za-z]+-[pP]0-(\d{3,}(?:-\d{1,2})?)$", s)   # Alg-p0-2101 names work item 2101; Alg-p0-01 is a pool slot
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    if ids or sessions:
        lg = None if offline else ledger_core.Ledger(root, "preview")
        for item_id in ids:
            if lg is None:
                return result(spec, "R1", "带工作项 ID #%s（离线，没核对是否存在）" % item_id, work_item=item_id)
            if ledger_core.is_local(item_id):          # a task a +1 handed to a +0: no work item, a local receipt
                try:
                    rec = ledger_core.local_task(lg, item_id)[1]
                except ledger_core.LedgerError as e:
                    return result(spec, "R3", "#%s 查不到：%s" % (item_id, str(e)[:120]), work_item=item_id)
                if rec.get("state") == ledger_core.LOCAL_ACCEPTED:
                    return result(spec, "R3", "#%s 已经验收，不能直接续上" % item_id, work_item=item_id)
                owner = rec.get("receiver") or rec.get("by")
                return result(spec, "R1", "#%s 是没建工作项的任务，状态“%s”" % (item_id, rec.get("state") or ledger_core.LOCAL_OPEN),
                              work_item=item_id, session=owner, pool=pool_info(root, owner, item_id))
            try:
                it = lg.item(item_id)
            except ledger_core.LedgerError as e:
                return result(spec, "R3", "#%s 查不到：%s" % (item_id, str(e)[:120]), work_item=item_id)
            if it["state"] in ledger_core.closed_states(lg):
                return result(spec, "R3", "#%s 已是“%s”，不能直接续上" % (item_id, it["state"]), work_item=item_id)
            seq_cn = lg.text_of(it, "sequence")
            seq = next((s for s, cn, _ in spec.sequences() if cn == seq_cn), None)
            owner = lg.text_of(it, "owner_session")
            return result(spec, "R1", "#%s 存在，状态“%s”" % (item_id, it["state"]), work_item=item_id,
                          session=owner, pool=pool_info(root, owner, item_id))
        for s in sessions:
            role = role_of_session(spec, s)
            if role:
                return result(spec, "R1", "带会话名 %s" % s, session=s, role=role, pool=pool_info(root, s))
    out = result(spec, None, "没带工作项 ID 或会话名；要按工作项名称、字段和状态包匹配（R2/R3），或判断是不是新任务（R4）、要人拍板（R5）")
    out["needs_router"] = True
    out["candidates"] = [rows[k]["id"] for k in ("R2", "R3", "R4", "R5") if k in rows]
    return out


def classify_draft(spec, root, path):
    """Where a package goes. From 1.1.0 its author uploads it, the scribe only builds the work item, and what the
    researcher is to decide goes through the router to the entry; a request to another sequence is checked and
    registered by the router and then goes between the two +2 (1.0.12)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    errors, _ = orgspec.lint_text(spec, text)
    if errors:
        return {"row": None, "reason": "草稿没通过检查", "errors": errors, "action": "退回作者修改"}
    d = ledger_core.Draft(spec, path)
    to_seq = d.receiver_seq
    notice = spec.term("msg", "notice")
    upload = "docs.py upload <草稿路径> --apply"
    if d.pid == "decision":
        who = orgspec.executor_of(spec, d.header.get(orgspec.EXECUTOR))
        carry = ("执行方是研究员，他说做完了，路由请书记员归档" if who == orgspec.RESEARCHER
                 else "书记员把决定内容发给执行方 %s，它照决定执行完，马上请书记员归档" % who)
        if d.header.get("渠道") == spec.term("channel", "researcher"):
            tell = "路由给“受影响的工作项”的负责会话发消息【%s #ID】，改状态包；显示“研究员在用”的改用 notify.py send 留言" % notice
            return result(spec, "R6", "研究员主动发起的决策包", next_hop="pmo-router",
                          steps=["和研究员谈的那个会话自己上传决策包（%s）" % upload,
                                 "它请书记员 register 后确认“决定”这一步，填决定内容和决定人（登记请求写“由 研究员 提出”，带原话）；"
                                 "拿到工作项 ID 后用 docs.py relink 写回文档，再用【%s #ID】告诉路由" % notice,
                                 carry, tell])
        return result(spec, "R5", "决策队列来的决策包", next_hop="pmo-router",
                      steps=["作者自己上传（%s），请书记员 register，停在“待决定”；拿到工作项 ID 后用 docs.py relink 写回文档" % upload,
                             "作者给路由发【%s #ID】，路由核对已登记，转告入口" % spec.term("msg", "decide"),
                             "路由转来的那一轮，入口在状态里提一次：事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）、主文档链接；"
                             "紧急程度为“插队”的，这一轮可以在收尾里问研究员一次",
                             "研究员主动来谈时，入口连同其余待决定的决策和等研究员执行的决策列给他",
                             "研究员定了，入口把原话交给路由，路由请书记员确认“决定”这一步，决策到“待执行”；" + carry])
    route = next((r for r in spec.rules["package_routes"] if r["pkg"] == d.pid and (r["rel"] or None) == d.rel), None)
    if d.pid == "request" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["发起方 +2 自己上传需求包（%s），把草稿路径用【%s】发给路由" % (upload, notice),
                 "路由对照台账查重复和冲突：ledger.py query cross、ledger.py query open --sequence <承接序列>；所属研究任务冻结的字段和待定决策“冻结的动作”",
                 "没有问题：请书记员 register（“提出”随登记确认，停在待接单），把工作项 ID 用【%s #ID】回给发起方，"
                 "由它写回文档（docs.py relink）、直接委托 %s" % (notice, hop),
                 "和进行中的工作重复、或者碰到冻结项：不登记，把是哪一条回给发起方；要研究员定的转告入口"]
        return result(spec, "R7", "%s（%s → %s）" % (spec.pkg(d.pid) + "（%s）" % spec.term("rel", d.rel),
                                                  route["from"] if route else "?", route["to"] if route else "?"),
                      next_hop="pmo-router", steps=steps)
    if d.pid == "delivery" and d.rel == "cross":
        return result(spec, None, "跨序列的%s不经过入口和路由：承接方自己上传答复（%s），请书记员填“交付包”字段、确认“执行”，"
                                  "再直接交付给发起方 +2" % (spec.pkg(d.pid), upload), next_hop=None)
    return result(spec, None, "序列内的%s不经过入口和路由，在本序列的上下级之间直接往来" % spec.pkg(d.pid), next_hop=None)


def result(spec, row, reason, **extra):
    out = {"row": row, "reason": reason}
    if row:
        r = next(x for x in spec.rules["routing"] if x["id"] == row)
        out.update({"when": r["when"], "action": r["action"], "detail": spec.render(r["detail"])})
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def pool_info(root, session=None, item_id=None):
    """Where the owner session stands (desktop ID, status, the researcher using it). A +0
    work name such as the ledger's 负责会话 (Alg-p0-2101) stands for the claim that owns the item."""
    policy = pool.load_policy(root)
    if not policy:
        return None
    pl, ok = pool.load(root)
    if not ok:
        return None
    name = pool.resolve(policy, pl, session) if session else None
    if (not name or name not in pl["claims"]) and item_id:
        name = (pl.get("owners") or {}).get(str(item_id)) or name
    row = next((r for r in pool.session_view(root, policy, pl) if r["name"] == name), None)
    if not row:
        return {"session": name, "claimed": False}
    return {"session": name, "claimed": True, "desk": row["desk"], "status": row["status"],
            "researcher_using": bool(row["focus"])}


def audit_cmd(spec, root, hours=24):
    policy = pool.need_pool(root)
    if not policy:
        return 0
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json")
        return 1
    hits, gates, asked = {}, {}, {}
    try:
        with open(os.path.join(root, ".claude", "logs", "guard.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    who = rec.get("session_name") or rec.get("role") or "?"
                    for h in rec.get("soft_hits") or []:
                        hits.setdefault(who, []).append(h)
                    if rec.get("agent_turn_gate") in AGENT_TURN_GATES:
                        gates.setdefault(who, dict.fromkeys(AGENT_TURN_GATES, 0))[rec["agent_turn_gate"]] += 1
                    if rec.get("asked") is True and rec.get("origin") == "claim":     # a pool session's wrap-up asked
                        n = asked.setdefault(who, dict.fromkeys(("agent", "left", "urgent", "researcher", "system", "unknown"), 0))
                        origin = rec.get("turn_origin")
                        if origin == "agent":
                            n["agent"] += 1
                            gate = rec.get("agent_turn_gate")
                            n["left"] += gate != "stripped"
                            n["urgent"] += gate == "allowed_urgent"
                        else:
                            n[origin if origin in ("researcher", "system") else "unknown"] += 1
    except OSError:
        pass
    events = pool.read_events(root)
    for rec in events:
        for h in rec.get("soft_hits") or []:
            hits.setdefault(rec.get("seen_by") or "?", []).append(h)
    print("软规则命中（只提醒，没拦）：")
    for who, hs in sorted(hits.items()):
        print("- %s：%d 次；最近一次：%s" % (who, len(hs), hs[-1][:100]))
    if not hits:
        print("- 无")
    print("其他 agent 会话叫醒的轮次里向研究员的提问（hook 处理）：")
    for who, n in sorted(gates.items()):
        print("- %s：收尾提问去掉 %d 次，AskUserQuestion 拒绝 %d 次，插队决策放行 %d 次" % (
            who, n["stripped"], n["denied"], n["allowed_urgent"]))
    if not gates:
        print("- 无")
    print("池会话带提问的收尾，按这一轮是谁开的（研究员不在时也有“研究员开的”，可能是桌面应用改了消息外壳）：")
    for who, n in sorted(asked.items()):
        print("- %s：其他 agent 叫醒的轮次 %d 次，提问留下 %d 次（其中插队决策放行 %d 次）；研究员开的轮次 %d 次；应用的轮次 %d 次%s" % (
            who, n["agent"], n["left"], n["urgent"], n["researcher"], n["system"],
            "；轮次不明 %d 次" % n["unknown"] if n["unknown"] else ""))
    if not asked:
        print("- 无")
    bare = {}
    for rec in events:
        if rec.get("event") in ("message", "message_misaddressed") and rec.get("wrapped") is False:
            bare.setdefault(rec.get("seen_by") or "?", []).append(str(rec.get("line") or ""))
    print("有插件抬头、没有桌面应用外壳的消息（多了就可能是外壳变了，这些消息仍按抬头认作其他 agent 会话叫醒的）：")
    for who, lines in sorted(bare.items()):
        print("- %s：%d 次；最近一次：%s" % (who, len(lines), lines[-1][:100]))
    if not bare:
        print("- 无")
    claimed = {r.get("name") for r in events if r.get("event") in ("claim", "reattach")}
    odd = [n for n in pl["claims"] if n not in claimed]
    if odd:
        print("认领表里有、日志里没有认领记录的：%s（可能被直接改过，请研究员核对）" % "、".join(odd))
    scribes = pool.claims_of(pl, policy.get("scribe_role"))
    if len(scribes) > 1:
        print("问题：书记员有 %d 条认领，台账写入已全部停下。" % len(scribes))
    conflicts = sorted({r.get("name") for r in events if r.get("event") == "desk_conflict"})
    if conflicts:
        print("和别的进程共用桌面会话 ID 的认领：%s（多半是从这些会话里起的 claude 进程）" % "、".join(conflicts))
    rows = pool.session_view(root, policy, pl)
    stale = [r["name"] for r in rows if r["status"] == "uncertain"]
    if stale:
        print("标着在忙、15 分钟没有工具调用的：%s" % "、".join(stale))
    at = pool.now()
    sent_times = {}
    for rec in events:
        if rec.get("event") == "message" and rec.get("kind") == "assign" and rec.get("id"):
            sent_times[(rec.get("from"), rec["id"])] = pool.parse_time(rec.get("ts"))
    late = []
    for name, (sent, _) in pool.all_open_items(root, pl.get("owners")).items():
        for item, to in sent.items():
            t = sent_times.get((name, item))
            if t and at - t > datetime.timedelta(hours=hours):
                late.append("#%s（%s → %s）" % (item, name, to))
    if late:
        print("委托出去 %d 小时还没交付的：%s" % (hours, "、".join(sorted(late))))
    mixed = pool.mixed_modes(rows)
    if mixed:
        print("权限模式和多数会话不同类的：%s" % "、".join(mixed))
    return 0


CROSS_DAYS = 7                 # route.py cross lists the requests that had traffic within this many days
CROSS_QUIET_HOURS = 48         # and says so when the last message about one is older than this
CROSS_RECORD = "packages/drafts/pmo/跨序列协调记录.md"
CROSS_SKIP = ("register_request", "register_receipt")     # the scribe's traffic: the ledger has what it wrote


def cross_cmd(root, item=None, days=CROSS_DAYS):
    """What the sessions said to each other about the 跨序列需求, from the dispatch log: one line a request, or
    every message of one. Read-only, and nothing is asked of Feishu. From 1.0.12 the two +2 of such a request
    talk to each other without the entry or the router in between, so this is where the router (and the entry,
    when the researcher asks) sees what went on: the log keeps the first line of each message, the rest is in
    the package the line names. Which items are 跨序列需求 is read from the registration receipts
    (packages/sent/<ID>.json); their state and deadline are the ledger's (ledger.py query cross)."""
    if not pool.need_pool(root):
        return 0
    files = pool.receipt_files(root)
    seen = {}

    def receipt(key):
        return pool.registration_receipt(root, key, files, seen)

    def title(key):
        name = os.path.basename(str(receipt(key).get("draft") or ""))
        return re.sub(r"^\d{8}-|\.md$", "", name) or "（本地没有登记回执）"
    talk = {}
    for rec in pool.read_events(root):
        if rec.get("event") in ("message", "message_misaddressed") and rec.get("id") and rec.get("kind") not in CROSS_SKIP:
            talk.setdefault(str(rec["id"]), []).append(rec)

    def line(rec):
        at = pool.parse_time(rec.get("ts"))
        return "%s %s" % (at.strftime("%m-%d %H:%M") if at else "?", str(rec.get("line") or "")[:160])
    if item:
        key = str(item).lstrip("#")
        rows = talk.get(key, [])
        kind = "" if receipt(key).get("type") == "cross_request" else "（登记回执里它不是跨序列需求）"
        print("#%s %s%s：往来 %d 条（不算和书记员之间的登记请求、登记回执）。" % (key, title(key), kind, len(rows)))
        for rec in rows:
            print("- " + line(rec))
        return 0
    at = pool.now()
    since = at - datetime.timedelta(days=days)
    rows = []
    for key, recs in talk.items():
        last = pool.parse_time(recs[-1].get("ts"))
        if receipt(key).get("type") == "cross_request" and last and last >= since:
            rows.append((last, key, recs))
    print("最近 %d 天有往来的跨序列需求 %d 条（状态和期限看 ledger.py query cross）：" % (days, len(rows)))
    for last, key, recs in sorted(rows, reverse=True):
        asked = next((r for r in recs if r.get("kind") == "assign"), None)
        who = "%s → %s，" % (asked.get("from") or "?", asked.get("seen_by") or asked.get("to") or "?") if asked else ""
        quiet = int((at - last).total_seconds() // 3600)
        print("- #%s %s：%s往来 %d 条%s；最近一条：%s" % (
            key, title(key), who, len(recs), "，已经 %d 小时没有往来" % quiet if quiet >= CROSS_QUIET_HOURS else "", line(recs[-1])))
    if not rows:
        print("- 无")
    print("看一条的全部往来：route.py cross <工作项 ID>。日志只记每条消息的首行，内容在首行写的包里。")
    print("路由的协调记录：%s%s" % (CROSS_RECORD, "" if os.path.isfile(os.path.join(root, CROSS_RECORD)) else "（还没有）"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="入口的机械路由、派发规范的审计和跨序列往来", allow_abbrev=False)
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("classify")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--draft")
    p = sub.add_parser("audit")
    p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("cross")
    p.add_argument("id", nargs="?", help="只看这条工作项的全部往来")
    p.add_argument("--days", type=int, default=CROSS_DAYS, help="不写工作项时：列出最近几天有往来的跨序列需求")
    args = ap.parse_args(argv)
    spec = orgspec.OrgSpec()
    root = ledger_core.project_root()
    if args.cmd == "classify":
        out = classify_text(spec, root, args.text, args.offline) if args.text else classify_draft(spec, root, args.draft)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "audit":
        return audit_cmd(spec, root, args.hours)
    if args.cmd == "cross":
        return cross_cmd(root, args.id, args.days)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
