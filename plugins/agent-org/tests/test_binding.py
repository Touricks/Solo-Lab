#!/usr/bin/env python3
"""Tests for `setup.py feishu` and `setup.py template` on a node-flow space:
python3 -I -B tests/test_binding.py

The space is replayed from tests/fixtures/feishu/v2/space-calls.json; nothing calls Feishu.
"""
import contextlib
import datetime
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
        self.spaces, self.made = [], []                 # knowledge spaces the account sees, and the ones made here

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
        if cmd[:3] == ["lark-cli", "wiki", "+space-list"]:
            return 0, json.dumps({"data": {"items": self.spaces, "has_more": False}}), ""
        if cmd[:3] == ["lark-cli", "wiki", "+space-create"]:
            new = {"name": cmd[cmd.index("--name") + 1], "space_id": "sp_%d" % (len(self.spaces) + 1)}
            self.spaces.append(new)
            self.made.append(new["name"])
            return 0, json.dumps({"data": {"space": new}}), ""
        return 1, "", "tests do not call %s" % " ".join(cmd[:3])

    def feishu(self, *args, edit=None, **kw):
        out = io.StringIO()
        with mock.patch.object(setup, "run", self.cli), mock.patch.object(space, "call", Replay(edit)), contextlib.redirect_stdout(out):
            code = setup.feishu(self.tmp, *args, **kw)
        return code, out.getvalue()


class FeishuTest(BindingCase):
    def test_preview_writes_nothing_and_apply_writes_the_binding(self):
        code, text = self.feishu([], True, False)
        self.assertEqual(code, 0)                                    # nothing to fix: 0 in a preview and when written
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
        self.assertEqual(code, 1)                                    # an initialisation stops here
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

    def test_a_space_that_is_not_there_is_made_from_the_template(self):
        self.projects = [OLD]
        with self.assertRaises(setup.SetupError) as e:
            self.feishu([], True, True)
        self.assertIn("没有叫“research_template”的飞书项目空间", str(e.exception))
        self.assertIn(orgspec.Contract(orgspec.OrgSpec()).template["url"], str(e.exception))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "feishu")), [])
        self.put("spec/task.json", {"name": "t", "feishu": {"meegle_space": "research_template",
                                                           "template_url": "https://example.com/templates/detail/abc"}})
        with self.assertRaises(setup.SetupError) as e:
            self.feishu([], True, False)
        self.assertIn("打开模板 https://example.com/templates/detail/abc", str(e.exception))   # the task file's own template

    def test_the_knowledge_space_is_looked_for_on_every_page_then_made(self):
        _, text = self.feishu([], True, False)
        self.assertIn("没有叫“t”的；加 --apply 时新建一个", text)                 # named after the project
        self.assertEqual(self.made, [])                                         # a preview makes nothing
        _, text = self.feishu([], True, True)
        self.assertEqual(self.made, ["t"])
        self.assertEqual(json.loads(self.read("feishu/ids.json"))["wiki"]["space_id"], "sp_1")
        self.feishu([], True, True)
        self.assertEqual(self.made, ["t"])                                      # the next run has it

    def test_a_knowledge_space_that_exists_is_used_and_two_of_a_name_are_not_chosen(self):
        self.spaces = [{"name": "别的", "space_id": "sp_x"}, {"name": "t", "space_id": "sp_t"}]
        self.feishu([], True, True)
        self.assertEqual((self.made, json.loads(self.read("feishu/ids.json"))["wiki"]["space_id"]), ([], "sp_t"))
        self.put("spec/task.json", {"name": "t", "feishu": {"meegle_space": "research_template", "wiki_space": "同名"}})
        self.spaces += [{"name": "同名", "space_id": "sp_a"}, {"name": "同名", "space_id": "sp_b"}]
        _, text = self.feishu([], True, True)
        self.assertIn("有 2 个知识空间叫“同名”（space_id：sp_a、sp_b）", text)
        self.assertEqual(self.made, [])

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


class LoginTest(unittest.TestCase):
    """setup.py login with both tools answered here: what they print is as recorded on 2026-11-12."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-org-login-")
        self.logged = {"lark-cli": False, "meegle": False}
        self.scanned, self.brand, self.calls = False, "feishu", []
        self.patches = [mock.patch.object(setup, "run", self.tools),
                        mock.patch.object(setup, "login_state", lambda: (self.tmp, os.path.join(self.tmp, "pending.json")))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tools(self, cmd, timeout=30):
        self.calls.append(cmd)
        if cmd[:3] == ["lark-cli", "auth", "status"]:
            on = self.logged["lark-cli"]
            user = {"available": on, "status": "ready" if on else "missing", "tokenStatus": "ready" if on else "expired",
                    "refreshExpiresAt": "2020-01-01T00:00:00+00:00"}
            return 0, json.dumps({"brand": self.brand, "identities": {"user": user}}), ""
        if cmd[:3] == ["meegle", "auth", "status"]:
            return (0 if self.logged["meegle"] else 1), "{}", ""
        if cmd[:3] == ["lark-cli", "auth", "login"]:
            if "--no-wait" in cmd:
                return 0, json.dumps({"device_code": "DEV-LARK-SECRET", "expires_in": 600, "hint": "…",
                                      "verification_url": "https://accounts.example/verify?user_code=ABCD"}), ""
            self.logged["lark-cli"] = self.scanned
            return 0, "", ""
        if cmd[:3] == ["meegle", "auth", "login"]:
            if "init" in cmd:
                return 0, json.dumps({"client_id": "CID-SECRET", "device_code": "DEV-MEEGLE-SECRET", "expires_in": 1800, "interval": 5,
                                      "user_code": "WXYZ", "verification_uri": "https://project.example/b",
                                      "verification_uri_complete": "https://project.example/b?user_code=WXYZ"}), ""
            self.logged["meegle"] = self.scanned
            return 0, "", ""
        if cmd[:3] == ["lark-cli", "auth", "qrcode"]:
            open(cmd[cmd.index("-o") + 1], "w").close()
            return 0, "", ""
        return 1, "", "tests do not call %s" % " ".join(cmd[:3])

    def login(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.login(**kw)
        return code, out.getvalue()

    def test_a_code_to_scan_and_a_link_but_never_the_device_code(self):
        code, text = self.login()
        self.assertEqual(code, 0)
        for cli, link in (("lark-cli", "https://accounts.example/verify?user_code=ABCD"),
                          ("meegle", "https://project.example/b?user_code=WXYZ")):
            self.assertIn("二维码图片：%s" % os.path.join(self.tmp, cli + ".png"), text)
            self.assertIn("链接：" + link, text)
        for secret in ("DEV-LARK-SECRET", "DEV-MEEGLE-SECRET", "CID-SECRET"):
            self.assertNotIn(secret, text)
        pending = os.path.join(self.tmp, "pending.json")
        self.assertEqual(os.stat(pending).st_mode & 0o777, 0o600)
        self.assertIn("project.feishu.cn", next(c for c in self.calls if c[:3] == ["meegle", "auth", "login"]))

    def test_wait_reads_the_login_back_and_clears_the_codes(self):
        self.login()
        code, text = self.login(wait=True, timeout=5)
        self.assertEqual(code, 1)                                     # nobody scanned yet
        self.assertIn("lark-cli：还没授权", text)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "pending.json")))
        self.scanned = True
        code, text = self.login(wait=True, timeout=5)
        self.assertEqual(code, 0)
        self.assertIn("lark-cli：已登录。", text)
        self.assertIn("meegle：已登录。", text)
        self.assertEqual(os.listdir(self.tmp), [])                    # codes and pictures are gone
        poll = next(c for c in self.calls if c[:3] == ["meegle", "auth", "login"] and "poll" in c)
        self.assertEqual(poll[poll.index("--device-code-value") + 1], "DEV-MEEGLE-SECRET")

    def test_only_what_is_logged_out_is_logged_in(self):
        self.logged["meegle"], self.brand = True, "lark"
        code, text = self.login()
        self.assertNotIn("meegle：", text)
        self.logged["lark-cli"] = True
        code, text = self.login()
        self.assertIn("都已登录", text)
        self.assertEqual(self.login(wait=True)[0], 2)                 # nothing is waiting any more

    def test_a_token_that_can_still_be_renewed_counts_as_logged_in(self):
        later = (datetime.datetime.now().astimezone() + datetime.timedelta(days=3)).isoformat()
        ready = lambda user: setup.lark_user_ready({"identities": {"user": user}})[0]
        self.assertTrue(ready({"available": True}))
        self.assertTrue(ready({"available": False, "status": "needs_refresh", "tokenStatus": "expired", "refreshExpiresAt": later}))
        self.assertFalse(ready({"available": False, "status": "missing", "tokenStatus": "expired", "refreshExpiresAt": later}))
        self.assertFalse(ready({"available": False, "status": "needs_refresh", "refreshExpiresAt": "2020-01-01T00:00:00+00:00"}))
        self.assertFalse(setup.lark_user_ready({})[0])


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-org-env-")
        os.makedirs(os.path.join(self.tmp, "feishu"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ids(self, **wiki):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump({"meegle": {"project_key": "pk", "simple_name": "codeopt"}, "wiki": wiki}, f)

    def env(self, apply=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.env_cmd(self.tmp, apply)
        return code, out.getvalue()

    def read(self):
        with open(os.path.join(self.tmp, ".env"), encoding="utf-8") as f:
            return f.read()

    def test_the_links_wait_for_the_tenant_domain(self):
        self.ids(space_id="sp_1")
        code, text = self.env(True)
        self.assertEqual(code, 1)
        self.assertIn("租户域名", text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".env")))
        self.assertTrue(setup.env_gaps(self.tmp))

    def test_a_new_file_holds_the_two_links(self):
        self.ids(space_id="sp_1", tenant_domain="example.feishu.cn")
        code, text = self.env()
        self.assertIn("这是预览", text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".env")))
        self.assertEqual(self.env(True)[0], 0)
        self.assertEqual(self.read(), "# agent-org：由 feishu/ids.json 生成\nFEISHU_PROJECT_URL=https://project.feishu.cn/codeopt\n"
                                      "FEISHU_WIKI_URL=https://example.feishu.cn/wiki/space/sp_1\n")
        self.assertEqual(os.stat(os.path.join(self.tmp, ".env")).st_mode & 0o777, 0o600)
        self.assertEqual(setup.env_gaps(self.tmp), [])
        self.assertIn("没有改", self.env(True)[1])

    def test_other_lines_stay_and_are_never_shown(self):
        self.ids(space_id="sp_1", tenant_domain="example.feishu.cn")
        mine = "# 我自己的\nSOME_PASSWORD=hunter2\nFEISHU_WIKI_URL=https://old.example/wiki/space/x\n\nOTHER=1\n"
        with open(os.path.join(self.tmp, ".env"), "w", encoding="utf-8") as f:
            f.write(mine)
        code, text = self.env(True)
        self.assertEqual(code, 0)
        for word in ("SOME_PASSWORD", "hunter2", "OTHER", "old.example"):
            self.assertNotIn(word, text)
        self.assertTrue(self.read().startswith("# 我自己的\nSOME_PASSWORD=hunter2\n\nOTHER=1\n\n# agent-org：由 feishu/ids.json 生成\n"))
        self.assertEqual(self.read().count("FEISHU_WIKI_URL="), 1)
        self.ids(space_id="sp_2", tenant_domain="example.feishu.cn")
        self.assertTrue(setup.env_gaps(self.tmp))                     # the binding moved on
        self.env(True)
        self.assertIn("/wiki/space/sp_2\n", self.read())
        self.assertIn("SOME_PASSWORD=hunter2\n", self.read())


if __name__ == "__main__":
    unittest.main()
