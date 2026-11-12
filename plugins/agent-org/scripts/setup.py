#!/usr/bin/env python3
"""Initialise a project directory for the agent organisation (the plugin's init step).

    python3 -I scripts/setup.py precheck
    python3 -I scripts/setup.py init --target DIR --task task.json [--sequences product,alg,...] [--apply]
    python3 -I scripts/setup.py feishu --target DIR [--map ID=NAME ...] [--live] [--apply]
    python3 -I scripts/setup.py template [--space NAME] [--target DIR]
    python3 -I scripts/setup.py check --target DIR [--live] [--sessions FILE]
    python3 -I scripts/setup.py pool --target DIR [--manual]
    python3 -I scripts/setup.py login [--wait]
    python3 -I scripts/setup.py env --target DIR [--apply]

init renders agents, project settings (permissions and hooks), CLAUDE.md, org/,
packages/, inbox/, spec/ and a copy of the plugin's scripts and rules into
.claude/agent-org/. Without --apply it only previews. Existing files are never
overwritten: a different version goes next to them as <file>.new. Running it
twice with the same inputs writes nothing.

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

Every role runs as a desktop session the researcher can see (the session pool): init adds
the pool hooks, .claude/agent-org/pool-plan.json and the draft of a first research-task
sheet. `setup.py pool` prints, for the session that initialises, every session to open in
order with its title, model and opening lines, and the claims so far (read only);
--manual prints how the researcher opens the top sessions by hand when that session cannot
open any. A task file may say "mode": "pool"; any other mode is refused.

login starts a device-code login of the command-line tool whose login is gone and prints a
QR code and a link for the researcher; --wait reads the login back. No device code, password
or token is printed. env writes the links of the project space and the knowledge space into
the project's .env and touches no other line of it.

check reports four layers: the project's own files, the Feishu side, the sessions, the first
research task. It returns 0 only when all four are ready. --live reads Feishu back (read
only); --sessions takes the client's own list of sessions saved as JSON.
Python 3.9 standard library only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import space  # noqa: E402
import project_paths  # noqa: E402

PLUGIN_ROOT = orgspec.PLUGIN_ROOT
SCAFFOLD = os.path.join(PLUGIN_ROOT, "scaffold")
TEMPLATES = os.path.join(PLUGIN_ROOT, "skills", "packages", "assets")
MIN_VERSIONS = {"lark-cli": "1.0.96", "meegle": "1.0.23", "claude": "2.1.219"}
DEFAULT_MODELS = {"p2": "opus", "p1": "sonnet", "p0": "sonnet", "pmo-p2": "opus",
                  "pmo-router": "sonnet", "pmo-scribe": "haiku", "quality-p1": "opus"}
REQUIRED_SEQUENCES = ("pmo", "quality")
BIN_SCRIPTS = ("orgspec.py", "ledger.py", "route.py", "space.py", "pool.py", "project_paths.py", "onboarding.py")
HOOK_SCRIPTS = ("guard.py", "audit.py", "instances.py", "inbox.py", "feishu_cmd.py")
SHARED_HOOK_SCRIPTS = ("pool.py",)         # kept in scripts/, copied into the hooks too
# liaison writes for readers other than the researcher: its +1 and +0 are opened when the first piece is due
POOL_DEFAULTS = {"p1": 1, "p0": 1, "later": ["liaison"], "max_sessions": 30, "label": ""}
OPEN_BATCH = 4                             # sessions opened before their claims are checked
FIRST_TASK = "packages/drafts/pmo/研究任务单-首条.md"
POOL_SESSIONS = (7, 57)

SCRIBE_LARK = ["docs +create", "docs +update", "wiki +node-create", "wiki +move",
               "drive +add-comment", "drive +add-reply", "drive +resolve-comment", "drive +upload"]
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
    "Bash(python3 -I .claude/agent-org/bin/ledger.py get *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py next *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py query *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py trace *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py status *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py mql *)",
    "Bash(python3 -I .claude/agent-org/bin/space.py snapshot *)",
    "Bash(python3 -I .claude/agent-org/bin/space.py bind *)",
    "Bash(python3 -I .claude/agent-org/bin/space.py check *)",
    "Bash(python3 -I .claude/agent-org/bin/space.py show *)",
    "Bash(python3 -I .claude/agent-org/bin/route.py classify *)",
    "Bash(python3 -I .claude/agent-org/bin/orgspec.py lint *)",
    "Bash(python3 -I .claude/agent-org/bin/orgspec.py show *)",
]
ADMIN_DENY = [
    "Bash(lark-cli * --yes)", "Bash(lark-cli * --yes *)", "Bash(lark-cli auth login *)",
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


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


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


def load_task_and_seeds(path, runtime=None):
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
                         "初始化以后用 setup.py feishu --live 绑定这个空间")
    if feishu.get("template_url") is not None and not re.match(r"https://[^\s/]+/\S+$", feishu["template_url"]):
        raise SetupError("研究任务的 feishu.template_url 要写成飞书项目模板的 https 链接")
    task["feishu"] = feishu
    if raw.get("models") is not None and not (
            isinstance(raw["models"], dict) and all(isinstance(v, str) for v in raw["models"].values())):
        raise SetupError("研究任务的 models 要写成 {\"角色或职级\": \"模型名\"}")
    runtime = runtime or raw.get("runtime", "claude")
    if runtime not in ("claude", "codex"):
        raise SetupError("runtime 只能是 claude 或 codex")
    task["runtime"] = runtime
    models = {} if runtime == "codex" else dict(DEFAULT_MODELS)
    models.update(raw.get("models") or {})
    if runtime == "codex" and any(v in ("opus", "sonnet", "haiku") for v in models.values()):
        raise SetupError("Codex 不沿用 Claude 的模型别名；移除 models 以继承用户设置，或填写明确选定的 Codex 模型")
    task["models"] = models
    if raw.get("mode", "pool") != "pool":
        raise SetupError("研究任务的 mode 只支持 pool（桌面会话池）：CLI 模式已取消，把 mode 这一行删掉或写成 pool")
    task["pool"] = load_pool_settings(raw.get("pool"))
    task = {k: task[k] for k in TASK_KEYS if k in task}
    seeds = {k: legacy_text(raw, k) for k, _ in LEGACY_SEEDS if legacy_text(raw, k)}
    legacy = [k for k, _ in LEGACY_SEEDS if k in raw]
    return task, seeds, legacy


def load_pool_settings(raw):
    """The pool section of the task file: initial +1 and +0 sessions per sequence, sequences
    that start with their +2 only, and a cap on all sessions."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SetupError("研究任务的 pool 要写成 {\"p1\": 1, \"p0\": 1, \"later\": [\"liaison\"], \"max_sessions\": 30}")
    unknown = sorted(k for k in raw if k not in POOL_DEFAULTS and not k.startswith("_"))
    if unknown:
        raise SetupError("研究任务的 pool 里有不认识的字段：%s；能写的是 p1、p0、later、max_sessions、label" % "、".join(unknown))
    out = dict(POOL_DEFAULTS)
    out.update({k: v for k, v in raw.items() if not k.startswith("_")})
    limits = orgspec.OrgSpec().rules["limits"]
    for key in ("p1", "p0"):
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


def load_task(path):
    return load_task_and_seeds(path)[0]


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
        for rank in (2, 1, 0):
            name = agent_name(seq, rank)
            pattern = fixed.get(name) or ("%s-p0-<工作项 ID>" % abbrev if rank == 0 else "%s-p%d-<序号>" % (abbrev, rank))
            pol = {"tag": tag_of(abbrev, rank), "abbrev": abbrev, "rank": rank, "sequence": seq,
                   "sequence_cn": spec.term("seq", seq),
                   "limit": limits.get(name, limits["p%d" % rank]), "fixed_session": fixed.get(name),
                   "session_pattern": pattern, "spawn": spec.dispatch_targets(name, seq, rank, seqs),
                   "write_paths": write_paths(seq, rank, task), "lark_write": [],
                   "meegle_write": ["comment add"], "ledger_write": False}
            if name == "pmo-scribe":
                pol.update(lark_write=SCRIBE_LARK, meegle_write=SCRIBE_MEEGLE, ledger_write=True)
            elif name == "pmo-p2":
                pol["lark_write"] = ["im +messages-send", "im +messages-reply"]
            agents[name] = pol
    p2s = [agent_name(s, 2) for s in seqs]
    infra = [agent_name("infra", r) for r in (2, 1, 0)] if "infra" in seqs else []
    # Hard: what the rules and the claims depend on. The researcher changes these through init.
    owned = {".claude/hooks/**": [], ".claude/settings.json": [], ".claude/settings.local.json": [],
             ".claude/state/**": [], ".claude/agents/**": [], ".claude/agent-org/**": [], "spec/task.json": [],
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
        "h_writers": [agent_name("quality", r) for r in (2, 1, 0)],
        "x_writers": [agent_name("rnd", r) for r in (2, 1, 0)] if "rnd" in seqs else [],
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


ROUTE = "python3 -I .claude/agent-org/bin/route.py"
POOL_ALLOW = ["Bash(python3 -I .claude/agent-org/bin/onboarding.py *)"] + ["Bash(%s %s)" % (ROUTE, c) for c in ("pool", "audit")] + [
    "Bash(%s %s *)" % (ROUTE, c) for c in ("claim", "expect", "pool", "refill", "audit", "notify", "focus")]
SCRIBE_ALLOW = ["Bash(python3 -I .claude/agent-org/bin/ledger.py %s *)" % w
                for w in ("register", "reply", "confirm", "rollback", "update", "comment", "status-block", "append", "archive",
                          "relink", "ensure-node")]
POOL_HOOK_EVENTS = ("Stop", "StopFailure", "Notification", "PermissionRequest", "PermissionDenied", "PostCompact")
# The desktop app's terminal panel tool runs shell commands outside the Bash tool. A matcher with
# regex characters is an unanchored regex, so it gets its own anchored group.
TERMINAL_TOOL = "^mcp__.+__run_in_terminal$"


def build_settings(task, risk, policy):
    high = sorted(k for k, v in risk.get("commands", {}).items() if v == "high-risk-write")
    deny = ADMIN_DENY + ["Bash(lark-cli %s *)" % k for k in high if k != "update"]
    ask = ["Edit(/.claude/settings.json)", "Edit(/.claude/settings.local.json)", "Edit(/.claude/hooks/**)",
           "Edit(/.claude/agents/**)", "Edit(/.claude/agent-org/**)", "Edit(/.claude/state/**)", "Edit(/org/**)",
           "Edit(/spec/**)"]
    ask += ["Edit(/%s)" % p for p in task["protected_paths"]]
    allow = list(READ_ALLOW)

    def hook(script):
        return [{"type": "command", "timeout": 30,
                 "command": "python3 -I -B \"$CLAUDE_PROJECT_DIR/.claude/hooks/%s\"" % script}]
    hooks = {
        "PreToolUse": [{"matcher": "Bash|Monitor|Edit|Write|NotebookEdit|Agent|Task|AskUserQuestion",
                        "hooks": hook("guard.py")},
                       {"matcher": TERMINAL_TOOL, "hooks": hook("guard.py")}],
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
    out["autoCompactWindow"] = 200000
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
        "日期字段写目标日期的 UTC 中午。meegle 的 workitem 命令里工作项类型写 api_name；MQL 的表名认 type_key 或类型现名，不认 api_name；view search 的 --view-scope 只认 type_key。ledger.py 已经处理好，手写时注意。",
    ]


def describe_writes(pol):
    parts = []
    if pol["ledger_write"]:
        parts.append("`ledger.py` 的全部写命令")
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


def common_role_lines(spec, seq, rank, name, pol, task, policy):
    lines = [
        "- 工作项、字段、状态和文档只由书记员写；你能写的飞书内容只有带会话标签的评论（`meegle comment add`，hook 会补标签）。",
        "- 读台账和文档：`ledger.py get`、`ledger.py query`、`ledger.py trace`、`ledger.py status`，以及 `lark-cli docs +fetch --as user`。",
    ]
    docs = [p for p, owners in sorted(policy["soft_owned"].items()) if name in owners and p in SPEC_DOC_NAMES]
    writable = code_list(pol["write_paths"]) + ("，以及你起草的 %s（写之前会请研究员确认）" % code_list(docs) if docs else "")
    lines.append("- 本地文件照规范只写：%s；评测协议的只读路径（%s）%s。" % (
        writable, code_list(task["protected_paths"]), "改动要研究员确认" if name in policy["h_writers"] else "不能改"))
    for doc in docs:
        how = "" if doc == "spec/requirements.md" else "用技能 `agent-org:spec` 访谈研究员后写，每条标来源；"
        lines.append("- 你负责起草%s `%s`：%s验证流程第 4 步研究员签字时，它和别的前四步产出一起冻结。" % (SPEC_DOC_NAMES[doc], doc, how))
    return lines


def sequence_lines(seq):
    lines = []
    if seq == "quality":
        lines.append("- 正式分数只由质量序列在冻结的 Eval Set 上跑出，正式集每一轮只跑一次；别的序列自己跑的是开发分，不进结论。评测协议的只读路径只有质量序列能改，改动要研究员确认。")
    if seq == "liaison":
        lines.append("- 材料写成草稿，由书记员上传到知识库的“汇报文档”节点；每份材料发出前要研究员签字（走决策包），研究员自己转交。读者不用飞书：不给他们开任何飞书权限。读者是研究员本人或组织里的会话的东西，不归外联写。")
    return lines


def agent_mechanics(spec, seq, rank, name, pol, task, policy):
    n = spec.rules["naming"]
    scribe = policy["agents"]["pmo-scribe"]["fixed_session"]
    entry = policy["agents"]["pmo-p2"]["fixed_session"]
    reg, receipt = spec.term("msg", "register_request"), spec.term("msg", "register_receipt")
    if rank == 0:
        session = ("会话池里你是一个桌面会话，池位名写作 `%s-p0-01` 这样的两位序号：侧栏标题、短名和消息抬头都用它。"
                   "接活时派活消息里“会话名：”那一行给的 `%s` 只写进工作项名称、包和“负责会话”字段。" % (pol["abbrev"], pol["session_pattern"]))
    else:
        session = "会话池里你是一个桌面会话，会话名在开局那句话里，写作 `%s` 这样的两位序号，认领后不变，消息抬头用它。" % pol["session_pattern"].replace("<序号>", "01")
    lines = [
        "- 会话名：%s会话标签是 `%s`。" % (session, pol["tag"]),
        "- 开工或接手：先读交给你的包；再用 `python3 -I .claude/agent-org/bin/ledger.py get <工作项 ID>` 读工作项，读主文档的“状态包”接上进度。不靠对话记状态。",
        "- 写包用技能 `agent-org:packages`（模板副本在 `.claude/agent-org/templates/`）。草稿写到 `packages/drafts/%s/`，文件名照 `%s`；写完跑 `python3 -I .claude/agent-org/bin/orgspec.py lint <路径>`。" % (seq, n["draft"]),
        "- 交回：给派活方发消息，首行写 `%s：<包名> <包路径>`，下面是三行以内的摘要。发完把回执（delivered 或 queued，以及 message_id）写进本轮的最后一句。" % msg_head(spec, "report", receiver="<派活方会话名>"),
    ]
    dispatch = ("再跑 `python3 -I .claude/agent-org/bin/route.py pool --role %s` 找空闲的会话。有空闲的：登记请求写“开工：是”，拿到【%s #ID】后发 "
                "`%s：<包名> <包路径>`%s。没有空闲的：登记请求写“开工：否”，活留在“待开工”排队；等有会话空出来，先请书记员确认“开工”这一步（`ledger.py confirm <工作项 ID> --by <承接方的会话名>`），再派。")
    if rank == 2:
        lines.append(("- 派 +1：先写下发关系的需求包，给 `%s` 发【%s】（写草稿路径）。" % (scribe, reg))
                     + dispatch % (pol["spawn"][0], receipt, msg_head(spec, "assign", receiver="<+1 的池位名>"), ""))
    elif rank == 1:
        lines.append(("- 派 +0：执行关系的需求包按验收标准划分，不按运行次数划分。先给 `%s` 发【%s】（写草稿路径和“来源工作项 #<它所属的下发需求包 ID>”）。" % (scribe, reg))
                     + dispatch % (pol["spawn"][0], receipt, msg_head(spec, "assign", receiver="<+0 的池位名>"),
                                   "，消息里单独写一行“会话名：%s-p0-<工作项 ID>”" % pol["abbrev"]))
    else:
        lines.append("- 你不再往下派。")
    if rank in (1, 2):
        lines.append("- 上限和补位：每个角色 +1 最多 3 个会话、+0 最多 5 个。没有空闲会话就排队；研究员在这一轮明确说“补位”或“再开一个 …”时，+2 才开新会话（开法照 `route.py claim` 输出里“补位用”那一行，开好马上 `route.py expect` 预登记），其余时候只运行 `route.py refill <角色>` 记一条补位请求，不提卡片。+1 不自己开会话。")
    if rank == 2:
        lines.append("- 你开的会话每结束一轮，应用会叫醒你一次。这不算交回：先跑 `route.py pool --queue <你的会话名>`，有等你派的活、又有空闲会话就派出去，没有就一句话结束本轮。")
        lines.append("- 跨序列的需求包、决策包写成草稿发给入口 `%s`（【%s】【%s】），由入口转交；你不直接给别的序列派活。" % (
            entry, spec.term("msg", "report"), spec.term("msg", "decide")))
        lines.append("- 研究任务走到你们序列负责的那一步时，这一步归你推进：先用 `python3 -I .claude/agent-org/bin/ledger.py next <研究任务 ID>` 看这一步必填什么，序列内的活派 +1，要别的序列配合就发跨序列的需求包；产出齐了，给 `%s` 发【%s】请它确认节点，写明各必填字段的值。正式分数只认质量序列在冻结的 Eval Set 上跑出的。" % (scribe, reg))
    else:
        if seq != "quality":
            lines.append("- 交付包里写明材料来源、每个变体的结果和运行 ID，失败的也列。你自己跑出的分数是开发分，只用来调试，不当结论写。")
        lines.append("- 需要别的序列配合的、要拍板的事，写进交给上一级的交付包，由本序列 +2 交给入口；你不直接找别的序列。")
    lines += common_role_lines(spec, seq, rank, name, pol, task, policy)
    if rank == 2:
        lines += [
            "- 研究员会直接点开你的会话交代事情。这时先运行 `python3 -I .claude/agent-org/bin/route.py focus open --topic '<一句话>'`，用 `route.py notify %s --text '…'` 告诉入口；做完或研究员说“结束”时运行 `route.py focus close`。谈定的事写成决策包（渠道：研究员主动发起）交给入口登记。没写进台账的决定不算数。" % entry,
            "- 研究员不在场时不停下来问人：要研究员拍板的事写决策包，或给入口发【%s】；+1 和 +0 要拍板的事写进交付包。" % spec.term("msg", "decide"),
            "- 你有项目级记忆（`.claude/agent-memory/%s/`），开工前读一下。只记跨阶段要沿用的标准和判断；任务的当前状态以台账和状态包为准。" % name,
        ]
    else:
        lines.append("- 研究员也可能直接点开你的会话。照做，开工时用 `route.py notify <派你活的会话名> --text '…'` 告诉它；研究员不在场时不停下来问人，要拍板的事写进交给上一级的交付包。")
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


def entry_values(spec, policy):
    """The parts of the entry's definition that name sessions and message heads."""
    pol = policy["agents"]["pmo-p2"]
    router = policy["agents"]["pmo-router"]["fixed_session"]
    scribe = policy["agents"]["pmo-scribe"]["fixed_session"]
    targets = code_list(pol["spawn"])
    me = pol["fixed_session"]
    return {
        "entry_runs_as": "在会话池里是研究员平时对话的会话",
        "entry_preface": "先写清工作方式：用中文回答，先给结论；先读再改，做完核对结果；研究员在场就问，不在场就写进决策队列，不猜参数、不编结果。你会运行很久，上下文一定会被压缩，所以不靠对话记状态：每次都从台账、状态包和 `python3 -I .claude/agent-org/bin/route.py pool` 读。",
        "entry_to_router": "给 `%s` 发 `%s：<一句话>`，把原话和脚本输出写进消息（长的先写进 `packages/drafts/pmo/` 再给路径）。它把结论用【%s】发回给你。" % (
            router, msg_head(spec, "assign", item=None, sender=me, receiver=router), spec.term("msg", "report")),
        "entry_dispatch": "**派活。** 照派发图给这些角色的会话发消息：%s。先跑 `python3 -I .claude/agent-org/bin/route.py pool --role <角色>` 找人：有空闲的发给空闲的，优先发给台账里“负责会话”对应的那个；显示“研究员在用”的不打断，用 `route.py notify <会话名> --text '...' --work-item <ID>` 留言，紧急的才发消息。消息首行写 `%s：<包名> <包路径>`，下面写要它做什么；正文不复述，包在文件里。回执是 delivered 或 queued 才算发出；对方收没收到，以对方 hook 记在 `.claude/logs/dispatch.jsonl` 的时间为准。\n\n**登记。** 工作项、字段、状态和文档只由书记员写。要登记的东西写成一条“%s”，发给 `%s`，它写完回【%s #ID】：" % (
            targets, msg_head(spec, "assign", sender=me, receiver="<+2 的会话名>"), spec.term("msg", "register_request"), scribe,
            spec.term("msg", "register_receipt")),
        "entry_extra_info": "**补充信息（R1、R2）。** 在工作项下加带标签的评论（请书记员登记，或自己 `meegle comment add`），再给负责会话发【%s #ID】说明一句；它正被研究员直接用着的，改用 `route.py notify` 留言。【%s】只由派活方发。" % (
            spec.term("msg", "notice"), spec.term("msg", "resume")),
        "entry_queue": "给 `%s` 发【%s】汇总成队列：每件写选项、代价和建议。研究员在本会话里" % (router, spec.term("msg", "assign")),
        "entry_focus": "**研究员直接委托。** 平时研究员找你；特殊情况下研究员会直接点开某个 +2 的会话交代事情，也会和研究任务当前这一步的负责序列的 +2 深谈。每条研究任务走到哪一步、由谁负责，用 `ledger.py query tasks` 和 `ledger.py next <ID>` 看，两个流程的对照在 `org/phases.md`。\n\n%s\n\n这时那个会话会自己运行 `route.py focus open`，并用 `route.py notify` 告诉你。`route.py pool` 显示它“研究员在用”期间，你不给它派活，新信息用 `route.py notify <会话名> --text '...' --work-item <ID>` 留在它的收件箱。谈定的事它会写成决策包（渠道：研究员主动发起）交给你：照 R6 请书记员建决策工作项，填上决定内容和决定人后确认“决定”这一步，“待签版本”写了要更新的字段的一并写进去，再通知受影响的会话。谈定的事没写进台账就不算数。\n\n**会话池。** 研究员说“恢复”时，运行 `route.py pool --recover`，照它列的清单发消息（你自己派出去的发【%s #ID】，别人派的请派活方发），先让书记员补完写到一半的登记。研究员在本会话明确说“补位”或“再开一个 …”时才开会话：路由和书记员由你开，其余序列的会话由那个序列的 +2 开；开法照 `route.py claim` 输出里的开池表，开好马上 `route.py expect` 预登记。没人要求时不开会话，也不提卡片；补位请求（`route.py refill`）放进每日决策队列。派一批活之前先看额度：5 小时额度用了八成以上，只派紧急程度为“插队”的。你开的会话每结束一轮，应用会叫醒你一次，这不算交回：先跑 `route.py pool --queue %s`，有待开工的活又有空闲会话就派出去，没有就一句话结束本轮。" % (
            bullets([spec.render(spec.rules["entry"]["focus_rule"])]), spec.term("msg", "resume"), me),
        "entry_permissions": "**权限提示。** 每个会话的权限提示只出现在它自己的会话里；`route.py pool --watch` 把等研究员批准的会话列在最前。请研究员不要对飞书写操作的提示选“不再询问”。",
    }


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
    if rank == 2:
        desc = "项目管理 +2（入口）。研究员平时对话的会话：信息和指示都先到这里，按路由决策表分发，管理决策队列和会话池。"
        common.update({
            "description_json": yaml_str(prefix + desc),
            "seq_intro": s["intro"], "handoff_rules": spec.handoff_rules(), "signoff": spec.signoff_items(),
            "routing_table": spec.routing_table(), "package_routes": spec.package_routes(),
            "writes": describe_writes(pol),
        })
        common.update(entry_values(spec, policy))
        return render(scaffold("agents/entry.md.tmpl"), common)
    if rank == 1:
        router_scribe = "**交给书记员。** 入口要你安排登记、分发或归档时，给 `%s` 发【%s】，等它回【%s】，再把书记员的写入结果和异常清单写进给入口的【%s】。" % (
            scribe, spec.term("msg", "register_request"), spec.term("msg", "register_receipt"), spec.term("msg", "report"))
        router_report = "**交回。** 给入口发【%s】：路由结论，或者决策队列表，或者书记员的写入结果。正文长的写进 `packages/drafts/pmo/`，消息里只给路径和三行摘要。" % spec.term("msg", "report")
        common.update({"description_json": yaml_str(prefix + "项目管理 +1（路由）。按路由决策表给出路由结论、汇总每日决策队列、安排书记员登记；不派发序列 +2。"),
                       "routing_table": spec.routing_table(), "router_scribe": router_scribe, "router_report": router_report})
        return render(scaffold("agents/router.md.tmpl"), common)
    intake = "\n\n会话池里各会话直接给你发【%s】。一次处理一条；你在忙时，后来的消息会自动排队。写完给请求方回 `%s：<写了什么>`，下面列出异常。写台账前先不加 `--apply` 预览，确认后再加；`ledger.py` 自己会加文件锁，同一时间只有一个写入。" % (
        spec.term("msg", "register_request"), msg_head(spec, "register_receipt", sender=scribe, receiver="<请求方会话名>"))
    common.update({"description_json": yaml_str(prefix + "项目管理 +0（书记员）。台账和文档唯一的写入方：按登记请求和需求包建工作项、上传文档、写字段、确认和回滚节点、归档。"),
                   "docs_tree": spec.docs_tree(), "scribe_intake": intake})
    return render(scaffold("agents/scribe.md.tmpl"), common)


def task_summary(task):
    lines = ["- 名称：%s（%s）" % (task["name"], task["description"]),
             "- 研究问题、目标和判据：研究任务单（飞书项目里“研究任务”工作项的主文档）和 `spec/requirements.md`。",
             "- 实验规格：`spec/spec.md`，写可改范围、目标指标、实验资源和搜索方法。可写路径：%s，只有工程序列能写。" % (
                 code_list(task["editable_paths"])),
             "- 评测协议：`spec/eval-protocol.md`，写数据、划分、打分和证据标准。只读路径：%s。" % code_list(task["protected_paths"])]
    return "\n".join(lines)


def where_text(task, ids):
    m = ids.get("meegle") or {}
    w = ids.get("wiki") or {}
    fs = task.get("feishu") or {}
    state = ("- 会话池的认领表和各会话的状态在 `.claude/state/`，由 hook 维护，不要手改；派活、交回和认领的记录在 `.claude/logs/dispatch.jsonl`；"
             "开池清单在 `.claude/agent-org/pool-plan.json`；飞书写操作的日志在 `feishu/write-log.jsonl`；收件箱在 `inbox/`。")
    return "\n".join([
        "- 台账：飞书项目空间「%s」，project_key %s；空间的结构（类型、流程、字段）在 `feishu/space.md`。" % (
            fs.get("meegle_space", "未填"), "`%s`" % m["project_key"] if m.get("project_key") else "待填（跑 `setup.py feishu`）"),
        "- 任务包：飞书知识库空间「%s」%s；发出前的草稿在 `packages/drafts/<序列>/`。" % (
            fs.get("wiki_space", "未填"), "，space_id `%s`" % w["space_id"] if w.get("space_id") else ""),
        "- 实验规格、评测协议和需求文档在 `spec/`，运行记录在 `runs/`，研究任务的流程（每一步谁负责、必填什么）在 `org/phases.md`。",
        state,
    ])


HARD_RULES = ("hook 只拦会损坏数据的事。在任何会话里都直接拒绝：改 `.claude/` 下的 hook、配置和角色定义，"
              "改评测协议的只读路径（质量序列的会话例外：改动弹窗要研究员确认，bypass 和 dontAsk 模式下也拒绝），"
              "high-risk-write 和带 `--yes` 的飞书命令，派发时带 `name`，用 Agent 工具派书记员，不照规定写法的认领、预登记和释放，第二条入口或书记员认领。"
              "在研究员自己的会话里弹窗询问、在别的会话里拒绝：书记员以外的会话写飞书和台账，在共享工作区里切分支、stash、reset 或 pull，释放认领。"
              "别的派发规则是规范：违反时 hook 提醒、记日志，不拦截。")


def shared_mechanics(spec, policy):
    agents = policy["agents"]
    entry, router, scribe = (agents[r]["fixed_session"] for r in ("pmo-p2", "pmo-router", "pmo-scribe"))
    words = "、".join("【%s】" % spec.term("msg", k) for k in ("report", "register_request", "register_receipt", "decide", "resume", "notice"))
    return bullets([
        "本目录是会话池：每个角色是研究员看得见的一个桌面会话。侧栏顶层是 %d 个序列的 +2，各自下面挂本序列的 +1 和 +0；入口 `%s` 下面挂路由 `%s` 和书记员 `%s`。研究员平时找入口，特殊情况直接点开某个会话交代。" % (
            len(policy.get("sequences") or []), entry, router, scribe),
        "认领：只有开局第一句写着“认领 <角色>（会话名 <名字>）”的会话才认领，照那句话运行 `python3 -I .claude/agent-org/bin/route.py claim <角色> --as <会话名>`，读输出里的角色说明，它就是本会话的工作说明。第一句没有这句话的，是研究员自己的会话：不要自己认领，也不替池里的角色干活。",
        "找人：派活前先跑 `python3 -I .claude/agent-org/bin/route.py pool --role <角色>`，看各会话忙不忙、手上是哪个工作项、桌面会话 ID，照脚本的建议发给空闲的会话。没有空闲的就排队，台账里“待开工”的工作项就是队列。",
        "发消息：派活、交回、登记请求、需拍板都用会话管理工具发消息，按对方的桌面会话 ID 发，同一家族里也可以写短名（会话名的小写）。首行写抬头 `%s：<包名> <包路径>`，抬头里的会话名写池位名（`route.py pool` 列的那个，+0 也写 `Alg-p0-01` 这样的池位名），其余几种只换抬头里的词：%s。正文放在包文件里，消息里只写抬头、包路径和三行以内的摘要。回执是 delivered 或 queued 才算发出，记下 message_id；对方收没收到，以对方 hook 记在 `.claude/logs/dispatch.jsonl` 的时间为准。" % (
            msg_head(spec, "assign"), words),
        "自动提醒：你开的会话每结束一轮，应用会叫醒你一次。这不算交回：先跑 `route.py pool --queue <自己的会话名>`，有待开工的活、又有空闲会话就派出去，没有就一句话结束本轮。【%s】（比如补充信息）也是消息，会叫起对方的下一轮；对方在 `route.py pool` 里显示“研究员在用”的不打断，改用 `route.py notify <会话名> --text '...'` 留在它的收件箱。" % spec.term("msg", "notice"),
        "开会话：只有研究员在这一轮明确要求开会话（“补位”“再开一个 alg-p0”）时才开；其余时候运行 `route.py refill <角色>` 记一条补位请求，不开会话，也不提卡片。",
        "工作项、字段、节点和文档只由唯一认领的书记员 `%s` 写，写操作都走 `ledger.py`；其他会话只能加带会话标签的评论。每一步谁负责、必填什么，以飞书空间为准：`ledger.py next <工作项 ID>` 现读。" % scribe,
        "high-risk-write 和带 `--yes` 的飞书命令、在本目录切分支、stash、reset 之类的 git 命令都由研究员执行：把完整命令写给研究员。",
        "知识库、文档和云空间的命令显式带 `--as user`；收发消息和订阅事件带 `--as bot`。",
        HARD_RULES,
    ])


def pool_sections(spec, policy):
    entry = policy["agents"]["pmo-p2"]["fixed_session"]
    decide = spec.term("msg", "decide")
    return "\n".join([
        "", "## 本目录怎样落实全局约定", "",
        bullets([
            "消息首行的抬头就是自报会话名。",
            "授权链看登记回执（`packages/sent/<工作项 ID>.json`）和派发图（`org/org.md` 的“派发图”）：对得上就照做；对不上，发【%s】给入口 `%s`。" % (decide, entry),
            "送达以收方 hook 的记录为准，不回 ACK，也不等 ACK。",
            "本项目的会话池就是研究员认可的派发方式：派活发给池里的会话，不改用后台子智能体。",
            "改了共享文件不群发通知，别人看台账里的“负责会话”。",
            "被消息叫起来的轮次不停下来问人：+2 和入口把要研究员拍板的事写【%s】发给入口，或者写成决策包；+1、+0 写进交给上一级的交付包。" % decide,
            "重发一条消息时，把“重发，上一条作废”写进抬头的【】里，比如【派活 #2101（重发，上一条作废）】。",
            "不提后续任务的卡片，后续工作写成需求包草稿交给派活方；不把手上的任务移交到新会话；收尾时不留等研究员回答的问题；不归档、清空或停止别的池会话，回退时由研究员本人在侧栏操作。",
        ]),
        "", "## Compact Instructions", "",
        "压缩上下文时保留：本会话的会话名和角色；未了事项，即派出去还没交回的工作项 ID 和接收方、还欠谁交回的工作项 ID；研究员在本会话说过的限制；还没写成决策包的口头决定。",
        "",
    ])


RUNTIME_INTRO = "本目录由插件 agent-org 初始化，按会话池运行：每个角色是研究员看得见的一个桌面会话，开局时认领角色，角色说明由 hook 注入（全文在 `.claude/agents/`）。研究员平时找入口 `PMO-p2-01`（项目管理 +2）。所有会话都会读到本文件，所以这里只放大家都要知道的内容。"


def claude_md(spec, task, ids, policy=None):
    values = {
        "project": task["name"], "task_summary": task_summary(task), "rank_table": spec.rank_table(),
        "general_rules": bullets(spec.general_rules_heads()), "handoff_rules": spec.handoff_rules(),
        "signoff": spec.signoff_items(), "naming": bullets(naming_lines(spec)), "where": where_text(task, ids),
        "mechanics": shared_mechanics(spec, policy), "runtime_intro": RUNTIME_INTRO,
        "pool_sections": pool_sections(spec, policy),
    }
    return render(scaffold("CLAUDE.md.tmpl"), values)


def org_md(spec, seqs):
    parts = ["# 组织规则", "",
             "> 由插件 agent-org 的规则文件（`.claude/agent-org/orgspec/rules.json`）生成，不要在这里改。组织规则的变更走决策包，研究员签字后改规则文件，再重跑初始化。研究员的补充决定在 `org/decisions.md`。", "",
             "## 职级", "", spec.rank_table(), ""]
    for head, body in spec.general_rules():
        parts += ["**%s**%s" % (head, body), ""]
    parts += ["## 只能由人来做的事", "", spec.signoff_items(), "", "## 序列", "", spec.sequence_overview(), ""]
    for sid in seqs:
        s = spec.sequence(sid)
        parts += ["### %s" % s["heading"], "", s["intro"], "",
                  orgspec.md_table(["职级", "已给定", "产出", "何时上报"],
                                   [[r["label"], r["given"], "<br>".join(r["outputs"]), r["escalate"]]
                                    for r in (s["ranks"][2], s["ranks"][1], s["ranks"][0])]), ""]
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


def pool_label(task, ids=None):
    """The prefix of every session title and the name of the sidebar section: pool.label,
    else the name of the Feishu project space."""
    m = (ids or {}).get("meegle") or {}
    return (task["pool"].get("label") or m.get("space_name") or (task.get("feishu") or {}).get("meegle_space")
            or task["name"])


def pool_entry(spec, task, policy, role, name, label):
    pol = policy["agents"][role]
    route = "python3 -I .claude/agent-org/bin/route.py"
    return {
        "role": role, "name": name, "short": "{short}" if name == "{name}" else name.lower(),
        "title": "[%s] %s" % (label, name),
        "model": task["models"].get(role) or task["models"].get("p%d" % pol["rank"]) or "inherit",
        "brief": "认领 %s（会话名 %s）。先运行 %s claim %s --as %s，读输出里的角色说明，回一句“已认领 %s”就结束本轮，等派活消息。" % (
            role, name, route, role, name, name),
        "background": "本目录是「%s」的智能体组织，按会话池运行：每个角色一个桌面会话，派活、交回、登记都发消息，抬头格式见项目的 CLAUDE.md。"
                      "你是 %s（%s +%d），开局先认领角色，认领输出里的角色说明就是你的工作说明；工作项和状态包在飞书，不靠对话记状态。" % (
                          task["name"], name, pol["sequence_cn"], pol["rank"]),
        "expect": "%s expect %s --role %s --desk <开会话时返回的桌面会话 ID>" % (route, name, role),
    }


SCRIBE_FIRST = (
    "认领以后接着做初始化的两件写入，做完再回话。命令都在项目目录里运行，前缀是 python3 -I .claude/agent-org/bin/："
    "1. onboarding.py start nodes；ledger.py ensure-node all --apply；onboarding.py end nodes。"
    "2. ledger.py query tasks，空间里已经有研究任务就跳过这一件；没有的话 onboarding.py start first_task；"
    "ledger.py register %s --apply；onboarding.py end first_task。"
    "回话写明：建了几个节点、沿用了几个，研究任务的工作项 ID 和主文档链接；哪一条命令失败，原样贴出报错，不要重试两次以上。" % FIRST_TASK)


def pool_plan(spec, task, policy, seqs, ids=None):
    """.claude/agent-org/pool-plan.json: every session of the pool with its title, model and
    opening lines. By default the initialising session opens them all in `order`, the scribe
    first; when it cannot open sessions, the researcher opens the top ones by hand with
    `opening` and each +2 opens its own children."""
    cfg = task["pool"]
    later = set(cfg.get("later") or [])
    label = pool_label(task, ids)
    tops, total = [], 0
    for seq in sorted(seqs, key=lambda x: x != "pmo"):     # the entry first
        role2 = agent_name(seq, 2)
        pol2 = policy["agents"][role2]
        name2 = pol2.get("fixed_session") or "%s-p2-01" % pol2["abbrev"]
        top = pool_entry(spec, task, policy, role2, name2, label)
        children = []
        if seq not in later:
            for rank in (1, 0):
                role = agent_name(seq, rank)
                pol = policy["agents"][role]
                count = 1 if pol.get("fixed_session") else cfg["p%d" % rank]
                for i in range(1, count + 1):
                    children.append(pool_entry(spec, task, policy, role,
                                               pol.get("fixed_session") or "%s-p%d-%02d" % (pol["abbrev"], rank, i), label))
        top["child_roles"] = [pool_entry(spec, task, policy, agent_name(seq, rank), "{name}", label) for rank in (1, 0)]
        what = "路由和书记员" if seq == "pmo" else "本序列的 +1 和 +0"
        top["brief"] += "不开子会话：%s由研究员的会话开出，找它们用 route.py pool。" % what
        top["opening"] = ("认领 %s（会话名 %s），然后照认领输出开出%s。" % (role2, name2, what) if children
                          else "认领 %s（会话名 %s）。本序列先不开子会话。" % (role2, name2))
        top["rebuild"] = "认领 %s（会话名 %s），不开子会话；用 route.py pool 找回本序列已有的会话，按桌面会话 ID 给它们发消息。" % (role2, name2)
        top["children"] = children
        tops.append(top)
        total += 1 + len(children)
    if total > cfg["max_sessions"]:
        raise SetupError("开池要开 %d 个会话，超过任务文件的 pool.max_sessions=%d：调小 pool.p1、pool.p0，把不急的序列写进 pool.later，"
                         "或调大 max_sessions" % (total, cfg["max_sessions"]))

    def weight(e):                                           # the scribe, the entry, the router, then by rank
        if e["role"] in (policy.get("scribe_role"), policy.get("entry_role"), policy.get("router_role")):
            return (policy.get("scribe_role"), policy.get("entry_role"), policy.get("router_role")).index(e["role"])
        return 5 - policy["agents"][e["role"]]["rank"]
    flat = tops + [c for t in tops for c in t["children"]]
    return {"version": 2, "project": task["name"], "label": label, "max_sessions": cfg["max_sessions"], "total": total,
            "batch": OPEN_BATCH, "order": [e["name"] for e in sorted(flat, key=weight)], "scribe_first": SCRIBE_FIRST,
            "top": tops}


def first_task_sheet(task):
    """The smallest research-task sheet: the project's name and its one line. The scribe
    registers it while the pool opens; the entry's interview replaces it afterwards."""
    return "\n".join([
        "# 研究任务单：%s" % task["name"], "",
        "- 流程：验证", "- 发出方：研究员", "- 关联工作项：待建", "",
        "## 目标", task["description"], "",
        "## 成功标准", "待定：立项访谈以后写。", "",
        "## 范围", "- 包含：待定", "- 不做：待定", "- 停止条件：待定", "",
        "## 资源上限", "- 总预算初值：待定", "- 期限：待定", "- 超出时：研究员决定", "",
        "## 已知的上下文", "待定", "",
        "## 风险和未决事项",
        "这份研究任务单是初始化时照项目名和一句话描述生成的。入口用技能 `agent-org:charter` 访谈研究员，写出修订稿，"
        "再请书记员运行 `ledger.py register <修订稿> --existing <这条研究任务的工作项 ID>` 换成主文档；工作项不变。", ""])


def registered_tasks(target):
    """Receipts of registered research-task sheets: [(receipt, path of the draft)]."""
    sent = os.path.join(target, "packages", "sent")
    out = []
    for name in sorted(os.listdir(sent)) if os.path.isdir(sent) else []:
        rec = load_json_file(os.path.join(sent, name)) if name.endswith(".json") and not name.startswith("reply-") else None
        if isinstance(rec, dict) and rec.get("status") == "registered" and rec.get("draft"):
            draft = os.path.join(target, rec["draft"])
            if os.path.isfile(draft) and ledger.sheet_kind(draft) == "research_task":
                out.append((rec, draft))
    return out


def phases_md(spec, snap=None, binding=None):
    """org/phases.md: the flows of the main-line type as the space has them. Before the space
    is bound there is nothing to show; `setup.py feishu --apply` writes it again from the snapshot."""
    main = (((binding or {}).get("types") or {}).get(orgspec.Contract(spec).main_line()) or {}).get("type_key")
    flows = space.flows_md(snap, types=[main]) if snap and main else ""
    return render(scaffold("org/phases.md.tmpl"), {
        "flows": flows or "还没读到空间里的流程：绑定飞书空间后，`setup.py feishu --live --apply` 会把两个流程的节点、负责角色和必填项写在这里。",
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


def feishu_readme(task, ids):
    m = ids.get("meegle") or {}
    line = "飞书项目「%s」（project_key %s）；知识库「%s」。" % (
        (task.get("feishu") or {}).get("meegle_space", "未填"), m.get("project_key") or "待填",
        (task.get("feishu") or {}).get("wiki_space", "未填"))
    return render(scaffold("feishu/README.md.tmpl"), {"space_line": line})


# ---------------------------------------------------------------- plan
class Plan:
    def __init__(self):
        self.files = {}      # rel -> (content, executable, keep_existing)
        self.notes = []
        self.manifest = {}


def build_plan(target, task_path, seq_arg, runtime=None):
    spec = orgspec.OrgSpec()
    problems = spec.validate()
    if problems:
        raise SetupError("规则文件有问题，先修好：\n" + "\n".join(problems))
    task, seeds, legacy = load_task_and_seeds(task_path, runtime)
    other = ".claude/agent-org" if task["runtime"] == "codex" else ".agent-org"
    if os.path.isfile(os.path.join(target, other, "manifest.json")):
        raise SetupError("目标目录已有另一运行平台的组织。请使用独立目录，先保留原绑定、任务包与回执，再迁移")
    seqs = pick_sequences(spec, seq_arg)
    ids = load_ids(target)
    risk, note = resolve_risk(target)
    plan = Plan()
    plan.notes.append(note)
    if not (ids.get("meegle") or {}).get("project_key"):
        plan.notes.append("提醒：项目还没绑定飞书空间。初始化以后跑 `setup.py feishu --target <项目目录> --live`，看任务文件里那个空间的匹配结果，核对后加 --apply；绑定以前台账脚本不能用。")
    policy = build_policy(spec, task, seqs)
    f = plan.files

    def put(rel, content, executable=False, keep=False):
        f[rel] = (content, executable, keep)

    if task["pool"]["later"] == POOL_DEFAULTS["later"]:      # the default only speaks of sequences that are enabled
        task["pool"]["later"] = [x for x in task["pool"]["later"] if x in seqs]
    unknown = [x for x in (task.get("pool") or {}).get("later", []) if x not in seqs]
    if unknown:
        raise SetupError("pool.later 里的序列没有启用：%s" % "、".join(unknown))
    put("CLAUDE.md", claude_md(spec, task, ids, policy))
    put(".claude/settings.json", dump_json(build_settings(task, risk, policy)))
    put(".claude/hooks/policy.json", dump_json(policy))
    put(".claude/hooks/lark-risk.json", dump_json(risk))
    for script in HOOK_SCRIPTS:
        put(".claude/hooks/" + script, scaffold("hooks/" + script), script != "feishu_cmd.py")
    for script in SHARED_HOOK_SCRIPTS:
        put(".claude/hooks/" + script, read_text(os.path.join(HERE, script)))
    for seq in seqs:
        for rank in (2, 1, 0):
            name = agent_name(seq, rank)
            content = pmo_file(spec, rank, task, policy) if seq == "pmo" else agent_file(spec, seq, rank, task, policy)
            put(".claude/agents/%s.md" % name, content)
    for script in BIN_SCRIPTS:
        put(".claude/agent-org/bin/" + script, read_text(os.path.join(HERE, script)), True)
    for name in ("rules.json", "terms.zh.json", "contract.json"):
        put(".claude/agent-org/orgspec/" + name, read_text(os.path.join(orgspec.ORGSPEC_DIR, name)))
    for name in sorted(os.listdir(TEMPLATES)):
        if name.endswith(".md"):
            put(".claude/agent-org/templates/" + name, read_text(os.path.join(TEMPLATES, name)))
    put(".claude/agent-org/pool-plan.json", dump_json(pool_plan(spec, task, policy, seqs, ids)))
    plan.notes.append("init 和绑定都做完以后开池：`setup.py pool --target <项目目录>` 打印开池清单。")
    put(FIRST_TASK, first_task_sheet(task), keep=True)
    put("org/org.md", org_md(spec, seqs))
    put("org/grants.md", grants_md(spec, task, policy))
    put("org/decisions.md", render(scaffold("org/decisions.md.tmpl"), {"deviations": deviations_text(spec)}), keep=True)
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
            plan.notes.append("提醒：%s 还是改名前的旧模板。新模板按小节写清每一项，用技能 `agent-org:spec` 和研究员访谈后重写；"
                              "新模板在插件的 scaffold/%s.tmpl。" % (rel, rel))
    put("feishu/README.md", feishu_readme(task, ids))
    put("packages/README.md", scaffold("packages/README.md.tmpl"))
    for seq in seqs:
        put("packages/drafts/%s/.gitkeep" % seq, "")
    put("packages/sent/.gitkeep", "")
    put("inbox/README.md", scaffold("inbox/README.md.tmpl"))
    put("runs/README.md", scaffold("runs/README.md.tmpl"))
    plan.manifest = {
        "plugin": "agent-org", "plugin_version": plugin_version(),
        "sequences": seqs, "entry_role": policy["entry_role"],
        "task_sha256": sha256(dump_json(task)),
        "rules_sha256": sha256(read_text(spec.rules_path)), "terms_sha256": sha256(read_text(spec.terms_path)),
        "contract_sha256": sha256(read_text(os.path.join(orgspec.ORGSPEC_DIR, "contract.json"))),
        "lark_risk": {"lark_cli_version": risk.get("lark_cli_version"), "commands": len(risk.get("commands", {}))},
        "files": {p: sha256(c) for p, (c, _, _) in sorted(f.items())},
    }
    if task.get("runtime") == "codex":
        import codex_setup
        return codex_setup.adapt(sys.modules[__name__], plan, target, spec, task, policy, seqs)
    return plan


def previous_hashes(target):
    m = os.path.join(target, ".claude", "agent-org", "manifest.json")
    try:
        return json.loads(read_text(m)).get("files", {})
    except (OSError, ValueError):
        return {}


def actions_for(target, plan):
    if plan.manifest.get("runtime") == "codex":
        import codex_setup
        return codex_setup.actions_for(target, plan)
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
        elif keep:
            acts.append(("kept", rel, None))
        elif current is not None and before.get(rel) == sha256(current):
            acts.append(("update", rel, current))
        elif os.path.isfile(path + ".new") and read_text(path + ".new") == content:
            acts.append(("pending", rel, current))
        else:
            acts.append(("new", rel, current))
    pair = (".claude/hooks/guard.py", ".claude/hooks/policy.json")
    on_disk = disk_policy_version(target)
    if on_disk is not None and on_disk != POLICY_VERSION and any(kind in ("new", "pending") for kind, rel, _ in acts if rel in pair):
        acts = [("paired", rel, cur) if rel in pair and kind == "update" else (kind, rel, cur) for kind, rel, cur in acts]
    return acts


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


POLICY_VERSION = 5


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


LABELS = {"create": "新建", "same": "相同，跳过", "chmod": "补可执行权限", "update": "更新（上次生成后没人改过）",
          "new": "本地改过，新版本写 .new", "paired": "和 guard.py、policy.json 中改过的那个配套，新版本也写 .new",
          "pending": "内容不同，.new 已存在", "kept": "已有，研究员维护，不动"}


def manifest_text(plan, acts):
    m = dict(plan.manifest)
    m["pending_new"] = sorted(rel for kind, rel, _ in acts if kind in ("new", "pending", "paired"))
    files = dict(m["files"])
    for kind, rel, current in acts:                  # a paired file keeps its old hash: once its partner is
        if kind == "paired" and current is not None:  # merged, the next init updates it in place
            files[rel] = sha256(current)
    m["files"] = files
    return dump_json(m)


def report(target, plan, acts, apply):
    print("目标目录：%s" % target)
    for n in plan.notes:
        print(n)
    counts = {}
    for kind, rel, current in acts:
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("same", "kept"):
            continue
        print("  [%s] %s" % (LABELS[kind], rel))
        if kind in ("new", "update", "paired"):
            diff = difflib.unified_diff((current or "").splitlines(True), plan.files[rel][0].splitlines(True),
                                        rel, rel + ".new", n=1)
            sys.stdout.writelines("      " + line for line in list(diff)[:60])
    print("合计：" + "，".join("%s %d" % (LABELS[k], v) for k, v in sorted(counts.items())))
    if "paired" in counts:
        print("提醒：guard.py 和 policy.json 要成对更新。两个 .new 一起合并以后再重跑 init；只合并 guard.py.new，hook 会全部拒绝；"
              "只合并 policy.json.new，旧 guard 会按新规则跑，部分硬规则悄悄变样。")
    problem = pair_problem(target)
    if problem:
        print("提醒：" + problem)
    if not apply:
        print("这是预览，没有写任何文件。加 --apply 才会写。")


def apply_plan(target, plan, acts):
    if plan.manifest.get("runtime") == "codex":
        import codex_setup
        return codex_setup.apply_plan(target, plan, acts)
    written = 0
    for kind, rel, _ in acts:
        path = os.path.join(target, rel)
        if kind in ("create", "new", "update", "paired"):
            content, executable, _ = plan.files[rel]
            dest = path + ".new" if kind in ("new", "paired") else path
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            if executable:
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
        elif kind == "chmod":
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
    mpath = os.path.join(target, ".claude", "agent-org", "manifest.json")
    text = manifest_text(plan, acts)
    if not (os.path.isfile(mpath) and read_text(mpath) == text):
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(text)
        written += 1
    print("已写入 %d 处。" % written)
    return written


# ------------------------------------------------------------ precheck
def precheck():
    rows = []

    def add(item, status, detail):
        rows.append((item, status, detail))

    if sys.platform == "darwin":
        apps = [p for p in ("/Applications/Lark.app", "/Applications/Feishu.app",
                            os.path.expanduser("~/Applications/Lark.app"),
                            os.path.expanduser("~/Applications/Feishu.app")) if os.path.isdir(p)]
        add("飞书客户端", "通过" if apps else "提醒", apps[0] if apps else "没找到 Lark.app 或 Feishu.app；网页版也能用")
    else:
        add("飞书客户端", "跳过", "不是 macOS，请人工确认")
    for cli, cmd in (("lark-cli", ["lark-cli", "--version"]), ("meegle", ["meegle", "version"]),
                     ("claude", ["claude", "--version"])):
        code, out, err = run(cmd)
        found = re.findall(r"\d+\.\d+\.\d+", out or "")
        if code != 0 or not found:
            add("%s 版本" % cli, "失败", (err or out or "未安装").strip()[:120])
        else:
            ok = version_tuple(found[0]) >= version_tuple(MIN_VERSIONS[cli])
            add("%s 版本" % cli, "通过" if ok else "失败", "%s（最低 %s）" % (found[0], MIN_VERSIONS[cli]))
    code, out, _ = run(["lark-cli", "doctor"], 60)
    try:
        doctor = json.loads(out)
        bad = [c for c in doctor.get("checks", []) if c.get("status") != "pass"]
        add("lark-cli doctor", "通过" if doctor.get("ok") else "失败",
            "；".join("%s=%s" % (c.get("name"), c.get("status")) for c in bad) or "全部通过")
    except ValueError:
        add("lark-cli doctor", "失败", "输出不是 JSON（退出码 %s）" % code)
    code, out, _ = run(["lark-cli", "auth", "status"])
    try:
        ok, user = lark_user_ready(json.loads(out))
        add("lark-cli 用户身份", "通过" if ok else "失败", "status=%s tokenStatus=%s%s" % (
            user.get("status"), user.get("tokenStatus"), "" if ok else "；运行 setup.py login 发起登录，研究员扫码"))
    except ValueError:
        add("lark-cli 用户身份", "失败", "auth status 输出不是 JSON（退出码 %s）" % code)
    code, _, _ = run(["meegle", "auth", "status"])
    add("meegle 登录", "通过" if code == 0 else "失败",
        "退出码 %s%s" % (code, "" if code == 0 else "；运行 setup.py login 发起登录，研究员扫码"))
    code, out, _ = run(["meegle", "project", "search", "--page-num", "1", "--format", "json"], 60)
    try:
        projects = (json.loads(out) or {}).get("projects") or []
        add("飞书项目权限", "通过" if projects else "失败", "能访问 %d 个空间" % len(projects))
    except ValueError:
        add("飞书项目权限", "失败", "project search 输出不是 JSON（退出码 %s）" % code)
    code, out, _ = run(["meegle", "telemetry", "status"])
    try:
        enabled = json.loads(out).get("enabled")
        add("meegle 诊断上报", "提醒" if enabled else "通过",
            "开着，请研究员决定是否关闭（meegle telemetry disable，本人执行）" if enabled else "已关闭")
    except ValueError:
        add("meegle 诊断上报", "跳过", "telemetry 子命令不可用（退出码 %s）" % code)
    code, out, _ = run(["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot", "--dry-run"], 60)
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
        add("技能 %s" % name, "通过" if here else "提醒", "已安装" if here else "没找到；书记员用 ledger.py，不依赖它，但人工排查时有用")
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
    if m.get("plugin_version") != plugin_version():
        problems.append("插件版本：项目是 %s，插件是 %s；重跑 init 预览差异" % (m.get("plugin_version"), plugin_version()))
    rules = read_text(os.path.join(orgspec.ORGSPEC_DIR, "rules.json"))
    if m.get("rules_sha256") != sha256(rules):
        problems.append("规则文件和项目里的副本不一致；重跑 init 预览差异")
    if m.get("terms_sha256") != sha256(read_text(os.path.join(orgspec.ORGSPEC_DIR, "terms.zh.json"))):
        problems.append("术语表和项目里的副本不一致；重跑 init 预览差异")
    if m.get("contract_sha256") != sha256(read_text(os.path.join(orgspec.ORGSPEC_DIR, "contract.json"))):
        problems.append("约定文件和项目里的副本不一致；重跑 init 预览差异，再跑 setup.py feishu --live 核对绑定")
    task_path = os.path.join(target, "spec", "task.json")
    fresh = None
    if os.path.isfile(task_path) and m.get("files", {}).get("spec/task.json") not in (None, sha256(read_text(task_path))):
        problems.append("改过：spec/task.json（和 init 用的任务文件不同）。它跟着 init --task 的任务文件走：要改设置，改任务文件再重跑 init --apply；"
                        "有 spec/task.json.new 的，对照合并。改回来之前，先不比对插件更新")
    else:
        try:      # regenerate from the project's own task and sequences: catches script, template and ID changes
            fresh = build_plan(target, task_path, ",".join(m.get("sequences") or []) or None)
        except SetupError as e:
            problems.append("没法按现在的插件重新生成来比对：%s" % e)
    if fresh:
        stale = [rel for rel, (content, _, keep) in sorted(fresh.files.items())
                 if not keep and m.get("files", {}).get(rel) != sha256(content)]
        risk_reported = ".claude/hooks/lark-risk.json" in stale
        if risk_reported:                                   # follows the lark-cli on this machine, not the plugin
            stale.remove(".claude/hooks/lark-risk.json")
            table = (load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
            problems.append("lark-cli 风险表和本机的 lark-cli 对不上：项目里的表按 %s 生成，本机是 %s；重跑 init 会换成对应的表" % (
                table or "未知版本", installed_lark_version() or "没装 lark-cli"))
        table = (load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
        installed = installed_lark_version()
        if table and installed and installed != table and not risk_reported:
            notes.append("本机 lark-cli 是 %s，项目的风险表按 %s 生成（插件自带的表）；新命令的风险等级可能缺失，未知的写命令一律按写处理" % (installed, table))
        if stale:
            problems.append("现在的插件、规则或 feishu/ids.json 生成的内容和初始化时不同（%d 个文件：%s）；重跑 init 预览差异" % (
                len(stale), "、".join(stale[:12]) + ("等" if len(stale) > 12 else "")))
    for rel, digest in sorted(m.get("files", {}).items()):
        path = os.path.join(target, rel)
        if not os.path.isfile(path):
            problems.append("缺文件：%s" % rel)
        elif rel in ("org/decisions.md", "org/phases.md") or rel.startswith(("spec/", "packages/drafts/")):
            continue                                        # the researcher's documents; a draft gets its work item written in
        else:
            try:
                changed = sha256(read_text(path)) != digest
            except (UnicodeDecodeError, OSError):
                problems.append("读不了：%s（不是 UTF-8 编码？）" % rel)
                continue
            if changed and not (fresh and rel in fresh.files and read_text(path) == fresh.files[rel][0]):
                problems.append("改过：%s（和初始化时不同）" % rel)
    for rel in m.get("pending_new", []):
        if os.path.isfile(os.path.join(target, rel + ".new")):
            problems.append("待合并：%s.new" % rel)
    problem = pair_problem(target)
    if problem:
        problems.append(problem)
    return problems, notes


def live_feishu(target, seqs):
    """Read the space and the knowledge nodes as they are now: the binding still holds and
    every node is in the knowledge space. Reads only."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    ids = load_json_file(os.path.join(target, "feishu", "ids.json")) or {}
    m, wiki = ids.get("meegle") or {}, ids.get("wiki") or {}
    problems, notes = [], []
    try:
        old = space.binding_of(ids)
        _, now_, found, notes = read_space(
            contract, {"project_key": m["project_key"], "simple_name": m.get("simple_name"), "name": m.get("space_name")}, {}, old)
        problems += ["空间结构：%s，%s。%s" % tuple(row) for row in found + space.stale(contract, old, now_)]
    except (SetupError, KeyError) as e:
        problems.append("读不了飞书项目空间：%s" % e)
    nodes = wiki.get("nodes") or {}

    def lost(key):
        code, out, _ = run(["lark-cli", "wiki", "+node-get", "--node-token", (nodes.get(key) or {}).get("node_token") or "", "--as", "user"], 60)
        try:
            doc = json.loads(out) if code == 0 else None
        except ValueError:
            doc = None
        where = ledger._find(doc or {}, ("space_id",))
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
    problems = feishu_gaps(target, seqs)
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
    tasks = registered_tasks(target)
    if not tasks:
        return ["还没有登记过研究任务：开池时由书记员登记 %s，清单见 setup.py pool" % FIRST_TASK], []
    rec, draft = tasks[0]
    item_id, doc = str(rec.get("work_item_id")), str(rec.get("doc") or "")
    problems = []
    if not doc.startswith("http"):
        problems.append("研究任务 #%s 的回执里没有主文档的地址" % item_id)
    try:
        line = ledger.header_of(read_text(draft)).get("关联工作项", "")
    except (OSError, UnicodeDecodeError):
        line = ""
    if item_id not in re.findall(r"#(\d+)", line):
        problems.append("草稿 %s 的“关联工作项”一行不是 #%s" % (rec["draft"], item_id))
    if live and not problems:
        try:
            lg = ledger.Ledger(target, "preview")
            main = lg.contract.main_line()
            it = lg.item(item_id)
            if not it or it.get("type") != main:
                return ["工作项 #%s 读不到，或者不是研究任务" % item_id], []
            link = ledger.plain(it["fields"].get(lg.field(main, "main_doc")["field_key"]))
            if link != doc:
                problems.append("研究任务 #%s 的主文档字段是 %s，回执记的是 %s" % (item_id, link or "空的", doc))
            node = lg.lark("wiki", "+node-get", "--node-token", doc.rstrip("/").rsplit("/", 1)[-1])
            above, want = ledger._find(node or {}, ("parent_node_token",)), (lg.nodes.get(main) or {}).get("node_token")
            if it["state"] != lg.state(main, "archived") and want and above != want:
                problems.append("研究任务 #%s 的主文档不在“%s”节点下" % (item_id, lg.spec.term("node", main)))
        except (ledger.LedgerError, orgspec.OrgSpecError, space.SpaceError, KeyError) as e:
            problems.append("回读研究任务 #%s 没读成：%s" % (item_id, str(e)[:200]))
    return problems, []


def listed_sessions(target, path, plan, pl):
    """(problems, reminders) of the claims against the client's own list of sessions: the
    result of list_sessions saved as it came."""
    rows = load_json_file(path)
    if not isinstance(rows, list):
        return ["--sessions 给的文件不是会话列表：把 list_sessions 的结果原样存成 JSON 文件（include_archived 写 true，limit 写够）"], []
    by_id = {str(r.get("sessionId")): r for r in rows if isinstance(r, dict)}
    label, here = plan.get("label"), os.path.realpath(target)
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
        if "group" not in row:
            unknown.append(name)
        elif (row["group"] or {}).get("name") != label:
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


def session_layer(target, m, listing=None):
    """(problems, reminders): every planned session holds its claim, one entry and one scribe,
    no pre-registration left over, one class of permission mode, all working in the project."""
    problems, notes = pool_health(target, m)
    if listing:
        import pool
        pl, ok = pool.load(target)
        plan = load_json_file(os.path.join(target, ".claude", "agent-org", "pool-plan.json")) or {}
        if ok:
            more, extra = listed_sessions(target, listing, plan, pl)
            problems += more
            notes += extra
    return problems, notes


def check(target, live=False, sessions=None):
    """Is the project ready, layer by layer: its own files, the Feishu side, the sessions, the
    first research task. 0 only when every layer is. --live reads Feishu back (read only)."""
    mpath = os.path.join(target, ".claude", "agent-org", "manifest.json")
    if not os.path.isfile(mpath):
        print("没有 %s：这个目录还没初始化。" % mpath)
        return 2
    m = json.loads(read_text(mpath))
    layers = [("本地配置",) + local_layer(target, m), ("飞书",) + feishu_layer(target, m, live),
              ("会话",) + session_layer(target, m, sessions), ("首条研究任务",) + first_task_layer(target, live)]
    for name, problems, notes in layers:
        print("%s：%s" % (name, "%d 个问题" % len(problems) if problems else "就绪"))
        for n in notes:
            print("  提醒：" + n)
        for p in problems:
            print("  问题：" + p)
    bad = [(name, len(problems)) for name, problems, _ in layers if problems]
    print("检查：%s%s" % ("还有 %d 个问题（%s）" % (sum(n for _, n in bad), "、".join("%s %d" % x for x in bad)) if bad else "四层都就绪",
                        "" if live else "。飞书一侧只看了本地记下的标识；加 --live 回读飞书"))
    return 1 if bad else 0


def pool_health(target, manifest):
    """(problems, reminders) from the claims and the sessions' status files."""
    import pool
    policy = load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) or {}
    if not policy.get("agents"):
        return ["读不到 .claude/hooks/policy.json"], []
    if not policy.get("entry_role") or not policy.get("scribe_role"):
        return ["policy.json 还是旧版的：先在 Claude 以外的终端里合并 .claude/hooks/ 下的 .new，重跑 init --apply，再查会话池"], []
    pl, ok = pool.load(target)
    if not ok:
        return ["读不出 .claude/state/pool.json：认领表坏了，台账写入会全部被拒；请研究员先看这个文件"], []
    problems, notes = [], []
    plan = load_json_file(os.path.join(target, ".claude", "agent-org", "pool-plan.json")) or {}
    planned = [t["name"] for t in plan.get("top", [])] + [c["name"] for t in plan.get("top", []) for c in t.get("children", [])]
    held = {}
    for c in pl["claims"].values():
        held.setdefault(c["role"], []).append(c["name"])
    for role in (policy["entry_role"], policy["scribe_role"]):
        if len(held.get(role, [])) > 1:
            problems.append("%s 有 %d 条认领（%s），只能有一条；请研究员 route.py release 多余的" % (role, len(held[role]), "、".join(held[role])))
    missing = sorted(set(planned) - set(pl["claims"]))
    if missing:
        problems.append("会话池还没开齐：%s 没人认领（共 %d/%d 条认领）；清单见 setup.py pool" % (
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
        notes.append("还停在旧版插件规则上的会话：%s；研究员退出并重开桌面应用后，它们下一轮会收到新的角色说明" % "、".join(old))
    versions = sorted({r["version"] for r in rows if r.get("version")})
    if len(versions) > 1:
        notes.append("池里的 Claude Code 版本不一致（%s）；行为有变化时重跑一遍会话池实验" % "、".join(versions))
    return problems, notes


def pool_checklist(target, manual=False):
    """`setup.py pool`: how to open the pool, and the claims so far. Reads only."""
    import pool
    plan = load_json_file(os.path.join(target, ".claude", "agent-org", "pool-plan.json"))
    policy = load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) or {}
    if not policy.get("agents") or not plan:
        print("这个项目还没有开池清单：先跑 init --apply。")
        return 2
    if plan.get("version") != 2:
        print("开池清单是旧版插件生成的：重跑 init --apply。")
        return 2
    pl, ok = pool.load(target)
    claimed = set(pl["claims"]) if ok else set()
    label = plan["label"]
    print("开池清单：%s（共 %d 个会话，总上限 %d）。会话标题的前缀和侧栏分区的名字：%s" % (
        plan["project"], plan["total"], plan["max_sessions"], label))
    print("")
    if manual:
        print("手工开池（研究员的会话开不了会话时用）：")
        if any(t["children"] for t in plan["top"] if t["role"] != policy.get("entry_role")):
            print("这份清单是按研究员的会话开出全部会话生成的。手工开池只建各序列的 +2：先把任务文件的 pool.later 写成"
                  "入口以外的全部序列，重跑 init --apply，再看这份清单。")
        print("研究员在桌面应用里新建 %d 个会话（New session，目录选本项目，不开 worktree），模型照括号里的选，每个粘贴一句：" % len(plan["top"]))
        for i, top in enumerate(plan["top"], 1):
            kids = "、".join("%s（%s）" % (c["name"], c["model"]) for c in top["children"]) or "先不开"
            print("%d. %s（%s，%s）：%s" % (i, top["name"], top["model"], "已认领" if top["name"] in claimed else "未认领", top["opening"]))
            print("   它会开出：%s" % kids)
            if ok and top["name"] not in claimed and any(c["name"] in claimed for c in top["children"]):
                print("   它的子会话还在，换会话时粘贴重建句：%s" % top["rebuild"])
        print("每个 +2 认领以后，照认领输出把自己的标题改成“[%s] <会话名>”，归入侧栏分区“%s”。其余的 +1、+0 开池时不建："
              "某个序列第一次有活时，研究员在它的 +2 会话里说一句“补位”。" % (label, label))
    else:
        flat = {e["name"]: e for t in plan["top"] for e in [t] + t["children"]}
        first = bool(feishu_gaps(target, enabled_sequences(target))) or not registered_tasks(target)
        print("研究员的会话（做初始化的这个会话）照下面的顺序开出全部会话：")
        print("- 每个会话用会话管理工具 start_session：initiation 写 user_asked，context 写 fresh，use_worktree 写 false，"
              "权限模式不填（继承本会话）；name、title、model、prompt、background 照下表。")
        print("- 每开一个，马上用返回的桌面会话 ID 运行它的 expect。第 1 个开书记员；其余每批至多 %d 个，这一批都认领了再开下一批。" % plan["batch"])
        print("- 全部认领以后：用 detach_session 把它们移到顶层；侧栏里没有叫“%s”的分区就用 create_section 建一个；"
              "用 move_sessions 把它们连同本会话归进去。本会话的标题是应用自动起的，就用 set_session_title 改成“[%s] Researcher”。" % (label, label))
        print("- model 填不进去就不填，开好以后再改；模型高于本会话的会被降档，记进交接。")
        print("")
        for i, name in enumerate(plan["order"], 1):
            e = flat[name]
            prompt = e["brief"] + (plan["scribe_first"] if first and e["role"] == policy.get("scribe_role") else "")
            print("%d. %s（%s）" % (i, name, "已认领，不用再开" if name in claimed else "未认领"))
            print("   name：%s　title：%s　model：%s" % (e["short"], e["title"], e["model"]))
            print("   prompt：%s" % prompt)
            print("   background：%s" % e["background"])
            print("   expect：%s" % e["expect"])
        print("")
        print("这个会话开不了会话时（比如它是别的会话代开的）：setup.py pool --target <项目目录> --manual 打印手工开池的做法。")
    print("")
    if ok:
        rows = pool.session_view(target, policy, pl)
        entry = len(pool.claims_of(pl, policy["entry_role"]))
        scribe = len(pool.claims_of(pl, policy["scribe_role"]))
        print("当前：%d/%d 条认领；入口 %d 条，书记员 %d 条；待转正的许可 %d 条。" % (
            len(pl["claims"]), plan["total"], entry, scribe, len(pl["permits"])))
        no_desk = [r["name"] for r in rows if not r["desk"]]
        if no_desk:
            print("没有记下桌面会话 ID 的：%s（终端里开的会话没有，桌面会话应该有）" % "、".join(no_desk))
        mixed = pool.mixed_modes(rows)
        if mixed:
            print("权限模式和多数会话不同类的：%s" % "、".join(mixed))
        if len(pl["claims"]) >= plan["total"] and entry == 1 and scribe == 1 and not pl["permits"]:
            print("会话池已开齐。接着运行 setup.py check --target <项目目录> --live。")
    else:
        print("读不出 .claude/state/pool.json。")
    return 0


def feishu_gaps(target, seqs):
    """What the project still lacks on the Feishu side, read from feishu/ids.json: the project
    space binding, then the knowledge space, then the node skeleton."""
    ids = load_json_file(os.path.join(target, "feishu", "ids.json")) or {}
    meegle, wiki = ids.get("meegle") or {}, ids.get("wiki") or {}
    if not (meegle.get("bound_at") and meegle.get("types")):
        return ["飞书项目空间还没绑定：运行 setup.py feishu --target <项目目录> --live，核对后加 --apply"]
    if not wiki.get("space_id"):
        return ["还没有知识空间：运行 setup.py feishu --target <项目目录> --live --apply，没有同名的知识空间时它会新建一个"]
    nodes = wiki.get("nodes") or {}
    missing = [k for k in orgspec.OrgSpec().node_keys(seqs) if not (nodes.get(k) or {}).get("node_token")]
    if missing:
        return ["知识库节点还缺 %d 个（%s）：书记员用 ledger.py ensure-node all 建齐" % (
            len(missing), "、".join(missing[:6]) + ("等" if len(missing) > 6 else ""))]
    return []


def enabled_sequences(target):
    """The sequences init enabled in this project, from its manifest; None (every sequence) when unknown."""
    m = load_json_file(os.path.join(project_paths.runtime_dir(target), "manifest.json")) if target else None
    return (m or {}).get("sequences") or None


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
             "> 由 `setup.py feishu` 生成。插件按含义处理的类型、字段、角色和状态写在约定文件里（`.claude/agent-org/orgspec/contract.json`）；"
             "这里列每个槽位对到空间里的哪个对象。流程、节点和必填项不在这里：它们以空间为准，读出来的结构在 `feishu/space.md`。", ""]
    if switch:
        lines += switch_md(switch)
    lines += ["## 绑定结果", "", space.binding_md(contract, snap or {}, space.binding_of(ids)), ""]
    lines += ["## 要处理的问题", ""]
    if problems is None:
        lines += ["还没读过空间，没法核对：先跑 `setup.py feishu --live`。", ""]
    else:
        lines += [orgspec.md_table(["对象", "问题", "怎么办"], problems) if problems else "无。", ""]
        if problems:
            lines += ["空间由模板建出来，缺的东西要在模板里补：把上表报给模板维护者，模板更新后重建或补齐空间，再重跑 `setup.py feishu --live`。"
                      "空间里已经有、只是名字不同的，用 `--map <类型槽位>=<空间里的类型名>` 或 `--map <类型槽位>.<字段槽位>=<空间里的字段名>` 指认。", ""]
    if notes or changes:
        lines += ["## 提醒", "", bullets(list(notes) + ["空间结构和上次读到的不一样：%s" % c for c in changes]), ""]
    rows = []
    nodes = (ids.get("wiki") or {}).get("nodes") or {}
    renames = False
    for key in spec.node_keys(seqs):
        head, _, seq = key.partition("/")
        title = spec.term("node", head) + ("/" + spec.term("seq", seq) if seq else "")
        n = nodes.get(key)
        if not n:
            rows.append([title, "没有", "书记员建节点：`ledger.py ensure-node %s`" % key])
        elif seq and n.get("title", "").split("/")[-1] != spec.term("seq", seq):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("seq", seq)])
        elif not seq and n.get("title", "").split("/")[0] != spec.term("node", head):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("node", head)])
    lines += ["## 知识库节点", "", orgspec.md_table(["节点", "现状", "要做的"], rows) if rows else "无差异。", ""]
    if any(r[1] == "没有" for r in rows):
        lines += ["缺的节点可以一次建齐：`ledger.py ensure-node all`，先预览，再加 `--apply`。它先建一级节点，再建各序列的节点；"
                  "知识空间里已经有同名节点的直接用，不再建。要先有知识空间：`setup.py feishu --live --apply` 找不到同名的知识空间就新建一个。", ""]
    if renames:
        lines += ["命令行没有给知识库节点改名的子命令；节点标题跟着节点文档的一级标题走。先读一遍节点文档：是空的，书记员可以写入只有新标题的正文："
                  "`lark-cli docs +update --doc <节点链接> --command overwrite --doc-format markdown --content '# <新标题>' --as user`；"
                  "有内容的，请研究员在网页上改标题。", ""]
    lark = ids.get("lark") or {}
    wiki = ids.get("wiki") or {}
    readback = [
        ["project_key", "有" if m.get("project_key") else "缺", "跑 `setup.py feishu --live --apply`：按 `spec/task.json` 里的空间名精确查找"],
        ["simple_name", "有" if m.get("simple_name") else "缺", "同上；工作项链接要用它"],
        ["研究员的 open_id", "有" if lark.get("researcher_open_id") else "缺", "同上：从 `lark-cli whoami` 读；没有它，飞书私聊监听不启动"],
        ["知识空间 space_id", "有" if wiki.get("space_id") else "缺", "先在已有的知识空间里找（名字可能不一样），确认没有再建，再重跑 `setup.py feishu --live --apply`"],
    ]
    lines += ["## 读回", "", orgspec.md_table(["项", "现状", "怎么补"], readback), ""]
    return "\n".join(lines)


def cli_json(cmd, timeout=60):
    code, out, err = run(cmd, timeout)
    if code != 0:
        raise SetupError("%s 失败（退出码 %s）：%s" % (" ".join(cmd[:4]), code, (err or out or "")[-300:]))
    try:
        return json.loads(out)
    except ValueError:
        raise SetupError("%s 的输出不是 JSON" % " ".join(cmd[:4]))


def search_hits(data, wanted):
    """The projects in a `meegle project search` answer, and those whose name, simple_name or
    project_key equals wanted."""
    projects = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(projects, list):
        projects = [d for d in ledger._walk(data) if isinstance(d, dict) and d.get("project_key")]
    projects = [p for p in projects if isinstance(p, dict)]
    return projects, [p for p in projects if wanted in (p.get("name"), p.get("simple_name"), p.get("project_key"))]


def find_project(data, wanted, fix=None):
    """The one project in `meegle project search` whose name, simple_name or project_key equals wanted."""
    projects, hits = search_hits(data, wanted)
    if len(hits) != 1:
        cands = "；".join("%s（simple_name %s，project_key %s）" % (p.get("name"), p.get("simple_name"), p.get("project_key"))
                          for p in projects or []) or "无"
        raise SetupError("按“%s”找到 %d 个飞书项目空间，要正好一个。候选：%s。%s" % (wanted, len(hits), cands, fix or (
            "把准确的 simple_name 或 project_key 写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply"
            "（spec/task.json 跟着更新；手改过它的，init 会写成 spec/task.json.new，对照合并），再重跑这条命令")))
    return hits[0]


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


def optional_read(what, fn):
    """Run one optional read-back step; a failure becomes a reminder, not an abort."""
    try:
        return fn()
    except SetupError as e:
        print("提醒：没能读回%s：%s" % (what, e))
        return None


def old_space_items(spec, project_key, old):
    """What a space the project is leaving still holds, for the types its binding had: (types
    with any work item, open work items as lines, types that could not be checked)."""
    held, open_items, unchecked = [], [], []
    stop = ledger.system_states(spec)
    for wid, t in (old.get("types") or {}).items():
        tkey = t.get("type_key")
        if not tkey:
            continue
        name = spec.term("wi", wid) if wid in spec.terms["wi"] else t.get("name") or wid
        done = [s["name"] for slot, s in ((old.get("states") or {}).get(wid) or {}).items() if slot == "archived" and s.get("name")]

        def query(mql):
            return ledger.query_rows(cli_json(["meegle", "workitem", "query", "--project-key", project_key,
                                               "--mql", mql, "--format", "json"]))
        try:
            rows = query("SELECT `work_item_id`, `name`, `work_item_status` FROM `%s`.`%s` WHERE %s LIMIT 20" % (
                project_key, tkey, ledger._not_in(done + stop)))
            anything = rows or query("SELECT `work_item_id` FROM `%s`.`%s` LIMIT 1" % (project_key, tkey))
        except (SetupError, ledger.LedgerError) as e:
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
    return "打开模板 %s，点“使用模板”，空间名填“%s”，其余用默认值" % (url, name)


def read_back(spec, contract, ids, task_feishu, maps, switch=None, old_snap=None, create=False):
    """Find the project's space, read its structure, bind the contract to it, and read open_id,
    space_id and node titles. Returns (snapshot, problems, notes, changes since the last
    snapshot). The space the task file names is looked up every time: a name can come to mean
    another space, a key cannot. When it is not the space bound last time, the old binding is
    dropped and `switch` gets both spaces and what the old one still holds."""
    m = ids["meegle"]
    wanted = str(task_feishu.get("meegle_space") or "").strip() or m.get("project_key")
    if not wanted:
        raise SetupError("ids.json 里没有 project_key，研究任务里也没写飞书项目空间：把空间名写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply，再重跑这条命令")
    data = cli_json(["meegle", "project", "search", "--project-key", wanted, "--format", "json"])
    if not search_hits(data, wanted)[1] and not m.get("project_key"):
        raise SetupError("没有叫“%s”的飞书项目空间，什么都没写。用模板建一个：%s；建好以后重跑这条命令。" % (
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
    snap, binding, problems, notes = read_space(
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
    if want_wiki and (not wiki.get("space_id") or wiki.get("space_name", want_wiki) != want_wiki):
        data = optional_read("知识空间", lambda: cli_json(
            ["lark-cli", "wiki", "+space-list", "--as", "user", "--page-all", "--page-limit", "0"], 120))
        hits = list({d["space_id"]: d for d in ledger._walk(data or {})
                     if d.get("name") == want_wiki and d.get("space_id")}.values())
        if len(hits) == 1:
            if wiki.get("space_id") and hits[0]["space_id"] != wiki["space_id"]:
                print("提醒：知识空间换成了“%s”，原来记下的节点作废，书记员在新空间里重建" % want_wiki)
                wiki.pop("nodes", None)
            wiki.update({"space_id": hits[0]["space_id"], "space_name": hits[0]["name"]})
        elif data is not None and not hits and create:
            made = optional_read("新建知识空间", lambda: cli_json(
                ["lark-cli", "wiki", "+space-create", "--name", want_wiki, "--as", "user"], 120))
            new = [d for d in ledger._walk(made or {}) if d.get("space_id")]
            if new:
                wiki.pop("nodes", None)
                wiki.update({"space_id": new[0]["space_id"], "space_name": want_wiki})
                print("知识空间：没有叫“%s”的，已新建。" % want_wiki)
            elif made is not None:
                print("提醒：新建知识空间没有拿到 space_id。先在网页上看“%s”建出来没有，再重跑这条命令：已经建出来的会被找到并沿用。" % want_wiki)
        elif data is not None and not hits:
            print("知识空间：翻完全部分页，没有叫“%s”的；加 --apply 时新建一个。" % want_wiki)
        elif len(hits) > 1:
            print("提醒：有 %d 个知识空间叫“%s”（space_id：%s），没有自动选；把要用的那个写进 feishu/ids.json 的 wiki.space_id，再重跑" % (
                len(hits), want_wiki, "、".join(str(h["space_id"]) for h in hits)))
    for key, n in (wiki.get("nodes") or {}).items():
        if n.get("node_token"):
            got = optional_read("节点 %s 的标题" % key,
                                lambda tok=n["node_token"]: cli_json(["lark-cli", "wiki", "+node-get", "--node-token", tok, "--as", "user"]))
            title = ledger._find(got or {}, ("title",))
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
            lines += ["没办完的（每种类型最多列 20 条）：", "", bullets(switch["open"]), ""]
    if switch["unchecked"]:
        lines += ["没查成旧空间里的%s有没有工作项。确认旧空间已经删了，或者里面的工作项可以留在原处，写入时加 `--leave-old-items`。"
                  % "、".join(switch["unchecked"]), ""]
    if not switch["held"] and not switch["unchecked"]:
        lines += ["旧空间里没有工作项，可以直接写入。" if switch["bound_types"] else
                  "旧绑定一种类型都没绑上，台账没在旧空间里记过东西，可以直接写入。", ""]
    lines += ["写入时 `feishu/ids.json` 的 `previous_spaces` 记下旧空间和换出的时间，本地记录里旧的工作项 ID 还能查到属于哪个空间。"
              "旧空间留不留由研究员决定，删空间只能研究员本人做。", ""]
    return lines


def feishu(target, maps, live, apply, leave_old=False):
    """Bind the project to its space: find the space, read its structure, fill the contract's
    slots, check the flows. Writes feishu/ids.json, space.json, space.md and binding.md, and
    org/phases.md from the flows it read."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    whole = load_json_file(os.path.join(target, "spec", "task.json")) or {}
    task = dict(whole.get("feishu") or {})
    task.setdefault("wiki_space", whole.get("name"))          # the knowledge space is named after the project
    try:
        maps = space.parse_maps(contract, maps)
    except space.SpaceError as e:
        raise SetupError(str(e))
    fd_dir = os.path.join(target, "feishu")
    ids_path = os.path.join(fd_dir, "ids.json")
    raw = load_json_file(ids_path) or {}
    ids, notes = normalise_ids(raw)
    snap = load_json_file(os.path.join(fd_dir, "space.json"))
    problems, space_notes, changes, switch = None, [], [], {}
    if live:
        try:
            snap, problems, space_notes, changes = read_back(spec, contract, ids, task, maps, switch, snap, create=apply)
        except SetupError:
            raise
        except Exception as e:                       # an output shape nobody expected
            raise SetupError("读回时遇到没见过的输出（%s: %s）；什么都没写" % (type(e).__name__, e))
        if switch and apply and not leave_old and (switch["held"] or switch["unchecked"]):
            why = ("旧空间“%s”里还有工作项（%s）" % (switch["old"].get("space_name", ""), "、".join(switch["held"]))
                   if switch["held"] else "没查成旧空间里的%s有没有工作项" % "、".join(switch["unchecked"]))
            raise SetupError(why + "，什么都没写。插件搬不了工作项，换空间以后它们留在旧空间，台账不再读写。先去掉 --apply 预览，"
                             "看 binding.md 的“换空间”一节；研究员确认这些工作项可以留在旧空间以后，再加 --leave-old-items 写入")
    else:
        moved = ledger.other_space({"feishu": task}, ids.get("meegle") or {})
        if moved:
            raise SetupError("任务文件里的飞书项目空间是“%s”，上次绑定的是“%s”：加 --live 从空间读一遍再绑定" % (
                moved, ids["meegle"]["space_name"]))
        if snap is not None:
            binding, problems = space.bind(contract, snap, space.binding_of(ids), maps)
            more, space_notes = space.check(contract, snap, binding)
            problems += more
            space.apply_binding(ids, contract, binding)
        elif maps:
            raise SetupError("还没读过空间，--map 没法核对：先跑 setup.py feishu --live")
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
    files = {ids_path: dump_json(ids),
             binding_path: binding_doc(spec, contract, ids, snap, problems, space_notes, changes, switch, enabled_sequences(target))}
    if snap is not None:
        files[os.path.join(fd_dir, "space.json")] = space.dump_json(snap)
        files[os.path.join(fd_dir, "space.md")] = space.to_markdown(snap, contract, space.binding_of(ids)) + "\n"
        if not blank:
            files[os.path.join(target, "org", "phases.md")] = phases_md(spec, snap, space.binding_of(ids))
    for p, content in files.items():
        current = read_text(p) if os.path.isfile(p) else None
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
        return 0
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
    if blank:
        print("一种类型都没绑上：这次只记下空间和 open_id，没写绑定时间，台账脚本仍然不读写飞书。")
    return 0


def check_template(target, name):
    """Check a space as a template: read its structure, fill the contract's slots, check the
    flows. Reads Feishu, writes nothing."""
    spec = orgspec.OrgSpec()
    contract = orgspec.Contract(spec)
    if not name:
        task = (load_json_file(os.path.join(target, "spec", "task.json")) or {}).get("feishu") or {} if target else {}
        name = str(task.get("template_space") or "").strip()
    if not name:
        raise SetupError("要核对哪个空间：用 --space 给出它的名字、simple_name 或 project_key")
    hit = find_project(cli_json(["meegle", "project", "search", "--project-key", name, "--format", "json"]), name,
                       "把空间的准确名字、simple_name 或 project_key 用 --space 传进来")
    snap, binding, problems, notes = read_space(contract, hit, {})
    held = []
    for wid, slot in binding["types"].items():
        mql = "SELECT `work_item_id` FROM `%s`.`%s` LIMIT 1" % (hit.get("project_key"), slot["type_key"])
        try:
            rows = ledger.query_rows(cli_json(["meegle", "workitem", "query", "--project-key", hit.get("project_key"),
                                               "--mql", mql, "--format", "json"]))
        except (SetupError, ledger.LedgerError) as e:
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


# --------------------------------------------------------------- login
LOGIN_WAIT = 300
MEEGLE_HOSTS = {"feishu": "project.feishu.cn", "lark": "project.larksuite.com"}


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


def needs_login():
    """(tools whose login is gone, the meegle host this tenant uses)."""
    out = []
    code, text, _ = run(["lark-cli", "auth", "status"])
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {}
    if not lark_user_ready(doc)[0]:
        out.append("lark-cli")
    code, _, _ = run(["meegle", "auth", "status"])
    if code != 0:
        out.append("meegle")
    return out, MEEGLE_HOSTS.get(doc.get("brand"), MEEGLE_HOSTS["feishu"])


def login_state():
    """(a directory of this user's own under /tmp, the file of pending logins). lark-cli auth
    qrcode only writes under the current directory, /tmp or ~/files. Device codes stay in that
    file, readable by the user only, and go when the login ends."""
    d = "/tmp/agent-org-login-%d" % os.getuid()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise SetupError("%s 不是只有本人能读写的目录，登录用的临时文件不往里放：删掉它再重试" % d)
    return d, os.path.join(d, "pending.json")


def start_login(cli, host):
    """Start a device-code login: {url, expires_at, the codes the poll needs}. What the tool
    prints goes nowhere else."""
    if cli == "lark-cli":
        cmd, keys, links = ["lark-cli", "auth", "login", "--no-wait", "--json"], ("device_code",), ("verification_url",)
    else:
        cmd = ["meegle", "auth", "login", "--device-code", "--phase", "init", "--host", host, "--format", "json"]
        keys, links = ("device_code", "client_id"), ("verification_uri_complete", "verification_uri")
    code, out, _ = run(cmd, 60)
    try:
        doc = json.loads(out) if code == 0 else {}
    except ValueError:
        doc = {}
    url = next((doc[k] for k in links if doc.get(k)), None) if isinstance(doc, dict) else None
    if not url or not all(doc.get(k) for k in keys):
        raise SetupError("%s 的设备码登录没发起成（退出码 %s）：研究员在终端里运行它的 auth login 看原因" % (cli, code))
    until = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=int(doc.get("expires_in") or 600))
    return dict({k: doc[k] for k in keys}, url=url, expires_at=until.isoformat(timespec="seconds"))


def poll_login(cli, info, seconds):
    """Wait for the researcher to authorise; the answer is read back from auth status, not from here."""
    if cli == "lark-cli":
        run(["lark-cli", "auth", "login", "--device-code", info["device_code"]], seconds)
    else:
        run(["meegle", "auth", "login", "--device-code", "--phase", "poll", "--device-code-value", info["device_code"],
             "--client-id", info["client_id"], "--expires-in", str(max(int(seconds), 5)), "--interval", "5", "--format", "json"],
            seconds + 10)


def save_pending(path, pending):
    if not pending:
        if os.path.lexists(path):
            os.remove(path)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(pending, f)


def login(wait=False, timeout=LOGIN_WAIT):
    """Log meegle and lark-cli in by device code: this starts the login and prints a QR code
    and a link for the researcher; `--wait` polls until he has authorised. The session never
    sees a password, a verification code or a token, and no device code is printed."""
    d, path = login_state()
    pending = load_json_file(path) or {}
    left = lambda info: (datetime.datetime.fromisoformat(info["expires_at"]) - datetime.datetime.now().astimezone()).total_seconds()
    if not wait:
        need, host = needs_login()
        if not need:
            save_pending(path, {})
            print("lark-cli 和 meegle 都已登录，不用再登录。")
            return 0
        for cli in need:
            info = start_login(cli, host)
            png = os.path.join(d, "%s.png" % cli)
            code, _, _ = run(["lark-cli", "auth", "qrcode", info["url"], "-o", png])
            info["qrcode"] = png if code == 0 and os.path.isfile(png) else None
            pending[cli] = info
            save_pending(path, pending)
            print("%s：请研究员扫码或打开链接授权，%d 分钟内有效。" % (cli, max(left(info), 0) // 60))
            print("  二维码图片：%s" % (info["qrcode"] or "没生成出来，用链接"))
            print("  链接：%s" % info["url"])
        print("把二维码图片和链接发给研究员。他授权以后运行 setup.py login --wait，读回登录状态。")
        return 0
    if not pending:
        print("没有在等的登录：先运行 setup.py login。")
        return 2
    deadline = time.monotonic() + timeout
    for cli, info in pending.items():
        budget = min(deadline - time.monotonic(), left(info))
        if budget > 0:
            poll_login(cli, info, budget)
    still, _ = needs_login()
    code = 0
    for cli in list(pending):
        info = pending[cli]
        if cli not in still:
            print("%s：已登录。" % cli)
        elif left(info) <= 0:
            print("%s：授权超时，这次的二维码作废；重新运行 setup.py login。" % cli)
            code = 1
        else:
            print("%s：还没授权，二维码还有 %d 分钟有效；研究员授权以后再运行 setup.py login --wait。" % (cli, left(info) // 60))
            code = 1
            continue
        if info.get("qrcode") and os.path.isfile(info["qrcode"]):
            os.remove(info["qrcode"])
        del pending[cli]
    save_pending(path, pending)
    return code


# ----------------------------------------------------------------- env
ENV_HEAD = "# agent-org：由 feishu/ids.json 生成"
ENV_OURS = re.compile(r"^\s*(?:export\s+)?(?:FEISHU_PROJECT_URL|FEISHU_WIKI_URL)\s*=")


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
        missing.append("知识空间的链接还拼不出来：租户域名在书记员建好第一个节点以后才记进 feishu/ids.json")
    return out, missing


def env_text(current, values):
    """A .env with our lines set. Every other line stays as it was and where it was."""
    kept = [line for line in (current or "").split("\n") if line.strip() != ENV_HEAD and not ENV_OURS.match(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept + ([""] if kept else []) + [ENV_HEAD] + ["%s=%s" % kv for kv in values.items()]) + "\n"


def read_env(target):
    path = os.path.join(target, ".env")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def env_gaps(target):
    """Problems of the project's .env against feishu/ids.json; other lines are not looked at."""
    values, missing = env_values(target)
    if missing:
        return ["还写不了 .env：%s" % "；".join(missing)]
    current = read_env(target)
    have = dict(line.strip().split("=", 1) for line in (current or "").split("\n")
                if line.strip().startswith(tuple(values)) and "=" in line)
    if have != values:
        return ["项目目录的 .env 还没有飞书项目和知识空间的链接，或者和 feishu/ids.json 对不上：运行 setup.py env --target <项目目录> --apply"]
    return []


def env_cmd(target, apply):
    """Write the two links into the project's .env. Only these lines are shown or changed."""
    values, missing = env_values(target)
    if missing:
        print("出错：%s。" % "；".join(missing))
        return 1
    current = read_env(target)
    new = env_text(current, values)
    state = "新建" if current is None else ("相同" if new == current else "更新")
    print("[%s] .env" % state)
    for kv in values.items():
        print("  %s=%s" % kv)
    if not apply:
        print("这是预览。加 --apply 写入；.env 里的其他行不动，也不显示。")
        return 0
    if state == "新建":
        fd = os.open(os.path.join(target, ".env"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
    elif state == "更新":
        with open(os.path.join(target, ".env"), "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(new)
    print("已写入 .env。" if state != "相同" else ".env 已经是这样，没有改。")
    return 0


def load_json_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        return json.loads(read_text(path))
    except ValueError:
        raise SetupError("%s 不是合法 JSON" % path)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="初始化智能体组织的项目目录")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("precheck")
    p = sub.add_parser("init")
    p.add_argument("--target", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--sequences")
    p.add_argument("--runtime", choices=("claude", "codex"))
    p.add_argument("--apply", action="store_true")
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
    p = sub.add_parser("check")
    p.add_argument("--target", required=True)
    p.add_argument("--live", action="store_true", help="只读回读飞书：结构和绑定、知识库节点、首条研究任务和它的主文档")
    p.add_argument("--sessions", metavar="FILE", help="客户端 list_sessions 的结果存成的 JSON 文件：核对会话还在、没归档、都在项目分区里")
    p = sub.add_parser("login")
    p.add_argument("--wait", action="store_true", help="研究员授权以后：等到登录完成，读回登录状态")
    p.add_argument("--timeout", type=int, default=LOGIN_WAIT, help="--wait 最多等多少秒")
    p = sub.add_parser("env")
    p.add_argument("--target", required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("pool")
    p.add_argument("--target", required=True)
    p.add_argument("--manual", action="store_true", help="研究员的会话开不了会话时：打印手工开池的做法")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "precheck":
            return precheck()
        if args.cmd == "init":
            target = os.path.abspath(args.target)
            if os.path.realpath(target) in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(os.sep)):
                raise SetupError("不在家目录或根目录里初始化：换一个只放这个项目的目录")
            plan = build_plan(target, args.task, args.sequences, args.runtime)
            acts = actions_for(target, plan)
            if plan.manifest.get("runtime") == "codex":
                import codex_setup
                return codex_setup.execute(target, plan, acts, args.apply)
            report(target, plan, acts, args.apply)
            if args.apply:
                apply_plan(target, plan, acts)
            return 0
        if args.cmd == "feishu":
            return feishu(os.path.abspath(args.target), args.map, args.live, args.apply, args.leave_old_items)
        if args.cmd == "template":
            return check_template(os.path.abspath(args.target) if args.target else None, args.space)
        if args.cmd == "check":
            if os.path.isfile(os.path.join(args.target, ".agent-org", "manifest.json")):
                import codex_setup
                return codex_setup.check(os.path.abspath(args.target), sys.modules[__name__])
            return check(os.path.abspath(args.target), args.live, args.sessions)
        if args.cmd == "login":
            return login(args.wait, args.timeout)
        if args.cmd == "env":
            return env_cmd(os.path.abspath(args.target), args.apply)
        if args.cmd == "pool":
            if os.path.isfile(os.path.join(args.target, ".agent-org", "manifest.json")):
                import codex_runtime
                print(dump_json(codex_runtime.Pool(args.target).plan()))
                return 0
            return pool_checklist(os.path.abspath(args.target), args.manual)
    except (SetupError, ValueError, OSError) as e:
        print("出错：%s" % e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
