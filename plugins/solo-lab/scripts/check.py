#!/usr/bin/env python3
"""What an initialisation checks: the machine before it starts, the four layers after it.

    python3.12 -I scripts/check.py precheck
    python3.12 -I scripts/check.py check --target DIR [--live] [--sessions FILE] [--readback FILE]

check reports four layers: the project's own files, the Feishu side, the sessions, the first
research task. It returns 0 only when all four are ready. --live reads Feishu back (read
only); --sessions takes the client's own list of sessions saved as JSON; --readback takes each
session's model, effort and context window as the client reported them, holds the model against
the pool plan and the window against the session that initialised, and prints the table.

What the project should look like is setup_core.py's.
Python 3.12 standard library only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import ledger_core  # noqa: E402
import space  # noqa: E402
import setup_core  # noqa: E402

MIN_VERSIONS = {"lark-cli": "1.0.96", "meegle": "1.0.23", "claude": "2.1.219"}


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def python_on_path():
    """(found, detail) for the precheck: the hooks and every command in the role texts start with PYTHON."""
    path = shutil.which(setup_core.PYTHON)
    if not path:
        return False, "PATH 里找不到 %s：hook 和角色说明里的命令都用它；先装一个，比如 brew install python@3.12" % setup_core.PYTHON
    code, out, err = setup_core.run([path, "--version"])
    return code == 0, "%s（%s）" % ((out or err or "").strip() or "?", path)


def precheck():
    rows = []

    def add(item, status, detail):
        rows.append((item, status, detail))

    found, detail = python_on_path()
    add(setup_core.PYTHON, "通过" if found else "失败", detail)

    if sys.platform == "darwin":
        apps = [p for p in ("/Applications/Lark.app", "/Applications/Feishu.app",
                            os.path.expanduser("~/Applications/Lark.app"),
                            os.path.expanduser("~/Applications/Feishu.app")) if os.path.isdir(p)]
        add("飞书客户端", "通过" if apps else "提醒", apps[0] if apps else "没找到 Lark.app 或 Feishu.app；网页版也能用")
    else:
        add("飞书客户端", "跳过", "不是 macOS，请人工确认")
    for cli, cmd in (("lark-cli", ["lark-cli", "--version"]), ("meegle", ["meegle", "version"]),
                     ("claude", ["claude", "--version"])):
        code, out, err = setup_core.run(cmd)
        found = re.findall(r"\d+\.\d+\.\d+", out or "")
        if code != 0 or not found:
            add("%s 版本" % cli, "提醒" if cli == "claude" else "失败",
                (err or out or "未安装").strip()[:120] + ("；只装了桌面应用时没有这条命令，不影响" if cli == "claude" else ""))
        else:
            ok = version_tuple(found[0]) >= version_tuple(MIN_VERSIONS[cli])
            add("%s 版本" % cli, "通过" if ok else "失败", "%s（最低 %s）" % (found[0], MIN_VERSIONS[cli]))
    code, out, _ = setup_core.run(["lark-cli", "doctor"], 60)
    try:
        doctor = json.loads(out)
        bad = [c for c in doctor.get("checks", []) if c.get("status") != "pass"]
        add("lark-cli doctor", "通过" if doctor.get("ok") else "失败",
            "；".join("%s=%s" % (c.get("name"), c.get("status")) for c in bad) or "全部通过")
    except ValueError:
        add("lark-cli doctor", "失败", "输出不是 JSON（退出码 %s）" % code)
    code, out, _ = setup_core.run(["lark-cli", "auth", "status"])
    try:
        ok, user = setup_core.lark_user_ready(json.loads(out))
        add("lark-cli 用户身份", "通过" if ok else "失败", "status=%s tokenStatus=%s%s" % (
            user.get("status"), user.get("tokenStatus"), "" if ok else "；运行 setup.py login 发起登录，研究员扫码"))
    except ValueError:
        add("lark-cli 用户身份", "失败", "auth status 输出不是 JSON（退出码 %s）" % code)
    code, _, _ = setup_core.run(["meegle", "auth", "status"])
    add("meegle 登录", "通过" if code == 0 else "失败",
        "退出码 %s%s" % (code, "" if code == 0 else "；运行 setup.py login 发起登录，研究员扫码"))
    code, out, _ = setup_core.run(["meegle", "project", "search", "--page-num", "1", "--format", "json"], 60)
    try:
        projects = (json.loads(out) or {}).get("projects") or []
        add("飞书项目权限", "通过" if projects else "提醒",
            "能访问 %d 个空间" % len(projects) if projects else "这个账号还看不到任何空间；初始化时用模板新建一个")
    except ValueError:
        add("飞书项目权限", "失败", "project search 输出不是 JSON（退出码 %s）" % code)
    code, out, _ = setup_core.run(["meegle", "telemetry", "status"])
    try:
        enabled = json.loads(out).get("enabled")
        add("meegle 诊断上报", "提醒" if enabled else "通过",
            "开着，请研究员决定是否关闭（meegle telemetry disable，本人执行）" if enabled else "已关闭")
    except ValueError:
        add("meegle 诊断上报", "跳过", "telemetry 子命令不可用（退出码 %s）" % code)
    code, out, _ = setup_core.run(["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot", "--dry-run"], 60)
    try:
        decision = (json.loads(out).get("data") or {}).get("decision") or {}
        bad = [c.get("name") for c in decision.get("preconditions", []) if c.get("status") != "ok"]
        ok = decision.get("status") == "ready" and not bad
        add("飞书私聊事件订阅", "通过" if ok else "提醒",
            "凭证、开发者后台的事件订阅、权限 im:message.p2p_msg:readonly 都就绪" if ok else
            "没就绪：%s；入口收不到飞书私聊，研究员转述也行" % ("、".join(bad) or decision.get("status") or "未知"))
    except (ValueError, AttributeError):
        add("飞书私聊事件订阅", "提醒", "event consume 的预览输出读不懂（退出码 %s）" % code)
    add("Python", "通过" if sys.version_info[:2] >= (3, 9) else "失败", sys.version.split()[0])
    for name in ("lark-doc", "lark-wiki", "meegle"):
        here = any(os.path.isdir(os.path.expanduser(os.path.join(b, name))) for b in ("~/.claude/skills", "~/.agents/skills"))
        add("技能 %s" % name, "通过" if here else "提醒", "已安装" if here else "没找到；书记员用台账脚本，不依赖它，但人工排查时有用")
    width = max(len(r[0]) for r in rows)
    for item, status, detail in rows:
        print("%s  %s  %s" % (item.ljust(width), status, detail))
    failed = [r for r in rows if r[1] == "失败"]
    print("预检：%d 项失败。" % len(failed) if failed else "预检：没有失败项。")
    return 1 if failed else 0


# --------------------------------------------------------------- check
def local_layer(target, m):
    """(problems, reminders) of the project's own files against the plugin that made them."""
    problems, notes = [], []
    if m.get("plugin_version") != setup_core.plugin_version():
        problems.append("插件版本：项目是 %s，插件是 %s；重跑 init 预览差异" % (m.get("plugin_version"), setup_core.plugin_version()))
    rules = setup_core.read_text(os.path.join(orgspec.ORGSPEC_DIR, "rules.json"))
    if m.get("rules_sha256") != setup_core.sha256(rules):
        problems.append("规则文件和项目里的副本不一致；重跑 init 预览差异")
    if m.get("terms_sha256") != setup_core.sha256(setup_core.read_text(os.path.join(orgspec.ORGSPEC_DIR, "terms.zh.json"))):
        problems.append("术语表和项目里的副本不一致；重跑 init 预览差异")
    if m.get("contract_sha256") != setup_core.sha256(setup_core.read_text(os.path.join(orgspec.ORGSPEC_DIR, "contract.json"))):
        problems.append("约定文件和项目里的副本不一致；重跑 init 预览差异，再跑 space.py feishu --live 核对绑定")
    if m.get("plugin_scripts") != setup_core.plugin_scripts():
        problems.append("插件脚本的位置或内容和初始化时不同，hook 不再放行研究员会话里的 setup.py；重跑 init --apply"
                        "（hook 拦下时，请研究员在自己的终端里运行这一次）")
    task_path = os.path.join(target, "spec", "task.json")
    fresh = None
    if os.path.isfile(task_path) and m.get("files", {}).get("spec/task.json") not in (None, setup_core.sha256(setup_core.read_text(task_path))):
        problems.append("改过：spec/task.json（和 init 用的任务文件不同）。它跟着 init --task 的任务文件走：要改设置，改任务文件再重跑 init --apply；"
                        "有 spec/task.json.new 的，对照合并。改回来之前，先不比对插件更新")
    else:
        try:      # regenerate from the project's own task and sequences: catches script, template and ID changes
            fresh = setup_core.build_plan(target, task_path, ",".join(m.get("sequences") or []) or None)
        except setup_core.SetupError as e:
            problems.append("没法按现在的插件重新生成来比对：%s" % e)
    if fresh:
        stale = [rel for rel, (content, _, keep) in sorted(fresh.files.items())
                 if not keep and m.get("files", {}).get(rel) != setup_core.sha256(content)]
        risk_reported = ".claude/hooks/lark-risk.json" in stale
        if risk_reported:                                   # follows the lark-cli on this machine, not the plugin
            stale.remove(".claude/hooks/lark-risk.json")
            table = (setup_core.load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
            problems.append("lark-cli 风险表和本机的 lark-cli 对不上：项目里的表按 %s 生成，本机是 %s；重跑 init 会换成对应的表" % (
                table or "未知版本", setup_core.installed_lark_version() or "没装 lark-cli"))
        table = (setup_core.load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
        installed = setup_core.installed_lark_version()
        if table and installed and installed != table and not risk_reported:
            notes.append("本机 lark-cli 是 %s，项目的风险表按 %s 生成（插件自带的表）；新命令的风险等级可能缺失，未知的写命令一律按写处理" % (installed, table))
        if stale:
            problems.append("现在的插件、规则或 feishu/ids.json 生成的内容和初始化时不同（%d 个文件：%s）；重跑 init 预览差异" % (
                len(stale), "、".join(stale[:12]) + ("等" if len(stale) > 12 else "")))
        # init writes files and takes none away: a role an earlier version had leaves its definition behind
        roles_dir = os.path.join(target, ".claude", "agents")
        gone = sorted(".claude/agents/" + n for n in (os.listdir(roles_dir) if os.path.isdir(roles_dir) else [])
                      if n.endswith(".md") and ".claude/agents/" + n not in fresh.files)
        if gone:
            problems.append("这些角色定义是旧版留下的，现在的规则里没有这些角色（1.1.0 起各序列只有 +1 和 +0 两级，带队的角色叫 <序列>-p1）：%s；请研究员删掉这些文件" % "、".join(gone))
    for rel, digest in sorted(m.get("files", {}).items()):
        path = os.path.join(target, rel)
        if not os.path.isfile(path):
            problems.append("缺文件：%s" % rel)
        elif rel in ("org/decisions.md", "org/phases.md", "runs/README.md") or rel.startswith(("spec/", "packages/drafts/")):
            continue            # the researcher's and the project's documents; a draft gets its work item written in
        else:
            try:
                changed = setup_core.sha256(setup_core.read_text(path)) != digest
            except (UnicodeDecodeError, OSError):
                problems.append("读不了：%s（不是 UTF-8 编码？）" % rel)
                continue
            if changed and not (fresh and rel in fresh.files and setup_core.read_text(path) == fresh.files[rel][0]):
                problems.append("改过：%s（和初始化时不同）" % rel)
    for rel in m.get("pending_new", []):
        if os.path.isfile(os.path.join(target, rel + ".new")):
            problems.append("待合并：%s.new" % rel)
    problem = setup_core.pair_problem(target)
    if problem:
        problems.append(problem)
    return problems, notes


def live_feishu(target, seqs):
    """Read the space and the knowledge nodes as they are now: the binding still holds and
    every node is in the knowledge space. Reads only."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    ids = setup_core.load_json_file(os.path.join(target, "feishu", "ids.json")) or {}
    m, wiki = ids.get("meegle") or {}, ids.get("wiki") or {}
    problems, notes = [], []
    try:
        old = space.binding_of(ids)
        _, now_, found, notes = setup_core.read_space(
            contract, {"project_key": m["project_key"], "simple_name": m.get("simple_name"), "name": m.get("space_name")}, {}, old)
        problems += ["空间结构：%s，%s。%s" % tuple(row) for row in found + space.stale(contract, old, now_)]
    except (setup_core.SetupError, KeyError) as e:
        problems.append("读不了飞书项目空间：%s" % e)
    nodes = wiki.get("nodes") or {}

    def lost(key):
        code, out, _ = setup_core.run(["lark-cli", "wiki", "+node-get", "--node-token", (nodes.get(key) or {}).get("node_token") or "", "--as", "user"], 60)
        try:
            doc = json.loads(out) if code == 0 else None
        except ValueError:
            doc = None
        where = ledger_core._find(doc or {}, ("space_id",))
        return not doc or bool(where) and str(where) != str(wiki.get("space_id"))
    keys = spec.node_keys(seqs)
    with ThreadPoolExecutor(max_workers=4) as workers:          # independent read-only requests
        bad = [k for k, gone in zip(keys, workers.map(lost, keys)) if gone]
    if bad:
        problems.append("知识库节点读不到，或者不在这个知识空间里（%d 个）：%s" % (len(bad), "、".join(bad[:8]) + ("等" if len(bad) > 8 else "")))
    return problems, list(notes)


def feishu_layer(target, m, live):
    """(problems, reminders): the binding, the knowledge space and its nodes, the links in .env."""
    seqs = m.get("sequences") or None
    problems = setup_core.feishu_gaps(target, seqs)
    if problems:
        return problems, []
    problems = env_gaps(target)
    notes = []
    if live:
        more, notes = live_feishu(target, seqs)
        problems += more
    return problems, notes


def first_task_layer(target, live):
    """(problems, reminders): a research task is registered, its receipt has the main document
    and its draft names the work item; --live reads the three of them back."""
    tasks = setup_core.registered_tasks(target)
    if not tasks and live:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rows = ledger.query(ledger_core.Ledger(target, "preview"), "tasks")
        except (ledger_core.LedgerError, orgspec.OrgSpecError, space.SpaceError, KeyError) as e:
            return ["查不了空间里的研究任务：%s" % str(e)[:200]], []
        if rows:
            return [], ["空间里已有 %d 条研究任务，这个目录里没有它们的登记回执（是从别处登记的）" % len(rows)]
    if not tasks:
        return ["还没有登记过研究任务：开池时由书记员登记 %s，清单见 pool.py open%s" % (
            setup_core.FIRST_TASK, "" if live else "；空间里已经有研究任务的，加 --live 核对")], []
    rec, draft = tasks[0]
    item_id, doc = str(rec.get("work_item_id")), str(rec.get("doc") or "")
    problems = []
    if not doc.startswith("http"):
        problems.append("研究任务 #%s 的回执里没有主文档的地址" % item_id)
    try:
        line = ledger_core.header_of(setup_core.read_text(draft)).get("关联工作项", "")
    except (OSError, UnicodeDecodeError):
        line = ""
    if item_id not in re.findall(r"#(\d+)", line):
        problems.append("草稿 %s 的“关联工作项”一行不是 #%s" % (rec["draft"], item_id))
    if live and not problems:
        try:
            lg = ledger_core.Ledger(target, "preview")
            main = lg.contract.main_line()
            it = lg.item(item_id)
            if not it or it.get("type") != main:
                return ["工作项 #%s 读不到，或者不是研究任务" % item_id], []
            link = ledger_core.plain(it["fields"].get(lg.field(main, "main_doc")["field_key"]))
            if link != doc:
                problems.append("研究任务 #%s 的主文档字段是 %s，回执记的是 %s" % (item_id, link or "空的", doc))
            node = lg.lark("wiki", "+node-get", "--node-token", doc.rstrip("/").rsplit("/", 1)[-1])
            above, want = ledger_core._find(node or {}, ("parent_node_token",)), (lg.nodes.get(main) or {}).get("node_token")
            if it["state"] != lg.state(main, "archived") and want and above != want:
                problems.append("研究任务 #%s 的主文档不在“%s”节点下" % (item_id, lg.spec.term("node", main)))
        except (ledger_core.LedgerError, orgspec.OrgSpecError, space.SpaceError, KeyError) as e:
            problems.append("回读研究任务 #%s 没读成：%s" % (item_id, str(e)[:200]))
    return problems, []


def listed_sessions(target, path, plan, pl):
    """(problems, reminders) of the claims against the client's own list of sessions: the
    result of list_sessions saved as it came."""
    rows = setup_core.load_json_file(path)
    if not isinstance(rows, list):
        return ["--sessions 给的文件不是会话列表：把 list_sessions 的结果原样存成 JSON 文件（include_archived 写 true，limit 写够）"], []
    by_id = {str(r.get("sessionId")): r for r in rows if isinstance(r, dict)}
    label, here = plan.get("label"), os.path.realpath(target)
    desks = {str(c.get("desk")) for c in pl["claims"].values()}      # the session that initialised is not one of them
    gone, archived, elsewhere, loose, pinned, titled, unknown = [], [], [], [], [], [], []
    for name, claim in sorted(pl["claims"].items()):
        row = by_id.get(str(claim.get("desk")))
        if not row:
            gone.append(name)
            continue
        if row.get("isArchived"):
            archived.append(name)
        if row.get("cwd") and os.path.realpath(row["cwd"]) != here:
            elsewhere.append(name)
        above, seen = row, set()                       # a nested session is filed where the pool session above it is
        while above.get("startedBy") in desks and above["startedBy"] in by_id and above["startedBy"] not in seen:
            seen.add(above["startedBy"])
            above = by_id[above["startedBy"]]
        if "group" not in row and "group" not in above:
            unknown.append(name)
        elif label not in ((row.get("group") or {}).get("name"), (above.get("group") or {}).get("name")):
            loose.append(name)
        if row.get("pinned"):
            pinned.append(name)
        if row.get("title") != "[%s] %s" % (label, name):
            titled.append(name)
    some = lambda names: "、".join(names[:8]) + ("等" if len(names) > 8 else "")
    problems = [text % some(names) for names, text in (
        (gone, "客户端的会话列表里没有这些认领的会话：%s（会话被删了，或者列表没取全）"),
        (archived, "已经归档的会话还占着认领：%s"),
        (elsewhere, "工作目录不是项目目录的会话：%s"),
        (loose, "不在侧栏分区“" + str(label) + "”里的会话：%s")) if names]
    notes = [text % some(names) for names, text in (
        (unknown, "客户端还没报告这些会话的分区，没核对：%s"),
        (pinned, "置顶了的会话：%s（会话池的会话归分区，不置顶）"),
        (titled, "标题不是“[" + str(label) + "] <会话名>”的会话：%s")) if names]
    return problems, notes


def session_readback(target, path):
    """(problems, reminders, rows): what each claimed session actually runs, against the pool plan
    and the session that initialised. The file is what get_session and get_usage reported, saved by
    that session: {"self": {model, effort, contextWindow}, "sessions": [{sessionId, model, effort,
    contextWindow}, ...]}; a value it could not read is null."""
    import pool
    data = setup_core.load_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("self"), dict) or not isinstance(data.get("sessions"), list):
        return ["--readback 给的文件不是回读表：要写成 {\"self\": {...}, \"sessions\": [...]}，每条带 sessionId、model、effort、"
                "contextWindow，读不到的写 null；做法见 setup 技能第 8 步"], [], []
    pl, ok = pool.load(target)
    if not ok:
        return [], [], []                              # pool_health reports the broken claims table
    plan = setup_core.load_json_file(os.path.join(target, ".claude", "solo-lab", "pool-plan.json")) or {}
    entries = list(setup_core.plan_entries(plan))
    planned = {e["name"]: e["model"] for e in entries}
    by_role = {e["role"]: e["model"] for e in entries}
    window = lambda r: r.get("contextWindow") if type(r.get("contextWindow")) is int else None
    own, by_id = data["self"], {str(r.get("sessionId")): r for r in data["sessions"] if isinstance(r, dict)}
    names = [e["name"] for e in entries if e["name"] in pl["claims"]] + sorted(set(pl["claims"]) - set(planned))
    models, narrow, efforts, unread = [], [], [], []
    rows = [("本会话", "—", own.get("model"), own.get("effort"), window(own))]
    for name in names:
        claim = pl["claims"][name]
        r = by_id.get(str(claim.get("desk"))) or {}
        want = planned.get(name) or by_role.get(claim.get("role")) or "inherit"
        model, effort, size = r.get("model"), r.get("effort"), window(r)
        rows.append((name, want, model, effort, size))
        if not model or size is None:
            unread.append(name)
        if model and setup_core.model_tier(want) >= 0 and setup_core.model_tier(model) != setup_core.model_tier(want):
            models.append(name)
        if size is not None and window(own) and size < window(own):
            narrow.append(name)
        if effort and own.get("effort") and effort != own["effort"]:
            efforts.append(name)
    some = lambda names: "、".join(names[:8]) + ("等" if len(names) > 8 else "")
    problems, notes = [], []
    if models:
        problems.append("模型和开池清单不在一档的会话：%s。会话的模型不能高于开它的会话，入口要用 fable，做初始化的会话本身就得是 fable；"
                        "照回读表在应用里切换模型，要换默认就改任务文件的 models 重跑 init --apply" % some(models))
    if narrow:
        problems.append("窗口小于本会话（%d）的会话：%s。查用户设置、项目设置、项目本地设置里的 autoCompactWindow 和环境变量 "
                        "CLAUDE_CODE_AUTO_COMPACT_WINDOW；设置只在会话进程启动时读，改完请研究员退出并重开桌面应用" % (window(own), some(narrow)))
    if window(own) is None:
        notes.append("回读表里没有本会话的窗口，各会话的窗口没比对")
    if efforts:
        notes.append("推理强度和本会话（%s）不同的会话：%s" % (own["effort"], some(efforts)))
    if unread:
        notes.append("没读到模型或窗口的会话：%s（会话进程没在跑、还在启动或已归档时读不到；过一会儿重读，还读不到就写进交接）" % some(unread))
    return problems, notes, rows


def session_layer(target, m, listing=None):
    """(problems, reminders): every planned session holds its claim, one entry and one scribe,
    no pre-registration left over, one class of permission mode, all working in the project."""
    problems, notes = pool_health(target, m)
    if listing:
        import pool
        pl, ok = pool.load(target)
        plan = setup_core.load_json_file(os.path.join(target, ".claude", "solo-lab", "pool-plan.json")) or {}
        if ok:
            more, extra = listed_sessions(target, listing, plan, pl)
            problems += more
            notes += extra
    return problems, notes


def check(target, live=False, sessions=None, readback=None):
    """Is the project ready, layer by layer: its own files, the Feishu side, the sessions, the
    first research task. 0 only when every layer is. --live reads Feishu back (read only);
    --readback holds each session's model, window and effort against the pool plan."""
    mpath = os.path.join(target, ".claude", "solo-lab", "manifest.json")
    if not os.path.isfile(mpath):
        print("没有 %s：这个目录还没初始化。" % mpath)
        return 2
    m = json.loads(setup_core.read_text(mpath))
    layers = [("本地配置",) + local_layer(target, m), ("飞书",) + feishu_layer(target, m, live),
              ("会话",) + session_layer(target, m, sessions), ("首条研究任务",) + first_task_layer(target, live)]
    more, extra, rows = session_readback(target, readback) if readback else ([], [], [])
    layers[2] = ("会话", layers[2][1] + more, layers[2][2] + extra)
    for name, problems, notes in layers:
        print("%s：%s" % (name, "%d 个问题" % len(problems) if problems else "就绪"))
        for n in notes:
            print("  提醒：" + n)
        for p in problems:
            print("  问题：" + p)
    if rows:
        print("回读表：")
        print("| 会话 | 清单模型 | 实际模型 | 推理强度 | 窗口 |")
        print("| --- | --- | --- | --- | --- |")
        for row in rows:
            print("| %s |" % " | ".join("读不到" if v is None else str(v) for v in row))
    bad = [(name, len(problems)) for name, problems, _ in layers if problems]
    print("检查：%s%s" % ("还有 %d 个问题（%s）" % (sum(n for _, n in bad), "、".join("%s %d" % x for x in bad)) if bad else "四层都就绪",
                        "" if live else "。飞书一侧只看了本地记下的标识；加 --live 回读飞书"))
    return 1 if bad else 0


def pool_health(target, manifest):
    """(problems, reminders) from the claims and the sessions' status files."""
    import pool
    policy = setup_core.load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) or {}
    if not policy.get("agents"):
        return ["读不到 .claude/hooks/policy.json"], []
    if not policy.get("entry_role") or not policy.get("scribe_role"):
        return ["policy.json 还是旧版的：先在 Claude 以外的终端里合并 .claude/hooks/ 下的 .new，重跑 init --apply，再查会话池"], []
    pl, ok = pool.load(target)
    if not ok:
        return ["读不出 .claude/state/pool.json：认领表坏了，台账写入会全部被拒；请研究员先看这个文件"], []
    problems, notes = [], []
    plan = setup_core.load_json_file(os.path.join(target, ".claude", "solo-lab", "pool-plan.json")) or {}
    planned = [e["name"] for e in setup_core.plan_entries(plan)]
    held = {}
    for c in pl["claims"].values():
        held.setdefault(c["role"], []).append(c["name"])
    retired = sorted(n for role, names in held.items() if role not in policy["agents"] for n in names)
    if retired:
        problems.append("认领表里有现在的规则没有的角色：%s。先让它们把手上的活交给本序列 +1，"
                        "再请研究员归档这些会话，逐个运行 pool.py release <会话名>" % "、".join(retired))
    for role in (policy["entry_role"], policy["scribe_role"]):
        if len(held.get(role, [])) > 1:
            problems.append("%s 有 %d 条认领（%s），只能有一条；请研究员 pool.py release 多余的" % (role, len(held[role]), "、".join(held[role])))
    missing = sorted(set(planned) - set(pl["claims"]))
    if missing:
        problems.append("会话池还没开齐：%s 没人认领（共 %d/%d 条认领）；清单见 pool.py open" % (
            "、".join(missing[:8]) + ("等" if len(missing) > 8 else ""), len(set(planned) & set(pl["claims"])), len(planned)))
    if pl["permits"]:
        problems.append("有 %d 条预登记还没转正：%s" % (len(pl["permits"]), "、".join(p["name"] for p in pl["permits"])))
    over = [r for r, names in held.items() if len(names) > pool.role_limit(policy, r)]
    if over:
        problems.append("超过上限的角色：%s" % "、".join(over))
    rows = pool.session_view(target, policy, pl)
    at = pool.now()
    gone = [r["name"] for r in rows if r["status"] == "offline" and r["last"] and at - pool.parse_time(r["last"]) > pool.OFFLINE_LONG]
    if gone:
        notes.append("离线超过 24 小时的会话：%s" % "、".join(gone))
    mixed = pool.mixed_modes(rows)
    if mixed:
        problems.append("权限模式和多数会话不是一类：%s（bypass 和其他模式之间的消息会被扣下）" % "、".join(mixed))
    away = [r["name"] for r in rows if r.get("cwd") and os.path.realpath(r["cwd"]) != os.path.realpath(target)]
    if away:
        problems.append("工作目录不是项目目录的会话：%s（会话要在项目目录里开，不开 worktree）" % "、".join(away))
    old = [r["name"] for r in rows if r.get("plugin_version") != manifest.get("plugin_version")]
    if old:
        notes.append("还停在旧版插件规则上的会话：%s；它们下次开局、压缩或清空上下文时收到新的角色说明，研究员退出并重开桌面应用的话下一轮就收到" % "、".join(old))
    versions = sorted({r["version"] for r in rows if r.get("version")})
    if len(versions) > 1:
        notes.append("池里的 Claude Code 版本不一致（%s）；行为有变化时重跑一遍会话池实验" % "、".join(versions))
    return problems, notes


def env_gaps(target):
    """Problems of the project's .env against feishu/ids.json; other lines are not looked at."""
    values, missing = setup_core.env_values(target)
    if missing:
        return ["还写不了 .env：%s" % "；".join(missing)]
    current = setup_core.read_env(target)
    have = dict(line.strip().split("=", 1) for line in (current or "").split("\n")
                if line.strip().startswith(tuple(values)) and "=" in line)
    if have != values:
        return ["项目目录的 .env 还没有飞书项目和知识空间的链接，或者和 feishu/ids.json 对不上：运行 setup.py env --target <项目目录> --apply"]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description="初始化的前置检查和四层检查")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("precheck")
    p = sub.add_parser("check")
    p.add_argument("--target", required=True)
    p.add_argument("--live", action="store_true", help="只读回读飞书：结构和绑定、知识库节点、首条研究任务和它的主文档")
    p.add_argument("--sessions", metavar="FILE", help="客户端 list_sessions 的结果存成的 JSON 文件：核对会话还在、没归档、都在项目分区里")
    p.add_argument("--readback", metavar="FILE",
                   help="本会话用 get_session、get_usage 读回的各会话模型、推理强度和窗口存成的 JSON 文件：模型对照开池清单，窗口和推理强度对照本会话")
    args = ap.parse_args(argv)
    try:
        setup_core.need_python()
        if args.cmd == "precheck":
            return precheck()
        if args.cmd == "check":
            return check(os.path.abspath(args.target), args.live, args.sessions, args.readback)
    except (setup_core.SetupError, ValueError, OSError) as e:
        print("出错：%s" % e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
