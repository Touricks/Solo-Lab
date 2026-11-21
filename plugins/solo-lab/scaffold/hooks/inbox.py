#!/usr/bin/env python3
"""UserPromptSubmit hook: tell a pool session who started its turn, and show it the
messages waiting in its inbox.

Messages live in inbox/<role>.jsonl, one JSON object per line, written with
`notify.py send`. The entry session also sees inbox/feishu.jsonl, where the
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
  hand, a revised one with its version, those past their expiry that keep waiting
  first, and the refill requests after them (they are no decisions). A register
  receipt for a decision that was revised (its receipt has revisions) is no
  registration: a revision is never mentioned to the researcher;
- reads the local decision state .claude/state/gates.json (ledger.py writes it,
  this hook only reads it): every pool session gets one line a turn with what the
  decisions still waiting freeze (1.0.4). No hook stops those actions (1.0.6): the
  line and the rules are what keeps them, and the line says so. The entry, in every
  turn, gets a line for each waiting decision past its expiry time, which is its
  registration plus the expiry time in org/decisions.md, worked out here as ledger.py
  works it out (pool.expiry_hours, pool.expires_at): a reversible one that may take
  its default (pool.takes_default) is to be confirmed by the scribe with `workitem.py
  confirm <ID> --by 到期默认`, who then tells the decision's 执行方 to act on the
  default; any other one, also one that was decided once and rolled back to 待决定,
  keeps waiting, comes first when the researcher next talks, and is not pushed to
  the researcher. At most MAX_DUE such lines a turn; in the turn that opens a
  session they count against the role text's budget;
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
QUEUED = "本轮登记了待定决策 #%s：在这一行状态里提一次，写事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）和主文档链接，不超过 3 行。"
URGENT = "它是插队的：本轮可以在收尾里问研究员一次。"
URGENT_AMONG = "其中 #%s 是插队的：本轮可以在收尾里问研究员一次。"
# The app reports a session this one opened from the wrap-up as the model wrote it, before guard.py
# took the question out (seen 2026-11-15).
SIDE_REPORT = "回报里说它在等研究员、带着提问的，不转告研究员：被其他 agent 会话叫醒的轮次，它收尾里的提问已被 hook 去掉。"
FIRST_TALK = "研究员来谈了。"           # no earlier message of theirs on record, in a new session or one from before 1.0.3
BACK_AFTER = "研究员隔了 %d 分钟回来。"
LIST_DUE = ("先回答研究员，再跑 python3.12 -I .claude/solo-lab/bin/ledger.py query queue，"
            "把待决定的决策和等研究员执行的决策附在末尾，每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接，"
            "改过版的在事项后面写版本号；pool.py show 里的研究员任务清单和补位请求一起列，它们不算决策；清单里飞书项目的工作项合成一件事说，补位请求写明启动卡在哪个会话里。都没有就不附。")
SOURCE_SHOWN = 80                     # characters of the sender's title shown
# The local decision state ledger.py keeps (1.0.4): the decisions still waiting, with what each of them freezes.
GATES_FILE = "gates.json"
FROZEN = "待定决策冻结的动作，决策定下以前谁都不做（hook 不拦，要自己守住），选项中立的准备照常做：%s。"
FROZEN_SHOWN = 700                    # characters of the frozen line at most
MORE_FROZEN = "……（全文看 python3.12 -I .claude/solo-lab/bin/ledger.py query queue 列的决策包）"
DUE_DEFAULT = "#%s 已到期：请书记员按到期默认确认（workitem.py confirm %s --by 到期默认），书记员确认后通知执行方 %s 照到期默认动作办（%s）；已经请过的不再请。"
MAX_DUE = 5                           # lines about decisions past their expiry, at most, a turn
MORE_DUE = "另有 %d 条已到期的待定决策，见 python3.12 -I .claude/solo-lab/bin/ledger.py query queue。"
DUE_WAIT = "#%s 已到期，继续等，研究员来谈时排在最前；不推给研究员，不提醒截止。"
WAITING_FIRST = "已经到期、继续等研究员的决策（%s）排在清单最前，不提截止。"


def queued_decision(root, item_id):
    """The receipt of work item item_id when it is a decision that went into the decision queue:
    workitem.py register writes type, queue and urgent into packages/sent/<ID>.json (or
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


def woken_text(source, entry, fresh=()):
    """A turn another agent session started: nobody to ask. The entry also mentions, this once, the
    decisions the turn's register receipt put into the queue (fresh: (ID, receipt) each), and may ask about
    one that jumps the queue. The report of a session this one opened may still say it waits on the researcher."""
    sender = "（来自 %s）" % source[:SOURCE_SHOWN] if source else ""
    side = SIDE_REPORT if source == pool.FROM_MY_SESSIONS else ""
    if not entry:
        return POOL_WOKEN % sender + side
    text = ENTRY_WOKEN % sender + side
    if fresh:
        text += QUEUED % "、#".join(str(item) for item, _ in fresh)
        urgent = [str(item) for item, rec in fresh if rec.get("urgent") is True]
        if urgent:
            text += URGENT if len(fresh) == 1 else URGENT_AMONG % "、#".join(urgent)
    return text


def back_text(prev, at, waiting=()):
    """The entry, in a turn the researcher started: their first message on record in this session, or
    the first after pool.FOCUS_IDLE or more, asks for what waits on them, the decisions past their expiry
    that keep waiting (waiting: IDs) first. None for any other message.
    A session from before 1.0.3 has no researcher_at either, so the text does not say it is the first."""
    first = WAITING_FIRST % "、".join("#%s" % i for i in waiting) if waiting else ""
    last = pool.parse_time(prev.get("researcher_at"))
    if last is None:
        return FIRST_TALK + LIST_DUE + first
    if last.tzinfo is None:
        last = last.astimezone()
    gap = at - last
    if gap < pool.FOCUS_IDLE:
        return None
    return BACK_AFTER % int(gap.total_seconds() // 60) + LIST_DUE + first


def load_pending(root):
    """The decisions still waiting in the local decision state (.claude/state/gates.json, which only
    ledger.py writes), by ID, in the order they were registered; {} when there is none or it cannot be read."""
    try:
        with open(os.path.join(pool.state_dir(root), GATES_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    pending = data.get("pending") if isinstance(data, dict) else None
    if not isinstance(pending, dict):
        return {}
    rows = [(str(k), v) for k, v in pending.items() if isinstance(v, dict)]
    return dict(sorted(rows, key=lambda kv: (str(kv[1].get("registered_at") or ""), kv[0])))


def frozen_text(pending):
    """One line: what each decision still waiting freezes, each item as its package wrote it (a category of
    the list of irreversible actions or a sentence; a command prefix in a state written before 1.0.6). None
    when nothing is frozen."""
    rows = []
    for item_id, entry in pending.items():
        texts = []
        for f in entry.get("frozen") if isinstance(entry.get("frozen"), list) else []:
            text = str(f.get("text") or f.get("prefix") or "").strip() if isinstance(f, dict) else ""
            if text and text not in texts:
                texts.append(text)
        if texts:
            rows.append("#%s%s：%s" % (item_id, "（%s）" % entry["title"] if entry.get("title") else "",
                                       "".join("“%s”" % t for t in texts)))
    if not rows:
        return None
    text = FROZEN % "；".join(rows)
    if len(text) > FROZEN_SHOWN:
        text = text[:FROZEN_SHOWN - len(MORE_FROZEN)] + MORE_FROZEN
    return text


def expired(pending, at, hours):
    """The decisions still waiting whose expiry time has come: their registration plus hours, the expiry
    time org/decisions.md states now (pool.expiry_hours). ledger.py works it out the same way, so what the
    entry is told and what `confirm --by 到期默认` accepts agree, also right after the researcher changed
    the time."""
    out = []
    for item_id, entry in pending.items():
        due = pool.expires_at(entry, hours)
        if due is not None and at >= due:
            out.append((item_id, entry))
    return out


def due_text(item_id, entry):
    """What the entry does about a decision past its expiry time: a reversible one that may take its
    default is confirmed by the scribe with it; any other one, also one decided once and rolled back to
    待决定, keeps waiting for the researcher."""
    if pool.takes_default(entry):
        return DUE_DEFAULT % (item_id, item_id, entry.get("executor") or "（包头写的执行方）",
                              entry.get("default_option") or entry.get("default") or "包头写的选项")
    return DUE_WAIT % item_id


def due_lines(due):
    """The lines about the decisions past their expiry, MAX_DUE at most, then how many more there are."""
    lines = [due_text(i, e) for i, e in due[:MAX_DUE]]
    if len(due) > MAX_DUE:
        lines.append(MORE_DUE % (len(due) - MAX_DUE))
    return "\n".join(lines)


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
    opened, opening = False, None
    if not prev.get("prompts") and origin == "researcher":  # only the researcher's own words open a session
        claim, note = pool.opening_permit(root, policy, sid, desk, body)
        if claim:
            pl, _ = pool.load(root)
            opening = (claim, pl.get("owners"))
            parts.append(None)                       # the role text: filled in last, with what the rest leaves of the budget
            opened = True
        if note:
            parts.append("认领：" + note)

    def finish(out):
        """The parts with the role text of a session this turn opened, cut to what the other parts leave of
        pool.INJECT_LIMIT (they are joined with blank lines)."""
        if not opening:
            return out
        others = [x for x in out if x is not None]
        room = pool.INJECT_LIMIT - sum(len(x) + 2 for x in others)
        text = pool.injection(root, policy, opening[0], "已按开局句认领。", limit=max(room, 2000), owners=opening[1])
        return [text if x is None else x for x in out]
    who = pool.identify(data, policy, root)
    msg = pool.parse_message(policy, body) if who.origin == "claim" else None
    entry = who.origin == "claim" and who.role == policy.get("entry_role")
    pl, ok = pool.load(root)
    fresh = []                                          # (ID, receipt) of the decisions this receipt is the first word of
    if entry and ok and origin == "agent" and msg and msg["kind"] == "register_receipt" \
            and pool.addressed_to(policy, pl, msg, who.claim):
        # every item the header names: the scribe answers several requests with one 【登记回执 #A / #B】, and until
        # 1.0.9 only the first counted, so a decision named second was announced by a later receipt about it
        for item in msg.get("ids") or [msg.get("id")]:
            rec = queued_decision(root, item)
            # a later receipt for it, or a revision: nothing new for the researcher
            if rec and not announced(root, item) and not rec.get("revisions") and item not in [i for i, _ in fresh]:
                fresh.append((item, rec))
    ask_ok = next((item for item, rec in fresh if rec.get("urgent") is True), None)
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
        return finish(parts + ["本会话有 %s 的认领许可，还没转正：照开局的说明运行 pool.py claim。" % who.role]), {}
    if who.origin != "claim":
        return finish(parts), {}
    me = who.claim
    pending = load_pending(root)
    due = expired(pending, at, pool.expiry_hours(root)[0]) if entry and pending else []
    if origin == "agent":
        parts.append(woken_text(source, entry, fresh))
        for item, rec in fresh:
            pool.log_event(root, {"event": "decision_announced", "id": item, "urgent": rec.get("urgent") is True,
                                  "seen_by": me["name"], "session_id": sid})
    elif origin == "researcher" and entry and not opened:
        text = back_text(prev, at, [i for i, e in due if not pool.takes_default(e)])
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
        parts.append("“研究员在用”的标记已因空闲 30 分钟失效。研究员接着和你谈的话，先重新运行 notify.py focus open --topic '…'。")
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    if msg and ok:
        msg["from"] = pool.resolve(policy, pl, msg["from"]) if msg.get("from") else None
        mine = pool.addressed_to(policy, pl, msg, me)
        hits = pool.receive_checks(policy, root, pl, msg, me)
        sender = pl["claims"].get(msg["from"]) if msg["from"] else None
        pool.log_event(root, {"event": "message" if mine else "message_misaddressed", "kind": msg["kind"],
                              "id": msg["id"], "from": msg["from"], "to": me["name"] if mine else msg.get("to"),
                              "header_to": msg.get("to"), "seen_by": me["name"], "session_id": sid,
                              "line": msg["line"], "wrapped": wrapper is not None, "soft_hits": hits,
                              # does the dispatch graph let the sender send this to me (pool.all_open_items)
                              "in_graph": bool(sender) and pool.kind_allowed(policy, msg["kind"], sender["role"], me["role"])})
        if msg["kind"] == "assign" and msg.get("id") and mine:
            with pool.locked(root) as locked_pool:
                locked_pool["owners"][msg["id"]] = me["name"]
        if hits:
            parts.append("这条消息有几处和规范对不上（只提醒，不拦）：\n" + "\n".join("- " + h for h in hits))
    if due:                                         # the entry, every turn: decisions past their expiry time
        parts.append(due_lines(due))
    frozen = frozen_text(pending)
    if frozen:                                      # every pool session, every turn
        parts.append(frozen)
    inbox = os.path.join(root, "inbox")
    sources = [(os.path.join(inbox, me["name"] + ".jsonl"), "收件箱里", INBOX)]
    if pool.role_limit(policy, me["role"]) == 1:
        sources.append((os.path.join(inbox, me["role"] + ".jsonl"), "按角色的收件箱里", INBOX))
    if me["role"] == policy.get("entry_role"):
        sources.append((os.path.join(inbox, "feishu.jsonl"), "研究员的飞书私聊里", FEISHU))
    reminder = pool.reminder_line(policy, me)
    used = sum(len(p) for p in parts if p) + len(reminder)
    more, marks = inbox_text(sources, used, counts_only=opened)
    return finish(parts + more + [reminder]), marks


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
