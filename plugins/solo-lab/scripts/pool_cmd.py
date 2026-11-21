#!/usr/bin/env python3
"""The commands of the session pool, run as pool.py <command>. They are kept out of pool.py because the hooks
load that file on every tool call and must not pay for what the commands import.

    pool.py claim <role> [--as NAME] [--desk ID]    claim the role a permit names (the hooks confirm it)
    pool.py expect NAME --role R (--desk ID | --session ID)   pre-register a session you opened
    pool.py release NAME                      the researcher frees a claim (it becomes a tombstone)
    pool.py show [--role R] [--item ID] [--queue NAME] [--recover] [--watch [--once]]
    pool.py show --prune [--apply]            take archived, terminated and deleted work items out of the table of who
                                              answers for what (pool.json owners) and strike those the researcher was
                                              asked to terminate or delete, read from the ledger; a preview without --apply
    pool.py refill <role> [--count N]         ask for more sessions of a role: a refill request in pool.json and the entry's
                                              inbox; --count 0 withdraws the role's requests
    pool.py open --target DIR [...]           the sessions an initialisation opens (pool_open.py, where the plugin is installed)

Standard library only.
"""
import argparse
import contextlib
import io
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import ledger_core  # noqa: E402
import pool  # noqa: E402

CLAIM_WAIT = 60


def plan_of(root):
    return pool.load_json(os.path.join(root, ".claude", "solo-lab", "pool-plan.json"), {}) or {}


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
    lines = ["开局句点了名要你开的会话，照下表开其中还没认领的（开局句没提开会话的，不开，用 pool.py show 找回已有的会话）；"
             "研究员在本会话里说“补位”时，开表里“补位用”的那一行。每个都用会话管理工具 start_session（它还没加载时，先用 ToolSearch 查 start_session 加载进来）：initiation 写 user_asked，context 写 fresh，"
             "use_worktree 写 false，权限模式不填（继承本会话）；name、title、model、prompt、background 照下表原样填。开好马上用返回的桌面会话 ID 运行表里的 expect。"
             "这是研究员初始化这个项目时定下的开法：下一级由上一级开出，侧栏里它才嵌在上一级下面。"
             "应用回“Not started: …”时，是会话自己开出的会话到了应用的上限（同时在跑的满 5 个，或者研究员这条消息以后已经开了 10 个）："
             "这一个先不开，也不要改提启动卡，回话里写明还差哪一个；研究员在本会话里说了，或者应用再叫醒你时，再照这张表开。",
             "补位请求（pool.py refill 记下的）不等研究员来说：记下的那一轮就为表里“补位用”的那一行提一张启动卡。先运行那一行的 expect，把 "
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
    policy = pool.need_pool(root)
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
                                           "desk": desk or None, "at": pool.stamp()})
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
                                       "desk": desk or permit.get("desk"), "at": pool.stamp()})
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
                  "子会话要等开它的会话运行 pool.py expect 预登记。研究员自己的会话不认领。" % (role, wait))
            return 1
        time.sleep(2)


def expect_cmd(spec, root, name, role, desk=None, session=None, card=False):
    policy = pool.need_pool(root)
    if not policy:
        return 0
    me, origin = pool.caller(root, policy)
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
                              "by": by, "via": "card" if card else "expect", "at": pool.stamp()})
        if card:                                     # the entry lists the request with where its card waits
            for r in pl["refills"]:
                if r.get("role") == role and not r.get("card_in"):
                    r["card_in"] = by
                    break
    pool.log_event(root, {"event": "expect", "name": full, "role": role, "desk": desk, "session_id": session, "by": by,
                          "card": card or None})
    if card:
        print("已预登记 %s（%s），等研究员点启动卡：卡片开出的会话运行 pool.py claim %s --as %s 时转正。%d 天内没人认领，这条预登记作废。" % (
            full, role, role, full, pool.CARD_DAYS))
    else:
        print("已预登记 %s（%s），%s。对方运行 pool.py claim 时转正。" % (full, role, "桌面会话 %s" % desk if desk else "会话 %s" % session))
    return 0


def release_cmd(spec, root, name):
    policy = pool.need_pool(root)
    if not policy:
        return 0
    me, origin = pool.caller(root, policy)
    if origin != "researcher":
        print("出错：只有研究员能释放认领，在研究员自己的会话或终端里运行。")
        return 1
    full = pool.canonical(policy, name) or name
    with pool.locked(root) as pl:
        claim = pl["claims"].pop(full, None)
        if claim:
            pl["tombstones"].append(dict(claim, released_at=pool.stamp(), released_by="researcher"))
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
        return "%s 没有空闲的会话，还没到上限 %d：排队，或运行 pool.py refill %s 记一条补位请求，%s的会话提启动卡，研究员点一下就开。" % (
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
    todos = [] if role else pool.open_todos(pl)
    for t in [t for t in todos if not t.get("kind")]:
        print("- 研究员任务 %d：%s（%s 提，%s%s%s）" % (
            t["n"], t.get("text"), t.get("by"), str(t.get("at"))[:16], "，为 #%s" % t["item"] if t.get("item") else "",
            "；做完算数：%s" % t["done_when"] if t.get("done_when") else ""))
    items = [t for t in todos if t.get("kind")]
    if items:                                        # however many: one thing to tell the researcher
        print("- 研究员任务·飞书项目：%d 条工作项等研究员在网页上处理，合成一件事跟他说，下面每条附给他；"
              "他说处理完了，运行 pool.py show --prune --apply 核对台账、划掉办了的。" % len(items))
        for t in items:
            print("  - #%s %s：%s；原因：%s（%s 提）%s" % (t["item"], pool.ITEM_KINDS.get(t["kind"], t["kind"]), t.get("title") or "名称没读到",
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
    lg = ledger_core.Ledger(root, "preview")
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


def local_waiting(root, pl, me):
    """The tasks without a work item this claim handed out that nobody holds yet (1.1.0: what a +1 hands to a +0),
    from the receipts packages/sent/<ID>-<n>.json and the dispatch log alone: nothing here asks Feishu."""
    held = set(pl.get("owners") or {}) | set(pool.all_open_items(root, pl.get("owners")).get(me["name"], ({}, {}))[0])
    out = []
    for path in pool.files_in(os.path.join(root, "packages", "sent"), "*.json"):
        rec = pool.load_json(path, {}) or {}
        if isinstance(rec, dict) and rec.get("local") and rec.get("status") == "registered" and rec.get("by") == me["name"] \
                and rec.get("state") == ledger_core.LOCAL_OPEN and str(rec.get("work_item_id")) not in held:
            out.append(rec)
    return out


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
    handed = local_waiting(root, pl, me)
    for rec in handed:
        print("- 还没派出去 #%s %s（没建工作项的任务：直接给 +0 发【%s #%s】）" % (
            rec.get("work_item_id"), rec.get("title") or "", (policy.get("msg") or {}).get("assign", "委托"), rec.get("work_item_id")))
    if offline:
        print("（离线，没查台账里待开工的工作项）")
        return 0
    try:
        items = queue_rows(root, policy, me, seq)
    except ledger_core.LedgerError as e:
        print("台账没读到：%s" % str(e)[:120])
        return 1
    for row in items:
        print("- %s #%s %s" % (row.get("work_item_status"), row.get("work_item_id"), row.get("name", "")))
    if not items and not handed:
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
    uploads, sent = [], pool.files_in(os.path.join(root, "packages", "sent"), "*.json")
    known = {(pool.load_json(p, {}) or {}).get("draft") for p in sent if not os.path.basename(p).startswith(("reply-", "upload-", "attach-"))}
    for path in sent:
        rec = pool.load_json(path, {}) or {}
        if isinstance(rec, dict) and os.path.basename(path).startswith("upload-"):     # the author's step of a registration
            if rec.get("status") != "uploaded":
                uploads.append("%s（文档没传完，作者 %s 重跑 docs.py upload <草稿>）" % (rec.get("draft"), rec.get("by") or "?"))
            elif rec.get("draft") not in known:
                uploads.append("%s（文档已上传，还没登记：作者 %s 把登记请求再发一次）" % (rec.get("draft"), rec.get("by") or "?"))
            continue
        if isinstance(rec, dict) and rec.get("status") not in (None, "registered", "replied"):
            if os.path.basename(path).startswith("reply-"):
                if rec.get("status") == "created":           # the document itself is not finished: the author's step
                    uploads.append("%s（交付包没传完，作者 %s 重跑 docs.py upload <草稿>）" % (rec.get("draft"), rec.get("by") or "?"))
                    continue
                replies.append("%s（答复 #%s，%s）" % (rec.get("draft"), rec.get("parent"), rec.get("status")))
            else:
                unfinished.append("%s（#%s，%s）" % (rec.get("draft"), rec.get("work_item_id"), rec.get("status")))
    if uploads:
        print("- 传了文档、还没登记成工作项的草稿：%s" % "；".join(uploads))
    if unfinished:
        print("- 书记员先补完写到一半的登记，逐条运行 workitem.py register <草稿> --resume：%s" % "；".join(unfinished))
    if replies:
        print("- 交付包已上传、“交付包”字段还没填：书记员逐条运行 workitem.py reply <草稿>（承接方把请求再发一次）：%s" % "；".join(replies))
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
    print("- 长期任务（比如 notify.py listen）随应用退出就停了，要研究员在终端里重新启动。")
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
        print("会话池 %s（每 %d 秒刷新，Ctrl-C 退出）" % (pool.stamp(), every))
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


def item_end(lg, item):
    """What became of a work item: (state name, its name) when it is archived or terminated, ("已删除", "") when
    the space no longer has it, None while it is open. A ledger that cannot be read is LedgerError: an item
    that is gone and a ledger that does not answer are told apart by what the space says (WorkItemNotFound)."""
    if ledger_core.is_local(item):                    # a task without a work item: over once it is accepted
        try:
            rec = ledger_core.local_task(lg, item)[1]
        except ledger_core.LedgerError:
            return "已删除", ""
        return (rec.get("state"), rec.get("title") or "") if rec.get("state") == ledger_core.LOCAL_ACCEPTED else None
    try:
        it = lg.item(item)
    except ledger_core.LedgerError as e:
        if "WorkItemNotFound" in str(e):
            return "已删除", ""
        raise
    if it is None:
        raise ledger_core.LedgerError("读不到 #%s" % item)
    return (it.get("state"), it.get("name") or "") if ledger_core.finished(lg, it.get("state"), it.get("type")) else None


def refill_cmd(spec, root, role, count=1):
    policy = pool.need_pool(root)
    if not policy:
        return 0
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    by = pool.sender_label(*pool.caller(root, policy))
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
        pl["refills"].append({"role": role, "count": count, "by": by, "at": pool.stamp()})
    pool.log_event(root, {"event": "refill", "role": role, "count": count, "by": by})
    entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
    pool.append_inbox(root, entry, {"id": uuid.uuid4().hex[:12], "ts": pool.stamp(), "from": by, "read": False,
                               "text": "补位请求：%s 再开 %d 个会话。研究员来谈时跟在决策清单后面列出，补位请求不算决策；%s的会话会提一张启动卡，"
                                       "研究员在那个会话里点一下就开。列的时候照 pool.py show 写明卡片在哪个会话。" % (
                                   role, count, opener(policy, role, "那个序列"))})
    me, origin = pool.caller(root, policy)
    mine, target = (policy["agents"].get(me["role"]) if me else None), policy["agents"][role]
    opens = bool(mine) and mine["sequence"] == target["sequence"] and me["role"] != role and (
        mine["rank"] == target["rank"] + 1 or mine["rank"] == 2 and not (policy.get("pool") or {}).get("p1"))
    if opens:
        how = ("这个角色由你开：本轮接着提一张启动卡，照 `pool.py claim %s --as %s` 输出里“补位用”的那一行"
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
    asked = [t for t in pool.open_todos(pl) if t.get("kind") and t.get("item")]
    gone, struck, waiting = [], [], []
    try:
        lg = ledger_core.Ledger(root, "preview")
        ends = {item: item_end(lg, item) for item in sorted(set(owners) | {t["item"] for t in asked})}
    except (ledger_core.LedgerError, orgspec.OrgSpecError) as e:
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
    policy = pool.need_pool(root)
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pool.py", description="会话池：认领、预登记、释放、找空闲的会话、补位", allow_abbrev=False)
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
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
    p = sub.add_parser("show")
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
    p = sub.add_parser("open")
    p.add_argument("--target", required=True)
    p.add_argument("--manual", action="store_true", help="研究员的会话开不了会话时：打印手工开池的做法")
    p.add_argument("--wait", type=int, metavar="SECONDS", help="等到清单里的会话都认领、书记员的首轮结束，最多等这么多秒")
    p.add_argument("--batch", type=int, metavar="N", help="和 --wait 一起用：只等第 N 批的会话认领、首轮结束")
    args = ap.parse_args(argv)
    if args.cmd == "open":
        # the list of sessions an initialisation opens comes from what setup_core.py knows about the project; it is in
        # pool_open.py, which exists where the plugin is installed and not among a project's copies
        try:
            import pool_open
        except ImportError:
            print("出错：pool.py open 从插件安装目录运行：python3.12 -I <插件目录>/scripts/pool.py open --target <项目目录>")
            return 2
        return pool_open.main(args)
    spec = orgspec.OrgSpec()
    root = ledger_core.project_root()
    if args.cmd == "claim":
        return claim_cmd(spec, root, args.role, args.name, args.desk, args.wait)
    if args.cmd == "expect":
        return expect_cmd(spec, root, args.name, args.role, args.desk, args.session, args.card)
    if args.cmd == "release":
        return release_cmd(spec, root, args.name)
    if args.cmd == "show":
        return pool_cmd(spec, root, args)
    if args.cmd == "refill":
        return refill_cmd(spec, root, args.role, args.count)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
