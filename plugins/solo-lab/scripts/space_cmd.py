#!/usr/bin/env python3
"""Bind a project to its Feishu space, and check a space against the contract. Run as space.py feishu and
space.py template, where the plugin is installed: they need what setup_core.py knows about the project, and
space.py itself is loaded by the ledger scripts.

    python3.12 -I scripts/space.py feishu --target DIR [--map ID=NAME ...] [--live] [--apply]
    python3.12 -I scripts/space.py template [--space NAME] [--target DIR]

feishu binds the project to the space spec/task.json names. With --live it reads the
space's structure (space.py snapshot), fills the slots of the contract
(orgspec/contract.json) by --map, else what was bound last time, else identifier, else
name, and checks that the flows can be driven. It writes feishu/ids.json (the binding),
feishu/space.json and space.md (the structure), feishu/binding.md (what is missing) and
org/phases.md (the flows of the main-line type). It only reads Feishu; the ledger
refuses to touch Feishu until a binding has been applied. When no type matches, it
points to creating the space from the template, and --apply records the space without
a binding time.

template runs the same contract check on any space, such as the one a template is
published from (--space, or feishu.template_space in the task file). It only reads
Feishu and writes nothing.
Python 3.12 standard library only.
"""
import argparse
import datetime
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger_core  # noqa: E402
import pool as pool_state  # noqa: E402
import space  # noqa: E402
import setup_core  # noqa: E402

def old_owners_out(target):
    """A project that moved to another space: the work items its sessions answer for (pool.json owners) are
    the old space's, and the ledger reads none of them any more. `pool.py show --prune` stops when it cannot
    read an item, so they could never be taken out; the switch takes them out, and writes them into the log.
    Returns what it took out, {} when there was nothing or the file cannot be read."""
    try:
        with pool_state.locked(target) as pl:
            left = dict(pl.get("owners") or {})
            pl["owners"] = {}
    except (pool_state.PoolError, OSError):
        return {}
    if left:
        pool_state.log_event(target, {"event": "owners_out", "why": "space_switch", "owners": left})
    return left


# -------------------------------------------------------------- feishu
def normalise_ids(raw):
    """ids.json as this version writes it. Any other shape starts empty: bind again with --live.
    A binding time without a single bound type counts as not bound."""
    if raw.get("schema") == space.IDS_SCHEMA:
        m = raw.get("meegle") or {}
        if m.get("bound_at") and not m.get("types"):
            m.pop("bound_at")
            return raw, ["feishu/ids.json 记着绑定时间，却一种类型都没绑上，按没绑定处理"]
        return raw, []
    notes = ["feishu/ids.json 不是这一版的格式：这一版只认用节点流程的空间，按空间重新绑定；--apply 时原文件备份成 ids.legacy.json"] if raw else []
    return {"schema": space.IDS_SCHEMA, "lark": {}, "meegle": {}, "wiki": {}}, notes


def binding_doc(spec, contract, ids, snap, problems=None, notes=(), changes=(), switch=None, seqs=None):
    """feishu/binding.md: which object of the space fills each slot of the contract, what is
    missing, and the knowledge-base nodes and read-back values the project still needs."""
    m = ids.get("meegle") or {}
    lines = ["# 绑定与核对清单", "",
             "> 由 `space.py feishu` 生成。插件按含义处理的类型、字段、角色和状态写在约定文件里（`.claude/solo-lab/orgspec/contract.json`）；"
             "这里列每个槽位对到空间里的哪个对象。流程、节点和必填项不在这里：它们以空间为准，读出来的结构在 `feishu/space.md`。", ""]
    if switch:
        lines += switch_md(switch)
    lines += ["## 绑定结果", "", space.binding_md(contract, snap or {}, space.binding_of(ids)), ""]
    lines += ["## 要处理的问题", ""]
    if problems is None:
        lines += ["还没读过空间，没法核对：先跑 `space.py feishu --live`。", ""]
    else:
        lines += [orgspec.md_table(["对象", "问题", "怎么办"], problems) if problems else "无。", ""]
        if problems:
            lines += ["空间由模板建出来，缺的东西要在模板里补：把上表报给模板维护者，模板更新后重建或补齐空间，再重跑 `space.py feishu --live`。"
                      "空间里已经有、只是名字不同的，用 `--map <类型槽位>=<空间里的类型名>` 或 `--map <类型槽位>.<字段槽位>=<空间里的字段名>` 指认。", ""]
    if notes or changes:
        lines += ["## 提醒", "", setup_core.bullets(list(notes) + ["空间结构和上次读到的不一样：%s" % c for c in changes]), ""]
    rows = []
    nodes = (ids.get("wiki") or {}).get("nodes") or {}
    renames = False
    for key in spec.node_keys(seqs):
        head, _, seq = key.partition("/")
        title = spec.term("node", head) + ("/" + spec.term("seq", seq) if seq else "")
        n = nodes.get(key)
        if not n:
            rows.append([title, "没有", "书记员建节点：`docs.py ensure-node %s`" % key])
        elif seq and n.get("title", "").split("/")[-1] != spec.term("seq", seq):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("seq", seq)])
        elif not seq and n.get("title", "").split("/")[0] != spec.term("node", head):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("node", head)])
    lines += ["## 知识库节点", "", orgspec.md_table(["节点", "现状", "要做的"], rows) if rows else "无差异。", ""]
    if any(r[1] == "没有" for r in rows):
        lines += ["缺的节点可以一次建齐：`docs.py ensure-node all`，先预览，再加 `--apply`。它先建一级节点，再建各序列的节点；"
                  "知识空间里已经有同名节点的直接用，不再建。要先有知识空间：`space.py feishu --live --apply` 找不到同名的知识空间就新建一个。", ""]
    if renames:
        lines += ["命令行没有给知识库节点改名的子命令；节点标题跟着节点文档的一级标题走。先读一遍节点文档：是空的，书记员可以写入只有新标题的正文："
                  "`lark-cli docs +update --doc <节点链接> --command overwrite --doc-format markdown --content '# <新标题>' --as user`；"
                  "有内容的，请研究员在网页上改标题。", ""]
    lark = ids.get("lark") or {}
    wiki = ids.get("wiki") or {}
    readback = [
        ["project_key", "有" if m.get("project_key") else "缺", "跑 `space.py feishu --live --apply`：按 `spec/task.json` 里的空间名精确查找"],
        ["simple_name", "有" if m.get("simple_name") else "缺", "同上；工作项链接要用它"],
        ["研究员的 open_id", "有" if lark.get("researcher_open_id") else "缺", "同上：从 `lark-cli whoami` 读；没有它，飞书私聊监听不启动"],
        ["知识空间 space_id", "有" if wiki.get("space_id") else "缺", "先在已有的知识空间里找（名字可能不一样），确认没有再建，再重跑 `space.py feishu --live --apply`"],
    ]
    lines += ["## 读回", "", orgspec.md_table(["项", "现状", "怎么补"], readback), ""]
    return "\n".join(lines)


def cli_json(cmd, timeout=60):
    code, out, err = setup_core.run(cmd, timeout)
    if code != 0:
        raise setup_core.SetupError("%s 失败（退出码 %s）：%s" % (" ".join(cmd[:4]), code, (err or out or "")[-300:]))
    try:
        return json.loads(out)
    except ValueError:
        raise setup_core.SetupError("%s 的输出不是 JSON" % " ".join(cmd[:4]))


def search_hits(data, wanted):
    """The projects in a `meegle project search` answer, and those whose name, simple_name or
    project_key equals wanted."""
    projects = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(projects, list):
        projects = [d for d in ledger_core._walk(data) if isinstance(d, dict) and d.get("project_key")]
    projects = [p for p in projects if isinstance(p, dict)]
    return projects, [p for p in projects if wanted in (p.get("name"), p.get("simple_name"), p.get("project_key"))]


def find_project(data, wanted, fix=None):
    """The one project in `meegle project search` whose name, simple_name or project_key equals wanted."""
    projects, hits = search_hits(data, wanted)
    if len(hits) != 1:
        cands = "；".join("%s（simple_name %s，project_key %s）" % (p.get("name"), p.get("simple_name"), p.get("project_key"))
                          for p in projects or []) or "无"
        raise setup_core.SetupError("按“%s”找到 %d 个飞书项目空间，要正好一个。候选：%s。%s" % (wanted, len(hits), cands, fix or (
            "把准确的 simple_name 或 project_key 写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply"
            "（spec/task.json 跟着更新；手改过它的，init 会写成 spec/task.json.new，对照合并），再重跑这条命令")))
    return hits[0]


def optional_read(what, fn):
    """Run one optional read-back step; a failure becomes a reminder, not an abort."""
    try:
        return fn()
    except setup_core.SetupError as e:
        print("提醒：没能读回%s：%s" % (what, e))
        return None


def old_space_items(spec, project_key, old):
    """What a space the project is leaving still holds, for the types its binding had: (types
    with any work item, open work items as lines, types that could not be checked)."""
    held, open_items, unchecked = [], [], []
    stop = ledger_core.system_states(spec)
    for wid, t in (old.get("types") or {}).items():
        tkey = t.get("type_key")
        if not tkey:
            continue
        name = spec.term("wi", wid) if wid in spec.terms["wi"] else t.get("name") or wid
        done = [s["name"] for slot, s in ((old.get("states") or {}).get(wid) or {}).items() if slot == "archived" and s.get("name")]

        def query(mql):
            return ledger_core.query_rows(cli_json(["meegle", "workitem", "query", "--project-key", project_key,
                                               "--mql", mql, "--format", "json"]))
        try:
            rows = query("SELECT `work_item_id`, `name`, `work_item_status` FROM `%s`.`%s` WHERE %s LIMIT 20" % (
                project_key, tkey, ledger_core._not_in(done + stop)))
            anything = rows or query("SELECT `work_item_id` FROM `%s`.`%s` LIMIT 1" % (project_key, tkey))
        except (setup_core.SetupError, ledger_core.LedgerError) as e:
            print("提醒：没能查旧空间里的%s：%s" % (name, e))
            unchecked.append(name)
            continue
        if anything:
            held.append(name)
        open_items += ["%s #%s %s（%s）" % (name, r.get("work_item_id"), r.get("name") or "", r.get("work_item_status") or "")
                       for r in rows]
    return held, open_items, unchecked


def template_hint(contract, task_feishu, name):
    """How to make the project space from the template; the link is the task file's, else the contract's."""
    url = (task_feishu or {}).get("template_url") or contract.template.get("url")
    if not url:
        return "照 setup 技能用参考模板创建空间，空间名填“%s”" % name
    return "打开模板 %s，点“使用模板”，空间名填“%s”，“实例数据”勾选“视图”，其余用默认值" % (url, name)


def read_back(spec, contract, ids, task_feishu, maps, switch=None, old_snap=None, create=False):
    """Find the project's space, read its structure, bind the contract to it, and read open_id,
    space_id and node titles. Returns (snapshot, problems, notes, changes since the last
    snapshot). The space the task file names is looked up every time: a name can come to mean
    another space, a key cannot. When it is not the space bound last time, the old binding is
    dropped and `switch` gets both spaces and what the old one still holds."""
    m = ids["meegle"]
    wanted = str(task_feishu.get("meegle_space") or "").strip() or m.get("project_key")
    if not wanted:
        raise setup_core.SetupError("ids.json 里没有 project_key，研究任务里也没写飞书项目空间：把空间名写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply，再重跑这条命令")
    data = cli_json(["meegle", "project", "search", "--project-key", wanted, "--format", "json"])
    if not search_hits(data, wanted)[1] and not m.get("project_key"):
        raise setup_core.SetupError("没有叫“%s”的飞书项目空间，什么都没写。用模板建一个：%s；建好以后重跑这条命令。" % (
            wanted, template_hint(contract, task_feishu, wanted)))
    if not search_hits(data, wanted)[1] and m.get("project_key") and wanted == m.get("space_name"):
        print("提醒：按“%s”找不到空间，可能在飞书里改了名；接着用上次绑定的空间（simple_name %s，project_key %s）。"
              "把任务文件的 feishu.meegle_space 改成新名字或 simple_name" % (wanted, m.get("simple_name"), m["project_key"]))
        hit = {"project_key": m["project_key"], "simple_name": m.get("simple_name"), "name": m.get("space_name")}
    else:
        hit = find_project(data, wanted)
    same = not m.get("project_key") or hit.get("project_key") == m["project_key"]
    if not same:
        print("提醒：任务文件里的空间“%s”不是上次绑定的“%s”，这是换空间：按新空间重新绑定，原来的绑定作废" % (
            wanted, m.get("space_name") or m["project_key"]))
        held, open_items, unchecked = old_space_items(spec, m["project_key"], m)
        if switch is not None:
            switch.update({"old": {k: m[k] for k in ("project_key", "simple_name", "space_name", "bound_at") if m.get(k)},
                           "new": {"project_key": hit.get("project_key"), "simple_name": hit.get("simple_name"),
                                   "space_name": hit.get("name")},
                           "bound_types": len(m.get("types") or {}), "held": held, "open": open_items, "unchecked": unchecked})
        for k in ("types", "field_keys", "roles", "states", "bound_at", "contract_version"):
            m.pop(k, None)
    m.update({"project_key": hit.get("project_key"), "simple_name": hit.get("simple_name"),
              "space_name": hit.get("name") or m.get("space_name")})
    snap, binding, problems, notes = setup_core.read_space(
        contract, {"project_key": m["project_key"], "simple_name": m.get("simple_name"), "name": m.get("space_name")},
        maps, space.binding_of(ids))
    space.apply_binding(ids, contract, binding)
    changes = space.diff(old_snap, snap) if same and old_snap and old_snap.get("project_key") == snap["project_key"] else []
    lark = ids.setdefault("lark", {})
    if not lark.get("researcher_open_id"):
        who = optional_read("研究员的 open_id", lambda: cli_json(["lark-cli", "whoami"]))
        who = who if isinstance(who, dict) else {}
        on_behalf = who.get("onBehalfOf") if isinstance(who.get("onBehalfOf"), dict) else {}
        oid = on_behalf.get("openId") if who.get("identity") == "user" else None
        if oid:
            lark["researcher_open_id"] = oid
        else:
            print("提醒：lark-cli whoami 里没有用户身份的 open_id；飞书私聊监听不会启动")
    wiki = ids.setdefault("wiki", {})
    want_wiki = task_feishu.get("wiki_space")
    want_id = str(task_feishu.get("wiki_space_id") or "").strip()
    if want_id:
        # The knowledge space by its ID (1.0.10). By name alone a project could not move to a space named as its
        # old one: the same name was not looked up again, and two spaces of one name are not chosen between.
        if str(wiki.get("space_id") or "") != want_id or not wiki.get("space_name"):
            data = optional_read("知识空间", lambda: cli_json(
                ["lark-cli", "wiki", "+space-list", "--as", "user", "--page-all", "--page-limit", "0"], 120))
            hit = next((d for d in ledger_core._walk(data or {}) if str(d.get("space_id") or "") == want_id), None)
            if hit:
                if wiki.get("space_id") and str(wiki["space_id"]) != want_id:
                    print("提醒：知识空间换成了“%s”（space_id %s），原来记下的节点作废，书记员在新空间里重建" % (hit.get("name") or "", want_id))
                    wiki.pop("nodes", None)
                wiki.update({"space_id": hit["space_id"], "space_name": hit.get("name") or want_wiki or ""})
            elif data is not None:
                print("提醒：翻完全部分页，没有 space_id 是 %s 的知识空间；知识空间的绑定没有动。核对任务文件的 feishu.wiki_space_id" % want_id)
    elif want_wiki and (not wiki.get("space_id") or wiki.get("space_name", want_wiki) != want_wiki):
        data = optional_read("知识空间", lambda: cli_json(
            ["lark-cli", "wiki", "+space-list", "--as", "user", "--page-all", "--page-limit", "0"], 120))
        hits = list({d["space_id"]: d for d in ledger_core._walk(data or {})
                     if d.get("name") == want_wiki and d.get("space_id")}.values())
        if len(hits) == 1:
            if wiki.get("space_id") and hits[0]["space_id"] != wiki["space_id"]:
                print("提醒：知识空间换成了“%s”，原来记下的节点作废，书记员在新空间里重建" % want_wiki)
                wiki.pop("nodes", None)
            wiki.update({"space_id": hits[0]["space_id"], "space_name": hits[0]["name"]})
        elif data is not None and not hits and create:
            made = optional_read("新建知识空间", lambda: cli_json(
                ["lark-cli", "wiki", "+space-create", "--name", want_wiki, "--as", "user"], 120))
            new = [d for d in ledger_core._walk(made or {}) if d.get("space_id")]
            if new:
                wiki.pop("nodes", None)
                wiki.update({"space_id": new[0]["space_id"], "space_name": want_wiki})
                print("知识空间：没有叫“%s”的，已新建。" % want_wiki)
            elif made is not None:
                print("提醒：新建知识空间没有拿到 space_id。先在网页上看“%s”建出来没有，再重跑这条命令：已经建出来的会被找到并沿用。" % want_wiki)
        elif data is not None and not hits:
            print("知识空间：翻完全部分页，没有叫“%s”的；加 --apply 时新建一个。" % want_wiki)
        elif len(hits) > 1:
            print("提醒：有 %d 个知识空间叫“%s”（space_id：%s），没有自动选；把要用的那个的 space_id 写进任务文件的 feishu.wiki_space_id，再重跑" % (
                len(hits), want_wiki, "、".join(str(h["space_id"]) for h in hits)))
    for key, n in (wiki.get("nodes") or {}).items():
        if n.get("node_token"):
            got = optional_read("节点 %s 的标题" % key,
                                lambda tok=n["node_token"]: cli_json(["lark-cli", "wiki", "+node-get", "--node-token", tok, "--as", "user"]))
            title = ledger_core._find(got or {}, ("title",))
            if title:
                n["title"] = title
    return snap, problems, notes, changes


def switch_md(switch):
    """The 换空间 section of binding.md: both spaces, what the old binding loses and what the
    old space still holds."""
    o, n = switch["old"], switch["new"]
    rows = [["旧空间", o.get("space_name", ""), o.get("simple_name", ""), o.get("project_key", "")],
            ["新空间", n.get("space_name") or "", n.get("simple_name") or "", n.get("project_key") or ""]]
    lines = ["## 换空间", "",
             "任务文件指的空间和上次绑定的不是同一个，这次按新空间重新绑定，原来的绑定作废。知识库不跟着换。", "",
             orgspec.md_table(["", "空间名", "simple_name", "project_key"], rows), ""]
    if switch["held"]:
        lines += ["旧空间里还有工作项：%s。插件搬不了工作项，它们留在旧空间当档案，台账以后不再读写；没办完的要不要在新空间重建，"
                  "由研究员逐条决定。研究员确认以后，写入时加 `--leave-old-items`。" % "、".join(switch["held"]), ""]
        if switch["open"]:
            lines += ["没办完的（每种类型最多列 20 条）：", "", setup_core.bullets(switch["open"]), ""]
    if switch["unchecked"]:
        lines += ["没查成旧空间里的%s有没有工作项。确认旧空间已经删了，或者里面的工作项可以留在原处，写入时加 `--leave-old-items`。"
                  % "、".join(switch["unchecked"]), ""]
    if not switch["held"] and not switch["unchecked"]:
        lines += ["旧空间里没有工作项，可以直接写入。" if switch["bound_types"] else
                  "旧绑定一种类型都没绑上，台账没在旧空间里记过东西，可以直接写入。", ""]
    lines += ["写入时 `feishu/ids.json` 的 `previous_spaces` 记下旧空间和换出的时间，本地记录里旧的工作项 ID 还能查到属于哪个空间；"
              "会话池的认领表里旧空间的工作项同时移出，记在 `.claude/logs/dispatch.jsonl`。"
              "旧空间留不留由研究员决定，删空间只能研究员本人做。", ""]
    return lines


def feishu(target, maps, live, apply, leave_old=False):
    """Bind the project to its space: find the space, read its structure, fill the contract's
    slots, check the flows. Writes feishu/ids.json, space.json, space.md and binding.md, and
    org/phases.md from the flows it read."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    whole = setup_core.load_json_file(os.path.join(target, "spec", "task.json")) or {}
    task = dict(whole.get("feishu") or {})
    task.setdefault("wiki_space", whole.get("name"))          # the knowledge space is named after the project
    try:
        maps = space.parse_maps(contract, maps)
    except space.SpaceError as e:
        raise setup_core.SetupError(str(e))
    fd_dir = os.path.join(target, "feishu")
    ids_path = os.path.join(fd_dir, "ids.json")
    raw = setup_core.load_json_file(ids_path) or {}
    ids, notes = normalise_ids(raw)
    snap = setup_core.load_json_file(os.path.join(fd_dir, "space.json"))
    problems, space_notes, changes, switch = None, [], [], {}
    if live:
        try:
            snap, problems, space_notes, changes = read_back(spec, contract, ids, task, maps, switch, snap, create=apply)
        except setup_core.SetupError:
            raise
        except Exception as e:                       # an output shape nobody expected
            raise setup_core.SetupError("读回时遇到没见过的输出（%s: %s）；什么都没写" % (type(e).__name__, e))
        if switch and apply and not leave_old and (switch["held"] or switch["unchecked"]):
            why = ("旧空间“%s”里还有工作项（%s）" % (switch["old"].get("space_name", ""), "、".join(switch["held"]))
                   if switch["held"] else "没查成旧空间里的%s有没有工作项" % "、".join(switch["unchecked"]))
            raise setup_core.SetupError(why + "，什么都没写。插件搬不了工作项，换空间以后它们留在旧空间，台账不再读写。先去掉 --apply 预览，"
                             "看 binding.md 的“换空间”一节；研究员确认这些工作项可以留在旧空间以后，再加 --leave-old-items 写入")
    else:
        moved = ledger_core.other_space({"feishu": task}, ids.get("meegle") or {})
        if moved:
            raise setup_core.SetupError("任务文件里的飞书项目空间是“%s”，上次绑定的是“%s”：加 --live 从空间读一遍再绑定" % (
                moved, ids["meegle"]["space_name"]))
        if snap is not None:
            binding, problems = space.bind(contract, snap, space.binding_of(ids), maps)
            more, space_notes = space.check(contract, snap, binding)
            problems += more
            space.apply_binding(ids, contract, binding)
        elif maps:
            raise setup_core.SetupError("还没读过空间，--map 没法核对：先跑 space.py feishu --live")
    blank = snap is not None and not (ids.get("meegle") or {}).get("types")
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if apply and snap is not None:
        if blank:                                    # nothing bound: the ledger keeps refusing
            ids["meegle"].pop("bound_at", None)
        else:
            ids["meegle"]["bound_at"] = stamp
    if switch and apply:
        ids["meegle"].setdefault("previous_spaces", []).append(dict(switch["old"], left_at=stamp, items_left=switch["held"]))
    for n in notes:
        print("提醒：" + n)
    if switch:
        old, new = switch["old"], switch["new"]
        print("换空间：旧空间“%s”（simple_name %s，project_key %s）→ 新空间“%s”（simple_name %s，project_key %s），"
              "详见 feishu/binding.md 的“换空间”一节。" % (old.get("space_name", ""), old.get("simple_name", ""),
                                                    old.get("project_key", ""), new.get("space_name"),
                                                    new.get("simple_name"), new.get("project_key")))
    binding_path = os.path.join(fd_dir, "binding.md")
    files = {ids_path: setup_core.dump_json(ids),
             binding_path: binding_doc(spec, contract, ids, snap, problems, space_notes, changes, switch, setup_core.enabled_sequences(target))}
    if snap is not None:
        files[os.path.join(fd_dir, "space.json")] = space.dump_json(snap)
        files[os.path.join(fd_dir, "space.md")] = space.to_markdown(snap, contract, space.binding_of(ids)) + "\n"
        if not blank:
            files[os.path.join(target, "org", "phases.md")] = setup_core.phases_md(spec, snap, space.binding_of(ids))
    for p, content in files.items():
        current = setup_core.read_text(p) if os.path.isfile(p) else None
        state = "相同" if current == content else ("新建" if current is None else "更新")
        print("[%s] %s" % (state, os.path.relpath(p, target)))
    if problems is None:
        print("还没读过空间，绑定不了：加 --live 从空间读。")
    else:
        print("约定核对（contract_version %d）：%s" % (contract.version, "通过" if not problems else
                                                "%d 个问题，见 feishu/binding.md 的“要处理的问题”" % len(problems)))
    for c in changes:
        print("变化：" + c)
    if blank:
        named = (ids.get("meegle") or {}).get("space_name") or task.get("meegle_space") or ""
        print("空间“%s”里没有一种类型对得上插件的约定。项目空间要用模板建：%s，再把任务文件的 feishu.meegle_space 指向它、重跑这条命令；"
              "空间里其实有这些类型、只是名字不同的，用 --map 指认。" % (named, template_hint(contract, task, "<新空间的名字>")))
    if not apply:
        print("这是预览。核对绑定结果后加 --apply 写入；台账脚本在绑定写入以后才能用。")
        if not blank:                                # a blank space gets the hint, not the whole list
            print()
            print(files[binding_path])
        return 1 if problems or blank else 0
    os.makedirs(fd_dir, exist_ok=True)
    if os.path.isfile(ids_path) and raw.get("schema") != space.IDS_SCHEMA:
        backup = os.path.join(fd_dir, "ids.legacy.json")
        if not os.path.exists(backup):
            shutil.copyfile(ids_path, backup)
    for p, content in files.items():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    print("已写入 %s。" % "、".join(os.path.relpath(p, target) for p in files))
    if switch:
        left = old_owners_out(target)
        if left:
            print("会话池的认领表里移出了旧空间的 %d 条工作项（%s）：它们留在旧空间，台账不再读写。" % (
                len(left), "、".join("#%s" % i for i in sorted(left))))
    if blank:
        print("一种类型都没绑上：这次只记下空间和 open_id，没写绑定时间，台账脚本仍然不读写飞书。")
    return 1 if problems or blank else 0                 # a space that fails the contract check stops an initialisation


def check_template(target, name):
    """Check a space as a template: read its structure, fill the contract's slots, check the
    flows. Reads Feishu, writes nothing."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    if not name:
        task = (setup_core.load_json_file(os.path.join(target, "spec", "task.json")) or {}).get("feishu") or {} if target else {}
        name = str(task.get("template_space") or "").strip()
    if not name:
        raise setup_core.SetupError("要核对哪个空间：用 --space 给出它的名字、simple_name 或 project_key")
    hit = find_project(cli_json(["meegle", "project", "search", "--project-key", name, "--format", "json"]), name,
                       "把空间的准确名字、simple_name 或 project_key 用 --space 传进来")
    snap, binding, problems, notes = setup_core.read_space(contract, hit, {})
    held = []
    for wid, slot in binding["types"].items():
        mql = "SELECT `work_item_id` FROM `%s`.`%s` LIMIT 1" % (hit.get("project_key"), slot["type_key"])
        try:
            rows = ledger_core.query_rows(cli_json(["meegle", "workitem", "query", "--project-key", hit.get("project_key"),
                                               "--mql", mql, "--format", "json"]))
        except (setup_core.SetupError, ledger_core.LedgerError) as e:
            print("提醒：没能查%s里有没有工作项：%s" % (spec.term("wi", wid), e))
            continue
        if rows:
            held.append(spec.term("wi", wid))
    if held:
        notes = list(notes) + ["空间里有工作项（%s）：流程图是从它们读出来的；发布成模板之前，样例要不要留由模板维护者决定" % "、".join(held)]
    print("空间“%s”（simple_name %s，project_key %s）。只读了飞书，没写任何文件。" % (
        hit.get("name"), hit.get("simple_name"), hit.get("project_key")))
    print()
    space.report(contract, snap, binding, problems, notes)
    if problems:
        print("还有 %d 个问题，在空间里补好以后再跑一次。" % len(problems))
        return 1
    print("这个空间符合约定：用它建出来的项目空间，绑定不用 --map。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="space.py", description="把项目绑定到飞书空间，核对空间是否符合约定")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("feishu")
    p.add_argument("--target", required=True)
    p.add_argument("--map", action="append", default=[],
                   help="指认空间里名字不同的类型或字段：<类型槽位>=<类型名> 或 <类型槽位>.<字段槽位>=<字段名或 field_key>，可重复")
    p.add_argument("--live", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--leave-old-items", action="store_true",
                   help="换空间时旧空间里还有工作项：研究员确认它们留在旧空间、台账以后不再读写，才加这个参数写入")
    p = sub.add_parser("template")
    p.add_argument("--space", help="要核对的空间：名字、simple_name 或 project_key；不写就用任务文件的 feishu.template_space")
    p.add_argument("--target", help="项目目录：从它的 spec/task.json 读 feishu.template_space")
    args = ap.parse_args(argv)
    try:
        setup_core.need_python()
        if args.cmd == "feishu":
            return feishu(os.path.abspath(args.target), args.map, args.live, args.apply, args.leave_old_items)
        if args.cmd == "template":
            return check_template(os.path.abspath(args.target) if args.target else None, args.space)
    except (setup_core.SetupError, ValueError, OSError) as e:
        print("出错：%s" % e)
        return 2
    ap.print_help()
    return 2
