#!/usr/bin/env python3
"""Lint for the Solo-Lab plugin repository. Standard library only, no Feishu access.

    python3.12 -I tools/lint.py [--root <another checkout of this repository>]

Each rule comes from a defect that reached a release and was met in a project. A rule looks for what caused
the defect, not for the one place it showed: the next of its kind is to stop here. The exit code is 1 when a
rule finds something, and also when a rule finds nothing to look at (then the rule is what broke).

    long-line      1.0.4 to 1.0.6   setup.py did not start under Python 3.9
    range-read     1.0.4 to 1.0.7   docs.py revise read nothing of the main document's opening
    project-file   up to 1.0.7      runs/README.md got a .new at every init
    template-line  up to 1.0.9      a draft that copied the template's 关联工作项 line was sent back
    two-gates      1.0.10           the scribe's wiki +node-delete passed the hook and was refused by the settings
"""
import argparse
import ast
import contextlib
import fnmatch
import io
import json
import os
import re
import sys
import tempfile

PLUGIN = os.path.join("plugins", "solo-lab")
SKIP_DIRS = {".git", "__pycache__", "worktrees"}

# Python 3.9, the python3 macOS ships, reads a script it is given as a file one line at a time into a buffer of
# 1024 bytes at the smallest; a longer line comes in pieces, and a piece that ends inside a multi-byte character
# is "SyntaxError: Non-UTF-8 code". A line of 1023 bytes, newline included, is always read whole.
LINE_LIMIT = 1000

# A generated file that says this is one the project writes into.
INVITES = re.compile(r"[写记补填][进在入到]本文件")


def python_files(root):
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found += [os.path.join(folder, n) for n in sorted(names) if n.endswith(".py")]
    return found


def long_line(root):
    """No line of a Python file is longer than LINE_LIMIT bytes.

    setup.py held two lines of Chinese text longer than the buffer. Where the cut fell inside a character the
    script stopped before its first statement, and whether it did changed with every edit above the line: it
    ran up to 1.0.3 and stopped from 1.0.4 on. The tests import the scripts, which compiles them from memory
    and never shows this; the sessions run them as files.
    """
    findings, files = [], python_files(root)
    for path in files:
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                if len(line) > LINE_LIMIT:
                    findings.append((path, n, "这一行 %d 字节，超过 %d：Python 3.9 把脚本当文件运行时分段读超长的行，断在汉字中间就报 "
                                              "SyntaxError。把长字符串写成相邻的几段，一段一行" % (len(line), LINE_LIMIT)))
    return findings, "%d 个 Python 文件" % len(files), len(files)


def words_of(node):
    """The string constants among a call's arguments or a list's elements, None where there is something else."""
    parts = node.args if isinstance(node, ast.Call) else node.elts
    return [p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else None for p in parts]


def range_read(root):
    """A `docs +fetch --scope range` names both ends of the range.

    revise read the opening of a main document as a range that named only its end. lark-cli (1.0.98) answers
    such a range with the end block alone, though its help calls the start optional, so every revise stopped.
    The command had only run against the tests' stand-in for lark-cli, which answered with the document up to
    the end block. What a tool does when an end is left out is not something a script may lean on.
    """
    findings, fetches, ranges = [], 0, 0
    for path in python_files(os.path.join(root, PLUGIN)):
        if os.sep + "tests" + os.sep in path:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Call, ast.List, ast.Tuple)):
                continue
            words = words_of(node)
            if "+fetch" not in words:
                continue
            fetches += 1
            if ("--scope", "range") not in zip(words, words[1:]):
                continue
            ranges += 1
            missing = [flag for flag in ("--start-block-id", "--end-block-id") if flag not in words]
            if missing:
                findings.append((path, node.lineno, "按范围读文档没有给 %s：lark-cli 对只给一头的范围只返回那一头的块，不是“从开头读到这里”。"
                                                    "两头都给；不知道另一头的 block id，就读整篇文档来找" % "、".join(missing)))
    return findings, "%d 处读文档的调用，其中按范围读的 %d 处" % (fetches, ranges), fetches


def project_file(root):
    """A generated file that asks the project to write into it is the project's own once it is there.

    The template of runs/README.md says the record format is to be written into it. A project that did so had
    the file taken for one changed locally: every init wrote a .new beside it that held the file's own first
    lines, and check reported it as changed and as waiting for a merge. Such a file is planned with keep=True,
    and what the project wrote into it is no finding of the local check.
    """
    scripts = os.path.join(root, PLUGIN, "scripts")
    sys.path.insert(0, scripts)
    import setup
    import setup_core
    import check
    setup_core.run = lambda cmd, timeout=30: (None, "", "lint does not call %s" % cmd[0])     # no lark-cli, meegle or claude
    source = os.path.join(scripts, "setup_core.py")
    findings = []
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        plan = setup_core.build_plan(tmp, os.path.join(root, PLUGIN, "scaffold", "task.example.json"), None)
        setup.apply_plan(tmp, plan, setup_core.actions_for(tmp, plan))
        invited = sorted(rel for rel, (content, _, _) in plan.files.items() if INVITES.search(content))
        for rel in invited:
            if not plan.files[rel][2]:
                findings.append((source, line_of(source, rel), "%s 要项目把内容写进去，init 却把它当成插件的文件：项目写了以后，每次 init 都在旁边写 "
                                                               ".new，check 报“改过”。生成它的那一行加 keep=True" % rel))
                continue
            with open(os.path.join(tmp, rel), "a", encoding="utf-8") as f:
                f.write("\n项目写进来的内容\n")
        task = os.path.join(tmp, "spec", "task.json")
        kinds = {rel: kind for kind, rel, _ in setup_core.actions_for(tmp, setup_core.build_plan(tmp, task, None))}
        with open(os.path.join(tmp, ".claude", "solo-lab", "manifest.json"), encoding="utf-8") as f:
            problems, _ = check.local_layer(tmp, json.load(f))
        for rel in invited:
            if not plan.files[rel][2]:
                continue
            if kinds.get(rel) != "kept":
                findings.append((source, line_of(source, rel), "%s 里项目写了内容以后，init 没有把它原样留着（处理成了 %s）" % (rel, kinds.get(rel))))
            for problem in problems:
                if rel in problem:
                    findings.append((source, line_of(source, rel), "%s 里项目写了内容以后，check 报“%s”：把它加进 local_layer "
                                                                   "里不比对的那几个文件" % (rel, problem)))
    return findings, "%d 个生成的文件，其中要项目往里写的 %d 个" % (len(plan.files), len(invited)), len(invited)


def template_line(root):
    """The 关联工作项 line of a template, copied as it stands, is one the register script takes.

    Two templates read 待建 with a note in brackets after it on that line (the decision template since 1.0.9),
    and register knew 待建 alone: a decision written from the template was sent back at its first
    registration. The template and the script were changed apart and nothing held one against the other.
    Every template line that starts with the placeholder is given to the script's own reading of that line.
    """
    scripts = os.path.join(root, PLUGIN, "scripts")
    sys.path.insert(0, scripts)
    import ledger
    import ledger_core
    findings, lines = [], 0
    for folder, dirs, names in os.walk(os.path.join(root, PLUGIN, "skills")):
        dirs[:] = sorted(dirs)
        for name in sorted(n for n in names if n.endswith(".md")):
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f.read().split("\n"), 1):
                    if not re.match(r"- 关联工作项[：:]\s*(待建|待登记)", line):
                        continue
                    lines += 1
                    try:
                        ledger_core.link_line(line + "\n", "#1", "1")
                    except ledger_core.LedgerError:
                        findings.append((path, n, "模板的这一行照抄进草稿，登记脚本不认（它读到的是“%s”）：这一行只写“待建”，"
                                                  "说明写到模板外面" % line.split("：", 1)[-1].strip()))
    return findings, "%d 行写着“待建”的模板包头" % lines, lines


def two_gates(root):
    """A Feishu command a role may run by the hook's policy is not refused by the project's permission rules.

    A command passes two gates: Claude Code's permission rules in .claude/settings.json, which no hook can
    lift, and guard.py, which judges by role. 1.0.10 let the scribe delete documents and opened the hook
    alone: the settings went on denying every command with --yes and every high-risk command, and in a
    project the scribe's delete was refused. The tests asked the hook; nothing asked the settings. Here
    every lark-cli command a role's policy lists is held against the deny rules init writes.
    """
    scripts = os.path.join(root, PLUGIN, "scripts")
    sys.path.insert(0, scripts)
    import setup_core
    setup_core.run = lambda cmd, timeout=30: (None, "", "lint does not call %s" % cmd[0])
    source = os.path.join(scripts, "setup_core.py")
    findings, commands = [], 0
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        plan = setup_core.build_plan(tmp, os.path.join(root, PLUGIN, "scaffold", "task.example.json"), None)
        deny = [r[5:-1] for r in json.loads(plan.files[".claude/settings.json"][0])["permissions"]["deny"]
                if r.startswith("Bash(") and r.endswith(")")]
        agents = json.loads(plan.files[".claude/hooks/policy.json"][0])["agents"]
    for role, pol in sorted(agents.items()):
        for key in pol.get("lark_write", ()):
            commands += 1
            risky = False                    # until 1.0.12 the scribe had one high-risk command of its own, run with --yes
            command = "lark-cli %s --token x%s --as user" % (key, " --yes" if risky else "")
            hit = next((d for d in deny if fnmatch.fnmatchcase(command, d)), None)
            if hit:
                findings.append((source, line_of(source, "ADMIN_DENY = ["), "%s 的 hook 策略放行 `lark-cli %s`%s，权限规则 `Bash(%s)` 却会拒绝它："
                                 "权限规则拒绝的命令 hook 放不开，在项目里这条命令跑不了。从权限规则里去掉这一条，交给 hook 按角色判断" % (
                                     role, key, "（带 --yes）" if risky else "", hit)))
    return findings, "%d 条各角色可以运行的 lark-cli 写命令" % commands, commands


def line_of(path, text):
    with open(path, encoding="utf-8") as f:
        return next((n for n, line in enumerate(f, 1) if '"%s"' % text in line or line.startswith(text)), 1)


RULES = (("long-line", "行长", long_line), ("range-read", "按范围读文档", range_read), ("project-file", "要项目写的文件", project_file),
         ("template-line", "模板的包头", template_line), ("two-gates", "权限规则和 hook", two_gates))


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Solo-Lab 插件仓库的 lint：每条规则来自一个发布出去的缺陷")
    ap.add_argument("--root", default=here, help="要检查的仓库目录，默认是本文件所在的仓库")
    root = os.path.abspath(ap.parse_args(argv).root)
    bad = 0
    for key, title, rule in RULES:
        findings, looked_at, subjects = rule(root)
        if not subjects:
            findings = findings + [(root, 0, "这条规则没有找到可检查的对象：规则本身要改")]
        bad += len(findings)
        print("%s（%s）：检查了 %s，%s" % (title, key, looked_at, "%d 处问题" % len(findings) if findings else "没有问题"))
        for path, line, message in findings:
            print("  %s:%d  %s" % (os.path.relpath(path, root), line, message))
    print("lint：%s" % ("%d 处问题" % bad if bad else "没有问题"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
