#!/usr/bin/env python3
"""Read and write the task ledger (Feishu Project) and package documents (Feishu Docs).

Read commands, for every session:
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

Write commands, for the scribe only (the guard hook enforces this). They print the
commands first; nothing is written without --apply:
    ledger.py register <draft> [--task ID] [--source ID] [--set f=v ...] [--resume]
                                            需求包 / 决策包 / 研究任务单 / 反馈: work item + main document
    ledger.py revise <ID> <draft> --note N  the next version of a 需求包 or 决策包: the main document's
                                            opening up to 状态包 replaced in place, a line in 交接记录
    ledger.py reply <draft>                 交付包: document under the request it answers, 交付包 field;
                                            handed in again after changes, its version one higher
    ledger.py confirm <ID> --by <会话名> [--set f=v ...] [--option 选项] [--executor 会话名]
                                            finish the step the item is at; on a decision's 决定 step,
                                            --option records the option picked and --executor hands the
                                            decision to a session in place of the package header's 执行方
    ledger.py confirm <ID> --by 到期默认    a reversible decision past its expiry time takes its default
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
Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import glob
import hashlib
import html
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
        self.tag = os.environ.get("SOLO_LAB_TAG", "[PMO+0]")
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
NOTED_PLACEHOLDER = re.compile(r"(待建|待登记)\s*[（(〔\[].*[）)〕\]]")
EXISTING_GONE = ("register --existing 停用了：研究任务单和包一样原位修订，文档链接不变。运行 "
                 "ledger.py revise %s <修订稿> --note '<改了什么>' --apply")
DRAFT_CHANGED = ("草稿在命令运行期间改过：进主文档的是命令开始时的内容，之后的改动留在草稿里，没有进主文档；"
                 "要进主文档，改好后走修订（ledger.py revise）。")


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
# .claude/state/gates.json (.agent-org/state/gates.json in a Codex project), next to the claims table and
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
BY_DEFAULT = "到期默认"
PRUNE_AFTER = datetime.timedelta(minutes=10)


def gates_path(root):
    base = ".agent-org" if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")) else ".claude"
    return os.path.join(root, base, "state", "gates.json")


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


def hours_text(hours):
    hours = round(float(hours), 1)
    return "%s 小时" % (int(hours) if hours.is_integer() else hours)


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


def decision_options(lg, item_id):
    """A decision's options as registered or revised: the local decision state first, then the receipt.
    None when neither has them (registered before 1.0.4)."""
    entry = load_gates(lg.root)["pending"].get(str(item_id))
    if entry and entry.get("options") is not None:
        return entry["options"]
    return receipt_of(lg, item_id).get("options")


def chosen_option(options, option, item_id):
    """The option --option names, checked against the decision's options table; {"name": "无"} when the
    researcher picked none of them; None when --option is left out. The option is a record only: it goes
    into the receipt and a comment, and nothing is released by it."""
    if option is None:
        return None
    option = option.strip()
    if option == orgspec.NOTHING:
        return {"name": orgspec.NOTHING, "approach": "", "irreversible": []}
    hit = next((o for o in options or [] if o.get("name") == option), None)
    if hit:
        return hit
    if options is None:
        raise LedgerError("#%s 没有记下选项表（包里的选项写的是“无”，或者是 1.0.4 以前登记的）：不加 --option，研究员定的内容写进 --set 决定内容"
                          % item_id)
    raise LedgerError("#%s 的选项里没有“%s”，有：%s；研究员没有选表里的任何一个，写 --option 无，或者不加 --option" % (
        item_id, option, "、".join(o.get("name", "") for o in options)))


def changed_executor(lg, value, item_id):
    """The session `confirm --executor` hands a decision to, under the package header's own check
    (orgspec.read_executor): a sequence's +2, the entry or the router. 研究员 is refused: what the researcher
    is to do by hand has to be in the package, so that takes a new version."""
    if value is None:
        return None
    who, why = orgspec.read_executor(lg.spec, value)
    if who == RESEARCHER:
        raise LedgerError("--executor 只收会话名：要把 #%s 的执行方改成研究员，请发出方写下一版（包头“%s”写“研究员”，“%s”写清楚要他做什么），"
                          "书记员用 ledger.py revise 换主文档以后再确认" % (item_id, orgspec.EXECUTOR, orgspec.RESEARCHER_ACTION))
    if not who:
        raise LedgerError("--executor 写的是“%s”：%s" % (value, why))
    return who


def settle(lg, item_id, choice, by_default=None, executor=None):
    """After 决定 is confirmed: the decision leaves the pending decisions, and its receipt keeps how it was
    decided: the option picked (when --option named one, or 到期默认 took the default), who decided (研究员 or
    到期默认) and when. executor (--executor) becomes the receipt's executor, with researcher_action null;
    as_packaged keeps what the package header said, for a rollback to 待决定 to put back."""
    if lg.mode != "apply":
        return
    save_gates(lg, lambda gates: gates["pending"].pop(str(item_id), None), keep=item_id)
    values = {"option": (choice or {}).get("name"), "decided_by": BY_DEFAULT if by_default else RESEARCHER, "decided_at": now()}
    rec = receipt_of(lg, item_id)
    if executor and executor != rec.get("executor"):
        packaged = rec.get("as_packaged") or {"executor": rec.get("executor"), "researcher_action": rec.get("researcher_action")}
        values.update(executor=executor, researcher_action=None, as_packaged=packaged)
    update_receipt(lg, item_id, **values)


def hold(lg, item_id, kinds, registered_at=None, rolled_back_at=None):
    """Put a decision from the decision queue into the local decision state (or refresh it), keeping the
    registration time it already has, and that it was rolled back to 待决定."""
    def change(gates):
        old = gates["pending"].get(str(item_id)) or {}
        gates["pending"][str(item_id)] = pending_entry(item_id, kinds, old.get("registered_at") or registered_at or now(),
                                                       old.get("rolled_back_at") or rolled_back_at)
    return save_gates(lg, change, keep=item_id)


def drop_pending(lg, item_id):
    """Take a decision out of the pending decisions (archived, terminated); True when it was there."""
    if lg.mode != "apply" or str(item_id) not in load_gates(lg.root)["pending"]:
        return False
    save_gates(lg, lambda gates: gates["pending"].pop(str(item_id), None), keep=item_id)
    return True


def finished(lg, state, wid):
    """Is this the state of a work item no session answers for any more: archived, or terminated on the web page?"""
    return bool(state) and (state in system_states(lg.spec) or bool(wid) and state == (lg.states.get(wid, {}).get("archived") or {}).get("name"))


def drop_owner(lg, item_id):
    """Take a finished work item out of the pool's table of who answers for it (pool.json owners), so that
    `route.py pool` stops listing it under a session; True when it was there. The table is written by the inbox
    hook when a session is handed the item and by nothing else, so until 1.0.8 it only grew. A project without
    a pool, or a pool.json that cannot be read, stops no archive: route.py pool --prune takes it out later."""
    if lg.mode != "apply":
        return False
    try:
        data, ok = pool.load(lg.root)
        if not ok or str(item_id) not in (data.get("owners") or {}):
            return False
        with pool.locked(lg.root) as held:
            return held.get("owners", {}).pop(str(item_id), None) is not None
    except (pool.PoolError, OSError):
        return False


def owner_note(lg, item_id):
    return "会话池的认领表里移除了 #%s。" % item_id if drop_owner(lg, item_id) else ""



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



# A research task sheet's 资源上限 names the conditions a paid run depends on: 执行环境 and 计算资源 say
# 待研究员确认, or name the decision that set them. Choosing them meets criterion 4 of the list of
# irreversible actions, so they go into the decision queue, exploratory tasks included.
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
                     "试探任务也一样；定了以后把决策 ID 写进这一行，比如“%s（决策 #<ID>）”，没定之前写“待研究员确认”。" % (row, value, row, value))
    return hints


def register(lg, path, source=None, pairs=None, resume=False, doc=None, task=None):
    """Register a draft: a work item with the draft as its main document.

    需求包 → 同序列需求 or 跨序列需求; 决策包 → 决策; 研究任务单 → 研究任务; 反馈 → 反馈.
    Everything that can fail is checked before the work item exists: the header, the
    fields the new-item page requires, the parent node. The item is created with a
    link to the space as its main document, since the page requires one; the document
    is created next, with the item's ID in its title and header, and written back. A decision's
    执行方 and 研究员要做的事 go into the fields of those names where the space has them (header_fields).
    A sheet or a package that is registered already takes its next version through revise, which keeps the
    document and its link (until 1.0.9 a revised 研究任务单 became a new document: register --existing).
    """
    spec = lg.spec
    kind = sheet_kind(path)
    d = Sheet(spec, path, kind) if kind else Draft(spec, path)
    wid = kind or work_item_type(d)
    if not d.author:
        raise LedgerError("发出方要带会话名，比如“算法 +2（Alg-p2-01）”")
    if not kind and d.version != 1:
        raise LedgerError("登记的是新工作项，包头“版本”写第 1 版，现在是第 %s 版：已经登记过的包改了内容，用 ledger.py revise <工作项 ID> <新草稿> 换主文档" % d.version)
    if kind == "research_task":
        for line in run_conditions_hints(d):
            print(line)
    rec = check_receipt(lg, path, resume or bool(doc) and bool(find_receipt(lg, path))) if lg.mode != "offline" else None
    if rec and rec.get("existing"):
        raise LedgerError(EXISTING_GONE % rec.get("work_item_id"))
    if rec:
        source, task = source or rec.get("source"), task or rec.get("task")
        pairs = merge_pairs(rec.get("pairs"), resolved_pairs(lg, pairs))   # rewritten with the main document
    source = item_id_of(source or d.source) if (source or d.source) else None
    link_line(d.text, "#0", "0")                      # a 关联工作项 line that is no placeholder stops here
    name, title = naming(spec, d, kind)

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
        required += [("relation", spec.term("rel", d.rel)), ("owner_session", d.receiver or "待委托")]
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
    fields += header_fields(lg, wid, d, {v["field_key"] for _, v in sets})
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

    kinds = receipt_kind(spec, wid, d, pairs)
    if rec:
        item_id = rec["work_item_id"]
        print("接着上次的 #%s 做。" % item_id)
        save_receipt(lg, path, item_id, "created", kinds, pairs=pairs)        # values given on --resume too
    else:
        created = lg.meegle("workitem", "create", "--work-item-type", lg.api(wid), "--fields",
                            json.dumps(fields, ensure_ascii=False), write=True)
        item_id = created_id(created, spec.term("wi", wid)) if created is not None else "<新工作项 ID>"
        save_receipt(lg, path, item_id, "created", kinds, source=source, task=task, pairs=resolved_pairs(lg, pairs),
                     registered_at=now())
    if wid == "decision" and kinds.get("queue"):      # frozen from the moment the work item exists
        hold(lg, item_id, kinds, (rec or {}).get("registered_at") or receipt_of(lg, item_id).get("registered_at"))
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
        if rec:
            updates += [v for _, v in sets]
        if wid == "same_seq" and d.rel == "execute" and not d.receiver:
            updates.append(lg.entry(wid, "owner_session", "%s-p0-%s" % (spec.term("abbrev", d.sender_seq), item_id)))
        lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields",
                  json.dumps(updates, ensure_ascii=False), write=True)
        lg.meegle("comment", "add", "--work-item-id", item_id, "--content",
                  "%s 已登记：%s。主文档：%s" % (lg.tag, title, url), write=True)
    except LedgerError as e:
        if state.get("doc_unknown", (rec or {}).get("doc_unknown")) and not (state.get("docx") or state.get("doc")):
            raise WriteMaybeDone("%s\n工作项 #%s 已经建了，建主文档没有拿到确定的结果：先在网页上节点 %s 下核对。建好了的，运行 ledger.py register %s --resume --doc <文档链接> --apply；确认没建成的，运行 ledger.py register %s --resume --doc new --apply" % (
                e, item_id, parent, path, path))
        raise LedgerError("%s\n工作项 #%s 已经建了，主文档字段暂时指着空间首页；修好后运行 ledger.py register %s --resume --apply 接着做，不会再建一个工作项（来源工作项、所属研究任务和 --set 的值已记在回执里）" % (
            e, item_id, path))
    drifted = ""
    if lg.mode == "apply":
        drifted = write_back(path, d.text, item_id)
        save_receipt(lg, path, item_id, "registered", doc=url, draft_changed=True if drifted else None)
    print(("登记：%s #%s，主文档放在节点 %s 下。" if lg.mode == "apply" else "预览：会登记一条%s（#%s），主文档放在节点 %s 下。") % (
        spec.term("wi", wid), item_id, parent))
    if drifted:
        print("提醒：" + drifted)
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
        print("请求方说马上委托下一级的（登记请求写了“开工”），接着确认：ledger.py confirm %s --by <承接方的会话名>；排队还没委托的留在“%s”。" % (
            item_id, spec.term("state", "queued")))
    elif wid in (lg.contract.main_line(), "feedback"):
        print("接着看这一步要什么：ledger.py next %s" % item_id)


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


def reply(lg, path, doc=None):
    """Hang a 交付包 under the main document of the request it answers and put its link into
    the request's 交付包 field.

    The target field and the parent node are worked out before anything is written; a
    receipt packages/sent/reply-<ID>-<file>.json lets a rerun reuse the document
    instead of creating a second one. The receipt keeps the version handed in. A delivery
    changed after one that was not accepted is handed in again, from the same file or another:
    its version is the last one plus one, it becomes a new document (the earlier one stays
    where it is) and the 交付包 field takes its link; the same version again, or a number that
    does not follow, stops with the number to write.
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
    earlier = delivered(lg, parent_id) if lg.mode != "offline" else []
    again = bool(earlier or rec.get("earlier"))        # not the first delivery of this request
    if earlier and rec.get("status") != "created":     # handed in again; an unfinished run of this one just goes on
        last, receipt = max(v or 1 for v, _ in earlier), earlier[-1][1]
        how = "验收不通过、改好后重新交的，包头“%s”写第 %d 版，再 reply" % (orgspec.VERSION, last + 1)
        if rec.get("status") == "registered" and d.version == (rec.get("version") or 1):
            raise LedgerError("这份%s（第 %d 版）已经挂到 #%s 下了（回执 %s），不要重复登记；%s" % (
                spec.pkg(d.pid), d.version, parent_id, os.path.relpath(rpath, lg.root), how))
        follows = d.version == last + 1 if all(v for v, _ in earlier) else d.version > last   # an old receipt: its number is not known
        if not follows:
            raise LedgerError("#%s 已经收过第 %d 版%s（回执 %s），这一份写的是第 %d 版：%s" % (
                parent_id, last, spec.pkg(d.pid), receipt, d.version, how))
    it = lg.item(parent_id)
    if it is not None and not it.get("type"):
        raise LedgerError("认不出 #%s 的类型（type_key %s）；确认工作项 ID 没写错，或者先跑 setup.py feishu --live --apply 重读类型" % (
            parent_id, it.get("type_key")))
    wid = (it and it.get("type")) or "same_seq"
    if wid not in lg.contract.types_with("fields", "delivery_link"):
        raise LedgerError("交付包答复的是同序列需求或跨序列需求，#%s 是%s" % (parent_id, spec.term("wi", wid)))
    link_key = lg.field(wid, "delivery_link")["field_key"]
    if again and it is not None and it["state"] in closed_states(lg):
        raise LedgerError("#%s 已经是“%s”：工作项关闭以后不再重新交%s" % (parent_id, it["state"], spec.pkg(d.pid)))
    node = lg.node_of(lg.doc_url(parent_id, it))
    if not node:
        raise LedgerError("#%s 的主文档不是知识库文档，答复没处挂" % parent_id)
    title = spec.rules["naming"]["reply_doc"].replace("<答复包名>", spec.pkg(d.pid)) \
        .replace("<作者会话名>", d.author or "?").replace("<同一个任务名>", d.task)
    if rec.get("status") == "registered":             # this file handed in again: a new document, the earlier one stays
        rec = {"earlier": list(rec.get("earlier") or []) + [{"version": rec.get("version") or 1, "doc": rec.get("doc")}]}

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=os.path.relpath(path, lg.root), parent=parent_id, version=d.version, updated_at=now())
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
                  "%s 收到%s%s：%s" % (lg.tag, spec.pkg("delivery"), "第 %d 版" % d.version if again else "", link), write=True)
    except LedgerError as e:
        receipt = os.path.relpath(rpath, lg.root)
        if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
            raise LedgerError("%s\n建文档没有拿到确定的结果（回执 %s）：先在网页上核对 #%s 的主文档下有没有这份%s；建好了的，重跑时加 --doc <文档链接>；确认没建成的，加 --doc new" % (
                e, receipt, parent_id, spec.pkg(d.pid)))
        if rec.get("doc") or rec.get("docx"):
            raise LedgerError("%s\n文档已经建好，回执记在 %s；修好后重跑同一条命令，会接着用这份文档，不会再建一个" % (e, receipt))
        raise LedgerError("%s\n文档还没建，飞书里没有写入；修好后重跑同一条命令即可" % e)
    remember(status="registered")
    print("交付包%s已挂到 #%s 下，“%s”字段已填%s。承接方接着请书记员确认这一步：ledger.py confirm %s --by <承接方的会话名>" % (
        "第 %d 版" % d.version if again else "", parent_id, lg.label(wid, link_key),
        "，上一版的文档留在原处" if again else "", parent_id))
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


def confirm(lg, item_id, by=None, pairs=None, node=None, decision=None, wid_hint=None, option=None, executor=None):
    """Finish the step a work item is at.

    The space says what the step still lacks; --set values are written first. The
    scripts' own checks come before anything is written: who asks, the signature a
    required field refers to, and, before the step that requires the conclusion,
    whether a frozen field was changed after it was signed.
    On a decision's 决定 step two things may be said, both optional. option is the option the
    researcher picked (无 for none of the table): a record, kept in the receipt and a comment;
    nothing is released by it. executor hands the decision to a session in place of the package
    header's 执行方, for when the header says 研究员 and the researcher picked an option that needs
    nothing done by hand: the receipt's executor becomes the session and its researcher_action null,
    the space's two fields follow where it has them, and from then on the decision goes as one a
    session carries out. by=到期默认 confirms a reversible decision that waited the expiry time with
    the option its package names (confirm_by_default).
    """
    if by == BY_DEFAULT:
        if pairs or option or decision or executor:
            raise LedgerError("--by 到期默认 的决定内容和决定人由脚本照包里写的到期默认动作写：去掉 --set、--option、--executor 和 --decision")
        return confirm_by_default(lg, item_id, node)
    return confirm_step(lg, item_id, by, pairs, node, decision, wid_hint, option, executor=executor)


def confirm_by_default(lg, item_id, node=None):
    """`confirm <ID> --by 到期默认`: record the decision a reversible decision's package names as its
    default once it has waited the expiry time since registration. Only when all of these hold: it is
    still 待决定, it is in the local decision state, a session carries it out with nothing for the
    researcher to do by hand, it is reversible, the researcher never decided it before (one rolled back
    to 待决定 waits for the researcher, however long), the expiry time has passed, and its 到期默认动作
    names an option. 决定人 is the researcher (the field takes a person); 决定内容 starts with 到期默认：."""
    it, wid = typed_item(lg, item_id, "decision")
    if it is None:
        raise LedgerError("到期默认要先读这条决策现在的状态，离线做不了")
    if wid != "decision":
        raise LedgerError("--by 到期默认 只用在决策上：#%s 是%s" % (item_id, lg.spec.term("wi", wid)))
    pending = lg.state("decision", "pending")
    if it["state"] != pending:
        raise LedgerError("#%s 现在是“%s”，不在“%s”：到期默认只给还没定的决策" % (item_id, it["state"], pending))
    entry = load_gates(lg.root)["pending"].get(str(item_id))
    if not entry:
        raise LedgerError("本地决策状态里没有 #%s：它不是决策队列来的，或者是 1.0.4 以前登记的。到期默认要照登记时写下的可逆性和到期默认动作办："
                          "请发出方按新模板写下一版，书记员 revise 以后再看" % item_id)
    if entry.get("executor") == RESEARCHER or entry.get("researcher_action"):
        raise LedgerError("#%s 的执行方是研究员（或者要研究员亲手做事），到期继续等研究员，不走到期默认" % item_id)
    if entry.get("reversible") is not True:
        raise LedgerError("#%s 是不可逆的决策（判据 %s），到期只能等研究员，不走到期默认" % (
            item_id, "、".join(str(n) for n in entry.get("criteria") or []) or "?"))
    back = pool.rolled_back(entry)
    if back:
        at = parse_stamp(back)
        raise LedgerError("#%s 定过一次，又回滚到了待决定（%s）：不再走到期默认，一直等研究员，他来谈时排在清单最前" % (
            item_id, at.strftime("%Y-%m-%d %H:%M") if at else back))
    hours, source, note = expiry_hours(lg.root)
    if note:
        print(note)
    due = pool.expires_at(entry, hours)
    if not due:
        raise LedgerError("本地决策状态里 #%s 的登记时间读不出来，没法算到期：请书记员 revise 一次重新记下" % item_id)
    if now_at() < due:
        waited = (now_at() - due).total_seconds() / 3600 + hours
        raise LedgerError("#%s 登记了 %s，还没满到期时长 %s（%s）：%s 以后才能按到期默认确认" % (
            item_id, hours_text(waited), hours_text(hours),
            "org/decisions.md" if source != "默认" else "默认值", due.strftime("%Y-%m-%d %H:%M")))
    option = entry.get("default_option")
    picked = next((o for o in entry.get("options") or [] if o.get("name") == option), None)
    if not picked:
        raise LedgerError("#%s 的到期默认动作是“%s”，没有指向选项表里的选项：到期继续等研究员" % (item_id, entry.get("default") or "空的"))
    content = "到期默认：%s%s（登记后 %s内研究员没有决定）" % (option, "：" + picked["approach"] if picked.get("approach") else "",
                                                 hours_text(hours))
    confirm_step(lg, item_id, RESEARCHER, ["决定内容=" + content, "决定人=" + RESEARCHER], node, None, "decision",
                 by_default=(content, picked))
    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, content), write=True)


def decided_note(choice, executor=None, packaged=None):
    """What a comment records about how 决定 was confirmed: the option picked with the irreversible actions it
    lists (choice, from --option), and the session the decision was handed to in place of the package header's
    执行方 (executor, from --executor; packaged is what the header said). "" when neither was said."""
    parts = []
    if choice and choice.get("name") == orgspec.NOTHING:
        parts.append("研究员没有选选项表里的选项")
    elif choice:
        listed = "；".join(a.get("text") or a.get("name") or "" for a in orgspec.option_actions(choice))
        parts.append("研究员选的选项：%s%s" % (choice["name"], "（%s：%s）" % (orgspec.IRREVERSIBLE_COLUMN, listed) if listed else ""))
    if executor:
        parts.append("%s改成 %s（包头写的是%s），“%s”记“%s”" % (
            orgspec.EXECUTOR, executor, packaged or "空的", orgspec.RESEARCHER_ACTION, orgspec.NOTHING))
    return "；".join(parts)


def confirm_step(lg, item_id, by=None, pairs=None, node=None, decision=None, wid_hint=None, option=None, by_default=None,
                 executor=None):
    it, wid = typed_item(lg, item_id, wid_hint)
    spec = lg.spec
    sets = parse_sets(lg, wid, pairs)
    pending = ((lg.states.get("decision") or {}).get("pending") or {}).get("name")
    deciding = wid == "decision" and it is not None and bool(pending) and it.get("state") == pending
    if it is not None and not deciding:                 # offline nothing can be read: commands only
        for flag, value, how in (("--option", option, "选的选项用 ledger.py comment 记一条评论"),
                                 ("--executor", executor, "要改执行方，先 ledger.py rollback %s --to 决定 --reason '<原因>' --by 研究员，"
                                                          "再带 --executor 确认“决定”" % item_id)):
            if value is not None:
                raise LedgerError("%s 只用在确认决策的“决定”这一步：#%s 是%s，现在是“%s”%s" % (
                    flag, item_id, spec.term("wi", wid), it["state"],
                    "；这一步已经确认过（比如研究员在网页上定的），" + how if wid == "decision" else ""))
    choice = chosen_option(decision_options(lg, item_id), option, item_id) if deciding and not by_default else None
    if by_default:
        choice = by_default[1]
    who = changed_executor(lg, executor, item_id)
    rec = receipt_of(lg, item_id) if deciding else {}
    packaged = carried_out_by(rec, lg.text_of(it, "sender")) if deciding else None
    if who and who == rec.get("executor"):
        who = None                                      # the package header's own 执行方: nothing changes
    handed = executor_fields(lg, wid, who, orgspec.NOTHING) if who else []
    if {f["field_key"] for f in handed} & {v["field_key"] for _, v in sets}:
        raise LedgerError("%s用 --executor 改：去掉 --set 里的“%s”和“%s”" % (orgspec.EXECUTOR, orgspec.EXECUTOR, orgspec.RESEARCHER_ACTION))
    note = decided_note(choice, who, packaged) if not by_default else ""
    if (lg.field_keys.get(wid) or {}).get("blocked_reason") in [v["field_key"] for _, v in sets]:
        raise LedgerError("阻塞原因用 update 写，不随确认节点写")
    nodes = lg.flow_nodes(item_id)
    if nodes is None:                                   # offline: nothing can be read, show the commands
        if sets or handed:
            sets = attach_documents(lg, item_id, it, wid, sets)
            lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                      json.dumps([v for _, v in sets] + handed, ensure_ascii=False), write=True)
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
    if sets or handed:
        sets = attach_documents(lg, item_id, it, wid, sets)     # every check has passed: local files become documents now
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([v for _, v in sets] + handed, ensure_ascii=False), write=True)
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
        if deciding:
            settle(lg, item_id, choice, by_default and by_default[0], who)
            if by_default:
                print("到期默认：照选项“%s”办，执行完请书记员归档。" % choice["name"])
            if note:                                       # the record of the choice: the receipt has it, a comment says it
                kept = "回执" if rec else ""                 # registered elsewhere: no receipt here
                try:
                    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, note), write=True)
                    print("%s。已记进%s评论。" % (note, kept + "和" if kept else ""))
                except LedgerError as e:
                    print("%s。%s评论没写上（%s），用 ledger.py comment %s --text '%s' 补。" % (
                        note, "已记进%s；" % kept if kept else "", str(e)[:160], item_id, note))
        if wid == "decision" and state == lg.state(wid, "decided"):   # and the decision's 执行方 what was decided
            carried = who or carried_out_by(receipt_of(lg, item_id), lg.text_of(it, "sender"))
            if carried:
                print("执行方：%s。" % carried)
    else:
        print("预览：会确认 #%s 的“%s”（%s）。" % (item_id, cur["name"], cur["key"]))
        if note:
            lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", "%s %s" % (lg.tag, note), write=True)


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
        state = (lg.item(item_id) or {}).get("state")
        print("现在状态“%s”。被退回的各步重做后再确认。" % state)
        if wid == "decision" and state == ((lg.states.get("decision") or {}).get("pending") or {}).get("name"):
            reopen(lg, item_id)


def reopen(lg, item_id):
    """A decision back at 待决定. Its receipt forgets how it was decided and keeps when it came back
    (rolled_back_at); a 执行方 the confirmation had changed (confirm --executor) goes back to what the package
    header says, in the receipt and in the space's two fields. One from the decision queue waits again in
    the local decision state, with its options and frozen actions from the receipt, marked rolled_back_at:
    it was decided once, so it takes no default from now on and waits for the researcher (its expiry still
    counts from its registration, for the entry's list)."""
    rec = receipt_of(lg, item_id)
    stamp = now()
    values = {"rolled_back_at": stamp, "option": None, "decided_by": None, "decided_at": None}
    packaged = rec.get("as_packaged") if isinstance(rec.get("as_packaged"), dict) else None
    if packaged:
        values.update(executor=packaged.get("executor"), researcher_action=packaged.get("researcher_action"), as_packaged=None)
    queued = bool(rec.get("queue")) and rec.get("options") is not None
    if queued:
        def change(gates):
            old = gates["pending"].get(str(item_id)) or {}
            gates["pending"][str(item_id)] = pending_entry(
                item_id, dict(rec, **values), old.get("registered_at") or rec.get("registered_at") or stamp, stamp)
        save_gates(lg, change, keep=item_id)
    update_receipt(lg, item_id, **values)
    if queued:
        print("本地决策状态：#%s 回到待决定，冻结的动作重新生效；它定过一次，不再走到期默认，一直等研究员。" % item_id)
    if packaged:
        back = "%s改回包头写的%s" % (orgspec.EXECUTOR, packaged.get("executor") or "（照包头）")
        fields = executor_fields(lg, "decision", packaged.get("executor"), packaged.get("researcher_action") or orgspec.NOTHING)
        try:
            if fields:
                lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields", json.dumps(fields, ensure_ascii=False), write=True)
            print(back + "。")
        except LedgerError as e:
            print("提醒：回执里的%s，空间里的字段没写上（%s）：用 ledger.py update %s --set 补。" % (back, str(e)[:160], item_id))


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


def whole_document(content):
    """The answer to a read of a whole document as one element: lark-cli gives the title and the top-level
    blocks side by side, with nothing around them."""
    return content if content.lstrip().startswith("<fragment") else "<fragment>%s</fragment>" % content


def opening_blocks(xml, end_id):
    """(first block, last block before end_id) of a document read whole (whole_document): the blocks
    `ledger.py revise` replaces. A list is its <li> blocks; an <excerpt> stands for its top block."""
    root = ET.fromstring(xml)
    children = [c for c in root if c.tag != "title"]
    if not children or children[0].get("id") == end_id or children[0].get("top-block-id") == end_id:
        raise LedgerError("主文档在“状态包”标题之前没有内容块")
    head = children[0]
    first = head.get("top-block-id") or head.get("id")
    if not first and head.tag in ("ul", "ol"):
        first = next((c.get("id") for c in head if c.get("id")), None)
    if not first:
        raise LedgerError("主文档开头那一块（<%s>）没有 block id；请在网页上替换主文档开头" % head.tag)
    return first, last_block_before(xml, end_id)


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


def title_block(whole):
    """(block id, text) of the title of a document read whole. The title is a block of its own whose id is the
    document's; replacing it renames the document and, with it, its node in the knowledge space."""
    m = re.search(r"<title(?:\s+[^>]*?\bid=\"([^\"]+)\")?[^>]*>(.*?)</title>", doc_content(whole), re.S)
    found = _find(whole, ("document_id",)) if isinstance(whole, dict) else None
    return (m.group(1) if m and m.group(1) else found), (html.unescape(m.group(2)) if m else None)


def revision_body(d, item_id, link):
    """The new opening of a main document: the draft without its title, its 关联工作项 naming the item with
    the link to it (also when the draft carries the bare ID an earlier revise wrote back into it), and
    nothing from 状态包 on (the document keeps its own 状态包 and 交接记录)."""
    body = link_line(d.body_without_title(), "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id, item_id, relink=True)
    m = re.search(r"^## (?:状态包|交接记录)\s*$", body, re.M)
    return (body[:m.start()] if m else body).rstrip("\n") + "\n"


def revise(lg, item_id, path, note):
    """Put the next version of a package in place: the main document's opening, up to its 状态包 heading,
    becomes the draft (block_replace, the same write as status-block), and 交接记录 gets a line
    “<标签> 第 N 版（日期）：<改了什么>”. The document keeps its link and identity; earlier versions stay in
    the document's history. Checked first: the draft passes the package check, its type and relation are the
    item's, its version is the previous one plus one, a decision is still 待决定 and any other item is not
    closed; the item's 紧急程度 (a request's 截止时间) is read too, and one the space has no option for is
    skipped with a reminder, as register does, and so are a decision's 执行方 and 研究员要做的事 where the
    space has those fields (header_fields). A draft copied from the main document has no title line: the
    item keeps the title it has. Then, in this order, the fields are written, the document's opening is
    replaced and the line is added to 交接记录, unless it is there already; the receipt and the local decision
    state follow last. A step that fails says what is already written; the same command run again goes on
    from there and adds no second line. Nothing is sent to the researcher: the queue shows the new version
    when they come."""
    spec = lg.spec
    if not (note or "").strip():
        raise LedgerError("修订要写改了什么：--note '<改了什么>'，它会记进交接记录")
    item_id = item_id_of(item_id)
    sheet = sheet_kind(path)
    if sheet and sheet != "research_task":
        raise LedgerError("反馈的主文档没有状态包，不用 revise：要补充的写成新的反馈，或者在工作项下加评论")
    d = Sheet(spec, path, sheet) if sheet else Draft(spec, path)
    if d.pid == "delivery":
        raise LedgerError("交付包不是主文档，不用 revise：改好后用 reply 重新交，版本号加 1")
    wid = "research_task" if sheet else work_item_type(d)
    head = d.text.split("\n## ", 1)[0]
    m = LINK_LINE.search(head)
    value = m.group(2).strip() if m else ""
    if m and not is_placeholder(value) and not re.fullmatch(r"#?%s|\[#?%s\]\(\S+\)" % (item_id, item_id), value):
        raise LedgerError("修订稿的“关联工作项”写的是“%s”，要修订的是 #%s：写“#%s”，或者照新草稿写“待建”" % (value, item_id, item_id))
    it = lg.item(item_id)
    rec = receipt_of(lg, item_id)
    if not title_of(d.text):                      # copied from the main document, which has no title line: the title stays
        d.task = rec.get("title") or re.split(r"[：:]", (it or {}).get("name") or "", 1)[-1].strip() or d.task
    if it is not None and sheet:
        cn = spec.term("wi", wid)
        if it.get("type") != wid:
            raise LedgerError("#%s 是%s，修订稿是%s单：类型不变才能修订" % (
                item_id, spec.term("wi", it["type"]) if it.get("type") else "别的类型", cn))
        if it.get("flow") and d.flow and it["flow"] != d.flow:
            raise LedgerError("#%s 走的是“%s”流程，修订稿写的是“%s”：流程在新建时选定，改不了；要换流程，另开一条%s" % (
                item_id, it["flow"], d.flow, cn))
        if it["state"] in closed_states(lg):
            raise LedgerError("#%s 已经是“%s”：工作项关闭以后不再修订" % (item_id, it["state"]))
        nodes = lg.flow_nodes(item_id) if lg.mode != "offline" else []
        if nodes and nodes[0]["status"] != "doing":          # allowed from 1.0.10 on; the researcher answers for it
            at = next((n["name"] for n in nodes if n["status"] == "doing"), None)
            print("提醒：#%s 已经过了第一步“%s”%s：后面各步已经定下的文档不会跟着%s单变。" % (
                item_id, nodes[0]["name"], "，现在在“%s”" % at if at else "", cn))
    elif it is not None:
        requests = ("same_seq", "cross_request")
        relation = "（关系“%s”）" % spec.term("rel", d.rel) if d.rel else ""
        if it.get("type") != wid and it.get("type") in requests and wid in requests:
            raise LedgerError("修订稿登记出来是%s%s，#%s 是%s：包类型和关系都不变才能修订；要改关系，另写一份登记" % (
                spec.term("wi", wid), relation, item_id, spec.term("wi", it["type"])))
        if it.get("type") != wid:
            raise LedgerError("#%s 是%s，修订稿是%s：包类型不变才能修订；要换成别的包，另写一份登记" % (
                item_id, spec.term("wi", it["type"]) if it.get("type") else "别的类型", spec.pkg(d.pid)))
        registered = lg.text_of(it, "relation") if wid == "same_seq" else None
        if registered and d.rel and registered != spec.term("rel", d.rel):
            raise LedgerError("修订稿的关系是“%s”，#%s 登记的是“%s”：包类型和关系都不变才能修订；要改关系，另写一份登记" % (
                spec.term("rel", d.rel), item_id, registered))
        if wid == "decision":
            pending = lg.state("decision", "pending")
            if it["state"] != pending:
                raise LedgerError("#%s 现在是“%s”：决策只在“%s”时能修订；研究员已经定了的，要改另写一条决策" % (item_id, it["state"], pending))
        elif it["state"] in closed_states(lg):
            raise LedgerError("#%s 已经是“%s”：工作项关闭以后不再修订" % (item_id, it["state"]))
    previous = int(rec.get("version") or 1)
    if sheet:                                     # a sheet has no 版本 line: its versions are counted here
        d.version, date = previous + 1, datetime.date.today().isoformat()
    else:
        if d.version != previous + 1:
            raise LedgerError("修订稿是第 %s 版，#%s 现在是第 %d 版：修订稿的“版本”写第 %d 版" % (d.version, item_id, previous, previous + 1))
        date = orgspec.read_version(d.header.get(orgspec.VERSION))[1]
    name, title = naming(spec, d, sheet) if title_of(d.text) else (None, None)
    title = title.replace("<工作项 ID>", str(item_id)) if title else None
    kinds = receipt_kind(spec, wid, d)
    updates = []                                  # field values are read before anything is written
    if wid == "decision" and kinds.get("urgency"):
        wanted = ("urgency", kinds["urgency"])
    elif wid != "decision" and "due" in lg.contract.slots(wid, "fields") and utc_noon_ms(d.header.get("回复期限", "")):
        wanted = ("due", d.header["回复期限"])
    else:
        wanted = None
    if wanted:
        try:
            updates.append(lg.entry(wid, *wanted))
        except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（之后可以用 update 补）：%s" % (wanted[0], e))
    updates += header_fields(lg, wid, d)
    if sheet:                                     # the task takes the sender and the sequence of this version: the
        wanted = [("sender", d.author)] if d.author else []           # first sheet is the one init wrote
        if "sequence" in lg.contract.slots(wid, "fields"):
            wanted.append(("sequence", spec.term("seq", d.sender_seq or "pmo")))
        for ref, raw in wanted:
            try:
                updates.append(lg.entry(wid, ref, raw))
            except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
                print("提醒：跳过字段 %s（之后可以用 update 补）：%s" % (ref, e))
    if name and it is not None and it.get("name") and it["name"] != name:  # the title changed: the item is renamed,
        updates.append({"field_key": "name", "field_value": name})         # and its document below
    if d.source and "source_item" in lg.contract.slots(wid, "fields"):   # the upstream item this version names,
        try:                                                              # as register writes it
            updates.append(lg.entry(wid, "source_item", item_id_of(d.source)))
        except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 source_item（之后可以用 update 补）：%s" % e)
    body = revision_body(d, item_id, lg.item_url(wid, item_id))
    url = lg.doc_url(item_id, it)
    heads = heading_ids(lg, url)
    status = next((b for b, t in heads if t == "状态包"), None)
    retitle = None
    if lg.mode == "offline":
        status = status or "<状态包标题的 block_id>"
        first, last = "<主文档第一块的 block_id>", "<状态包前一块的 block_id>"
    else:
        if not status:
            raise LedgerError("#%s 的主文档里找不到“状态包”标题：revise 只换带状态包的主文档" % item_id)
        # The whole document, not a range that names only its end: lark-cli (1.0.98) answers such a range with the
        # end block alone, and the id of the first block is known from no smaller read.
        whole = lg.lark("docs", "+fetch", "--doc", url, "--detail", "with-ids")
        first, last = opening_blocks(whole_document(doc_content(whole)), status) if whole is not None else (
            "<主文档第一块的 block_id>", "<状态包前一块的 block_id>")
        doc_id, doc_title = title_block(whole) if whole is not None else (None, None)
        retitle = (doc_id, doc_title) if title and doc_id and doc_title is not None and doc_title.strip() != title else None
    done, logged = [], False                      # what is written already, should a later step fail
    try:
        if updates:                               # the fields first: nothing in the document has changed if they fail
            lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields", json.dumps(updates, ensure_ascii=False), write=True)
            done.append("工作项的字段已经按第 %d 版写了" % d.version)
        lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", first, "--end-block-id", last,
                "--doc-format", "markdown", "--content", "-", write=True, stdin=body)
        done.append("主文档开头已经换成第 %d 版" % d.version)
        if retitle:                               # the document's title, and its node's: one block, the document's own
            try:
                lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--block-id", retitle[0],
                        "--doc-format", "xml", "--content", "-", write=True, stdin="<title>%s</title>" % html.escape(title, quote=False))
                print("改名：主文档和它的知识库节点的标题从“%s”改成“%s”。" % (retitle[1].strip(), title))
            except LedgerError as e:              # not worth stopping a revision for: the text is in place
                print("提醒：主文档的标题没有改成，还是“%s”；请在网页上把它改成“%s”。原因：%s" % (
                    retitle[1].strip(), title, str(e).splitlines()[-1]))
        logged = revision_logged(lg, url, heads, d.version, date)
        if not logged:
            lg.lark("docs", "+update", "--doc", url, "--command", "append", "--doc-format", "markdown", "--content", "-", write=True,
                    stdin="%s 第 %d 版（%s）：%s\n" % (lg.tag, d.version, date, note.strip()))
    except LedgerError as e:
        if lg.mode != "apply" or not done and isinstance(e, WriteMaybeDone):
            raise
        if not done:
            raise LedgerError("%s\n工作项和主文档都还没改；修好后重跑同一条命令" % e)
        raise type(e)("%s\n%s；回执和本地决策状态还是第 %d 版。修好后重跑同一条命令接着做，交接记录不会多出一行" % (
            e, "，".join(done), previous))
    if lg.mode == "apply":
        rel = os.path.relpath(path, lg.root)
        revisions = list(rec.get("revisions") or []) + [{"version": d.version, "date": date, "note": note.strip(), "draft": rel, "at": now()}]
        queue = rec["queue"] if "queue" in rec else kinds.get("queue")
        values = dict(kinds, queue=queue, revisions=revisions)
        values.pop("type", None)
        if wid == "decision":
            values["as_packaged"] = None                       # the new version's header says who carries it out
        if not update_receipt(lg, item_id, **values):          # registered elsewhere: this draft's receipt holds it now
            save_receipt(lg, path, item_id, "registered", dict(kinds, revisions=revisions), doc=url)
        if wid == "decision" and queue:
            first_seen = rec.get("registered_at") or started_at(lg, item_id)
            hold(lg, item_id, dict(kinds, queue=queue), first_seen, rec.get("rolled_back_at") or rec.get("reopened_at"))
        drifted = write_back(path, d.text, item_id)             # the draft now names its item: it is not registered again
        if drifted:
            update_receipt(lg, item_id, draft_changed=True)
            print("提醒：" + drifted)
        print("修订：#%s 的主文档开头换成第 %d 版（文档链接不变），%s。%s" % (
            item_id, d.version, "交接记录里已经有这一版的一行，没有再加" if logged else "交接记录追加了一行",
            "本地决策状态已按新版本更新。" if wid == "decision" and queue else ""))
    else:
        print("预览：会把 #%s 的主文档开头换成第 %d 版，交接记录追加一行。" % (item_id, d.version))
    return d.version


def revision_logged(lg, url, heads, version, date):
    """Does 交接记录 already have the line of this revision (“<标签> 第 N 版（日期）：…”)? A run that failed
    after the line was added, or whose answer never came, is run again: the line is not added twice. Only the
    line revise writes counts: one that starts with the scribe's tag, at the start of a line or of a block
    (the answer may be Markdown or blocks in tags). Another session's note that names the version
    (“[Infra+2] 准备出第 2 版（日期）：…”) is no such line. False when the section cannot be read, offline included."""
    log = next((b for b, t in heads if t == "交接记录"), None)
    if not log or lg.mode == "offline":
        return False
    try:
        data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section", "--start-block-id", log)
    except LedgerError:
        return False
    tag = "".join(("\\\\?" if c in "[]+" else "") + re.escape(c) for c in lg.tag)      # a Markdown export may escape the brackets
    line = r"(?:^|>)[ \t>*-]*%s\s*第\s*%d\s*版\s*[（(]\s*%s\s*[）)]\s*[：:]" % (tag, version, re.escape(date or ""))
    return bool(re.search(line, doc_content(data), re.M))


def started_at(lg, item_id):
    """When a work item's first step began, as an ISO time: when it was created. None when unknown."""
    try:
        nodes = lg.flow_nodes(item_id) or []
    except LedgerError:
        return None
    ms = nodes[0].get("started_at") if nodes else None
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds") if ms else None


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
        if wid == "decision" and it["state"] in system_states(lg.spec):     # terminated on the web page
            gone = drop_pending(lg, item_id)
            print("#%s 是“%s”，不用归档。%s%s" % (item_id, it["state"], "本地决策状态里移除了它：冻结解除。" if gone else "",
                                              owner_note(lg, item_id)))
            return
        if it["state"] == archived:
            if wid == "decision" and drop_pending(lg, item_id):
                print("本地决策状态里移除了 #%s：冻结解除。" % item_id)
            print("#%s 已经是“%s”，不用再归档。%s" % (item_id, archived, owner_note(lg, item_id)))
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
    if wid == "decision" and drop_pending(lg, item_id):
        print("本地决策状态里移除了 #%s：冻结解除。" % item_id)
    note = owner_note(lg, item_id)
    if note:
        print(note)


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
    at, hours, out = now_at(), expiry_hours(lg.root)[0], set()
    for item_id, entry in load_gates(lg.root)["pending"].items():
        due = pool.expires_at(entry, hours)
        if due and at >= due and not pool.takes_default(entry):
            out.add(str(item_id))
    return out


def receipt_of(lg, item_id):
    """The registration receipt of a work item: packages/sent/<ID>.json, or <ID>-<draft file>.json; {} when none."""
    num = str(item_id)
    folder = glob.escape(sent_dir(lg))
    for path in sorted(glob.glob(os.path.join(folder, num + ".json"))) + sorted(glob.glob(os.path.join(folder, num + "-*.json"))):
        rec = load_json(path, {})
        if isinstance(rec, dict) and str(rec.get("work_item_id")) == num:
            return rec
    return {}


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
        rec = receipt_of(lg, r.get("work_item_id"))
        for key in ("due", "doc"):
            if rec.get(key) and key not in r:
                r[key] = rec[key]
        if name == "queue":
            who = carried_out_by(rec)
            if r.get("work_item_status") == decided and who != RESEARCHER:
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
    rows = shown_fields(lg, name, rows)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            extra = "  版本：第 %s 版" % r["version"] if r.get("version") else ""
            extra += "".join("  %s：%s" % (label, r[key] if isinstance(r[key], str) else plain(r[key]))
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
            p.add_argument("--existing", metavar="ID", help=argparse.SUPPRESS)      # gone with 1.0.10: EXISTING_GONE
    p = sub.add_parser("confirm"); p.add_argument("id")
    p.add_argument("--by", help="请书记员确认这一步的会话名；研究员交代的写 研究员；到期默认 表示照决策包的到期默认动作确认")
    p.add_argument("--set", action="append"); p.add_argument("--node", help="有几个节点同时进行时指明哪一个")
    p.add_argument("--decision", help="冻结的字段在签字后改过时：研究员认可这些改动的决策工作项 ID")
    p.add_argument("--option", help="决策的“决定”这一步：研究员选的选项名，记进回执和评论；表里的都没选写 无")
    p.add_argument("--executor", metavar="会话名", help="决策的“决定”这一步：把执行方改成这个会话")
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("revise"); p.add_argument("id"); p.add_argument("draft")
    p.add_argument("--note", required=True, help="这一版改了什么，记进交接记录")
    p.add_argument("--apply", action="store_true")
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
        if args.existing:
            raise LedgerError(EXISTING_GONE % item_id_of(args.existing))
        register(lg, args.draft, args.source, pairs=args.set, resume=args.resume, doc=args.doc, task=args.task)
    elif args.cmd == "reply":
        reply(lg, args.draft, args.doc)
    elif args.cmd == "confirm":
        confirm(lg, args.id, args.by, args.set, args.node, args.decision, args.type, args.option, args.executor)
    elif args.cmd == "revise":
        revise(lg, args.id, args.draft, args.note)
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
