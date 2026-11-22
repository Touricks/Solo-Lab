#!/usr/bin/env python3
"""Read a Feishu Project space and bind the plugin's contract to it.

    space.py snapshot --target <项目目录> [--apply]      read the project's space: feishu/space.json, feishu/space.md
    space.py bind     --target <项目目录> [--map k=v]    show which object of the space would fill each slot
    space.py check    --target <项目目录>                contract check on feishu/space.json and feishu/ids.json
    space.py check    --space <名字、simple_name 或 project_key>   read any space (a template) and check it; writes nothing
    space.py show     --target <项目目录> [flows|fields|binding|structure]
    space.py show     --snapshot <space.json> structure   the structure without any key of the space
    space.py export   --space <名字…> --out <文件>         a template maintainer's snapshot of any space, outside every project

The plugin and a template are independent. The plugin carries a contract
(orgspec/contract.json): the types, fields, roles and states its scripts handle by meaning.
A snapshot is what a space really has: types, fields with options, roles, the fields a new
work item must fill, states and, for every flow that already has a work item, the nodes
in order with their roles and required form items. Binding says which object of the space
fills each slot; it matches by --map, then what was bound last time, then the identifier
(api_name, role id, the field alias shown on the new-item page), then the name (a state also
by a name it had in an earlier template: terms.zh.json state_former). The binding
itself (feishu/ids.json) is written by `space.py feishu --apply` only, which the researcher's
own session runs: every session can read a space, one place changes what the ledger trusts.

Only read-only meegle commands are used. A flow chart can only be read from a work item
of that flow (`workflow get-node`), so a flow nobody has used yet is listed without nodes.
Exit codes: 0 fine; 1 the contract check found problems; 2 the command failed.
Python 3.9 standard library only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402

SNAPSHOT_SCHEMA = "solo-lab/space-v1"
IDS_SCHEMA = "solo-lab/ids-v2"
NOT_WORK_ITEMS = {"chart"}          # listed by meta-types, holds no work items
FIELD_TYPES = {"select": "单选", "multi_select": "多选", "text": "单行文本", "multi_text": "多行文本", "date": "日期",
               "link": "链接", "number": "数字", "bool": "开关", "user": "人员"}
# Field type names a space may report for each contract type. A name outside all of them is not judged.
# meta-fields reports 多行文本 as text, the same as 单行文本, so text passes for multi_text too.
FIELD_TYPE_ALIASES = {"select": {"select", "radio"}, "multi_select": {"multi_select", "multi-select", "checkbox"},
                      "text": {"text", "multi_text", "multi-text", "multi_pure_text", "multi-pure-text"},
                      "multi_text": {"multi_text", "multi-text", "multi_pure_text", "multi-pure-text", "text"},
                      "date": {"date"}, "link": {"link"}, "number": {"number"}, "bool": {"bool", "switch"},
                      "user": {"user"}}
KNOWN_FIELD_TYPES = set().union(*FIELD_TYPE_ALIASES.values())
MAINTAINER = "报给模板维护者"


class SpaceError(Exception):
    pass


def call(argv, timeout=60):
    """Run one read-only meegle command and parse its JSON. Tests replace this function."""
    cmd = ["meegle", *argv, "--format", "json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SpaceError("运行不了 %s：%s" % (" ".join(cmd[:3]), e))
    if p.returncode != 0:
        raise SpaceError("%s 失败（退出码 %s）：%s" % (" ".join(cmd[:3]), p.returncode, (p.stderr or p.stdout or "")[-300:]))
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except ValueError:
        raise SpaceError("%s 的输出不是 JSON" % " ".join(cmd[:3]))


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------- snapshot
def paged(argv, key="list", limit=40):
    """Every item of a paged meta read: meta-fields holds 50 per page, get-node 20."""
    items, page = [], 1
    while page <= limit:
        data = call(argv + ["--page-num", str(page)])
        batch = data.get(key) or []
        items.extend(batch)
        if not batch or not (data.get("pagination") or {}).get("has_more"):
            return items, data
        page += 1
    raise SpaceError("%s 翻了 %d 页还没读完" % (" ".join(argv[:2]), limit))


def field_record(f):
    rec = {"field_key": f.get("field_key"), "name": f.get("field_name"), "field_type": f.get("field_type")}
    if f.get("option"):
        rec["options"] = [{"id": str(o.get("option_id")), "name": o.get("option_name")} for o in f["option"]]
    return rec


def ordered(nodes):
    """Nodes in flow order: each one after the nodes it follows (the flows here are straight lines)."""
    by_key = {n["key"]: n for n in nodes}
    left, out = dict(by_key), []
    while left:
        ready = [k for k, n in left.items() if not [p for p in n["prev"] if p in left]]
        if not ready:                                   # a cycle: keep what the space gave
            out += list(left.values())
            break
        for k in ready:
            out.append(left.pop(k))
    return out


def node_record(n):
    basic, people = n.get("basic") or {}, n.get("assignees") or {}
    forms = [f for f in n.get("form_items") or [] if isinstance(f, dict) and f.get("field_key")]
    return {"key": basic.get("node_key"), "name": basic.get("name"),
            "prev": list(basic.get("pre_node_keys") or []), "next": list(basic.get("next_node_keys") or []),
            "roles": sorted((people.get("role_assignees") or {}).keys()),
            "required": [{"field_key": f["field_key"], "name": f.get("field_name"), "field_type": f.get("field_type")}
                         for f in forms if f.get("is_required")],
            "optional": [{"field_key": f["field_key"], "name": f.get("field_name"), "field_type": f.get("field_type")}
                         for f in forms if not f.get("is_required")]}


def read_flow(key, type_key, flow_name):
    """One flow's nodes, read from any work item of that flow; None when it has none yet."""
    mql = "SELECT `work_item_id` FROM `%s`.`%s` WHERE `template` = '%s' LIMIT 1" % (
        key, type_key, flow_name.replace("\\", "\\\\").replace("'", "\\'"))
    data = call(["workitem", "query", "--project-key", key, "--mql", mql])
    groups = data.get("data") if isinstance(data, dict) else None
    item = None
    for group in (groups or {}).values():
        for rec in group or []:
            for f in rec.get("moql_field_list") or []:
                if f.get("key") == "work_item_id":
                    item = (f.get("value") or {}).get("long_value")
    if not item:
        return None
    nodes, last = paged(["workflow", "get-node", "--project-key", key, "--work-item-id", str(item)])
    return {"read_from": str(item), "version": (last.get("workflow_template") or {}).get("template_version"),
            "nodes": ordered([node_record(n) for n in nodes])}


def read_type(key, t):
    type_key = t["type_key"]
    fields, _ = paged(["workitem", "meta-fields", "--project-key", key, "--work-item-type", type_key])
    roles, _ = paged(["workitem", "meta-roles", "--project-key", key, "--work-item-type", type_key])
    create = call(["workitem", "meta-create-fields", "--project-key", key, "--work-item-type", type_key])
    rec = {"name": t.get("name"), "api_name": t.get("api_name"), "fields": [], "roles": [], "create_fields": [],
           "states": [], "flows": {}}
    alias = {}
    for c in create.get("FieldConfList") or []:
        if c.get("field_alias"):
            alias[c.get("field_key")] = c["field_alias"]
        rec["create_fields"].append({"field_key": c.get("field_key"), "name": c.get("field_name"),
                                     "required": c.get("is_required") == 1})
    for f in fields:
        if f.get("field_key") == "work_item_status":
            rec["states"] = [{"id": str(o.get("option_id")), "name": o.get("option_name")} for o in f.get("option") or []]
        elif f.get("field_key") == "template":
            for o in f.get("option") or []:
                rec["flows"][str(o.get("option_id"))] = {"name": o.get("option_name")}
        one = field_record(f)
        if one["field_key"] in alias:
            one["alias"] = alias[one["field_key"]]
        rec["fields"].append(one)
    rec["roles"] = [{"role_id": r.get("role_id"), "role_name": r.get("role_name")} for r in roles]
    for flow in rec["flows"].values():
        got = read_flow(key, type_key, flow["name"])
        flow.update(got or {"nodes": None})
    return type_key, rec


def snapshot(project, wanted=None, workers=4):
    """What the space has. project: {project_key, simple_name, name}. wanted(type) says which
    enabled types to read in full; by default every type that can hold work items."""
    key = project["project_key"]
    types = call(["workitem", "meta-types", "--project-key", key]).get("list") or []
    snap = {"schema": SNAPSHOT_SCHEMA, "project_key": key, "simple_name": project.get("simple_name"),
            "space_name": project.get("name") or project.get("space_name"), "read_at": now(),
            "types": {}, "disabled_types": [], "other_types": []}
    read = []
    for t in types:
        if t.get("is_disable") == 1:
            snap["disabled_types"].append({"name": t.get("name"), "api_name": t.get("api_name"), "type_key": t.get("type_key")})
        elif t.get("type_key") in NOT_WORK_ITEMS or (wanted and not wanted(t)):
            snap["other_types"].append({"name": t.get("name"), "api_name": t.get("api_name"), "type_key": t.get("type_key")})
        else:
            read.append(t)
    with ThreadPoolExecutor(max_workers=workers) as pool:        # independent read-only requests
        snap["types"] = dict(pool.map(lambda t: read_type(key, t), read))
    return snap


def wanted_by(contract, maps=None, old=None):
    """Read in full only the types that can fill a type slot: by identifier, name, --map or
    the binding made last time (old)."""
    names = set(contract.types()) | {contract.name("types", t) for t in contract.types()}
    names |= {v for k, v in (maps or {}).items() if "." not in k}
    names |= {t.get("type_key") for t in ((old or {}).get("types") or {}).values()}
    names.discard(None)
    return lambda t: bool(names & {t.get("api_name"), t.get("name"), t.get("type_key")})


# ---------------------------------------------------------------- lookup
def type_of(snap, type_key):
    return (snap.get("types") or {}).get(type_key) or {}


def field_of(snap, type_key, ref):
    """A field of a type by field_key, alias or name; None when there is none or the name is not unique."""
    fields = type_of(snap, type_key).get("fields") or []
    for attr in ("field_key", "alias", "name"):
        hits = [f for f in fields if f.get(attr) == ref]
        if len(hits) == 1:
            return hits[0]
    return None


def flow_of(snap, type_key, ref=None):
    """(template id, flow) by id or name; the only flow when ref is None and the type has one."""
    flows = type_of(snap, type_key).get("flows") or {}
    if ref is None:
        return next(iter(flows.items())) if len(flows) == 1 else (None, None)
    for tid, flow in flows.items():
        if str(ref) in (tid, flow.get("name")):
            return tid, flow
    return None, None


# --------------------------------------------------------------- binding
def parse_maps(contract, items):
    """--map <类型槽位>=<类型名> / <类型槽位>.<字段槽位>=<字段名>: what the researcher pointed at by hand."""
    maps = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        wid, _, fid = key.partition(".")
        ok = sep and value and wid in contract.types() and (not fid or fid in contract.slots(wid, "fields"))
        if not ok:
            raise SpaceError("--map 要写成 <类型槽位>=<空间里的类型名> 或 <类型槽位>.<字段槽位>=<空间里的字段名>：%s" % item)
        maps[key] = value
    return maps


def bind(contract, snap, old=None, maps=None):
    """Fill the contract's slots from a snapshot. Returns (binding, problems): binding is
    {types, field_keys, roles, states} keyed by slot; problems are [对象, 问题, 怎么办] rows.
    A slot is matched by --map, else by what was bound last time while it still exists, else
    by its identifier, else by its name; exactly one object must match."""
    old, maps = old or {}, maps or {}
    space = snap.get("types") or {}
    out = {"types": {}, "field_keys": {}, "roles": {}, "states": {}}
    problems = []
    for wid in contract.types():
        want = contract.name("types", wid)
        asked, kept = maps.get(wid), ((old.get("types") or {}).get(wid) or {}).get("type_key")
        if asked:
            hits = [k for k, t in space.items() if asked in (k, t.get("name"), t.get("api_name"))]
        elif kept in space:
            hits = [kept]
        else:
            hits = [k for k, t in space.items() if t.get("api_name") == wid] or \
                   [k for k, t in space.items() if t.get("name") == want]
        if len(hits) != 1:
            off = [t for t in snap.get("disabled_types") or [] if wid == t.get("api_name") or want == t.get("name")]
            if off:
                problems.append([want, "空间里这个类型是停用的", "在空间配置里启用“%s”" % want])
            elif hits:
                problems.append([want, "空间里有 %d 个对得上的类型" % len(hits), "用 `--map %s=<type_key>` 指明用哪一个" % wid])
            else:
                problems.append([want, "按“%s”找不到类型" % asked if asked else "空间里没有这个类型",
                                 "%s：模板要有“%s”类型；已经有、只是名字不同的，用 `--map %s=<空间里的类型名>` 指认" % (MAINTAINER, want, wid)])
            continue
        t = space[hits[0]]
        out["types"][wid] = {"type_key": hits[0], "api_name": t.get("api_name"), "name": t.get("name")}
        _bind_fields(contract, wid, t, old, maps, out, problems)
        _bind_roles(contract, wid, t, old, out, problems)
        _bind_states(contract, wid, t, old, out, problems)
    return out, problems


def _bind_fields(contract, wid, t, old, maps, out, problems):
    custom = [f for f in t.get("fields") or [] if str(f.get("field_key") or "").startswith("field_")]
    by_key = {f["field_key"]: f for f in custom}
    for slot in contract.slots(wid, "fields"):
        d, name = contract.field(slot), contract.name("fields", slot)
        label = "%s的“%s”" % (contract.name("types", wid), name)
        asked, kept = maps.get("%s.%s" % (wid, slot)), ((old.get("field_keys") or {}).get(wid) or {}).get(slot)
        if asked:
            hits = [f for f in custom if asked in (f["field_key"], f.get("name"))]
        elif kept in by_key:
            hits = [by_key[kept]]
        else:
            hits = [f for f in custom if f.get("alias") == slot] or [f for f in custom if f.get("name") == name]
        if not hits:
            problems.append([label, "按“%s”找不到字段" % asked if asked else "空间里没有",
                             "%s：这个类型要有%s字段“%s”；已经有、只是名字不同的，用 `--map %s.%s=<空间里的字段名>` 指认" % (
                                 MAINTAINER, FIELD_TYPES.get(d["type"], d["type"]), name, wid, slot)])
            continue
        if len(hits) > 1:
            problems.append([label, "有 %d 个对得上的字段（%s）" % (len(hits), "、".join(f["field_key"] for f in hits)),
                             "用 `--map %s.%s=<field_key>` 指明用哪一个" % (wid, slot)])
            continue
        f = hits[0]
        got = f.get("field_type")
        if got in KNOWN_FIELD_TYPES and got not in FIELD_TYPE_ALIASES.get(d["type"], {got}):
            problems.append([label, "空间里的类型是 %s，约定的是%s" % (got, FIELD_TYPES.get(d["type"], d["type"])),
                             "%s：改成对的类型，或者另建一个字段再指认" % MAINTAINER])
            continue
        out["field_keys"].setdefault(wid, {})[slot] = f["field_key"]
        have = [o.get("name") for o in f.get("options") or []]
        missing = [o for o in contract.option_names(slot) if o not in have]
        if missing:
            problems.append([label, "缺选项：%s" % "、".join(missing),
                             "%s：在空间里加上这些选项；现有：%s" % (MAINTAINER, "、".join(have) or "无")])


def _bind_roles(contract, wid, t, old, out, problems):
    roles = t.get("roles") or []
    ids = {r.get("role_id") for r in roles}
    for slot in contract.slots(wid, "roles"):
        name = contract.name("roles", slot)
        kept = ((old.get("roles") or {}).get(wid) or {}).get(slot)
        hits = [kept] if kept in ids else ([slot] if slot in ids else [r["role_id"] for r in roles if r.get("role_name") == name])
        if len(hits) != 1:
            problems.append(["%s的角色“%s”" % (contract.name("types", wid), name),
                             "空间里没有" if not hits else "有 %d 个同名的角色" % len(hits),
                             "%s：这个类型要有角色“%s”（角色 id %s）" % (MAINTAINER, name, slot)])
            continue
        out["roles"].setdefault(wid, {})[slot] = hits[0]


def _bind_states(contract, wid, t, old, out, problems):
    """A state slot by what was bound last time while its id is still there, else by name: the reference
    template's name first, then the names the state had in earlier templates (Contract.names)."""
    states = t.get("states") or []
    by_id = {s["id"]: s for s in states}
    named = lambda n: [s for s in states if s.get("name") == n]  # noqa: E731
    for slot in contract.slots(wid, "states"):
        names = contract.names("states", slot)
        kept = (((old.get("states") or {}).get(wid) or {}).get(slot) or {}).get("id")
        hits = [by_id[kept]] if kept in by_id else next((named(n) for n in names if named(n)), [])
        if len(hits) != 1:
            problems.append(["%s的状态“%s”" % (contract.name("types", wid), names[0]),
                             "空间里没有" if not hits else "有 %d 个同名的状态" % len(hits),
                             "%s：这个类型的流程要有状态%s；现有：%s" % (
                                 MAINTAINER, "或".join("“%s”" % n for n in names),
                                 "、".join(s.get("name") or "" for s in states) or "无")])
            continue
        out["states"].setdefault(wid, {})[slot] = {"id": hits[0]["id"], "name": hits[0].get("name")}


# ----------------------------------------------------------------- check
def check(contract, snap, binding):
    """Problems the binding does not show: what the flows must look like for the scripts to
    drive them. Returns (problems, notes); notes do not fail the check."""
    problems, notes = [], []
    arch = contract.flow["archive_node"]
    for wid, slot in (binding.get("types") or {}).items():
        t = type_of(snap, slot["type_key"])
        cn = contract.name("types", wid)
        role_ids = set(((binding.get("roles") or {}).get(wid) or {}).values())
        scribe = ((binding.get("roles") or {}).get(wid) or {}).get(arch["role"])
        if not t.get("flows"):
            problems.append([cn, "读不到流程", "%s：这个类型要是节点模式，至少有一个流程" % MAINTAINER])
        for tid, flow in (t.get("flows") or {}).items():
            what = "%s的流程“%s”" % (cn, flow.get("name"))
            nodes = flow.get("nodes")
            if nodes is None:
                notes.append("%s还没有工作项，流程图读不到；登记第一条以后重读" % what)
                continue
            ends = [n for n in nodes if not n["next"]]
            if len(ends) != 1 or (scribe and ends[0]["roles"] != [scribe]):
                problems.append([what, "最后一个节点要正好一个，负责角色是“%s”" % contract.name("roles", arch["role"]),
                                 "%s：每个流程以归档节点收尾，由%s确认" % (MAINTAINER, contract.name("roles", arch["role"]))])
            for n in nodes:
                unknown = [r for r in n["roles"] if r not in role_ids]
                if unknown:
                    problems.append(["%s的节点“%s”" % (what, n.get("name")), "负责角色 %s 不在约定里" % "、".join(unknown),
                                     "%s：节点的负责角色用约定里的角色，或者给约定加角色槽位" % MAINTAINER])
                if len(n["roles"]) != 1:
                    problems.append(["%s的节点“%s”" % (what, n.get("name")), "要正好一个负责角色，现在是 %d 个" % len(n["roles"]),
                                     "%s：每个节点联动一个角色" % MAINTAINER])
        keys = (binding.get("field_keys") or {}).get(wid) or {}
        required = {c["field_key"] for c in t.get("create_fields") or [] if c.get("required")}
        outside = [c["name"] for c in t.get("create_fields") or []
                   if c.get("required") and c["field_key"] in required - set(keys.values())]
        if outside:
            notes.append("%s新建时必填的“%s”不在约定里：登记时用 --set 按字段名传" % (cn, "”“".join(outside)))
    # 1.1.0: a 同序列需求 starts with the plan. The +0 it is handed to uploads one, and the +1 that sent it confirms
    # the first node, whose form requires the plan: this is where the middle rank a sequence no longer has decided the method.
    gate = contract.flow.get("plan_gate") or {}
    held = gate.get("type")
    if held in (binding.get("types") or {}):
        t = type_of(snap, binding["types"][held]["type_key"])
        key = ((binding.get("field_keys") or {}).get(held) or {}).get(gate["field"])
        role = ((binding.get("roles") or {}).get(held) or {}).get(gate["role"])
        for flow in (t.get("flows") or {}).values():
            first = next((n for n in flow.get("nodes") or [] if not n["prev"]), None)
            if key and role and first and (first["roles"] != [role] or key not in [r["field_key"] for r in first["required"]]):
                problems.append(["%s的流程“%s”的第一个节点“%s”" % (contract.name("types", held), flow.get("name"), first.get("name")),
                                 "负责角色要是“%s”，完成时必填“%s”：承接的会话先交方案，发出方确认了才执行" % (
                                     contract.name("roles", gate["role"]), contract.name("fields", gate["field"])),
                                 "%s：把这个节点的负责角色改成“%s”，在它的完成表单里把“%s”设成必填" % (
                                     MAINTAINER, contract.name("roles", gate["role"]), contract.name("fields", gate["field"]))])
    main = contract.main_line()
    if main in (binding.get("types") or {}):
        t = type_of(snap, binding["types"][main]["type_key"])
        keys = (binding.get("field_keys") or {}).get(main) or {}
        every = list((t.get("flows") or {}).values())
        flows = [f for f in every if f.get("nodes")]
        for rule, slot in (("签字后冻结", contract.flow["freeze"]["sealed_by"]), ("结论前核对", contract.flow["freeze"]["checked_before"])):
            key = keys.get(slot)
            held = any(key in [r["field_key"] for r in n["required"]] for f in flows for n in f["nodes"])
            if key and flows and not held and len(flows) == len(every):      # a flow not read yet may be the one that holds it
                problems.append(["%s的“%s”" % (contract.name("types", main), contract.name("fields", slot)),
                                 "没有节点把它设成必填，%s做不了" % rule,
                                 "%s：在对应节点的完成表单里把它设成必填" % MAINTAINER])
    return problems, notes


def diff(old, new):
    """What changed between two snapshots of a space, as lines for a person."""
    lines = []
    a, b = old.get("types") or {}, new.get("types") or {}
    for key in sorted(set(a) | set(b)):
        name = (b.get(key) or a.get(key)).get("name")
        if key not in b:
            lines.append("类型“%s”没有了（或停用了）" % name)
            continue
        if key not in a:
            lines.append("多了类型“%s”" % name)
            continue
        for label, attr, ident, show in (("字段", "fields", "field_key", "name"), ("角色", "roles", "role_id", "role_name"),
                                         ("状态", "states", "id", "name")):
            x = {i[ident]: i for i in a[key].get(attr) or []}
            y = {i[ident]: i for i in b[key].get(attr) or []}
            gone, more = [x[k][show] for k in x if k not in y], [y[k][show] for k in y if k not in x]
            renamed = ["%s→%s" % (x[k][show], y[k][show]) for k in x if k in y and x[k][show] != y[k][show]]
            if gone:
                lines.append("%s：少了%s %s" % (name, label, "、".join(gone)))
            if more:
                lines.append("%s：多了%s %s" % (name, label, "、".join(more)))
            if renamed:
                lines.append("%s：%s改名 %s" % (name, label, "、".join(renamed)))
            if attr == "fields":
                for k in x:
                    if k in y and [o["name"] for o in x[k].get("options") or []] != [o["name"] for o in y[k].get("options") or []]:
                        lines.append("%s：字段“%s”的选项变了" % (name, y[k][show]))
        fa, fb = a[key].get("flows") or {}, b[key].get("flows") or {}
        for tid in sorted(set(fa) | set(fb)):
            fname = (fb.get(tid) or fa.get(tid)).get("name")
            if tid not in fb or tid not in fa:
                lines.append("%s：%s流程“%s”" % (name, "少了" if tid not in fb else "多了", fname))
                continue
            na, nb = fa[tid].get("nodes"), fb[tid].get("nodes")
            if na is None or nb is None:
                continue
            shape = lambda ns: [(n["key"], n["roles"], sorted(r["field_key"] for r in n["required"])) for n in ns]  # noqa: E731
            if shape(na) != shape(nb):
                lines.append("%s：流程“%s”的节点、负责角色或必填项变了" % (name, fname))
    return lines


# -------------------------------------------------------------- markdown
def flows_md(snap, binding=None, types=None):
    """The flows as 节点、负责角色、交付物 tables: what org/phases.md and space.md show."""
    lines = []
    for type_key, t in (snap.get("types") or {}).items():
        if types is not None and type_key not in types:
            continue
        role_name = {r["role_id"]: r["role_name"] for r in t.get("roles") or []}
        for tid, flow in (t.get("flows") or {}).items():
            title = t["name"] if len(t["flows"]) == 1 else "%s：%s" % (t["name"], flow.get("name"))
            lines += ["### %s" % title, ""]
            if flow.get("nodes") is None:
                lines += ["这个流程还没有工作项，流程图读不到；登记第一条以后重读。", ""]
                continue
            rows = [[str(i), n.get("name"), "、".join(role_name.get(r, r) for r in n["roles"]) or "无",
                     "、".join(r.get("name") or r["field_key"] for r in n["required"]) or "无"]
                    for i, n in enumerate(flow["nodes"], 1)]
            lines += [orgspec.md_table(["步", "节点", "负责角色", "完成时必填"], rows), ""]
    return "\n".join(lines)


def to_markdown(snap, contract=None, binding=None):
    lines = ["# 空间结构：%s" % (snap.get("space_name") or snap["project_key"]), "",
             "> 读取时间：%s。由 `space.py snapshot` 从空间读出，只读。流程图读自各流程的一条工作项；"
             "写工作项时以 field_key 为准。空间配置改了就重读。" % snap.get("read_at"), ""]
    if snap.get("disabled_types"):
        lines += ["停用的类型：%s。" % "、".join(t["name"] for t in snap["disabled_types"]), ""]
    if contract is not None and binding is not None:
        lines += ["## 约定的槽位对到哪里", "", binding_md(contract, snap, binding), ""]
    lines += ["## 流程", "", flows_md(snap)]
    for type_key, t in (snap.get("types") or {}).items():
        lines += ["## %s（`%s`，`%s`）" % (t["name"], t.get("api_name"), type_key), ""]
        if t.get("roles"):
            lines += ["角色：" + "、".join("%s（`%s`）" % (r["role_name"], r["role_id"]) for r in t["roles"]), ""]
        if t.get("states"):
            lines += ["状态：" + "、".join(s["name"] for s in t["states"]), ""]
        must = [c["name"] for c in t.get("create_fields") or [] if c.get("required")]
        lines += ["新建时必填：" + ("、".join(must) or "无"), ""]
        rows = [[f["name"], "`%s`" % f["field_key"], f.get("field_type") or "", "、".join(o["name"] for o in f.get("options") or [])]
                for f in t.get("fields") or []]
        lines += [orgspec.md_table(["字段", "field_key", "类型", "选项"], rows), ""]
    return "\n".join(lines)


def structure_md(snap):
    """A template's structure for its own documentation: names, identifiers a builder types in
    (system identifier, role id, node id), field types and options; none of the space's keys."""
    lines = []
    for t in (snap.get("types") or {}).values():
        role_name = {r["role_id"]: r["role_name"] for r in t.get("roles") or []}
        lines += ["## %s（系统标识 `%s`）" % (t["name"], t.get("api_name")), "",
                  "- 角色：%s" % ("、".join("%s（`%s`）" % (r["role_name"], r["role_id"]) for r in t.get("roles") or []) or "无"),
                  "- 状态：%s" % "、".join(s["name"] for s in t.get("states") or []),
                  "- 新建时必填：%s" % ("、".join(c["name"] for c in t.get("create_fields") or [] if c.get("required")) or "无"), ""]
        steps = {}
        for flow in (t.get("flows") or {}).values():
            many = len(t["flows"]) > 1
            lines += ["### 流程%s" % ("：" + flow["name"] if many else ""), ""]
            if flow.get("nodes") is None:
                lines += ["这个流程还没有工作项，流程图读不到。", ""]
                continue
            rows = []
            for i, n in enumerate(flow["nodes"], 1):
                rows.append([str(i), n.get("name"), "`%s`" % n["key"],
                             "、".join("%s（`%s`）" % (role_name.get(r, r), r) for r in n["roles"]),
                             "、".join(r.get("name") or "" for r in n["required"]) or "无"])
                for r in n["required"]:
                    steps.setdefault(r["field_key"], []).append((flow["name"] + "·" if many else "") + (n.get("name") or ""))
            lines += [orgspec.md_table(["步", "节点", "节点 ID", "负责角色", "完成时必填"], rows), ""]
        must = {c["field_key"] for c in t.get("create_fields") or [] if c.get("required")}
        rows = [[f["name"], f.get("field_type") or "", "、".join(o["name"] for o in f.get("options") or []),
                 "是" if f["field_key"] in must else "", "、".join(steps.get(f["field_key"], []))]
                for f in t.get("fields") or [] if str(f.get("field_key") or "").startswith("field_")]
        lines += ["### 字段", "", orgspec.md_table(["字段", "类型", "选项", "新建时必填", "哪一步必填"], rows), ""]
    return "\n".join(lines)


def binding_md(contract, snap, binding):
    rows = []
    for wid in contract.types():
        slot = (binding.get("types") or {}).get(wid)
        counts = ["%d / %d" % (len((binding.get(k) or {}).get(wid) or {}), len(contract.slots(wid, kind)))
                  for k, kind in (("field_keys", "fields"), ("roles", "roles"), ("states", "states"))]
        rows.append([contract.name("types", wid), "%s（`%s`）" % (slot["name"], slot["type_key"]) if slot else "没绑定"]
                    + (counts if slot else ["—"] * 3))
    return orgspec.md_table(["类型槽位", "空间里的类型", "字段", "角色", "状态"], rows)


def problems_md(problems, notes=()):
    out = []
    if problems:
        out += [orgspec.md_table(["对象", "问题", "怎么办"], problems)]
    out += ["提醒：%s。" % n for n in notes]
    return "\n".join(out)


# ------------------------------------------------------------------ files
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def dump_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def apply_binding(ids, contract, binding):
    """Put a binding into ids.json's meegle section; the space's own keys stay as they are."""
    ids["schema"] = IDS_SCHEMA
    m = ids.setdefault("meegle", {})
    m.update({k: binding.get(k) or {} for k in ("types", "field_keys", "roles", "states")})
    m["contract_version"] = contract.version
    return ids


def binding_of(ids):
    m = (ids or {}).get("meegle") or {}
    return {k: m.get(k) or {} for k in ("types", "field_keys", "roles", "states")}


def stale(contract, binding, fresh):
    """Problems where feishu/ids.json records something else than the snapshot now shows:
    any session may re-read the snapshot, the binding is written in one place only, so a
    renamed state or a slot the space has since gained leaves the ledger on old names."""
    rows, fix = [], "重跑 space.py feishu --target <项目目录> --live，核对后加 --apply"
    shown = lambda v: "“%s”" % v.get("name") if isinstance(v, dict) else str(v)
    for wid, new in (fresh.get("types") or {}).items():
        cn, was = contract.name("types", wid), (binding.get("types") or {}).get(wid)
        if not was:
            rows.append([cn, "空间里有这个类型，绑定里还没记", fix])
            continue
        if was != new:
            rows.append([cn, "空间里现在是“%s”（%s），绑定记的是“%s”（%s）" % (
                new.get("name"), new.get("api_name"), was.get("name"), was.get("api_name")), fix])
        for kind, group, label in (("field_keys", "fields", "字段"), ("roles", "roles", "角色"), ("states", "states", "状态")):
            old, now_ = (binding.get(kind) or {}).get(wid) or {}, (fresh.get(kind) or {}).get(wid) or {}
            for slot, value in now_.items():
                what = "%s的%s“%s”" % (cn, label, contract.name(group, slot))
                if slot not in old:
                    rows.append([what, "空间里有，绑定里还没记", fix])
                elif old[slot] != value:
                    rows.append([what, "空间里现在是 %s，绑定记的是 %s" % (shown(value), shown(old[slot])), fix])
    return rows


def find_project(wanted):
    """The one space whose name, simple_name or project_key equals wanted."""
    data = call(["project", "search", "--project-key", wanted])
    projects = data.get("projects") if isinstance(data, dict) else data
    projects = [p for p in projects or [] if isinstance(p, dict)]
    hits = [p for p in projects if wanted in (p.get("name"), p.get("simple_name"), p.get("project_key"))]
    if len(hits) != 1:
        raise SpaceError("按“%s”找到 %d 个飞书项目空间，要正好一个" % (wanted, len(hits)))
    return hits[0]


def report(contract, snap, binding, problems, notes):
    print(binding_md(contract, snap, binding))
    print()
    if problems or notes:
        print(problems_md(problems, notes))
        print()
    print("约定核对（contract_version %d）：%s" % (
        contract.version, "%d 个问题" % len(problems) if problems else "通过"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("feishu", "template"):
        # binding a project and checking a template need what setup_core.py knows about the project; they are in
        # space_cmd.py, which exists where the plugin is installed and not among a project's copies
        try:
            import space_cmd
        except ImportError:
            print("出错：space.py %s 从插件安装目录运行：python3.12 -I <插件目录>/scripts/space.py %s …" % (argv[0], argv[0]))
            return 2
        return space_cmd.main(argv)
    ap = argparse.ArgumentParser(description="读飞书项目空间的结构，把插件的约定绑定到它")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("feishu", "template"):           # carried out by space_cmd.py, above; listed here for --help
        sub.add_parser(name)
    p = sub.add_parser("export")
    p.add_argument("--space", required=True, help="要读的空间（模板）：名字、simple_name 或 project_key")
    p.add_argument("--out", required=True, help="快照写到这个文件")
    for name in ("snapshot", "bind", "check", "show"):
        p = sub.add_parser(name)
        p.add_argument("--target", help="项目目录")
        if name == "snapshot":
            p.add_argument("--apply", action="store_true")
        if name == "bind":
            p.add_argument("--map", action="append", default=[])
        if name == "check":
            p.add_argument("--space", help="不看项目目录，直接读这个空间（模板）并核对；什么都不写")
        if name == "show":
            p.add_argument("--snapshot", help="不看项目目录，直接读这份快照文件")
            p.add_argument("what", nargs="?", default="flows", choices=("flows", "fields", "binding", "structure"))
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    try:
        if args.cmd == "export":
            snap = snapshot(find_project(args.space), wanted_by(contract))
            write(os.path.abspath(args.out), dump_json(snap))
            binding, _ = bind(contract, snap)
            for note in check(contract, snap, binding)[1]:
                print("提醒：" + note)
            print("空间“%s”的快照写到 %s。文件里有这个空间的各种标识；要放进仓库的是不带标识的结构：\n"
                  "  space.py show --snapshot %s structure" % (snap.get("space_name"), args.out, args.out))
            return 0
        if args.cmd == "check" and args.space:
            snap = snapshot(find_project(args.space), wanted_by(contract))
            binding, problems = bind(contract, snap)
            more, notes = check(contract, snap, binding)
            print("空间“%s”（simple_name %s，project_key %s）。只读了飞书，没写任何文件。" % (
                snap.get("space_name"), snap.get("simple_name"), snap["project_key"]))
            print()
            report(contract, snap, binding, problems + more, notes)
            return 1 if problems + more else 0
        if args.cmd == "show" and args.snapshot:
            snap = load_json(args.snapshot)
            if not snap:
                raise SpaceError("读不了快照 %s" % args.snapshot)
            if args.what == "binding":
                raise SpaceError("快照文件里没有绑定：binding 要给 --target <项目目录>")
            print({"flows": flows_md, "fields": to_markdown, "structure": structure_md}[args.what](snap))
            return 0
        if not args.target:
            raise SpaceError("要给 --target <项目目录>")
        fdir = os.path.join(os.path.abspath(args.target), "feishu")
        ids = load_json(os.path.join(fdir, "ids.json")) or {}
        m = ids.get("meegle") or {}
        if args.cmd == "snapshot":
            if not m.get("project_key"):
                raise SpaceError("feishu/ids.json 里没有 project_key：先跑 space.py feishu --live --apply 找到项目空间")
            old = load_json(os.path.join(fdir, "space.json"))
            snap = snapshot({"project_key": m["project_key"], "simple_name": m.get("simple_name"), "name": m.get("space_name")},
                            wanted_by(contract, old=binding_of(ids)))
            for line in diff(old, snap) if old else []:
                print("变化：" + line)
            if args.apply:
                write(os.path.join(fdir, "space.json"), dump_json(snap))
                write(os.path.join(fdir, "space.md"), to_markdown(snap, contract, binding_of(ids)) + "\n")
                print("已写入 feishu/space.json 和 feishu/space.md。")
            else:
                print(flows_md(snap))
                print("这是预览。加 --apply 写入 feishu/space.json 和 feishu/space.md。")
            return 0
        snap = load_json(os.path.join(fdir, "space.json"))
        if not snap:
            raise SpaceError("还没有 feishu/space.json：先跑 space.py snapshot --target <项目目录> --apply")
        if args.cmd == "bind":
            binding, problems = bind(contract, snap, binding_of(ids), parse_maps(contract, args.map))
            more, notes = check(contract, snap, binding)
            report(contract, snap, binding, problems + more, notes)
            print("这是预览，照本地的快照算的。绑定由研究员的会话写入：space.py feishu --target <项目目录> --live，核对后加 --apply。")
            return 1 if problems + more else 0
        binding = binding_of(ids)
        if args.cmd == "check":
            fresh, problems = bind(contract, snap, binding)
            problems += stale(contract, binding, fresh)
            more, notes = check(contract, snap, binding)
            if m.get("contract_version") not in (None, contract.version):
                problems.append(["feishu/ids.json", "绑定按约定第 %s 版做的，插件现在是第 %d 版" % (m.get("contract_version"), contract.version),
                                 "重跑 space.py feishu --target <项目目录> --live，核对后加 --apply"])
            report(contract, snap, binding, problems + more, notes)
            return 1 if problems + more else 0
        if args.what == "binding":
            print(binding_md(contract, snap, binding))
        else:
            print({"flows": flows_md, "fields": to_markdown, "structure": structure_md}[args.what](snap))
        return 0
    except (SpaceError, orgspec.OrgSpecError) as e:
        print("出错：%s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
