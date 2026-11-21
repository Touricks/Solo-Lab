#!/usr/bin/env python3
"""What ledger.py, docs.py and workitem.py share: the binding to the space, reading and writing Feishu,
package drafts, receipts and the write lock. Not run by itself.

Decisions from the decision queue are mirrored in a local decision state, .claude/state/gates.json
(shape and timing above save_gates): the decisions still waiting, with what each freezes and how it may
end. Only the scribe's --apply writes it; the inbox hook reads it (the guard hook does not, and nothing
is released to any hook: 1.0.6).

Document bodies go to lark-cli on stdin (`--content -`): lark-cli only reads @files
under the working directory, /tmp or ~/files. The shapes parsed here are the ones
recorded from a real space (tests/fixtures/feishu/). --offline prints every command
without calling Feishu (used by tests). Every --apply holds an exclusive lock on
feishu/.ledger.lock (waiting up to 60 seconds), so two writers never interleave; ids.json
and receipts are replaced atomically.

Every work item type has node flows. Which flows there are, how the nodes follow each
other, who is responsible for a step and what it requires is read from the space, never
kept here. What the scripts must find by meaning (the main-line type, the main document
field, the role that signs ...) is the contract, orgspec/contract.json; feishu/ids.json
binds each slot to this project's space and feishu/space.json is the space's structure as
`space.py snapshot` read it.

A field is named by its slot (main_doc), its name in the space (成功标准) or its field_key.
Values go to meegle as strings: dates as UTC noon in milliseconds, numbers as digits.
Standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import pool  # noqa: E402
import project_paths  # noqa: E402
import space  # noqa: E402

# name: (type slot, state slots to keep or None, state slots to leave out, extra condition or None, description)
QUERIES = {
    "queue": ("decision", ["pending", "decided"], [], None, "决策队列：待决定的决策，和研究员定了、执行方是研究员、还没归档的决策"),
    "verifying": ("same_seq", ["verifying"], [], None, "待验收的同序列需求"),
    "open": ("same_seq", None, ["accepted", "archived"], None, "进行中的同序列需求"),
    "cross": ("cross_request", None, ["accepted", "archived"], None, "进行中的跨序列需求"),
    "tasks": ("research_task", None, ["archived"], None, "没归档的研究任务"),
    "recent": ("same_seq", None, [], "RELATIVE_DATETIME_BETWEEN(`updated_at`, 'past', '7d')", "近 7 天更新过的同序列需求"),
}
QUERY_NAMES = ["queue", "blocked", "verifying", "open", "cross", "tasks", "recent"]
READ_COMMANDS = ("get", "next", "query", "mql", "trace", "status")
TRUE_WORDS = {"是", "true", "True", "1", "yes", "要"}
FALSE_WORDS = {"否", "false", "False", "0", "no", "不要"}
RESEARCHER = "研究员"
FIRST_TASK = "packages/drafts/pmo/研究任务单-首条.md"    # the sheet init writes; the scribe registers it while the pool opens
SESSION_NAME = re.compile(r"^[A-Za-z]+-p[0-2]-\w+$")


class LedgerError(Exception):
    pass


class WriteMaybeDone(LedgerError):
    """A write whose outcome is unknown (a timeout, an answer without the expected ID): it may be in Feishu."""


def project_root():
    return project_paths.project_root()


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    """Write JSON through a temporary file and os.replace, so a reader never sees half a file."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


LOCK_WAIT = 60


@contextlib.contextmanager
def write_lock(root, wait=None):
    """Hold feishu/.ledger.lock for one --apply run. The file names the holder (session and pid)."""
    wait = LOCK_WAIT if wait is None else wait
    path = os.path.join(root, "feishu", ".ledger.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    f.seek(0)
                    holder = f.read().strip() or "未知"
                    raise LedgerError("台账正在被别的写入占用（%s），等了 %d 秒还没放开；等它写完再试，不要删锁文件" % (holder, wait))
                time.sleep(0.5)
        try:
            f.seek(0)
            f.truncate()
            f.write("会话 %s，pid %d，%s 开始" % (os.environ.get("CLAUDE_CODE_SESSION_ID") or "终端", os.getpid(), now()))
            f.flush()
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def other_space(task, meegle):
    """The space spec/task.json names when it is not the bound one; None when they agree or
    the binding does not record a space name to compare with."""
    fs = task.get("feishu") if isinstance(task, dict) else None
    wanted = str(fs.get("meegle_space") or "").strip() if isinstance(fs, dict) else ""
    known = [meegle.get(k) for k in ("project_key", "simple_name", "space_name") if meegle.get(k)]
    return wanted if wanted and meegle.get("space_name") and wanted not in known else None


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def utc_noon_ms(text):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text or "")
    if not m:
        return None
    try:
        d = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return str(int(d.timestamp() * 1000))


def item_id_of(value):
    """A work item id given as 2003 or #2003; LedgerError for anything else."""
    text = str(value or "").strip().lstrip("#")
    if not text.isdigit():
        raise LedgerError("工作项 ID 要写成数字，比如 2003 或 #2003：%r" % value)
    return text


def system_states(spec):
    return list(spec.rules["feishu"].get("system_states", []))


def closed_states(lg):
    """Names of the states in which new information no longer resumes a work item: the
    contract's closed states as this space calls them, and the system state 已终止."""
    out = set(system_states(lg.spec))
    for wid, states in lg.states.items():
        out.update(s["name"] for slot, s in states.items() if slot in lg.contract.flow.get("closed", []) and s.get("name"))
    return out


def plain(value):
    """A field value read back by `workitem get`, as plain text (None when empty).

    select {label, value} -> label; date {iso_time, timestamp} -> iso_time;
    related work item {id, name} -> id; roles [{label, value}] -> labels; text, link,
    number and bool come back as strings already.
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        parts = [plain(v) for v in value]
        return "、".join(p for p in parts if p) or None
    if isinstance(value, dict):
        if "label" in value:
            return value.get("label")
        if "iso_time" in value:
            return value.get("iso_time")
        if "id" in value:
            return str(value["id"])
        if "name" in value:
            return value.get("name")
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def moql_value(v):
    """One cell of `workitem query` output: data.<group>[].moql_field_list[].value.

    A person comes back as user_value {name_cn, name_en, user_key, email}; only the
    name is kept, so a query never prints e-mail addresses.
    """
    if not isinstance(v, dict):
        return v
    for k in ("long_value", "string_value", "double_value", "bool_value"):
        if k in v:
            return v[k]
    if "user_value" in v:
        users = v["user_value"] if isinstance(v["user_value"], list) else [v["user_value"]]
        names = [u.get("name_cn") or u.get("name_en") or u.get("user_key") for u in users if isinstance(u, dict)]
        return "、".join(n for n in names if n) or None
    labels = v.get("key_label_value_list")
    if labels:
        return "、".join(str(x.get("label") or x.get("key")) for x in labels)
    one = v.get("key_label_value")
    if isinstance(one, dict):
        return one.get("label") or one.get("key")
    return v.get("value", v)


# ------------------------------------------------------------- context
class Ledger:
    def __init__(self, root, mode):
        self.root, self.mode = root, mode          # mode: apply | preview | offline
        self.spec = orgspec.OrgSpec()
        self.contract = orgspec.Contract(self.spec)
        self.ids = load_json(os.path.join(root, "feishu", "ids.json"), {}) or {}
        self.snap = load_json(os.path.join(root, "feishu", "space.json"), {}) or {}
        m = self.ids.get("meegle") or {}
        self.key = m.get("project_key") or "<project_key>"
        self.simple_name = m.get("simple_name")
        self.types = m.get("types") or {}
        self.field_keys = m.get("field_keys") or {}
        self.roles = m.get("roles") or {}
        self.states = m.get("states") or {}
        wiki = self.ids.get("wiki") or {}
        self.nodes = wiki.get("nodes") or {}
        self.tenant = wiki.get("tenant_domain")
        self.caller, self.origin = who(root)        # the session that runs the script; None for the researcher's own
        self.tag = (self.caller or {}).get("tag") or os.environ.get("SOLO_LAB_TAG", "[PMO+0]")
        self.planned = []
        self._me = None

    # names and keys
    def bound_type(self, wid):
        t = self.types.get(wid) or {}
        if not t.get("type_key"):
            raise LedgerError("%s还没绑定到空间里的类型：先跑 space.py feishu --live，核对后加 --apply" % self.spec.term("wi", wid))
        return t

    def api(self, wid):
        """The type's system identifier in this space (work item links use it)."""
        t = self.bound_type(wid)
        return t.get("api_name") or t["type_key"]

    def type_ref(self, wid):
        """What MQL accepts as a table name: the type_key bound for this space."""
        return self.bound_type(wid)["type_key"]

    def wid_of(self, key):
        """Type slot from the type_key, api_name or name bound for this space, or from the slot
        itself (what --type takes)."""
        if not key:
            return None
        for wid, t in self.types.items():
            if key in (t.get("type_key"), t.get("api_name"), t.get("name")):
                return wid
        return key if key in self.contract.types() else None

    def space_type(self, wid):
        """What the snapshot holds for a bound type; {} when there is no snapshot of it."""
        return space.type_of(self.snap, (self.types.get(wid) or {}).get("type_key"))

    def field(self, wid, ref):
        """A field of a type, named by its slot, its name in the space, its alias or its
        field_key: {field_key, name, field_type, options, slot}."""
        bound = self.field_keys.get(wid) or {}
        key = bound.get(ref)
        found = space.field_of(self.snap, (self.types.get(wid) or {}).get("type_key"), key or ref)
        if found is None and key:                  # bound, but not in the snapshot: go by the contract
            found = {"field_key": key, "name": self.spec.field_name(ref), "field_type": self.contract.field(ref)["type"]}
        if found is None:
            if ref in self.contract.data["fields"]:
                raise LedgerError("%s的字段 %s（%s）还没绑定：报给模板维护者补上，名字不同的用 --map 指认，再跑 space.py feishu --live --apply" % (
                    self.spec.term("wi", wid), ref, self.spec.field_name(ref)))
            raise LedgerError("%s上没有字段“%s”：写字段名、约定里的标识或 field_key；这个类型有哪些字段见 feishu/space.md" % (
                self.spec.term("wi", wid), ref))
        slot = ref if key else next((s for s, k in bound.items() if k == found["field_key"]), None)
        return dict(found, slot=slot)

    def key_of(self, wid, ref):
        try:
            return self.field(wid, ref)["field_key"]
        except (LedgerError, orgspec.OrgSpecError, KeyError):
            return None

    def label(self, wid, key):
        """What a field_key is called in the space."""
        f = space.field_of(self.snap, (self.types.get(wid) or {}).get("type_key"), key)
        return (f or {}).get("name") or key

    def state(self, wid, slot):
        """The name of a state slot in this space."""
        s = (self.states.get(wid) or {}).get(slot)
        if not s or not s.get("name"):
            raise LedgerError("%s的状态“%s”还没绑定：先跑 space.py feishu --live --apply" % (
                self.spec.term("wi", wid), self.spec.term("state", slot)))
        return s["name"]

    def me(self):
        """user_key of the account every agent runs as (the researcher's)."""
        if self._me is None:
            if self.mode == "offline":
                self._me = "<研究员的 user_key>"
            else:
                data = self.run(["meegle", "user", "me", "--format", "json"], False)
                key = _find(data, ("user_key", "userkey", "userKey")) if data else None
                if not key:
                    raise LedgerError("meegle user me 的输出里找不到 user_key：%s" % json.dumps(data, ensure_ascii=False)[:300])
                self._me = str(key)
        return self._me

    def value(self, wid, ref, raw):
        """Convert a human value to the STRING protocol meegle expects. An empty value clears the field."""
        f = self.field(wid, ref)
        kind, name = f.get("field_type"), f.get("name") or ref
        raw = str(raw).strip()
        if raw == "":
            return ""
        if kind == "date":
            ms = utc_noon_ms(raw)
            if not ms:
                raise LedgerError("%s 要写成存在的日期 YYYY-MM-DD：%s" % (name, raw))
            return ms
        if kind == "number":
            if not re.fullmatch(r"-?\d+(\.\d+)?", raw):
                raise LedgerError("%s 要写数字：%s" % (name, raw))
            return raw
        if kind in ("bool", "switch"):
            if raw in TRUE_WORDS:
                return "true"
            if raw in FALSE_WORDS:
                return "false"
            raise LedgerError("%s 要写“是”或“否”：%s" % (name, raw))
        if kind == "user":
            # every agent runs as the researcher's account, and only the researcher decides
            if raw in (RESEARCHER, "我", "me") or (self.mode != "offline" and raw == self.me()):
                return self.me()
            if self.mode == "offline":
                return raw
            raise LedgerError("%s 只能填“研究员”（或研究员的 user_key）：%s" % (name, raw))
        if kind in ("select", "radio", "multi_select", "multi-select", "checkbox", "_work_item_template"):
            options = f.get("options") or []
            hit = next((o.get("id") for o in options if raw in (o.get("name"), o.get("id"))), None)
            if hit is None:
                if self.mode == "offline" and not options:
                    return "<%s 的选项 ID>" % raw
                raise LedgerError("%s 没有选项“%s”；空间里的选项：%s" % (
                    name, raw, "、".join(o.get("name", "") for o in options) or "无（先跑 space.py snapshot）"))
            return json.dumps([{"option_id": hit}]) if kind in ("multi_select", "multi-select", "checkbox") else hit
        return raw

    def entry(self, wid, ref, raw):
        return {"field_key": self.field(wid, ref)["field_key"], "field_value": self.value(wid, ref, raw)}

    def role_owners(self, wid):
        """role_owners entry for workitem create: every role of the type is the researcher's
        account, so every node has someone who may finish it. None when no role is known."""
        roles = [r["role_id"] for r in self.space_type(wid).get("roles") or []] or sorted(set((self.roles.get(wid) or {}).values()))
        if not roles:
            print("提醒：读不到%s有哪些角色，工作项的角色人员会空着" % self.spec.term("wi", wid))
            return None
        return {"field_key": "role_owners", "field_value": json.dumps([{"role": r, "owners": [self.me()]} for r in roles])}

    def run(self, argv, write, stdin=None):
        shown = " ".join(_quote(a) for a in argv)
        if stdin is not None:
            shown += "  # 正文从标准输入传入，%d 字" % len(stdin)
        if write and self.mode != "apply" or self.mode == "offline":
            self.planned.append(shown)
            print(("将执行：" if write else "将读取：") + shown)
            return None
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120, input=stdin)
        except subprocess.TimeoutExpired:
            if write:
                self.log(argv, "timeout", "")
                raise WriteMaybeDone("写命令超时（120 秒）：%s\n这一步可能已经写进飞书。先在网页上核对，再决定要不要重跑。" % shown)
            raise LedgerError("命令超时（120 秒）：%s" % shown)
        except OSError as e:
            raise LedgerError("运行不了 %s（%s）；先装好并登录，见 check.py precheck" % (argv[0], e))
        out = p.stdout.strip()
        if write:
            self.log(argv, p.returncode, out[-2000:])
        if p.returncode != 0:
            raise LedgerError("命令失败（退出码 %s）：%s\n%s" % (p.returncode, shown, (p.stderr or out)[-800:]))
        try:
            return json.loads(out) if out else {}
        except ValueError:
            return {"_text": out}

    def meegle(self, *args, write=False):
        return self.run(["meegle", *args, "--project-key", self.key, "--format", "json"], write)

    def lark(self, *args, write=False, stdin=None):
        return self.run(["lark-cli", *args, "--as", "user"], write, stdin)

    def log(self, argv, code, out):
        path = os.path.join(self.root, "feishu", "write-log.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ids = sorted(set(re.findall(r'"(?:work_item_id|node_token|obj_token|document_id|url)"\s*:\s*"?([^",}\s]+)', out)))
        rec = {"ts": now(), "tool": "ledger.py", "session": self.tag,
               "argv": [a if len(a) < 300 else a[:300] + "…" for a in argv], "exit": code, "ids": ids[:10]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # reads
    def get(self, item_id, field_keys=None):
        args = ["workitem", "get", "--work-item-id", str(item_id)]
        if field_keys:
            args += ["--fields", json.dumps(field_keys)]
        return self.meegle(*args)

    def item(self, item_id):
        """{type, type_key, state, flow, flow_id, name, fields{field_key: raw value}}; None offline.

        Shape of `workitem get --fields '["_all"]'`: work_item_attribute holds the
        type {key: type_key, name}, the state {key, name}, the flow {id, name} and
        work_item_name; work_item_fields lists {key, name, value}. Fields without a
        value are left out. One page holds 100 fields; the types here have under 60.
        """
        data = self.get(item_id, ["_all"])
        if data is None:
            return None
        attr = data.get("work_item_attribute")
        if not isinstance(attr, dict):
            raise LedgerError("读不出工作项 %s：输出里没有 work_item_attribute：%s" % (
                item_id, json.dumps(data, ensure_ascii=False)[:300]))
        tkey = (attr.get("work_item_type") or {}).get("key")
        fields = {}
        for f in data.get("work_item_fields") or []:
            if isinstance(f, dict) and f.get("key"):
                fields[f["key"]] = f.get("value")
        flow = attr.get("template") or {}
        return {"type": self.wid_of(tkey), "type_key": tkey,
                "state": (attr.get("work_item_status") or {}).get("name"),
                "flow": flow.get("name"), "flow_id": str(flow["id"]) if flow.get("id") is not None else None,
                "name": attr.get("work_item_name"), "fields": fields}

    def flow_nodes(self, item_id):
        """The item's flow as it stands, in order: each node's key, name, status (doing,
        finished, not_started), when it last began and finished (ms), roles and form items with
        their values. None offline. One page of `workflow get-node` holds 20 nodes."""
        raw, page = [], 1
        while True:
            data = self.meegle("workflow", "get-node", "--work-item-id", str(item_id), "--page-num", str(page))
            if data is None:
                return None
            batch = data.get("list") or []
            raw += batch
            if not batch or not (data.get("pagination") or {}).get("has_more"):
                break
            page += 1
        nodes = []
        for n in raw:
            rec = space.node_record(n)
            rec["status"] = (n.get("basic") or {}).get("status")
            rec["started_at"] = (n.get("schedule") or {}).get("actual_begin_time")
            rec["finished_at"] = (n.get("schedule") or {}).get("actual_finish_time")
            values = {f.get("field_key"): f.get("value") for f in n.get("form_items") or [] if isinstance(f, dict)}
            for f in rec["required"] + rec["optional"]:
                f["value"] = values.get(f["field_key"])
            nodes.append(rec)
        if not nodes:
            raise LedgerError("读不到 #%s 的节点：它的类型不是节点流程，或者工作项不在这个空间" % item_id)
        return space.ordered(nodes)

    def unfinished(self, item_id, node_key):
        """What the space says a node still lacks before it can be confirmed: [{class, key, ...}]."""
        data = self.meegle("workflow", "list-state-required", "--work-item-id", str(item_id),
                           "--state-key", node_key, "--mode", "unfinished")
        if data is None:
            return None
        return [f for f in data.get("form_items") or [] if isinstance(f, dict) and not f.get("finished")]

    def op_records(self, item_id, module, start=None):
        """The item's operation records of one module (node_mod, field_mod ...), oldest first,
        from `start` (ms) on. A page holds 20; --start only works together with --end."""
        args = ["workitem", "list-op-records", "--work-item-id", str(item_id), "--op-record-module", module]
        if start is not None:
            args += ["--start", str(int(start)), "--end", str(int(time.time() * 1000) + 60000)]
        out, token = [], None
        for _ in range(60):
            data = self.meegle(*(args + (["--start-from", token] if token else [])))
            if data is None:
                return None
            out += data.get("op_records") or []
            token = data.get("start_from")
            if not data.get("has_more") or not token:
                return out
        raise LedgerError("#%s 的操作记录翻了 60 页还没读完" % item_id)

    def people(self, it, item_id, keys=None):
        """Fill in the person fields `workitem get` leaves empty (决定人 reads back as null).

        MQL has them as user_value; one query reads the empty ones and puts the names
        into it["fields"]. keys: the field_keys to read, by default every person field
        of the item's type. Only a non-empty name counts as filled in: an empty cell
        ({}, [], null) stays empty, and so does a shape moql_value cannot read, with a
        reminder that leaves the value out (it may hold e-mail addresses). When the
        query fails they stay empty, with a reminder.
        """
        wid = it.get("type") if it else None
        if not wid:
            return
        if keys is None:
            keys = [f["field_key"] for f in self.space_type(wid).get("fields") or []
                    if f.get("field_type") == "user" and str(f.get("field_key")).startswith("field_")]
        keys = [k for k in dict.fromkeys(keys) if k and plain(it["fields"].get(k)) is None]
        num = str(item_id).strip().lstrip("#")
        if not keys or not num.isdigit():
            return
        try:
            mql = "SELECT `work_item_id`, %s FROM `%s`.`%s` WHERE `work_item_id` = %s" % (
                ", ".join("`%s`" % k for k in keys), self.key, self.type_ref(wid), num)
            data = self.run(["meegle", "workitem", "query", "--project-key", self.key, "--mql", mql, "--format", "json"], False)
            rows = query_rows(data) if data is not None else []
        except LedgerError as e:
            print("提醒：人员字段读不出来，按空的处理：%s" % e)
            return
        row = rows[0] if rows else {}
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                it["fields"][k] = v
            elif v not in (None, "", {}, []):
                print("提醒：人员字段 %s 的 MQL 输出读不懂，按空的处理" % k)

    def text_of(self, it, ref):
        """Plain text of a field on a work item read with item(); None if empty or absent.

        When the item's type cannot be read, shared fields (main_doc, sequence,
        sender ...) are still found through any type that has them.
        """
        if not it:
            return None
        wids = [it["type"]] if it.get("type") else list(self.contract.types())
        for wid in wids:
            key = self.key_of(wid, ref)
            if key and key in it["fields"]:
                return plain(it["fields"].get(key))
        return None

    def doc_url(self, item_id, it=None):
        it = it if it is not None else self.item(item_id)
        if it is None:
            return "<#%s 的主文档链接>" % item_id
        url = self.text_of(it, "main_doc")
        if not url:
            raise LedgerError("工作项 %s 的“主文档”是空的" % item_id)
        return url

    def node_of(self, url):
        """Wiki node token of a document link: /wiki/<node> directly, /docx/<id> via wiki +node-get."""
        if self.mode == "offline" and (url or "").startswith("<"):
            return "<%s对应的节点>" % url.strip("<>")
        m = re.search(r"/wiki/([A-Za-z0-9]+)", url or "")
        if m:
            return m.group(1)
        m = re.search(r"/docx/([A-Za-z0-9]+)", url or "")
        if not m:
            return None
        data = self.lark("wiki", "+node-get", "--node-token", m.group(1))
        if data is None:
            return "<%s 对应的节点>" % m.group(1)
        return _find(data, ("node_token",))

    def remember_tenant(self, link):
        """Keep the tenant domain of the first document link in ids.json (wiki.tenant_domain):
        a later --resume that only has a document_id needs it to build the /wiki/ link."""
        m = re.match(r"https?://([^/\s]+)/", link or "")
        if not m or self.tenant:
            return
        self.tenant = m.group(1)
        if self.mode == "apply":
            path = os.path.join(self.root, "feishu", "ids.json")
            ids = load_json(path, {})
            if isinstance(ids, dict) and not (ids.get("wiki") or {}).get("tenant_domain"):
                ids.setdefault("wiki", {})["tenant_domain"] = self.tenant
                save_json(path, ids)

    def wiki_url(self, node, near=None):
        m = re.match(r"https?://([^/]+)/", near or "")
        host = m.group(1) if m else self.tenant
        if not host:
            raise LedgerError("不知道租户域名：ids.json 的 wiki.tenant_domain 是空的")
        return "https://%s/wiki/%s" % (host, node)

    def item_url(self, wid, item_id):
        """Canonical work item link /{simple_name}/{api_name}/detail/{id}; None without simple_name."""
        if not self.simple_name:
            return None
        host = (self.ids.get("meegle") or {}).get("host") or "project.feishu.cn"
        return "https://%s/%s/%s/detail/%s" % (host, self.simple_name, self.api(wid), item_id)

    # writes on documents
    def create_doc(self, parent, title, body, created=None):
        """Create a document under a wiki node; return its /wiki/ link.

        docs +create answers with a /docx/ link and document_id only, so the node
        token is looked up with wiki +node-get before the link is stored anywhere.
        created(docx_link_or_id) is called as soon as the document exists, so a
        failed lookup can be resumed without creating a second document.
        """
        out = self.lark("docs", "+create", "--parent-token", parent, "--title", title, "--doc-format", "markdown",
                        "--content", "-", write=True, stdin=body)
        if out is None:
            return "<新文档的知识库链接>"
        docx = _find(out, ("url",))
        doc_id = _find(out, ("document_id", "obj_token"))
        self.remember_tenant(docx)
        if not (docx or doc_id):
            raise WriteMaybeDone("docs +create 的返回里没有文档链接和 document_id：%s\n文档可能已经建好了" % (
                json.dumps(out, ensure_ascii=False)[:300]))
        if created:
            created(docx or doc_id)
        return self.doc_link(docx, doc_id)

    def doc_link(self, docx=None, doc_id=None):
        """/wiki/ link of a document known by its /docx/ link or document_id."""
        node = self.node_of(docx) if docx else None
        if not node and doc_id:
            got = self.lark("wiki", "+node-get", "--node-token", doc_id)
            node = _find(got, ("node_token",)) if got else None
        if not node:
            raise LedgerError("文档建好了（%s），但还没查到它的知识库节点" % (docx or doc_id))
        return self.wiki_url(node, docx)


def _quote(a):
    return a if re.fullmatch(r"[\w@%+=:,./#-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def _walk(v):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def _find(data, keys):
    for d in _walk(data):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
    return None


def doc_content(data):
    """The XML string of a docs +fetch answer: data.document.content."""
    if not isinstance(data, dict):
        return ""
    doc = (data.get("data") or {}).get("document") or data.get("document") or {}
    return doc.get("content") or data.get("_text") or ""


# --------------------------------------------------------------- drafts
HEADER = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")
SHEETS = {"研究任务单": "research_task", "反馈": "feedback"}
SHEET_KEYS = {"research_task": ["流程", "发出方", "关联工作项"], "feedback": ["发出方", "关联工作项"]}
LINK_LINE = re.compile(r"^(- 关联工作项[：:][ \t]*)([^；;\n]*)(.*)$", re.M)
PLACEHOLDERS = {"", "待建", "待登记", "空", "无", "-", "—"}
NOTED_PLACEHOLDER = re.compile(r"(待建|待登记)\s*[（(〔\[].*[）)〕\]]")
DRAFT_CHANGED = ("草稿在命令运行期间改过：进主文档的是命令开始时的内容，之后的改动留在草稿里，没有进主文档；"
                 "要进主文档，改好后走修订（docs.py revise）。")


def is_placeholder(value):
    """Does a 关联工作项 value stand for "no work item yet"? 待建 with a note in brackets after it is one
    too: a template once carried such a note on this line, and drafts copied from it are still around."""
    value = value.strip()
    return value.strip("（）()〔〕[] ") in PLACEHOLDERS or bool(NOTED_PLACEHOLDER.fullmatch(value))


def header_of(text):
    """The `- 键：值` lines before the first section, as a dict (the first of a repeated key wins)."""
    header = {}
    for line in text.splitlines():
        m = HEADER.match(line)
        if m and m.group(1) not in header:
            header[m.group(1)] = m.group(2).strip()
        if line.startswith("## "):
            break
    return header


def sequence_named(spec, who):
    """The sequence a 发出方 or 接收方 line names: by the sequence's name in front (算法 +2（Alg-p2-01）)
    or, when only a session is written (Alg-p2-01), by its abbreviation. None for 研究员 and the like."""
    who = (who or "").strip()
    for s, _, abbrev in spec.sequences():
        if who.startswith(spec.term("seq", s)):
            return s
    if SESSION_NAME.match(who):
        head = who.split("-", 1)[0].lower()
        return next((s for s, _, abbrev in spec.sequences() if abbrev.lower() == head), None)
    return None


def title_of(text):
    return next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")


def link_line(text, ref, item_id, relink=False):
    """The draft with its work item in the 关联工作项 header line.

    A placeholder (待建, 待登记, empty ...) becomes ref; whatever follows the first ；
    (；来源工作项 #2100) is kept. A line that already names this item is left alone,
    unless relink is set: then the bare ID (#2140, as register and revise write it back
    into a draft) becomes ref too, so that a main document keeps the link to its item.
    Anything else is neither a placeholder nor this item: LedgerError, before a
    document carries a wrong ID.
    """
    head = text.split("\n## ", 1)[0]
    m = LINK_LINE.search(head)
    if not m:
        raise LedgerError("包头里没有“关联工作项”一行：登记新工作项的草稿写“- 关联工作项：待建”")
    value, num = m.group(2).strip(), re.escape(str(item_id))
    if is_placeholder(value) or relink and re.fullmatch(r"#?%s" % num, value):
        return text[:m.start()] + m.group(1) + ref + m.group(3) + text[m.end():]
    if re.fullmatch(r"#?%s|\[#?%s\]\(\S+\)" % (num, num), value):
        return text
    raise LedgerError("包头的“关联工作项”写的是“%s”：登记新工作项的草稿这一行写“待建”，登记时换成新建的工作项 ID" % value)


def write_back(path, read_text, item_id):
    """The draft file gets its work item in the 关联工作项 line, and nothing else in it changes.

    The file is read again and only that line is replaced: what its author wrote into it while the command
    ran (a register or a revise takes ten seconds and more) stays. Writing back what the command read at its
    start put an author's edits out once. Returns DRAFT_CHANGED when the file is not what the command read,
    for the output and the receipt: the main document holds the earlier text. A file whose line can no longer
    take the ID is left alone and the note says so; the receipt keeps the draft from a second registration.
    The write goes through a temporary file, so that no reader meets half a draft.
    """
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = read_text
    changed = current != read_text
    try:
        fixed = link_line(current, "#%s" % item_id, item_id)
    except LedgerError:
        if not changed:
            raise
        return "草稿在命令运行期间改过，包头里找不到可以写回的“关联工作项”一行，草稿没有动：它的工作项是 #%s，请作者自己写上。" % item_id
    if fixed != current or not os.path.exists(path):
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(fixed)
        os.replace(tmp, path)
    return DRAFT_CHANGED if changed else ""


class Draft:
    """A package draft: 需求包, 交付包 or 决策包."""

    def __init__(self, spec, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        errors, _ = orgspec.lint_text(spec, self.text)
        if errors:
            raise LedgerError("草稿没通过检查，先改好：\n" + "\n".join(errors))
        self.header = header_of(self.text)
        if "包类型" not in self.header:
            raise LedgerError("草稿的包头没有“包类型”：%s" % path)
        names = {v: k for k, v in spec.terms["pkg"].items()}
        self.pid = names[self.header["包类型"].split("（")[0].strip()]
        rels = {v: k for k, v in spec.terms["rel"].items()}
        self.rel = rels.get(self.header.get("关系", "").strip())
        title = title_of(self.text)
        self.task = re.split(r"[：:]", title, 1)[-1].strip() if title else os.path.basename(path)
        m = re.search(r"（([A-Za-z]+-p[0-2]-[\w]+)）", self.header.get("发出方", ""))
        self.author = m.group(1) if m else None
        m = re.search(r"（([A-Za-z]+-p[0-2]-[\w]+)）", self.header.get("接收方", ""))
        self.receiver = m.group(1) if m else None
        seq_by_cn = {spec.term("seq", s): s for s, _, _ in spec.sequences()}
        self.sender_seq = sequence_named(spec, self.header.get("发出方", ""))
        self.receiver_seq = sequence_named(spec, self.header.get("接收方", ""))
        link = self.header.get("关联工作项", "")
        self.linked = re.findall(r"#(\d+)", link)
        src = re.search(r"来源工作项[：:\s]*#?(\d+)", self.text)
        self.source = src.group(1) if src else None
        ref = re.search(r"#?(\d{3,})", self.header.get("所属研究任务", ""))
        self.task_ref = ref.group(1) if ref else None
        self.flow = None
        self.version = orgspec.version_of(self.header.get(orgspec.VERSION))
        self.terms = orgspec.decision_parts(spec, self.text, self.header)[0] if self.pid == "decision" else None

    def section(self, name):
        m = re.search(r"\n## %s\n(.*?)(?:\n## |\Z)" % re.escape(name), self.text, re.S)
        return m.group(1).strip() if m else ""

    def section_choice(self, name):
        """The first line of a section, without trailing punctuation or explanation, to match an option."""
        first = (self.section(name).splitlines() or [""])[0].strip()
        return re.split(r"[：:。；;，,（(]", first, 1)[0].strip() or None

    def body_without_title(self):
        return "\n".join(l for l in self.text.splitlines() if not l.startswith("# ")).strip() + "\n"


def sheet_kind(path):
    """research_task or feedback when the file is a 研究任务单 or a 反馈; None for a package draft."""
    with open(path, encoding="utf-8") as f:
        title = title_of(f.read())
    return next((kind for word, kind in SHEETS.items() if re.match(r"%s\s*[：:]" % word, title)), None)


class Sheet(Draft):
    """A 研究任务单 or a 反馈: `# 研究任务单：<任务名>`, header lines, then sections. Not a
    package: nobody receives it; it becomes the main document of a work item of its own."""

    def __init__(self, spec, path, kind):
        self.path, self.kind = path, kind
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        self.header = header_of(self.text)
        word = next(w for w, k in SHEETS.items() if k == kind)
        missing = [k for k in SHEET_KEYS[kind] if k not in self.header]
        if missing:
            raise LedgerError("%s的开头缺这几行：%s" % (word, "、".join("“- %s：…”" % k for k in missing)))
        self.pid, self.rel, self.receiver, self.receiver_seq, self.linked = None, None, None, None, []
        self.version, self.terms = None, None
        self.task = re.split(r"[：:]", title_of(self.text), 1)[-1].strip()
        if not self.task:
            raise LedgerError("%s的标题要写成“# %s：<名字>”" % (word, word))
        sender = self.header.get("发出方", "").strip()
        if re.search(r"[<>]", self.task + sender + self.header.get("流程", "")):
            raise LedgerError("%s的标题或开头几行还是模板里的占位（<…>），先填好再登记" % word)
        m = re.search(r"（([A-Za-z]+-p[0-2]-[\w]+)）", sender)
        self.author = m.group(1) if m else (sender or None)        # a session, or 研究员, 导师 ...
        self.sender_seq = sequence_named(spec, sender)
        src = re.search(r"来源工作项[：:\s]*#?(\d+)", self.text)
        self.source = src.group(1) if src else None
        ref = re.search(r"#?(\d{3,})", self.header.get("所属研究任务", ""))
        self.task_ref = ref.group(1) if ref else None
        self.flow = self.header.get("流程", "").strip() or None


# ------------------------------------------------------------- receipts
def sent_dir(lg):
    return os.path.join(lg.root, "packages", "sent")


def find_receipt(lg, path):
    rel = os.path.relpath(path, lg.root)
    d = sent_dir(lg)
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        if name.endswith(".json") and not name.startswith(("reply-", "upload-", "attach-")):
            rec = load_json(os.path.join(d, name))
            if isinstance(rec, dict) and rec.get("draft") == rel:
                rec["_file"] = "packages/sent/%s" % name
                return rec
    return None


def receipt_file(lg, stems, path):
    """packages/sent/<stem>.json for the first stem (then <stem>-2.json, -3 ...) that is free
    or already belongs to this draft: a receipt is never taken over by another draft."""
    rel = os.path.relpath(path, lg.root)
    for stem in stems:
        for k in range(1, 100):
            full = os.path.join(sent_dir(lg), "%s.json" % stem if k == 1 else "%s-%d.json" % (stem, k))
            rec = load_json(full, {}) if os.path.exists(full) else {}
            if isinstance(rec, dict) and (not rec or rec.get("draft") == rel):
                return full
            if stem == stems[0] and len(stems) > 1:
                break
    raise LedgerError("packages/sent/ 里同名的回执太多了：%s" % stems[-1])


def safe_name(path):
    return re.sub(r"[^\w.-]", "_", os.path.basename(path))


def receipt_path(lg, path, item_id):
    """packages/sent/<ID>.json, or <ID>-<draft file>.json when another draft already holds <ID>.json
    (the same draft file name registered again)."""
    return receipt_file(lg, [str(item_id), "%s-%s" % (item_id, safe_name(path))], path)


URGENT = "插队"              # the 紧急程度 of a decision that jumps the queue


def researcher_action(d):
    """A decision package's 研究员要做的事: what the researcher must do by hand once it is decided, as
    the header wrote it, also when it opens with a bracket (（本周内）…). None when it says 无 (无。 or 无，…
    too: orgspec.SAYS_NOTHING); the package check sends other ways of writing nothing back."""
    value = (d.header.get(orgspec.RESEARCHER_ACTION) or "").strip()
    return None if not value or orgspec.SAYS_NOTHING.match(value) else value


def urgency_of(d, pairs=None):
    """The 紧急程度 a decision was registered with: the header's, or the --set value when the header
    still reads like the template (插队 / 今天 / ...); None when neither says."""
    value = (d.header.get("紧急程度") or "").strip()
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        if key.strip() in ("紧急程度", "urgency") and raw.strip():
            value = value if value and "/" not in value else raw.strip()
    return value if value and "/" not in value else None


def receipt_kind(spec, wid, d, pairs=None):
    """What the receipt says the registration was, for the inbox hook of the session the
    【登记回执】 reaches: the work item type, whether a decision waits in the decision queue and
    whether it jumps it. A decision's 回复期限 has no field in the space, and 执行方 and 研究员要做的事
    have one only in a space made from template 2.2.0 on (header_fields); all three are kept here as
    the package header wrote them, for `query queue` and `confirm`. executor is
    研究员 or the session's name; researcher_action is null when the header says 无, which the package
    check allows only when a session carries the decision out. A receipt without the keys is older than
    those header lines: carried_out_by. From 1.0.4 a package's receipt keeps its version, and a decision's
    also what decision_parts read (reversible, criteria, default, default_option, options, frozen) with its
    title, urgency and sender: `confirm --option` checks the option against them, and a decision rolled
    back to 待决定 is put back into the local decision state from them. Confirming 决定 adds how it was
    decided (settle); a rollback to 待决定 adds rolled_back_at (reopen)."""
    version = getattr(d, "version", None)
    if wid != "decision":
        return dict({"type": wid, "queue": False, "urgent": False}, **({"version": version} if version else {}))
    urgency = urgency_of(d, pairs) or ""
    terms = getattr(d, "terms", None) or {}
    return {"type": wid, "queue": (d.header.get("渠道") or "").strip() == spec.term("channel", "queue"),
            "urgent": re.split(r"[\s（(，,。；;]", urgency, 1)[0] == URGENT,
            "due": (d.header.get("回复期限") or "").strip() or None,
            "executor": orgspec.executor_of(spec, d.header.get(orgspec.EXECUTOR)), "researcher_action": researcher_action(d),
            "version": version, "title": d.task, "urgency": urgency or None, "sender": d.author,
            "reversible": terms.get("reversible"), "criteria": terms.get("criteria") or [], "default": terms.get("default"),
            "default_option": terms.get("default_option"), "options": terms.get("options"), "frozen": terms.get("frozen") or []}


# A decision package's header lines that go into fields of the same name where the space has them: template
# 2.2.0 added the two fields, and its view 待定决策 lists the researcher's decisions by 执行方 = 研究员. They are no
# contract slots, so they are found by name, like every field outside the contract; a space made from template
# 2.1.0 or earlier has neither, and the registration goes on without them.
HEADER_FIELDS = (orgspec.EXECUTOR, orgspec.RESEARCHER_ACTION)


def header_fields(lg, wid, d, given=()):
    """[{field_key, field_value}] for a decision package's 执行方 (研究员 or the session's name, as the receipt
    keeps it) and 研究员要做的事 (as the header wrote it), each into the space's field of that name
    (executor_fields)."""
    if wid != "decision":
        return []
    return executor_fields(lg, wid, orgspec.executor_of(lg.spec, d.header.get(orgspec.EXECUTOR)),
                           (d.header.get(orgspec.RESEARCHER_ACTION) or "").strip(), given)


def executor_fields(lg, wid, executor, action, given=()):
    """[{field_key, field_value}] for a decision's 执行方 and 研究员要做的事, each into the space's field of that
    name. A field the space lacks is left out without a word; one a --set value fills (given: field_keys) is
    left to it; an empty value writes nothing; a value the field does not take is skipped with a reminder, as
    register does with its other optional fields."""
    values = {orgspec.EXECUTOR: executor, orgspec.RESEARCHER_ACTION: action}
    out = []
    for name in HEADER_FIELDS:
        key = lg.key_of(wid, name)
        if not key or key in given or not values[name]:
            continue
        try:
            out.append(lg.entry(wid, name, values[name]))
        except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（之后可以用 update 补）：%s" % (name, e))
    return out


def carried_out_by(rec, sender=None):
    """Who carries out the decision a registration receipt is for: the executor it keeps (研究员 or a
    session's name). A receipt older than the 执行方 line goes by researcher_action: something for the
    researcher to do by hand makes it 研究员; otherwise the sender carries it out, as it did before
    (sender: the work item's 发出方, when known)."""
    if rec.get("executor"):
        return rec["executor"]
    return RESEARCHER if rec.get("researcher_action") else sender


def save_receipt(lg, path, item_id, status, kind=None, **extra):
    """packages/sent/<ID>.json: written right after the work item exists, updated as steps finish.

    Keeps what a --resume needs: the source, the --set pairs, and the document as
    soon as it exists (docx), then its /wiki/ link (doc). register also writes what the
    registration was (kind, from receipt_kind: type, queue, urgent, a decision's due, executor and
    researcher_action), nulls included; the inbox hook, `query queue` and `confirm` read it.
    """
    if lg.mode != "apply":
        return
    os.makedirs(sent_dir(lg), exist_ok=True)
    target = receipt_path(lg, path, item_id)
    rec = load_json(target, {}) or {}
    rel = os.path.relpath(path, lg.root)
    if not isinstance(rec, dict) or rec.get("draft") != rel:
        rec = {}
    rec.update({"draft": rel, "work_item_id": item_id, "status": status, "updated_at": now()})
    rec.update({k: v for k, v in extra.items() if v is not None})
    rec.update(kind or {})
    save_json(target, rec)


def receipt_file_of(lg, item_id):
    """(path, receipt) of a work item's registration receipt; (None, {}) when there is none."""
    num = str(item_id)
    folder = glob.escape(sent_dir(lg))
    for path in sorted(glob.glob(os.path.join(folder, num + ".json"))) + sorted(glob.glob(os.path.join(folder, num + "-*.json"))):
        rec = load_json(path, {})
        if isinstance(rec, dict) and str(rec.get("work_item_id")) == num:
            return path, rec
    return None, {}


def update_receipt(lg, item_id, **values):
    """Merge values into a work item's registration receipt (apply only); False when it has none."""
    if lg.mode != "apply":
        return False
    path, rec = receipt_file_of(lg, item_id)
    if not path:
        return False
    rec.update(values, updated_at=now())
    save_json(path, rec)
    return True


# ---------------------------------------------------- local decision state
# .claude/state/gates.json, next to the claims table and
# guarded like it: only ledger.py run by the scribe with --apply writes it (atomically, under the ledger's
# write lock); the inbox hook only reads it, the guard hook not at all. Shape (GATES_VERSION 2, from 1.0.6):
#   {"version": 2, "updated_at": ISO,
#    "pending": {"<ID>": {
#        "id", "title" (事项), "urgency" (紧急程度), "registered_at" (ISO, the registration; a revision keeps it,
#        and the expiry counts from it), "rolled_back_at" (ISO, only on a decision the researcher decided once
#        and that was rolled back to 待决定: it never takes 到期默认), "version" (int),
#        "reversible" (true/false/null), "criteria" [int], "default" (到期默认动作 as written),
#        "default_option" (the option it names, null unless reversible), "default_applies" (true when
#        confirm --by 到期默认 may take it once expired: reversible, a session carries it out, nothing by hand,
#        never rolled back), "executor" (研究员 or a session), "researcher_action" (null for 无),
#        "due" (回复期限, reference only), "sender",
#        "options" [{"name", "approach", "irreversible": [{"category" (id in orgspec/irreversible.json), "name",
#                    "qualifier" (the note after the category, "" when none), "text"}]}],
#        "frozen" [{"kind": "category", "category", "name", "qualifier", "text"} | {"kind": "text", "text"}]}}}
# pending: decisions from the decision queue while 待决定. Written when one is registered (from the moment
# its work item exists) and revised; removed when 决定 is confirmed (by the researcher or by 到期默认), when
# it is archived or found terminated, and when another write finds it no longer waiting in the space; put back,
# marked rolled_back_at, when a decided one is rolled back to 待决定.
# Nothing else is kept. Not the expiry time: pool.expiry_hours reads org/decisions.md whenever it is needed,
# here and in the inbox hook, so the two cannot disagree. And nothing is released: until 1.0.6 a "releases"
# list held what decided decisions let the guard hook pass. A file written by 1.0.4 or 1.0.5 is read as far
# as it still applies (load_gates) and loses the rest the next time it is written.
GATES_VERSION = 2
PRUNE_AFTER = datetime.timedelta(minutes=10)


def gates_path(root):
    return os.path.join(root, ".claude", "state", "gates.json")


def load_gates(root):
    """The local decision state: the decisions still waiting. An empty one when the file is missing or
    unreadable. What 1.0.4 and 1.0.5 kept beside them (releases, the expiry time) is left out; a waiting
    decision's reopened_at becomes rolled_back_at, its expires_at goes, and an option's releases are its
    irreversible."""
    data = load_json(gates_path(root), {})
    data = data if isinstance(data, dict) else {}
    pending = {str(k): v for k, v in (data.get("pending") or {}).items() if isinstance(v, dict)} \
        if isinstance(data.get("pending"), dict) else {}
    for entry in pending.values():
        entry.pop("expires_at", None)
        since = entry.pop("reopened_at", None)
        if since and not entry.get("rolled_back_at"):
            entry["rolled_back_at"] = since
        if entry.get("rolled_back_at"):
            entry["default_applies"] = False
        for option in entry.get("options") or []:
            if isinstance(option, dict) and "releases" in option:
                option.setdefault("irreversible", option.pop("releases"))
    return {"version": GATES_VERSION, "updated_at": data.get("updated_at"), "pending": pending}


def expiry_hours(root):
    """(hours, where from, reminder or None): how long a reversible decision waits before 到期默认 may take
    it. pool.expiry_hours, which the inbox hook uses too."""
    return pool.expiry_hours(root)


def now_at():
    return datetime.datetime.now().astimezone()


def parse_stamp(text):
    try:
        at = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.astimezone()


def pending_entry(item_id, kinds, registered_at, rolled_back_at=None):
    """The local decision state's record of a decision waiting for the researcher, from its receipt kinds.
    rolled_back_at: when the researcher had decided it and it was rolled back to 待决定; such a decision
    takes no default."""
    default_applies = (kinds.get("reversible") is True and bool(kinds.get("default_option")) and not rolled_back_at
                       and kinds.get("executor") not in (None, RESEARCHER) and not kinds.get("researcher_action"))
    entry = {"id": str(item_id), "title": kinds.get("title"), "urgency": kinds.get("urgency"), "registered_at": registered_at,
             "version": kinds.get("version"), "reversible": kinds.get("reversible"), "criteria": kinds.get("criteria") or [],
             "default": kinds.get("default"), "default_option": kinds.get("default_option"), "default_applies": default_applies,
             "executor": kinds.get("executor"), "researcher_action": kinds.get("researcher_action"), "due": kinds.get("due"),
             "sender": kinds.get("sender"),
             "options": [dict({k: v for k, v in o.items() if k != "releases"}, irreversible=orgspec.option_actions(o))
                         for o in kinds.get("options") or []],
             "frozen": kinds.get("frozen") or []}
    if rolled_back_at:
        entry["rolled_back_at"] = rolled_back_at
    return entry


def prune_pending(lg, gates, keep=None):
    """Drop pending decisions the space no longer has waiting (terminated, or decided on the web page).
    One query; entries younger than PRUNE_AFTER and the item being written are left alone, and nothing
    is dropped when the query fails."""
    old = now_at() - PRUNE_AFTER
    stale = [i for i, e in gates["pending"].items()
             if i != str(keep) and (parse_stamp(e.get("registered_at")) or now_at()) < old]
    if not stale or "decision" not in lg.types:
        return
    try:
        rows = query_all(lg, _select(lg, "decision", "`work_item_status` = '%s'" % lg.state("decision", "pending")))
    except (LedgerError, orgspec.OrgSpecError, KeyError) as e:
        print("提醒：没能核对本地决策状态里的待定决策还在不在待决定（%s），先都留着" % str(e)[:160])
        return
    waiting = {str(r.get("work_item_id")) for r in rows}
    for i in stale:
        if i not in waiting:
            del gates["pending"][i]
            print("本地决策状态：#%s 在空间里已经不在待决定（终止了，或者在网页上处理了），它的冻结解除。" % i)


def save_gates(lg, change, keep=None):
    """Apply change(gates) to the local decision state and write it: only with --apply, which only the
    scribe can run. While a decision waits that 到期默认 may take, an expiry time in org/decisions.md that
    cannot be read is said."""
    if lg.mode != "apply":
        return None
    gates = load_gates(lg.root)
    change(gates)
    prune_pending(lg, gates, keep)
    note = expiry_hours(lg.root)[2]
    if note and any(pool.takes_default(e) for e in gates["pending"].values()):
        print(note)
    gates["updated_at"] = now()
    path = gates_path(lg.root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, gates)
    return gates


def hold(lg, item_id, kinds, registered_at=None, rolled_back_at=None):
    """Put a decision from the decision queue into the local decision state (or refresh it), keeping the
    registration time it already has, and that it was rolled back to 待决定."""
    def change(gates):
        old = gates["pending"].get(str(item_id)) or {}
        gates["pending"][str(item_id)] = pending_entry(item_id, kinds, old.get("registered_at") or registered_at or now(),
                                                       old.get("rolled_back_at") or rolled_back_at)
    return save_gates(lg, change, keep=item_id)


def finished(lg, state, wid):
    """Is this the state of a work item no session answers for any more: archived, or terminated on the web page?"""
    return bool(state) and (state in system_states(lg.spec) or bool(wid) and state == (lg.states.get(wid, {}).get("archived") or {}).get("name"))


def work_item_type(draft):
    if draft.pid == "request":
        return "cross_request" if draft.rel == "cross" else "same_seq"
    if draft.pid == "decision":
        return "decision"
    raise LedgerError("%s是答复，不新建工作项；用 reply" % draft.header["包类型"])


# ------------------------------------------------------------ node flows
def typed_item(lg, item_id, wid_hint=None):
    it = lg.item(item_id)
    wid = (it and it.get("type")) or wid_hint
    if not wid:
        raise LedgerError("读不出 #%s 的类型；用 --type 指明" % item_id)
    return it, wid


# ------------------------------------------------- documents of new work items
# From 1.1.0 the document comes first and the work item second: the author runs `docs.py upload`, which creates
# the document and keeps a receipt (packages/sent/upload-<draft file>.json); the scribe's `workitem.py register`
# reads the draft's header and that receipt and creates the work item with the link in its 主文档 field; the
# author's `docs.py relink` writes the item's ID into the document. What the two scripts share is here.
STATUS_SECTION = "## 状态包\n\n### 简介\n\n%s### 当前状态\n\n### 近期变化\n\n### 风险\n"


def cognitive_layer(body):
    """What a new 状态包 starts from: the 认知层 of a package, the 目标 of a 研究任务单. '' when neither."""
    for heading in ("认知层", "目标"):
        m = re.search(r"^## %s[ \t]*\n(.*?)(?=^## |\Z)" % heading, body, re.M | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def with_status_sections(body):
    """Main documents end with 状态包 and then 交接记录; add whichever the draft lacks.
    A new 状态包 starts with the 认知层 as its 简介: the router matches against it."""
    has_status = re.search(r"^## 状态包\s*$", body, re.M)
    has_log = re.search(r"^## 交接记录\s*$", body, re.M)
    if has_status and has_log:
        return body
    intro = cognitive_layer(body)
    status = STATUS_SECTION % (intro + "\n\n" if intro else "")
    if has_log:
        return body[:has_log.start()] + status + "\n" + body[has_log.start():]
    if has_status:
        return body.rstrip("\n") + "\n\n## 交接记录\n"
    return body + "\n" + status + "\n## 交接记录\n"


def doc_from_link(lg, link, given=False):
    """/wiki/ link of a document known by a /wiki/ or /docx/ link or a document_id.
    given=True checks a link typed with --doc before using it."""
    link = (link or "").strip()
    if given and not re.match(r"https?://[^/\s]+/(?:wiki|docx)/[A-Za-z0-9]{6,}$", link):
        raise LedgerError("--doc 要给文档的 /wiki/ 或 /docx/ 链接，或者 new：%s" % link)
    if "/wiki/" in link:
        return link
    return lg.doc_link(link if link.startswith("http") else None, None if link.startswith("http") else link)


def main_document(lg, rec, doc, where, create, remember):
    """The /wiki/ link of the draft's document: given with --doc, kept in the receipt, or created now.

    create() makes the document and calls remember(docx=...) as soon as it exists;
    remember(**kw) updates the receipt. When docs +create timed out or answered
    without a link, the receipt keeps doc_unknown and the next run stops until --doc
    says what to do: --doc <链接> takes over the document found on the web, --doc new
    creates another one.
    """
    if doc and doc.strip().lower() != "new":
        url = doc_from_link(lg, doc, given=True)
        remember(doc=url, doc_unknown=False)
        return url
    if rec.get("doc"):
        return rec["doc"]
    if rec.get("docx"):
        url = doc_from_link(lg, rec["docx"])
        remember(doc=url)
        return url
    if rec.get("doc_unknown") and (doc or "").strip().lower() != "new":
        raise LedgerError("上次建文档没有拿到确定的结果，文档可能已经建在%s下了。先在网页上核对：建好了的，重跑时加 --doc <文档链接> 接着用它；确认没建成的，加 --doc new 重新建" % where)
    try:
        url = create()
    except WriteMaybeDone:
        remember(doc_unknown=True)
        raise
    remember(doc=url, doc_unknown=False)
    return url


def parent_node(lg, wid, seq, rel, source):
    if wid == "same_seq" and rel == "execute":
        if not source:
            raise LedgerError("执行关系的需求包要写来源工作项（它所属的下发需求包）：--source <ID>")
        node = lg.node_of(lg.doc_url(source))
        if not node:
            raise LedgerError("来源工作项 #%s 的主文档不是知识库文档，没法把执行需求包挂在它下面" % source)
        return node
    key = "same_seq/%s" % seq if wid == "same_seq" else wid
    n = lg.nodes.get(key)
    if not n or not n.get("node_token"):
        raise LedgerError("ids.json 里没有知识库节点 %s；先跑 ensure-node %s" % (key, key))
    return n["node_token"]




RUN_CONDITIONS = ("执行环境", "计算资源")
NOT_YET = re.compile(r"(?:待研究员确认|待确认|待定|未定|无)(?:$|[\s（(，,。；;：:])")
DECISION_REF = re.compile(r"(?:决策\s*#?\s*|#)(\d{3,})")


def run_conditions_hints(d):
    """Lines to print when a research task sheet writes 执行环境 or 计算资源 without the decision that set them."""
    section = d.section("资源上限")
    hints = []
    for row in RUN_CONDITIONS:
        m = re.search(r"^\s*[-*]\s*%s\s*[：:][ \t]*(.*)$" % row, section, re.M)
        value = m.group(1).strip() if m else ""
        if not value or orgspec.placeholder(value) or NOT_YET.match(value) or DECISION_REF.search(value):
            continue
        hints.append("提示：研究任务单“资源上限”里的%s写了“%s”，没有定下它的决策 ID：%s要进决策队列。请发出方写决策包交研究员定，"
                     "探索任务也一样；定了以后把决策 ID 写进这一行，比如“%s（决策 #<ID>）”，没定之前写“待研究员确认”。" % (row, value, row, value))
    return hints


def find_file(lg, name):
    """The file an @name of --set points at: looked for under the current directory, then the project."""
    for base in dict.fromkeys((os.getcwd(), lg.root)):
        full = name if os.path.isabs(name) else os.path.join(base, name)
        if os.path.isfile(full):
            return full
    return None


def read_text_file(lg, full):
    try:
        with open(full, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise LedgerError("文件 %s 不是 UTF-8 文本；转成 UTF-8 再传" % os.path.relpath(full, lg.root))


def delivered(lg, parent_id):
    """The deliveries a request has received so far, from the reply receipts that finished
    (packages/sent/reply-<ID>-*.json): [(version, receipt)], lowest first. A receipt from before 1.0.6 keeps
    no version: None, which sorts as the first."""
    out, folder = [], glob.escape(sent_dir(lg))
    for path in sorted(glob.glob(os.path.join(folder, "reply-%s-*.json" % parent_id))):
        rec = load_json(path, {})
        if isinstance(rec, dict) and rec.get("status") == "registered" and str(rec.get("parent", parent_id)) == str(parent_id):
            out.append((rec.get("version") if isinstance(rec.get("version"), int) else None, os.path.relpath(path, lg.root)))
    return sorted(out, key=lambda x: x[0] or 1)




def attach_receipt(lg, item_id, label, path, text):
    """(path, receipt) of the upload of a local file for one of an item's link fields: one receipt per file
    and content (packages/sent/attach-<ID>-<field>-<file>-<hash>.json). A rerun with the same content finds the
    document it made; a changed file has no receipt yet and becomes a new document with a new link."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    target = receipt_file(lg, ["attach-%s-%s-%s-%s" % (item_id, safe_name(label), safe_name(path), digest)], path)
    rec = load_json(target, {}) if os.path.exists(target) else {}
    return target, rec if isinstance(rec, dict) else {}


def check_new(d, kind):
    """A draft of a new work item: its sender is a session (a sheet's may be 研究员, 导师) and a package is
    its first version."""
    if not d.author:
        raise LedgerError("发出方要带会话名，比如“算法 +2（Alg-p2-01）”")
    if not kind and d.version != 1:
        raise LedgerError("登记的是新工作项，包头“版本”写第 1 版，现在是第 %s 版：已经登记过的包改了内容，用 docs.py revise <工作项 ID> <新草稿> 换主文档" % d.version)


def new_item_sequence(lg, d, kind, wid):
    """The sequence a new work item belongs to; a 同序列需求's relation goes with its sender's rank and a
    跨序列需求 names both sequences."""
    spec = lg.spec
    # a sheet the researcher or the mentor is the sender of belongs to the entry's sequence
    seq = d.receiver_seq if wid == "cross_request" else d.sender_seq or ("pmo" if kind else None)
    if not seq:
        raise LedgerError("从发出方、接收方认不出序列：写成“<序列名> +<职级>（<会话名>）”")
    if wid == "same_seq":
        if not d.rel:
            raise LedgerError("同序列需求要写关系：下发或执行")
        rank = re.search(r"-p([0-2])-", d.author or "")
        # +2 发下发关系的包，+1 发执行关系的包；本序列没开 +1 时 +2 直接委托 +0，写的也是执行关系（1.0.12）
        expected = {"2": ("assign", "execute"), "1": ("execute",)}.get(rank.group(1), ()) if rank else ()
        if d.rel not in expected:
            raise LedgerError("关系“%s”和发出方 %s 的职级对不上：+2 发给 +1 的同序列需求是下发，本序列没开 +1、直接发给 +0 的是执行；+1 发的是执行；退回作者改包头" % (
                spec.term("rel", d.rel), d.author))
    if wid == "cross_request" and not (d.sender_seq and d.receiver_seq):
        raise LedgerError("跨序列需求要认得出发起序列和承接序列")
    return seq


ITEM_IN_TITLE = re.compile(r"^工作项\s*(?:<工作项 ID>|#?\d+)\s*｜\s*")


def new_document(spec, d, kind):
    """(title, body) of the document a draft of a new work item becomes: the title without the item's ID,
    which `docs.py relink` puts in front once the item exists; the draft without its title line, with 状态包
    and 交接记录 at its end (a 反馈 has neither)."""
    title = ITEM_IN_TITLE.sub("", naming(spec, d, kind)[1])
    body = d.body_without_title()
    return title, body if kind == "feedback" else with_status_sections(body)


def text_digest(title, body):
    return hashlib.sha256(("%s\n%s" % (title, body)).encode("utf-8")).hexdigest()


def upload_receipt(lg, path):
    """(path, receipt) of a draft's upload receipt packages/sent/upload-<draft file>.json; the receipt is {}
    when the draft was not uploaded."""
    target = receipt_file(lg, ["upload-%s" % safe_name(path)], path)
    rec = load_json(target, {}) if os.path.exists(target) else {}
    return target, rec if isinstance(rec, dict) else {}


def is_last(node):
    return not node["next"]


# ------------------------------------------------------------- who runs the script
# From 1.1.0 a document is written by the session that answers for it, the work items by the scribe alone. Every
# session may run docs.py; the script holds the session it runs in against the document's owner. The session is
# read from the Bash environment and the pool's claims, which a session cannot change: the hook refuses a
# command that sets those variables.
def who(root):
    """(caller, origin). caller is {name, role, tag, sequence, rank} of the claim the session holds; None for
    the researcher's own session, a plain terminal and a project without a session pool (origin "researcher" or
    None). A session that holds no claim gives (None, origin) with what it is: pool.ORIGIN_LABEL."""
    try:
        policy = pool.load_policy(root)
        if not policy:
            return None, None
        claim, origin = pool.caller(root, policy)
    except (pool.PoolError, OSError, ValueError, KeyError):
        return None, "pool_unreadable"
    if not claim:
        return None, origin
    pol = (policy.get("agents") or {}).get(claim.get("role")) or {}
    return {"name": claim.get("name"), "role": claim.get("role"), "tag": pol.get("tag"),
            "sequence": pol.get("sequence"), "rank": pol.get("rank")}, origin


def doc_owner(lg, it=None, d=None):
    """Who writes an item's main document: (session names, sequence, words). The session its 发出方 names; when
    that is no session (研究员, 导师), the +2 of the item's sequence, 项目管理 where the item has none. Before
    the item exists, or offline, the draft says both."""
    sender = (lg.text_of(it, "sender") if it else None) or (getattr(d, "author", None) if d else None) or ""
    if SESSION_NAME.match(sender.strip()):
        return [sender.strip()], None, sender.strip()
    by_cn = {cn: s for s, cn, _ in lg.spec.sequences()}
    seq = by_cn.get((lg.text_of(it, "sequence") or "").strip()) if it else None
    seq = seq or (getattr(d, "sender_seq", None) if d else None) or "pmo"
    return [], seq, "%s +2" % lg.spec.term("seq", seq)


def must_be(lg, what, names=(), roles=(), seq=None, why="", any_claim=False):
    """Stop unless the session running the script may do `what`: one of the sessions in names, a session of
    one of the roles, the +2 of seq, any session that holds a claim (any_claim), or the researcher's own
    session. why says whose it is, for the message. Offline nothing is written and nothing is asked."""
    me = lg.caller
    if lg.mode == "offline":
        return
    if me is None:
        if lg.origin in (None, "researcher"):
            return
        raise LedgerError("%s不能%s：先认领会话（pool.py claim），认领表有疑问的请研究员用 pool.py show 核对。" % (
            pool.ORIGIN_LABEL.get(lg.origin, "未认领的会话"), what))
    mine = (me.get("name") or "").lower()
    if any_claim or any(mine == (n or "").lower() for n in names) or me.get("role") in roles \
            or seq and me.get("sequence") == seq and me.get("rank") == 2:
        return
    raise LedgerError("（%s）不能%s%s。" % (me.get("name"), what, "：" + why if why else ""))


def seq_of_session(lg, name):
    """The sequence a session name such as Alg-p1-02 belongs to, by its abbreviation; None for other text."""
    if not SESSION_NAME.match(name or ""):
        return None
    head = name.split("-", 1)[0].lower()
    return next((s for s, _, abbrev in lg.spec.sequences() if abbrev.lower() == head), None)


def is_p2_of(lg, name, seq):
    return seq_of_session(lg, name) == seq and "-p2-" in (name or "").lower()


def is_p0_of(lg, name, seq):
    return seq_of_session(lg, name) == seq and "-p0-" in (name or "").lower()


def role_slot(lg, wid, node):
    """The contract's role slot a node belongs to."""
    slots = {rid: slot for slot, rid in (lg.roles.get(wid) or {}).items()}
    slot = slots.get(node["roles"][0]) if len(node["roles"]) == 1 else None
    if not slot:
        raise LedgerError("节点“%s”的负责角色（%s）不在约定里，判断不了谁能推进这一步：先跑 space.py check" % (
            node["name"], "、".join(node["roles"]) or "无"))
    return slot


def responsible(lg, wid, it, node):
    """(who is responsible for a node, in words; test(by) -> bool): the node's role decides
    who may ask the scribe to finish or undo the step. The researcher may always."""
    slot = role_slot(lg, wid, node)
    rule = lg.contract.role(slot)
    who, cn = rule["who"], lg.contract.name("roles", slot)
    if who == "scribe":
        return cn, lambda by: True
    if who == "researcher":
        return RESEARCHER, lambda by: by == RESEARCHER
    if who == "sequence":
        return "%s +2" % cn, lambda by: by == RESEARCHER or is_p2_of(lg, by, slot)
    value = (lg.text_of(it, rule["field"][wid]) or "").strip()
    by_cn = {cn_: s for s, cn_, _ in lg.spec.sequences()}
    if value in by_cn:
        return "%s（%s +2）" % (cn, value), lambda by: by == RESEARCHER or is_p2_of(lg, by, by_cn[value])
    if SESSION_NAME.match(value):
        seq = seq_of_session(lg, value)
        # a +0 goes by two names: its pool slot (Alg-p0-01) and, on a work item, a work name after the item (Alg-p0-2101)
        p0 = "-p0-" in value.lower()
        return "%s（%s）" % (cn, value), lambda by: by == RESEARCHER or by.lower() == value.lower() or bool(seq) and (
            is_p2_of(lg, by, seq) or p0 and is_p0_of(lg, by, seq))
    if value == RESEARCHER:
        return "%s（研究员）" % cn, lambda by: by == RESEARCHER
    return "%s（工作项上还没记是谁）" % cn, lambda by: bool(by)


def sign_slots(lg, wid, node):
    """[(slot, field_key)] of the signature fields a node's form requires."""
    keys = lg.field_keys.get(wid) or {}
    required = {r["field_key"] for r in node["required"]}
    return [(s, keys[s]) for s in lg.contract.signatures() if keys.get(s) in required]


HEADING = re.compile(r'<h([1-9])\b[^>]*?\bid="([^"]+)"[^>]*>(.*?)</h\1>', re.S)


def heading_ids(lg, url):
    """[(block_id, heading text)] from `docs +fetch --scope outline --detail with-ids`."""
    data = lg.lark("docs", "+fetch", "--doc", url, "--scope", "outline", "--max-depth", "2", "--detail", "with-ids")
    if data is None:
        return []
    return [(m.group(2), re.sub(r"<[^>]+>", "", m.group(3)).strip()) for m in HEADING.finditer(doc_content(data))]


def naming(spec, d, kind):
    """(the work item's name, its main document's title with <工作项 ID> still in it) for a draft: a package,
    or a sheet of the given kind."""
    if kind == "research_task":
        return d.task, spec.rules["naming"]["task_doc"].replace("<任务名>", d.task)
    if kind == "feedback":
        name = "%s：%s" % (next(w for w, k in SHEETS.items() if k == kind), d.task)
        return name, "工作项 <工作项 ID>｜%s" % name
    pkg, rules = spec.pkg(d.pid), spec.rules["naming"]
    fill = lambda rule: rule.replace("<包名>", pkg).replace("<作者会话名>", d.author or "").replace("<任务名>", d.task)
    return fill(rules["work_item"]), fill(rules["main_doc"])


# ---------------------------------------------------------------- reads
def _not_in(states):
    return "`work_item_status` NOT IN (%s)" % ", ".join("'%s'" % s for s in states)


def mql_text(lg, name, sequence=None):
    """MQL for a named query. Every query leaves out the system state 已终止:
    views hide terminated items by default, MQL does not."""
    terminated = system_states(lg.spec)

    names = {s: cn for s, cn, _ in lg.spec.sequences()}
    if sequence and sequence not in names:
        hit = [s for s, cn in names.items() if cn == sequence]
        if not hit:
            raise LedgerError("没有序列“%s”；--sequence 写标识或中文名：%s" % (sequence, "、".join("%s（%s）" % kv for kv in names.items())))
        sequence = hit[0]

    def of_sequence(wid):
        return "`%s` = '%s'" % (lg.field(wid, "sequence")["field_key"], lg.spec.term("seq", sequence))

    if name.startswith("source:"):
        src = item_id_of(name.split(":", 1)[1])
        if sequence:
            raise LedgerError("source:<ID> 查的是由某个工作项引出的所有工作项，不按序列筛；去掉 --sequence")
        return [_select(lg, w, "`%s` = '%s' AND %s" % (lg.field(w, "source_item")["field_key"], src, _not_in(terminated)))
                for w in lg.contract.types_with("fields", "source_item") if w in lg.types]
    if name == "blocked":                               # blocked is two fields on an item, not a state
        out = []
        for w in lg.contract.types_with("fields", "blocked_reason"):
            if w in lg.types:
                reason = lg.field(w, "blocked_reason")["field_key"]
                parts = ["`%s` IS NOT NULL" % reason, _not_in([lg.state(w, "archived")] + terminated)]
                out.append(_select(lg, w, " AND ".join(parts + ([of_sequence(w)] if sequence else [])), [reason]))
        return out
    if name not in QUERIES:
        raise LedgerError("没有查询 %s；可选：%s、source:<ID>" % (name, "、".join(QUERY_NAMES)))
    wid, keep, leave, cond, _ = QUERIES[name]
    parts = [cond] if cond else []
    if keep:
        parts.append("`work_item_status` IN (%s)" % ", ".join("'%s'" % lg.state(wid, s) for s in keep) if len(keep) > 1
                     else "`work_item_status` = '%s'" % lg.state(wid, keep[0]))
    else:
        parts.append(_not_in([lg.state(wid, s) for s in leave] + terminated))
    if sequence:
        parts.append(of_sequence(wid))
    return [_select(lg, wid, " AND ".join(parts), [k for k in [lg.key_of(wid, s) for s in QUERY_FIELDS.get(name, ())] if k])]


def _select(lg, wid, cond, extra=()):
    return "SELECT `work_item_id`, `name`, `work_item_status`%s FROM `%s`.`%s` WHERE %s" % (
        "".join(", `%s`" % k for k in extra), lg.key, lg.type_ref(wid), cond)


# Fields a named query shows besides ID, state and name (blocked shows blocked_reason), and how they are
# labelled. The entry lists query queue when the researcher comes back, each decision with who carries
# it out and what the researcher must do by hand.
QUERY_FIELDS = {"queue": ("urgency",)}


def receipt_of(lg, item_id):
    """The registration receipt of a work item: packages/sent/<ID>.json, or <ID>-<draft file>.json; {} when none."""
    num = str(item_id)
    folder = glob.escape(sent_dir(lg))
    for path in sorted(glob.glob(os.path.join(folder, num + ".json"))) + sorted(glob.glob(os.path.join(folder, num + "-*.json"))):
        rec = load_json(path, {})
        if isinstance(rec, dict) and str(rec.get("work_item_id")) == num:
            return rec
    return {}


def query_rows(data):
    """Rows of `workitem query --format json`: data.<group>[].moql_field_list[{key, value}].

    Anything else (an error text, a JSON object without data, other shapes)
    stops with LedgerError: an empty list would read as "nothing is waiting".
    """
    bad = LedgerError("workitem query 的输出读不懂，没法确定有没有结果：%s" % json.dumps(data, ensure_ascii=False)[:300])
    groups = data.get("data") if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        raise bad
    rows = []
    for group in groups.values():
        if group is None:
            continue
        if not isinstance(group, list):
            raise bad
        for rec in group:
            fl = rec.get("moql_field_list") if isinstance(rec, dict) else None
            if not isinstance(fl, list) or not all(isinstance(f, dict) for f in fl):
                raise bad
            for f in fl:
                labels = f.get("value", {}).get("key_label_value_list") if isinstance(f.get("value"), dict) else None
                if labels is not None and not (isinstance(labels, list) and all(isinstance(x, dict) for x in labels)):
                    raise bad
            rows.append({f.get("key"): moql_value(f.get("value")) for f in fl})
    return rows


def past_the_end(data):
    """The recorded answer for a page after the last one: every item null, no error."""
    return isinstance(data, dict) and bool(data) and all(v is None for v in data.values())


def query_count(data):
    info = data.get("list")[0] if isinstance(data.get("list"), list) and data.get("list") else None
    count = info.get("count") if isinstance(info, dict) else None
    if isinstance(count, bool):
        return None
    if isinstance(count, int):
        return count
    if isinstance(count, float) and count.is_integer():
        return int(count)
    if isinstance(count, str) and count.isdigit():
        return int(count)
    return None


QUERY_PAGE, QUERY_MAX_PAGES = 50, 20


def query_all(lg, mql):
    """Every row of one MQL query. The first answer holds 50 rows at most, with the
    total in list[0].count; the rest are read page by page with its session_id."""
    data = lg.run(["meegle", "workitem", "query", "--project-key", lg.key, "--mql", mql, "--format", "json"], False)
    if data is None:
        return []
    rows = query_rows(data)
    count = query_count(data)
    if count is None and len(rows) < QUERY_PAGE or count is not None and count <= len(rows):
        return rows                                    # a short first page is complete
    info = data["list"][0] if isinstance(data.get("list"), list) and data["list"] else {}
    groups = info.get("group_infos") if isinstance(info, dict) else None
    group = next((g.get("group_id") for g in groups or [] if isinstance(g, dict) and g.get("group_id")), "1")
    session = data.get("session_id")
    last = min(-(-count // QUERY_PAGE), QUERY_MAX_PAGES) if count is not None else QUERY_MAX_PAGES
    page = 1
    while session and page < last:
        page += 1
        more = lg.run(["meegle", "workitem", "query", "--project-key", lg.key, "--session-id", str(session),
                       "--group-pagination-list", json.dumps([{"group_id": group, "page_num": page}]), "--format", "json"], False)
        if past_the_end(more):
            break
        try:
            got = query_rows(more)
        except LedgerError as e:
            raise LedgerError("第 %d 页（前面已读出 %d 项）：%s" % (page, len(rows), e))
        rows += got
        if count is None and len(got) < QUERY_PAGE:
            break
    if count is not None and len(rows) < count or count is None and (page >= QUERY_MAX_PAGES or not session):
        print("提醒：%s只读出 %d 项（每页 50 项，没翻完）；完整结果在飞书项目的网页上看。" % (
            "共 %d 项，" % count if count is not None else "", len(rows)))
    return rows


def also_to_router(lg, item_id):
    """One more line for the scribe once it has written on a 跨序列需求: the receipt goes to the router too. From
    1.0.12 the two +2 of such a request talk to each other and ask the scribe themselves, the entry is not in
    between; the router keeps the record of who waits for whom, and a receipt is what wakes it. The type is
    read from the registration receipt (packages/sent/<ID>.json): nothing more is asked of the space."""
    if lg.mode == "apply" and item_id and receipt_of(lg, str(item_id).lstrip("#")).get("type") == "cross_request":
        print("跨序列需求：这条回执另发一份给路由 %s（请求方就是它的不用再发）。" % lg.spec.rules["naming"]["fixed_sessions"]["pmo-router"])


def parser(description):
    """(parser, subparsers) of an entry script: --offline and the subcommands the script adds."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--offline", action="store_true", help="不调用飞书，只打印命令")
    return ap, ap.add_subparsers(dest="cmd")


def run(ap, argv, command):
    """What every entry script does around its own commands: read the binding, hold the write lock while
    it applies, turn an error into one line. `command(lg, args)` carries out the subcommand."""
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    mode = "offline" if args.offline else ("apply" if getattr(args, "apply", False) else "preview")
    lg = Ledger(project_root(), mode)
    m = lg.ids.get("meegle") or {}
    if mode != "offline" and not (m.get("bound_at") and m.get("types")):  # a binding time with no type is no binding
        print("出错：这个项目还没绑定飞书空间。先跑 space.py feishu --target <项目目录> --live 看绑定结果，核对后加 --apply；"
              "绑定以前台账脚本不读也不写飞书。")
        return 1
    if mode != "offline" and lg.ids.get("schema") != space.IDS_SCHEMA:
        print("出错：feishu/ids.json 是旧版的绑定（%s）。这一版只认用节点流程的空间：照 setup 技能用模板建空间，"
              "再跑 space.py feishu --target <项目目录> --live --apply 重新绑定。" % (lg.ids.get("schema") or "没有 schema"))
        return 1
    moved = other_space(load_json(os.path.join(lg.root, "spec", "task.json"), {}), m)
    if mode != "offline" and moved:
        print("出错：spec/task.json 里的飞书项目空间是“%s”，绑定的是“%s”。重跑 space.py feishu --target <项目目录> --live 核对，"
              "再加 --apply；在这以前台账脚本不读也不写飞书。" % (moved, m["space_name"]))
        return 1
    lock = write_lock(lg.root) if mode == "apply" else contextlib.nullcontext()
    try:
        with lock:
            command(lg, args)
    except (LedgerError, orgspec.OrgSpecError, space.SpaceError, ET.ParseError, OSError) as e:
        print("出错：%s" % e)
        return 1
    if mode == "preview" and lg.planned:
        print("这是预览，没有写飞书。核对后加 --apply 执行。")
    return 0
