#!/usr/bin/env python3
"""Tests for `setup.py feishu` and `setup.py template` on a node-flow space:
python3 -I -B tests/test_binding.py

The space is replayed from tests/fixtures/feishu/v2/space-calls.json; nothing calls Feishu.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import orgspec  # noqa: E402
import setup  # noqa: E402
import space  # noqa: E402
from test_space import Replay, fixture  # noqa: E402

SNAP = fixture("space.json")
HERE = {"name": "research_template", "simple_name": SNAP["simple_name"], "project_key": SNAP["project_key"]}
OLD = {"name": "旧空间", "simple_name": "oldspace", "project_key": "pk_old"}


class BindingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-org-binding-")
        for d in ("feishu", "spec", "org"):
            os.makedirs(os.path.join(self.tmp, d))
        self.put("spec/task.json", {"name": "t", "feishu": {"meegle_space": "research_template"}})
        self.projects, self.rows = [HERE, OLD], {"data": {}, "list": None}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, rel, obj):
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def has(self, rel):
        return os.path.exists(os.path.join(self.tmp, rel))

    def cli(self, cmd, timeout=60):
        """setup.run: project search, whoami and the queries on a space the project leaves."""
        if cmd[:3] == ["meegle", "project", "search"]:
            return 0, json.dumps({"projects": self.projects}), ""
        if cmd[:2] == ["lark-cli", "whoami"]:
            return 0, json.dumps({"identity": "user", "onBehalfOf": {"openId": "ou_researcher"}}), ""
        if cmd[:3] == ["meegle", "workitem", "query"]:
            return 0, json.dumps(self.rows), ""
        return 1, "", "tests do not call %s" % " ".join(cmd[:3])

    def feishu(self, *args, edit=None, **kw):
        out = io.StringIO()
        with mock.patch.object(setup, "run", self.cli), mock.patch.object(space, "call", Replay(edit)), contextlib.redirect_stdout(out):
            code = setup.feishu(self.tmp, *args, **kw)
        return code, out.getvalue()


class FeishuTest(BindingCase):
    def test_preview_writes_nothing_and_apply_writes_the_binding(self):
        code, text = self.feishu([], True, False)
        self.assertEqual(code, 0)
        self.assertIn("约定核对（contract_version 1）：通过", text)
        self.assertIn("这是预览", text)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "feishu")), [])
        code, text = self.feishu([], True, True)
        ids = json.loads(self.read("feishu/ids.json"))
        m = ids["meegle"]
        self.assertEqual((ids["schema"], m["contract_version"], m["project_key"], m["space_name"]),
                         ("agent-org/ids-v2", 1, SNAP["project_key"], "research_template"))
        self.assertTrue(m["bound_at"])
        self.assertEqual(sorted(m["types"]), sorted(orgspec.Contract(orgspec.OrgSpec()).types()))
        self.assertEqual(ids["lark"]["researcher_open_id"], "ou_researcher")
        self.assertEqual(json.loads(self.read("feishu/space.json"))["types"], SNAP["types"])
        self.assertIn("| 4 | 确定假设条件 | 算法 | ", self.read("feishu/space.md"))
        doc = self.read("feishu/binding.md")
        self.assertIn("## 要处理的问题\n\n无。", doc)
        self.assertIn("ledger.py ensure-node all", doc)                # the knowledge base is still to be built
        phases = self.read("org/phases.md")
        self.assertIn("### 研究任务：验证", phases)
        self.assertIn("### 研究任务：试探", phases)
        self.assertNotIn("### 决策", phases)                           # the main line only
        self.assertIn("回退由研究员决定", phases)
        self.assertFalse(self.has("feishu/migration.md") or self.has("feishu/field-dictionary.json"))

    def test_a_second_run_changes_nothing_and_needs_no_live_read(self):
        self.feishu([], True, True)
        before = {n: self.read("feishu/" + n) for n in ("space.json", "space.md", "binding.md")}
        with mock.patch.object(space, "call", side_effect=AssertionError("read the space")):
            out = io.StringIO()
            with mock.patch.object(setup, "run", self.cli), contextlib.redirect_stdout(out):
                self.assertEqual(setup.feishu(self.tmp, [], False, True), 0)
        self.assertEqual(before, {n: self.read("feishu/" + n) for n in before})
        self.assertIn("[相同] feishu/space.json", out.getvalue())

    def test_what_the_space_lacks_goes_to_the_template_maintainer(self):
        def no_signature(argv, out):
            if argv[:2] == ["workitem", "meta-fields"]:
                out["list"] = [f for f in out.get("list") or [] if f.get("field_name") != "下一步签字"]
            return out
        code, text = self.feishu([], True, True, edit=no_signature)
        self.assertIn("1 个问题，见 feishu/binding.md", text)
        doc = self.read("feishu/binding.md")
        self.assertIn("研究任务的“下一步签字”", doc)
        self.assertIn("报给模板维护者", doc)
        m = json.loads(self.read("feishu/ids.json"))["meegle"]
        self.assertNotIn("next_step_signoff", m["field_keys"]["research_task"])
        self.assertTrue(m["bound_at"])                                 # what is there is bound; the gap is listed

    def test_a_field_under_another_name_is_pointed_at(self):
        def renamed(argv, out):
            if argv[:2] == ["workitem", "meta-fields"]:
                for f in out.get("list") or []:
                    if f.get("field_name") == "签字结果":
                        f["field_name"] = "批不批"
            return out
        _, text = self.feishu([], True, False, edit=renamed)
        self.assertIn("--map decision.sign_result=", text)
        _, text = self.feishu(["decision.sign_result=批不批"], True, False, edit=renamed)
        self.assertIn("约定核对（contract_version 1）：通过", text)
        with self.assertRaises(setup.SetupError):
            self.feishu(["decision.nope=x"], True, False)

    def test_a_type_under_another_name_stays_bound_on_the_next_read(self):
        def renamed(argv, out):
            if argv[:2] == ["workitem", "meta-types"]:
                for t in out.get("list") or []:
                    if t.get("api_name") == "decision":
                        t["api_name"], t["name"] = "sign_off", "拍板"
            return out
        _, text = self.feishu([], True, False, edit=renamed)
        self.assertIn("--map decision=", text)
        code, text = self.feishu(["decision=拍板"], True, True, edit=renamed)
        self.assertIn("约定核对（contract_version 1）：通过", text)
        code, text = self.feishu([], True, True, edit=renamed)                      # the next read, nothing pointed at
        self.assertIn("约定核对（contract_version 1）：通过", text)
        self.assertEqual(json.loads(self.read("feishu/ids.json"))["meegle"]["types"]["decision"]["name"], "拍板")

    def test_every_session_may_read_the_space(self):
        for cmd in ("snapshot", "bind", "check", "show"):
            self.assertIn("Bash(python3 -I .claude/agent-org/bin/space.py %s *)" % cmd, setup.READ_ALLOW)
        self.assertEqual([a for a in setup.READ_ALLOW if "export" in a], [])        # writes a file wherever it is told to

    def test_a_space_without_the_types_is_recorded_but_not_bound(self):
        def blank(argv, out):
            if argv[:2] == ["workitem", "meta-types"]:
                out["list"] = [t for t in out["list"] if t["api_name"] in ("story", "issue", "chart", "sub_task")]
            return out
        code, text = self.feishu([], True, True, edit=blank)
        self.assertIn("没有一种类型对得上插件的约定", text)
        self.assertIn("没写绑定时间", text)
        m = json.loads(self.read("feishu/ids.json"))["meegle"]
        self.assertEqual((m["project_key"], m.get("bound_at"), m["types"]), (SNAP["project_key"], None, {}))
        self.assertFalse(self.has("org/phases.md"))

    def test_a_change_of_structure_is_named(self):
        self.feishu([], True, True)

        def more(argv, out):
            if argv[:2] == ["workitem", "meta-fields"] and argv[5] == next(k for k, t in SNAP["types"].items() if t["api_name"] == "feedback"):
                out["list"].append({"field_key": "field_extra", "field_name": "来源渠道", "field_type": "text"})
            return out
        _, text = self.feishu([], True, False, edit=more)
        self.assertIn("变化：反馈：多了字段 来源渠道", text)
        self.assertIn("空间结构和上次读到的不一样", text)

    def test_a_binding_to_a_state_flow_space_is_set_aside(self):
        self.put("feishu/ids.json", {"schema": "agent-org/ids-v1", "meegle": {"project_key": SNAP["project_key"], "types": {"project": {"type_key": "x"}}}})
        _, text = self.feishu([], True, True)
        self.assertIn("这一版只认用节点流程的空间", text)
        self.assertTrue(self.has("feishu/ids.legacy.json"))
        self.assertNotIn("project", json.loads(self.read("feishu/ids.json"))["meegle"]["types"])

    def test_the_task_must_name_exactly_one_space(self):
        self.projects = [HERE, dict(HERE, project_key="pk_twin", simple_name="twin")]
        with self.assertRaises(setup.SetupError) as e:
            self.feishu([], True, False)
        self.assertIn("找到 2 个飞书项目空间", str(e.exception))

    def test_another_space_in_the_task_is_a_switch(self):
        self.feishu([], True, True)
        ids = json.loads(self.read("feishu/ids.json"))
        ids["meegle"].update(project_key="pk_old", simple_name="oldspace", space_name="旧空间")
        self.put("feishu/ids.json", ids)
        self.rows = {"data": {"1": [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": 2101}},
                                                         {"key": "name", "value": {"string_value": "没办完的需求"}}]}]}, "list": None}
        _, text = self.feishu([], True, False)
        self.assertIn("换空间：旧空间“旧空间”", text)
        self.assertIn("## 换空间", text)
        with self.assertRaises(setup.SetupError) as e:
            self.feishu([], True, True)
        self.assertIn("--leave-old-items", str(e.exception))
        self.assertEqual(json.loads(self.read("feishu/ids.json"))["meegle"]["project_key"], "pk_old")       # nothing was written
        self.feishu([], True, True, True)
        m = json.loads(self.read("feishu/ids.json"))["meegle"]
        self.assertEqual((m["project_key"], m["previous_spaces"][0]["project_key"]), (SNAP["project_key"], "pk_old"))

    def test_without_a_snapshot_there_is_nothing_to_bind(self):
        code, text = self.feishu([], False, False)
        self.assertIn("还没读过空间，绑定不了：加 --live 从空间读", text)
        with self.assertRaises(setup.SetupError):
            self.feishu(["decision=拍板"], False, False)


class TemplateTest(BindingCase):
    def template(self, name, edit=None):
        out = io.StringIO()
        with mock.patch.object(setup, "run", self.cli), mock.patch.object(space, "call", Replay(edit)), contextlib.redirect_stdout(out):
            code = setup.check_template(None, name)
        return code, out.getvalue()

    def test_the_reference_template_passes(self):
        code, text = self.template("research_template")
        self.assertEqual(code, 0)
        self.assertIn("只读了飞书，没写任何文件", text)
        self.assertIn("这个空间符合约定", text)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "feishu")), [])

    def test_work_items_in_a_template_are_named_not_refused(self):
        self.rows = {"data": {"1": [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": 2101}}]}]}, "list": None}
        code, text = self.template("research_template")
        self.assertEqual(code, 0)
        self.assertIn("空间里有工作项", text)

    def test_a_template_with_a_gap_fails(self):
        def no_scribe(argv, out):
            if argv[:2] == ["workitem", "meta-roles"]:
                out["list"] = [r for r in out["list"] if r["role_id"] != "scribe"]
            return out
        code, text = self.template("research_template", no_scribe)
        self.assertEqual(code, 1)
        self.assertIn("的角色“书记员”", text)

    def test_the_space_has_to_be_named(self):
        with self.assertRaises(setup.SetupError):
            self.template(None)


if __name__ == "__main__":
    unittest.main()
