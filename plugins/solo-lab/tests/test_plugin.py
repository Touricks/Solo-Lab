#!/usr/bin/env python3
"""Tests for the solo-lab plugin. Standard library only, no Feishu access.

    python3 -I -B tests/test_plugin.py
"""
import contextlib
import datetime
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(ROOT))
if not os.path.isfile(os.path.join(REPO, ".claude-plugin", "marketplace.json")):
    REPO = ROOT              # an installed copy (plugins/cache/<marketplace>/<plugin>/<version>): only the plugin
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import orgspec  # noqa: E402
import setup  # noqa: E402
import ledger  # noqa: E402
import route  # noqa: E402
import pool as pool_lib  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fake_space  # noqa: E402

TASK = os.path.join(ROOT, "scaffold", "task.example.json")


def no_cli(cmd, timeout=30):
    """setup.run during tests: no lark-cli, meegle or claude is ever called."""
    return None, "", "tests do not call %s" % cmd[0]


setup.run = no_cli
FIX = os.path.join(ROOT, "tests", "fixtures", "feishu")
EXAMPLES = os.path.join(ROOT, "skills", "packages", "references", "examples.md")


def example_blocks():
    with open(EXAMPLES, encoding="utf-8") as f:
        return re.findall(r"```markdown\n(.*?)\n```", f.read(), re.S)


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def clean_env():
    """The test process's environment without the IDs of the session that runs the tests."""
    return {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID")}


class ProjectCase(unittest.TestCase):
    """A fresh initialised project in a temp dir, with fake Feishu IDs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-lab-test-")
        self.spec = orgspec.OrgSpec()
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, TASK, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        fake_space.install(self.tmp, spec=self.spec)
        self.hooks = os.path.join(self.tmp, ".claude", "hooks")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, script, data, env=None):
        env = dict(clean_env(), CLAUDE_PROJECT_DIR=self.tmp, **(env or {}))
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(self.hooks, script)],
                           input=json.dumps(data), capture_output=True, text=True, env=env)
        out = json.loads(p.stdout) if p.stdout.strip() else {}
        return p.returncode, out.get("hookSpecificOutput", {})

    def guard(self, agent_type, tool, tool_input, agent_id="a1"):
        """A tool call from a subagent of that type; with agent_id=None, from a session nobody claimed."""
        data = {"tool_name": tool, "tool_input": tool_input, "cwd": self.tmp, "session_id": "s1",
                "agent_type": agent_type}
        if agent_id:
            data["agent_id"] = agent_id
        return self.hook("guard.py", data)

    def scribe(self, tool, tool_input):
        """A tool call from the scribe's one claimed session: the only place Feishu and the ledger are written from."""
        with open(os.path.join(self.hooks, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        with pool_lib.locked(self.tmp) as table:
            if not pool_lib.claim_by_session(table, "s-scribe"):
                permit = {"role": policy["scribe_role"], "via": "test", "by": "researcher"}
                claim, note = pool_lib.promote(table, policy, permit, "s-scribe", None)
                self.assertIsNotNone(claim, note)
        return self.hook("guard.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                                      "cwd": self.tmp, "session_id": "s-scribe"})

    def draft(self, block_no, name="d.md", edit=None):
        text = example_blocks()[block_no]
        if edit:
            text = edit(text)
        path = os.path.join(self.tmp, "packages", "drafts", "alg", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_quiet(self, fn, *args, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*args, **kw)
        return res, buf.getvalue()


# ---------------------------------------------------------------- rules
class RulesTest(unittest.TestCase):
    def test_rules_valid(self):
        self.assertEqual(orgspec.OrgSpec().validate(), [])

    def test_examples_pass_lint(self):
        spec = orgspec.OrgSpec()
        for i, block in enumerate(example_blocks()):
            errors, _ = orgspec.lint_text(spec, block)
            self.assertEqual(errors, [], "示例 %d：%s" % (i, errors))

    def test_lint_catches_problems(self):
        spec = orgspec.OrgSpec()
        bad = example_blocks()[0].replace("## 授权与边界", "## 自己加的").replace("关联工作项：#2001", "关联工作项：委托包 #2001")
        errors, _ = orgspec.lint_text(spec, bad)
        joined = "\n".join(errors)
        self.assertIn("旧包名", joined)
        self.assertIn("缺栏目“授权与边界”", joined)
        self.assertIn("不临时加栏目", joined)

    def test_no_retired_names_in_plugin_docs(self):
        spec = orgspec.OrgSpec()
        # These files describe what the live space is called today, so old names are expected there.
        allowed = {"terms.zh.json", "rules.json", "structure.md", "test_plugin.py"}
        hits = []
        for base, _, files in os.walk(REPO):
            # .git and the recorded Feishu outputs keep the names the live space uses
            if "/.git" in base or "/fixtures" in base:
                continue
            for name in files:
                if name in allowed or not name.endswith((".md", ".json", ".tmpl", ".py")):
                    continue
                with open(os.path.join(base, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                for old in spec.terms["retired"]:
                    if re.search(re.escape(old) + "(?![含括装办])", text):
                        hits.append("%s：%s" % (os.path.relpath(os.path.join(base, name), REPO), old))
        self.assertEqual(hits, [])

    def test_principles_tables_match_rules(self):
        spec = orgspec.OrgSpec()
        with open(os.path.join(ROOT, "skills", "packages", "references", "principles.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(spec.package_table(), text)
        self.assertIn(spec.relation_matrix(), text)

    def test_ledger_structure_matches_rules(self):
        spec = orgspec.OrgSpec()
        with open(os.path.join(ROOT, "skills", "ledger", "references", "structure.md"), encoding="utf-8") as f:
            text = f.read()
        for part in (spec.work_items_table(), spec.docs_tree()):
            self.assertIn(part, text)
        for gone in ("| 工作项类型 | 状态 | 允许的流转 |", "| 工作项类型 | 字段 | 标识 |"):
            self.assertNotIn(gone, text)                 # flows and fields are read from the space, not copied here

    def test_skills_have_frontmatter(self):
        for name in ("setup", "charter", "spec", "packages", "ledger", "entry", "space-read", "space-bind"):
            with open(os.path.join(ROOT, "skills", name, "SKILL.md"), encoding="utf-8") as f:
                head = f.read().split("---")[1]
            self.assertIn("name: %s" % name, head)
            self.assertIn("description:", head)


# ---------------------------------------------------------------- setup
class SetupTest(ProjectCase):
    def test_idempotent(self):
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = setup.actions_for(self.tmp, plan)
        changed = {rel for k, rel, _ in acts if k not in ("same", "kept")}
        self.assertEqual(changed, {"CLAUDE.md", "feishu/README.md"})   # they quote the new project_key
        self.assertEqual({k for k, rel, _ in acts if rel in changed}, {"update"})
        self.run_quiet(setup.apply_plan, self.tmp, plan, acts)
        plan = setup.build_plan(self.tmp, TASK, None)
        kinds = {k for k, _, _ in setup.actions_for(self.tmp, plan)}
        self.assertTrue(kinds <= {"same", "kept"}, kinds)

    def test_local_edit_goes_to_new(self):
        path = os.path.join(self.tmp, "CLAUDE.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n本地补充\n")
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = dict((rel, k) for k, rel, _ in setup.actions_for(self.tmp, plan))
        self.assertEqual(acts["CLAUDE.md"], "new")
        self.assertEqual(acts["org/decisions.md"], "same")

    def test_hook_scripts_are_updated_together(self):
        """The hooks import pool.py: one held back for a merge holds back the others it would run with."""
        with open(os.path.join(self.hooks, "pool.py"), "a", encoding="utf-8") as f:
            f.write("\n# local change\n")
        with open(os.path.join(self.hooks, "inbox.py"), "a", encoding="utf-8") as f:
            f.write("\n# an older version\n")
        manifest = os.path.join(self.tmp, ".claude", "solo-lab", "manifest.json")
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
        with open(os.path.join(self.hooks, "inbox.py"), encoding="utf-8") as f:
            m["files"][".claude/hooks/inbox.py"] = setup.sha256(f.read())      # as if the last init wrote it
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(m, f)
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = setup.actions_for(self.tmp, plan)
        kinds = dict((rel, k) for k, rel, _ in acts)
        self.assertEqual((kinds[".claude/hooks/pool.py"], kinds[".claude/hooks/inbox.py"]), ("new", "paired"))
        self.assertEqual(kinds[".claude/hooks/guard.py"], "same")
        self.assertEqual(kinds[".claude/solo-lab/bin/pool.py"], "same")                # the scripts' own copy is apart
        _, out = self.run_quiet(setup.report, self.tmp, plan, acts, False)
        self.assertIn("[和改过的那个文件配套，新版本也写 .new] .claude/hooks/inbox.py", out)
        self.assertIn("提醒：.claude/hooks/ 下的 hook 脚本共用 pool.py，要换就一起换", out)
        self.assertNotIn("guard.py 和 policy.json 要成对更新", out)
        self.run_quiet(setup.apply_plan, self.tmp, plan, acts)
        self.assertTrue(os.path.isfile(os.path.join(self.hooks, "inbox.py.new")))
        with open(os.path.join(self.hooks, "inbox.py"), encoding="utf-8") as f:
            self.assertIn("# an older version", f.read())                          # still runs with the old pool.py
        with open(os.path.join(self.hooks, "pool.py.new"), encoding="utf-8") as f:
            merged = f.read()
        with open(os.path.join(self.hooks, "pool.py"), "w", encoding="utf-8") as f:
            f.write(merged)                                                        # the researcher takes the new one
        plan = setup.build_plan(self.tmp, TASK, None)
        kinds = dict((rel, k) for k, rel, _ in setup.actions_for(self.tmp, plan))
        self.assertEqual((kinds[".claude/hooks/pool.py"], kinds[".claude/hooks/inbox.py"]), ("same", "update"))


class DecisionsFileTest(ProjectCase):
    """org/decisions.md is the researcher's, but init keeps the section the plugin wrote current."""

    # The section's list as 1.0.0 to 1.0.2 wrote it (rules.json plugin_decisions did not change in between).
    OLD = ["项目管理 +2 是入口，" + "每" + "日决策队列由它交给研究员。依据：研究员 2026-11-01 定下的入口方向。",
           "书记员除了接项目管理 +1 的需求包，也接各序列 +2、+1 的登记请求，只登记请求方自己写的草稿。原因：委托 +1、+0 之前要先有工作项 ID，请求方直接找书记员登记最快。",
           "各会话可以自己加带会话标签的评论（只追加）；工作项、字段、状态和文档仍只由书记员写。",
           "研究员可以直接找其余序列的 +2 交代，不必都经过入口；谈定的事仍写成决策包交给入口登记。依据：研究员 2026-11-02 定。",
           "会话池（研究员 2026-11-03 起采用）：各角色是研究员看得见的桌面会话，委托、交付、登记都发消息；书记员的写权只认唯一的那条认领；"
           "派发时带 name、在共享工作区里切分支或 stash 按硬规则拒绝，其余派发规则是规范加审计。",
           "同序列需求用“关系”字段区分下发和执行；反馈的主文档是导师或研究员原话所在的文档。",
           "决策包包头有“紧急程度”和“渠道”两行：决策队列按紧急程度排序；渠道写“研究员主动发起”的，登记后直接确认“决定”节点。",
           "取消送审，改由测量把关：先冻结 Eval Set 和阈值，正式分数只由质量序列跑出，结论对照签过字的阈值。依据：研究员 2026-11-11 定。",
           "五类工作项都用节点流程，退回一律是回滚节点并写原因；“阻塞”是工作项上的两个字段，不是状态。依据：研究员 2026-11-11 定。",
           "七个序列都保留；外联只写给研究员以外的读者看的材料，导师意见的登记和解析归项目管理。周报、月报由研究员对入口说一句触发，不设定时任务。依据：研究员 2026-11-12 定。",
           "流程、表单和字段清单不写在规则里，运行时读空间；脚本按含义处理的槽位写在 orgspec/contract.json，每个项目的对应关系记在 feishu/ids.json。"]
    MINE = "- 2026-11-14：评测集只用公开数据。理由：真实数据的授权还没到。决策 #7001。"

    def path(self):
        return os.path.join(self.tmp, "org", "decisions.md")

    def text(self, path=None):
        with open(path or self.path(), encoding="utf-8") as f:
            return f.read()

    def write(self, items):
        """decisions.md as an earlier init wrote it with these plugin arrangements, plus a decision of the researcher's own."""
        text = setup.render(setup.scaffold("org/decisions.md.tmpl"), {"deviations": setup.bullets(items)})
        with open(self.path(), "w", encoding="utf-8") as f:
            f.write(text.replace("（暂无）", self.MINE))
        return self.text()

    def section(self, text):
        start, end = setup.section_span(text, setup.PLUGIN_SECTION)
        return text[start:end]

    def init(self):
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = setup.actions_for(self.tmp, plan)
        _, out = self.run_quiet(setup.report, self.tmp, plan, acts, True)
        self.run_quiet(setup.apply_plan, self.tmp, plan, acts)
        return dict((rel, k) for k, rel, _ in acts)["org/decisions.md"], out

    def current_section(self):
        return self.section(setup.render(setup.scaffold("org/decisions.md.tmpl"), {"deviations": setup.deviations_text(self.spec)}))

    def test_every_plugin_section_is_known(self):
        old = setup.render(setup.scaffold("org/decisions.md.tmpl"), {"deviations": setup.bullets(self.OLD)})
        self.assertEqual(setup.PLUGIN_SECTIONS.get(setup.sha256(setup.squeeze(self.section(old)))), "1.0.0 至 1.0.2")
        digest = setup.sha256(setup.squeeze(self.current_section()))
        self.assertIn(digest, setup.PLUGIN_SECTIONS, "plugin_decisions 改了：把 %s 加进 setup.PLUGIN_SECTIONS，"
                      "下一版才认得出这一版写的那一节" % digest)

    def test_the_section_an_earlier_init_wrote_is_refreshed_in_place(self):
        before = self.write(self.OLD)
        kind, out = self.init()
        self.assertEqual(kind, "update")
        self.assertIn("[更新（上次生成后没人改过）] org/decisions.md", out)
        after = self.text()
        self.assertEqual(self.section(after), self.current_section())
        start, end = setup.section_span(before, setup.PLUGIN_SECTION)
        new_start, new_end = setup.section_span(after, setup.PLUGIN_SECTION)
        self.assertEqual((after[:new_start], after[new_end:]), (before[:start], before[end:]))   # the rest kept byte for byte
        self.assertIn("## 补充决定\n\n" + self.MINE, after)
        self.assertFalse(os.path.exists(self.path() + ".new"))
        self.assertEqual(self.init()[0], "same")

    def test_an_edited_section_goes_to_new(self):
        items = list(self.OLD)
        items[2] += "研究员 11-14 补：评论也只由书记员写。"
        before = self.write(items)
        kind, out = self.init()
        self.assertEqual(kind, "new")
        self.assertIn("[本地改过，新版本写 .new] org/decisions.md", out)
        self.assertEqual(self.text(), before)                                       # the file is as the researcher left it
        new = self.text(self.path() + ".new")
        self.assertEqual(self.section(new), self.current_section())
        self.assertIn("## 补充决定\n\n" + self.MINE, new)
        with open(os.path.join(self.tmp, ".claude", "solo-lab", "manifest.json"), encoding="utf-8") as f:
            self.assertIn("org/decisions.md", json.load(f)["pending_new"])
        _, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("待合并：org/decisions.md.new", out)
        self.assertEqual(self.init()[0], "pending")

    def test_a_current_section_is_left_alone(self):
        text = self.text().replace("（暂无）", self.MINE)
        start, end = setup.section_span(text, setup.PLUGIN_SECTION)
        text = text[:start] + self.section(text).replace("\n- ", "\n\n- ") + text[end:]     # spaced out by hand
        with open(self.path(), "w", encoding="utf-8") as f:
            f.write(text)
        self.assertEqual(self.init()[0], "same")
        self.assertEqual(self.text(), text)
        with open(self.path(), "w", encoding="utf-8") as f:
            f.write("# 研究员的补充决定\n\n" + self.MINE + "\n")                    # no plugin section at all
        self.assertEqual(self.init()[0], "kept")
        self.assertEqual(self.text(), "# 研究员的补充决定\n\n" + self.MINE + "\n")

    def test_deployed_scripts_find_their_files(self):
        """The copies under .claude/solo-lab run on their own: scripts, rules, terms and the contract."""
        for name in ("rules.json", "terms.zh.json", "contract.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, ".claude", "solo-lab", "orgspec", name)), name)
        env = dict(clean_env(), CLAUDE_PROJECT_DIR=self.tmp)
        out = {}
        for argv in (["ledger.py", "--offline", "mql", "tasks"], ["space.py", "check", "--target", self.tmp], ["orgspec.py", "check"]):
            p = subprocess.run([sys.executable, "-I", "-B", os.path.join(self.tmp, ".claude", "solo-lab", "bin", argv[0])] + argv[1:],
                               capture_output=True, text=True, env=env, cwd=self.tmp)
            self.assertEqual((p.returncode, p.stderr), (0, ""), argv)
            out[argv[0]] = p.stdout
        self.assertIn("约定核对（contract_version 1）：通过", out["space.py"])
        self.assertIn("SELECT `work_item_id`", out["ledger.py"])

    def test_dispatch_lists_and_entry(self):
        with open(os.path.join(self.hooks, "policy.json"), encoding="utf-8") as f:
            pol = json.load(f)
        self.assertEqual((pol["entry_role"], pol["scribe_role"]), ("pmo-p2", "pmo-scribe"))
        for key in ("mode", "main_agent"):
            self.assertNotIn(key, pol)
        agents = pol["agents"]
        self.assertIn("alg-p2", agents["pmo-p2"]["spawn"])
        self.assertNotIn("alg-p2", agents["pmo-router"]["spawn"])
        self.assertEqual(agents["alg-p2"]["spawn"], ["alg-p1"])
        self.assertEqual(agents["alg-p0"]["spawn"], [])
        self.assertTrue(agents["pmo-scribe"]["ledger_write"])
        self.assertFalse(agents["pmo-p2"]["ledger_write"])
        with open(os.path.join(self.tmp, ".claude", "settings.json"), encoding="utf-8") as f:
            self.assertNotIn("agent", json.load(f))                       # every role is a pool session

    def test_subset_of_sequences(self):
        tmp = tempfile.mkdtemp()
        try:
            plan = setup.build_plan(tmp, TASK, "product,alg,quality,pmo")
            agents = [p for p in plan.files if p.startswith(".claude/agents/")]
            self.assertEqual(len(agents), 12)
            with self.assertRaises(setup.SetupError):
                setup.build_plan(tmp, TASK, "product,alg")
        finally:
            shutil.rmtree(tmp)

    def test_ids_of_another_shape_start_empty(self):
        ids, notes = setup.normalise_ids({"meegle": {"project_key": "pk"}})
        self.assertEqual(ids, {"schema": "solo-lab/ids-v2", "lark": {}, "meegle": {}, "wiki": {}})
        self.assertTrue(notes)
        old, notes = setup.normalise_ids({"schema": "solo-lab/ids-v1", "meegle": {"project_key": "pk", "types": {"project": {}}}})
        self.assertEqual(old["meegle"], {})                           # a binding to a state-flow space is not carried over
        self.assertIn("节点流程", notes[0])
        same, notes = setup.normalise_ids({"schema": "solo-lab/ids-v2", "meegle": {"project_key": "pk"}})
        self.assertEqual((same["meegle"]["project_key"], notes), ("pk", []))


# ---------------------------------------------------------------- hooks
class HookTest(ProjectCase):

    def test_only_scribe_writes_ledger(self):
        cmd = {"command": "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"}
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.scribe("Bash", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("pmo-scribe", "Bash", cmd)                    # a subagent of that type holds no claim
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p2", "Bash", {"command": "python3 -I .claude/solo-lab/bin/ledger.py query open"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_tampered_script_not_trusted(self):
        cmd = {"command": "python3 -I .claude/solo-lab/bin/ledger.py query open"}
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        with open(os.path.join(self.tmp, ".claude", "solo-lab", "bin", "ledger.py"), "a", encoding="utf-8") as f:
            f.write("\n# changed\n")
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_meegle_write_only_scribe(self):
        cmd = {"command": "meegle workitem update --work-item-id 1 --project-key pk --fields '[]'"}
        _, out = self.guard("alg-p1", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.scribe("Bash", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Bash", {"command": "meegle comment add --work-item-id 1 --content hi"})
        self.assertEqual(out.get("permissionDecision"), "deny")           # comments come from claimed sessions, not subagents

    def test_drafts_only_own_sequence(self):
        own = os.path.join(self.tmp, "packages", "drafts", "alg", "a.md")
        other = os.path.join(self.tmp, "packages", "drafts", "rnd", "a.md")
        _, out = self.guard("alg-p1", "Write", {"file_path": own})
        self.assertEqual(out.get("permissionDecision"), "allow")
        _, out = self.guard("alg-p1", "Write", {"file_path": other})
        self.assertNotEqual(out.get("permissionDecision"), "deny")        # outside its range: reminded
        self.assertIn("写入范围", out.get("additionalContext", ""))
        _, out = self.guard("alg-p1", "Write", {"file_path": os.path.join(self.tmp, "repo", "eval", "x.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")           # the evaluation protocol stays hard


# --------------------------------------------------------------- ledger


# ---------------------------------------------------------------- route
class RouteTest(ProjectCase):
    def test_id_routes_to_r1(self):
        out = route.classify_text(self.spec, self.tmp, "#7151870875 的访谈记录补上了", offline=True)
        self.assertEqual(out["row"], "R1")

    def test_session_name_routes_to_r1(self):
        out = route.classify_text(self.spec, self.tmp, "Alg-p1-02 那边的消融结果怎样", offline=True)
        self.assertEqual(out["row"], "R1")
        self.assertEqual(out["role"], "alg-p1")

    def test_plain_text_needs_router(self):
        out = route.classify_text(self.spec, self.tmp, "导师说下周想看和人工基线的对比", offline=True)
        self.assertIsNone(out["row"])
        self.assertTrue(out["needs_router"])

    def test_drafts(self):
        cases = {2: ("R7", "alg-p2"), 4: ("R7", "quality-p2"), 7: ("R5", "pmo-scribe"), 8: ("R6", "pmo-scribe")}
        for block, (row, hop) in cases.items():
            out = route.classify_draft(self.spec, self.tmp, self.draft(block, "b%d.md" % block))
            self.assertEqual((out["row"], out.get("next_hop")), (row, hop), block)

    def test_in_sequence_package_not_routed(self):
        out = route.classify_draft(self.spec, self.tmp, self.draft(0))
        self.assertIsNone(out["row"])


# ------------------------------------------------- recorded Feishu shapes


class ListenTest(ProjectCase):
    def listen_with(self, events, open_id="ou_researcher"):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["lark"] = {"researcher_open_id": open_id} if open_id else {}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)
        started = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                started.append(cmd)
                self.stdout = io.StringIO("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))

            def wait(self):
                return 0

        real = route.subprocess.Popen
        route.subprocess.Popen = FakeProc
        try:
            code, _ = self.run_quiet(route.listen, self.tmp)
        finally:
            route.subprocess.Popen = real
        inbox = os.path.join(self.tmp, "inbox", "feishu.jsonl")
        lines = []
        if os.path.isfile(inbox):
            with open(inbox, encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]
        return code, lines, started

    def test_researcher_dm_is_taken_once(self):
        ev = fixture("event-message.json")
        code, lines, _ = self.listen_with([ev, ev])
        self.assertEqual(code, 0)
        self.assertEqual([m["text"] for m in lines], ["#7151870875 访谈记录补上了"])
        self.assertEqual(lines[0]["id"], "om_1")

    def test_others_are_ignored(self):
        ev = fixture("event-message.json")
        others = [dict(ev, sender_id="ou_other", message_id="om_2"), dict(ev, chat_type="group", message_id="om_3"),
                  dict(ev, sender_type="bot", message_id="om_4")]
        _, lines, _ = self.listen_with(others)
        self.assertEqual(lines, [])

    def test_refuses_without_open_id(self):
        code, lines, started = self.listen_with([fixture("event-message.json")], open_id=None)
        self.assertEqual(code, 2)
        self.assertEqual(started, [])


class SecondRoundTest(ProjectCase):
    """Fixes from the independent check of the first round."""

    def test_guard_sees_every_ledger_write(self):
        for cmd in ("python3 -I .claude/solo-lab/bin/ledger.py \\\n  confirm 1 执行中 --apply",
                    "python3 -I .claude/solo-lab/bin/ledger.py 'register' x.md",
                    "bash -c 'python3 .claude/solo-lab/bin/ledger.py update 1 --set a=b'"):
            _, out = self.guard("alg-p2", "Bash", {"command": cmd})
            self.assertEqual(out.get("permissionDecision"), "deny", cmd)
        _, out = self.guard("alg-p2", "Bash", {"command": "python3 -I .claude/solo-lab/bin/ledger.py mql queue"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")


class ThirdRoundTest(ProjectCase):
    """Fixes from the second independent check."""

    def test_guard_table(self):
        sys.path.insert(0, self.hooks)
        import importlib
        guard = importlib.import_module("guard")
        guard.fc.TRUSTED = guard.trusted_scripts(self.tmp)       # as evaluate() does
        reads = [
            "python3 -I .claude/solo-lab/bin/ledger.py query open",
            "python3 -I .claude/solo-lab/bin/ledger.py --offline get 1",
            "python3 -I .claude/solo-lab/bin/ledger.py confirm --help",
            "cat .claude/solo-lab/bin/ledger.py",
            'grep -rn "ledger.py:" org',
            "ls -la .claude/solo-lab/bin/ledger.py*",
            "diff .claude/solo-lab/bin/ledger.py .claude/solo-lab/bin/ledger.py.new",
            'git commit -m "ledger.py: fix confirm"',
            'echo "ledger.py register 由书记员跑"',
            "python3 -I -c \"import ast; ast.parse(open('.claude/solo-lab/bin/ledger.py').read())\"",
            "python3 -I .claude/solo-lab/bin/route.py classify --text 'ledger.py confirm 2030 要改'",
            "cat > packages/drafts/alg/x.md <<'EOF'\nrun python3 .claude/solo-lab/bin/ledger.py register x.md\nit's fine\nEOF",
        ]
        writes = [
            "python3 -I .claude/solo-lab/bin/ledger.py \\\n  confirm 7151870875 执行中 --apply",
            "python3 -X utf8 .claude/solo-lab/bin/ledger.py confirm 1 执行中 --apply",
            "python3 -I -W ignore .claude/solo-lab/bin/ledger.py update 1 --set a=b --apply",
            "python3 -m cProfile -o /dev/null .claude/solo-lab/bin/ledger.py confirm 1 x",
            "cd .claude/solo-lab/bin && python3 - confirm 1 x < ledger.py",
            "cat .claude/solo-lab/bin/ledger.py | python3 - confirm 1 x",
            "PYTHONPATH=.claude/solo-lab/bin python3 -m ledger confirm 1 x",
            'python3 -I .claude/solo-lab/bin/"ledger".py archive 1',
            "python3 $(echo .claude/solo-lab/bin/ledger.py) confirm 1 x",
            "sub=confirm; python3 .claude/solo-lab/bin/ledger.py $sub 1 x",
            "bash -c 'python3 .claude/solo-lab/bin/ledger.py update 1 --set a=b'",
            "timeout 60 python3 .claude/solo-lab/bin/ledger.py append 1 --file r.md",
            "python3 -I .claude/solo-lab/bin/ledger.py query open | head -5 && python3 .claude/solo-lab/bin/ledger.py archive 3",
        ]
        for cmd in reads:
            self.assertIsNone(guard.ledger_write(cmd, self.tmp), cmd)
        for cmd in writes:
            self.assertTrue(guard.ledger_write(cmd, self.tmp), cmd)


class FourthRoundTest(ProjectCase):
    """Fixes from the bug hunt and the completeness check."""

    def guard_module(self):
        sys.path.insert(0, self.hooks)
        import importlib
        guard = importlib.import_module("guard")
        guard.fc.TRUSTED = guard.trusted_scripts(self.tmp)       # as evaluate() does
        return guard

    def test_guard_shell_forms(self):
        guard = self.guard_module()
        L = ".claude/solo-lab/bin/ledger.py"
        reads = [f"python3 -I {L}", f"python3 -m py_compile {L}", f"python3 -m pyflakes {L}",
                 "python3 -I .claude/solo-lab/bin/route.py classify --text '问题出在 ledger.py'",
                 "python3 - <<'EOF'\nimport json\nprint(json.load(open('/tmp/ledger-open.json')))\nEOF",
                 f"for q in queue blocked; do python3 -I {L} query $q; done",
                 f"echo python3 {L} confirm 1", f"git log -- {L}"]
        writes = [f"python3 -I {L} query open\npython3 -I {L} confirm 1 执行中 --apply",
                  f"for id in 1 2; do python3 -I {L} confirm $id 归档 --apply; done",
                  f"while read id; do python3 {L} archive $id --apply; done < ids.txt",
                  f"if true; then python3 {L} update 1 --set a=b; fi", f"{{ python3 {L} comment 1 --text x; }}",
                  f"! python3 {L} register x.md", f"echo 1 | xargs -I{{}} python3 {L} confirm {{}} 执行中",
                  f"find . -name '*.md' -exec python3 {L} register {{}} \;",
                  f"bash <<'EOF'\npython3 {L} confirm 1 x --apply\nEOF",
                  f"python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['python3', '{L}', 'confirm', '1', 'x'])\nEOF",
                  f"sudo -u me python3 {L} archive 1", f"chrt -r 1 python3 {L} update 1"]
        for cmd in reads:
            self.assertIsNone(guard.ledger_write(cmd, self.tmp), cmd)
        for cmd in writes:
            self.assertTrue(guard.ledger_write(cmd, self.tmp), cmd)

    def test_append_from_a_file_redirect_is_explained(self):
        cmd = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --doc-format markdown --content - --as user < rec.md"
        _, out = self.scribe("Bash", {"command": cmd})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        self.assertIn("看不到要追加的正文", out.get("additionalContext", ""))
        with open(os.path.join(self.tmp, ".claude", "logs", "guard.jsonl"), encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertTrue(any("看不到要追加的正文" in h for h in last["soft_hits"]))


class FifthRoundTest(ProjectCase):
    """The fourth adversarial check: comments, runners, script files, option clusters, find, eval."""

    L = ".claude/solo-lab/bin/ledger.py"

    def setUp(self):
        super().setUp()
        sys.path.insert(0, self.hooks)
        import importlib
        self.g = importlib.import_module("guard")
        self.g.fc.TRUSTED = self.g.trusted_scripts(self.tmp)       # as evaluate() does
        runs = os.path.join(self.tmp, "runs", "alg")
        os.makedirs(runs, exist_ok=True)
        files = {"r.sh": "python3 -I %s register packages/drafts/alg/x.md --apply\n" % self.L,
                 "r.py": "import subprocess\nsubprocess.run(['python3', '-I', '%s', 'register', 'x.md', '--apply'])\n" % self.L,
                 "stats.py": "import json\nprint(len(json.load(open('runs/alg/meegle.json'))))\n",
                 "q.sh": "# list what is open\npython3 -I %s query open\n" % self.L}
        for name, text in files.items():
            with open(os.path.join(runs, name), "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        sys.path.remove(self.hooks)
        sys.modules.pop("guard", None)
        sys.modules.pop("feishu_cmd", None)
        sys.modules.pop("instances", None)
        super().tearDown()

    def check(self, reads, writes):
        for cmd in reads:
            self.assertIsNone(self.g.ledger_write(cmd, self.tmp), cmd)
        for cmd in writes:
            self.assertTrue(self.g.ledger_write(cmd, self.tmp), cmd)

    def test_comments_do_not_hide_the_next_line(self):
        L = self.L
        self.check([f"python3 -I {L} get 2106   # current state", "echo see issue#12", "bash runs/alg/q.sh",
                    f"cat > packages/drafts/alg/x.md <<'END-DRAFT'\nWe don't run ledger.py here\nEND-DRAFT"],
                   [f"python3 -I {L} get 2106   # current state\npython3 -I {L} confirm 2106 执行中 --apply",
                    f"ls packages/drafts/alg  # check\npython3 -I {L} register packages/drafts/alg/x.md --apply",
                    f"echo see issue#12; python3 -I {L} archive 2106 --apply"])

    def test_runners_scripts_and_option_clusters(self):
        L = self.L
        self.check(["python3 runs/alg/stats.py", f"python3 -m py_compile {L}",
                    "python3 -c \"import json; rows=[]; [rows.append(r['id']) for r in json.load(open('runs/alg/x.json'))]\""],
                   [f"uv run python {L} register x.md --apply", f"uv run {L} update 2106 --set a=b --apply",
                    f"uv run --no-project python3 -I {L} archive 2106 --apply", f"poetry run python {L} archive 1",
                    f"conda run -n base python {L} archive 1",
                    "bash runs/alg/r.sh", "sh runs/alg/r.sh", "source runs/alg/r.sh", ". runs/alg/r.sh",
                    "bash < runs/alg/r.sh", "cat runs/alg/r.sh | sh", "python3 -I runs/alg/r.py",
                    f"cat > runs/alg/r2.sh <<'EOF'\npython3 -I {L} register x.md --apply\nEOF\nbash runs/alg/r2.sh",
                    f"python3 -BW ignore {L} register x.md --apply",
                    "cd .claude/solo-lab/bin && python3 -Bm ledger archive 2106 --apply",
                    f"python3 -m cProfile --sort cumtime {L} archive 2106 --apply",
                    f"python3 -m trace --count -C runs/alg {L} archive 1",
                    "cd .claude/solo-lab/bin && python3 -m runpy ledger archive 2106 --apply",
                    "cd .claude/solo-lab/bin && python3 -m cProfile -m ledger archive 1",
                    "python3 -I -c \"import runpy; runpy.run_path('%s', run_name='__main__')\" register x.md --apply" % L,
                    "python3 -c \"import sys; sys.path.insert(0, '.claude/solo-lab/bin'); import ledger as m; m.archive(m.Ledger('.', 'apply'), '1')\"",
                    f"python3 -I {L} register --apply -- -h"])

    def test_find_eval_shells_and_other_interpreters(self):
        L = self.L
        self.check([f"find .claude -name ledger.py -exec cat {{}} \;",
                    f"find .claude/solo-lab/bin -name ledger.py -execdir python3 -I {{}} query open \;",
                    f"ls {L}(.)", "reg() { ls; }; reg", f"grep -n update $(ls {L})"],
                   [f"find .claude/solo-lab/bin -name ledger.py -execdir python3 -I {{}} archive 2106 --apply \;",
                    f"find . -name '*.md' -exec echo {{}} \; -exec python3 {L} register {{}} \;",
                    f"eval eval eval eval eval python3 -I {L} archive 2106 --apply",
                    f"echo archive 2106 --apply | xargs python3 -I {L}",
                    f"function reg {{ python3 -I {L} register x.md --apply; }}; reg",
                    f"env -S 'python3 -I' {L} archive 2106 --apply",
                    f"CMD='python3 -I {L} archive 2106 --apply'; eval \"$CMD\"",
                    f"CMD='python3 -I {L} archive 2106 --apply'; bash -c \"$CMD\"",
                    f"csh -c 'python3 {L} archive 1'", f"bash -c -- 'python3 {L} archive 1'",
                    "perl -e 'system(\"python3\", \"%s\", \"archive\", \"2106\")'" % L,
                    "node -e \"require('child_process').execFileSync('python3', ['%s', 'archive', '2106'])\"" % L,
                    f"cat > runs/alg/out.txt <<EOF\n$(python3 -I {L} archive 2106 --apply)\nEOF",
                    "python3 -I .claude/solo-lab/bin/ledger.p? archive 2106 --apply",
                    "python3 -I .claude/solo-lab/bin/ledger.[p]y archive 1", f"python3 -I {L}(.) archive 1"])

    def test_words_in_code_and_text_are_not_commands(self):
        L = self.L
        self.check([f"python3 - <<'PY'\nimport subprocess\nout=subprocess.run(['python3','-I','{L}','query','open'],capture_output=True)\nids=[1]\nids.append(2)\nPY",
                    f"cat > packages/drafts/alg/x.md <<'EOF'\n- 请书记员 ledger.py register 后 ledger.py update 2106\nEOF\npython3 - <<'PY'\nprint(1)\nPY",
                    'git commit -m "ledger.py: fix confirm"', f"python3 -I {L} query open 2>/dev/null || echo 'ledger.py: 查询失败'",
                    "python3 -I .claude/solo-lab/bin/route.py classify --text 'ledger.py confirm 2030 要改'"], [])
        cmd = "python3 -I .claude/solo-lab/bin/route.py classify --text 'ledger.py confirm 2030 要改'"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_feishu_commands_after_a_comment_or_in_find(self):
        cmd = "ls  # 先看看\nlark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --content 'no tag' --as user"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        cmd = "# 改字段\nmeegle workitem update --work-item-id 1 --project-key pk --fields '[]'"
        _, out = self.guard("alg-p1", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        cmd = "find . -name '*.md' -exec lark-cli docs +update --doc {} --command overwrite --content x --as user \;"
        _, out = self.guard("alg-p1", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Bash", {"command": "grep -rn meegle org/"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_odd_input_never_fails_open(self):
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        cmd = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --content @pipe --as user"
        _, out = self.scribe("Bash", {"command": cmd})       # must not hang on the FIFO
        self.assertNotEqual(out.get("permissionDecision"), "deny")        # the tag check is a reminder now
        self.assertIn("会话标签", out.get("additionalContext", ""))
        code, out = self.guard("alg-p2", "Bash", {"command": "python3 -I %s register \ud800 --apply" % self.L})
        self.assertIn(code, (0, 2))
        if code == 0:
            self.assertEqual(out.get("permissionDecision"), "deny")


class FifthRoundLedgerTest(ProjectCase):
    """Ledger, setup and parser fixes from the fourth check."""

    def test_check_regenerates_and_compares(self):
        code, out = self.run_quiet(setup.check, self.tmp)       # setUp wrote ids.json after init
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md", out)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("本地配置：就绪", out)                      # the other layers wait for the pool and the first task

    def test_python_clusters_and_heredocs_hiding_feishu_calls(self):
        with open(os.path.join(self.tmp, "w.py"), "w", encoding="utf-8") as f:
            f.write("import subprocess\nsubprocess.run(['lark-cli', 'docs', '+update'])\n")
        for cmd in ("python3 -IW ignore w.py",
                    "python3 -IBc \"import subprocess; subprocess.run(['lark-cli','docs','+update'])\"",
                    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['lark-cli', 'docs', '+update'])\nEOF"):
            _, out = self.guard("alg-p2", "Bash", {"command": cmd})
            self.assertEqual(out.get("permissionDecision"), "deny", cmd)
        cmd = "cat > packages/drafts/alg/n.md <<'EOF'\n用 lark-cli docs +fetch 读\nEOF\npython3 - <<'PY'\nprint(1)\nPY"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertNotEqual(out.get("permissionDecision"), "deny")


class SixthRoundHookTest(ProjectCase):
    """The fifth adversarial check of the hooks, replayed through the real guard.py process."""

    L = ".claude/solo-lab/bin/ledger.py"
    LARK_W = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command overwrite --content x --as user"

    def setUp(self):
        super().setUp()
        L, W = self.L, self.LARK_W
        files = {
            "runs/alg/r.py": "import subprocess\nsubprocess.run(['python3', '-I', '%s', 'register', 'x.md', '--apply'])\n" % L,
            "runs/alg/r.sh": "python3 -I %s register packages/drafts/alg/x.md --apply\n" % L,
            "runs/alg/r2.py": "import subprocess\nsubprocess.run(['python3', '-I', '../../%s', 'archive', '2106', '--apply'])\n" % L,
            "runs/alg/push.py": "import subprocess\nsubprocess.run(%r.split())\n" % W,
            "runs/alg/push.sh": W + "\n",
            "runs/alg/rx.sh": "#!/bin/sh\npython3 -I %s archive 2106 --apply\n" % L,
            "runs/alg/rx.py": "#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(['python3', '%s', 'archive', '2106', '--apply'])\n" % L,
            "runs/alg/pushx.sh": "#!/bin/sh\n" + W + "\n",
            "runs/alg/batch.py": "import glob, subprocess\nLEDGER = '%s'\nfor path in sorted(glob.glob('packages/drafts/alg/*.md')):\n"
                                 "    subprocess.run(f'python3 -I {LEDGER} register {path} --apply', shell=True, check=True)\n" % L,
            "runs/alg/batch2.py": "import shlex, subprocess\nL = '%s'\nsubprocess.run(shlex.split(f'python3 -I {L} confirm 2106 执行中 --apply'))\n" % L,
            "runs/alg/batch3.py": "import subprocess, sys\nsubprocess.run([sys.executable, '-I', '%s'] + 'archive 2106 --apply'.split())\n" % L,
            "runs/alg/report.py": "import json, subprocess\nout = subprocess.run(['python3', '-I', '%s', 'query', 'open', '--json'], capture_output=True, text=True).stdout\n"
                                  "rows = json.loads(out or '[]')\nnames = []\nfor r in rows: print(r['id']); names.append(r['name'])\n"
                                  "n = len(rows)\nprint(f'{n} items need an update')\n" % L,
            "runs/alg/pk.py": "import json\nids = json.load(open('feishu/ids.json'))\nprint(ids['meegle']['project_key'])\n",
            "runs/alg/summarize_ledger.py": "import json, sys\nprint(len(json.load(open(sys.argv[-1]))))\n",
            "runs/alg/stats.py": "import json, sys\nprint(len(json.load(open(sys.argv[1]))))\n",
            "runs/alg/readdoc.py": "import json\n# doc.json was saved earlier by lark-cli docs +fetch\nprint(json.load(open('runs/alg/doc.json'))['title'])\n",
            "runs/alg/analyze_x.py": "print(1)\n",
        }
        for rel, text in files.items():
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            if os.path.basename(rel) in ("rx.sh", "rx.py", "pushx.sh"):
                os.chmod(path, 0o755)

    def check(self, cases):
        for role, cmd, deny in cases:
            call = {"command": cmd}
            code, out = self.scribe("Bash", call) if role == "pmo-scribe" else self.guard(role, "Bash", call)
            denied = out.get("permissionDecision") == "deny" or code == 2
            self.assertEqual(denied, deny, "%s: %s → %s" % (role, cmd, out.get("permissionDecisionReason")))

    def test_interpreters_by_path_cd_and_direct_scripts(self):
        self.check([("alg-p2", c, True) for c in (
            ".venv/bin/python runs/alg/r.py", "/usr/bin/python3 runs/alg/r.py", "/bin/bash runs/alg/r.sh",
            "\"python3\" runs/alg/r.py", "ipython runs/alg/r.py", "cd runs/alg && python3 r.py", "cd runs/alg && bash r.sh",
            "cd runs/alg && python3 r2.py", "./runs/alg/rx.sh", "./runs/alg/rx.py", "uv run runs/alg/r.py",
            "nohup ./runs/alg/rx.sh", "bash -lc ./runs/alg/rx.sh", "cd runs/alg && python3 push.py",
            "uv run runs/alg/push.py", "./runs/alg/pushx.sh")])

    def test_heredocs_inside_substitutions_and_comments(self):
        L, W = self.L, self.LARK_W
        g = "git commit -q -m \"$(cat <<'EOF'\nAdd eval script\nEOF\n)\" && "
        pyl = "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['python3', '-I', '%s', 'archive', '2106', '--apply'])\nPY" % L
        pyf = "python3 - <<'PY'\nimport subprocess\nsubprocess.run(%r.split())\nPY" % W
        self.check([
            ("alg-p2", g + pyl, True), ("alg-p2", g + pyf, True),
            ("alg-p2", "TITLE=\"$(cat <<'EOF'\n周报\nEOF\n)\"\ncat > packages/drafts/alg/n.md <<'EOF'\n用 lark-cli docs +fetch 读原文\nEOF\n"
                       "python3 - <<'PY'\nprint(1)\nPY", False),
            ("alg-p2", "TITLE=\"$(cat <<'EOF'\n周报\nEOF\n)\"\ncat > packages/drafts/alg/n.md <<'EOF'\npython3 -I %s register n.md --apply\nEOF\n"
                       "python3 - <<'PY'\nprint(1)\nPY" % L, False),
            ("alg-p2", "X=\"$(python3 - <<'PY'\nimport subprocess\nsubprocess.run(['python3', '%s', 'archive', '2106', '--apply'])\nPY\n)\"" % L, True),
            ("alg-p2", "X=\"$(python3 - <<'PY'\nimport subprocess\nsubprocess.run(%r.split())\nPY\n)\"" % W, True),
            ("alg-p2", "MSG=$(cat <<'EOF'\nfix\nEOF\n) && bash <<'SH'\npython3 -I %s archive 2106 --apply\nSH" % L, True),
            ("alg-p2", "# drafts are written with cat <<EOF, not here\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p1", "ls runs/alg  # then cat <<'EOF' > notes\nmeegle workitem update --work-item-id 1 --project-key pk --fields '[]'", True),
            ("alg-p2", "grep -rn \"<<EOF\" runs/alg || true\n" + W, True),
            ("alg-p2", "echo 'use <<EOF for drafts'\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "git commit -q -m \"$(cat <<'EOF'\nSay \"use lark-cli docs +fetch first\nEOF\n)\"", False),
            ("alg-p2", "ls runs/alg # list \\\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "python3 - <<'PY'\nimport json\n# doc.json was saved earlier by lark-cli docs +fetch\n"
                       "print(json.load(open('runs/alg/doc.json'))['title'])\nPY", False),
            ("alg-p2", "python3 -m json.tool <<'EOF'\n{\"c\": \"lark-cli docs +update\"}\nEOF", False),
        ])

    def test_code_that_builds_the_command(self):
        L = self.L
        self.check([
            ("alg-p2", "python3 runs/alg/batch.py", True), ("alg-p2", "python3 runs/alg/batch2.py", True),
            ("alg-p2", "python3 runs/alg/batch3.py", True),
            ("alg-p2", "python3 - <<'PY'\nimport subprocess\nL = '%s'\nfor i in (2106, 2107):\n"
                       "    subprocess.run(f'python3 -I {L} archive {i} --apply', shell=True)\nPY" % L, True),
            ("alg-p2", "python3 runs/alg/report.py", False),
        ])

    def test_cli_names_used_as_data(self):
        self.check([
            ("alg-p2", "python3 -c \"import json; print(json.load(open('feishu/ids.json'))['meegle']['project_key'])\"", False),
            ("alg-p2", "python3 - <<'PY'\nimport json\nids = json.load(open('feishu/ids.json'))\nprint(ids['meegle']['project_key'])\nPY", False),
            ("alg-p2", "python3 runs/alg/pk.py", False),
            ("alg-p2", "meegle workitem get --work-item-id 2106 --project-key \"$(python3 runs/alg/pk.py)\"", False),
            ("alg-p2", "python3 -c \"import json; print(json.load(open('feishu/ids.json')).get('meegle', {}))\"", False),
            ("alg-p2", "python3 -c \"import subprocess,sys; subprocess.run(['meegle'] + sys.argv[1:])\" workitem update --work-item-id 1", True),
            ("alg-p2", "python3 -c \"import json; print(len(json.load(open('runs/alg/meegle.json'))))\"", False),
            ("alg-p2", "node -e \"const d=require('./runs/alg/meegle.json'); console.log(d.length)\"", False),
            ("alg-p2", "python3 runs/alg/readdoc.py", False),
        ])

    def test_arguments_are_not_subcommands(self):
        L = self.L
        self.check([
            ("alg-p2", "python3 -I %s query open --json > /tmp/open.json && python3 - /tmp/open.json <<'PY'\n"
                       "import json, sys\nprint(len(json.load(open(sys.argv[1]))))\nPY" % L, False),
            ("alg-p2", "python3 -I %s query open --json > runs/alg/open.json && python3 \"$CLAUDE_PROJECT_DIR/runs/alg/stats.py\" runs/alg/open.json" % L, False),
            ("alg-p2", "python3 \"$(git rev-parse --show-toplevel)/runs/alg/stats.py\" runs/alg/ledger-open.json", False),
            ("alg-p2", "for f in %s; do python3 $f archive 2106 --apply; done" % L, True),
            ("alg-p2", "python3 runs/alg/summarize_ledger.py runs/alg/open.json", False),
            ("alg-p2", "python3 runs/alg/summarize_ledger.py --top 5 runs/alg/open.json", False),
            ("alg-p2", "cp %s /tmp/my_ledger.py && python3 /tmp/my_ledger.py archive 2106 --apply" % L, True),
        ])

    def test_shell_options_pipes_and_here_strings(self):
        L, W = self.L, self.LARK_W
        self.check([
            ("alg-p2", "bash runs/alg/r.sh -c configs/a.yaml", True), ("alg-p2", "bash -eo pipefail runs/alg/r.sh", True),
            ("alg-p2", "bash runs/alg/push.sh -c configs/a.yaml", True), ("alg-p2", "bash -euo pipefail runs/alg/push.sh", True),
            ("alg-p2", "bash -o pipefail -c 'python3 %s archive 1'" % L, True),
            ("alg-p2", "cat <<'EOF' | python3 -\nimport subprocess\nsubprocess.run(['python3', '-I', '%s', 'archive', '2106', '--apply'])\nEOF" % L, True),
            ("alg-p2", "cat <<'EOF' | python3 -\nimport subprocess\nsubprocess.run(%r.split())\nEOF" % W, True),
            ("alg-p2", "cat <<'EOF' | bash\npython3 -I %s archive 2106 --apply\nEOF" % L, True),
            ("alg-p2", "bash <<< \"python3 -I %s archive 2106 --apply\"" % L, True),
            ("alg-p2", "python3 - <<< \"import subprocess; subprocess.run(['python3', '%s', 'archive', '2106'])\"" % L, True),
            ("alg-p2", "python3 - <<< \"import subprocess; subprocess.run(%r.split())\"" % W, True),
            ("alg-p2", "grep ';' runs/alg/r.sh", False),
            ("alg-p2", "source .venv/bin/activate && python3 -I %s query open" % L, False),
        ])

    def test_find_runners_and_inline_flags(self):
        L, W = self.L, self.LARK_W
        self.check([
            ("alg-p2", "find runs/alg -name 'analyze_*.py' -exec python3 {} runs/alg/ledger-open.json \;", False),
            ("alg-p2", "find .claude/solo-lab/bin \\( -name ledger.py \\) -execdir python3 -I {} archive 2106 --apply \;", True),
            ("alg-p2", "find runs/alg -name '*.py' -exec python3 {} runs/alg/data.json \;", False),
            ("alg-p2", "find runs/alg -name '*.py' -exec python3 {} --limit 5 \;", False),
            ("alg-p2", "find .claude -name 'ledger*' -exec python3 {} archive 1 \;", True),
            ("alg-p2", "conda run -n base --cwd . python %s archive 2106 --apply" % L, True),
            ("alg-p2", "uv --directory . run python %s archive 2106 --apply" % L, True),
            ("alg-p2", "poetry -C . run python %s archive 2106 --apply" % L, True),
            ("alg-p2", "uv --directory . run " + W, True), ("alg-p2", "conda run -n base --cwd . " + W, True),
            ("alg-p2", "perl -we 'system(\"python3\", \"%s\", \"archive\", \"2106\", \"--apply\")'" % L, True),
            ("alg-p2", "node -pe \"require('child_process').execFileSync('python3', ['%s', 'archive', '2106', '--apply']).toString()\"" % L, True),
            ("alg-p2", "node --eval=\"require('child_process').execFileSync('python3', ['%s', 'archive', '2106'])\"" % L, True),
            ("alg-p2", "node -r dotenv/config -e \"require('child_process').execFileSync('lark-cli', ['docs', '+update', '--doc', 'X', "
                       "'--command', 'overwrite', '--content', 'x', '--as', 'user'])\"", True),
            ("alg-p2", "ipython -c \"import subprocess; subprocess.run(['lark-cli', 'docs', '+update', '--doc', 'X', '--command', "
                       "'overwrite', '--content', 'x', '--as', 'user'])\"", True),
            ("alg-p2", "ruby -rtime -e \"system('lark-cli docs +update --doc X --command overwrite --content x --as user')\"", True),
        ])

    def test_scribe_tag_check_reads_the_heredoc_fed_to_lark(self):
        append = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --doc-format markdown --content - --as user"
        _, out = self.scribe("Bash", {"command": "cat > /tmp/prev.md <<'EOF'\n[PMO+0] 上一条记录\nEOF\n%s <<'EOF'\n没有会话标签的记录\nEOF" % append})
        self.assertNotEqual(out.get("permissionDecision"), "deny")        # the tag is a norm: reminded
        self.assertIn("第一行要带会话标签", out.get("additionalContext", ""))
        self.check([
            ("pmo-scribe", "cat > /tmp/prev.md <<'EOF'\n没有标签\nEOF\n%s <<'EOF'\n[PMO+0] 记录\nEOF" % append, False),
            ("pmo-scribe", "cat <<'EOF' | %s\n[PMO+0] 记录\nEOF" % append, False),
            ("pmo-scribe", "python3 -I %s register packages/drafts/alg/x.md --apply" % self.L, False),
        ])


class SixthRoundLedgerTest(ProjectCase):
    """Ledger and setup fixes from the fifth adversarial check."""

    def test_check_separates_task_edits_and_lark_versions(self):
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        risk = os.path.join(self.tmp, ".claude", "hooks", "lark-risk.json")
        with open(risk, encoding="utf-8") as f:
            table = json.load(f)
        table["lark_cli_version"] = "1.0.96"
        with open(risk, "w", encoding="utf-8") as f:
            f.write(setup.dump_json(table))
        mpath = os.path.join(self.tmp, ".claude", "solo-lab", "manifest.json")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        m["files"][".claude/hooks/lark-risk.json"] = setup.sha256(setup.read_text(risk))
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f)
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("lark-cli 风险表", out)
        self.assertNotIn("现在的插件", out)
        task = os.path.join(self.tmp, "spec", "task.json")
        with open(task, encoding="utf-8") as f:
            t = json.load(f)
        t["feishu"]["meegle_space"] = "ar1"
        with open(task, "w", encoding="utf-8") as f:
            json.dump(t, f)
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("改过：spec/task.json", out)
        self.assertNotIn("现在的插件", out)

    def test_malformed_task_and_ids_are_explained(self):
        with open(TASK, encoding="utf-8") as f:
            base = json.load(f)
        for bad in (None, 5, dict(base, name=2026), dict(base, models="opus"), dict(base, editable_paths="repo")):
            path = os.path.join(self.tmp, "bad-task.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            with self.assertRaises(setup.SetupError, msg=repr(bad)[:60]):
                setup.load_task(path)
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        with self.assertRaises(setup.SetupError):
            setup.load_ids(self.tmp)

class SeventhRoundTest(ProjectCase):
    """Cases from the sixth check, which stopped half way: its repro scripts were replayed by hand."""

    L = ".claude/solo-lab/bin/ledger.py"

    def check(self, cases):
        for role, cmd, deny in cases:
            call = {"command": cmd}
            code, out = self.scribe("Bash", call) if role == "pmo-scribe" else self.guard(role, "Bash", call)
            denied = out.get("permissionDecision") == "deny" or code == 2
            self.assertEqual(denied, deny, "%s: %r → %s %s" % (role, cmd, code, out.get("permissionDecisionReason")))

    def test_hook_reads_ordinary_commands(self):
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        with open(os.path.join(tools, "hello.js"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env node\n/*\n * Copyright (c) 2026\n * Redistributions of source code must retain this notice.\n */\nconsole.log('hi')\n")
        os.chmod(os.path.join(tools, "hello.js"), 0o755)
        draft_writer = ("python3 - <<'PY'\nfrom pathlib import Path\nPath('packages/drafts/alg/req.md').write_text("
                        "'# 执行请求\\n请书记员运行 ledger.py register packages/drafts/alg/req.md\\n')\nPY")
        self.check([("alg-p2", c, False) for c in (
            'cat "runs/alg/notes [2024-11-01].md"', 'tail -n 100 "runs/alg/train [lr=3e-4].log"',
            '"$PY" -m pytest "tests/test_math.py::test_sub[5-3-2]"', "./tools/hello.js world",
            "python3 - <<'PY'\nrows = [1,2]\nprint(f\"{len(rows)} update(s) pending in ledger\")\nPY",
            "python3 - <<'PY'\nimport json\nd = json.load(open('/tmp/ledger-open.json'))\nstates = {'register': 0, 'update': 0}\nPY",
            "python3 -c \"import json; d=json.load(open('/tmp/ledger-open.json')); print([r for r in d if r['type']=='reply'])\"",
            "python3 - <<'PY'\n# summarise; the scribe runs ledger.py update afterwards\nimport json\nprint(1)\nPY",
            draft_writer,
        )])

    def test_hook_still_sees_writes_after_arithmetic_and_crlf(self):
        L = self.L
        self.check([
            ("alg-p2", "x=1; (( y = x << n ))\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "cat > /tmp/a.md <<'EOF'\r\nhello\r\nEOF\r\npython3 -I %s archive 2106 --apply\r\n" % L, True),
            ("alg-p2", "x=1; (( y = x << n ))\nlark-cli docs +update --doc https://x.feishu.cn/wiki/A --command overwrite --content x --as user", True),
        ])

    def test_scribe_multi_line_lark_command(self):
        cmd = ("lark-cli docs +update \\\n  --doc https://x.feishu.cn/wiki/A \\\n  --command append --doc-format markdown \\\n"
               "  --content - --as user <<'EOF'\n[PMO+0] 记录\nEOF")
        self.check([("pmo-scribe", cmd, False)])


class SpecAndRoutingTest(ProjectCase):
    """Task file with named fields, two spec documents written by interview, routing by 状态包."""

    LEGACY = {"name": "AR", "description": "旧版任务", "x": {"definition": "只能改模型结构。", "paths": ["repo/**"]},
              "f": {"definition": "留出集 AUC，3 个种子取平均。"}, "g": {"definition": "每个快照 2 卡 1 小时。", "budget_gpu_hours": 300},
              "h": {"definition": "评测脚本和测试集只读。", "paths": ["repo/eval/**"]}, "search": "MCGS 按平均分扩展。",
              "feishu": {"meegle_space": "AR", "wiki_space": "AR"}}

    def write_task(self, obj, name="task.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def plan_for(self, task_path):
        target = tempfile.mkdtemp(prefix="solo-lab-spec-")
        self.addCleanup(shutil.rmtree, target, True)
        with contextlib.redirect_stdout(io.StringIO()):
            return target, setup.build_plan(target, task_path, None)

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def test_task_file_holds_only_what_scripts_use(self):
        task = setup.load_task(TASK)
        self.assertEqual(tuple(task), tuple(k for k in setup.TASK_KEYS if k != "mode"))   # mode is read, not kept
        self.assertEqual(task["pool"], setup.POOL_DEFAULTS)
        self.assertEqual(json.loads(self.read("spec/task.json")), task)
        claude = self.read("CLAUDE.md")
        for part in ("`spec/spec.md`", "`spec/eval-protocol.md`", "可写路径：`repo/**`", "只读路径：`repo/eval/**`、`data/eval/**`"):
            self.assertIn(part, claude)
        for letter in ("- x：", "- f：", "- g：", "- h：", "h 的路径"):
            self.assertNotIn(letter, claude)
        for name in os.listdir(os.path.join(self.tmp, ".claude", "agents")):
            self.assertNotIn("h 的路径", self.read(os.path.join(".claude", "agents", name)))
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        self.assertEqual((policy["x_paths"], policy["h_paths"]), (["repo/**"], ["repo/eval/**", "data/eval/**"]))

    def test_spec_documents_have_the_named_sections(self):
        spec_md, protocol = self.read("spec/spec.md"), self.read("spec/eval-protocol.md")
        for heading in ("## 可改范围", "## 目标指标", "## 假设和阈值", "## 实验资源", "## 搜索方法", "## 签字"):
            self.assertIn(heading, spec_md)
        for heading in ("## 存放和版本", "## 划分和可见性", "## 接入方式", "## 打分", "## 证据标准", "## 签字"):
            self.assertIn(heading, protocol)
        self.assertIn("〔推测，待确认〕", spec_md)
        self.assertIn("solo-lab:spec", protocol)
        self.assertIn("约束优化问题", spec_md)
        self.assertNotIn("x、f、g、h", spec_md + protocol)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "skills", "spec", "references", "questions.md")))

    def test_old_task_file_still_works_and_seeds_the_documents(self):
        target, plan = self.plan_for(self.write_task(self.LEGACY))
        spec_md = plan.files["spec/spec.md"][0]
        self.assertIn("只能改模型结构。（取自旧版任务文件）〔推测，待确认〕", spec_md)
        self.assertIn("MCGS 按平均分扩展。（取自旧版任务文件）〔推测，待确认〕", spec_md)
        self.assertIn("评测脚本和测试集只读。（取自旧版任务文件）〔推测，待确认〕", plan.files["spec/eval-protocol.md"][0])
        task = json.loads(plan.files["spec/task.json"][0])
        self.assertEqual((task["editable_paths"], task["protected_paths"]), (["repo/**"], ["repo/eval/**"]))
        self.assertFalse({"x", "f", "g", "h", "search"} & set(task))
        self.assertTrue(any("旧写法（x、f、g、h、search）" in n and "先把 x.paths、h.paths 改写成" in n for n in plan.notes))
        both = dict(self.LEGACY, editable_paths=["src/**"])
        with self.assertRaises(setup.SetupError):
            setup.load_task(self.write_task(both, "both.json"))

    def test_task_file_names_the_space(self):
        base = setup.load_task(TASK)
        target, plan = self.plan_for(self.write_task(dict(base, _comment="ok")))
        self.assertNotIn("_comment", plan.files["spec/task.json"][0])
        self.assertTrue(any("还没绑定飞书空间" in n for n in plan.notes))
        for bad in ({"eval_resources": []}, {"feishu": {"wiki_space": "w"}}, {"feishu": {"meegle_space": " "}},
                    {"protected_paths": ["/abs/eval"]}, {"editable_paths": "repo"}, {"metric": "auc"}):
            with self.assertRaises(setup.SetupError, msg=repr(bad)):
                setup.load_task(self.write_task(dict(base, **bad), "bad.json"))

    def test_ledger_waits_for_the_binding(self):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"].pop("bound_at")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f)
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            code, out = self.run_quiet(ledger.main, ["update", "2003", "--set", "运行 ID=r-1", "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("还没绑定飞书空间", out)
            code, _ = self.run_quiet(ledger.main, ["--offline", "update", "2003", "--set", "运行 ID=r-1", "--type", "same_seq"])
            self.assertEqual(code, 0)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_ledger_stops_when_the_task_names_another_space(self):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"]["space_name"] = "旧空间"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f)
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            code, out = self.run_quiet(ledger.main, ["update", "2003", "--set", "run_ids=r-1", "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("绑定的是“旧空间”", out)
            with self.assertRaises(setup.SetupError):          # the local dictionary belongs to the old space
                self.run_quiet(setup.feishu, self.tmp, [], False, False)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_old_spec_documents_are_kept_and_flagged(self):
        old = "# AR 规格（草稿）\n\n## x：可以改的代码\n\n只能改模型结构。\n"
        with open(os.path.join(self.tmp, "spec", "spec.md"), "w", encoding="utf-8") as f:
            f.write(old)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
        self.assertTrue(any("spec/spec.md 还是改名前的旧模板" in n for n in plan.notes))
        acts = {rel: act for act, rel, _ in setup.actions_for(self.tmp, plan)}
        self.assertEqual(acts["spec/spec.md"], "kept")

    def test_hook_messages_use_the_names(self):
        _, out = self.guard("alg-p0", "Write", {"file_path": "repo/eval/score.py", "content": "x"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("评测协议", out.get("permissionDecisionReason", ""))
        _, out = self.guard("alg-p0", "Write", {"file_path": "repo/model.py", "content": "x"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        self.assertIn("可改范围", out.get("additionalContext", ""))
        _, out = self.guard("rnd-p0", "Write", {"file_path": "repo/model.py", "content": "x"}, agent_id="a2")
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_router_reads_the_status_block(self):
        _, out = self.guard("pmo-router", "Bash", {"command": "python3 -I .claude/solo-lab/bin/ledger.py status 2101"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("pmo-router", "Bash", {"command": "python3 -I .claude/solo-lab/bin/ledger.py next 2101"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        fake = fake_space.FakeSpace()
        lg = fake_space.SimLedger(self.tmp, "apply", fake)
        item = str(fake.create("decision", [{"field_key": "name", "field_value": "x"}, ledger.flow_entry(lg, "decision"),
                                            lg.entry("decision", "sequence", "算法"), lg.entry("decision", "sender", "Alg-p2-01"),
                                            lg.entry("decision", "urgency", "今天"),
                                            lg.entry("decision", "main_doc", "https://example.feishu.cn/wiki/wikMain")])["work_item_id"])
        section = {"data": {"document": {"content": "<h2>状态包</h2><h3>简介</h3><p>访谈提纲</p>"}}}
        lg = fake_space.SimLedger(self.tmp, "preview", fake, [(("--scope", "outline"), fixture("docs-fetch-outline.json")),
                                                              (("--scope", "section"), section)])
        _, printed = self.run_quiet(ledger.status, lg, item)
        self.assertIn("访谈提纲", printed)
        argv = next(c[0] for c in lg.calls if "section" in c[0])
        self.assertEqual(argv[argv.index("--start-block-id") + 1], "doxcnnyQfTdXSmSuMyN3ajvOmgh")
        self.assertIn("status", ledger.READ_COMMANDS)
        self.assertIn("next", ledger.READ_COMMANDS)
        self.assertNotIn("index", ledger.READ_COMMANDS)

    def test_new_status_block_starts_with_the_cognitive_layer(self):
        body = "## 认知层\n\n这个阶段整理访谈，涉及 Product-p1-01 和需求文档 v1。\n\n## 目标\n\n写提纲。\n"
        out = ledger.with_status_sections(body)
        self.assertIn("### 简介\n\n这个阶段整理访谈，涉及 Product-p1-01 和需求文档 v1。\n\n### 当前状态", out)
        self.assertLess(out.index("## 状态包"), out.index("## 交接记录"))
        self.assertIn("### 简介\n\n### 当前状态", ledger.with_status_sections("## 方法\n\n写提纲。\n"))

    def test_plugin_cites_nothing_outside_itself(self):
        """Nothing in the repository, docs included, points to material outside the plugin."""
        forbidden = re.compile("|".join([
            "/Users/", "llm-?[Ww]iki", "WikiWorkspace", "ResearchWorkspace", "协作" + "记录", "协作" + "请求",
            "讨论" + "记录", "scratch" + "pad", "《", "文" + "章", "local_[0-9a-f]{8}", "Anthrop" + "ic", "Open" + "AI",
            "ME" + "TR ", "Cogni" + "tion", "Chro" + "ma ", "SWE-" + "bench", "Simple" + "QA", "Hua" + "ng 等人",
            "τ-" + "bench", "评审" + "会话"]))
        work_item = re.compile("#?715[0-9]{7}")
        hits = []
        for base, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                path = os.path.join(base, name)
                if os.path.samefile(path, __file__) or not name.endswith((".md", ".json", ".tmpl", ".py", ".txt")):
                    continue
                docs = name.endswith((".md", ".tmpl")) and os.sep + "tests" + os.sep not in path
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for no, line in enumerate(f, 1):
                        if forbidden.search(line) or docs and work_item.search(line):
                            hits.append("%s:%d" % (os.path.relpath(path, REPO), no))
        self.assertEqual(hits, [])

    def test_plugin_names_no_platform_or_test_space(self):
        """The plugin body names no platform outside its dependencies and no key or name of the space
        it was tested on; tests/ keeps the recorded outputs."""
        with open(os.path.join(ROOT, "tests", "fixtures", "feishu", "space-keys.json"), encoding="utf-8") as f:
            space = json.load(f)
        keys = [space["project_key"], space["simple_name"]] + [t["type_key"] for t in space["types"].values()]
        keys += [x["field_key"] for fields in space["fields"].values() for x in fields.values()]
        with open(os.path.join(ROOT, "tests", "fixtures", "feishu", "v2", "space.json"), encoding="utf-8") as f:
            v2 = json.load(f)                            # the reference template space, read by space.py
        keys += [v2["project_key"], v2["simple_name"]] + list(v2["types"])
        for t in v2["types"].values():
            keys += [x["field_key"] for x in t["fields"] if x["field_key"].startswith("field_")] + list(t["flows"])
            keys += [s["id"] for s in t["states"] if s["id"] not in ("started", "closed")]
        words = ["Coze" + "Loop", "coze" + "loop", "eval_" + "resources", "Auto" + "Research", "MC" + "GS", "贝索" + "斯",
                 "Claude in " + "Chrome", "mcp__" + "ccd", "ccd_" + "session"]
        earlier = "(?<!当)" + "前" + "文"         # a pointer to earlier text; “当前文件” is not one
        pattern = re.compile("|".join([earlier] + [re.escape(w) for w in words + sorted(set(keys))]))
        hits = []
        for base, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "tests")]
            for name in files:
                if not name.endswith((".md", ".json", ".tmpl", ".py", ".txt")):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for no, line in enumerate(f, 1):
                        m = pattern.search(line)
                        if m:
                            hits.append("%s:%d %s" % (os.path.relpath(path, REPO), no, m.group(0)))
        self.assertEqual(hits, [])

    def test_packages_no_longer_carry_the_index(self):
        skills = os.path.join(ROOT, "skills")
        for rel in ("packages/assets/request.md", "packages/assets/delivery.md", "packages/assets/decision.md", "packages/references/examples.md",
                    "packages/references/principles.md", "packages/SKILL.md", "entry/SKILL.md",
                    "entry/references/routing.md", "ledger/SKILL.md"):
            with open(os.path.join(skills, rel), encoding="utf-8") as f:
                self.assertNotIn("人、事、物索引", f.read(), rel)
        self.assertIn("状态包", orgspec.OrgSpec().routing_table())
        for rel in ("scaffold/agents/router.md.tmpl", "skills/entry/references/routing.md", "skills/packages/assets/request.md",
                    "skills/packages/assets/delivery.md", "skills/packages/assets/decision.md",
                    "skills/packages/references/principles.md", "skills/packages/references/examples.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertNotIn("索引", f.read(), rel)

    def test_settings_allow_the_status_read(self):
        allow = json.loads(self.read(".claude/settings.json"))["permissions"]["allow"]
        self.assertIn("Bash(python3 -I .claude/solo-lab/bin/ledger.py status *)", allow)
        self.assertFalse([r for r in allow if "ledger.py index" in r])

    def test_drafters_see_their_documents(self):
        alg, quality, rnd = (self.read(".claude/agents/%s.md" % n) for n in ("alg-p2", "quality-p2", "rnd-p0"))
        self.assertIn("以及你起草的 `spec/spec.md`", alg)
        self.assertIn("你负责起草实验规格 `spec/spec.md`：用技能 `solo-lab:spec`", alg)
        self.assertIn("你负责起草评测协议 `spec/eval-protocol.md`", quality)
        self.assertIn("只读路径（`repo/eval/**`、`data/eval/**`）改动要研究员确认", quality)
        self.assertIn("只读路径（`repo/eval/**`、`data/eval/**`）不能改", rnd)
        self.assertNotIn("spec/spec.md", rnd)

    def test_spec_without_the_algorithm_sequence(self):
        target = tempfile.mkdtemp(prefix="solo-lab-spec-")
        self.addCleanup(shutil.rmtree, target, True)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(target, TASK, "pmo,quality,rnd,product")
        spec_md = plan.files["spec/spec.md"][0]
        self.assertIn("没有启用算法序列，由研究员本人写", spec_md)
        self.assertNotIn("研究员和研究员", spec_md)

    def test_paths_are_normalised_and_gaps_reported(self):
        base = setup.load_task(TASK)
        task = setup.load_task(self.write_task(dict(base, editable_paths=["./repo/"], protected_paths=[]), "paths.json"))
        self.assertEqual((task["editable_paths"], task["protected_paths"]), (["repo/**"], []))
        target, plan = self.plan_for(self.write_task(dict(base, protected_paths=[]), "noprot.json"))
        self.assertTrue(any("没有写 protected_paths" in n for n in plan.notes))
        os.makedirs(os.path.join(target, "data", "eval"))
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(target, self.write_task(dict(base, protected_paths=["data/eval"]), "dir.json"), None)
        self.assertTrue(any("写成 data/eval/** 才能管到" in n for n in plan.notes))
        for bad in (["./"], ["./../x"]):
            with self.assertRaises(setup.SetupError, msg=repr(bad)):
                setup.load_task(self.write_task(dict(base, protected_paths=bad), "bad.json"))

    def test_spec_document_that_is_not_utf8(self):
        with open(os.path.join(self.tmp, "spec", "spec.md"), "wb") as f:
            f.write("# AR 实验规格\n\n## x：可以改的代码\n".encode("gbk"))
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
        self.assertTrue(any("UTF-8" in n for n in plan.notes))
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertNotIn("Traceback", out)

    def test_path_errors_quote_the_entry_as_written(self):
        base = setup.load_task(TASK)
        for written in ("./", ".//repo", "/"):
            with self.assertRaises(setup.SetupError) as e:
                setup.load_task(self.write_task(dict(base, editable_paths=[written]), "bad.json"))
            self.assertTrue(str(e.exception).endswith("：" + written), str(e.exception))


@unittest.skipIf(REPO == ROOT, "装好的插件目录里没有仓库主页")
class ReadmeTest(unittest.TestCase):
    """The two READMEs on the repository home page stay in step with each other and with the scripts."""

    def read(self, name):
        with open(os.path.join(REPO, name), encoding="utf-8") as f:
            return f.read()

    def test_both_languages_have_the_same_sections_and_commands(self):
        zh, en = self.read("README.md"), self.read("README.en.md")
        self.assertIn("[English](README.en.md)", zh)
        self.assertIn("[简体中文](README.md)", en)
        sections = lambda text: [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(len(sections(zh)), len(sections(en)))
        typed = lambda text: re.findall(r"```(?:bash|text)\n(.*?)\n```", text, re.S)   # what the reader types or says
        self.assertTrue(typed(zh))
        self.assertEqual(typed(zh), typed(en))

    def test_both_state_the_plugin_version(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            version = json.load(f)["version"]
        for name in ("README.md", "README.en.md"):
            self.assertIn("v" + version, self.read(name), "%s 里没有 v%s" % (name, version))


class FeishuDocsTest(ProjectCase):
    """The Feishu Docs side of setup: the knowledge space and the node skeleton."""

    def edit_json(self, rel, fn):
        path = os.path.join(self.tmp, rel)
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        fn(obj)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_check_reports_what_the_feishu_side_lacks(self):
        self.edit_json("feishu/ids.json", lambda d: d["wiki"]["nodes"].pop("archive/alg"))
        _, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("知识库节点还缺 1 个（archive/alg）", out)
        self.assertIn("ensure-node all", out)
        self.edit_json("feishu/ids.json", lambda d: d["wiki"]["nodes"].pop("archive/infra"))
        self.edit_json(".claude/solo-lab/manifest.json",
                       lambda m: m.update(sequences=[s for s, _, _ in self.spec.sequences() if s != "infra"]))
        _, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("知识库节点还缺 1 个（archive/alg）", out)       # infra is not enabled: its node is not expected
        self.edit_json("feishu/ids.json", lambda d: d["wiki"].pop("space_id"))
        _, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("还没有知识空间", out)
        self.assertNotIn("知识库节点还缺", out)
        self.edit_json("feishu/ids.json", lambda d: d["meegle"].pop("bound_at"))
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("飞书项目空间还没绑定", out)
        self.assertNotIn("还没有知识空间", out)


# ---------------------------------------------------------- pool mode
def assign_targets(seq, rank, seqs):
    """Who a role hands work to, written out by hand to check the assign edges in rules.json."""
    if (seq, rank) == ("pmo", 2):
        return ["%s-p2" % s for s in seqs if s != "pmo"] + ["pmo-router"]
    if (seq, rank) == ("pmo", 1):
        return ["pmo-scribe"]
    if rank == 0:
        return []
    return ["%s-p%d" % (seq, rank - 1)]


class PoolProjectCase(unittest.TestCase):
    """A fresh project with its pool settings written out, and fake Feishu IDs."""

    POOL = {"p1": 1, "p0": 1, "later": [], "max_sessions": 30}
    L = ".claude/solo-lab/bin/ledger.py"
    R = ".claude/solo-lab/bin/route.py"
    PREFIX = "solo-lab-pool-"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix=self.PREFIX)
        self.spec = orgspec.OrgSpec()
        with open(TASK, encoding="utf-8") as f:
            task = json.load(f)
        task.update(pool=dict(self.POOL))
        self.task_path = os.path.join(self.tmp, "solo-lab.task.json")
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, self.task_path, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        fake_space.install(self.tmp, spec=self.spec)
        self.hooks = os.path.join(self.tmp, ".claude", "hooks")
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def env(self, sid=None, desk=None):
        env = dict(clean_env(), CLAUDE_PROJECT_DIR=self.tmp)
        if sid:
            env["CLAUDE_CODE_SESSION_ID"] = sid
        if desk:
            env["CLAUDE_CODE_HOST_SESSION_ID"] = desk
        return env

    def hook(self, script, data, desk=None):
        data = dict({"cwd": self.tmp}, **data)
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(self.hooks, script)], input=json.dumps(data),
                           capture_output=True, text=True, env=self.env(desk=desk))
        out = json.loads(p.stdout) if p.stdout.strip() else {}
        return p.returncode, out.get("hookSpecificOutput", {})

    def route(self, *argv, sid=None, desk=None):
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(self.tmp, self.R)] + list(argv), capture_output=True,
                           text=True, env=self.env(sid, desk), cwd=self.tmp)
        return p.returncode, p.stdout + p.stderr

    def guard(self, sid, tool, tool_input, desk=None, **extra):
        data = dict({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "session_id": sid}, **extra)
        return self.hook("guard.py", data, desk=desk)

    def bash(self, sid, command, desk=None, **extra):
        return self.guard(sid, "Bash", {"command": command}, desk=desk, **extra)

    def prompt(self, sid, text, desk=None, **extra):
        return self.hook("inbox.py", dict({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}, **extra),
                         desk=desk)

    def opening(self, role, name, sid, desk=None):
        return self.prompt(sid, "认领 %s（会话名 %s），然后照认领输出开出本序列的 +1 和 +0。" % (role, name), desk=desk)

    def claim(self, role, name, sid, desk=None, hook_desk=None):
        """A child session's claim: guard, route.py claim (its Bash sees the desktop ID), then audit."""
        cmd = "python3 -I %s claim %s --as %s" % (self.R, role, name)
        self.bash(sid, cmd, desk=hook_desk)
        code, out = self.route("claim", role, "--as", name, "--wait", "0", sid=sid, desk=desk)
        self.hook("audit.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": sid,
                               "tool_input": {"command": cmd}, "tool_response": {"stdout": out, "stderr": ""}}, desk=hook_desk)
        return code, out

    def pool(self):
        return json.loads(self.read(".claude/state/pool.json"))

    def session(self, sid):
        path = os.path.join(self.tmp, ".claude", "state", "sessions", sid + ".json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def write_pool(self, data):
        path = os.path.join(self.tmp, ".claude", "state", "pool.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def started_pool(self):
        """Entry, scribe and the algorithm sequence claimed; sessions named after their roles."""
        self.opening("pmo-p2", "PMO-p2-01", "s-entry")
        self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d-scribe", sid="s-entry")
        self.claim("pmo-scribe", "PMO-p0-01", "s-scribe", desk="d-scribe")
        self.opening("alg-p2", "Alg-p2-01", "s-alg2")
        for name, role, sid in (("Alg-p1-01", "alg-p1", "s-alg1"), ("Alg-p0-01", "alg-p0", "s-alg0")):
            self.route("expect", name, "--role", role, "--desk", "d-" + sid, sid="s-alg2")
            self.claim(role, name, sid, desk="d-" + sid)
        self.opening("quality-p2", "Quality-p2-01", "s-q2")
        self.route("expect", "Quality-p1-01", "--role", "quality-p1", "--desk", "d-q1", sid="s-q2")
        self.claim("quality-p1", "Quality-p1-01", "s-q1", desk="d-q1")
        return self.pool()


class PoolSetupTest(PoolProjectCase):
    def test_settings_and_policy(self):
        settings = json.loads(self.read(".claude/settings.json"))
        self.assertNotIn("agent", settings)
        self.assertIs(settings["autoMemoryEnabled"], False)
        self.assertNotIn("autoCompactWindow", settings)    # the window is the account's and the model's own
        for event in ("Stop", "StopFailure", "Notification", "PermissionRequest", "PermissionDenied", "PostCompact"):
            self.assertIn(event, settings["hooks"])
        self.assertIn("AskUserQuestion", settings["hooks"]["PreToolUse"][0]["matcher"])
        self.assertEqual(settings["hooks"]["PostToolUse"][0]["matcher"], "Bash|Monitor|Agent|Task")
        self.assertIn("Monitor", settings["hooks"]["PreToolUse"][0]["matcher"])
        allow = settings["permissions"]["allow"]
        self.assertIn("Bash(python3 -I .claude/solo-lab/bin/route.py claim *)", allow)
        self.assertIn("Bash(python3 -I .claude/solo-lab/bin/ledger.py register *)", allow)
        self.assertNotIn("Bash(python3 -I .claude/solo-lab/bin/route.py release *)", allow)
        self.assertIn("Edit(/.claude/state/**)", settings["permissions"]["ask"])
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        self.assertNotIn("mode", policy)
        self.assertEqual(policy["version"], setup.POLICY_VERSION)
        self.assertEqual(policy["pool"], self.POOL)
        for key in ("lark_ask", "meegle_ask", "docs_create_parents"):
            self.assertNotIn(key, policy["agents"]["alg-p1"])
        self.assertEqual(policy["owned_paths"][".claude/state/**"], [])
        self.assertIn("spec/spec.md", policy["soft_owned"])
        for rel in (".claude/hooks/pool.py", ".claude/solo-lab/bin/pool.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, rel)))

    def test_texts_are_written_for_the_pool(self):
        claude = self.read("CLAUDE.md")
        for part in ("按会话池运行", "route.py claim", "## 本目录怎样落实全局约定", "## Compact Instructions", "不回 ACK"):
            self.assertIn(part, claude)
        self.assertNotIn("主会话以 `pmo-p2`", claude)
        entry = self.read(".claude/agents/pmo-p2.md")
        self.assertIn("在会话池里是研究员平时对话的会话", entry)
        self.assertNotIn("替换了 Claude Code 的默认系统提示", entry)
        self.assertNotIn("{{", entry)
        for name in os.listdir(os.path.join(self.tmp, ".claude", "agents")):
            text = self.read(os.path.join(".claude", "agents", name))
            self.assertIn(setup.POOL_DESC, text.split("\n", 3)[2], name)
            self.assertNotIn("用 Agent 工具派 `", text, name)
        self.assertIn("【登记请求】", self.read(".claude/agents/alg-p1.md"))
        self.assertIn("【登记回执", self.read(".claude/agents/pmo-scribe.md"))
        # every step of a request has someone who asks the scribe for it
        for name in ("alg-p0", "alg-p1"):
            self.assertIn("动作“上传答复”，再写一行“确认节点：#<需求 ID>，由 <你的会话名> 提出”", self.read(".claude/agents/%s.md" % name))
        for name in ("alg-p1", "alg-p2"):
            self.assertIn("- 验收：", self.read(".claude/agents/%s.md" % name))
        self.assertNotIn("- 验收：", self.read(".claude/agents/alg-p0.md"))
        lead = self.read(".claude/agents/alg-p2.md")
        self.assertIn("先接单：把需求复述、承接范围、预计交付时间", lead)
        self.assertIn("交付包发给入口 `PMO-p2-01`", lead)
        self.assertIn("登记请求里把字段写成 `<字段>=@<本地文件>`，书记员上传到研究任务单下面并填上链接", lead)   # what the +2 writes itself
        self.assertIn("验收通过以后用承接方那份交付包文档的链接", lead)       # what it hands down
        self.assertIn("链接字段给的是 `@文件` 时不读内容", self.read(".claude/agents/pmo-scribe.md"))
        scribe = self.read(".claude/agents/pmo-scribe.md")
        self.assertIn("你也接任何会话的登记请求", scribe)
        self.assertIn("【通知 #<任务 ID>】PMO-p0-01 → <那个 +2 的会话名>：研究任务走到“<节点>”", scribe)
        self.assertIn("接着请它归档这条研究任务", self.read(".claude/agents/pmo-p2.md"))
        self.assertIn("### 派发图", self.read("org/org.md"))
        self.assertIn("初始池", self.read("org/grants.md"))

    def test_pool_plan(self):
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        entries = list(setup.plan_entries(plan))
        names = [e["name"] for e in entries]
        self.assertEqual((plan["version"], plan["total"], len(plan["top"]), len(set(names))), (5, 21, 7, 21))
        # the +2s are opened in batches: what a batch opens below itself stays within what the app runs at once
        self.assertEqual(plan["batches"], [["PMO-p2-01", "Product-p2-01"], ["Alg-p2-01", "RnD-p2-01"],
                                           ["Infra-p2-01", "Quality-p2-01"], ["Liaison-p2-01"]])
        # ...and the batches in rounds: after one message of the researcher the app lets sessions open ten sessions
        self.assertEqual(plan["rounds"], [[1, 2], [3, 4]])
        self.assertEqual(plan["order"], [t["name"] for t in plan["top"]])
        self.assertEqual(plan["order"][0], "PMO-p2-01")                     # the scribe is two levels below the entry
        for t in plan["top"]:                                                # a +2 opens its +1, a +1 its +0
            self.assertTrue(t["opening"].startswith("认领 %s（会话名 %s）" % (t["role"], t["name"])))
            self.assertEqual([c["name"].split("-")[1] for c in t["children"]], ["p1"])
            one = t["children"][0]
            self.assertEqual([c["name"].split("-")[1] for c in one["children"]], ["p0"])
            self.assertEqual(one["children"][0]["children"], [])
            self.assertIn("开出 %s，" % one["name"], t["brief"])
            self.assertIn("开出 %s，" % one["children"][0]["name"], one["brief"])
            self.assertIn("就结束本轮，等委托消息", one["children"][0]["brief"])
        pmo = plan["top"][0]
        self.assertEqual((pmo["children"][0]["name"], pmo["children"][0]["children"][0]["name"]), ("PMO-p1-01", "PMO-p0-01"))
        for e in entries:
            self.assertTrue(re.fullmatch(r"[a-z0-9-]{1,24}", e["short"]))
            self.assertIn("route.py claim %s --as %s" % (e["role"], e["name"]), e["brief"])
            self.assertGreater(len(e["background"]), 80)                    # start_session refuses a shorter one
            self.assertEqual(e["title"], "[%s] %s" % (plan["label"], e["name"]))
            self.assertLess(len(e["title"]), 60)
            self.assertIn("route.py expect %s --role %s" % (e["name"], e["role"]), e["expect"])
        self.assertIn("ensure-node all --apply", plan["scribe_first"])
        self.assertIn("register %s --apply" % setup.FIRST_TASK, plan["scribe_first"])

    def test_the_label_names_the_titles_and_the_section(self):
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        self.assertEqual(json.loads(self.read(".claude/solo-lab/pool-plan.json"))["label"], task["feishu"]["meegle_space"])
        task["pool"]["label"] = "CodeOpt"
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        doc = json.loads(setup.build_plan(self.tmp, self.task_path, None).files[".claude/solo-lab/pool-plan.json"][0])
        self.assertEqual((doc["label"], doc["top"][0]["title"]), ("CodeOpt", "[CodeOpt] PMO-p2-01"))
        for bad in ("[x]", "长" * 41, 3):
            task["pool"]["label"] = bad
            with open(self.task_path, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False)
            with self.assertRaises(setup.SetupError):
                setup.build_plan(self.tmp, self.task_path, None)

    def test_liaison_opens_with_its_p2_unless_the_task_says_otherwise(self):
        self.assertEqual(setup.load_pool_settings(None)["later"], ["liaison"])
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        del task["pool"]["later"]
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        doc = json.loads(setup.build_plan(self.tmp, self.task_path, None).files[".claude/solo-lab/pool-plan.json"][0])
        self.assertEqual(doc["total"], 19)
        self.assertEqual(next(t for t in doc["top"] if t["role"] == "liaison-p2")["children"], [])
        few = setup.build_plan(self.tmp, self.task_path, "pmo,quality,alg")   # the default says nothing of a sequence that is off
        self.assertEqual(json.loads(few.files[".claude/solo-lab/pool-plan.json"][0])["total"], 9)

    def test_a_space_name_that_cannot_head_a_title_needs_a_label(self):
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        task["feishu"]["meegle_space"] = "一个名字特别长的飞书项目空间" * 4
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        os.remove(os.path.join(self.tmp, "feishu", "ids.json"))
        with self.assertRaises(setup.SetupError) as e:
            setup.build_plan(self.tmp, self.task_path, None)
        self.assertIn("pool.label", str(e.exception))

    def test_init_refuses_the_home_directory(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(["init", "--target", os.path.expanduser("~"), "--task", self.task_path])
        self.assertEqual(code, 2)
        self.assertIn("不在家目录或根目录里初始化", out.getvalue())

    def test_the_first_task_sheet_is_written_once(self):
        path = os.path.join(self.tmp, setup.FIRST_TASK)
        self.assertEqual(ledger.sheet_kind(path), "research_task")
        with open(path, "a", encoding="utf-8") as f:
            f.write("研究员补了一句。\n")
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, self.task_path, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        self.assertIn("研究员补了一句。", self.read(setup.FIRST_TASK))                    # a draft is the researcher's
        self.assertFalse(os.path.exists(path + ".new"))

    def test_later_sequences_start_with_their_p2(self):
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        task["pool"].update(later=["liaison"], p0=2)
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        plan = setup.build_plan(self.tmp, self.task_path, None)
        doc = json.loads(plan.files[".claude/solo-lab/pool-plan.json"][0])
        liaison = next(t for t in doc["top"] if t["role"] == "liaison-p2")
        self.assertEqual(liaison["children"], [])
        alg = next(t for t in doc["top"] if t["role"] == "alg-p2")
        self.assertEqual([c["name"] for c in alg["children"]], ["Alg-p1-01"])
        self.assertEqual([c["name"] for c in alg["children"][0]["children"]], ["Alg-p0-01", "Alg-p0-02"])
        # three sessions below a +2 leave no room for a second sequence; one that opens nothing joins the last batch
        self.assertEqual(doc["batches"], [["PMO-p2-01"], ["Product-p2-01"], ["Alg-p2-01"], ["RnD-p2-01"], ["Infra-p2-01"],
                                          ["Quality-p2-01", "Liaison-p2-01"]])
        self.assertEqual(doc["rounds"], [[1, 2, 3], [4, 5], [6]])            # 2 + 3 + 3 sessions below, then 3 + 3, then 3
        task["pool"].update(p1=0)                                           # no +1: the +0s hang under the +2
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        doc = json.loads(setup.build_plan(self.tmp, self.task_path, None).files[".claude/solo-lab/pool-plan.json"][0])
        alg = next(t for t in doc["top"] if t["role"] == "alg-p2")
        self.assertEqual([c["name"] for c in alg["children"]], ["Alg-p0-01", "Alg-p0-02"])

    def test_injection_fits_every_role(self):
        sys.path.insert(0, self.hooks)
        import pool
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        for role, pol in policy["agents"].items():
            claim = {"name": pol.get("fixed_session") or "%s-p%d-01" % (pol["abbrev"], pol["rank"]), "role": role}
            text = pool.injection(self.tmp, policy, claim, "规则已更新。")
            self.assertLessEqual(len(text), 9500, role)
            self.assertIn("未了事项", text)

    def test_init_twice_and_check(self):
        for _ in range(2):                                 # the first run takes in the fake IDs written in setUp
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                plan = setup.build_plan(self.tmp, self.task_path, None)
                setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        self.assertIn("已写入 0 处", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertNotIn("现在的插件", out.getvalue())
        self.assertIn("会话池还没开齐", out.getvalue())

    def test_pool_settings_are_checked(self):
        with open(self.task_path, encoding="utf-8") as f:
            base = json.load(f)
        for bad in ({"p1": 4}, {"p0": -1}, {"later": "liaison"}, {"later": ["pmo"]}, {"max_sessions": 6},
                    {"max_sessions": 58}, {"size": 3}):
            task = dict(base, pool=dict(self.POOL, **bad))
            path = os.path.join(self.tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False)
            with self.assertRaises(setup.SetupError, msg=str(bad)):
                setup.load_task(path)
        for mode in ("cli", "desktop"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dict(base, mode=mode), f, ensure_ascii=False)
            with self.assertRaises(setup.SetupError) as refused:
                setup.load_task(path)
            self.assertIn("CLI 模式已取消", str(refused.exception))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(base, mode="pool"), f, ensure_ascii=False)
        self.assertNotIn("mode", setup.load_task(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(base, pool=dict(self.POOL, later=["infra"])), f, ensure_ascii=False)
        with self.assertRaises(setup.SetupError):                     # a sequence that is not enabled
            setup.build_plan(self.tmp, path, "pmo,quality,alg")

    def test_dispatch_targets_are_the_assign_edges(self):
        seqs = [s for s, _, _ in self.spec.sequences()]
        for seq in seqs:
            for rank in (2, 1, 0):
                role = setup.agent_name(seq, rank)
                self.assertEqual(self.spec.dispatch_targets(role, seq, rank, seqs), assign_targets(seq, rank, seqs), role)
        some = ["pmo", "quality", "alg"]
        self.assertEqual(self.spec.dispatch_targets("pmo-p2", "pmo", 2, some), assign_targets("pmo", 2, some))

    def test_pool_checklist(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp]), 0)
        text = out.getvalue()
        self.assertIn("1. PMO-p2-01（未认领）", text)
        self.assertIn("只开下面这 7 个 +2，分 4 批、2 轮开", text)
        self.assertIn("\n第 1 轮（研究员说完初始化那句话以后）\n第 1 批\n1. PMO-p2-01", text)
        self.assertIn("\n第 2 轮（研究员回一句“继续开池”以后）\n第 3 批\n5. Infra-p2-01", text)
        self.assertIn("结束本轮，请研究员回一句“继续开池”", text)
        self.assertIn("\n第 4 批\n7. Liaison-p2-01", text)
        self.assertIn("--wait 180 --batch", text)
        self.assertLess(text.index("各调一次 detach_session"), text.index("逐个运行它们的 expect"))   # top level first, then the permit
        self.assertNotIn("注意：", text)
        self.assertIn("   └ PMO-p1-01（", text)                                    # the levels below, opened by the level above
        self.assertIn("     └ PMO-p0-01（", text)
        self.assertNotIn("PMO-p1-01（未认领）\n   name：", text)
        self.assertIn("再照输出里的表开出 PMO-p1-01，", text)
        self.assertIn("] Initialization”", text)
        self.assertIn("当前：0/21 条认领", text)
        self.assertNotIn("--watch", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp, "--manual"]), 0)
        self.assertIn("认领 pmo-p2（会话名 PMO-p2-01），然后照认领输出开出 PMO-p1-01。本会话由研究员手工开出。", out.getvalue())
        self.assertIn("书记员由路由开出", out.getvalue())
        self.assertIn("--wait 240", text)                                         # how the initialising session waits for the levels below
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        self.assertNotIn(setup.BY_HAND, "".join(e["brief"] for e in setup.plan_entries(plan)))   # only a +2 opened by hand files itself


class AnnounceTest(PoolProjectCase):
    """setup.py announce: what an initialisation is about to create, said before anything is made."""

    def announce(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(["announce"] + list(args))
        return code, out.getvalue()

    def test_a_fresh_directory_needs_a_name_and_gets_the_defaults(self):
        fresh = os.path.join(self.tmp, "Thesis-Phase1")
        os.mkdir(fresh)
        code, text = self.announce("--target", fresh)                    # the directory's name is not taken for the project's
        self.assertEqual(code, 2)
        self.assertIn("还没有项目名：先问研究员这个项目叫什么", text)
        self.assertNotIn("Thesis-Phase1", text)
        self.assertEqual(self.announce("--target", fresh, "--name", " ")[0], 2)
        code, text = self.announce("--target", fresh, "--name", "新项目")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("把下面的清单原样发给研究员，一个字不改；发出去以后再运行下一条命令。\n\n将要新建（"), text[:80])
        self.assertIn("- 飞书项目空间 1 个：新项目，用模板创建\n- 飞书知识空间 1 个：新项目\n", text)
        self.assertIn("- 知识库节点 %d 个\n" % len(self.spec.node_keys()), text)
        self.assertIn("- 桌面会话 19 个（外联只开 +2），侧栏分区 1 个：新项目\n", text)
        self.assertIn("会话分 2 轮开，第 1 轮开完回一句“%s”" % setup.RESUME_WORDS, text)
        self.assertIn("新建以外的动作", text)
        self.assertIn("权限模式：本会话用 auto 模式时，开池不用你点。用 bypass 模式时，应用每开一个会话都要你点一次确认，这个池共 19 次。", text)
        self.assertEqual(os.listdir(fresh), [])                                    # it only says, it writes nothing
        code, text = self.announce("--target", fresh, "--name", "另一个名字")
        self.assertIn("- 飞书项目空间 1 个：另一个名字，", text)

    def test_the_counts_follow_the_task_file_and_the_enabled_sequences(self):
        fresh = os.path.join(self.tmp, "demo")
        os.mkdir(fresh)
        code, text = self.announce("--target", fresh, "--name", "demo", "--sequences", "pmo,quality,alg")
        self.assertEqual(code, 0, text)
        self.assertIn("- 知识库节点 %d 个\n" % len(self.spec.node_keys(["pmo", "quality", "alg"])), text)
        self.assertIn("- 桌面会话 9 个，侧栏分区 1 个：demo\n", text)
        self.assertNotIn("轮开", text)                                              # one round: nothing to answer in between
        code, text = self.announce("--target", fresh, "--task", self.task_path)   # every sequence with its +1 and +0
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        self.assertIn("- 桌面会话 %d 个，侧栏分区 1 个：%s\n" % (plan["total"], plan["label"]), text)
        self.assertEqual(plan["total"], 21)

    def test_an_initialised_directory_is_read_from_its_own_task_file(self):
        code, text = self.announce("--target", self.tmp, "--name", "别的名字")
        self.assertEqual(code, 0, text)
        self.assertIn("- 桌面会话 21 个，侧栏分区", text)
        self.assertNotIn("别的名字", text)
        self.assertIn("这个目录已经初始化过", text)
        manifest = json.loads(self.read(".claude/solo-lab/manifest.json"))
        tops = {p.split("/")[0] + ("/" if "/" in p else "") for p in manifest["files"]}
        self.assertEqual(tops, set(setup.LOCAL_TOPS))                              # the directories it names are the ones init writes

    def test_it_refuses_what_init_refuses(self):
        code, text = self.announce("--target", os.path.expanduser("~"))
        self.assertEqual(code, 2)
        self.assertIn("不在家目录或根目录里初始化", text)
        other = os.path.join(self.tmp, "codex")
        os.makedirs(os.path.join(other, ".agent-org"))
        with open(os.path.join(other, ".agent-org", "manifest.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        code, text = self.announce("--target", other)
        self.assertEqual(code, 2)
        self.assertIn("另一运行平台", text)

    def test_the_skill_runs_it_instead_of_writing_the_list_itself(self):
        with open(os.path.join(ROOT, "skills", "setup", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertIn('setup.py" announce --target <项目目录>', skill)
        self.assertLess(skill.index('setup.py" announce --target'), skill.index('onboarding.py" begin --target'))   # the first command of all
        self.assertNotIn("将要新建（", skill)                                       # the list is the script's, not the skill's
        for old in ("没说用目录名", "脚本用目录名"):                                # no name: it asks
            self.assertNotIn(old, skill)
        self.assertIn("还没有项目名", skill)


class SpaceNameTest(unittest.TestCase):
    """CLAUDE.md and feishu/README.md name the spaces as Feishu does once they are bound."""

    def test_the_spaces_go_by_their_feishu_names_once_bound(self):
        task = {"name": "ThesisP1", "feishu": {"meegle_space": "bw4etl"}}
        self.assertIn("飞书项目空间「bw4etl」", setup.where_text(task, {}))        # before binding: as the task file says
        self.assertIn("飞书知识库空间「ThesisP1」", setup.where_text(task, {}))    # the knowledge space is named after the project
        ids = {"meegle": {"project_key": "p_1", "space_name": "ThesisP1"}, "wiki": {"space_id": "s_1", "space_name": "ThesisP1 知识库"}}
        text = setup.where_text(task, ids)
        self.assertIn("飞书项目空间「ThesisP1」，project_key `p_1`", text)
        self.assertIn("飞书知识库空间「ThesisP1 知识库」，space_id `s_1`", text)
        self.assertNotIn("bw4etl", text)
        self.assertIn("飞书项目「ThesisP1」（project_key p_1）；知识库「ThesisP1 知识库」。", setup.feishu_readme(task, ids))


class PoolModeTest(PoolProjectCase):
    """setup.py pool: in bypass mode the app asks the researcher before every start_session, so it is said first."""

    def checklist(self, mode, sid="s-init"):
        if mode:
            pool_lib.update_session(self.tmp, sid, lambda st: st.update({"session_id": sid, "permission_mode": mode}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {pool_lib.SESSION_ENV: sid}), contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp]), 0)
        return out.getvalue()

    def test_bypass_mode_is_told_before_the_pool_opens(self):
        text = self.checklist("bypassPermissions")
        self.assertIn("注意：本会话是 bypass 模式。", text)
        self.assertIn("还没开的 21 个会话要点 21 次", text)
        self.assertIn("1. 换 auto 模式：", text)
        self.assertIn("2. 留在本会话：", text)
        self.assertLess(text.index("注意：本会话是 bypass 模式。"), text.index("只开下面这 7 个 +2"))

    def test_auto_mode_and_an_unknown_mode_are_not_warned(self):
        self.assertNotIn("bypass 模式", self.checklist("auto"))
        self.assertNotIn("bypass 模式", self.checklist(None, sid="s-nobody"))

    def test_once_sessions_are_open_the_mode_is_kept(self):
        self.started_pool()
        text = self.checklist("bypassPermissions")
        self.assertIn("接着在本会话开", text)
        self.assertNotIn("1. 换 auto 模式：", text)

    def test_the_manual_checklist_says_nothing_of_it(self):
        pool_lib.update_session(self.tmp, "s-init", lambda st: st.update({"session_id": "s-init", "permission_mode": "bypassPermissions"}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {pool_lib.SESSION_ENV: "s-init"}), contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp, "--manual"]), 0)
        self.assertNotIn("bypass 模式", out.getvalue())                  # the researcher opens those sessions by hand


class CheckLayersTest(PoolProjectCase):
    """setup.py check: the project's own files, the Feishu side, the sessions, the first research task."""

    def setUp(self):
        super().setUp()
        with contextlib.redirect_stdout(io.StringIO()):       # take in the IDs written after the first init
            plan = setup.build_plan(self.tmp, self.task_path, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        self.plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        self.fake = fake_space.FakeSpace()

    def check(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(["check", "--target", self.tmp] + list(args))
        return code, out.getvalue()

    def claim_all(self):
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        with pool_lib.locked(self.tmp) as table:
            for e in setup.plan_entries(self.plan):
                permit = {"role": e["role"], "name": e["name"], "via": "test", "by": "researcher"}
                claim, note = pool_lib.promote(table, policy, permit, "s-" + e["name"], "local_" + e["name"])
                self.assertIsNotNone(claim, note)

    def listing(self, edit=None):
        rows = {name: {"sessionId": "local_" + name, "title": "[%s] %s" % (self.plan["label"], name), "cwd": self.tmp,
                       "isArchived": False, "group": {"id": "g1", "name": self.plan["label"]}, "pinned": False}
                for name in (e["name"] for e in setup.plan_entries(self.plan))}
        if edit:
            edit(rows)
        path = os.path.join(self.tmp, "sessions.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(rows.values()), f, ensure_ascii=False)
        return path

    def ready(self):
        """Everything the opening leaves behind: the claims, the first task, the links in .env."""
        self.claim_all()
        lg = fake_space.SimLedger(self.tmp, "apply", self.fake, ())
        with contextlib.redirect_stdout(io.StringIO()):
            task = ledger.register(lg, os.path.join(self.tmp, setup.FIRST_TASK))
            setup.env_cmd(self.tmp, True)
        return task

    def wait(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(["pool", "--target", self.tmp, "--wait", "0"])
        return code, out.getvalue()

    def test_wait_says_who_is_missing_and_who_opens_it(self):
        code, text = self.wait()
        self.assertEqual(code, 1)
        self.assertIn("21 个会话里还有 21 个没认领", text)
        self.assertIn("- PMO-p2-01：+2，由做初始化的会话开出", text)
        self.assertIn("- PMO-p0-01：由 PMO-p1-01 开出，PMO-p1-01 还没认领", text)
        self.assertNotIn("请研究员点开上一级的会话", text)
        self.claim_all()
        with pool_lib.locked(self.tmp) as table:
            del table["claims"]["Alg-p0-01"]
        code, text = self.wait()
        self.assertEqual(code, 1)
        self.assertIn("还有 1 个没认领", text)
        self.assertIn("- Alg-p0-01：由 Alg-p1-01 开出，Alg-p1-01 已认领", text)
        self.assertIn("请研究员点开上一级的会话，说一句“照认领输出开出下一级”", text)

    def wait_batch(self, n):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(["pool", "--target", self.tmp, "--wait", "0", "--batch", str(n)])
        return code, out.getvalue()

    def test_wait_for_a_batch_ends_when_its_first_turns_are_over(self):
        code, text = self.wait_batch(1)
        self.assertEqual(code, 1)
        self.assertIn("第 1 批的 6 个会话还没开齐", text)
        self.assertIn("- Product-p0-01：由 Product-p1-01 开出，Product-p1-01 还没认领", text)
        self.assertNotIn("Alg-p2-01：", text)                                    # another batch
        self.assertIn("不用再等这一批。接着开第 2 批：Alg-p2-01、RnD-p2-01。", text)
        self.claim_all()
        code, text = self.wait_batch(1)                                         # claimed, the hooks have not seen a turn end yet
        self.assertEqual(code, 1)
        self.assertIn("首轮还没结束的：PMO-p2-01、PMO-p1-01、Product-p2-01、Product-p1-01、Product-p0-01。", text)
        for name in ("PMO-p2-01", "PMO-p1-01", "Product-p2-01", "Product-p1-01"):
            self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-" + name})
        self.assertIn("首轮还没结束的：Product-p0-01。", self.wait_batch(1)[1])
        self.hook("instances.py", {"hook_event_name": "PermissionRequest", "session_id": "s-Product-p0-01", "tool_name": "Bash"})
        self.assertIn("停在确认框上、等研究员批准的：Product-p0-01。", self.wait_batch(1)[1])
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-Product-p0-01"})
        code, text = self.wait_batch(1)                                         # the scribe may still be writing
        self.assertEqual(code, 0, text)
        self.assertIn("第 1 批开齐：6 个会话都已认领，首轮都结束了。接着开第 2 批：Alg-p2-01、RnD-p2-01。", text)
        for name in ("Alg-p2-01", "Alg-p1-01", "Alg-p0-01", "RnD-p2-01", "RnD-p1-01", "RnD-p0-01"):
            self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-" + name})
        code, text = self.wait_batch(2)                                         # the round ends here: the researcher has to speak
        self.assertEqual(code, 0, text)
        self.assertIn("第 2 批开齐：6 个会话都已认领，首轮都结束了。这一轮到此为止：研究员每发一条消息，应用最多让会话自己开出 10 个会话，"
                      "这一轮用了 8 个。现在结束本轮，请研究员回一句“继续开池”；他回话以后再开第 3 批：Infra-p2-01、Quality-p2-01。", text)
        self.assertIn("接着开第 4 批：Liaison-p2-01。", self.wait_batch(3)[1])   # inside the second round
        for name in ("Liaison-p2-01", "Liaison-p1-01", "Liaison-p0-01"):
            self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-" + name})
        code, text = self.wait_batch(4)
        self.assertEqual(code, 0, text)
        self.assertIn("这是最后一批。接着把 +2 和本会话归入分区", text)
        self.assertEqual(self.wait_batch(5)[0], 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp, "--batch", "1"]), 2)
        self.assertIn("--batch 要和 --wait 一起用", out.getvalue())

    def test_wait_ends_when_the_scribe_has_done_its_first_writes(self):
        scribe = "s-PMO-p0-01"
        self.claim_all()
        code, text = self.wait()
        self.assertEqual(code, 1)
        self.assertIn("会话池已开齐：21/21 条认领。书记员的首轮还没结束（状态：unknown）", text)
        self.prompt(scribe, "认领 pmo-scribe（会话名 PMO-p0-01）。")
        self.assertIn("书记员的首轮还没结束（状态：busy）", self.wait()[1])
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": scribe})
        code, text = self.wait()                                  # its turn is over, nothing registered from this directory
        self.assertEqual(code, 0, text)
        self.assertIn("没有首条研究任务的登记回执", text)
        self.assertIn("桌面会话 local_PMO-p0-01", text)
        lg = fake_space.SimLedger(self.tmp, "apply", self.fake, ())
        with contextlib.redirect_stdout(io.StringIO()):
            task = ledger.register(lg, os.path.join(self.tmp, setup.FIRST_TASK))
        code, text = self.wait()
        self.assertEqual(code, 0, text)
        self.assertIn("知识库节点都在，首条研究任务已登记（#%s）" % task, text)
        ids = json.loads(self.read("feishu/ids.json"))
        ids["wiki"]["nodes"].pop(sorted(ids["wiki"]["nodes"])[0])
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)
        code, text = self.wait()
        self.assertEqual(code, 1)
        self.assertIn("书记员的首轮已结束，但是知识库节点还缺 1 个", text)
        self.hook("instances.py", {"hook_event_name": "PermissionRequest", "session_id": scribe, "tool_name": "Bash"})
        self.assertIn("书记员停在一个确认框上", self.wait()[1])

    def test_a_fresh_project_says_what_each_layer_lacks(self):
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("本地配置：就绪", text)
        self.assertIn("飞书：1 个问题", text)
        self.assertIn("setup.py env --target <项目目录> --apply", text)
        self.assertIn("会话：1 个问题", text)
        self.assertIn("会话池还没开齐", text)
        self.assertIn("首条研究任务：1 个问题", text)
        self.assertIn("还没有登记过研究任务", text)
        self.assertIn("检查：还有 3 个问题（飞书 1、会话 1、首条研究任务 1）", text)

    def test_every_layer_ready(self):
        self.ready()
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn("检查：四层都就绪", text)
        self.assertIn("加 --live 回读飞书", text)
        code, text = self.check("--sessions", self.listing())
        self.assertEqual(code, 0, text)

        def nested(rows):                                    # as the client lists them: only a +2 is filed, the rest sit under it
            for e in setup.plan_entries(self.plan):
                for c in e["children"]:
                    rows[c["name"]].update(startedBy="local_" + e["name"], group=None)
        code, text = self.check("--sessions", self.listing(nested))
        self.assertEqual(code, 0, text)

        def with_opener(rows):                               # a list taken elsewhere also has the session that opened the +2s
            nested(rows)
            rows["init"] = {"sessionId": "local_init", "title": "x", "cwd": self.tmp, "isArchived": False, "group": None}
            for top in self.plan["top"]:
                rows[top["name"]]["startedBy"] = "local_init"
        code, text = self.check("--sessions", self.listing(with_opener))
        self.assertEqual(code, 0, text)

        def unfiled(rows):                                   # one +2 left outside the section takes its whole sequence with it
            nested(rows)
            rows["Alg-p2-01"]["group"] = None
        code, text = self.check("--sessions", self.listing(unfiled))
        self.assertEqual(code, 1)
        self.assertIn("不在侧栏分区", text)
        for name in ("Alg-p2-01", "Alg-p1-01", "Alg-p0-01"):
            self.assertIn(name, text)
        self.assertNotIn("RnD-p1-01", text.split("不在侧栏分区")[1].split("\n")[0])

    def test_the_first_task_needs_its_document_and_its_id_in_the_draft(self):
        task = self.ready()
        receipt = os.path.join(self.tmp, "packages", "sent", "%s.json" % task)
        with open(receipt, encoding="utf-8") as f:
            rec = json.load(f)
        with open(receipt, "w", encoding="utf-8") as f:
            json.dump(dict(rec, doc=""), f)
        self.assertIn("回执里没有主文档的地址", self.check()[1])
        with open(receipt, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        path = os.path.join(self.tmp, setup.FIRST_TASK)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("#%s" % task, "待建"))
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("“关联工作项”一行不是 #%s" % task, out)

    def test_a_space_that_already_holds_research_tasks(self):
        """A second directory set up against a space in use: no receipt here, the tasks are there."""
        self.claim_all()
        with contextlib.redirect_stdout(io.StringIO()):
            setup.env_cmd(self.tmp, True)
        code, text = self.check()
        self.assertIn("空间里已经有研究任务的，加 --live 核对", text)
        other = fake_space.SimLedger(self.tmp, "apply", self.fake, ())
        sheet = os.path.join(self.tmp, "packages", "drafts", "pmo", "elsewhere.md")
        shutil.copyfile(os.path.join(self.tmp, setup.FIRST_TASK), sheet)
        with contextlib.redirect_stdout(io.StringIO()):
            ledger.register(other, sheet)
        sent = os.path.join(self.tmp, "packages", "sent")
        for name in os.listdir(sent):                                        # as if registered from another directory
            if name.endswith(".json"):
                os.remove(os.path.join(sent, name))
        ids = json.loads(self.read("feishu/ids.json"))
        snap = json.loads(self.read("feishu/space.json"))
        tokens = {n["node_token"] for n in ids["wiki"]["nodes"].values()}
        node = lambda cmd, timeout=30: (0, json.dumps({"data": {"node": {"space_id": "space_fake"}}}), "") \
            if "--node-token" in cmd and cmd[cmd.index("--node-token") + 1] in tokens else (1, "", "not found")
        with mock.patch.object(setup, "run", node), \
                mock.patch.object(setup, "read_space", lambda *a, **k: (snap, setup.space.binding_of(ids), [], [])), \
                mock.patch.object(setup.ledger, "Ledger", lambda root, mode: fake_space.SimLedger(root, mode, self.fake, ())):
            code, text = self.check("--live")
        self.assertEqual(code, 0, text)
        self.assertIn("空间里已有 1 条研究任务，这个目录里没有它们的登记回执", text)

    def test_sessions_are_held_against_the_clients_own_list(self):
        self.ready()

        def edit(rows):
            rows["Alg-p2-01"]["isArchived"] = True
            rows["Alg-p1-01"]["group"] = None
            rows["Alg-p0-01"]["cwd"] = "/tmp"
            rows["RnD-p2-01"]["pinned"] = True
            rows["RnD-p1-01"]["title"] = "随手起的名字"
            del rows["Infra-p2-01"]
            del rows["Infra-p1-01"]["group"]
        code, text = self.check("--sessions", self.listing(edit))
        self.assertEqual(code, 1)
        for line in ("客户端的会话列表里没有这些认领的会话：Infra-p2-01", "已经归档的会话还占着认领：Alg-p2-01",
                     "工作目录不是项目目录的会话：Alg-p0-01", "”里的会话：Alg-p1-01", "还没报告这些会话的分区，没核对：Infra-p1-01",
                     "置顶了的会话：RnD-p2-01", "<会话名>”的会话：RnD-p1-01", "会话：4 个问题"):
            self.assertIn(line, text)
        with open(os.path.join(self.tmp, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertIn("不是会话列表", self.check("--sessions", os.path.join(self.tmp, "bad.json"))[1])

    def readback(self, edit=None, own=None):
        ids = {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5"}
        rows = {e["name"]: {"sessionId": "local_" + e["name"], "model": ids[e["model"]], "effort": "xhigh",
                            "contextWindow": 1000000} for e in setup.plan_entries(self.plan)}
        if edit:
            edit(rows)
        path = os.path.join(self.tmp, "readback.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"self": dict({"model": "claude-fable-5-2", "effort": "xhigh", "contextWindow": 1000000}, **(own or {})),
                       "sessions": list(rows.values())}, f, ensure_ascii=False)
        return path

    def test_each_sessions_model_window_and_effort_are_read_back(self):
        self.ready()
        code, text = self.check("--readback", self.readback())
        self.assertEqual(code, 0, text)
        self.assertIn("| 本会话 | — | claude-fable-5-2 | xhigh | 1000000 |", text)
        self.assertIn("| PMO-p2-01 | fable | claude-fable-5-2 | xhigh | 1000000 |", text)
        self.assertIn("| PMO-p0-01 | opus | claude-opus-5-5 | xhigh | 1000000 |", text)
        table = text.split("回读表：")[1]
        self.assertEqual(table.count("\n| ") - 2, 1 + len(list(setup.plan_entries(self.plan))))   # header, rule, this session, the pool
        self.assertLess(table.index("PMO-p2-01"), table.index("PMO-p1-01"))                  # in the order the pool opens
        self.assertNotIn("回读表", self.check()[1])

    def test_a_wrong_model_or_a_narrow_window_is_a_problem(self):
        self.ready()

        def edit(rows):
            rows["PMO-p2-01"]["model"] = "claude-opus-5-5"             # start_session fell back to the opener's model
            rows["Alg-p1-01"]["model"] = "claude-sonnet-5-5"
            rows["Alg-p0-01"]["contextWindow"] = 200000
        code, text = self.check("--readback", self.readback(edit))
        self.assertEqual(code, 1)
        self.assertIn("会话：2 个问题", text)
        self.assertIn("模型和开池清单不在一档的会话：PMO-p2-01、Alg-p1-01", text)
        self.assertIn("窗口小于本会话（1000000）的会话：Alg-p0-01", text)
        self.assertIn("autoCompactWindow", text)
        code, text = self.check("--readback", self.readback(own={"model": "claude-opus-5-5", "contextWindow": 200000}))
        self.assertEqual(code, 0, text)                               # the pool matches its plan; this session's window is no floor

    def test_another_effort_or_an_unread_session_is_a_reminder(self):
        self.ready()

        def edit(rows):
            rows["RnD-p2-01"]["effort"] = "high"
            rows["RnD-p1-01"]["contextWindow"] = None                 # get_usage said unavailable
            del rows["RnD-p0-01"]
        code, text = self.check("--readback", self.readback(edit))
        self.assertEqual(code, 0, text)
        self.assertIn("提醒：推理强度和本会话（xhigh）不同的会话：RnD-p2-01", text)
        self.assertIn("提醒：没读到模型或窗口的会话：RnD-p1-01、RnD-p0-01", text)
        self.assertIn("| RnD-p0-01 | opus | 读不到 | 读不到 | 读不到 |", text)
        code, text = self.check("--readback", self.readback(own={"contextWindow": None}))
        self.assertEqual(code, 0, text)
        self.assertIn("回读表里没有本会话的窗口", text)
        with open(os.path.join(self.tmp, "bad.json"), "w", encoding="utf-8") as f:
            f.write("[]")
        code, text = self.check("--readback", os.path.join(self.tmp, "bad.json"))
        self.assertEqual(code, 1)
        self.assertIn("不是回读表", text)

    def test_a_session_working_elsewhere_and_a_leftover_pre_registration(self):
        self.ready()
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-Alg-p2-01", "source": "startup", "cwd": "/tmp"})
        self.route("expect", "Alg-p1-02", "--role", "alg-p1", "--desk", "local_extra", sid="s-researcher")
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("工作目录不是项目目录的会话：Alg-p2-01", text)
        self.assertIn("有 1 条预登记还没转正：Alg-p1-02", text)

    def test_live_reads_the_space_the_nodes_and_the_first_task_back(self):
        task = self.ready()
        ids = json.loads(self.read("feishu/ids.json"))
        snap = json.loads(self.read("feishu/space.json"))
        tokens = {n["node_token"] for n in ids["wiki"]["nodes"].values()}
        lost = set()

        def tools(cmd, timeout=30):
            if cmd[:3] == ["lark-cli", "wiki", "+node-get"]:
                token = cmd[cmd.index("--node-token") + 1]
                if token in tokens - lost:
                    return 0, json.dumps({"data": {"node": {"node_token": token, "space_id": "space_fake"}}}), ""
                return 1, "", "node not found"
            return 1, "", "tests do not call %s" % " ".join(cmd[:3])

        def live():
            binding = setup.space.binding_of(ids)
            with mock.patch.object(setup, "run", tools), \
                    mock.patch.object(setup, "read_space", lambda *a, **k: (snap, binding, [], [])), \
                    mock.patch.object(setup.ledger, "Ledger", lambda root, mode: fake_space.SimLedger(root, mode, self.fake, ())):
                return self.check("--live")
        code, text = live()
        self.assertEqual(code, 0, text)
        self.assertIn("检查：四层都就绪", text)
        self.assertNotIn("加 --live", text)
        lost.add("node_archive_alg")
        code, text = live()
        self.assertEqual(code, 1)
        self.assertIn("知识库节点读不到，或者不在这个知识空间里（1 个）：archive/alg", text)
        lost.clear()
        key = fake_space.SimLedger(self.tmp, "preview", self.fake, ()).field("research_task", "main_doc")["field_key"]
        self.fake.item(task)["fields"][key] = "https://example.feishu.cn/wiki/elsewhere"
        code, text = live()
        self.assertEqual(code, 1)
        self.assertIn("研究任务 #%s 的主文档字段是 https://example.feishu.cn/wiki/elsewhere" % task, text)


class PoolClaimTest(PoolProjectCase):
    def test_claim_needs_a_permit(self):
        cmd = "python3 -I %s claim alg-p1 --as Alg-p1-01" % self.R
        _, out = self.bash("s-nobody", cmd)
        self.assertNotIn("permissionDecision", out)      # route.py checks the permit against the session's own IDs
        code, text = self.route("claim", "alg-p1", "--as", "Alg-p1-01", "--wait", "0", sid="s-nobody")
        self.assertEqual(code, 1)
        self.assertIn("没有找到", text)
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, ".claude", "state", "pool.json")) and self.pool()["claims"])

    def test_opening_line_claims_a_p2_only(self):
        _, out = self.opening("alg-p2", "Alg-p2-01", "s1")
        self.assertIn("本会话是 Alg-p2-01", out.get("additionalContext", ""))
        self.assertIn("算法 +2", out.get("additionalContext", ""))
        self.assertEqual(self.pool()["claims"]["Alg-p2-01"]["session_id"], "s1")
        self.prompt("s2", "你好")
        self.opening("product-p2", "Product-p2-01", "s2")                  # not the first prompt
        self.prompt("s3", "认领 alg-p1（会话名 Alg-p1-01）。先运行 route.py claim")   # not a +2
        self.assertEqual(sorted(self.pool()["claims"]), ["Alg-p2-01"])
        _, out = self.opening("alg-p2", "Alg-p2-01", "s4")                 # a second alg-p2
        self.assertIn("上限", out.get("additionalContext", ""))
        self.assertEqual(sorted(self.pool()["claims"]), ["Alg-p2-01"])

    def test_who_may_pre_register(self):
        self.opening("alg-p2", "Alg-p2-01", "s1")
        self.opening("pmo-p2", "PMO-p2-01", "s-entry")
        self.assertEqual(self.route("expect", "Alg-p1-01", "--role", "alg-p1", "--desk", "d1", sid="s1")[0], 0)
        self.assertEqual(self.route("expect", "Quality-p1-01", "--role", "quality-p1", "--desk", "d2", sid="s1")[0], 1)
        self.assertEqual(self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d3", sid="s1")[0], 1)
        cmd = "python3 -I %s expect Quality-p1-01 --role quality-p1 --desk d2" % self.R
        self.assertEqual(self.bash("s1", cmd)[1].get("permissionDecision"), "deny")
        self.assertEqual(self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d3", sid="s-entry")[0], 0)
        self.assertEqual(self.route("expect", "Rnd-p0-01", "--role", "rnd-p0", "--desk", "d4", sid="s-researcher")[0], 0)
        self.assertEqual(self.route("expect", "Rnd-p1-01", "--role", "alg-p1", "--desk", "d5", sid="s-researcher")[0], 1)
        self.assertEqual(sorted(p["name"] for p in self.pool()["permits"]), ["Alg-p1-01", "PMO-p0-01", "RnD-p0-01"])   # canonical case

    def test_desktop_id_in_the_hook_promotes_at_once(self):
        self.opening("alg-p2", "Alg-p2-01", "s1")
        self.route("expect", "Alg-p1-01", "--role", "alg-p1", "--desk", "d1", sid="s1")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s5", "source": "startup"}, desk="d1")
        self.assertIn("本会话是 Alg-p1-01", out.get("additionalContext", ""))
        self.assertEqual(self.pool()["claims"]["Alg-p1-01"]["session_id"], "s5")
        _, out = self.bash("s5", "python3 -I %s claim alg-p1 --as Alg-p1-01 --desk d9" % self.R, desk="d1")
        self.assertEqual(out.get("permissionDecision"), "deny")
        code, text = self.route("claim", "alg-p1", "--as", "Alg-p1-01", "--wait", "0", sid="s5", desk="d1")
        self.assertEqual(code, 0)
        self.assertIn("已认领", text)

    def test_two_step_claim_without_a_desktop_id_in_the_hook(self):
        self.opening("alg-p2", "Alg-p2-01", "s1")
        self.route("expect", "Alg-p1-01", "--role", "alg-p1", "--desk", "d1", sid="s1")
        cmd = "python3 -I %s claim alg-p1 --as Alg-p1-01" % self.R
        _, out = self.bash("s6", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")        # a permit waits for this role
        code, text = self.route("claim", "alg-p1", "--as", "Alg-p1-01", "--wait", "0", sid="s6", desk="d1")
        self.assertEqual(code, 0)
        self.assertIn("待转正", text)
        self.assertNotIn("Alg-p1-01", self.pool()["claims"])
        self.hook("audit.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s6", "agent_id": "x",
                               "tool_input": {"command": cmd}, "tool_response": {"stdout": text}})
        self.assertNotIn("Alg-p1-01", self.pool()["claims"])               # a subagent cannot finish it
        self.hook("audit.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s6",
                               "tool_input": {"command": cmd}, "tool_response": {"stdout": text}})
        claim = self.pool()["claims"]["Alg-p1-01"]
        self.assertEqual((claim["session_id"], claim["desk"]), ("s6", "d1"))
        self.assertEqual(self.pool()["permits"], [])

    def test_one_entry_and_one_scribe(self):
        self.started_pool()
        code, text = self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d-x", sid="s-researcher")
        self.assertEqual(code, 1)
        _, out = self.opening("pmo-p2", "PMO-p2-01", "s-second")
        self.assertIn("上限", out.get("additionalContext", ""))
        pl = self.pool()
        self.assertEqual([c["name"] for c in pl["claims"].values() if c["role"] == "pmo-scribe"], ["PMO-p0-01"])

    def test_release_leaves_a_tombstone(self):
        self.started_pool()
        _, out = self.bash("s-alg1", "python3 -I %s release Alg-p0-01" % self.R)
        self.assertEqual(out.get("permissionDecision"), "deny")           # a claimed session cannot release
        self.assertEqual(self.route("release", "Alg-p0-01", sid="s-alg1")[0], 1)
        _, out = self.bash("s-researcher", "python3 -I %s release Alg-p0-01" % self.R)
        self.assertEqual(out.get("permissionDecision"), "ask")
        self.assertEqual(self.route("release", "Alg-p0-01", sid="s-researcher")[0], 0)
        pl = self.pool()
        self.assertNotIn("Alg-p0-01", pl["claims"])
        self.assertEqual(pl["tombstones"][-1]["name"], "Alg-p0-01")
        _, out = self.bash("s-alg0", "python3 -I %s register packages/drafts/alg/x.md --apply" % self.L)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.bash("s-alg0", "python3 -I %s claim alg-p0 --as Alg-p0-01" % self.R)
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.route("expect", "Alg-p0-01", "--role", "alg-p0", "--desk", "d-s-alg0", sid="s-alg2")
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg0", "source": "resume"}, desk="d-s-alg0")
        self.assertEqual(self.pool()["claims"]["Alg-p0-01"]["session_id"], "s-alg0")
        self.assertEqual(self.pool()["tombstones"], [])

    def test_clear_reattaches_the_claim(self):
        self.started_pool()
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-alg1", "reason": "clear"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1b", "source": "clear"}, desk="d-s-alg1")
        self.assertEqual(self.pool()["claims"]["Alg-p1-01"]["session_id"], "s-alg1b")
        # without a desktop ID: one SessionEnd(clear) in the last minute pairs with the next SessionStart(clear)
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-entry", "reason": "clear"})
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-entry2", "source": "clear"})
        self.assertEqual(self.pool()["claims"]["PMO-p2-01"]["session_id"], "s-entry2")
        self.assertIn("上下文清空后接回认领", out.get("additionalContext", ""))
        # two at once: neither is guessed
        for sid in ("s-q2", "s-q1"):
            self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": sid, "reason": "clear"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-new", "source": "clear"})
        pl = self.pool()
        self.assertEqual((pl["claims"]["Quality-p2-01"]["session_id"], pl["claims"]["Quality-p1-01"]["session_id"]),
                         ("s-q2", "s-q1"))


class PoolOpenedByTheResearcherTest(PoolProjectCase):
    """Sessions pre-registered by the session that opens them: the researcher's own session the
    +2s, a +2 its +1s, a +1 its +0s."""

    OPENED = (("PMO-p0-01", "pmo-scribe", "s-scribe"), ("PMO-p2-01", "pmo-p2", "s-entry"),
              ("Alg-p2-01", "alg-p2", "s-alg2"), ("Alg-p0-01", "alg-p0", "s-alg0"))

    def open_all(self):
        outs = {}
        for name, role, sid in self.OPENED:
            code, out = self.route("expect", name, "--role", role, "--desk", "d-" + sid, sid="s-researcher")
            self.assertEqual(code, 0, out)
            code, outs[name] = self.claim(role, name, sid, desk="d-" + sid)
            self.assertEqual(code, 0, outs[name])
        return outs

    def test_every_role_is_claimed_through_a_pre_registration(self):
        self.open_all()
        self.assertEqual(sorted(self.pool()["claims"]), sorted(n for n, _, _ in self.OPENED))
        self.assertEqual(self.pool()["permits"], [])

    def test_pre_registering_twice_is_fine(self):
        self.open_all()
        code, out = self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-s-alg2", sid="s-researcher")
        self.assertEqual(code, 0, out)                       # the opening run again after an interruption
        self.assertIn("不用再预登记", out)
        code, out = self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-other", sid="s-researcher")
        self.assertEqual(code, 1, out)
        self.assertIn("已经被认领", out)

    def test_a_p2_may_claim_through_its_opening_line_before_or_after(self):
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        briefs = {t["name"]: t["brief"] for t in plan["top"]}
        self.prompt("s-alg2", briefs["Alg-p2-01"], desk="d-s-alg2")           # the hook reads the opening line
        self.assertIn("Alg-p2-01", self.pool()["claims"])
        code, out = self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-s-alg2", sid="s-researcher")
        self.assertEqual(code, 0, out)
        self.assertIn("不用再预登记", out)
        code, out = self.route("expect", "RnD-p2-01", "--role", "rnd-p2", "--desk", "d-s-rnd2", sid="s-researcher")
        self.assertEqual(code, 0, out)                                         # the other order: pre-registered first
        self.prompt("s-rnd2", briefs["RnD-p2-01"], desk="d-s-rnd2")
        self.assertIn("RnD-p2-01", self.pool()["claims"])
        self.assertEqual(self.pool()["permits"], [])                           # nothing is left waiting

    def test_each_level_opens_the_next(self):
        """A +2 pre-registers and opens its +1, a +1 its +0: a session nests under the one that opened it."""
        self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-alg2", sid="s-researcher")
        self.claim("alg-p2", "Alg-p2-01", "s-alg2", desk="d-alg2")
        _, out = self.route("claim", "alg-p2", "--as", "Alg-p2-01", "--wait", "0", sid="s-alg2", desk="d-alg2")
        self.assertIn("- Alg-p1-01：name=alg-p1-01", out)
        self.assertNotIn("- Alg-p0-01：", out)                                 # not the +2's to open
        self.assertEqual(self.route("expect", "Alg-p1-01", "--role", "alg-p1", "--desk", "d-alg1", sid="s-alg2")[0], 0)
        _, out = self.claim("alg-p1", "Alg-p1-01", "s-alg1", desk="d-alg1")   # its very first claim already says what to open
        self.assertIn("- Alg-p0-01：name=alg-p0-01", out)
        _, verdict = self.bash("s-alg1", "python3 -I %s expect Alg-p0-01 --role alg-p0 --desk d-alg0" % self.R, desk="d-alg1")
        self.assertNotEqual(verdict.get("permissionDecision"), "deny")
        code, out = self.route("expect", "Alg-p0-01", "--role", "alg-p0", "--desk", "d-alg0", sid="s-alg1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.claim("alg-p0", "Alg-p0-01", "s-alg0", desk="d-alg0")[0], 0)
        for role, name in (("rnd-p0", "RnD-p0-01"), ("alg-p1", "Alg-p1-02")):   # another sequence, its own rank
            _, verdict = self.bash("s-alg1", "python3 -I %s expect %s --role %s --desk d-x" % (self.R, name, role), desk="d-alg1")
            self.assertEqual(verdict.get("permissionDecision"), "deny", name)
            self.assertEqual(self.route("expect", name, "--role", role, "--desk", "d-x", sid="s-alg1")[0], 1, name)
        self.assertEqual(sorted(self.pool()["claims"]), ["Alg-p0-01", "Alg-p1-01", "Alg-p2-01"])

    def test_the_router_opens_the_scribe_with_its_first_writes(self):
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        self.prompt("s-entry", plan["top"][0]["brief"], desk="d-entry")        # the entry claims through its opening line
        _, out = self.route("claim", "pmo-p2", "--as", "PMO-p2-01", "--wait", "0", sid="s-entry", desk="d-entry")
        self.assertIn("- PMO-p1-01：", out)
        self.assertNotIn("- PMO-p0-01：", out)
        self.route("expect", "PMO-p1-01", "--role", "pmo-router", "--desk", "d-router", sid="s-entry")
        _, out = self.claim("pmo-router", "PMO-p1-01", "s-router", desk="d-router")
        row = out[out.index("- PMO-p0-01："):]
        self.assertIn("ledger.py ensure-node all --apply", row)                # nothing is registered yet
        self.assertNotIn("就结束本轮，等委托消息", row.split("background：")[0])
        self.assertEqual(self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d-scribe", sid="s-router")[0], 0)
        with open(os.path.join(self.tmp, "packages", "sent", "7160000001.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "registered", "draft": "packages/drafts/pmo/x.md"}, f)
        _, out = self.route("claim", "pmo-router", "--as", "PMO-p1-01", "--wait", "0", sid="s-router", desk="d-router")
        self.assertNotIn("ensure-node all --apply", out)                       # a later scribe gets the plain opening

    def test_a_p2_is_told_its_title_and_section(self):
        self.open_all()
        label = json.loads(self.read(".claude/solo-lab/pool-plan.json"))["label"]
        code, out = self.route("claim", "alg-p2", "--as", "Alg-p2-01", "--wait", "0", sid="s-alg2", desk="d-s-alg2")
        self.assertEqual(code, 0, out)
        self.assertIn("title=“[%s] Alg-p1-01”" % label, out)
        self.assertIn("归入侧栏分区“%s”" % label, out)
        self.assertNotIn("置顶", out)

    def test_a_session_records_where_it_works(self):
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-entry", "source": "startup"}, desk="d-s-entry")
        self.assertEqual(self.session("s-entry")["cwd"], self.tmp)

    def test_only_the_researcher_starts_a_login(self):
        self.open_all()
        cmd = "python3 -I /opt/plugins/solo-lab/scripts/setup.py login"
        _, out = self.bash("s-alg2", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("登录由研究员的会话发起", out.get("permissionDecisionReason", ""))
        _, out = self.bash("s-researcher", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        _, out = self.bash("s-researcher", cmd, agent_id="a1")
        self.assertEqual(out.get("permissionDecision"), "deny")           # a subagent of the researcher's session
        _, out = self.bash("s-alg2", "python3 -I /opt/plugins/solo-lab/scripts/setup.py check --target .")
        self.assertEqual(out.get("permissionDecision"), "deny")           # every setup.py command is the researcher's
        self.assertIn("只由研究员的会话运行", out.get("permissionDecisionReason", ""))

    def test_the_researcher_runs_the_plugins_own_setup_in_any_mode(self):
        """After init the hook is on in the session that initialises: setup.py, where the plugin is
        installed, has to go on working there, also when no prompt can appear."""
        self.open_all()
        real = shlex.quote(os.path.join(ROOT, "scripts", "setup.py"))
        for args in ("feishu --target . --live --apply", "pool --target .", "check --target . --live",
                     "env --target . --apply", "login", "init --target . --task solo-lab.task.json --apply"):
            for mode in ("default", "bypassPermissions"):
                _, out = self.bash("s-researcher", "python3 -I %s %s" % (real, args), permission_mode=mode)
                self.assertNotIn(out.get("permissionDecision"), ("deny", "ask"), (args, mode, out))
            _, out = self.bash("s-alg2", "python3 -I %s %s" % (real, args))
            self.assertEqual(out.get("permissionDecision"), "deny", args)
        space_py = shlex.quote(os.path.join(ROOT, "scripts", "space.py"))
        _, out = self.bash("s-alg2", "python3 -I %s export --space x --out .claude/settings.json" % space_py)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.bash("s-alg2", "python3 -I .claude/solo-lab/bin/space.py check --target .")
        self.assertNotEqual(out.get("permissionDecision"), "deny")           # reading the space is for every session

    def test_a_setup_script_that_is_not_the_recorded_one_is_not_trusted(self):
        self.open_all()
        real = os.path.join(ROOT, "scripts", "setup.py")
        manifest = os.path.join(self.tmp, ".claude", "solo-lab", "manifest.json")
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(sorted(os.path.basename(p) for p in m["plugin_scripts"]), ["onboarding.py", "setup.py", "space.py"])
        m["plugin_scripts"][real] = "0" * 64                                 # another build of the plugin
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(m, f)
        _, out = self.bash("s-researcher", "python3 -I %s feishu --target . --live" % shlex.quote(real), permission_mode="bypassPermissions")
        self.assertEqual(out.get("permissionDecision"), "deny")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertIn("hook 不再放行研究员会话里的 setup.py", out.getvalue())


class PoolTreeAfterOpeningTest(PoolProjectCase):
    """Sessions opened after the pool nest the same way: a +2 opens a +1, a +1 a +0."""

    POOL = {"p1": 1, "p0": 1, "later": ["liaison"], "max_sessions": 30}

    def test_a_sequence_opened_later_nests_too(self):
        self.route("expect", "Liaison-p2-01", "--role", "liaison-p2", "--desk", "d-l2", sid="s-researcher")
        _, out = self.claim("liaison-p2", "Liaison-p2-01", "s-l2", desk="d-l2")
        self.assertIn("本序列开池时只开 +2", out)
        row = out[out.index("- Liaison-p1-01："):]
        self.assertIn("（补位用）", row.split("\n")[0])
        self.assertIn("再照输出里的表开出 Liaison-p0-01", row.split("background：")[0])   # the first +1 brings the first +0 along
        self.assertNotIn("- Liaison-p0-01：", out)                              # not the +2's to open
        self.assertEqual(self.route("expect", "Liaison-p1-01", "--role", "liaison-p1", "--desk", "d-l1", sid="s-l2")[0], 0)
        _, out = self.claim("liaison-p1", "Liaison-p1-01", "s-l1", desk="d-l1")
        self.assertIn("- Liaison-p0-01：name=liaison-p0-01", out)                # not in the plan, and still told what to open
        self.assertEqual(self.route("expect", "Liaison-p0-01", "--role", "liaison-p0", "--desk", "d-l0", sid="s-l1")[0], 0)
        self.assertEqual(self.claim("liaison-p0", "Liaison-p0-01", "s-l0", desk="d-l0")[0], 0)
        _, out = self.route("claim", "liaison-p2", "--as", "Liaison-p2-01", "--wait", "0", sid="s-l2", desk="d-l2")
        row = out[out.index("- Liaison-p1-02："):]
        self.assertNotIn("再照输出里的表开出", row.split("background：")[0])     # the sequence has its +0 now
        self.assertNotIn("- Liaison-p0-", out)

    def test_a_refilled_p1_is_told_what_to_open(self):
        self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-a2", sid="s-researcher")
        self.claim("alg-p2", "Alg-p2-01", "s-a2", desk="d-a2")
        for name, sid in (("Alg-p1-01", "s-a1"), ("Alg-p1-02", "s-a1b")):
            self.assertEqual(self.route("expect", name, "--role", "alg-p1", "--desk", "d-" + sid, sid="s-a2")[0], 0)
            code, out = self.claim("alg-p1", name, sid, desk="d-" + sid)
            self.assertEqual(code, 0, out)
        row = out[out.index("- Alg-p0-02："):]                                  # Alg-p0-01 is kept for Alg-p1-01, which opens it
        self.assertIn("（补位用）", row.split("\n")[0])
        self.assertNotIn("- Alg-p0-01：", out)
        self.assertEqual(self.route("expect", "Alg-p0-02", "--role", "alg-p0", "--desk", "d-a0b", sid="s-a1b")[0], 0)
        _, out = self.route("claim", "alg-p2", "--as", "Alg-p2-01", "--wait", "0", sid="s-a2", desk="d-a2")
        self.assertNotIn("- Alg-p0-", out)                                      # a +0 is opened by a +1, never by this +2
        code, out = self.route("pool", "--role", "alg-p0", sid="s-a2")
        self.assertIn("本序列的一个 +1", out)


class DefaultModelTest(PoolProjectCase):
    """The defaults: the entry runs fable, every other session opus."""

    def test_the_entry_runs_fable_and_the_rest_opus(self):
        task, _, _ = setup.load_task_and_seeds(TASK)
        self.assertEqual(task["models"], setup.DEFAULT_MODELS)          # the example says what the defaults are
        with open(self.task_path, encoding="utf-8") as f:
            raw = json.load(f)
        raw.pop("models")
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, self.task_path, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        models = {e["role"]: e["model"] for e in setup.plan_entries(json.loads(self.read(".claude/solo-lab/pool-plan.json")))}
        self.assertEqual(models.pop("pmo-p2"), "fable")
        self.assertIn("pmo-scribe", models)
        self.assertEqual(set(models.values()), {"opus"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp]), 0)
        self.assertNotRegex(out.getvalue(), "注意：\\S+ 的模型")              # opus under fable, opus under opus


class PoolModelTest(PoolProjectCase):
    """A session cannot run a higher model than the session that opens it."""

    def setUp(self):
        super().setUp()
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        task["models"] = dict(task.get("models") or {}, **{"alg-p0": "opus", "alg-p1": "sonnet", "alg-p2": "opus"})
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, self.task_path, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))

    def test_the_checklist_names_a_model_above_its_opener(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp]), 0)
        text = out.getvalue()
        self.assertIn("注意：Alg-p0-01 的模型 opus 高于开它的 Alg-p1-01（sonnet）", text)
        self.assertEqual(text.count("注意："), 1)                   # opus under opus, sonnet under opus: nothing to say
        self.assertEqual([setup.model_tier(m) for m in ("claude-haiku-5-5", "sonnet", "Opus", "inherit", None)], [0, 1, 2, -1, -1])


class PoolBatchTest(PoolProjectCase):
    """A sequence that opens more below its +2 than a batch has room for."""

    POOL = {"p1": 1, "p0": 4, "later": ["liaison"], "max_sessions": 40}

    def test_the_checklist_says_which_sequences_overflow(self):
        plan = json.loads(self.read(".claude/solo-lab/pool-plan.json"))
        self.assertEqual(plan["batches"], [["PMO-p2-01"], ["Product-p2-01"], ["Alg-p2-01"], ["RnD-p2-01"], ["Infra-p2-01"],
                                           ["Quality-p2-01", "Liaison-p2-01"]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(["pool", "--target", self.tmp]), 0)
        text = out.getvalue()
        self.assertEqual(plan["rounds"], [[1, 2], [3], [4], [5], [6]])
        self.assertIn("分 6 批、5 轮开", text)
        self.assertIn("注意：Product-p2-01 下面要开 5 个会话，一批最多容得下 4 个", text)
        self.assertEqual(text.count("注意："), 5)                               # not the entry: a router and a scribe
        self.assertNotIn("注意：PMO-p2-01", text)


class PoolWithoutP1Test(PoolProjectCase):
    """A sequence run without +1s: the +2 opens its +0s itself, until it has a +1."""

    POOL = {"p1": 0, "p0": 2, "later": [], "max_sessions": 30}

    def test_the_p2_opens_its_p0s(self):
        self.route("expect", "Alg-p2-01", "--role", "alg-p2", "--desk", "d-a2", sid="s-researcher")
        _, out = self.claim("alg-p2", "Alg-p2-01", "s-a2", desk="d-a2")
        for name in ("Alg-p0-01", "Alg-p0-02"):
            self.assertIn("- %s：name=%s，" % (name, name.lower()), out)
            self.assertNotIn("（补位用）", out[out.index("- %s：" % name):].split("\n")[0])
        self.assertNotIn("- Alg-p0-03：", out)                                  # no refill before the planned ones are open
        row = out[out.index("- Alg-p1-01："):]
        self.assertIn("（补位用）", row.split("\n")[0])
        self.assertNotIn("再照输出里的表开出", row.split("background：")[0])     # the sequence has its +0s planned
        self.assertEqual(self.route("expect", "Alg-p1-01", "--role", "alg-p1", "--desk", "d-a1", sid="s-a2")[0], 0)
        _, out = self.claim("alg-p1", "Alg-p1-01", "s-a1", desk="d-a1")
        self.assertIn("- Alg-p0-03：name=alg-p0-03", out)                        # from now on a +0 goes under the +1
        _, out = self.route("claim", "alg-p2", "--as", "Alg-p2-01", "--wait", "0", sid="s-a2", desk="d-a2")
        self.assertNotIn("- Alg-p0-03：", out)


class PoolGuardTest(PoolProjectCase):
    W = "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"

    def setUp(self):
        super().setUp()
        self.started_pool()

    def decision(self, sid, command, **extra):
        return self.bash(sid, command, **extra)[1].get("permissionDecision")

    def test_only_the_claimed_scribe_writes_the_ledger(self):
        self.assertEqual(self.decision("s-researcher", self.W), "ask")
        self.assertEqual(self.decision("s-researcher", self.W, permission_mode="bypassPermissions"), "deny")
        self.assertEqual(self.decision("s-alg2", self.W), "deny")
        self.assertEqual(self.decision("s-scribe", self.W), "allow")
        self.assertNotEqual(self.decision("s-alg2", "python3 -I .claude/solo-lab/bin/ledger.py query open"), "deny")
        _, out = self.bash("s-alg2", "meegle comment add --work-item-id 1 --content hi")
        self.assertEqual(out.get("permissionDecision"), "allow")
        self.assertIn("[Alg+2]", out.get("updatedInput", {}).get("command", ""))

    def test_the_scribe_uploads_a_local_file_for_a_link_field(self):
        L = "python3 -I .claude/solo-lab/bin/ledger.py"
        update = L + " update 2101 --set 需求文档=@packages/drafts/product/prd.md --apply"
        confirm = L + " confirm 2101 --by Alg-p2-01 --set 上下文文档=@packages/drafts/alg/context.md --apply"
        for command in (update, confirm):
            self.assertEqual(self.decision("s-scribe", command), "allow", command)
            self.assertEqual(self.decision("s-scribe", command, permission_mode="bypassPermissions"), "allow", command)
            self.assertEqual(self.decision("s-alg2", command), "deny", command)
            self.assertEqual(self.decision("s-researcher", command), "ask", command)
        self.assertEqual(self.decision("s-alg2", update.replace(" --apply", "")), "deny")     # the preview is the scribe's too

    def test_two_scribe_claims_stop_all_writes(self):
        pl = self.pool()
        pl["claims"]["PMO-p0-02"] = dict(pl["claims"]["PMO-p0-01"], name="PMO-p0-02", session_id="s-x")
        self.write_pool(pl)
        self.assertEqual(self.decision("s-scribe", self.W), "deny")

    def test_no_write_power_outside_the_claim(self):
        self.assertEqual(self.decision("s-alg2", self.W, agent_id="a1", agent_type="pmo-scribe"), "deny")   # subagent
        self.assertEqual(self.decision("s-term", self.W, agent_type="pmo-scribe"), "deny")                  # claude --agent
        self.assertEqual(self.decision("s-scribe", self.W, agent_id="a2", agent_type="Explore"), "deny")    # helper in the scribe
        self.route("expect", "Rnd-p0-01", "--role", "rnd-p0", "--desk", "d-r0", sid="s-researcher")
        self.assertEqual(self.decision("s-r0", self.W), "ask")            # a session the claims do not know
        self.assertEqual(self.bash("s-r0b", self.W, desk="d-r0")[1].get("permissionDecision"), "deny")     # promoted, not the scribe

    def test_agent_tool_in_the_pool(self):
        _, out = self.guard("s-alg2", "Agent", {"subagent_type": "pmo-scribe", "prompt": "登记请求"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("s-alg1", "Agent", {"subagent_type": "alg-p0", "prompt": "会话名：Alg-p0-2101"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        self.assertIn("发消息", out.get("additionalContext", ""))
        _, out = self.guard("s-alg1", "Agent", {"subagent_type": "Explore", "prompt": "找找", "name": "x"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("s-alg1", "Agent", {"subagent_type": "Explore", "prompt": "找找"})
        self.assertNotIn("permissionDecision", out)

    def test_claim_commands_must_be_plain(self):
        R = self.R
        for cmd in ("FOO=1 python3 -I %s claim alg-p1 --as Alg-p1-01" % R,
                    "python3 -I %s claim alg-p1 --as Alg-p1-01 | cat" % R,
                    "env python3 -I %s expect Alg-p0-02 --role alg-p0 --desk d" % R,
                    "python3 %s claim alg-p1" % R):
            self.assertEqual(self.decision("s-alg1", cmd), "deny", cmd)
        self.assertEqual(self.decision("s-alg1", "python3 -I %s claim alg-p1 --as Alg-p1-01" % R, agent_id="a", agent_type="Explore"), "deny")

    def test_state_and_config_are_not_edited(self):
        for rel in (".claude/state/pool.json", ".claude/hooks/guard.py", ".claude/settings.json", ".claude/agents/alg-p1.md"):
            _, out = self.guard("s-alg2", "Edit", {"file_path": os.path.join(self.tmp, rel)})
            self.assertEqual(out.get("permissionDecision"), "deny", rel)
        _, out = self.guard("s-alg2", "Edit", {"file_path": os.path.join(self.tmp, ".claude", "agent-memory", "alg-p2", "m.md")})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_no_branch_switching_in_the_shared_directory(self):
        for cmd in ("git checkout -b x", "git stash", "cd repo && git -C . reset --hard", "git switch main"):
            self.assertEqual(self.decision("s-alg1", cmd), "deny", cmd)
        self.assertEqual(self.decision("s-researcher", "git stash"), "ask")
        self.assertIsNone(self.decision("s-alg1", "git status && git log -1"))

    def test_soft_rules_remind_and_log(self):
        _, out = self.guard("s-alg1", "Write", {"file_path": os.path.join(self.tmp, "packages", "drafts", "quality", "x.md")})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        self.assertIn("写入范围", out.get("additionalContext", ""))
        with open(os.path.join(self.tmp, ".claude", "logs", "guard.jsonl"), encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual((last["session_name"], last["origin"]), ("Alg-p1-01", "claim"))
        self.assertTrue(last["soft_hits"])
        _, out = self.guard("s-alg0", "Write", {"file_path": os.path.join(self.tmp, "packages", "drafts", "quality", "x.md")})
        self.assertIn("Alg-p1-01", out.get("additionalContext", ""))     # two sessions on one file within 30 minutes

    def test_evaluation_protocol_stays_read_only(self):
        path = os.path.join(self.tmp, "repo", "eval", "score.py")
        self.assertEqual(self.guard("s-alg1", "Edit", {"file_path": path})[1].get("permissionDecision"), "deny")
        self.assertEqual(self.guard("s-q1", "Edit", {"file_path": path})[1].get("permissionDecision"), "ask")
        self.assertEqual(self.guard("s-q1", "Edit", {"file_path": path}, permission_mode="bypassPermissions")[1].get("permissionDecision"), "deny")

    def test_a_broken_claims_file_denies_only_the_hard_rules(self):
        with open(os.path.join(self.tmp, ".claude", "state", "pool.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        code, out = self.bash("s-scribe", self.W)
        self.assertEqual((code, out.get("permissionDecision")), (0, "deny"))
        code, out = self.guard("s-alg1", "Edit", {"file_path": os.path.join(self.tmp, "runs", "alg", "n.md")})
        self.assertEqual(code, 0)
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_a_question_in_a_turn_a_message_started(self):
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 packages/drafts/alg/x.md")
        _, out = self.guard("s-alg1", "AskUserQuestion", {"questions": []})
        self.assertEqual(out.get("permissionDecision"), "deny")                       # nobody is there to answer
        self.assertIn("交给上一级的交付包", out.get("permissionDecisionReason", ""))   # a +1 goes up the chain
        self.prompt("s-alg2", "【委托 #2105】PMO-p2-01 → Alg-p2-01：需求包 x.md")
        _, out = self.guard("s-alg2", "AskUserQuestion", {"questions": []})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("【需拍板】", out.get("permissionDecisionReason", ""))          # a +2 asks the entry
        self.prompt("s-alg1", "研究员：先停一下")
        _, out = self.guard("s-alg1", "AskUserQuestion", {"questions": []})
        self.assertEqual(out, {})


class AskAndIdentityFlagTest(ProjectCase):
    """A session nobody claimed is the researcher's: asked before a ledger write. lark-cli calls get the identity flag."""

    def test_researcher_session_asks_and_bypass_denies(self):
        cmd = {"command": "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"}
        _, out = self.guard("", "Bash", cmd, agent_id=None)
        self.assertEqual(out.get("permissionDecision"), "ask")
        data = {"tool_name": "Bash", "tool_input": cmd, "cwd": self.tmp, "session_id": "s1", "permission_mode": "bypassPermissions"}
        _, out = self.hook("guard.py", data)
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("终端", out.get("permissionDecisionReason", ""))

    def test_reads_without_the_identity_flag_are_reminded(self):
        _, out = self.guard("alg-p2", "Bash", {"command": "lark-cli docs +fetch --doc https://x.feishu.cn/wiki/A"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        self.assertIn("--as user", out.get("additionalContext", ""))
        _, out = self.scribe("Bash", {"command": "lark-cli docs +create --title t --markdown x"})
        self.assertEqual(out.get("permissionDecision"), "allow")
        self.assertTrue(out.get("updatedInput", {}).get("command", "").endswith("--as user"))
        _, out = self.scribe("Bash", {"command": "lark-cli docs +create --title t --markdown x --as bot"})
        self.assertEqual(out.get("permissionDecision"), "deny")


class PoolHookTest(PoolProjectCase):
    def setUp(self):
        super().setUp()
        self.started_pool()

    def test_session_start_texts(self):
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-mine", "source": "startup"})
        self.assertIn("不要自己认领", out.get("additionalContext", ""))
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "compact"})
        text = out.get("additionalContext", "")
        self.assertIn("本会话是 Alg-p1-01", text)
        self.assertIn("未了事项", text)
        manifest = os.path.join(self.tmp, ".claude", "solo-lab", "manifest.json")
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
        m["plugin_version"] = "9.9.9"
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(m, f)
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "resume"})
        self.assertIn("规则已更新", out.get("additionalContext", ""))

    def test_status_follows_the_events(self):
        sid = "s-alg1"
        steps = [
            ({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, "instances.py", "waiting_researcher"),
            ({"hook_event_name": "StopFailure", "error": "rate_limit"}, "instances.py", "failed:rate_limit"),
            ({"hook_event_name": "Stop", "background_tasks": [{"id": "t1"}], "last_assistant_message": "跑着"}, "instances.py", "idle_bg"),
            ({"hook_event_name": "Stop", "background_tasks": [], "last_assistant_message": "好了"}, "instances.py", "idle"),
            ({"hook_event_name": "Notification", "notification_type": "permission_prompt"}, "instances.py", "waiting_researcher"),
            ({"hook_event_name": "UserPromptSubmit", "prompt": "继续", "permission_mode": "auto"}, "inbox.py", "busy"),
            ({"hook_event_name": "SessionEnd", "reason": "other"}, "instances.py", "offline"),
        ]
        for data, script, status in steps:
            self.hook(script, dict(data, session_id=sid))
            self.assertEqual(self.session(sid)["status"], status, data)
        self.assertEqual(self.session(sid)["permission_mode"], "auto")
        self.hook("instances.py", {"hook_event_name": "PermissionDenied", "session_id": sid, "reason": "x"})
        self.assertEqual(self.session(sid)["denied"], 1)
        self.assertIn("Alg-p1-01", self.pool()["claims"])                  # offline keeps the claim

    def test_messages_are_recorded_and_checked(self):
        _, out = self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 packages/drafts/alg/x.md")
        self.assertIn("没有登记回执", out.get("additionalContext", ""))
        self.assertEqual(self.pool()["owners"]["2101"], "Alg-p1-01")
        events = [json.loads(x) for x in self.read(".claude/logs/dispatch.jsonl").splitlines()]
        msg = [e for e in events if e.get("event") == "message"][-1]
        self.assertEqual((msg["kind"], msg["id"], msg["from"], msg["to"], msg["seen_by"]), ("assign", "2101", "Alg-p2-01", "Alg-p1-01", "Alg-p1-01"))
        with open(os.path.join(self.tmp, "packages", "sent", "2102.json"), "w", encoding="utf-8") as f:
            json.dump({"work_item_id": "2102"}, f)
        _, out = self.prompt("s-alg1", "【委托 #2102】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.assertNotIn("没有登记回执", out.get("additionalContext", ""))
        _, out = self.prompt("s-alg0", "【委托 #2102】Alg-p1-01 → Alg-p0-01：需求包 x.md")
        self.assertIn("会话名：", out.get("additionalContext", ""))
        _, out = self.prompt("s-alg0", "【委托 #2102】Alg-p1-01 → Alg-p0-01：需求包 x.md\n会话名：Alg-p0-2102")
        self.assertNotIn("会话名：", out.get("additionalContext", "").split("你是 ")[0])

    def test_the_dispatch_graph_is_a_reminder(self):
        _, out = self.prompt("s-q2", "【委托 #2103】Alg-p1-01 → Quality-p2-01：送审包 x.md")
        self.assertIn("派发图", out.get("additionalContext", ""))
        for sid, text in (("s-alg1", "【交付 #2102】Alg-p0-01 → Alg-p1-01：交付包 y.md"),
                          ("s-scribe", "【登记请求】Alg-p0-01 → PMO-p0-01：草稿 x.md"),
                          ("s-entry", "【送审 #2102】Alg-p0-01 → PMO-p2-01：送审包 z.md")):
            _, out = self.prompt(sid, text)
            self.assertNotIn("派发图", out.get("additionalContext", ""), text)
        self.prompt("s-alg1", "【委托 #2104】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        _, out = self.prompt("s-alg2", "【交付 #2104】Alg-p0-01 → Alg-p2-01：交付包 y.md")
        self.assertIn("负责会话是 Alg-p1-01", out.get("additionalContext", ""))

    def test_compaction_summary_is_kept(self):
        self.hook("instances.py", {"hook_event_name": "PostCompact", "session_id": "s-alg2", "trigger": "auto",
                                   "compact_summary": "在等 #2101 的交付"})
        rec = json.loads(self.read(".claude/logs/compact/Alg-p2-01.jsonl").splitlines()[-1])
        self.assertEqual(rec["summary"], "在等 #2101 的交付")

    def test_open_items_are_injected_after_compaction(self):
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg2", "source": "compact"})
        self.assertIn("#2101→Alg-p1-01", out.get("additionalContext", ""))
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "compact"})
        self.assertIn("#2101（交付给 Alg-p2-01）", out.get("additionalContext", ""))
        self.prompt("s-alg2", "【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg2", "source": "compact"})
        self.assertIn("未了事项：无", out.get("additionalContext", ""))


class PoolInboxTest(PoolProjectCase):
    def setUp(self):
        super().setUp()
        self.started_pool()

    def test_each_session_reads_its_own_inbox(self):
        self.assertEqual(self.route("notify", "alg-p1-01", "--text", "规格 v3 已签字", sid="s-entry")[0], 0)
        self.route("notify", "Quality-p1-01", "--text", "给质量的", sid="s-entry")
        _, out = self.prompt("s-q1", "继续")
        self.assertIn("给质量的", out.get("additionalContext", ""))
        self.assertNotIn("规格 v3", out.get("additionalContext", ""))
        _, out = self.prompt("s-alg1", "继续")
        self.assertIn("规格 v3 已签字", out.get("additionalContext", ""))
        self.assertIn("来自 PMO-p2-01", out.get("additionalContext", ""))
        _, out = self.prompt("s-alg1", "继续")
        self.assertNotIn("规格 v3 已签字", out.get("additionalContext", ""))
        route.append_inbox(self.tmp, "feishu", {"id": "m1", "ts": "2026-11-03T08:00:00+08:00", "from": "研究员（飞书私聊）",
                                                "text": "今天先看评测", "read": False})
        _, out = self.prompt("s-mine", "随便聊聊")
        self.assertEqual(out, {})                                          # the researcher's own session reads nothing
        _, out = self.prompt("s-alg2", "继续")
        self.assertNotIn("今天先看评测", out.get("additionalContext", ""))
        _, out = self.prompt("s-entry", "早")
        self.assertIn("今天先看评测", out.get("additionalContext", ""))


class PoolRouteTest(PoolProjectCase):
    def setUp(self):
        super().setUp()
        self.started_pool()

    def set_status(self, sid, status, minutes_ago=0):
        path = os.path.join(self.tmp, ".claude", "state", "sessions", sid + ".json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        at = (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        st.update(status=status, status_at=at, last_tool_at=at)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def test_pool_suggests_whom_to_send_to(self):
        self.set_status("s-alg1", "idle")
        code, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertEqual(code, 0)
        self.assertIn("建议：发给 Alg-p1-01", text)
        self.assertIn("d-s-alg1", text)
        self.set_status("s-alg1", "busy")
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("还没到上限 3", text)
        self.set_status("s-alg1", "busy", minutes_ago=20)
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("不确定", text)
        self.set_status("s-scribe", "busy")
        _, text = self.route("pool", "--role", "pmo-scribe", sid="s-alg2")
        self.assertIn("已到上限 1", text)

    def test_owner_first_and_focus_skipped(self):
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.set_status("s-alg1", "idle")
        _, text = self.route("pool", "--role", "alg-p1", "--item", "2101", sid="s-alg2")
        self.assertIn("它是 #2101 的负责会话", text)
        self.assertEqual(self.route("focus", "open", "--topic", "谈规格", sid="s-alg1")[0], 0)
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("研究员在用", text)
        self.assertNotIn("建议：发给", text)
        self.route("focus", "close", sid="s-alg1")
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("建议：发给 Alg-p1-01", text)

    def test_mixed_permission_modes_are_flagged(self):
        self.prompt("s-alg1", "继续", permission_mode="auto")
        self.prompt("s-alg0", "继续", permission_mode="auto")
        self.prompt("s-alg2", "继续", permission_mode="bypassPermissions")
        _, text = self.route("pool", sid="s-entry")
        self.assertIn("Alg-p2-01 的权限模式和池里多数会话不是一类", text)

    def test_queue_recover_watch_refill_audit(self):
        self.set_status("s-alg1", "idle")
        code, text = self.route("--offline", "pool", "--queue", "Alg-p2-01", sid="s-alg2")
        self.assertEqual(code, 0)
        self.assertIn("Alg-p1-01", text)
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.set_status("s-alg0", "busy")
        code, text = self.route("pool", "--recover", sid="s-entry")
        self.assertIn("给 Alg-p2-01 发消息【通知 #2101】", text)
        self.assertIn("Alg-p0-01：在忙", text)
        code, text = self.route("pool", "--watch", "--once", sid="s-entry")
        self.assertEqual(code, 0)
        self.assertIn("Alg-p1-01", text)
        self.assertEqual(self.route("refill", "alg-p0", "--count", "2", sid="s-alg2")[0], 0)
        self.assertEqual(self.pool()["refills"][-1]["role"], "alg-p0")
        self.assertIn("补位请求", self.read("inbox/PMO-p2-01.jsonl"))
        self.guard("s-alg1", "Write", {"file_path": os.path.join(self.tmp, "packages", "drafts", "quality", "x.md")})
        code, text = self.route("audit", sid="s-entry")
        self.assertIn("- Alg-p1-01：", text.split("软规则命中", 1)[1].split("其他 agent 会话叫醒的轮次", 1)[0])

    def test_session_names_in_classify(self):
        sys.path.insert(0, os.path.join(self.tmp, ".claude", "solo-lab", "bin"))
        out = route.classify_text(self.spec, self.tmp, "alg-p1-02 那边的结果出来了吗", True)
        self.assertEqual((out["row"], out.get("role")), ("R1", "alg-p1"))
        out = route.classify_text(self.spec, self.tmp, "Alg-p0-01 在干嘛", True)
        self.assertNotIn("work_item", out)
        out = route.classify_text(self.spec, self.tmp, "Alg-p0-2101 跑完了", True)
        self.assertEqual(out.get("work_item"), "2101")


class LedgerLockTest(ProjectCase):
    def test_a_held_lock_stops_another_write(self):
        import fcntl
        path = os.path.join(self.tmp, "feishu", ".ledger.lock")
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            old = ledger.LOCK_WAIT
            ledger.LOCK_WAIT = 0
            try:
                with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmp}):
                    code, out = self.run_quiet(ledger.main, ["comment", "2101", "--text", "x", "--apply"])
            finally:
                ledger.LOCK_WAIT = old
                fcntl.flock(f, fcntl.LOCK_UN)
        self.assertEqual(code, 1)
        self.assertIn("台账正在被别的写入占用", out)

    def test_previews_take_no_lock(self):
        with mock.patch.object(ledger, "write_lock", side_effect=AssertionError("locked")):
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmp}):
                code, _ = self.run_quiet(ledger.main, ["--offline", "comment", "2101", "--text", "x"])
        self.assertEqual(code, 0)

    def test_ids_are_replaced_whole(self):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with mock.patch.object(ledger.os, "replace", wraps=os.replace) as rep:
            ledger.save_json(path, {"schema": "x"})
        rep.assert_called_once()
        self.assertEqual(json.loads(open(path, encoding="utf-8").read()), {"schema": "x"})
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], [])


class DispatchGraphTest(unittest.TestCase):
    def spec_with(self, change):
        spec = orgspec.OrgSpec()
        change(spec.rules, spec.terms)
        return spec

    def test_validate_catches_graph_mistakes(self):
        spec = self.spec_with(lambda r, t: r["dispatch"]["assign"].append({"from": "p2", "to": "alg-p9"}))
        self.assertTrue(any("alg-p9" in p for p in spec.validate()))
        spec = self.spec_with(lambda r, t: t["msg"].pop("notice"))
        self.assertTrue(any("notice" in p for p in spec.validate()))
        spec = self.spec_with(lambda r, t: r["dispatch"]["kinds"]["decide"].update(to="pmo-p9"))
        self.assertTrue(any("pmo-p9" in p for p in spec.validate()))

    def test_header_regex(self):
        rx = orgspec.OrgSpec().message_header_re()
        self.assertEqual(rx.search("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包").groups(), ("委托", "2101", "Alg-p2-01", "Alg-p1-01"))
        self.assertEqual(rx.search("【交付 #2101（重发，上一条作废）】Alg-p1-01 -> Alg-p2-01").group(1, 2), ("交付", "2101"))
        self.assertIsNone(rx.search("【备注】随便写写"))


class PoolReviewIdentityTest(PoolProjectCase):
    """Fixes from the adversarial review of 0.5.0: identity and hard rules."""
    W = "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"

    def setUp(self):
        super().setUp()
        self.started_pool()

    def decision(self, sid, command, desk=None, **extra):
        return self.bash(sid, command, desk=desk, **extra)[1].get("permissionDecision")

    def test_a_process_with_the_scribes_desktop_id_is_not_the_scribe(self):
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-nested", "source": "startup"},
                           desk="d-scribe")
        self.assertIn("共用一个桌面会话 ID", out.get("additionalContext", ""))
        self.assertEqual(self.decision("s-nested", self.W, desk="d-scribe"), "deny")
        self.assertEqual(self.pool()["claims"]["PMO-p0-01"]["session_id"], "s-scribe")
        self.route("notify", "PMO-p0-01", "--text", "登记请求：x", sid="s-entry")
        _, out = self.prompt("s-nested", "继续", desk="d-scribe")
        self.assertNotIn("登记请求：x", out.get("additionalContext", ""))
        self.assertEqual(self.decision("s-scribe", self.W), "allow")
        # once the scribe's own session has ended, a /clear moves the claim to the new session
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-scribe", "reason": "clear"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-scribe-2", "source": "clear"}, desk="d-scribe")
        self.assertEqual(self.pool()["claims"]["PMO-p0-01"]["session_id"], "s-scribe-2")

    def test_monitor_commands_are_checked_like_bash(self):
        _, out = self.guard("s-alg1", "Monitor", {"command": self.W, "description": "x", "timeout_ms": 60000})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("s-alg1", "Monitor", {"command": "tail -f runs/alg/log.txt", "description": "x"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_claim_commands_are_found_by_parsing(self):
        R = self.R
        for cmd in ("python3 -I %s --offline release PMO-p0-01" % R, "bash -c 'python3 -I %s release PMO-p0-01'" % R):
            self.assertEqual(self.decision("s-researcher", cmd), "deny", cmd)
        self.assertEqual(self.decision("s-researcher", "python3 -I %s 'release' PMO-p0-01" % R), "ask")   # same as the plain form
        self.assertEqual(self.decision("s-alg1", "python3 -I %s 'release' PMO-p0-01" % R), "deny")
        self.assertEqual(self.decision("s-alg2", "python3 -I %s --offline expect PMO-p0-02 --role pmo-scribe --session s-alg1" % R), "deny")
        for cmd in ('grep -n "route.py claim" CLAUDE.md', "echo '研究员请运行 route.py release Alg-p0-01'",
                    "cat > packages/drafts/alg/n.md <<'EOF'\npython3 -I %s expect Alg-p0-02 --role alg-p0 --desk d\nEOF" % R):
            self.assertIsNone(self.decision("s-alg2", cmd), cmd)

    def test_a_cd_to_this_project_in_front_of_a_claim_is_the_same_command(self):
        self.route("expect", "RnD-p2-01", "--role", "rnd-p2", "--desk", "d-rnd", sid="s-researcher")
        plain = "python3 -I %s claim rnd-p2 --as RnD-p2-01" % self.R
        for front in ('cd "%s" && ' % self.tmp, "cd '%s' && " % self.tmp, "cd %s && " % self.tmp, 'cd "%s/" &&  ' % self.tmp):
            self.assertIsNone(self.decision("s-term", front + plain), front)
        for cmd in ('cd "%s" ; %s' % (self.tmp, plain), "cd /tmp && " + plain, 'cd "%s" && %s | tail -3' % (self.tmp, plain),
                    'cd "%s" && cd /tmp && %s' % (self.tmp, plain), 'X=1 cd "%s" && %s' % (self.tmp, plain),
                    'cd "%s/.claude" && %s' % (self.tmp, plain), "cd . && " + plain):
            verdict = self.bash("s-term", cmd)[1]
            self.assertEqual(verdict.get("permissionDecision"), "deny", cmd)
            self.assertIn("前面最多带一句 cd 到本项目目录", verdict.get("permissionDecisionReason", ""), cmd)
        plain = "python3 -I %s expect Alg-p0-02 --role alg-p0 --desk d-x" % self.R
        self.assertIsNone(self.decision("s-alg2", 'cd "%s" && %s' % (self.tmp, plain)))       # expect is read the same way

    def test_claim_with_an_asserted_desk_or_an_old_session_asks(self):
        self.route("expect", "RnD-p2-01", "--role", "rnd-p2", "--desk", "d-rnd", sid="s-researcher")
        cmd = "python3 -I %s claim rnd-p2 --as RnD-p2-01 --desk d-rnd" % self.R
        self.assertEqual(self.decision("s-term", cmd), "ask")              # no desktop ID in the hook to compare with
        cmd = "python3 -I %s claim rnd-p2 --as RnD-p2-01" % self.R
        self.assertIsNone(self.decision("s-term", cmd))                    # without --desk route.py uses the session's own IDs
        code, text = self.route("claim", "rnd-p2", "--as", "RnD-p2-01", "--wait", "0", sid="s-term")
        self.assertEqual(code, 1)                                          # and finds no permit for them
        self.assertNotIn("RnD-p2-01", self.pool()["claims"])
        code, text = self.route("claim", "rnd-p2", "--as", "RnD-p2-01", "--de", "d-rnd", "--wait", "0", sid="s-term")
        self.assertNotEqual(code, 0)                                       # no abbreviated options

    def test_a_stale_permit_can_be_replaced_or_released(self):
        self.route("release", "PMO-p0-01", sid="s-researcher")
        self.assertEqual(self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d-dead", sid="s-entry")[0], 0)
        code, text = self.route("expect", "PMO-p0-01", "--role", "pmo-scribe", "--desk", "d-new", sid="s-entry")
        self.assertEqual(code, 0, text)
        self.assertEqual([p["desk"] for p in self.pool()["permits"] if p["role"] == "pmo-scribe"], ["d-new"])
        code, text = self.route("release", "PMO-p0-01", sid="s-researcher")
        self.assertEqual(code, 0, text)
        self.assertIn("许可", text)
        self.assertEqual([p for p in self.pool()["permits"] if p["role"] == "pmo-scribe"], [])

    def test_git_commands_that_touch_the_working_tree(self):
        for cmd in ('bash -c "git checkout main"', "sh -c 'git stash'", "/usr/bin/git checkout main", "git -P stash",
                    "git --git-dir .git --work-tree . checkout main", "git pull --rebase origin main", "\\git stash"):
            self.assertEqual(self.decision("s-alg1", cmd), "deny", cmd)
        for cmd in ('git commit -m "docs: never git stash in the pool"', 'grep -n "git stash" docs/x.md', "git log -3",
                    "cat <<'EOF' > n.md\nDo not run git rebase here.\nEOF"):
            self.assertIsNone(self.decision("s-alg1", cmd), cmd)

    def test_a_git_prompt_does_not_hide_a_denied_feishu_command(self):
        cmd = "git stash && lark-cli docs +update --doc x --command overwrite --content y --yes --as user"
        self.assertEqual(self.decision("s-researcher", cmd), "deny")

    def test_two_scribes_stop_every_ledger_write(self):
        pl = self.pool()
        pl["claims"]["PMO-p0-02"] = dict(pl["claims"]["PMO-p0-01"], name="PMO-p0-02", session_id="s-x", desk="d-x")
        self.write_pool(pl)
        self.assertEqual(self.decision("s-researcher", self.W), "deny")

    def test_fixed_names_and_the_feishu_inbox(self):
        _, out = self.opening("pmo-p2", "PMO-p2-07", "s-other")
        self.assertIn("固定是 PMO-p2-01", out.get("additionalContext", ""))
        self.assertEqual(self.route("expect", "PMO-p0-09", "--role", "pmo-scribe", "--desk", "d9", sid="s-researcher")[0], 1)
        code, text = self.route("notify", "feishu", "--from", "研究员（飞书私聊）", "--text", "补位", sid="s-alg1")
        self.assertNotEqual(code, 0)
        self.route("notify", "PMO-p2-01", "--from", "研究员", "--text", "冒名", sid="s-alg1")
        msg = json.loads(self.read("inbox/PMO-p2-01.jsonl").splitlines()[-1])
        self.assertEqual(msg["from"], "Alg-p1-01")

    def test_odd_state_files_never_crash_the_guard(self):
        for bad in ({"version": 1, "claims": {}, "permits": None}, {"claims": {"X": "y"}}, {"claims": {}, "tombstones": {}}):
            self.write_pool(bad)
            code, out = self.bash("s-researcher", "ls")
            self.assertEqual(code, 0, bad)
            self.assertEqual(self.bash("s-scribe", self.W)[1].get("permissionDecision"), "deny", bad)
        with open(os.path.join(self.tmp, ".claude", "state", "edits.json"), "w", encoding="utf-8") as f:
            json.dump([{"who": "x"}], f)
        code, _ = self.guard("s-alg1", "Write", {"file_path": os.path.join(self.tmp, "runs", "alg", "n.md")})
        self.assertEqual(code, 0)

    def test_session_start_texts_for_odd_origins(self):
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-term2", "source": "startup",
                                            "agent_type": "alg-p2"})
        self.assertIn("claude --agent alg-p2", out.get("additionalContext", ""))
        with open(os.path.join(self.tmp, ".claude", "state", "pool.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "compact"})
        self.assertIn("读不出来", out.get("additionalContext", ""))
        self.assertNotIn("研究员自己的会话", out.get("additionalContext", ""))

    def test_a_refused_permit_is_dropped(self):
        self.route("expect", "RnD-p2-01", "--role", "rnd-p2", "--desk", "d-a", sid="s-researcher")
        self.opening("rnd-p2", "RnD-p2-01", "s-b")                        # the researcher's own opening line wins
        self.bash("s-a", "ls", desk="d-a")
        self.bash("s-a", "ls", desk="d-a")
        refused = [e for e in map(json.loads, self.read(".claude/logs/dispatch.jsonl").splitlines()) if e.get("event") == "claim_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual([p for p in self.pool()["permits"] if p["role"] == "rnd-p2"], [])

    @unittest.skipUnless(sys.platform == "darwin", "case folding only on macOS")
    def test_a_path_spelled_in_another_case_is_the_same_file(self):
        os.makedirs(os.path.join(self.tmp, "repo", "eval"), exist_ok=True)
        with open(os.path.join(self.tmp, "repo", "eval", "score.py"), "w") as f:
            f.write("x")
        _, out = self.guard("s-alg1", "Edit", {"file_path": os.path.join(self.tmp, "repo", "EVAL", "score.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("s-infra1" if False else "s-alg1", "Edit", {"file_path": os.path.join(self.tmp, ".claude", "State", "pool.json")})
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_policy_of_another_version_fails_closed(self):
        path = os.path.join(self.hooks, "policy.json")
        with open(path, encoding="utf-8") as f:
            policy = json.load(f)
        policy["version"] = 3
        with open(path, "w", encoding="utf-8") as f:
            json.dump(policy, f)
        code, _ = self.bash("s-researcher", "ls")
        self.assertEqual(code, 2)


class PoolReviewStateTest(PoolProjectCase):
    """Fixes from the adversarial review of 0.5.0: status, messages, inboxes, route.py."""

    def setUp(self):
        super().setUp()
        self.started_pool()

    def set_status(self, sid, **fields):
        path = os.path.join(self.tmp, ".claude", "state", "sessions", sid + ".json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st.update(fields)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def ago(self, minutes):
        return (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def test_a_quoted_header_is_not_a_message(self):
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.prompt("s-alg2", "【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md")
        self.prompt("s-alg2", "你上周收到的【委托 #2101】Alg-p2-01 → Alg-p1-01 那件事，结论再讲一遍。")
        self.prompt("s-entry", "这条“【委托 #2101】Alg-p1-01 → Alg-p0-01：需求包”对方收到了吗？")
        self.assertEqual(self.pool()["owners"]["2101"], "Alg-p1-01")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg2", "source": "compact"})
        self.assertIn("未了事项：无", out.get("additionalContext", ""))
        self.assertEqual(self.session("s-alg2")["turn_origin"], "researcher")

    def test_a_message_addressed_to_another_session_takes_no_lease(self):
        _, out = self.prompt("s-alg2", "【委托 #2103】PMO-p2-01 → Infra-p2-01：需求包 x.md")
        self.assertIn("不是本会话", out.get("additionalContext", ""))
        self.assertNotIn("2103", self.pool()["owners"])

    def test_a_late_report_from_a_replaced_session_keeps_the_open_item(self):
        self.route("expect", "Alg-p1-02", "--role", "alg-p1", "--desk", "d-a12", sid="s-alg2")
        self.claim("alg-p1", "Alg-p1-02", "s-alg12", desk="d-a12")
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.prompt("s-alg12", "【委托 #2101】Alg-p2-01 → Alg-p1-02：需求包 x.md（重新委托）")
        self.prompt("s-alg2", "【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg2", "source": "compact"})
        self.assertIn("#2101→Alg-p1-02", out.get("additionalContext", ""))

    def test_a_report_passed_up_is_not_taken_for_a_late_one(self):
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")       # Alg-p1-01 owns #2101
        _, out = self.prompt("s-entry", "【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md")   # its +2 passes the result up
        self.assertNotIn("负责会话是", out.get("additionalContext", ""))
        self.route("expect", "Alg-p1-02", "--role", "alg-p1", "--desk", "d-a12", sid="s-alg2")
        self.claim("alg-p1", "Alg-p1-02", "s-alg12", desk="d-a12")
        self.prompt("s-alg12", "【委托 #2101】Alg-p2-01 → Alg-p1-02：需求包 x.md（重新委托）")
        _, out = self.prompt("s-alg2", "【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md")    # the session it was taken from
        self.assertIn("#2101 的负责会话是 Alg-p1-02，这条交付是 Alg-p1-01 发的", out.get("additionalContext", ""))

    def test_p0_work_names_stand_for_their_claim(self):
        self.prompt("s-alg0", "【委托 #2103】Alg-p1-01 → Alg-p0-2103：需求包 x.md\n会话名：Alg-p0-2103")
        self.assertEqual(self.pool()["owners"]["2103"], "Alg-p0-01")
        _, out = self.prompt("s-alg1", "【交付 #2103】Alg-p0-2103 → Alg-p1-01：交付包 y.md")
        ctx = out.get("additionalContext", "")
        self.assertNotIn("不在会话池的认领表里", ctx)
        self.assertNotIn("负责会话是", ctx)
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg0", "source": "compact"})
        self.assertIn("未了事项：无", out.get("additionalContext", ""))
        self.route("notify", "Alg-p0-2103", "--text", "补充：数据换成 v2", sid="s-entry")
        _, out = self.prompt("s-alg0", "继续")
        self.assertIn("数据换成 v2", out.get("additionalContext", ""))

    def test_notify_to_a_role_with_several_sessions(self):
        self.route("expect", "Alg-p1-02", "--role", "alg-p1", "--desk", "d-a12", sid="s-alg2")
        self.claim("alg-p1", "Alg-p1-02", "s-alg12", desk="d-a12")
        code, text = self.route("notify", "alg-p1", "--text", "x", sid="s-entry")
        self.assertNotEqual(code, 0)
        self.assertIn("Alg-p1-01", text)
        self.assertEqual(self.route("notify", "quality-p1", "--text", "y", sid="s-entry")[0], 0)   # one claim: it
        _, out = self.prompt("s-q1", "继续")
        self.assertIn("y", out.get("additionalContext", ""))

    def test_status_after_a_prompt_a_background_call_and_a_compaction(self):
        self.hook("instances.py", {"hook_event_name": "PermissionRequest", "session_id": "s-alg1", "tool_name": "Bash", "tool_use_id": "t1"})
        self.assertEqual(self.session("s-alg1")["status"], "waiting_researcher")
        self.hook("guard.py", {"hook_event_name": "PreToolUse", "session_id": "s-alg1", "tool_name": "Bash",
                               "tool_use_id": "t2", "tool_input": {"command": "ls"}})
        self.assertEqual(self.session("s-alg1")["status"], "busy")
        self.hook("instances.py", {"hook_event_name": "PermissionRequest", "session_id": "s-alg1", "tool_name": "Bash", "tool_use_id": "t3"})
        self.hook("audit.py", {"hook_event_name": "PostToolUse", "session_id": "s-alg1", "tool_name": "Bash", "tool_use_id": "t3",
                               "tool_input": {"command": "ls"}, "tool_response": {"stdout": ""}})
        self.assertEqual(self.session("s-alg1")["status"], "busy")
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-alg1", "background_tasks": [{"id": "b"}]})
        self.hook("guard.py", {"hook_event_name": "PreToolUse", "session_id": "s-alg1", "agent_id": "bg", "agent_type": "Explore",
                               "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(self.session("s-alg1")["status"], "idle_bg")
        self.prompt("s-alg1", "继续")
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "compact"})
        self.assertEqual(self.session("s-alg1")["status"], "busy")

    def test_a_turn_that_just_started_is_not_uncertain(self):
        self.set_status("s-alg1", status="idle", last_tool_at=self.ago(60), status_at=self.ago(60))
        self.prompt("s-alg1", "【通知】PMO-p2-01 → Alg-p1-01：看一下")
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("在忙", text)
        self.assertNotIn("不确定", text)

    def test_recover_lists_quota_and_status_before_the_end(self):
        self.prompt("s-alg0", "继续")
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-alg0", "reason": "other"})
        self.hook("instances.py", {"hook_event_name": "Notification", "session_id": "s-q1", "notification_type": "quota_auto_resume_stale"})
        _, text = self.route("pool", "--recover", sid="s-entry")
        self.assertIn("Alg-p0-01：应用退出前在忙", text)
        self.assertIn("Quality-p1-01：额度暂停过", text)

    def test_injection_and_opening_context_stay_within_budget(self):
        for i in range(400):
            self.prompt("s-alg2", "【委托 #%d】PMO-p2-01 → Alg-p2-01：需求包 x.md" % (3000 + i))
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-entry", "source": "compact"})
        text = out.get("additionalContext", "")
        self.assertLessEqual(len(text), 9500)
        self.assertIn("另有", text)
        self.assertIn("## 怎么干活", text)
        for i in range(20):
            route.append_inbox(self.tmp, "feishu", {"id": "m%d" % i, "ts": "2026-11-03T08:00:00+08:00", "from": "研究员（飞书私聊）",
                                                    "text": "长消息" * 100, "read": False})
        self.route("release", "PMO-p2-01", sid="s-researcher")
        _, out = self.opening("pmo-p2", "PMO-p2-01", "s-entry-new")
        self.assertLessEqual(len(out.get("additionalContext", "")), 9600)
        self.assertIn("还有 20 条消息没看", out.get("additionalContext", ""))
        _, out = self.prompt("s-entry-new", "早")
        self.assertIn("长消息", out.get("additionalContext", ""))

    def test_a_malformed_inbox_line_loses_nothing(self):
        path = os.path.join(self.tmp, "inbox", "pmo-p2.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps("补位请求：随手写的一行") + "\n")
        self.route("notify", "PMO-p2-01", "--text", "真正的留言", sid="s-alg2")
        _, out = self.prompt("s-entry", "早")
        self.assertIn("真正的留言", out.get("additionalContext", ""))

    def test_focus_flag_lapses_for_good(self):
        self.route("focus", "open", "--topic", "直接问路由", sid="s-router" if False else "s-alg1")
        self.set_status("s-alg1", status="idle", status_at=self.ago(60), last_tool_at=self.ago(60))
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("建议：发给 Alg-p1-01", text)
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-alg1"})
        _, text = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertNotIn("研究员在用", text)
        self.assertNotIn("Alg-p1-01", self.pool()["focus"])

    def test_refills_are_consumed_by_new_claims(self):
        self.route("refill", "alg-p0", sid="s-alg2")
        self.route("expect", "Alg-p0-02", "--role", "alg-p0", "--desk", "d-a02", sid="s-alg2")
        self.claim("alg-p0", "Alg-p0-02", "s-a02", desk="d-a02")
        _, text = self.route("pool", "--watch", "--once", sid="s-entry")
        self.assertNotIn("待补位", text)

    def test_the_cap_and_the_refill_rows(self):
        code, text = self.route("claim", "alg-p2", "--as", "Alg-p2-01", sid="s-alg2")
        self.assertIn("补位用", text)
        self.assertIn("Alg-p1-02", text)
        with open(self.task_path, encoding="utf-8") as f:
            task = json.load(f)
        task["pool"].update(max_sessions=15)
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        with self.assertRaises(setup.SetupError):
            setup.build_plan(self.tmp, self.task_path, None)
        policy_path = os.path.join(self.hooks, "policy.json")
        with open(policy_path, encoding="utf-8") as f:
            policy = json.load(f)
        policy["pool"]["max_sessions"] = len(self.pool()["claims"])
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump(policy, f)
        code, text = self.route("expect", "Alg-p0-02", "--role", "alg-p0", "--desk", "d-a02", sid="s-alg2")
        self.assertEqual(code, 1)
        self.assertIn("max_sessions", text)

    def test_an_org_subagent_in_the_pool_has_no_pool_name(self):
        _, out = self.hook("instances.py", {"hook_event_name": "SubagentStart", "session_id": "s-alg2", "agent_id": "a9",
                                            "agent_type": "alg-p1"})
        self.assertIn("不是会话池里的会话", out.get("additionalContext", ""))

    def test_queue_lists_only_what_the_caller_dispatches(self):
        rows = {"open": [{"work_item_id": "2201", "work_item_status": "待开工", "name": "需求包（Alg-p1-01）：跑消融 A"},
                         {"work_item_id": "2202", "work_item_status": "待开工", "name": "需求包（Alg-p2-01）：阶段二"}],
                "cross": [{"work_item_id": "2301", "work_item_status": "待接单", "name": "需求包（Alg-p2-01）：要数据"}]}
        names = {"waiting": "待接单", "queued": "待开工"}
        with mock.patch.object(ledger, "Ledger") as bound, \
                mock.patch.object(ledger, "query", side_effect=lambda lg, name, seq=None, as_json=False: rows[name]):
            bound.return_value.state.side_effect = lambda wid, slot: names[slot]
            mine = route.queue_rows(self.tmp, json.loads(self.read(".claude/hooks/policy.json")),
                                    self.pool()["claims"]["Alg-p2-01"], "alg")
            entry = route.queue_rows(self.tmp, json.loads(self.read(".claude/hooks/policy.json")),
                                     self.pool()["claims"]["PMO-p2-01"], "pmo")
        self.assertEqual([r["work_item_id"] for r in mine], ["2202"])
        self.assertEqual([r["work_item_id"] for r in entry], ["2301"])


class ReviewTextTest(PoolProjectCase):
    """Fixes from the adversarial review of 0.5.0: generated text."""

    def test_pool_role_texts(self):
        p1, p0, q0 = (self.read(".claude/agents/%s.md" % r) for r in ("alg-p1", "alg-p0", "quality-p0"))
        for text in (p1, p0):
            self.assertNotIn("跨序列的需求包、送审包、决策包写成草稿发给入口", text)
            self.assertIn("写进交给上一级的交付包", text)
        self.assertIn("开工：否", p1)
        self.assertIn("消息抬头都用它", p0)
        self.assertNotIn("执行结果可以直接送审", q0)
        self.assertNotIn("研究员在本会话里研究员", self.read(".claude/agents/pmo-p2.md"))
        self.assertIn("会话池（研究员 2026-11-03 起采用）", self.read("org/decisions.md"))
        self.assertIn("用 Agent 工具派书记员", self.read("CLAUDE.md"))

    def test_check_counts_sessions_and_the_checklist_shows_rebuild_lines(self):
        self.started_pool()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertIn("会话池还没开齐", out.getvalue())
        self.route("release", "Alg-p2-01", sid="s-researcher")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.main(["pool", "--target", self.tmp, "--manual"])
        self.assertIn("重建句", out.getvalue())


class FirstReviewTest(ProjectCase):
    """Regressions found by the adversarial review of 0.5.0, outside the claim table."""

    def test_notify_refuses_a_name_that_is_no_role_or_session(self):
        with self.assertRaises(SystemExit):
            route.notify(self.spec, self.tmp, "Nope-p2-01", "x")

    def test_an_ask_keeps_the_identity_rewrite(self):
        _, out = self.guard("", "Bash", {"command": "lark-cli docs +create --title t --markdown x"}, agent_id=None)
        self.assertEqual(out.get("permissionDecision"), "ask")
        self.assertTrue(out.get("updatedInput", {}).get("command", "").endswith("--as user"))

    def test_guard_and_policy_are_updated_as_a_pair(self):
        with open(os.path.join(self.hooks, "guard.py"), "a", encoding="utf-8") as f:
            f.write("\n# local change\n")
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = {rel: kind for kind, rel, _ in setup.actions_for(self.tmp, plan)}
        self.assertEqual(acts[".claude/hooks/guard.py"], "new")
        self.assertIn(acts[".claude/hooks/policy.json"], ("new", "same"))


class PoolSecondReviewTest(PoolProjectCase):
    """Fixes from the second adversarial review of 0.5.0."""
    W = "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"

    def setUp(self):
        super().setUp()
        self.started_pool()

    def decision(self, sid, command, desk=None, **extra):
        return self.bash(sid, command, desk=desk, **extra)[1].get("permissionDecision")

    def test_a_resumed_nested_run_is_still_a_desk_conflict(self):
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-n1", "source": "startup"}, desk="d-scribe")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-n1", "source": "resume"}, desk="d-scribe")
        self.assertIn("共用一个桌面会话 ID", out.get("additionalContext", ""))
        self.assertEqual(self.decision("s-n1", self.W, desk="d-scribe"), "deny")
        self.assertEqual(self.pool()["claims"]["PMO-p0-01"]["session_id"], "s-scribe")
        # a same-ID run that ends marks the session offline, but that frees nothing
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-scribe", "reason": "other"})
        self.assertEqual(self.decision("s-other", self.W, desk="d-scribe"), "deny")
        self.assertEqual(self.pool()["claims"]["PMO-p0-01"]["session_id"], "s-scribe")

    def test_commands_hidden_in_substitutions_pipes_and_heredocs(self):
        for cmd in ('out="$(git stash 2>&1)"', "echo `git stash`", "bash <<'EOF'\ngit checkout main\nEOF",
                    'echo "git stash" | bash', 'bash <<< "git stash"', "find . -maxdepth 0 -exec git stash \\;",
                    "env -S 'git stash'", "tcsh -c 'git stash'", "xcrun git stash", "gh pr checkout 123"):
            self.assertEqual(self.decision("s-alg1", cmd), "deny", cmd)
        R = self.R
        for cmd in ('out="$(python3 -I %s release PMO-p0-01)"' % R, "python3 -X utf8 %s release PMO-p0-01" % R,
                    "python3 -W ignore -I %s release PMO-p0-01" % R):
            self.assertEqual(self.decision("s-researcher", cmd), "deny", cmd)

    def test_read_only_and_elsewhere_git_is_fine(self):
        for cmd in ("git stash list", "git stash show -p stash@{0}", "git checkout --help", "git clean -n",
                    "git -C /tmp/other-clone checkout v1.2", "cd /tmp/other-clone && git checkout v1.2"):
            self.assertIsNone(self.decision("s-alg1", cmd), cmd)

    def test_identity_variables_stay_put(self):
        cmd = "env -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_HOST_SESSION_ID python3 -I %s notify PMO-p0-01 --text x" % self.R
        self.assertEqual(self.decision("s-alg1", cmd), "deny")
        self.assertEqual(self.decision("s-alg1", "CLAUDE_CODE_SESSION_ID=s-scribe %s" % self.W), "deny")
        self.assertIsNone(self.decision("s-alg1", "echo $CLAUDE_CODE_SESSION_ID"))

    def test_terminal_panel_commands_are_checked(self):
        _, out = self.guard("s-alg1", "mcp__terminal__run_in_terminal", {"command": self.W})
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_claim_fields_of_the_wrong_type(self):
        pl = self.pool()
        pl["claims"]["PMO-p0-01"]["session_id"] = 7
        self.write_pool(pl)
        code, out = self.bash("s-alg1", "ls")
        self.assertEqual(code, 0)
        self.assertEqual(self.decision("s-scribe", self.W), "deny")

    def test_a_long_message_does_not_block_the_inbox(self):
        self.route("notify", "Alg-p1-01", "--text", "长" * 12000, sid="s-entry")
        self.route("notify", "Alg-p1-01", "--text", "短消息：新数据到了", sid="s-entry")
        _, out = self.prompt("s-alg1", "继续")
        self.assertIn("全文在 inbox/Alg-p1-01.jsonl", out.get("additionalContext", ""))
        self.assertLessEqual(len(out.get("additionalContext", "")), 9600)
        _, out = self.prompt("s-alg1", "继续")
        self.assertIn("短消息：新数据到了", out.get("additionalContext", ""))

    def test_the_desk_is_filled_in_by_route_claim(self):
        self.opening("rnd-p2", "RnD-p2-01", "s-rnd2")                      # the hook saw no desktop ID
        self.assertIsNone(self.pool()["claims"]["RnD-p2-01"]["desk"])
        self.claim("rnd-p2", "RnD-p2-01", "s-rnd2", desk="d-rnd2")
        self.assertEqual(self.pool()["claims"]["RnD-p2-01"]["desk"], "d-rnd2")
        self.assertEqual(self.pool()["pending"], [])

    def test_a_redispatched_item_is_not_owed_by_the_old_session(self):
        self.route("expect", "Alg-p1-02", "--role", "alg-p1", "--desk", "d-a12", sid="s-alg2")
        self.claim("alg-p1", "Alg-p1-02", "s-alg12", desk="d-a12")
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.prompt("s-alg12", "【委托 #2101】Alg-p2-01 → Alg-p1-02：需求包 x.md")
        _, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-alg1", "source": "compact"})
        self.assertIn("未了事项：无", out.get("additionalContext", ""))

    def test_waits_end_when_work_goes_on(self):
        self.hook("instances.py", {"hook_event_name": "Notification", "session_id": "s-alg2", "notification_type": "permission_prompt"})
        self.assertEqual(self.session("s-alg2")["status"], "waiting_researcher")
        self.hook("guard.py", {"hook_event_name": "PreToolUse", "session_id": "s-alg2", "agent_id": "sub", "agent_type": "Explore",
                               "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertNotEqual(self.session("s-alg2")["status"], "waiting_researcher")
        self.hook("instances.py", {"hook_event_name": "Notification", "session_id": "s-alg1", "notification_type": "elicitation_dialog"})
        self.hook("instances.py", {"hook_event_name": "Notification", "session_id": "s-alg1", "notification_type": "elicitation_complete"})
        self.assertNotEqual(self.session("s-alg1")["status"], "waiting_researcher")

    def test_recover_lists_background_runs_until_the_next_prompt(self):
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-q2", "background_tasks": [{"id": "train"}]})
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-q2", "reason": "other"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-q2", "source": "resume"})
        _, text = self.route("pool", "--recover", sid="s-entry")
        self.assertIn("Quality-p2-01：应用退出前有后台任务在跑", text)
        self.prompt("s-q2", "继续")
        _, text = self.route("pool", "--recover", sid="s-entry")
        self.assertNotIn("Quality-p2-01：应用退出前", text)

    def test_notify_targets_that_nobody_reads_are_refused(self):
        code, text = self.route("notify", "Alg-p0-2999", "--text", "x", sid="s-entry")
        self.assertNotEqual(code, 0)
        self.assertIn("没有记下负责会话", text)

    def test_a_returning_researcher_renews_the_focus_flag(self):
        self.route("focus", "open", "--topic", "谈规格", sid="s-alg2")
        path = os.path.join(self.tmp, ".claude", "state", "sessions", "s-alg2.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st.update(status="idle", status_at=(datetime.datetime.now().astimezone() - datetime.timedelta(minutes=45)).isoformat())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.prompt("s-alg2", "我们接着说规格 v3")                        # the researcher, not a message
        self.assertIn("Alg-p2-01", self.pool()["focus"])

    def test_the_entry_queue_skips_requests_already_sent(self):
        self.prompt("s-alg2", "【委托 #2301】PMO-p2-01 → Alg-p2-01：跨序列需求 x.md")
        rows = {"open": [], "cross": [{"work_item_id": "2301", "work_item_status": "待接单", "name": "需求包（Product-p2-01）：要数据"},
                                      {"work_item_id": "2302", "work_item_status": "待接单", "name": "需求包（Product-p2-01）：要标注"}]}
        names = {"waiting": "待接单", "queued": "待开工"}
        with mock.patch.object(ledger, "Ledger") as bound, \
                mock.patch.object(ledger, "query", side_effect=lambda lg, name, seq=None, as_json=False: rows[name]):
            bound.return_value.state.side_effect = lambda wid, slot: names[slot]
            entry = route.queue_rows(self.tmp, json.loads(self.read(".claude/hooks/policy.json")),
                                     self.pool()["claims"]["PMO-p2-01"], "pmo")
        self.assertEqual([r["work_item_id"] for r in entry], ["2302"])

    def test_no_refill_rows_at_the_cap(self):
        policy_path = os.path.join(self.hooks, "policy.json")
        with open(policy_path, encoding="utf-8") as f:
            policy = json.load(f)
        policy["pool"]["max_sessions"] = len(self.pool()["claims"])
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump(policy, f)
        _, text = self.route("claim", "alg-p2", "--as", "Alg-p2-01", sid="s-alg2")
        self.assertNotIn("（补位用）", text)
        self.assertIn("总上限", text)


class SecondReviewSetupTest(ProjectCase):
    """Second review: paths through symlinks, the guard/policy pair, the hard-rule wording."""

    def test_a_protected_path_that_is_a_symlink_stays_protected(self):
        outside_dir = tempfile.mkdtemp(prefix="solo-lab-data-")
        try:
            os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
            os.symlink(outside_dir, os.path.join(self.tmp, "data", "eval"))
            _, out = self.guard("alg-p0", "Write", {"file_path": os.path.join(self.tmp, "data", "eval", "test.json")})
            self.assertEqual(out.get("permissionDecision"), "deny")
            _, out = self.guard("", "Write", {"file_path": "data/eval/test.json"}, agent_id=None)
            self.assertEqual(out.get("permissionDecision"), "deny")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_same_version_changes_are_not_held_back(self):
        with open(os.path.join(self.hooks, "guard.py"), "a", encoding="utf-8") as f:
            f.write("\n# local change\n")
        with open(TASK, encoding="utf-8") as f:
            task = json.load(f)
        task["protected_paths"].append("data/holdout/**")
        path = os.path.join(self.tmp, "task2.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        plan = setup.build_plan(self.tmp, path, None)
        acts = {rel: kind for kind, rel, _ in setup.actions_for(self.tmp, plan)}
        self.assertEqual(acts[".claude/hooks/guard.py"], "new")
        self.assertEqual(acts[".claude/hooks/policy.json"], "update")

    def test_check_names_a_mismatched_pair(self):
        path = os.path.join(self.hooks, "policy.json")
        with open(path, encoding="utf-8") as f:
            policy = json.load(f)
        policy["version"] = 3
        with open(path, "w", encoding="utf-8") as f:
            json.dump(policy, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertIn("guard.py 和 policy.json 不配套", out.getvalue())

    def test_hard_rule_wording_says_what_denies_and_what_asks(self):
        with open(os.path.join(self.tmp, "CLAUDE.md"), encoding="utf-8") as f:
            claude = f.read()
        self.assertIn("在任何会话里都直接拒绝", claude)
        self.assertIn("用 Agent 工具派书记员", claude)
        self.assertIn("弹窗询问", claude)

    def test_notify_by_role_only_when_the_role_has_one_session(self):
        with self.assertRaises(SystemExit):
            route.notify(self.spec, self.tmp, "alg-p1", "x")
        with contextlib.redirect_stdout(io.StringIO()):
            route.notify(self.spec, self.tmp, "alg-p2", "y")


class PoolThirdReviewTest(PoolProjectCase):
    """Fixes from the third, narrow review of 0.5.0."""
    W = "python3 -I .claude/solo-lab/bin/ledger.py register packages/drafts/alg/x.md --apply"

    def setUp(self):
        super().setUp()
        self.started_pool()

    def decision(self, sid, command, desk=None, **extra):
        return self.bash(sid, command, desk=desk, **extra)[1].get("permissionDecision")

    def set_session(self, sid, **fields):
        path = os.path.join(self.tmp, ".claude", "state", "sessions", sid + ".json")
        st = self.session(sid)
        st.update(fields)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)

    def ago(self, minutes):
        return (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def test_quoted_escaped_and_commented_mentions_are_not_run(self):
        for cmd in ("git commit -m 'docs: use `git worktree add` instead of `git checkout -b`'",
                    "echo '- never run `git stash` in the shared tree' >> NOTES.md",
                    "# check before `git checkout`\ngit status",
                    'echo "later: \\$(git stash)" >> NOTES.md',
                    "gh pr create --body 'Do not run `git pull` here'",
                    "rg -n -F '`route.py release`' docs/ CLAUDE.md",
                    "# then ask the researcher to run `route.py release Alg-p0-01`\npython3 -I %s pool" % self.R):
            for sid in ("s-alg1", "s-researcher"):
                self.assertIsNone(self.decision(sid, cmd), (sid, cmd))
        self.assertEqual(self.decision("s-alg1", 'git commit -m "$(git stash)"'), "deny")
        self.assertEqual(self.decision("s-alg1", "echo \"`git stash`\""), "deny")

    def test_more_ways_to_run_a_command(self):
        for cmd in ("find . -maxdepth 0 -exec bash -c 'git stash' \\;",
                    "find . -maxdepth 0 -exec env git stash \\;",
                    "find /usr/bin -maxdepth 1 -name git -exec {} stash \\;",
                    "printf 'git stash\\n' > /tmp/solo-lab-test-sw.sh && . /tmp/solo-lab-test-sw.sh",
                    "cat > /tmp/solo-lab-test-sw2.sh <<'EOF'\ngit checkout main\nEOF\nbash /tmp/solo-lab-test-sw2.sh",
                    "cat <<'EOF' | bash\ngit stash\nEOF",
                    "(cd /tmp && true); git checkout main",
                    "cd /tmp && cd - && git checkout main",
                    "C='git stash'; bash -c \"$C\"",
                    "git --git-dir=/tmp/other/.git checkout -f main"):
            self.assertEqual(self.decision("s-alg1", cmd), "deny", cmd)
        for cmd in ("find . -maxdepth 0 -exec sh -c 'python3 -I %s release PMO-p0-01' \\;" % self.R,
                    "python3 -m cProfile -o /dev/null %s release PMO-p0-01" % self.R,
                    "X=%s; python3 -I $X pool; python3 -I $X release Alg-p0-01" % self.R):
            self.assertEqual(self.decision("s-researcher", cmd), "deny", cmd)

    def test_git_in_other_trees_is_fine(self):
        for cmd in ("cd ~/other-repo && git pull", "git -C ~/other-repo checkout v1.2",
                    "cd /tmp/ref && out=$(git pull 2>&1); echo \"$out\""):
            self.assertIsNone(self.decision("s-alg1", cmd), cmd)
        wt = os.path.join(self.tmp, ".claude", "worktrees", "feat")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /somewhere/project/.git/worktrees/feat\n")
        self.assertIsNone(self.decision("s-alg1", "git -C .claude/worktrees/feat checkout -b feat3"))
        self.assertIsNone(self.decision("s-alg1", "git stash", cwd=wt))
        os.makedirs(os.path.join(self.tmp, "repo", ".git"))        # a clone inside the project is still shared
        self.assertEqual(self.decision("s-alg1", "git -C repo checkout other"), "deny")

    def test_a_repeated_option_is_refused(self):
        cmd = "python3 -I %s claim pmo-scribe --desk= --desk d-scribe --wait 0" % self.R
        self.assertEqual(self.decision("s-term", cmd), "deny")
        cmd = "python3 -I %s expect Alg-p0-02 --role alg-p0 --role pmo-scribe --desk d-x" % self.R
        self.assertEqual(self.decision("s-alg2", cmd), "deny")

    def test_identity_variables_in_other_forms(self):
        tail = "python3 -I %s notify PMO-p2-01 --text x" % self.R
        for prefix in ("unset -v CLAUDE_CODE_SESSION_ID CLAUDE_CODE_HOST_SESSION_ID; ", 'env -i PATH="$PATH" ',
                       'env -u "CLAUDE_CODE_SESSION_ID" -u "CLAUDE_CODE_HOST_SESSION_ID" ',
                       "unset CLAUDE_CODE_{,HOST_}SESSION_ID; ", "export -n CLAUDE_CODE_SESSION_ID CLAUDE_CODE_HOST_SESSION_ID; "):
            self.assertEqual(self.decision("s-alg1", prefix + tail), "deny", prefix)
        for cmd in ('echo "CLAUDE_CODE_HOST_SESSION_ID=$CLAUDE_CODE_HOST_SESSION_ID"', "env | grep CLAUDE_CODE_SESSION_ID=",
                    "env -i make test"):
            self.assertIsNone(self.decision("s-alg1", cmd), cmd)

    def test_who_a_request_is_from(self):
        self.route("refill", "alg-p1")                                    # a plain terminal
        self.route("refill", "alg-p0", sid="s-nested", desk="d-s-alg1")   # a process started inside Alg-p1-01
        rows = [json.loads(line) for line in self.read("inbox/PMO-p2-01.jsonl").splitlines() if line.strip()]
        self.assertEqual([r["from"] for r in rows if "补位请求" in r.get("text", "")],
                         ["终端里的进程（没有会话 ID）", "和已认领会话共用桌面会话 ID 的进程"])

    def test_settings_hook_the_terminal_panel(self):
        hooks = json.loads(self.read(".claude/settings.json"))["hooks"]
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            regex = [g["matcher"] for g in hooks[event] if not re.fullmatch(r"[A-Za-z0-9_|, -]+", g["matcher"])]
            self.assertTrue(any(re.search(m, "mcp__terminal__run_in_terminal") for m in regex), event)
            self.assertFalse(any(re.search(m, "Bash") or re.search(m, "Task") for m in regex), event)

    def test_a_link_into_an_owned_path_keeps_its_owner(self):
        os.symlink("../.claude/hooks", os.path.join(self.tmp, "org", "hk"))
        _, out = self.guard("s-alg1", "Edit", {"file_path": os.path.join(self.tmp, "org", "hk", "guard.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")
        spec_md = os.path.join(self.tmp, "spec", "spec.md")
        if os.path.lexists(spec_md):
            os.remove(spec_md)
        os.symlink("task.json", spec_md)
        _, out = self.guard("s-alg2", "Edit", {"file_path": spec_md})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("s-alg1", "Edit", {"file_path": os.path.join(self.tmp, "org", "notes.md")})
        self.assertIsNone(out.get("permissionDecision"))
        _, out = self.guard("s-alg2", "Edit", {"file_path": os.path.join(self.tmp, ".claude", "agent-memory", "alg-p2", "MEMORY.md")})
        self.assertIsNone(out.get("permissionDecision"))

    def test_a_subagent_call_leaves_the_parent_prompt_open(self):
        perm = {"hook_event_name": "PermissionRequest", "session_id": "s-alg1", "tool_name": "Bash", "tool_input": {"command": "x"}}
        self.hook("instances.py", perm)
        self.bash("s-alg1", "ls", agent_id="bg-1")
        self.assertEqual(self.session("s-alg1")["status"], "waiting_researcher")
        self.hook("audit.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s-alg1",
                               "tool_input": {"command": "x"}, "tool_response": {"stdout": "", "stderr": ""}})
        self.assertEqual(self.session("s-alg1")["status"], "busy")
        self.hook("instances.py", dict(perm, agent_id="a-A"))          # two subagents: A asks, B goes on
        self.hook("instances.py", {"hook_event_name": "Notification", "session_id": "s-alg1",
                                   "notification_type": "permission_prompt", "message": "x"})
        self.bash("s-alg1", "ls", agent_id="a-B")
        self.assertEqual(self.session("s-alg1")["status"], "waiting_researcher")
        self.bash("s-alg1", "ls", agent_id="a-A")
        self.assertEqual(self.session("s-alg1")["status"], "busy")

    def test_clear_keeps_a_claim_made_in_a_terminal(self):
        self.route("expect", "Quality-p0-01", "--role", "quality-p0", "--session", "s-t", sid="s-q2")
        self.claim("quality-p0", "Quality-p0-01", "s-t")
        self.assertIsNone(self.pool()["claims"]["Quality-p0-01"].get("desk"))
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-t", "source": "resume"}, desk="d-t")
        self.assertEqual(self.pool()["claims"]["Quality-p0-01"]["desk"], "d-t")
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-t", "reason": "clear"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-t2", "source": "clear"}, desk="d-t")
        self.assertEqual(self.pool()["claims"]["Quality-p0-01"]["session_id"], "s-t2")
        # never seen with its desktop ID: paired by time
        self.route("expect", "PMO-p1-01", "--role", "pmo-router", "--session", "s-r", sid="s-entry")
        self.claim("pmo-router", "PMO-p1-01", "s-r")
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "s-r", "reason": "clear"})
        self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "s-r2", "source": "clear"}, desk="d-r")
        claim = self.pool()["claims"]["PMO-p1-01"]
        self.assertEqual((claim["session_id"], claim["desk"]), ("s-r2", "d-r"))

    def test_focus_notes_after_a_message_and_a_report(self):
        self.route("focus", "open", "--topic", "谈规格 v3", sid="s-alg2")
        self.set_session("s-alg2", status="idle", status_at=self.ago(45), last_tool_at=self.ago(45))
        self.prompt("s-alg2", "【委托 #2301】PMO-p2-01 → Alg-p2-01：需求包 packages/drafts/alg/x.md")
        self.assertNotIn("Alg-p2-01", self.pool()["focus"])
        _, out = self.prompt("s-alg2", "我回来了，接着说规格 v3")
        self.assertIn("已因空闲 30 分钟失效", out.get("additionalContext", ""))
        self.route("focus", "open", "--topic", "谈规格 v4", sid="s-alg2")
        self.set_session("s-alg2", status="idle_bg", status_at=self.ago(50), last_tool_at=self.ago(50))
        _, out = self.prompt("s-alg2", "<task-notification>\n<status>completed</status>\n</task-notification>")
        self.assertNotIn("Alg-p2-01", self.pool()["focus"])            # a background report is not the researcher
        self.assertNotIn("失效", out.get("additionalContext", ""))
        _, out = self.prompt("s-alg2", "接着说")
        self.assertIn("已因空闲 30 分钟失效", out.get("additionalContext", ""))

    def test_one_meaning_of_the_notice(self):
        claude = self.read("CLAUDE.md")
        self.assertIn("也是消息，会叫起对方的下一轮", claude)
        self.assertNotIn("留在收件箱，不发消息", claude)
        path = os.path.join(self.tmp, "packages", "drafts", "quality", "v.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(re.findall(r"```markdown\n(.*?)\n```", open(EXAMPLES, encoding="utf-8").read(), re.S)[1].replace("关系：下发", "关系：跨序列")
                    .replace("产品 +1（Product-p1-03）", "算法 +2（Alg-p2-01）"))
        out = route.classify_draft(self.spec, self.tmp, path)
        self.assertEqual(out.get("row"), None if out.get("errors") else "R7", out)
        p0 = self.read(".claude/agents/alg-p0.md")
        self.assertEqual(p0.count("你自己跑出的分数是开发分"), 1)
        self.assertNotIn("。；", p0)
        self.assertNotIn("送审", p0 + claude + self.read(".claude/agents/pmo-p2.md") + self.read(".claude/agents/quality-p2.md"))
        self.assertIn("质量序列的会话例外", claude)
        self.assertIn("其余序列中任一个", self.read(".claude/agents/pmo-p2.md"))


class ThirdReviewSetupTest(ProjectCase):
    """Third review: guard/policy pairs during an upgrade."""

    def set_policy(self, **fields):
        path = os.path.join(self.hooks, "policy.json")
        with open(path, encoding="utf-8") as f:
            policy = json.load(f)
        for k, v in fields.items():
            if v is None:
                policy.pop(k, None)
            else:
                policy[k] = v
        with open(path, "w", encoding="utf-8") as f:
            json.dump(policy, f, ensure_ascii=False)

    def test_pair_messages_say_which_way(self):
        path = os.path.join(self.hooks, "guard.py")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("POLICY_VERSION = %d\n" % setup.POLICY_VERSION, ""))   # a guard from before policy versions
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertIn("旧 guard 在按新规则跑", out.getvalue())
        self.assertNotIn("改过：.claude/hooks/policy.json", out.getvalue())

    def test_pool_health_on_an_old_policy(self):
        self.set_policy(entry_role=None)                            # as left by a plugin from before the pool
        problems, _ = setup.pool_health(self.tmp, {})
        self.assertTrue(problems and "还是旧版的" in problems[0])


class BracketPathTest(PoolProjectCase):
    """A project path with glob characters, like Phase1[Current], is read literally."""
    PREFIX = "solo-lab-pool-Phase1[Current]-"

    def setUp(self):
        super().setUp()
        self.started_pool()

    def test_a_cd_to_a_path_with_brackets_is_still_this_project(self):
        self.route("expect", "RnD-p2-01", "--role", "rnd-p2", "--desk", "d-rnd", sid="s-researcher")
        cmd = 'cd "%s" && python3 -I %s claim rnd-p2 --as RnD-p2-01' % (self.tmp, self.R)
        self.assertIsNone(self.bash("s-term", cmd)[1].get("permissionDecision"), cmd)

    def test_pool_views_read_sessions_and_receipts(self):
        self.assertIn("[", self.tmp)
        self.hook("instances.py", {"hook_event_name": "Stop", "session_id": "s-alg1", "last_assistant_message": "好"})
        _, out = self.route("pool", "--role", "alg-p1", sid="s-alg2")
        self.assertIn("Alg-p1-01（alg-p1）空闲", out)
        self.assertIn("建议：发给 Alg-p1-01", out)
        sent = os.path.join(self.tmp, "packages", "sent")
        with open(os.path.join(sent, "2101.json"), "w", encoding="utf-8") as f:
            json.dump({"work_item_id": "2101", "draft": "packages/drafts/alg/a.md", "status": "registered"}, f)
        with open(os.path.join(sent, "2102.json"), "w", encoding="utf-8") as f:
            json.dump({"work_item_id": "2102", "draft": "packages/drafts/alg/b.md", "status": "creating"}, f)
        import pool
        self.assertTrue(pool.receipt_exists(self.tmp, "2101"))
        self.assertFalse(pool.receipt_exists(self.tmp, "2103"))
        _, out = self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 packages/drafts/alg/a.md")
        self.assertNotIn("登记回执", out.get("additionalContext", ""))
        _, out = self.route("pool", "--recover", sid="s-entry")
        self.assertIn("#2102", out)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.check(self.tmp)
        self.assertNotIn("还停在旧版插件规则上", out.getvalue())


class DeliveryTextTest(PoolProjectCase):
    """1.0.3: decisions reach the researcher at two moments; a turn another session woke asks nobody."""

    GATES = "其他 agent 会话叫醒的轮次里向研究员的提问（hook 处理）："

    def generated(self):
        texts = {"CLAUDE.md": self.read("CLAUDE.md"), "org/org.md": self.read("org/org.md")}
        for name in sorted(os.listdir(os.path.join(self.tmp, ".claude", "agents"))):
            texts[name] = self.read(os.path.join(".claude", "agents", name))
        return texts

    def test_settings_hook_the_wrap_up_tool(self):
        self.assertEqual(setup.WRAPUP_TOOL, "^mcp__.+__wrap_up$")
        groups = json.loads(self.read(".claude/settings.json"))["hooks"]["PreToolUse"]
        wrap = [g for g in groups if g["matcher"] == setup.WRAPUP_TOOL]
        self.assertEqual(len(wrap), 1)
        self.assertEqual(wrap[0]["hooks"], groups[0]["hooks"])                    # written like the other guard groups
        self.assertTrue(wrap[0]["hooks"][0]["command"].endswith('/.claude/hooks/guard.py"'))
        self.assertTrue(re.search(setup.WRAPUP_TOOL, "mcp__ccd_turn__wrap_up"))
        for other in ("mcp__ccd_turn__update_plan", "wrap_up", "AskUserQuestion", "Bash"):
            self.assertFalse(re.search(setup.WRAPUP_TOOL, other), other)

    def test_texts_say_when_decisions_reach_the_researcher(self):
        texts = self.generated()
        daily, day = "每" + "日", "每" + "天"                  # split so a grep for the old wording finds none here
        for name, text in texts.items():
            for old in (daily + "决策队列", day + "交一次", day + "把待决定的决策", daily + "队列", "随时" + "交",
                        day + "的决策队列", "被消息叫起来的轮次不停下来问人"):
                self.assertNotIn(old, text, name)
        entry = texts["pmo-p2.md"]
        for part in ("**决策队列。** 决策只在两个时刻推给研究员，到时 hook 会在上下文里提示你",
                     "在状态里提一次，写事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）和工作项链接，不超过 3 行",
                     "紧急程度为“插队”的，这一轮可以在收尾里问研究员一次",
                     "距他上一条消息 30 分钟以上，或者第一次", "之前提过的也附", "改版、截止前、每日汇总都不推",
                     "**面向研究员的内容。** 你主动给研究员的只有两类：决策（给链接，执行方和要研究员亲手做的事一并写上）、"
                     "里程碑（研究任务走完一步、出了结论）",
                     "收到【需拍板】，请发来的 +2 写成决策包，不直接转问研究员",
                     "面向研究员最多写一行状态，不提问，不标“等研究员回答”，不复述已经登记过的决策",
                     "补位请求（`route.py refill`）记在 `route.py pool` 里，研究员来谈时跟在决策清单后面列出；补位请求不算决策"):
            self.assertIn(part, entry)
        self.assertIn("研究员在本会话里拍板后，请书记员确认决策的“决定”这一步", entry)     # what follows the decision stays
        self.assertIn("入口要时汇总决策队列", texts["pmo-router.md"])
        claude = texts["CLAUDE.md"]
        for part in ("被其他 agent 会话叫醒的轮次，研究员多半不在，不向研究员提问",
                     "其他会话发来的消息、本会话开出的会话结束一轮的回报、开会话时上级写的首轮",
                     "hook 会去掉收尾里的提问、拒绝 AskUserQuestion", "唯一的例外是插队决策的登记回执到入口的那一轮",
                     "hook 只拦两类事：会损坏数据的，和研究员多半不在时向他提的问题",
                     "池会话被其他 agent 会话叫醒的轮次：拒绝 AskUserQuestion，去掉收尾工具里的提问"):
            self.assertIn(part, claude)
        self.assertNotIn("收尾时不留等研究员回答的问题", claude)                  # the exception above would contradict it
        for name in ("alg-p2.md", "alg-p1.md", "alg-p0.md"):
            self.assertIn("被其他 agent 会话叫醒的轮次", texts[name], name)
            self.assertIn("收尾里的提问 hook 会去掉", texts[name], name)
        self.assertNotIn("研究员每天", texts["org/org.md"])
        decisions = self.read("org/decisions.md")
        self.assertIn("紧急程度为“插队”的，登记那一轮可以问研究员一次", decisions)
        self.assertIn("2026-11-15 定下推送时机", decisions)
        self.assertIn("被其他 agent 会话叫醒的轮次，池会话都不向研究员提问，只有插队决策的登记回执到入口的那一轮，入口可以在收尾里问一次", decisions)
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        text = pool_lib.injection(self.tmp, policy, {"name": "PMO-p2-01", "role": "pmo-p2"}, "规则已更新。")
        self.assertNotIn("下文略", text)                                         # the entry's role text is injected whole
        self.assertIn("**面向研究员的内容。**", text)

    def test_what_the_researcher_does_by_hand_is_a_decision(self):
        """2026-11-16: whatever needs the researcher is a decision; its package header says who carries it out
        (执行方: 研究员 or a session) and what the researcher must do by hand (研究员要做的事). A decision goes
        待决定 → 待执行 → 归档 (carried out): the scribe tells the 执行方 what was decided, and a session that
        carries it out asks for the archive as soon as it is done. The entry tells the researcher about two
        kinds of things only: decisions and milestones."""
        texts = self.generated()
        texts["org/decisions.md"] = self.read("org/decisions.md")
        template = texts["templates/decision.md"] = self.read(".claude/solo-lab/templates/decision.md")
        with open(os.path.join(ROOT, "skills", "packages", "assets", "decision.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), template)
        sources = dict(texts)                     # and what agents and the researcher read straight from the plugin
        for rel in ("skills/entry/SKILL.md", "skills/entry/references/routing.md", "skills/entry/references/focus.md",
                    "skills/ledger/SKILL.md", "skills/ledger/references/commands.md", "skills/packages/SKILL.md",
                    "skills/packages/assets/decision.md", "skills/packages/references/examples.md",
                    "skills/packages/references/principles.md", "skills/setup/references/mechanisms.md",
                    "scaffold/hooks/inbox.py", "scripts/route.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                sources[rel] = f.read()
        with open(os.path.join(REPO, "docs", "使用说明.md"), encoding="utf-8") as f:
            sources["docs/使用说明.md"] = f.read()
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
            sources["CHANGELOG.md 1.0.3"] = f.read().split("\n## 1.0.3\n", 1)[1].split("\n## ", 1)[0]
        to_do = "研究员" + "待办"                    # split so a grep for the old term finds none here
        for name, text in sources.items():
            for old in (to_do, "动手：", "等研究员：<事项>", "（截止 <时间>）", "带期限", "记成阻塞", "“等研究员：”开头", "已做：",
                        "只有三类", "要做的事也做完了",
                        # who carries a decision out follows its header, not the option picked
                        "选定的做法", "某个选项下", "选的做法",
                        # ... and its 执行方 line: neither 研究员要做的事 nor being the sender settles it
                        "由发出方执行", "发出方办完", "不是“无”的，由研究员执行", "不是“无”的由研究员执行", "还要他亲手做",
                        # 决定 is confirmed as soon as the researcher decides: no early comment, no confirming after the work
                        "记下选了哪个选项", "选了还没做", "做完才确认", "做完以后才确认", "做完了才确认"):
                self.assertNotIn(old, text, name)
        for rel, part in (("skills/entry/SKILL.md", "执行方是研究员的（包头“执行方”写“研究员”，“研究员要做的事”写着要他亲手做什么），"
                                                    "`query queue` 在它定了以后、归档以前把它列成“待执行”"),
                          ("skills/entry/SKILL.md", "执行方是会话的，书记员确认“决定”时把决定内容发给它；它照决定执行完，马上请书记员归档，不在待执行里停留"),
                          ("skills/entry/SKILL.md", "每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接"),
                          ("skills/entry/references/routing.md", "研究员定了就确认“决定”，到“待执行”，书记员通知执行方；执行方是研究员的，研究员说做完了才归档；"
                                                                 "执行方是会话的，它照决定执行完马上请书记员归档"),
                          ("skills/entry/references/routing.md", "写上执行方和要研究员亲手做的事"),
                          ("skills/ledger/SKILL.md", "输出的最后一行是“执行方：<研究员或会话名>。”"),
                          ("skills/ledger/references/commands.md", "状态“<状态>”。”。决策确认了“决定”这一步"),
                          ("skills/ledger/SKILL.md", "执行方是会话的，把决定内容发给它，请它照决定执行，执行完马上请书记员归档"),
                          ("skills/ledger/SKILL.md", "执行方是研究员的（回执里 `executor` 是“研究员”；没有这一项的旧回执，`researcher_action` 有值的算研究员）"),
                          ("skills/ledger/references/commands.md", "状态是“已决定”、执行方是研究员的也列，到归档为止。执行方看回执里的 `executor`；"
                                                                   "没有这一项的旧回执，`researcher_action` 有值的算研究员"),
                          ("skills/ledger/references/commands.md", "`stage`、`urgency`、`due`、`executor`、`researcher_action`、`doc`"),
                          ("skills/packages/SKILL.md", "每份决策包都有这一行，没有要研究员亲手做的写“无”"),
                          ("skills/packages/SKILL.md", "包头还有“执行方”一行：写“研究员”，或者执行这条决策的会话名，多半是发出方"),
                          ("skills/packages/references/principles.md", "\n- 执行方：<决策定下以后由谁执行：研究员，或者执行它的会话名>\n- 研究员要做的事："),
                          ("skills/setup/references/mechanisms.md", "决策包包头还多一行“执行方”，写“研究员”或者执行这条决策的会话名"),
                          ("skills/setup/references/mechanisms.md", "升级前写好、还没登记的决策包草稿，补上这两行再登记"),
                          ("scaffold/hooks/inbox.py", "每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接"),
                          ("docs/使用说明.md", "每条一行：状态、事项、紧急程度、回复期限、执行方、要你亲手做的事、链接"),
                          ("docs/使用说明.md", "补上这两行再登记"),
                          ("CHANGELOG.md 1.0.3", "决策包包头新加两行：“执行方”写这条决策定下以后由谁执行"),
                          ("CHANGELOG.md 1.0.3", "补上“执行方”和“研究员要做的事”两行再登记")):
            self.assertIn(part, sources[rel], rel)
        for name in ("CLAUDE.md", "pmo-p2.md", "org/org.md", "org/decisions.md", "templates/decision.md", "pmo-scribe.md",
                     "pmo-router.md", "alg-p2.md"):
            self.assertIn("研究员要做的事", texts[name], name)
            self.assertIn("执行方", texts[name], name)
        entry = texts["pmo-p2.md"]
        for part in ("你主动给研究员的只有两类：决策（给链接，执行方和要研究员亲手做的事一并写上）、里程碑（研究任务走完一步、出了结论）",
                     "再把待决定的决策和等研究员执行的决策全部附在末尾，之前提过的也附，每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接",
                     "**决策的三个阶段。** 每条决策走三步：待决定、待执行、归档（已执行）；空间里的“已决定”就是待执行。"
                     "研究员定了，照上面马上请书记员确认“决定”这一步，书记员会通知包头写的执行方，各会话就知道选了哪个选项",
                     "执行方是研究员的：`ledger.py query queue` 在它定了以后、归档以前列成“待执行”",
                     "登记请求里写“归档：#<决策 ID>，由 研究员 提出”，照抄他的原话，写明他做了什么，书记员记成评论再归档",
                     "执行方是会话的，书记员把决定内容发给它，它照决定执行完，马上请书记员归档，不在待执行里停留",
                     "执行方是你自己的（比如第 9 步的决策），你也照决定办完马上请书记员归档",
                     "被决策卡住的工作项照旧记阻塞，阻塞原因写“等研究员：决策 #<ID>”，决策归档以后由卡住的那个 +2 请书记员清掉",
                     "补位请求不算决策",
                     # the table the router is asked for: the same columns as the entry skill and the router's role
                     "请它汇总成表：每件写状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、选项、建议，“插队”的排最前。"):
            self.assertIn(part, entry)
        claude = texts["CLAUDE.md"]
        for part in ("要研究员亲手做的事（整理数据、挂 API、维护沙箱、开 Docker、调设置这类）也是决策",
                     "包头“执行方”写“研究员”，“研究员要做的事”写明要他做什么；",
                     "每份决策包都有“执行方”和“研究员要做的事”这两行：执行方写“研究员”，或者执行这条决策的会话名，多半是发出方；"
                     "“研究员要做的事”没有要研究员亲手做的写“无”，执行方是研究员时不能是“无”，执行方是会话时写“无”。",
                     "每条决策走三步：待决定、待执行（空间里的“已决定”）、归档（已执行）。研究员定了，书记员马上确认“决定”这一步，并通知执行方",
                     "执行方是会话的，照决定执行完，马上请书记员归档，不在待执行里停留；",
                     "执行方是研究员的，他在入口会话说做完了，入口带着他的原话请书记员归档，书记员在评论里记下他做了什么。",
                     "决策归档以后，卡住的那个 +2 请书记员清掉阻塞"):
            self.assertIn(part, claude)
        alg = texts["alg-p2.md"]
        for part in ("也写决策包，写在包头“研究员要做的事”。每份决策包都有这一行，没有要研究员亲手做的写“无”。",
                     "包头还有“执行方”一行，写这条决策定下以后由谁执行：“研究员”，或者执行它的会话名，多半是你自己。",
                     "执行方是研究员时“研究员要做的事”不能是“无”，执行方是会话时写“无”，对不上包检查会退回。",
                     "执行方是研究员的，他做完了，入口请书记员归档，再通知你",
                     "被决策卡住的工作项，登记以后请书记员把阻塞原因写成“等研究员：决策 #<ID>”，这条决策归档以后再请它清掉",
                     "执行方是你的，书记员把决定内容发给你，你照决定执行完，马上请书记员归档，不在待执行里停留"):
            self.assertIn(part, alg)
        self.assertNotIn("研究员要做的事", texts["alg-p1.md"])                    # only a +2 writes decision packages
        self.assertIn("决策包的执行方写的是你时，书记员确认“决定”以后会把决定内容发给你：照决定执行完，马上请书记员归档，不在待执行里停留",
                      texts["pmo-router.md"])
        self.assertIn("状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事", texts["pmo-router.md"])
        scribe = texts["pmo-scribe.md"]
        self.assertIn("“决定”照上一条在研究员定了以后就确认，确认完通知执行方：输出的最后一行写着执行方，也就是决策包包头的“执行方”", scribe)
        self.assertIn("另给执行方发一条 `【通知 #<决策 ID>】PMO-p0-01 → <执行方的会话名>：研究员定了：<决定内容>。"
                      "照决定执行，执行完马上请书记员归档`", scribe)
        self.assertIn("执行方是研究员的（包头“研究员要做的事”写着要他亲手做什么），不另发，入口按清单列给研究员；他做完了才归档", scribe)
        self.assertIn("归档的登记请求要写“由 研究员 提出”，带研究员说做完了的原话", scribe)
        self.assertIn("归档前先用 `ledger.py comment` 记下研究员做了什么", scribe)
        decisions = texts["org/decisions.md"]
        self.assertIn("每条决策走三步：待决定、待执行（空间里的“已决定”）、归档（已执行）。研究员定了，书记员就确认“决定”这一步，并通知执行方", decisions)
        self.assertIn("决策包包头“执行方”写这条决策定下以后由谁执行，“研究员”或者执行它的会话名，多半是发出方", decisions)
        self.assertIn("入口主动给研究员的内容只有决策（给链接，执行方和要研究员亲手做的事一并写上）和里程碑两类；补位请求不算决策。依据：研究员 2026-11-16 定。", decisions)
        self.assertIn("执行方、定下以后研究员要做的事", texts["org/org.md"])        # the package table
        for part in ("- 需要接收方做的事：拍板（决策队列）／确认记录无误（研究员主动发起）\n"
                     "- 执行方：<研究员，或者执行这条决策的会话名，多半是发出方的会话名>\n"
                     "- 研究员要做的事：<这条决策定下以后要研究员亲手做的事，比如整理数据、挂 API、维护沙箱、开 Docker、调设置；没有写“无”>\n",
                     "每条决策走三步：待决定、待执行、归档（已执行）",
                     "包检查要求决策包有这一行",
                     "比如 `Alg-p2-01`，只写名字、不加说明。",
                     "执行方是研究员时，“研究员要做的事”不能是“无”；执行方是会话时，“研究员要做的事”写“无”。"
                     "缺了“执行方”这一行，或者两行对不上，包检查会退回。"):
            self.assertIn(part, template)

    def test_a_decision_from_the_queue_is_mentioned_when_it_is_registered(self):
        path = os.path.join(self.tmp, "packages", "drafts", "alg", "q.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(example_blocks()[7])
        out = route.classify_draft(self.spec, self.tmp, path)
        self.assertEqual((out["row"], out["next_hop"]), ("R5", "pmo-scribe"))
        steps = "\n".join(out["steps"])
        self.assertNotIn("每日", steps)
        self.assertIn("登记回执到的那一轮，在状态里提一次：事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）、工作项链接", steps)
        self.assertIn("“插队”的，这一轮可以在收尾里问研究员一次", steps)
        self.assertIn("研究员主动来谈时，连同其余待决定的决策和等研究员执行的决策列给他", steps)
        self.assertIn("研究员定了，书记员确认“决定”这一步，决策到“待执行”；书记员把决定内容发给执行方 Alg-p2-01，"
                      "它照决定执行完，马上请书记员归档", steps)                    # the package's 执行方
        with open(path, "w", encoding="utf-8") as f:                              # one the researcher carries out
            f.write(example_blocks()[9])
        steps = "\n".join(route.classify_draft(self.spec, self.tmp, path)["steps"])
        self.assertIn("研究员定了，书记员确认“决定”这一步，决策到“待执行”；执行方是研究员，他说做完了，请书记员归档", steps)
        with open(path, "w", encoding="utf-8") as f:                              # decided in a talk with Product-p2-01
            f.write(example_blocks()[8])
        out = route.classify_draft(self.spec, self.tmp, path)
        self.assertEqual(out["row"], "R6")
        self.assertIn("书记员把决定内容发给执行方 Product-p2-01，它照决定执行完，马上请书记员归档", out["steps"])

    def test_a_refill_request_waits_for_the_researcher(self):
        self.started_pool()
        code, text = self.route("refill", "alg-p0", sid="s-alg2")
        self.assertEqual(code, 0)
        self.assertIn("研究员来谈时入口会列出", text)
        inbox = self.read("inbox/PMO-p2-01.jsonl")
        self.assertIn("研究员来谈时跟在决策清单后面列出，补位请求不算决策", inbox)
        self.assertNotIn("每日", inbox + text)

    def test_audit_counts_the_questions_hooks_took_out(self):
        self.started_pool()
        code, text = self.route("audit", sid="s-entry")
        self.assertEqual(code, 0)
        self.assertIn(self.GATES + "\n- 无", text)
        wrap, ask = "mcp__ccd_turn__wrap_up", "AskUserQuestion"
        rows = [{"session_name": "PMO-p2-01", "role": "pmo-p2", "tool": wrap, "agent_turn_gate": "stripped"}] * 3 + [
            {"session_name": "PMO-p2-01", "role": "pmo-p2", "tool": wrap, "agent_turn_gate": "allowed_urgent"},
            {"session_name": "Alg-p1-01", "role": "alg-p1", "tool": ask, "agent_turn_gate": "denied"},
            {"session_name": "Alg-p1-01", "role": "alg-p1", "tool": wrap, "agent_turn_gate": "stripped"},
            {"session_name": "Alg-p2-01", "role": "alg-p2", "tool": wrap},                        # nothing was asked
            {"session_name": "Alg-p2-01", "role": "alg-p2", "tool": "Bash", "agent_turn_gate": "unknown"}]
        path = os.path.join(self.tmp, ".claude", "logs", "guard.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.write("{broken\n")
        code, text = self.route("audit", sid="s-entry")
        self.assertEqual(code, 0)
        section = text.split(self.GATES, 1)[1]
        self.assertIn("\n- PMO-p2-01：收尾提问去掉 3 次，AskUserQuestion 拒绝 0 次，插队决策放行 1 次", section)
        self.assertIn("\n- Alg-p1-01：收尾提问去掉 1 次，AskUserQuestion 拒绝 1 次，插队决策放行 0 次", section)
        self.assertNotIn("Alg-p2-01：收尾", section)
        self.assertNotIn(self.GATES + "\n- 无", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
