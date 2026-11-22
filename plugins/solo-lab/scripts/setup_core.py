#!/usr/bin/env python3
"""What the initialisation scripts share: what a project should look like. The files init renders (agents,
project settings with permissions and hooks, CLAUDE.md, org/, packages/, spec/, the hook policy, the pool
plan, the copy of the plugin's scripts and rules), the plan that holds them and what it would change in a
directory, and the helpers that read a project back.

setup.py (announce, init, login, env), check.py (precheck, check), space_cmd.py (space.py feishu, template)
and pool_open.py (pool.py open) load it; it has no commands of its own. It reads the plugin's scaffold and
rules next to it, so it stays where the plugin is installed and is not copied into a project.
Python 3.12 standard library only (1.0.12: until then 3.9, the python3 macOS ships).
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger_core  # noqa: E402
import pool as pool_state  # noqa: E402
import space  # noqa: E402
import project_paths  # noqa: E402

PLUGIN_ROOT = orgspec.PLUGIN_ROOT
SCAFFOLD = os.path.join(PLUGIN_ROOT, "scaffold")
TEMPLATES = os.path.join(PLUGIN_ROOT, "skills", "packages", "assets")
DEFAULT_MODELS = {"p2": "opus", "p0": "opus", "pmo-p2": "fable", "pmo-router": "opus"}
REQUIRED_SEQUENCES = ("pmo", "quality")
BIN_SCRIPTS = ("orgspec.py", "ledger_core.py", "ledger.py", "docs.py", "workitem.py", "route.py", "notify.py", "todo.py", "space.py",
               "pool.py", "pool_cmd.py", "project_paths.py", "onboarding.py")
HOOK_SCRIPTS = ("guard.py", "audit.py", "instances.py", "inbox.py", "feishu_cmd.py")
SHARED_HOOK_SCRIPTS = ("pool.py",)         # kept in scripts/, copied into the hooks too
# liaison writes for readers other than the researcher: its +0s are opened when the first piece is due
# later: sequences that open with their +2 alone. 产品 and 算法 joined 外联 in 1.0.12 (the researcher, 2026-11-20): their
# work is one line of thought, and of 1217 messages a real pool sent in a day the sessions below their +2 received five.
# 1.1.0: a sequence has no +1 (the researcher, 2026-11-21), so p0 is the only size left.
POOL_DEFAULTS = {"p0": 1, "later": ["liaison", "product", "alg"], "max_sessions": 30, "label": ""}
# plugin scripts the researcher's own session runs from the plugin directory; the hook trusts them by path and hash
PLUGIN_RUN = ("setup.py", "check.py", "space.py", "pool.py", "onboarding.py")
FIRST_TASK = ledger_core.FIRST_TASK
POOL_SESSIONS = (7, 57)

# 1.1.0: no session runs a lark-cli write itself. A document is written through docs.py by the session that
# answers for it (the script checks who that is), moved to the archive by the scribe's workitem.py archive, and
# deleted by the researcher on the web: a session that wants one gone puts it on the researcher's to-do list.
# Until 1.0.12 the scribe had docs +create, docs +update, wiki +node-create, wiki +move and the drive comment
# commands, and from 1.0.10 the one high-risk command a session ran, wiki +node-delete.
SCRIBE_MEEGLE = ["workitem create", "workitem update", "comment add", "attachment +upload", "attachment prepare-upload"]
READ_ALLOW = [
    "Bash(lark-cli docs +fetch *)", "Bash(lark-cli docs +search *)", "Bash(lark-cli wiki +node-list *)",
    "Bash(lark-cli wiki +node-get *)", "Bash(lark-cli wiki +space-list *)",
    "Bash(lark-cli drive +list-comments *)", "Bash(lark-cli im +chat-messages-list *)",
    "Bash(lark-cli doctor)", "Bash(lark-cli whoami)", "Bash(lark-cli --version)",
    "Bash(lark-cli schema *)", "Bash(lark-cli * --help)",
    "Bash(meegle workitem get *)", "Bash(meegle workitem query *)", "Bash(meegle workitem meta-types *)",
    "Bash(meegle workitem meta-fields *)", "Bash(meegle workitem meta-roles *)",
    "Bash(meegle workitem meta-create-fields *)", "Bash(meegle workitem list-op-records *)",
    "Bash(meegle comment list *)", "Bash(meegle workflow get-node *)",
    "Bash(meegle workflow list-state-required *)", "Bash(meegle project search *)",
    "Bash(meegle view search *)", "Bash(meegle view get *)", "Bash(meegle user me *)",
    "Bash(meegle auth status)", "Bash(meegle telemetry status)", "Bash(meegle version)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py get *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py next *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py query *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py trace *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py status *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/ledger.py mql *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/space.py snapshot *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/space.py bind *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/space.py check *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/space.py show *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/route.py classify *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/orgspec.py lint *)",
    "Bash(python3.12 -I .claude/solo-lab/bin/orgspec.py show *)",
]
# What no session runs, whatever its role: Claude Code refuses these before a hook is asked, and a hook's
# "allow" does not lift a deny. So nothing a role may run belongs here. Until 1.0.10 the list also denied
# every lark-cli command with --yes, and build_settings denied every high-risk command: the scribe's
# wiki +node-delete, which guard.py let through, was refused all the same. --yes and the high-risk commands
# are guard.py's to judge, by role (check_lark); tools/lint.py holds the two lists against each other.
ADMIN_DENY = [
    "Bash(lark-cli auth login *)",
    "Bash(lark-cli auth logout *)", "Bash(lark-cli config *)", "Bash(lark-cli profile *)",
    "Bash(lark-cli update *)", "Bash(meegle auth login *)", "Bash(meegle auth logout *)",
    "Bash(meegle telemetry enable *)", "Bash(meegle telemetry disable *)", "Bash(meegle config set *)",
    "Bash(meegle config init *)", "Bash(meegle preference *)", "Bash(meegle ai-handoff *)",
]


class SetupError(Exception):
    pass


# ------------------------------------------------------------- helpers
def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def scaffold(rel):
    return read_text(os.path.join(SCAFFOLD, rel))


def render(template, values):
    def rep(m):
        if m.group(1) not in values:
            raise SetupError("模板缺少取值：%s" % m.group(1))
        return values[m.group(1)]
    return re.sub(r"\{\{([a-z_]+)\}\}", rep, template)


def dump_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def yaml_str(value):
    return json.dumps(value, ensure_ascii=False)


def bullets(items, indent=""):
    return "\n".join("%s- %s" % (indent, i) for i in items)


def code_list(items):
    return "、".join("`%s`" % p for p in items) if items else "无"


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", str(e)


def plugin_version():
    try:
        return json.loads(read_text(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"))).get("version")
    except (OSError, ValueError):
        return None


# -------------------------------------------------------------- inputs
TASK_KEYS = ("name", "description", "runtime", "mode", "pool", "editable_paths", "protected_paths", "feishu", "models")
# Earlier task files described the experiment in one line each (x, f, g, h, search). Their
# paths are taken over; the lines only seed the documents in spec/ the first time.
LEGACY_SEEDS = (("x", "可改范围"), ("f", "目标指标"), ("g", "实验资源"), ("h", "评测协议"), ("search", "搜索方法"))


def legacy_text(raw, key):
    v = raw.get(key)
    if isinstance(v, dict):
        v = v.get("definition")
    return v.strip() if isinstance(v, str) else ""


def load_task_and_seeds(path):
    """The task file in its current form, plus the one-line descriptions an earlier file gave.

    The task file holds only what scripts and hooks use: names, editable_paths (可改范围),
    protected_paths (评测协议), the Feishu space this project is bound to and the models.
    What the experiment is goes into spec/spec.md and spec/eval-protocol.md.
    """
    try:
        raw = json.loads(read_text(path))
    except (OSError, ValueError) as e:
        raise SetupError("读不到研究任务 %s：%s" % (path, e))
    if not isinstance(raw, dict):
        raise SetupError("研究任务 %s 要写成一个 JSON 对象" % path)
    return parse_task(raw)


def parse_task(raw):
    """What a task file says, checked and filled in with the defaults: (task, seeds, legacy keys)."""
    for key in ("name", "description"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise SetupError("研究任务的 %s 要写成非空的文字" % key)
    for key in ("x", "f", "g", "h"):
        v = raw.get(key)
        if v is not None and not (isinstance(v, dict) and isinstance(v.get("definition", ""), str)):
            raise SetupError("研究任务里的 %s 是旧写法，只认 {\"definition\": \"...\", ...}；新写法见插件的 scaffold/task.example.json" % key)
    if raw.get("search") is not None and not isinstance(raw["search"], str):
        raise SetupError("研究任务的 search 要写成文字")
    unknown = sorted(k for k in raw if k not in TASK_KEYS + ("x", "f", "g", "h", "search") and not k.startswith("_"))
    if unknown:
        raise SetupError("研究任务里有不认识的字段：%s；能写的是 %s" % ("、".join(unknown), "、".join(TASK_KEYS)))
    task = {"name": raw["name"], "description": raw["description"]}
    for key, old in (("editable_paths", "x"), ("protected_paths", "h")):
        legacy = (raw.get(old) or {}).get("paths")
        if key in raw and legacy is not None:
            raise SetupError("研究任务里同时有 %s 和旧写法的 %s.paths；只留 %s" % (key, old, key))
        paths = raw.get(key, legacy)
        if paths is None:
            paths = []
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise SetupError("研究任务的 %s 要写成路径列表" % key)
        clean = []
        for written in paths:
            p = written
            while p.startswith("./"):         # the hook compares paths without ./ and without a trailing /
                p = p[2:]
            if p.endswith("/"):
                p += "**"
            if not p or p.startswith("/") or ".." in p.split("/"):
                raise SetupError("%s 要写成项目目录内的相对路径：%s" % (key, written))
            clean.append(p)
        task[key] = clean
    feishu = raw.get("feishu")
    if feishu is None:
        feishu = {}
    if not isinstance(feishu, dict) or not all(isinstance(v, str) for v in feishu.values() if v is not None):
        raise SetupError("研究任务的 feishu 要写成 {\"meegle_space\": \"...\", ...}，值都是文字")
    if not (feishu.get("meegle_space") or "").strip():
        raise SetupError("研究任务要写明这个项目用哪个飞书项目空间：feishu.meegle_space 填空间名、simple_name 或 project_key。"
                         "初始化以后用 space.py feishu --live 绑定这个空间")
    if feishu.get("template_url") is not None and not re.match(r"https://[^\s/]+/\S+$", feishu["template_url"]):
        raise SetupError("研究任务的 feishu.template_url 要写成飞书项目模板的 https 链接")
    task["feishu"] = feishu
    if raw.get("models") is not None and not (
            isinstance(raw["models"], dict) and all(isinstance(v, str) for v in raw["models"].values())):
        raise SetupError("研究任务的 models 要写成 {\"角色或职级\": \"模型名\"}")
    if raw.get("runtime", "claude") != "claude":
        raise SetupError("这一版只在 Claude 桌面端运行：把研究任务里 runtime 那一行删掉，或者写成 claude")
    task["runtime"] = "claude"
    models = dict(DEFAULT_MODELS)
    models.update(raw.get("models") or {})
    task["models"] = models
    if raw.get("mode", "pool") != "pool":
        raise SetupError("研究任务的 mode 只支持 pool（桌面会话池）：CLI 模式已取消，把 mode 这一行删掉或写成 pool")
    task["pool"] = load_pool_settings(raw.get("pool"))
    task = {k: task[k] for k in TASK_KEYS if k in task}
    seeds = {k: legacy_text(raw, k) for k, _ in LEGACY_SEEDS if legacy_text(raw, k)}
    legacy = [k for k, _ in LEGACY_SEEDS if k in raw]
    return task, seeds, legacy


def load_pool_settings(raw):
    """The pool section of the task file: initial +0 sessions per sequence, sequences
    that start with their +2 only, and a cap on all sessions."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SetupError("研究任务的 pool 要写成 {\"p0\": 1, \"later\": [\"liaison\", \"product\", \"alg\"], \"max_sessions\": 30}")
    if "p1" in raw:
        raise SetupError("研究任务的 pool.p1 从 1.1.0 起没有了：各序列只有 +2 和 +0 两级，把这一行删掉")
    unknown = sorted(k for k in raw if k not in POOL_DEFAULTS and not k.startswith("_"))
    if unknown:
        raise SetupError("研究任务的 pool 里有不认识的字段：%s；能写的是 p0、later、max_sessions、label" % "、".join(unknown))
    out = dict(POOL_DEFAULTS)
    out.update({k: v for k, v in raw.items() if not k.startswith("_")})
    limits = orgspec.OrgSpec().rules["limits"]
    for key in ("p0",):
        v = out[key]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= limits[key]:
            raise SetupError("研究任务的 pool.%s 要写 0 到 %d 之间的整数：每个序列初始开几个 +%s 会话" % (key, limits[key], key[1]))
    later = out["later"]
    if not isinstance(later, list) or not all(isinstance(x, str) for x in later):
        raise SetupError("研究任务的 pool.later 要写成序列名的列表，比如 [\"liaison\"]")
    if "pmo" in later:
        raise SetupError("pool.later 不能有 pmo：路由和书记员开池时就要在")
    cap = out["max_sessions"]
    if not isinstance(cap, int) or isinstance(cap, bool) or not POOL_SESSIONS[0] <= cap <= POOL_SESSIONS[1]:
        raise SetupError("研究任务的 pool.max_sessions 要写 %d 到 %d 之间的整数" % POOL_SESSIONS)
    label = out["label"]
    if not isinstance(label, str) or len(label.strip()) > 40 or re.search(r"[\[\]\n]", label):
        raise SetupError("研究任务的 pool.label 要写成 40 个字以内的文字，不带方括号：它是会话标题的前缀和侧栏分区的名字；"
                         "不写就用飞书项目空间的名字")
    out["label"] = label.strip()
    out["later"] = list(dict.fromkeys(later))
    return out


def pick_sequences(spec, wanted):
    all_ids = [s for s, _, _ in spec.sequences()]
    if not wanted:
        return all_ids
    chosen = [s.strip() for s in wanted.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in all_ids]
    if unknown:
        raise SetupError("没有这些序列：%s；可选：%s" % ("、".join(unknown), "、".join(all_ids)))
    for s in REQUIRED_SEQUENCES:
        if s not in chosen:
            raise SetupError("序列 %s 必须启用：项目管理是入口，质量负责验收" % s)
    return [s for s in all_ids if s in chosen]


def settle_later(task, seqs):
    """pool.later against the enabled sequences: the default only speaks of those that are enabled."""
    if task["pool"]["later"] == POOL_DEFAULTS["later"]:
        task["pool"]["later"] = [x for x in task["pool"]["later"] if x in seqs]
    unknown = [x for x in task["pool"]["later"] if x not in seqs]
    if unknown:
        raise SetupError("pool.later 里的序列没有启用：%s" % "、".join(unknown))


def refuse_other_runtime(target):
    """A directory with the organisation of the Codex adapter (.agent-org/, in the plugin until 1.0.12) is left alone."""
    if os.path.isfile(os.path.join(target, ".agent-org", "manifest.json")):
        raise SetupError("目标目录已有另一运行平台的组织。请使用独立目录，先保留原绑定、任务包与回执，再迁移")


def load_ids(target):
    path = os.path.join(target, "feishu", "ids.json")
    if not os.path.isfile(path):
        return {}
    try:
        ids = json.loads(read_text(path))
    except ValueError:
        raise SetupError("%s 不是合法 JSON" % path)
    if not isinstance(ids, dict) or any(not isinstance(ids.get(k), (dict, type(None))) for k in ("meegle", "wiki", "lark")):
        raise SetupError("%s 的结构不对：要是一个对象，meegle、wiki、lark 各是一个对象" % path)
    return ids


# --------------------------------------------------------------- roles
def agent_name(seq, rank):
    if seq == "pmo" and rank == 1:
        return "pmo-router"
    if seq == "pmo" and rank == 0:
        return "pmo-scribe"
    return "%s-p%d" % (seq, rank)


def tag_of(abbrev, rank):
    return "[%s+%d]" % (abbrev, rank)


def write_paths(seq, rank, task):
    if seq == "pmo":
        return {2: ["packages/**", "inbox/**", "org/**"], 1: ["packages/drafts/pmo/**"],
                0: ["packages/**", "feishu/**"]}[rank]
    paths = ["packages/drafts/%s/**" % seq]
    if rank == 2:
        paths.append(".claude/agent-memory/%s/**" % agent_name(seq, rank))
    if seq == "rnd":
        paths += list(task["editable_paths"]) + ["runs/**"]
    elif seq in ("alg", "quality"):
        paths.append("runs/%s/**" % seq)
    elif seq == "infra":
        paths += ["infra/**", "runs/infra/**"]
    return paths


def build_policy(spec, task, seqs):
    rules = spec.rules
    limits, fixed = rules["limits"], rules["naming"]["fixed_sessions"]
    agents = {}
    for seq in seqs:
        abbrev = spec.term("abbrev", seq)
        for rank in spec.ranks_of(seq):
            name = agent_name(seq, rank)
            pattern = fixed.get(name) or ("%s-p0-<任务编号>" % abbrev if rank == 0 else "%s-p%d-<序号>" % (abbrev, rank))
            pol = {"tag": tag_of(abbrev, rank), "abbrev": abbrev, "rank": rank, "sequence": seq,
                   "sequence_cn": spec.term("seq", seq),
                   "limit": limits[name] if name in limits else limits["p%d" % rank], "fixed_session": fixed.get(name),
                   "session_pattern": pattern, "spawn": spec.dispatch_targets(name, seq, rank, seqs),
                   "write_paths": write_paths(seq, rank, task), "lark_write": [],
                   "meegle_write": [], "ledger_write": False}     # 1.1.0: a comment on a work item is the scribe's too
            if name == "pmo-scribe":
                pol.update(meegle_write=SCRIBE_MEEGLE, ledger_write=True)
            elif name == "pmo-p2":
                pol["lark_write"] = ["im +messages-send", "im +messages-reply"]
            agents[name] = pol
    p2s = [agent_name(s, 2) for s in seqs]
    infra = [agent_name("infra", r) for r in spec.ranks_of("infra")] if "infra" in seqs else []
    # Hard: what the rules and the claims depend on. The researcher changes these through init.
    owned = {".claude/hooks/**": [], ".claude/settings.json": [], ".claude/settings.local.json": [],
             ".claude/state/**": [], ".claude/agents/**": [], ".claude/solo-lab/**": [], "spec/task.json": [],
             ".claude/**": infra}
    # Soft: who drafts which document; the settings still ask the researcher before org/ and spec/ change.
    soft_owned = {".claude/agent-memory/**": p2s, "org/**": ["pmo-p2"],
                  "spec/spec.md": ["alg-p2"] if "alg" in seqs else [],
                  "spec/requirements.md": ["product-p2"] if "product" in seqs else [],
                  "spec/eval-protocol.md": ["quality-p2"]}
    entry = rules["entry"]
    policy = {
        "version": POLICY_VERSION,
        "entry_role": entry["role"],
        "router_role": entry["router"],
        "scribe_role": entry["scribe"],
        "single_claim_roles": [entry["role"], entry["scribe"]],
        "channel": "send_message",
        "agents": agents,
        "default_agent": {"tag": "[unknown]", "abbrev": "", "rank": 0, "sequence": "", "sequence_cn": "", "limit": 0,
                          "fixed_session": None, "session_pattern": "", "spawn": [], "write_paths": [],
                          "lark_write": [], "meegle_write": [], "ledger_write": False},
        "h_paths": list(task["protected_paths"]),         # 评测协议的只读路径
        "x_paths": list(task["editable_paths"]),          # 可改范围的可写路径
        "h_writers": [agent_name("quality", r) for r in spec.ranks_of("quality")],
        "x_writers": [agent_name("rnd", r) for r in spec.ranks_of("rnd")] if "rnd" in seqs else [],
        "owned_paths": owned,
        "soft_owned": soft_owned,
        "hard_limits": [entry["scribe"]],
        "identity": {"user_domains": ["docs", "wiki", "drive", "markdown", "base", "sheets"],
                     "bot_commands": ["im +messages-send", "im +messages-reply", "event consume"]},
        "dispatch": {k: v for k, v in rules["dispatch"].items() if k != "note"},
        "msg": dict(spec.terms["msg"]),
        "sequences": seqs,
    }
    policy["pool"] = {k: v for k, v in task["pool"].items() if k != "label"}      # the label only names titles
    return policy


# The interpreter every command the plugin writes names: the hooks in settings.json, the permission rules, the
# role texts, the skills. From 1.0.12 the scripts are tested under Python 3.12 alone and run there alone (the
# researcher, 2026-11-20). The command names the version, python3.12 and not python3: on macOS python3 is the
# system's 3.9, and whatever the researcher installed for it (their experiments' packages) hangs on that name.
PYTHON = "python3.12"
MIN_PYTHON = (3, 12)
BIN = "%s -I .claude/solo-lab/bin/" % PYTHON
POOL_ALLOW = ["Bash(python3.12 -I .claude/solo-lab/bin/onboarding.py *)"] + [
    "Bash(%s%s)" % (BIN, c) for c in ("pool.py show", "route.py audit", "route.py cross")] + [
    "Bash(%s%s *)" % (BIN, c) for c in ("pool.py claim", "pool.py expect", "pool.py show", "pool.py refill", "todo.py add", "todo.py done",
                                         "route.py audit", "route.py cross", "notify.py send", "notify.py focus")]
SCRIBE_ALLOW = ["Bash(python3.12 -I .claude/solo-lab/bin/%s.py %s *)" % (script, w) for script, words in (
    ("workitem", ("register", "reply", "confirm", "rollback", "update", "comment", "archive")),
    ("docs", ("upload", "attach", "revise", "status-block", "append", "relink", "ensure-node"))) for w in words]
POOL_HOOK_EVENTS = ("Stop", "StopFailure", "Notification", "PermissionRequest", "PermissionDenied", "PostCompact")
# The desktop app's terminal panel tool runs shell commands outside the Bash tool. A matcher with
# regex characters is an unanchored regex, so it gets its own anchored group.
TERMINAL_TOOL = "^mcp__.+__run_in_terminal$"
# The desktop app's end-of-turn tool (an MCP tool whose name ends in __wrap_up) can draw a question
# box for the researcher; guard.py takes the question out in a turn another session woke.
WRAPUP_TOOL = "^mcp__.+__wrap_up$"


def build_settings(task, risk, policy):
    high = sorted(k for k, v in risk.get("commands", {}).items() if v == "high-risk-write")
    deny = ADMIN_DENY + ["Bash(lark-cli %s *)" % k for k in high if k != "update"]
    ask = ["Edit(/.claude/settings.json)", "Edit(/.claude/settings.local.json)", "Edit(/.claude/hooks/**)",
           "Edit(/.claude/agents/**)", "Edit(/.claude/solo-lab/**)", "Edit(/.claude/state/**)", "Edit(/org/**)",
           "Edit(/spec/**)"]
    ask += ["Edit(/%s)" % p for p in task["protected_paths"]]
    allow = list(READ_ALLOW)

    def hook(script):
        return [{"type": "command", "timeout": 30,
                 "command": "python3.12 -I -B \"$CLAUDE_PROJECT_DIR/.claude/hooks/%s\"" % script}]
    hooks = {
        "PreToolUse": [{"matcher": "Bash|Monitor|Edit|Write|NotebookEdit|Agent|Task|AskUserQuestion",
                        "hooks": hook("guard.py")},
                       {"matcher": TERMINAL_TOOL, "hooks": hook("guard.py")},
                       {"matcher": WRAPUP_TOOL, "hooks": hook("guard.py")}],
        "PostToolUse": [{"matcher": "Bash|Monitor|Agent|Task", "hooks": hook("audit.py")},
                        {"matcher": TERMINAL_TOOL, "hooks": hook("audit.py")}],
        "PostToolUseFailure": [{"matcher": "Bash|Monitor|Agent|Task", "hooks": hook("audit.py")},
                               {"matcher": TERMINAL_TOOL, "hooks": hook("audit.py")}],
        "SubagentStart": [{"hooks": hook("instances.py")}],
        "SubagentStop": [{"hooks": hook("instances.py")}],
        "SessionStart": [{"hooks": hook("instances.py")}],
        "SessionEnd": [{"hooks": hook("instances.py")}],
        "UserPromptSubmit": [{"hooks": hook("inbox.py")}],
    }
    out = {}
    out["env"] = {"MEEGLE_NO_UPDATE_CHECK": "1", "MEEGLE_AI_HANDOFF": "disabled"}
    out["autoMemoryEnabled"] = False          # 21 sessions would share one memory directory
    allow += POOL_ALLOW + SCRIBE_ALLOW        # guard.py denies the ledger writes of every session but the scribe
    for event in POOL_HOOK_EVENTS:
        hooks[event] = [{"hooks": hook("instances.py")}]
    out["permissions"] = {"allow": allow, "ask": ask, "deny": deny}
    out["hooks"] = hooks
    return out


# ----------------------------------------------------------- lark risk
def installed_lark_version():
    code, out, _ = run(["lark-cli", "--version"])
    found = re.findall(r"\d+\.\d+\.\d+", out or "") if code == 0 else []
    return found[0] if found else None


def resolve_risk(target):
    """Prefer an existing table for the installed lark-cli; else the plugin's static table."""
    static = json.loads(scaffold("hooks/lark-risk.static.json"))
    installed = installed_lark_version()
    existing = os.path.join(target, ".claude", "hooks", "lark-risk.json")
    if installed and os.path.isfile(existing):
        try:
            table = json.loads(read_text(existing))
            if table.get("lark_cli_version") == installed:
                return table, "风险表：沿用项目里 lark-cli %s 的风险表" % installed
        except ValueError:
            pass
    note = "风险表：用插件自带的静态表（lark-cli %s）" % static.get("lark_cli_version")
    if installed and installed != static.get("lark_cli_version"):
        note += "；本机是 %s，新命令的风险等级可能缺失，未知的写命令一律按写处理" % installed
    return static, note


# -------------------------------------------------------------- render
def naming_lines(spec):
    n = spec.rules["naming"]
    fixed = "、".join("%s 是 `%s`" % (k, v) for k, v in n["fixed_sessions"].items())
    return [
        "会话名写作 `%s`；+0 写作 `%s`；%s。" % (n["session"], n["session_p0"], fixed),
        "会话标签写作 `%s`，飞书评论和交接记录的开头都带上它。" % n["tag"],
        "工作项名称写作 `%s`；主文档标题写作 `%s`；挂在下面的答复写作 `%s`。" % (n["work_item"], n["main_doc"], n["reply_doc"]),
        "草稿路径写作 `%s`，序列用 product、alg、rnd、infra、quality、pmo、liaison。" % n["draft"],
        "日期字段写目标日期的 UTC 中午。meegle 的 workitem 命令里工作项类型写 api_name；MQL 的表名认 type_key 或类型现名，不认 api_name。台账脚本已经处理好，手写时注意。",
    ]


def describe_writes(pol):
    parts = []
    if pol["ledger_write"]:
        parts.append("`workitem.py` 和 `docs.py` 的全部写命令")
    parts += ["`lark-cli %s`" % k for k in pol["lark_write"]]
    parts += ["`meegle %s`" % k for k in pol["meegle_write"]]
    return "、".join(parts) or "无"


def tools_for(seq, pol):
    tools = ["Read", "Grep", "Glob", "Bash", "Skill", "Write", "Edit"]
    if seq in ("product", "alg"):
        tools += ["WebSearch", "WebFetch"]
    if pol["spawn"]:
        tools.append("Agent")
    return tools


def general_rank_text(spec, rank):
    row = spec.rank_rows()[rank]
    return "\n".join([
        "- 给定的上下文：%s" % row["given"], "- 需要产出：%s" % row["outputs"],
        "- 上下文维护标准：%s" % row["context"], "- 什么时候上报：%s" % row["escalate"],
        "- 通用规则：%s。" % "；".join(h.rstrip("。") for h in spec.general_rules_heads()),
    ])


SPEC_DOC_NAMES = {"spec/spec.md": "实验规格", "spec/eval-protocol.md": "评测协议", "spec/requirements.md": "需求文档"}


def msg_head(spec, kind, item="<工作项 ID>", sender="<你的会话名>", receiver="<接收方会话名>"):
    word = spec.term("msg", kind)
    return "【%s%s】%s → %s" % (word, " #%s" % item if item else "", sender, receiver)


STATUS_CMD = pool_state.STATUS_CMD % "<研究任务 ID>"     # the command the hook names after a compaction, too


def after_compaction(upper="上一级", entry=False):
    """What every role does once its context was compacted (the researcher, 2026-11-16): a session then
    remembers the work items it holds, not where they sit in their research task, so it reads the task's
    状态包 before it goes on. upper: whom it tells when the 状态包 and the package in hand disagree. The entry
    keeps the 状态包 of every research task that is not archived, so it reads those too."""
    whose = ("没归档的研究任务（`ledger.py query tasks`），和手上工作项所属的研究任务" if entry else "手上工作项所属的研究任务")
    nothing = "没有研究任务可读" if entry else "手上没有工作项"
    return ("压缩上下文以后、接着干活之前，先读研究任务的状态包：找出%s（`ledger.py get <工作项 ID>` 的“所属研究任务”字段；"
            "没有这个字段的，用 `ledger.py trace <工作项 ID>` 沿“来源工作项”往上找到研究任务），运行 `%s`，读完再继续。"
            "状态包里已定的实验条件和暂停范围，和手上任务包的写法冲突时，以状态包和台账为准，并报给%s。"
            "%s，或者找不到所属研究任务，说一句“跳过”再继续；`ledger.py status` 读失败，报出失败原因后继续，不卡住。" % (
                whose, STATUS_CMD, upper, nothing))


def common_role_lines(spec, seq, rank, name, pol, task, policy):
    lines = [
        "- 文档谁负责谁写，工作项只由书记员写。你写的包和文档由你自己用 `docs.py` 传进飞书、自己修订，脚本会核对你是不是作者；"
        "建工作项、填字段、确认和回滚节点、归档、加评论，给书记员发登记请求。直接敲 `lark-cli`、`meegle` 的写命令会被 hook 拒绝；"
        "要删的文档记进研究员任务清单：`todo.py add --text '删文档 <链接>：<原因>'`。",
        "- 读台账和文档：`ledger.py get`、`ledger.py query`、`ledger.py trace`、`ledger.py status`，以及 `lark-cli docs +fetch --as user`。",
    ]
    docs = [p for p, owners in sorted(policy["soft_owned"].items()) if name in owners and p in SPEC_DOC_NAMES]
    writable = code_list(pol["write_paths"]) + ("，以及你起草的 %s（写之前会请研究员确认）" % code_list(docs) if docs else "")
    lines.append("- 本地文件照规范只写：%s；评测协议的只读路径（%s）%s。" % (
        writable, code_list(task["protected_paths"]), "改动要研究员确认" if name in policy["h_writers"] else "不能改"))
    for doc in docs:
        how = "" if doc == "spec/requirements.md" else "用技能 `solo-lab:spec` 访谈研究员后写，每条标来源；"
        lines.append("- 你负责起草%s `%s`：%s正式实验流程第 4 步研究员签字时，它和别的前四步产出一起冻结。" % (SPEC_DOC_NAMES[doc], doc, how))
    return lines


def sequence_lines(seq):
    lines = []
    if seq == "quality":
        lines.append("- 正式分数只由质量序列在冻结的 Eval Set 上跑出，正式集每一轮只跑一次；别的序列自己跑的是开发分，不进结论。评测协议的只读路径只有质量序列能改，改动要研究员确认。")
    if seq == "liaison":
        lines.append("- 材料写成交付包，自己用 `docs.py upload` 传进知识库；每份材料发出前要研究员签字（走决策包），研究员自己转交。读者不用飞书：不给他们开任何飞书权限。读者是研究员本人或组织里的会话的东西，不归外联写。")
    return lines


def agent_mechanics(spec, seq, rank, name, pol, task, policy):
    n = spec.rules["naming"]
    scribe = policy["agents"]["pmo-scribe"]["fixed_session"]
    entry = policy["agents"]["pmo-p2"]["fixed_session"]
    router = policy["agents"]["pmo-router"]["fixed_session"]
    reg, receipt = spec.term("msg", "register_request"), spec.term("msg", "register_receipt")
    if rank == 0:
        session = ("会话池里你是一个桌面会话，池位名写作 `%s-p0-01` 这样的两位序号：侧栏标题、短名和消息抬头都用它。"
                   "接受委托时，委托消息里“会话名：”那一行给的 `%s` 只写进包里；任务编号是“上级工作项 ID-序号”，比如 2101-3。" % (pol["abbrev"], pol["session_pattern"]))
    else:
        session = "会话池里你是一个桌面会话，会话名在开局那句话里，写作 `%s` 这样的两位序号，认领后不变，消息抬头用它。" % pol["session_pattern"].replace("<序号>", "01")
    lines = [
        "- 会话名：%s会话标签是 `%s`。" % (session, pol["tag"]),
        "- 开工或接手：先读交给你的包；再用 `python3.12 -I .claude/solo-lab/bin/ledger.py get <工作项 ID 或任务编号>` 读它，读主文档的“状态包”接上进度。不靠对话记状态。",
        "- " + after_compaction("路由" if rank == 2 else "委托你的会话"),
        "- 写包用技能 `solo-lab:packages`（模板副本在 `.claude/solo-lab/templates/`）。草稿写到 `packages/drafts/%s/`，文件名照 `%s`；写完跑 `python3.12 -I .claude/solo-lab/bin/orgspec.py lint <路径>`。" % (seq, n["draft"]),
    ]
    # 1.1.0: whoever delivers uploads its own delivery (docs.py upload). For a work item the scribe then fills the
    # request's 交付包 field and confirms 执行; a closed task has no work item, and its delivery needs nobody else.
    report_head = msg_head(spec, "report", item="<编号>" if rank == 0 else "<工作项 ID>", receiver="<委托方会话名>")
    sent_line = "发完把回执（delivered 或 queued，以及 message_id）写进本轮的最后一句。"
    notice, resume = spec.term("msg", "notice"), spec.term("msg", "resume")
    zero, plan = agent_name(seq, 0), spec.sequence(seq)["plan"]
    if rank == 0:
        lines.append("- 交付：写好交付包，运行 `%sdocs.py upload <交付包路径> --apply` 自己上传。执行的任务，包头“关联工作项”写委托给你的任务编号（#<上级工作项 ID>-<序号>），"
                     "它挂在需求包下面，不找书记员。下发的需求，包头“关联工作项”写那条工作项的 ID，上传以后给 `%s` 发【%s】：写 `workitem.py reply <交付包路径> --apply`，"
                     "再写一行“确认节点：#<需求 ID>，由 <你的会话名> 提出”，它填“交付包”字段并确认“执行”这一步，等它回【%s】。"
                     "两种都是最后给委托方发消息，首行写 `%s：<包名> <包路径>`，下面是三行以内的摘要。%s" % (
                         BIN, scribe, reg, receipt, report_head, sent_line))
    else:
        lines.append("- 交付：写好交付包，运行 `%sdocs.py upload <交付包路径> --apply` 自己上传，它挂在需求的主文档下面。再给 `%s` 发【%s】：写 `workitem.py reply <交付包路径> --apply`，"
                     "再写一行“确认节点：#<需求 ID>，由 <你的会话名> 提出”，它填“交付包”字段并确认“执行”这一步。"
                     "拿到【%s】后给委托方发消息，首行写 `%s：<包名> <包路径>`，下面是三行以内的摘要。%s%s" % (
                         BIN, scribe, reg, receipt, report_head,
                         "别的序列发来的跨序列需求，委托方就是发起它的那个 +2：交付直接发给它，不经入口和路由。", sent_line))
    # 1.1.0: a closed task (执行) has no work item. The session that hands it out uploads the package, gets a
    # number for it, sends it and accepts the delivery itself; the scribe is not asked.
    to_p0 = ("这类活不建工作项，也不找书记员：运行 `%sdocs.py upload <草稿路径> --source <%s> --apply`，输出里给出任务编号（上级工作项 ID-序号，比如 2101-3）。"
             "再跑 `pool.py show --role %s` 找空闲的 +0，发 `%s：<包名> <包路径>`，消息里单独写一行“会话名：%s-p0-<编号>”；没有空闲的先放着，`pool.py show --queue <你的会话名>` 会列出还没派出去的。"
             "收到【%s #<编号>】后对照验收标准看交付包（`ledger.py get <编号>` 列出它的链接），运行 `docs.py accept <编号> --result 通过 --apply`；"
             "不通过的写 `--result 不通过 --note '<要改什么>'`，再给它发【%s #<编号>】。")
    overlap = "要派的活和在途的 +0 有没有改同一批文件，有重叠的排在它后面做，不并行"
    if rank == 2:
        # 1.1.0: a sequence has no +1. What needs judgement on the way goes to a +0 as a 下发 request: the +0 hands in
        # a plan first and the +2 confirms it (the first step of a 同序列需求). A closed task goes out as before.
        lines.append("- 同序列的活委托给 +0，分两种，看一件事做到一半还需不需要新的信息或新的判断。需要的（要设计、要开跑一个批次并盯着、要合并和解冲突这类）写下发关系："
                     "建工作项，承接的 +0 先交方案，你确认了它才动手，之后它在方案的范围内自己判断。不需要的是封闭任务，写执行关系：方法和验收标准一起写明，不建工作项。"
                     "补 +0 之前先看：%s。" % overlap)
        lines.append("- 委托下发的需求：写好需求包，运行 `%sdocs.py upload <草稿路径> --apply` 自己上传，再给 `%s` 发【%s】（写草稿路径）。"
                     "拿到【%s #ID】后先运行 `docs.py relink <ID> --apply` 把工作项 ID 写进文档，再跑 `python3.12 -I .claude/solo-lab/bin/pool.py show --role %s` 找空闲的 +0，"
                     "发 `%s：<包名> <包路径>`。没有空闲的，工作项留在“%s”排队，等有会话空出来再委托。" % (
                         BIN, scribe, reg, receipt, zero, msg_head(spec, "assign", receiver="<+0 的池位名>"), spec.term("state", "queued")))
        lines.append("- 确认方案：承接的 +0 用【%s #ID】把方案交给你。对照需求包的目标和验收标准看三件事：照这个做法能不能达到验收标准，拆出去的子任务是不是封闭任务，资源够不够。"
                     "本序列的方案是%s。可以：给 `%s` 发【%s】，写“确认节点：#<ID>，由 <你的会话名> 提出，必填项：方案=@<方案文件>”，拿到【%s】后给那个 +0 发【%s #ID】让它动手。"
                     "要改：用【%s #ID】告诉它改什么，不确认节点。方案确认以后，要改目标、改验收标准、超出资源上限或者超出方案的事，它会停下来找你："
                     "你同意的，它交方案的下一版，你请书记员把“方案”字段换成新的（动作“改字段”，写 方案=@<新的方案文件>）。" % (
                         notice, plan, scribe, reg, receipt, resume, resume))
        lines.append("- 委托封闭任务：需求包写执行关系。" + to_p0 % (
            BIN, "这件活所属的工作项 ID：研究任务、跨序列需求或下发的需求", zero, msg_head(spec, "assign", item="<编号>", receiver="<+0 的池位名>"), pol["abbrev"],
            spec.term("msg", "report"), resume))
        if seq in (task.get("pool") or {}).get("later", []):
            lines.append("- 本序列开池时只有你一个会话：活自己做。有封闭任务（查文献、跑一段分析、独立复核这类），或者有可以先交方案再做的问题时补 +0。")
        lines.append("- 验收工作项（下发的需求、跨序列需求）：收到【%s】后，对照需求包的验收标准看交付包，`ledger.py get <需求 ID>` 的“交付包”字段是它的链接。通过：给 `%s` 发【%s】，动作“确认节点”和“归档”，"
                     "写“确认节点：#<需求 ID>，由 <你的会话名> 提出”。不通过：动作“回滚节点”，写“回滚节点：#<需求 ID> 退回到 执行，原因：<一句话>”，"
                     "拿到【%s】后给承接方发【%s】说明要改什么。你发起的跨序列需求，承接方 +2 直接给你发【%s】，验收的做法一样，要改什么也直接告诉它。" % (
                         spec.term("msg", "report"), scribe, reg, receipt, resume, spec.term("msg", "report")))
        lines.append("- 上限和补位：本序列的 +0 最多 %d 个会话，由你开，侧栏里嵌在你下面。没有空闲会话就排队。"
                     "开局那句话点了名的，认领那一轮就开；此后研究员在你的会话里明确说“补位”或“再开一个 …”时照表开"
                     "（开法照 `pool.py claim` 输出里的表，开好马上 `pool.py expect` 预登记）。人手不够时运行 `pool.py refill %s` 记一条补位请求，"
                     "同一轮为表里“补位用”的那一行提一张启动卡（先 `pool.py expect … --card`，再 start_session，initiation 写 own_initiative），"
                     "研究员在你的会话里点一下就开。+0 记了补位请求的，也由你提卡。" % (spec.rules["limits"]["p0"], zero))
        lines.append("- 你开的会话每结束一轮，应用会叫醒你一次。这不算交付：先跑 `pool.py show --queue <你的会话名>`，有等你委托的事、又有空闲会话就委托出去，没有就一句话结束本轮。")
    else:
        lines.append("- 委托给你的活分两种，看需求包包头的“关系”。执行：封闭任务，编号是“上级工作项 ID-序号”，照需求包的方法和验收标准做完就交付。"
                     "任务中途第二次被要求转向（改方法、改范围、撤掉重来这类）时先停下，告诉委托方“这个任务不是封闭任务”，由它改成下发的需求或者收回自己做；第一次转向照做。"
                     "下发：编号是纯数字的工作项 ID，做法由你定，先交方案。")
        lines.append("- 下发的需求先交方案：照 `.claude/solo-lab/templates/plan.md` 写，本序列的方案是%s。存到 `packages/drafts/%s/`，运行 `%sdocs.py attach <工作项 ID> 方案 <方案文件> --apply` 自己上传，"
                     "再给委托方发 `%s：方案 <方案文件的路径>`。它确认了（你会收到【%s #ID】）才动手；要你改的，改好重新上传，再发一次。"
                     "方案确认以后，范围内的调整自己定，记进交付包的“和方案的出入”一节。要改目标、改验收标准、超出资源上限或者超出方案的，停下来用【%s #ID】告诉委托方；"
                     "它同意了，你写方案的下一版重新上传，把新文件的路径告诉它。" % (
                         plan, seq, BIN, msg_head(spec, "notice", receiver="<委托方会话名>"), resume, notice))
        lines.append("- 下发的需求里有能分给别人做的封闭部分时，写进方案的“拆分”一节，方案确认以后由你派给本序列别的 +0：需求包写执行关系，按验收标准划分，不按运行次数划分。" + to_p0 % (
            BIN, "你手上这条下发需求的工作项 ID", zero, msg_head(spec, "assign", item="<编号>", receiver="<+0 的池位名>"), pol["abbrev"],
            spec.term("msg", "report"), resume)
            + "没有空闲的 +0 时运行 `pool.py refill %s` 记一条补位请求，本序列 +2 会提启动卡。子任务都验收以后，把它们的结论写进你的交付包的“子任务汇总”一节。" % zero)
    if rank == 2:
        # 1.0.12: a request to another sequence goes to the router once (duplicates, conflicts, registration), and
        # from then on the two +2 talk to each other. Until then every message of it passed through the entry.
        lines.append("- 跨序列的需求包写成草稿，运行 `%sdocs.py upload <草稿路径> --apply` 自己上传，再给路由 `%s` 发 `%s：跨序列需求包 <包路径>`。它对照台账查重复和冲突，请书记员登记，"
                     "用【%s #ID】把工作项 ID 回给你；你运行 `docs.py relink <ID> --apply`，再直接给承接方 +2 发 `%s：<包名> <包路径>`。它说和进行中的工作重复、或者碰到冻结项的，照它说的先处理，不发委托。" % (
                         BIN, router, msg_head(spec, "notice", item=None, receiver=router), spec.term("msg", "notice"),
                         msg_head(spec, "assign", receiver="<承接方 +2 的会话名>")))
        lines.append("- 决策包写成草稿，运行 `%sdocs.py upload <草稿路径> --apply` 自己上传，给 `%s` 发【%s】登记；拿到【%s #ID】后运行 `docs.py relink <ID> --apply`，"
                     "再给路由 `%s` 发 `%s：<事项一句话> <主文档链接>`。路由转告入口，入口讲给研究员；你不直接找入口。" % (
                         BIN, scribe, reg, receipt, router, msg_head(spec, "decide", receiver=router)))
        lines.append("- 进展和里程碑发给路由 `%s`，不发入口：研究任务走完一步、跨序列需求验收、出现要研究员动手的事、工作项过了截止时间，用【%s #ID】告诉它，它改状态包、转告入口。"
                     "研究员的话由入口交路由、路由转给你，抬头是【%s】或【%s #ID】，是原话，路由只加了抬头。" % (
                         router, spec.term("msg", "notice"), spec.term("msg", "notice"), spec.term("msg", "resume")))
        lines.append("- 别的序列发来的跨序列需求，发起方 +2 直接用【%s #ID】发给你。先接单：给 `%s` 发【%s】，写“确认节点：#<ID>，由 <你的会话名> 提出”和三项必填（需求复述、承接范围、预计交付时间），"
                     "这时还不写交付包；拿到【%s】后用【%s #ID】把这三项告诉发起方。不接：请书记员“回滚节点：#<ID> 退回到 提出”并写原因，再告诉发起方。接了以后序列内照常委托。" % (
                         spec.term("msg", "assign"), scribe, reg, receipt, spec.term("msg", "notice")))
        lines.append("- 跨序列需求登记以后，澄清、答复、进度和验收意见都带着工作项 ID 直接和对方 +2 往来，不经入口和路由；路由从登记回执和派发日志看进展。"
                     "改范围、改成功标准、改冻结项不在往来里谈定：写决策包，照上面登记后交路由。")
        lines.append("- 研究任务走到你们序列负责的那一步时，这一步归你推进；书记员确认上一步以后会用【%s】告诉你。先用 `python3.12 -I .claude/solo-lab/bin/ledger.py next <研究任务 ID>` 看这一步必填什么，"
                     "序列内的事委托 +0，要别的序列配合就发跨序列的需求包。要填链接的产出有两条路。你自己写的（和研究员谈出来的规格、需求文档这一类）：写在本地，"
                     "运行 `docs.py attach <任务 ID> <字段> <本地文件> --apply` 自己传到研究任务单下面，登记请求里把字段写成 `<字段>=@<本地文件>`，书记员照文件找到那份文档、填上链接；"
                     "研究员签字前要看的，把 attach 输出的链接写进决策包。委托下去做的（要查、要跑、要整理的调研文档、实验方案、探索记录这一类）："
                     "走本序列的一条同序列需求，验收通过以后用承接方那份交付包文档的链接，它在那条需求的“交付包”字段里。产出齐了，给 `%s` 发【%s】请它确认节点，写明各必填字段的值。"
                     "正式分数只认质量序列在冻结的 Eval Set 上跑出的。" % (spec.term("msg", "notice"), scribe, reg))
    else:
        if seq != "quality":
            lines.append("- 交付包里写明材料来源、每个变体的结果和运行 ID，失败的也列。你自己跑出的分数是开发分，只用来调试，不当结论写。")
        lines.append("- 需要别的序列配合的、要拍板的事，写进交付包，由本序列 +2 去办；等不到交付的，用【%s】先告诉本序列 +2。你不直接找别的序列。" % notice)
    lines += common_role_lines(spec, seq, rank, name, pol, task, policy)
    if rank == 2:
        lines += [
            "- 研究员会直接点开你的会话交代事情。这时先运行 `python3.12 -I .claude/solo-lab/bin/notify.py focus open --topic '<一句话>'`，用 `notify.py send %s --text '…'` 告诉路由；做完或研究员说“结束”时运行 `notify.py focus close`。谈定的事写成决策包（渠道：研究员主动发起），照决策包的做法上传、登记，再交路由。没写进台账的决定不算数。" % router,
            "- 被其他 agent 会话叫醒的轮次，研究员多半不在，不向研究员提问：要研究员拍板的事写决策包，登记后给路由发【%s】；+0 要拍板的事写进交付包。收尾里的提问 hook 会去掉。" % spec.term("msg", "decide"),
            "- 只要研究员动一下手、没有选项要他挑的事（恢复集群连接、下载数据、连 VPN、跑一条只有他能跑的命令这类），不写决策包：运行 `todo.py add --text '<要他做什么>' --for <工作项 ID> --done-when '<做完怎么算数>'` 记进研究员任务清单，入口向研究员列出；他做完了，或者你自己核对到了结果，运行 `todo.py done <编号>`。建错的、不做了的工作项归档不了，也不去推它的节点：运行 `todo.py add --for <工作项 ID> --terminate --text '<为什么不要它了>'`，由研究员在网页上终止。有两个以上的选项要他挑，或者做了以后不好撤回的，是决策，照旧写决策包。",
            "- 决策定下以后要研究员亲手做的事（整理数据、挂 API、维护沙箱、开 Docker、调设置这类），写在决策包包头“研究员要做的事”。"
            "每份决策包都有这一行，没有要研究员亲手做的写“无”。"
            "包头还有“执行方”一行，写这条决策定下以后由谁执行：“研究员”，或者执行它的会话名，多半是你自己。"
            "执行方写会话的只收各序列的 +2、入口和路由，要下级干的活由你用需求包委托。"
            "只要有一个选项要研究员亲手做，执行方就写“研究员”；他选了不用他动手的选项，路由请书记员确认“决定”时用 `--executor` 把执行方改成会话。"
            "执行方是研究员时“研究员要做的事”不能是“无”，执行方是会话时写“无”，对不上包检查会退回。"
            "被决策卡住的工作项，登记以后请书记员把阻塞原因写成“等研究员：决策 #<ID>”，这条决策归档以后再请它清掉。"
            "研究员定了，书记员确认“决定”，决策到“待执行”，并通知执行方。执行方是研究员的，他做完了，路由请书记员归档，再通知你；"
            "执行方是你的，书记员把决定内容发给你，你照决定执行完，马上请书记员归档，不在待执行里停留。",
            "- 决策登记以后、研究员定之前要改，复制主文档开头到“状态包”之前的部分改成新草稿，“版本”写上一版加 1，自己运行 "
            "`docs.py revise <决策 ID> <新草稿> --note '<改了什么>' --apply` 原位换掉，不另登记；输出里列出要书记员改的字段（紧急程度、执行方这类）的，照它给书记员发登记请求。修订不推给研究员。决策待定期间，包里“冻结的动作”谁都不做，选项中立的准备照常做。不可逆事项清单（`org/org.md`）里的事，"
            "要等研究员定了、所选选项里列了它才做。hook 不拦这些命令，也不按待定决策冻结，权限照 Claude Code 自己的检查。"
            "上面两条规矩不变，靠你照规则办、每轮的冻结提示和包检查。一件事可逆不可逆，你按判据判断，清单是必须写不可逆的下限。"
            "可逆的决策登记满到期时长（`org/decisions.md`，默认 24 小时）"
            "还没定，书记员按包头的到期默认动作记成决定，执行方照它办；不可逆的、要研究员亲手做事的、定过一次又回滚到待决定的一直等研究员。",
            "- 你有项目级记忆（`.claude/agent-memory/%s/`），开工前读一下。只记跨阶段要沿用的标准和判断；任务的当前状态以台账和状态包为准。" % name,
        ]
    else:
        lines.append("- 研究员也可能直接点开你的会话。照做，开工时用 `notify.py send <委托你的会话名> --text '…'` 告诉它。"
                     "被其他 agent 会话叫醒的轮次不向研究员提问，要拍板的事写进交付包；收尾里的提问 hook 会去掉。")
    lines += sequence_lines(seq)
    return "\n".join(lines)


def agent_file(spec, seq, rank, task, policy):
    name = agent_name(seq, rank)
    pol = policy["agents"][name]
    s = spec.sequence(seq)
    row = s["ranks"][rank]
    desc = "%s +%d。接手：%s。何时上报：%s。" % (s["cn"], rank, row["given"].replace("<br>", "；"),
                                         row["escalate"].replace("<br>", "；"))
    desc = POOL_DESC + desc
    values = {
        "name": name, "description_json": yaml_str(desc), "tools": ", ".join(tools_for(seq, pol)),
        "model": task["models"].get(name) or task["models"].get("p%d" % rank) or "inherit",
        "memory_line": "memory: project\n" if rank == 2 else "",
        "project": task["name"], "seq_cn": s["cn"], "rank": str(rank), "tag": pol["tag"],
        "seq_intro": s["intro"], "rank_given": row["given"], "rank_outputs": bullets(row["outputs"], "  "),
        "rank_escalate": row["escalate"], "seq_notes": ("\n" + s["notes"] + "\n") if s["notes"] else "",
        "general_rank": general_rank_text(spec, rank),
        "mechanics": agent_mechanics(spec, seq, rank, name, pol, task, policy),
    }
    return render(scaffold("agents/agent.md.tmpl"), values)


POOL_DESC = "不要用 Agent 工具派这个角色，给它的会话发消息。"
CROSS_RECORD = "packages/drafts/pmo/跨序列协调记录.md"     # the router keeps it; route.py cross names it too


def entry_values(spec, policy):
    """The parts of the entry's definition that name sessions and message heads. From 1.1.0 the entry talks to the
    researcher and to the router only: what the researcher says goes to the router once, written down as it was
    said; what the sequences report reaches the router, which passes on milestones and decisions."""
    pol = policy["agents"]["pmo-p2"]
    router = policy["agents"]["pmo-router"]["fixed_session"]
    me = pol["fixed_session"]
    return {
        "entry_runs_as": "在会话池里是研究员平时对话的会话",
        "entry_preface": "先写清工作方式：用中文回答，先给结论；先读再改，做完核对结果；研究员在场就问，不在场就写进决策队列，不猜参数、不编结果。你会运行很久，上下文一定会被压缩，所以不靠对话记状态：每次都从台账、状态包和 `python3.12 -I .claude/solo-lab/bin/pool.py show` 读。",
        "to_router_head": msg_head(spec, "notice", item=None, sender=me, receiver=router),
        "decide": spec.term("msg", "decide"),
        "entry_queue": "清单长、要附选项和建议时，请路由 `%s` 汇总成表：每件写状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、选项、建议，改过版的在事项后面写版本号；已经到期、继续等研究员的排最前，其次是“插队”的。研究员在本会话里" % router,
        "entry_focus": (
            "**研究员直接交代。** 当前这一步由谁负责，照上面“研究任务”一段查；两个流程的对照在 `org/phases.md`。\n\n%s\n\n"
            "这时那个会话会自己运行 `notify.py focus open`，并告诉路由；`pool.py show` 显示它“研究员在用”。"
            "谈定的事它会写成决策包（渠道：研究员主动发起），自己上传，请书记员登记、确认“决定”，再告诉路由；路由照 R6 通知受影响的会话，改状态包。\n\n**会话池。"
            "** 研究员说“恢复”时，运行 `pool.py show --recover`，把清单交给路由，由它照清单发消息、让书记员补完写到一半的登记。"
            "研究员在本会话明确说“补位”或“再开一个 …”时才开会话：路由由你开（书记员由路由开），其余序列的会话由那个序列的 +2 开；"
            "开法照 `pool.py claim` 输出里的开池表，开好马上 `pool.py expect` 预登记。没人要求时不开会话。"
            "你开的会话每结束一轮，应用会叫醒你一次，这不算交付：没有路由发来的消息，就一句话结束本轮。") % (
            bullets([spec.render(spec.rules["entry"]["focus_rule"])])),
        "entry_cross": "跨序列需求不经你：发起方 +2 交路由查重、登记，再直接委托承接方 +2，看进展用 `route.py cross` 和路由的协调记录 `%s`。" % CROSS_RECORD,
        "entry_permissions": "**权限提示。** 每个会话的权限提示只出现在它自己的会话里；`pool.py show --watch` 把等研究员批准的会话列在最前。请研究员不要对飞书写操作的提示选“不再询问”。",
        "after_compaction": after_compaction("研究员", entry=True),
    }


REGISTER_FORM = """```text
登记请求
- 草稿：packages/drafts/<序列>/<文件>.md（请求方已经用 docs.py upload 传好）
- 动作：新建工作项 / 填交付包字段 / 确认节点 / 回滚节点 / 归档 / 改字段 / 评论
- 来源工作项：#<ID>
- 确认节点：#<ID>，由 <提出请求的会话名> 提出，必填项：<字段名>=<值>；链接字段可以写 <字段名>=@<本地文件>（先用 docs.py attach 传好）或 <字段名>=<文档链接>
- 回滚节点：#<ID> 退回到 <节点名>，原因：<一句话>；研究任务的回退附研究员批准的决策 #<ID>
```"""


def pmo_file(spec, rank, task, policy):
    name = agent_name("pmo", rank)
    pol = policy["agents"][name]
    s = spec.sequence("pmo")
    row = s["ranks"][rank]
    model = task["models"].get(name) or task["models"].get("p%d" % rank) or "inherit"
    prefix = POOL_DESC
    common = {"project": task["name"], "session": pol["fixed_session"], "tag": pol["tag"], "model": model,
              "rank_given": row["given"], "rank_outputs": bullets(row["outputs"], "  "),
              "rank_escalate": row["escalate"], "naming": bullets(naming_lines(spec))}
    scribe = policy["agents"]["pmo-scribe"]["fixed_session"]
    entry = policy["agents"]["pmo-p2"]["fixed_session"]
    if rank == 2:
        desc = "项目管理 +2（入口）。研究员平时对话的会话：只和研究员对话，把他的话照录交给路由，读状态包、决策队列和台账回答他，把决策和里程碑讲给他。"
        common.update({
            "description_json": yaml_str(prefix + desc),
            "seq_intro": s["intro"], "handoff_rules": spec.handoff_rules(), "signoff": spec.signoff_items(),
            "routing_table": spec.routing_table(),
            "writes": describe_writes(pol),
        })
        common.update(entry_values(spec, policy))
        return render(scaffold("agents/entry.md.tmpl"), common)
    if rank == 1:
        me, notice, reg, receipt = pol["fixed_session"], spec.term("msg", "notice"), spec.term("msg", "register_request"), spec.term("msg", "register_receipt")
        router_scribe = "**交给书记员。** 要登记、确认、回滚或归档时，给 `%s` 发【%s】，等它回【%s】。格式：\n\n%s" % (scribe, reg, receipt, REGISTER_FORM)
        router_report = ("**给入口的消息。** 只有三种：里程碑，要研究员定的事（【%s #ID】），你判断不了归属的话。正文长的写进 `packages/drafts/pmo/`，消息里只给路径和三行摘要。"
                         "你开的会话（书记员）每结束一轮，应用会叫醒你一次，这不算交付：没有要处理的消息，就一句话结束本轮。" % spec.term("msg", "decide"))
        router_status = ("**状态包由你维护。** 研究任务主文档末尾的“状态包”记这条任务现在的状态：上游卡在哪条决策、哪些实验条件已经定死、哪些工作暂停了；各会话压缩上下文以后先读它，入口用它回答研究员。"
                         "工作项状态变化、研究员定下新决策、暂停或恢复某类工作时，你改写它：先 `ledger.py status <研究任务 ID>` 读现在的，把整段（简介、当前状态、近期变化、风险）改好存成 `packages/drafts/pmo/` 下的文件，"
                         "运行 `python3.12 -I .claude/solo-lab/bin/docs.py status-block <研究任务 ID> --file <文件> --apply` 整段替换。")
        # 1.0.12: the router is where a request to another sequence starts, and it keeps the record of what the
        # two +2 then settle between themselves. 1.1.0: the +2 uploads the package itself before it hands it in.
        router_cross = (
            "**跨序列需求的委托。** 各序列的 +2 把跨序列的需求包自己上传，再把草稿路径用【%s】发给你。先跑 `python3.12 -I .claude/solo-lab/bin/route.py classify --draft <包路径>`，草稿没通过检查的退回作者。"
            "再对照台账查两件事：和进行中的工作有没有重复（`ledger.py query cross`、`ledger.py query open --sequence <承接序列>`）；"
            "有没有碰到所属研究任务签字后冻结的字段，或者待定决策“冻结的动作”（`ledger.py status <研究任务 ID>`、`ledger.py query queue`）。"
            "没有问题：给 `%s` 发【%s】（草稿路径，动作“新建工作项”），拿到【%s #ID】后回给发起方 `%s：已登记，可以委托`，由它直接委托承接方 +2。"
            "承接方是项目管理 +2 的（比如外联要新的台账查询），登记后由你转给入口，它的答复也经你转回。"
            "有问题：不登记，把是哪一条回给发起方；要研究员定的，请发起方写决策包。你不改写包的内容，不替序列判断做不做，也不委托承接方。\n\n"
            "**协调记录。** 跨序列需求登记以后，两个 +2 直接往来，不经你和入口。书记员在这类工作项上每写一次（接单、交付、验收、回滚、归档），会把【%s】另发一份给你。"
            "收到后跑 `python3.12 -I .claude/solo-lab/bin/route.py cross <工作项 ID>` 看这条需求的往来（日志只记每条消息的首行，内容读首行写的包），"
            "更新 `%s`：每条进行中的跨序列需求一行，写谁在等谁、卡在哪一步、前置条件齐没齐、最近一次往来。"
            "有下面三种情况，告诉入口，写明是哪条需求、哪一句：往来里出现改范围、改成功标准、改冻结项的内容；两个序列的说法对不上；"
            "过了截止时间没有动静（期限看 `ledger.py query cross`，`route.py cross` 会标出 48 小时没有往来的）。都没有就一句话结束本轮，不给入口发消息。") % (
            notice, scribe, reg, receipt,
            msg_head(spec, "notice", item="<工作项 ID>", sender=me, receiver="<发起方 +2 的会话名>"), receipt, CROSS_RECORD)
        common.update({"description_json": yaml_str(prefix + "项目管理 +1（路由）。消息的枢纽：把研究员的话对着台账分给会话，收各序列的进展、交付和要拍板的事，维护研究任务的状态包和跨序列协调记录，只把里程碑和要研究员定的事交给入口；给跨序列需求查重、安排登记。"),
                       "routing_table": spec.routing_table(), "router_scribe": router_scribe, "router_report": router_report,
                       "router_cross": router_cross, "router_status": router_status, "after_compaction": after_compaction("入口", entry=True),
                       "notice": notice, "reg": reg, "receipt": receipt, "decide": spec.term("msg", "decide"), "assign": spec.term("msg", "assign"),
                       "entry": entry, "resume_head": msg_head(spec, "resume", sender=me, receiver="<负责会话的会话名>"),
                       "to_entry_head": msg_head(spec, "notice", sender=me, receiver=entry),
                       "decide_head": msg_head(spec, "decide", sender=me, receiver=entry)})
        return render(scaffold("agents/router.md.tmpl"), common)
    intake = "\n\n会话池里各会话直接给你发【%s】。一次处理一条；你在忙时，后来的消息会自动排队。写完给请求方回 `%s：<写了什么>`，下面列出异常。写台账前先不加 `--apply` 预览，确认后再加；脚本自己会加文件锁，同一时间只有一个写入。" % (
        spec.term("msg", "register_request"), msg_head(spec, "register_receipt", sender=scribe, receiver="<请求方会话名>"))
    common.update({"description_json": yaml_str(prefix + "项目管理 +0（书记员）。飞书项目唯一的写入方：按登记请求建工作项、写字段和评论、确认和回滚节点、归档；文档由各会话自己上传，书记员不上传、不修订、不删。"),
                   "docs_tree": spec.docs_tree(), "scribe_intake": intake, "after_compaction": after_compaction("路由")})
    return render(scaffold("agents/scribe.md.tmpl"), common)


def task_summary(task):
    lines = ["- 名称：%s（%s）" % (task["name"], task["description"]),
             "- 研究问题、目标和判据：研究任务单（飞书项目里“研究任务”工作项的主文档）和 `spec/requirements.md`。",
             "- 实验规格：`spec/spec.md`，写可改范围、目标指标、实验资源和搜索方法。可写路径：%s，只有工程序列能写。" % (
                 code_list(task["editable_paths"])),
             "- 评测协议：`spec/eval-protocol.md`，写数据、划分、打分和证据标准。只读路径：%s。" % code_list(task["protected_paths"])]
    return "\n".join(lines)


def space_names(task, ids):
    """(project space, knowledge space) by the names Feishu gave when they were bound, else as the
    task file wrote them: the task file may name the project space by simple_name or project_key."""
    fs = task.get("feishu") or {}
    return ((ids.get("meegle") or {}).get("space_name") or fs.get("meegle_space") or "未填",
            (ids.get("wiki") or {}).get("space_name") or fs.get("wiki_space") or task["name"])


def where_text(task, ids):
    m = ids.get("meegle") or {}
    w = ids.get("wiki") or {}
    meegle, wiki = space_names(task, ids)
    state = ("- 会话池的认领表和各会话的状态在 `.claude/state/`，由 hook 维护，不要手改；委托、交付和认领的记录在 `.claude/logs/dispatch.jsonl`；"
             "开池清单在 `.claude/solo-lab/pool-plan.json`；飞书写操作的日志在 `feishu/write-log.jsonl`；收件箱在 `inbox/`。")
    return "\n".join([
        "- 台账：飞书项目空间「%s」，project_key %s；空间的结构（类型、流程、字段）在 `feishu/space.md`。" % (
            meegle, "`%s`" % m["project_key"] if m.get("project_key") else "待填（跑 `space.py feishu`）"),
        "- 任务包：飞书知识库空间「%s」%s；发出前的草稿在 `packages/drafts/<序列>/`。" % (
            wiki, "，space_id `%s`" % w["space_id"] if w.get("space_id") else ""),
        "- 实验规格、评测协议和需求文档在 `spec/`，运行记录在 `runs/`，研究任务的流程（每一步谁负责、必填什么）在 `org/phases.md`。",
        state,
    ])


HARD_RULES = ("hook 只拦两类事：会损坏数据的，和研究员多半不在时向他提的问题。在任何会话里都直接拒绝：用编辑工具改 `.claude/` 下的 hook、配置和角色定义，"
              "用编辑工具改评测协议的只读路径（质量序列的会话和研究员自己的会话例外：改动弹窗要研究员确认，bypass 和 dontAsk 模式下也拒绝），"
              "high-risk-write 和带 `--yes` 的飞书命令，派发时带 `name`，用 Agent 工具派书记员，不照规定写法的认领、预登记和释放，第二条入口或书记员认领。"
              "在研究员自己的会话里弹窗询问、在别的会话里拒绝：书记员以外的会话写工作项（`workitem.py` 和 `meegle` 的写命令），"
              "任何会话直接敲 `lark-cli` 的文档写命令（写文档只经 `docs.py`，它每个认领的会话都能运行，脚本自己核对是不是作者），"
              "在共享工作区里切分支、stash、reset 或 pull，释放认领。"
              "不可逆事项清单（`org/org.md`）里的命令、删改文件的 shell 命令，hook 都不拦，权限照 Claude Code 自己的检查。规矩不变，靠规则、每轮提示和包检查："
              "清单里的事要先有研究员批准的决策，待定决策“冻结的动作”谁都不做，评测协议的只读路径和 `.claude/` 下的文件用 shell 命令也不许改。"
              "池会话被其他 agent 会话叫醒的轮次：拒绝 AskUserQuestion，去掉收尾工具里的提问（ask、actions、blocked），"
              "只有路由把一条插队的决策转告入口的那一轮放行一次。"
              "别的派发规则是规范：违反时 hook 提醒、记日志，不拦截。")


def shared_mechanics(spec, policy):
    agents = policy["agents"]
    entry, router, scribe = (agents[r]["fixed_session"] for r in ("pmo-p2", "pmo-router", "pmo-scribe"))
    words = "、".join("【%s】" % spec.term("msg", k) for k in ("report", "register_request", "register_receipt", "decide", "resume", "notice"))
    return bullets([
        "本目录是会话池：每个角色是研究员看得见的一个桌面会话。侧栏顶层是 %d 个序列的 +2，+2 下面是本序列的 +0：谁开出的会话就嵌在谁下面；入口 `%s` 下面是路由 `%s`，路由下面是书记员 `%s`。研究员平时找入口，特殊情况直接点开某个会话交代。" % (
            len(policy.get("sequences") or []), entry, router, scribe),
        "认领：只有开局第一句写着“认领 <角色>（会话名 <名字>）”的会话才认领，照那句话运行 `python3.12 -I .claude/solo-lab/bin/pool.py claim <角色> --as <会话名>`，读输出里的角色说明，它就是本会话的工作说明。第一句没有这句话的，是研究员自己的会话：不要自己认领，也不替池里的角色干活。",
        "找人：委托前先跑 `python3.12 -I .claude/solo-lab/bin/pool.py show --role <角色>`，看各会话忙不忙、手上是哪个工作项、桌面会话 ID，照脚本的建议发给空闲的会话。没有空闲的就排队，台账里“待开工”的工作项就是队列。",
        "发消息：委托、交付、登记请求、需拍板都用会话管理工具发消息，按对方的桌面会话 ID 发，同一家族里也可以写短名（会话名的小写）。首行写抬头 `%s：<包名> <包路径>`，抬头里的会话名写池位名（`pool.py show` 列的那个，+0 也写 `Alg-p0-01` 这样的池位名），其余几种只换抬头里的词：%s。正文放在包文件里，消息里只写抬头、包路径和三行以内的摘要。回执是 delivered 或 queued 才算发出，记下 message_id；对方收没收到，以对方 hook 记在 `.claude/logs/dispatch.jsonl` 的时间为准。" % (
            msg_head(spec, "assign"), words),
        "自动提醒：你开的会话每结束一轮，应用会叫醒你一次。这不算交付：先跑 `pool.py show --queue <自己的会话名>`，有待开工的工作项、又有空闲会话就委托出去，没有就一句话结束本轮。【%s】（比如补充信息）也是消息，会叫起对方的下一轮；对方在 `pool.py show` 里显示“研究员在用”的不打断，改用 `notify.py send <会话名> --text '...'` 留在它的收件箱。" % spec.term("msg", "notice"),
        "开会话：+0 由本序列的 +2 开，侧栏里新会话嵌在开它的会话下面。开局那句话点了名要你开的，在认领那一轮照认领输出开，这是研究员初始化时定下的开法；"
        "此后研究员在你的会话里明确要求（“补位”“再开一个 alg-p0”）时照表开。人手不够时运行 `pool.py refill <角色>` 记一条补位请求，"
        "开这个角色的会话在同一轮提一张启动卡；研究员在那个会话里点一下才开，不会自动开。",
        "工作项、字段、节点和评论只由唯一认领的书记员 `%s` 写（`workitem.py`）；文档谁负责谁写，各会话用 `docs.py` 上传和修订自己的文档。"
        "派给 +0 的封闭任务（执行关系）不建工作项，按“上级工作项 ID-序号”编号（比如 2101-3），在消息抬头里和工作项 ID 一样用。每一步谁负责、必填什么，以飞书空间为准：`ledger.py next <工作项 ID>` 现读。" % scribe,
        "入口 `%s` 只和研究员、路由往来：研究员的话由入口照录交给路由 `%s`，路由转给该知道的会话；进展、交付和【%s】发给路由，不发入口。" % (entry, router, spec.term("msg", "decide")),
        "high-risk-write 和带 `--yes` 的飞书命令、在本目录切分支、stash、reset 之类的 git 命令都由研究员执行：把完整命令写给研究员。",
        "知识库、文档和云空间的命令显式带 `--as user`；收发消息和订阅事件带 `--as bot`。",
        HARD_RULES,
    ])


def pool_sections(spec, policy):
    entry = policy["agents"]["pmo-p2"]["fixed_session"]
    router = policy["agents"]["pmo-router"]["fixed_session"]
    decide = spec.term("msg", "decide")
    return "\n".join([
        "", "## 本目录怎样落实全局约定", "",
        bullets([
            "消息首行的抬头就是自报会话名。",
            "授权链看登记回执（`packages/sent/<工作项 ID>.json`）和派发图（`org/org.md` 的“派发图”）：对得上就照做；对不上，告诉路由 `%s`。" % router,
            "送达以收方 hook 的记录为准，不回 ACK，也不等 ACK。",
            "本项目的会话池就是研究员认可的派发方式：委托消息发给池里的会话，不改用后台子智能体。",
            "改了共享文件不群发通知，别人看台账里的“负责会话”。",
            "被其他 agent 会话叫醒的轮次，研究员多半不在，不向研究员提问。这类轮次有三种：其他会话发来的消息、本会话开出的会话结束一轮的回报、开会话时上级写的首轮。"
            "这时面向研究员最多写一行状态，不标“等研究员回答”，不复述已经登记过的决策；要研究员拍板的事，+2 写成决策包，登记后给路由发【%s】，+0 写进交付包。"
            "hook 会去掉收尾里的提问、拒绝 AskUserQuestion；唯一的例外是路由把一条插队的决策转告入口的那一轮，入口可以在收尾里问研究员一次。" % decide,
            "只要研究员动一下手、没有选项要他挑的事（恢复集群连接、下载数据、连 VPN、跑一条只有他能跑的命令这类），不写决策包：运行 `todo.py add --text '<要他做什么>' --for <工作项 ID> --done-when '<做完怎么算数>'` 记进研究员任务清单，入口向研究员列出；他做完了，或者你自己核对到了结果，运行 `todo.py done <编号>`。建错的、不做了的工作项归档不了，也不去推它的节点：运行 `todo.py add --for <工作项 ID> --terminate --text '<为什么不要它了>'`，由研究员在网页上终止。有两个以上的选项要他挑，或者做了以后不好撤回的，是决策，照旧写决策包。"
            "有选项要研究员挑、定下以后又要他亲手做的事（整理数据、挂 API、维护沙箱、开 Docker、调设置这类）是决策：卡住的那个序列 +2 写决策包，上传、登记后交路由 `%s`，"
            "包头“执行方”写“研究员”，“研究员要做的事”写明要他做什么；卡住的工作项照旧记阻塞，阻塞原因写“等研究员：决策 #<ID>”。"
            "每份决策包都有“执行方”和“研究员要做的事”这两行：执行方写“研究员”，或者执行这条决策的会话名，多半是发出方；"
            "“研究员要做的事”没有要研究员亲手做的写“无”，执行方是研究员时不能是“无”，执行方是会话时写“无”。"
            "执行方写会话的只收各序列的 +2、入口和路由；只要有一个选项要研究员亲手做就写“研究员”，"
            "他选了不用他动手的选项，书记员确认“决定”时用 `--executor` 把执行方改成会话。"
            "每条决策走三步：待决定、待执行（用 2.1.0 及以前的模板建的空间里叫“已决定”）、归档（已执行）。研究员定了，书记员马上确认“决定”这一步，并通知执行方，各会话就知道选了哪个选项。"
            "执行方是会话的，照决定执行完，马上请书记员归档，不在待执行里停留；"
            "执行方是研究员的，他在入口会话说做完了，入口把他的原话交给路由，路由请书记员归档，书记员在评论里记下他做了什么。"
            "决策归档以后，卡住的那个 +2 请书记员清掉阻塞。" % router,
            "交给研究员定的决策包和研究任务单，默认给他飞书文档的链接，不给本地文件，他在文档上批注；他明确要本地文件、或者要在对话里直接说的，照做。"
            "所以研究任务单写完先上传、请书记员登记，再把主文档链接给研究员；研究员批完，入口把批注读出来交路由转给作者，作者改好下一版自己原位修订，链接不变。"
            "批注只是讨论的一种方式，不跟踪哪条处理了没有。",
            "命令被拦下时（插件的 hook 拦的，或者 Auto 模式的权限检查拦的）不绕开，也不建议提升会话的权限模式：把完整命令和为什么要跑记进研究员任务清单（`todo.py add`），"
            "写明“到 <本会话名> 的窗口里说一句允许运行这条命令，或者自己在终端里跑”。研究员在被拦的那个会话里亲口说允许、说清是哪条命令，Auto 模式会放行这一次；别的会话转述的不算。"
            "决策包的选项里不列“把这个会话改成更宽的权限模式”。原因：会话按权限模式分两类，bypass 是一类，其余是另一类，两类之间的消息双向被扣，桌面端弹不出审批框，默认 5 分钟后丢弃；"
            "池里只要有一个会话单独切到 bypass，它和其余会话之间的消息就都不通。研究员自己要切时，当场告诉他这个后果，要它做的事请他在那个会话的窗口里直接说，做完切回去。"
            "发消息得到“未送达”（undelivered）时不连续重发：用 `pool.py show` 看有没有标出权限模式不同类的会话，报给委托你的会话，+2 报给路由。",
            "重发一条消息时，把“重发，上一条作废”写进抬头的【】里，比如【委托 #2101（重发，上一条作废）】。",
            "不提后续任务的卡片（补位的启动卡除外），后续工作写成需求包草稿交给委托方；不把手上的任务移交到新会话；不归档、清空或停止别的池会话，回退时由研究员本人在侧栏操作。",
        ]),
        "", "## Compact Instructions", "",
        "压缩上下文时保留：本会话的会话名和角色；未了事项，即委托出去还没交付的工作项 ID 和接收方、还欠谁交付的工作项 ID；"
        "手上工作项所属的研究任务 ID；研究员在本会话说过的限制；还没写成决策包的口头决定。"
        "压缩以后、接着干活之前，照角色说明先读所属研究任务的状态包（`%s`）。" % STATUS_CMD,
        "",
    ])


RUNTIME_INTRO = "本目录由插件 solo-lab 初始化，按会话池运行：每个角色是研究员看得见的一个桌面会话，开局时认领角色，角色说明由 hook 注入（全文在 `.claude/agents/`）。研究员平时找入口 `PMO-p2-01`（项目管理 +2）。所有会话都会读到本文件，所以这里只放大家都要知道的内容。"


def claude_md(spec, task, ids, policy=None):
    values = {
        "project": task["name"], "task_summary": task_summary(task), "rank_table": spec.rank_table(),
        "general_rules": bullets(spec.general_rules_heads()), "handoff_rules": spec.handoff_rules(),
        "signoff": spec.signoff_items(), "naming": bullets(naming_lines(spec)), "where": where_text(task, ids),
        "mechanics": shared_mechanics(spec, policy), "runtime_intro": RUNTIME_INTRO,
        "pool_sections": pool_sections(spec, policy),
    }
    return render(scaffold("CLAUDE.md.tmpl"), values)


def irreversible_md(spec, task=None):
    """org/org.md's section on irreversible actions: the criteria, the list, the rule that goes with it (no hook
    checks it), and the paths this project protects."""
    lines = ["## 不可逆事项", "", spec.irreversible_section(), ""]
    if task is not None:
        lines += ["本项目登记的受保护路径（任务文件 `spec/task.json` 的 protected_paths）：%s。" % code_list(task["protected_paths"]), ""]
    return lines


def org_md(spec, seqs, task=None):
    parts = ["# 组织规则", "",
             "> 由插件 solo-lab 的规则文件（`.claude/solo-lab/orgspec/rules.json`）生成，不要在这里改。组织规则的变更走决策包，研究员签字后改规则文件，再重跑初始化。研究员的补充决定在 `org/decisions.md`。", "",
             "## 职级", "", spec.rank_table(), ""]
    for head, body in spec.general_rules():
        parts += ["**%s**%s" % (head, body), ""]
    parts += ["## 只能由人来做的事", "", spec.signoff_items(), ""] + irreversible_md(spec, task)
    parts += ["## 序列", "", spec.sequence_overview(), ""]
    for sid in seqs:
        s = spec.sequence(sid)
        parts += ["### %s" % s["heading"], "", s["intro"], "",
                  orgspec.md_table(["职级", "已给定", "产出", "何时上报"],
                                   [[r["label"], r["given"], "<br>".join(r["outputs"]), r["escalate"]]
                                    for r in (s["ranks"][k] for k in spec.ranks_of(sid))]), ""]
        if s["notes"]:
            parts += [s["notes"], ""]
    parts += ["## 任务包", "", spec.package_table(), "", "### 三种关系", "", spec.relation_matrix(), "",
              "### 包的流向", "", spec.package_routes(), "", "### 交接规则", "", spec.handoff_rules(), "",
              "## 入口和路由", "", spec.routing_table(), "", "### 派发图", "", spec.dispatch_table(), "",
              "## 研究任务怎么推进", "", spec.task_rules(), "",
              "## 台账：飞书项目", "", spec.work_items_table(), "", "### 流程和字段", "", spec.rules["feishu"]["structure_note"], "",
              "这个项目的空间结构在 `feishu/space.md`，研究任务两个流程的对照在 `org/phases.md`。", "",
              "## 任务包文档：飞书文档", "", spec.docs_tree(), ""]
    return "\n".join(parts)


def initial_pool(task, policy, name):
    """How many sessions of this role the pool opens at first."""
    pol = policy["agents"][name]
    cfg = task["pool"]
    if pol["rank"] == 2 or pol.get("fixed_session"):
        return 1
    if pol["sequence"] in cfg.get("later", []):
        return 0
    return cfg["p%d" % pol["rank"]]


def grants_md(spec, task, policy):
    rows = []
    for name, pol in policy["agents"].items():
        s = spec.sequence(pol["sequence"])
        row = [name, s["cn"], "+%d" % pol["rank"],
               task["models"].get(name) or task["models"].get("p%d" % pol["rank"]) or "inherit", pol["limit"]]
        row.append(initial_pool(task, policy, name))
        rows.append(row + [s["ranks"][pol["rank"]]["given"], "初始授权，待评测"])
    head = spec.general_rules_heads()[2]
    cols = ["配置", "序列", "职级", "模型", "上限", "初始池", "授权的任务（已给定）", "状态"]
    note = "上限在会话池里是这个角色最多开几个会话（入口和书记员各只能有一条认领），初始池是开池时开几个。"
    return "\n".join(["# 授权表：配置 × 任务类型", "",
                      "> 组织规则：**%s**一份配置就是 `.claude/agents/` 里的一份定义，包括模型、提示词和工具；授权的任务就是这一级“已给定”的那类包。按评测结果重新核定时，改这张表和对应的 agent 定义，走组织规则变更，由研究员签字。%s" % (head, note), "",
                      orgspec.md_table(cols, rows), ""])


def plugin_scripts():
    """{path: sha256} of the plugin scripts the researcher's session runs where the plugin is installed."""
    return {os.path.join(HERE, name): sha256(read_text(os.path.join(HERE, name))) for name in PLUGIN_RUN}


def pool_label(task, ids=None):
    """The prefix of every session title and the name of the sidebar section: pool.label,
    else the name of the Feishu project space."""
    m = (ids or {}).get("meegle") or {}
    label = (task["pool"].get("label") or m.get("space_name") or (task.get("feishu") or {}).get("meegle_space")
             or task["name"])
    if len(label) > 40 or re.search(r"[\[\]\n]", label):
        raise SetupError("会话标题的前缀取自飞书项目空间的名字“%s”：它超过 40 个字，或者带方括号。在任务文件的 pool.label 里另写一个前缀" % label)
    return label


BRIEF_END = "，回一句“已认领 %s”就结束本轮，等委托消息。"      # how an opening line ends when nothing else is asked
# The desktop app runs at most this many sessions at once that sessions started on their own: a +0
# opened by its +2, the scribe opened by the router. It counts them across everything one initialising session set
# off, also after a +2 is moved to top level. One is kept for the scribe, whose first turn is long.
AUTO_RUNNING = 5
OPEN_BUDGET = AUTO_RUNNING - 1
# ...and after each message from the user it starts at most this many of them. A round is what the
# initialising session opens after one message; two are kept spare.
MESSAGE_STARTS = 10
ROUND_BUDGET = MESSAGE_STARTS - 2
RESUME_WORDS = "继续开池"                  # what the researcher is asked to say between two rounds
PLAN_VERSION = 6
# what the researcher pastes into a +2 opened by hand ends with this: that +2 titles and files itself
BY_HAND = "本会话由研究员手工开出。"
# ...and when the session opens the next level in its first turn
OPENS = "；再照输出里的表开出 %s，每开一个马上运行表里的 expect；然后回一句“已认领 %s，已开 …”，结束本轮。"


def pool_entry(spec, task, policy, role, name, label):
    pol = policy["agents"][role]
    route = "python3.12 -I .claude/solo-lab/bin/pool.py"
    claim = "认领 %s（会话名 %s）。先运行 %s claim %s --as %s，读输出里的角色说明" % (role, name, route, role, name)
    return {
        "role": role, "name": name, "short": "{short}" if name == "{name}" else name.lower(),
        "title": "[%s] %s" % (label, name),
        "model": task["models"].get(role) or task["models"].get("p%d" % pol["rank"]) or "inherit",
        "claim": claim, "brief": claim + BRIEF_END % name,
        "background": "本目录是「%s」的智能体组织，按会话池运行：每个角色一个桌面会话，委托、交付、登记都发消息，抬头格式见项目的 CLAUDE.md。"
                      "你是 %s（%s +%d），开局先认领角色，认领输出里的角色说明就是你的工作说明；工作项和状态包在飞书，不靠对话记状态。" % (
                          task["name"], name, pol["sequence_cn"], pol["rank"]),
        "expect": "%s expect %s --role %s --desk <开会话时返回的桌面会话 ID>" % (route, name, role),
        "children": [],
    }


SCRIBE_FIRST = (
    "。认领以后接着做初始化的两件写入，做完再回话。命令都在项目目录里运行，前缀是 python3.12 -I .claude/solo-lab/bin/；"
    "onboarding.py 只管计时，它报错就跳过，不影响写入："
    "1. onboarding.py start nodes；docs.py ensure-node all --apply；onboarding.py end nodes。"
    "2. ledger.py query tasks，空间里已经有研究任务就跳过这一件；没有的话 onboarding.py start first_task；"
    "docs.py upload %s --apply；workitem.py register %s --apply；照登记输出的工作项 ID 运行 docs.py relink <工作项 ID> --apply；"
    "onboarding.py end first_task。"
    "回话写明：建了几个节点、沿用了几个，研究任务的工作项 ID 和主文档链接；哪一条命令失败，原样贴出报错，不要重试两次以上。" % (FIRST_TASK, FIRST_TASK))


MODEL_TIERS = ("haiku", "sonnet", "opus", "fable")


def model_tier(model):
    """Where a model sits among the families; -1 when it cannot be told (inherit, an unknown id)."""
    text = str(model or "").lower()
    return next((i for i, word in enumerate(MODEL_TIERS) if word in text), -1)


def plan_entries(plan):
    """Every session of a pool plan, a parent before the sessions it opens."""
    def walk(entry):
        yield entry
        for child in entry.get("children") or []:
            yield from walk(child)
    for top in (plan or {}).get("top") or []:
        yield from walk(top)


def pool_plan(spec, task, policy, seqs, ids=None):
    """.claude/solo-lab/pool-plan.json: the pool as a tree, the way work is handed down. The
    initialising session opens each sequence's +2; a +2 opens its +0s in its first turn (the entry
    the router, and the router the scribe), so the sidebar nests them: a session sits under the one that opened
    it. When that session cannot open any, the researcher opens the +2s by hand (`opening`)."""
    cfg = task["pool"]
    later = set(cfg.get("later") or [])
    label = pool_label(task, ids)
    tops = []
    for seq in sorted(seqs, key=lambda x: x != "pmo"):     # the entry first: the scribe is two levels below it
        role2 = agent_name(seq, 2)
        pol2 = policy["agents"][role2]
        top = pool_entry(spec, task, policy, role2, pol2.get("fixed_session") or "%s-p2-01" % pol2["abbrev"], label)
        below = [r for r in spec.ranks_of(seq) if r < 2]      # [0]; [1, 0] for the PMO: the router and the scribe
        ranks = {1: [], 0: []}
        if seq not in later:
            for rank in below:
                role = agent_name(seq, rank)
                pol = policy["agents"][role]
                for i in range(1, (1 if pol.get("fixed_session") else cfg["p%d" % rank]) + 1):
                    ranks[rank].append(pool_entry(spec, task, policy, role,
                                                  pol.get("fixed_session") or "%s-p%d-%02d" % (pol["abbrev"], rank, i), label))
        top["children"] = ranks[1] or ranks[0]              # the +0s hang under their +2; the scribe under the router
        for i, zero in enumerate(ranks[0] if ranks[1] else []):
            ranks[1][i % len(ranks[1])]["children"].append(zero)
        for entry in [top] + ranks[1]:
            kids = [c["name"] for c in entry["children"]]
            if kids:
                entry["brief"] = entry["claim"] + OPENS % ("、".join(kids), entry["name"])
        kids = [c["name"] for c in top["children"]]
        if not kids:
            top["brief"] += "本序列的 +0 开池时先不开，研究员说“补位”时再照认领输出开。"
        # templates for a refill: a +2 opens another +0
        top["child_roles"] = [pool_entry(spec, task, policy, agent_name(seq, rank), "{name}", label) for rank in below]
        top["opening"] = ("认领 %s（会话名 %s），然后照认领输出开出 %s。" % (role2, top["name"], "、".join(kids)) if kids
                          else "认领 %s（会话名 %s）。本序列先不开下一级。" % (role2, top["name"])) + BY_HAND
        top["rebuild"] = "认领 %s（会话名 %s），不开下一级；用 pool.py show 找回本序列已有的会话，按桌面会话 ID 给它们发消息。%s" % (
            role2, top["name"], BY_HAND)
        tops.append(top)
    below = {t["name"]: sum(1 for _ in plan_entries({"top": t["children"]})) for t in tops}
    batches, load = [[]], 0                                # +2s opened together: what they open below stays within the budget
    for top in tops:
        if below[top["name"]] and batches[-1] and load + below[top["name"]] > OPEN_BUDGET:   # a +2 that opens nothing fits anywhere
            batches.append([])
            load = 0
        batches[-1].append(top["name"])
        load += below[top["name"]]
    rounds, load = [[]], 0                                 # batches opened after one message of the researcher
    for number, names in enumerate(batches, 1):
        weight = sum(below[n] for n in names)
        if weight and rounds[-1] and load + weight > ROUND_BUDGET:
            rounds.append([])
            load = 0
        rounds[-1].append(number)
        load += weight
    plan = {"version": PLAN_VERSION, "project": task["name"], "label": label, "max_sessions": cfg["max_sessions"],
            "order": [t["name"] for t in tops], "batches": batches, "rounds": rounds,
            "scribe_first": SCRIBE_FIRST, "opens": OPENS, "top": tops}
    plan["total"] = sum(1 for _ in plan_entries(plan))
    if plan["total"] > cfg["max_sessions"]:
        raise SetupError("开池要开 %d 个会话，超过任务文件的 pool.max_sessions=%d：调小 pool.p0，把不急的序列写进 pool.later，"
                         "或调大 max_sessions" % (plan["total"], cfg["max_sessions"]))
    return plan


def first_task_sheet(task):
    """The smallest research-task sheet: the project's name and its one line. The scribe
    registers it while the pool opens; the entry's interview replaces it afterwards."""
    return "\n".join([
        "# 研究任务单：%s" % task["name"], "",
        "- 流程：正式实验", "- 发出方：研究员", "- 关联工作项：待建", "",
        "## 目标", task["description"], "",
        "## 成功标准", "待定：立项访谈以后写。", "",
        "## 范围", "- 包含：待定", "- 不做：待定", "- 停止条件：待定", "",
        "## 资源上限", "- 总预算初值：待定", "- 期限：待定", "- 超出时：研究员决定", "- 执行环境：待研究员确认",
        "- 计算资源：待研究员确认", "",
        "## 已知的上下文", "待定", "",
        "## 风险和未决事项",
        "这份研究任务单是初始化时照项目名和一句话描述生成的。入口用技能 `solo-lab:charter` 访谈研究员，写出修订稿，"
        "再请书记员运行 `docs.py revise <这条研究任务的工作项 ID> <修订稿> --note '<改了什么>'` 原位换掉主文档开头；工作项和文档链接都不变。", ""])


def registered_tasks(target):
    """Receipts of registered research-task sheets: [(receipt, path of the draft)]."""
    sent = os.path.join(target, "packages", "sent")
    out = []
    for name in sorted(os.listdir(sent)) if os.path.isdir(sent) else []:
        rec = load_json_file(os.path.join(sent, name)) if name.endswith(".json") and not name.startswith("reply-") else None
        if isinstance(rec, dict) and rec.get("status") == "registered" and rec.get("draft"):
            draft = os.path.join(target, rec["draft"])
            if os.path.isfile(draft) and ledger_core.sheet_kind(draft) == "research_task":
                out.append((rec, draft))
    return sorted(out, key=lambda x: str(x[0].get("updated_at") or ""), reverse=True)   # a revised sheet before the one it replaced


def phases_md(spec, snap=None, binding=None):
    """org/phases.md: the flows of the main-line type as the space has them. Before the space
    is bound there is nothing to show; `space.py feishu --apply` writes it again from the snapshot."""
    main = (((binding or {}).get("types") or {}).get(orgspec.Contract(spec).main_line()) or {}).get("type_key")
    flows = space.flows_md(snap, types=[main]) if snap and main else ""
    return render(scaffold("org/phases.md.tmpl"), {
        "flows": flows or "还没读到空间里的流程：绑定飞书空间后，`space.py feishu --live --apply` 会把两个流程的节点、负责角色和必填项写在这里。",
        "rules": spec.task_rules()})


SEED_PLACEHOLDERS = {
    "x": "<能改哪些模块、配置、超参或提示词；即使在可写路径里也不能碰的文件；一个候选是什么，比如一个 git 快照；从哪个版本起步，它的分数是多少；能用哪些依赖和外部服务>",
    "f": "<指标名称和方向，在哪个划分上算；多个种子怎么合成一个分数；提高多少才算数，用什么检验或置信区间判断；哪些护栏指标不能变差；优化时看哪个分数，最终报告用哪个>",
    "g": "<用什么计成本：GPU 小时、API 费用、token、墙钟时间，评测本身的花费也算；单个候选的上限和总预算；谁计量、记在哪；超了谁叫停>",
    "h": "<一句话说清：用什么数据、怎么打分、优化过程中谁能看到什么>",
    "search": "<外层怎样生成候选、怎样挑下一个要扩展的候选、什么时候停>",
}


def seeded(seeds, key):
    text = seeds.get(key)
    return "%s（取自旧版任务文件）〔推测，待确认〕" % text if text else SEED_PLACEHOLDERS[key]


def spec_files(spec, task, seeds, seqs):
    sign = "需求文档、规格、权限策略……由研究员签字"
    for item in spec.rules.get("only_humans", []):
        if item.startswith("签字"):
            sign = item
    req = "\n\n".join("## %s\n\n无" % h for h in ("要解决的问题和目标用户", "现状工作流和基线", "成功判据（事先写下）",
                                                 "约束", "停止条件", "试点方案"))
    common = {"project": task["name"], "sign_rule": sign, "req_sections": req,
              "spec_drafting": "由%s +2 访谈研究员后起草" % spec.term("seq", "alg") if "alg" in seqs
                               else "这个项目没有启用算法序列，由研究员本人写",
              "eval_drafting": "由%s +2 访谈研究员后起草" % spec.term("seq", "quality"),
              "scope_seed": seeded(seeds, "x"), "metric_seed": seeded(seeds, "f"),
              "resources_seed": seeded(seeds, "g"), "search_seed": seeded(seeds, "search"),
              "eval_seed": seeded(seeds, "h")}
    return {"spec/spec.md": render(scaffold("spec/spec.md.tmpl"), common),
            "spec/requirements.md": render(scaffold("spec/requirements.md.tmpl"), common),
            "spec/eval-protocol.md": render(scaffold("spec/eval-protocol.md.tmpl"), common),
            "spec/task.json": dump_json(task)}


# Headings only the templates before the rename used; such a document is still the old draft.
OLD_SPEC_HEADINGS = {"spec/spec.md": "## x：可以改的代码", "spec/eval-protocol.md": "## h 的定义"}


def deviations_text(spec):
    items = []
    for item in spec.rules.get("plugin_decisions", []):
        if isinstance(item, dict):
            item = item["text"]
        items.append(item)
    return bullets(items) if items else "- 无"


# org/decisions.md is the researcher's file, except the section under this heading: the plugin writes it
# from rules.json plugin_decisions and init keeps it current.
PLUGIN_SECTION = "## 插件定下的几处安排"
# SHA-256 of that section as each version of the plugin wrote it, white space collapsed (squeeze). A
# section that still reads like one of them was not edited: init replaces it in place. When
# plugin_decisions change, add the new hash here (a test says which) so the next version can do the same.
PLUGIN_SECTIONS = {
    "da04954351482219e763b897bc64d8f5c0a652e15c2acc9c22dc9e52d7b30a45": "1.0.0 至 1.0.2",
    "354838a5ab3b366410e5b748072309e6b5626e02560fca8b87fbab63989cc5b7": "1.0.3",
    "dd2d7f72868799a7bf3f86b583dad924d4c1d8525d7e74b92f65c060c27c769a": "1.0.4",
    "d0062ed622919fb97c63ba55db952a0b8a784a1c1d9f10f720becf1e8988f8b0": "1.0.5",
    "1c0702acad9db570f7783b8d0ee3e34bc68a2c6301adc4bec33803e9fe8c06d4": "1.0.6 至 1.0.9",
    "c80f58059cf5cb10a43cc52da992aeb7b420f68618042de373c679496a383233": "1.0.10",
    "27f611c7282725b164819e37100fac25b86d8252a113c36b8a0a7fda75df2cc1": "1.0.11",
    "477a750ca960f939523f71466a9bd67391ecb9ae4f0afeeae5d691676d5fd753": "1.0.12",
    "846370b22c851ecf4b78bca8587e1cd9766ff0bf3f36fc7d6536a1e887e8dedc": "1.1.0",
}


def squeeze(text):
    return " ".join(text.split())


def section_span(text, heading):
    """(start, end) of the section under heading: from its heading line up to the next "## " heading,
    or to the end of text. None when text has no such heading."""
    m = re.search(r"^%s[ \t]*$" % re.escape(heading), text, re.M)
    if not m:
        return None
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return m.start(), m.end() + nxt.start() if nxt else len(text)


def decisions_file(target, fresh):
    """(content, kind) for org/decisions.md; fresh is the file a new project gets. In a file that
    exists only the plugin's section can change: a section that reads like the current rules is left
    alone; one an earlier version wrote and nobody edited (PLUGIN_SECTIONS) is replaced in place,
    every other byte kept (kind update); one the researcher edited stays, and the file with the
    section replaced goes to .new (kind new). A file without the section, or not in UTF-8, is the
    researcher's alone (kind None: kept)."""
    path = os.path.join(target, "org", "decisions.md")
    try:
        current = read_text(path) if os.path.isfile(path) else None
    except (UnicodeDecodeError, OSError):
        current = None
    span = section_span(current, PLUGIN_SECTION) if current is not None else None
    if span is None:
        return fresh, None
    start, end = section_span(fresh, PLUGIN_SECTION)
    old, new = current[span[0]:span[1]], fresh[start:end]
    if squeeze(old) == squeeze(new):
        return current, None
    if span[1] == len(current):                    # the section ends the file
        new = new.rstrip("\n") + "\n"
    content = current[:span[0]] + new + current[span[1]:]
    return content, "update" if sha256(squeeze(old)) in PLUGIN_SECTIONS else "new"


def feishu_readme(task, ids):
    m = ids.get("meegle") or {}
    meegle, wiki = space_names(task, ids)
    line = "飞书项目「%s」（project_key %s）；知识库「%s」。" % (meegle, m.get("project_key") or "待填", wiki)
    return render(scaffold("feishu/README.md.tmpl"), {"space_line": line})


# ---------------------------------------------------------------- plan
class Plan:
    def __init__(self):
        self.files = {}      # rel -> (content, executable, keep_existing)
        self.decided = {}    # rel -> what to do with a kept file that differs, decided from its content
        self.notes = []
        self.manifest = {}


def build_plan(target, task_path, seq_arg):
    spec = orgspec.OrgSpec()
    problems = spec.validate()
    if problems:
        raise SetupError("规则文件有问题，先修好：\n" + "\n".join(problems))
    task, seeds, legacy = load_task_and_seeds(task_path)
    refuse_other_runtime(target)
    seqs = pick_sequences(spec, seq_arg)
    ids = load_ids(target)
    risk, note = resolve_risk(target)
    plan = Plan()
    plan.notes.append(note)
    if not (ids.get("meegle") or {}).get("project_key"):
        plan.notes.append("提醒：项目还没绑定飞书空间。初始化以后跑 `space.py feishu --target <项目目录> --live`，看任务文件里那个空间的匹配结果，核对后加 --apply；绑定以前台账脚本不能用。")
    policy = build_policy(spec, task, seqs)
    f = plan.files

    def put(rel, content, executable=False, keep=False):
        f[rel] = (content, executable, keep)

    settle_later(task, seqs)
    put("CLAUDE.md", claude_md(spec, task, ids, policy))
    put(".claude/settings.json", dump_json(build_settings(task, risk, policy)))
    put(".claude/hooks/policy.json", dump_json(policy))
    put(".claude/hooks/lark-risk.json", dump_json(risk))
    for script in HOOK_SCRIPTS:
        put(".claude/hooks/" + script, scaffold("hooks/" + script), script != "feishu_cmd.py")
    for script in SHARED_HOOK_SCRIPTS:
        put(".claude/hooks/" + script, read_text(os.path.join(HERE, script)))
    for seq in seqs:
        for rank in spec.ranks_of(seq):
            name = agent_name(seq, rank)
            content = pmo_file(spec, rank, task, policy) if seq == "pmo" else agent_file(spec, seq, rank, task, policy)
            put(".claude/agents/%s.md" % name, content)
    for script in BIN_SCRIPTS:
        put(".claude/solo-lab/bin/" + script, read_text(os.path.join(HERE, script)), True)
    for name in ("rules.json", "terms.zh.json", "contract.json", "irreversible.json"):
        put(".claude/solo-lab/orgspec/" + name, read_text(os.path.join(orgspec.ORGSPEC_DIR, name)))
    for name in sorted(os.listdir(TEMPLATES)):
        if name.endswith(".md"):
            put(".claude/solo-lab/templates/" + name, read_text(os.path.join(TEMPLATES, name)))
    put(".claude/solo-lab/pool-plan.json", dump_json(pool_plan(spec, task, policy, seqs, ids)))
    plan.notes.append("init 和绑定都做完以后开池：`pool.py open --target <项目目录>` 打印开池清单。")
    put(FIRST_TASK, first_task_sheet(task), keep=True)
    put("org/org.md", org_md(spec, seqs, task))
    put("org/grants.md", grants_md(spec, task, policy))
    decisions, kind = decisions_file(target, render(scaffold("org/decisions.md.tmpl"), {"deviations": deviations_text(spec)}))
    put("org/decisions.md", decisions, keep=True)
    if kind:
        plan.decided["org/decisions.md"] = kind
    snap, bound = load_json_file(os.path.join(target, "feishu", "space.json")), space.binding_of(ids)
    if snap and bound["types"]:                    # a bound project: the flows as its snapshot has them, the rules as
        put("org/phases.md", phases_md(spec, snap, bound), keep=True)     # they read now, so a reworded rule does not stay
        plan.decided["org/phases.md"] = "regen"
    else:
        put("org/phases.md", phases_md(spec), keep=True)
    for rel, content in spec_files(spec, task, seeds, seqs).items():
        put(rel, content, keep=rel != "spec/task.json")
    if legacy:
        plan.notes.append("提醒：任务文件还是旧写法（%s）。路径已经当作 editable_paths、protected_paths 读进来；旧的一句话说明只在"
                          "第一次生成 spec/ 下的文档时填进对应小节，标成〔推测，待确认〕。换成新写法时，先把 x.paths、h.paths 改写成 "
                          "editable_paths、protected_paths，再删掉这几项；格式见插件的 scaffold/task.example.json。" % "、".join(legacy))
    if not task["editable_paths"]:
        plan.notes.append("提醒：任务文件没有写 editable_paths，工程序列改不了任何代码。")
    if not task["protected_paths"]:
        plan.notes.append("提醒：任务文件没有写 protected_paths，评测脚本和评测集没有只读保护。")
    for key in ("editable_paths", "protected_paths"):
        for p in task[key]:
            if not re.search(r"[*?\[]", p) and os.path.isdir(os.path.join(target, p)):
                plan.notes.append("提醒：%s 里的 %s 是目录，写成 %s/** 才能管到里面的文件。" % (key, p, p))
    for rel, heading in sorted(OLD_SPEC_HEADINGS.items()):
        path = os.path.join(target, rel)
        if not os.path.isfile(path):
            continue
        try:
            old = heading in read_text(path)
        except (UnicodeDecodeError, OSError):
            plan.notes.append("提醒：%s 读不了（不是 UTF-8 编码？），没检查它是不是旧模板；请另存成 UTF-8。" % rel)
            continue
        if old:
            plan.notes.append("提醒：%s 还是改名前的旧模板。新模板按小节写清每一项，用技能 `solo-lab:spec` 和研究员访谈后重写；"
                              "新模板在插件的 scaffold/%s.tmpl。" % (rel, rel))
    put("feishu/README.md", feishu_readme(task, ids))
    put("packages/README.md", scaffold("packages/README.md.tmpl"))
    for seq in seqs:
        put("packages/drafts/%s/.gitkeep" % seq, "")
    put("packages/sent/.gitkeep", "")
    put("inbox/README.md", scaffold("inbox/README.md.tmpl"))
    # runs/README.md asks for the record format to be written into it: seeded once, the project's own from then on
    put("runs/README.md", scaffold("runs/README.md.tmpl"), keep=True)
    plan.manifest = {
        "plugin": "solo-lab", "plugin_version": plugin_version(), "plugin_scripts": plugin_scripts(),
        "sequences": seqs, "entry_role": policy["entry_role"],
        "task_sha256": sha256(dump_json(task)),
        "rules_sha256": sha256(read_text(spec.rules_path)), "terms_sha256": sha256(read_text(spec.terms_path)),
        "contract_sha256": sha256(read_text(os.path.join(orgspec.ORGSPEC_DIR, "contract.json"))),
        "lark_risk": {"lark_cli_version": risk.get("lark_cli_version"), "commands": len(risk.get("commands", {}))},
        "files": {p: sha256(c) for p, (c, _, _) in sorted(f.items())},
    }
    return plan


def previous_hashes(target):
    m = os.path.join(target, ".claude", "solo-lab", "manifest.json")
    try:
        return json.loads(read_text(m)).get("files", {})
    except (OSError, ValueError):
        return {}


def actions_for(target, plan):
    acts = []
    before = previous_hashes(target)
    for rel, (content, executable, keep) in sorted(plan.files.items()):
        path = os.path.join(target, rel)
        if not os.path.lexists(path):
            acts.append(("create", rel, None))
            continue
        try:
            current = read_text(path) if os.path.isfile(path) else None
        except UnicodeDecodeError:
            current = None
        if current == content:
            ok = not executable or os.access(path, os.X_OK)
            acts.append(("same" if ok else "chmod", rel, None))
        elif rel in plan.decided:                      # org/decisions.md: its plugin section decides; org/phases.md: regen
            kind = plan.decided[rel]
            if kind == "new" and os.path.isfile(path + ".new") and read_text(path + ".new") == content:
                kind = "pending"
            acts.append((kind, rel, current))
        elif keep:
            acts.append(("kept", rel, None))
        elif current is not None and before.get(rel) == sha256(current):
            acts.append(("update", rel, current))
        elif os.path.isfile(path + ".new") and read_text(path + ".new") == content:
            acts.append(("pending", rel, current))
        else:
            acts.append(("new", rel, current))
    groups = [HOOK_CODE]
    on_disk = disk_policy_version(target)
    if on_disk is not None and on_disk != POLICY_VERSION:
        groups.append(GUARD_PAIR)
    held = True
    while held:                                        # one file of a group held back holds back the rest
        held = False
        stuck = {rel for kind, rel, _ in acts if kind in ("new", "pending", "paired")}
        for group in groups:
            if stuck.intersection(group):
                for i, (kind, rel, cur) in enumerate(acts):
                    if rel in group and kind == "update":
                        acts[i] = ("paired", rel, cur)
                        held = True
    return acts


# The hook scripts import pool.py and feishu_cmd.py from their own directory: a project runs them all from
# one version, or the inbox hook may fail on every prompt.
HOOK_CODE = tuple(".claude/hooks/" + s for s in HOOK_SCRIPTS + SHARED_HOOK_SCRIPTS)
GUARD_PAIR = (".claude/hooks/guard.py", ".claude/hooks/policy.json")


def pair_problem(target):
    """What a guard.py and a policy.json of different versions do, or None when they go together."""
    gv, pv = disk_guard_version(target), disk_policy_version(target)
    if gv is None or pv is None or gv == pv:
        return None
    if gv < pv:
        return ("guard.py 和 policy.json 不配套：guard.py 第 %s 版比 policy.json 第 %s 版旧。hook 不会拒绝，但旧 guard 在按新规则跑，"
                "部分硬规则已经悄悄变样。在 Claude 以外的终端里合并 .claude/hooks/guard.py.new，再重跑 init" % (gv, pv))
    return ("guard.py 和 policy.json 不配套：guard.py 第 %s 版比 policy.json 第 %s 版新，hook 会全部拒绝。"
            "在 Claude 以外的终端里合并 .claude/hooks/policy.json.new，再重跑 init" % (gv, pv))


POLICY_VERSION = 9


def disk_policy_version(target):
    """The version of the policy.json in the project, None when there is none."""
    data = load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) if target else None
    return (data or {}).get("version") if isinstance(data, dict) else None


def disk_guard_version(target):
    """POLICY_VERSION in the project's guard.py; a guard older than 0.5.0 has none and goes with policy 3."""
    try:
        m = re.search(r"^POLICY_VERSION = (\d+)", read_text(os.path.join(target, ".claude", "hooks", "guard.py")), re.M)
    except (OSError, UnicodeDecodeError):
        return None
    return int(m.group(1)) if m else 3


# ------------------------------------------------------------ precheck
def need_python(version=None):
    """setup.py is how a project gets the plugin's files, so it is where an older interpreter is turned away:
    nothing is written, and the project keeps the files it has, which still run under what it had."""
    found = tuple((version or sys.version_info)[:3])
    if found < MIN_PYTHON:
        raise SetupError("这个版本的插件用 Python %d.%d 以上运行，现在是 %s：把命令开头换成 %s（%s -I <插件目录>/scripts/setup.py …）；"
                         "本机没有的话先装一个，比如 brew install python@3.12" % (
                             MIN_PYTHON + (".".join(str(x) for x in found), PYTHON, PYTHON)))


def feishu_gaps(target, seqs):
    """What the project still lacks on the Feishu side, read from feishu/ids.json: the project
    space binding, then the knowledge space, then the node skeleton."""
    ids = load_json_file(os.path.join(target, "feishu", "ids.json")) or {}
    meegle, wiki = ids.get("meegle") or {}, ids.get("wiki") or {}
    if not (meegle.get("bound_at") and meegle.get("types")):
        return ["飞书项目空间还没绑定：运行 space.py feishu --target <项目目录> --live，核对后加 --apply"]
    if not wiki.get("space_id"):
        return ["还没有知识空间：运行 space.py feishu --target <项目目录> --live --apply，没有同名的知识空间时它会新建一个"]
    nodes = wiki.get("nodes") or {}
    missing = [k for k in orgspec.OrgSpec().node_keys(seqs) if not (nodes.get(k) or {}).get("node_token")]
    if missing:
        return ["知识库节点还缺 %d 个（%s）：书记员用 docs.py ensure-node all 建齐" % (
            len(missing), "、".join(missing[:6]) + ("等" if len(missing) > 6 else ""))]
    return []


def enabled_sequences(target):
    """The sequences init enabled in this project, from its manifest; None (every sequence) when unknown."""
    m = load_json_file(os.path.join(project_paths.runtime_dir(target), "manifest.json")) if target else None
    return (m or {}).get("sequences") or None


def read_space(contract, project, maps, old_binding=None):
    """Snapshot a space, fill the contract's slots and check the flows: (snapshot, binding,
    problems, notes). Reads only."""
    try:
        snap = space.snapshot(project, space.wanted_by(contract, maps, old_binding))
    except space.SpaceError as e:
        raise SetupError(str(e))
    binding, problems = space.bind(contract, snap, old_binding, maps)
    more, notes = space.check(contract, snap, binding)
    return snap, binding, problems + more, notes


def lark_user_ready(doc):
    """(usable, the user identity as lark-cli reports it): ready, or its access token ran out
    while the refresh token still holds, since the next call renews it."""
    user = ((doc or {}).get("identities") or {}).get("user") or {}
    if user.get("available"):
        return True, user
    try:
        alive = datetime.datetime.fromisoformat(user.get("refreshExpiresAt") or "") > datetime.datetime.now().astimezone()
    except (TypeError, ValueError):
        alive = False
    return alive and user.get("status") != "missing", user


def env_values(target):
    """({FEISHU_PROJECT_URL, FEISHU_WIKI_URL} as far as feishu/ids.json has them, what is missing)."""
    ids = load_json_file(os.path.join(target, "feishu", "ids.json")) or {}
    m, w = ids.get("meegle") or {}, ids.get("wiki") or {}
    out, missing = {}, []
    if m.get("simple_name") or m.get("project_key"):
        out["FEISHU_PROJECT_URL"] = "https://%s/%s" % (m.get("host") or "project.feishu.cn", m.get("simple_name") or m["project_key"])
    else:
        missing.append("飞书项目空间还没绑定")
    if w.get("space_id") and w.get("tenant_domain"):
        out["FEISHU_WIKI_URL"] = "https://%s/wiki/space/%s" % (w["tenant_domain"], w["space_id"])
    else:
        missing.append("知识空间的链接还拼不出来：租户域名在书记员登记第一份文档以后才记进 feishu/ids.json")
    return out, missing


def read_env(target):
    path = os.path.join(target, ".env")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def load_json_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        return json.loads(read_text(path))
    except ValueError:
        raise SetupError("%s 不是合法 JSON" % path)
