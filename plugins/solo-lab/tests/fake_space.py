"""A project bound to the reference template, and a stand-in for its Feishu space.

The structure comes from tests/fixtures/feishu/v2/space.json, the snapshot of the reference
template space. FakeSpace answers the meegle and lark-cli commands ledger.py runs, in the
shapes recorded from the real space, and behaves as the real one was seen to on
2026-11-12: a new item must carry the fields its new-item page requires; a node is
confirmed only while it is in progress and its required fields are filled; a roll-back
needs a reason, puts the target back in progress and the nodes after it back to not
started, and keeps the field values; an operation record takes a while to show up.
"""
import copy
import contextlib
import datetime
import html
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ledger  # noqa: E402
import ledger_core  # noqa: E402
import orgspec  # noqa: E402
import space  # noqa: E402

V2 = os.path.join(ROOT, "tests", "fixtures", "feishu", "v2")
ME = "7000000000000000001"
TENANT = "example.feishu.cn"
# The state an item shows while a node is in progress: the template's status rules, which the
# command line cannot read. Nodes not listed show a state named after the node.
STATE_OF = {
    ("decision", "started"): "待决定", ("decision", "archive"): "已决定",
    ("feedback", "started"): "待解析", ("feedback", "archive"): "已处理",
    ("same_seq", "started"): "待开工", ("same_seq", "execute"): "执行中", ("same_seq", "accept"): "待验收",
    ("same_seq", "archive"): "已验收",
    ("cross_request", "started"): "已退回", ("cross_request", "take_order"): "待接单", ("cross_request", "execute"): "执行中",
    ("cross_request", "accept"): "待验收", ("cross_request", "archive"): "已验收",
    ("research_task", "archive"): "已结束",
}
DONE = "归档"
# What template 2.2.0 changed on the decision type: two text fields the view 待定决策 shows, off the new-item page
# so without an alias (the field keys are made up), and the state 已决定 renamed 待执行, its id kept.
FIELDS_220 = (("field_d22001", "执行方"), ("field_d22002", "研究员要做的事"))


def snapshot():
    with open(os.path.join(V2, "space.json"), encoding="utf-8") as f:
        return json.load(f)


def template_220(snap=None):
    """The structure of a space made from template 2.2.0: the reference template's, with the decision type as
    2.2.0 changed it."""
    snap = copy.deepcopy(snap or snapshot())
    t = next(t for t in snap["types"].values() if t.get("api_name") == "decision")
    for s in t["states"]:
        if s["name"] == "已决定":
            s["name"] = "待执行"
    t["fields"] += [{"field_key": key, "name": name, "field_type": "text"} for key, name in FIELDS_220]
    return snap


def bound_ids(snap=None, spec=None, space_name=None):
    """ids.json of a project bound to the reference template, with its knowledge-base nodes.
    The space name is left out unless given: the ledger compares it with the task file."""
    spec = spec or orgspec.OrgSpec()
    snap = snap or snapshot()
    contract = orgspec.Contract(spec)
    binding, problems = space.bind(contract, snap)
    assert not problems, problems
    nodes = {k: {"title": k, "node_token": "node_%s" % k.replace("/", "_")} for k in spec.node_keys()}
    ids = {"schema": space.IDS_SCHEMA, "lark": {},
           "meegle": {"project_key": snap["project_key"], "simple_name": snap["simple_name"],
                      "bound_at": "2026-11-12T00:00:00+08:00"},
           "wiki": {"space_id": "space_fake", "tenant_domain": TENANT, "nodes": nodes}}
    if space_name:
        ids["meegle"]["space_name"] = space_name
    return space.apply_binding(ids, contract, binding)


def install(root, snap=None, spec=None):
    """Write feishu/ids.json and feishu/space.json into a project directory."""
    snap = snap or snapshot()
    os.makedirs(os.path.join(root, "feishu"), exist_ok=True)
    for name, obj in (("ids.json", bound_ids(snap, spec)), ("space.json", snap)):
        with open(os.path.join(root, "feishu", name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    return snap


def failed(code, message):
    """What Ledger.run raises for a command that exits with an error."""
    return ledger_core.LedgerError("命令失败（退出码 1）：meegle ...\n" + json.dumps(
        {"data": None, "error": {"code": "SERVER_CALL_FAILED", "message": "error=%s,message=%s,retriable=false" % (code, message)}},
        ensure_ascii=False))


class FakeSpace:
    PAGE = 20

    def __init__(self, snap=None):
        self.snap = snap or snapshot()
        self.items, self.docs, self.comments, self.ops = {}, {}, [], []
        self.next_id, self.clock = 7160000001, 1794400000000
        self.lag = 0                       # operation records younger than this many ms are not listed yet
        self.moved = []
        self.title_fails = False           # a rename of a document is refused
        # STATE_OF names the states of the reference template. A status rule points at a state, not at its name,
        # so a state this space renamed (its id kept) shows its new name: (type_key, name there) -> name here.
        base, self.renamed = snapshot(), {}
        for key, t in self.snap["types"].items():
            was = {s["id"]: s["name"] for s in (base["types"].get(key) or {}).get("states") or []}
            self.renamed.update({(key, was[s["id"]]): s["name"] for s in t["states"] if was.get(s["id"]) not in (None, s["name"])})

    # ------------------------------------------------------------ helpers
    def tick(self, ms=1000):
        self.clock += ms
        return self.clock

    def type_key(self, ref):
        for key, t in self.snap["types"].items():
            if ref in (key, t.get("api_name"), t.get("name")):
                return key
        raise failed("ErrWorkItemTypeNotFound", "type not found: %s" % ref)

    def field(self, type_key, key):
        return next((f for f in self.snap["types"][type_key]["fields"] if f["field_key"] == key), None)

    def state_of(self, item):
        t = self.snap["types"][item["type_key"]]
        if item.get("terminated"):                     # terminated on the web page: the system state
            return "已终止"
        doing = [n for n in item["nodes"] if n["status"] == "doing"]
        if not doing:
            name = DONE if all(n["status"] == "finished" for n in item["nodes"]) else "开始"
        else:
            name = STATE_OF.get((t["api_name"], doing[0]["key"]), doing[0]["name"])
        return self.renamed.get((item["type_key"], name), name)

    def record(self, item, module, kind, obj_type, obj, old=None, new=None, extra=None):
        self.ops.append({"op_record_module": module, "operation_time": self.tick(10), "operation_type": kind,
                         "operator": ME, "operator_type": "user", "project_key": self.snap["project_key"],
                         "record_contents": [{"add": [], "delete": [], "extra": extra, "new": new or [], "old": old or [],
                                              "object": {"object_type": obj_type, "object_value": obj}, "object_property": None}],
                         "work_item_id": int(item["id"]), "work_item_type_key": item["type_key"]})

    def set_field(self, item, key, value):
        old = item["fields"].get(key, "")
        if str(old) == str(value):
            return
        if value == "":
            item["fields"].pop(key, None)
        else:
            item["fields"][key] = value
        self.record(item, "field_mod", "modify", "field", key, [str(old)] if old != "" else [], [str(value)] if value != "" else [])

    def shown(self, type_key, key, value):
        """A stored value as `workitem get` shows it."""
        f = self.field(type_key, key) or {}
        kind = f.get("field_type")
        if kind == "select":
            return {"label": next((o["name"] for o in f.get("options") or [] if o["id"] == value), value), "value": value}
        if kind == "date":
            ts = int(value)
            return {"iso_time": datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds"),
                    "timestamp": ts}
        if kind == "user":
            return None                                 # person fields read back empty; MQL has them
        return value

    # ------------------------------------------------------------- meegle
    def create(self, type_ref, fields):
        type_key = self.type_key(type_ref)
        t = self.snap["types"][type_key]
        given = {f["field_key"]: f.get("field_value") for f in fields}
        for f in fields:
            if not isinstance(f.get("field_value"), str):
                raise failed("MCPGatewayRequestMismatch", "argument arguments.fields.field_value must be string")
        flow = t["flows"].get(str(given.get("template")))
        if not flow or not flow.get("nodes"):
            raise failed("ErrTemplateNotFound", "template not found")
        for c in t["create_fields"]:
            if c["required"] and given.get(c["field_key"], "") == "":
                raise failed("ErrFieldRequired", "{field_name}({field_key}) 必填,%s(%s) 必填" % (c["name"], c["field_key"]))
        item = {"id": str(self.next_id), "type_key": type_key, "name": given.get("name", ""), "flow_id": str(given["template"]),
                "fields": {}, "roles": {}, "nodes": [dict(copy.deepcopy(n), status="not_started", started_at=None, finished_at=None) for n in flow["nodes"]]}
        self.next_id += 1
        item["nodes"][0]["status"], item["nodes"][0]["started_at"] = "doing", self.clock
        for key, value in given.items():
            if key == "role_owners":
                item["roles"] = {r["role"]: list(r["owners"]) for r in json.loads(value)}
            elif key not in ("name", "template") and value != "":
                item["fields"][key] = value
        self.items[item["id"]] = item
        self.record(item, "work_item_mod", "create", "work_item", item["id"])
        return {"work_item_id": int(item["id"]), "url": "https://project.feishu.cn/%s/%s/detail/%s" % (
            self.snap["project_key"], type_key, item["id"])}

    def item(self, item_id):
        item = self.items.get(str(item_id))
        if not item:
            raise failed("ErrWorkItemNotFound", "work item not found")
        return item

    def get(self, item_id):
        item = self.item(item_id)
        t = self.snap["types"][item["type_key"]]
        state = self.state_of(item)
        fields = [{"key": k, "name": (self.field(item["type_key"], k) or {}).get("name"), "value": self.shown(item["type_key"], k, v)}
                  for k, v in item["fields"].items()]
        fields.append({"key": "current_status_operator", "name": "当前负责人", "value": None})
        return {"pagination": {"has_more": False, "page_size": 100, "total": len(fields)},
                "work_item_attribute": {
                    "work_item_id": item["id"], "work_item_name": item["name"], "work_item_mod": "节点流",
                    "work_item_type": {"key": item["type_key"], "name": t["name"]},
                    "work_item_status": {"key": next((s["id"] for s in t["states"] if s["name"] == state), state), "name": state},
                    "template": {"id": int(item["flow_id"]), "name": t["flows"][item["flow_id"]]["name"]},
                    "role_members": [{"key": r["role_id"], "name": r["role_name"],
                                      "members": [{"key": u, "name": ""} for u in item["roles"].get(r["role_id"], [])]}
                                     for r in t["roles"]]},
                "work_item_fields": fields}

    def node_json(self, item, n):
        forms = [{"field_key": f["field_key"], "field_name": f["name"], "field_type": f["field_type"], "form_item_type": "field",
                  "is_required": required, "value": item["fields"].get(f["field_key"], ""), "value_label": ""}
                 for required, group in ((True, n["required"]), (False, n["optional"])) for f in group]
        owners = [{"email": "", "name": "", "user_key": u} for r in n["roles"] for u in item["roles"].get(r, [])]
        return {"basic": {"name": n["name"], "node_key": n["key"], "node_uuid": "node_%s_%s" % (n["key"], item["id"]),
                          "pre_node_keys": n["prev"], "next_node_keys": n["next"], "status": n["status"]},
                "assignees": {"node_owner_mode": 3, "owners": owners, "participants": [],
                              "role_assignees": {r: owners for r in n["roles"]}},
                "form_items": forms, "sub_tasks": None, "sub_workitems": [],
                "schedule": {"actual_begin_time": n.get("started_at"), "actual_finish_time": n["finished_at"], "is_delayed": False}}

    def get_node(self, item_id, page=1):
        item = self.item(item_id)
        nodes = list(reversed(item["nodes"]))           # the real answer is not in flow order
        part = nodes[(page - 1) * self.PAGE:page * self.PAGE]
        return {"list": [self.node_json(item, n) for n in part],
                "pagination": {"has_more": page * self.PAGE < len(nodes), "page_num": page, "page_size": self.PAGE, "total": len(nodes)},
                "workflow_template": {"template_id": int(item["flow_id"]), "template_version": 1}}

    def node(self, item, key):
        n = next((n for n in item["nodes"] if n["key"] == key), None)
        if not n:
            raise failed("ErrNodeNotFound", "node not found")
        return n

    def required(self, item_id, node_key, mode=None):
        item = self.item(item_id)
        rows = [{"class": "field", "field_type_key": f["field_type"], "finished": item["fields"].get(f["field_key"], "") != "",
                 "key": f["field_key"]} for f in self.node(item, node_key)["required"]]
        if mode == "unfinished":
            rows = [r for r in rows if not r["finished"]]
        return {"form_items": rows} if rows else {}

    def update(self, item_id, fields=None, role_operate=None):
        item = self.item(item_id)
        for f in fields or []:
            if not isinstance(f.get("field_value"), str):
                raise failed("MCPGatewayRequestMismatch", "argument arguments.fields.field_value must be string, got number")
            if f["field_key"] == "name":                 # the item's name, as create takes it
                item["name"] = f["field_value"]
                continue
            if (self.field(item["type_key"], f["field_key"]) or {}).get("field_type") == "bool" and f["field_key"] == "is_frozen":
                raise failed("ErrAPIUpdateField", "Update Field Invalid,is_frozen:can not update system calculate field")
            if not self.field(item["type_key"], f["field_key"]):
                raise failed("ErrFieldNotFound", "field not found: %s" % f["field_key"])
            self.set_field(item, f["field_key"], f["field_value"])
        for op in role_operate or []:
            members = item["roles"].setdefault(op["role_key"], [])
            for u in op["user_keys"]:
                if op["op"] == "add" and u not in members:
                    members.append(u)
        return {"mcp_result": ""}

    def transition(self, item_id, node_key, action, reason=None):
        item = self.item(item_id)
        n = self.node(item, node_key)
        before = self.state_of(item)
        if action == "confirm":
            if n["status"] != "doing":
                raise failed("ErrAPIOperateNotArrivedNode", "Node Is Not Arrived, Could Not Be operated")
            for f in n["required"]:
                if item["fields"].get(f["field_key"], "") == "":
                    raise failed("ErrFieldRequired", "{field_name}({field_key}) 必填,%s(%s) 必填" % (f["name"], f["field_key"]))
            n["status"], n["finished_at"] = "finished", self.tick()
            for nxt in item["nodes"]:
                if nxt["key"] in n["next"]:
                    nxt["status"], nxt["started_at"] = "doing", n["finished_at"]
            self.record(item, "node_mod", "complete", "node", n["key"])
        elif action == "rollback":
            if not (reason or "").strip():
                raise failed("ErrAPIInvalidParam", "rollback reason is required")
            if n["status"] != "finished":
                raise failed("ErrAPIOperateNotArrivedNode", "Node Is Not Arrived, Could Not Be operated")
            at = item["nodes"].index(n)
            for later in item["nodes"][at + 1:]:
                if later["status"] != "not_started":
                    later["status"], later["started_at"], later["finished_at"] = "not_started", None, None
                    self.record(item, "node_mod", "rollback", "node", later["key"], extra={"reason_suffix": [reason]})
            n["status"], n["started_at"], n["finished_at"] = "doing", self.tick(), None   # measured: begins again at the rollback
            self.record(item, "node_mod", "rollback", "node", n["key"], extra={"reason_suffix": [reason]})
        else:
            raise failed("ErrAPIInvalidParam", "unknown action")
        after = self.state_of(item)
        if after != before:
            self.record(item, "field_mod", "modify", "field", "work_item_status", [before], [after])
        return "success"

    def op_records(self, item_id, module=None, start=None, end=None, start_from=None):
        if (start is None) != (end is None):
            raise failed("ErrAPIInvalidParam", "Invalid Param,start and end must be provided together")
        rows = [r for r in self.ops if str(r["work_item_id"]) == str(item_id) and (not module or r["op_record_module"] == module)
                and r["operation_time"] <= self.clock - self.lag
                and (start is None or int(start) <= r["operation_time"] <= int(end))]
        at = int(start_from or 0)
        part = rows[at:at + self.PAGE]
        more = at + self.PAGE < len(rows)
        return {"op_records": part, "has_more": more, "start_from": str(at + self.PAGE) if more else "", "total": 0}

    def cell(self, item, key):
        if key == "work_item_id":
            return {"long_value": int(item["id"])}
        if key == "name":
            return {"string_value": item["name"]}
        if key == "work_item_status":
            return {"key_label_value_list": [{"key": self.state_of(item), "label": self.state_of(item)}]}
        if key == "template":
            return {"key_label_value": {"key": item["flow_id"], "label": self.snap["types"][item["type_key"]]["flows"][item["flow_id"]]["name"]}}
        value = item["fields"].get(key)
        f = self.field(item["type_key"], key) or {}
        if f.get("field_type") == "user":
            return {"user_value": {"email": "researcher@example.com", "name_cn": "研究员甲", "user_key": value}} if value else {}
        if f.get("field_type") == "select" and value:
            return {"key_label_value_list": [{"key": value, "label": self.shown(item["type_key"], key, value)["label"]}]}
        return {"string_value": value} if value else {}

    def text(self, item, key):
        """What a WHERE condition compares a field with: labels for selects and states."""
        cell = self.cell(item, key)
        for k in ("long_value", "string_value"):
            if k in cell:
                return str(cell[k])
        for k in ("key_label_value_list", ):
            if k in cell:
                return cell[k][0]["label"]
        if "key_label_value" in cell:
            return cell["key_label_value"]["label"]
        return None

    def matches(self, item, cond):
        cond = cond.strip()
        if not cond or cond.startswith("RELATIVE_DATETIME_BETWEEN"):
            return True
        m = re.fullmatch(r"`(\w+)` (NOT IN|IN) \((.*)\)", cond)
        if m:
            values = re.findall(r"'([^']*)'", m.group(3))
            return (self.text(item, m.group(1)) in values) == (m.group(2) == "IN")
        m = re.fullmatch(r"`(\w+)` IS (NOT )?NULL", cond)
        if m:
            return (self.text(item, m.group(1)) is None) == (m.group(2) is None)
        m = re.fullmatch(r"`(\w+)` = '?([^']*)'?", cond)
        if m:
            return self.text(item, m.group(1)) == m.group(2)
        raise failed("ErrMQLSyntax", "cannot read condition: %s" % cond)

    def query(self, mql):
        m = re.fullmatch(r"SELECT (.*?) FROM `[^`]+`\.`([^`]+)`(?: WHERE (.*?))?(?: LIMIT (\d+))?", mql.strip(), re.S)
        if not m:
            raise failed("ErrMQLSyntax", "cannot read: %s" % mql)
        cols = re.findall(r"`(\w+)`", m.group(1))
        type_key = self.type_key(m.group(2))
        conds = re.split(r"\s+AND\s+", m.group(3)) if m.group(3) else []
        rows = [i for i in self.items.values() if i["type_key"] == type_key and all(self.matches(i, c) for c in conds)]
        rows = rows[:int(m.group(4))] if m.group(4) else rows
        if not rows:
            return {"data": {}, "list": None}
        return {"data": {"1": [{"moql_field_list": [{"key": c, "value": self.cell(i, c)} for c in cols]} for i in rows]},
                "list": [{"count": len(rows), "group_infos": [{"group_id": "1"}]}], "session_id": "fake"}

    # --------------------------------------------------------------- lark
    def create_doc(self, parent, title, body):
        n = len(self.docs) + 1
        doc = {"document_id": "docx%04d" % n, "node_token": "wik%04d" % n, "parent": parent, "title": title, "body": body}
        self.docs[doc["document_id"]] = doc
        return {"ok": True, "data": {"document": {"document_id": doc["document_id"], "revision_id": 1,
                                                  "url": "https://%s/docx/%s" % (TENANT, doc["document_id"])}}}

    def doc_by(self, token):
        return next((d for d in self.docs.values() if token in (d["document_id"], d["node_token"])), None)

    # A document as blocks, for docs +fetch --scope outline|range and docs +update block_replace|append:
    # every heading, list item, paragraph line and table is a block with an id; lists come back as <ul>
    # around their <li> blocks, as recorded (tests/fixtures/feishu/docs-fetch-range-status.json).
    def blocks(self, doc):
        if "blocks" not in doc:
            doc["blocks"] = self.parse_blocks(doc["body"] or "")
        return doc["blocks"]

    def parse_blocks(self, text):
        out, lines, i = [], text.split("\n"), 0
        while i < len(lines):
            line = lines[i]
            i += 1
            if not line.strip():
                continue
            if line.startswith("|"):
                rows = [line]
                while i < len(lines) and lines[i].startswith("|"):
                    rows.append(lines[i])
                    i += 1
                kind, level, body = "table", 0, "\n".join(rows)
            elif re.match(r"^#{1,6} ", line):
                kind, level, body = "h", len(line.split(" ", 1)[0]), line.split(" ", 1)[1].strip()
            elif re.match(r"^\s*[-*] ", line):
                kind, level, body = "li", 0, re.sub(r"^\s*[-*] ", "", line)
            else:
                kind, level, body = "p", 0, line
            self.block_no = getattr(self, "block_no", 0) + 1
            out.append({"id": "blk%05d" % self.block_no, "kind": kind, "level": level, "text": body})
        return out

    def render(self, doc):
        """The document's text again, after an update: what a test reads back."""
        prefix = {"h": lambda b: "#" * b["level"] + " ", "li": lambda b: "- "}
        doc["body"] = "\n".join(prefix.get(b["kind"], lambda b: "")(b) + b["text"] for b in doc["blocks"]) + "\n"

    def xml(self, blocks):
        from xml.sax.saxutils import escape
        parts, in_list = [], False
        for b in blocks:
            if b["kind"] == "li":
                parts += [] if in_list else ["<ul>"]
                parts.append('<li id="%s">%s</li>' % (b["id"], escape(b["text"])))
                in_list = True
                continue
            if in_list:
                parts.append("</ul>")
                in_list = False
            tag = "h%d" % b["level"] if b["kind"] == "h" else {"table": "table", "p": "p"}[b["kind"]]
            parts.append('<%s id="%s">%s</%s>' % (tag, b["id"], escape(b["text"]), tag))
        return "".join(parts) + ("</ul>" if in_list else "")

    def doc_of_link(self, link):
        doc = self.doc_by((link or "").rstrip("/").rsplit("/", 1)[-1])
        if not doc:
            raise ledger_core.LedgerError("命令失败（退出码 1）：lark-cli docs\ndocument not found: %s" % link)
        return doc

    def fetch(self, flags):
        doc = self.doc_of_link(flags.get("--doc"))
        blocks = self.blocks(doc)
        if flags.get("--scope") == "outline":
            depth = int(flags.get("--max-depth", 9))
            heads = [b for b in blocks if b["kind"] == "h" and b["level"] <= depth]
            content = '<fragment mode="outline"><outline>%s</outline></fragment>' % self.xml(heads)
        elif flags.get("--scope") == "range":
            ids = [b["id"] for b in blocks]
            end = ids.index(flags["--end-block-id"]) if flags.get("--end-block-id") not in (None, "-1") else len(ids) - 1
            # as lark-cli 1.0.98 answers: a range that names only its end is the end block alone, not the document up to it
            start = ids.index(flags["--start-block-id"]) if flags.get("--start-block-id") else end
            content = '<fragment mode="range">%s</fragment>' % self.xml(blocks[start:end + 1])
        else:
            # with ids the title carries the document's own: it is the block a rename replaces
            ident = ' id="%s"' % doc["document_id"] if flags.get("--detail") == "with-ids" else ""
            content = "<title%s>%s</title>%s" % (ident, html.escape(doc["title"], quote=False), self.xml(blocks))
        return {"ok": True, "data": {"document": {"content": content, "document_id": doc["document_id"], "revision_id": 1}}}

    def doc_update(self, flags, stdin):
        doc = self.doc_of_link(flags.get("--doc"))
        if flags.get("--command") == "block_replace" and flags.get("--block-id") == doc["document_id"]:
            m = re.fullmatch(r"\s*<title>(.*)</title>\s*", stdin or "", re.S)       # the title block: a rename
            if not m or flags.get("--doc-format") != "xml":
                raise ledger_core.LedgerError("命令失败（退出码 1）：lark-cli docs +update\nthe title block takes <title>…</title> as xml")
            if self.title_fails:
                raise ledger_core.LedgerError("命令失败（退出码 1）：lark-cli docs +update\npermission denied")
            doc["title"] = html.unescape(m.group(1))
            return {"ok": True, "data": {"result": "success", "updated_blocks_count": 1}}
        blocks = self.blocks(doc)
        new = self.parse_blocks(stdin or "")
        if flags.get("--command") == "append":
            blocks.extend(new)
        elif flags.get("--command") == "overwrite":       # the whole document; its title stays
            blocks[:] = new
        elif flags.get("--command") == "block_replace":
            ids = [b["id"] for b in blocks]
            start, end = ids.index(flags["--start-block-id"]), ids.index(flags["--end-block-id"])
            if end < start:
                raise ledger_core.LedgerError("命令失败（退出码 1）：lark-cli docs +update\nreverse range")
            blocks[start:end + 1] = new
        else:
            raise AssertionError("假空间不认识这条文档命令：%s" % flags.get("--command"))
        self.render(doc)
        return {"ok": True, "data": {"result": "success", "updated_blocks_count": len(new)}}

    def node_get(self, token):
        doc = self.doc_by(token)
        if not doc:
            raise ledger_core.LedgerError("命令失败（退出码 1）：lark-cli wiki +node-get\nnode not found")
        return {"ok": True, "data": {"node_token": doc["node_token"], "obj_token": doc["document_id"], "obj_type": "docx",
                                     "parent_node_token": doc["parent"], "title": doc["title"]}}

    # ----------------------------------------------------------- dispatch
    def answer(self, argv, stdin=None):
        """The answer to one command line; LedgerError for the failures the real tools report."""
        flags, words = {}, []
        i = 1
        while i < len(argv):
            if argv[i].startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[argv[i]] = argv[i + 1]
                i += 2
            else:
                words.append(argv[i])
                i += 1
        cmd = tuple(w for w in words if not w.startswith("--"))[:2]
        item_id = flags.get("--work-item-id")
        if argv[0] == "meegle":
            if cmd == ("user", "me"):
                return {"user_key": ME, "name": "研究员甲"}
            if cmd == ("workitem", "create"):
                return self.create(flags["--work-item-type"], json.loads(flags["--fields"]))
            if cmd == ("workitem", "get"):
                return self.get(item_id)
            if cmd == ("workitem", "update"):
                return self.update(item_id, json.loads(flags["--fields"]) if "--fields" in flags else None,
                                   json.loads(flags["--role-operate"]) if "--role-operate" in flags else None)
            if cmd == ("workitem", "query"):
                return self.query(flags["--mql"])
            if cmd == ("workitem", "list-op-records"):
                return self.op_records(item_id, flags.get("--op-record-module"), flags.get("--start"), flags.get("--end"),
                                       flags.get("--start-from"))
            if cmd == ("workflow", "get-node"):
                return self.get_node(item_id, int(flags.get("--page-num", 1)))
            if cmd == ("workflow", "list-state-required"):
                return self.required(item_id, flags["--state-key"], flags.get("--mode"))
            if cmd == ("workflow", "transition"):
                return self.transition(item_id, flags["--node-id"], flags["--action"], flags.get("--rollback-reason"))
            if cmd == ("comment", "add"):
                self.comments.append((item_id, flags.get("--content")))
                return {}
        if argv[0] == "lark-cli":
            if cmd == ("docs", "+create"):
                return self.create_doc(flags.get("--parent-token"), flags.get("--title"), stdin)
            if cmd == ("wiki", "+node-get"):
                return self.node_get(flags["--node-token"])
            if cmd == ("docs", "+fetch"):
                return self.fetch(flags)
            if cmd == ("docs", "+update"):
                return self.doc_update(flags, stdin)
            if cmd == ("wiki", "+move"):
                doc = self.doc_by(flags["--node-token"])
                if doc:
                    doc["parent"] = flags["--target-parent-token"]
                self.moved.append((flags["--node-token"], flags["--target-parent-token"]))
                return {"ok": True}
        raise AssertionError("假空间不认识这条命令：%s" % " ".join(argv[:4]))


class SimLedger(ledger_core.Ledger):
    """A Ledger that talks to a FakeSpace. `replies` are answered first: [(words, value,
    exception or function of the command line)], the first whose words are all in it."""

    def __init__(self, root, mode, fake, replies=()):
        super().__init__(root, mode)
        self.fake, self.replies, self.calls = fake, list(replies), []

    def run(self, argv, write, stdin=None):
        shown = " ".join(argv)
        if write and self.mode != "apply" or self.mode == "offline":
            self.planned.append(shown)
            print(("将执行：" if write else "将读取：") + shown)
            return None
        self.calls.append((list(argv), write, stdin))
        for words, value in self.replies:
            if all(w in argv for w in words):
                if isinstance(value, Exception):
                    raise value
                return value(argv) if callable(value) else value
        return self.fake.answer(argv, stdin)

    def wrote(self, *words):
        """The write commands that contain all the words."""
        return [argv for argv, write, _ in self.calls if write and all(w in argv for w in words)]


def register(lg, path, source=None, pairs=None, resume=False, doc=None, task=None):
    """A registration as its two sessions carry it out from 1.1.0: the author uploads the document
    (docs.upload), the scribe creates the work item with the link in its 主文档 field (workitem.register), the
    author writes the item's ID into the document (docs.relink). What the two steps of the author print is
    left out: the tests of a registration read what the scribe sees. Only the scribe's step runs offline,
    with --resume or --doc, and on a draft that is registered already (the scribe's step says so). A preview
    is the scribe's: by then the author has uploaded."""
    import docs
    import workitem
    if lg.mode != "offline" and not resume and not doc and not ledger_core.find_receipt(lg, path):
        author = lg if lg.mode == "apply" else SimLedger(lg.root, "apply", lg.fake)
        with contextlib.redirect_stdout(io.StringIO()):
            docs.upload(author, path, source=source)
    item = workitem.register(lg, path, source=source, pairs=pairs, resume=resume, doc=doc, task=task)
    if lg.mode == "apply":
        with contextlib.redirect_stdout(io.StringIO()):
            docs.relink(lg, item)
    return item


def reply(lg, path, doc=None):
    """A delivery as its two sessions carry it out from 1.1.0: the session that delivers uploads the 交付包
    (docs.upload), the scribe puts its link into the request's 交付包 field (workitem.reply). What the upload
    prints is left out. With --doc and offline only the scribe's step runs."""
    import docs
    import workitem
    if lg.mode == "apply" and not doc:
        with contextlib.redirect_stdout(io.StringIO()):
            docs.upload(lg, path)
    return workitem.reply(lg, path, doc=doc)
