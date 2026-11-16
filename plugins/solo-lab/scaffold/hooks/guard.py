#!/usr/bin/env python3
"""PreToolUse guard for a project set up by the solo-lab plugin.

Reads the hook input JSON on stdin and answers Bash, Monitor, Edit, Write,
NotebookEdit, Agent and AskUserQuestion calls, and the desktop app's terminal and turn
wrap-up tools (mcp__<server>__run_in_terminal, mcp__<server>__wrap_up), based on
policy.json and lark-risk.json in this directory. Three kinds of checks:
- hard rules guard data: only the one claimed scribe session writes Feishu (and nobody
  while two scribes are claimed), the evaluation protocol's files stay read-only, high-risk and
  --yes Feishu commands are the researcher's, the hooks, settings, role definitions
  and claims under .claude/ are not edited, Agent calls carry no name, pool sessions
  do not dispatch the scribe with the Agent tool, do not switch branches, stash or
  pull in the shared directory, and claim, pre-register and release only as allowed.
  Writes to the evaluation protocol's files and to .claude/ are denied in every
  session; the other hard rules ask in the researcher's own session (deny there too
  under bypassPermissions or dontAsk, where no prompt can appear). The paths that only
  init and the plugin's scripts write (policy.json owned_paths with nobody as owner: the hooks,
  settings, role definitions, the local state with the claims and gates.json, spec/task.json)
  are also denied to shell commands that overwrite, move or delete them (check_owned_writes).
- soft rules are the dispatch norms: they never block, they add a reminder to the
  model's context and a line to .claude/logs/guard.jsonl.
- irreversible actions (1.0.4): the categories of the irreversible list (policy.json
  "irreversible", from orgspec/irreversible.json) that a shell command or a path shows.
  One needs a release in the local decision state .claude/state/gates.json, which only
  ledger.py run by the scribe writes: a decision the researcher made whose option picked
  lists the category, with a qualifier that covers the command (compared word by word: hosts with
  the host of a remote argument, paths with the files the command changes). Without one, a pool
  session is denied (the reason says how to write the decision package) and the researcher's own
  session asks. A decision still waiting freezes what its package lists under 冻结的动作
  (categories, command prefixes): those are stopped in the same way until it is decided (a frozen
  qualifier matches more loosely than a release: any of its words in the command), and releases
  that overlap what it freezes are held back meanwhile. Protected paths (the task file's
  protected_paths) are never released by a decision: deleting, moving, overwriting them or
  changing their permissions with a shell command is denied in a pool session, asked in the
  researcher's own (and in a quality session, as an edit is). A release lets the call through
  to Claude Code's own permission checks; guard.jsonl records every category met under
  irreversible, and the decisions' IDs under released_by when the call is not denied.
A turn another agent session started (turn_origin agent, written by inbox.py) has nobody
to answer a question, so in a pool session the wrap-up keeps its recap and loses ask,
actions and blocked (allow with updatedInput), and AskUserQuestion is denied. The one
exception: the entry may ask in the turn that registered a decision that jumps the queue
(ask_ok in its status file). guard.jsonl records each case under agent_turn_gate:
stripped, denied or allowed_urgent, and for every wrap-up whether it asked something (asked)
in whose turn (turn_origin). The researcher's turns and the app's are left alone.
Anything unexpected in a hard check exits 2, which blocks the call: Claude Code
treats other non-zero exits as non-blocking, so failing open must be avoided
explicitly. Settings deny and ask rules still apply after this hook answers allow.
"""
import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feishu_cmd as fc  # noqa: E402
import pool  # noqa: E402

TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
POLICY_VERSION = 6


def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def matches(rel, patterns):
    return any(re.fullmatch(glob_regex(p), rel) for p in patterns)


class Verdict:
    ORDER = {None: 0, "allow": 1, "ask": 2, "deny": 3}

    def __init__(self, decision=None, reason="", gate=None, note=""):
        self.decision, self.reasons, self.updated = decision, [reason] if reason else [], None
        self.gate = gate                        # agent_turn_gate in guard.jsonl: stripped, denied, allowed_urgent
        self.notes = [note] if note else []     # for the model's context; not a soft-rule hit
        self.irreversible = []                  # guard.jsonl irreversible: each category of the list met
        self.released = []                      # “放行：…” lines, shown only when the call is not denied

    def merge(self, other):
        if self.ORDER[other.decision] > self.ORDER[self.decision]:
            self.decision = other.decision
        self.reasons.extend(r for r in other.reasons if r not in self.reasons)
        self.gate = self.gate or other.gate
        self.notes.extend(n for n in other.notes if n not in self.notes)
        self.irreversible.extend(other.irreversible)
        self.released.extend(n for n in other.released if n not in self.released)
        return self

    def context(self):
        """What goes into the model's context: the notes, and the releases unless the call is denied."""
        return self.notes + (self.released if self.decision != "deny" else [])


def refuse(who, data, reason):
    """Ask in the researcher's own session; deny everywhere else, and wherever no prompt can appear."""
    if not who.researcher:
        return Verdict("deny", reason)
    mode = data.get("permission_mode")
    if mode in pool.BYPASS_MODES:
        return Verdict("deny", "%s 当前是 %s 模式，确认框弹不出来：请研究员在自己的终端里运行。" % (reason, mode))
    return Verdict("ask", reason)


def project_root(data):
    return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())


def resolve_role(data, policy):
    """(role, policy entry, researcher's session?) for the caller; see pool.identify."""
    who = pool.identify(data, policy, project_root(data))
    return who.role, who.pol, who.researcher


# --------------------------------------------------------------- Bash
def content_of(inv, flags):
    """Return the text passed to --content (inline, @file or stdin), or None."""
    value = fc.last(flags, "--content")
    if value is None:
        return None
    if value == "-":
        return inv.stdin            # its own here-document or here-string, or what a pipe feeds it
    if isinstance(value, str) and value.startswith("@"):
        return fc.read_script(value[1:], inv.cwd)      # regular files up to 2 MB only: a FIFO would hang the hook
    return value


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line
    return ""


def tag_comment(args, tag):
    """Return new args with the session tag prefixed to the comment, or None."""
    new = list(args)
    for i, a in enumerate(new):
        if a == "--content" and i + 1 < len(new):
            if not new[i + 1].lstrip().startswith(tag):
                new[i + 1] = "%s %s" % (tag, new[i + 1])
            return new
        if a.startswith("--content="):
            value = a.split("=", 1)[1]
            if not value.lstrip().startswith(tag):
                new[i] = "--content=%s %s" % (tag, value)
            return new
    for i, a in enumerate(new):
        if a == "--params" and i + 1 < len(new):
            try:
                obj = json.loads(new[i + 1])
            except ValueError:
                return None
            if isinstance(obj, dict) and isinstance(obj.get("content"), str):
                if not obj["content"].lstrip().startswith(tag):
                    obj["content"] = "%s %s" % (tag, obj["content"])
                    new[i + 1] = json.dumps(obj, ensure_ascii=False)
            return new
    return new


def as_needed(policy, c):
    ident = policy["identity"]
    domain = c["pos"][0] if c["pos"] else ""
    if domain in ident["user_domains"]:
        return "user"
    if c["key"] in ident["bot_commands"]:
        return "bot"
    return None


AS_HINT = {"user": "知识库、文档和云空间的命令用研究员身份，带 --as user",
           "bot": "收发消息和订阅事件用机器人身份，带 --as bot"}


def check_lark(inv, c, who, policy, data, soft, rewritable):
    key, pol, name = c["key"], who.pol, who.label
    if c["help"]:
        return Verdict("allow", "查看帮助"), None
    if c["yes"]:
        return Verdict("deny", "带 --yes 的命令只能由研究员本人执行。不要自己加 --yes；把完整命令写进交付的内容。"), None
    if c["tier"] == "high-risk-write":
        return Verdict("deny", "%s 是 high-risk-write，只能由研究员本人执行。把完整命令写进交付的内容。" % key), None
    if c["dry_run"]:
        return Verdict("allow", "--dry-run 只预览"), None
    new_args = None
    need = as_needed(policy, c)
    if need and c["as"] != need:
        if c["tier"] == "read":
            soft.append("%s：%s。" % (key, AS_HINT[need]))
        elif c["as"] is None and rewritable:
            new_args = list(inv.args) + ["--as", need]
        else:
            return Verdict("deny", "%s 是写命令，%s；身份不对，写出来的东西会落在别人名下。" % (key, AS_HINT[need])), None
    if c["tier"] == "read":
        return Verdict("allow", "只读命令"), new_args
    if key == "docs +update" and fc.last(c["flags"], "--command") == "append":
        text = content_of(inv, c["flags"])
        if text is None:
            soft.append("看不到要追加的正文（比如从文件重定向传入），没法核对第一行的会话标签 %s；书记员用 ledger.py append。" % pol["tag"])
        elif pol["tag"] not in first_line(text):
            soft.append("追加的交接记录第一行要带会话标签 %s；书记员用 ledger.py append。" % pol["tag"])
    if key in pol.get("lark_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name)), new_args
    return refuse(who, data, "（%s）不能执行写命令 %s。要写进飞书的内容先写成草稿，请书记员登记。" % (name, key)), new_args


def check_meegle(inv, c, who, data, soft, rewritable):
    key, pol, name = c["key"], who.pol, who.label
    if c["help"]:
        return Verdict("allow", "查看帮助"), None
    if c["tier"] == "admin":
        return Verdict("deny", "%s 属于登录、诊断上报或配置类命令，只能由研究员本人执行。" % key), None
    if c["dry_run"]:
        return Verdict("allow", "--dry-run 只预览"), None
    if c["tier"] == "read":
        return Verdict("allow", "只读命令"), None
    new_args = list(inv.args)
    if key == "comment add":
        tagged = tag_comment(new_args, pol["tag"])
        if tagged is None:
            soft.append("评论的 --params 不是合法 JSON，没法补会话标签 %s；请写在内容开头。" % pol["tag"])
        elif tagged != new_args and not rewritable:
            soft.append("评论开头缺会话标签 %s；命令不是单条简单命令，没法自动补，请在内容开头加上。" % pol["tag"])
        else:
            new_args = tagged
    if rewritable and "--envelope" not in c["flags"]:
        new_args.append("--envelope")
    updated = new_args if new_args != inv.args else None
    if key in pol.get("meegle_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name)), updated
    return refuse(who, data, "（%s）不能执行写命令 %s。工作项、字段、节点和文档只由书记员写；先写成草稿，请书记员登记。" % (name, key)), None


LEDGER_READS = {"", "get", "next", "query", "mql", "trace", "status", "-h", "--help"}
LEDGER_WRITE_WORDS = {"register", "revise", "reply", "confirm", "rollback", "update", "comment", "status-block", "append",
                      "archive", "relink", "ensure-node"}
_WORDS = "|".join(re.escape(w) for w in sorted(LEDGER_WRITE_WORDS, key=len, reverse=True))
CODE_WRITE = re.compile(r"(?<![\w-])['\"](?:%s)['\"](?!\s*:)|ledger\.py['\"]?\s+(?:-\S+\s+)*(?:%s)\b" % (_WORDS, _WORDS))
CODE_RUNS_LEDGER = re.compile(r"run_(?:path|module)\([^)]*ledger")
CODE_IMPORTS_LEDGER = re.compile(r"\bimport\s+ledger\b|\bfrom\s+ledger\s+import\b|import_module\(\s*['\"]ledger['\"]")
STRING_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n])*)\1""")
LITERAL_AFTER = re.compile(r"(?:\}|ledger\.py)\s+(?:-\S+\s+)*(?:%s)\s+(?:\{|#?\d|--apply)" % _WORDS)   # f"{L} archive {i}"
LITERAL_LEAD = re.compile(r"\s*(?:%s)\s+(?:#?\d{3,}|\{[^}]*\}|.*\s--apply\b)" % _WORDS)               # "archive 2106 --apply".split()
SHELL_SCRIPT = re.compile(r"^#!.*\b(?:sh|bash|zsh|dash|ksh)\b")
# Code can only start ledger.py through one of these (imports and runpy are checked separately).
CODE_EXECUTES = re.compile(r"subprocess|os\.(?:system|popen|exec|spawn)|\bpty\.spawn|Popen|check_output|check_call|getoutput"
                           r"|child_process|execSync|execFileSync|spawnSync|\bsystem\s*\(|\bexec\s*\(|\bqx\b|%x|IO\.popen|Open3|`")
PYTHON = fc.PYTHON_RE
SHELLS_RUN = set(fc.SHELLS) | {"csh", "tcsh"}
SOURCE = {"source", "."}
INLINE_CODE = {"perl", "ruby", "node", "php", "bun", "deno"}
SCRIPT_MODULES = {"cProfile", "profile", "pdb", "trace", "runpy"}
RUN_WRAPPERS = {"sudo", "doas", "chrt", "ionice", "taskset", "nohup", "time", "command", "exec", "caffeinate",
                "arch", "nice", "timeout", "env", "stdbuf", "xargs"} | fc.RUNNERS
GLOB_QUALIFIER = re.compile(r"(ledger[^\s;&|()<>'\"]*)\([^()\s]*\)")    # zsh: ledger.py(.)


def _is_ledger_script(tok):
    name = os.path.basename(tok)
    if name == "ledger.py":
        return True
    return any(ch in name for ch in "*?[") and fc.glob_names_ledger(name)


def _ledger_named(tok, rest):
    """tok runs ledger.py: the script itself, or a renamed copy (my_ledger.py) given a write subcommand."""
    if _is_ledger_script(tok):
        return True
    return os.path.basename(tok).endswith("ledger.py") and _subcommand(rest) in LEDGER_WRITE_WORDS


def _subcommand(args):
    """The ledger.py subcommand in the arguments after the script; --help before any -- makes it a read."""
    head = args[:args.index("--")] if "--" in args else args
    if "-h" in head or "--help" in head:
        return "--help"
    return next((a for a in args if not a.startswith("-")), "")


def _verdict(sub):
    return None if sub in LEDGER_READS else sub


def _writes_in_code(code):
    """Does Python (or other) source text run a ledger.py write? Imports of ledger count as writes.

    Only code that names ledger.py and can start a process counts (a data file such as
    ledger-open.json does not, nor a draft whose text mentions the command).
    A write word counts when it is quoted on its own as a list item ('register', not a
    'register': key), follows ledger.py, follows a {placeholder} inside a string and goes on
    with an argument (f"{L} archive {i}"), or starts a string that goes on with an ID or
    --apply ("archive 2106 --apply".split()). Full-line comments are skipped.
    """
    code = fc.code_lines(code)
    if CODE_IMPORTS_LEDGER.search(code):
        return True
    if "ledger.py" not in code or not CODE_EXECUTES.search(code):  # text about ledger.py written to a file
        return False
    if CODE_WRITE.search(code):
        return True
    return any(LITERAL_AFTER.search(m.group(2)) or LITERAL_LEAD.match(m.group(2))
               for m in STRING_LITERAL.finditer(code))


def _code_verdict(code, rest):
    """python -c CODE [args], or a script on stdin: CODE that runs ledger.py takes args as its subcommand."""
    if CODE_RUNS_LEDGER.search(code):
        return _verdict(_subcommand(rest)) if rest else "（代码里运行了 ledger.py）"
    return "（代码里调用了 ledger.py 的写命令）" if _writes_in_code(code) else None


class _Ctx:
    def __init__(self, bodies, cwd, depth):
        self.text, self.bodies, self.cwd, self.depth = "", bodies, cwd, depth
        self.assign = {}
        self.find_named = None      # inside a find: True when it selects ledger.py, so {} is the script


class _Stdin:
    """What a command reads on standard input, as far as the text shows."""

    def __init__(self, texts=(), ledger=False):
        self.texts = [t for t in texts if t is not None]
        self.ledger = ledger        # ledger.py itself: python3 - archive 1 < ledger.py, cat ledger.py | python3 -


def _substitute(payload, ctx):
    for name, value in ctx.assign.items():
        payload = payload.replace("${%s}" % name, value).replace("$%s" % name, value)
    return payload


def _script_text(path, ctx):
    full = fc._full(path, ctx.cwd)
    if full in fc.TRUSTED:
        return None
    return fc.read_script(path, ctx.cwd)


def _python_run(argv, ctx, stdin, via_xargs=False):
    kind, script, rest = fc.python_target(argv[1:])     # option clusters: -BW ignore, -IBc CODE, -Bm mod
    if kind == "code":
        return _code_verdict(script, rest)
    if kind == "module":
        return _module_verdict(script, rest)
    if kind == "stdin":
        if stdin.ledger:
            return _verdict(_subcommand(rest))
        for text in stdin.texts:
            found = _code_verdict(text, rest)
            if found:
                return found
        known = stdin.texts and not any(m in t for t in stdin.texts for m in ("$(", "`", "ledger", "$__SUB"))
        if not known and "ledger.py" in ctx.text and _subcommand(rest) in LEDGER_WRITE_WORDS:
            return _subcommand(rest)                   # python3 - archive 1 <<< "$(cat ledger.py)"
        return None
    if _ledger_named(script, rest):
        sub = _subcommand(rest)
        return "（xargs 传入的子命令）" if not sub and via_xargs else _verdict(sub)
    name = script.rsplit("/", 1)[-1]
    if ("$" in name or "`" in name) and "ledger.py" in ctx.text and _subcommand(rest) in LEDGER_WRITE_WORDS:
        return _subcommand(rest)                       # for f in .../ledger.py; do python3 $f archive ...
    text = _script_text(script, ctx)
    if text and _writes_in_code(text):
        return "（脚本 %s 里调用了 ledger.py 的写命令）" % script
    return None


def _module_verdict(module, rest):
    if module == "ledger" or module.endswith(".ledger"):
        return _verdict(_subcommand(rest))
    if module in SCRIPT_MODULES:
        for j, a in enumerate(rest):
            if _is_ledger_script(a):
                return _verdict(_subcommand(rest[j + 1:]))
            if a == "-m" and j + 1 < len(rest) and (rest[j + 1] == "ledger" or rest[j + 1].endswith(".ledger")):
                return _verdict(_subcommand(rest[j + 2:]))
            if module == "runpy" and (a == "ledger" or a.endswith(".ledger")):
                return _verdict(_subcommand(rest[j + 1:]))
    return None


def _pipe_out(argv, stdin, ctx):
    """(text, is ledger.py) that a command writes to a pipe: cat/head/tail of files or of their own
    standard input, echo/printf text."""
    if not argv:
        return None, False
    head = os.path.basename(argv[0])
    if head in ("cat", "head", "tail"):
        files = [a for a in argv[1:] if not a.startswith("-")]
        if not files:
            return "\n".join(stdin.texts) or None, stdin.ledger
        if any(_is_ledger_script(f) for f in files):
            return None, True
        return "\n".join(t for t in (_script_text(f, ctx) for f in files) if t) or None, False
    if head in ("echo", "printf"):
        return " ".join(a for a in argv[1:] if not a.startswith("-")), False
    return None, False


def _strip_substitutions(text, ctx):
    """Replace $(...) and `...` outside single quotes with placeholders; check what they run."""
    out, i, n, q, k = [], 0, len(text), None, 0
    while i < n:
        c = text[i]
        if q == "'":
            out.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if c == "'" and q is None:
            q = "'"
            out.append(c)
            i += 1
            continue
        if c == '"':
            q = None if q == '"' else '"'
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        inner = None
        if text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner, i = text[i + 2:j - 1], j
        elif c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            inner, i = text[i + 1:j], j + 1
        if inner is not None:
            found = _ledger_write(inner, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return None, found
            k += 1
            out.append("$__SUB%d" % k)
            continue
        out.append(c)
        i += 1
    return "".join(out), None


def ledger_write(command, cwd=None):
    """The ledger.py subcommand a shell command would run, unless it is a read.

    A guardrail against accidental writes by well-behaved agents, not a security
    boundary: enough obfuscation can always hide a command from a shell parser.
    Returns None when ledger.py is not run, or only with get/query/mql/trace/status,
    --help or no subcommand. Handled: comments, new lines, ; && || |, cd, shell
    keywords and functions, wrappers and project runners (env, timeout, nice,
    sudo, xargs, uv run ...), python option clusters, -c / -m (ledger, runpy,
    cProfile ...), scripts on stdin (here-documents, here-strings, < file, pipes),
    script files run by a shell, source, python or their own path, $(...) and
    backticks, eval and sh -c (with simple $VAR substitution), find -exec, other
    interpreters' inline code, and glob script names.
    """
    return _ledger_write(command, cwd, 0)


def _ledger_write(command, cwd, depth, bodies=None):
    if depth > 4:
        return "（嵌套太深）" if "ledger" in command else None
    text, new = fc.split_heredocs(command)
    ctx = _Ctx(dict(bodies or {}), cwd, depth)
    ctx.bodies.update(new)
    for body, quoted in new.values():                  # $(...) in an unquoted here-document runs
        if not quoted:
            for inner in fc.substitutions(body):
                found = _ledger_write(inner, cwd, depth + 1, ctx.bodies)
                if found:
                    return found
    text = GLOB_QUALIFIER.sub(r"\1", fc.strip_comments(text).replace("\\\n", " "))   # a comment ending in \ does not continue
    ctx.text = text                                    # still with the text of $(...): "ledger.py" in ctx.text
    text, found = _strip_substitutions(text, ctx)
    if found:
        return found
    try:
        tokens = fc.tokenize(text)
    except ValueError:
        return "（看不清的命令）" if "ledger" in text else None
    segs, piped = fc._segments(tokens)
    prev_out, prev_ledger = None, False
    for raw, is_piped in zip(segs, piped):
        docs, strings, infile = fc.seg_stdin(raw, ctx.bodies, ctx.cwd)
        texts = docs + strings
        from_ledger = bool(infile and _is_ledger_script(infile))
        if infile and not from_ledger:
            texts.append(_script_text(infile, ctx))
        if is_piped:
            texts.append(prev_out)
            from_ledger = from_ledger or prev_ledger
        stdin = _Stdin(texts, from_ledger)
        for tok in raw:
            if fc.ASSIGN_RE.match(tok):
                name, _, value = tok.partition("=")
                ctx.assign[name] = value
        seg, _ = fc._drop_redirects(raw)
        argv, wrappers = fc._unwrap(seg)
        if argv and argv[0] == "function":
            argv, more = fc._unwrap(argv[2:])
            wrappers = wrappers + more
        if argv and (" " in argv[0] or "\t" in argv[0]):        # env -S 'python3 -I' ...
            try:
                argv = shlex.split(argv[0]) + argv[1:]
            except ValueError:
                pass
        if argv and os.path.basename(argv[0]) == "cd":
            ctx.cwd = fc._cd(ctx.cwd, argv)
            prev_out, prev_ledger = None, False
            continue
        if argv and not (os.path.basename(argv[0]) == "find" or argv[0].startswith("-")):
            ctx.find_named = None
        found = _segment_write(argv, wrappers, ctx, stdin)
        if found:
            return found
        prev_out, prev_ledger = _pipe_out(argv, stdin, ctx)
    return None


def _direct_script(argv, ctx):
    """./x.sh, ./x.py, uv run x.py: read the file and check it as bash FILE or python FILE would."""
    path = argv[0]
    if "/" not in path and not os.path.isfile(os.path.join(ctx.cwd or ".", path)):
        return None
    text = _script_text(path, ctx)
    if not text or "\0" in text:
        return None
    first = text.split("\n", 1)[0]
    shell = path.endswith((".sh", ".bash", ".zsh")) or bool(SHELL_SCRIPT.match(first)) or (
        not first.startswith("#!") and "." not in os.path.basename(path))
    if shell:
        return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
    return "（脚本 %s 里调用了 ledger.py 的写命令）" % path if _writes_in_code(text) else None


def _segment_write(argv, wrappers, ctx, stdin):
    if not argv:
        return None
    head = os.path.basename(argv[0])
    via_xargs = any(os.path.basename(w) == "xargs" for w in wrappers)
    if via_xargs and stdin.texts:                         # echo archive 2106 | xargs python3 ledger.py
        argv = argv + " ".join(stdin.texts).split()

    def run_verdict(args):
        sub = _subcommand(args)
        if not sub and via_xargs:
            return "（xargs 传入的子命令）"
        return _verdict(sub)

    def check_texts(texts):
        for src in texts:
            found = _ledger_write(src, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return found
        return None

    if head == "eval":
        return _ledger_write(_substitute(" ".join(argv[1:]), ctx), ctx.cwd, ctx.depth + 1, ctx.bodies)
    if head in SHELLS_RUN:
        kind, value, _ = fc.shell_target(argv[1:])
        if kind == "command":
            return _ledger_write(_substitute(value, ctx), ctx.cwd, ctx.depth + 1, ctx.bodies)
        if kind == "script":
            text = _script_text(value, ctx)
            if text is not None:
                return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if not os.path.exists(fc._full(value, ctx.cwd)):  # written by a here-document earlier in this command
                return check_texts(b for b, _ in ctx.bodies.values())
            return None
        return check_texts(stdin.texts)
    if head in SOURCE and len(argv) > 1:
        text = _script_text(argv[1], ctx)
        if text is not None:
            return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
        if not os.path.exists(fc._full(argv[1], ctx.cwd)):
            return check_texts(b for b, _ in ctx.bodies.values())
        return None
    if head == "find" or (argv[0].startswith("-") and ctx.find_named is not None):
        if head == "find":                                # find bin -name ledger.py -execdir python3 {} <sub>
            ctx.find_named = fc.find_names_ledger(argv)
        else:                                             # the rest of a find split at ( or )
            ctx.find_named = ctx.find_named or fc.find_names_ledger(argv)
        for cmd in fc.find_clauses(argv):
            cmd = ["ledger.py" if a == "{}" and ctx.find_named else a for a in cmd]
            found = _ledger_write(shlex.join(cmd), ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return found
        return None
    if head in INLINE_CODE:
        code = fc.inline_code(head, argv[1:])
        if code is None:
            files = [a for a in argv[1:] if not a.startswith("-")][:1]
            code = _script_text(files[0], ctx) if files else ("\n".join(stdin.texts) or None)
        if code and _writes_in_code(code):
            return "（%s 的代码里调用了 ledger.py 的写命令）" % head
        return None
    if PYTHON.search(head):
        return _python_run(argv, ctx, stdin, via_xargs)
    if _ledger_named(argv[0], argv[1:]):                  # ./ledger.py <sub>, uv run ledger.py <sub>
        return run_verdict(argv[1:])
    j = next((k for k, w in enumerate(argv) if PYTHON.search(os.path.basename(w))), None)
    wrapped = head in RUN_WRAPPERS or any(os.path.basename(w) in RUN_WRAPPERS for w in wrappers)
    if j is not None and wrapped:                         # a wrapper with option values we do not know
        return _python_run(argv[j:], ctx, stdin, via_xargs)
    if argv[0].startswith("$"):                           # $PY .../ledger.py <sub>
        s = next((k for k, w in enumerate(argv) if _is_ledger_script(w)), None)
        if s is not None:
            return run_verdict(argv[s + 1:])
        if "ledger" in ctx.text:
            return next((w for w in argv[1:] if w in LEDGER_WRITE_WORDS), None)
        return None
    return _direct_script(argv, ctx)


POOL_SUBS = ("claim", "expect", "release")
GIT_SWITCHES = ("checkout", "switch", "stash", "rebase", "merge", "reset", "clean", "restore", "pull")
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
                     "--super-prefix", "--list-cmds"}
SHELL_NAMES = set(fc.SHELLS) | {"csh", "tcsh"}
IDENTITY = r"CLAUDE_CODE_(?:HOST_)?SESSION_ID"
IDENTITY_VARS = re.compile(
    r"\bunset\b[^;&|\n]*CLAUDE_CODE_"                                        # unset [-v] NAME, "NAME", NAME{,…}
    r"|\b(?:export|declare|typeset|readonly|local)\b[^;&|\n]*" + IDENTITY +  # export -n NAME, declare +x NAME
    r"|(?:^|\s)-u\s*['\"]?CLAUDE_CODE_|--unset[= ]\s*['\"]?CLAUDE_CODE_"     # env -u NAME
    r"|(?:^|[;&|({\n])\s*(?:[A-Za-z_]\w*=\S*\s+)*" + IDENTITY + "="           # NAME=value where a command starts
    r"|\benv\b(?:\s+(?:-[uCS]\s+\S+|-\S+|[A-Za-z_]\w*=\S*))*\s+" + IDENTITY + "=", re.M)
CLEAR_ENV = re.compile(r"\benv\b[^;&|\n]*\s(?:-[a-zA-Z]*i[a-zA-Z]*|--ignore-environment|-)(?=\s|$)", re.M)
SUB_MARK = re.compile(r"\$__SUB(\d+)")
VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def split_substitutions(text, heredoc=False):
    """(text with each $(...) and `...` the shell would run replaced by $__SUB<n>, their inner texts).
    Inside single quotes and after a backslash they are plain text; a here-document body has no quotes."""
    out, subs, i, n, q = [], [], 0, len(text), None
    while i < n:
        c = text[i]
        if q == "'":
            out.append(c)
            q = None if c == "'" else q
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if not heredoc and c == "'" and q is None:
            q = "'"
        elif not heredoc and c == '"':
            q = None if q == '"' else '"'
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            subs.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
            out.append("$__SUB%d" % len(subs))
            i = j
            continue
        elif c == "`":
            j = text.find("`", i + 1)
            subs.append(text[i + 1:] if j < 0 else text[i + 1:j])
            out.append("$__SUB%d" % len(subs))
            i = n if j < 0 else j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), subs


def expand(word, assign):
    """$NAME and ${NAME} replaced by values assigned earlier in the same command."""
    if "$" not in word or not assign:
        return word
    return VAR_REF.sub(lambda m: assign.get(m.group(1) or m.group(2), m.group(0)), word)


def target_dir(cur, path, root):
    """Where cd PATH (or git -C PATH) leads from cur, ~ and $VAR expanded; the project root when it
    cannot be told (cd -, a variable the hook does not have)."""
    if path == "-" or cur is None:
        return root
    path = os.path.expandvars(os.path.expanduser(path or "~"))
    if "$" in path or "`" in path:
        return root
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cur, path))


def full_path(path, where):
    return os.path.normpath(os.path.join(where or ".", os.path.expanduser(path)))


def fed_script(path, where, written, bodies):
    """Texts a shell or source would read from path: what this command wrote there earlier
    (echo, printf, cat or tee into it), the file itself, or, for a file that does not exist yet,
    the here-documents of the command."""
    full = full_path(path, where)
    texts = [written[full]] if full in written else []
    text = fc.read_script(full, None)
    if text is not None:
        texts.append(text)
    elif not texts and not os.path.exists(full):
        texts += [b for b, _ in bodies.values()]
    return texts


def out_file(raw, where):
    """The file a command's > or >> writes to, or None."""
    for i, t in enumerate(raw[:-1]):
        if t in (">", ">>", ">|", "&>"):
            return full_path(raw[i + 1], where)
    return None


WRITE_REDIRECTS = (">", ">>", ">|", "&>", "<>")


def redirect_targets(raw, where, assign):
    """([full path], [as written]) of every file a command's redirections write to (> >> >| &> <>, and >&
    to a file name)."""
    out, shown = [], []
    for i, t in enumerate(raw[:-1]):
        target = raw[i + 1]
        if t in WRITE_REDIRECTS or (t == ">&" and not target.isdigit() and target != "-"):
            target = expand(target, assign)
            if target and target not in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"):
                out.append(full_path(os.path.expandvars(target) if "$" in target else target, where))
                shown.append("%s %s" % (t, target))
    return out, shown


def find_pick(argv):
    """What {} stands for in a find's -exec: git or route.py when the find selects that file."""
    for i, a in enumerate(argv[1:], 1):
        if a in fc.FIND_NAMES and i + 1 < len(argv):
            names = [argv[i + 1]]
        elif not a.startswith("-") and (i == 1 or argv[i - 1] not in fc.FIND_NAMES):
            names = [a]
        else:
            continue
        for v in names:
            base = os.path.basename(v)
            for pick in ("git", "route.py"):
                try:
                    if base == pick or fnmatch.fnmatchcase(pick, base):
                        return pick
                except re.error:
                    pass
    return None


def find_starts(argv):
    """The starting points of a find (the words before its first test or action); . when it names none."""
    out, i = [], 1
    while i < len(argv) and (argv[i] in ("-H", "-L", "-P", "-D") or argv[i].startswith("-O")):
        i += 2 if argv[i] == "-D" else 1
    while i < len(argv) and not argv[i].startswith("-") and argv[i] not in ("(", "!", ")", "\\(", "\\!", "\\)", ","):
        out.append(argv[i])
        i += 1
    return out or ["."]


XARGS_VALUES = ("-I", "-i", "-n", "-P", "-L", "-d", "-E", "-s", "-a")


def xargs_command(seg, wrappers):
    """(the wrappers in front of an xargs among the wrappers, the command it runs with its own wrappers, the -I
    replacement string or None); None without an xargs. An empty command is xargs's own echo. wrappers is the
    head of seg."""
    k = next((j for j, w in enumerate(wrappers) if os.path.basename(w) == "xargs"), None)
    if k is None:
        return None
    i, repl = k + 1, None
    while i < len(seg) and seg[i].startswith("-"):
        if seg[i] in ("-I", "-i") and i + 1 < len(seg):
            repl = seg[i + 1]
        elif seg[i].startswith("-I") and len(seg[i]) > 2:
            repl = seg[i][2:]
        i += 2 if seg[i] in XARGS_VALUES else 1
    return seg[:k], seg[i:], repl


def sudo_shell(wrappers):
    """Do the wrappers end with sudo -s, sudo -i or doas -s: a root shell that reads its commands from standard
    input when no command follows?"""
    for k, w in enumerate(wrappers):
        if os.path.basename(w) not in ("sudo", "doas"):
            continue
        takes, j = fc.VALUE_OPTIONS.get(os.path.basename(w), ()), k + 1
        while j < len(wrappers) and wrappers[j].startswith("-"):
            o = wrappers[j]
            if o in ("--shell", "--login") or (not o.startswith("--") and ("s" in o[1:] or "i" in o[1:])):
                return True
            j += 2 if o in takes else 1
    return False


def simple_output(tokens, where):
    """What one plain echo, printf or cat prints, from its tokens; None for anything else."""
    segs, _ = fc._segments(tokens)
    if len(segs) != 1:
        return None
    argv, _ = fc._unwrap(fc._drop_redirects(segs[0])[0])
    text = fc.pipe_output(argv, "", where)
    if text and argv and os.path.basename(argv[0]) == "printf":
        text = text.replace("\\n", "\n").replace("\\t", "\t")
    return text


def command_runs(command, cwd, root, depth=0, bodies=None, assign=None, written=None, detail=False):
    """(argv, directory) of each simple command a shell command runs, as far as the text shows.

    Split at ; && || | and ( ); wrappers (env, timeout, nohup ...) dropped; cd followed, but a cd
    inside ( ... ), $(...) or a pipeline stays there. Followed: $(...) and backticks the shell runs
    (not those inside single quotes, after a backslash or in a comment), sh -c and eval, simple
    NAME=value variables, here-documents, here-strings, pipes and files fed to a shell, to source or
    to a root shell (su, sudo -s, sudo -i: a file written earlier in the same command counts), su -c,
    find -exec ({} stands for git or route.py when the find selects it, and for the find's starting
    points; what a find writes to a pipe is its starting points too), and xargs (what it runs is read
    with the words of its standard input when they show; a command that comes from an unknown
    standard input is text the parser cannot read). Here-document bodies not fed to a shell, quoted
    arguments and comments are not commands. (None, text) stands for text the parser cannot read.
    With detail, each entry has a third item: {"wrappers": the words dropped in front of argv (sudo,
    env FOO=1 ...), "writes": the files its redirections write to, "redirects": those redirections as
    written}; a command made of redirections only (> file) or of wrappers only (sudo -s) is an entry
    with an empty argv, and (None, text, {}) is text the parser cannot read."""
    if depth > 4:
        return []
    text, new = fc.split_heredocs(command)
    bodies = dict(bodies or {})
    bodies.update(new)
    assign = dict(assign or {})
    written = {} if written is None else written
    out = []

    def deeper(inner, where):
        return command_runs(inner, where, root, depth + 1, bodies, assign, written, detail)

    def unread(rest):
        return (None, rest, {}) if detail else (None, rest)

    for body, quoted in new.values():                   # an unquoted here-document expands $(...)
        if not quoted:
            for inner in split_substitutions(body, heredoc=True)[1]:
                out.extend(deeper(inner, cwd))
    text, subs = split_substitutions(fc.strip_comments(text).replace("\\\n", " "))
    try:
        tokens = fc.tokenize(text)
    except ValueError:
        for inner in subs:
            out.extend(deeper(inner, cwd))
        return out + [unread(text)]
    st, stack, done = {"where": cwd, "prev_out": None}, [], set()

    def segment(raw, piped_in, piped_out):
        where = st["where"]
        for tok in raw:                                 # $(...) in this command runs here, before it
            for m in SUB_MARK.finditer(tok):
                k = int(m.group(1)) - 1
                if 0 <= k < len(subs) and k not in done:
                    done.add(k)
                    out.extend(deeper(subs[k], where))
        for tok in raw:
            if fc.ASSIGN_RE.match(tok):
                name, _, value = tok.partition("=")
                assign[name] = expand(value, assign)
        docs, strings, infile = fc.seg_stdin(raw, bodies, where)
        fed = docs + strings + (fed_script(infile, where, written, {}) if infile else [])
        if piped_in and st["prev_out"]:
            fed.append(st["prev_out"])
        stdin_text = "\n".join(t for t in fed if t)
        st["prev_out"] = None
        seg, _ = fc._drop_redirects(raw)
        argv, wrappers = fc._unwrap(seg)
        argv = [expand(a, assign) for a in argv]
        if argv and (" " in argv[0] or "\t" in argv[0]):   # env -S 'python3 -I' ..., $C holding a command
            try:
                argv = shlex.split(argv[0]) + argv[1:]
            except ValueError:
                pass
        info = None
        if detail:
            writes, shown = redirect_targets(raw, where, assign)
            info = {"wrappers": [expand(w, assign) for w in wrappers], "writes": writes, "redirects": shown}

        def emit(words):
            out.append((words, where, info) if detail else (words, where))
        xargs = xargs_command(seg, wrappers)
        if xargs is not None and xargs[1]:
            front, inner, repl = xargs
            if stdin_text.strip():                      # echo ls | xargs sudo: xargs runs sudo ls
                fed = stdin_text[:4000]
                lines = [ln for ln in fed.splitlines() if ln.strip()][:8]
                texts = ([shlex.join(front + [t.replace(repl, ln) for t in inner]) for ln in lines] if repl
                         else [shlex.join(front + inner) + " " + " ".join(fed.split())])
                for t in texts:
                    out.extend(deeper(t, where))
                return
            run, _ = fc._unwrap(inner)
            if not run or os.path.basename(run[0]).lstrip("\\") in SHELL_NAMES | {"eval", "su"} | SOURCE:
                out.append(unread(" ".join(raw)))       # the command itself comes from standard input
        if not argv:
            if detail and (info["writes"] or any(not fc.ASSIGN_RE.match(w) for w in wrappers)):
                emit([])                                # > file: a write with no command; sudo -s: the wrappers run
            if sudo_shell(wrappers) and stdin_text:     # echo cmd | sudo -s: a root shell reads its commands
                out.extend(deeper(stdin_text, where))
            return
        head = os.path.basename(argv[0]).lstrip("\\")
        if head == "cd":
            if not (piped_in or piped_out):
                st["where"] = target_dir(where, argv[1] if len(argv) > 1 else "", root)
            return
        if head == "su":                                # su -c CMD runs CMD; su alone reads its commands from stdin
            cmd = flag_value(argv[1:], "-c") or flag_value(argv[1:], "--command")
            for t in ([cmd] if cmd else [stdin_text]):
                if t:
                    out.extend(deeper(t, where))
            emit(argv)
            return
        if head in SHELL_NAMES:
            kind, value, _ = fc.shell_target(argv[1:])
            if kind == "command":
                out.extend(deeper(value, where))
                if detail and (info["wrappers"] or info["writes"]):
                    emit(argv)                          # sudo sh -c '...': the wrapper runs too
                return
            for t in ([stdin_text] if kind == "stdin" else fed_script(value, where, written, bodies)):
                if t:
                    out.extend(deeper(t, where))
            emit(argv)
            return
        if head in SOURCE and len(argv) > 1:
            for t in fed_script(argv[1], where, written, bodies):
                if t:
                    out.extend(deeper(t, where))
            return
        if head == "eval":
            out.extend(deeper(" ".join(argv[1:]), where))
            return
        if head == "find":                              # {} is git or route.py when the find selects it, and the files under its starting points
            pick, starts = find_pick(argv), find_starts(argv)
            for clause in fc.find_clauses(argv):
                seen = []
                for stand in ([[pick]] if pick else []) + [starts]:
                    words = [y for x in clause for y in (stand if x == "{}" else [x])]
                    if words and words not in seen:
                        seen.append(words)
                        out.extend(deeper(shlex.join(words), where))
        if head == "xcrun" and len(argv) > 1:
            argv = argv[1:]
        emit(argv)
        text_out = fc.pipe_output(argv, stdin_text, where)
        if head == "find" and text_out is None:         # find ... | xargs rm: the files under its starting points
            text_out = "\n".join(find_starts(argv))
        if text_out and (head == "printf" or (head == "echo" and "-e" in argv[1:2])):
            text_out = text_out.replace("\\n", "\n").replace("\\t", "\t")      # printf 'git stash\n'
        if head == "tee":
            text_out = stdin_text
            for f in argv[1:]:
                if not f.startswith("-"):
                    written[full_path(f, where)] = stdin_text
        target = out_file(raw, where)
        if target and text_out is not None:
            written[target] = text_out
        st["prev_out"] = text_out

    cur, piped_in, i = [], False, 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in ("<", ">") and i < len(tokens) and tokens[i] == "(":        # <(cmd) >(cmd): runs cmd
            depth, j = 1, i + 1
            while j < len(tokens) and depth:
                depth += {"(": 1, ")": -1}.get(tokens[j], 0)
                j += 1
            inner = tokens[i + 1:j - 1]
            out.extend(deeper(" ".join(t if t in fc.SEPARATORS or t in fc.REDIRECTS else shlex.quote(t) for t in inner),
                              st["where"]))
            produced = simple_output(inner, st["where"]) if tok == "<" else None
            if produced:                                # bash <(echo git stash) reads what cmd prints
                cur += ["<<<", produced]
            i = j
            continue
        if tok in fc.SEPARATORS:
            piped_out = tok in ("|", "|&")
            if cur:
                segment(cur, piped_in, piped_out)
                cur = []
            piped_in = piped_out
            if tok == "(":
                stack.append(st["where"])
            elif tok == ")" and stack:
                st["where"] = stack.pop()
            continue
        cur.append(tok)
    if cur:
        segment(cur, piped_in, False)
    for k, inner in enumerate(subs):
        if k not in done:
            out.extend(deeper(inner, st["where"]))
    return out


def route_sub(argv):
    """The route.py subcommand this argv runs (interpreter options, global options such as
    --offline and runner modules such as cProfile skipped), or None."""
    if not argv:
        return None
    if PYTHON.search(os.path.basename(argv[0])):
        kind, target, rest = fc.python_target(argv[1:])
        if kind == "module" and (target == "route" or str(target).endswith(".route")):
            script = "route.py"
        elif kind == "module" and target in SCRIPT_MODULES:     # python3 -m cProfile -o x route.py <sub>
            j = next((k for k, a in enumerate(rest) if os.path.basename(a) == "route.py"
                      or ((target == "runpy" or (k and rest[k - 1] == "-m")) and (a == "route" or a.endswith(".route")))),
                     None)
            if j is None:
                return None
            script, rest = "route.py", rest[j + 1:]
        elif kind == "script":
            script = target
        else:
            return None
    else:
        script, rest = argv[0], argv[1:]
    if os.path.basename(script or "").lstrip("\\") != "route.py":
        return None
    return next((x for x in rest if not x.startswith("-")), "")


def flag_value(args, flag):
    for i, x in enumerate(args):
        if x == flag and i + 1 < len(args):
            return args[i + 1]
        if x.startswith(flag + "="):
            return x.split("=", 1)[1]
    return None


def past_cd_home(command, root):
    """The command after a leading `cd <this project's directory> &&`. Sessions often put it in
    front of a project script, and it changes nothing about what runs. Any other cd stays."""
    m = re.match(r"""\s*cd\s+("[^"$`\\]*"|'[^']*'|[^\s;&|`$<>"'\\]+)\s*&&\s*(\S.*)$""", command, re.S)
    if not m:
        return command
    where = m.group(1)
    if where[:1] in ("'", '"'):
        where = where[1:-1]
    if os.path.isabs(where) and os.path.realpath(where) == os.path.realpath(root):
        return m.group(2)
    return command


def check_pool_command(data, who, policy, root, command):
    """route.py claim / expect / release: one plain command, run by a session allowed to."""
    runs = command_runs(command, data.get("cwd") or root, root)
    subs = [route_sub(argv) for argv, _ in runs if argv]
    sub = next((x for x in subs if x in POOL_SUBS), None)
    if sub is None:
        unread = [t for argv, t in runs if argv is None]
        if any("route.py" in t and re.search(r"\b(?:%s)\b" % "|".join(POOL_SUBS), t) for t in unread):
            return refuse(who, data, "看不清的 route.py 命令；认领、预登记、释放要写成单条简单命令。")
        return None
    if data.get("agent_id"):
        return Verdict("deny", "子智能体不能运行 route.py %s：认领属于会话本身，不属于会话里开出的子智能体。" % sub)
    text = past_cd_home(command, root)
    try:
        argv = shlex.split(text)
    except ValueError:
        argv = []
    plain = (len(argv) >= 4 and PYTHON.search(os.path.basename(argv[0])) and argv[1] == "-I"
             and argv[2].endswith(".claude/solo-lab/bin/route.py") and argv[3] == sub
             and not re.search(r"[;&|`$<>\n]", text))
    if not plain:
        return Verdict("deny", "route.py %s 要写成单条简单命令：python3 -I .claude/solo-lab/bin/route.py %s …；"
                               "前面最多带一句 cd 到本项目目录（cd <项目目录> &&），不带环境变量赋值、包装命令或 --offline 这类全局选项，"
                               "后面不接管道。" % (sub, sub))
    args = argv[4:]
    names = [a.split("=", 1)[0] for a in args if a.startswith("--")]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        return Verdict("deny", "route.py %s 的选项不能重复写（%s）：hook 和脚本会各读到不同的值。" % (sub, "、".join(twice)))
    desk = os.environ.get(pool.DESK_ENV) or ""
    given = flag_value(args, "--desk")
    if sub == "claim":
        if given is not None and given != desk:
            if desk:
                return Verdict("deny", "--desk %s 和本会话的桌面会话 ID 不一致。" % (given or "''"))
            return refuse(who, data, "hook 看不到本会话的桌面会话 ID，没法核对 --desk %s；会话自己的 Bash 里有桌面会话 ID 时，"
                                     "去掉 --desk 重试。" % (given or "''"))
        if who.origin in ("claim", "permit", "pending", "researcher"):
            return None                       # route.py checks the permit against this session's own IDs
        if who.origin == "tombstone":
            return Verdict("deny", "本会话的认领已经释放；要重新认领，请研究员或开它的会话重新预登记。")
        if who.origin == "desk_conflict":
            return Verdict("deny", "本会话和已认领的 %s 共用一个桌面会话 ID（可能是从那个会话里起的进程），不能认领。" % who.conflict)
        return Verdict("deny", "（%s）不能认领。" % who.label)
    if sub == "expect":
        role = flag_value(args, "--role") or ""
        if who.researcher:
            return None
        if who.origin != "claim":
            return Verdict("deny", "只有已认领的 +2（预登记本序列的 +1、+0）、+1（预登记本序列的 +0）、入口（预登记路由和书记员）和研究员自己的会话能预登记。")
        mine = policy["agents"].get(who.role) or {}
        target = policy["agents"].get(role) or {}
        if who.role == policy.get("entry_role") and role in (policy.get("router_role"), policy.get("scribe_role")):
            return None
        if mine.get("rank") == 2 and who.role != policy.get("entry_role") and target.get("sequence") == mine.get("sequence") \
                and target.get("rank") in (0, 1):
            return None
        if mine.get("rank") == 1 and target.get("sequence") == mine.get("sequence") and target.get("rank") == 0:
            return None                       # a +1 opens its +0s: the sidebar nests a session under the one that opened it
        return Verdict("deny", "（%s）不能预登记 %s：+2 只预登记本序列的 +1、+0，+1 只预登记本序列的 +0，入口只预登记路由和书记员。" % (
            who.label, role or "这个角色"))
    if who.researcher:
        return refuse(who, data, "释放认领会让这个会话不再是 %s；请研究员确认。" % (args[0] if args else "该角色"))
    return Verdict("deny", "只有研究员能释放认领（route.py release）。")


def setup_sub(argv):
    """(script, subcommand) when argv runs the plugin's setup.py or space.py with python; else None."""
    if not argv or not PYTHON.search(os.path.basename(argv[0])):
        return None
    kind, target, rest = fc.python_target(argv[1:])
    name = os.path.basename(target or "").lstrip("\\")
    if kind != "script" or name not in ("setup.py", "space.py"):
        return None
    return name, next((x for x in rest if not x.startswith("-")), "")


def check_setup(data, who, root, command):
    """setup.py changes what the project is bound to, starts logins and opens the pool: it is for
    the researcher's own session. So is space.py export, which writes a file wherever it is told."""
    if "setup.py" not in command and "space.py" not in command:
        return None
    hits = []
    for argv, text in command_runs(command, data.get("cwd") or root, root):
        found = setup_sub(argv) if argv else (("setup.py", "") if "setup.py" in text else None)
        if found and (found[0] == "setup.py" or found[1] == "export"):
            hits.append(found)
    if not hits or (who.researcher and not data.get("agent_id")):
        return None
    if any(sub_ == "login" for _, sub_ in hits):
        return Verdict("deny", "登录由研究员的会话发起（setup.py login），研究员扫码完成：把“登录过期”报给研究员。")
    return Verdict("deny", "%s 只由研究员的会话运行：把要做的事报给研究员。" % " ".join(x for x in hits[0] if x))


def check_identity_vars(data, who, command):
    """A command that unsets, clears or sets the session's identity variables would speak
    for another session to route.py and ledger.py."""
    if IDENTITY_VARS.search(command) or (CLEAR_ENV.search(command) and re.search(r"route|ledger", command)):
        return Verdict("deny", "不要改或清掉 CLAUDE_CODE_SESSION_ID、CLAUDE_CODE_HOST_SESSION_ID（unset、env -u、env -i、export -n 都算）："
                               "认领和台账写权按它们认会话。")
    return None


def linked_worktree(path, root):
    """Is path inside a linked git worktree below the project root (a .git file pointing into
    .../worktrees/, such as Claude Code's own .claude/worktrees/<name>)? Such a tree is not shared.
    A nested clone or a submodule is still part of the shared directory."""
    real_root, cur = os.path.realpath(root), resolved(path)
    while inside(cur, real_root) and cur != real_root:
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            return False
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8", errors="ignore") as f:
                    head = f.read(1024)
            except OSError:
                return False
            return head.startswith("gitdir:") and "/worktrees/" in head
        cur = os.path.dirname(cur)
    return False


def outside(path, cwd, root):
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or root, path))
    if not inside(resolved(full), os.path.realpath(root)) and not inside(full, root):
        return True
    return linked_worktree(full, root)


def git_switch(command, root, cwd=None):
    """The git subcommand that switches, stashes, merges or pulls in this project's working tree,
    if the command runs one. Read-only forms (stash list/show, --help, clean -n) and git run in
    another tree (-C, --work-tree, a cd before it, a linked worktree) do not count; with --git-dir
    alone the working tree is still the current directory."""
    for argv, where in command_runs(command, cwd or root, root):
        if not argv:
            continue
        head = os.path.basename(argv[0]).lstrip("\\")
        if head == "gh" and argv[1:3] == ["pr", "checkout"]:
            if not outside(where, where, root):
                return "pr checkout"
            continue
        if head != "git":
            continue
        i, target, work = 1, where, None
        while i < len(argv) and argv[i].startswith("-"):
            opt = argv[i].split("=", 1)[0]
            value = argv[i].split("=", 1)[1] if "=" in argv[i] else (argv[i + 1] if i + 1 < len(argv) else "")
            if opt == "-C" and value:
                target = target_dir(target, value, root)
            elif opt == "--work-tree" and value:
                work = value
            i += 1 if "=" in argv[i] else (2 if opt in GIT_VALUE_OPTIONS else 1)
        if work:
            target = target_dir(target, work, root)
        if i >= len(argv) or argv[i] not in GIT_SWITCHES or outside(target, target, root):
            continue
        sub, rest = argv[i], argv[i + 1:]
        if "-h" in rest or "--help" in rest:
            continue
        if sub == "stash" and rest[:1] in (["list"], ["show"]):
            continue
        if sub == "clean" and any(x in ("-n", "--dry-run") or (x.startswith("-") and not x.startswith("--") and "n" in x)
                                  for x in rest):
            continue
        return sub
    return None


def check_git(data, who, command, root):
    sub = git_switch(command, root, data.get("cwd"))
    if not sub:
        return None
    reason = ("（%s）不能在共享工作区里运行 git %s：会话池的会话共用这个目录，切分支、stash、reset、pull 这类命令会丢掉"
              "或混进别的会话还没提交的改动。把要做的事写进交付，由研究员执行。" % (who.label, sub))
    return refuse(who, data, reason)


# ------------------------------------------------------------ irreversible
# policy.json "irreversible" is the irreversible list (orgspec/irreversible.json) as setup.py writes it: the
# categories a command or a path shows, with their match rules (the list file explains them), and the
# project's protected paths. .claude/state/gates.json is the local decision state ledger.py keeps (format
# above ledger.save_gates): "pending" decisions with what they freeze, "releases" of decided ones.
GATES_FILE = "gates.json"
DOCKER_VALUES = {"--context", "-c", "--config", "-H", "--host", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"}
CMD_VALUES = {"git": GIT_VALUE_OPTIONS, "docker": DOCKER_VALUES}       # global options with a value, before the subcommand
REMOTE_ITSELF = {"ssh", "sftp"}                                         # they always reach another host
REMOTE_ARG = re.compile(r"^(?:(?:rsync|scp|sftp|ssh)://|[A-Za-z0-9._-]+::|(?:[^/@\s:]+@)?[A-Za-z0-9][A-Za-z0-9._-]*:(?!//))")
DELETERS = {"rm", "rmdir", "unlink", "shred"}
PERMISSION_CMDS = {"chmod", "chown", "chgrp", "chflags"}
COPIERS = {"cp", "install", "ln"}
IN_PLACE = {"sed": ("efl", ""), "gsed": ("efl", ""), "perl": ("eEIMmxFdDV", "0lC")}   # (letters taking the rest, letters taking digits)
PATH_VALUES = {   # options that take the next word as their value, for the commands whose file arguments are read
    "cp": {"-t", "-S", "--target-directory", "--suffix"}, "ln": {"-t", "-S", "--target-directory", "--suffix"},
    "mv": {"-t", "-S", "--target-directory", "--suffix"},
    "install": {"-t", "-S", "-m", "-o", "-g", "-f", "-B", "--target-directory", "--suffix", "--mode", "--owner", "--group"},
    "truncate": {"-s", "-r", "--size", "--reference"}, "shred": {"-n", "-s", "--iterations", "--size"},
    "sed": {"-e", "-f", "-l", "--expression", "--file", "--line-length"},
    "gsed": {"-e", "-f", "-l", "--expression", "--file", "--line-length"}, "perl": {"-e", "-E", "-I", "-M", "-m", "-x"},
}
# A qualifier's words a command can show: command names, options, hosts, paths (~/.ssh and .env keep their ~/ and .).
TERM = re.compile(r"[~./*?\[A-Za-z0-9_-][A-Za-z0-9._:/@+~=*?\[\]-]*")
QUALIFIER_OR = re.compile(r"\s*(?:[、|]|或者|或)\s*")              # alternatives: scp、ssh, ssh 或 scp
SHOWN = 80                                                             # characters of a command quoted in a reason


def load_gates(root):
    """(pending decisions by ID, releases, problem) from .claude/state/gates.json, which only ledger.py writes.
    A missing file is an empty state; one that cannot be read gives a problem and releases nothing."""
    path = os.path.join(pool.state_dir(root), GATES_FILE)
    if not os.path.exists(path):
        return {}, [], None
    data = fc.load_json(path, None)
    if not isinstance(data, dict):
        return {}, [], "本地决策状态 .claude/state/gates.json 读不出来，放行一律不算"
    pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    releases = data.get("releases") if isinstance(data.get("releases"), list) else []
    return ({str(k): v for k, v in pending.items() if isinstance(v, dict)},
            [r for r in releases if isinstance(r, dict) and r.get("category")], None)


def irreversible_categories(policy):
    """The categories of the irreversible list a command or a path shows (policy.json)."""
    irr = policy.get("irreversible") if isinstance(policy.get("irreversible"), dict) else {}
    return [c for c in irr.get("categories") or [] if isinstance(c, dict) and c.get("id") and c.get("match")]


def protected_globs(policy):
    irr = policy.get("irreversible") if isinstance(policy.get("irreversible"), dict) else {}
    return list((irr.get("paths") or {}).get("protected") or policy.get("h_paths") or [])


def short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= SHOWN else text[:SHOWN - 1] + "…"


def positionals(args, values=()):
    """The non-empty arguments of a command that are neither options nor option values; all after -- count."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(x for x in args[i + 1:] if x)
            break
        if a.startswith("-") and len(a) > 1:
            i += 2 if a in values else 1
            continue
        if a:
            out.append(a)
        i += 1
    return out


def has_flag(args, letter, long_name):
    """Is a short option given alone or in a cluster (-R, -Rf), or its long form (--recursive)?"""
    for a in args:
        if a == "--":
            return False
        if a == long_name or (a.startswith("-") and not a.startswith("--") and letter in a[1:]):
            return True
    return False


def option_value(args, names):
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
        for n in names:
            if n.startswith("--") and a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def in_place(args, head):
    """Does sed or perl edit its files in place: -i with or without a backup suffix, alone or in a cluster
    (-pi, -Ei, -i.bak), or --in-place?"""
    rest, digits = IN_PLACE.get(head, ("", ""))
    for a in args:
        if a == "--":
            return False
        if a == "--in-place" or a.startswith("--in-place="):
            return True
        if a.startswith("-") and not a.startswith("--"):
            k = 1
            while k < len(a):
                ch = a[k]
                if ch == "i":
                    return True
                if ch in rest or not ch.isalpha():
                    break
                k += 1
                if ch in digits:
                    while k < len(a) and a[k].isdigit():
                        k += 1
    return False


def arg_given(rest, value, head):
    """args_any: the option is given (exactly, or as --opt=value); sed's and perl's -i also in a cluster."""
    if head in IN_PLACE and value.startswith("-") and not value.startswith("--") and "i" in value:
        return in_place(rest, head)
    return any(a == value or a.startswith(value + "=") for a in rest)


def cmd_rest(argv, words):
    """The arguments after the words a match rule's cmd names (the first compared by file name, the others in
    order, global options in between skipped); None when argv is not that command."""
    if not argv or not words or os.path.basename(argv[0]).lstrip("\\") != words[0]:
        return None
    values = CMD_VALUES.get(words[0], ())
    i = 1
    for word in words[1:]:
        while i < len(argv) and argv[i].startswith("-") and len(argv[i]) > 1 and argv[i] != "--":
            i += 2 if argv[i] in values else 1
        if i >= len(argv) or argv[i] != word:
            return None
        i += 1
    return argv[i:]


def opened_apps(args):
    """Names of the applications `open` starts: -a NAME or -b BUNDLE (also at the end of a cluster such as
    -na), or a path ending in .app."""
    names, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--args":
            break
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            if a[-1] in "ab" and i + 1 < len(args):
                names.append(args[i + 1])
                i += 2
                continue
        elif a.startswith("--"):
            i += 2 if a in ("--env", "--stdin", "--stdout", "--stderr") else 1
            continue
        elif a.rstrip("/").lower().endswith(".app"):
            names.append(a)
        i += 1
    return [os.path.basename(n.rstrip("/")) for n in names if n]


def app_entry(cat, name):
    """The application of a category's apps list that name (Docker, Docker.app, com.docker.docker) stands for."""
    name = str(name or "").strip().lower()
    for entry in cat.get("apps") or []:
        if name in [str(n).lower() for n in [entry.get("name", "")] + list(entry.get("names") or [])]:
            return entry
    return None


BRACE = re.compile(r"(?<!\$)\{([^{}]*,[^{}]*)\}")


def brace_expand(word, limit=64):
    """The words the shell's brace expansion makes of word: data/{eval,dev}/x is data/eval/x and data/dev/x
    (comma lists, nested ones too; not ${VAR} or a find's {})."""
    out = [word]
    for _ in range(8):
        nxt, changed = [], False
        for w in out:
            m = BRACE.search(w)
            if not m:
                nxt.append(w)
                continue
            changed = True
            nxt += [w[:m.start()] + part + w[m.end():] for part in m.group(1).split(",")]
        out = nxt[:limit]
        if not changed:
            break
    return out


RSYNC_VALUES = {"-e", "--rsh", "-f", "--filter", "--exclude", "--include", "--exclude-from", "--include-from", "--files-from",
                "-T", "--temp-dir", "--partial-dir", "--compare-dest", "--copy-dest", "--link-dest", "--backup-dir", "--suffix",
                "--chmod", "--chown", "-B", "--block-size", "-M", "--remote-option", "--log-file", "--password-file", "--port",
                "--bwlimit", "--timeout", "--max-size", "--min-size", "--out-format", "--info", "--debug"}


def git_sub(argv, where):
    """(git's subcommand, its arguments, the directory it works in) for a git command line; None for anything else."""
    if not argv or os.path.basename(argv[0]).lstrip("\\") != "git":
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i].split("=", 1)[0]
        value = argv[i].split("=", 1)[1] if "=" in argv[i] else (argv[i + 1] if i + 1 < len(argv) else "")
        if opt == "-C" and value:
            where = full_path(value, where)
        i += 1 if "=" in argv[i] else (2 if opt in GIT_VALUE_OPTIONS else 1)
    return (argv[i], argv[i + 1:], where) if i < len(argv) else None


def copy_targets(srcs, dest, full, into_dir=False):
    """(what cp, install, ln, mv or rsync writes, the directories whose contents it may overwrite): each source's
    name in the destination when that is a directory (several sources, -t, a trailing /, or one that exists), and
    the destination itself; a source written as dir/ or dir/. copies its contents into the destination."""
    target = full(dest)
    is_dir = into_dir or len(srcs) > 1 or dest.endswith("/") or os.path.isdir(target)
    written = []
    for s in srcs:
        path = target if s.endswith(("/", "/.")) else full(os.path.join(dest, os.path.basename(s))) if is_dir else None
        if path and path not in written:
            written.append(path)
    into = [target] if any(s.endswith(("/", "/.")) for s in srcs) else []
    return written + ([target] if target not in written else []), into


def path_targets(argv, where):
    """(paths a command writes or removes, directories whose whole contents it removes or changes), full paths.
    Only the commands of the list are read, after brace expansion (mv data/{eval,old} is mv data/eval data/old).
    cp, install, ln and rsync write their destination and the copies in it (all of a destination they copy a
    directory's contents into, and with rsync --delete); mv and git mv take their sources away and write their
    destination; rm, git rm and the like remove their arguments with what is under them, find -delete what is
    under its starting points; tee, truncate, dd of=, sed -i and perl -i write their files; chmod, chown, chgrp
    and chflags change their files, with -R the trees under them (the mode or owner counts as a file too: a mode
    such as -w looks like an option, and a mode taken for a path matches no group)."""
    if not argv:
        return [], []
    head = os.path.basename(argv[0]).lstrip("\\")
    args = [x for a in argv[1:] for x in brace_expand(a)]

    def full(p):
        return full_path(os.path.expandvars(p) if "$" in p else p, where)
    sub = git_sub(argv, where) if head == "git" else None
    if sub and sub[0] in ("rm", "mv"):
        rest = [x for a in sub[1] for x in brace_expand(a)]
        if has_flag(rest, "n", "--dry-run"):
            return [], []
        return path_targets([sub[0]] + rest, sub[2])
    if head == "find":
        if "-delete" not in args:
            return [], []
        starts = [full(p) for p in find_starts(argv)]
        return starts, starts
    pos = positionals(args, PATH_VALUES.get(head, ()))
    if head in DELETERS:
        paths = [full(p) for p in pos]
        return paths, paths
    if head == "rsync":
        pos = positionals(args, RSYNC_VALUES)
        if len(pos) < 2 or REMOTE_ARG.match(pos[-1]):
            return [], []                               # to another host: 对外发送 sees it
        written, into = copy_targets(pos[:-1], pos[-1], full)
        removes = [a for a in args if a.startswith("--delete") or a == "--remove-source-files"]
        local = [full(s) for s in pos[:-1] if not REMOTE_ARG.match(s)]
        if "--remove-source-files" in removes:
            written, into = written + local, into + local
        return written, (into + [full(pos[-1])]) if removes else into
    if head in COPIERS or head == "mv":
        if head == "install" and has_flag(args, "d", "--directory"):
            return [full(p) for p in pos], []
        target = option_value(args, ("-t", "--target-directory"))
        if target:
            srcs, dest = pos, target
        elif len(pos) >= 2:
            srcs, dest = pos[:-1], pos[-1]
        elif head == "ln" and pos:
            srcs, dest = pos, "."
        else:
            return ([full(p) for p in pos], [full(p) for p in pos]) if head == "mv" else ([], [])
        out, into = copy_targets(srcs, dest, full, bool(target) or dest == ".")
        if head == "mv":
            return [full(s) for s in srcs] + out, [full(s) for s in srcs]
        return out, into
    if head in ("tee", "truncate"):
        return [full(p) for p in pos], []
    if head == "dd":
        return [full(a[3:]) for a in args if a.startswith("of=") and len(a) > 3], []
    if head in IN_PLACE:
        return ([full(p) for p in pos], []) if in_place(args, head) else ([], [])
    if head in PERMISSION_CMDS:
        paths = [full(p) for p in pos]
        return paths, paths if has_flag(args, "R", "--recursive") else []
    return [], []


def path_forms(path, root):
    """(forms relative to the project root, absolute forms) of a full path, as written and with symlinks followed."""
    lex = os.path.normpath(path)
    real = resolved(lex)
    rels = []
    for r in (relative(lex, root), relative(real, os.path.realpath(root)), relative(lex, os.path.realpath(root))):
        if r is not None and r not in rels:
            rels.append(r)
    return rels, [lex] if real == lex else [lex, real]


def is_outside(path, root):
    """Outside the project directory and the temporary directories, as written and with symlinks followed."""
    rels, forms = path_forms(path, root)
    if rels:
        return False
    tmp = os.environ.get("TMPDIR", "")
    return not any((p + "/").startswith(TMP_PREFIXES) or (tmp and inside(p, os.path.realpath(tmp))) for p in forms)


WILD = re.compile(r"[*?\[]")


def parts_of(path):
    return [c for c in path.split("/") if c]


def part_meets(a, b):
    """Can one name match both path components a (from a command, maybe a shell wildcard) and b (from a glob)?"""
    if FOLD_CASE:
        a, b = a.lower(), b.lower()
    wa, wb = bool(WILD.search(a)), bool(WILD.search(b))
    if not wa and not wb:
        return a == b
    try:
        if not wa:
            return fnmatch.fnmatchcase(a, b)
        if not wb:
            return fnmatch.fnmatchcase(b, a)
    except re.error:
        return True
    return True                                     # two wildcards: take it that they can meet


def meets(p, g, subtree=False, literal=True):
    """Do the components p of a path (shell wildcards allowed: rm data/*/test.csv) reach what the components g of a
    glob name? A path the glob matches counts, so does one under it, and the base directory of a glob ending in **
    (cp -R new/. repo/eval for repo/eval/**). With subtree (rm -r, chmod -R), also a directory that holds the
    glob's base, the part before its first wildcard (rm -rf data for data/eval/**; not every directory for
    **/*.pem). ** in the glob stands for any number of components."""
    if not g:
        return True                                 # the path is what the glob names, or under it
    if g[0] == "**":
        return meets(p, g[1:], subtree, False) or (bool(p) and meets(p[1:], g, subtree, False))
    if not p:
        return subtree and literal
    return part_meets(p[0], g[0]) and meets(p[1:], g[1:], subtree, literal and not WILD.search(g[0]))


def glob_hit(path, root, globs, subtree=False):
    """The form of path a group of globs matches, or None. ~/ globs are compared with the absolute path, **/
    globs with both forms, other globs with the path relative to the project root (meets: a path under a glob, the
    base directory of a glob ending in **, and a path with shell wildcards that may reach one count too). With
    subtree, a directory that holds what a glob names counts too (rm -r, chmod -R), and so does one that holds
    the whole project (rm -rf .. ) for the globs relative to it."""
    rels, forms = path_forms(path, root)
    real_root = os.path.realpath(root)
    holds_project = subtree and not rels and any(inside(root, f) or inside(real_root, f) for f in forms)
    for g in globs:
        if g == "~" or g.startswith("~/"):
            pat, cands = os.path.expanduser(g), forms
        elif g.startswith("**/"):
            pat, cands = g, rels + forms
        else:
            pat, cands = g, rels
        for c in cands:
            if (c.startswith("/") == pat.startswith("/") or pat.startswith("**/")) and meets(parts_of(c), parts_of(pat), subtree):
                return c or "."                     # the project directory itself
        if holds_project and cands is not forms:
            return forms[0]
    return None


def in_group(group, path, cat, root, policy, subtree=False):
    """The form of path a path group of the list matches (outside, protected, or one of the category's own), or None."""
    if group == "outside":
        return path if is_outside(path, root) else None
    if group == "protected":
        return glob_hit(path, root, protected_globs(policy), subtree)
    return glob_hit(path, root, (cat.get("paths") or {}).get(group) or [], subtree)


def group_hit(groups, path, cat, root, policy, subtree=False):
    """The form of path the first of these path groups matches, or None."""
    for group in groups:
        found = in_group(group, path, cat, root, policy, subtree)
        if found:
            return found
    return None


def group_paths(groups, paths, cat, root, policy, subtree=False):
    """[(the form a path group matches, full path)] for each of paths that one of these groups matches."""
    out = []
    for p in paths:
        form = group_hit(groups, p, cat, root, policy, subtree)
        if form:
            out.append((form, p))
    return out


def rule_hit(rule, cat, cands, argv, where, redirects, root, policy):
    """{app, paths, targets} when one simple command meets a match rule of the list; None otherwise: paths are
    the forms of what it changes in a path group, targets the same as full paths. lark-cli rules are met on the
    parsed invocation instead (lark_hits). A writes rule takes the files written themselves: the redirections'
    targets, what cp, mv, tee or sed -i write, a file removed; not a directory that holds one (rm -rf ~/Library is
    no write of Docker Desktop's settings)."""
    if rule.get("writes"):
        written, _ = path_targets(argv, where)
        found = group_paths([rule["writes"]], redirects + written, cat, root, policy)
        return {"app": None, "paths": [f for f, _ in found], "targets": [p for _, p in found]} if found else None
    words = rule.get("cmd") or []
    if not words or words[0] == "lark-cli":
        return None
    for cand in cands:
        rest = cmd_rest(cand, words)
        if rest is None:
            continue
        if rule.get("args_any") and not any(arg_given(rest, v, words[0]) for v in rule["args_any"]):
            continue
        if rule.get("sub_prefix"):
            first = positionals(rest)[:1]
            if not first or not any(first[0].startswith(p) for p in rule["sub_prefix"]):
                continue
        app = None
        if rule.get("app"):
            app = next((e for e in (app_entry(cat, n) for n in opened_apps(rest)) if e), None)
            if not app:
                continue
        if rule.get("app_name"):
            app = app_entry(cat, rule["app_name"])
        if rule.get("remote") and words[0] not in REMOTE_ITSELF and not any(REMOTE_ARG.match(a) for a in positionals(rest)):
            continue
        found = []
        if rule.get("paths"):
            written, subtree = path_targets(cand, where)
            found = (group_paths(rule["paths"], written, cat, root, policy)
                     + group_paths(rule["paths"], subtree, cat, root, policy, True))
            if not found:
                continue
        return {"app": app.get("name") if app else None, "paths": [f for f, _ in found], "targets": [p for _, p in found]}
    return None


def word_cands(argv, wrappers):
    """The ways one simple command can be read: its argv, and from each wrapper word on (sudo, doas, env ...)."""
    seg = list(wrappers) + list(argv)
    return [list(argv)] + [seg[i:] for i, w in enumerate(wrappers) if not fc.ASSIGN_RE.match(w) and not w.startswith("-")]


def unread_names(text, cats):
    """Categories of the list whose plain commands (sudo, git push, scp ...) appear in text the parser cannot read."""
    names = []
    for cat in cats:
        for rule in cat.get("match") or []:
            words = rule.get("cmd") or []
            if words and words[0] != "lark-cli" and set(rule) <= {"cmd", "remote", "app_name"} and re.search(
                    r"(?<![\w./-])%s(?![\w-])" % r"\s+".join(re.escape(w) for w in words), text):
                names.append(cat["name"])
                break
    return names


def irreversible_hits(runs, root, policy, cats):
    """([hit], [(text, category names)]): each category of the list a simple command meets, as {cat, text
    (the command as written, wrappers included), argv (its words, wrappers included), where (its directory),
    root, app (the privileged application), paths (what it changes in a path group), targets (the same as full
    paths)}; and the texts the parser could not read that name a plain command of the list."""
    hits, unread = [], []
    for argv, where, info in runs:
        if argv is None:
            names = unread_names(where or "", cats)
            if names:
                unread.append((where, names))
            continue
        cands = word_cands(argv, info["wrappers"])
        words = list(info["wrappers"]) + list(argv)
        heads = [w for w in info["wrappers"] if not fc.ASSIGN_RE.match(w) and not w.startswith("-")] + list(argv[:1])
        text = " ".join(words + list(info["redirects"]))
        for cat in cats:
            for rule in cat["match"]:
                hit = rule_hit(rule, cat, cands, argv, where, info["writes"], root, policy)
                if hit:
                    hits.append(dict(hit, cat=cat, text=text, argv=words, heads=heads, where=where, root=root))
                    break
    return hits, unread


def researcher_messages(root):
    """IDs of the researcher's Feishu DMs, which route.py listen writes into inbox/feishu.jsonl."""
    ids = set()
    try:
        with open(os.path.join(root, "inbox", "feishu.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    ids.add(str(rec["id"]))
    except OSError:
        pass
    return ids


def to_researcher_only(c, root):
    """except researcher: a message to the researcher alone (--user-id is the open_id in feishu/ids.json), or a
    reply to one of the researcher's DMs."""
    flags, verb = c["flags"], c["pos"][1:2]
    if verb == ["+messages-send"]:
        ids = fc.load_json(os.path.join(root, "feishu", "ids.json"), {}) or {}
        me = ((ids.get("lark") or {}) if isinstance(ids, dict) else {}).get("researcher_open_id")
        users = [u for u in flags.get("--user-id") or [] if u]
        return bool(me) and bool(users) and not flags.get("--chat-id") and all(u == me for u in users)
    if verb == ["+messages-reply"]:
        mid = fc.last(flags, "--message-id")
        return bool(mid) and str(mid) in researcher_messages(root)
    return False


def lark_hits(command, cwd, root, risk, cats):
    """Hits of the list's lark-cli rules (mail +send, im +messages-send ...), on the invocations fc.parse finds."""
    hits = []
    for inv in fc.parse(command, cwd).invocations:
        if inv.cli != "lark":
            continue
        c = fc.classify(inv, risk)
        if c["help"] or c["dry_run"]:
            continue
        for cat in cats:
            for rule in cat["match"]:
                words = rule.get("cmd") or []
                if not words or words[0] != "lark-cli" or c["pos"][:len(words) - 1] != words[1:]:
                    continue
                if rule.get("except") == "researcher" and to_researcher_only(c, root):
                    continue
                hits.append({"cat": cat, "text": "lark-cli " + " ".join(inv.args), "argv": ["lark-cli"] + list(inv.args),
                             "heads": ["lark-cli"], "where": inv.cwd or cwd, "root": root, "app": None, "paths": [], "targets": []})
                break
    return hits


def term_kind(term):
    """path (~/.ssh, .env, repo/eval, *.pem), remote (campus-hpc:/scratch, host::module, a URL) or word (scp,
    campus-hpc, git, push)."""
    if "://" in term or (REMOTE_ARG.match(term) and not term.startswith(("~", "/", "."))):
        return "remote"
    if term.startswith(("~", "/", ".")) or "/" in term or WILD.search(term):
        return "path"
    return "word"


def qualifier_terms(qualifier):
    """What of a qualifier a command can show: groups that must all be met, each a list of alternatives
    (written with 、 | 或), each a list of terms; Chinese words are left out. Words and hosts are lower-cased,
    paths keep their case and their ~/ or . (~/.ssh, .env). “scp、ssh 到 CAMPUS-HPC” is (scp or ssh) and campus-hpc.
    [] when nothing is left."""
    groups = []
    for chunk in QUALIFIER_OR.sub("、", qualifier or "").split():
        alts = []
        for alt in chunk.split("、"):
            terms = []
            for t in TERM.findall(alt):
                t = t.rstrip(".,;:") if re.search(r"[A-Za-z0-9*?]", t.rstrip(".,;:")) else t
                if re.search(r"[A-Za-z0-9*?]", t):
                    terms.append(t if term_kind(t) == "path" else t.lower())
            if terms:
                alts.append(terms)
        if alts:
            groups.append(alts)
    return groups


SSH_VALUES = {"-b", "-B", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-P", "-Q", "-R",
              "-S", "-W", "-w"}
REMOTE_SHELLS = {"ssh", "sftp", "slogin", "mosh", "autossh"}
URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:?#]+)(?::\d+)?(/[^?#]*)?")
HOST_PATH = re.compile(r"^(?:[^/@\s:]+@)?([A-Za-z0-9][A-Za-z0-9._-]*)::?(.*)$")


def host_path(arg):
    """(host, path there) of a remote argument (user@host:path, host::module, scheme://user@host:port/path); None
    for anything else."""
    m = URL.match(arg)
    if m:
        return m.group(1).lower(), m.group(2) or ""
    m = HOST_PATH.match(arg) if REMOTE_ARG.match(arg) else None
    return (m.group(1).lower(), m.group(2)) if m else None


def command_words(argv, heads=()):
    """(the words of a command lower-cased, with the value of each --opt=value and the file names of the commands
    it runs (heads: /usr/bin/scp is scp); the hosts it reaches, [(host, path there)]: the host of each remote
    argument and the destination of ssh, sftp and the like). A path's last part is no word of its own:
    campus-hpc in runs/campus-hpc or in ssh host 'ls /x/campus-hpc' is no host."""
    words, remotes = {os.path.basename(h).lower() for h in heads}, []
    for a in argv:
        low = a.lower()
        words.add(low)
        if low.startswith("--") and "=" in low:
            words.add(low.split("=", 1)[1])
        found = host_path(a)
        if found:
            remotes.append(found)
    for k, a in enumerate(argv):
        if os.path.basename(a) in REMOTE_SHELLS:
            dest = positionals(argv[k + 1:], SSH_VALUES)[:1]
            if dest and not host_path(dest[0]):
                remotes.append((dest[0].rsplit("@", 1)[-1].lower(), ""))
    return words, remotes


def word_meets(term, words, remotes):
    """A word term is one of the command's words or the host it reaches (campus-hpc is campus-hpc:~/x, me@campus-hpc, ssh
    campus-hpc; example.edu is login.example.edu); a remote term (campus-hpc:/scratch) is a host it reaches, with that
    path or one under it."""
    if term_kind(term) == "word":
        return term in words or any(h == term or h.endswith("." + term) for h, _ in remotes)
    found = host_path(term)
    if not found:
        return term in words
    host, path = found
    return term in words or any(h == host and (not path or p == path or p.startswith(path.rstrip("/") + "/")) for h, p in remotes)


def path_under(path, term, root):
    """Is a full path the one a path term names, or under it? A term without / (.env, .ssh, *.pem) is a file or
    directory name anywhere on the path; ~ is the home directory; other relative terms start at the project root."""
    path = os.path.normpath(path)
    if "/" not in term.rstrip("/") and not term.startswith("~"):
        return any(part_meets(c, term.rstrip("/")) for c in parts_of(path))
    base = os.path.expanduser(term)
    base = os.path.normpath(base if os.path.isabs(base) else os.path.join(root, base))
    return path.startswith("/") and meets(parts_of(path), parts_of(base))


def local_args(hit):
    """The full paths of the local files a hit's command names (no option, no remote argument)."""
    argv, where = hit.get("argv") or [], hit.get("where") or hit.get("root") or "."
    return [full_path(a, where) for a in positionals(argv[1:]) if not host_path(a)]


def qualifier_apps(cat, text):
    """The applications of a category's apps list (by their name) that a qualifier names anywhere in it: Docker,
    Docker Desktop 的沙箱, com.docker.docker and 启动 Docker all name Docker."""
    low, found = str(text or "").lower(), []
    for entry in cat.get("apps") or []:
        for n in [entry.get("name", "")] + list(entry.get("names") or []):
            n = str(n).lower()
            if n and re.search(r"(?<![A-Za-z0-9.])%s(?![A-Za-z0-9])" % re.escape(n), low):
                found.append(entry.get("name"))
                break
    return found


def covers(qualifier, cat, hit, strict=True):
    """Does a release's or a frozen item's qualifier cover a hit? An empty one is the whole category. A
    privileged application is compared by the application names found in it; a category met only through the
    files it writes (Docker 资源) takes its qualifier as a note. Other qualifiers by qualifier_terms: strict (a
    release) wants each word to be one of the command's words or the host it reaches, and every file the
    command changes in the category under a path the qualifier names; loose (a frozen item) takes any term found
    in the command's text. None when the qualifier holds nothing to compare (no application or term): a frozen
    item takes it as the whole category, a release as nothing."""
    q = str(qualifier or "").strip()
    if not q:
        return True
    if cat.get("apps"):
        found = qualifier_apps(cat, q)
        return hit.get("app") in found if found else None
    if all(not r.get("cmd") for r in cat["match"]):
        return True
    groups = qualifier_terms(q)
    if not groups:
        return None
    words, remotes = command_words(hit.get("argv") or hit["text"].split(), hit.get("heads") or [])
    root = hit.get("root") or "."
    if not strict:
        text = hit["text"].lower()
        files = hit.get("targets") or local_args(hit)

        def appears(t):
            if t.lower() in text or (term_kind(t) == "path" and os.path.expanduser(t).lower() in text):
                return True
            return any(path_under(p, t, root) for p in files) if term_kind(t) == "path" else word_meets(t, words, remotes)
        return all(any(all(appears(t) for t in alt) for alt in alts) for alts in groups)
    paths = []
    for alts in groups:
        named = []
        for alt in alts:
            paths += [t for t in alt if term_kind(t) == "path"]
            rest = [t for t in alt if term_kind(t) != "path"]
            if rest:
                named.append(rest)
        if named and not any(all(word_meets(t, words, remotes) for t in alt) for alt in named):
            return False
    if paths:
        files = hit.get("targets") or local_args(hit)
        return bool(files) and all(any(path_under(p, t, root) for t in paths) for p in files)
    return True


def overlaps(release, frozen, cat):
    """Does a pending decision's frozen item hold a release back: the same category, and the same qualifier
    or either one the whole category (ledger.overlaps); also two qualifiers that name the same application, and
    a frozen qualifier the hook cannot compare, which stands for the whole category."""
    qa, qb = str(release.get("qualifier") or "").strip(), str(frozen.get("qualifier") or "").strip()
    if not qa or not qb or qa.lower() == qb.lower():
        return True
    if cat.get("apps"):
        a, b = set(qualifier_apps(cat, qa)), set(qualifier_apps(cat, qb))
        return not a or not b or bool(a & b)
    return not qualifier_terms(qb)


def decision_label(entry):
    return "#%s（%s）" % (entry.get("id"), entry.get("title") or "决策包")


def protected_verdict(who, data, policy, hit):
    """Protected paths are never released by a decision: as an edit of them, a quality session asks; the
    researcher's own session asks too; any other session is denied."""
    where = next((p for p in hit["paths"] if p != "."), "项目目录（里面有受保护路径）" if hit["paths"] else "受保护路径")
    what = "`%s`" % short(hit["text"])
    if who.role in policy.get("h_writers", []):
        if data.get("permission_mode") in pool.BYPASS_MODES:
            v = Verdict("deny", "%s 会改评测协议的受保护路径 %s，要研究员确认；当前是 %s 模式，确认框弹不出来，请研究员自己改。" % (
                what, where, data["permission_mode"]))
        else:
            v = Verdict("ask", "%s 会改评测协议的受保护路径 %s（评测脚本、评测集和接口），改动要研究员确认。" % (what, where))
    elif who.researcher:
        v = refuse(who, data, "%s 会删除、移动、覆盖受保护路径 %s 或改它的权限（任务文件登记的评测脚本、评测集和接口）。" % (what, where))
    else:
        v = Verdict("deny", "（%s）不能用 shell 命令删除、移动、覆盖受保护路径，也不能改它们的权限：%s 动到了 %s。任务文件登记的评测脚本、"
                            "评测集和接口在优化过程中保持不变；只有质量序列能提出修改，并且要研究员签字。" % (who.label, what, where))
    v.irreversible.append({"category": hit["cat"]["name"], "command": short(hit["text"]), "result": "protected", "decisions": []})
    return v


def gate_verdict(who, data, policy, hit, pending, releases, problem):
    """The verdict on one hit: frozen by a pending decision, released by a decided one, or neither."""
    cat = hit["cat"]
    if cat.get("release") == "researcher_session":
        return protected_verdict(who, data, policy, hit)
    what, name = "`%s`" % short(hit["text"]), cat["name"]
    record = {"category": name, "command": short(hit["text"])}
    if hit.get("app"):
        record["qualifier"] = hit["app"]
    frozen = [(e, f) for e in pending.values() for f in (e.get("frozen") if isinstance(e.get("frozen"), list) else [])
              if isinstance(f, dict) and f.get("kind") == "category" and f.get("category") == cat["id"]]
    stopping = [(e, f) for e, f in frozen if covers(f.get("qualifier"), cat, hit, strict=False) is not False]
    if stopping:
        e, f = stopping[0]
        v = refuse(who, data, "%s 属于不可逆事项清单里的“%s”，待定决策 %s冻结了“%s”：决策定下以前谁都不做。先做选项中立的准备；"
                              "要提前做，请研究员先定这条决策。" % (what, name, decision_label(e), f.get("text") or name))
        v.irreversible.append(dict(record, result="frozen", decisions=sorted({str(x.get("id")) for x, _ in stopping})))
        return v
    own = [r for r in releases if r.get("category") == cat["id"]]
    fits = [r for r in own if covers(r.get("qualifier"), cat, hit) is True]
    held = [r for r in fits if any(overlaps(r, f, cat) for _, f in frozen)]
    active = [r for r in fits if r not in held]
    if active:
        ids = sorted({str(r.get("decision")) for r in active})
        v = Verdict()
        v.released.append("放行：%s 属于不可逆事项清单里的“%s”，由决策 %s放行。" % (what, name, "、".join(
            "#%s（选项“%s”：%s）" % (r.get("decision"), r.get("option") or "", r.get("text") or name) for r in active)))
        v.irreversible.append(dict(record, result="released", decisions=ids))
        return v
    why = []
    if problem:
        why.append(problem + "。")
    for r in held:
        e, f = next(((x, y) for x, y in frozen if overlaps(r, y, cat)), ({}, {}))
        why.append("决策 #%s 放行的“%s”暂停：待定决策 %s冻结了和它重叠的“%s”。" % (
            r.get("decision"), r.get("text") or name, decision_label(e), f.get("text") or name))
    for r in own:
        if r not in fits:
            unread = covers(r.get("qualifier"), cat, hit) is None
            why.append("决策 #%s 放行的是“%s”，%s。" % (r.get("decision"), r.get("text") or name,
                                               ("限定里没有应用名单里的应用（%s），hook 认不出这条命令在不在里面" % "、".join(
                                                   str(a.get("name")) for a in cat.get("apps") or []) if cat.get("apps") else
                                                "限定里没有能和命令对上的词（命令名、主机名、路径这类），hook 认不出这条命令在不在里面")
                                               if unread else "这条命令不在里面"))
    v = refuse(who, data, "%s 属于不可逆事项清单里的“%s”，没有研究员批准、所选选项里列了它的决策。%s要做就写决策包：选项表“要放行的不可逆动作”"
                          "一列写“%s”（可以加限定，写命令里看得到的词），研究员选了这个选项、书记员按它确认以后放行；+1、+0 写进交给上一级的交付包。" % (
                              what, name, "".join(why), name))
    v.irreversible.append(dict(record, result="no_release", decisions=sorted({str(r.get("decision")) for r in held})))
    return v


def same_word(word, frozen, where, root):
    """Is a command's word the word of a frozen prefix: the same, or the same path once both are full paths (the
    command's from its directory, the prefix's from the project root: ./tools/x.py, cd tools && x.py)."""
    if word == frozen:
        return True
    a, b = full_path(word, where), full_path(frozen, root)
    return a.lower() == b.lower() if FOLD_CASE else a == b


def prefix_meets(cand, words, where, root):
    """Does a command start with a frozen command prefix? The first word by its file name (any python counts for
    python3); the others in order, skipping the options in between with one value each (python3 -I x.py, docker
    compose -f infra.yml up), paths compared as paths (same_word)."""
    if not cand:
        return False
    head, first = os.path.basename(cand[0]).lstrip("\\"), os.path.basename(words[0])
    if head != first and not (PYTHON.search(head) and PYTHON.search(first)):
        return False
    i = 1
    for w in words[1:]:
        after_option = False
        while i < len(cand) and not same_word(cand[i], w, where, root):
            if cand[i].startswith("-") and len(cand[i]) > 1:
                after_option = True
            elif after_option:
                after_option = False              # the value of the option just skipped
            else:
                return False
            i += 1
        if i >= len(cand):
            return False
        i += 1
    return True


def frozen_commands(runs, pending, root):
    """[(decision, frozen item, command as written)] for each simple command that starts with a frozen command prefix
    (prefix_meets)."""
    items = [(e, f, [str(w) for w in f["words"]]) for e in pending.values()
             for f in (e.get("frozen") if isinstance(e.get("frozen"), list) else [])
             if isinstance(f, dict) and f.get("kind") == "command" and isinstance(f.get("words"), list) and len(f["words"]) >= 2]
    out = []
    for argv, where, info in runs if items else []:
        if not argv:
            continue
        cands = word_cands(argv, info["wrappers"])
        for e, f, words in items:
            if any(prefix_meets(c, words, where, root) for c in cands):
                out.append((e, f, " ".join(list(info["wrappers"]) + list(argv))))
    return out


def owned_hard(policy):
    """The paths only init and the plugin's scripts write: owned_paths with nobody as owner."""
    return [p for p, owners in (policy.get("owned_paths") or {}).items() if not owners]


def check_owned_writes(who, policy, root, runs):
    """A shell command that overwrites, moves or deletes a path only init and the plugin's scripts write (the hooks,
    settings, role definitions, the local state with the claims and gates.json, spec/task.json): denied in every
    session, as an edit of them is. ledger.py and route.py write the state themselves, not through the shell."""
    hard, group = owned_hard(policy), {"paths": {"owned": owned_hard(policy)}}
    for argv, where, info in runs if hard else []:
        if argv is None:
            continue
        written, subtree = path_targets(argv, where)
        mine = [p for p in subtree if any(path_forms(p, root)[0])]  # rm -rf . and rm -rf .. are the protected paths' rule
        found = (group_paths(["owned"], info["writes"] + written, group, root, policy)
                 + group_paths(["owned"], mine, group, root, policy, True))
        if found:
            text = short(" ".join(list(info["wrappers"]) + list(argv) + list(info["redirects"])))
            return Verdict("deny", "（%s）不能用 shell 命令覆盖、挪走或删除 %s：`%s` 动到的是只由初始化和插件的脚本写的文件（hook、配置、角色定义，"
                                   "认领表和本地决策状态只由 route.py 和书记员跑的 ledger.py 写），在任何会话里都不手改；要改请研究员改插件或任务文件后重跑 init。" % (
                                       who.label, found[0][0], text))
    return None


def check_irreversible(data, who, policy, root, command, risk, runs=None):
    """The irreversible list and the pending decisions' frozen actions, for one shell command (runs: its
    command_runs with detail, when already worked out)."""
    cats = irreversible_categories(policy)
    pending, releases, problem = load_gates(root)
    if not cats and not pending:
        return Verdict()
    cwd = data.get("cwd") or root
    runs = command_runs(command, cwd, root, detail=True) if runs is None else runs
    hits, unread = irreversible_hits(runs, root, policy, cats)
    hits += lark_hits(command, cwd, root, risk, cats)
    verdict = Verdict()
    for e, f, text in frozen_commands(runs, pending, root):
        v = refuse(who, data, "`%s` 被待定决策 %s冻结（冻结的动作：%s）：决策定下以前谁都不做。先做选项中立的准备；要提前做，请研究员先定这条决策。" % (
            short(text), decision_label(e), f.get("text") or f.get("prefix")))
        v.irreversible.append({"category": "冻结的命令", "command": short(text), "result": "frozen", "decisions": [str(e.get("id"))]})
        verdict.merge(v)
    for text, names in unread:
        verdict.merge(refuse(who, data, "看不清这条命令，里面像是有不可逆事项清单里的命令（%s）：写成能看清的单条命令再试。" % "、".join(names)))
    for hit in hits:
        verdict.merge(gate_verdict(who, data, policy, hit, pending, releases, problem))
    return verdict


def check_edit_irreversible(data, who, policy):
    """An edit tool writing a file a category of the list watches (Docker Desktop's settings). Protected paths
    keep their own rule in check_path."""
    cats = irreversible_categories(policy)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not cats or not isinstance(path, str) or not path:
        return Verdict()
    root = project_root(data)
    full = full_path(path, root)
    hits = []
    for cat in cats:
        for rule in cat["match"]:
            group = rule.get("writes")
            if group and group != "protected" and in_group(group, full, cat, root, policy):
                hits.append({"cat": cat, "text": "%s %s" % (data.get("tool_name"), path), "argv": [str(data.get("tool_name")), path],
                             "heads": [str(data.get("tool_name"))], "where": root, "root": root, "app": None, "paths": [path],
                             "targets": [full]})
                break
    if not hits:
        return Verdict()
    pending, releases, problem = load_gates(root)
    verdict = Verdict()
    for hit in hits:
        verdict.merge(gate_verdict(who, data, policy, hit, pending, releases, problem))
    return verdict


def check_ledger(data, who, policy, root, sub):
    scribe = policy.get("scribe_role", "pmo-scribe")
    fixed = (policy["agents"].get(scribe) or {}).get("fixed_session") or scribe
    pl, ok = pool.load(root)
    if not ok or len(pool.claims_of(pl, scribe)) > 1:
        return Verdict("deny", "认领表读不出来，或者书记员的认领不止一条：为免两处同时写台账，所有会话都先停写。"
                               "请研究员用 route.py pool 核对，release 多余的认领。")
    if who.origin == "claim" and who.role == scribe:
        if pool.scribe_ok(pl, policy, who.claim):
            return Verdict("allow", "唯一认领的书记员 %s 写台账" % who.name)
        return Verdict("deny", "这条书记员认领已经失效；请研究员用 route.py pool 核对。")
    return refuse(who, data, "（%s）不能运行 ledger.py %s：工作项、字段、状态和文档只由书记员 %s 写。写成草稿，给它发【%s】。" % (
        who.label, sub, fixed, (policy.get("msg") or {}).get("register_request", "登记请求")))


def check_bash(data, who, policy, risk, soft, command=None):
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or "" if command is None else command
    root = project_root(data)
    verdict = Verdict()
    for found in (check_identity_vars(data, who, command), check_pool_command(data, who, policy, root, command),
                  check_setup(data, who, root, command), check_git(data, who, command, root)):
        if found:
            verdict.merge(found)
    if verdict.decision == "deny":
        return verdict
    runs = command_runs(command, data.get("cwd") or root, root, detail=True)
    owned = check_owned_writes(who, policy, root, runs)
    if owned:
        return verdict.merge(owned)
    verdict.merge(check_irreversible(data, who, policy, root, command, risk, runs))
    if verdict.decision == "deny":
        return verdict
    ledger = ledger_write(command, data.get("cwd") or root)
    if ledger:
        verdict.merge(check_ledger(data, who, policy, root, ledger))
        if verdict.decision == "deny":
            return verdict
    parsed = fc.parse(command, data.get("cwd") or root)
    if parsed.error and parsed.mentions:
        return verdict.merge(refuse(who, data, "命令里用到飞书命令行，但%s；请改成单条简单命令。" % parsed.error))
    hidden = [o for o in parsed.opaque if "飞书" in o or parsed.mentions]
    if hidden:
        return verdict.merge(refuse(who, data, "看不到实际执行的飞书命令（%s）；请直接写出单条命令。" % "；".join(hidden)))
    if not parsed.invocations:
        return verdict
    rewritable = (parsed.simple and len(parsed.invocations) == 1 and data.get("tool_name") == "Bash"
                  and "$" not in command and "`" not in command)
    for inv in parsed.invocations:
        c = fc.classify(inv, risk)
        if inv.cli == "lark":
            v, new_args = check_lark(inv, c, who, policy, data, soft, rewritable)
        else:
            v, new_args = check_meegle(inv, c, who, data, soft, rewritable)
        verdict.merge(v)
        if new_args is not None and v.decision in ("allow", "ask"):
            updated = dict(tool_input)
            updated["command"] = shlex.join(inv.prefix + new_args)
            verdict.updated = updated
    if verdict.decision == "deny":
        verdict.updated = None
    return verdict


# --------------------------------------------------------------- files
EDIT_WINDOW = datetime.timedelta(minutes=30)


def note_edit(root, rel, who, data, soft):
    """Remind both sides when two sessions change the same file within 30 minutes."""
    me = who.name or "%s:%s" % (who.role, (data.get("session_id") or "")[:8])
    path = os.path.join(pool.state_dir(root), "edits.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            edits = fc.load_json(path, {}) or {}
            at = pool.now()
            fresh = {}
            for k, rows in edits.items():
                keep = [r for r in rows if (pool.parse_time(r.get("at")) or at) > at - EDIT_WINDOW]
                if keep:
                    fresh[k] = keep
            others = sorted({r["who"] for r in fresh.get(rel, []) if r.get("who") != me})
            if others:
                soft.append("%s 在 30 分钟内也被 %s 改过；先和对方对一下，别互相覆盖。" % (rel, "、".join(others)))
            fresh.setdefault(rel, [])
            fresh[rel] = [r for r in fresh[rel] if r.get("who") != me] + [{"who": me, "at": pool.stamp(at)}]
            pool.save_json(path, fresh)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


FOLD_CASE = sys.platform == "darwin"             # macOS volumes ignore case unless formatted otherwise


def resolved(path):
    """The real path, symlinks followed; a path that does not exist yet keeps its missing tail."""
    head, tail = os.path.normpath(path), []
    while head and not os.path.exists(head) and os.path.dirname(head) != head:
        head, part = os.path.dirname(head), os.path.basename(head)
        tail.insert(0, part)
    return os.path.join(os.path.realpath(head), *tail) if tail else os.path.realpath(head)


def inside(full, root):
    if FOLD_CASE:
        full, root = full.lower(), root.lower()
    return full == root or full.startswith(root + os.sep)


def path_matches(rel, patterns):
    if FOLD_CASE:
        return matches(rel.lower(), [p.lower() for p in patterns])
    return matches(rel, patterns)


def relative(full, root):
    if not inside(full, root):
        return None
    return full[len(root) + 1:].replace(os.sep, "/") if len(full) > len(root) else ""


def check_path(data, who, policy, soft):
    """Rules hold for the path as written and for where it leads after symlinks: a protected path
    that is a symlink stays protected, and a link into a protected path is protected too."""
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return Verdict()
    lex_root, real_root = project_root(data), os.path.realpath(project_root(data))
    lexical = os.path.normpath(path if os.path.isabs(path) else os.path.join(lex_root, path))
    full = resolved(lexical)
    rels = []
    for r in (relative(lexical, lex_root), relative(full, real_root), relative(lexical, real_root)):
        if r is not None and r not in rels:
            rels.append(r)
    if not rels:
        tmpdir = os.environ.get("TMPDIR", "")
        if full.startswith(TMP_PREFIXES) or (tmpdir and full.startswith(os.path.realpath(tmpdir))):
            return Verdict()
        if not who.researcher:
            soft.append("（%s）写到了项目目录和临时目录以外：%s；确认这是该写的地方。" % (who.label, path))
        return Verdict()
    rel = rels[0]
    if any(path_matches(r, policy["h_paths"]) for r in rels):
        if who.role in policy["h_writers"]:
            if data.get("permission_mode") in pool.BYPASS_MODES:
                return Verdict("deny", "评测协议用到的文件改动要研究员确认；当前是 %s 模式，确认框弹不出来，请研究员自己改。" % data["permission_mode"])
            return Verdict("ask", "评测协议用到的文件（评测脚本、评测集和接口），改动要研究员确认。")
        return Verdict("deny", "评测协议用到的文件（评测脚本、评测集和接口）在优化过程中保持不变；只有质量序列能提出修改，并且要研究员签字。")
    try:
        note_edit(project_root(data), rel, who, data, soft)
    except Exception:                          # a soft check never blocks
        pass
    soft_owned = policy.get("soft_owned") or {}
    owned = False
    for r in rels:                              # each path: a soft-owned pattern first, then the first owned one
        if any(path_matches(r, [pattern]) for pattern in soft_owned):
            continue
        for pattern, owners in policy["owned_paths"].items():
            if path_matches(r, [pattern]):
                if who.role not in owners:
                    owner = "、".join(owners) if owners else "研究员本人（改插件或任务文件后重跑 init）"
                    return Verdict("deny", "%s 只能由 %s 修改。" % (pattern, owner))
                owned = True
                break
    for pattern, owners in soft_owned.items():
        if any(path_matches(r, [pattern]) for r in rels):
            if who.role not in owners and not who.researcher:
                soft.append("%s 照规范由 %s 提出修改；改了要在交付里写明原因。" % (pattern, "、".join(owners) or "研究员本人"))
            return Verdict()
    if owned:
        return Verdict()
    if path_matches(rel, policy["x_paths"]) and who.role not in policy["x_writers"]:
        if not who.researcher:
            soft.append("可改范围里的代码照规范只由工程序列修改；改了要在交付里写明。")
        return Verdict()
    if path_matches(rel, who.pol.get("write_paths", [])):
        return Verdict("allow", "在 %s 的写入范围内" % who.label)
    if not who.researcher:
        soft.append("（%s）的写入范围是：%s；%s 不在里面，确认是你该改的。" % (
            who.label, "、".join(who.pol.get("write_paths", [])) or "无", rel))
    return Verdict()


def check_agent(data, who, policy, soft):
    tool_input = data.get("tool_input") or {}
    if tool_input.get("name"):
        return Verdict("deny", "派子智能体时不要带 name：开着 agent teams 时，带 name 的派发会变成队员。去掉 name 重试。")
    target = tool_input.get("subagent_type") or "general-purpose"
    if target == policy.get("scribe_role"):
        fixed = (policy["agents"].get(target) or {}).get("fixed_session") or target
        return Verdict("deny", "不用 Agent 工具派书记员：登记请求发消息给 %s。" % fixed)
    if target in policy["agents"]:
        soft.append("要委托 %s，就给它的会话发消息（先跑 route.py pool --role %s），不要用 Agent 工具另派一个池外的实例。" % (target, target))
    return Verdict()


# --------------------------------------------------------- agent turns
WRAPUP_TOOL = re.compile(r"^mcp__.+__wrap_up$")       # the desktop app's turn wrap-up; setup.py WRAPUP_TOOL
ASK_KEYS = ("ask", "actions", "blocked")              # the wrap-up's question to the researcher


def agent_turn(data, who):
    """The status of a pool session whose current turn another agent session started (inbox.py wrote
    turn_origin agent); None for any other turn, and for a session that holds no claim (the
    researcher's own, one not claimed). An older value (message, other) is not agent."""
    if who.origin != "claim":
        return None
    st = pool.load_session(project_root(data), data.get("session_id") or "")
    return st if st.get("turn_origin") == "agent" else None


def filled(value):
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def check_wrapup(data, who, policy):
    """In a turn another agent session started the wrap-up keeps its recap and loses its question,
    except the one question the entry may ask in the turn that registered a decision that jumps
    the queue (ask_ok). A wrap-up without a question is left alone."""
    st = agent_turn(data, who)
    tool_input = data.get("tool_input")
    if st is None or not isinstance(tool_input, dict) or not any(filled(tool_input.get(k)) for k in ASK_KEYS):
        return Verdict()
    if st.get("ask_ok") and who.role == policy.get("entry_role"):
        return Verdict(gate="allowed_urgent", note="本轮登记了插队决策 #%s，可以问这一次。" % st["ask_ok"])
    verdict = Verdict("allow", "其他 agent 会话叫醒的轮次，收尾里的提问已去掉", gate="stripped",
                      note="这一轮是其他 agent 会话叫醒的：收尾里的提问已去掉，只留回顾。")
    verdict.updated = {k: v for k, v in tool_input.items() if k not in ASK_KEYS}
    return verdict


def check_ask(data, who, policy):
    """In a turn another agent session started nobody answers AskUserQuestion: denied in every pool
    session. The researcher's turns and the app's are left alone."""
    st = agent_turn(data, who)
    if st is None:
        return Verdict()
    decide = (policy.get("msg") or {}).get("decide", "需拍板")
    reason = ("这一轮是其他 agent 会话叫醒的，研究员多半不在：不用 AskUserQuestion。要研究员拍板的事写决策包或发【%s】给入口；"
              "+1、+0 写进交给上一级的交付包。" % decide)
    if who.role == policy.get("entry_role"):
        note = ("本轮登记了插队决策 #%s：在收尾里问研究员这一次。" % st["ask_ok"] if st.get("ask_ok")
                else "要研究员拍板的事写成决策包，请书记员登记进决策队列。")
    elif (policy["agents"].get(who.role) or {}).get("rank") == 2:
        note = "要研究员拍板的事写成决策包，或者写【%s】发给入口。" % decide
    else:
        note = "要拍板的事写进交给上一级的交付包。"
    return Verdict("deny", reason, gate="denied", note=note)


def log(root, data, who, verdict, soft):
    try:
        path = os.path.join(root, ".claude", "logs", "guard.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_id": data.get("session_id"), "tool": data.get("tool_name"),
            "agent_type": data.get("agent_type"), "has_agent_id": bool(data.get("agent_id")),
            "role": who.role, "origin": who.origin, "session_name": who.name,
            "desk": os.environ.get(pool.DESK_ENV) or None, "permission_mode": data.get("permission_mode"),
            "decision": verdict.decision, "reason": "；".join(verdict.reasons)[:200],
            "soft_hits": [h[:160] for h in soft],
        }
        if verdict.gate:
            rec["agent_turn_gate"] = verdict.gate
        if verdict.irreversible:                            # each category of the list met, and what became of it
            rec["irreversible"] = verdict.irreversible
            released = sorted({d for g in verdict.irreversible if g.get("result") == "released" for d in g.get("decisions") or []})
            if released and verdict.decision != "deny":
                rec["released_by"] = released               # the decisions whose releases let the call through
        if WRAPUP_TOOL.match(str(data.get("tool_name"))):   # route.py audit: questions left in, by who started the turn
            tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
            rec["asked"] = any(filled(tool_input.get(k)) for k in ASK_KEYS)
            rec["turn_origin"] = pool.load_session(root, data.get("session_id") or "").get("turn_origin")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def touch(root, data, who):
    """A tool call means the session is working; remember when. A subagent's call
    (a background one, say) also ends a wait that was its own or nobody's (pool.end_wait); the
    session's own call, other than the one a permission prompt was raised for, means the prompt
    was answered."""
    sid = data.get("session_id") or ""
    stamp = pool.stamp()

    def change(st):
        st["last_tool_at"] = stamp
        if data.get("agent_id"):
            pool.end_wait(st, data["agent_id"])
            return
        if not str(st.get("status", "")).startswith("waiting"):
            st["status"] = "busy"
            st["status_at"] = stamp
        elif st.get("waiting_tool_use_id") != data.get("tool_use_id"):
            pool.end_wait(st)
    try:
        pool.update_session(root, sid, change)
    except OSError:
        pass


def trusted_scripts(root):
    """Plugin scripts that still match the manifest: the copies in .claude/solo-lab/bin/, and the
    ones the researcher's session runs from where the plugin is installed (setup.py)."""
    m = fc.load_json(os.path.join(root, ".claude", "solo-lab", "manifest.json"), {}) or {}
    out = set()
    for rel, digest in (m.get("files") or {}).items():
        if rel.startswith(".claude/solo-lab/bin/") and rel.endswith(".py"):
            full = os.path.join(root, rel)
            try:
                with open(full, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == digest:
                        out.add(os.path.normpath(full))
            except OSError:
                pass
    for path, digest in (m.get("plugin_scripts") or {}).items():     # setup.py and its like, where the plugin is installed
        try:
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == digest:
                    out.add(os.path.normpath(path))
        except OSError:
            pass
    return out


def evaluate(data):
    """(verdict, soft reminders) for one PreToolUse call."""
    root = project_root(data)
    fc.TRUSTED = trusted_scripts(root)
    policy = fc.load_json(os.path.join(HERE, "policy.json"))
    if not policy:
        raise RuntimeError("读不到 policy.json")
    if policy.get("version") != POLICY_VERSION:
        raise RuntimeError("policy.json 是第 %s 版，这个 guard.py 要第 %d 版：两者不配套，硬规则会悄悄变样，所以先全部拒绝。"
                           "请研究员在 Claude 以外的终端里把 .claude/hooks/ 下的 guard.py.new 和 policy.json.new 一起合并，"
                           "再运行 setup.py init --apply" % (policy.get("version"), POLICY_VERSION))
    risk = fc.load_json(os.path.join(HERE, "lark-risk.json"), {"commands": {}})
    who = pool.identify(data, policy, root)
    soft = []
    tool = data.get("tool_name")
    if tool == "Bash":
        verdict = check_bash(data, who, policy, risk, soft)
    elif tool == "Monitor" or str(tool).endswith("run_in_terminal"):   # shell commands outside the Bash tool
        command = (data.get("tool_input") or {}).get("command")
        verdict = check_bash(data, who, policy, risk, soft, command) if command else Verdict()
    elif tool in ("Edit", "Write", "NotebookEdit"):
        verdict = check_path(data, who, policy, soft).merge(check_edit_irreversible(data, who, policy))
    elif tool in ("Agent", "Task"):
        verdict = check_agent(data, who, policy, soft)
    elif tool == "AskUserQuestion":
        verdict = check_ask(data, who, policy)
    elif WRAPUP_TOOL.match(str(tool)):
        verdict = check_wrapup(data, who, policy)
    else:
        verdict = Verdict()
    touch(root, data, who)
    log(root, data, who, verdict, soft)
    return verdict, soft


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook 输入不是 JSON 对象")
        verdict, soft = evaluate(data)
        out = {"hookEventName": "PreToolUse"}
        if verdict.decision:
            out["permissionDecision"] = verdict.decision
            out["permissionDecisionReason"] = "；".join(verdict.reasons) or verdict.decision
            if verdict.updated is not None:
                out["updatedInput"] = verdict.updated
        context = verdict.context() + ["提醒：" + s for s in soft]
        if context:
            out["additionalContext"] = "\n".join(context)
        if len(out) > 1:
            sys.stdout.write(json.dumps({"hookSpecificOutput": out}, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except Exception as e:  # fail closed: exit 2 blocks; any other exit code would let the call through
        try:
            sys.stderr.write("[guard] 检查出错，按拒绝处理：%r\n" % e)
        finally:
            sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
