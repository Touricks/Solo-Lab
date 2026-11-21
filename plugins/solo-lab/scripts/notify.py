#!/usr/bin/env python3
"""Messages between sessions: inboxes, focus sessions and the Feishu DM listener.

    notify.py send <role or session name> --text T [--work-item ID]
    notify.py focus open [<role or session>] --topic T | focus close [<role or session>] | focus list
    notify.py listen                          Feishu DM listener: appends to inbox/feishu.jsonl

Standard library only.
"""
import argparse
import json
import os
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger_core  # noqa: E402
import pool  # noqa: E402

def notify_target(spec, root, policy, role):
    """The inbox a notify goes to: a session's; a role's only when the role can have one session or
    has exactly one claim; a +0 work name goes to the claim that owns the work item."""
    if role == "feishu":
        raise SystemExit("inbox/feishu.jsonl 只由飞书私聊监听（notify.py listen）写，notify 不写它")
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
    policy = pool.load_policy(root) or {}
    target = notify_target(spec, root, policy, role)
    me, origin = pool.caller(root, policy)               # the sender is whoever runs this, not --from
    sender = pool.sender_label(me, origin)
    msg = {"id": uuid.uuid4().hex[:12], "ts": pool.stamp(), "from": sender, "text": text, "read": False}
    if work_item:
        msg["work_item"] = str(work_item).lstrip("#")
    pool.append_inbox(root, target, msg)
    print("已留言给 %s：%s" % (target, text[:60]))


def agent_roles(spec):
    out = []
    for s, _, _ in spec.sequences():
        for r in (2, 1, 0):
            out.append("pmo-router" if (s, r) == ("pmo", 1) else "pmo-scribe" if (s, r) == ("pmo", 0) else "%s-p%d" % (s, r))
    return out


def focus(spec, root, action, role=None, topic=None, offline=False):
    return pool_focus(spec, root, pool.load_policy(root) or {}, action, role, topic)


def pool_focus(spec, root, policy, action, target, topic):
    """Mark a session as the researcher's for now; pool.py show will not suggest it."""
    if action == "list":
        pl, _ = pool.load(root)
        rows = [r for r in pool.session_view(root, policy, pl) if r["focus"]]
        for r in rows:
            left = "，空闲 %d 分钟后失效" % r["focus_left_min"] if r["focus_left_min"] is not None else ""
            print("%s：研究员在用（%s，%s 开始%s）" % (r["name"], r["focus"].get("topic"), r["focus"].get("opened_at"), left))
        if not rows:
            print("没有研究员在用的会话。")
        return
    me = pool.caller(root, policy)[0]
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
            pl["focus"][name] = {"topic": topic or "", "opened_at": pool.stamp()}
        sid = pl["claims"][name].get("session_id") or ""
    if action != "close":                               # an old lapse note no longer applies
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    pool.log_event(root, {"event": "focus_" + action, "name": name, "topic": topic})
    if action == "close":
        print("已收尾：%s。谈定的事要写成决策包交给入口登记，没写进台账的不算数。" % name)
    else:
        print("已标记：研究员在用 %s（%s）。会话空闲满 30 分钟自动失效；这期间 pool.py show 不推荐它。去侧栏找 %s。" % (
            name, topic or "未写", name))


# --------------------------------------------------------------- listen
def listen(root, max_events=None):
    """Append the researcher's Feishu DMs to inbox/feishu.jsonl. Run by the researcher.

    `event consume im.message.receive_v1` prints one flat JSON object per message:
    type, sender_id (open_id), sender_type, chat_type, content (already readable
    text) and message_id, which is the key to deduplicate on.
    """
    ids = pool.load_json(os.path.join(root, "feishu", "ids.json"), {})
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
        pool.append_inbox(root, "feishu", {"id": key, "ts": pool.stamp(), "from": "研究员（飞书私聊）", "text": text, "read": False})
        print("收到：%s" % text[:60])
    return proc.wait()


def main(argv=None):
    ap = argparse.ArgumentParser(description="给会话的收件箱留言、聚焦会话、监听机器人私聊", allow_abbrev=False)
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("send")
    p.add_argument("role", help="角色（alg-p2）或会话池里的会话名（Alg-p2-01）")
    p.add_argument("--text", required=True)
    p.add_argument("--work-item")
    p.add_argument("--from", dest="sender")
    p = sub.add_parser("focus")
    p.add_argument("action", choices=["open", "close", "list"])
    p.add_argument("role", nargs="?")
    p.add_argument("--topic")
    p = sub.add_parser("listen")
    p.add_argument("--max-events", type=int,
                   help="lark-cli 收到这么多条事件就退出；别人发给机器人的私聊、群消息也算数，不只算收进收件箱的")
    args = ap.parse_args(argv)
    spec = orgspec.OrgSpec()
    root = ledger_core.project_root()
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        print("Codex 的会话操作使用 codex_runtime.py；本入口只复用 classify 的业务路由")
        return 2
    if args.cmd == "send":
        notify(spec, root, args.role, args.text, args.work_item, args.sender)
        return 0
    if args.cmd == "focus":
        focus(spec, root, args.action, args.role, args.topic, args.offline)
        return 0
    if args.cmd == "listen":
        return listen(root, args.max_events)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
