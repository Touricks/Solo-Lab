#!/usr/bin/env python3
"""Tests for ledger.py on node flows: python3 -I -B tests/test_ledger.py

The space is tests/fake_space.py: the structure of the reference template, behaving as the
real space was seen to. Nothing here calls Feishu.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fake_space  # noqa: E402
import ledger  # noqa: E402
import orgspec  # noqa: E402

EXAMPLES = os.path.join(ROOT, "skills", "packages", "references", "examples.md")
ASSIGN, DELIVERY, CROSS, EXECUTE, EXECUTE_DELIVERY, DECISION, DECIDED = 0, 1, 2, 5, 6, 7, 8   # blocks of examples.md
SHEET = """# 研究任务单：复现并测量基线

- 流程：%s
- 发出方：%s
- 关联工作项：待建

## 目标
在公开评测集上复现基线，测出分数和波动。

## 成功标准
复现分数和公开数字相差 0.5 个点以内。
"""
FEEDBACK = """# 反馈：基线要和人工调参比

- 发出方：导师（研究员转述）
- 关联工作项：待建

## 原话
基线只和默认配置比不够，要加上人工调了一周的版本。
"""
LINK = "https://example.feishu.cn/wiki/wikDoc"
STEPS = [   # the 验证 flow: who asks for each step, and what its form requires
    ("Product-p2-01", ["成功标准=复现分数和公开数字相差 0.5 个点以内"]),
    ("Alg-p2-01", ["上下文文档=" + LINK]),
    ("Product-p2-01", ["需求文档=" + LINK, "评测集版本=evalset-v1"]),
    ("Alg-p2-01", ["规格=" + LINK, "规格版本=spec-v1", "总预算=200", "波动估计=基线重复 3 次，标准差 0.2"]),
    ("Alg-p2-01", ["实验方案=" + LINK, "实验轮次=1"]),
    ("Infra-p2-01", ["冒烟运行 ID=smoke-0001"]),
    ("RnD-p2-01", ["运行 ID=run-0001,run-0002", "试过的变体数=2"]),
    ("Alg-p2-01", ["结论=成立", "结论文档=" + LINK]),
    ("PMO-p2-01", []),
]


def example(n):
    with open(EXAMPLES, encoding="utf-8") as f:
        return re.findall(r"```markdown\n(.*?)\n```", f.read(), re.S)[n]


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-org-ledger-")
        fake_space.install(self.tmp)
        os.makedirs(os.path.join(self.tmp, "packages", "drafts", "alg"))
        self.fake = fake_space.FakeSpace()
        self.spec = orgspec.OrgSpec()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lg(self, mode="apply", replies=()):
        return fake_space.SimLedger(self.tmp, mode, self.fake, replies)

    def write(self, text, name="d.md"):
        path = os.path.join(self.tmp, "packages", "drafts", "alg", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def draft(self, block, name="d.md", edit=None):
        """An example package as a new draft: its work item is still to be created."""
        text = re.sub(r"^- 关联工作项：#\d+[^；\n]*", "- 关联工作项：待建", example(block), flags=re.M)
        text = re.sub(r"^- 所属研究任务：.*\n", "", text, flags=re.M)
        return self.write(edit(text) if edit else text, name)

    def out(self, fn, *args, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*args, **kw)
        return res, buf.getvalue()

    def quiet(self, fn, *args, **kw):
        return self.out(fn, *args, **kw)[0]

    def fails(self, fn, *args, **kw):
        with self.assertRaises(ledger.LedgerError) as e, contextlib.redirect_stdout(io.StringIO()):
            fn(*args, **kw)
        return str(e.exception)

    def task(self, flow="验证", sender="产品 +2（Product-p2-01）", name="task.md"):
        return self.quiet(ledger.register, self.lg(), self.write(SHEET % (flow, sender), name))

    def state(self, item_id):
        return self.fake.state_of(self.fake.item(item_id))

    def value(self, item_id, wid, ref):
        return self.fake.item(item_id)["fields"].get(self.lg().field(wid, ref)["field_key"], "")

    def decision(self, task_id=None, result="批准", decided=True):
        """A decision work item, as the scribe leaves it after the researcher decided."""
        lg = self.lg()
        fields = [{"field_key": "name", "field_value": "决策包（PMO-p2-01）：签字"}, ledger.flow_entry(lg, "decision"),
                  lg.entry("decision", "sequence", "项目管理"), lg.entry("decision", "sender", "PMO-p2-01"),
                  lg.entry("decision", "main_doc", LINK), lg.entry("decision", "urgency", "今天"), lg.role_owners("decision")]
        if task_id:
            fields.append(lg.entry("decision", "task_ref", task_id))
        did = str(self.fake.create("decision", fields)["work_item_id"])
        if decided:
            sets = ["决定内容=同意", "决定人=研究员"] + (["签字结果=" + result] if result else [])
            self.quiet(ledger.confirm, lg, did, "研究员", sets)
        return did

    def walk(self, task_id, upto, signer=None):
        """Confirm the steps of a 验证 task up to (not including) step number `upto`."""
        signs = {}
        for i, (by, sets) in enumerate(STEPS[:upto - 1], 1):
            if self.fake.item(task_id)["nodes"][i - 1]["status"] != "doing":
                continue
            if i in (4, 9):
                signs[i] = signer or self.decision(task_id)
                sets = sets + ["%s=%s" % ("假设签字" if i == 4 else "下一步签字", signs[i])]
            self.quiet(ledger.confirm, self.lg(), task_id, by, sets)
        return signs


class RegisterTest(LedgerCase):
    def test_research_task_sheet(self):
        path = self.write(SHEET % ("验证", "产品 +2（Product-p2-01）"))
        lg = self.lg()
        item_id, text = self.out(ledger.register, lg, path)
        item = self.fake.item(item_id)
        t = self.fake.snap["types"][item["type_key"]]
        self.assertEqual((t["api_name"], t["flows"][item["flow_id"]]["name"], item["name"]), ("research_task", "验证", "复现并测量基线"))
        self.assertEqual(self.state(item_id), "确定目标")
        self.assertEqual((self.value(item_id, "research_task", "sender"), self.fake.shown(item["type_key"], lg.field("research_task", "sequence")["field_key"],
                                                                                           self.value(item_id, "research_task", "sequence"))["label"]),
                         ("Product-p2-01", "产品"))
        doc = next(iter(self.fake.docs.values()))
        self.assertEqual((doc["title"], doc["parent"]), ("工作项 %s｜研究任务单：复现并测量基线" % item_id, "node_research_task"))
        self.assertEqual(self.value(item_id, "research_task", "main_doc"), "https://example.feishu.cn/wiki/" + doc["node_token"])
        self.assertIn("- 关联工作项：[#%s](https://project.feishu.cn/" % item_id, doc["body"])
        self.assertIn("## 状态包\n\n### 简介\n\n在公开评测集上复现基线", doc["body"])      # the 目标 starts the 状态包
        self.assertTrue(doc["body"].rstrip().endswith("## 交接记录"))
        self.assertEqual(sorted(item["roles"]), sorted(r["role_id"] for r in t["roles"]))       # every node has an owner
        self.assertTrue(all(members == [fake_space.ME] for members in item["roles"].values()))
        with open(path, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s\n" % item_id, f.read())
        with open(os.path.join(self.tmp, "packages", "sent", "%s.json" % item_id), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "registered")
        self.assertIn("ledger.py next %s" % item_id, text)
        self.assertEqual(len(self.fake.comments), 1)

    def test_the_item_is_created_with_everything_its_page_requires(self):
        lg = self.lg()
        self.quiet(ledger.register, lg, self.write(SHEET % ("试探", "算法 +2（Alg-p2-01）")))
        created = json.loads(lg.wrote("workitem", "create")[0][lg.wrote("workitem", "create")[0].index("--fields") + 1])
        keys = {f["field_key"] for f in created}
        need = {c["field_key"] for c in lg.space_type("research_task")["create_fields"] if c["required"]}
        self.assertTrue(need <= keys)
        self.assertTrue(all(isinstance(f["field_value"], str) for f in created))               # meegle takes strings only
        doc = next(f for f in created if f["field_key"] == lg.field("research_task", "main_doc")["field_key"])
        self.assertEqual(doc["field_value"], "https://project.feishu.cn/" + self.fake.snap["simple_name"])   # until the document exists

    def test_sheet_header_and_flow_are_checked_first(self):
        text = self.fails(ledger.register, self.lg(), self.write("# 研究任务单：x\n\n- 发出方：产品 +2（Product-p2-01）\n- 关联工作项：待建\n"))
        self.assertIn("“- 流程：…”", text)
        text = self.fails(ledger.register, self.lg(), self.write(SHEET % ("冲刺", "产品 +2（Product-p2-01）")))
        self.assertIn("验证、试探", text)
        self.assertIn("没有“冲刺”", text)
        text = self.fails(ledger.register, self.lg(), self.write((SHEET % ("验证", "产品 +2（Product-p2-01）")).replace("待建", "#2001")))
        self.assertIn("这一行写“待建”", text)
        text = self.fails(ledger.register, self.lg(), self.write(SHEET % ("验证", "<序列> +2（<会话名>）")))
        self.assertIn("还是模板里的占位", text)                         # the template as it comes is not a task
        self.assertEqual(self.fake.items, {})

    def test_a_task_the_researcher_opens(self):
        item_id = self.task(sender="研究员")
        lg = self.lg()
        item = self.fake.item(item_id)
        self.assertEqual(self.value(item_id, "research_task", "sender"), "研究员")
        self.assertEqual(self.fake.shown(item["type_key"], lg.field("research_task", "sequence")["field_key"],
                                         self.value(item_id, "research_task", "sequence"))["label"], "项目管理")
        self.assertIn("负责方：产品 +2", self.quiet(ledger.next_step, self.lg("preview"), item_id))

    def test_feedback_sheet(self):
        task = self.task()
        item_id = self.quiet(ledger.register, self.lg(), self.write(FEEDBACK, "fb.md"))
        item = self.fake.item(item_id)
        self.assertEqual((item["name"], self.state(item_id)), ("反馈：基线要和人工调参比", "待解析"))
        self.assertEqual(self.value(item_id, "feedback", "task_ref"), task)                    # the only open task
        self.assertEqual(self.value(item_id, "feedback", "sender"), "导师（研究员转述）")
        doc = self.fake.doc_by(self.value(item_id, "feedback", "main_doc").rsplit("/", 1)[1])
        self.assertEqual(doc["parent"], "node_feedback")
        self.assertNotIn("## 状态包", doc["body"])                                             # the words stay as they were said

    def test_request_goes_under_its_sequence_and_waits(self):
        task = self.task()
        lg = self.lg()
        item_id, text = self.out(ledger.register, lg, self.draft(ASSIGN))
        self.assertEqual(self.state(item_id), "待开工")
        self.assertEqual(self.fake.item(item_id)["name"], "需求包（Product-p2-01）：代码优化器的需求文档")
        self.assertEqual((self.value(item_id, "same_seq", "task_ref"), self.value(item_id, "same_seq", "owner_session")), (task, "待派"))
        doc = self.fake.doc_by(self.value(item_id, "same_seq", "main_doc").rsplit("/", 1)[1])
        self.assertEqual(doc["parent"], "node_same_seq_product")
        self.assertIn("confirm %s --by <承接方的会话名>" % item_id, text)

    def test_a_package_names_its_task_when_the_space_has_none_or_several(self):
        text = self.fails(ledger.register, self.lg(), self.draft(ASSIGN))
        self.assertIn("没归档的研究任务有 0 条", text)
        first, second = self.task(name="t1.md"), self.task(name="t2.md")
        text = self.fails(ledger.register, self.lg(), self.draft(ASSIGN))
        self.assertIn("有 2 条", text)
        self.assertIn("--task <ID>", text)
        item_id = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN), task=second)
        self.assertEqual(self.value(item_id, "same_seq", "task_ref"), second)
        by_header = self.draft(ASSIGN, "h.md", edit=lambda t: t.replace("- 需要接收方做的事", "- 所属研究任务：#%s\n- 需要接收方做的事" % first, 1))
        self.assertEqual(self.value(self.quiet(ledger.register, self.lg(), by_header), "same_seq", "task_ref"), first)

    def test_execute_request_hangs_under_its_source(self):
        self.task()
        source = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        text = self.fails(ledger.register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("；来源工作项 #2100", "")))
        self.assertIn("--source <ID>", text)
        item_id = self.quiet(ledger.register, self.lg(), self.draft(EXECUTE, "e2.md", edit=lambda t: t.replace("；来源工作项 #2100", "")), source=source)
        self.assertEqual(self.value(item_id, "same_seq", "source_item"), source)
        self.assertEqual(self.value(item_id, "same_seq", "owner_session"), "Quality-p0-%s" % item_id)   # the +0's work name
        parent = self.fake.doc_by(self.value(source, "same_seq", "main_doc").rsplit("/", 1)[1])["node_token"]
        self.assertEqual(self.fake.doc_by(self.value(item_id, "same_seq", "main_doc").rsplit("/", 1)[1])["parent"], parent)
        wrong = self.draft(ASSIGN, "e3.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "产品 +1（Product-p1-03）"))
        self.assertIn("职级对不上", self.fails(ledger.register, self.lg(), wrong))

    def test_cross_request_is_put_in_by_its_sender(self):
        self.task()
        item_id, text = self.out(ledger.register, self.lg(), self.draft(CROSS))
        self.assertEqual(self.state(item_id), "待接单")                       # 提出 is confirmed with the registration
        self.assertIn("“提出”是发出方自己的一步，已随登记确认", text)
        item = self.fake.item(item_id)
        lg = self.lg()
        label = lambda ref: self.fake.shown(item["type_key"], lg.field("cross_request", ref)["field_key"], self.value(item_id, "cross_request", ref))["label"]
        self.assertEqual((label("from_sequence"), label("to_sequence"), label("sequence")), ("产品", "算法", "算法"))

    def test_decisions_from_the_queue_and_from_the_researcher(self):
        self.task()
        dated = lambda t: t.replace("- 回复期限：两天", "- 回复期限：2026-12-01")
        item_id, text = self.out(ledger.register, self.lg(), self.draft(DECISION, edit=dated))
        self.assertEqual(self.state(item_id), "待决定")
        self.assertNotIn("跳过字段", text)                                              # a decision has no deadline field
        self.assertNotIn("研究员主动发起的决策", text)
        item_id, text = self.out(ledger.register, self.lg(), self.draft(DECIDED, "d2.md"))
        self.assertIn("confirm %s --by 研究员 --set 决定内容='停止条件的“四周”从搜索开始计时，试点期另设两周观察。' --set 决定人=研究员" % item_id, text)

    def test_what_the_new_item_page_requires_is_asked_for_before_creating(self):
        self.task()
        lg = self.lg()
        path = self.draft(DECISION, edit=lambda t: t.replace("- 紧急程度：今天\n", "- 紧急程度：插队 / 今天 / 本周 / 可延后\n"))
        text = self.fails(ledger.register, lg, path)
        self.assertIn("这些字段必填，草稿里读不出来：紧急程度", text)
        self.assertEqual(lg.wrote("workitem", "create"), [])
        self.quiet(ledger.register, lg, path, pairs=["紧急程度=本周"])
        self.assertEqual(len(lg.wrote("workitem", "create")), 1)

    def test_preview_writes_nothing(self):
        _, text = self.out(ledger.register, self.lg("preview"), self.write(SHEET % ("验证", "产品 +2（Product-p2-01）")))
        self.assertIn("将执行：meegle workitem create", text)
        self.assertIn("预览：会登记一条研究任务", text)
        self.assertNotIn("\n登记：", "\n" + text)
        self.assertEqual((self.fake.items, self.fake.docs), ({}, {}))

    def test_a_failed_document_is_resumed_without_a_second_item(self):
        path = self.write(SHEET % ("验证", "产品 +2（Product-p2-01）"))
        down = [(("docs", "+create"), ledger.LedgerError("命令失败（退出码 1）：lark-cli docs +create\n503"))]
        text = self.fails(ledger.register, self.lg(replies=down), path)
        self.assertIn("--resume --apply", text)
        self.assertIn("主文档字段暂时指着空间首页", text)
        item_id = next(iter(self.fake.items))
        self.assertIn("加 --resume", self.fails(ledger.register, self.lg(), path))
        self.assertEqual(self.quiet(ledger.register, self.lg(), path, resume=True), item_id)
        self.assertEqual(len(self.fake.items), 1)
        self.assertTrue(self.value(item_id, "research_task", "main_doc").startswith("https://example.feishu.cn/wiki/"))
        self.assertIn("不要重复登记", self.fails(ledger.register, self.lg(), path))


class LinkLineTest(unittest.TestCase):
    def line(self, value, item_id="2106"):
        text = "# 需求包：x\n\n- 包类型：需求包\n- 关联工作项：%s\n- 回复期限：一天\n\n## 目标\n关联工作项：待建 这一句在正文里，不动。\n" % value
        return ledger.link_line(text, "#" + item_id, item_id).splitlines()[3]

    def test_placeholders_become_the_item(self):
        for value in ("待建", "待登记", "", "（待建）", "无"):
            self.assertEqual(self.line(value), "- 关联工作项：#2106", value)

    def test_what_follows_the_first_semicolon_is_kept(self):
        self.assertEqual(self.line("待建；来源工作项 #2100"), "- 关联工作项：#2106；来源工作项 #2100")

    def test_a_line_that_names_the_item_is_left_alone(self):
        self.assertEqual(self.line("#2106；来源工作项 #2100"), "- 关联工作项：#2106；来源工作项 #2100")
        self.assertEqual(self.line("[#2106](https://project.feishu.cn/x/same_seq/detail/2106)"),
                         "- 关联工作项：[#2106](https://project.feishu.cn/x/same_seq/detail/2106)")

    def test_anything_else_stops(self):
        for value in ("#2001", "见下文", "#21060"):
            with self.assertRaises(ledger.LedgerError):
                self.line(value)
        with self.assertRaises(ledger.LedgerError):
            ledger.link_line("# x\n\n- 包类型：需求包\n\n## 目标\n", "#1", "1")

    def test_only_the_header_is_touched(self):
        text = "# x\n\n- 关联工作项：待建\n\n## 目标\n- 关联工作项：待建\n"
        self.assertEqual(ledger.link_line(text, "#7", "7").count("待建"), 1)


class ReplyTest(LedgerCase):
    def test_delivery_fills_the_field_of_the_request_it_answers(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request), "back.md")
        lg = self.lg()
        link, text = self.out(ledger.reply, lg, path)
        self.assertEqual(self.value(request, "same_seq", "delivery_link"), link)
        parent = self.fake.doc_by(self.value(request, "same_seq", "main_doc").rsplit("/", 1)[1])["node_token"]
        self.assertEqual(self.fake.doc_by(link.rsplit("/", 1)[1])["parent"], parent)
        self.assertIn("“交付包”字段已填", text)
        self.assertIn("不要重复登记", self.fails(ledger.reply, self.lg(), path))

    def test_only_a_delivery_is_a_reply(self):
        self.task()
        self.assertIn("要用 register 登记", self.fails(ledger.reply, self.lg(), self.draft(DECISION)))
        decision = self.decision()
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % decision), "back.md")
        self.assertIn("交付包答复的是同序列需求或跨序列需求", self.fails(ledger.reply, self.lg(), path))


class StepTest(LedgerCase):
    def test_next_says_who_and_what(self):
        task = self.task()
        text = self.quiet(ledger.next_step, self.lg("preview"), task)
        self.assertIn("#%s  研究任务·验证  确定目标  复现并测量基线" % task, text)
        self.assertIn("这一步：确定目标（started），负责方：产品 +2", text)
        self.assertIn("待填 成功标准", text)
        self.assertIn("下一步：准备上下文", text)
        self.assertIn("ledger.py confirm %s --by <会话名>" % task, text)
        self.walk(task, 4)
        text = self.quiet(ledger.next_step, self.lg("preview"), task)
        self.assertIn("待填 假设签字（hypothesis_signoff）", text)
        self.assertIn("“假设签字”填一条研究员批准的决策工作项的 ID", text)

    def test_who_may_ask_for_a_step(self):
        task = self.task()
        sets = STEPS[0][1]
        self.assertIn("加 --by <提出请求的会话名>", self.fails(ledger.confirm, self.lg(), task, None, sets))
        for by in ("Alg-p2-01", "Product-p1-01", "PMO-p0-01"):
            self.assertIn("不能请书记员确认这一步", self.fails(ledger.confirm, self.lg(), task, by, sets), by)
        self.assertEqual(self.state(task), "确定目标")
        self.quiet(ledger.confirm, self.lg(), task, "product-p2-01", sets)             # the sequence's +2, any case
        self.quiet(ledger.confirm, self.lg(), task, "研究员", STEPS[1][1])               # the researcher may always
        self.assertEqual(self.state(task), "准备 PRD 和 Eval Set")

    def test_the_issuers_and_the_assignees_steps(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))           # Product-p2-01 → 产品 +1, nobody named yet
        self.assertIn("加 --by", self.fails(ledger.confirm, self.lg(), request))
        self.quiet(ledger.confirm, self.lg(), request, "Product-p1-02", ["owner_session=Product-p1-02"])
        self.assertEqual(self.state(request), "执行中")
        text = self.fails(ledger.confirm, self.lg(), request, "Product-p1-03", ["交付包=" + LINK])
        self.assertIn("负责方是承接方（Product-p1-02）", text)
        self.quiet(ledger.confirm, self.lg(), request, "Product-p2-01", ["交付包=" + LINK])      # the +2 of the assignee's sequence
        self.assertIn("负责方是发出方（Product-p2-01）", self.fails(ledger.confirm, self.lg(), request, "Product-p1-02"))
        self.quiet(ledger.confirm, self.lg(), request, "Product-p2-01")
        self.assertEqual(self.state(request), "已验收")

    def test_a_p0_asks_under_its_pool_name(self):
        self.task()
        source = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        request = self.quiet(ledger.register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("；来源工作项 #2100", "")), source=source)
        self.assertEqual(self.value(request, "same_seq", "owner_session"), "Quality-p0-%s" % request)   # the work name
        for by in ("Alg-p0-01", "Quality-p1-02"):
            self.assertIn("不能请书记员确认这一步", self.fails(ledger.confirm, self.lg(), request, by), by)
        self.quiet(ledger.confirm, self.lg(), request, "Quality-p0-01")                # the pool slot of the +0 on the item
        self.assertEqual(self.state(request), "执行中")

    def test_a_preview_says_so_on_every_result_line(self):
        task = self.task()
        self.walk(task, 7)
        _, text = self.out(ledger.confirm, self.lg("preview"), task, "RnD-p2-01", STEPS[6][1])
        self.assertIn("预览：会确认 #%s 的“跑 Eval Set”" % task, text)
        _, text = self.out(ledger.rollback, self.lg("preview"), task, "设计实验方案", "第 1 轮不够", decision=self.decision(task))
        self.assertIn("预览：会把 #%s 退回到“设计实验方案”" % task, text)
        self.assertIn("会清空 实验轮次，原值：1", text)
        decision = self.decision()
        self.assertIn("预览：会确认 #%s 的最后一步" % decision, self.out(ledger.archive, self.lg("preview"), decision, moved=True)[1])
        self.assertEqual((self.state(task), self.state(decision)), ("跑 Eval Set", "已决定"))

    def test_everything_missing_is_named_at_once(self):
        task = self.task()
        self.walk(task, 4)
        text = self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", ["规格=" + LINK])
        for name in ("假设签字", "总预算", "波动估计", "规格版本"):
            self.assertIn(name, text)
        self.assertNotIn("规格、", text.replace("规格版本", ""))
        self.assertEqual(self.value(task, "research_task", "规格"), "")               # nothing was written
        self.assertIn("总预算 要写数字", self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", ["总预算=两百"]))
        self.assertIn("没有选项“大概成立”", self.fails(ledger.update, self.lg(), task, ["结论=大概成立"]))
        self.assertIn("研究任务上没有字段“验收结论”", self.fails(ledger.update, self.lg(), task, ["验收结论=接受"]))

    def test_values_are_written_then_the_step_is_confirmed_and_read_back(self):
        task = self.task()
        lg = self.lg()
        _, text = self.out(ledger.confirm, lg, task, "Product-p2-01", STEPS[0][1])
        self.assertIn("确认：#%s “确定目标”完成，现在到“准备上下文”，状态“准备上下文”。" % task, text)
        writes = [argv[1:3] for argv, write, _ in lg.calls if write]
        self.assertEqual(writes, [["workitem", "update"], ["workflow", "transition"]])
        self.assertTrue(self.value(task, "research_task", "成功标准"))
        _, text = self.out(ledger.confirm, self.lg("preview"), task, "Alg-p2-01", STEPS[1][1])
        self.assertIn("将执行：meegle workflow transition", text)
        self.assertEqual(self.state(task), "准备上下文")

    def test_a_step_out_of_turn(self):
        task = self.task()
        self.assertIn("现在走到的是“确定目标”", self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", node="prep_context"))
        self.assertIn("没有节点“复盘”", self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", node="复盘"))

    def test_dates_and_people(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))
        self.quiet(ledger.update, self.lg(), request, ["截止时间=2026-12-01"])
        self.assertEqual(self.value(request, "same_seq", "due"), "1796126400000")       # UTC noon, as a string
        self.assertIn("要写成存在的日期", self.fails(ledger.update, self.lg(), request, ["due=2026-02-30"]))
        decision = self.decision(decided=False)
        self.assertIn("只能填“研究员”", self.fails(ledger.confirm, self.lg(), decision, "研究员", ["决定内容=同意", "决定人=张三"]))
        self.quiet(ledger.confirm, self.lg(), decision, "研究员", ["决定内容=同意", "决定人=研究员"])
        self.assertEqual((self.state(decision), self.value(decision, "decision", "决定人")), ("已决定", fake_space.ME))
        self.assertIn("负责方是研究员", self.fails(ledger.rollback, self.lg(), decision, "决定", "重新考虑", by="Alg-p2-01"))


class SignTest(LedgerCase):
    def test_a_signature_is_an_approved_decision_for_this_task(self):
        task, other = self.task(name="t1.md"), self.task(name="t2.md")
        self.walk(task, 4)
        sets = STEPS[3][1]

        def refused(decision):
            return self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % decision])
        self.assertIn("要填一条决策工作项的 ID；#%s 是研究任务" % other, refused(other))
        self.assertIn("状态是“待决定”", refused(self.decision(task, decided=False)))
        self.assertIn("签字结果是“退回”，要是“批准”", refused(self.decision(task, result="退回")))
        self.assertIn("签字结果是空的", refused(self.decision(task, result=None)))
        self.assertIn("所属研究任务是 #%s，不是 #%s" % (other, task), refused(self.decision(other)))
        self.assertIn("所属研究任务是空的", refused(self.decision()))
        self.assertIn("工作项 ID 要写成数字", refused("研究员口头同意"))
        self.assertEqual(self.state(task), "确定假设条件")
        good = self.decision(task)
        self.quiet(ledger.archive, self.lg(), good, moved=True)                          # an archived decision still signs
        self.quiet(ledger.confirm, self.lg(), task, "Alg-p2-01", sets + ["hypothesis_signoff=%s" % good])
        self.assertEqual(self.state(task), "设计实验方案")

    def test_the_whole_verify_flow(self):
        task = self.task()
        signs = self.walk(task, 10)
        self.assertEqual(sorted(signs), [4, 9])
        self.assertEqual(self.state(task), "已结束")
        self.assertIn("要先把主文档移到归档节点", self.fails(ledger.confirm, self.lg(), task, "PMO-p0-01"))
        lg = self.lg()
        _, text = self.out(ledger.archive, lg, task)
        self.assertEqual(self.state(task), "归档")
        doc = self.fake.doc_by(self.value(task, "research_task", "main_doc").rsplit("/", 1)[1])
        self.assertEqual(doc["parent"], "node_archive_product")
        self.assertIn("已经是“归档”", self.out(ledger.archive, self.lg(), task)[1])
        self.assertIn("流程已经走完", self.quiet(ledger.next_step, self.lg("preview"), task))

    def test_the_probe_flow_has_no_signature(self):
        task = self.task("试探", "算法 +2（Alg-p2-01）")
        for sets in (["试探问题=错误集中在哪一类样本", "总预算=20"], ["试探记录=" + LINK], ["候选假设=重采样能提高长尾类别的准确率", "去向=转验证"]):
            self.quiet(ledger.confirm, self.lg(), task, "Alg-p2-01", sets)
        self.assertEqual(self.state(task), "已结束")
        self.quiet(ledger.archive, self.lg(), task)
        self.assertEqual(self.state(task), "归档")


class SpentDecisionTest(LedgerCase):
    """One decision passes one gate once: it has to be made after the step it is used on began."""

    def test_a_decision_made_before_the_step_began(self):
        task = self.task()
        early = self.decision(task)                                                   # decided while the task was at its first step
        self.walk(task, 4)
        text = self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", STEPS[3][1] + ["假设签字=%s" % early])
        self.assertIn("一条决定只用在它决定时所在的那一步", text)
        self.assertEqual(self.state(task), "确定假设条件")

    def test_the_signature_of_step_four_does_not_sign_step_nine(self):
        task = self.task()
        signs = self.walk(task, 9)
        self.assertIn("重新决定", self.fails(ledger.confirm, self.lg(), task, "PMO-p2-01", ["下一步签字=%s" % signs[4]]))
        self.assertEqual(self.state(task), "下一步讨论")

    def test_after_a_rollback_the_old_signature_and_the_rollback_decision_are_spent(self):
        task = self.task()
        signs = self.walk(task, 6)
        back = self.decision(task)
        self.quiet(ledger.rollback, self.lg(), task, "确定假设条件", "阈值定错了", decision=back)
        sets = STEPS[3][1]
        for spent in (signs[4], back):
            self.assertIn("重新决定", self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % spent]))
        self.quiet(ledger.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % self.decision(task)])
        self.assertEqual(self.state(task), "设计实验方案")
        self.assertIn("重新决定", self.fails(ledger.rollback, self.lg(), task, "确定假设条件", "再退一次", decision=back))


class FreezeTest(LedgerCase):
    def frozen_task(self):
        task = self.task()
        self.walk(task, 8)
        return task

    def test_untouched_fields_pass(self):
        task = self.frozen_task()
        self.quiet(ledger.update, self.lg(), task, ["已用预算=30"])                      # not a frozen field
        self.quiet(ledger.confirm, self.lg(), task, *STEPS[7])
        self.assertEqual(self.state(task), "下一步讨论")

    def test_a_change_after_signing_stops_the_conclusion(self):
        task = self.frozen_task()
        self.quiet(ledger.update, self.lg(), task, ["总预算=300"])
        self.fake.tick(120000)
        text = self.fails(ledger.confirm, self.lg(), task, *STEPS[7])
        self.assertIn("签字以后，冻结的字段被改过", text)
        self.assertRegex(text, r"总预算：\d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d，200 → 300")
        self.assertEqual(self.state(task), "得出结论")
        self.assertEqual(self.value(task, "research_task", "结论"), "")                 # the conclusion was not written either

    def test_a_change_the_records_do_not_show_yet(self):
        task = self.frozen_task()
        self.fake.tick(600000)
        self.fake.lag = 60000                                                           # records take a minute to show up
        self.quiet(ledger.update, self.lg(), task, ["规格版本=spec-v2"])
        text = self.fails(ledger.confirm, self.lg(), task, *STEPS[7])
        self.assertIn("规格版本：签字时是 spec-v1，现在是 spec-v2（操作记录里还没有这次改动）", text)

    def test_the_researcher_accepts_a_change_with_a_decision(self):
        task = self.frozen_task()
        self.quiet(ledger.update, self.lg(), task, ["总预算=300"])
        self.fake.tick(120000)
        self.assertIn("不合格", self.fails(ledger.confirm, self.lg(), task, *STEPS[7], decision=self.decision(task, result="退回")))
        _, text = self.out(ledger.confirm, self.lg(), task, *STEPS[7], decision=self.decision(task))
        self.assertIn("冻结的字段在签字后改过（1 处），研究员已用决策", text)
        self.assertEqual(self.state(task), "下一步讨论")

    def test_signing_again_starts_a_new_freeze(self):
        task = self.frozen_task()
        self.fake.tick(120000)
        self.quiet(ledger.rollback, self.lg(), task, "确定假设条件", "阈值定高了", decision=self.decision(task))
        self.assertEqual(self.value(task, "research_task", "总预算"), "")               # the undone steps start empty
        self.fake.tick(120000)
        signer = self.decision(task)
        for by, sets in STEPS[3:7]:
            sets = [s.replace("总预算=200", "总预算=150") for s in sets] + (["假设签字=" + signer] if by == "Alg-p2-01" and "规格=" + LINK in sets else [])
            self.quiet(ledger.confirm, self.lg(), task, by, sets)
        self.fake.tick(120000)
        self.quiet(ledger.confirm, self.lg(), task, *STEPS[7])                          # 150 was signed: nothing changed since
        self.assertEqual(self.state(task), "下一步讨论")


class RollbackTest(LedgerCase):
    def test_the_main_line_goes_back_only_on_the_researchers_decision(self):
        task = self.task()
        self.walk(task, 7)
        self.assertIn("回退由研究员决定", self.fails(ledger.rollback, self.lg(), task, "design_experiment", "第 1 轮不够", by="研究员"))
        self.assertIn("回滚要写原因", self.fails(ledger.rollback, self.lg(), task, "design_experiment", " ", decision=self.decision(task)))
        self.assertIn("不合格", self.fails(ledger.rollback, self.lg(), task, "design_experiment", "第 1 轮不够",
                                         decision=self.decision(task, result="退回")))
        self.assertIn("只能退回到已完成的节点", self.fails(ledger.rollback, self.lg(), task, "run_eval", "x", decision=self.decision(task)))
        decision = self.decision(task)
        lg = self.lg()
        _, text = self.out(ledger.rollback, lg, task, "设计实验方案", "第 1 轮不够", decision=decision)
        self.assertEqual(self.state(task), "设计实验方案")
        self.assertIn("原因：第 1 轮不够（研究员决定 #%s）" % decision, text)
        for line in ("清空 实验方案，原值：" + LINK, "清空 实验轮次，原值：1", "清空 冒烟运行 ID，原值：smoke-0001"):
            self.assertIn(line, text)
        self.assertEqual(self.value(task, "research_task", "实验轮次"), "")
        self.assertEqual(self.value(task, "research_task", "规格版本"), "spec-v1")      # the steps before stay as they were
        reasons = [c["extra"]["reason_suffix"][0] for r in self.fake.ops if r["operation_type"] == "rollback" for c in r["record_contents"]]
        self.assertTrue(reasons and all(r.endswith("（研究员决定 #%s）" % decision) for r in reasons))
        self.assertIn("待填 实验轮次", self.quiet(ledger.next_step, self.lg("preview"), task))
        self.assertIn("要先填", self.fails(ledger.confirm, self.lg(), task, "Alg-p2-01"))   # the step has to be done again

    def test_a_delivery_that_is_not_accepted(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN, edit=lambda t: t.replace("- 接收方：产品 +1", "- 接收方：产品 +1（Product-p1-02）")))
        self.assertEqual(self.value(request, "same_seq", "owner_session"), "Product-p1-02")
        self.quiet(ledger.confirm, self.lg(), request, "Product-p1-02")
        self.quiet(ledger.confirm, self.lg(), request, "Product-p1-02", ["交付包=" + LINK])
        self.assertIn("负责方是发出方（Product-p2-01）", self.fails(ledger.rollback, self.lg(), request, "执行", "第 2 条没有证据", by="Product-p1-02"))
        self.assertIn("不走决策", self.fails(ledger.rollback, self.lg(), request, "执行", "第 2 条没有证据", decision=self.decision()))
        _, text = self.out(ledger.rollback, self.lg(), request, "执行", "第 2 条没有证据", by="Product-p2-01")
        self.assertEqual((self.state(request), self.value(request, "same_seq", "delivery_link")), ("执行中", ""))
        self.assertIn("清空 交付包", text)

    def test_a_request_handed_back_to_its_sender(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(CROSS))
        self.quiet(ledger.rollback, self.lg(), request, "提出", "范围不清", by="Alg-p2-01")     # the receiving +2 does not take it
        self.assertEqual(self.state(request), "已退回")
        self.assertTrue(self.value(request, "cross_request", "main_doc"))               # what the new-item page required stays
        self.quiet(ledger.confirm, self.lg(), request, "Product-p2-01")
        take = ["需求复述=照三条目标", "承接范围=接受第 1、2 条", "预计交付时间=2026-12-10"]
        self.quiet(ledger.confirm, self.lg(), request, "Alg-p2-01", take)
        self.assertEqual(self.state(request), "执行中")

    def test_an_archived_item_is_reopened_by_the_researcher(self):
        decision = self.decision()
        self.quiet(ledger.archive, self.lg(), decision, moved=True)
        self.assertIn("加 --by 研究员", self.fails(ledger.rollback, self.lg(), decision, "归档", "还要补一条", by="PMO-p2-01"))
        self.quiet(ledger.rollback, self.lg(), decision, "归档", "还要补一条", by="研究员")
        self.assertEqual(self.state(decision), "已决定")


class ArchiveTest(LedgerCase):
    def test_archive_only_at_the_last_step(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))
        lg = self.lg()
        self.assertIn("还没走到最后一步“归档”：现在在“开工”", self.fails(ledger.archive, lg, request))
        self.assertEqual(lg.wrote("wiki", "+move"), [])

    def test_a_document_moved_before_the_step_failed(self):
        decision = self.decision()
        self.value(decision, "decision", "main_doc")
        down = [(("workflow", "transition"), ledger.LedgerError("命令失败（退出码 1）：meegle workflow transition\n502"))]
        doc = self.fake.create_doc("node_decision", "决策", "x")["data"]["document"]
        self.quiet(ledger.update, self.lg(), decision, ["main_doc=https://example.feishu.cn/wiki/" + self.fake.docs[doc["document_id"]]["node_token"]])
        text = self.fails(ledger.archive, self.lg(replies=down), decision)
        self.assertIn("archive %s --moved --apply" % decision, text)
        self.assertEqual(len(self.fake.moved), 1)
        self.quiet(ledger.archive, self.lg(), decision, moved=True)
        self.assertEqual((self.state(decision), len(self.fake.moved)), ("归档", 1))


class FieldsAndQueriesTest(LedgerCase):
    def test_blocked_is_two_fields(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))
        self.quiet(ledger.update, self.lg(), request, ["blocked_reason=等 GPU 配额"])
        self.assertRegex(self.value(request, "same_seq", "blocked_since"), r"^\d{13}$")
        self.assertIn("阻塞中（", self.quiet(ledger.next_step, self.lg("preview"), request))
        rows = self.quiet(ledger.query, self.lg("preview"), "blocked")
        self.assertEqual([str(r["work_item_id"]) for r in rows], [request])
        self.quiet(ledger.update, self.lg(), request, ["阻塞原因="])
        self.assertEqual((self.value(request, "same_seq", "blocked_reason"), self.value(request, "same_seq", "blocked_since")), ("", ""))
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "blocked"), [])
        for ref in ("blocked_reason", "阻塞原因"):                                       # however the field is written
            self.assertIn("阻塞原因用 update 写", self.fails(ledger.confirm, self.lg(), request, "Product-p1-01", [ref + "=x"]))

    def test_named_queries(self):
        lg = self.lg("offline")
        mql = lambda name, seq=None: ledger.mql_text(lg, name, seq)
        self.assertIn("`work_item_status` = '待决定'", mql("queue")[0])
        self.assertIn("NOT IN ('已验收', '归档', '已终止')", mql("open")[0])
        self.assertIn("NOT IN ('归档', '已终止')", mql("tasks")[0])
        self.assertEqual(len(mql("blocked")), 3)                                        # the three types that can be blocked
        self.assertTrue(all("IS NOT NULL" in q and "'归档', '已终止'" in q for q in mql("blocked")))
        self.assertEqual(len(mql("source:2100")), 4)
        self.assertIn("= '算法'", mql("open", "alg")[0])
        self.assertEqual(mql("open", "算法"), mql("open", "alg"))                        # the Chinese name is taken too
        with self.assertRaises(ledger.LedgerError) as e:
            mql("open", "算法组")
        self.assertIn("alg（算法）", str(e.exception))
        for bad in ("reviewing", "nope"):
            with self.assertRaises(ledger.LedgerError):
                mql(bad)
        self.assertEqual(sorted(ledger.QUERY_NAMES), sorted(list(ledger.QUERIES) + ["blocked"]))
        self.assertEqual(len(ledger.QUERY_NAMES), 7)

    def test_queries_see_the_items(self):
        task = self.task()
        decision = self.decision(task, decided=False)
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "tasks")], [task])
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [decision])
        self.assertEqual(ledger.open_tasks(self.lg("preview")), [(task, "复现并测量基线")])

    def test_get_shows_fields_by_name(self):
        task = self.task()
        text = self.quiet(ledger.digest, self.lg("preview"), task)
        self.assertIn("#%s  研究任务·验证  确定目标  复现并测量基线" % task, text)
        self.assertIn("  序列（sequence）：产品", text)
        self.assertIn("  发出方（sender）：Product-p2-01", text)

    def test_states_that_no_longer_take_new_information(self):
        self.assertEqual(ledger.closed_states(self.lg("offline")), {"已验收", "归档", "已终止"})

    def test_relink_replaces_the_block_in_place(self):
        task = self.task()

        def page(line):
            return {"data": {"document": {"content": '<docx><ul><li id="b1">流程：验证</li><li id="b2">%s</li></ul><h2 id="h1">目标</h2></docx>' % line}}}
        lg = self.lg(replies=[(("docs", "+fetch"), page("关联工作项：待建；来源工作项 #2100")), (("docs", "+update"), {})])
        self.quiet(ledger.relink, lg, task)
        argv, _, stdin = next(c for c in lg.calls if "+update" in c[0])
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--command", "--start-block-id", "--end-block-id")], ["block_replace", "b2", "b2"])
        self.assertRegex(stdin, r"^- 关联工作项：\[#%s\]\(https://project\.feishu\.cn/\S+/detail/%s\)；来源工作项 #2100\n$" % (task, task))
        lg = self.lg(replies=[(("docs", "+fetch"), page('关联工作项：<a href="x">#%s</a>' % task))])
        self.assertIn("不用改", self.out(ledger.relink, lg, task)[1])
        self.assertEqual(lg.wrote("docs", "+update"), [])
        lg = self.lg(replies=[(("docs", "+fetch"), page("关联工作项：#2001"))])
        self.assertIn("这一行写“待建”", self.fails(ledger.relink, lg, task))


class MainTest(LedgerCase):
    def run_main(self, *argv):
        buf = io.StringIO()
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            with contextlib.redirect_stdout(buf):
                code = ledger.main(list(argv))
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old
        return code, buf.getvalue()

    def ids(self, change):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        change(ids)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)

    def test_offline_prints_the_commands(self):
        code, text = self.run_main("--offline", "confirm", "2101", "--by", "Alg-p2-01", "--set", "规格版本=spec-v1", "--type", "research_task")
        self.assertEqual(code, 0)
        self.assertIn("将执行：meegle workflow transition --work-item-id 2101 --node-id '<当前节点>' --action confirm", text)
        code, text = self.run_main("--offline", "rollback", "2101", "--to", "执行", "--reason", "证据不全", "--by", "Alg-p2-01", "--type", "same_seq")
        self.assertEqual(code, 0)
        self.assertIn("--action rollback --rollback-reason 证据不全", text)
        self.assertEqual(self.run_main("--offline", "mql", "tasks")[0], 0)

    def test_an_unbound_or_old_binding_is_refused(self):
        self.ids(lambda ids: ids.update(schema="agent-org/ids-v1"))
        code, text = self.run_main("get", "2101")
        self.assertEqual(code, 1)
        self.assertIn("旧版的绑定", text)
        self.ids(lambda ids: ids["meegle"].pop("bound_at"))
        code, text = self.run_main("get", "2101")
        self.assertIn("还没绑定飞书空间", text)

    def test_the_old_commands_are_gone(self):
        for argv in (["transition", "2101", "执行中"], ["register", "x.md", "--existing"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.run_main(*argv)


def recorded(name):
    """An answer recorded from a real space before node flows (tests/fixtures/feishu/)."""
    with open(os.path.join(ROOT, "tests", "fixtures", "feishu", name), encoding="utf-8") as f:
        return json.load(f)


class DocumentsTest(LedgerCase):
    """Documents, receipts and knowledge-base nodes: what a registration leaves behind when a step fails."""

    def sheet(self, name="t.md"):
        return self.write(SHEET % ("验证", "产品 +2（Product-p2-01）"), name)

    def ids(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_status_block_replaces_the_range_between_the_two_headings(self):
        decision = self.decision(decided=False)
        lg = self.lg(replies=[(("+fetch", "outline"), recorded("docs-fetch-outline.json")),
                              (("+fetch", "range"), recorded("docs-fetch-range-status.json")), (("docs", "+update"), {})])
        heads = ledger.heading_ids(lg, LINK)
        self.assertEqual((len(heads), heads[-1]), (12, ("doxcnakjeLdey6R5sMps4TyITGg", "交接记录")))
        path = self.write("### 当前状态\n执行中。\n", "status.md")
        self.quiet(ledger.status_block, lg, decision, path)
        argv, write, stdin = lg.calls[-1]
        self.assertTrue(write)
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--doc", "--start-block-id", "--end-block-id", "--content")],
                         [LINK, "doxcnnyQfTdXSmSuMyN3ajvOmgh", "doxcnm3kiNhVu6Y7yOgNWnLzddd", "-"])
        self.assertTrue(stdin.startswith("## 状态包"))

    def test_block_shapes(self):
        xml = recorded("docs-fetch-range-status.json")["data"]["document"]["content"]
        self.assertEqual(ledger.last_block_before(xml, "doxcnakjeLdey6R5sMps4TyITGg"), "doxcnm3kiNhVu6Y7yOgNWnLzddd")
        nested = '<fragment><h2 id="a">状态包</h2><p id="b">x</p><table id="t"><tr><td id="c">y</td></tr></table><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(ledger.last_block_before(nested, "z"), "t")
        excerpt = '<fragment><h2 id="a">状态包</h2><excerpt top-block-id="tb"><p id="inner">x</p></excerpt><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(ledger.last_block_before(excerpt, "z"), "tb")
        html = '<fragment><h2 align="left" id="s1">状态包</h2><p id="p">x</p><h2 id="z">交接记录</h2></fragment>'
        lg = self.lg("preview", [(("+fetch", "outline"), {"data": {"document": {"content": html}}})])
        self.assertEqual(ledger.heading_ids(lg, "u")[0], ("s1", "状态包"))

    def test_status_sections_come_before_the_log(self):
        body = ledger.with_status_sections("## 认知层\nx\n\n## 交接记录\n[Product+2] 起草。\n")
        self.assertLess(body.index("## 状态包"), body.index("## 交接记录"))
        self.assertEqual(ledger.with_status_sections(body), body)
        self.assertTrue(ledger.with_status_sections("## 认知层\nx\n").rstrip().endswith("## 交接记录"))

    def test_append_and_comment_carry_a_session_tag(self):
        decision = self.decision(decided=False)
        self.assertIn("第一行要带会话标签", self.fails(ledger.append, self.lg(), decision, self.write("没有标签的记录\n", "rec.md")))
        lg = ledger.Ledger(self.tmp, "offline")
        self.quiet(ledger.comment, lg, "2101", "[紧急] 预算超了")
        self.assertIn("'%s [紧急] 预算超了'" % lg.tag, lg.planned[-1])
        self.quiet(ledger.comment, lg, "2101", "[Alg+1] 收到")
        self.assertIn("'[Alg+1] 收到'", lg.planned[-1])

    def test_a_create_answer_without_an_id_stops(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.created_id({"data": {"template": {"id": 11093046}}}, "x")
        self.assertEqual(ledger.created_id({"data": {"work_item_id": 5}}, "x"), "5")
        self.assertEqual(ledger.created_id({"data": {"id": 6}}, "x"), "6")
        self.assertIn("输出里没有工作项 ID", self.fails(ledger.register, self.lg(replies=[(("workitem", "create"), {"ok": True})]), self.sheet()))

    def test_a_document_that_exists_is_not_created_twice(self):
        path = self.sheet()
        down = [(("wiki", "+node-get"), ledger.LedgerError("node-get down"))]
        self.fails(ledger.register, self.lg(replies=down), path)
        item_id = next(iter(self.fake.items))
        with open(os.path.join(self.tmp, "packages", "sent", "%s.json" % item_id), encoding="utf-8") as f:
            self.assertIn("docx", json.load(f))
        lg = self.lg()
        self.quiet(ledger.register, lg, path, resume=True)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs)), ([], 1))
        self.assertIn("/wiki/wik0001", self.value(item_id, "research_task", "main_doc"))

    def test_an_uncertain_document_needs_doc(self):
        path = self.sheet()
        with self.assertRaises(ledger.WriteMaybeDone), contextlib.redirect_stdout(io.StringIO()):
            ledger.register(self.lg(replies=[(("docs", "+create"), ledger.WriteMaybeDone("timeout"))]), path)
        item_id = next(iter(self.fake.items))
        self.assertIn("--doc", self.fails(ledger.register, self.lg(), path, resume=True))
        for bad in ("https://example.feishu.cn/wiki/", "https://example.feishu.cn/drive/folder/fldX", "docxMainDoc0001"):
            self.assertIn("--doc", self.fails(ledger.register, self.lg(), path, doc=bad))
        lg = self.lg()
        self.quiet(ledger.register, lg, path, doc="https://example.feishu.cn/wiki/wikFoundOnWeb01")      # no --resume needed
        self.assertEqual((lg.wrote("docs", "+create"), lg.wrote("workitem", "create")), ([], []))
        self.assertEqual(self.value(item_id, "research_task", "main_doc"), "https://example.feishu.cn/wiki/wikFoundOnWeb01")

    def test_a_receipt_whose_item_is_gone(self):
        path = self.sheet()
        os.makedirs(os.path.join(self.tmp, "packages", "sent"), exist_ok=True)
        with open(os.path.join(self.tmp, "packages", "sent", "2105.json"), "w", encoding="utf-8") as f:
            json.dump({"draft": os.path.relpath(path, self.tmp), "work_item_id": "2105", "status": "created"}, f)
        self.assertIn("删掉回执", self.fails(ledger.register, self.lg(), path, resume=True))

    def test_reply_reruns_reuse_the_document_and_skip_odd_receipts(self):
        self.task()
        request = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN))
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request), "back.md")
        with open(os.path.join(self.tmp, "packages", "sent", "reply-%s-back.md.json" % request), "w", encoding="utf-8") as f:
            json.dump(["x"], f)                                                         # not a receipt: left alone
        before = len(self.fake.docs)
        self.fails(ledger.reply, self.lg(replies=[(("wiki", "+node-get"), ledger.LedgerError("down"))]), path)
        lg = self.lg()
        self.quiet(ledger.reply, lg, path)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs)), ([], before + 1))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "packages", "sent", "reply-%s-back.md-2.json" % request)))

    def test_the_tenant_domain_is_kept_from_the_first_document(self):
        ids = self.ids()
        ids["wiki"].pop("tenant_domain")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        self.quiet(ledger.register, self.lg(), self.sheet())
        self.assertEqual(self.ids()["wiki"].get("tenant_domain"), "example.feishu.cn")

    def test_roles_that_did_not_take_are_filled_in(self):
        path = self.sheet()
        lg = self.lg()
        real = self.fake.create

        def without_roles(type_ref, fields):
            return real(type_ref, [f for f in fields if f["field_key"] != "role_owners"])
        self.fake.create = without_roles
        _, text = self.out(ledger.register, lg, path)
        self.assertIn("已用 --role-operate 把研究员补进", text)
        item = next(iter(self.fake.items.values()))
        self.assertTrue(item["roles"] and all(m == [fake_space.ME] for m in item["roles"].values()))

    def nodes(self, *pairs):
        return {"ok": True, "data": {"nodes": [{"node_token": t, "title": title, "has_child": False} for t, title in pairs],
                                     "has_more": False}}

    def test_ensure_node_reuses_a_node_of_the_same_name(self):
        lg = self.lg(replies=[(("wiki", "+node-list"), self.nodes(("wikSame001", "反馈包"), ("wikX", "汇报文档")))])
        lg.nodes.pop("feedback")
        self.quiet(ledger.ensure_node, lg, "feedback")
        self.assertEqual(lg.wrote("wiki", "+node-create"), [])
        self.assertEqual(self.ids()["wiki"]["nodes"]["feedback"]["node_token"], "wikSame001")
        for listing in (self.nodes(("wikA", "产品"), ("wikB", "产品")), {"_text": "error"}):
            lg = self.lg(replies=[(("wiki", "+node-list"), listing)])
            lg.nodes.pop("archive/product")
            self.fails(ledger.ensure_node, lg, "archive/product")
        lg = self.lg(replies=[(("wiki", "+node-list"), self.nodes(("wikZ", "算法"))),
                              (("wiki", "+node-create"), {"data": {"node": {"node_token": "wikNew0001"}}})])
        lg.nodes.pop("archive/product")
        self.quiet(ledger.ensure_node, lg, "archive/product")
        self.assertIn("node_archive", " ".join(lg.wrote("wiki", "+node-create")[0]))
        self.assertEqual(self.ids()["wiki"]["nodes"]["archive/product"]["node_token"], "wikNew0001")

    def test_ensure_node_all_builds_parents_first(self):
        empty = self.nodes()
        lg = self.lg("preview", [(("wiki", "+node-list"), empty)])
        lg.nodes.clear()
        _, text = self.out(ledger.ensure_all_nodes, lg)
        top = self.spec.rules["docs_tree"]["nodes"]
        self.assertEqual(len([p for p in lg.planned if "+node-create" in p]), len(top))
        self.assertIn("节点 archive/alg 要等上一级 archive 建好", text)
        ids = self.ids()
        ids["wiki"]["nodes"] = {}
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        made = []
        lg = self.lg(replies=[(("wiki", "+node-list"), empty),
                              (("wiki", "+node-create"), lambda argv: made.append(argv) or {"data": {"node": {"node_token": "wikNew%04d" % len(made)}}})])
        lg.nodes.clear()
        self.quiet(ledger.ensure_all_nodes, lg)
        keys = self.spec.node_keys()
        self.assertEqual(len(made), len(keys))
        self.assertEqual(len(keys), 21)
        self.assertFalse([c for c in made[:len(top)] if "--parent-node-token" in c])
        self.assertTrue(all("--parent-node-token" in c for c in made[len(top):]))
        self.assertEqual(sorted(self.ids()["wiki"]["nodes"]), sorted(keys))


class ValuesAndReadsTest(LedgerCase):
    def test_set_values_from_files(self):
        lg = ledger.Ledger(self.tmp, "offline")
        with open(os.path.join(self.tmp, "usable.md"), "w", encoding="utf-8") as f:
            f.write("结论原文\n")
        self.assertEqual(ledger.set_value(lg, "@usable.md"), "结论原文")
        self.assertEqual(ledger.set_value(lg, "@研究员 还没批预算"), "@研究员 还没批预算")
        self.assertEqual(ledger.set_value(lg, "@@usable.md"), "@usable.md")
        for missing in ("@usabel.md", "@runs/alg/x", "@可使用的结论原文", "@<可使用的结论原文文件>", "@runs/alg/重评 结果.md", "@usable"):
            with self.assertRaises(ledger.LedgerError, msg=missing):
                ledger.set_value(lg, missing)
        cwd = os.getcwd()
        try:
            os.chdir(os.path.join(self.tmp, "packages"))               # falls back to the project directory
            self.assertEqual(ledger.set_value(lg, "@usable.md"), "结论原文")
        finally:
            os.chdir(cwd)
        gbk = os.path.join(self.tmp, "gbk.txt")
        with open(gbk, "wb") as f:
            f.write("结论原文".encode("gbk"))
        with self.assertRaises(ledger.LedgerError):
            ledger.set_value(lg, "@" + gbk)

    def test_source_line_with_a_full_width_colon(self):
        path = self.draft(EXECUTE, edit=lambda t: t.replace("；来源工作项 #2100", "\n- 来源工作项：#2100"))
        self.assertEqual(ledger.Draft(self.spec, path).source, "2100")

    def test_query_shapes(self):
        rows = ledger.query_rows(recorded("workitem-query-queue.json"))
        self.assertEqual([(r["work_item_id"], r["work_item_status"]) for r in rows], [(7152108550, "待决定")])
        self.assertEqual(ledger.query_rows(recorded("workitem-query-empty.json")), [])
        for bad in ({"_text": "Error: token expired"}, {"code": 10001, "data": None}, {"data": {"1": "x"}},
                    {"data": {"1": [{"moql_field_list": "x"}]}}, {"data": {"1": ["abc"]}}, {"data": ["unexpected"]}):
            with self.assertRaises(ledger.LedgerError):
                ledger.query_rows(bad)
        for cell in (["待决定"], "待决定", [None]):
            odd = {"data": {"1": [{"moql_field_list": [{"key": "work_item_status", "value": {"key_label_value_list": cell}}]}]}}
            with self.assertRaises(ledger.LedgerError):
                ledger.query_rows(odd)

    def page(self, start, n, count=None, session="S1"):
        group = [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": start + i}}]} for i in range(n)]
        return {"data": {"1": group}, "session_id": session if count != "none" else None,
                "list": None if count in (None, "none") else [{"count": count, "group_infos": [{"group_id": "1"}]}]}

    def test_query_reads_every_page(self):
        pages = [(("--session-id", json.dumps([{"group_id": "1", "page_num": k}])), self.page(50 * (k - 1), n)) for k, n in ((2, 50), (3, 20))]
        for count in (120, "120", 120.0, None):
            rows = self.quiet(ledger.query, self.lg("preview", pages + [(("--mql",), self.page(0, 50, count))]), "open")
            self.assertEqual(len(rows), 120, count)
        lg = self.lg("preview", [(("--session-id",), recorded("workitem-query-page-past-end.json")), (("--mql",), self.page(0, 50, 120))])
        rows, text = self.out(ledger.query, lg, "open")
        self.assertEqual(len(rows), 50)
        self.assertIn("只读出 50 项", text)
        bad = [(("--session-id",), {"_text": "Error: session expired"}), (("--mql",), self.page(0, 50, 120))]
        self.assertIn("第 2 页", self.fails(ledger.query, self.lg("preview", bad), "open"))
        rows, text = self.out(ledger.query, self.lg("preview", [(("--mql",), self.page(0, 50, "none"))]), "open")
        self.assertEqual(len(rows), 50)
        self.assertIn("没翻完", text)

    def test_a_person_is_read_with_mql_and_shown_by_name_only(self):
        decision = self.decision()
        lg = self.lg("preview")
        text = self.quiet(ledger.digest, lg, decision)
        self.assertIn("决定人：研究员甲", text)
        self.assertNotIn("example.com", text)
        mql = next(argv[argv.index("--mql") + 1] for argv, _, _ in lg.calls if "query" in argv)
        self.assertIn("WHERE `work_item_id` = %s" % decision, mql)
        odd = {"data": {"1": [{"moql_field_list": [{"key": lg.field("decision", "决定人")["field_key"],
                                                    "value": {"user_value_list": [{"email": "r@example.com", "name_cn": "甲"}]}}]}]}}
        text, printed = self.out(ledger.digest, self.lg("preview", [(("workitem", "query"), odd)]), decision)
        self.assertNotIn("决定人", text)
        self.assertIn("读不懂", printed)
        self.assertNotIn("example.com", text + printed)
        text, printed = self.out(ledger.digest, self.lg("preview", [(("workitem", "query"), ledger.LedgerError("down"))]), decision)
        self.assertIn("人员字段读不出来", printed)
        self.assertIn("决定内容：同意", text)

    def test_write_log_ids_have_no_trailing_newline(self):
        lg = ledger.Ledger(self.tmp, "apply")
        lg.log(["meegle", "workitem", "create"], 0, '{\n  "work_item_id": 7152343534\n}\n')
        with open(os.path.join(self.tmp, "feishu", "write-log.jsonl"), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read().splitlines()[-1])["ids"], ["7152343534"])

    def test_trace_follows_the_source(self):
        self.task()
        source = self.quiet(ledger.register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        child = self.quiet(ledger.register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("；来源工作项 #2100", "")), source=source)
        _, text = self.out(ledger.trace, self.lg("preview"), child)
        self.assertEqual([line.split()[0] for line in text.splitlines()], ["#" + child, "#" + source])


if __name__ == "__main__":
    unittest.main()
