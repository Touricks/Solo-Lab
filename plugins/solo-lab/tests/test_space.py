#!/usr/bin/env python3
"""Tests for space.py and the contract: python3.12 -I -B tests/test_space.py

The fixtures in tests/fixtures/feishu/v2/ were recorded from the reference template space
with read-only commands: space-calls.json holds every meegle answer space.py asked for,
space.json the snapshot they give.
"""
import contextlib
import copy
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
import orgspec  # noqa: E402
import space  # noqa: E402

V2 = os.path.join(ROOT, "tests", "fixtures", "feishu", "v2")


def fixture(name):
    with open(os.path.join(V2, name), encoding="utf-8") as f:
        return json.load(f)


class Replay:
    """space.call answered from the recorded calls; a call nobody recorded fails the test."""

    def __init__(self, edit=None):
        self.calls = {tuple(c["argv"]): c["out"] for c in fixture("space-calls.json")}
        self.used = set()
        self.edit = edit

    def __call__(self, argv, timeout=60):
        key = tuple(argv)
        if key not in self.calls:
            raise AssertionError("没录过这条命令：%s" % " ".join(argv))
        self.used.add(key)
        out = copy.deepcopy(self.calls[key])
        return self.edit(argv, out) if self.edit else out


class SpaceCase(unittest.TestCase):
    def setUp(self):
        self.spec = orgspec.OrgSpec()
        self.contract = orgspec.Contract(self.spec)
        self.snap = fixture("space.json")

    def type_key(self, api_name, snap=None):
        return next(k for k, t in (snap or self.snap)["types"].items() if t["api_name"] == api_name)

    def type(self, api_name, snap=None):
        return (snap or self.snap)["types"][self.type_key(api_name, snap)]

    def field(self, api_name, name):
        return next(f for f in self.type(api_name)["fields"] if f["name"] == name)

    def flow(self, api_name, name):
        return next(f for f in self.type(api_name)["flows"].values() if f["name"] == name)

    def bind(self, old=None, maps=None):
        return space.bind(self.contract, self.snap, old, space.parse_maps(self.contract, maps))

    def check(self):
        binding, problems = self.bind()
        more, notes = space.check(self.contract, self.snap, binding)
        return problems + more, notes


class SnapshotTest(SpaceCase):
    def read(self, edit=None):
        replay = Replay(edit)
        with mock.patch.object(space, "call", replay):
            snap = space.snapshot(space.find_project("research_template"), space.wanted_by(self.contract), workers=1)
        return snap, replay

    def test_recorded_calls_give_the_recorded_snapshot(self):
        snap, replay = self.read()
        self.assertEqual(set(replay.calls) - replay.used, set())          # nothing was recorded in vain
        self.assertEqual(dict(snap, read_at=None), dict(self.snap, read_at=None))
        self.assertEqual(snap["schema"], space.SNAPSHOT_SCHEMA)

    def test_only_types_that_hold_work_items_are_read(self):
        self.assertEqual(sorted(t["api_name"] for t in self.snap["types"].values()),
                         ["cross_request", "decision", "feedback", "research_task", "same_seq"])
        self.assertEqual(sorted(t["api_name"] for t in self.snap["disabled_types"]), ["issue", "sprint", "story", "version"])
        self.assertEqual(sorted(t["api_name"] for t in self.snap["other_types"]), ["chart", "sub_task"])

    def test_the_fields_and_states_of_the_main_line_are_read(self):
        task = self.type("research_task")
        self.assertEqual(len(task["fields"]), 50)
        self.assertEqual([s["name"] for s in task["states"]][-2:], ["得出结论", "决定下一步"])

    def test_a_paged_read_goes_on_while_the_space_reports_more(self):
        pages = {"1": {"list": ["a", "b"], "pagination": {"has_more": True}},
                 "2": {"list": ["c"], "pagination": {"has_more": False}}}
        asked = []

        def call(argv, timeout=60):                                        # meta-fields holds 50 a page
            asked.append(argv[-1])
            return pages[argv[-1]]
        with mock.patch.object(space, "call", call):
            items, last = space.paged(["workitem", "meta-fields"])
        self.assertEqual((items, asked), (["a", "b", "c"], ["1", "2"]))
        self.assertIs(last, pages["2"])

    def test_flows_are_in_order_with_roles_and_required_items(self):
        verify = self.flow("research_task", "正式实验")
        self.assertEqual([n["key"] for n in verify["nodes"]],
                         ["started", "prep_context", "prep_prd_eval", "set_hypothesis", "design_experiment",
                          "build_platform", "run_eval", "conclude", "next_step", "archive"])
        self.assertEqual([n["roles"] for n in verify["nodes"]],
                         [["product"], ["alg"], ["product"], ["alg"], ["alg"], ["infra"], ["rnd"], ["alg"], ["pmo"], ["scribe"]])
        sign = verify["nodes"][3]
        self.assertEqual(sorted(r["name"] for r in sign["required"]), ["假设签字", "总预算", "波动估计", "规格", "规格版本"])
        self.assertEqual(sum(len(n["required"]) for n in verify["nodes"]), 16)
        probe = self.flow("research_task", "探索")
        self.assertEqual([n["name"] for n in probe["nodes"]], ["定方向", "探索", "提出假设", "归档"])
        self.assertEqual([len(f["nodes"]) for t in self.snap["types"].values() for f in t["flows"].values()], [2, 2, 4, 5, 10, 4])

    def test_aliases_come_from_the_new_item_page(self):
        self.assertEqual(self.field("decision", "紧急程度")["alias"], "urgency")
        self.assertNotIn("alias", self.field("decision", "决定内容"))     # not on the new-item page: the CLI cannot read it
        self.assertEqual([c["name"] for c in self.type("feedback")["create_fields"] if c["required"]], ["主文档"])

    def test_a_flow_nobody_used_is_listed_without_nodes(self):
        def no_probe(argv, out):
            if argv[:2] == ["workitem", "query"] and "'探索'" in argv[-1]:
                return {"data": {}, "list": None}
            return out
        snap, _ = self.read(no_probe)
        flows = {f["name"]: f for f in self.type("research_task", snap)["flows"].values()}
        self.assertIsNone(flows["探索"]["nodes"])
        self.assertEqual(len(flows["正式实验"]["nodes"]), 10)
        binding, problems = space.bind(self.contract, snap)
        more, notes = space.check(self.contract, snap, binding)
        self.assertEqual(problems + more, [])
        self.assertTrue(any("探索" in n and "读不到" in n for n in notes))
        self.assertIn("还没有工作项", space.flows_md(snap))

    def test_a_request_inside_a_sequence_starts_with_its_plan(self):
        """1.1.0: a sequence has no middle rank, and the method of a 下发 request is the plan its +0 hands in. The first node
        of 同序列需求 is the sender's, and its form requires the plan; a space that still starts with 开工, the
        assignee's and with nothing required, is named with what to change."""
        self.assertEqual(self.check()[0], [])
        first = self.flow("same_seq", "默认同序列需求类型")["nodes"][0]
        self.assertEqual((first["name"], first["roles"], [r["name"] for r in first["required"]]), ("定方案", ["issuer"], ["方案"]))
        for change in (dict(roles=["assignee"]), dict(required=[])):
            snap = json.loads(json.dumps(self.snap))
            node = next(f for f in self.type("same_seq", snap)["flows"].values())["nodes"][0]
            node.update(name="开工", **change)
            binding, problems = space.bind(self.contract, snap)
            more, _ = space.check(self.contract, snap, binding)
            self.assertEqual(problems + more, [["同序列需求的流程“默认同序列需求类型”的第一个节点“开工”",
                                               "负责角色要是“发出方”，完成时必填“方案”：承接的会话先交方案，发出方确认了才执行",
                                               "报给模板维护者：把这个节点的负责角色改成“发出方”，在它的完成表单里把“方案”设成必填"]], change)
        snap = json.loads(json.dumps(self.snap))                                    # a space from an earlier template: no such field
        t = self.type("same_seq", snap)
        t["fields"] = [f for f in t["fields"] if f["name"] != "方案"]
        binding, problems = space.bind(self.contract, snap)
        self.assertTrue(any("方案" in " ".join(map(str, p)) for p in problems), problems)

    def test_snapshot_keeps_structure_only(self):
        text = json.dumps(self.snap, ensure_ascii=False)
        for word in ("user_key", "7000000000000000001", "owners", "assignee_schedule_list"):
            self.assertNotIn(word, text)

    def test_a_failing_command_stops_the_read(self):
        def broken(argv, timeout=60):
            raise space.SpaceError("meegle workitem meta-types 失败（退出码 1）：未登录")
        with mock.patch.object(space, "call", broken), self.assertRaises(space.SpaceError):
            space.snapshot({"project_key": "pk"})


class BindTest(SpaceCase):
    def test_reference_template_fills_every_slot(self):
        binding, problems = self.bind()
        self.assertEqual(problems, [])
        self.assertEqual(sorted(binding["types"]), sorted(self.contract.types()))
        for wid in self.contract.types():
            for key, kind in (("field_keys", "fields"), ("roles", "roles"), ("states", "states")):
                self.assertEqual(sorted(binding[key][wid]), sorted(self.contract.slots(wid, kind)), (wid, kind))
        self.assertEqual(binding["field_keys"]["decision"]["sign_result"], self.field("decision", "签字结果")["field_key"])
        self.assertEqual(binding["states"]["decision"]["pending"]["name"], "待决定")
        self.assertEqual(binding["roles"]["research_task"]["rnd"], "rnd")

    def test_shared_fields_have_one_key_on_every_type(self):
        binding, _ = self.bind()
        self.assertEqual({binding["field_keys"][w]["main_doc"] for w in self.contract.types()}.__len__(), 1)

    def test_identifiers_win_over_names(self):
        self.type("decision")["name"] = "拍板"
        self.field("decision", "紧急程度")["name"] = "多急"           # its alias on the new-item page still says urgency
        for r in self.type("decision")["roles"]:
            r["role_name"] += "（改过名）"
        binding, problems = self.bind()
        self.assertEqual(problems, [])
        self.assertEqual(binding["types"]["decision"]["name"], "拍板")

    def test_a_renamed_field_is_a_problem_until_it_is_pointed_at(self):
        self.field("decision", "签字结果")["name"] = "批不批"
        binding, problems = self.bind()
        self.assertEqual([p[0] for p in problems], ["决策的“签字结果”"])
        self.assertIn("--map decision.sign_result=", problems[0][2])
        self.assertNotIn("sign_result", binding["field_keys"]["decision"])
        binding, problems = self.bind(maps=["decision.sign_result=批不批"])
        self.assertEqual(problems, [])
        with self.assertRaises(space.SpaceError):
            self.bind(maps=["decision.nope=x"])

    def test_a_type_pointed_at_by_hand_is_read_again(self):
        odd = {"type_key": "k9", "name": "拍板", "api_name": "sign_off"}
        self.assertFalse(space.wanted_by(self.contract)(odd))
        self.assertTrue(space.wanted_by(self.contract, {"decision": "拍板"})(odd))
        self.assertTrue(space.wanted_by(self.contract, old={"types": {"decision": {"type_key": "k9"}}})(odd))
        self.assertFalse(space.wanted_by(self.contract, old={"types": {"decision": {}}})({"name": "别的"}))

    def test_what_was_bound_last_time_survives_a_rename(self):
        old, _ = self.bind()
        self.field("decision", "签字结果")["name"] = "批不批"
        self.type("same_seq")["name"] = "序列内需求"
        self.type("same_seq")["api_name"] = "inner"
        for s in self.type("decision")["states"]:
            s["name"] = s["name"].replace("待决定", "等拍板")
        binding, problems = self.bind(old=old)
        self.assertEqual(problems, [])
        self.assertEqual(binding["states"]["decision"]["pending"]["name"], "等拍板")

    def test_the_decided_state_binds_under_either_name(self):
        """Template 2.2.0 calls the decision's decided state 待执行, 2.1.0 and earlier 已决定 (terms.zh.json
        state_former): binding by name takes either, 待执行 first; a binding made before keeps going by the id."""
        decided = next(s for s in self.type("decision")["states"] if s["name"] == "已决定")
        binding, problems = self.bind()
        self.assertEqual((problems, binding["states"]["decision"]["decided"]), ([], {"id": decided["id"], "name": "已决定"}))
        old = binding
        decided["name"] = "待执行"                                                  # a space made from 2.2.0
        binding, problems = self.bind()
        self.assertEqual((problems, binding["states"]["decision"]["decided"]), ([], {"id": decided["id"], "name": "待执行"}))
        self.assertEqual(self.bind(old=old)[0]["states"]["decision"]["decided"], {"id": decided["id"], "name": "待执行"})
        self.type("decision")["states"].append({"id": "both", "name": "已决定"})       # both names: the present one wins
        self.assertEqual(self.bind()[0]["states"]["decision"]["decided"]["id"], decided["id"])
        self.type("decision")["states"] = [s for s in self.type("decision")["states"] if s["name"] not in ("待执行", "已决定")]
        _, problems = self.bind()
        self.assertEqual([p[:2] for p in problems], [["决策的状态“待执行”", "空间里没有"]])
        self.assertIn("这个类型的流程要有状态“待执行”或“已决定”", problems[0][2])
        self.assertEqual(self.contract.names("states", "decided"), ["待执行", "已决定"])
        self.assertEqual(self.contract.names("states", "pending"), ["待决定"])
        self.assertEqual(self.contract.names("fields", "urgency"), ["紧急程度"])

    def test_missing_field_role_and_state_are_listed(self):
        task = self.type("research_task")
        task["fields"] = [f for f in task["fields"] if f["name"] != "假设签字"]
        task["roles"] = [r for r in task["roles"] if r["role_id"] != "scribe"]
        task["states"] = [s for s in task["states"] if s["name"] != "归档"]
        _, problems = self.bind()
        self.assertEqual(sorted(p[0] for p in problems), ["研究任务的“假设签字”", "研究任务的状态“归档”", "研究任务的角色“书记员”"])
        self.assertTrue(all("报给模板维护者" in p[2] for p in problems))

    def test_a_disabled_type_is_named_as_disabled(self):
        key = self.type_key("feedback")
        t = self.snap["types"].pop(key)
        self.snap["disabled_types"].append({"name": t["name"], "api_name": t["api_name"], "type_key": key})
        binding, problems = self.bind()
        self.assertEqual([(p[0], p[1]) for p in problems], [("反馈", "空间里这个类型是停用的")])
        self.assertNotIn("feedback", binding["types"])

    def test_two_types_of_one_name_need_a_choice(self):
        key = self.type_key("decision")
        twin = copy.deepcopy(self.snap["types"][key])
        twin["api_name"] = "decision_copy"
        self.snap["types"][key]["api_name"] = "decision_old"
        self.snap["types"]["twin"] = twin
        _, problems = self.bind()
        self.assertIn("2 个对得上的类型", problems[0][1])
        _, problems = self.bind(maps=["decision=decision_copy"])
        self.assertEqual(problems, [])

    def test_wrong_field_type_and_missing_option(self):
        self.field("same_seq", "截止时间")["field_type"] = "text"
        relation = self.field("same_seq", "关系")
        relation["options"] = [o for o in relation["options"] if o["name"] != "执行"]
        _, problems = self.bind()
        text = {p[0]: p[1] for p in problems}
        self.assertIn("约定的是日期", text["同序列需求的“截止时间”"])
        self.assertEqual(text["同序列需求的“关系”"], "缺选项：执行")


class CheckTest(SpaceCase):
    def test_reference_template_passes(self):
        self.assertEqual(self.check(), ([], []))

    def test_every_flow_ends_with_the_scribes_node(self):
        self.flow("decision", "默认决策类型")["nodes"][-1]["roles"] = ["researcher"]
        problems, _ = self.check()
        self.assertEqual(len(problems), 1)
        self.assertIn("最后一个节点", problems[0][1])

    def test_a_node_role_outside_the_contract(self):
        self.type("research_task")["roles"].append({"role_id": "quality", "role_name": "质量"})
        self.flow("research_task", "正式实验")["nodes"][6]["roles"] = ["quality"]
        problems, _ = self.check()
        self.assertEqual([p[1] for p in problems], ["负责角色 quality 不在约定里"])

    def test_a_node_has_one_role(self):
        self.flow("same_seq", "默认同序列需求类型")["nodes"][1]["roles"] = ["assignee", "issuer"]
        problems, _ = self.check()
        self.assertIn("要正好一个负责角色", problems[0][1])

    def test_a_new_item_field_outside_the_contract_is_a_note(self):
        self.type("feedback")["create_fields"].append({"field_key": "field_extra", "name": "来源渠道", "required": True})
        problems, notes = self.check()
        self.assertEqual(problems, [])
        self.assertTrue(any("来源渠道" in n and "--set" in n for n in notes))

    def test_the_freeze_needs_its_two_fields_in_a_form(self):
        for node in self.flow("research_task", "正式实验")["nodes"]:
            node["required"] = [r for r in node["required"] if r["name"] != "结论"]
        problems, _ = self.check()
        self.assertEqual([p[0] for p in problems], ["研究任务的“结论”"])
        self.flow("research_task", "探索")["nodes"] = None            # a flow not read yet may be the one that holds it
        self.assertEqual(self.check()[0], [])

    def test_the_freeze_is_not_judged_from_the_probe_flow_alone(self):
        self.flow("research_task", "正式实验")["nodes"] = None
        problems, notes = self.check()
        self.assertEqual(problems, [])
        self.assertTrue(any("正式实验" in n for n in notes))

    def test_a_type_without_a_flow(self):
        self.type("feedback")["flows"] = {}
        problems, _ = self.check()
        self.assertEqual([p[1] for p in problems], ["读不到流程"])


class DiffTest(SpaceCase):
    def test_no_change_no_line(self):
        self.assertEqual(space.diff(self.snap, copy.deepcopy(self.snap)), [])

    def test_changes_are_listed(self):
        new = copy.deepcopy(self.snap)
        task = self.type("research_task", new)
        task["fields"].append({"field_key": "field_new", "name": "复现种子", "field_type": "text"})
        task["fields"] = [f for f in task["fields"] if f["name"] != "去向"]
        next(f for f in task["fields"] if f["name"] == "结论")["options"].pop()
        next(f for f in task["flows"].values() if f["name"] == "正式实验")["nodes"][0]["required"] = []
        del new["types"][self.type_key("feedback")]
        lines = space.diff(self.snap, new)
        for part in ("类型“反馈”没有了", "多了字段 复现种子", "少了字段 去向", "字段“结论”的选项变了", "流程“正式实验”的节点、负责角色或必填项变了"):
            self.assertTrue(any(part in line for line in lines), part)


class TextTest(SpaceCase):
    def test_flow_tables(self):
        text = space.flows_md(self.snap)
        self.assertIn("### 研究任务：正式实验", text)
        self.assertIn("| 4 | 假设评审 | 算法 | ", text)
        self.assertIn("### 决策\n", text)                                    # one flow: the type's name is enough
        binding, _ = self.bind()
        doc = space.to_markdown(self.snap, self.contract, binding)
        self.assertIn("| 研究任务 | 研究任务（`%s`） | 10 / 10 | 7 / 7 | 1 / 1 |" % self.type_key("research_task"), doc)
        self.assertIn("新建时必填：紧急程度、发出方、序列、主文档", doc)


class CliTest(SpaceCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="solo-lab-space-")
        os.makedirs(os.path.join(self.tmp, "feishu"))
        self.put("ids.json", {"schema": "solo-lab/ids-v2", "meegle": {"project_key": self.snap["project_key"]}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name, obj):
        with open(os.path.join(self.tmp, "feishu", name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def bound(self, snap=None):
        """The binding as `space.py feishu --apply` writes it."""
        binding, _ = space.bind(self.contract, snap or self.snap)
        ids = space.apply_binding(json.loads(self.get("ids.json")), self.contract, binding)
        self.put("ids.json", ids)
        return ids

    def get(self, name):
        with open(os.path.join(self.tmp, "feishu", name), encoding="utf-8") as f:
            return f.read()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = space.main(list(argv))
        return code, out.getvalue()

    def test_snapshot_bind_check(self):
        with mock.patch.object(space, "call", Replay()):
            code, out = self.run_cli("snapshot", "--target", self.tmp)
            self.assertEqual(code, 0)
            self.assertIn("这是预览", out)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "feishu", "space.json")))
            self.assertEqual(self.run_cli("snapshot", "--target", self.tmp, "--apply")[0], 0)
        code, out = self.run_cli("bind", "--target", self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("space.py feishu --target <项目目录> --live", out)
        self.assertNotIn("types", json.loads(self.get("ids.json"))["meegle"])   # only space.py feishu writes the binding
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("bind", "--target", self.tmp, "--apply")
        ids = self.bound()
        self.assertEqual((ids["schema"], ids["meegle"]["contract_version"]), ("solo-lab/ids-v2", self.contract.version))
        self.assertEqual(sorted(ids["meegle"]["types"]), sorted(self.contract.types()))
        code, out = self.run_cli("check", "--target", self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("约定核对（contract_version 2）：通过", out)
        self.assertIn("研究任务：正式实验", self.run_cli("show", "--target", self.tmp)[1])

    def test_check_names_what_is_missing(self):
        task = self.type("research_task")
        task["fields"] = [f for f in task["fields"] if f["name"] != "下一步签字"]
        self.put("space.json", self.snap)
        code, out = self.run_cli("bind", "--target", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("研究任务的“下一步签字”", out)
        self.bound()
        self.assertEqual(self.run_cli("check", "--target", self.tmp)[0], 1)

    def test_a_binding_made_for_another_contract_version(self):
        self.put("space.json", self.snap)
        ids = self.bound()
        ids["meegle"]["contract_version"] = 0
        self.put("ids.json", ids)
        code, out = self.run_cli("check", "--target", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("绑定按约定第 0 版做的", out)

    def test_check_says_when_the_binding_is_behind_the_snapshot(self):
        ids = self.bound()
        next(s for s in self.type("decision")["states"] if s["name"] == "待决定")["name"] = "等研究员"
        del ids["meegle"]["field_keys"]["research_task"]["conclusion"]
        self.put("space.json", self.snap)
        self.put("ids.json", ids)
        code, out = self.run_cli("check", "--target", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("空间里现在是 “等研究员”，绑定记的是 “待决定”", out)
        self.assertIn("研究任务的字段“结论”", out)
        self.assertIn("空间里有，绑定里还没记", out)
        self.assertIn("space.py feishu --target <项目目录> --live", out)

    def test_export_puts_any_space_into_a_file_outside_the_project(self):
        path = os.path.join(self.tmp, "template.json")
        with mock.patch.object(space, "call", Replay()):
            code, out = self.run_cli("export", "--space", "research_template", "--out", path)
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["types"], self.snap["types"])
        self.assertIn("show --snapshot", out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "feishu")), ["ids.json"])

    def test_check_a_space_by_name_writes_nothing(self):
        before = sorted(os.listdir(os.path.join(self.tmp, "feishu")))
        with mock.patch.object(space, "call", Replay()):
            code, out = self.run_cli("check", "--space", "research_template")
        self.assertEqual(code, 0)
        self.assertIn("没写任何文件", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "feishu"))), before)

    def test_check_passes_a_template_under_either_name_of_the_decided_state(self):
        """space.py check --space on the template space before and after 2.2.0 renamed 已决定 to 待执行."""
        def renamed(argv, out):
            if argv[:2] == ["workitem", "meta-fields"] and argv[5] == self.type_key("decision"):
                status = [f for f in out.get("list") or [] if f.get("field_key") == "work_item_status"]
                for o in (status[0].get("option") or []) if status else []:
                    if o.get("option_name") == "已决定":
                        o["option_name"] = "待执行"
            return out
        for edit, name in ((None, "已决定"), (renamed, "待执行")):
            with mock.patch.object(space, "call", Replay(edit)):
                code, out = self.run_cli("check", "--space", "research_template")
                snap = space.snapshot(space.find_project("research_template"), space.wanted_by(self.contract), workers=1)
            self.assertEqual(code, 0, out)
            self.assertIn("约定核对（contract_version 2）：通过", out)
            self.assertIn(name, [s["name"] for s in self.type("decision", snap)["states"]])

    def test_commands_say_what_to_run_first(self):
        code, out = self.run_cli("check", "--target", self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("space.py snapshot", out)
        self.put("ids.json", {})
        code, out = self.run_cli("snapshot", "--target", self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("project_key", out)


class ContractTest(unittest.TestCase):
    def contract(self, change):
        c = orgspec.Contract(orgspec.OrgSpec())
        change(c.data)
        return c

    def test_the_contract_is_consistent(self):
        self.assertEqual(orgspec.Contract(orgspec.OrgSpec()).validate(), [])

    def test_validate_catches_mistakes(self):
        cases = [
            (lambda d: d["types"]["same_seq"].update(main_line=True), "主线类型"),
            (lambda d: d["types"]["feedback"]["fields"].append("nope"), "feedback 的 fields 槽位 nope 没有定义"),
            (lambda d: d["fields"]["sender"].update(type="rich"), "字段槽位 sender 的类型 rich 不认识"),
            (lambda d: d["roles"]["assignee"]["field"].pop("cross_request"), "角色槽位 assignee 在 cross_request 上"),
            (lambda d: d["types"]["decision"]["states"].remove("decided"), "签字关"),
            (lambda d: d["flow"]["freeze"].update(sealed_by="conclusion"), "冻结核对"),
            (lambda d: d["types"]["feedback"]["roles"].remove("scribe"), "feedback 要有归档节点的角色槽位"),
            (lambda d: d["flow"]["rollback_needs_decision"].append("project"), "不是类型槽位"),
        ]
        for change, part in cases:
            problems = self.contract(change).validate()
            self.assertTrue(any(part in p for p in problems), (part, problems))

    def test_the_template_is_1_1_0(self):
        """A space is made from template 1.1.0 (plugin 1.1.0 on; from this version the template carries the version
        of the plugin it is first used with): 2.2.0 with the research task's flows, six steps, three fields and one
        option renamed. It is published as a template of its own (Solo-Lab Template v3 in the template centre); 2.2.0
        keeps its link for plugins 1.0.5 to 1.0.12, 2.1.0 for plugin 1.0.4 and earlier. A research-task sheet plugin
        1.1.0 writes names the flow 正式实验: a space made from 2.2.0 calls it 验证 and refuses the sheet, so the
        contract never goes back to that link."""
        c = orgspec.Contract(orgspec.OrgSpec())
        self.assertEqual(c.template, {"name": "Solo-Lab Template v3", "version": "1.1.0",
                                      "url": "https://project.feishu.cn/templates/detail/e66f7ed7cc364a60942ca1e719332625"})
        self.assertNotIn("67ba9ba510e046fe9bd35be99b883f26", c.template["url"])

    def test_former_state_names_are_checked(self):
        spec = orgspec.OrgSpec()
        self.assertEqual(spec.terms["state"]["decided"], "待执行")
        self.assertEqual(spec.terms["state_former"], {"decided": ["已决定"]})
        spec.terms["state_former"] = {"nope": ["x"], "decided": "已决定"}
        problems = orgspec.Contract(spec).validate()
        self.assertTrue(any("state_former 里的状态槽位 nope 没有定义" in p for p in problems), problems)
        self.assertTrue(any("state_former 里 decided 的旧名字要写成字符串列表" in p for p in problems), problems)

    def test_names_and_options(self):
        c = orgspec.Contract(orgspec.OrgSpec())
        self.assertEqual(c.main_line(), "research_task")
        self.assertEqual((c.name("types", "research_task"), c.name("roles", "rnd"), c.name("roles", "issuer"),
                          c.name("states", "verifying")), ("研究任务", "工程", "发出方", "待验收"))
        self.assertEqual(c.option_names("relation"), ["下发", "执行"])
        self.assertEqual(len(c.option_names("sequence")), 7)
        self.assertEqual(c.option_names("urgency"), [])
        self.assertEqual(c.signatures(), ["hypothesis_signoff", "next_step_signoff"])


if __name__ == "__main__":
    unittest.main()
