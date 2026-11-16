#!/usr/bin/env python3
"""UserPromptSubmit hook: tell a pool session who started its turn, and show it the
messages waiting in its inbox.

Messages live in inbox/<role>.jsonl, one JSON object per line, written with
`route.py notify`. The entry session also sees inbox/feishu.jsonl, where the
Feishu DM listener appends the researcher's messages. Each message is shown once
and marked read only after the text is built; what does not fit in the context
budget stays unread for the next prompt.

The hook also:
- tells who started the turn (pool.turn_origin): another agent session (its
  message, the report of a session this one opened, the first turn the session
  that opened this one wrote), the researcher (also with the app's
  <system-reminder> blocks in front), or the app (<task-notification> ..., or
  nothing but reminders). It writes turn_origin into the session's status file,
  researcher_at for a turn the researcher started, and ask_ok, which it clears
  every turn; guard.py reads them;
- in a turn another agent session started, tells a pool session not to ask the
  researcher anything (guard.py takes the question out of the wrap-up and denies
  AskUserQuestion), and in the report of a session it opened, not to pass on that
  the session waits on the researcher. The entry writes one line of status at
  most; when the turn is the first 【登记回执】 addressed to it for a decision that
  went into the decision queue (its receipt in packages/sent/ says type decision
  and queue true), it mentions that decision this once, and may ask about it in
  the wrap-up when it jumps the queue (urgent: ask_ok is set to its ID). The
  mention is logged as decision_announced in .claude/logs/dispatch.jsonl, so the
  receipts that come later for the same decision (the confirmation of 决定, an
  archive, a re-send) mention nothing;
- when the researcher writes to the entry with no earlier message of theirs on
  record in this session, or after 30 minutes or more (pool.FOCUS_IDLE), asks it
  to list after its answer the decisions waiting on the researcher (`ledger.py
  query queue`: to be decided, or decided and carried out by the researcher, not
  archived yet), each with who carries it out and what the researcher must do by
  hand, and the refill requests after them (they are no decisions);
- turns the researcher's opening line in a new top-level session
  ("认领 <+2 角色>（会话名 …）", also after <system-reminder> blocks) into a claim
  and injects the role text;
- marks the session busy and records its permission mode;
- recognises a message header at the start of one of the first lines of the
  message (inside the app's wrapper, 【委托 #ID】A → B …), records it in
  .claude/logs/dispatch.jsonl (its time is the receipt; wrapped says whether it came
  inside the app's wrapper, which route.py audit counts), makes the receiver the
  work item's owner on 【委托】 addressed to it, and adds reminders where the
  message breaks the dispatch norms; a +0 work name (Alg-p0-2101) stands for the
  claim that owns that work item;
- reads inbox/<session name>.jsonl (and inbox/<role>.jsonl for one-session roles)
  and adds one line saying who this session is.
What a turn adds comes before the inbox and counts against the same context
budget. The researcher's own session reads no inbox. This hook never blocks:
problems go to stderr and the prompt goes through.
Python 3.9 standard library only.
"""
import fcntl
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

MAX_SHOWN = 20


def key_of(msg):
    return msg.get("id") or "%s|%s" % (msg.get("ts"), msg.get("text"))


def peek_unread(path):
    """Unread messages in path, oldest first; lines that are not JSON objects are skipped."""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and not msg.get("read"):
                out.append(msg)
    return out


def mark_read(path, keys):
    """Mark the messages with these keys read, under a lock; other lines are kept as they are."""
    if not keys or not os.path.isfile(path):
        return
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            out = []
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if isinstance(msg, dict) and not msg.get("read") and key_of(msg) in keys:
                    msg["read"] = True
                    line = json.dumps(msg, ensure_ascii=False)
                out.append(line)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def take_unread(path):
    """Return unread messages in path and mark them read (kept for callers that show them all)."""
    msgs = peek_unread(path)[:MAX_SHOWN]
    mark_read(path, {key_of(m) for m in msgs})
    return msgs


def describe(msg):
    head = str(msg.get("ts", ""))[:16]
    if msg.get("from"):
        head += " 来自 %s" % msg["from"]
    if msg.get("work_item"):
        head += "，工作项 #%s" % msg["work_item"]
    return "- %s：%s" % (head, str(msg.get("text") or "").strip())


def inbox_text(sources, used, budget=pool.CONTEXT_LIMIT, counts_only=False):
    """(text parts, {path: keys to mark read}) for the inboxes in sources [(path, title, action)],
    fitting what is left of the budget; with counts_only, just say how many wait and leave them unread."""
    parts, marks = [], {}
    for path, title, action in sources:
        msgs = peek_unread(path)
        if not msgs:
            continue
        if counts_only:
            parts.append("%s还有 %d 条消息没看，下一轮对话时显示。" % (title, len(msgs)))
            continue
        lines, keys = [], set()
        for m in msgs[:MAX_SHOWN]:
            line = describe(m)
            room = budget - used - 160
            if len(line) > room:
                if lines or room < 400:
                    break
                line = line[:room - 60] + "……（全文在 inbox/%s，id %s）" % (os.path.basename(path), m.get("id", "?"))
            lines.append(line)
            keys.add(key_of(m))
            used += len(line) + 1
        if lines:
            more = len(msgs) - len(lines)
            parts.append("%s有 %d 条消息，%s：\n%s%s" % (
                title, len(lines), action, "\n".join(lines), "\n（另有 %d 条，下一轮显示）" % more if more else ""))
            marks[path] = keys
        else:
            parts.append("%s还有 %d 条消息没看，下一轮对话时显示。" % (title, len(msgs)))
    return parts, marks


INBOX = "先处理再回答研究员"
FEISHU = "照路由决策表处理"
# What a turn adds before the inbox; see pool.turn_origin.
ENTRY_WOKEN = ("本轮由其他 agent 会话叫醒%s，研究员多半不在。面向研究员最多写一行状态：不提问，不标“等研究员回答”，"
               "不复述已经登记过的决策；收尾里的提问会被 hook 去掉。")
POOL_WOKEN = "本轮由其他 agent 会话叫醒%s：不向研究员提问，要拍板的事照规则写进包里；收尾里的提问会被 hook 去掉。"
QUEUED = "本轮登记了待定决策 #%s：在这一行状态里提一次，写事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）和工作项链接，不超过 3 行。"
URGENT = "它是插队的：本轮可以在收尾里问研究员一次。"
# The app reports a session this one opened from the wrap-up as the model wrote it, before guard.py
# took the question out (seen 2026-11-15).
SIDE_REPORT = "回报里说它在等研究员、带着提问的，不转告研究员：被其他 agent 会话叫醒的轮次，它收尾里的提问已被 hook 去掉。"
FIRST_TALK = "研究员来谈了。"           # no earlier message of theirs on record, in a new session or one from before 1.0.3
BACK_AFTER = "研究员隔了 %d 分钟回来。"
LIST_DUE = ("先回答研究员，再跑 python3 -I .claude/solo-lab/bin/ledger.py query queue，"
            "把待决定的决策和等研究员执行的决策附在末尾，每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接；"
            "route.py pool 里还没处理的补位请求一起列，补位请求不算决策。都没有就不附。")
SOURCE_SHOWN = 80                     # characters of the sender's title shown


def queued_decision(root, item_id):
    """The receipt of work item item_id when it is a decision that went into the decision queue:
    ledger.py register writes type, queue and urgent into packages/sent/<ID>.json (or
    <ID>-<draft file>.json) from 1.0.3 on. None for anything else, an older receipt included."""
    if not item_id:
        return None
    sent, key = os.path.join(root, "packages", "sent"), glob.escape(str(item_id))
    for path in pool.files_in(sent, key + ".json") + pool.files_in(sent, key + "-*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("type") == "decision" and rec.get("queue") is True:
            return rec
    return None


def announced(root, item_id):
    """Was the entry told about this queued decision already? Only its first register receipt counts:
    the scribe answers every later request about it (confirm 决定, archive, a re-send) with the
    same 【登记回执 #ID】."""
    return any(r.get("event") == "decision_announced" and str(r.get("id")) == str(item_id) for r in pool.read_events(root))


def woken_text(source, entry, item=None, receipt=None):
    """A turn another agent session started: nobody to ask. The entry also mentions, this once, the
    decision the turn's register receipt put into the queue (receipt), and may ask about one that
    jumps the queue. The report of a session this one opened may still say it waits on the researcher."""
    sender = "（来自 %s）" % source[:SOURCE_SHOWN] if source else ""
    side = SIDE_REPORT if source == pool.FROM_MY_SESSIONS else ""
    if not entry:
        return POOL_WOKEN % sender + side
    text = ENTRY_WOKEN % sender + side
    if receipt:
        text += QUEUED % item
        if receipt.get("urgent") is True:
            text += URGENT
    return text


def back_text(prev, at):
    """The entry, in a turn the researcher started: their first message on record in this session, or
    the first after pool.FOCUS_IDLE or more, asks for what waits on them. None for any other message.
    A session from before 1.0.3 has no researcher_at either, so the text does not say it is the first."""
    last = pool.parse_time(prev.get("researcher_at"))
    if last is None:
        return FIRST_TALK + LIST_DUE
    if last.tzinfo is None:
        last = last.astimezone()
    gap = at - last
    if gap < pool.FOCUS_IDLE:
        return None
    return BACK_AFTER % int(gap.total_seconds() // 60) + LIST_DUE


def pool_prompt(data, policy, root):
    sid = data.get("session_id") or ""
    if not sid:
        return [], {}
    desk = os.environ.get(pool.DESK_ENV) or ""
    prompt = data.get("prompt") or ""
    origin, source = pool.turn_origin(policy, prompt)
    body, wrapper = pool.unwrap(pool.strip_reminders(prompt))     # without the app's reminders and wrapper
    parts = []
    prev = pool.load_session(root, sid)
    opened = False
    if not prev.get("prompts") and origin == "researcher":  # only the researcher's own words open a session
        claim, note = pool.opening_permit(root, policy, sid, desk, body)
        if claim:
            pl, _ = pool.load(root)
            parts.append(pool.injection(root, policy, claim, "已按开局句认领。", owners=pl.get("owners")))
            opened = True
        if note:
            parts.append("认领：" + note)
    who = pool.identify(data, policy, root)
    msg = pool.parse_message(policy, body) if who.origin == "claim" else None
    entry = who.origin == "claim" and who.role == policy.get("entry_role")
    pl, ok = pool.load(root)
    receipt = None
    if entry and ok and origin == "agent" and msg and msg["kind"] == "register_receipt" \
            and pool.addressed_to(policy, pl, msg, who.claim):
        receipt = queued_decision(root, msg.get("id"))
        if receipt and announced(root, msg["id"]):     # a later receipt for it: nothing new for the researcher
            receipt = None
    ask_ok = msg["id"] if receipt and receipt.get("urgent") is True else None
    at = pool.now()
    stamp = pool.stamp(at)

    def change(st):
        st.update({"status": "busy", "status_at": stamp, "prompts": st.get("prompts", 0) + 1, "turn_origin": origin})
        for k in ("quota", "quota_at", "prev_status", "prev_status_at", "ask_ok") + pool.WAIT_KEYS:
            st.pop(k, None)
        if origin == "researcher" and not opened:   # the opening line starts the session, it is no talk yet
            st["researcher_at"] = stamp
        if ask_ok:
            st["ask_ok"] = ask_ok
        if data.get("permission_mode"):
            st["permission_mode"] = data["permission_mode"]
        if who.name and who.origin == "claim":
            st["name"] = who.name
        if msg and msg.get("id") and msg["kind"] in ("assign", "resume"):
            st["work_item"] = msg["id"]
    pool.update_session(root, sid, change)
    if who.origin in ("permit", "pending"):
        return parts + ["本会话有 %s 的认领许可，还没转正：照开局的说明运行 route.py claim。" % who.role], {}
    if who.origin != "claim":
        return parts, {}
    me = who.claim
    if origin == "agent":
        parts.append(woken_text(source, entry, msg and msg.get("id"), receipt))
        if receipt:
            pool.log_event(root, {"event": "decision_announced", "id": msg["id"], "urgent": receipt.get("urgent") is True,
                                  "seen_by": me["name"], "session_id": sid})
    elif origin == "researcher" and entry and not opened:
        text = back_text(prev, at)
        if text:
            parts.append(text)
    back = origin == "researcher"                   # the researcher typed this prompt
    if ok and me["name"] in (pl.get("focus") or {}) and pool.focus_expired(prev):
        with pool.locked(root) as locked_pool:
            if back:                                # the researcher came back to this session
                locked_pool["focus"][me["name"]]["opened_at"] = pool.stamp()
            else:
                locked_pool["focus"].pop(me["name"], None)
        pool.log_event(root, {"event": "focus_renewed" if back else "focus_expired", "name": me["name"]})
        if not back:                                # say so when the researcher comes back
            pool.update_session(root, sid, lambda st: st.update({"focus_lapsed": True}))
    elif prev.get("focus_lapsed") and back and me["name"] not in (pl.get("focus") or {}):
        parts.append("“研究员在用”的标记已因空闲 30 分钟失效。研究员接着和你谈的话，先重新运行 route.py focus open --topic '…'。")
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    if msg and ok:
        msg["from"] = pool.resolve(policy, pl, msg["from"]) if msg.get("from") else None
        mine = pool.addressed_to(policy, pl, msg, me)
        hits = pool.receive_checks(policy, root, pl, msg, me)
        pool.log_event(root, {"event": "message" if mine else "message_misaddressed", "kind": msg["kind"],
                              "id": msg["id"], "from": msg["from"], "to": me["name"] if mine else msg.get("to"),
                              "header_to": msg.get("to"), "seen_by": me["name"], "session_id": sid,
                              "line": msg["line"], "wrapped": wrapper is not None, "soft_hits": hits})
        if msg["kind"] == "assign" and msg.get("id") and mine:
            with pool.locked(root) as locked_pool:
                locked_pool["owners"][msg["id"]] = me["name"]
        if hits:
            parts.append("这条消息有几处和规范对不上（只提醒，不拦）：\n" + "\n".join("- " + h for h in hits))
    inbox = os.path.join(root, "inbox")
    sources = [(os.path.join(inbox, me["name"] + ".jsonl"), "收件箱里", INBOX)]
    if pool.role_limit(policy, me["role"]) == 1:
        sources.append((os.path.join(inbox, me["role"] + ".jsonl"), "按角色的收件箱里", INBOX))
    if me["role"] == policy.get("entry_role"):
        sources.append((os.path.join(inbox, "feishu.jsonl"), "研究员的飞书私聊里", FEISHU))
    reminder = pool.reminder_line(policy, me)
    used = sum(len(p) for p in parts) + len(reminder)
    more, marks = inbox_text(sources, used, counts_only=opened)
    return parts + more + [reminder], marks


def main():
    try:
        data = json.load(sys.stdin)
        with open(os.path.join(HERE, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        if data.get("agent_id"):
            return 0
        root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        parts, marks = pool_prompt(data, policy, root)
        if parts:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))
            sys.stdout.flush()
        for path, keys in marks.items():          # only what was shown is marked read
            mark_read(path, keys)
    except Exception as e:  # never block the prompt
        sys.stderr.write("[inbox] 读取收件箱出错：%s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
