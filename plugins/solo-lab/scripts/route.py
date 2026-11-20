#!/usr/bin/env python3
"""Mechanical part of the entry's routing, plus inboxes, focus sessions and the session pool.

    route.py classify --text '<message>'      rows R1/R3 by work item ID or session name; else needs_router
    route.py classify --draft <path>          rows R5/R6/R7 for a package handed back to the entry
    route.py notify <role or session name> --text T [--work-item ID]
    route.py focus open [<role or session>] --topic T | focus close [<role or session>] | focus list
    route.py listen                           Feishu DM listener: appends to inbox/feishu.jsonl

The session pool (see pool.py):
    route.py claim <role> [--as NAME] [--desk ID]    claim the role a permit names (the hooks confirm it)
    route.py expect NAME --role R (--desk ID | --session ID)   pre-register a session you opened
    route.py release NAME                     the researcher frees a claim (it becomes a tombstone)
    route.py pool [--role R] [--item ID] [--queue NAME] [--recover] [--watch [--once]]
    route.py pool --prune [--apply]           take archived, terminated and deleted work items out of the table of who
                                              answers for what (pool.json owners) and strike those the researcher was
                                              asked to terminate or delete, read from the ledger; a preview without --apply
    route.py refill <role> [--count N]        ask for more sessions of a role: a refill request in pool.json and the entry's
                                              inbox; --count 0 withdraws the role's requests
    route.py todo add --text T [--for ID] [--done-when T] [--terminate | --delete]
                                              something the researcher is to do by hand and that takes no decision:
                                              a line in pool.json and the entry's inbox, listed after the decisions;
                                              --terminate / --delete: the work item --for is to go, on the web page
    route.py todo done <n> [--note T]         the to-do is done: taken off the list
    route.py audit                            the soft-rule hits, questions the hooks took out of turns
                                              other sessions woke, wrap-ups that asked by who started the
                                              turn, headers without the app's wrapper, stale work and
                                              claim anomalies

classify walks the routing table in orgspec/rules.json and stops at the first row
it can decide without judgement. Matching a message to work by the work item's
name, fields and 状态包 (R2) is left to the router agent: the script answers
needs_router instead of guessing. --offline skips the ledger lookups.
Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import pool  # noqa: E402

ID_RE = re.compile(r"#(\d{3,})|(?<![\d-])(\d{9,12})(?!\d)")
SESSION_RE = re.compile(r"(?<![\w-])([A-Za-z]+-[pP][0-2]-\d+)(?![\w-])")
CLAIM_WAIT = 60
# guard.jsonl's agent_turn_gate: what guard.py did to a question in a turn another session woke
AGENT_TURN_GATES = ("stripped", "denied", "allowed_urgent")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


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


def focus_state(root):
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        import codex_runtime
        return {b["role"]: b["focus"] for b in codex_runtime.Pool(root).load()["bindings"].values() if b.get("focus")}
    return {}       # a Claude pool keeps "the researcher is using it" in pool.json; pool_info reports it


def focus_for_sequence(root, seq):
    for role, info in focus_state(root).items():
        if role.startswith(seq + "-p2"):
            return role
    return None


# ------------------------------------------------------------- classify
def classify_text(spec, root, text, offline):
    rows = {r["id"]: r for r in spec.rules["routing"]}
    ids = [a or b for a, b in ID_RE.findall(text)]
    sessions = SESSION_RE.findall(text)
    for s in sessions:
        m = re.match(r"[A-Za-z]+-[pP]0-(\d{3,})$", s)        # Alg-p0-2101 names work item 2101; Alg-p0-01 is a pool slot
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    if ids or sessions:
        lg = None if offline else ledger.Ledger(root, "preview")
        for item_id in ids:
            if lg is None:
                return result(spec, "R1", "带工作项 ID #%s（离线，没核对是否存在）" % item_id, work_item=item_id)
            try:
                it = lg.item(item_id)
            except ledger.LedgerError as e:
                return result(spec, "R3", "#%s 查不到：%s" % (item_id, str(e)[:120]), work_item=item_id)
            if it["state"] in ledger.closed_states(lg):
                return result(spec, "R3", "#%s 已是“%s”，不能直接续上" % (item_id, it["state"]), work_item=item_id)
            seq_cn = lg.text_of(it, "sequence")
            seq = next((s for s, cn, _ in spec.sequences() if cn == seq_cn), None)
            owner = lg.text_of(it, "owner_session")
            focus = focus_for_sequence(root, seq) if seq else None
            return result(spec, "R1", "#%s 存在，状态“%s”" % (item_id, it["state"]), work_item=item_id,
                          session=owner, focus=focus, pool=pool_info(root, owner, item_id))
        for s in sessions:
            role = role_of_session(spec, s)
            if role:
                return result(spec, "R1", "带会话名 %s" % s, session=s, role=role,
                              focus=role if role in focus_state(root) else None, pool=pool_info(root, s))
    out = result(spec, None, "没带工作项 ID 或会话名；要按工作项名称、字段和状态包匹配（R2/R3），或判断是不是新任务（R4）、要人拍板（R5）")
    out["needs_router"] = True
    out["candidates"] = [rows[k]["id"] for k in ("R2", "R3", "R4", "R5") if k in rows]
    return out


def classify_draft(spec, root, path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    errors, _ = orgspec.lint_text(spec, text)
    if errors:
        return {"row": None, "reason": "草稿没通过检查", "errors": errors, "action": "退回作者修改"}
    d = ledger.Draft(spec, path)
    to_seq = d.receiver_seq
    notice = spec.term("msg", "notice")
    if d.pid == "decision":
        who = orgspec.executor_of(spec, d.header.get(orgspec.EXECUTOR))
        carry = ("执行方是研究员，他说做完了，请书记员归档" if who == orgspec.RESEARCHER
                 else "书记员把决定内容发给执行方 %s，它照决定执行完，马上请书记员归档" % who)
        if d.header.get("渠道") == spec.term("channel", "researcher"):
            tell = "给“受影响的工作项”的负责会话发消息【%s #ID】；显示“研究员在用”的改用 route.py notify 留言" % notice
            return result(spec, "R6", "研究员主动发起的决策包", next_hop="pmo-scribe",
                          steps=["书记员 register 后确认“决定”这一步，填决定内容和决定人", carry, tell])
        return result(spec, "R5", "决策队列来的决策包", next_hop="pmo-scribe",
                      steps=["书记员 register，停在“待决定”",
                             "登记回执到的那一轮，在状态里提一次：事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）、主文档链接；"
                             "紧急程度为“插队”的，这一轮可以在收尾里问研究员一次",
                             "研究员主动来谈时，连同其余待决定的决策和等研究员执行的决策列给他",
                             "研究员定了，书记员确认“决定”这一步，决策到“待执行”；" + carry])
    route = next((r for r in spec.rules["package_routes"] if r["pkg"] == d.pid and (r["rel"] or None) == d.rel), None)
    steps, hop = [], None
    if d.pid == "request" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["书记员 register，建跨序列需求（“提出”随登记确认，停在待接单）", "请 %s 接单：复述需求、承接范围、预计交付时间" % hop]
    elif d.pid == "delivery" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["书记员 reply，把交付包挂到跨序列需求下；承接方请书记员确认“执行”这一步，工作项到“待验收”",
                 "给 %s 的会话发消息【%s #ID】，写交付包路径；它验收通过就请书记员确认“验收”，不通过就回滚到“执行”并写原因" % (hop, notice)]
    else:
        return result(spec, None, "序列内的%s不经过入口，交给本序列上一级" % spec.pkg(d.pid), next_hop=None)
    if hop and hop.endswith("-p2") and to_seq and focus_for_sequence(root, to_seq):
        steps.append("%s 正开着聚焦会话：用 route.py notify 留言，不另发消息" % hop)
    return result(spec, "R7", "%s（%s → %s）" % (spec.pkg(d.pid) + ("（%s）" % spec.term("rel", d.rel) if d.rel else ""),
                                              route["from"] if route else "?", route["to"] if route else "?"),
                  next_hop=hop, steps=steps)


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
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        import codex_runtime
        state = codex_runtime.Pool(root).load()
        b = state["bindings"].get(session)
        if not b:
            return {"session": session, "claimed": False}
        return {"session": session, "claimed": True, "thread_id": b["thread_id"], "host_id": b["host_id"],
                "status": state["sessions"].get(b["thread_id"], {}).get("status", "unknown"),
                "researcher_using": bool(b.get("focus"))}
    policy = load_policy(root)
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


# ------------------------------------------------------- inbox & focus
def append_inbox(root, role, msg):
    path = os.path.join(root, "inbox", role + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def notify_target(spec, root, policy, role):
    """The inbox a notify goes to: a session's; a role's only when the role can have one session or
    has exactly one claim; a +0 work name goes to the claim that owns the work item."""
    if role == "feishu":
        raise SystemExit("inbox/feishu.jsonl 只由飞书私聊监听（route.py listen）写，notify 不写它")
    roles = agent_roles(spec)
    pl, ok = pool.load(root)
    if role in roles:
        if pool.role_limit(policy, role) == 1:
            return role
        held = sorted(c["name"] for c in pool.claims_of(pl, role)) if ok else []
        if len(held) == 1:
            return held[0]
        raise SystemExit("会话池里 %s 可能有几个会话（现在认领了：%s），写会话名，比如 %s" % (
            role, "、".join(held) or "无", pool.slot_name(policy["agents"][role], 1)))
    name = pool.resolve(policy, pl, role) if ok else pool.canonical(policy, role)
    if not name or not pool.canonical(policy, name):
        raise SystemExit("没有角色或会话 %s" % role)
    if pool.work_item_of(name) and name not in pl["claims"]:
        r = pool.role_of(policy, name)
        held = sorted(c["name"] for c in pool.claims_of(pl, r)) if ok else []
        raise SystemExit("%s 是 +0 的工作名，#%s 现在没有记下负责会话；改写池位名（%s 现有：%s）" % (
            name, pool.work_item_of(name), r, "、".join(held) or "无"))
    return name


def notify(spec, root, role, text, work_item=None, sender=None):
    """Leave a message in a session's inbox (a role's when the role has one session)."""
    policy = load_policy(root) or {}
    target = notify_target(spec, root, policy, role)
    me, origin = caller(root, policy)               # the sender is whoever runs this, not --from
    sender = sender_label(me, origin)
    msg = {"id": uuid.uuid4().hex[:12], "ts": now(), "from": sender, "text": text, "read": False}
    if work_item:
        msg["work_item"] = str(work_item).lstrip("#")
    append_inbox(root, target, msg)
    print("已留言给 %s：%s" % (target, text[:60]))


ORIGIN_LABEL = {"researcher": "研究员的会话", "desk_conflict": "和已认领会话共用桌面会话 ID 的进程",
                "tombstone": "已释放认领的会话", "permit": "还没转正的会话", "pending": "还没转正的会话",
                "pool_unreadable": "认领表读不出时的会话"}


def sender_label(me, origin):
    """Who a message or request is from: the claim's name, or what kind of caller ran the script.
    A process with neither a session ID nor a desktop ID is a plain terminal, not a session."""
    if me:
        return me["name"]
    if origin == "researcher" and not os.environ.get(pool.SESSION_ENV) and not os.environ.get(pool.DESK_ENV):
        return "终端里的进程（没有会话 ID）"
    return ORIGIN_LABEL.get(origin, "未认领的会话")


def agent_roles(spec):
    out = []
    for s, _, _ in spec.sequences():
        for r in (2, 1, 0):
            out.append("pmo-router" if (s, r) == ("pmo", 1) else "pmo-scribe" if (s, r) == ("pmo", 0) else "%s-p%d" % (s, r))
    return out


def focus(spec, root, action, role=None, topic=None, offline=False):
    return pool_focus(spec, root, load_policy(root) or {}, action, role, topic)


def pool_focus(spec, root, policy, action, target, topic):
    """Mark a session as the researcher's for now; route.py pool will not suggest it."""
    if action == "list":
        pl, _ = pool.load(root)
        rows = [r for r in pool.session_view(root, policy, pl) if r["focus"]]
        for r in rows:
            left = "，空闲 %d 分钟后失效" % r["focus_left_min"] if r["focus_left_min"] is not None else ""
            print("%s：研究员在用（%s，%s 开始%s）" % (r["name"], r["focus"].get("topic"), r["focus"].get("opened_at"), left))
        if not rows:
            print("没有研究员在用的会话。")
        return
    me = caller(root, policy)[0]
    pl, _ = pool.load(root)
    name = None
    if target:
        if target in policy["agents"]:
            held = [c for c in pl["claims"].values() if c.get("role") == target]
            name = held[0]["name"] if len(held) == 1 else None
        else:
            name = pool.resolve(policy, pl, target)
    name = name or (me or {}).get("name")
    if not name:
        raise SystemExit("不知道是哪个会话：在池会话里运行时不用写名字；在别处运行写会话名，比如 Alg-p2-01")
    with pool.locked(root) as pl:
        if name not in pl["claims"]:
            raise SystemExit("%s 不在认领表里" % name)
        if action == "close":
            pl["focus"].pop(name, None)
        else:
            pl["focus"][name] = {"topic": topic or "", "opened_at": now()}
        sid = pl["claims"][name].get("session_id") or ""
    if action != "close":                               # an old lapse note no longer applies
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    pool.log_event(root, {"event": "focus_" + action, "name": name, "topic": topic})
    if action == "close":
        print("已收尾：%s。谈定的事要写成决策包交给入口登记，没写进台账的不算数。" % name)
    else:
        print("已标记：研究员在用 %s（%s）。会话空闲满 30 分钟自动失效；这期间 route.py pool 不推荐它。去侧栏找 %s。" % (
            name, topic or "未写", name))


# ------------------------------------------------------------------ pool
def load_policy(root):
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        return load_json(os.path.join(root, ".agent-org", "policy.json"), None)
    return load_json(os.path.join(root, ".claude", "hooks", "policy.json"), None)


def caller(root, policy):
    """(claim, origin) of the session running this script, from its Bash environment. A desktop ID
    whose claim belongs to another running session counts as a desk conflict, not as that claim."""
    sid = os.environ.get(pool.SESSION_ENV) or ""
    desk = os.environ.get(pool.DESK_ENV) or ""
    pl, ok = pool.load(root)
    if not ok:
        return None, "pool_unreadable"
    claim = pool.claim_by_session(pl, sid)
    if claim:
        return claim, "claim"
    other = pool.claim_by_desk(pl, desk)
    if other:
        return (other, "claim") if pool.cleared(root, other) else (None, "desk_conflict")
    if pool.permit_for(pl, sid, desk):
        return None, "permit"
    if pool.pending_for(pl, sid):
        return None, "pending"
    if pool.tombstoned(pl, sid, desk):
        return None, "tombstone"
    return None, "researcher"


def need_pool(root):
    policy = load_policy(root)
    if not policy:
        raise SystemExit("读不到 .claude/hooks/policy.json：这个目录还没初始化")
    return policy


def plan_of(root):
    return load_json(os.path.join(root, ".claude", "solo-lab", "pool-plan.json"), {}) or {}


def next_slot(policy, pl, role, reserved=()):
    """The name a new session of role gets: its fixed name, or the smallest slot not claimed,
    permitted or reserved."""
    pol = policy["agents"][role]
    if pol.get("fixed_session"):
        return pol["fixed_session"]
    taken = set(pl["claims"]) | {p.get("name") for p in pl["permits"]} | set(reserved)
    n = 1
    while pool.slot_name(pol, n) in taken:
        n += 1
    return pool.slot_name(pol, n)


def child_row(tpl, name):
    return {k: (v.replace("{name}", name).replace("{short}", name.lower()) if isinstance(v, str) else v)
            for k, v in tpl.items()}


def plan_walk(entry):
    """A session of pool-plan.json and every session planned below it."""
    yield entry
    for child in entry.get("children") or []:
        yield from plan_walk(child)


def plan_entry(plan, name):
    """(the session's entry in pool-plan.json, the +2 entry of its sequence), at any depth."""
    for top in plan.get("top", []):
        hit = next((e for e in plan_walk(top) if e.get("name") == name), None)
        if hit:
            return hit, top
    return None, None


def opener(policy, role, seq="本序列"):
    """Whose session opens a new session of this role: the one a level above, so that it nests there."""
    if policy["agents"][role]["rank"] >= 1:
        return "%s +2" % seq
    return "%s的一个 +1（这个序列没有 +1 时是 +2）" % seq


def children_text(root, policy, claim):
    """What a session opens below itself: a +2 its +1s, a +1 its +0s. The rows come from
    pool-plan.json, the ones already claimed marked, plus a ready row for the next free slot
    (for a refill). A session sits in the sidebar under the one that opened it, so the pool
    nests as work is handed down."""
    pl, _ = pool.load(root)
    plan = plan_of(root)
    agents = policy["agents"]
    mine = agents[claim["role"]]
    entry, top = plan_entry(plan, claim["name"])
    if not top:                              # opened as a refill, so not in the plan: it gets refill rows only
        top = next((t for t in plan.get("top", []) if agents[t["role"]]["sequence"] == mine["sequence"]), None)
        entry = top if top and top["role"] == claim["role"] else None
    if not top:
        return ""
    rows = [(ch, ch["name"] in pl["claims"], False) for ch in (entry or {}).get("children", [])]
    sizes = policy.get("pool") or {}
    cap = sizes.get("max_sessions")
    used = len(pl["claims"]) + len(pl["permits"])
    full = bool(cap) and used >= cap

    def has(rank):                           # the sequence has a session of this rank: claimed, being opened, or planned
        held = list(pl["claims"].values()) + list(pl["permits"]) + list(plan_walk(top))
        return any(agents[c["role"]]["sequence"] == mine["sequence"] and agents[c["role"]]["rank"] == rank
                   for c in held if c.get("role") in agents)

    zero = next((t for t in top.get("child_roles", []) if agents[t["role"]]["rank"] == 0), None)
    # names the plan keeps for sessions another session of the sequence opens
    reserved = {e["name"] for e in plan_walk(top)} - {ch["name"] for ch, _, _ in rows}
    for tpl in ([] if full else top.get("child_roles", [])):
        rank = agents[tpl["role"]]["rank"]
        # a +2 opens +1s and a +1 opens +0s; a +2 opens +0s itself only where the sequence runs without +1s
        wanted = rank == mine["rank"] - 1 or (mine["rank"] == 2 and rank == 0 and not sizes.get("p1") and not has(1))
        if not wanted or pool.role_limit(policy, tpl["role"]) <= len(pool.claims_of(pl, tpl["role"])):
            continue
        name = next_slot(policy, pl, tpl["role"], reserved)
        if any(ch["name"] == name for ch, _, _ in rows):
            continue
        row = child_row(tpl, name)
        if (rank == 1 and zero and sizes.get("p0") and not has(0) and plan.get("opens")
                and not (cap and used + 2 > cap)):
            # the first +1 of a sequence that has no +0 brings one along, in its own first turn
            row["brief"] = row["claim"] + plan["opens"] % (next_slot(policy, pl, zero["role"], reserved), name)
        rows.append((row, False, True))
    if not rows:
        return ""
    lines = ["开局句点了名要你开的会话，照下表开其中还没认领的（开局句没提开会话的，不开，用 route.py pool 找回已有的会话）；"
             "研究员在本会话里说“补位”时，开表里“补位用”的那一行。每个都用会话管理工具 start_session（它还没加载时，先用 ToolSearch 查 start_session 加载进来）：initiation 写 user_asked，context 写 fresh，"
             "use_worktree 写 false，权限模式不填（继承本会话）；name、title、model、prompt、background 照下表原样填。开好马上用返回的桌面会话 ID 运行表里的 expect。"
             "这是研究员初始化这个项目时定下的开法：下一级由上一级开出，侧栏里它才嵌在上一级下面。"
             "应用回“Not started: …”时，是会话自己开出的会话到了应用的上限（同时在跑的满 5 个，或者研究员这条消息以后已经开了 10 个）："
             "这一个先不开，也不要改提启动卡，回话里写明还差哪一个；研究员在本会话里说了，或者应用再叫醒你时，再照这张表开。",
             "补位请求（route.py refill 记下的）不等研究员来说：记下的那一轮就为表里“补位用”的那一行提一张启动卡。先运行那一行的 expect，把 "
             "`--desk <…>` 换成 `--card`；再调 start_session，initiation 写 own_initiative，其余参数照那一行。被别的会话叫醒的轮次也能提卡，"
             "研究员在本会话里点一下，会话就开出来，它自己认领。一条补位请求只提一张卡。"]
    if entry is top and not top.get("children"):
        lines.append("本序列开池时只开 +2，下表只有补位用的行。")
    if full:
        lines.append("会话池已到任务文件的总上限 max_sessions=%d，不能再补位：先请研究员归档不用的会话并 release。" % cap)
    first = pool.first_writes_due(root)
    for ch, done, refill in rows:
        mark = "（已认领，不用再开）" if done else ("（补位用）" if refill else "")
        prompt = ch["brief"]
        if first and ch["role"] == policy.get("scribe_role"):      # the scribe's first turn does the two first writes
            prompt = ch["claim"] + plan.get("scribe_first", "")
        lines.append("- %s：name=%s，title=“%s”，model=%s%s\n  prompt：%s\n  background：%s\n  然后：%s" % (
            ch["name"], ch["short"], ch["title"], ch["model"], mark, prompt, ch["background"], ch["expect"]))
    if entry is top:
        lines.append("开局句里写着“本会话由研究员手工开出”的 +2：开完以后用 set_session_title 把本会话的标题改成“%s”，"
                     "再用 move_sessions 把本会话归入侧栏分区“%s”（没有这个分区就先用 create_section 建），下面的会话跟着走；"
                     "回研究员一句“已认领 %s，已开 …”。开局句里没有这句话的 +2 不用做这一步，开它的会话会统一处理。"
                     "model 填不进去就不填，开好以后再改模型。" % (top["title"], plan.get("label") or "", claim["name"]))
    return "\n".join(lines)


def claim_cmd(spec, root, role, name=None, desk_arg=None, wait=CLAIM_WAIT):
    policy = need_pool(root)
    if not policy:
        return 0
    sid = os.environ.get(pool.SESSION_ENV) or ""
    desk = os.environ.get(pool.DESK_ENV) or desk_arg or ""
    if not sid:
        print("出错：拿不到本会话的 ID（CLAUDE_CODE_SESSION_ID）；这条命令要在会话自己的 Bash 里运行。")
        return 1
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    deadline = time.monotonic() + wait
    while True:
        pl, ok = pool.load(root)
        if not ok:
            print("出错：读不出 .claude/state/pool.json；请研究员先处理。")
            return 1
        claim = pool.claim_by_session(pl, sid) or pool.claim_by_desk(pl, desk)
        if claim:
            if claim["role"] != role:
                print("出错：本会话已经认领了 %s（%s），不能再认领 %s。" % (claim["role"], claim["name"], role))
                return 1
            if claim.get("session_id") != sid and not pool.cleared(root, claim):
                print("出错：认领 %s 属于另一个会话（它没有因为清空上下文而结束），不能接过来。要换会话，请研究员先 release。" % claim["name"])
                return 1
            if claim.get("session_id") != sid or (desk and claim.get("desk") != desk):
                with pool.locked(root) as pl2:
                    pl2["pending"] = [p for p in pl2["pending"] if p.get("session_id") != sid]
                    pl2["pending"].append({"kind": "attach" if claim.get("session_id") != sid else "desk",
                                           "name": claim["name"], "role": role, "session_id": sid,
                                           "desk": desk or None, "at": now()})
                print("待转正：本会话的 ID 有变化，这条命令结束时 hook 会把认领 %s 接到本会话上。" % claim["name"])
            print("已认领：%s。" % pool.describe(policy, claim))
            print(pool.injection(root, policy, claim, owners=pl.get("owners")))
            more = children_text(root, policy, claim) if policy["agents"][role]["rank"] >= 1 else ""
            if more:
                print("\n" + more)
            return 0
        permit = pool.permit_for(pl, sid, desk)
        if not permit and pool.card_permit(pl, pool.canonical(policy, name) if name else None, role):
            with pool.locked(root) as pl2:           # the session a start card opened: its permit waits under its name
                held = pool.card_permit(pl2, pool.canonical(policy, name), role)
                if held:
                    held.update(session_id=sid, desk=desk or None)
            continue
        if permit:
            if permit.get("role") != role:
                print("出错：本会话的许可是 %s（%s），不是 %s。" % (permit["role"], permit["name"], role))
                return 1
            with pool.locked(root) as pl2:
                pl2["pending"] = [p for p in pl2["pending"] if p.get("session_id") != sid]
                pl2["pending"].append({"kind": "claim", "name": permit["name"], "role": role, "session_id": sid,
                                       "desk": desk or permit.get("desk"), "at": now()})
            pool.log_event(root, {"event": "pending", "session_id": sid, "desk": desk or None,
                                  "name": permit["name"], "role": role})
            print("待转正：%s（%s）。这条命令结束时 hook 会转正，下一条命令起本会话就是 %s。" % (permit["name"], role, permit["name"]))
            print(pool.injection(root, policy, dict(permit, name=permit["name"]), owners=pl.get("owners")))
            more = children_text(root, policy, permit) if policy["agents"][role]["rank"] >= 1 else ""
            if more:                                 # what it opens below itself, right in its first turn
                print("\n" + more)
            return 0
        if pool.tombstoned(pl, sid, desk):
            print("出错：本会话原来的认领已经释放；要重新认领，请研究员或开它的会话重新预登记。")
            return 1
        if time.monotonic() >= deadline:
            print("出错：没有找到本会话认领 %s 的许可（等了 %d 秒）。开局第一句写着“认领 <+2 角色>（会话名 …）”的顶层会话由 hook 直接认领；"
                  "子会话要等开它的会话运行 route.py expect 预登记。研究员自己的会话不认领。" % (role, wait))
            return 1
        time.sleep(2)


def expect_cmd(spec, root, name, role, desk=None, session=None, card=False):
    policy = need_pool(root)
    if not policy:
        return 0
    me, origin = caller(root, policy)
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    full = pool.canonical(policy, name)
    fixed = pool.fixed_name(policy, role)
    if not full or pool.role_of(policy, full) != role or not pool.is_slot(full):
        print("出错：会话名 %s 和角色 %s 对不上；写成 %s 这样的名字。" % (name, role, fixed or pool.slot_name(policy["agents"][role], 1)))
        return 1
    if fixed and full != fixed:
        print("出错：%s 的会话名固定是 %s。" % (role, fixed))
        return 1
    if card and (desk or session):
        print("出错：--card 是给还没开出来的会话预登记的，不和 --desk、--session 一起用。")
        return 1
    if not desk and not session and not card:
        print("出错：要给 --desk <桌面会话 ID>（开会话时返回的 ID），终端里测试时给 --session <会话 ID>；提启动卡时给 --card。")
        return 1
    if origin == "claim":
        mine, target = policy["agents"][me["role"]], policy["agents"][role]
        ok = ((me["role"] == policy.get("entry_role") and role in (policy.get("router_role"), policy.get("scribe_role")))
              or (mine["rank"] == 2 and me["role"] != policy.get("entry_role")
                  and target["sequence"] == mine["sequence"] and target["rank"] in (0, 1))
              or (mine["rank"] == 1 and target["sequence"] == mine["sequence"] and target["rank"] == 0))
        if not ok:
            print("出错：%s 不能预登记 %s：+2 只预登记本序列的 +1、+0，+1 只预登记本序列的 +0，入口只预登记路由和书记员。" % (me["name"], role))
            return 1
    elif origin != "researcher":
        print("出错：本会话（%s）不能预登记。" % origin)
        return 1
    by = me["name"] if me else "researcher"
    cap = (policy.get("pool") or {}).get("max_sessions")
    with pool.locked(root) as pl:
        other = pool.claim_by_desk(pl, desk) or pool.claim_by_session(pl, session)
        if other and other["name"] == full and other.get("role") == role:
            print("%s（%s）已经由这个会话认领，不用再预登记。" % (full, role))        # the opening run again after an interruption
            return 0
        if other:
            print("出错：这个会话已经认领了 %s。" % other["name"])
            return 1
        if full in pl["claims"]:
            print("出错：%s 已经被认领；换一个名字，或请研究员先 release。" % full)
            return 1
        def same_target(p):
            return (desk and p.get("desk") == desk) or (session and p.get("session_id") == session) or p.get("name") == full
        pl["permits"] = [p for p in pl["permits"] if not same_target(p)]   # a new pre-registration replaces the old one
        held = pool.claims_of(pl, role)
        waiting = [p for p in pl["permits"] if p.get("role") == role]
        if len(held) + len(waiting) >= pool.role_limit(policy, role):
            print("出错：%s 已有 %d 条认领、%d 条许可，上限 %d。" % (role, len(held), len(waiting), pool.role_limit(policy, role)))
            return 1
        if cap and len(pl["claims"]) + len(pl["permits"]) >= cap:
            print("出错：会话池已有 %d 条认领和许可，到了任务文件的总上限 max_sessions=%d；先让研究员归档不用的会话并 release。" % (
                len(pl["claims"]) + len(pl["permits"]), cap))
            return 1
        pl["permits"].append({"name": full, "role": role, "desk": desk or None, "session_id": session or None,
                              "by": by, "via": "card" if card else "expect", "at": now()})
        if card:                                     # the entry lists the request with where its card waits
            for r in pl["refills"]:
                if r.get("role") == role and not r.get("card_in"):
                    r["card_in"] = by
                    break
    pool.log_event(root, {"event": "expect", "name": full, "role": role, "desk": desk, "session_id": session, "by": by,
                          "card": card or None})
    if card:
        print("已预登记 %s（%s），等研究员点启动卡：卡片开出的会话运行 route.py claim %s --as %s 时转正。%d 天内没人认领，这条预登记作废。" % (
            full, role, role, full, pool.CARD_DAYS))
    else:
        print("已预登记 %s（%s），%s。对方运行 route.py claim 时转正。" % (full, role, "桌面会话 %s" % desk if desk else "会话 %s" % session))
    return 0


def release_cmd(spec, root, name):
    policy = need_pool(root)
    if not policy:
        return 0
    me, origin = caller(root, policy)
    if origin != "researcher":
        print("出错：只有研究员能释放认领，在研究员自己的会话或终端里运行。")
        return 1
    full = pool.canonical(policy, name) or name
    with pool.locked(root) as pl:
        claim = pl["claims"].pop(full, None)
        if claim:
            pl["tombstones"].append(dict(claim, released_at=now(), released_by="researcher"))
            pl["focus"].pop(full, None)
        permits = [p for p in pl["permits"] if p.get("name") == full]
        pending = [p for p in pl["pending"] if p.get("name") == full]
        pl["permits"] = [p for p in pl["permits"] if p.get("name") != full]
        pl["pending"] = [p for p in pl["pending"] if p.get("name") != full]
    if not claim and not permits and not pending:
        print("出错：认领表里没有 %s，也没有它的许可。" % full)
        return 1
    pool.log_event(root, {"event": "release", "name": full, "role": (claim or {}).get("role"),
                          "session_id": (claim or {}).get("session_id"), "permits": len(permits), "pending": len(pending)})
    if claim:
        print("已释放 %s（%s）。原来的会话不再代表这个角色；+2 照 pool-plan.json 的重建句新建顶层会话，其余角色由开它的会话重新预登记。" % (
            full, claim["role"]))
    if permits or pending:
        print("已撤掉 %s 的 %d 条许可、%d 条待转正。" % (full, len(permits), len(pending)))
    return 0


STATUS_CN = {"busy": "在忙", "idle": "空闲", "idle_bg": "空闲（后台任务在跑）", "waiting_researcher": "等研究员批准",
             "offline": "离线", "uncertain": "不确定（标着在忙，15 分钟没有工具调用）", "unknown": "未知"}


def status_cn(status):
    status = status or "unknown"
    if status.startswith("failed"):
        return "失败（%s）" % status.partition(":")[2]
    return STATUS_CN.get(status, status)


def suggest(policy, rows, role, item, pl):
    """Who to send this role's work to: an idle session (the work item's owner first), else refill or queue."""
    mine = [r for r in rows if r["role"] == role]
    owner = (pl.get("owners") or {}).get(str(item)) if item else None
    free = [r for r in mine if r["status"] in ("idle", "idle_bg") and not r["focus"]]
    if owner and any(r["name"] == owner for r in free):
        return "发给 %s：它是 #%s 的负责会话，现在空闲。" % (owner, item)
    if free:
        return "发给 %s（空闲）。" % free[0]["name"]
    cap = (policy.get("pool") or {}).get("max_sessions")
    if cap and len(pl["claims"]) + len(pl["permits"]) >= cap:
        return "%s 没有空闲的会话，会话池也到了总上限 %d：留在“待开工”排队，等有会话空出来。" % (role, cap)
    if len(mine) < pool.role_limit(policy, role):
        return "%s 没有空闲的会话，还没到上限 %d：排队，或运行 route.py refill %s 记一条补位请求，%s的会话提启动卡，研究员点一下就开。" % (
            role, pool.role_limit(policy, role), role, opener(policy, role))
    return "%s 没有空闲的会话，已到上限 %d：留在“待开工”排队，等有会话空出来。" % (role, pool.role_limit(policy, role))


def drop_expired_focus(root, policy, pl):
    """Remove focus flags whose sessions have been idle for 30 minutes, so they do not come back."""
    sessions = pool.all_sessions(root)
    stale = [n for n in (pl.get("focus") or {}) if n in pl["claims"]
             and pool.focus_expired(sessions.get(pl["claims"][n].get("session_id")) or {})]
    if not stale:
        return pl
    with pool.locked(root) as locked_pool:
        for n in stale:
            locked_pool["focus"].pop(n, None)
    for n in stale:
        pool.log_event(root, {"event": "focus_expired", "name": n})
        pool.update_session(root, pl["claims"][n].get("session_id") or "", lambda st: st.update({"focus_lapsed": True}))
    return pool.load(root)[0]


def open_refills(pl):
    return [r for r in pl.get("refills", []) if int(r.get("count") or 1) > 0]


def print_pool(root, policy, role=None, item=None):
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json；请研究员先处理。")
        return 1
    pl = drop_expired_focus(root, policy, pl)
    rows = pool.session_view(root, policy, pl)
    shown = [r for r in rows if not role or r["role"] == role]
    total = len(pl["claims"]) + len(pl["permits"])
    print("会话池：%d 条认领，%d 条待转正的许可，上限 %s 个会话。" % (len(pl["claims"]), len(pl["permits"]),
                                                       (policy.get("pool") or {}).get("max_sessions", "?")))
    for r in shown:
        extra = []
        if r["work_items"]:
            extra.append("负责 " + "、".join("#" + i for i in r["work_items"]))
        if r["focus"]:
            left = "，还剩 %d 分钟" % r["focus_left_min"] if r["focus_left_min"] is not None else ""
            extra.append("研究员在用（%s%s）" % (r["focus"].get("topic") or "未写", left))
        if r["quota"]:
            extra.append("额度暂停（%s）" % r["quota"])
        if r["denied"]:
            extra.append("被拦 %d 次" % r["denied"])
        print("- %s（%s）%s；最后活动 %s；桌面会话 %s；短名 %s%s" % (
            r["name"], r["role"], status_cn(r["status"]), r["last"] or "无记录", r["desk"] or "未知", r["short"],
            "；" + "；".join(extra) if extra else ""))
    for p in pl["permits"]:
        if not role or p.get("role") == role:
            print("- 待转正：%s（%s），预登记方 %s" % (p.get("name"), p.get("role"), p.get("by")))
    for r in open_refills(pl):
        if not role or r.get("role") == role:
            where = pl["claims"].get(r.get("card_in") or "") or {}
            card = ("；启动卡在 %s 的会话里（桌面会话 %s），研究员点一下就开" % (r["card_in"], where.get("desk") or "未知") if r.get("card_in")
                    else "；还没有提启动卡")
            print("- 补位请求：%s ×%s（%s，%s 提）%s" % (r.get("role"), r.get("count", 1), r.get("by"), str(r.get("at"))[:16], card))
    todos = [] if role else open_todos(pl)
    for t in [t for t in todos if not t.get("kind")]:
        print("- 研究员任务 %d：%s（%s 提，%s%s%s）" % (
            t["n"], t.get("text"), t.get("by"), str(t.get("at"))[:16], "，为 #%s" % t["item"] if t.get("item") else "",
            "；做完算数：%s" % t["done_when"] if t.get("done_when") else ""))
    items = [t for t in todos if t.get("kind")]
    if items:                                        # however many: one thing to tell the researcher
        print("- 研究员任务·飞书项目：%d 条工作项等研究员在网页上处理，合成一件事跟他说，下面每条附给他；"
              "他说处理完了，运行 route.py pool --prune --apply 核对台账、划掉办了的。" % len(items))
        for t in items:
            print("  - #%s %s：%s；原因：%s（%s 提）%s" % (t["item"], ITEM_KINDS.get(t["kind"], t["kind"]), t.get("title") or "名称没读到",
                                              t.get("text"), t.get("by"), "；%s" % t["link"] if t.get("link") else ""))
    mixed = pool.mixed_modes(rows)
    if mixed:
        print("提醒：%s 的权限模式和池里多数会话不是一类（bypass 和其他模式之间的消息会被扣下）。" % "、".join(mixed))
    near = [r["name"] for r in rows if (r["denied"] or 0) >= 15]
    if near:
        print("提醒：%s 被自动模式拦了 15 次以上，到 20 次会退回逐条询问。" % "、".join(near))
    cap = (policy.get("pool") or {}).get("max_sessions")
    if cap and total >= cap - 2:
        print("提醒：会话数 %d 接近总上限 %d。" % (total, cap))
    if role:
        print("建议：" + suggest(policy, rows, role, item, pl))
        target = next((r for r in shown if r["status"] in ("idle", "idle_bg") and not r["focus"]), None)
        if target:
            print("发法：用会话管理工具发消息（%s），对方的桌面会话 ID 是 %s；同一家族里也可以写短名 %s。"
                  "抬头里写对方的池位名 %s。回执是 delivered 或 queued 才算发出。" % (
                      policy.get("channel", "send_message"), target["desk"] or "未知", target["short"], target["name"]))
    return 0


def queue_rows(root, policy, me, seq):
    """The 待开工 work items this claim dispatches: for the entry, cross-sequence requests waiting
    for a +2 and 下发 packages it wrote; for a +2 or +1, the packages of its sequence it wrote."""
    lg = ledger.Ledger(root, "preview")
    tag = "（%s）" % me["name"]
    rows = []
    with contextlib.redirect_stdout(io.StringIO()):
        if me["role"] == policy.get("entry_role"):
            leased = set((pool.load(root)[0].get("owners") or {}))
            rows += [r for r in ledger.query(lg, "cross", None, as_json=False)
                     if r.get("work_item_status") == lg.state("cross_request", "waiting") and str(r.get("work_item_id")) not in leased]
            rows += [r for r in ledger.query(lg, "open", None, as_json=False)
                     if r.get("work_item_status") == lg.state("same_seq", "queued") and tag in str(r.get("name", ""))]
        else:
            rows += [r for r in ledger.query(lg, "open", seq, as_json=False)
                     if r.get("work_item_status") == lg.state("same_seq", "queued") and tag in str(r.get("name", ""))]
    return rows


def queue_cmd(root, policy, name, offline=False):
    pl, _ = pool.load(root)
    me = pl["claims"].get(pool.canonical(policy, name) or name)
    if not me:
        print("出错：%s 不在认领表里。" % name)
        return 1
    seq = policy["agents"][me["role"]]["sequence"]
    rows = pool.session_view(root, policy, pl)
    free = [r["name"] for r in rows if r["status"] in ("idle", "idle_bg") and not r["focus"] and r["name"] != me["name"]
            and pool.kind_allowed(policy, "assign", me["role"], r["role"])]
    print("能委托的空闲会话：%s" % ("、".join(free) or "无"))
    if offline:
        print("（离线，没查台账里待开工的工作项）")
        return 0
    try:
        items = queue_rows(root, policy, me, seq)
    except ledger.LedgerError as e:
        print("台账没读到：%s" % str(e)[:120])
        return 1
    for row in items:
        print("- %s #%s %s" % (row.get("work_item_status"), row.get("work_item_id"), row.get("name", "")))
    if not items:
        print("没有等你委托的工作项。没事就一句话结束本轮。")
    elif free:
        print("有等你委托的事、又有空闲会话：先请书记员确认“开工”这一步（跨序列需求是请对方 +2 接单），再委托出去。")
    return 0


RECOVER_BEFORE = ("busy", "uncertain", "waiting_researcher")


def recover_cmd(root, policy):
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json")
        return 1
    rows = pool.session_view(root, policy, pl)
    entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
    word = (policy.get("msg") or {}).get("resume", "继续")
    print("恢复清单：")
    unfinished, replies = [], []
    for path in pool.files_in(os.path.join(root, "packages", "sent"), "*.json"):
        rec = load_json(path, {}) or {}
        if isinstance(rec, dict) and rec.get("status") not in (None, "registered", "replied"):
            if os.path.basename(path).startswith("reply-"):
                replies.append("%s（答复 #%s，%s）" % (rec.get("draft"), rec.get("parent"), rec.get("status")))
            else:
                unfinished.append("%s（#%s，%s）" % (rec.get("draft"), rec.get("work_item_id"), rec.get("status")))
    if unfinished:
        print("- 书记员先补完写到一半的登记，逐条运行 ledger.py register <草稿> --resume：%s" % "；".join(unfinished))
    if replies:
        print("- 书记员补完写到一半的答复，逐条重跑 ledger.py reply <草稿>（会接着上次的回执做）：%s" % "；".join(replies))
    for r in rows:
        st, before = r["status"], r.get("prev_status") or ""
        if before == "idle_bg":
            print("- %s：应用退出前有后台任务在跑，后台任务随应用退出停了。到侧栏点开它看一眼，需要的话重新启动。" % r["name"])
        elif before in RECOVER_BEFORE or before.startswith("failed"):
            print("- %s：应用退出前%s。到侧栏点开它看一眼，需要的话重发上一条消息。" % (r["name"], status_cn(before)))
        elif st in RECOVER_BEFORE or st.startswith("failed"):
            print("- %s：%s。到侧栏点开它看一眼，需要的话按回车或重发上一条消息。" % (r["name"], status_cn(st)))
        if r.get("quota"):
            print("- %s：额度暂停过（%s），要按回车才继续的，到侧栏点开它。" % (r["name"], r["quota"]))
    found = False
    items = pool.all_open_items(root, pl.get("owners"))
    for name in sorted(pl["claims"]):
        sent, _ = items.get(name, ({}, {}))
        for item, to in sorted(sent.items()):
            found = True
            if name == entry:
                print("- #%s：你委托给 %s，还没交付。发：【%s #%s】%s → %s：重启后接着做；做完了的，重发交付。" % (
                    item, to, word, item, entry, to))
            else:
                print("- #%s：%s 委托给 %s，还没交付。给 %s 发消息【%s #%s】%s → %s：重启后请给 %s 发【%s #%s】。" % (
                    item, name, to, name, (policy.get("msg") or {}).get("notice", "通知"), item, entry, name, to, word, item))
    if not found:
        print("- 没有委托出去还没交付的工作项。")
    print("- 长期任务（比如 route.py listen）随应用退出就停了，要研究员在终端里重新启动。")
    return 0


def watch_cmd(root, policy, once=False, every=30):
    while True:
        pl, ok = pool.load(root)
        if ok:
            pl = drop_expired_focus(root, policy, pl)
        rows = pool.session_view(root, policy, pl) if ok else []
        order = {"waiting_researcher": 0, "uncertain": 2}
        rows.sort(key=lambda r: (0 if r["status"] == "waiting_researcher" else 1 if r["status"].startswith("failed")
                                 else order.get(r["status"], 3), r["name"]))
        if not once:
            sys.stdout.write("\033[2J\033[H")
        print("会话池 %s（每 %d 秒刷新，Ctrl-C 退出）" % (now(), every))
        for r in rows:
            print("%-14s %-22s %s %s" % (r["name"], status_cn(r["status"]), " ".join("#" + i for i in r["work_items"]),
                                         "研究员在用" if r["focus"] else ""))
        pending = open_refills(pl)
        if pending:
            print("待补位：" + "；".join("%s×%s（%s）" % (p.get("role"), p.get("count", 1), str(p.get("at"))[5:16]) for p in pending))
        if once:
            return 0
        sys.stdout.flush()
        time.sleep(every)


def open_todos(pl):
    return [t for t in pl.get("todos", []) if isinstance(t.get("n"), int)]


ITEM_KINDS = {"terminate": "终止", "delete": "删除"}          # what the researcher is asked to do with a work item


def item_end(lg, item):
    """What became of a work item: (state name, its name) when it is archived or terminated, ("已删除", "") when
    the space no longer has it, None while it is open. A ledger that cannot be read is LedgerError: an item
    that is gone and a ledger that does not answer are told apart by what the space says (WorkItemNotFound)."""
    try:
        it = lg.item(item)
    except ledger.LedgerError as e:
        if "WorkItemNotFound" in str(e):
            return "已删除", ""
        raise
    if it is None:
        raise ledger.LedgerError("读不到 #%s" % item)
    return (it.get("state"), it.get("name") or "") if ledger.finished(lg, it.get("state"), it.get("type")) else None


def todo_cmd(spec, root, action, n=None, text=None, item=None, done_when=None, note=None, kind=None):
    """研究员任务清单: what a session needs the researcher to do by hand where nothing is to be decided (fetch a
    key, connect a VPN, run a command only they may run). Until 1.0.9 it took a decision package, six to
    eight turns each. It is a line in pool.json that the entry lists after the decisions; no work item, no
    document. What tells it from a decision is for the session that writes it to judge: nothing here checks.

    A work item to be terminated or deleted on the web page (kind) is such a line too: one that was made by
    mistake or withdrawn before its work began cannot be archived, and no script terminates one. The entry
    tells the researcher of all of them as one thing (pool_cmd). Nobody looks whether it was done; when the
    researcher says so, `route.py pool --prune --apply` reads the ledger once and strikes those that are."""
    policy = need_pool(root)
    if not policy:
        return 0
    by = sender_label(*caller(root, policy))
    if action == "add":
        if not (text or "").strip():
            print("出错：--text 写要研究员做什么；请他终止或删除工作项的，写为什么不要这条工作项了")
            return 1
        item = str(item).lstrip("#") if item else None
        if kind and not item:
            print("出错：--terminate、--delete 要和 --for <工作项 ID> 一起用")
            return 1
        title = link = None
        if kind:                                     # the item's name and link, read once, for the researcher to click
            try:
                lg = ledger.Ledger(root, "preview")
                it = lg.item(item)
                if it:
                    title, link = it.get("name"), lg.item_url(it.get("type"), item) if it.get("type") else None
            except Exception:                        # the name and the link are a convenience: the line is written without
                pass
        with pool.locked(root) as pl:
            same = next((t for t in open_todos(pl) if kind and t.get("kind") and t.get("item") == item), None)
            if not same:
                number = max([t["n"] for t in open_todos(pl)] + [int(pl.get("todo_seq") or 0)]) + 1
                pl["todo_seq"] = number
                pl["todos"].append({"n": number, "text": text.strip(), "item": item, "kind": kind, "title": title, "link": link,
                                    "done_when": (done_when or "").strip() or None, "by": by, "at": now()})
        if same:
            print("#%s 已经在研究员任务清单里了（第 %d 条，%s 提），没有再记。" % (item, same["n"], same.get("by")))
            return 0
        pool.log_event(root, {"event": "todo", "n": number, "by": by, "item": item, "kind": kind, "line": text.strip()[:200]})
        entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
        if kind:
            said = "研究员任务清单 %d：飞书项目里的 #%s 等研究员%s（%s）。这类工作项不管有几条，合成一件事跟研究员说，不算决策；他说处理完了，运行 route.py pool --prune --apply 核对。" % (
                number, item, ITEM_KINDS[kind], text.strip())
        else:
            said = "研究员任务清单 %d：%s%s。研究员来谈时跟在决策清单后面列出，不算决策；他做完了，运行 route.py todo done %d。" % (
                number, text.strip(), "（为 #%s）" % item if item else "", number)
        append_inbox(root, entry, {"id": uuid.uuid4().hex[:12], "ts": now(), "from": by, "read": False, "text": said})
        if kind:
            print("已记进研究员任务清单（第 %d 条）：#%s 等研究员在网页上%s。入口把清单上这类工作项合成一件事跟研究员说；"
                  "他处理完告诉入口，入口再核对台账划掉。不用为它写决策包，也不要去推它的节点。" % (number, item, ITEM_KINDS[kind]))
        else:
            print("已记进研究员任务清单（第 %d 条）：%s。入口向研究员列出时会带上它。研究员做完了，或者你自己核对到了结果，"
                  "运行 route.py todo done %d --note '<研究员的话或核对到的结果>'。" % (number, text.strip(), number))
        return 0
    with pool.locked(root) as pl:
        hit = next((t for t in open_todos(pl) if t["n"] == n), None)
        if hit:
            pl["todos"].remove(hit)
    if not hit:
        print("研究员任务清单里没有第 %s 条；route.py pool 列着现在还开着的。" % n)
        return 1
    pool.log_event(root, {"event": "todo_done", "n": n, "by": by, "item": hit.get("item"), "line": (note or "").strip()[:200]})
    print("研究员任务清单第 %d 条已划掉：%s。%s" % (n, hit["text"], "" if hit.get("kind") or not hit.get("item") else
                                    "请书记员在 #%s 下加一条评论，写研究员做了什么%s。" % (
                                        hit["item"], "（%s）" % note.strip() if (note or "").strip() else "")))
    return 0


def refill_cmd(spec, root, role, count=1):
    policy = need_pool(root)
    if not policy:
        return 0
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    by = sender_label(*caller(root, policy))
    if count < 0:
        print("出错：--count 写 0 是撤回这个角色的补位请求，写正数是再要几个会话")
        return 1
    if count == 0:                                   # withdrawn: until a session of the role was opened, a request stayed
        with pool.locked(root) as pl:
            dropped = [r for r in pl["refills"] if r.get("role") == role]
            pl["refills"] = [r for r in pl["refills"] if r.get("role") != role]
        if not dropped:
            print("没有 %s 的补位请求，没有东西可撤。" % role)
            return 0
        pool.log_event(root, {"event": "refill_withdrawn", "role": role, "by": by, "requests": len(dropped)})
        print("已撤回 %s 的补位请求（%d 条）。入口的清单里不再列它。" % (role, len(dropped)))
        return 0
    with pool.locked(root) as pl:
        pl["refills"].append({"role": role, "count": count, "by": by, "at": now()})
    pool.log_event(root, {"event": "refill", "role": role, "count": count, "by": by})
    entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
    append_inbox(root, entry, {"id": uuid.uuid4().hex[:12], "ts": now(), "from": by, "read": False,
                               "text": "补位请求：%s 再开 %d 个会话。研究员来谈时跟在决策清单后面列出，补位请求不算决策；%s的会话会提一张启动卡，"
                                       "研究员在那个会话里点一下就开。列的时候照 route.py pool 写明卡片在哪个会话。" % (
                                   role, count, opener(policy, role, "那个序列"))})
    me, origin = caller(root, policy)
    mine, target = (policy["agents"].get(me["role"]) if me else None), policy["agents"][role]
    opens = bool(mine) and mine["sequence"] == target["sequence"] and me["role"] != role and (
        mine["rank"] == target["rank"] + 1 or mine["rank"] == 2 and not (policy.get("pool") or {}).get("p1"))
    if opens:
        how = ("这个角色由你开：本轮接着提一张启动卡，照 `route.py claim %s --as %s` 输出里“补位用”的那一行"
               "（先 expect --card，再 start_session，initiation 写 own_initiative）。" % (me["role"], me["name"]))
    else:
        pl, _ = pool.load(root)
        up = sorted(c["name"] for c in pl["claims"].values() if c.get("role") in policy["agents"]
                    and policy["agents"][c["role"]]["sequence"] == target["sequence"]
                    and policy["agents"][c["role"]]["rank"] == target["rank"] + 1)
        how = "这个角色由%s开：%s" % (opener(policy, role), "给 %s 发一条【%s】，请它为这条补位请求提启动卡。" % (
            "、".join(up), spec.term("msg", "notice")) if up else "那一级还没有会话，先请研究员开出来。")
    print("已记补位请求：%s ×%d。要研究员在提卡片的那个会话里点一下才会开，不会自动开；入口向研究员列出时附那个会话。%s新会话认领后这条请求自动销掉。" % (
        role, count, how))
    return 0


def audit_cmd(spec, root, hours=24):
    policy = need_pool(root)
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


def prune_cmd(root, apply=False):
    """Take what is over out of pool.json: the work items that are archived, terminated or deleted leave the
    table of owners, and those the researcher was asked to terminate or delete leave the 研究员任务清单. A work
    item is terminated or deleted on the web page, where no script sees it, so both are checked against the
    ledger, when the researcher says it is done and not before. Nothing is written without --apply, and
    nothing when the ledger cannot be read; an item the space no longer has is over, not unread (until 1.0.10
    one deleted item in the table stopped this command for good). Nothing but these entries is touched."""
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json；请研究员先处理。")
        return 1
    owners = pl.get("owners") or {}
    asked = [t for t in open_todos(pl) if t.get("kind") and t.get("item")]
    gone, struck, waiting = [], [], []
    try:
        lg = ledger.Ledger(root, "preview")
        ends = {item: item_end(lg, item) for item in sorted(set(owners) | {t["item"] for t in asked})}
    except (ledger.LedgerError, orgspec.OrgSpecError) as e:
        print("出错：台账读不到，认领表和研究员任务清单都没有动：%s" % str(e)[:300])
        return 1
    for item, name in sorted(owners.items()):
        if ends[item]:
            gone.append((item, name, ends[item][0], ends[item][1]))
    for t in asked:
        (struck if ends[t["item"]] else waiting).append(t)
    if not gone and not struck:
        print("认领表里的 %d 条工作项都还没归档，也没终止，没有要移出的。" % len(owners))
        if waiting:
            print("研究员任务清单里等研究员处理的 %d 条工作项都还没处理：%s。" % (len(waiting), "、".join("#%s" % t["item"] for t in waiting)))
        return 0
    if gone:
        print("认领表里有 %d 条工作项，其中 %d 条已经归档、终止或删除：" % (len(owners), len(gone)))
        for item, name, state, title in gone:
            print("- #%s（%s）%s，认领表里记在 %s 名下" % (item, state, title, name))
    if struck:
        print("研究员任务清单里等研究员处理的工作项，有 %d 条已经办了：%s。" % (
            len(struck), "、".join("#%s（%s）" % (t["item"], ends[t["item"]][0]) for t in struck)))
    if waiting:
        print("还有 %d 条没处理，留在清单上，告诉研究员：%s。" % (len(waiting), "、".join("#%s" % t["item"] for t in waiting)))
    if not apply:
        print("这是预览，认领表和研究员任务清单都没有动。加 --apply 才写。")
        return 0
    with pool.locked(root) as held:
        done = [item for item, name, _, _ in gone if held.get("owners", {}).get(item) == name and held["owners"].pop(item)]
        numbers = {t["n"] for t in struck}
        off = [t for t in held.get("todos", []) if t.get("n") in numbers]
        held["todos"] = [t for t in held.get("todos", []) if t.get("n") not in numbers]
    pool.log_event(root, {"event": "prune", "items": done, "todos": [t["item"] for t in off]})
    print("已从认领表移出 %d 条，研究员任务清单划掉 %d 条。" % (len(done), len(off)))
    return 0


def pool_cmd(spec, root, args):
    policy = need_pool(root)
    if not policy:
        return 0
    if args.prune:
        return prune_cmd(root, args.apply)
    if args.recover:
        return recover_cmd(root, policy)
    if args.watch:
        return watch_cmd(root, policy, args.once)
    if args.queue:
        return queue_cmd(root, policy, args.queue, args.offline)
    return print_pool(root, policy, args.role, args.item)


# --------------------------------------------------------------- listen
def listen(root, max_events=None):
    """Append the researcher's Feishu DMs to inbox/feishu.jsonl. Run by the researcher.

    `event consume im.message.receive_v1` prints one flat JSON object per message:
    type, sender_id (open_id), sender_type, chat_type, content (already readable
    text) and message_id, which is the key to deduplicate on.
    """
    ids = load_json(os.path.join(root, "feishu", "ids.json"), {})
    researcher = (ids.get("lark") or {}).get("researcher_open_id")
    if not researcher:
        print("feishu/ids.json 里没有研究员的 open_id（lark.researcher_open_id），不启动监听：否则谁私聊机器人都会进入口。"
              "先跑 setup.py feishu --live --apply 读回。")
        return 2
    path = os.path.join(root, "inbox", "feishu.jsonl")
    seen = set()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line).get("id"))
                except ValueError:
                    pass
    cmd = ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"]
    if max_events:
        cmd += ["--max-events", str(max_events)]
    print("监听飞书私聊：%s（Ctrl-C 停止）" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type", "im.message.receive_v1") != "im.message.receive_v1":
            continue
        if ev.get("sender_type") != "user" or ev.get("chat_type") != "p2p" or ev.get("sender_id") != researcher:
            continue
        key = ev.get("message_id") or ev.get("id")
        text = ev.get("content")
        if not key or not text or key in seen:
            continue
        seen.add(key)
        append_inbox(root, "feishu", {"id": key, "ts": now(), "from": "研究员（飞书私聊）", "text": text, "read": False})
        print("收到：%s" % text[:60])
    return proc.wait()


def main(argv=None):
    ap = argparse.ArgumentParser(description="入口的机械路由、收件箱和聚焦会话", allow_abbrev=False)
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("classify")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--draft")
    p = sub.add_parser("notify")
    p.add_argument("role", help="角色（alg-p2）或会话池里的会话名（Alg-p2-01）")
    p.add_argument("--text", required=True)
    p.add_argument("--work-item")
    p.add_argument("--from", dest="sender")
    p = sub.add_parser("focus")
    p.add_argument("action", choices=["open", "close", "list"])
    p.add_argument("role", nargs="?")
    p.add_argument("--topic")
    p = sub.add_parser("claim", allow_abbrev=False)
    p.add_argument("role")
    p.add_argument("--as", dest="name")
    p.add_argument("--desk", help="环境里拿不到桌面会话 ID 时，从会话管理工具读出自己的 ID 传进来")
    p.add_argument("--wait", type=int, default=CLAIM_WAIT)
    p = sub.add_parser("expect", allow_abbrev=False)
    p.add_argument("name")
    p.add_argument("--role", required=True)
    p.add_argument("--desk")
    p.add_argument("--session")
    p.add_argument("--card", action="store_true", help="给启动卡将要开出的会话预登记：按会话名等它认领，不带桌面会话 ID")
    p = sub.add_parser("release", allow_abbrev=False)
    p.add_argument("name")
    p = sub.add_parser("pool")
    p.add_argument("--role")
    p.add_argument("--item")
    p.add_argument("--queue")
    p.add_argument("--recover", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--prune", action="store_true", help="对照台账，把已归档和已终止的工作项移出认领表；不加 --apply 只预览")
    p.add_argument("--apply", action="store_true", help="和 --prune 一起用：写入")
    p = sub.add_parser("refill")
    p.add_argument("role")
    p.add_argument("--count", type=int, default=1, help="再要几个会话；0 是撤回这个角色的补位请求")
    p = sub.add_parser("todo")
    p.add_argument("action", choices=("add", "done"))
    p.add_argument("n", nargs="?", type=int, help="done 时写：研究员任务清单里要划掉的那一条的编号")
    p.add_argument("--text", help="add 时写：要研究员做什么；带 --terminate、--delete 时写为什么不要这条工作项")
    p.add_argument("--for", dest="item", metavar="ID", help="add 时写：为哪条工作项")
    p.add_argument("--terminate", dest="kind", action="store_const", const="terminate", help="add 时写：请研究员在网页上终止 --for 的工作项")
    p.add_argument("--delete", dest="kind", action="store_const", const="delete", help="add 时写：请研究员在网页上删除 --for 的工作项")
    p.add_argument("--done-when", help="add 时写：做完怎么算数")
    p.add_argument("--note", help="done 时写：研究员的话，或者核对到的结果")
    p = sub.add_parser("audit")
    p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("listen")
    p.add_argument("--max-events", type=int,
                   help="lark-cli 收到这么多条事件就退出；别人发给机器人的私聊、群消息也算数，不只算收进收件箱的")
    args = ap.parse_args(argv)
    spec = orgspec.OrgSpec()
    root = ledger.project_root()
    codex = os.path.isfile(os.path.join(root, ".agent-org", "manifest.json"))
    if codex and args.cmd != "classify":
        print("Codex 的会话操作使用 codex_runtime.py；本入口只复用 classify 的业务路由")
        return 2
    if args.cmd == "classify":
        out = classify_text(spec, root, args.text, args.offline) if args.text else classify_draft(spec, root, args.draft)
        if codex:
            out = json.loads(json.dumps(out, ensure_ascii=False).replace("route.py notify", "entry 技能的通知流程"))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "notify":
        notify(spec, root, args.role, args.text, args.work_item, args.sender)
        return 0
    if args.cmd == "focus":
        focus(spec, root, args.action, args.role, args.topic, args.offline)
        return 0
    if args.cmd == "listen":
        return listen(root, args.max_events)
    if args.cmd == "claim":
        return claim_cmd(spec, root, args.role, args.name, args.desk, args.wait)
    if args.cmd == "expect":
        return expect_cmd(spec, root, args.name, args.role, args.desk, args.session, args.card)
    if args.cmd == "release":
        return release_cmd(spec, root, args.name)
    if args.cmd == "pool":
        return pool_cmd(spec, root, args)
    if args.cmd == "refill":
        return refill_cmd(spec, root, args.role, args.count)
    if args.cmd == "todo":
        if args.action == "done" and args.n is None:
            print("出错：todo done 后面写编号，route.py pool 列着")
            return 1
        return todo_cmd(spec, root, args.action, args.n, args.text, args.item, args.done_when, args.note, args.kind)
    if args.cmd == "audit":
        return audit_cmd(spec, root, args.hours)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
