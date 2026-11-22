#!/usr/bin/env python3
"""Session pool state.

Every org role runs as a desktop session the researcher can see and talk
to. A session becomes a role by claiming it, and a claim needs a permit:
- a pre-registration (pool.py expect) by the session that opened it, keyed by the new
  session's desktop ID (or, in a terminal, its session ID); or
- the opening line the researcher pastes into a new top-level session,
  "认领 <角色>（会话名 <名字>）…", for +2 roles only.
Hooks turn a permit into a claim; the commands (pool_cmd.py) only write permits and pending entries.

A claim belongs to one session ID. Its desktop ID moves it to a new session ID only after
/clear: the old session's SessionEnd(clear) and the new session's SessionStart(clear) with
the same desktop ID (or, without it, a pairing by time). A resume keeps the session ID, so
it needs no move. Any other process that shows the same desktop ID, such as a claude
started (or resumed) from that session's shell, is a desk conflict: it holds no role and
no write right.

Every prompt starts a turn, and turn_origin says who started it: another agent session
(agent: its message, the report of a session this one opened, or the first turn the session
that opened this one wrote), the researcher (researcher: what they typed, also when the app
puts <system-reminder> blocks in front of it), or the app itself (system: a background
task's report, a prompt that is nothing but the app's reminders, and the like). The inbox
hook writes it into the session's status file with researcher_at (when the researcher last
started a turn) and ask_ok (the decision the entry may ask the researcher about in this
turn); the guard hook reads them.

setup.py copies this file to .claude/hooks/ (for the hooks) and .claude/solo-lab/bin/
(for the scripts). Being the one module both sides load, it also holds what
ledger.py and the inbox hook must agree on about decisions that wait for the researcher:
the expiry time read from org/decisions.md, and which decisions 到期默认 may take.

    .claude/state/pool.json            claims, permits, pending, tombstones, work item
                                       owners, refill requests, focus flags, re-attach notes,
                                       the researcher's to-dos (todos: n, text, item, done_when, by, at)
    .claude/state/sessions/<id>.json   one session's status, written by its own hooks
    .claude/logs/dispatch.jsonl        messages seen, claims, releases, refills, queued decisions
                                       the entry was told about

Writers lock .claude/state/pool.lock and replace pool.json atomically; readers do not
lock. None of this is a security boundary: a shell can still write the files. The guard
hook denies edits to .claude/state/ and checks the claims again before each ledger write.
Python 3.9 standard library only.
"""
import contextlib
import datetime
import fcntl
import glob
import html
import json
import os
import re

OPENING_RE = re.compile(r"^\s*认领\s*([a-z]+-p2)\s*[（(]\s*会话名\s*[：:]?\s*([A-Za-z]+-p2-\d+)\s*[)）]")
NAME_RE = re.compile(r"^([A-Za-z]+)-p([0-2])-(\d+(?:-\d{1,2})?)$")     # a +0's work name may end in a task number: Alg-p0-2101-3
NAME_IN_TEXT = r"[A-Za-z]+-[pP][0-2]-\d+(?:-\d{1,2}(?!\d))?"
# A work item ID, or the number of a task without a work item (1.1.0: what a +1 hands to a +0), <ID>-<n>.
ITEM_IN_TEXT = r"\d{3,}(?:-\d{1,2}(?![\d-]))?"
HEADER_LINES = 5                      # a message header starts one of the first non-empty lines
REATTACH_WINDOW = datetime.timedelta(seconds=60)
STALE_BUSY = datetime.timedelta(minutes=15)
FOCUS_IDLE = datetime.timedelta(minutes=30)
OFFLINE_LONG = datetime.timedelta(hours=24)
INJECT_LIMIT = 9500
CONTEXT_LIMIT = 9500
ITEMS_SHOWN = 20
BYPASS_MODES = ("bypassPermissions", "dontAsk")
DESK_ENV = "CLAUDE_CODE_HOST_SESSION_ID"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
LIST_KEYS = ("permits", "pending", "tombstones", "refills", "reattach", "todos")
DICT_KEYS = ("owners", "focus")


class PoolError(Exception):
    pass


# ----------------------------------------------------------------- time
def now():
    return datetime.datetime.now().astimezone()


def stamp(at=None):
    return (at or now()).isoformat(timespec="seconds")


def parse_time(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- files
def state_dir(root):
    return os.path.join(root, ".claude", "state")


def pool_path(root):
    return os.path.join(state_dir(root), "pool.json")


def empty():
    return {"version": 1, "claims": {}, "permits": [], "pending": [], "tombstones": [], "owners": {},
            "refills": [], "focus": {}, "reattach": [], "todos": []}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


ENTRY_FIELDS = ("name", "role", "session_id", "desk")


def _entry_ok(x):
    return isinstance(x, dict) and all(isinstance(x.get(k), (str, type(None))) for k in ENTRY_FIELDS)


def well_formed(data):
    """pool.json has the shape this module writes: anything else counts as unreadable."""
    if not isinstance(data, dict):
        return False
    claims = data.get("claims", {})
    if not isinstance(claims, dict) or not all(_entry_ok(c) for c in claims.values()):
        return False
    for key in LIST_KEYS:
        v = data.get(key, [])
        if not isinstance(v, list) or not all(_entry_ok(x) for x in v):
            return False
    if not all(isinstance(r.get("count", 1), int) for r in data.get("refills", [])):
        return False
    for key in DICT_KEYS:
        if not isinstance(data.get(key, {}), dict):
            return False
    return all(isinstance(v, str) for v in data.get("owners", {}).values()) and \
        all(isinstance(v, dict) for v in data.get("focus", {}).values())


def load(root):
    """(pool, ok). A missing file is an empty pool; an unreadable or misshapen one gives ok=False."""
    path = pool_path(root)
    if not os.path.isfile(path):
        return empty(), True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty(), False
    if not well_formed(data):
        return empty(), False
    for key, value in empty().items():
        data.setdefault(key, value)
    return data, True


@contextlib.contextmanager
def locked(root):
    """Load pool.json under an exclusive lock; save it if the block succeeds."""
    os.makedirs(state_dir(root), exist_ok=True)
    with open(os.path.join(state_dir(root), "pool.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data, ok = load(root)
            if not ok:
                raise PoolError("读不出 .claude/state/pool.json；请研究员先看这个文件，不要让会话去改它")
            yield data
            save_json(pool_path(root), data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def log_event(root, rec):
    try:
        path = os.path.join(root, ".claude", "logs", "dispatch.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = dict(rec)
        rec.setdefault("ts", stamp())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(root):
    """The records of dispatch.jsonl. A line that is no JSON object is skipped, and so is one with a byte that
    is no UTF-8: the hooks that read the log (the injection after a compaction, the open items) go on with the rest."""
    out = []
    path = os.path.join(root, ".claude", "logs", "dispatch.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def manifest_version(root):
    try:
        with open(os.path.join(root, ".claude", "solo-lab", "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("plugin_version")
    except (OSError, ValueError, AttributeError):
        return None


# ---------------------------------------------------- decisions waiting
# How long a reversible decision waits before 到期默认 may take it, and which waiting decisions may take it.
# ledger.py (confirm --by 到期默认, query queue) and the inbox hook (the entry's lines about decisions past
# their expiry) both work it out here, each time they need it, from org/decisions.md and the decision's
# registration time: the local decision state keeps no expiry time of its own (1.0.6), so the two never disagree.
EXPIRY_HOURS = 24
EXPIRY_KEY = "可逆决策的到期时长"
DURATION = r"(\d+(?:\.\d+)?)\s*(小时|天)"
EXPIRY_FALLBACK = re.compile(r"读不到时?按\s*\d+(?:\.\d+)?\s*(?:小时|天)算")        # the tail of the plugin's own line
EXPIRY_CHANGE = "改成|改为|改到|延长到|延长为|延长至|缩短到|缩短为|缩短至|调整为|调整到|调成|调到|变成|变为"
EXPIRY_CHANGED = re.compile(r"(?:%s)[^0-9\n，,。；;]{0,8}?%s" % (EXPIRY_CHANGE, DURATION))
# What follows the key when a line states a time, readable or not: a colon or an equals sign, 是 / 为 / 定为 with a
# number after it, or a number straight away (：48h, ：一周, 是 2d, 72). A line that only mentions the key does not.
EXPIRY_STATED = re.compile(r"\s*(?:[:：=＝]|(?:是|为|定为|定成|设为|设成)\s*(?:登记后\s*)?[0-9一二两三四五六七八九十半]|[0-9])")
EXPIRY_UNREADABLE = ("提醒：org/decisions.md 里“%s”一行读不出时长（写成“登记后 24 小时”或“登记后 2 天”；改时长的写成“改成登记后 36 小时”），"
                     "按 %d 小时算" % (EXPIRY_KEY, EXPIRY_HOURS))


def read_expiry(line):
    """(hours, says nothing) for a line of org/decisions.md that names the expiry time. hours when the line
    states one time (登记后 24 小时, 登记后 2 天) or says what the time becomes (从 24 小时改成 36 小时 is 36);
    None otherwise: (None, True) for a line that only mentions the key (先不动，下个月再看), which settles
    nothing; (None, False) for one that states a time and cannot be read without guessing: several times and
    no word for which one holds, a time that is no number (改成两天), another unit or none (：48h, ：一周, ：72)."""
    rest = EXPIRY_FALLBACK.sub("", line.split(EXPIRY_KEY, 1)[1])
    if re.search(EXPIRY_CHANGE, rest):
        hits = EXPIRY_CHANGED.findall(rest)
    else:
        hits = re.findall(DURATION, rest)
        if not hits and not re.search("小时|天", rest) and not EXPIRY_STATED.match(rest):
            return None, True
        if len(hits) != 1:
            return None, False
    if not hits:
        return None, False
    n, unit = hits[-1]
    hours = float(n) * (24 if unit == "天" else 1)
    return (hours, False) if hours > 0 else (None, False)


def expiry_hours(root):
    """(hours, where from, reminder or None): how long a reversible decision waits before 到期默认 may take
    it, counted from its registration. The last line of org/decisions.md that states the time decides, so what
    the researcher wrote under 补充决定 wins over the plugin's own line; without one, 24 hours. A line that
    cannot be read without guessing is not guessed at: 24 hours, with a reminder."""
    try:
        with open(os.path.join(root, "org", "decisions.md"), encoding="utf-8") as f:
            lines = [line for line in f.read().splitlines() if EXPIRY_KEY in line]
    except (OSError, UnicodeDecodeError):
        return EXPIRY_HOURS, "默认", None
    for line in reversed(lines):
        hours, nothing = read_expiry(line)
        if hours:
            return hours, "org/decisions.md", None
        if not nothing:
            break
    return EXPIRY_HOURS, "默认", EXPIRY_UNREADABLE if lines else None


def rolled_back(entry):
    """When a waiting decision was rolled back to 待决定 after the researcher had decided it: rolled_back_at
    (reopened_at in a local decision state written by 1.0.4 or 1.0.5). None when it never was."""
    return entry.get("rolled_back_at") or entry.get("reopened_at") or None


def takes_default(entry):
    """May 到期默认 take this waiting decision once it has expired? Only a reversible one that a session
    carries out with nothing for the researcher to do by hand (default_applies, worked out when it was
    registered), and never one the researcher decided once and rolled back: that one waits for the researcher."""
    return entry.get("default_applies") is True and not rolled_back(entry)


def expires_at(entry, hours):
    """When a waiting decision expires: its registration plus the expiry time. None when the registration
    time cannot be read."""
    since = parse_time(entry.get("registered_at"))
    if since is None:
        return None
    return (since if since.tzinfo else since.astimezone()) + datetime.timedelta(hours=hours)


# ------------------------------------------------------- session status
def session_file(root, sid):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "")[:100] or "_"
    return os.path.join(state_dir(root), "sessions", safe + ".json")


def load_session(root, sid):
    try:
        with open(session_file(root, sid), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_session(root, sid, change):
    """Apply change(state) to this session's own status file under a lock; return the new state."""
    if not sid:
        return {}
    path = session_file(root, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_session(root, sid)
            state.setdefault("session_id", sid)
            change(state)
            state["updated_at"] = stamp()
            save_json(path, state)
            return state
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


WAIT_KEYS = ("waiting_tool_use_id", "waiting_self", "waiting_agent_id", "status_before_wait")


def set_status(root, sid, status, **fields):
    def change(state):
        if not status.startswith("waiting"):
            for k in WAIT_KEYS:
                state.pop(k, None)
        state["status"] = status
        state["status_at"] = stamp()
        state.update({k: v for k, v in fields.items() if v is not None})
    return update_session(root, sid, change)


def end_wait(st, agent_id=None):
    """A tool call went on, so a prompt it was waiting on was answered. A subagent's call ends only a
    wait that is not the session's own prompt (waiting_self) and not another subagent's
    (waiting_agent_id); the session's own call ends any wait but a subagent's. Returns True when
    the wait ended."""
    if not str(st.get("status", "")).startswith("waiting"):
        return False
    owner = st.get("waiting_agent_id")
    if agent_id:
        if st.get("waiting_self") or (owner and owner != agent_id):
            return False
        st["status"] = st.get("status_before_wait") or "busy"
    else:
        if owner and not st.get("waiting_self"):
            return False
        st["status"] = "busy"
    st["status_at"] = stamp()
    for k in WAIT_KEYS:
        st.pop(k, None)
    return True


def mark_claimed(root, sid, claim):
    """Record in the session's own file which claim it holds and under which plugin version."""
    version = manifest_version(root)
    try:
        update_session(root, sid, lambda st: st.update({"name": claim.get("name"), "plugin_version": version}))
    except OSError:
        pass


def files_in(folder, pattern):
    """Paths in folder whose names match the glob pattern, sorted. The folder is taken literally:
    a project path can hold glob characters, as in Phase1[Current]."""
    return sorted(glob.glob(os.path.join(glob.escape(folder), pattern)))


def all_sessions(root):
    out = {}
    for path in files_in(os.path.join(state_dir(root), "sessions"), "*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id"):
            out[data["session_id"]] = data
    return out


def mode_class(mode):
    """The desktop holds a message back when sender and receiver are in different classes."""
    if not mode:
        return None
    return "bypass" if mode == "bypassPermissions" else "other"


# ---------------------------------------------------------------- names
def name_map(policy):
    return {(pol["abbrev"].lower(), pol["rank"]): role for role, pol in policy["agents"].items()}


def canonical(policy, name):
    """Alg-p1-01 for alg-p1-01 or ALG-P1-01; None when the name names no role."""
    m = re.match(r"^([A-Za-z]+)-[pP]([0-2])-(\d+(?:-\d{1,2})?)$", (name or "").strip())
    if not m:
        return None
    role = name_map(policy).get((m.group(1).lower(), int(m.group(2))))
    if not role:
        return None
    return "%s-p%s-%s" % (policy["agents"][role]["abbrev"], m.group(2), m.group(3))


def role_of(policy, name):
    c = canonical(policy, name)
    if not c:
        return None
    m = NAME_RE.match(c)
    return name_map(policy).get((m.group(1).lower(), int(m.group(2))))


def is_slot(name):
    """A +2's name (and the router's) is a pool slot; a +0 name is a slot with two digits (Alg-p0-01)
    and a work name with three or more (Alg-p0-2101, after the work item ID; Alg-p0-2101-3, after the number
    of a task without a work item)."""
    m = NAME_RE.match(name or "")
    return bool(m) and "-" not in m.group(3) and (m.group(2) != "0" or len(m.group(3)) == 2)


def work_item_of(name):
    """The work item ID in a +0 work name (Alg-p0-2101 → 2101), or the task number (Alg-p0-2101-3 → 2101-3);
    None for any other name."""
    m = NAME_RE.match(name or "")
    if m and m.group(2) == "0" and ("-" in m.group(3) or len(m.group(3)) >= 3):
        return m.group(3)
    return None


def slot_name(pol, n):
    return "%s-p%d-%02d" % (pol["abbrev"], pol["rank"], n)


def short_name(name):
    return (name or "").lower()


def resolve(policy, pool, name, item=None):
    """The claim a name in a header or a ledger field stands for: the claim itself, or for a +0
    work name (Alg-p0-2101) the claim that owns that work item. Other names come back canonical."""
    c = canonical(policy, name)
    if not c:
        return name or None
    if c in pool["claims"]:
        return c
    wid = work_item_of(c)
    owner = (pool.get("owners") or {}).get(wid) if wid else None
    if owner and owner in pool["claims"]:
        return owner
    return c


# --------------------------------------------------------------- claims
def claims_of(pool, role=None):
    return [c for c in pool["claims"].values() if role is None or c.get("role") == role]


def claim_by_session(pool, sid):
    if not sid:
        return None
    return next((c for c in pool["claims"].values() if c.get("session_id") == sid), None)


def claim_by_desk(pool, desk):
    if not desk:
        return None
    return next((c for c in pool["claims"].values() if c.get("desk") == desk), None)


def permit_for(pool, sid, desk):
    return next((p for p in pool["permits"]
                 if (sid and p.get("session_id") == sid) or (desk and p.get("desk") == desk)), None)


CARD_DAYS = 7                         # a start card nobody clicked is gone from the app after a week


def card_permit(pool, name, role, at=None):
    """The permit that waits under a session name for whichever session a start card opens: `pool.py expect
    --card` writes it without a session, since a card has none until the researcher clicks it, and the
    session's own `pool.py claim --as <name>` binds it. One older than CARD_DAYS waits for nobody."""
    if not name:
        return None
    for p in pool["permits"]:
        if p.get("via") == "card" and p.get("name") == name and p.get("role") == role and not (p.get("session_id") or p.get("desk")):
            made = parse_time(p.get("at") or "")
            if made is None or ((at or now()) - made).days < CARD_DAYS:
                return p
    return None


def pending_for(pool, sid):
    return [p for p in pool["pending"] if sid and p.get("session_id") == sid]


def tombstoned(pool, sid, desk):
    return any((sid and t.get("session_id") == sid) or (desk and t.get("desk") == desk)
               for t in pool["tombstones"])


def first_writes_due(root):
    """The scribe's two first writes (the knowledge nodes, the first research task) are still to
    do: nothing has been registered from this directory yet."""
    sent = os.path.join(root, "packages", "sent")
    for name in os.listdir(sent) if os.path.isdir(sent) else []:
        if name.endswith(".json") and not name.startswith("reply-"):
            try:
                with open(os.path.join(sent, name), encoding="utf-8") as f:
                    if (json.load(f) or {}).get("status") == "registered":
                        return False
            except (OSError, ValueError, AttributeError):
                pass
    return True


def single_roles(policy):
    return policy.get("single_claim_roles") or [policy.get("entry_role", "pmo-p2"), policy.get("scribe_role", "pmo-scribe")]


def role_limit(policy, role):
    pol = policy["agents"].get(role) or {}
    return 1 if role in single_roles(policy) else int(pol.get("limit") or 1)


def fixed_name(policy, role):
    return (policy["agents"].get(role) or {}).get("fixed_session")


def cleared(root, claim):
    """The claim's recorded session ended with /clear (SessionEnd reason clear, not resumed since)."""
    st = load_session(root, claim.get("session_id") or "")
    return st.get("status") == "offline" and st.get("ended_reason") == "clear"


def consume_refill(pool, role):
    for r in pool["refills"]:
        if r.get("role") == role:
            r["count"] = int(r.get("count") or 1) - 1
            if r["count"] <= 0:
                pool["refills"].remove(r)
            return


def promote(pool, policy, permit, sid, desk, at=None):
    """Turn permit into a claim for session sid. Returns (claim, note); claim is None when refused."""
    role = permit.get("role")
    pol = policy["agents"].get(role)
    if not pol:
        return None, "没有角色 %s" % role
    held = claims_of(pool, role)
    if len(held) >= role_limit(policy, role):
        return None, "%s 已有 %d 条认领（%s），上限 %d；要换会话，研究员先 release 旧的" % (
            role, len(held), "、".join(c["name"] for c in held), role_limit(policy, role))
    name, note = permit.get("name"), None
    fixed = pol.get("fixed_session")
    if fixed:
        if name != fixed:
            note = "%s 的会话名固定是 %s" % (role, fixed)
        name = fixed
        if name in pool["claims"]:
            return None, "会话名 %s 已被占用" % name
    elif not name or name in pool["claims"]:
        n = 1
        while slot_name(pol, n) in pool["claims"]:
            n += 1
        note = "会话名 %s 已被占用，改用 %s" % (name, slot_name(pol, n))
        name = slot_name(pol, n)
    claim = {"name": name, "short": short_name(name), "role": role, "session_id": sid,
             "desk": desk or permit.get("desk"), "claimed_at": stamp(at), "via": permit.get("via"),
             "by": permit.get("by")}
    pool["claims"][name] = claim
    # the permit that was used, and any other one for this same session: a +2 opened by the researcher's
    # session is pre-registered by its desktop ID and may claim through its opening line first
    pool["permits"] = [p for p in pool["permits"] if p is not permit
                       and not (sid and p.get("session_id") == sid) and not (claim["desk"] and p.get("desk") == claim["desk"])]
    pool["pending"] = [p for p in pool["pending"] if p.get("session_id") != sid]
    pool["tombstones"] = [t for t in pool["tombstones"]
                          if not ((sid and t.get("session_id") == sid) or (claim["desk"] and t.get("desk") == claim["desk"]))]
    consume_refill(pool, role)
    return claim, note


def promote_session(root, policy, sid, desk, how):
    """Promote a permit waiting for this session, under the lock. A permit that cannot be
    promoted is dropped, so it is not retried on every hook call. Returns the claim or None."""
    permit = None
    try:
        with locked(root) as pool:
            claim = claim_by_session(pool, sid)
            if claim:
                return claim
            permit = permit_for(pool, sid, desk)
            if not permit:
                return None
            claim, note = promote(pool, policy, permit, sid, desk)
            if claim is None and permit in pool["permits"]:
                pool["permits"].remove(permit)
    except (PoolError, OSError):
        return None
    log_event(root, {"event": "claim" if claim else "claim_refused", "how": how, "session_id": sid,
                     "desk": desk or None, "name": (claim or permit).get("name"),
                     "role": (claim or permit).get("role"), "note": note})
    if claim:
        mark_claimed(root, sid, claim)
    return claim


def reattach(root, name, sid, desk=None, how="desk"):
    """A claim moves to a new session ID: its desktop ID after /clear or a resume, or a /clear pairing."""
    old = None
    try:
        with locked(root) as pool:
            claim = pool["claims"].get(name)
            if not claim:
                return None
            old = claim.get("session_id")
            claim["session_id"] = sid
            if desk:
                claim["desk"] = desk
            claim["reattached_at"] = stamp()
            pool["reattach"] = [r for r in pool["reattach"] if r.get("name") != name]
    except (PoolError, OSError):
        return None
    log_event(root, {"event": "reattach", "how": how, "name": name, "old_session_id": old, "session_id": sid})
    mark_claimed(root, sid, claim)
    return claim


def no_write(pol):
    p = dict(pol)
    p.update(lark_write=[], meegle_write=[], ledger_write=False)
    return p


class Identity:
    """Who a hook call comes from.

    origin: subagent | agent_flag | claim | permit | pending | tombstone | desk_conflict |
            pool_unreadable | researcher.
    researcher: the researcher drives this session (it holds no claim, permit or tombstone):
    hard rules ask instead of deny.
    """

    def __init__(self, role, pol, origin, name=None, researcher=False, claim=None, conflict=None):
        self.role, self.pol, self.origin, self.name = role, pol, origin, name
        self.researcher, self.claim, self.conflict = researcher, claim, conflict

    @property
    def label(self):
        return self.name or self.role


def scribe_ok(pool, policy, claim):
    """The claim is the one unreleased scribe claim: the only session that writes the ledger."""
    scribe = policy.get("scribe_role", "pmo-scribe")
    held = claims_of(pool, scribe)
    return claim.get("role") == scribe and len(held) == 1 and held[0].get("name") == claim.get("name")


def claim_identity(policy, pool, claim):
    role = claim.get("role")
    pol = policy["agents"].get(role)
    if not pol:
        return Identity(role or "unknown", policy["default_agent"], "claim", claim.get("name"), False, claim)
    if role == policy.get("scribe_role", "pmo-scribe") and not scribe_ok(pool, policy, claim):
        pol = no_write(pol)
    return Identity(role, pol, "claim", claim.get("name"), False, claim)


def may_reattach(root, claim, data):
    """A claim found only by desktop ID moves to this session only at this session's
    SessionStart after /clear, when the claim's own session ended with /clear."""
    return (data.get("hook_event_name") == "SessionStart" and data.get("source") == "clear"
            and cleared(root, claim))


def identify(data, policy, root, env=None, promote_permits=True):
    env = os.environ if env is None else env
    agents = policy["agents"]
    agent = data.get("agent_type") or ""
    sid = data.get("session_id") or ""
    desk = env.get(DESK_ENV) or ""
    if data.get("agent_id"):
        pol = agents.get(agent, policy["default_agent"])
        return Identity(agent if agent in agents else (agent or "unknown"),
                        no_write(pol), "subagent")
    if agent in agents:                                  # claude --agent <role> in a terminal
        return Identity(agent, no_write(agents[agent]), "agent_flag")
    pool, ok = load(root)
    if not ok:
        return Identity("unknown", policy["default_agent"], "pool_unreadable")
    claim = claim_by_session(pool, sid)
    if claim is None:
        other = claim_by_desk(pool, desk)
        if other is not None:
            if not (promote_permits and may_reattach(root, other, data)):
                return Identity("desk_conflict", policy["default_agent"], "desk_conflict", conflict=other.get("name"))
            claim = reattach(root, other["name"], sid, desk) or other
            pool, _ = load(root)
    if claim is None:
        permit = permit_for(pool, sid, desk)
        if permit and promote_permits:
            claim = promote_session(root, policy, sid, desk, "hook")
            pool, _ = load(root)
        if claim is None and permit and permit in pool["permits"]:
            return Identity(permit.get("role"), no_write(agents.get(permit.get("role"), policy["default_agent"])),
                            "permit", permit.get("name"))
    if claim:
        return claim_identity(policy, pool, claim)
    pend = pending_for(pool, sid)
    if pend:
        return Identity(pend[0].get("role"), no_write(agents.get(pend[0].get("role"), policy["default_agent"])),
                        "pending", pend[0].get("name"))
    if tombstoned(pool, sid, desk):
        return Identity("released", policy["default_agent"], "tombstone")
    return Identity("researcher", policy["default_agent"], "researcher", researcher=True)


# --------------------------------------------------------------- opening
def opening_permit(root, policy, sid, desk, prompt):
    """The researcher's opening line in a new top-level session: a permit for a +2 role."""
    m = OPENING_RE.match(prompt or "")
    if not m:
        return None, None
    role, name = m.group(1), canonical(policy, m.group(2))
    pol = policy["agents"].get(role)
    if not pol or pol["rank"] != 2 or role_of(policy, name) != role:
        return None, "开局句里的角色 %s 和会话名 %s 对不上，或者不是 +2；只有 +2 能用开局句认领" % (role, m.group(2))
    if pol.get("fixed_session") and name != pol["fixed_session"]:
        return None, "%s 的会话名固定是 %s，开局句写错了；改成“认领 %s（会话名 %s）”再开一个会话" % (
            role, pol["fixed_session"], role, pol["fixed_session"])
    try:
        with locked(root) as pool:
            if claim_by_session(pool, sid):
                return None, None
            permit = {"name": name, "role": role, "session_id": sid, "desk": desk or None,
                      "by": "researcher", "via": "opening", "at": stamp()}
            pool["permits"] = [p for p in pool["permits"] if p.get("session_id") != sid]
            pool["permits"].append(permit)
            claim, note = promote(pool, policy, permit, sid, desk)
            if claim is None:
                pool["permits"].remove(permit)
    except (PoolError, OSError) as e:
        return None, str(e)
    log_event(root, {"event": "claim" if claim else "claim_refused", "how": "opening", "session_id": sid,
                     "desk": desk or None, "name": name, "role": role, "note": note})
    if claim:
        mark_claimed(root, sid, claim)
    return claim, note


# -------------------------------------------------------------- messages
def header_re(policy):
    names = sorted((policy.get("msg") or {}).values(), key=len, reverse=True)
    return re.compile(r"^\s*(?:\*\*)?(?:[（(【]重发[^）)】]*[）)】]\s*)?【(%s)(?:\s*#(%s))?[^】]*】\s*(?:(%s)\s*(?:→|->)\s*(%s))?" % (
        "|".join(re.escape(n) for n in names), ITEM_IN_TEXT, NAME_IN_TEXT, NAME_IN_TEXT))


def parse_message(policy, text):
    """The message header that starts one of the first non-empty lines: kind, work item ID, sender,
    receiver. A header quoted inside a sentence is not a message. None if there is none. id is the first
    work item the header names and ids all of them."""
    if not text or not policy.get("msg"):
        return None
    kinds = {v: k for k, v in policy["msg"].items()}
    rx = header_re(policy)
    lines = [ln for ln in text.splitlines() if ln.strip()][:HEADER_LINES]
    for line in lines:
        m = rx.match(line)
        if not m:
            continue
        p0 = re.search(r"会话名[：:]\s*(%s)" % NAME_IN_TEXT, text)
        named = re.search(r"【%s[^】]*】" % re.escape(m.group(1)), line)       # 【登记回执 #2140 / #2141】: one answer for several items
        return {"kind": kinds[m.group(1)], "id": m.group(2), "ids": re.findall(r"#(%s)" % ITEM_IN_TEXT, named.group(0)) if named else [],
                "from": canonical(policy, m.group(3)) or m.group(3), "to": canonical(policy, m.group(4)) or m.group(4),
                "p0_name": canonical(policy, p0.group(1)) if p0 else None, "line": line.strip()[:200]}
    return None


# ----------------------------------------------------------------- turns
REMINDER_RE = re.compile(r"\s*<system-reminder(?:\s[^>]*)?>.*?</system-reminder>", re.S)
WRAPPER_RE = re.compile(r"\s*(?:Another Claude session sent a message:\s*)?"
                        r"<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|\Z)", re.S)
ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
SIDE_EVENT_RE = re.compile(r"<side-sessions-event[\s>]")
STARTED_BY = "Background (from the session that started this one):"
GENERATED = re.compile(r"\s*<[a-z]+(?:-[a-z]+)+[\s>]")     # <task-notification> and other text the app writes
FROM_MY_SESSIONS = "本会话开出的会话"
FROM_MY_OPENER = "开出本会话的会话"


def strip_reminders(text):
    """text without the <system-reminder> blocks the app may put in front of it, and without the
    white space before what is left."""
    text = text or ""
    while True:
        m = REMINDER_RE.match(text)
        if not m:
            return text.lstrip()
        text = text[m.end():]


def unwrap(text):
    """(body, sender) for a message another session sent: the text inside <cross-session-message>
    (the model sees the line "Another Claude session sent a message:" before it, so that is taken
    too) and {"from": its session ID, "from_name": its title}; (text, None) for anything else."""
    m = WRAPPER_RE.match(text or "")
    if not m:
        return text, None
    attrs = {k: html.unescape(dq or sq) for k, dq, sq in ATTR_RE.findall(m.group(1))}
    return m.group(2).strip(), {"from": attrs.get("from"), "from_name": attrs.get("from-name")}


def turn_origin(policy, prompt):
    """(origin, source) for the turn a prompt starts; origin is agent, researcher or system.

    After the <system-reminder> blocks in front:
    - a <cross-session-message> (after "Another Claude session sent a message:" or not): agent,
      from the sender's title (from-name), or its session ID (from) when it has none;
    - <side-sessions-event>: agent, a session this one opened finished a turn;
    - "Background (from the session that started this one):": agent, the first turn the session
      that opened this one wrote;
    - a message header (parse_message) at the start of one of the first lines: agent, from the
      sender in the header;
    - any other tag the app writes (<task-notification> ...): system, no source;
    - nothing (the app's reminders alone): system, no source;
    - anything else: researcher, no source. A header quoted inside a sentence is the researcher's.
    """
    text = strip_reminders(prompt)
    if not text:
        return "system", None
    _, sender = unwrap(text)
    if sender is not None:
        return "agent", sender.get("from_name") or sender.get("from") or None
    if SIDE_EVENT_RE.match(text):
        return "agent", FROM_MY_SESSIONS
    if text.startswith(STARTED_BY):
        return "agent", FROM_MY_OPENER
    msg = parse_message(policy, text)
    if msg:
        return "agent", msg.get("from")
    if GENERATED.match(text):
        return "system", None
    return "researcher", None


def addressed_to(policy, pool, msg, me):
    """Is this message addressed to the claim me? A +0 work name of my own role counts as me."""
    to = msg.get("to")
    if not to:
        return True
    if to == me["name"]:
        return True
    return bool(work_item_of(to)) and role_of(policy, to) == me.get("role")


def _matches(policy, spec, role):
    """An end of an edge names a role, or a rank: p2 and p0 stand for the sequences' own roles, and the
    router and the scribe go by their names (orgspec._edge_targets reads the graph the same way)."""
    pol = policy["agents"].get(role)
    return bool(pol) and (spec == role or (spec == "p%d" % pol["rank"] and role == "%s-p%d" % (pol["sequence"], pol["rank"])))


def edge_ok(policy, edge, a, b):
    if not (_matches(policy, edge["from"], a) and _matches(policy, edge["to"], b)):
        return False
    sa, sb = policy["agents"][a]["sequence"], policy["agents"][b]["sequence"]
    scope = edge.get("scope")
    if scope == "same_sequence":
        return sa == sb
    if scope == "other_sequences":
        return sa != sb
    return True


def kind_allowed(policy, kind, sender, receiver):
    """Does the dispatch graph let sender send this kind of message to receiver (role names)?"""
    d = policy.get("dispatch") or {}
    rule = (d.get("kinds") or {}).get(kind)
    if rule is None or sender not in policy["agents"] or receiver not in policy["agents"]:
        return False
    if rule.get("along"):
        a, b = (receiver, sender) if rule.get("reverse") else (sender, receiver)
        along = [rule["along"]] if isinstance(rule["along"], str) else rule["along"]    # one group of edges, or several
        return any(edge_ok(policy, e, a, b) for group in along for e in d.get(group, []))
    if rule.get("to") and receiver != rule["to"]:
        return False
    if rule.get("from") and sender != rule["from"]:
        return False
    return True


def receipt_exists(root, item_id):
    if not item_id:
        return False
    sent, key = os.path.join(root, "packages", "sent"), glob.escape(str(item_id))
    return bool(files_in(sent, key + ".json") or files_in(sent, key + "-*.json"))


def receive_checks(policy, root, pool, msg, me):
    """Reminders for a message this claim received; also the norms it breaks, for the log.
    msg["from"] is already resolved to a claim name where possible."""
    out = []
    names = policy["msg"]
    kind, item, sender = msg["kind"], msg.get("id"), msg.get("from")
    sender_claim = pool["claims"].get(sender) if sender else None
    if sender and not sender_claim:
        out.append("发出方 %s 不在会话池的认领表里；先用 pool.py show 核对是谁发的" % sender)
    if not addressed_to(policy, pool, msg, me):
        out.append("抬头写的接收方是 %s，不是本会话 %s；确认是不是发错了" % (msg["to"], me["name"]))
    entry_role, hub = policy.get("entry_role"), policy.get("router_role")
    ends = {sender_claim["role"], me["role"]} if sender_claim else set()
    if hub in policy["agents"] and entry_role in ends and not ends <= {entry_role, hub, policy.get("scribe_role")}:
        # 1.1.0: the entry talks to the researcher and the router only
        fixed = policy["agents"][hub].get("fixed_session") or hub
        out.append("入口只和研究员、路由往来，这条越过了路由 %s：%s" % (fixed, (
            "进展、交付和要研究员拍板的事该发给路由，由它改状态包、把里程碑告诉你；这条转给路由，并请发出方以后直接发给它"
            if me["role"] == entry_role else "研究员的话该由入口照录交给路由，路由转给你；这条照常处理，回话发给路由，不发入口")))
    elif sender_claim and not kind_allowed(policy, kind, sender_claim["role"], me["role"]):
        out.append("派发图里 %s（%s）不给 %s（%s）发【%s】；照规范该走本序列 +2，跨序列的该由两边的 +2 往来，接不接先和发出方确认" % (
            sender, sender_claim["role"], me["name"], me["role"], names.get(kind, kind)))
    routing = sender_claim and sender_claim["role"] == policy.get("entry_role") and me["role"] == policy.get("router_role")
    if kind == "assign" and item and not receipt_exists(root, item):
        out.append(("#%s 在 packages/sent/ 里没有回执；这是没建工作项的任务，派活的会话要先运行 docs.py upload 取得编号，先跟委托方确认" if "-" in item else
                    "#%s 在 packages/sent/ 里没有登记回执；委托要先登记，先跟委托方确认") % item)
    if kind == "assign" and not item and not routing:
        out.append("委托消息没写工作项 ID；先跟委托方确认是否已登记")
    if kind == "assign" and (policy["agents"].get(me["role"]) or {}).get("rank") == 0 and not msg.get("p0_name"):
        out.append("委托 +0 的消息要单独写一行“会话名：<简写>-p0-<工作项 ID>”；先按需求包里的工作项 ID 定会话名")
    if kind == "report" and item:
        owner = (pool.get("owners") or {}).get(item)
        if owner and sender and owner != sender:
            owner_claim, above = pool["claims"].get(owner), False
            if owner_claim and sender_claim:
                o, s = policy["agents"].get(owner_claim["role"]) or {}, policy["agents"].get(sender_claim["role"]) or {}
                above = s.get("sequence") == o.get("sequence") and s.get("rank", 0) > o.get("rank", 0)
            # the owner's own +2 or +1 passing the result up, and whoever reports to the entry, is the normal
            # way; a report from elsewhere may be a late one from the session the item was taken from
            if not above and me["role"] != policy.get("entry_role"):
                out.append("#%s 的负责会话是 %s，这条交付是 %s 发的；可能是重新委托后旧会话迟到的交付，先核对再用" % (item, owner, sender))
    return out


# ------------------------------------------------------------ open items
def all_open_items(root, owners=None):
    """{name: ({#ID: receiver} assigned and not reported back, {#ID: sender} still owed)} from the
    messages the receivers' hooks saw. One pass over dispatch.jsonl. A session owes an item until it reports
    it: to the session it owes it to, or to another one the dispatch graph lets it report to (in_graph, which
    the receiver's hook writes from 1.0.12 on)."""
    owners = owners or {}
    sent, owed = {}, {}
    for rec in read_events(root):
        if rec.get("event") != "message" or not rec.get("id"):
            continue
        kind, item = rec.get("kind"), rec["id"]
        frm, to = rec.get("from"), rec.get("seen_by") or rec.get("to")
        if not frm or not to:
            continue
        if kind == "assign":
            prev = sent.get(frm, {}).get(item)
            if prev and prev != to and owed.get(prev, {}).get(item) == frm:
                owed[prev].pop(item)                 # re-dispatched: the old receiver owes nothing
            sent.setdefault(frm, {})[item] = to
            owed.setdefault(to, {})[item] = frm
        elif kind == "report":
            mine = sent.get(to, {})
            if item in mine and (mine[item] == frm or owners.get(item) == frm):
                mine.pop(item)
            theirs = owed.get(frm, {})
            if theirs.get(item) == to:
                theirs.pop(item)
            elif item in theirs and rec.get("in_graph"):
                # delivered to somebody else, along an edge of the graph: a 跨序列需求 the entry passed on before
                # 1.0.12 is answered to the +2 that asked for it, and the entry is owed nothing more
                waiting = theirs.pop(item)
                if sent.get(waiting, {}).get(item) == frm:
                    sent[waiting].pop(item)
    names = set(sent) | set(owed)
    return {n: (sent.get(n, {}), owed.get(n, {})) for n in names}


def open_items(root, name, owners=None):
    """([(#ID, receiver)] assigned by name and not reported back, [(#ID, sender)] name still owes)."""
    sent, owed = all_open_items(root, owners).get(name, ({}, {}))
    return sorted(sent.items()), sorted(owed.items())


# ----------------------------------------------- research tasks of held items
# A session whose context was compacted still knows the work items it holds, not where they sit in their
# research task: which decision the task waits on, which conditions of the experiment are settled, what is
# paused. The task's main document keeps that in its 状态包 (`ledger.py status <研究任务 ID>`; the entry keeps it
# current). So what is injected after a compaction names the research tasks of the items the session holds,
# as far as the registration receipts say (packages/sent/<ID>.json, written by ledger.py: type, task, source),
# with the command that prints a task's 状态包, given once. Local files only: nothing here calls Feishu, and
# whatever cannot be found becomes one line that says so, never an error. The line shares the injection's
# limit with the role text and the open items, and the entry's role text nearly fills it: the line names the
# tasks, not every item again, and the open items are written by session (items_text).
TASK_TYPE = "research_task"            # the contract's main-line type, as a receipt names it
TASK_HOPS = 5                          # how far up 来源工作项 the receipts are followed
TASKS_SHOWN = 4                        # research tasks named in the line, at most
TASK_ITEMS_SHOWN = 6                   # work items named for one research task, at most
STATUS_CMD = "python3.12 -I .claude/solo-lab/bin/ledger.py status %s"
COMPACTED = "上下文刚压缩过，接着干活之前先读所属研究任务的状态包（规矩照角色说明里“压缩上下文以后”那一条）："
NOTHING_HELD = ("上下文刚压缩过。派发记录里本会话没有未了的工作项：手上确实没有的，说一句“跳过”再继续；"
                "有的，照角色说明里“压缩上下文以后”那一条找到它所属的研究任务，先运行 %s 读状态包。" % (STATUS_CMD % "<研究任务 ID>"))
TASK_UNKNOWN = ("%s 在本地回执里查不到所属研究任务：用 ledger.py get <工作项 ID> 看“所属研究任务”字段，"
                "没有就用 ledger.py trace <工作项 ID> 沿“来源工作项”往上找，找到了%s；找不到就说一句“跳过”再继续")
EACH_TASK = "逐条运行 %s" % (STATUS_CMD % "<研究任务 ID>")
# The router keeps the 状态包 of every research task that is not archived and the entry answers the researcher from
# them (1.1.0; until then the entry kept them), so both read them all, whatever they hold.
ENTRY_COMPACTED = COMPACTED.replace("所属研究任务的", "研究任务的")
ENTRY_TASKS = ENTRY_COMPACTED + "未了事项属于研究任务 %s；连同其余没归档的（ledger.py query tasks 列出），" + EACH_TASK + "。"
ENTRY_NOTHING = ENTRY_COMPACTED + "用 ledger.py query tasks 列出没归档的研究任务，" + EACH_TASK + "；一条都没有，说一句“跳过”再继续。"
HINT_FAILED = "上下文刚压缩过。没能从本地回执查出手上工作项所属的研究任务（%s）：照角色说明里“压缩上下文以后”那一条自己找，找不到就说一句“跳过”再继续。"


# <ID>.json, <ID>-<draft file>.json; <ID>-<n>.json is a task without a work item (its own number); not reply-<ID>-….json
RECEIPT_NAME = re.compile(r"(\d+(?:-\d{1,2})?)(?:-.*)?\.json$")


def receipt_files(root):
    """{work item ID: [paths]} of the registration receipts in packages/sent/, <ID>.json before <ID>-<draft
    file>.json, from one listing of the folder; {} when it cannot be listed."""
    sent, out = os.path.join(root, "packages", "sent"), {}
    try:
        names = os.listdir(sent)
    except OSError:
        return out
    for name in sorted(names, key=lambda n: ("-" in n, n)):
        m = RECEIPT_NAME.match(name)
        if m:
            out.setdefault(m.group(1), []).append(os.path.join(sent, name))
    return out


def registration_receipt(root, item_id, files=None, seen=None):
    """The registration receipt of a work item, packages/sent/<ID>.json or <ID>-<draft file>.json; {} when
    there is none or it cannot be read. files: receipt_files(root), when the caller looks up many; seen:
    receipts read before, by ID."""
    key = str(item_id)
    if seen is not None and key in seen:
        return seen[key]
    found = {}
    for path in (receipt_files(root) if files is None else files).get(key, []):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and str(rec.get("work_item_id")) == key:
            found = rec
            break
    if seen is not None:
        seen[key] = found
    return found


def research_task_of(root, item_id, files=None, seen=None):
    """The ID of the research task a work item belongs to, by the registration receipts alone: the item
    itself when it is a research task, else the task its receipt names (所属研究任务), else the same for the
    item it came from (来源工作项), TASK_HOPS items up at most. None when the receipts do not say."""
    item, walked = str(item_id or "").lstrip("#"), set()
    files = receipt_files(root) if files is None else files
    for _ in range(TASK_HOPS):
        if not re.fullmatch(r"\d+(?:-\d{1,2})?", item) or item in walked:      # an ID, or the number of a task without a work item
            return None
        walked.add(item)
        rec = registration_receipt(root, item, files, seen)
        if rec.get("type") == TASK_TYPE:
            return item
        task = str(rec.get("task") or "").lstrip("#")
        if task.isdigit():
            return task
        item = str(rec.get("source") or "").lstrip("#")
    return None


def listed(ids, shown):
    """#1、#2 … for the first IDs, then how many there are when more."""
    text = "、".join("#%s" % i for i in ids[:shown])
    return text + (" 等 %d 条" % len(ids) if len(ids) > shown else "")


def status_hint(root, items, entry=False):
    """What a session is told once its context was compacted: the research tasks of the work items it holds
    (items: their IDs, the ones it is working on first) and the command that prints a task's 状态包, written
    once: with the task's ID when there is one task, with a placeholder when there are several. For an item
    whose research task the receipts do not give, how to find it and that it may be skipped; with no item,
    that there is nothing to read. The entry (entry) keeps the 状态包 of every research task that is not
    archived: it is told to list those and read each, whatever it holds, and its own items are named by their
    tasks alone (the open items above the line have the IDs). One paragraph; never raises."""
    try:
        ids = []
        for i in items or []:
            i = str(i or "").lstrip("#")
            if re.fullmatch(r"\d+(?:-\d{1,2})?", i) and i not in ids:      # an ID, or the number of a task without a work item
                ids.append(i)
        tasks, unknown, files, seen = {}, [], receipt_files(root) if ids else {}, {}       # the folder is listed once
        for i in ids:
            task = research_task_of(root, i, files, seen)
            if task:
                tasks.setdefault(task, []).append(i)
            else:
                unknown.append(i)
        if entry:
            return ENTRY_TASKS % listed(list(tasks), TASKS_SHOWN) if tasks else ENTRY_NOTHING
        if not ids:
            return NOTHING_HELD
        shown = list(tasks.items())[:TASKS_SHOWN]
        one = len(tasks) == 1                        # one task: the command with its ID; several: the command once
        parts = []
        for task, held in shown:
            held = [i for i in held if i != task]
            parts.append("%s研究任务 #%s%s" % ("%s 属于" % listed(held, TASK_ITEMS_SHOWN) if held else "手上的是", task,
                                            "，运行 %s" % (STATUS_CMD % task) if one else ""))
        if len(tasks) > TASKS_SHOWN:
            parts.append("另有 %d 条研究任务，用 ledger.py query tasks 列出" % (len(tasks) - TASKS_SHOWN))
        if not one and tasks:
            parts.append(EACH_TASK)
        if unknown:
            parts.append(TASK_UNKNOWN % (listed(unknown, TASK_ITEMS_SHOWN),
                                         "运行 %s" % (STATUS_CMD % "<研究任务 ID>") if one or not tasks else "照上面的命令读"))
        return COMPACTED + "；".join(parts) + "。"
    except Exception as e:                           # a hint, never a reason to fail the hook
        return HINT_FAILED % (str(e)[:120] or type(e).__name__)


def reads_every_task(policy, role):
    """The two sessions that read the 状态包 of every research task that is not archived, whatever they hold: the
    router keeps them (1.1.0; until then the entry did), and the entry answers the researcher from them."""
    return role in (policy.get("entry_role"), policy.get("router_role"))


def compaction_hint(root, policy, claim, owners=None, current=None):
    """status_hint for the session that holds claim, from the open items the dispatch log gives it and the item
    its status file names (current). Whatever goes wrong on the way becomes the line that says so."""
    try:
        sent, owed = open_items(root, claim.get("name"), owners)
        return status_hint(root, held_items(sent, owed, current), entry=reads_every_task(policy, claim.get("role")))
    except Exception as e:
        return HINT_FAILED % (str(e)[:120] or type(e).__name__)


def role_body(root, role):
    try:
        with open(os.path.join(root, ".claude", "agents", role + ".md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            text = text[end + 5:]
    return text.strip()


def describe(policy, claim):
    pol = policy["agents"].get(claim.get("role")) or {}
    return "%s（%s +%s，角色 %s，标签 %s）" % (claim.get("name"), pol.get("sequence_cn") or pol.get("sequence", ""),
                                       pol.get("rank", "?"), claim.get("role"), pol.get("tag", ""))


def items_text(pairs, fmt, more):
    """The first ITEMS_SHOWN of [(#ID, session)], the items of one session written together (#1、#2→X；#3→Y):
    a session that assigned many items to a few others sees them all in little room."""
    groups = {}
    for item, other in pairs[:ITEMS_SHOWN]:
        groups.setdefault(other, []).append("#%s" % item)
    shown = [fmt % ("、".join(ids), other) for other, ids in groups.items()]
    if len(pairs) > ITEMS_SHOWN:
        shown.append(more % (len(pairs) - ITEMS_SHOWN))
    return "；".join(shown)


def held_items(sent, owed, current=None):
    """The IDs of the work items a session holds, the ones it works on first: what it still owes, the item
    its last 【委托】 or 【继续】 named (current), then what it assigned and has not got back."""
    ids = [i for i, _ in owed] + [current] + [i for i, _ in sent]
    return [i for n, i in enumerate(ids) if i and i not in ids[:n]]


def injection(root, policy, claim, reason="", limit=INJECT_LIMIT, owners=None, compacted=False, current=None):
    """Role text injected after a claim, a resume, a compaction or a /clear: the role definition
    without frontmatter, the open items and a pointer, at most limit characters: what does not fit is cut
    from the end of the role text, with a pointer to the file. After a compaction (compacted) it ends with
    the research tasks of the items the session holds and the command that reads a task's 状态包
    (status_hint); current is the work item the session's status file names."""
    name, role = claim.get("name"), claim.get("role")
    sent, owed = open_items(root, name, owners)
    items = []
    if sent:
        items.append("委托出去还没交付：" + items_text(sent, "%s→%s", "另有 %d 条，用 pool.py show --recover 看全"))
    if owed:
        items.append("还欠交付：" + items_text(owed, "%s（交付给 %s）", "另有 %d 条"))
    head = "%s本会话是 %s。下面的角色说明就是本会话的工作说明。" % (reason, describe(policy, claim))
    tail = "未了事项：%s。台账和状态包是正本：开工前用 ledger.py get 读工作项，找人用 pool.py show。完整说明在 .claude/agents/%s.md。" % (
        "；".join(items) or "无", role)
    if compacted:
        tail += "\n\n" + status_hint(root, held_items(sent, owed, current), entry=reads_every_task(policy, role))
    body = role_body(root, role)
    room = limit - len(head) - len(tail) - 4
    if len(body) > room:
        cut = "……（下文略，见 .claude/agents/%s.md）" % role
        body = body[:max(room - len(cut), 0)] + cut
    text = "\n\n".join(x for x in (head, body, tail) if x)
    return text[:limit]


def reminder_line(policy, claim):
    pol = policy["agents"].get(claim.get("role")) or {}
    return "你是 %s（%s +%s），标签 %s。委托、交付、登记都发消息，抬头照 CLAUDE.md 的格式；找人先跑 pool.py show。" % (
        claim.get("name"), pol.get("sequence_cn") or pol.get("sequence", ""), pol.get("rank", "?"), pol.get("tag", ""))


# ------------------------------------------------------------------ view
def last_activity(st):
    times = [t for t in (parse_time(st.get("last_tool_at")), parse_time(st.get("status_at"))) if t]
    return max(times) if times else None


def focus_expired(st, at=None):
    """A focus flag lapses once its session has been idle for 30 minutes."""
    at = at or now()
    since = parse_time(st.get("status_at"))
    return st.get("status") in ("idle", "idle_bg", "offline") and bool(since) and at - since > FOCUS_IDLE


def session_view(root, policy, pool, sessions=None, at=None):
    """One row per claim: name, role, status (with uncertain for a busy session gone quiet), work
    items, last activity, desktop ID, permission mode, focus flag, quota pause, status before the end."""
    at = at or now()
    sessions = all_sessions(root) if sessions is None else sessions
    owners = {}
    for item, owner in (pool.get("owners") or {}).items():
        owners.setdefault(owner, []).append(item)
    rows = []
    for name, claim in sorted(pool["claims"].items()):
        st = sessions.get(claim.get("session_id")) or {}
        status = st.get("status") or "unknown"
        last = last_activity(st)
        if status == "busy" and last and at - last > STALE_BUSY:
            status = "uncertain"
        focus = (pool.get("focus") or {}).get(name)
        focus_left = None
        if focus:
            if focus_expired(st, at):
                focus = None
            elif st.get("status") in ("idle", "idle_bg", "offline") and parse_time(st.get("status_at")):
                focus_left = int((FOCUS_IDLE - (at - parse_time(st["status_at"]))).total_seconds() // 60)
        rows.append({"name": name, "short": claim.get("short"), "role": claim.get("role"), "status": status,
                     "work_items": sorted(owners.get(name, [])), "last": stamp(last) if last else None,
                     "desk": claim.get("desk"), "permission_mode": st.get("permission_mode"), "cwd": st.get("cwd"),
                     "version": st.get("version"), "plugin_version": st.get("plugin_version"),
                     "denied": st.get("denied", 0), "focus": focus, "focus_left_min": focus_left,
                     "quota": st.get("quota"), "quota_at": st.get("quota_at"),
                     "prev_status": st.get("prev_status"), "last_message": st.get("last_message")})
    return rows


def mixed_modes(rows):
    """Names whose permission-mode class differs from most of the pool."""
    classes = [mode_class(r.get("permission_mode")) for r in rows if r.get("permission_mode")]
    if len(set(classes)) < 2:
        return []
    major = max(set(classes), key=classes.count)
    return [r["name"] for r in rows if r.get("permission_mode") and mode_class(r["permission_mode"]) != major]


# ------------------------------------------------- shared by the session scripts
# What pool_cmd.py, notify.py, todo.py and route.py all use: who is running the script, the hook policy,
# the inbox files. Until 1.1.0 this was in route.py.
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


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


ORIGIN_LABEL = {"researcher": "研究员的会话", "desk_conflict": "和已认领会话共用桌面会话 ID 的进程",
                "tombstone": "已释放认领的会话", "permit": "还没转正的会话", "pending": "还没转正的会话",
                "pool_unreadable": "认领表读不出时的会话"}


def sender_label(me, origin):
    """Who a message or request is from: the claim's name, or what kind of caller ran the script.
    A process with neither a session ID nor a desktop ID is a plain terminal, not a session."""
    if me:
        return me["name"]
    if origin == "researcher" and not os.environ.get(SESSION_ENV) and not os.environ.get(DESK_ENV):
        return "终端里的进程（没有会话 ID）"
    return ORIGIN_LABEL.get(origin, "未认领的会话")


# ------------------------------------------------------------------ pool
def load_policy(root):
    return load_json(os.path.join(root, ".claude", "hooks", "policy.json"), None)


def caller(root, policy):
    """(claim, origin) of the session running this script, from its Bash environment. A desktop ID
    whose claim belongs to another running session counts as a desk conflict, not as that claim."""
    sid = os.environ.get(SESSION_ENV) or ""
    desk = os.environ.get(DESK_ENV) or ""
    pl, ok = load(root)
    if not ok:
        return None, "pool_unreadable"
    claim = claim_by_session(pl, sid)
    if claim:
        return claim, "claim"
    other = claim_by_desk(pl, desk)
    if other:
        return (other, "claim") if cleared(root, other) else (None, "desk_conflict")
    if permit_for(pl, sid, desk):
        return None, "permit"
    if pending_for(pl, sid):
        return None, "pending"
    if tombstoned(pl, sid, desk):
        return None, "tombstone"
    return None, "researcher"


def need_pool(root):
    policy = load_policy(root)
    if not policy:
        raise SystemExit("读不到 .claude/hooks/policy.json：这个目录还没初始化")
    return policy


def open_todos(pl):
    return [t for t in pl.get("todos", []) if isinstance(t.get("n"), int)]


ITEM_KINDS = {"terminate": "终止", "delete": "删除"}          # what the researcher is asked to do with a work item

def main(argv=None):
    """`pool.py <command>`: the commands are in pool_cmd.py. The hooks load this file on every tool call and
    must not pay for what the commands import."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))      # python -I leaves the script's directory out
    import pool_cmd
    return pool_cmd.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

