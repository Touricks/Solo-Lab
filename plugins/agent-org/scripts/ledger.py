#!/usr/bin/env python3
"""Read and write the task ledger (Feishu Project) and package documents (Feishu Docs).

Read commands, for every session:
    ledger.py get <ID> [--raw]              type, state, flow, name and the fields that have a value
    ledger.py next <ID>                     the step the item is at: who is responsible, what is still to fill in
    ledger.py query <name> [--sequence s]   queue | blocked | verifying | open | cross | tasks | recent | source:<ID>
    ledger.py mql <name> [--sequence s]     print the MQL only
    ledger.py trace <ID>                    follow 来源工作项 upwards
    ledger.py status <ID>                   print the 状态包 section of the main document

Write commands, for the scribe only (the guard hook enforces this). They print the
commands first; nothing is written without --apply:
    ledger.py register <draft> [--task ID] [--source ID] [--set f=v ...] [--resume] [--existing ID]
                                            需求包 / 决策包 / 研究任务单 / 反馈: work item + main document
    ledger.py reply <draft>                 交付包: document under the request it answers, 交付包 field
    ledger.py confirm <ID> --by <会话名> [--set f=v ...]     finish the step the item is at
    ledger.py rollback <ID> --to <节点> --reason R (--by <会话名> | --decision <ID>)
    ledger.py archive <ID>                  move the main document subtree to 归档文档/<序列>, then finish the last step
    ledger.py update <ID> --set f=v [...]   blocked_reason=<原因> also sets the date; an empty value clears a field
    ledger.py comment <ID> --text T
    ledger.py relink <ID>                   put the item's ID into the 关联工作项 line of its main document
    ledger.py status-block <ID> --file F    replace the 状态包 section (block_replace)
    ledger.py append <ID> --file F          append to 交接记录 (first line carries the session tag)
    ledger.py ensure-node <key> | all       create knowledge-base nodes such as feedback or archive/alg

Every work item type has node flows. Which flows there are, how the nodes follow each
other, who is responsible for a step and what it requires is read from the space, never
kept here: `confirm` asks the space what the current node still lacks, writes the --set
values, confirms the node and reads it back. What the scripts must find by meaning (the
main-line type, the main document field, the role that signs ...) is the contract,
orgspec/contract.json; feishu/ids.json binds each slot to this project's space and
feishu/space.json is the space's structure as `space.py snapshot` read it.

A field is named by its slot (main_doc), its name in the space (成功标准) or its field_key.
A link field may be given a local file, --set 规格=@spec/spec.md: confirm and update upload it
as a document under the work item's main document and write its link.
Values go to meegle as strings: dates as UTC noon in milliseconds, numbers as digits.
Three checks are the scripts' own. A step whose form requires a signature field takes a
decision that is decided, approved and made for this task. Before the step that requires
the conclusion, the operation records are read: a frozen field changed after the signing
step stops the command. A roll-back on the main line needs an approved decision and
clears the required fields of the steps it undoes.

Document bodies go to lark-cli on stdin (`--content -`): lark-cli only reads @files
under the working directory, /tmp or ~/files. The shapes parsed here are the ones
recorded from a real space (tests/fixtures/feishu/). --offline prints every command
without calling Feishu (used by tests). Every --apply holds an exclusive lock on
feishu/.ledger.lock (waiting up to 60 seconds), so two writers never interleave; ids.json
and receipts are replaced atomically.
Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
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
import project_paths  # noqa: E402
import space  # noqa: E402

# name: (type slot, state slots to keep or None, state slots to leave out, extra condition or None, description)
QUERIES = {
    "queue": ("decision", ["pending"], [], None, "决策队列：待决定的决策"),
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
SESSION_NAME = re.compile(r"^[A-Za-z]+-p[0-2]-\w+$")
NODE_STATUS = {"doing": "进行中", "finished": "已完成", "not_started": "未开始"}


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
        self.tag = os.environ.get("AGENT_ORG_TAG", "[PMO+0]")
        self.actor = None
        if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
            import codex_runtime
            context = codex_runtime.Pool(root).context()
            policy = load_json(os.path.join(root, ".agent-org", "policy.json"), {})
            self.tag = (policy.get("agents", {}).get(context.get("role")) or {}).get("tag", "[unknown]")
            self.actor = {key: context.get(key) for key in ("thread_id", "host_id", "slot", "role")}
        self.planned = []
        self._me = None

    # names and keys
    def bound_type(self, wid):
        t = self.types.get(wid) or {}
        if not t.get("type_key"):
            raise LedgerError("%s还没绑定到空间里的类型：先跑 setup.py feishu --live，核对后加 --apply" % self.spec.term("wi", wid))
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
                raise LedgerError("%s的字段 %s（%s）还没绑定：报给模板维护者补上，名字不同的用 --map 指认，再跑 setup.py feishu --live --apply" % (
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
            raise LedgerError("%s的状态“%s”还没绑定：先跑 setup.py feishu --live --apply" % (
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
            raise LedgerError("运行不了 %s（%s）；先装好并登录，见 setup.py precheck" % (argv[0], e))
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
        if self.actor is not None:
            rec["actor"] = self.actor
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


def created_id(out, what):
    """The work item id in a `workitem create` answer; stop if there is none.

    Only work_item_id (anywhere), or an id at the top level or directly under data:
    a nested id may belong to the template or the project.
    """
    item_id = _find(out, ("work_item_id",))
    if item_id in (None, "") and isinstance(out, dict):
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        item_id = out.get("id") or data.get("id")
    if item_id in (None, ""):
        raise LedgerError("%s建了，但输出里没有工作项 ID：%s；去网页上核对，不要重跑" % (
            what, json.dumps(out, ensure_ascii=False)[:300]))
    return str(item_id)


# --------------------------------------------------------------- drafts
HEADER = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")
SHEETS = {"研究任务单": "research_task", "反馈": "feedback"}
SHEET_KEYS = {"research_task": ["流程", "发出方", "关联工作项"], "feedback": ["发出方", "关联工作项"]}
LINK_LINE = re.compile(r"^(- 关联工作项[：:][ \t]*)([^；;\n]*)(.*)$", re.M)
PLACEHOLDERS = {"", "待建", "待登记", "空", "无", "-", "—"}


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


def title_of(text):
    return next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")


def link_line(text, ref, item_id):
    """The draft with its work item in the 关联工作项 header line.

    A placeholder (待建, 待登记, empty ...) becomes ref; whatever follows the first ；
    (；来源工作项 #2100) is kept. A line that already names this item is left alone.
    Anything else is neither a placeholder nor this item: LedgerError, before a
    document carries a wrong ID.
    """
    head = text.split("\n## ", 1)[0]
    m = LINK_LINE.search(head)
    if not m:
        raise LedgerError("包头里没有“关联工作项”一行：登记新工作项的草稿写“- 关联工作项：待建”")
    value, num = m.group(2).strip(), re.escape(str(item_id))
    if value.strip("（）()〔〕[] ") in PLACEHOLDERS:
        return text[:m.start()] + m.group(1) + ref + m.group(3) + text[m.end():]
    if re.fullmatch(r"#?%s|\[#?%s\]\(\S+\)" % (num, num), value):
        return text
    raise LedgerError("包头的“关联工作项”写的是“%s”：登记新工作项的草稿这一行写“待建”，登记时换成新建的工作项 ID" % value)


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
        self.sender_seq = next((s for cn, s in seq_by_cn.items() if self.header.get("发出方", "").startswith(cn)), None)
        self.receiver_seq = next((s for cn, s in seq_by_cn.items() if self.header.get("接收方", "").startswith(cn)), None)
        link = self.header.get("关联工作项", "")
        self.linked = re.findall(r"#(\d+)", link)
        src = re.search(r"来源工作项[：:\s]*#?(\d+)", self.text)
        self.source = src.group(1) if src else None
        ref = re.search(r"#?(\d{3,})", self.header.get("所属研究任务", ""))
        self.task_ref = ref.group(1) if ref else None
        self.flow = None

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
        self.task = re.split(r"[：:]", title_of(self.text), 1)[-1].strip()
        if not self.task:
            raise LedgerError("%s的标题要写成“# %s：<名字>”" % (word, word))
        sender = self.header.get("发出方", "").strip()
        if re.search(r"[<>]", self.task + sender + self.header.get("流程", "")):
            raise LedgerError("%s的标题或开头几行还是模板里的占位（<…>），先填好再登记" % word)
        m = re.search(r"（([A-Za-z]+-p[0-2]-[\w]+)）", sender)
        self.author = m.group(1) if m else (sender or None)        # a session, or 研究员, 导师 ...
        seq_by_cn = {spec.term("seq", s): s for s, _, _ in spec.sequences()}
        self.sender_seq = next((s for cn, s in seq_by_cn.items() if sender.startswith(cn)), None)
        src = re.search(r"来源工作项[：:\s]*#?(\d+)", self.text)
        self.source = src.group(1) if src else None
        ref = re.search(r"#?(\d{3,})", self.header.get("所属研究任务", ""))
        self.task_ref = ref.group(1) if ref else None
        self.flow = self.header.get("流程", "").strip() or None


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


# ------------------------------------------------------------- receipts
def sent_dir(lg):
    return os.path.join(lg.root, "packages", "sent")


def find_receipt(lg, path):
    rel = os.path.relpath(path, lg.root)
    d = sent_dir(lg)
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        if name.endswith(".json") and not name.startswith("reply-"):
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


def save_receipt(lg, path, item_id, status, **extra):
    """packages/sent/<ID>.json: written right after the work item exists, updated as steps finish.

    Keeps what a --resume needs: the source, the --set pairs, and the document as
    soon as it exists (docx), then its /wiki/ link (doc).
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
    save_json(target, rec)


def check_receipt(lg, path, resume, again=""):
    """The receipt to continue from with --resume (or --doc); None for a fresh registration."""
    rec = find_receipt(lg, path)
    if not rec:
        if resume:
            raise LedgerError("没有这份草稿的登记回执，不用 --resume")
        return None
    item_id = rec.get("work_item_id")
    if rec.get("status") == "registered":
        raise LedgerError("这份草稿已经登记成 #%s（回执 %s），不要重复登记%s" % (item_id, rec["_file"], again))
    if not resume:
        step = "写回主文档链接" if rec.get("doc") else ("查主文档的知识库节点" if rec.get("docx") else "建主文档")
        raise LedgerError("上次已经建了工作项 #%s，但没做完（停在：%s）。修好原因后加 --resume 接着做，不会再建一个" % (item_id, step))
    try:
        lg.item(item_id)
    except LedgerError as e:
        raise LedgerError("回执里的工作项 #%s 读不到了（%s）。如果它已经被删掉，删掉回执 %s 再重新登记" % (
            item_id, str(e)[:120], rec["_file"]))
    return rec


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


def resolved_pairs(lg, pairs):
    """--set pairs with @file replaced by the file's text, as kept in a receipt: a --resume
    writes the same values even if the file has moved or changed."""
    out = []
    for pair in pairs or []:
        fid, eq, raw = pair.partition("=")
        if not eq:
            raise LedgerError("--set 要写成 字段标识=值：%s" % pair)
        value = set_value(lg, raw)
        out.append("%s=%s" % (fid, "@" + value if value.startswith("@") else value))
    return out


def merge_pairs(*lists):
    """Later --set pairs win over earlier ones for the same field."""
    merged = {}
    for pairs in lists:
        for pair in pairs or []:
            merged[pair.partition("=")[0]] = pair
    return list(merged.values())


def work_item_type(draft):
    if draft.pid == "request":
        return "cross_request" if draft.rel == "cross" else "same_seq"
    if draft.pid == "decision":
        return "decision"
    raise LedgerError("%s是答复，不新建工作项；用 reply" % draft.header["包类型"])


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


def ensure_roles(lg, wid, item_id):
    """After create, check that every role of the item has the researcher in it; add where it
    does not. Returns "ok", "added", "unknown" (could not tell) or None (not checked)."""
    if lg.mode != "apply":
        return None
    try:
        data = lg.get(item_id, ["_all"])
        members = ((data or {}).get("work_item_attribute") or {}).get("role_members")
        if not isinstance(members, list):
            print("提醒：读不到 #%s 的角色人员，没法确认各节点有没有负责人；请在网页上核对。" % item_id)
            return "unknown"
        me = lg.me()
        empty = [r.get("key") for r in members if isinstance(r, dict) and r.get("key")
                 and me not in json.dumps(r.get("members") or [], ensure_ascii=False)]
        if not empty:
            return "ok"
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--role-operate",
                  json.dumps([{"op": "add", "role_key": r, "user_keys": [me]} for r in empty]), write=True)
        print("建工作项时带的角色人员没全生效，已用 --role-operate 把研究员补进：%s。" % "、".join(empty))
        return "added"
    except LedgerError as e:
        print("提醒：没能核对 #%s 的角色人员：%s" % (item_id, str(e)[:200]))
        return "unknown"


def flow_entry(lg, wid, name=None):
    """The `template` field of workitem create: which flow the new item follows."""
    tid, flow = space.flow_of(lg.snap, (lg.types.get(wid) or {}).get("type_key"), name)
    if not tid:
        if lg.mode == "offline":
            return {"field_key": "template", "field_value": "<流程 ID>"}
        flows = "、".join(f.get("name") or "" for f in (lg.space_type(wid).get("flows") or {}).values())
        if not flows:
            raise LedgerError("feishu/space.json 里没有%s的流程；先跑 setup.py feishu --live --apply" % lg.spec.term("wi", wid))
        raise LedgerError("%s有这些流程：%s；%s" % (lg.spec.term("wi", wid), flows,
                                               "没有“%s”" % name if name else "要写明走哪一个"))
    return {"field_key": "template", "field_value": str(tid)}


def space_url(lg):
    host = (lg.ids.get("meegle") or {}).get("host") or "project.feishu.cn"
    return "https://%s/%s" % (host, lg.simple_name or lg.key)


def open_tasks(lg):
    """The research tasks that are not archived: [(id, name)]. [] offline."""
    main = lg.contract.main_line()
    if lg.mode == "offline" or main not in lg.types:
        return []
    rows = []
    for mql in mql_text(lg, "tasks"):
        rows += query_all(lg, mql)
    return [(str(r.get("work_item_id")), r.get("name") or "") for r in rows]


def task_of(lg, wid, given):
    """The research task a new item belongs to: what was given, else the only one that is open."""
    if given:
        return item_id_of(given)
    tasks = open_tasks(lg)
    if len(tasks) == 1:
        return tasks[0][0]
    if lg.mode == "offline":
        return "<所属研究任务 ID>"
    key = lg.key_of(wid, "task_ref")
    must = any(c.get("required") and c.get("field_key") == key for c in lg.space_type(wid).get("create_fields") or [])
    if not must:
        return None
    raise LedgerError("没法确定这份包属于哪条研究任务：空间里没归档的研究任务有 %d 条%s。包头加一行“- 所属研究任务：#<ID>”，或者登记时加 --task <ID>" % (
        len(tasks), "（%s）" % "、".join("#%s %s" % t for t in tasks[:6]) if tasks else ""))


def first_step_is_the_senders(lg, wid, nodes):
    """The first node only records that the sender put the item in (跨序列需求的“提出”): its
    role is the issuer and everything it requires is filled in when the item is created."""
    first = next((n for n in nodes or [] if n["status"] == "doing"), None)
    if not first or first["prev"] or len(nodes) < 3:
        return None
    created = {c["field_key"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required")}
    issuer = (lg.roles.get(wid) or {}).get("issuer")
    if first["roles"] == [issuer] and first["required"] and all(r["field_key"] in created for r in first["required"]):
        return first
    return None


def revisable(lg, item_id, d, kind):
    """The research task whose sheet a revised sheet replaces: it is still at its first step
    and runs the flow the revision names. Returns the link of its present main document."""
    spec = lg.spec
    cn = spec.term("wi", "research_task")
    if kind != "research_task":
        raise LedgerError("--existing 只给%s换主文档：草稿的标题要写成“# %s：<任务名>”" % (
            cn, next(w for w, k in SHEETS.items() if k == "research_task")))
    it, wid = typed_item(lg, item_id, "research_task")
    if wid != "research_task":
        raise LedgerError("#%s 是%s，--existing 只给%s换主文档" % (item_id, spec.term("wi", wid), cn))
    first = lg.flow_nodes(item_id)[0]
    if first["status"] != "doing":
        raise LedgerError("#%s 已经过了第一步“%s”，研究任务单不再整份替换：后面的改动写进各步自己的产出文档；要改目标，请研究员决定退回第一步" % (
            item_id, first["name"]))
    if it.get("flow") and d.flow and it["flow"] != d.flow:
        raise LedgerError("#%s 走的是“%s”流程，修订稿写的是“%s”：流程在新建时选定，改不了；要换流程，另开一条%s" % (
            item_id, it["flow"], d.flow, cn))
    return plain(it["fields"].get(lg.field(wid, "main_doc")["field_key"]))


def register(lg, path, source=None, pairs=None, resume=False, doc=None, task=None, existing=None):
    """Register a draft: a work item with the draft as its main document.

    需求包 → 同序列需求 or 跨序列需求; 决策包 → 决策; 研究任务单 → 研究任务; 反馈 → 反馈.
    Everything that can fail is checked before the work item exists: the header, the
    fields the new-item page requires, the parent node. The item is created with a
    link to the space as its main document, since the page requires one; the document
    is created next, with the item's ID in its title and header, and written back.
    existing=<ID>: a revised research-task sheet becomes the main document of that task;
    no item is created, the old document stays where it is.
    """
    spec = lg.spec
    kind = sheet_kind(path)
    d = Sheet(spec, path, kind) if kind else Draft(spec, path)
    wid = kind or work_item_type(d)
    if not d.author:
        raise LedgerError("发出方要带会话名，比如“算法 +2（Alg-p2-01）”")
    rec = check_receipt(lg, path, resume or bool(doc) and bool(find_receipt(lg, path))) if lg.mode != "offline" else None
    if existing and not rec:
        if lg.mode == "offline":
            raise LedgerError("--existing 要先读这条研究任务走到哪一步，离线做不了")
        existing = str(item_id_of(existing))
        rec = {"work_item_id": existing, "existing": True, "replaced": revisable(lg, existing, d, kind)}
    swap = bool(rec and rec.get("existing"))
    if rec:
        source, task = source or rec.get("source"), task or rec.get("task")
        pairs = merge_pairs(rec.get("pairs"), resolved_pairs(lg, pairs))   # rewritten with the main document
    source = item_id_of(source or d.source) if (source or d.source) else None
    if existing:                                      # a revised sheet may already name the task it revises
        link_line(d.text, "#%s" % item_id_of(existing), str(item_id_of(existing)))
    else:
        link_line(d.text, "#0", "0")                  # a 关联工作项 line that is no placeholder stops here
    word = next((w for w, k in SHEETS.items() if k == kind), None)
    if kind == "research_task":
        name, title = d.task, spec.rules["naming"]["task_doc"].replace("<任务名>", d.task)
    elif kind == "feedback":
        name = "%s：%s" % (word, d.task)
        title = "工作项 <工作项 ID>｜%s" % name
    else:
        pkg = spec.pkg(d.pid)
        name = spec.rules["naming"]["work_item"].replace("<包名>", pkg).replace("<作者会话名>", d.author).replace("<任务名>", d.task)
        title = spec.rules["naming"]["main_doc"].replace("<包名>", pkg).replace("<作者会话名>", d.author).replace("<任务名>", d.task)

    fields = [{"field_key": "name", "field_value": name}, flow_entry(lg, wid, d.flow)]
    required = [("sender", d.author)]
    # a sheet the researcher or the mentor is the sender of belongs to the entry's sequence
    seq = d.receiver_seq if wid == "cross_request" else d.sender_seq or ("pmo" if kind else None)
    if not seq:
        raise LedgerError("从发出方、接收方认不出序列：写成“<序列名> +<职级>（<会话名>）”")
    if "sequence" in lg.contract.slots(wid, "fields"):
        required.append(("sequence", spec.term("seq", seq)))
    if wid == "same_seq":
        if not d.rel:
            raise LedgerError("同序列需求要写关系：下发或执行")
        rank = re.search(r"-p([0-2])-", d.author or "")
        expected = {"2": "assign", "1": "execute"}.get(rank.group(1)) if rank else None
        if expected != d.rel:                       # +2 发下发关系的包，+1 发执行关系的包
            raise LedgerError("关系“%s”和发出方 %s 的职级对不上：+2 发的同序列需求是下发，+1 发的是执行；退回作者改包头" % (
                spec.term("rel", d.rel), d.author))
        required += [("relation", spec.term("rel", d.rel)), ("owner_session", d.receiver or "待派")]
    if wid == "cross_request":
        if not (d.sender_seq and d.receiver_seq):
            raise LedgerError("跨序列需求要认得出发起序列和承接序列")
        required += [("from_sequence", spec.term("seq", d.sender_seq)), ("to_sequence", spec.term("seq", d.receiver_seq))]
    if source:
        required.append(("source_item", source))
    if "task_ref" in lg.contract.slots(wid, "fields"):
        task = task_of(lg, wid, task or d.task_ref)
        if task:
            required.append(("task_ref", task))
    for ref, raw in required:
        fields.append(lg.entry(wid, ref, raw))
    optional = []
    has_due = "due" in lg.contract.slots(wid, "fields")          # a decision has no deadline: it waits for the researcher
    if has_due and utc_noon_ms(d.header.get("回复期限", "")):
        optional.append(("due", d.header["回复期限"]))
    elif has_due and re.search(r"\d{4}-\d{1,2}-\d{1,2}", d.header.get("回复期限", "")):
        print("提醒：回复期限“%s”不是存在的日期，截止时间没写；改好后用 update 补" % d.header["回复期限"])
    if wid == "decision":
        optional.append(("urgency", d.header.get("紧急程度")))
    for ref, raw in optional:
        if not raw or "/" in str(raw) and ref == "urgency":       # the template's "插队/今天/本周/可延后" left as it was
            continue
        try:
            fields.append(lg.entry(wid, ref, raw))
        except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（之后可以用 update 补）：%s" % (ref, e))
    sets = parse_sets(lg, wid, pairs)                 # on --resume, all values are written again with the main document
    if any("upload" in v for _, v in sets):
        raise LedgerError("登记时还没有主文档，产出文档没处挂：先登记，再用 ledger.py update <工作项 ID> --set <字段>=@<文件> 上传")
    if not rec:
        fields += [v for _, v in sets]
    doc_key = lg.field(wid, "main_doc")["field_key"]
    fields = [f for f in fields if f["field_key"] != doc_key] + [{"field_key": doc_key, "field_value": space_url(lg)}]
    have = {f["field_key"] for f in fields if f.get("field_value") not in (None, "")}
    lacking = [c["name"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required") and c["field_key"] not in have]
    if lacking and not rec:
        raise LedgerError("新建%s时这些字段必填，草稿里读不出来：%s。在包头写上，或者登记时用 --set <字段名>=<值> 传" % (
            spec.term("wi", wid), "、".join(lacking)))
    owners = lg.role_owners(wid)
    if owners:
        fields.append(owners)
    parent = parent_node(lg, wid, seq, d.rel, source)

    if rec:
        item_id = rec["work_item_id"]
        print(("给 #%s 换研究任务单。" if swap else "接着上次的 #%s 做。") % item_id)
        save_receipt(lg, path, item_id, "created", pairs=pairs,               # values given on --resume too
                     existing=rec.get("existing"), replaced=rec.get("replaced"))
    else:
        created = lg.meegle("workitem", "create", "--work-item-type", lg.api(wid), "--fields",
                            json.dumps(fields, ensure_ascii=False), write=True)
        item_id = created_id(created, spec.term("wi", wid)) if created is not None else "<新工作项 ID>"
        save_receipt(lg, path, item_id, "created", source=source, task=task, pairs=resolved_pairs(lg, pairs))
    title = title.replace("<工作项 ID>", item_id)

    state = {}

    def remember(**kw):
        state.update(kw)
        save_receipt(lg, path, item_id, "created", **kw)

    def create():
        link = lg.item_url(wid, item_id)
        body = link_line(d.body_without_title(), "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id)
        if kind != "feedback":
            body = with_status_sections(body)
        return lg.create_doc(parent, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        if owners and not rec:
            ensure_roles(lg, wid, item_id)
        url = main_document(lg, rec or {}, doc, "节点 %s " % parent, create, remember)
        updates = [{"field_key": doc_key, "field_value": url}]
        if swap:                                    # the task takes the name, sender and sequence of the revision
            mine = {lg.field(wid, s)["field_key"] for s in ("sender", "sequence")}
            updates += [{"field_key": "name", "field_value": name}] + [f for f in fields if f["field_key"] in mine]
        if rec:
            updates += [v for _, v in sets]
        if wid == "same_seq" and d.rel == "execute" and not d.receiver:
            updates.append(lg.entry(wid, "owner_session", "%s-p0-%s" % (spec.term("abbrev", d.sender_seq), item_id)))
        lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields",
                  json.dumps(updates, ensure_ascii=False), write=True)
        lg.meegle("comment", "add", "--work-item-id", item_id, "--content",
                  ("%s 研究任务单换成修订稿：%s。主文档：%s；原来那份：" + (rec.get("replaced") or "无") if swap
                   else "%s 已登记：%s。主文档：%s") % (lg.tag, title, url), write=True)
    except LedgerError as e:
        if state.get("doc_unknown", (rec or {}).get("doc_unknown")) and not (state.get("docx") or state.get("doc")):
            raise WriteMaybeDone("%s\n工作项 #%s 已经建了，建主文档没有拿到确定的结果：先在网页上节点 %s 下核对。建好了的，运行 ledger.py register %s --resume --doc <文档链接> --apply；确认没建成的，运行 ledger.py register %s --resume --doc new --apply" % (
                e, item_id, parent, path, path))
        raise LedgerError("%s\n工作项 #%s 已经建了，主文档字段暂时指着空间首页；修好后运行 ledger.py register %s --resume --apply 接着做，不会再建一个工作项（来源工作项、所属研究任务和 --set 的值已记在回执里）" % (
            e, item_id, path))
    if lg.mode == "apply":
        with open(path, "w", encoding="utf-8") as f:
            f.write(link_line(d.text, "#%s" % item_id, item_id))
        save_receipt(lg, path, item_id, "registered", doc=url)
    if swap:
        print(("换主文档：#%s 的研究任务单换成修订稿，放在节点 %s 下；原来那份留在原处。" if lg.mode == "apply"
               else "预览：会把 #%s 的研究任务单换成修订稿，放在节点 %s 下。") % (item_id, parent))
    else:
        print(("登记：%s #%s，主文档放在节点 %s 下。" if lg.mode == "apply" else "预览：会登记一条%s（#%s），主文档放在节点 %s 下。") % (
            spec.term("wi", wid), item_id, parent))
    after_register(lg, wid, d, item_id)
    return item_id


def after_register(lg, wid, d, item_id):
    """What follows a registration: the sender's own first step is confirmed at once; for the
    rest, the command that comes next is printed."""
    spec = lg.spec
    if lg.mode == "apply":
        try:
            first = first_step_is_the_senders(lg, wid, lg.flow_nodes(item_id))
            if first:
                lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", first["key"],
                          "--action", "confirm", write=True)
                print("“%s”是发出方自己的一步，已随登记确认。" % first["name"])
        except LedgerError as e:
            print("提醒：登记好了，第一步没能确认：%s。接着运行 ledger.py confirm %s --by %s --apply" % (str(e)[:200], item_id, d.author))
    if wid == "decision" and d.header.get("渠道") == spec.term("channel", "researcher"):
        sec = d.section("决定")
        content = re.search(r"决定内容[：:]\s*(.+)", sec)
        decider = re.search(r"决定人[：:]\s*(.+)", sec)
        if content and decider:
            print("研究员主动发起的决策，接着确认：ledger.py confirm %s --by 研究员 --set 决定内容='%s' --set 决定人=研究员"
                  % (item_id, content.group(1).strip()))
        else:
            print("研究员主动发起的决策，但“决定”一节没写全决定内容和决定人：退回作者补上，再确认“决定”这一步。")
    elif wid == "same_seq":
        print("请求方说马上派下一级的（登记请求写了“开工”），接着确认：ledger.py confirm %s --by <承接方的会话名>；排队没派的留在“%s”。" % (
            item_id, spec.term("state", "queued")))
    elif wid in (lg.contract.main_line(), "feedback"):
        print("接着看这一步要什么：ledger.py next %s" % item_id)


def reply(lg, path, doc=None):
    """Hang a 交付包 under the main document of the request it answers and put its link into
    the request's 交付包 field.

    The target field and the parent node are worked out before anything is written; a
    receipt packages/sent/reply-<ID>-<file>.json lets a rerun reuse the document
    instead of creating a second one.
    """
    spec = lg.spec
    d = Draft(spec, path)
    if d.pid != "delivery":
        raise LedgerError("%s要用 register 登记" % d.header["包类型"])
    if not d.linked:
        raise LedgerError("包头的“关联工作项”要写它答复的工作项 ID，比如 #2003")
    parent_id = item_id_of(d.linked[0])
    rpath = receipt_file(lg, ["reply-%s-%s" % (parent_id, safe_name(path))], path)
    rec = (load_json(rpath, {}) or {}) if lg.mode != "offline" else {}
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("status") == "registered":
        raise LedgerError("这份%s已经挂到 #%s 下了（回执 %s），不要重复登记" % (spec.pkg(d.pid), parent_id, os.path.relpath(rpath, lg.root)))
    it = lg.item(parent_id)
    if it is not None and not it.get("type"):
        raise LedgerError("认不出 #%s 的类型（type_key %s）；确认工作项 ID 没写错，或者先跑 setup.py feishu --live --apply 重读类型" % (
            parent_id, it.get("type_key")))
    wid = (it and it.get("type")) or "same_seq"
    if wid not in lg.contract.types_with("fields", "delivery_link"):
        raise LedgerError("交付包答复的是同序列需求或跨序列需求，#%s 是%s" % (parent_id, spec.term("wi", wid)))
    link_key = lg.field(wid, "delivery_link")["field_key"]
    node = lg.node_of(lg.doc_url(parent_id, it))
    if not node:
        raise LedgerError("#%s 的主文档不是知识库文档，答复没处挂" % parent_id)
    title = spec.rules["naming"]["reply_doc"].replace("<答复包名>", spec.pkg(d.pid)) \
        .replace("<作者会话名>", d.author or "?").replace("<同一个任务名>", d.task)

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=os.path.relpath(path, lg.root), parent=parent_id, updated_at=now())
            rec.setdefault("status", "created")
            os.makedirs(sent_dir(lg), exist_ok=True)
            save_json(rpath, rec)

    def create():
        return lg.create_doc(node, title, d.body_without_title(), created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        link = main_document(lg, dict(rec), doc, "#%s 的主文档下" % parent_id, create, remember)
        lg.meegle("workitem", "update", "--work-item-id", parent_id, "--fields", json.dumps(
            [{"field_key": link_key, "field_value": link}]), write=True)
        lg.meegle("comment", "add", "--work-item-id", parent_id, "--content",
                  "%s 收到%s：%s" % (lg.tag, spec.pkg("delivery"), link), write=True)
    except LedgerError as e:
        receipt = os.path.relpath(rpath, lg.root)
        if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
            raise LedgerError("%s\n建文档没有拿到确定的结果（回执 %s）：先在网页上核对 #%s 的主文档下有没有这份%s；建好了的，重跑时加 --doc <文档链接>；确认没建成的，加 --doc new" % (
                e, receipt, parent_id, spec.pkg(d.pid)))
        if rec.get("doc") or rec.get("docx"):
            raise LedgerError("%s\n文档已经建好，回执记在 %s；修好后重跑同一条命令，会接着用这份文档，不会再建一个" % (e, receipt))
        raise LedgerError("%s\n文档还没建，飞书里没有写入；修好后重跑同一条命令即可" % e)
    remember(status="registered")
    print("交付包已挂到 #%s 下，“%s”字段已填。承接方接着请书记员确认这一步：ledger.py confirm %s --by <承接方的会话名>" % (
        parent_id, lg.label(wid, link_key), parent_id))
    return link


def set_value(lg, raw):
    """--set key=@file reads the file (relative to the current directory, then the project);
    @@text keeps a literal @text.

    When no such file exists, the value stops the command if it could be a file name:
    one word, or anything with a /, a .ext ending or a <placeholder>. Only text with a
    space and none of those (@研究员 还没批预算) is kept as it is.
    """
    if raw.startswith("@@"):
        return raw[1:]
    if not raw.startswith("@") or len(raw) == 1:
        return raw
    name = raw[1:]
    full = find_file(lg, name)
    if full:
        return read_text_file(lg, full).strip()
    if not re.search(r"\s", name) or re.search(r"/|<|>|\.[A-Za-z0-9]{1,5}$", name):
        raise LedgerError("找不到文件 %s（--set 的 @文件 先按当前目录、再按项目目录找）；要写以 @ 开头的文字，用 @@" % name)
    return raw


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


def parse_sets(lg, wid, pairs):
    out = []
    for pair in pairs or []:
        if "=" not in pair:
            raise LedgerError("--set 要写成 字段=值：%s" % pair)
        ref, raw = pair.split("=", 1)
        try:
            f = lg.field(wid, ref)
            if f.get("field_type") == "link" and raw.startswith("@") and not raw.startswith("@@") and len(raw) > 1:
                # a link field given a local file: the file becomes a document when the values are written
                full = find_file(lg, raw[1:])
                if not full:
                    raise LedgerError("找不到文件 %s（--set 的 @文件 先按当前目录、再按项目目录找）" % raw[1:])
                out.append((ref, {"field_key": f["field_key"], "field_value": "@" + os.path.relpath(full, lg.root), "upload": full}))
                continue
            out.append((ref, lg.entry(wid, ref, set_value(lg, raw))))
        except (orgspec.OrgSpecError, KeyError) as e:
            raise LedgerError("--set %s：%s（%s有哪些字段见 feishu/space.md）" % (ref, e, lg.spec.term("wi", wid)))
    return out


def attach(lg, item_id, it, wid, key, path):
    """Upload a local file as a document under the work item's main document; return its link.

    This is how a document written by the session that owns a step gets into Feishu without a
    request of its own: the step is the work item behind it. One receipt per file and content
    (packages/sent/attach-<ID>-<field>-<file>-<hash>.json): a rerun with the same content uses
    the document it made, a changed file becomes a new document with a new link, so a field
    frozen by a signature cannot change unnoticed.
    """
    text = read_text_file(lg, path)
    rel, label = os.path.relpath(path, lg.root), lg.label(wid, key)
    if not text.strip():
        raise LedgerError("文件 %s 是空的" % rel)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    rpath = receipt_file(lg, ["attach-%s-%s-%s-%s" % (item_id, safe_name(label), safe_name(path), digest)], path)
    rec = (load_json(rpath, {}) or {}) if lg.mode != "offline" else {}
    rec = rec if isinstance(rec, dict) else {}
    receipt = os.path.relpath(rpath, lg.root)
    if rec.get("status") == "registered" and rec.get("doc"):
        print("%s 没有改过，沿用上次上传的文档：%s" % (rel, rec["doc"]))
        return rec["doc"]
    if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
        raise LedgerError("上次上传 %s 没有拿到确定的结果（回执 %s）。先在网页上看 #%s 的主文档下有没有这份文档：有的话把它的链接直接写进 "
                          "--set %s=<文档链接>；确认没有，删掉这份回执再重跑" % (rel, receipt, item_id, label))
    node = lg.node_of(lg.doc_url(item_id, it))
    if not node:
        raise LedgerError("#%s 的主文档不是知识库文档，“%s”没处挂" % (item_id, label))
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
    title = "%s：%s" % (label, title_of(text) or (it or {}).get("name") or "#%s" % item_id)
    body = "\n".join(l for i, l in enumerate(lines) if i != head).strip() + "\n"

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=rel, parent=str(item_id), field=label, updated_at=now())
            rec.setdefault("status", "created")
            os.makedirs(sent_dir(lg), exist_ok=True)
            save_json(rpath, rec)

    def create():
        return lg.create_doc(node, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        link = main_document(lg, dict(rec), None, "#%s 的主文档下" % item_id, create, remember)
    except WriteMaybeDone as e:
        raise LedgerError("%s\n上传 %s 没有拿到确定的结果（回执 %s）。先在网页上看 #%s 的主文档下有没有这份文档：有的话把它的链接直接写进 "
                          "--set %s=<文档链接>；确认没有，删掉这份回执再重跑" % (e, rel, receipt, item_id, label))
    remember(status="registered")
    if lg.mode == "apply":
        print("已上传“%s”：%s" % (title, link))
    return link


def attach_documents(lg, item_id, it, wid, sets):
    """--set pairs with every local file given for a link field uploaded and replaced by its link."""
    return [(ref, {"field_key": v["field_key"], "field_value": attach(lg, item_id, it, wid, v["field_key"], v["upload"])}
             if "upload" in v else v) for ref, v in sets]


# ------------------------------------------------------------ node flows
def typed_item(lg, item_id, wid_hint=None):
    it = lg.item(item_id)
    wid = (it and it.get("type")) or wid_hint
    if not wid:
        raise LedgerError("读不出 #%s 的类型；用 --type 指明" % item_id)
    return it, wid


def current_node(nodes, item_id, wanted=None):
    """The node an item is at. Flows are straight lines, so one node is in progress."""
    doing = [n for n in nodes if n["status"] == "doing"]
    if wanted:
        hit = next((n for n in nodes if wanted in (n["key"], n["name"])), None)
        if not hit:
            raise LedgerError("#%s 的流程里没有节点“%s”；有：%s" % (item_id, wanted, "、".join("%s（%s）" % (n["name"], n["key"]) for n in nodes)))
        if hit["status"] != "doing":
            raise LedgerError("#%s 的“%s”%s，现在走到的是“%s”" % (
                item_id, hit["name"], NODE_STATUS.get(hit["status"], hit["status"]), "、".join(n["name"] for n in doing) or "流程已经走完"))
        return hit
    if not doing:
        raise LedgerError("#%s 没有进行中的节点：流程已经走完，或者工作项已终止" % item_id)
    if len(doing) > 1:
        raise LedgerError("#%s 有 %d 个节点同时在进行（%s）：用 --node 指明确认哪一个" % (
            item_id, len(doing), "、".join("%s（%s）" % (n["name"], n["key"]) for n in doing)))
    return doing[0]


def is_last(node):
    return not node["next"]


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


def is_scribes(lg, wid, node):
    return lg.contract.role(role_slot(lg, wid, node))["who"] == "scribe"


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


def check_by(lg, wid, it, node, by, what):
    label, allowed = responsible(lg, wid, it, node)
    if is_scribes(lg, wid, node):
        return label
    if not by:
        raise LedgerError("“%s”这一步的负责方是%s：加 --by <提出请求的会话名>；研究员交代的写 --by 研究员" % (node["name"], label))
    if not allowed(by):
        raise LedgerError("“%s”这一步的负责方是%s，%s 不能请书记员%s；退回请求方，让负责的会话来提" % (node["name"], label, by, what))
    return label


def clock(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def check_sign(lg, task_id, decision_id, what, since=None):
    """A signature is a decision that is decided, approved and made for this task. since (ms)
    is when the step it is used on began: a decision made before that was made for an
    earlier step, so one decision passes one gate once."""
    gate = lg.contract.flow["sign_gate"]
    did = item_id_of(decision_id)
    it = lg.item(did)
    if it is None:
        return did
    dtype = lg.spec.term("wi", gate["type"])
    if it.get("type") != gate["type"]:
        raise LedgerError("%s要填一条%s工作项的 ID；#%s 是%s" % (what, dtype, did, lg.spec.term("wi", it["type"]) if it.get("type") else "别的类型"))
    problems = []
    states = [lg.state(gate["type"], s) for s in gate["states"]]
    if it["state"] not in states:
        problems.append("状态是“%s”，要是%s" % (it["state"], "或".join("“%s”" % s for s in states)))
    approved = lg.spec.term("sign", gate["approved"])
    result = lg.text_of(it, gate["result"])
    if result != approved:
        problems.append("%s是%s，要是“%s”" % (lg.spec.field_name(gate["result"]), "“%s”" % result if result else "空的", approved))
    task = (lg.text_of(it, gate["task"]) or "").strip().lstrip("#")
    if task != str(task_id):
        problems.append("%s是%s，不是 #%s" % (lg.spec.field_name(gate["task"]), " #" + task if task else "空的", task_id))
    if since and not problems:
        decided = (lg.flow_nodes(did) or [{}])[0].get("finished_at")
        if decided and decided < since:
            problems.append("它是 %s 决定的，#%s 现在这一步是 %s 开始的；一条决定只用在它决定时所在的那一步，请研究员就现在这一步重新决定" % (
                clock(decided), task_id, clock(since)))
    if problems:
        raise LedgerError("%s用的%s #%s 不合格：%s" % (what, dtype, did, "；".join(problems)))
    return did


def sign_slots(lg, wid, node):
    """[(slot, field_key)] of the signature fields a node's form requires."""
    keys = lg.field_keys.get(wid) or {}
    required = {r["field_key"] for r in node["required"]}
    return [(s, keys[s]) for s in lg.contract.signatures() if keys.get(s) in required]


def raw_text(value):
    """A field value read with `workitem get`, as the operation records write values: a list of strings."""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [x for v in value for x in raw_text(v)]
    if isinstance(value, dict):
        for k in ("value", "timestamp", "id", "user_key"):
            if value.get(k) not in (None, ""):
                return [str(value[k])]
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    return [str(value)]


def frozen_changes(lg, wid, item_id, it, nodes):
    """Changes to the frozen fields after the signing step was last finished, as lines.

    The signing step is the node whose form requires the contract's sealed_by field;
    what it and the nodes before it require is frozen from the moment it was finished.
    Two things are compared. The operation records name every change made since, on
    the web page as well; but a record takes a while to show up. So the value each
    field had at the signing, taken from the records before it, is also compared with
    the value the item has now.
    """
    freeze = lg.contract.flow["freeze"]
    seal = (lg.field_keys.get(wid) or {}).get(freeze["sealed_by"])
    idx = next((i for i, n in enumerate(nodes) if seal in [r["field_key"] for r in n["required"]]), None)
    if seal is None or idx is None or not nodes[idx].get("finished_at"):
        return []
    since = nodes[idx]["finished_at"]
    frozen = {r["field_key"]: r for n in nodes[:idx + 1] for r in n["required"]}
    history = {}
    for rec in sorted(lg.op_records(item_id, "field_mod") or [], key=lambda r: r.get("operation_time") or 0):
        for c in rec.get("record_contents") or []:
            key = (c.get("object") or {}).get("object_value") if isinstance(c, dict) else None
            if key in frozen:
                history.setdefault(key, []).append((rec.get("operation_time") or 0, c))

    def show(values):
        return "、".join(map(str, values or [])) or "空"

    lines = []
    for key, f in frozen.items():
        name = f.get("name") or key
        after = [(t, c) for t, c in history.get(key, []) if t > since]
        before = [(t, c) for t, c in history.get(key, []) if t <= since]
        for t, c in after:
            stamp = datetime.datetime.fromtimestamp(t / 1000).astimezone().isoformat(timespec="seconds")
            lines.append("%s：%s，%s → %s" % (name, stamp, show(c.get("old")), show(c.get("new"))))
        if not after and before and f.get("field_type") != "user":
            signed, current = [str(x) for x in before[-1][1].get("new") or []], raw_text(it["fields"].get(key))
            if signed != current:
                lines.append("%s：签字时是 %s，现在是 %s（操作记录里还没有这次改动）" % (name, show(signed), show(current)))
    return lines


def step_lines(lg, wid, it, node, nodes):
    """What `next` shows for the node an item is at."""
    label, _ = responsible(lg, wid, it, node)
    lines = ["  这一步：%s（%s），负责方：%s" % (node["name"], node["key"], label)]
    for r in node["required"]:
        filled = plain(it["fields"].get(r["field_key"])) is not None or r.get("value") not in (None, "", [])
        slot = next((s for s, k in (lg.field_keys.get(wid) or {}).items() if k == r["field_key"]), None)
        lines.append("    %s %s%s" % ("已填" if filled else "待填", r.get("name") or r["field_key"], "（%s）" % slot if slot else ""))
    if not node["required"]:
        lines.append("    没有必填项")
    for slot, _ in sign_slots(lg, wid, node):
        lines.append("    “%s”填一条研究员批准的决策工作项的 ID，确认前由脚本核对" % lg.spec.field_name(slot))
    after = [n["name"] for n in nodes if n["key"] in node["next"]]
    lines.append("  下一步：%s" % ("、".join(after) if after else "没有了，这是最后一步"))
    return lines


def next_step(lg, item_id):
    """`ledger.py next`: where an item is and what its step still needs. Reads only."""
    it, wid = typed_item(lg, item_id)
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
        cmd = "archive %s" % item_id if is_last(node) else "confirm %s --by <会话名>" % item_id
        lines.append("  书记员推进：ledger.py %s" % cmd)
    reason = lg.text_of(it, "blocked_reason") if "blocked_reason" in lg.contract.slots(wid, "fields") else None
    if reason:
        lines.append("  阻塞中（%s 起）：%s" % ((lg.text_of(it, "blocked_since") or "?")[:10], reason))
    return "\n".join(lines)


def confirm(lg, item_id, by=None, pairs=None, node=None, decision=None, wid_hint=None):
    """Finish the step a work item is at.

    The space says what the step still lacks; --set values are written first. The
    scripts' own checks come before anything is written: who asks, the signature a
    required field refers to, and, before the step that requires the conclusion,
    whether a frozen field was changed after it was signed.
    """
    it, wid = typed_item(lg, item_id, wid_hint)
    spec = lg.spec
    sets = parse_sets(lg, wid, pairs)
    if (lg.field_keys.get(wid) or {}).get("blocked_reason") in [v["field_key"] for _, v in sets]:
        raise LedgerError("阻塞原因用 update 写，不随确认节点写")
    nodes = lg.flow_nodes(item_id)
    if nodes is None:                                   # offline: nothing can be read, show the commands
        if sets:
            sets = attach_documents(lg, item_id, it, wid, sets)
            lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                      json.dumps([v for _, v in sets], ensure_ascii=False), write=True)
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", node or "<当前节点>",
                  "--action", "confirm", write=True)
        return
    cur = current_node(nodes, item_id, node)
    if is_last(cur) and len(nodes) > 1:
        raise LedgerError("“%s”是最后一步，要先把主文档移到归档节点：用 ledger.py archive %s" % (cur["name"], item_id))
    check_by(lg, wid, it, cur, by, "确认这一步")
    given = {v["field_key"]: v["field_value"] for _, v in sets}
    lacking = lg.unfinished(item_id, cur["key"]) or []
    missing = [lg.label(wid, f.get("key")) for f in lacking if f.get("class") == "field" and given.get(f.get("key"), "") == ""]
    missing += [lg.label(wid, k) for k, v in given.items() if v == "" and k in {r["field_key"] for r in cur["required"]}
                and lg.label(wid, k) not in missing]
    other = sorted({str(f.get("class")) for f in lacking if f.get("class") != "field"})
    if missing or other:
        parts = []
        if missing:
            parts.append("要先填：%s（用 --set <字段名>=<值> 传）" % "、".join(missing))
        if other:
            parts.append("空间还要求别的东西（%s），命令行填不了：请研究员在网页上看这个节点" % "、".join(other))
        raise LedgerError("#%s 的“%s”还不能确认。%s" % (item_id, cur["name"], "；".join(parts)))
    for slot, key in sign_slots(lg, wid, cur):
        value = given.get(key) or plain(it["fields"].get(key))
        check_sign(lg, item_id, value, "“%s”" % spec.field_name(slot), cur.get("started_at"))
    freeze = lg.contract.flow["freeze"]
    if (lg.field_keys.get(wid) or {}).get(freeze["checked_before"]) in {r["field_key"] for r in cur["required"]}:
        changed = frozen_changes(lg, wid, item_id, it, nodes)
        if changed and not decision:
            raise LedgerError("#%s 签字以后，冻结的字段被改过：\n  %s\n这一步先不确认。请研究员决定：退回到签字那一步或之前，改完重新签字；"
                              "或者认可这些改动，写成决策包，批准后重跑并加 --decision <决策工作项 ID>" % (item_id, "\n  ".join(changed)))
        if changed:
            check_sign(lg, item_id, decision, "认可冻结字段的改动", cur.get("started_at"))
            print("提醒：冻结的字段在签字后改过（%d 处），研究员已用决策 #%s 认可。" % (len(changed), item_id_of(decision)))
    if sets:
        sets = attach_documents(lg, item_id, it, wid, sets)     # every check has passed: local files become documents now
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([v for _, v in sets], ensure_ascii=False), write=True)
    lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", cur["key"], "--action", "confirm", write=True)
    if lg.mode == "apply":
        after = lg.flow_nodes(item_id)
        done = next(n for n in after if n["key"] == cur["key"])
        if done["status"] != "finished":
            raise LedgerError("确认后回读，“%s”还是%s" % (cur["name"], NODE_STATUS.get(done["status"], done["status"])))
        state = (lg.item(item_id) or {}).get("state")
        doing = [n for n in after if n["status"] == "doing"]
        print("确认：#%s “%s”完成，现在到“%s”，状态“%s”。" % (
            item_id, cur["name"], "、".join(n["name"] for n in doing) or "流程末尾", state))
        for n in doing:                                    # the scribe tells whoever the next step belongs to
            print("“%s”这一步的负责方：%s。" % (n["name"], responsible(lg, wid, it, n)[0]))
    else:
        print("预览：会确认 #%s 的“%s”（%s）。" % (item_id, cur["name"], cur["key"]))


def rollback(lg, item_id, to, reason, by=None, decision=None, wid_hint=None):
    """Send a work item back to a step it has finished.

    Who decides: on the main line the researcher, with an approved decision; elsewhere
    whoever is responsible for the step the item is at (the sender who does not accept
    a delivery, the receiver who hands a request back). When the item only waits for the
    scribe to archive it, whoever is responsible for the step it goes back to. The space
    keeps the values of the undone steps, so their required fields are cleared: the
    steps have to be done again before they can be confirmed again. Fields the new-item
    page requires are the item's own and stay.
    """
    it, wid = typed_item(lg, item_id, wid_hint)
    if not (reason or "").strip():
        raise LedgerError("回滚要写原因：--reason <为什么退回>")
    nodes = lg.flow_nodes(item_id)
    if nodes is None:
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", to, "--action", "rollback",
                  "--rollback-reason", reason, write=True)
        return
    target = next((n for n in nodes if to in (n["key"], n["name"])), None)
    if not target:
        raise LedgerError("#%s 的流程里没有节点“%s”；有：%s" % (item_id, to, "、".join("%s（%s）" % (n["name"], n["key"]) for n in nodes)))
    if target["status"] != "finished":
        raise LedgerError("只能退回到已完成的节点；“%s”%s" % (target["name"], NODE_STATUS.get(target["status"], target["status"])))
    doing = [n for n in nodes if n["status"] == "doing"]
    if wid in lg.contract.flow.get("rollback_needs_decision", []):
        if not decision:
            raise LedgerError("%s的回退由研究员决定：退到哪一步、为什么，写成决策包，研究员批准后重跑并加 --decision <决策工作项 ID>" % lg.spec.term("wi", wid))
        since = max([n.get("started_at") or 0 for n in doing] or [n.get("finished_at") or 0 for n in nodes]) or None
        reason = "%s（研究员决定 #%s）" % (reason.strip(), check_sign(lg, item_id, decision, "回退", since))
    elif decision:
        raise LedgerError("%s的退回不走决策：由这一步的负责方提，加 --by <会话名>；研究员交代的写 --by 研究员" % lg.spec.term("wi", wid))
    elif doing:
        check_by(lg, wid, it, target if is_scribes(lg, wid, doing[0]) else doing[0], by, "回滚")
    elif by != RESEARCHER:
        raise LedgerError("#%s 的流程已经走完，重新打开要研究员交代：加 --by 研究员" % item_id)
    start = nodes.index(target)
    end = max([nodes.index(n) for n in doing] or [len(nodes) - 1])
    kept = {c["field_key"] for c in lg.space_type(wid).get("create_fields") or [] if c.get("required")}
    clear = {}
    for n in nodes[start:end + 1]:
        for r in n["required"]:
            old = plain(it["fields"].get(r["field_key"]))
            if r["field_key"] not in kept and old is not None:
                clear[r["field_key"]] = (r.get("name") or r["field_key"], old)
    lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", target["key"], "--action", "rollback",
              "--rollback-reason", reason, write=True)
    if clear:
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([{"field_key": k, "field_value": ""} for k in clear], ensure_ascii=False), write=True)
    applied = lg.mode == "apply"
    print(("回滚：#%s 退回到“%s”，原因：%s" if applied else "预览：会把 #%s 退回到“%s”，原因：%s") % (item_id, target["name"], reason))
    for name, old in clear.values():
        print("  %s %s，原值：%s" % ("清空" if applied else "会清空", name, old if len(old) <= 120 else old[:120] + "…"))
    if lg.mode == "apply":
        after = lg.flow_nodes(item_id)
        if next(n for n in after if n["key"] == target["key"])["status"] != "doing":
            raise LedgerError("回滚后回读，“%s”不在进行中" % target["name"])
        print("现在状态“%s”。被退回的各步重做后再确认。" % (lg.item(item_id) or {}).get("state"))


def update(lg, item_id, pairs, wid_hint=None):
    """Write field values. 阻塞原因 carries its date: setting it sets 阻塞开始日期 to today
    unless given, clearing it clears both."""
    it, wid = typed_item(lg, item_id, wid_hint)
    sets = parse_sets(lg, wid, pairs)
    keys = lg.field_keys.get(wid) or {}
    by_key = {v["field_key"]: v for _, v in sets}
    reason, since = keys.get("blocked_reason"), keys.get("blocked_since")
    if reason in by_key and since and since not in by_key:
        if by_key[reason]["field_value"] == "":
            sets.append(("blocked_since", {"field_key": since, "field_value": ""}))
        else:
            sets += parse_sets(lg, wid, ["blocked_since=%s" % datetime.date.today().isoformat()])   # local date
    sets = attach_documents(lg, item_id, it, wid, sets)
    lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
              json.dumps([v for _, v in sets], ensure_ascii=False), write=True)


SESSION_TAG = re.compile(r"\s*\[[A-Za-z]+\+[0-2]\]")


def comment(lg, item_id, text):
    if not SESSION_TAG.match(text):
        text = "%s %s" % (lg.tag, text)
    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", text, write=True)


HEADING = re.compile(r'<h([1-9])\b[^>]*?\bid="([^"]+)"[^>]*>(.*?)</h\1>', re.S)


def heading_ids(lg, url):
    """[(block_id, heading text)] from `docs +fetch --scope outline --detail with-ids`."""
    data = lg.lark("docs", "+fetch", "--doc", url, "--scope", "outline", "--max-depth", "2", "--detail", "with-ids")
    if data is None:
        return []
    return [(m.group(2), re.sub(r"<[^>]+>", "", m.group(3)).strip()) for m in HEADING.finditer(doc_content(data))]


def last_block_before(xml, end_id):
    """Id of the last top-level block before end_id in a range fragment.

    --start-block-id and --end-block-id must be siblings. Lists come back as a
    <ul>/<ol> without an id around <li id=...>, so a list ends at its last <li>;
    an <excerpt top-block-id=...> wrapper stands for its top-level block.
    """
    root = ET.fromstring(xml)
    children = list(root)
    idx = next((i for i, c in enumerate(children) if c.get("id") == end_id or c.get("top-block-id") == end_id), None)
    if not idx:
        raise LedgerError("在“状态包”和“交接记录”之间找不到内容块")
    prev = children[idx - 1]
    if prev.get("top-block-id"):
        return prev.get("top-block-id")
    if prev.get("id"):
        return prev.get("id")
    items = [c for c in prev if c.get("id")]
    if prev.tag in ("ul", "ol") and items:
        return items[-1].get("id")
    raise LedgerError("“交接记录”前面那一块（<%s>）没有 block id；请在网页上替换状态包" % prev.tag)


def status_block(lg, item_id, path):
    url = lg.doc_url(item_id)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.lstrip().startswith("## 状态包"):
        content = "## 状态包\n\n" + content
    heads = heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    end_head = next((b for b, t in heads if t == "交接记录"), None)
    if lg.mode == "offline":
        start, end = start or "<状态包标题的 block_id>", "<交接记录前一块的 block_id>"
    else:
        if not start or not end_head:
            raise LedgerError("主文档里找不到“状态包”或“交接记录”标题")
        rng = lg.lark("docs", "+fetch", "--doc", url, "--scope", "range", "--start-block-id", start,
                      "--end-block-id", end_head, "--detail", "with-ids")
        end = last_block_before(doc_content(rng), end_head) if rng is not None else "<交接记录前一块的 block_id>"
    lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", start,
            "--end-block-id", end, "--doc-format", "markdown", "--content", "-", write=True, stdin=content)


def append(lg, item_id, path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    first = next((l for l in content.splitlines() if l.strip()), "")
    if not re.search(r"\[[A-Za-z]+\+[0-2]\]", first):
        raise LedgerError("交接记录第一行要带会话标签，比如 [Alg+1]")
    url = lg.doc_url(item_id)
    lg.lark("docs", "+update", "--doc", url, "--command", "append", "--doc-format", "markdown",
            "--content", "-", write=True, stdin=content)


def archive(lg, item_id, moved=False):
    """Move the main document's subtree to 归档文档/<序列>, then finish the last step of the
    flow. With moved=True the document is already there and only the step is finished."""
    it = lg.item(item_id)
    url = lg.doc_url(item_id, it)
    wid = it.get("type") if it else None
    seq_cn = lg.text_of(it, "sequence")
    seq = next((s for s, cn, _ in lg.spec.sequences() if cn == seq_cn), None)
    if it is not None and not seq:
        raise LedgerError("认不出 #%s 属于哪个序列（“序列”字段是 %s），没法决定归档到哪" % (item_id, seq_cn or "空的"))
    last, archived = None, None
    if it is not None:
        if not wid:
            raise LedgerError("认不出 #%s 的类型（type_key %s）；先跑 setup.py feishu --live --apply 重读类型" % (item_id, it.get("type_key")))
        archived = lg.state(wid, lg.contract.flow["archive_node"]["state"])
        if it["state"] == archived:
            print("#%s 已经是“%s”，不用再归档。" % (item_id, archived))
            return
        nodes = lg.flow_nodes(item_id)
        last = next((n for n in nodes if is_last(n)), None)
        if not last or last["status"] != "doing":
            raise LedgerError("#%s 还没走到最后一步“%s”：现在在“%s”。前面的步骤确认完再归档" % (
                item_id, (last or {}).get("name") or "归档", "、".join(n["name"] for n in nodes if n["status"] == "doing") or "流程之外"))
    key = "archive/%s" % (seq or "<序列>")
    if not moved:
        n = lg.nodes.get(key) if (lg.nodes.get(key) or {}).get("node_token") else None
        if not n and lg.mode != "offline":
            raise LedgerError("ids.json 里没有归档节点 %s；先跑 ensure-node %s" % (key, key))
        node = lg.node_of(url)
        if not node:
            raise LedgerError("#%s 的主文档不是知识库文档，没法移动" % item_id)
        lg.lark("wiki", "+move", "--node-token", node, "--target-parent-token", (n or {}).get("node_token") or "<%s 的节点>" % key,
                write=True)
    try:
        lg.meegle("workflow", "transition", "--work-item-id", str(item_id), "--node-id", last["key"] if last else "<最后一个节点>",
                  "--action", "confirm", write=True)
        if lg.mode == "apply":
            state = (lg.item(item_id) or {}).get("state")
            if state != archived:
                raise LedgerError("确认后回读状态是“%s”，不是“%s”" % (state, archived))
    except LedgerError as e:
        if lg.mode == "apply" and not moved:
            raise LedgerError("%s\n主文档已经移到 %s 下；修好后运行 ledger.py archive %s --moved --apply，只确认最后一步" % (e, key, item_id))
        raise
    if lg.mode != "apply":
        print("预览：会%s确认 #%s 的最后一步。" % ("" if moved else "把主文档整棵子树移到 %s 下，再" % key, item_id))
    elif moved:
        print("归档：#%s 的最后一步已确认（主文档此前已移到 %s 下）。" % (item_id, key))
    else:
        print("归档：#%s 的主文档整棵子树移到 %s 下（移动的是原节点，文档链接不变），最后一步已确认。" % (item_id, key))


LIST_ITEM = re.compile(r'<li\b[^>]*?\bid="([^"]+)"[^>]*>(.*?)</li>', re.S)


def relink(lg, item_id):
    """Put the work item into the 关联工作项 line of its main document, when the document
    was created with a placeholder there. The block is replaced in place: the document
    keeps its identity and link."""
    it = lg.item(item_id)
    url = lg.doc_url(item_id, it)
    data = lg.lark("docs", "+fetch", "--doc", url, "--detail", "with-ids")
    if data is None:
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", "<“关联工作项”那一行的 block_id>",
                "--end-block-id", "<同一个 block_id>", "--doc-format", "markdown", "--content", "-", write=True,
                stdin="- 关联工作项：#%s\n" % item_id)
        return
    hit = None
    for m in LIST_ITEM.finditer(doc_content(data)):
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if re.match(r"关联工作项\s*[：:]", text):
            hit = (m.group(1), text)
            break
    if not hit:
        raise LedgerError("#%s 的主文档开头找不到“关联工作项”一行" % item_id)
    block, text = hit
    link = lg.item_url(it["type"], item_id) if it.get("type") else None
    line = "- " + text
    fixed = link_line(line, "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id)
    if fixed == line:
        print("#%s 的主文档已经写着它的 ID，不用改。" % item_id)
        return
    lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", block, "--end-block-id", block,
            "--doc-format", "markdown", "--content", "-", write=True, stdin=fixed + "\n")
    print("已把 #%s 写进主文档的“关联工作项”一行。" % item_id)


def existing_node(lg, space, parent, key, title):
    """(node_token, title) of a node already in the space for this key: directly under parent
    (the space root when parent is None), titled with the node's name. None when there is
    none; offline it is not looked up."""
    args = ["wiki", "+node-list", "--space-id", space, "--page-all"]
    if parent:
        args += ["--parent-node-token", parent]
    cache = getattr(lg, "_node_listing_cache", None)
    cache_key = (space, parent)
    data = cache.get(cache_key) if cache is not None else None
    if data is None:
        data = lg.lark(*args)
    if data is None:
        return None
    nodes = (data.get("data") or {}).get("nodes") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
    if not isinstance(nodes, list):
        raise LedgerError("wiki +node-list 的输出读不懂，没法确认已有没有这个节点：%s" % json.dumps(data, ensure_ascii=False)[:300])

    def same(t):
        return (t or "").split("：")[0].strip() == title

    hits = [n for n in nodes if isinstance(n, dict) and n.get("node_token") and same(n.get("title"))]
    if len(hits) > 1:
        raise LedgerError("知识空间里已经有 %d 个“%s”节点（%s）；在 feishu/ids.json 的 wiki.nodes.%s 里写明用哪一个" % (
            len(hits), title, "、".join(n["node_token"] for n in hits), key))
    if cache is not None:
        cache[cache_key] = {"data": {"nodes": list(nodes)}}
    return (hits[0]["node_token"], hits[0].get("title")) if hits else None


def ensure_node(lg, key):
    if (lg.nodes.get(key) or {}).get("node_token"):
        print("节点 %s 已有：%s" % (key, lg.nodes[key].get("node_token")))
        return
    head, _, seq = key.partition("/")
    title = lg.spec.term("node", head) if not seq else lg.spec.term("seq", seq)
    space = (lg.ids.get("wiki") or {}).get("space_id")
    if not space and lg.mode != "offline":
        raise LedgerError("ids.json 里没有知识空间 space_id；先建知识空间，再跑 setup.py feishu --live --apply")
    parent = None
    if seq:
        parent = (lg.nodes.get(head) or {}).get("node_token")
        if not parent:
            raise LedgerError("ids.json 里的上一级节点 %s 没有 node_token；先跑 ensure-node %s" % (head, head))
    found = existing_node(lg, space, parent, key, title) if space else None
    if found:                                          # the space already has it: record it, do not create a second
        token, current = found
        record = {"title": current or title, "node_token": token, "obj_token": None}
        out = None
        print("知识空间里已经有节点 %s（%s），直接用，不再建。" % (key, token))
    else:
        args = ["wiki", "+node-create", "--space-id", space or "<space_id>", "--obj-type", "docx",
                "--node-type", "origin", "--title", title]
        if parent:
            args += ["--parent-node-token", parent]
        out = lg.lark(*args, write=True)
        record = None
    if lg.mode == "apply":
        if record is None:
            token = _find(out, ("node_token",))
            if not token:
                raise LedgerError("节点建了，但输出里没有 node_token：%s；去网页上核对，不要重跑" % json.dumps(out, ensure_ascii=False)[:300])
            record = {"title": title, "node_token": token, "obj_token": _find(out, ("obj_token",))}
        path = os.path.join(lg.root, "feishu", "ids.json")
        ids = load_json(path, {})
        ids.setdefault("wiki", {}).setdefault("nodes", {})[key] = record
        save_json(path, ids)
        lg.nodes[key] = record                         # a child created next in the same run finds it
        cache = getattr(lg, "_node_listing_cache", None)
        if cache is not None and out is not None and (space, parent) in cache:
            cache[(space, parent)]["data"]["nodes"].append(record)
        if out is not None:
            print("已建节点 %s：%s，写进 feishu/ids.json。" % (key, record["node_token"]))
        else:
            print("写进 feishu/ids.json。")


def ensure_all_nodes(lg):
    """The whole node skeleton: the top-level nodes first, then the enabled sequences' nodes
    under them. Nodes already recorded, or already in the space under the same title, are kept."""
    manifest = load_json(os.path.join(project_paths.runtime_dir(lg.root), "manifest.json"), {})
    lg._node_listing_cache = {}  # One listing per parent, only within this locked transaction.
    try:
        for key in lg.spec.node_keys(manifest.get("sequences") or None):
            head, _, seq = key.partition("/")
            if seq and not (lg.nodes.get(head) or {}).get("node_token"):
                print("节点 %s 要等上一级 %s 建好：加 --apply 时先建上一级，再建它。" % (key, head))
                continue
            ensure_node(lg, key)
    finally:
        del lg._node_listing_cache


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
                parts = ["`%s` IS NOT NULL" % lg.field(w, "blocked_reason")["field_key"],
                         _not_in([lg.state(w, "archived")] + terminated)]
                out.append(_select(lg, w, " AND ".join(parts + ([of_sequence(w)] if sequence else []))))
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
    return [_select(lg, wid, " AND ".join(parts))]


def _select(lg, wid, cond):
    return "SELECT `work_item_id`, `name`, `work_item_status` FROM `%s`.`%s` WHERE %s" % (lg.key, lg.type_ref(wid), cond)


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


def query(lg, name, sequence=None, as_json=False):
    rows = []
    for mql in mql_text(lg, name, sequence):
        rows += query_all(lg, mql)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print("#%s  %s  %s" % (r.get("work_item_id"), r.get("work_item_status", ""), r.get("name", "")))
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
            text = plain(it["fields"].get(f["field_key"]))
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
    heads = heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    if lg.mode == "offline":
        start = start or "<状态包标题的 block_id>"
    if not start:
        raise LedgerError("主文档里找不到“状态包”标题")
    data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section",
                   "--start-block-id", start)
    if data is not None:
        print(doc_content(data) or json.dumps(data, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="读写智能体组织的台账和任务包文档")
    ap.add_argument("--offline", action="store_true", help="不调用飞书，只打印命令")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("get"); p.add_argument("id"); p.add_argument("--raw", action="store_true")
    p = sub.add_parser("next"); p.add_argument("id")
    p = sub.add_parser("query"); p.add_argument("name"); p.add_argument("--sequence"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("mql"); p.add_argument("name"); p.add_argument("--sequence")
    p = sub.add_parser("trace"); p.add_argument("id")
    p = sub.add_parser("status"); p.add_argument("id")
    for name in ("register", "reply"):
        p = sub.add_parser(name); p.add_argument("draft"); p.add_argument("--apply", action="store_true")
        p.add_argument("--doc", help="上次建文档没有确定结果时：已建好的文档链接，或 new 重新建")
        if name == "register":
            p.add_argument("--source", help="来源工作项的 ID")
            p.add_argument("--task", help="所属研究任务的 ID；空间里只有一条没归档的研究任务时不用写")
            p.add_argument("--set", action="append", help="字段=值；字段写名字、约定里的标识或 field_key")
            p.add_argument("--resume", action="store_true")
            p.add_argument("--existing", metavar="ID",
                           help="研究任务单的修订稿：换成这条研究任务的主文档，工作项不变；它还在第一步时才能换")
    p = sub.add_parser("confirm"); p.add_argument("id")
    p.add_argument("--by", help="请书记员确认这一步的会话名；研究员交代的写 研究员")
    p.add_argument("--set", action="append"); p.add_argument("--node", help="有几个节点同时进行时指明哪一个")
    p.add_argument("--decision", help="冻结的字段在签字后改过时：研究员认可这些改动的决策工作项 ID")
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("rollback"); p.add_argument("id")
    p.add_argument("--to", required=True, help="退回到哪个已完成的节点：节点 ID 或名字")
    p.add_argument("--reason", required=True)
    p.add_argument("--by", help="请书记员回滚的会话名；研究员交代的写 研究员")
    p.add_argument("--decision", help="研究员批准回退的决策工作项 ID；研究任务的回退必填")
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("update"); p.add_argument("id"); p.add_argument("--set", action="append", required=True)
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("comment"); p.add_argument("id"); p.add_argument("--text", required=True); p.add_argument("--apply", action="store_true")
    for name in ("status-block", "append"):
        p = sub.add_parser(name); p.add_argument("id"); p.add_argument("--file", required=True); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("archive"); p.add_argument("id"); p.add_argument("--apply", action="store_true")
    p.add_argument("--moved", action="store_true", help="主文档已经移到归档节点下，只确认最后一步")
    p = sub.add_parser("relink"); p.add_argument("id"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("ensure-node"); p.add_argument("key"); p.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    mode = "offline" if args.offline else ("apply" if getattr(args, "apply", False) else "preview")
    lg = Ledger(project_root(), mode)
    m = lg.ids.get("meegle") or {}
    if mode != "offline" and not (m.get("bound_at") and m.get("types")):  # a binding time with no type is no binding
        print("出错：这个项目还没绑定飞书空间。先跑 setup.py feishu --target <项目目录> --live 看绑定结果，核对后加 --apply；"
              "绑定以前台账脚本不读也不写飞书。")
        return 1
    if mode != "offline" and lg.ids.get("schema") != space.IDS_SCHEMA:
        print("出错：feishu/ids.json 是旧版的绑定（%s）。这一版只认用节点流程的空间：照 setup 技能用模板建空间，"
              "再跑 setup.py feishu --target <项目目录> --live --apply 重新绑定。" % (lg.ids.get("schema") or "没有 schema"))
        return 1
    moved = other_space(load_json(os.path.join(lg.root, "spec", "task.json"), {}), m)
    if mode != "offline" and moved:
        print("出错：spec/task.json 里的飞书项目空间是“%s”，绑定的是“%s”。重跑 setup.py feishu --target <项目目录> --live 核对，"
              "再加 --apply；在这以前台账脚本不读也不写飞书。" % (moved, m["space_name"]))
        return 1
    lock = write_lock(lg.root) if mode == "apply" else contextlib.nullcontext()
    try:
        with lock:
            if mode == "apply" and os.path.isfile(os.path.join(lg.root, ".agent-org", "manifest.json")):
                import codex_runtime
                try:
                    codex_runtime.assert_project_integrity(lg.root)
                except codex_runtime.RuntimeError as e:
                    raise LedgerError(str(e))
            run_command(lg, args)
    except (LedgerError, orgspec.OrgSpecError, space.SpaceError, ET.ParseError, OSError) as e:
        print("出错：%s" % e)
        return 1
    if mode == "preview" and lg.planned:
        print("这是预览，没有写飞书。核对后加 --apply 执行。")
    return 0


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
        print("\n".join(mql_text(lg, args.name, args.sequence)))
    elif args.cmd == "trace":
        trace(lg, args.id)
    elif args.cmd == "status":
        status(lg, args.id)
    elif args.cmd == "register":
        register(lg, args.draft, args.source, pairs=args.set, resume=args.resume, doc=args.doc, task=args.task,
                 existing=args.existing)
    elif args.cmd == "reply":
        reply(lg, args.draft, args.doc)
    elif args.cmd == "confirm":
        confirm(lg, args.id, args.by, args.set, args.node, args.decision, args.type)
    elif args.cmd == "rollback":
        rollback(lg, args.id, args.to, args.reason, args.by, args.decision, args.type)
    elif args.cmd == "update":
        update(lg, args.id, args.set, args.type)
    elif args.cmd == "comment":
        comment(lg, args.id, args.text)
    elif args.cmd == "status-block":
        status_block(lg, args.id, args.file)
    elif args.cmd == "append":
        append(lg, args.id, args.file)
    elif args.cmd == "archive":
        archive(lg, args.id, args.moved)
    elif args.cmd == "relink":
        relink(lg, args.id)
    elif args.cmd == "ensure-node":
        if args.key == "all":
            ensure_all_nodes(lg)
        else:
            ensure_node(lg, args.key)


if __name__ == "__main__":
    sys.exit(main())
