#!/usr/bin/env python3
"""Tests for ledger.py on node flows: python3.12 -I -B tests/test_ledger.py

The space is tests/fake_space.py: the structure of the reference template, behaving as the
real space was seen to. Nothing here calls Feishu.
"""
import contextlib
import datetime
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
import docs  # noqa: E402
import ledger_core  # noqa: E402
import workitem  # noqa: E402
import orgspec  # noqa: E402

EXAMPLES = os.path.join(ROOT, "skills", "packages", "references", "examples.md")
ASSIGN, DELIVERY, CROSS, EXECUTE, EXECUTE_DELIVERY, DECISION, DECIDED, BY_HAND = 0, 1, 2, 5, 6, 7, 8, 9   # blocks of examples.md
PERMISSION, REVERSIBLE = 10, 11    # blocks of examples.md: an irreversible decision whose options list irreversible actions, with frozen actions; a reversible one
# BY_HAND's 研究员要做的事: the same whichever option the researcher picks
API_KEY = "用自己的账号在模型服务的控制台建一个 API key，照定下的金额设好每天的花费上限，再把 key 填进 gpu-01 上实验平台的配置"
SESSION_DOES = "- 执行方：Alg-p2-01\n- 研究员要做的事：无\n"             # DECISION's two lines: its sender carries it out
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
STEPS = [   # the 正式实验 flow: who asks for each step, and what its form requires
    ("Product-p2-01", ["成功标准=复现分数和公开数字相差 0.5 个点以内"]),
    ("Alg-p2-01", ["调研文档=" + LINK]),
    ("Product-p2-01", ["需求文档=" + LINK, "评测集版本=evalset-v1"]),
    ("Alg-p2-01", ["规格=" + LINK, "规格版本=spec-v1", "总预算=200", "波动估计=基线重复 3 次，标准差 0.2"]),
    ("Alg-p2-01", ["实验方案=" + LINK, "实验轮次=1"]),
    ("Infra-p2-01", ["冒烟运行 ID=smoke-0001"]),
    ("RnD-p2-01", ["运行 ID=run-0001,run-0002"]),
    ("Alg-p2-01", ["结论=成立", "结论文档=" + LINK]),
    ("PMO-p2-01", []),
]


def example(n):
    with open(EXAMPLES, encoding="utf-8") as f:
        return re.findall(r"```markdown\n(.*?)\n```", f.read(), re.S)[n]


def by_hand(value):
    """An edit of DECISION: the researcher carries it out and must do value by hand."""
    return lambda t: t.replace(SESSION_DOES, "- 执行方：研究员\n- 研究员要做的事：%s\n" % value)


register = fake_space.register         # upload, register, relink: a registration as its two sessions carry it out
reply = fake_space.reply               # upload, reply: a delivery likewise


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-lab-ledger-")
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
        with self.assertRaises(ledger_core.LedgerError) as e, contextlib.redirect_stdout(io.StringIO()):
            fn(*args, **kw)
        return str(e.exception)

    def task(self, flow="正式实验", sender="产品 +2（Product-p2-01）", name="task.md"):
        return self.quiet(register, self.lg(), self.write(SHEET % (flow, sender), name))

    def state(self, item_id):
        return self.fake.state_of(self.fake.item(item_id))

    def uploaded(self, path):
        """The upload receipt packages/sent/upload-<draft file>.json of a draft."""
        with open(os.path.join(self.tmp, "packages", "sent", "upload-%s.json" % os.path.basename(path)), encoding="utf-8") as f:
            return json.load(f)

    def scribe_writes(self, lg, item_id):
        """What a revision asked the scribe for, written as the scribe writes it. From 1.1.0 docs.revise replaces
        the document and leaves the work item alone: what the new version changes on it is printed as one
        `workitem.py update` (lg.field_asks holds its values) for the author to send to the scribe."""
        asks = list(getattr(lg, "field_asks", None) or [])
        if asks:
            self.quiet(workitem.update, self.lg(), item_id, ["%s=%s" % pair for pair in asks])
        return asks

    def receipt(self, item_id):
        """The registration receipt packages/sent/<ID>.json."""
        with open(os.path.join(self.tmp, "packages", "sent", "%s.json" % item_id), encoding="utf-8") as f:
            return json.load(f)

    def edit_receipt(self, item_id, *drop, **values):
        """The registration receipt as an earlier version wrote it: the keys in drop left out, values changed."""
        rec = self.receipt(item_id)
        for key in drop:
            del rec[key]
        rec.update(values)
        with open(os.path.join(self.tmp, "packages", "sent", "%s.json" % item_id), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)

    def value(self, item_id, wid, ref):
        return self.fake.item(item_id)["fields"].get(self.lg().field(wid, ref)["field_key"], "")

    def decision(self, task_id=None, result="批准", decided=True):
        """A decision work item, as the scribe leaves it after the researcher decided."""
        lg = self.lg()
        fields = [{"field_key": "name", "field_value": "决策包（PMO-p2-01）：签字"}, workitem.flow_entry(lg, "decision"),
                  lg.entry("decision", "sequence", "项目管理"), lg.entry("decision", "sender", "PMO-p2-01"),
                  lg.entry("decision", "main_doc", LINK), lg.entry("decision", "urgency", "今天"), lg.role_owners("decision")]
        if task_id:
            fields.append(lg.entry("decision", "task_ref", task_id))
        did = str(self.fake.create("decision", fields)["work_item_id"])
        if decided:
            sets = ["决定内容=同意", "决定人=研究员"] + (["签字结果=" + result] if result else [])
            self.quiet(workitem.confirm, lg, did, "研究员", sets)
        return did

    def walk(self, task_id, upto, signer=None):
        """Confirm the steps of a 正式实验 task up to (not including) step number `upto`."""
        signs = {}
        for i, (by, sets) in enumerate(STEPS[:upto - 1], 1):
            if self.fake.item(task_id)["nodes"][i - 1]["status"] != "doing":
                continue
            if i in (4, 9):
                signs[i] = signer or self.decision(task_id)
                sets = sets + ["%s=%s" % ("假设签字" if i == 4 else "下一步签字", signs[i])]
            self.quiet(workitem.confirm, self.lg(), task_id, by, sets)
        return signs


class RegisterTest(LedgerCase):
    def test_research_task_sheet(self):
        path = self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"))
        lg = self.lg()
        item_id, text = self.out(register, lg, path)
        item = self.fake.item(item_id)
        t = self.fake.snap["types"][item["type_key"]]
        self.assertEqual((t["api_name"], t["flows"][item["flow_id"]]["name"], item["name"]), ("research_task", "正式实验", "复现并测量基线"))
        self.assertEqual(self.state(item_id), "确定目标")
        self.assertEqual((self.value(item_id, "research_task", "sender"), self.fake.shown(item["type_key"], lg.field("research_task", "sequence")["field_key"],
                                                                                           self.value(item_id, "research_task", "sequence"))["label"]),
                         ("Product-p2-01", "产品"))
        doc = next(iter(self.fake.docs.values()))
        self.assertEqual((doc["title"], doc["parent"]), ("工作项 %s｜研究任务单：复现并测量基线" % item_id, "node_research_task"))
        self.assertEqual(self.value(item_id, "research_task", "main_doc"), "https://example.feishu.cn/wiki/" + doc["node_token"])
        self.assertIn("- 关联工作项：[#%s](https://project.feishu.cn/" % item_id, doc["body"])
        self.assertIn("## 状态包\n### 简介\n在公开评测集上复现基线", re.sub(r"\n+", "\n", doc["body"]))   # the 目标 starts the 状态包
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
        self.quiet(register, lg, self.write(SHEET % ("探索", "算法 +2（Alg-p2-01）")))
        created = json.loads(lg.wrote("workitem", "create")[0][lg.wrote("workitem", "create")[0].index("--fields") + 1])
        keys = {f["field_key"] for f in created}
        need = {c["field_key"] for c in lg.space_type("research_task")["create_fields"] if c["required"]}
        self.assertTrue(need <= keys)
        self.assertTrue(all(isinstance(f["field_value"], str) for f in created))               # meegle takes strings only
        doc = next(f for f in created if f["field_key"] == lg.field("research_task", "main_doc")["field_key"])
        self.assertEqual(doc["field_value"], "https://example.feishu.cn/wiki/wik0001")       # the document its author uploaded:
        self.assertEqual(lg.wrote("workitem", "update"), [])                               # the item never points anywhere else

    def test_sheet_header_and_flow_are_checked_first(self):
        text = self.fails(register, self.lg(), self.write("# 研究任务单：x\n\n- 发出方：产品 +2（Product-p2-01）\n- 关联工作项：待建\n"))
        self.assertIn("“- 流程：…”", text)
        text = self.fails(register, self.lg(), self.write(SHEET % ("冲刺", "产品 +2（Product-p2-01）")))
        self.assertIn("正式实验、探索", text)
        self.assertIn("没有“冲刺”", text)
        text = self.fails(register, self.lg(), self.write((SHEET % ("正式实验", "产品 +2（Product-p2-01）")).replace("待建", "#2001")))
        self.assertIn("这一行写“待建”", text)
        text = self.fails(register, self.lg(), self.write(SHEET % ("正式实验", "<序列> +2（<会话名>）")))
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
        bare = self.task(sender="Alg-p2-01", name="bare.md")                       # only the session: its sequence, not the entry's
        item = self.fake.item(bare)
        self.assertEqual(self.fake.shown(item["type_key"], lg.field("research_task", "sequence")["field_key"],
                                         self.value(bare, "research_task", "sequence"))["label"], "算法")
        self.assertEqual(self.value(bare, "research_task", "sender"), "Alg-p2-01")

    def test_a_revised_sheet_takes_the_place_of_the_one_before_in_the_same_document(self):
        """1.0.10: a 研究任务单 is revised in place, like a package. Until 1.0.9 its revision became a new document
        (register --existing): the link changed every time, the comments stayed on the one before, and the old
        documents piled up under the node."""
        task = self.task()
        old = self.value(task, "research_task", "main_doc")
        count = len(self.fake.docs)
        revised = self.write((SHEET % ("正式实验", "算法 +2（Alg-p2-01）")).replace("测出分数和波动", "测出分数、波动和耗时"), "v2.md")
        lg = self.lg()
        _, text = self.out(docs.revise, lg, task, revised, "补耗时")
        self.assertEqual(lg.wrote("workitem", "create"), [])                             # the work item stays,
        self.assertEqual(self.value(task, "research_task", "main_doc"), old)             # and so do the document
        self.assertEqual(len(self.fake.docs), count)                                      # and its link
        self.assertIn("第 2 版（文档链接不变）", text)
        body = self.fake.doc_of_link(old)["body"]
        opening = body.split("## 状态包")[0]
        self.assertIn("测出分数、波动和耗时", opening)
        self.assertNotIn("测出分数和波动。", opening)
        self.assertIn("## 状态包", body)                                                  # the document keeps its own
        self.assertIn("第 2 版（%s）：补耗时" % datetime.date.today().isoformat(), body)
        self.assertEqual(self.value(task, "research_task", "sender"), "Product-p2-01")   # the work item is the scribe's:
        self.assertIn("workitem.py update %s --set 发出方=Alg-p2-01 --set 序列=算法 --apply" % task, text)
        self.scribe_writes(lg, task)
        self.assertEqual(self.value(task, "research_task", "sender"), "Alg-p2-01")       # sender and sequence follow
        self.assertEqual(self.receipt(task)["version"], 2)
        with open(revised, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s" % task, f.read())
        third = self.write(SHEET % ("正式实验", "算法 +2（Alg-p2-01）"), "v3.md")              # no 版本 line: counted here
        self.quiet(docs.revise, self.lg(), task, third, "改回原来的目标")
        self.assertEqual(self.receipt(task)["version"], 3)
        self.assertEqual(self.value(task, "research_task", "main_doc"), old)
        other = self.task(name="t9.md")
        with open(revised, encoding="utf-8") as f:
            named = self.write(f.read(), "v2c.md")                                       # it names the first task
        self.assertIn("关联工作项", self.fails(docs.revise, self.lg(), other, named, "x"))
        probe = self.write(SHEET % ("探索", "产品 +2（Product-p2-01）"), "v4.md")
        self.assertIn("流程在新建时选定", self.fails(docs.revise, self.lg(), task, probe, "x"))
        self.assertIn("反馈的主文档没有状态包", self.fails(docs.revise, self.lg(), task, self.write(FEEDBACK, "f.md"), "x"))

    def test_a_sheet_is_revised_after_its_first_step_too_and_says_so(self):
        """The researcher's ruling of 2026-11-20: a task past its first step may have its sheet revised, and
        answers for it. The command says where the task is and goes on."""
        task = self.task()
        self.quiet(workitem.confirm, self.lg(), task, *STEPS[0])
        late = self.write((SHEET % ("正式实验", "产品 +2（Product-p2-01）")).replace("0.5 个点", "1 个点"), "late.md")
        _, text = self.out(docs.revise, self.lg(), task, late, "放宽标准")
        self.assertIn("已经过了第一步", text)
        self.assertIn("不会跟着研究任务单变", text)
        self.assertIn("1 个点", self.fake.doc_of_link(self.value(task, "research_task", "main_doc"))["body"])

    def test_a_revision_with_a_new_title_renames_the_item_and_its_document(self):
        """1.0.10: nothing renamed a main document; an item whose title changed kept a document under the old
        one. A revision whose title differs replaces the document's title block, which renames the document and
        its node in the knowledge space with it; the item is renamed by the scribe, whom the author asks (1.1.0)."""
        task = self.task()
        doc = self.fake.doc_of_link(self.value(task, "research_task", "main_doc"))
        self.assertIn("复现并测量基线", doc["title"])
        renamed = self.write((SHEET % ("正式实验", "产品 +2（Product-p2-01）")).replace("# 研究任务单：复现并测量基线", "# 研究任务单：复现基线 & 测波动"), "r.md")
        lg = self.lg()
        _, text = self.out(docs.revise, lg, task, renamed, "改名")
        self.assertEqual(self.fake.item(task)["name"], "复现并测量基线")                 # the work item is the scribe's to rename:
        self.assertIn("给书记员发【登记请求】，请它运行下面这条命令。\n"                      # the author is given the command for it
                      "python3.12 -I .claude/solo-lab/bin/workitem.py update %s --set '名称=复现基线 & 测波动' --apply" % task, text)
        self.assertEqual(self.scribe_writes(lg, task), [("名称", "复现基线 & 测波动")])
        self.assertEqual(self.fake.item(task)["name"], "复现基线 & 测波动")
        self.assertEqual(doc["title"], "工作项 %s｜研究任务单：复现基线 & 测波动" % task)
        self.assertIn("改名：主文档和它的知识库节点的标题", text)
        same = self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"), "s.md")
        self.fake.title_fails = True                                   # the rename is refused: the revision still lands
        _, text = self.out(docs.revise, self.lg(), task, same, "改回")
        self.assertIn("主文档的标题没有改成", text)
        self.assertIn("请在网页上把它改成“工作项 %s｜研究任务单：复现并测量基线”" % task, text)
        self.assertEqual(self.receipt(task)["version"], 3)

    def test_the_sheet_init_writes_registers_as_it_is(self):
        import setup_core
        path = self.write(setup_core.first_task_sheet({"name": "约束优化器", "description": "面向代码的约束优化器"}), "first.md")
        task = self.quiet(register, self.lg(), path)
        self.assertEqual(self.state(task), "确定目标")
        self.assertEqual(self.fake.item(task)["name"], "约束优化器")

    def test_feedback_sheet(self):
        task = self.task()
        item_id = self.quiet(register, self.lg(), self.write(FEEDBACK, "fb.md"))
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
        item_id, text = self.out(register, lg, self.draft(ASSIGN))
        self.assertEqual(self.state(item_id), "待开工")
        self.assertEqual(self.fake.item(item_id)["name"], "需求包（Product-p2-01）：代码优化器的需求文档")
        self.assertEqual((self.value(item_id, "same_seq", "task_ref"), self.value(item_id, "same_seq", "owner_session")), (task, "待委托"))
        doc = self.fake.doc_by(self.value(item_id, "same_seq", "main_doc").rsplit("/", 1)[1])
        self.assertEqual(doc["parent"], "node_same_seq_product")
        self.assertIn("confirm %s --by <承接方的会话名>" % item_id, text)

    def test_a_package_names_its_task_when_the_space_has_none_or_several(self):
        text = self.fails(register, self.lg(), self.draft(ASSIGN))
        self.assertIn("没归档的研究任务有 0 条", text)
        first, second = self.task(name="t1.md"), self.task(name="t2.md")
        text = self.fails(register, self.lg(), self.draft(ASSIGN))
        self.assertIn("有 2 条", text)
        self.assertIn("--task <ID>", text)
        item_id = self.quiet(register, self.lg(), self.draft(ASSIGN), task=second)
        self.assertEqual(self.value(item_id, "same_seq", "task_ref"), second)
        by_header = self.draft(ASSIGN, "h.md", edit=lambda t: t.replace("- 需要接收方做的事", "- 所属研究任务：#%s\n- 需要接收方做的事" % first, 1))
        self.assertEqual(self.value(self.quiet(register, self.lg(), by_header), "same_seq", "task_ref"), first)

    def test_execute_request_hangs_under_its_source(self):
        self.task()
        source = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        text = self.fails(register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("- 来源工作项：#2100\n", "")))
        self.assertIn("--source <ID>", text)
        item_id = self.quiet(register, self.lg(), self.draft(EXECUTE, "e2.md", edit=lambda t: t.replace("- 来源工作项：#2100\n", "")), source=source)
        self.assertEqual(self.value(item_id, "same_seq", "source_item"), source)
        self.assertEqual(self.value(item_id, "same_seq", "owner_session"), "Quality-p0-%s" % item_id)   # the +0's work name
        parent = self.fake.doc_by(self.value(source, "same_seq", "main_doc").rsplit("/", 1)[1])["node_token"]
        self.assertEqual(self.fake.doc_by(self.value(item_id, "same_seq", "main_doc").rsplit("/", 1)[1])["parent"], parent)
        wrong = self.draft(ASSIGN, "e3.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "产品 +1（Product-p1-03）"))
        self.assertIn("职级对不上", self.fails(register, self.lg(), wrong))

    def test_a_p2_without_a_p1_hands_a_closed_task_to_its_p0(self):
        """1.0.12: a sequence that opens with its +2 alone (产品, 算法 and 外联 by default) gives a closed task to a +0
        itself: a request of the 执行 relation, under the work item the task belongs to. A +1 writes 执行 only."""
        task = self.task()
        by_p2 = lambda t: t.replace("- 来源工作项：#2100\n", "").replace("质量 +1（Quality-p1-02）", "质量 +2（Quality-p2-01）")
        item_id = self.quiet(register, self.lg(), self.draft(EXECUTE, "p2.md", edit=by_p2), source=task)
        self.assertEqual(self.value(item_id, "same_seq", "source_item"), task)
        self.assertEqual(self.value(item_id, "same_seq", "owner_session"), "Quality-p0-%s" % item_id)   # the work name a 执行 request gets
        self.assertIn("--source <ID>", self.fails(register, self.lg(), self.draft(EXECUTE, "p2b.md", edit=by_p2)))
        down = self.draft(ASSIGN, "p1.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "产品 +1（Product-p1-03）"))
        text = self.fails(register, self.lg(), down)
        self.assertIn("职级对不上", text)
        self.assertIn("本序列没开 +1、直接发给 +0 的是执行", text)

    def test_the_router_gets_the_receipts_of_a_cross_request(self):
        """1.0.12: the two +2 of a 跨序列需求 ask the scribe themselves and talk to each other, so each thing the
        scribe writes on one ends with a line that sends the receipt to the router too. Not on other items,
        and not on a preview."""
        self.task()
        cross = self.quiet(register, self.lg(), self.draft(CROSS, "c.md"))
        same = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        line = "跨序列需求：这条回执另发一份给路由 PMO-p1-01"
        self.assertIn(line, self.out(ledger_core.also_to_router, self.lg(), cross)[1])
        self.assertIn(line, self.out(ledger_core.also_to_router, self.lg(), "#" + cross)[1])
        self.assertEqual(self.out(ledger_core.also_to_router, self.lg(), same)[1], "")
        self.assertEqual(self.out(ledger_core.also_to_router, self.lg("preview"), cross)[1], "")
        self.assertEqual(self.out(ledger_core.also_to_router, self.lg(), None)[1], "")

    def test_cross_request_is_put_in_by_its_sender(self):
        self.task()
        item_id, text = self.out(register, self.lg(), self.draft(CROSS))
        self.assertEqual(self.state(item_id), "待接单")                       # 提出 is confirmed with the registration
        self.assertIn("“提出”是发出方自己的一步，已随登记确认", text)
        item = self.fake.item(item_id)
        lg = self.lg()
        label = lambda ref: self.fake.shown(item["type_key"], lg.field("cross_request", ref)["field_key"], self.value(item_id, "cross_request", ref))["label"]
        self.assertEqual((label("from_sequence"), label("to_sequence"), label("sequence")), ("产品", "算法", "算法"))

    def test_decisions_from_the_queue_and_from_the_researcher(self):
        self.task()
        dated = lambda t: t.replace("- 回复期限：2026-11-29", "- 回复期限：2026-12-01")
        item_id, text = self.out(register, self.lg(), self.draft(DECISION, edit=dated))
        self.assertEqual(self.state(item_id), "待决定")
        self.assertNotIn("跳过字段", text)                                              # a decision has no deadline field
        self.assertNotIn("研究员主动发起的决策", text)
        item_id, text = self.out(register, self.lg(), self.draft(DECIDED, "d2.md"))
        self.assertIn("confirm %s --by 研究员 --set 决定内容='停止条件的“四周”从搜索开始计时，试点期另设两周观察。' --set 决定人=研究员" % item_id, text)

    def test_the_receipt_says_what_was_registered(self):
        """The inbox hook of the session the 【登记回执】 reaches reads type, queue and urgent off the receipt;
        query queue reads a decision's 回复期限, 执行方 and 研究员要做的事 there."""
        task = self.task()
        self.assertEqual((self.receipt(task)["type"], self.receipt(task)["queue"]), ("research_task", False))
        urgent = self.draft(DECISION, "u.md", edit=lambda t: t.replace("- 紧急程度：今天\n", "- 紧急程度：插队\n"))
        rec = self.receipt(self.quiet(register, self.lg(), urgent))
        self.assertEqual((rec["type"], rec["status"], rec["due"]), ("decision", "registered", "2026-11-29"))
        self.assertIs(rec["queue"], True)
        self.assertIs(rec["urgent"], True)
        self.assertIn("researcher_action", rec)
        self.assertIsNone(rec["researcher_action"])                          # 研究员要做的事：无
        self.assertEqual(rec["executor"], "Alg-p2-01")                       # 执行方：Alg-p2-01
        rec = self.receipt(self.quiet(register, self.lg(), self.draft(DECISION, "today.md")))
        self.assertEqual((rec["type"], rec["queue"], rec["urgent"]), ("decision", True, False))
        rec = self.receipt(self.quiet(register, self.lg(), self.draft(BY_HAND, "hand.md")))
        self.assertEqual((rec["executor"], rec["researcher_action"]), ("研究员", API_KEY))
        rec = self.receipt(self.quiet(register, self.lg(), self.draft(
            DECISION, "named.md", edit=lambda t: t.replace("- 执行方：Alg-p2-01\n", "- 执行方：算法 +2（Alg-p2-01）\n"))))
        self.assertEqual(rec["executor"], "Alg-p2-01")                       # written like 发出方: the session's name
        rec = self.receipt(self.quiet(register, self.lg(), self.draft(
            DECIDED, "d2.md", edit=lambda t: t.replace("- 紧急程度：可延后\n", "- 紧急程度：插队\n"))))
        self.assertEqual((rec["type"], rec["executor"]), ("decision", "Product-p2-01"))
        self.assertIs(rec["queue"], False)                                   # the researcher decided it in a talk
        for block, name, wid in ((ASSIGN, "a.md", "same_seq"), (CROSS, "c.md", "cross_request")):
            rec = self.receipt(self.quiet(register, self.lg(), self.draft(block, name)))
            self.assertEqual((rec["type"], rec["queue"], rec["urgent"]), (wid, False, False), name)
            self.assertNotIn("researcher_action", rec, name)
            self.assertNotIn("executor", rec, name)
        rec = self.receipt(self.quiet(register, self.lg(), self.write(FEEDBACK, "fb.md")))
        self.assertEqual(rec["type"], "feedback")
        def action(n, value, edit):
            return self.receipt(self.quiet(register, self.lg(), self.draft(DECISION, "action-%d.md" % n, edit=edit)))["researcher_action"]
        for n, value in enumerate(("无", "无。", "无，发出方自己办", "无：发出方自己办", "无（发出方自己办）")):   # nothing for the researcher
            self.assertIsNone(action(n, value, lambda t: t.replace("- 研究员要做的事：无\n", "- 研究员要做的事：%s\n" % value)), value)
        for n, value in enumerate(("（今晚以前）把真实数据放进 data/raw/", "(今晚以前) 把真实数据放进 data/raw/",
                                   "无论选哪个，都要研究员在 gpu-01 上装好驱动"), 10):           # kept as written
            self.assertEqual(action(n, value, by_hand(value)), value)

    def test_a_decision_the_researcher_carries_out(self):
        """2026-11-16: what the researcher must do by hand is a decision; its package header says so: 执行方
        研究员, and the work in 研究员要做的事. It goes 待决定 → 待执行 (已决定 in this space, made from template 2.1.0; confirmed as soon as
        the researcher decides) → 归档 (carried out), and query queue lists it until it is archived. The stuck
        work item names it in its blocked reason."""
        text = example(BY_HAND)
        self.assertIn("\n- 渠道：决策队列\n", text)
        self.assertIn("\n- 需要接收方做的事：拍板\n- 执行方：研究员\n- 研究员要做的事：%s\n" % API_KEY, text)
        self.assertEqual(orgspec.lint_text(self.spec, text)[0], [])
        task = self.task()
        hand = self.quiet(register, self.lg(), self.draft(BY_HAND, "hand.md"))
        self.assertEqual(self.state(hand), "待决定")
        self.assertEqual((self.receipt(hand)["executor"], self.receipt(hand)["researcher_action"]), ("研究员", API_KEY))
        rows, out = self.out(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"], r["executor"], r["researcher_action"]) for r in rows],
                         [(hand, "待决定", "研究员", API_KEY)])
        self.assertRegex(out, r"(?m)^#%s  待决定  .*  执行方：研究员  研究员要做的事：%s  主文档：https://\S+$" % (hand, re.escape(API_KEY)))
        waits = "等研究员：决策 #%s" % hand
        self.quiet(workitem.update, self.lg(), task, ["blocked_reason=" + waits])
        rows = self.quiet(ledger.query, self.lg("preview"), "blocked")
        self.assertEqual([(str(r["work_item_id"]), r["blocked_reason"]) for r in rows], [(task, waits)])
        # the researcher decides: 决定 is confirmed at once, and the decision waits for the researcher to carry it out
        _, out = self.out(workitem.confirm, self.lg(), hand, "研究员", ["决定内容=每天上限 30 美元", "决定人=研究员"], option="每天 30 美元")
        self.assertEqual(out.splitlines()[-1], "执行方：研究员。")              # nobody else to tell
        self.assertEqual((self.state(hand), self.value(hand, "decision", "决定内容")), ("已决定", "每天上限 30 美元"))
        rows, out = self.out(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"], r["work_item_status"]) for r in rows], [(hand, "待执行", "已决定")])
        self.assertRegex(out, r"(?m)^#%s  待执行  .*  执行方：研究员  研究员要做的事：%s  主文档：https://\S+$" % (hand, re.escape(API_KEY)))
        # one a session carries out leaves the queue once it is decided: the scribe tells that session what was decided
        other = self.quiet(register, self.lg(), self.draft(DECISION, "q.md"))
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [hand, other])
        _, out = self.out(workitem.confirm, self.lg(), other, "研究员", ["决定内容=移进评测协议", "决定人=研究员"])
        self.assertEqual(out.splitlines()[-1], "执行方：Alg-p2-01。")
        self.assertEqual(self.state(other), "已决定")
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [hand])
        # the researcher says it is done: the scribe comments what was done and archives it
        self.quiet(workitem.comment, self.lg(), hand, "研究员建好 API key，每天上限设成 30 美元，key 填进了 gpu-01 上实验平台的配置（原话：“key 挂好了，上限 30”）")
        self.quiet(workitem.archive, self.lg(), hand)
        self.assertEqual(self.state(hand), "归档")
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "queue"), [])
        self.quiet(workitem.update, self.lg(), task, ["blocked_reason="])
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "blocked"), [])

    def test_what_the_researcher_does_stays_listed_however_it_opens(self):
        """研究员要做的事 opening with a bracket is still something for the researcher: query queue lists the
        decision as 待执行 once it is decided."""
        value = "（今晚以前）把真实数据放进 data/raw/"
        hand = self.quiet(register, self.lg(), self.draft(DECISION, "b.md", edit=by_hand(value)))
        self.assertEqual(self.receipt(hand)["researcher_action"], value)
        self.quiet(workitem.confirm, self.lg(), hand, "研究员", ["决定内容=移进评测协议", "决定人=研究员"])
        self.assertEqual(self.state(hand), "已决定")
        rows = self.quiet(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"], r["researcher_action"]) for r in rows], [(hand, "待执行", value)])

    def test_a_decision_package_says_what_the_researcher_does_by_hand(self):
        """The package check wants 研究员要做的事 in a decision package's header (无 when nothing), and in no other package."""
        good = example(DECISION)
        self.assertIn("\n- 研究员要做的事：无\n", good)
        self.assertEqual(orgspec.lint_text(self.spec, good)[0], [])
        without = good.replace("- 研究员要做的事：无\n", "")
        errors = orgspec.lint_text(self.spec, without)[0]
        self.assertEqual(errors, ["包头缺“研究员要做的事”：写这条决策定下以后要研究员亲手做的事，没有写“无”"])
        self.assertIn("草稿没通过检查", self.fails(register, self.lg(), self.write(without)))
        self.assertEqual(self.fake.items, {})
        for value, why in (("", "没写内容"), ("<这条决策定下以后要研究员亲手做的事；没有写“无”>", "还是模板里的占位")):
            errors = orgspec.lint_text(self.spec, good.replace("- 研究员要做的事：无\n", "- 研究员要做的事：%s\n" % value))[0]
            self.assertEqual(len(errors), 1, errors)
            self.assertIn(why, errors[0])
        for value in ("没有", "不需要", "无需研究员动手", "暂无", "N/A", "不用研究员做", "—"):   # nothing, written otherwise
            text = good.replace("- 研究员要做的事：无\n", "- 研究员要做的事：%s\n" % value)
            self.assertEqual(orgspec.lint_text(self.spec, text)[0], [
                "包头“研究员要做的事”写的是“%s”：没有要研究员亲手做的事，只写“无”；有的话，直接写要他做什么" % value], value)
        self.assertIn("草稿没通过检查", self.fails(register, self.lg(), self.write(text, "dash.md")))
        self.assertEqual(self.fake.items, {})
        for value in ("在 gpu-01 上装好驱动", "无论选哪个，都要研究员在 gpu-01 上装好驱动", "NAS 上的原始数据由研究员拷到 gpu-01"):
            self.assertEqual(orgspec.lint_text(self.spec, by_hand(value)(good))[0], [], value)
        for block in (ASSIGN, DELIVERY, CROSS, EXECUTE, EXECUTE_DELIVERY):     # the other packages have no such lines
            text = example(block)
            self.assertNotIn("研究员要做的事", text)
            self.assertNotIn("\n- 执行方：", text)
            self.assertEqual(orgspec.lint_text(self.spec, text)[0], [], block)
        with open(os.path.join(ROOT, "skills", "packages", "assets", "decision.md"), encoding="utf-8") as f:
            template = orgspec.template_block(f.read())
        self.assertIn("\n- 需要接收方做的事：拍板（决策队列）／确认记录无误（研究员主动发起）\n"
                      "- 执行方：<研究员，或者执行这条决策的会话名：各序列的 +2、入口或路由，多半是发出方；只要有一个选项要研究员亲手做，就写“研究员”>\n- 研究员要做的事：<", template)

    def test_a_decision_package_names_who_carries_it_out(self):
        """2026-11-16: a decision package's header says who carries it out once it is decided (执行方): 研究员, or
        one session, mostly the sender. The researcher has something to do by hand (研究员要做的事), a session has
        无. The package check sends back a package whose two lines disagree, and the scribe registers nothing."""
        good = example(DECISION)
        self.assertIn("\n- 需要接收方做的事：拍板\n" + SESSION_DOES, good)
        # what only the researcher can do (the task file, init) goes in a package he carries out, not in this one
        signed = good.split("\n## 待签版本\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("再重跑 init，这两步要研究员亲手做，写进重新签字的那份决策包：那份包的执行方写“研究员”", signed)
        lint = lambda text: orgspec.lint_text(self.spec, text)[0]
        idle = good.replace(SESSION_DOES, "- 执行方：研究员\n- 研究员要做的事：无\n")
        self.assertEqual(lint(idle), ["执行方是研究员，包头“研究员要做的事”却写“无”：写清楚要研究员亲手做什么；"
                                      "不用他亲手做什么的，执行方改写执行这条决策的会话名"])
        busy = good.replace(SESSION_DOES, "- 执行方：Alg-p2-01\n- 研究员要做的事：把真实数据放进 data/raw/\n")
        self.assertEqual(lint(busy), ["执行方是 Alg-p2-01，包头“研究员要做的事”就要写“无”；要研究员亲手做这件事的，执行方改写“研究员”"])
        for text, name in ((idle, "idle.md"), (busy, "busy.md")):
            self.assertIn("草稿没通过检查", self.fails(register, self.lg(), self.write(text, name)))
        self.assertEqual(self.fake.items, {})
        for who, action in (("Alg-p2-01", "无"), ("算法 +2（Alg-p2-01）", "无（发出方自己办）"), ("Infra-p2-01", "无"),
                            ("研究员", "把真实数据放进 data/raw/"), ("研究员", "（今晚以前）把真实数据放进 data/raw/")):
            self.assertEqual(lint(good.replace(SESSION_DOES, "- 执行方：%s\n- 研究员要做的事：%s\n" % (who, action))), [], who)
        self.assertEqual(lint(example(BY_HAND)), [])
        hint = "写“研究员”，或者执行这条决策的那个会话的名字：各序列的 +2、入口或路由（多半是发出方的会话名），只写名字、不加说明，比如 Alg-p2-01"
        self.assertEqual(lint(good.replace("- 执行方：Alg-p2-01\n", "")), ["包头缺“执行方”：" + hint])
        for value, why in (("", "没写内容"), ("<研究员，或者执行这条决策的会话名：各序列的 +2、入口或路由，多半是发出方；只要有一个选项要研究员亲手做，就写“研究员”>", "还是模板里的占位"),
                           ("发出方", "写的是“发出方”"), ("Alg-p2-01（发出方）", "写的是“Alg-p2-01（发出方）”"),
                           ("研究员和 Alg-p2-01", "写的是“研究员和 Alg-p2-01”")):
            errors = lint(good.replace("- 执行方：Alg-p2-01\n", "- 执行方：%s\n" % value))
            self.assertEqual(len(errors), 1, errors)                        # whether the two agree waits until both are right
            self.assertIn(why, errors[0])
            self.assertTrue(errors[0].endswith(hint), errors[0])

    def test_the_executor_is_one_of_the_organizations_sessions(self):
        """A session named as 执行方 is the organization's: its abbreviation names a sequence (case aside, as the
        ledger compares names), and a sequence and rank written in front are its own. The receipt, confirm and
        route.py name the session as the abbreviation spells it."""
        good = example(DECISION)
        lint = lambda who: orgspec.lint_text(self.spec, good.replace(SESSION_DOES, "- 执行方：%s\n- 研究员要做的事：无\n" % who))[0]
        for who, why in (("Algo-p2-01", "序列的简写只有 Product、Alg、RnD、Infra、Quality、PMO、Liaison，没有 Algo"),
                         ("Foo-p2-01", "序列的简写只有 Product、Alg、RnD、Infra、Quality、PMO、Liaison，没有 Foo"),
                         ("产品 +2（Alg-p2-01）", "Alg-p2-01 是算法 +2 的会话，前面写的序列和职级要和它一致，或者只写会话名"),
                         ("算法 +1（Alg-p2-01）", "Alg-p2-01 是算法 +2 的会话，前面写的序列和职级要和它一致，或者只写会话名")):
            self.assertEqual(lint(who), ["包头“执行方”写的是“%s”：%s" % (who, why)], who)
        self.assertIn("草稿没通过检查", self.fails(register, self.lg(), self.write(good.replace("Alg-p2-01\n- 研究员", "Algo-p2-01\n- 研究员"))))
        self.assertEqual(self.fake.items, {})
        for who, name in (("alg-p2-01", "Alg-p2-01"), ("算法 +2（alg-p2-01）", "Alg-p2-01"), ("项目管理 +2（PMO-p2-01）", "PMO-p2-01")):
            self.assertEqual(lint(who), [], who)
            self.assertEqual(orgspec.executor_of(self.spec, who), name, who)

    def test_the_executor_is_a_p2_the_entry_or_the_router(self):
        """1.0.6 (the researcher, 2026-11-16): the session a decision package names as 执行方 answers for the matter:
        a sequence's +2, the entry (项目管理 +2) or the router (项目管理 +1). A +1, a +0 and the scribe are sent back
        with what to write instead, and nothing is registered. A receipt that names another session from before
        is read and archived as it is."""
        good = example(DECISION)
        named = lambda who: good.replace(SESSION_DOES, "- 执行方：%s\n- 研究员要做的事：无\n" % who)
        lint = lambda who: orgspec.lint_text(self.spec, named(who))[0]
        for who in ("Alg-p2-01", "Quality-p2-01", "Liaison-p2-01", "PMO-p2-01", "项目管理 +1（PMO-p1-01）", "pmo-p1-01"):
            self.assertEqual(lint(who), [], who)
        self.assertEqual(orgspec.executor_of(self.spec, "pmo-p1-01"), "PMO-p1-01")
        only = "执行方只收研究员、各序列的 +2、入口和路由。"
        how = "；要下级干的活，由 +2 用需求包委托"
        for who, name, what, p2 in (("Alg-p1-01", "Alg-p1-01", "算法 +1 的会话", "Alg-p2-01"),
                                    ("算法 +1（Alg-p1-02）", "Alg-p1-02", "算法 +1 的会话", "Alg-p2-01"),
                                    ("Quality-p0-2106", "Quality-p0-2106", "质量 +0 的会话", "Quality-p2-01"),
                                    ("rnd-p0-01", "RnD-p0-01", "工程 +0 的会话", "RnD-p2-01"),
                                    ("PMO-p0-01", "PMO-p0-01", "书记员", "PMO-p2-01")):
            self.assertEqual(lint(who), ["包头“执行方”写的是“%s”：%s 是%s：%s改写负责这件事的 +2 的会话名（比如 %s）%s" % (
                who, name, what, only, p2, how)], who)
            self.assertIsNone(orgspec.executor_of(self.spec, who), who)
        self.assertIn("草稿没通过检查", self.fails(register, self.lg(), self.write(named("Alg-p1-01"))))
        self.assertEqual(self.fake.items, {})
        # registered before 1.0.6 with a +1 as 执行方: its receipt is read as it is, in the queue's list and at the archive
        old = self.quiet(register, self.lg(), self.draft(DECISION, "old.md"))
        self.edit_receipt(old, executor="Alg-p1-01")
        _, out = self.out(workitem.confirm, self.lg(), old, "研究员", ["决定内容=移进评测协议", "决定人=研究员"])
        self.assertEqual(out.splitlines()[-1], "执行方：Alg-p1-01。")
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "queue"), [])          # a session carries it out
        self.quiet(workitem.archive, self.lg(), old)
        self.assertEqual(self.state(old), "归档")

    def test_a_researcher_who_carries_it_out_is_never_told_to_write_nothing(self):
        """When 执行方 is 研究员, a 研究员要做的事 that is missing, empty, the template's placeholder or nothing written
        otherwise is sent back to say what the researcher does, or to hand the decision to a session with 无: the
        advice leads to a package that passes, not to the disagreement the next check would send back."""
        good = example(DECISION)
        lint = lambda text: orgspec.lint_text(self.spec, text)[0]
        mine = lambda action: good.replace(SESSION_DOES, "- 执行方：研究员\n" + ("" if action is None else "- 研究员要做的事：%s\n" % action))
        does = ("执行方是研究员，写清楚要研究员亲手做什么；不用他亲手做什么的，执行方改写执行这条决策的会话名，"
                "“研究员要做的事”写“无”")
        for action, why in ((None, "包头缺“研究员要做的事”"), ("", "包头“研究员要做的事”没写内容"),
                            ("<这条决策定下以后要研究员亲手做的事；没有写“无”>", "包头“研究员要做的事”还是模板里的占位"),
                            ("没有", "包头“研究员要做的事”写的是“没有”"), ("不需要", "包头“研究员要做的事”写的是“不需要”")):
            self.assertEqual(lint(mine(action)), [why + "：" + does], action)
        self.assertEqual(lint(mine("把真实数据放进 data/raw/")), [])                         # either way the advice goes
        self.assertEqual(lint(good), [])                                                  # (the session and 无)

    def test_query_queue_keeps_what_the_researcher_carries_out(self):
        """query queue lists a decided decision until it is archived only when the researcher carries it out.
        The receipt's executor says who does; a receipt older than the 执行方 line goes by researcher_action:
        something for the researcher makes him the executor, and confirm names the sender otherwise."""
        self.task()
        new = lambda name, edit=None: self.quiet(register, self.lg(), self.draft(BY_HAND if name == "mine" else DECISION,
                                                                                     name + ".md", edit=edit))
        mine, theirs = new("mine"), new("theirs")
        old_hand, old_none, older = new("old_hand", by_hand("把真实数据放进 data/raw/")), new("old_none"), new("older")
        odd = new("odd")
        self.edit_receipt(old_hand, "executor")                             # 1.0.3 before the 执行方 line
        self.edit_receipt(old_none, "executor")
        self.edit_receipt(older, "executor", "researcher_action")           # 1.0.2 and before
        self.edit_receipt(odd, researcher_action="把真实数据放进 data/raw/")   # the executor decides, not researcher_action
        rows = self.quiet(ledger.query, self.lg("preview"), "queue")
        self.assertEqual({str(r["work_item_id"]): (r["stage"], r.get("executor")) for r in rows},
                         {mine: ("待决定", "研究员"), theirs: ("待决定", "Alg-p2-01"), old_hand: ("待决定", "研究员"),
                          old_none: ("待决定", None), older: ("待决定", None), odd: ("待决定", "Alg-p2-01")})
        said = {}
        for item_id in (mine, theirs, old_hand, old_none, older, odd):
            _, out = self.out(workitem.confirm, self.lg(), item_id, "研究员", ["决定内容=照建议办", "决定人=研究员"],
                              option="每天 30 美元" if item_id == mine else None)     # the option picked, for the record
            said[item_id] = out.splitlines()[-1]
        self.assertEqual(said, {mine: "执行方：研究员。", theirs: "执行方：Alg-p2-01。", old_hand: "执行方：研究员。",
                                old_none: "执行方：Alg-p2-01。", older: "执行方：Alg-p2-01。", odd: "执行方：Alg-p2-01。"})
        rows, out = self.out(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"], r["executor"]) for r in rows],
                         [(mine, "待执行", "研究员"), (old_hand, "待执行", "研究员")])
        self.assertIn("#%s  待执行  " % old_hand, out)

    def test_what_the_new_item_page_requires_is_asked_for_before_creating(self):
        self.task()
        lg = self.lg()
        path = self.draft(DECISION, edit=lambda t: t.replace("- 紧急程度：今天\n", "- 紧急程度：插队 / 今天 / 本周 / 可延后\n"))
        text = self.fails(register, lg, path)
        self.assertIn("这些字段必填，草稿里读不出来：紧急程度", text)
        self.assertEqual(lg.wrote("workitem", "create"), [])
        self.quiet(register, lg, path, pairs=["紧急程度=本周"])
        self.assertEqual(len(lg.wrote("workitem", "create")), 1)

    def test_preview_writes_nothing(self):
        path = self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"))
        _, text = self.out(docs.upload, self.lg("preview"), path)                       # the author's preview
        self.assertIn("将执行：lark-cli docs +create --parent-token node_research_task --title 研究任务单：复现并测量基线", text)
        self.assertIn("预览：会把文档建在节点 node_research_task 下", text)
        self.assertEqual((self.fake.items, self.fake.docs), ({}, {}))
        self.assertIn("这份草稿的文档还没上传", self.fails(workitem.register, self.lg("preview"), path))
        self.quiet(docs.upload, self.lg(), path)
        _, text = self.out(workitem.register, self.lg("preview"), path)                 # the scribe's preview
        self.assertIn("将执行：meegle workitem create", text)
        self.assertIn("预览：会登记一条研究任务", text)
        self.assertNotIn("\n登记：", "\n" + text)
        self.assertEqual((self.fake.items, len(self.fake.docs)), ({}, 1))

    def test_a_registration_that_fails_half_way_is_resumed_without_a_second_item(self):
        """The scribe's step has one place left to stop at: the item exists, with its document in it from the
        first write, and what follows (a +0's work name, the comment) failed."""
        path = self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"))
        down = [(("comment", "add"), ledger_core.LedgerError("命令失败（退出码 1）：meegle comment add\n503"))]
        text = self.fails(register, self.lg(replies=down), path)
        self.assertIn("--resume --apply", text)
        self.assertIn("主文档字段已经填上", text)
        item_id = next(iter(self.fake.items))
        self.assertTrue(self.value(item_id, "research_task", "main_doc").startswith("https://example.feishu.cn/wiki/"))
        self.assertIn("加 --resume", self.fails(register, self.lg(), path))
        self.assertEqual(self.quiet(register, self.lg(), path, resume=True), item_id)
        self.assertEqual((len(self.fake.items), len(self.fake.docs)), (1, 1))
        self.assertIn("不要重复登记", self.fails(register, self.lg(), path))


class Template220Test(LedgerCase):
    """Template 2.2.0 (plugin 1.0.5) gave the decision two text fields, 执行方 and 研究员要做的事, which its view
    待定决策 shows and picks the researcher's decisions by, and renamed the state 已决定 to 待执行. The two fields are
    no contract slots: register and revise write the package header's two lines into them by name where the space
    has them. A space made from 2.1.0 or earlier (the fake space by default) has neither and keeps 已决定."""

    def use_220(self):
        snap = fake_space.template_220()
        fake_space.install(self.tmp, snap)
        self.fake = fake_space.FakeSpace(snap)

    def fields(self, item_id):
        """(执行方, 研究员要做的事) as the work item holds them; None where it holds nothing."""
        held = self.fake.item(item_id)["fields"]
        return tuple(held.get(key) for key, _ in fake_space.FIELDS_220)

    def test_the_two_header_lines_go_into_their_fields(self):
        self.use_220()
        task = self.task()
        hand, out = self.out(register, self.lg(), self.draft(BY_HAND, "hand.md"))
        self.assertNotIn("跳过字段", out)
        self.assertEqual(self.fields(hand), ("研究员", API_KEY))                  # what the view filters by: 执行方 = 研究员
        named = lambda t: t.replace("- 执行方：Alg-p2-01\n", "- 执行方：算法 +2（Alg-p2-01）\n")
        other = self.quiet(register, self.lg(), self.draft(DECISION, "q.md", edit=named))
        self.assertEqual(self.fields(other), ("Alg-p2-01", "无"))                # the session's name, as the receipt keeps it
        self.assertEqual(self.receipt(other)["executor"], "Alg-p2-01")
        lg = self.lg()                                                            # a --set value for one of them wins
        given = self.quiet(register, lg, self.draft(DECISION, "s.md"), pairs=["研究员要做的事=无，Alg-p2-01 自己改协议"])
        created = json.loads(lg.wrote("workitem", "create")[0][lg.wrote("workitem", "create")[0].index("--fields") + 1])
        self.assertEqual([f["field_value"] for f in created if f["field_key"] == fake_space.FIELDS_220[1][0]], ["无，Alg-p2-01 自己改协议"])
        self.assertEqual(self.fields(given), ("Alg-p2-01", "无，Alg-p2-01 自己改协议"))
        # decided: 待执行 is the state's name in this space, and the stages and the signature check go by it
        self.assertIn("`work_item_status` IN ('待决定', '待执行')", ledger_core.mql_text(self.lg("offline"), "queue")[0])
        _, out = self.out(workitem.confirm, self.lg(), hand, "研究员", ["决定内容=每天上限 30 美元", "决定人=研究员"], option="每天 30 美元")
        self.assertIn("状态“待执行”", out)
        self.assertEqual(out.splitlines()[-1], "执行方：研究员。")
        self.assertEqual(self.state(hand), "待执行")
        rows = self.quiet(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"], r["work_item_status"]) for r in rows],
                         [(hand, "待执行", "待执行"), (other, "待决定", "待决定"), (given, "待决定", "待决定")])
        self.walk(task, 5)                                                        # step 4 signs with a decision at 待执行
        self.assertEqual(self.state(task), "设计实验方案")

    def test_a_revision_asks_for_the_two_fields_again(self):
        self.use_220()
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        self.assertEqual(self.fields(item), ("Infra-p2-01", "无"))
        mine = lambda t: t.replace("- 执行方：Infra-p2-01\n- 研究员要做的事：无\n",
                                   "- 执行方：研究员\n- 研究员要做的事：在本机装好 Docker Desktop 并登录\n")
        lg = self.lg()
        _, out = self.out(docs.revise, lg, item, self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22", mine)), "改由研究员执行")
        self.assertIn("修订：#%s 的主文档开头换成第 2 版" % item, out)
        self.assertEqual(self.fields(item), ("Infra-p2-01", "无"))                      # the fields wait for the scribe
        self.assertIn("workitem.py update %s --set 执行方=研究员 --set '研究员要做的事=在本机装好 Docker Desktop 并登录' --apply" % item, out)
        self.scribe_writes(lg, item)
        self.assertEqual(self.fields(item), ("研究员", "在本机装好 Docker Desktop 并登录"))
        self.assertEqual((self.receipt(item)["executor"], self.receipt(item)["researcher_action"]), ("研究员", "在本机装好 Docker Desktop 并登录"))

    def test_a_space_without_the_two_fields_registers_as_before(self):
        """A space made from 2.1.0 or earlier: no field is written for the two lines and nothing is said about it;
        the rest is registered and revised as before, and its state 已决定 still counts as 待执行."""
        self.task()
        item, out = self.out(register, self.lg(), self.draft(PERMISSION))
        self.assertNotIn("跳过字段", out)
        self.assertIn("登记：决策 #%s" % item, out)
        self.assertEqual(self.state(item), "待决定")
        self.assertEqual(ledger_core.header_fields(self.lg(), "decision", ledger_core.Draft(self.spec, self.draft(PERMISSION, "again.md"))), [])
        _, out = self.out(docs.revise, self.lg(), item, self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22")), "改日期")
        self.assertNotIn("跳过字段", out)
        self.assertEqual(self.receipt(item)["version"], 2)
        self.assertIn("`work_item_status` IN ('待决定', '已决定')", ledger_core.mql_text(self.lg("offline"), "queue")[0])

    def test_a_space_brought_up_to_2_2_0_while_the_project_keeps_its_old_files(self):
        """What an old project sees once its space was brought up to 2.2.0 by hand (two fields added, 已决定 renamed
        待执行), step by step. Nothing done: the two header lines are written nowhere, and the ledger still asks
        the space for 已决定, so a decided decision the researcher carries out is missing from the queue, the
        confirmation does not say who carries it out, and a signature that names it is refused. The snapshot read
        again (space.py snapshot --apply, any session): the two fields are written; the state is still asked for
        by its old name. Bound again (space.py feishu --live --apply, the researcher's session): everything."""
        new = fake_space.template_220()
        self.fake = fake_space.FakeSpace(new)                         # the space is 2.2.0; feishu/ids.json and space.json are old
        task = self.task()
        hand, out = self.out(register, self.lg(), self.draft(BY_HAND, "hand.md"))
        self.assertNotIn("跳过字段", out)
        self.assertEqual(self.fields(hand), (None, None))
        _, out = self.out(workitem.confirm, self.lg(), hand, "研究员", ["决定内容=每天上限 30 美元", "决定人=研究员"])
        self.assertEqual(self.state(hand), "待执行")
        self.assertNotIn("执行方：", out)                              # the binding calls this state 已决定
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "queue"), [])
        self.assertIn("状态是“待执行”，要是“已决定”或“归档”", self.fails(self.walk, task, 5))
        with open(os.path.join(self.tmp, "feishu", "space.json"), "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False)                     # what space.py snapshot --apply writes
        other = self.quiet(register, self.lg(), self.draft(DECISION, "q.md"))
        self.assertEqual(self.fields(other), ("Alg-p2-01", "无"))
        self.assertIn("`work_item_status` IN ('待决定', '已决定')", ledger_core.mql_text(self.lg("offline"), "queue")[0])
        rows = self.quiet(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([str(r["work_item_id"]) for r in rows], [other])                 # the decided one is still missing
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(fake_space.bound_ids(new), f, ensure_ascii=False)                   # what space.py feishu --live --apply writes
        rows = self.quiet(ledger.query, self.lg("preview"), "queue")
        self.assertEqual([(str(r["work_item_id"]), r["stage"]) for r in rows], [(hand, "待执行"), (other, "待决定")])
        self.walk(task, 5)
        self.assertEqual(self.state(task), "设计实验方案")

    def test_confirming_may_hand_the_decision_to_a_session(self):
        """1.0.6 (the researcher, 2026-11-16): a package one of whose options needs the researcher's own hands says
        执行方：研究员. When the researcher picks an option that does not, the scribe confirms 决定 with --executor
        <会话名>: the receipt's executor becomes that session and its researcher_action null, the space's two fields
        follow where it has them, a comment records it, and from then on the decision goes as one a session
        carries out: query queue no longer lists it as waiting for the researcher. Without --executor the header
        stands. A rollback to 待决定 puts back what the header says."""
        decided = ["决定内容=每天上限 30 美元，Infra-p2-01 用现成的 key", "决定人=研究员"]
        for with_fields in (True, False):
            if with_fields:
                self.use_220()
            else:
                self.fake = fake_space.FakeSpace()
                fake_space.install(self.tmp)
            task = self.task(name="task-%s.md" % with_fields)
            name = "hand-%s.md" % with_fields
            hand = self.quiet(register, self.lg(), self.draft(BY_HAND, name))
            done = "已决定" if not with_fields else "待执行"
            # what --executor takes: a session's name, under the package header's check
            text = self.fails(workitem.confirm, self.lg(), hand, "研究员", decided, executor="研究员")
            self.assertIn("--executor 只收会话名：要把 #%s 的执行方改成研究员，请发出方写下一版（包头“执行方”写“研究员”，“研究员要做的事”写清楚要他做什么），"
                          "书记员用 docs.py revise 换主文档以后再确认" % hand, text)
            text = self.fails(workitem.confirm, self.lg(), hand, "研究员", decided, executor="Infra-p1-01")
            self.assertIn("--executor 写的是“Infra-p1-01”：Infra-p1-01 是基础设施 +1 的会话：执行方只收研究员、各序列的 +2、入口和路由。"
                          "改写负责这件事的 +2 的会话名（比如 Infra-p2-01）；要下级干的活，由 +2 用需求包委托", text)
            self.assertIn("--executor 写的是“PMO-p0-01”：PMO-p0-01 是书记员", self.fails(workitem.confirm, self.lg(), hand, "研究员", decided, executor="PMO-p0-01"))
            self.assertIn("序列的简写只有", self.fails(workitem.confirm, self.lg(), hand, "研究员", decided, executor="Infro-p2-01"))
            self.assertIn("执行方用 --executor 改：去掉 --set 里的“执行方”和“研究员要做的事”" if with_fields else "没有字段“执行方”",
                          self.fails(workitem.confirm, self.lg(), hand, "研究员", decided + ["执行方=Infra-p2-01"], executor="Infra-p2-01"))
            self.assertEqual((self.state(hand), self.receipt(hand)["executor"]), ("待决定", "研究员"))
            before = len(self.fake.comments)
            _, out = self.out(workitem.confirm, self.lg("preview"), hand, "研究员", decided, option="每天 30 美元", executor="infra-p2-01")
            note = ("研究员选的选项：每天 30 美元（不可逆的动作：花钱：模型服务每天最多 30 美元）；"
                    "执行方改成 Infra-p2-01（包头写的是研究员），“研究员要做的事”记“无”")
            self.assertIn("[PMO+0] " + note, out)
            self.assertEqual((self.state(hand), self.receipt(hand)["executor"], len(self.fake.comments)), ("待决定", "研究员", before))
            lg = self.lg()
            _, out = self.out(workitem.confirm, lg, hand, "研究员", decided, option="每天 30 美元", executor="infra-p2-01")
            self.assertIn(note + "。已记进回执和评论。", out)
            self.assertEqual(out.splitlines()[-1], "执行方：Infra-p2-01。")              # the scribe tells that session
            self.assertIn((hand, "[PMO+0] " + note), self.fake.comments)
            rec = self.receipt(hand)
            self.assertEqual((rec["executor"], rec["researcher_action"], rec["as_packaged"], rec["option"], rec["decided_by"]),
                             ("Infra-p2-01", None, {"executor": "研究员", "researcher_action": API_KEY}, "每天 30 美元", "研究员"))
            self.assertEqual(self.state(hand), done)
            if with_fields:
                self.assertEqual(self.fields(hand), ("Infra-p2-01", "无"))             # the view 待定决策 no longer shows it
                self.assertEqual(len(lg.wrote("workitem", "update")), 1)                # with the step's own values, in one write
            else:
                self.assertEqual(self.fields(hand), (None, None))                       # a space without the two fields: skipped
            self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "queue"), [])    # a session carries it out: not the researcher's
            # --executor is for the 决定 step only
            text = self.fails(workitem.confirm, self.lg(), hand, "研究员", executor="Infra-p2-01")
            self.assertIn("--executor 只用在确认决策的“决定”这一步：#%s 是决策，现在是“%s”；这一步已经确认过（比如研究员在网页上定的），"
                          "要改执行方，先 workitem.py rollback %s --to 决定 --reason '<原因>' --by 研究员，再带 --executor 确认“决定”" % (hand, done, hand), text)
            self.assertIn("--executor 只用在确认决策的“决定”这一步：#%s 是研究任务" % task,
                          self.fails(workitem.confirm, self.lg(), task, "Product-p2-01", ["成功标准=x"], executor="Infra-p2-01"))
            # rolled back to 待决定: the header's 执行方 is back, in the receipt, the local state and the fields
            _, out = self.out(workitem.rollback, self.lg(), hand, "决定", "研究员改主意了", by="研究员")
            self.assertIn("执行方改回包头写的研究员。", out)
            rec = self.receipt(hand)
            self.assertEqual((rec["executor"], rec["researcher_action"], rec["as_packaged"]), ("研究员", API_KEY, None))
            entry = ledger_core.load_gates(self.tmp)["pending"][hand]
            self.assertEqual((entry["executor"], entry["researcher_action"]), ("研究员", API_KEY))
            if with_fields:
                self.assertEqual(self.fields(hand), ("研究员", API_KEY))
            rows = self.quiet(ledger.query, self.lg("preview"), "queue")
            self.assertEqual([(str(r["work_item_id"]), r["stage"], r["executor"]) for r in rows], [(hand, "待决定", "研究员")])
            # without --executor the header stands, as before: the researcher carries it out and it stays listed
            _, out = self.out(workitem.confirm, self.lg(), hand, "研究员", decided)
            self.assertEqual(out.splitlines()[-1], "执行方：研究员。")
            self.assertEqual([(str(r["work_item_id"]), r["stage"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [(hand, "待执行")])
            self.quiet(workitem.archive, self.lg(), hand)
            # the header's own session: nothing changes, nothing is recorded
            other = self.quiet(register, self.lg(), self.draft(DECISION, "q-%s.md" % with_fields))
            _, out = self.out(workitem.confirm, self.lg(), other, "研究员", ["决定内容=移进评测协议", "决定人=研究员"], executor="Alg-p2-01")
            self.assertEqual(out.splitlines()[-1], "执行方：Alg-p2-01。")
            self.assertNotIn("as_packaged", self.receipt(other))
            self.assertNotIn("执行方改成", out)
            # one session in place of another
            third = self.quiet(register, self.lg(), self.draft(DECISION, "r-%s.md" % with_fields))
            _, out = self.out(workitem.confirm, self.lg(), third, "研究员", ["决定内容=移进评测协议", "决定人=研究员"], executor="Quality-p2-01")
            self.assertIn("执行方改成 Quality-p2-01（包头写的是Alg-p2-01），“研究员要做的事”记“无”。已记进回执和评论。", out)
            self.assertEqual((out.splitlines()[-1], self.receipt(third)["executor"]), ("执行方：Quality-p2-01。", "Quality-p2-01"))
            # a revision's header says anew who carries it out
            fourth = self.quiet(register, self.lg(), self.draft(BY_HAND, "s-%s.md" % with_fields))
            self.quiet(workitem.confirm, self.lg(), fourth, "研究员", decided, executor="Infra-p2-01")
            self.quiet(workitem.rollback, self.lg(), fourth, "决定", "再想想", by="研究员")
            self.quiet(docs.revise, self.lg(), fourth, self.draft(BY_HAND, "s2-%s.md" % with_fields, edit=lambda t: t.replace(
                "第 1 版（2026-11-20）", "第 2 版（2026-11-21）")), "补背景")
            self.assertEqual((self.receipt(fourth)["executor"], self.receipt(fourth)["as_packaged"]), ("研究员", None))
            shutil.rmtree(os.path.join(self.tmp, "packages", "sent"))          # the next space starts from nothing
            os.remove(ledger_core.gates_path(self.tmp))

    def test_the_cli_hands_a_decision_over(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = workitem.main(["--offline", "confirm", "2130", "--by", "研究员", "--option", "每天 30 美元", "--executor", "Infra-p2-01",
                                    "--type", "decision"])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("workflow transition --work-item-id 2130", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = workitem.main(["--offline", "confirm", "2130", "--by", "研究员", "--executor", "Infra-p0-01", "--type", "decision"])
            self.assertEqual(code, 1)
            self.assertIn("出错：--executor 写的是“Infra-p0-01”：Infra-p0-01 是基础设施 +0 的会话", buf.getvalue())
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)


class LinkLineTest(unittest.TestCase):
    def line(self, value, item_id="2106"):
        text = "# 需求包：x\n\n- 包类型：需求包\n- 关联工作项：%s\n- 回复期限：一天\n\n## 目标\n关联工作项：待建 这一句在正文里，不动。\n" % value
        return ledger_core.link_line(text, "#" + item_id, item_id).splitlines()[3]

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
            with self.assertRaises(ledger_core.LedgerError):
                self.line(value)
        with self.assertRaises(ledger_core.LedgerError):
            ledger_core.link_line("# x\n\n- 包类型：需求包\n\n## 目标\n", "#1", "1")

    def test_only_the_header_is_touched(self):
        text = "# x\n\n- 关联工作项：待建\n\n## 目标\n- 关联工作项：待建\n"
        self.assertEqual(ledger_core.link_line(text, "#7", "7").count("待建"), 1)


class ReplyTest(LedgerCase):
    def test_delivery_fills_the_field_of_the_request_it_answers(self):
        """1.1.0: the session that delivers uploads the 交付包, the scribe fills the request's field with its link."""
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request), "back.md")
        self.assertIn("这份交付包的文档还没上传：文档谁负责谁写，请 Product-p1-03 先运行", self.fails(workitem.reply, self.lg(), path))
        lg = self.lg()
        link, text = self.out(docs.upload, lg, path)
        parent = self.fake.doc_by(self.value(request, "same_seq", "main_doc").rsplit("/", 1)[1])["node_token"]
        self.assertEqual(self.fake.doc_by(link.rsplit("/", 1)[1])["parent"], parent)
        self.assertEqual((lg.wrote("meegle"), self.value(request, "same_seq", "delivery_link")), ([], ""))   # the field is the scribe's
        self.assertIn("上传：交付包挂在 #%s 的主文档下：%s\n接着给书记员发【登记请求】：请它运行 python3.12 -I .claude/solo-lab/bin/workitem.py reply "
                      "packages/drafts/alg/back.md --apply 填“交付包”字段，再确认“执行”这一步。" % (request, link), text)
        lg = self.lg()
        again, text = self.out(docs.upload, lg, path)                                   # run again: no second document
        self.assertEqual((again, lg.wrote("lark-cli")), (link, []))
        self.assertIn("已经上传过，没有再传", text)
        lg = self.lg()
        filled, text = self.out(workitem.reply, lg, path)
        self.assertEqual((filled, self.value(request, "same_seq", "delivery_link"), lg.wrote("lark-cli")), (link, link, []))
        self.assertIn("“交付包”字段已填", text)
        self.assertIn("不要重复登记", self.fails(workitem.reply, self.lg(), path))
        self.assertIn("不要重复上传", self.fails(docs.upload, self.lg(), path))

    def test_a_delivery_that_was_not_accepted_is_handed_in_again(self):
        """The delivery template says a delivery changed after it was not accepted is handed in again with its
        version one higher. Its author uploads it, from the same file or another: a new document under the
        request, the earlier document where it was; the scribe's reply puts the 交付包 field on the new link.
        The same version again, and a number that does not follow, stop the upload with the number to write."""
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, edit=lambda t: t.replace("- 接收方：产品 +1", "- 接收方：产品 +1（Product-p1-02）")))
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02")
        first = example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request)
        path = self.write(first, "back.md")
        v1 = self.quiet(reply, self.lg(), path)
        receipt = "packages/sent/reply-%s-back.md.json" % request
        with open(os.path.join(self.tmp, receipt), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], 1)
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02")
        self.assertEqual(self.state(request), "待验收")
        self.quiet(workitem.rollback, self.lg(), request, "执行", "第 2 条没有证据", by="Product-p2-01")
        self.assertEqual((self.state(request), self.value(request, "same_seq", "delivery_link")), ("执行中", ""))
        again = "验收不通过、改好后重新交的，包头“版本”写第 2 版，再上传"
        self.assertEqual(self.fails(docs.upload, self.lg(), path),                         # unchanged: the way out is said
                         "这份交付包（第 1 版）已经挂到 #%s 下了（回执 %s），不要重复上传；%s" % (request, receipt, again))
        self.assertIn("这份交付包（第 1 版）已经挂到 #%s 下了（回执 %s），不要重复登记" % (request, receipt), self.fails(workitem.reply, self.lg(), path))
        other = self.write(first, "back-again.md")                                       # another file, the number not raised
        self.assertEqual(self.fails(docs.upload, self.lg(), other),
                         "#%s 已经收过第 1 版交付包（回执 %s），这一份写的是第 1 版：%s" % (request, receipt, again))
        self.write(first.replace("第 1 版（2026-11-02）", "第 3 版（2026-11-05）"), "back.md")
        self.assertIn("这一份写的是第 3 版：" + again, self.fails(docs.upload, self.lg(), path))
        count = len(self.fake.docs)
        self.write(first.replace("第 1 版（2026-11-02）", "第 2 版（2026-11-04）"), "back.md")
        _, out = self.out(docs.upload, self.lg("preview"), path)
        self.assertIn("预览：会把交付包第 2 版挂在 #%s 的主文档下" % request, out)
        self.assertIn("这份交付包的文档还没上传（回执里是第 1 版，草稿是第 2 版）", self.fails(workitem.reply, self.lg(), path))
        self.assertEqual((len(self.fake.docs), self.value(request, "same_seq", "delivery_link")), (count, ""))
        v2, out = self.out(docs.upload, self.lg(), path)
        self.assertIn("上传：交付包第 2 版挂在 #%s 的主文档下：%s，上一版的文档留在原处" % (request, v2), out)
        self.assertEqual((len(self.fake.docs), self.value(request, "same_seq", "delivery_link")), (count + 1, ""))   # the field waits for the scribe
        _, out = self.out(workitem.reply, self.lg(), path)
        self.assertIn("交付包第 2 版已挂到 #%s 下，“交付包”字段已填，上一版的文档留在原处。" % request, out)
        self.assertNotEqual(v1, v2)
        self.assertEqual((len(self.fake.docs), self.value(request, "same_seq", "delivery_link")), (count + 1, v2))
        self.assertIn("第 2 版（2026-11-04）", self.fake.doc_by(v2.rsplit("/", 1)[1])["body"])
        self.assertIn("第 1 版（2026-11-02）", self.fake.doc_by(v1.rsplit("/", 1)[1])["body"])      # the earlier one stays
        self.assertIn((request, "[PMO+0] 收到交付包第 2 版：%s" % v2), self.fake.comments)
        with open(os.path.join(self.tmp, receipt), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual((rec["version"], rec["status"], rec["doc"], rec["earlier"]), (2, "registered", v2, [{"version": 1, "doc": v1}]))
        self.assertIn("这份交付包（第 2 版）已经挂到 #%s 下了" % request, self.fails(workitem.reply, self.lg(), path))
        # a third version from another file; the scribe's write fails once, and both steps are run again
        third = self.write(first.replace("第 1 版（2026-11-02）", "第 3 版（2026-11-06）"), "back-3.md")
        count = len(self.fake.docs)
        v3 = self.quiet(docs.upload, self.lg(), third)
        self.assertEqual(self.fails(workitem.reply, self.lg(replies=[(("workitem", "update"), ledger_core.LedgerError("down"))]), third), "down")
        lg = self.lg()
        self.assertEqual(self.quiet(docs.upload, lg, third), v3)
        _, out = self.out(workitem.reply, lg, third)
        self.assertIn("交付包第 3 版已挂到", out)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs), self.value(request, "same_seq", "delivery_link")), ([], count + 1, v3))
        # a receipt from before 1.0.6 keeps no version: the next one only has to be higher than the first
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02")
        self.quiet(workitem.confirm, self.lg(), request, "Product-p2-01")
        self.assertEqual(self.state(request), "已验收")
        fourth = self.write(first.replace("第 1 版（2026-11-02）", "第 4 版（2026-11-07）"), "back-4.md")
        self.assertIn("#%s 已经是“已验收”：工作项关闭以后不再重新交交付包" % request, self.fails(docs.upload, self.lg(), fourth))
        old = self.quiet(register, self.lg(), self.draft(ASSIGN, "old.md"))
        old_path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % old), "old-back.md")
        self.quiet(reply, self.lg(), old_path)
        old_receipt = os.path.join(self.tmp, "packages", "sent", "reply-%s-old-back.md.json" % old)
        with open(old_receipt, encoding="utf-8") as f:
            rec = json.load(f)
        del rec["version"]
        with open(old_receipt, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        self.assertIn("不要重复上传；" + again, self.fails(docs.upload, self.lg(), old_path))
        self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % old).replace(
            "第 1 版（2026-11-02）", "第 3 版（2026-11-09）"), "old-back.md")
        self.quiet(reply, self.lg(), old_path)
        self.assertTrue(self.value(old, "same_seq", "delivery_link"))

    def test_only_a_delivery_is_a_reply(self):
        self.task()
        self.assertIn("要用 register 登记", self.fails(reply, self.lg(), self.draft(DECISION)))
        decision = self.decision()
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % decision), "back.md")
        self.assertIn("交付包答复的是同序列需求或跨序列需求", self.fails(reply, self.lg(), path))


class StepTest(LedgerCase):
    def test_next_says_who_and_what(self):
        task = self.task()
        text = self.quiet(ledger.next_step, self.lg("preview"), task)
        self.assertIn("#%s  研究任务·正式实验  确定目标  复现并测量基线" % task, text)
        self.assertIn("这一步：确定目标（started），负责方：产品 +2", text)
        self.assertIn("待填 成功标准", text)
        self.assertIn("下一步：前期调研", text)
        self.assertIn("workitem.py confirm %s --by <会话名>" % task, text)
        self.walk(task, 4)
        text = self.quiet(ledger.next_step, self.lg("preview"), task)
        self.assertIn("待填 假设签字（hypothesis_signoff）", text)
        self.assertIn("“假设签字”填一条研究员批准的决策工作项的 ID", text)

    def test_who_may_ask_for_a_step(self):
        task = self.task()
        sets = STEPS[0][1]
        self.assertIn("加 --by <提出请求的会话名>", self.fails(workitem.confirm, self.lg(), task, None, sets))
        for by in ("Alg-p2-01", "Product-p1-01", "PMO-p0-01"):
            self.assertIn("不能请书记员确认这一步", self.fails(workitem.confirm, self.lg(), task, by, sets), by)
        self.assertEqual(self.state(task), "确定目标")
        self.quiet(workitem.confirm, self.lg(), task, "product-p2-01", sets)             # the sequence's +2, any case
        self.quiet(workitem.confirm, self.lg(), task, "研究员", STEPS[1][1])               # the researcher may always
        self.assertEqual(self.state(task), "定需求和评测集")

    def test_the_issuers_and_the_assignees_steps(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))           # Product-p2-01 → 产品 +1, nobody named yet
        self.assertIn("加 --by", self.fails(workitem.confirm, self.lg(), request))
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02", ["owner_session=Product-p1-02"])
        self.assertEqual(self.state(request), "执行中")
        text = self.fails(workitem.confirm, self.lg(), request, "Product-p1-03", ["交付包=" + LINK])
        self.assertIn("负责方是承接方（Product-p1-02）", text)
        self.quiet(workitem.confirm, self.lg(), request, "Product-p2-01", ["交付包=" + LINK])      # the +2 of the assignee's sequence
        self.assertIn("负责方是发出方（Product-p2-01）", self.fails(workitem.confirm, self.lg(), request, "Product-p1-02"))
        self.quiet(workitem.confirm, self.lg(), request, "Product-p2-01")
        self.assertEqual(self.state(request), "已验收")

    def test_a_p0_asks_under_its_pool_name(self):
        self.task()
        source = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        request = self.quiet(register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("- 来源工作项：#2100\n", "")), source=source)
        self.assertEqual(self.value(request, "same_seq", "owner_session"), "Quality-p0-%s" % request)   # the work name
        for by in ("Alg-p0-01", "Quality-p1-02"):
            self.assertIn("不能请书记员确认这一步", self.fails(workitem.confirm, self.lg(), request, by), by)
        self.quiet(workitem.confirm, self.lg(), request, "Quality-p0-01")                # the pool slot of the +0 on the item
        self.assertEqual(self.state(request), "执行中")

    def test_a_preview_says_so_on_every_result_line(self):
        task = self.task()
        self.walk(task, 7)
        _, text = self.out(workitem.confirm, self.lg("preview"), task, "RnD-p2-01", STEPS[6][1])
        self.assertIn("预览：会确认 #%s 的“执行离线评测”" % task, text)
        _, text = self.out(workitem.rollback, self.lg("preview"), task, "设计实验方案", "第 1 轮不够", decision=self.decision(task))
        self.assertIn("预览：会把 #%s 退回到“设计实验方案”" % task, text)
        self.assertIn("会清空 实验轮次，原值：1", text)
        decision = self.decision()
        self.assertIn("预览：会确认 #%s 的最后一步" % decision, self.out(workitem.archive, self.lg("preview"), decision, moved=True)[1])
        self.assertEqual((self.state(task), self.state(decision)), ("执行离线评测", "已决定"))

    def test_everything_missing_is_named_at_once(self):
        task = self.task()
        self.walk(task, 4)
        text = self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", ["规格=" + LINK])
        for name in ("假设签字", "总预算", "波动估计", "规格版本"):
            self.assertIn(name, text)
        self.assertNotIn("规格、", text.replace("规格版本", ""))
        self.assertEqual(self.value(task, "research_task", "规格"), "")               # nothing was written
        self.assertIn("总预算 要写数字", self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", ["总预算=两百"]))
        self.assertIn("没有选项“大概成立”", self.fails(workitem.update, self.lg(), task, ["结论=大概成立"]))
        self.assertIn("研究任务上没有字段“验收结论”", self.fails(workitem.update, self.lg(), task, ["验收结论=接受"]))

    def test_values_are_written_then_the_step_is_confirmed_and_read_back(self):
        task = self.task()
        lg = self.lg()
        _, text = self.out(workitem.confirm, lg, task, "Product-p2-01", STEPS[0][1])
        self.assertIn("确认：#%s “确定目标”完成，现在到“前期调研”，状态“前期调研”。" % task, text)
        self.assertIn("“前期调研”这一步的负责方：算法 +2。", text)           # whom the scribe tells that the task is theirs now
        writes = [argv[1:3] for argv, write, _ in lg.calls if write]
        self.assertEqual(writes, [["workitem", "update"], ["workflow", "transition"]])
        self.assertTrue(self.value(task, "research_task", "成功标准"))
        _, text = self.out(workitem.confirm, self.lg("preview"), task, "Alg-p2-01", STEPS[1][1])
        self.assertIn("将执行：meegle workflow transition", text)
        self.assertEqual(self.state(task), "前期调研")

    def test_a_step_out_of_turn(self):
        task = self.task()
        self.assertIn("现在走到的是“确定目标”", self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", node="prep_context"))
        self.assertIn("没有节点“复盘”", self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", node="复盘"))

    def test_dates_and_people(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))
        self.quiet(workitem.update, self.lg(), request, ["截止时间=2026-12-01"])
        self.assertEqual(self.value(request, "same_seq", "due"), "1796126400000")       # UTC noon, as a string
        self.assertIn("要写成存在的日期", self.fails(workitem.update, self.lg(), request, ["due=2026-02-30"]))
        decision = self.decision(decided=False)
        self.assertIn("只能填“研究员”", self.fails(workitem.confirm, self.lg(), decision, "研究员", ["决定内容=同意", "决定人=张三"]))
        self.quiet(workitem.confirm, self.lg(), decision, "研究员", ["决定内容=同意", "决定人=研究员"])
        self.assertEqual((self.state(decision), self.value(decision, "decision", "决定人")), ("已决定", fake_space.ME))
        self.assertIn("负责方是研究员", self.fails(workitem.rollback, self.lg(), decision, "决定", "重新考虑", by="Alg-p2-01"))


class SignTest(LedgerCase):
    def test_a_signature_is_an_approved_decision_for_this_task(self):
        task, other = self.task(name="t1.md"), self.task(name="t2.md")
        self.walk(task, 4)
        sets = STEPS[3][1]

        def refused(decision):
            return self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % decision])
        self.assertIn("要填一条决策工作项的 ID；#%s 是研究任务" % other, refused(other))
        self.assertIn("状态是“待决定”", refused(self.decision(task, decided=False)))
        self.assertIn("签字结果是“退回”，要是“批准”", refused(self.decision(task, result="退回")))
        self.assertIn("签字结果是空的", refused(self.decision(task, result=None)))
        self.assertIn("所属研究任务是 #%s，不是 #%s" % (other, task), refused(self.decision(other)))
        self.assertIn("所属研究任务是空的", refused(self.decision()))
        self.assertIn("工作项 ID 要写成数字", refused("研究员口头同意"))
        self.assertEqual(self.state(task), "假设评审")
        good = self.decision(task)
        self.quiet(workitem.archive, self.lg(), good, moved=True)                          # an archived decision still signs
        self.quiet(workitem.confirm, self.lg(), task, "Alg-p2-01", sets + ["hypothesis_signoff=%s" % good])
        self.assertEqual(self.state(task), "设计实验方案")

    def test_the_whole_verify_flow(self):
        task = self.task()
        signs = self.walk(task, 10)
        self.assertEqual(sorted(signs), [4, 9])
        self.assertEqual(self.state(task), "已结束")
        self.assertIn("要先把主文档移到归档节点", self.fails(workitem.confirm, self.lg(), task, "PMO-p0-01"))
        lg = self.lg()
        _, text = self.out(workitem.archive, lg, task)
        self.assertEqual(self.state(task), "归档")
        doc = self.fake.doc_by(self.value(task, "research_task", "main_doc").rsplit("/", 1)[1])
        self.assertEqual(doc["parent"], "node_archive_product")
        self.assertIn("已经是“归档”", self.out(workitem.archive, self.lg(), task)[1])
        self.assertIn("流程已经走完", self.quiet(ledger.next_step, self.lg("preview"), task))

    def test_the_probe_flow_has_no_signature(self):
        task = self.task("探索", "算法 +2（Alg-p2-01）")
        for sets in (["探索问题=错误集中在哪一类样本", "总预算=20"], ["探索记录=" + LINK], ["候选假设=重采样能提高长尾类别的准确率", "去向=转正式实验"]):
            self.quiet(workitem.confirm, self.lg(), task, "Alg-p2-01", sets)
        self.assertEqual(self.state(task), "已结束")
        self.quiet(workitem.archive, self.lg(), task)
        self.assertEqual(self.state(task), "归档")


class SpentDecisionTest(LedgerCase):
    """One decision passes one gate once: it has to be made after the step it is used on began."""

    def test_a_decision_made_before_the_step_began(self):
        task = self.task()
        early = self.decision(task)                                                   # decided while the task was at its first step
        self.walk(task, 4)
        text = self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", STEPS[3][1] + ["假设签字=%s" % early])
        self.assertIn("一条决定只用在它决定时所在的那一步", text)
        self.assertEqual(self.state(task), "假设评审")

    def test_the_signature_of_step_four_does_not_sign_step_nine(self):
        task = self.task()
        signs = self.walk(task, 9)
        self.assertIn("重新决定", self.fails(workitem.confirm, self.lg(), task, "PMO-p2-01", ["下一步签字=%s" % signs[4]]))
        self.assertEqual(self.state(task), "决定下一步")

    def test_after_a_rollback_the_old_signature_and_the_rollback_decision_are_spent(self):
        task = self.task()
        signs = self.walk(task, 6)
        back = self.decision(task)
        self.quiet(workitem.rollback, self.lg(), task, "假设评审", "阈值定错了", decision=back)
        sets = STEPS[3][1]
        for spent in (signs[4], back):
            self.assertIn("重新决定", self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % spent]))
        self.quiet(workitem.confirm, self.lg(), task, "Alg-p2-01", sets + ["假设签字=%s" % self.decision(task)])
        self.assertEqual(self.state(task), "设计实验方案")
        self.assertIn("重新决定", self.fails(workitem.rollback, self.lg(), task, "假设评审", "再退一次", decision=back))


class FreezeTest(LedgerCase):
    def frozen_task(self):
        task = self.task()
        self.walk(task, 8)
        return task

    def test_untouched_fields_pass(self):
        task = self.frozen_task()
        self.quiet(workitem.update, self.lg(), task, ["已用预算=30"])                      # not a frozen field
        self.quiet(workitem.confirm, self.lg(), task, *STEPS[7])
        self.assertEqual(self.state(task), "决定下一步")

    def test_a_change_after_signing_stops_the_conclusion(self):
        task = self.frozen_task()
        self.quiet(workitem.update, self.lg(), task, ["总预算=300"])
        self.fake.tick(120000)
        text = self.fails(workitem.confirm, self.lg(), task, *STEPS[7])
        self.assertIn("签字以后，冻结的字段被改过", text)
        self.assertRegex(text, r"总预算：\d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d，200 → 300")
        self.assertEqual(self.state(task), "得出结论")
        self.assertEqual(self.value(task, "research_task", "结论"), "")                 # the conclusion was not written either

    def test_a_change_the_records_do_not_show_yet(self):
        task = self.frozen_task()
        self.fake.tick(600000)
        self.fake.lag = 60000                                                           # records take a minute to show up
        self.quiet(workitem.update, self.lg(), task, ["规格版本=spec-v2"])
        text = self.fails(workitem.confirm, self.lg(), task, *STEPS[7])
        self.assertIn("规格版本：签字时是 spec-v1，现在是 spec-v2（操作记录里还没有这次改动）", text)

    def test_the_researcher_accepts_a_change_with_a_decision(self):
        task = self.frozen_task()
        self.quiet(workitem.update, self.lg(), task, ["总预算=300"])
        self.fake.tick(120000)
        self.assertIn("不合格", self.fails(workitem.confirm, self.lg(), task, *STEPS[7], decision=self.decision(task, result="退回")))
        _, text = self.out(workitem.confirm, self.lg(), task, *STEPS[7], decision=self.decision(task))
        self.assertIn("冻结的字段在签字后改过（1 处），研究员已用决策", text)
        self.assertEqual(self.state(task), "决定下一步")

    def test_signing_again_starts_a_new_freeze(self):
        task = self.frozen_task()
        self.fake.tick(120000)
        self.quiet(workitem.rollback, self.lg(), task, "假设评审", "阈值定高了", decision=self.decision(task))
        self.assertEqual(self.value(task, "research_task", "总预算"), "")               # the undone steps start empty
        self.fake.tick(120000)
        signer = self.decision(task)
        for by, sets in STEPS[3:7]:
            sets = [s.replace("总预算=200", "总预算=150") for s in sets] + (["假设签字=" + signer] if by == "Alg-p2-01" and "规格=" + LINK in sets else [])
            self.quiet(workitem.confirm, self.lg(), task, by, sets)
        self.fake.tick(120000)
        self.quiet(workitem.confirm, self.lg(), task, *STEPS[7])                          # 150 was signed: nothing changed since
        self.assertEqual(self.state(task), "决定下一步")


class RollbackTest(LedgerCase):
    def test_the_main_line_goes_back_only_on_the_researchers_decision(self):
        task = self.task()
        self.walk(task, 7)
        self.assertIn("回退由研究员决定", self.fails(workitem.rollback, self.lg(), task, "design_experiment", "第 1 轮不够", by="研究员"))
        self.assertIn("回滚要写原因", self.fails(workitem.rollback, self.lg(), task, "design_experiment", " ", decision=self.decision(task)))
        self.assertIn("不合格", self.fails(workitem.rollback, self.lg(), task, "design_experiment", "第 1 轮不够",
                                         decision=self.decision(task, result="退回")))
        self.assertIn("只能退回到已完成的节点", self.fails(workitem.rollback, self.lg(), task, "run_eval", "x", decision=self.decision(task)))
        decision = self.decision(task)
        lg = self.lg()
        _, text = self.out(workitem.rollback, lg, task, "设计实验方案", "第 1 轮不够", decision=decision)
        self.assertEqual(self.state(task), "设计实验方案")
        self.assertIn("原因：第 1 轮不够（研究员决定 #%s）" % decision, text)
        for line in ("清空 实验方案，原值：" + LINK, "清空 实验轮次，原值：1", "清空 冒烟运行 ID，原值：smoke-0001"):
            self.assertIn(line, text)
        self.assertEqual(self.value(task, "research_task", "实验轮次"), "")
        self.assertEqual(self.value(task, "research_task", "规格版本"), "spec-v1")      # the steps before stay as they were
        reasons = [c["extra"]["reason_suffix"][0] for r in self.fake.ops if r["operation_type"] == "rollback" for c in r["record_contents"]]
        self.assertTrue(reasons and all(r.endswith("（研究员决定 #%s）" % decision) for r in reasons))
        self.assertIn("待填 实验轮次", self.quiet(ledger.next_step, self.lg("preview"), task))
        self.assertIn("要先填", self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01"))   # the step has to be done again

    def test_a_delivery_that_is_not_accepted(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, edit=lambda t: t.replace("- 接收方：产品 +1", "- 接收方：产品 +1（Product-p1-02）")))
        self.assertEqual(self.value(request, "same_seq", "owner_session"), "Product-p1-02")
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02")
        self.quiet(workitem.confirm, self.lg(), request, "Product-p1-02", ["交付包=" + LINK])
        self.assertIn("负责方是发出方（Product-p2-01）", self.fails(workitem.rollback, self.lg(), request, "执行", "第 2 条没有证据", by="Product-p1-02"))
        self.assertIn("不走决策", self.fails(workitem.rollback, self.lg(), request, "执行", "第 2 条没有证据", decision=self.decision()))
        _, text = self.out(workitem.rollback, self.lg(), request, "执行", "第 2 条没有证据", by="Product-p2-01")
        self.assertEqual((self.state(request), self.value(request, "same_seq", "delivery_link")), ("执行中", ""))
        self.assertIn("清空 交付包", text)

    def test_a_request_handed_back_to_its_sender(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(CROSS))
        self.quiet(workitem.rollback, self.lg(), request, "提出", "范围不清", by="Alg-p2-01")     # the receiving +2 does not take it
        self.assertEqual(self.state(request), "已退回")
        self.assertTrue(self.value(request, "cross_request", "main_doc"))               # what the new-item page required stays
        self.quiet(workitem.confirm, self.lg(), request, "Product-p2-01")
        take = ["需求复述=照三条目标", "承接范围=接受第 1、2 条", "预计交付时间=2026-12-10"]
        self.quiet(workitem.confirm, self.lg(), request, "Alg-p2-01", take)
        self.assertEqual(self.state(request), "执行中")

    def test_an_archived_item_is_reopened_by_the_researcher(self):
        decision = self.decision()
        self.quiet(workitem.archive, self.lg(), decision, moved=True)
        self.assertIn("加 --by 研究员", self.fails(workitem.rollback, self.lg(), decision, "归档", "还要补一条", by="PMO-p2-01"))
        self.quiet(workitem.rollback, self.lg(), decision, "归档", "还要补一条", by="研究员")
        self.assertEqual(self.state(decision), "已决定")


class SourceLineTest(LedgerCase):
    """1.0.9: a 需求包 or 决策包 names the work item it came from in a header line of its own, 来源工作项. Until
    then only the template of an execute request mentioned it, on the 关联工作项 line, and in a project every
    decision and cross-sequence request was registered with the field empty: `ledger.py trace` stopped there.
    The line is asked for by the templates and nothing checks it: a package without it, or with 无, registers."""

    def test_the_templates_ask_for_the_upstream_item(self):
        assets = os.path.join(ROOT, "skills", "packages", "assets")
        for name in ("request.md", "decision.md"):
            with open(os.path.join(assets, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("- 关联工作项：待建\n- 来源工作项：#<引出", text, name)
            self.assertIn("没有上游的写“无”", text, name)
            self.assertNotIn("执行关系写成", text, name)                 # the line it used to share

    def test_a_package_names_its_upstream_item_in_its_header(self):
        self.task()
        cross = self.quiet(register, self.lg(), self.draft(CROSS, "c.md"))
        named = lambda value: lambda t: t.replace("- 关联工作项：待建\n", "- 关联工作项：待建\n- 来源工作项：%s\n" % value)
        item = self.quiet(register, self.lg(), self.draft(PERMISSION, "d.md", edit=named("#%s" % cross)))
        self.assertEqual(self.value(item, "decision", "source_item"), cross)
        none = self.quiet(register, self.lg(), self.draft(REVERSIBLE, "n.md", edit=named("无")))
        self.assertFalse(self.value(none, "decision", "source_item"))    # 无 is no item, and no reason to send it back
        plain = self.draft(REVERSIBLE, "p.md", edit=lambda t: t.replace("用 VPN 拉基线依赖", "用镜像拉基线依赖"))
        with open(plain, encoding="utf-8") as f:
            self.assertNotIn("来源工作项", f.read().split("\n## ", 1)[0])
        self.assertTrue(self.quiet(register, self.lg(), plain))    # nor is a package without the line

    def test_a_revision_asks_for_the_upstream_item(self):
        """A package registered before its template had the line gets it with its next version: revise asks the
        scribe for the field register writes (until 1.0.8 it left the field alone; until 1.0.12 it wrote it)."""
        self.task()
        cross = self.quiet(register, self.lg(), self.draft(CROSS, "c.md"))
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        self.assertFalse(self.value(item, "decision", "source_item"))
        v2 = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22", lambda t: t.replace(
            "- 关联工作项：待建\n", "- 关联工作项：待建\n- 来源工作项：#%s\n" % cross)))
        lg = self.lg()
        _, out = self.out(docs.revise, lg, item, v2, "补来源工作项")
        self.assertFalse(self.value(item, "decision", "source_item"))
        self.assertIn("workitem.py update %s --set 来源工作项=%s --apply" % (item, cross), out)
        self.scribe_writes(lg, item)
        self.assertEqual(self.value(item, "decision", "source_item"), cross)


class UploadTest(LedgerCase):
    """1.1.0: the document first, the work item second. The author uploads the draft (docs.py upload), the
    scribe registers it with the link (workitem.py register), the author writes the item's ID into the document
    (docs.py relink). Three steps of two sessions: each can be run again, and none creates a second document or
    a second work item."""

    def sheet(self, name="t.md", text=None):
        return self.write(text or SHEET % ("正式实验", "产品 +2（Product-p2-01）"), name)

    def test_the_three_steps(self):
        path = self.sheet()
        lg = self.lg()
        url, out = self.out(docs.upload, lg, path)
        doc = next(iter(self.fake.docs.values()))
        self.assertEqual((doc["title"], doc["parent"], url), ("研究任务单：复现并测量基线", "node_research_task", "https://example.feishu.cn/wiki/wik0001"))
        self.assertIn("- 关联工作项：待建\n", doc["body"])                                # no item yet: the line waits
        self.assertTrue(doc["body"].rstrip().endswith("## 交接记录"))
        self.assertEqual(lg.wrote("meegle"), [])                                        # the author writes no work item
        self.assertIn("上传：文档建在节点 node_research_task 下：%s\n接着给书记员发【登记请求】，写上草稿 packages/drafts/alg/t.md；" % url, out)
        self.assertIn("docs.py relink <工作项 ID> --apply", out)
        rec = self.uploaded(path)
        self.assertEqual((rec["status"], rec["doc"], rec["draft"], rec["title"], rec["type"], rec["by"]),
                         ("uploaded", url, "packages/drafts/alg/t.md", "研究任务单：复现并测量基线", "research_task", "研究员"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：待建\n", f.read())                              # the draft is as it was
        lg = self.lg()
        item, out = self.out(workitem.register, lg, path)
        self.assertEqual((lg.wrote("lark-cli"), len(self.fake.docs)), ([], 1))          # the scribe writes no document
        self.assertEqual(self.value(item, "research_task", "main_doc"), url)
        self.assertIn("登记：研究任务 #%s，主文档是 %s。" % (item, url), out)
        self.assertIn("回执里告诉发出方 Product-p2-01：运行 python3.12 -I .claude/solo-lab/bin/docs.py relink %s --apply" % item, out)
        self.assertEqual(self.fake.comments, [(item, "[PMO+0] 已登记：工作项 %s｜研究任务单：复现并测量基线。主文档：%s" % (item, url))])
        with open(path, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s\n" % item, f.read())                        # the scribe writes the ID into the draft
        lg = self.lg()
        _, out = self.out(docs.relink, lg, item)
        self.assertIn("- 关联工作项：[#%s](https://project.feishu.cn/" % item, doc["body"])
        self.assertEqual(doc["title"], "工作项 %s｜研究任务单：复现并测量基线" % item)
        self.assertIn("已把 #%s 写进主文档的“关联工作项”一行。" % item, out)
        self.assertIn("改名：主文档和它的知识库节点的标题从“研究任务单：复现并测量基线”改成“工作项 %s｜研究任务单：复现并测量基线”。" % item, out)
        lg = self.lg()
        _, out = self.out(docs.relink, lg, item)                                         # again: nothing left to do
        self.assertEqual((lg.wrote("lark-cli"), out), ([], "#%s 的主文档已经写着它的 ID，不用改。\n" % item))

    def test_an_upload_run_again(self):
        path = self.sheet()
        url = self.quiet(docs.upload, self.lg(), path)
        lg = self.lg()
        again, out = self.out(docs.upload, lg, path)
        self.assertEqual((again, lg.wrote("lark-cli"), len(self.fake.docs)), (url, [], 1))
        self.assertIn("这份草稿已经上传过，没有再传：%s" % url, out)
        # the draft changes before it is registered: the document follows it, under the same link
        self.sheet(text=(SHEET % ("正式实验", "产品 +2（Product-p2-01）")).replace("测出分数和波动", "测出分数、波动和耗时").replace(
            "# 研究任务单：复现并测量基线", "# 研究任务单：复现基线并测耗时"))
        self.assertIn("草稿在上传以后改过，文档里还是上传时的内容：请 Product-p2-01 重新运行", self.fails(workitem.register, self.lg(), path))
        lg = self.lg()
        again, out = self.out(docs.upload, lg, path)
        doc = next(iter(self.fake.docs.values()))
        self.assertEqual((again, len(self.fake.docs), doc["title"]), (url, 1, "研究任务单：复现基线并测耗时"))
        self.assertIn("测出分数、波动和耗时", doc["body"])
        self.assertTrue(doc["body"].rstrip().endswith("## 交接记录"))
        self.assertIn("草稿在上传以后改过：文档已经换成现在的内容，链接不变：%s" % url, out)
        self.assertEqual([argv[argv.index("--command") + 1] for argv in lg.wrote("docs", "+update")], ["overwrite", "block_replace"])
        item = self.quiet(workitem.register, self.lg(), path)
        self.assertEqual((self.fake.item(item)["name"], self.value(item, "research_task", "main_doc")), ("复现基线并测耗时", url))
        # once it is registered, the next version is a revision
        text = self.fails(docs.upload, self.lg(), path)
        self.assertIn("这份草稿已经登记成 #%s（回执 packages/sent/%s.json），不要重复登记；改了内容的，用 docs.py revise %s" % (item, item, item), text)

    def test_a_document_nobody_relinked_gets_its_item_with_the_next_revision(self):
        path = self.sheet()
        self.quiet(docs.upload, self.lg(), path)
        item = self.quiet(workitem.register, self.lg(), path)
        doc = next(iter(self.fake.docs.values()))
        self.assertEqual(doc["title"], "研究任务单：复现并测量基线")                       # the author forgot docs.py relink
        revised = self.sheet("v2.md", (SHEET % ("正式实验", "产品 +2（Product-p2-01）")).replace("测出分数和波动", "测出分数、波动和耗时"))
        self.quiet(docs.revise, self.lg(), item, revised, "补耗时")
        self.assertEqual(doc["title"], "工作项 %s｜研究任务单：复现并测量基线" % item)
        self.assertIn("- 关联工作项：[#%s](https://project.feishu.cn/" % item, doc["body"])

    def test_a_package_and_what_stops_an_upload(self):
        self.task()
        path = self.draft(DECISION, "dec.md")
        url = self.quiet(docs.upload, self.lg(), path)
        doc = self.fake.doc_of_link(url)
        self.assertEqual((doc["parent"], doc["title"]), ("node_decision", "决策包（Alg-p2-01）：%s" % ledger_core.Draft(self.spec, path).task))
        item = self.quiet(workitem.register, self.lg(), path)
        self.assertEqual(self.value(item, "decision", "main_doc"), url)
        # what register refuses is refused before a document exists
        count = len(self.fake.docs)
        self.assertIn("包头“版本”写第 1 版", self.fails(docs.upload, self.lg(), self.draft(DECISION, "v2.md", edit=lambda t: t.replace("第 1 版", "第 2 版"))))
        self.assertIn("关联工作项", self.fails(docs.upload, self.lg(), self.draft(DECISION, "linked.md", edit=lambda t: t.replace(
            "- 关联工作项：待建", "- 关联工作项：#2999"))))
        self.assertEqual(len(self.fake.docs), count)
        self.assertIn("书记员不建文档", self.fails(workitem.register, self.lg(), self.draft(DECISION, "n.md"), doc="new"))


class WhoWritesTest(LedgerCase):
    """1.1.0: a document is written by the session that answers for it, and every session may run docs.py. The
    script reads the session from its Bash environment and the pool's claims, and holds it against the
    document's owner: the session a work item's 发出方 names, or the +2 of its sequence when 发出方 is no
    session. The researcher's own session, which holds no claim, writes any document."""

    CLAIMS = (("Alg-p2-01", "alg-p2"), ("Alg-p1-01", "alg-p1"), ("Product-p2-01", "product-p2"), ("PMO-p2-01", "pmo-p2"),
              ("PMO-p1-01", "pmo-router"), ("PMO-p0-01", "pmo-scribe"))

    def setUp(self):
        super().setUp()
        import pool
        self.pool = pool
        agents = {}
        for name, role in self.CLAIMS:
            abbrev, rank = name.split("-")[0], int(name.split("-p")[1][0])
            seq = {"Alg": "alg", "Product": "product", "PMO": "pmo"}[abbrev]
            agents[role] = {"tag": "[%s+%d]" % (abbrev, rank), "sequence": seq, "rank": rank}
        os.makedirs(os.path.join(self.tmp, ".claude", "hooks"))
        with open(os.path.join(self.tmp, ".claude", "hooks", "policy.json"), "w", encoding="utf-8") as f:
            json.dump({"agents": agents}, f)
        table = pool.empty()
        for name, role in self.CLAIMS:
            table["claims"][name] = {"name": name, "short": name.lower(), "role": role, "session_id": "sid-" + name, "desk": None}
        table["pending"].append({"session_id": "sid-new", "name": "Alg-p1-02", "role": "alg-p1"})
        pool.save_json(pool.pool_path(self.tmp), table)
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        super().tearDown()

    def as_session(self, name=None):
        """The next Ledger is made in this session; None is the researcher's own, which has no claim."""
        os.environ.pop(self.pool.DESK_ENV, None)
        if name is None:
            os.environ.pop(self.pool.SESSION_ENV, None)
        else:
            os.environ[self.pool.SESSION_ENV] = name if name.startswith("sid-") else "sid-" + name

    def test_the_session_is_read_from_the_claim(self):
        self.as_session("Alg-p2-01")
        lg = self.lg()
        self.assertEqual(lg.caller, {"name": "Alg-p2-01", "role": "alg-p2", "tag": "[Alg+2]", "sequence": "alg", "rank": 2})
        self.assertEqual(lg.tag, "[Alg+2]")
        self.as_session()
        lg = self.lg()
        self.assertEqual((lg.caller, lg.origin, lg.tag), (None, "researcher", "[PMO+0]"))
        self.as_session("sid-new")
        self.assertEqual((self.lg().caller, self.lg().origin), (None, "pending"))

    def test_a_revision_is_its_authors(self):
        self.as_session()
        self.task()
        item = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))            # from Product-p2-01
        v2 = lambda name: self.draft(ASSIGN, name, edit=lambda t: t.replace("第 1 版（2026-10-19）", "第 2 版（2026-10-20）"))
        for other in ("Alg-p1-01", "Alg-p2-01", "PMO-p0-01", "PMO-p1-01"):
            self.as_session(other)
            self.assertEqual(self.fails(docs.revise, self.lg(), item, v2("o.md"), "x"),
                             "（%s）不能修订 #%s 的主文档：这份文档归 Product-p2-01 写，请它来修订。" % (other, item))
            self.assertIn("不能修订", self.fails(docs.revise, self.lg("preview"), item, v2("o.md"), "x"))     # the preview says so too
        self.as_session("sid-new")
        self.assertEqual(self.fails(docs.revise, self.lg(), item, v2("o.md"), "x"),
                         "还没转正的会话不能修订 #%s 的主文档：先认领会话（pool.py claim），认领表有疑问的请研究员用 pool.py show 核对。" % item)
        self.assertEqual(self.receipt(item)["version"], 1)
        self.as_session("Product-p2-01")
        _, out = self.out(docs.revise, self.lg(), item, v2("mine.md"), "补一句")
        self.assertIn("修订：#%s 的主文档开头换成第 2 版" % item, out)
        self.assertIn("[Product+2] 第 2 版（2026-10-20）：补一句",                                   # under its own tag
                      self.fake.doc_of_link(self.value(item, "same_seq", "main_doc"))["body"])
        self.as_session()                                                                # the researcher's session writes any
        v3 = self.draft(ASSIGN, "v3.md", edit=lambda t: t.replace("第 1 版（2026-10-19）", "第 3 版（2026-10-21）"))
        self.quiet(docs.revise, self.lg(), item, v3, "研究员自己改")
        self.assertEqual(self.receipt(item)["version"], 3)

    def test_an_upload_is_its_authors(self):
        self.as_session()
        self.task()
        path = self.draft(ASSIGN, "a.md")                                                # from Product-p2-01
        self.as_session("Alg-p2-01")
        self.assertEqual(self.fails(docs.upload, self.lg(), path), "（Alg-p2-01）不能上传这份文档：这份草稿的发出方是 Product-p2-01，请它来上传。")
        self.as_session("PMO-p0-01")                                                     # nor the scribe, who registers it
        self.assertIn("（PMO-p0-01）不能上传这份文档", self.fails(docs.upload, self.lg(), path))
        self.as_session("Product-p2-01")
        self.quiet(docs.upload, self.lg(), path)
        self.assertEqual(self.uploaded(path)["by"], "Product-p2-01")
        self.as_session("PMO-p0-01")
        item = self.quiet(workitem.register, self.lg(), path)
        self.as_session("Alg-p2-01")
        self.assertIn("（Alg-p2-01）不能改 #%s 的主文档：这份文档归 Product-p2-01 写" % item, self.fails(docs.relink, self.lg(), item))
        self.as_session("Product-p2-01")
        self.quiet(docs.relink, self.lg(), item)
        # a sheet whose sender is no session: the +2 of its sequence, 项目管理 where it names none
        fb = self.write(FEEDBACK, "fb.md")
        self.assertIn("这份草稿的发出方是 项目管理 +2，请它来上传", self.fails(docs.upload, self.lg(), fb))
        self.as_session("PMO-p2-01")
        self.quiet(docs.upload, self.lg(), fb)

    def test_a_delivery_and_a_steps_document_are_their_sessions(self):
        self.as_session()
        task = self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("- 接收方：产品 +1", "- 接收方：产品 +1（Product-p1-03）")))
        back = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request), "back.md")   # from Product-p1-03
        self.as_session("Product-p2-01")
        self.assertEqual(self.fails(docs.upload, self.lg(), back), "（Product-p2-01）不能上传这份交付包：这份交付包的发出方是 Product-p1-03，请它来上传。")
        # a step's document: the session that answers for the step in progress, or the owner of the main document
        os.makedirs(os.path.join(self.tmp, "spec"))
        with open(os.path.join(self.tmp, "spec", "goal.md"), "w", encoding="utf-8") as f:
            f.write("# 调研\n\n三篇相关工作。\n")
        self.as_session()
        self.quiet(workitem.confirm, self.lg(), task, "Product-p2-01", STEPS[0][1])     # now at 前期调研, the algorithm +2's step
        self.as_session("Alg-p1-01")
        self.assertEqual(self.fails(docs.attach, self.lg(), task, "调研文档", "spec/goal.md"),
                         "（Alg-p1-01）不能给 #%s 上传“调研文档”：“前期调研”归算法 +2，主文档归 Product-p2-01 写。" % task)
        self.as_session("Alg-p2-01")
        link = self.quiet(docs.attach, self.lg(), task, "调研文档", "spec/goal.md")
        self.as_session("PMO-p0-01")
        _, out = self.out(workitem.confirm, self.lg(), task, "Alg-p2-01", ["调研文档=@spec/goal.md"])
        self.assertIn("spec/goal.md 用的是 Alg-p2-01 上传的文档：%s" % link, out)
        self.assertEqual(self.value(task, "research_task", "调研文档"), link)

    def test_the_scribe_uploads_the_sheet_init_writes(self):
        """One document is nobody's: the research-task sheet init writes, which the scribe registers while the
        pool opens. The scribe uploads and relinks that one; any other sheet from the researcher is the entry's."""
        import setup_core
        text = setup_core.first_task_sheet({"name": "约束优化器", "description": "面向代码的约束优化器"})
        first = os.path.join(self.tmp, ledger_core.FIRST_TASK)
        os.makedirs(os.path.dirname(first), exist_ok=True)
        with open(first, "w", encoding="utf-8") as f:
            f.write(text)
        self.as_session("PMO-p0-01")
        url = self.quiet(docs.upload, self.lg(), first)
        task = self.quiet(workitem.register, self.lg(), first)
        self.quiet(docs.relink, self.lg(), task)
        self.assertEqual(self.fake.doc_of_link(url)["title"], "工作项 %s｜研究任务单：约束优化器" % task)
        self.assertEqual(self.uploaded(first)["by"], "PMO-p0-01")
        self.assertEqual(self.fails(docs.upload, self.lg(), self.write(text.replace("约束优化器", "另一个任务"), "copy.md")),
                         "（PMO-p0-01）不能上传这份文档：这份草稿的发出方是 项目管理 +2，请它来上传。")

    def test_a_sheet_from_the_researcher_is_its_sequences(self):
        """A 研究任务单 whose 发出方 is 研究员 (the one init writes) is revised by the +2 of its sequence: the
        entry. Once a revision names a session as its sender and the scribe has written it, that session writes."""
        self.as_session()
        import setup_core
        task = self.quiet(register, self.lg(), self.write(setup_core.first_task_sheet({"name": "约束优化器", "description": "面向代码的约束优化器"}), "first.md"))
        sheet = lambda who: (SHEET % ("正式实验", who)).replace("复现并测量基线", "约束优化器")
        self.as_session("Product-p2-01")
        self.assertEqual(self.fails(docs.revise, self.lg(), task, self.write(sheet("产品 +2（Product-p2-01）"), "p.md"), "访谈以后"),
                         "（Product-p2-01）不能修订 #%s 的主文档：这份文档归 项目管理 +2 写，请它来修订。" % task)
        self.as_session("PMO-p2-01")
        lg = self.lg()
        self.quiet(docs.revise, lg, task, self.write(sheet("产品 +2（Product-p2-01）"), "e.md"), "访谈以后")
        self.as_session()
        self.scribe_writes(lg, task)                                                   # 发出方 and 序列 are the scribe's to write
        self.as_session("Product-p2-01")
        self.quiet(docs.revise, self.lg(), task, self.write(sheet("产品 +2（Product-p2-01）"), "p2.md"), "补成功标准")
        self.assertEqual(self.receipt(task)["version"], 3)

    def test_the_status_block_the_log_and_the_nodes(self):
        self.as_session()
        task = self.task()
        status = self.write("### 简介\n复现基线。\n", "status.md")
        for name, ok in (("PMO-p1-01", True), ("PMO-p2-01", True), ("Product-p2-01", True), ("Alg-p2-01", False), ("PMO-p0-01", False)):
            self.as_session(name)
            if ok:
                self.quiet(docs.status_block, self.lg(), task, status)
            else:
                self.assertEqual(self.fails(docs.status_block, self.lg(), task, status),
                                 "（%s）不能换 #%s 的状态包：状态包归 Product-p2-01、路由或入口写。" % (name, task))
        self.as_session("Alg-p2-01")                                                    # every session keeps its own notes
        self.quiet(docs.append, self.lg(), task, self.write("[Alg+2] 接手第 2 步。\n", "note.md"))
        self.assertEqual(self.fails(docs.append, self.lg(), task, self.write("[Product+2] 接手第 2 步。\n", "other.md")),
                         "交接记录第一行的会话标签要是自己的 [Alg+2]：每个会话只记自己的交接")
        self.as_session("sid-new")
        self.assertIn("还没转正的会话不能追加 #%s 的交接记录" % task, self.fails(docs.append, self.lg(), task, self.write("[Alg+1] 接手。\n", "n2.md")))
        self.as_session("Alg-p2-01")
        self.assertEqual(self.fails(docs.ensure_node, self.lg(), "feedback"), "（Alg-p2-01）不能建知识库节点：知识库的节点由书记员建。")
        self.as_session("PMO-p0-01")
        self.quiet(docs.ensure_node, self.lg(), "feedback")
        self.as_session("Alg-p2-01")
        self.quiet(docs.ensure_node, self.lg("offline"), "feedback")                    # offline nothing is written, nothing asked


class WriteBackTest(LedgerCase):
    """1.0.10: register and revise wrote the draft back as they had read it ten seconds earlier, with the work
    item's ID in it: what the author changed in the file meanwhile was gone (it happened to version 2 of a
    request). The file is read again and only the 关联工作项 line changes; the output and the receipt say that
    the main document holds the earlier text."""

    def meanwhile(self, added):
        """ledger.write_back, with the author writing into the draft just before it."""
        real = ledger_core.write_back

        def late(path, read_text, item_id):
            with open(path, "a", encoding="utf-8") as f:
                f.write(added)
            return real(path, read_text, item_id)
        ledger_core.write_back = late
        self.addCleanup(setattr, ledger_core, "write_back", real)

    def test_what_the_author_wrote_meanwhile_stays_in_the_draft(self):
        self.task()
        path = self.draft(PERMISSION)
        self.meanwhile("\n作者在登记期间补的一句。\n")
        item, out = self.out(register, self.lg(), path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("- 关联工作项：#%s\n" % item, text)
        self.assertIn("作者在登记期间补的一句。", text)
        self.assertIn("草稿在命令运行期间改过", out)
        self.assertIn("没有进主文档", out)
        self.assertTrue(self.receipt(item)["draft_changed"])
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if ".tmp-" in n])

    def test_an_untouched_draft_gets_its_item_and_no_note(self):
        self.task()
        path = self.draft(PERMISSION)
        item, out = self.out(register, self.lg(), path)
        with open(path, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s\n" % item, f.read())
        self.assertNotIn("草稿在命令运行期间改过", out)
        self.assertNotIn("draft_changed", self.receipt(item))

    def test_a_revision_keeps_the_authors_edits_too(self):
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        v2 = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22"))
        self.meanwhile("\n修订期间补的一句。\n")
        _, out = self.out(docs.revise, self.lg(), item, v2, "改措辞")
        with open(v2, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("- 关联工作项：#%s\n" % item, text)
        self.assertIn("修订期间补的一句。", text)
        self.assertIn("草稿在命令运行期间改过", out)
        self.assertTrue(self.receipt(item)["draft_changed"])

    def test_a_draft_whose_line_is_gone_is_left_alone(self):
        path = self.write("# 决策包：x\n\n- 包类型：决策包\n- 关联工作项：待建\n\n## 背景\n")
        with open(path, encoding="utf-8") as f:
            read = f.read()
        gone = read.replace("- 关联工作项：待建\n", "")
        with open(path, "w", encoding="utf-8") as f:
            f.write(gone)
        note = ledger_core.write_back(path, read, "2140")
        self.assertIn("#2140", note)
        self.assertIn("草稿没有动", note)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), gone)


class PlaceholderTest(LedgerCase):
    """1.0.10: the 关联工作项 line of two templates read 待建 with a note in brackets after it (the decision
    template since 1.0.9), and register sent back a draft that copied the line: it knew 待建 alone. The
    templates say 待建 and nothing else on that line; drafts written from the earlier ones still register."""

    def test_the_templates_say_only_the_placeholder(self):
        for skill, name in (("packages", "request.md"), ("packages", "decision.md"), ("charter", "charter.md")):
            with open(os.path.join(ROOT, "skills", skill, "assets", name), encoding="utf-8") as f:
                self.assertIn("\n- 关联工作项：待建\n", f.read(), name)

    def test_a_note_after_the_placeholder_is_still_a_placeholder(self):
        self.task()
        noted = lambda t: t.replace("- 关联工作项：待建\n", "- 关联工作项：待建（这份包自己的工作项，登记后由书记员填）\n")
        path = self.draft(PERMISSION, edit=noted)
        item = self.quiet(register, self.lg(), path)
        with open(path, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s\n" % item, f.read())

    def test_another_items_id_is_no_placeholder(self):
        self.assertFalse(ledger_core.is_placeholder("#2100"))
        self.assertFalse(ledger_core.is_placeholder("待建的那一条 #2100"))
        self.assertTrue(ledger_core.is_placeholder("待登记（登记后填）"))


class ArchiveTest(LedgerCase):
    def test_an_archived_item_leaves_the_pools_table_of_owners(self):
        """pool.json owners says which session answers for a work item; the inbox hook writes it when the item
        is handed over, and until 1.0.8 nothing took an entry out: `pool.py show` listed archived items under
        their sessions for good. The archive takes the item out, and touches nothing else in the file."""
        pool = ledger_core.pool
        decision, later = self.decision(), self.decision()
        data = pool.empty()
        data["owners"] = {str(decision): "PMO-p2-01", "2999": "Alg-p0-01"}
        data["refills"] = [{"role": "alg-p0", "count": 1, "by": "Alg-p2-01", "at": "2026-11-19T10:00:00-05:00"}]
        os.makedirs(pool.state_dir(self.tmp), exist_ok=True)
        pool.save_json(pool.pool_path(self.tmp), data)
        self.out(workitem.archive, self.lg("preview"), decision, moved=True)
        self.assertEqual(pool.load(self.tmp)[0]["owners"], data["owners"])           # a preview writes nothing
        _, out = self.out(workitem.archive, self.lg(), decision, moved=True)
        self.assertIn("会话池的认领表里移除了 #%s。" % decision, out)
        self.assertEqual(pool.load(self.tmp)[0], dict(data, owners={"2999": "Alg-p0-01"}))
        _, out = self.out(workitem.archive, self.lg(), later, moved=True)                # an item no session answers for
        self.assertNotIn("认领表", out)
        os.remove(pool.pool_path(self.tmp))                                           # and a project without a pool
        third = self.decision()
        self.quiet(workitem.archive, self.lg(), third, moved=True)
        self.assertFalse(os.path.exists(pool.pool_path(self.tmp)))

    def test_archive_only_at_the_last_step(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))
        lg = self.lg()
        self.assertIn("还没走到最后一步“归档”：现在在“开工”", self.fails(workitem.archive, lg, request))
        # 1.0.11: an item made by mistake is not pushed through its steps; the message says how to ask the researcher
        self.assertIn("todo.py add --for", self.fails(workitem.archive, lg, request))
        self.assertIn("记进研究员任务清单，由研究员在网页上终止", self.fails(workitem.archive, lg, request))
        self.assertEqual(lg.wrote("wiki", "+move"), [])

    def test_a_document_moved_before_the_step_failed(self):
        decision = self.decision()
        self.value(decision, "decision", "main_doc")
        down = [(("workflow", "transition"), ledger_core.LedgerError("命令失败（退出码 1）：meegle workflow transition\n502"))]
        doc = self.fake.create_doc("node_decision", "决策", "x")["data"]["document"]
        self.quiet(workitem.update, self.lg(), decision, ["main_doc=https://example.feishu.cn/wiki/" + self.fake.docs[doc["document_id"]]["node_token"]])
        text = self.fails(workitem.archive, self.lg(replies=down), decision)
        self.assertIn("archive %s --moved --apply" % decision, text)
        self.assertEqual(len(self.fake.moved), 1)
        self.quiet(workitem.archive, self.lg(), decision, moved=True)
        self.assertEqual((self.state(decision), len(self.fake.moved)), ("归档", 1))


class FieldsAndQueriesTest(LedgerCase):
    def test_blocked_is_two_fields(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))
        self.quiet(workitem.update, self.lg(), request, ["blocked_reason=等 GPU 配额"])
        self.assertRegex(self.value(request, "same_seq", "blocked_since"), r"^\d{13}$")
        self.assertIn("阻塞中（", self.quiet(ledger.next_step, self.lg("preview"), request))
        rows = self.quiet(ledger.query, self.lg("preview"), "blocked")
        self.assertEqual([str(r["work_item_id"]) for r in rows], [request])
        self.quiet(workitem.update, self.lg(), request, ["阻塞原因="])
        self.assertEqual((self.value(request, "same_seq", "blocked_reason"), self.value(request, "same_seq", "blocked_since")), ("", ""))
        self.assertEqual(self.quiet(ledger.query, self.lg("preview"), "blocked"), [])
        for ref in ("blocked_reason", "阻塞原因"):                                       # however the field is written
            self.assertIn("阻塞原因用 update 写", self.fails(workitem.confirm, self.lg(), request, "Product-p1-01", [ref + "=x"]))

    def test_named_queries(self):
        lg = self.lg("offline")
        mql = lambda name, seq=None: ledger_core.mql_text(lg, name, seq)
        self.assertIn("`work_item_status` IN ('待决定', '已决定')", mql("queue")[0])    # 已决定: see query queue's stages
        self.assertIn("NOT IN ('已验收', '归档', '已终止')", mql("open")[0])
        self.assertIn("NOT IN ('归档', '已终止')", mql("tasks")[0])
        self.assertEqual(len(mql("blocked")), 3)                                        # the three types that can be blocked
        self.assertTrue(all("IS NOT NULL" in q and "'归档', '已终止'" in q for q in mql("blocked")))
        self.assertEqual(len(mql("source:2100")), 4)
        self.assertIn("= '算法'", mql("open", "alg")[0])
        self.assertEqual(mql("open", "算法"), mql("open", "alg"))                        # the Chinese name is taken too
        with self.assertRaises(ledger_core.LedgerError) as e:
            mql("open", "算法组")
        self.assertIn("alg（算法）", str(e.exception))
        for bad in ("reviewing", "nope"):
            with self.assertRaises(ledger_core.LedgerError):
                mql(bad)
        self.assertEqual(sorted(ledger_core.QUERY_NAMES), sorted(list(ledger_core.QUERIES) + ["blocked"]))
        self.assertEqual(len(ledger_core.QUERY_NAMES), 7)

    def test_queries_see_the_items(self):
        task = self.task()
        decision = self.decision(task, decided=False)
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "tasks")], [task])
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [decision])
        self.assertEqual(workitem.open_tasks(self.lg("preview")), [(task, "复现并测量基线")])

    def test_queue_and_blocked_show_what_the_entry_lists(self):
        """The researcher comes back: the entry lists query queue, each decision with its stage, who carries it
        out and what the researcher must do by hand; query blocked shows what each stuck work item waits on."""
        self.task()
        urgent = self.draft(DECISION, "u.md", edit=lambda t: t.replace("- 紧急程度：今天\n", "- 紧急程度：插队\n"))
        decision = self.quiet(register, self.lg(), urgent)
        hand = self.quiet(register, self.lg(), self.draft(BY_HAND, "hand.md"))
        rows, text = self.out(ledger.query, self.lg("preview"), "queue")
        line = next(x for x in text.splitlines() if x.startswith("#%s " % decision))
        self.assertRegex(line, r"^#%s  待决定  决策包（Alg-p2-01）：要不要把时间切分移进评测协议  版本：第 1 版  紧急程度：插队  回复期限：2026-11-29  "
                               r"执行方：Alg-p2-01  研究员要做的事：无  主文档：https://\S+/wiki/\w+$" % decision)
        row = next(r for r in rows if str(r["work_item_id"]) == decision)
        self.assertEqual((row["stage"], row["urgency"], row["due"], row["executor"], row["researcher_action"]),
                         ("待决定", "插队", "2026-11-29", "Alg-p2-01", "无"))
        line = next(x for x in text.splitlines() if x.startswith("#%s " % hand))
        self.assertRegex(line, r"  紧急程度：今天  回复期限：无  执行方：研究员  研究员要做的事：%s  主文档：https://\S+/wiki/\w+$" % re.escape(API_KEY))
        mine = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        other = self.quiet(register, self.lg(), self.draft(CROSS, "c.md"))
        self.quiet(workitem.update, self.lg(), mine, ["blocked_reason=等研究员：决策 #%s" % hand])
        self.quiet(workitem.update, self.lg(), other, ["blocked_reason=等基础设施：GPU 节点没排上"])
        rows, text = self.out(ledger.query, self.lg("preview"), "blocked")
        lines = {x.split()[0]: x for x in text.splitlines() if x.startswith("#")}
        self.assertIn("  阻塞原因：等研究员：决策 #%s  主文档：https://" % hand, lines["#" + mine])
        self.assertIn("  阻塞原因：等基础设施：GPU 节点没排上", lines["#" + other])
        self.assertEqual(sorted(r["blocked_reason"] for r in rows), ["等基础设施：GPU 节点没排上", "等研究员：决策 #%s" % hand])
        self.assertTrue(all("stage" not in r and "executor" not in r and "researcher_action" not in r for r in rows))   # queue columns only
        self.assertNotIn("回复期限", lines["#" + mine])                                  # a request keeps no due in its receipt
        lg = self.lg("offline")
        self.assertIn("`%s`" % lg.field("decision", "urgency")["field_key"], ledger_core.mql_text(lg, "queue")[0])
        self.assertEqual(ledger_core.moql_value({"key_label_value": {"key": "opt1", "label": "插队"}}), "插队")

    def test_get_shows_fields_by_name(self):
        task = self.task()
        text = self.quiet(ledger.digest, self.lg("preview"), task)
        self.assertIn("#%s  研究任务·正式实验  确定目标  复现并测量基线" % task, text)
        self.assertIn("  序列（sequence）：产品", text)
        self.assertIn("  发出方（sender）：Product-p2-01", text)

    def test_states_that_no_longer_take_new_information(self):
        self.assertEqual(ledger_core.closed_states(self.lg("offline")), {"已验收", "归档", "已终止"})

    def test_relink_replaces_the_block_in_place(self):
        task = self.task()

        def page(line):
            return {"data": {"document": {"content": '<docx><ul><li id="b1">流程：正式实验</li><li id="b2">%s</li></ul><h2 id="h1">目标</h2></docx>' % line}}}
        lg = self.lg(replies=[(("docs", "+fetch"), page("关联工作项：待建；来源工作项 #2100")), (("docs", "+update"), {})])
        self.quiet(docs.relink, lg, task)
        argv, _, stdin = next(c for c in lg.calls if "+update" in c[0])
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--command", "--start-block-id", "--end-block-id")], ["block_replace", "b2", "b2"])
        self.assertRegex(stdin, r"^- 关联工作项：\[#%s\]\(https://project\.feishu\.cn/\S+/detail/%s\)；来源工作项 #2100\n$" % (task, task))
        lg = self.lg(replies=[(("docs", "+fetch"), page('关联工作项：<a href="x">#%s</a>' % task))])
        self.assertIn("不用改", self.out(docs.relink, lg, task)[1])
        self.assertEqual(lg.wrote("docs", "+update"), [])
        lg = self.lg(replies=[(("docs", "+fetch"), page("关联工作项：#2001"))])
        self.assertIn("这一行写“待建”", self.fails(docs.relink, lg, task))


def entry(argv):
    """The script a subcommand lives in."""
    cmd = next(a for a in argv if not a.startswith("--"))
    if cmd in ledger_core.READ_COMMANDS:
        return ledger
    return docs if cmd in ("revise", "status-block", "append", "relink", "ensure-node") else workitem


class MainTest(LedgerCase):
    def run_main(self, *argv):
        buf = io.StringIO()
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            with contextlib.redirect_stdout(buf):
                code = entry(argv).main(list(argv))
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
        self.ids(lambda ids: ids.update(schema="solo-lab/ids-v1"))
        code, text = self.run_main("get", "2101")
        self.assertEqual(code, 1)
        self.assertIn("旧版的绑定", text)
        self.ids(lambda ids: ids["meegle"].pop("bound_at"))
        code, text = self.run_main("get", "2101")
        self.assertIn("还没绑定飞书空间", text)

    def test_register_existing_points_at_revise(self):
        code, text = self.run_main("register", "x.md", "--existing", "2101", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("register --existing 停用了", text)
        self.assertIn("docs.py revise 2101", text)

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
        return self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"), name)

    def ids(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_status_block_replaces_the_range_between_the_two_headings(self):
        decision = self.decision(decided=False)
        lg = self.lg(replies=[(("+fetch", "outline"), recorded("docs-fetch-outline.json")),
                              (("+fetch", "range"), recorded("docs-fetch-range-status.json")), (("docs", "+update"), {})])
        heads = ledger_core.heading_ids(lg, LINK)
        self.assertEqual((len(heads), heads[-1]), (12, ("doxcnakjeLdey6R5sMps4TyITGg", "交接记录")))
        path = self.write("### 当前状态\n执行中。\n", "status.md")
        self.quiet(docs.status_block, lg, decision, path)
        argv, write, stdin = lg.calls[-1]
        self.assertTrue(write)
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--doc", "--start-block-id", "--end-block-id", "--content")],
                         [LINK, "doxcnnyQfTdXSmSuMyN3ajvOmgh", "doxcnm3kiNhVu6Y7yOgNWnLzddd", "-"])
        self.assertTrue(stdin.startswith("## 状态包"))

    def test_block_shapes(self):
        xml = recorded("docs-fetch-range-status.json")["data"]["document"]["content"]
        self.assertEqual(docs.last_block_before(xml, "doxcnakjeLdey6R5sMps4TyITGg"), "doxcnm3kiNhVu6Y7yOgNWnLzddd")
        nested = '<fragment><h2 id="a">状态包</h2><p id="b">x</p><table id="t"><tr><td id="c">y</td></tr></table><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(docs.last_block_before(nested, "z"), "t")
        excerpt = '<fragment><h2 id="a">状态包</h2><excerpt top-block-id="tb"><p id="inner">x</p></excerpt><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(docs.last_block_before(excerpt, "z"), "tb")
        html = '<fragment><h2 align="left" id="s1">状态包</h2><p id="p">x</p><h2 id="z">交接记录</h2></fragment>'
        lg = self.lg("preview", [(("+fetch", "outline"), {"data": {"document": {"content": html}}})])
        self.assertEqual(ledger_core.heading_ids(lg, "u")[0], ("s1", "状态包"))

    def test_a_revision_reads_the_whole_document_to_find_its_opening(self):
        """What revise replaces runs from the document's first block to the block before 状态包. Up to 1.0.7 it was
        read as a range that named only its end. lark-cli (1.0.98, a project's scribe on 2026-11-19) answers such a
        range with the end block alone, so every revise stopped at 主文档在“状态包”标题之前没有内容块: the command
        had only run against a stand-in that answered with the document up to the end block. The whole document is
        read instead; lark-cli gives its title and its blocks side by side, with nothing around them."""
        whole = ('<title id="t0">决策包</title><callout id="c1"><p id="c2">摘要</p></callout><ul><li id="l1">版本：第 1 版</li>'
                 '<li id="l2">执行方：研究员</li></ul><h2 id="s1">状态包</h2><p id="p9">x</p><h2 id="z">交接记录</h2>')
        self.assertEqual(docs.opening_blocks(docs.whole_document(whole), "s1"), ("c1", "l2"))
        listed = '<title id="t0">决策包</title><ul><li id="l1">版本：第 1 版</li></ul><p id="p1">正文</p><h2 id="s1">状态包</h2>'
        self.assertEqual(docs.opening_blocks(docs.whole_document(listed), "s1"), ("l1", "p1"))
        alone = '<fragment mode="range" requested-end="s1"><h2 id="s1">状态包</h2></fragment>'      # the range, as answered
        self.assertEqual(docs.whole_document(alone), alone)
        with self.assertRaises(ledger_core.LedgerError) as caught:
            docs.opening_blocks(alone, "s1")
        self.assertIn("主文档在“状态包”标题之前没有内容块", str(caught.exception))
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        lg = self.lg()
        self.quiet(docs.revise, lg, item, self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22")), "改日期")
        reads = [argv for argv, write, _ in lg.calls if "+fetch" in argv and not write]
        self.assertTrue(any("--scope" not in argv and "with-ids" in argv for argv in reads))    # the whole document, with ids
        for argv in reads:                                              # and no range is left to say where it starts
            self.assertTrue("range" not in argv or "--start-block-id" in argv, argv)

    def test_status_sections_come_before_the_log(self):
        body = ledger_core.with_status_sections("## 认知层\nx\n\n## 交接记录\n[Product+2] 起草。\n")
        self.assertLess(body.index("## 状态包"), body.index("## 交接记录"))
        self.assertEqual(ledger_core.with_status_sections(body), body)
        self.assertTrue(ledger_core.with_status_sections("## 认知层\nx\n").rstrip().endswith("## 交接记录"))

    def test_append_and_comment_carry_a_session_tag(self):
        decision = self.decision(decided=False)
        self.assertIn("第一行要带会话标签", self.fails(docs.append, self.lg(), decision, self.write("没有标签的记录\n", "rec.md")))
        lg = ledger_core.Ledger(self.tmp, "offline")
        self.quiet(workitem.comment, lg, "2101", "[紧急] 预算超了")
        self.assertIn("'%s [紧急] 预算超了'" % lg.tag, lg.planned[-1])
        self.quiet(workitem.comment, lg, "2101", "[Alg+1] 收到")
        self.assertIn("'[Alg+1] 收到'", lg.planned[-1])

    def test_a_create_answer_without_an_id_stops(self):
        with self.assertRaises(ledger_core.LedgerError):
            workitem.created_id({"data": {"template": {"id": 11093046}}}, "x")
        self.assertEqual(workitem.created_id({"data": {"work_item_id": 5}}, "x"), "5")
        self.assertEqual(workitem.created_id({"data": {"id": 6}}, "x"), "6")
        self.assertIn("输出里没有工作项 ID", self.fails(register, self.lg(replies=[(("workitem", "create"), {"ok": True})]), self.sheet()))

    def test_a_document_that_exists_is_not_created_twice(self):
        path = self.sheet()
        down = [(("wiki", "+node-get"), ledger_core.LedgerError("node-get down"))]
        self.fails(docs.upload, self.lg(replies=down), path)
        self.assertEqual(self.fake.items, {})
        self.assertEqual((self.uploaded(path)["status"], "docx" in self.uploaded(path)), ("created", True))
        lg = self.lg()
        url = self.quiet(docs.upload, lg, path)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs)), ([], 1))
        self.assertIn("/wiki/wik0001", url)
        item_id = self.quiet(register, self.lg(), path)
        self.assertEqual((self.value(item_id, "research_task", "main_doc"), len(self.fake.docs)), (url, 1))

    def test_an_uncertain_document_needs_doc(self):
        path = self.sheet()
        with self.assertRaises(ledger_core.WriteMaybeDone) as e, contextlib.redirect_stdout(io.StringIO()):
            docs.upload(self.lg(replies=[(("docs", "+create"), ledger_core.WriteMaybeDone("timeout"))]), path)
        self.assertIn("运行 docs.py upload packages/drafts/alg/t.md --doc <文档链接> --apply", str(e.exception))
        self.assertIn("--doc", self.fails(docs.upload, self.lg(), path))
        for bad in ("https://example.feishu.cn/wiki/", "https://example.feishu.cn/drive/folder/fldX", "docxMainDoc0001"):
            self.assertIn("--doc", self.fails(docs.upload, self.lg(), path, doc=bad))
        self.assertIn("这份草稿的文档还没上传", self.fails(workitem.register, self.lg(), path))   # nothing to register yet
        lg = self.lg()
        self.quiet(docs.upload, lg, path, doc="https://example.feishu.cn/wiki/wikFoundOnWeb01")
        self.assertEqual(lg.wrote("docs", "+create"), [])
        item_id = self.quiet(workitem.register, self.lg(), path)
        self.assertEqual(self.value(item_id, "research_task", "main_doc"), "https://example.feishu.cn/wiki/wikFoundOnWeb01")

    def test_a_receipt_whose_item_is_gone(self):
        path = self.sheet()
        os.makedirs(os.path.join(self.tmp, "packages", "sent"), exist_ok=True)
        with open(os.path.join(self.tmp, "packages", "sent", "2105.json"), "w", encoding="utf-8") as f:
            json.dump({"draft": os.path.relpath(path, self.tmp), "work_item_id": "2105", "status": "created"}, f)
        self.assertIn("删掉回执", self.fails(register, self.lg(), path, resume=True))

    def test_reply_reruns_reuse_the_document_and_skip_odd_receipts(self):
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN))
        path = self.write(example(DELIVERY).replace("- 关联工作项：#2001", "- 关联工作项：#%s" % request), "back.md")
        with open(os.path.join(self.tmp, "packages", "sent", "reply-%s-back.md.json" % request), "w", encoding="utf-8") as f:
            json.dump(["x"], f)                                                         # not a receipt: left alone
        before = len(self.fake.docs)
        self.fails(reply, self.lg(replies=[(("wiki", "+node-get"), ledger_core.LedgerError("down"))]), path)
        lg = self.lg()
        self.quiet(reply, lg, path)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs)), ([], before + 1))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "packages", "sent", "reply-%s-back.md-2.json" % request)))

    def test_the_tenant_domain_is_kept_from_the_first_document(self):
        ids = self.ids()
        ids["wiki"].pop("tenant_domain")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        self.quiet(register, self.lg(), self.sheet())
        self.assertEqual(self.ids()["wiki"].get("tenant_domain"), "example.feishu.cn")

    def test_roles_that_did_not_take_are_filled_in(self):
        path = self.sheet()
        lg = self.lg()
        real = self.fake.create

        def without_roles(type_ref, fields):
            return real(type_ref, [f for f in fields if f["field_key"] != "role_owners"])
        self.fake.create = without_roles
        _, text = self.out(register, lg, path)
        self.assertIn("已用 --role-operate 把研究员补进", text)
        item = next(iter(self.fake.items.values()))
        self.assertTrue(item["roles"] and all(m == [fake_space.ME] for m in item["roles"].values()))

    def nodes(self, *pairs):
        return {"ok": True, "data": {"nodes": [{"node_token": t, "title": title, "has_child": False} for t, title in pairs],
                                     "has_more": False}}

    def test_ensure_node_reuses_a_node_of_the_same_name(self):
        lg = self.lg(replies=[(("wiki", "+node-list"), self.nodes(("wikSame001", "反馈包"), ("wikX", "汇报文档")))])
        lg.nodes.pop("feedback")
        self.quiet(docs.ensure_node, lg, "feedback")
        self.assertEqual(lg.wrote("wiki", "+node-create"), [])
        self.assertEqual(self.ids()["wiki"]["nodes"]["feedback"]["node_token"], "wikSame001")
        for listing in (self.nodes(("wikA", "产品"), ("wikB", "产品")), {"_text": "error"}):
            lg = self.lg(replies=[(("wiki", "+node-list"), listing)])
            lg.nodes.pop("archive/product")
            self.fails(docs.ensure_node, lg, "archive/product")
        lg = self.lg(replies=[(("wiki", "+node-list"), self.nodes(("wikZ", "算法"))),
                              (("wiki", "+node-create"), {"data": {"node": {"node_token": "wikNew0001"}}})])
        lg.nodes.pop("archive/product")
        self.quiet(docs.ensure_node, lg, "archive/product")
        self.assertIn("node_archive", " ".join(lg.wrote("wiki", "+node-create")[0]))
        self.assertEqual(self.ids()["wiki"]["nodes"]["archive/product"]["node_token"], "wikNew0001")

    def test_ensure_node_all_builds_parents_first(self):
        empty = self.nodes()
        lg = self.lg("preview", [(("wiki", "+node-list"), empty)])
        lg.nodes.clear()
        _, text = self.out(docs.ensure_all_nodes, lg)
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
        self.quiet(docs.ensure_all_nodes, lg)
        keys = self.spec.node_keys()
        self.assertEqual(len(made), len(keys))
        self.assertEqual(len(keys), 21)
        self.assertFalse([c for c in made[:len(top)] if "--parent-node-token" in c])
        self.assertTrue(all("--parent-node-token" in c for c in made[len(top):]))
        self.assertEqual(sorted(self.ids()["wiki"]["nodes"]), sorted(keys))


class AttachTest(LedgerCase):
    """A link field given a local file. From 1.1.0 the session that answers for the step uploads the file under
    the item's main document (docs.py attach); the scribe's --set <字段>=@<文件> finds that document by the file
    and its content, and uploads nothing."""

    def local(self, text="# 上下文整理\n\n基线的公开数字和三篇相关工作。\n", name="context.md"):
        os.makedirs(os.path.join(self.tmp, "spec"), exist_ok=True)
        with open(os.path.join(self.tmp, "spec", name), "w", encoding="utf-8") as f:
            f.write(text)
        return "@spec/" + name

    def receipts(self, task):
        sent = os.path.join(self.tmp, "packages", "sent")
        return sorted(n for n in os.listdir(sent) if n.startswith("attach-%s-" % task))

    def test_a_step_is_confirmed_with_the_document_of_a_local_file(self):
        task = self.task()
        self.quiet(workitem.confirm, self.lg(), task, "Product-p2-01", STEPS[0][1])
        before = len(self.fake.docs)
        ref = self.local()
        text = self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", ["调研文档=" + ref])       # not uploaded: the scribe stops
        self.assertIn("spec/context.md 还没上传，或者上传以后改过：书记员不上传文档", text)
        self.assertIn("docs.py attach %s 调研文档 spec/context.md --apply" % task, text)
        self.assertEqual((len(self.fake.docs), self.state(task)), (before, "前期调研"))
        lg = self.lg()
        link, text = self.out(docs.attach, lg, task, "调研文档", ref)                              # the step's session uploads
        self.assertIn("已上传“调研文档：上下文整理”：%s\n请书记员写进工作项：--set 调研文档=@spec/context.md" % link, text)
        self.assertEqual((len(self.fake.docs), lg.wrote("meegle")), (before + 1, []))
        argv, _, stdin = next(c for c in lg.calls if c[1] and "+create" in c[0])
        self.assertIn("调研文档：上下文整理", argv)
        self.assertEqual(stdin, "基线的公开数字和三篇相关工作。\n")                # the heading became the title
        self.assertRegex(link, r"^https://[^/]+/wiki/\w+")
        lg = self.lg()
        _, text = self.out(workitem.confirm, lg, task, "Alg-p2-01", ["调研文档=" + ref])
        self.assertIn("spec/context.md 用的是 研究员 上传的文档：%s" % link, text)
        self.assertIn("“前期调研”完成", text)
        self.assertEqual(self.value(task, "research_task", "调研文档"), link)
        writes = [a[1:3] for a, w, _ in lg.calls if w]
        self.assertEqual(writes, [["workitem", "update"], ["workflow", "transition"]])       # the scribe writes the work item only
        self.assertEqual(len(self.receipts(task)), 1)

    def test_nothing_is_uploaded_before_the_checks_pass(self):
        task = self.task()
        self.walk(task, 4)
        before = len(self.fake.docs)
        text = self.fails(workitem.confirm, self.lg(), task, "Alg-p2-01", ["规格=" + self.local(name="spec.md")])
        self.assertIn("假设签字", text)                                          # the step lacks other things
        self.assertNotIn("规格、", text.replace("规格版本", ""))                    # the file counts as given
        self.assertEqual((len(self.fake.docs), self.receipts(task)), (before, []))
        self.assertIn("将执行：lark-cli docs +create", self.out(docs.attach, self.lg("preview"), task, "规格", "spec/spec.md")[1])
        self.assertEqual((len(self.fake.docs), self.receipts(task)), (before, []))  # a preview writes nothing either

    def test_the_same_content_is_uploaded_once_and_a_changed_file_again(self):
        task = self.task()
        ref = self.local(name="spec.md")
        first = self.quiet(docs.attach, self.lg(), task, "规格", ref)
        self.quiet(workitem.update, self.lg(), task, ["规格=" + ref])
        self.assertEqual(self.value(task, "research_task", "规格"), first)
        lg = self.lg()
        again, text = self.out(docs.attach, lg, task, "规格", ref)
        self.assertIn("spec/spec.md 没有改过，沿用上次上传的文档", text)
        self.assertEqual((lg.wrote("docs", "+create"), again), ([], first))
        self.local("# 规格 v2\n\n阈值改成 0.3。\n", "spec.md")
        self.assertIn("还没上传，或者上传以后改过", self.fails(workitem.update, self.lg(), task, ["规格=" + ref]))   # the changed file is no document yet
        self.assertEqual(self.value(task, "research_task", "规格"), first)
        lg = self.lg()
        second = self.quiet(docs.attach, lg, task, "规格", ref)
        self.assertEqual(len(lg.wrote("docs", "+create")), 1)                    # a new document: the link changes with the content
        self.quiet(workitem.update, self.lg(), task, ["规格=" + ref])
        self.assertEqual(self.value(task, "research_task", "规格"), second)
        self.assertNotEqual(second, first)
        self.assertEqual(len(self.receipts(task)), 2)
        self.quiet(workitem.update, self.lg(), task, ["规格=" + first])           # a link is taken as it is
        self.assertEqual(self.value(task, "research_task", "规格"), first)

    def test_a_document_that_exists_is_not_made_twice_after_a_failure(self):
        task = self.task()
        ref = self.local(name="spec.md")
        before = len(self.fake.docs)
        self.fails(docs.attach, self.lg(replies=[(("wiki", "+node-get"), ledger_core.LedgerError("down"))]), task, "规格", ref)
        self.assertEqual(len(self.fake.docs), before + 1)                        # created, its link not known yet
        lg = self.lg()
        link = self.quiet(docs.attach, lg, task, "规格", ref)
        self.assertEqual((lg.wrote("docs", "+create"), len(self.fake.docs)), ([], before + 1))
        self.fails(workitem.update, self.lg(replies=[(("workitem", "update"), ledger_core.LedgerError("down"))]), task, ["规格=" + ref])
        self.quiet(workitem.update, self.lg(), task, ["规格=" + ref])             # the scribe's write again: the same document
        self.assertEqual((self.value(task, "research_task", "规格"), len(self.fake.docs)), (link, before + 1))
        text = self.fails(docs.attach, self.lg(replies=[(("docs", "+create"), ledger_core.WriteMaybeDone("timeout"))]), task,
                          "需求文档", self.local(name="prd.md"))
        self.assertIn("没有拿到确定的结果", text)
        self.assertIn("--set 需求文档=<文档链接>", self.fails(docs.attach, self.lg(), task, "需求文档", "@spec/prd.md"))

    def test_what_is_refused(self):
        task = self.task()
        self.assertIn("找不到文件 spec/none.md", self.fails(workitem.update, self.lg(), task, ["规格=@spec/none.md"]))
        self.assertIn("找不到文件 spec/none.md", self.fails(docs.attach, self.lg(), task, "规格", "spec/none.md"))
        self.local("", "empty.md")
        self.assertIn("是空的", self.fails(workitem.update, self.lg(), task, ["规格=@spec/empty.md"]))
        self.assertIn("是空的", self.fails(docs.attach, self.lg(), task, "规格", "spec/empty.md"))
        self.assertIn("“成功标准”不是链接字段，不用上传", self.fails(docs.attach, self.lg(), task, "成功标准", self.local("十个节点\n", "goal.txt")))
        self.quiet(workitem.update, self.lg(), task, ["成功标准=" + self.local("十个节点都确认到归档\n", "goal.txt")])
        self.assertEqual(self.value(task, "research_task", "成功标准"), "十个节点都确认到归档")   # a text field still takes the text
        path = self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"), "second.md")
        self.assertIn("登记时不上传产出文档", self.fails(register, self.lg(), path, pairs=["规格=" + self.local(name="spec.md")]))


class ValuesAndReadsTest(LedgerCase):
    def test_set_values_from_files(self):
        lg = ledger_core.Ledger(self.tmp, "offline")
        with open(os.path.join(self.tmp, "usable.md"), "w", encoding="utf-8") as f:
            f.write("结论原文\n")
        self.assertEqual(workitem.set_value(lg, "@usable.md"), "结论原文")
        self.assertEqual(workitem.set_value(lg, "@研究员 还没批预算"), "@研究员 还没批预算")
        self.assertEqual(workitem.set_value(lg, "@@usable.md"), "@usable.md")
        for missing in ("@usabel.md", "@runs/alg/x", "@可使用的结论原文", "@<可使用的结论原文文件>", "@runs/alg/重评 结果.md", "@usable"):
            with self.assertRaises(ledger_core.LedgerError, msg=missing):
                workitem.set_value(lg, missing)
        cwd = os.getcwd()
        try:
            os.chdir(os.path.join(self.tmp, "packages"))               # falls back to the project directory
            self.assertEqual(workitem.set_value(lg, "@usable.md"), "结论原文")
        finally:
            os.chdir(cwd)
        gbk = os.path.join(self.tmp, "gbk.txt")
        with open(gbk, "wb") as f:
            f.write("结论原文".encode("gbk"))
        with self.assertRaises(ledger_core.LedgerError):
            workitem.set_value(lg, "@" + gbk)

    def test_source_line_with_a_full_width_colon(self):
        path = self.draft(EXECUTE)
        self.assertEqual(ledger_core.Draft(self.spec, path).source, "2100")

    def test_query_shapes(self):
        rows = ledger_core.query_rows(recorded("workitem-query-queue.json"))
        self.assertEqual([(r["work_item_id"], r["work_item_status"]) for r in rows], [(7152108550, "待决定")])
        self.assertEqual(ledger_core.query_rows(recorded("workitem-query-empty.json")), [])
        for bad in ({"_text": "Error: token expired"}, {"code": 10001, "data": None}, {"data": {"1": "x"}},
                    {"data": {"1": [{"moql_field_list": "x"}]}}, {"data": {"1": ["abc"]}}, {"data": ["unexpected"]}):
            with self.assertRaises(ledger_core.LedgerError):
                ledger_core.query_rows(bad)
        for cell in (["待决定"], "待决定", [None]):
            odd = {"data": {"1": [{"moql_field_list": [{"key": "work_item_status", "value": {"key_label_value_list": cell}}]}]}}
            with self.assertRaises(ledger_core.LedgerError):
                ledger_core.query_rows(odd)

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
        text, printed = self.out(ledger.digest, self.lg("preview", [(("workitem", "query"), ledger_core.LedgerError("down"))]), decision)
        self.assertIn("人员字段读不出来", printed)
        self.assertIn("决定内容：同意", text)

    def test_write_log_ids_have_no_trailing_newline(self):
        lg = ledger_core.Ledger(self.tmp, "apply")
        lg.log(["meegle", "workitem", "create"], 0, '{\n  "work_item_id": 7152343534\n}\n')
        with open(os.path.join(self.tmp, "feishu", "write-log.jsonl"), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read().splitlines()[-1])["ids"], ["7152343534"])

    def test_trace_follows_the_source(self):
        self.task()
        source = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md", edit=lambda t: t.replace("产品 +2（Product-p2-01）", "质量 +2（Quality-p2-01）")))
        child = self.quiet(register, self.lg(), self.draft(EXECUTE, "e.md", edit=lambda t: t.replace("- 来源工作项：#2100\n", "")), source=source)
        _, text = self.out(ledger.trace, self.lg("preview"), child)
        self.assertEqual([line.split()[0] for line in text.splitlines()], ["#" + child, "#" + source])


# ------------------------------------------------------------------ 1.0.4
SANDBOX = "| 同账号沙箱 | 用 sandbox-exec 限定候选代码能读写的路径 | 沙箱规则要逐条调 | 规则漏一条，候选代码就能读到评测集 | 无 |\n"


def version(n, date, edit=None):
    """An edit of PERMISSION: its n-th version, dated, and changed by edit."""
    def change(text):
        text = text.replace("第 1 版（2026-11-21）", "第 %d 版（%s）" % (n, date))
        return edit(text) if edit else text
    return change


def without_sandbox(text):
    """PERMISSION without its 同账号沙箱 option, the cognitive layer included."""
    return text.replace(SANDBOX, "").replace("本机 Docker 容器、同账号沙箱，还是送到", "本机 Docker 容器，还是送到")


class DecisionStateTest(LedgerCase):
    """The local decision state .claude/state/gates.json (1.0.4): the decisions from the queue that still wait,
    with what each freezes and how it may end. Only ledger.py --apply writes it; the inbox hook reads it. From
    1.0.6 nothing is released to a hook: the state keeps no releases, and no expiry time either (it is worked
    out from org/decisions.md whenever it is needed)."""

    def gates(self):
        path = ledger_core.gates_path(self.tmp)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def age(self, item_id, hours):
        """The pending decision as if it had been registered that many hours ago: in the local state and in its receipt."""
        since = (ledger_core.now_at() - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
        data = self.gates()
        data["pending"][str(item_id)]["registered_at"] = since
        self.put_gates(data)
        self.edit_receipt(item_id, registered_at=since)

    def put_gates(self, data):
        with open(ledger_core.gates_path(self.tmp), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def stages(self):
        """What query queue lists: [(ID, stage)]."""
        return [(str(r["work_item_id"]), r["stage"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")]

    def doc(self, item_id, wid="decision"):
        return self.fake.doc_by(self.value(item_id, wid, "main_doc").rsplit("/", 1)[1])

    def test_a_queued_decision_is_held_with_what_it_freezes(self):
        self.task()
        self.quiet(register, self.lg("preview"), self.draft(PERMISSION, "pre.md"))
        self.assertEqual(self.gates(), {})                                     # a preview writes nothing
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        self.assertEqual(ledger_core.gates_path(self.tmp), os.path.join(self.tmp, ".claude", "state", "gates.json"))
        g = self.gates()
        entry = g["pending"][item]
        self.assertEqual((entry["id"], entry["title"], entry["urgency"], entry["version"], entry["reversible"], entry["criteria"]),
                         (item, "实验平台用什么隔离候选代码", "今天", 1, False, [2, 3]))
        self.assertEqual((entry["default"], entry["default_option"], entry["default_applies"], entry["executor"],
                          entry["researcher_action"], entry["due"], entry["sender"]),
                         ("等研究员", None, False, "Infra-p2-01", None, "2026-11-22", "Infra-p2-01"))
        self.assertEqual(entry["frozen"], [                                   # a category of the list, or a sentence
            {"kind": "category", "category": "privileged_app", "name": "特权应用", "qualifier": "Docker", "text": "特权应用：Docker"},
            {"kind": "text", "text": "`docker build`"},
            {"kind": "text", "text": "照某个选项改实验平台的计量和内存闸"}])
        self.assertEqual([(o["name"], [r["text"] for r in o["irreversible"]]) for o in entry["options"]],
                         [("本机 Docker", ["特权应用：Docker"]), ("同账号沙箱", []), ("CAMPUS-HPC", ["对外发送：scp、ssh 到 CAMPUS-HPC"])])
        self.assertEqual(entry["options"][0]["irreversible"][0],
                         {"category": "privileged_app", "name": "特权应用", "qualifier": "Docker", "text": "特权应用：Docker"})
        self.assertEqual(entry["options"][2]["irreversible"][0]["qualifier"], "scp、ssh 到 CAMPUS-HPC")     # the note, whole
        since = ledger_core.parse_stamp(entry["registered_at"])
        self.assertLess(abs((ledger_core.now_at() - since).total_seconds()), 60)
        # 1.0.6: the waiting decisions and nothing else; no releases, and no expiry time kept beside org/decisions.md
        self.assertEqual((sorted(g), g["version"]), (["pending", "updated_at", "version"], 2))
        for gone in ("expires_at", "rolled_back_at", "reopened_at"):
            self.assertNotIn(gone, entry)
        rec = self.receipt(item)
        self.assertEqual((rec["version"], rec["reversible"], rec["default"], rec["options"], rec["frozen"]),
                         (1, False, "等研究员", entry["options"], entry["frozen"]))
        self.assertEqual(rec["registered_at"], entry["registered_at"])
        self.quiet(register, self.lg(), self.draft(DECIDED, "talk.md"))     # decided in a talk: it never waits
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        self.assertEqual(sorted(self.gates()["pending"]), [item])
        self.assertEqual(self.receipt(request)["version"], 1)
        self.assertNotIn("reversible", self.receipt(request))

    def test_the_option_picked_is_a_record_and_releases_nothing(self):
        """1.0.6: while the decision waits its frozen actions are in the local state. Once the researcher decides
        the scribe confirms 决定; --option is optional and only records which option was picked, in the receipt
        and a comment, with the irreversible actions that option lists. Nothing is released and nothing is taken
        back: the local state keeps the waiting decisions only."""
        self.task()
        decided = ["决定内容=用本机 Docker", "决定人=研究员"]
        plain = self.quiet(register, self.lg(), self.draft(PERMISSION, "plain.md"))
        _, out = self.out(workitem.confirm, self.lg(), plain, "研究员", decided)       # without --option, though options list actions
        self.assertEqual((self.state(plain), out.splitlines()[-1]), ("已决定", "执行方：Infra-p2-01。"))
        self.assertNotIn("研究员选的选项", out)
        self.assertEqual((self.receipt(plain)["option"], self.receipt(plain)["decided_by"]), (None, "研究员"))
        self.assertFalse([c for c in self.fake.comments if c[0] == plain and "选项" in c[1]])
        first = self.quiet(register, self.lg(), self.draft(PERMISSION))
        text = self.fails(workitem.confirm, self.lg(), first, "研究员", decided, option="Podman")
        self.assertIn("没有“Podman”，有：本机 Docker、同账号沙箱、CAMPUS-HPC；研究员没有选表里的任何一个，写 --option 无，或者不加 --option", text)
        note = "研究员选的选项：本机 Docker（不可逆的动作：特权应用：Docker）"
        _, out = self.out(workitem.confirm, self.lg("preview"), first, "研究员", decided, option="本机 Docker")
        self.assertIn("[PMO+0] " + note, out)                                         # the comment a preview would add
        self.assertEqual((self.state(first), sorted(self.gates()["pending"])), ("待决定", [first]))
        _, out = self.out(workitem.confirm, self.lg(), first, "研究员", decided, option="本机 Docker")
        self.assertIn(note + "。已记进回执和评论。", out)
        self.assertEqual(out.splitlines()[-1], "执行方：Infra-p2-01。")
        self.assertIn((first, "[PMO+0] " + note), self.fake.comments)
        for word in ("放行", "收回"):
            self.assertNotIn(word, out)
        g = self.gates()
        self.assertEqual((g["pending"], "releases" in g), ({}, False))
        rec = self.receipt(first)
        self.assertEqual((rec["option"], rec["decided_by"]), ("本机 Docker", "研究员"))
        self.assertTrue(ledger_core.parse_stamp(rec["decided_at"]))
        # the step is confirmed already (here by the scribe, or by the researcher on the web page): the option is a comment
        text = self.fails(workitem.confirm, self.lg(), first, "研究员", decided, option="本机 Docker")
        self.assertIn("--option 只用在确认决策的“决定”这一步：#%s 是决策，现在是“已决定”；这一步已经确认过（比如研究员在网页上定的），"
                      "选的选项用 workitem.py comment 记一条评论" % first, text)
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        text = self.fails(workitem.confirm, self.lg(), request, "Product-p1-03", option="本机 Docker")
        self.assertTrue(text.endswith("--option 只用在确认决策的“决定”这一步：#%s 是同序列需求，现在是“待开工”" % request), text)
        # another decision names the same category and the researcher picks an option without it: nothing is taken back
        later = self.quiet(register, self.lg(), self.draft(PERMISSION, "later.md", edit=lambda t: t.replace(
            "# 决策包：实验平台用什么隔离候选代码", "# 决策包：内存不够，要不要改到 CAMPUS-HPC")))
        self.assertEqual(self.gates()["pending"][later]["frozen"][0]["text"], "特权应用：Docker")
        _, out = self.out(workitem.confirm, self.lg(), later, "研究员", ["决定内容=改到 CAMPUS-HPC", "决定人=研究员"], option="CAMPUS-HPC")
        self.assertIn("研究员选的选项：CAMPUS-HPC（不可逆的动作：对外发送：scp、ssh 到 CAMPUS-HPC）。已记进回执和评论。", out)
        self.assertNotIn("收回", out)
        sandbox = self.quiet(register, self.lg(), self.draft(PERMISSION, "sandbox.md"))
        _, out = self.out(workitem.confirm, self.lg(), sandbox, "研究员", ["决定内容=同账号沙箱", "决定人=研究员"], option="同账号沙箱")
        self.assertIn("研究员选的选项：同账号沙箱。已记进回执和评论。", out)          # an option that lists none
        third = self.quiet(register, self.lg(), self.draft(PERMISSION, "third.md"))
        _, out = self.out(workitem.confirm, self.lg(), third, "研究员", ["决定内容=先都不做", "决定人=研究员"], option="无")
        self.assertIn("研究员没有选选项表里的选项。已记进回执和评论。", out)
        self.assertEqual((self.receipt(third)["option"], self.gates()["pending"]), ("无", {}))
        # a comment that fails leaves the step confirmed and says how to add it
        fourth = self.quiet(register, self.lg(), self.draft(PERMISSION, "fourth.md"))
        _, out = self.out(workitem.confirm, self.lg(replies=[(("comment", "add"), ledger_core.LedgerError("502"))]), fourth, "研究员", decided,
                          option="本机 Docker")
        self.assertIn("已记进回执；评论没写上（502），用 workitem.py comment %s --text '%s' 补。" % (fourth, note), out)
        self.assertEqual((self.state(fourth), self.receipt(fourth)["option"], self.gates()["pending"]), ("已决定", "本机 Docker", {}))

    def test_a_state_written_before_1_0_6_loses_its_releases(self):
        """A local decision state as 1.0.4 or 1.0.5 left it: releases, an expiry time, options that say releases, a
        frozen command prefix, a decision rolled back. It is read as far as it still applies, the releases are
        ignored, and the next write drops what is no longer kept."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(REVERSIBLE))
        new = self.gates()["pending"][item]
        old = dict(new, expires_at="2026-11-27T10:00:00+08:00", reopened_at="2026-11-26T12:00:00+08:00", default_applies=True,
                   options=[{"name": o["name"], "approach": o["approach"], "releases": o["irreversible"]} for o in new["options"]],
                   frozen=[{"kind": "command", "prefix": "python3 tools/relayout_runs.py", "words": ["python3", "tools/relayout_runs.py"],
                            "text": "`python3 tools/relayout_runs.py`"}])
        release = {"category": "admin", "name": "管理员权限", "qualifier": "", "text": "管理员权限", "decision": "2401", "option": "A",
                   "at": "2026-11-20T10:00:00+08:00"}
        self.put_gates({"version": 1, "updated_at": "2026-11-26T12:00:00+08:00", "expiry_hours": 24, "expiry_from": "默认",
                        "pending": {item: old, "junk": "not a decision"}, "releases": [release]})
        g = ledger_core.load_gates(self.tmp)
        self.assertEqual((sorted(g), g["version"], sorted(g["pending"])), (["pending", "updated_at", "version"], 2, [item]))
        entry = g["pending"][item]
        self.assertEqual((entry["rolled_back_at"], entry["default_applies"]), ("2026-11-26T12:00:00+08:00", False))
        self.assertEqual([sorted(o) for o in entry["options"]], [["approach", "irreversible", "name"]] * 2)
        self.assertEqual(entry["frozen"][0]["text"], "`python3 tools/relayout_runs.py`")       # still shown each turn
        self.assertIn("releases", self.gates())                                               # reading writes nothing
        other = self.quiet(register, self.lg(), self.draft(PERMISSION, "p.md"))        # the next write
        g = self.gates()
        self.assertEqual((sorted(g), sorted(g["pending"])), (["pending", "updated_at", "version"], sorted([item, other])))
        for gone in ("expires_at", "reopened_at"):
            self.assertNotIn(gone, g["pending"][item])
        self.assertEqual(g["pending"][item]["rolled_back_at"], "2026-11-26T12:00:00+08:00")

    def test_a_decision_rolled_back_waits_for_the_researcher(self):
        """1.0.6 (the researcher, 2026-11-16): a decision that was decided once and rolled back to 待决定 waits for
        the researcher, however long: 到期默认 is refused with the reason, the local state and the receipt say when
        it was rolled back, and query queue lists it first once it is past its expiry, which still counts from its
        registration. Nothing is taken back, there being nothing released."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(REVERSIBLE))
        self.assertIs(self.gates()["pending"][item]["default_applies"], True)
        self.age(item, 30)
        registered = self.gates()["pending"][item]["registered_at"]
        self.quiet(workitem.confirm, self.lg(), item, "研究员", ["决定内容=按变体分目录", "决定人=研究员"], option="按变体分目录")
        self.assertEqual(self.receipt(item)["option"], "按变体分目录")
        _, out = self.out(workitem.rollback, self.lg(), item, "决定", "研究员想再看看", by="研究员")
        self.assertIn("本地决策状态：#%s 回到待决定，冻结的动作重新生效；它定过一次，不再走到期默认，一直等研究员。" % item, out)
        for word in ("放行", "收回", "重新算"):
            self.assertNotIn(word, out)
        entry = self.gates()["pending"][item]
        back = ledger_core.parse_stamp(entry["rolled_back_at"])
        self.assertLess(abs((ledger_core.now_at() - back).total_seconds()), 60)
        self.assertEqual((entry["default_applies"], entry["default_option"], entry["registered_at"]), (False, "按轮次分目录", registered))
        self.assertNotIn("reopened_at", entry)
        self.assertEqual([f["text"] for f in entry["frozen"]], ["`python3 tools/relayout_runs.py`", "把已有的运行记录挪到新的目录结构下"])
        rec = self.receipt(item)
        self.assertEqual((rec["rolled_back_at"], rec["option"], rec["decided_by"], rec["decided_at"]), (entry["rolled_back_at"], None, None, None))
        text = self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT)
        self.assertIn("#%s 定过一次，又回滚到了待决定（%s）：不再走到期默认，一直等研究员，他来谈时排在清单最前" % (
            item, back.strftime("%Y-%m-%d %H:%M")), text)
        self.assertEqual((self.state(item), self.stages()), ("待决定", [(item, "待决定（已到期，继续等研究员）")]))
        self.age(item, 2000)                                                       # however long it waits
        self.assertIn("不再走到期默认", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        self.assertFalse(any("到期默认" in text for _, text in self.fake.comments))
        # a revision keeps that it was rolled back
        self.quiet(docs.revise, self.lg(), item, self.draft(REVERSIBLE, "v2.md", edit=lambda t: t.replace(
            "第 1 版（2026-11-26）", "第 2 版（2026-11-27）")), "补一句背景")
        entry = self.gates()["pending"][item]
        self.assertEqual((entry["version"], entry["rolled_back_at"], entry["default_applies"]), (2, rec["rolled_back_at"], False))
        self.assertIn("不再走到期默认", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        # one rolled back before its expiry is listed like any other until the expiry time has passed
        fresh = self.quiet(register, self.lg(), self.draft(REVERSIBLE, "fresh.md"))
        self.quiet(workitem.confirm, self.lg(), fresh, "研究员", ["决定内容=按变体分目录", "决定人=研究员"])
        self.quiet(workitem.rollback, self.lg(), fresh, "决定", "再想想", by="研究员")
        self.assertEqual(self.stages(), [(item, "待决定（已到期，继续等研究员）"), (fresh, "待决定")])
        self.age(fresh, 25)
        self.assertEqual(dict(self.stages())[fresh], "待决定（已到期，继续等研究员）")
        # one that took its default and is rolled back takes no default again either
        late = self.quiet(register, self.lg(), self.draft(REVERSIBLE, "late.md"))
        self.age(late, 25)
        self.quiet(workitem.confirm, self.lg(), late, workitem.BY_DEFAULT)
        self.assertEqual(self.receipt(late)["decided_by"], "到期默认")
        self.quiet(workitem.rollback, self.lg(), late, "决定", "研究员不同意默认的选项", by="研究员")
        self.assertIn("不再走到期默认", self.fails(workitem.confirm, self.lg(), late, workitem.BY_DEFAULT))
        # an irreversible one goes back with what it froze
        perm = self.quiet(register, self.lg(), self.draft(PERMISSION, "perm.md"))
        self.quiet(workitem.confirm, self.lg(), perm, "研究员", ["决定内容=用本机 Docker", "决定人=研究员"], option="本机 Docker")
        self.assertNotIn(perm, self.gates()["pending"])
        self.quiet(workitem.rollback, self.lg(), perm, "决定", "研究员改主意了", by="研究员")
        entry = self.gates()["pending"][perm]
        self.assertEqual([f["text"] for f in entry["frozen"]][:2], ["特权应用：Docker", "`docker build`"])
        self.assertEqual(entry["registered_at"], self.receipt(perm)["registered_at"])

    def test_a_revision_whose_urgency_the_space_has_no_option_for(self):
        """revise reads the values it asks the scribe for before it writes anything: an urgency the space has no
        option for is skipped with a reminder, as register does, and the document, the receipt and the local state
        all take the new version once."""
        self.task()
        odd = lambda t: t.replace("- 紧急程度：今天", "- 紧急程度：今天（下班前）")
        item, out = self.out(register, self.lg(), self.draft(PERMISSION, "p.md", edit=odd), pairs=["紧急程度=今天"])
        self.assertIn("提醒：跳过字段 urgency", out)
        v2 = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22", lambda t: odd(t).replace("- `docker build`\n", "")))
        _, out = self.out(docs.revise, self.lg(), item, v2, "不再冻结 docker build")
        self.assertIn("提醒：跳过字段 urgency（之后可以请书记员用 update 补）", out)
        self.assertIn("修订：#%s 的主文档开头换成第 2 版" % item, out)
        self.assertNotIn("workitem.py update", out)                                    # nothing left to ask for
        entry = self.gates()["pending"][item]
        self.assertEqual((self.receipt(item)["version"], entry["version"]), (2, 2))
        self.assertNotIn("`docker build`", [f["text"] for f in entry["frozen"]])
        log = self.doc(item)["body"].split("## 交接记录", 1)[1]
        self.assertEqual(len(re.findall(r"第 2 版（2026-11-22）", log)), 1)

    def test_a_terminated_decision_leaves_the_local_state(self):
        self.task()
        gone, other = (self.quiet(register, self.lg(), self.draft(REVERSIBLE, name)) for name in ("g.md", "o.md"))
        self.fake.item(gone)["terminated"] = True                                # the researcher terminated it on the web page
        _, out = self.out(workitem.archive, self.lg(), gone)
        self.assertIn("#%s 是“已终止”，不用归档。本地决策状态里移除了它" % gone, out)
        self.assertEqual(sorted(self.gates()["pending"]), [other])
        self.fake.item(other)["terminated"] = True                               # nobody told the scribe this time
        self.age(other, 1)
        newer, out = self.out(register, self.lg(), self.draft(PERMISSION, "p.md"))
        self.assertIn("本地决策状态：#%s 在空间里已经不在待决定" % other, out)
        self.assertEqual(sorted(self.gates()["pending"]), [newer])

    def test_a_decision_revised_twice(self):
        """B4: two revisions of a decision. The main document opens with the third version and its number, its
        交接记录 has two revision lines, the dropped option is gone from the opening, the link is the same.
        The receipt and the local decision state follow, and the work item's 紧急程度 once the scribe has written
        what the revision asked for; the queue shows the version."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        link, count = self.value(item, "decision", "main_doc"), len(self.fake.docs)
        v2 = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22", without_sandbox))
        _, out = self.out(docs.revise, self.lg(), item, v2, "去掉同账号沙箱：沙箱规则调不完")
        self.assertIn("修订：#%s 的主文档开头换成第 2 版（文档链接不变）" % item, out)
        with open(v2, encoding="utf-8") as f:
            self.assertIn("- 关联工作项：#%s\n" % item, f.read())                  # the revision names its item now
        self.assertIn("用 docs.py revise", self.fails(register, self.lg(), v2))      # and it is not registered as new
        v3 = self.draft(PERMISSION, "v3.md", edit=version(3, "2026-11-23", lambda t: without_sandbox(t).replace(
            "- 紧急程度：今天", "- 紧急程度：插队").replace("- `docker build`\n", "")))
        lg = self.lg()
        _, out = self.out(docs.revise, lg, item, v3, "改成插队；不再冻结 docker build")
        self.assertIn("workitem.py update %s --set 紧急程度=插队 --apply" % item, out)
        self.assertEqual(self.scribe_writes(lg, item), [("紧急程度", "插队")])
        body = self.doc(item)["body"]
        self.assertTrue(body.startswith("- 包类型：决策包\n- 版本：第 3 版（2026-11-23）\n"), body[:80])
        opening, rest = body.split("## 状态包", 1)
        self.assertIn("| CAMPUS-HPC |", opening)
        for gone in ("同账号沙箱", "`docker build`", "紧急程度：今天"):
            self.assertNotIn(gone, opening)
        self.assertIn("- 关联工作项：[#%s](https://project.feishu.cn/" % item, opening)
        self.assertIn("### 简介", rest)                                          # 状态包 and 交接记录 are kept
        log = rest.split("## 交接记录", 1)[1]
        self.assertEqual(re.findall(r"^\[PMO\+0\] 第 (\d) 版（([\d-]+)）：(.+)$", log, re.M),
                         [("2", "2026-11-22", "去掉同账号沙箱：沙箱规则调不完"), ("3", "2026-11-23", "改成插队；不再冻结 docker build")])
        self.assertEqual((self.value(item, "decision", "main_doc"), len(self.fake.docs)), (link, count))   # same document, same link
        urgency = self.lg().field("decision", "urgency")["field_key"]
        self.assertEqual(self.fake.shown(self.fake.item(item)["type_key"], urgency, self.value(item, "decision", "urgency"))["label"], "插队")
        entry = self.gates()["pending"][item]
        self.assertEqual((entry["version"], entry["urgency"], [f["text"] for f in entry["frozen"]], [o["name"] for o in entry["options"]]),
                         (3, "插队", ["特权应用：Docker", "照某个选项改实验平台的计量和内存闸"], ["本机 Docker", "CAMPUS-HPC"]))
        rec = self.receipt(item)
        self.assertEqual((rec["version"], [r["version"] for r in rec["revisions"]], rec["urgent"], rec["queue"]), (3, [2, 3], True, True))
        self.assertEqual(entry["registered_at"], rec["registered_at"])                # expiry still counts from registration
        _, out = self.out(ledger.query, self.lg("preview"), "queue")
        self.assertIn("#%s  待决定  决策包（Infra-p2-01）：实验平台用什么隔离候选代码  版本：第 3 版  紧急程度：插队" % item, out)
        # what revise refuses
        again = self.draft(PERMISSION, "again.md", edit=version(3, "2026-11-24"))
        self.assertIn("修订稿是第 3 版，#%s 现在是第 3 版：修订稿的“版本”写第 4 版" % item, self.fails(docs.revise, self.lg(), item, again, "再改"))
        self.assertIn("要写改了什么", self.fails(docs.revise, self.lg(), item, again, " "))
        other = self.draft(PERMISSION, "other.md", edit=version(4, "2026-11-24", lambda t: t.replace("- 关联工作项：待建", "- 关联工作项：#2999")))
        self.assertIn("修订稿的“关联工作项”写的是“#2999”，要修订的是 #%s" % item, self.fails(docs.revise, self.lg(), item, other, "x"))
        self.assertIn("草稿没通过检查", self.fails(docs.revise, self.lg(), item, self.draft(
            PERMISSION, "bad.md", edit=version(4, "2026-11-24", lambda t: t.replace("## 约束的出处", "## 限制"))), "x"))
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        self.assertIn("包类型不变才能修订", self.fails(docs.revise, self.lg(), request, self.draft(PERMISSION, "v4.md", edit=version(2, "2026-11-24")), "x"))
        self.assertIn("修订稿是研究任务单", self.fails(docs.revise, self.lg(), request, self.write(SHEET % ("正式实验", "产品 +2（Product-p2-01）"), "s.md"), "x"))
        self.assertIn("交付包不是主文档", self.fails(docs.revise, self.lg(), request, self.draft(DELIVERY, "dl.md", edit=lambda t: t.replace(
            "第 1 版（2026-11-02）", "第 2 版（2026-11-03）")), "x"))
        self.quiet(workitem.confirm, self.lg(), item, "研究员", ["决定内容=本机 Docker", "决定人=研究员"], option="本机 Docker")
        self.assertIn("现在是“已决定”：决策只在“待决定”时能修订", self.fails(docs.revise, self.lg(), item, self.draft(
            PERMISSION, "v4b.md", edit=version(4, "2026-11-24")), "x"))
        # a request revises while its work item is open: its 截止时间 follows a dated 回复期限
        a2 = self.draft(ASSIGN, "a2.md", edit=lambda t: t.replace("第 1 版（2026-10-19）", "第 2 版（2026-10-20）").replace(
            "- 回复期限：两周内交付", "- 回复期限：2026-11-30"))
        lg = self.lg()
        _, out = self.out(docs.revise, lg, request, a2, "回复期限写成日期")
        self.assertIn("workitem.py update %s --set 截止时间=2026-11-30 --apply" % request, out)
        self.scribe_writes(lg, request)
        self.assertEqual(self.value(request, "same_seq", "due"), ledger_core.utc_noon_ms("2026-11-30"))
        self.assertTrue(self.doc(request, "same_seq")["body"].startswith("- 包类型：需求包\n- 版本：第 2 版（2026-10-20）\n"))
        self.assertEqual(self.receipt(request)["version"], 2)
        self.assertNotIn(request, self.gates()["pending"])
        _, out = self.out(docs.revise, self.lg(), request, self.draft(ASSIGN, "a3.md", edit=lambda t: t.replace(
            "第 1 版（2026-10-19）", "第 3 版（2026-10-21）").replace("- 回复期限：两周内交付", "- 回复期限：2026-11-30")), "正文补一句")
        self.assertNotIn("workitem.py update", out)                                    # the item holds the date already: nothing to ask for

    def test_a_revision_copied_from_the_main_document_keeps_its_title(self):
        """The template has a revision start from the main document's opening, which has no title line. The item
        keeps the title it has, in the receipt and in the line every pool session is shown; it is never the name
        of the draft file. A revision that does carry a title line changes it."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        opening = self.doc(item)["body"].split("## 状态包", 1)[0]
        self.assertFalse(ledger_core.title_of(opening))
        v2 = self.write(opening.replace("第 1 版（2026-11-21）", "第 2 版（2026-11-22）"), "v2.md")
        self.assertEqual(self.quiet(docs.revise, self.lg(), item, v2, "只改日期"), 2)
        self.assertEqual((self.receipt(item)["title"], self.gates()["pending"][item]["title"]), ("实验平台用什么隔离候选代码",) * 2)
        self.edit_receipt(item, "title")                                           # a receipt without it: the work item's name says it
        v3 = self.write(opening.replace("第 1 版（2026-11-21）", "第 3 版（2026-11-23）"), "v3.md")
        self.quiet(docs.revise, self.lg(), item, v3, "再改日期")
        self.assertEqual(self.gates()["pending"][item]["title"], "实验平台用什么隔离候选代码")
        v4 = self.draft(PERMISSION, "v4.md", edit=version(4, "2026-11-24", lambda t: t.replace(
            "# 决策包：实验平台用什么隔离候选代码", "# 决策包：候选代码在哪里跑")))
        self.quiet(docs.revise, self.lg(), item, v4, "改事项")
        self.assertEqual((self.receipt(item)["title"], self.gates()["pending"][item]["title"]), ("候选代码在哪里跑",) * 2)

    def test_a_revision_that_fails_half_way_is_run_again(self):
        """revise replaces the document's opening, then adds the line in 交接记录, and writes the receipt and the
        local state last. A step that fails says what is written already; the same command run again goes on, and
        交接记录 has one line for the version, also when the line was added and only its answer was lost. The work
        item is not written at any point (1.1.0): its 紧急程度 changes when the scribe runs what revise printed."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        urgent = version(2, "2026-11-22", lambda t: t.replace("- 紧急程度：今天", "- 紧急程度：插队"))
        v2 = self.draft(PERMISSION, "v2.md", edit=urgent)
        log = lambda: re.findall(r"第 2 版（2026-11-22）：", self.doc(item)["body"].split("## 交接记录", 1)[1])
        versions = lambda: (self.receipt(item)["version"], self.gates()["pending"][item]["version"])
        urgency = self.lg().field("decision", "urgency")["field_key"]
        shown = lambda: self.fake.shown(self.fake.item(item)["type_key"], urgency, self.value(item, "decision", "urgency"))["label"]
        # the opening fails: nothing has changed anywhere
        text = self.fails(docs.revise, self.lg(replies=[(("docs", "+update", "block_replace"), ledger_core.LedgerError("down"))]), item, v2, "改成插队")
        self.assertEqual(text, "down\n主文档还没改；修好后重跑同一条命令")
        self.assertTrue(self.doc(item)["body"].startswith("- 包类型：决策包\n- 版本：第 1 版（2026-11-21）\n"))
        self.assertEqual((log(), versions(), shown()), ([], (1, 1), "今天"))
        # the line in 交接记录 fails: the opening is the new version already
        text = self.fails(docs.revise, self.lg(replies=[(("docs", "+update", "append"), ledger_core.LedgerError("down"))]), item, v2, "改成插队")
        self.assertEqual(text, "down\n主文档开头已经换成第 2 版；回执和本地决策状态还是第 1 版。修好后重跑同一条命令接着做，交接记录不会多出一行")
        self.assertTrue(self.doc(item)["body"].startswith("- 包类型：决策包\n- 版本：第 2 版（2026-11-22）\n"))
        self.assertEqual((log(), versions()), ([], (1, 1)))
        # the line is added but its answer never comes: the next run finds it and adds no second one
        lg = self.lg()

        def lost(argv):
            self.fake.answer(argv, lg.calls[-1][2])
            raise ledger_core.WriteMaybeDone("写命令超时（120 秒）")
        lg.replies.append((("docs", "+update", "append"), lost))
        with self.assertRaises(ledger_core.WriteMaybeDone) as e, contextlib.redirect_stdout(io.StringIO()):
            docs.revise(lg, item, v2, "改成插队")
        self.assertIn("主文档开头已经换成第 2 版；回执和本地决策状态还是第 1 版", str(e.exception))
        self.assertEqual((len(log()), versions()), (1, (1, 1)))
        lg = self.lg()
        _, out = self.out(docs.revise, lg, item, v2, "改成插队")
        self.assertIn("修订：#%s 的主文档开头换成第 2 版（文档链接不变），交接记录里已经有这一版的一行，没有再加。" % item, out)
        self.assertEqual((len(log()), versions(), self.gates()["pending"][item]["urgency"]), (1, (2, 2), "插队"))
        self.assertEqual(lg.wrote("workitem"), [])                                     # not one write on the work item,
        self.assertEqual(shown(), "今天")                                               # which waits for the scribe
        self.assertIn("workitem.py update %s --set 紧急程度=插队 --apply" % item, out)
        self.scribe_writes(lg, item)
        self.assertEqual(shown(), "插队")
        # the order of the writes, on a run with nothing in its way
        lg = self.lg()
        self.quiet(docs.revise, lg, item, self.draft(PERMISSION, "v3.md", edit=version(3, "2026-11-23")), "改回今天")
        wrote = [("fields" if "workitem" in argv else argv[argv.index("--command") + 1]) for argv, write, _ in lg.calls if write]
        self.assertEqual(wrote, ["block_replace", "append"])

    def test_another_sessions_note_is_not_the_line_of_the_revision(self):
        """交接记录 may name the version a revision brings before it is there: a session wrote “准备出第 2 版（日期）：…”.
        Only the line revise itself writes, the one that starts with the scribe's tag, says the revision is logged;
        with another session's note in the way the line is added all the same (it was not, and what the revision
        changed was missing from 交接记录)."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        self.quiet(docs.append, self.lg(), item, self.write("[Infra+2] 准备出第 2 版（2026-11-22）：先和算法对一下口径\n", "note.md"))
        _, out = self.out(docs.revise, self.lg(), item, self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22")), "改日期")
        self.assertIn("修订：#%s 的主文档开头换成第 2 版（文档链接不变），交接记录追加了一行。" % item, out)
        log = self.doc(item)["body"].split("## 交接记录", 1)[1]
        self.assertEqual(len(re.findall(r"^\[PMO\+0\] 第 2 版（2026-11-22）：改日期$", log, re.M)), 1)
        self.assertIn("[Infra+2] 准备出第 2 版（2026-11-22）：先和算法对一下口径", log)
        # the line itself is found as an export may write it: brackets escaped, in a list, in a quote; not inside another line
        lg = self.lg()
        url = lg.doc_url(item)
        heads = ledger_core.heading_ids(lg, url)
        for text, found in (("[PMO+0] 第 3 版（2026-11-23）：改回今天", True), ("\\[PMO+0\\] 第 3 版（2026-11-23）：改回今天", True),
                            ("- [PMO+0] 第 3 版 (2026-11-23): 改回今天", True), ("> [PMO+0] 第 3 版（2026-11-23）：改回今天", True),
                            ('<p id="b1">一行</p><p id="b2">[PMO+0] 第 3 版（2026-11-23）：改回今天</p>', True),
                            ('<ul><li id="b3">[Infra+2] 等 [PMO+0] 第 3 版（2026-11-23）：再说</li></ul>', False),
                            ("[Infra+2] 第 3 版（2026-11-23）：我来改", False), ("[Infra+2] 等 [PMO+0] 第 3 版（2026-11-23）：再说", False),
                            ("[PMO+0] 第 3 版（2026-11-24）：改回今天", False), ("[PMO+0] 第 13 版（2026-11-23）：改回今天", False)):
            reader = self.lg(replies=[(("docs", "+fetch", "section"), {"data": {"document": {"content": "## 交接记录\n\n一行\n\n%s\n" % text}}})])
            self.assertEqual(docs.revision_logged(reader, url, heads, 3, "2026-11-23"), found, text)

    def test_the_next_revision_keeps_the_link_to_its_item(self):
        """revise writes the bare ID back into the draft, and the next version is written from that file: the main
        document's 关联工作项 still links to the work item."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION))
        linked = "- 关联工作项：[#%s](https://project.feishu.cn/" % item
        self.assertIn(linked, self.doc(item)["body"])
        v2 = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22"))
        self.quiet(docs.revise, self.lg(), item, v2, "改日期")
        with open(v2, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("- 关联工作项：#%s\n" % item, text)
        self.write(text.replace("第 2 版（2026-11-22）", "第 3 版（2026-11-23）"), "v2.md")
        self.quiet(docs.revise, self.lg(), item, v2, "再改日期")
        body = self.doc(item)["body"]
        self.assertTrue(body.startswith("- 包类型：决策包\n- 版本：第 3 版（2026-11-23）\n"))
        self.assertIn(linked, body)
        self.assertNotIn("- 关联工作项：#%s\n" % item, body)
        self.assertEqual(ledger_core.link_line("- 关联工作项：#7\n", "[#7](u)", "7"), "- 关联工作项：#7\n")        # elsewhere a line that names its item is left alone
        self.assertEqual(ledger_core.link_line("- 关联工作项：#7；来源工作项 #5\n", "[#7](u)", "7", relink=True), "- 关联工作项：[#7](u)；来源工作项 #5\n")

    def test_a_revision_does_not_change_the_relation(self):
        """A request package revised into another relation is another work item: the error says which relation
        the revision would register as, instead of calling both sides a request package."""
        self.task()
        request = self.quiet(register, self.lg(), self.draft(ASSIGN, "a.md"))
        second = lambda t: re.sub(r"第 1 版（", "第 2 版（", t, 1)
        text = self.fails(docs.revise, self.lg(), request, self.draft(CROSS, "c2.md", edit=second), "改成跨序列")
        self.assertEqual(text, "修订稿登记出来是跨序列需求（关系“跨序列”），#%s 是同序列需求：包类型和关系都不变才能修订；要改关系，另写一份登记" % request)
        text = self.fails(docs.revise, self.lg(), request, self.draft(EXECUTE, "e2.md", edit=lambda t: second(t).replace("- 来源工作项：#2100\n", "")), "改成执行")
        self.assertEqual(text, "修订稿的关系是“执行”，#%s 登记的是“下发”：包类型和关系都不变才能修订；要改关系，另写一份登记" % request)
        cross = self.quiet(register, self.lg(), self.draft(CROSS, "c.md"))
        text = self.fails(docs.revise, self.lg(), cross, self.draft(ASSIGN, "a2.md", edit=second), "改成下发")
        self.assertEqual(text, "修订稿登记出来是同序列需求（关系“下发”），#%s 是跨序列需求：包类型和关系都不变才能修订；要改关系，另写一份登记" % cross)
        self.assertEqual((self.receipt(request)["version"], self.receipt(cross)["version"]), (1, 1))
        self.assertEqual(self.quiet(docs.revise, self.lg(), request, self.draft(ASSIGN, "a3.md", edit=second), "补一条验收标准"), 2)

    def test_a_decision_registered_before_1_0_4_is_held_once_revised(self):
        """Upgrading: a decision still waiting from before 1.0.4 has no record in the local state; its sender
        writes version 2 on the new template and the scribe revises it. From then on it is held, its
        expiry counted from when its work item was created."""
        self.task()
        old = self.quiet(register, self.lg(), self.draft(PERMISSION))
        with open(ledger_core.gates_path(self.tmp), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "pending": {}, "releases": []}, f)                 # as 1.0.3 left things
        self.edit_receipt(old, "version", "registered_at", "options", "frozen", "reversible", "default", "default_option", "criteria")
        self.assertIn("1.0.4 以前登记的", self.fails(workitem.confirm, self.lg(), old, "研究员", ["决定内容=x", "决定人=研究员"], option="本机 Docker"))
        self.quiet(docs.revise, self.lg(), old, self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22")), "按 1.0.4 的模板补全")
        entry = self.gates()["pending"][old]
        self.assertEqual((entry["version"], entry["frozen"][0]["text"]), (2, "特权应用：Docker"))
        created = self.fake.item(old)["nodes"][0]["started_at"]
        self.assertEqual(ledger_core.parse_stamp(entry["registered_at"]).timestamp(), created // 1000)

    def test_a_reversible_decision_past_its_expiry_takes_its_default(self):
        """B8: a reversible decision registered 24 hours ago and still undecided. confirm --by 到期默认 records the
        option its package names: 决定内容 starts with 到期默认：, 决定人 is the researcher, and a comment says the
        same. The decision goes on to 待执行 like one the researcher decided."""
        self.task()
        item = self.quiet(register, self.lg(), self.draft(REVERSIBLE))
        entry = self.gates()["pending"][item]
        self.assertEqual((entry["reversible"], entry["default"], entry["default_option"], entry["default_applies"]),
                         (True, "按轮次分目录", "按轮次分目录", True))
        self.assertIn("还没满到期时长 24 小时（默认值）", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        self.age(item, 23)
        self.assertIn("登记了 23 小时，还没满到期时长 24 小时", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        self.assertIn("去掉 --set、--option、--executor 和 --decision", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT, ["决定内容=按轮次分目录"]))
        self.assertIn("去掉 --set、--option、--executor 和 --decision", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT, executor="RnD-p2-01"))
        self.assertEqual(self.state(item), "待决定")
        self.age(item, 25)
        _, out = self.out(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT)
        content = "到期默认：按轮次分目录：runs/rnd/<轮次>/<变体>/（登记后 24 小时内研究员没有决定）"
        self.assertEqual((self.state(item), self.value(item, "decision", "决定内容"), self.value(item, "decision", "决定人")),
                         ("已决定", content, fake_space.ME))
        self.assertIn((item, "[PMO+0] " + content), self.fake.comments)
        self.assertIn("到期默认：照选项“按轮次分目录”办，执行完请书记员归档。", out)
        self.assertNotIn("放行", out)
        self.assertEqual(out.splitlines()[-1], "执行方：RnD-p2-01。")
        self.assertEqual(self.gates()["pending"], {})
        self.assertEqual((self.receipt(item)["option"], self.receipt(item)["decided_by"]), ("按轮次分目录", "到期默认"))
        self.assertIn("现在是“已决定”，不在“待决定”", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        self.assertEqual([str(r["work_item_id"]) for r in self.quiet(ledger.query, self.lg("preview"), "queue")], [])

    def test_what_waits_for_the_researcher_never_takes_a_default(self):
        """B8: an irreversible decision, one the researcher carries out and one that signs are refused by
        confirm --by 到期默认 however long they waited: no decision is recorded. They are not even marked as
        taking a default."""
        self.task()
        perm = self.quiet(register, self.lg(), self.draft(PERMISSION))
        hand = self.quiet(register, self.lg(), self.draft(BY_HAND, "hand.md"))
        signing = self.quiet(register, self.lg(), self.draft(DECISION, "sign.md"))
        for item in (perm, hand, signing):
            self.age(item, 100)
            entry = self.gates()["pending"][item]
            self.assertEqual((entry["default"], entry["default_option"], entry["default_applies"]), ("等研究员", None, False))
        self.assertIn("#%s 是不可逆的决策（判据 2、3），到期只能等研究员" % perm, self.fails(workitem.confirm, self.lg(), perm, workitem.BY_DEFAULT))
        self.assertIn("#%s 的执行方是研究员（或者要研究员亲手做事），到期继续等研究员" % hand, self.fails(workitem.confirm, self.lg(), hand, workitem.BY_DEFAULT))
        self.assertIn("#%s 是不可逆的决策（判据 4）" % signing, self.fails(workitem.confirm, self.lg(), signing, workitem.BY_DEFAULT))
        self.assertEqual(sorted(self.gates()["pending"]), sorted([perm, hand, signing]))
        self.assertEqual([self.state(i) for i in (perm, hand, signing)], ["待决定"] * 3)
        self.assertFalse(any("到期默认" in text for _, text in self.fake.comments))
        talk = self.quiet(register, self.lg(), self.draft(DECIDED, "talk.md"))
        self.assertIn("本地决策状态里没有 #%s" % talk, self.fails(workitem.confirm, self.lg(), talk, workitem.BY_DEFAULT))
        self.assertIn("--by 到期默认 只用在决策上", self.fails(workitem.confirm, self.lg(), self.task(name="t2.md"), workitem.BY_DEFAULT))

    def test_the_expiry_time_comes_from_decisions_md(self):
        import pool
        import setup_core
        path = os.path.join(self.tmp, "org", "decisions.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def write(text):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        self.assertEqual(ledger_core.expiry_hours(self.tmp), (24, "默认", None))
        text = setup_core.render(setup_core.scaffold("org/decisions.md.tmpl"), {"deviations": setup_core.deviations_text(self.spec)})
        self.assertIn("- 可逆决策的到期时长：登记后 24 小时。研究员可以改这一行的数字", text)       # the plugin's section, as init writes it
        write(text)
        self.assertEqual(ledger_core.expiry_hours(self.tmp), (24, "org/decisions.md", None))
        self.assertEqual(pool.expiry_hours(self.tmp), ledger_core.expiry_hours(self.tmp))       # one reader, for the ledger and the inbox hook
        for written, hours in (("登记后 48 小时", 48), ("登记后 2 天", 48), ("登记后 1.5 小时", 1.5)):
            write(text.replace("登记后 24 小时", written))
            self.assertEqual(ledger_core.expiry_hours(self.tmp)[:2], (hours, "org/decisions.md"), written)
        # the researcher's later decision wins, in the words people use for a change: the time it changes to is read
        for said, hours in (("可逆决策的到期时长改成登记后 36 小时", 36), ("可逆决策的到期时长从 24 小时改成 36 小时", 36),
                            ("可逆决策的到期时长由登记后 24 小时延长到 48 小时", 48), ("可逆决策的到期时长不再用 24 小时，改成 2 天", 48),
                            ("可逆决策的到期时长：登记后 24 小时缩短到 12 小时", 12), ("可逆决策的到期时长改为：登记后 3 天", 72)):
            write(text.replace("（暂无）", "- 2026-11-20：%s。决策 #7001。" % said))
            self.assertEqual(ledger_core.expiry_hours(self.tmp), (hours, "org/decisions.md", None), said)
        # what cannot be read without guessing is not guessed at: 24 hours and a reminder
        # another unit, or none, is a time stated all the same (1.0.6: such a line was passed over without a word)
        for said in ("可逆决策的到期时长：登记后 24 小时或者 48 小时", "可逆决策的到期时长改成两天", "可逆决策的到期时长：登记后两天",
                     "可逆决策的到期时长：48h", "可逆决策的到期时长：72", "可逆决策的到期时长：一周", "可逆决策的到期时长：登记后 48 hours",
                     "可逆决策的到期时长：2d（研究员 2026-11-20 定）", "可逆决策的到期时长改成 48h", "可逆决策的到期时长是 2d",
                     "可逆决策的到期时长为一周", "可逆决策的到期时长 = 36", "可逆决策的到期时长 48h"):
            write(text.replace("（暂无）", "- 2026-11-20：%s。" % said))
            hours, source, note = ledger_core.expiry_hours(self.tmp)
            self.assertEqual((hours, source), (24, "默认"), said)
            self.assertIn("读不出时长", note, said)
        # a line that only mentions the key states no time: the plugin's line stands, with the number the researcher put there
        for said in ("可逆决策的到期时长先不动，下个月再看", "可逆决策的到期时长见上", "可逆决策的到期时长是否要改，下周再定",
                     "入口要在周报里写可逆决策的到期时长有没有改过。决策 #7001"):
            write(text.replace("（暂无）", "- 2026-11-20：%s。" % said))
            self.assertEqual(ledger_core.expiry_hours(self.tmp), (24, "org/decisions.md", None), said)
            write(text.replace("登记后 24 小时", "登记后 48 小时").replace("（暂无）", "- 2026-11-20：%s。" % said))
            self.assertEqual(ledger_core.expiry_hours(self.tmp), (48, "org/decisions.md", None), said)
        write(text.replace("登记后 24 小时", "登记后两天"))
        hours, source, note = ledger_core.expiry_hours(self.tmp)
        self.assertEqual((hours, source), (24, "默认"))
        self.assertIn("读不出时长", note)
        write(text)
        self.task()
        item = self.quiet(register, self.lg(), self.draft(REVERSIBLE))
        waits = self.quiet(register, self.lg(), self.draft(PERMISSION, "p.md"))
        self.age(item, 30)
        self.age(waits, 30)
        self.assertEqual(ledger.past_due_waiting(self.lg("preview")), {waits})
        # the researcher changes the time: the queue's list and 到期默认 follow at once, with no write in between
        before = self.gates()
        write(text.replace("登记后 24 小时", "登记后 48 小时"))
        self.assertEqual(ledger.past_due_waiting(self.lg("preview")), set())
        self.assertIn("登记了 30 小时，还没满到期时长 48 小时（org/decisions.md）", self.fails(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT))
        self.assertEqual(self.gates(), before)
        self.age(item, 49)
        self.age(waits, 49)
        self.assertEqual(ledger.past_due_waiting(self.lg("preview")), {waits})
        self.quiet(workitem.confirm, self.lg(), item, workitem.BY_DEFAULT)
        self.assertTrue(self.value(item, "decision", "决定内容").endswith("（登记后 48 小时内研究员没有决定）"))
        write(text.replace("登记后 24 小时", "登记后 12 小时"))                                # shorter: also at once
        short = self.quiet(register, self.lg(), self.draft(REVERSIBLE, "short.md"))
        self.age(short, 13)
        self.quiet(workitem.confirm, self.lg(), short, workitem.BY_DEFAULT)
        self.assertTrue(self.value(short, "decision", "决定内容").endswith("（登记后 12 小时内研究员没有决定）"))

    def test_the_cli_takes_the_new_arguments(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            path = self.draft(PERMISSION, "v2.md", edit=version(2, "2026-11-22"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = docs.main(["--offline", "revise", "2140", path, "--note", "补上 CAMPUS-HPC"])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("--command block_replace --start-block-id '<主文档第一块的 block_id>' --end-block-id '<状态包前一块的 block_id>'", buf.getvalue())
            self.assertIn("--command append", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = workitem.main(["--offline", "confirm", "2140", "--by", "研究员", "--option", "本机 Docker", "--type", "decision"])
            self.assertEqual(code, 0, buf.getvalue())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                docs.main(["--offline", "revise", "2140", path])                    # --note is required
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)


class RunConditionsTest(LedgerCase):
    """B7: a research task sheet's 执行环境 and 计算资源 say 待研究员确认 or name the decision that set them;
    registering one that names a concrete choice without a decision ID prints a hint."""

    def test_a_concrete_choice_without_a_decision_gets_a_hint(self):
        sheet = SHEET % ("探索", "算法 +2（Alg-p2-01）") + "\n## 资源上限\n- 总预算初值：50 美元\n- 执行环境：%s\n- 计算资源：%s\n"
        _, out = self.out(register, self.lg(), self.write(sheet % ("本机 Docker", "待研究员确认"), "t1.md"))
        self.assertIn("提示：研究任务单“资源上限”里的执行环境写了“本机 Docker”，没有定下它的决策 ID：执行环境要进决策队列", out)
        self.assertNotIn("计算资源写了", out)
        _, out = self.out(register, self.lg(), self.write(sheet % ("本机 Docker（决策 #2140）", "2 张 A100"), "t2.md"))
        self.assertNotIn("执行环境写了", out)
        self.assertIn("计算资源写了“2 张 A100”，没有定下它的决策 ID：计算资源要进决策队列", out)
        for env, compute in (("待研究员确认", "<待研究员确认；或者写定下它的决策 ID>"), ("CAMPUS-HPC（#2150）", "待定：第 4 步签字时定")):
            _, out = self.out(register, self.lg(), self.write(sheet % (env, compute), "t-%s.md" % env[:3]))
            self.assertNotIn("提示：", out, env)
        with open(os.path.join(ROOT, "skills", "charter", "assets", "charter.md"), encoding="utf-8") as f:
            template = orgspec.template_block(f.read())
        self.assertIn("- 执行环境：<待研究员确认；或者写定下它的决策 ID", template)
        self.assertIn("- 计算资源：<待研究员确认；或者写定下它的决策 ID>", template)


class PackageCheckTest(LedgerCase):
    """The package check on what 1.0.4 added: 版本 on every package; on a decision package 可逆性, 到期默认动作,
    the options table's 不可逆的动作 (要放行的不可逆动作 until 1.0.6), 冻结的动作, 已发生的偏向性工作, 没有列入的选项
    and 约束的出处, and how they agree. From 1.0.6 no hook reads what a package lists, so a note after a category
    and a frozen action are free text; what the check keeps: a category is one of the list, and a decision one
    of whose options lists a category, or that signs, says 不可逆."""

    def lint(self, text):
        return orgspec.lint_text(self.spec, text)[0]

    def test_a_decision_package_without_the_unlisted_options_or_their_sources_goes_back(self):
        """B7: a decision package missing 没有列入的选项 or 约束的出处 is sent back, and nothing is registered."""
        good = example(REVERSIBLE)
        self.assertEqual(self.lint(good), [])
        unlisted = "## 没有列入的选项\n两种目录都建、用软链接互指：两份目录要一起维护，工程和质量都不同意。\n\n"
        source = "## 约束的出处\n“打分记录要能按轮次找到”出自评测协议 v1 第 3 节，第 4 步签字时研究员确认过（决策 #2040）。\n\n"
        for cut, name in ((unlisted, "没有列入的选项"), (source, "约束的出处")):
            self.assertIn(cut, good)
            self.assertEqual(self.lint(good.replace(cut, "")), ["缺栏目“%s”；没有内容写“无”" % name])
        self.task()
        self.assertIn("缺栏目“约束的出处”", self.fails(register, self.lg(), self.draft(REVERSIBLE, edit=lambda t: t.replace(source, ""))))
        self.assertEqual(len(self.fake.items), 1)                                   # the research task only
        self.assertEqual(self.lint(good.replace(source, "## 约束的出处\n\n")),
                         ["栏目“约束的出处”是空的：写限定选项的约束出自哪份文件、谁定的、研究员确认过没有；没有写“无”"])

    def test_every_package_says_its_version(self):
        for block in (ASSIGN, DELIVERY, CROSS, EXECUTE, EXECUTE_DELIVERY, DECISION, DECIDED, BY_HAND, PERMISSION, REVERSIBLE):
            text = example(block)
            line = re.search(r"^- 版本：(.*)$", text, re.M).group(1)
            self.assertEqual(orgspec.version_of(line), 1, block)
            how = ("交付包改了就用 reply 重新交，版本号加 1" if block in (DELIVERY, EXECUTE_DELIVERY)       # a delivery is no main document
                   else "已经登记的包要改，写上一版加 1，请书记员用 revise 换主文档")
            self.assertEqual(self.lint(text.replace("- 版本：%s\n" % line, "")), ["包头缺“版本”：写“第 1 版（YYYY-MM-DD）”；" + how], block)
            for bad, why in (("v1", "写成“第 N 版（YYYY-MM-DD）”"), ("第 1 版（2026-02-30）", "日期 2026-02-30 不存在"), ("第 0 版（2026-11-02）", "从第 1 版算起")):
                errors = self.lint(text.replace("- 版本：%s\n" % line, "- 版本：%s\n" % bad))
                self.assertEqual(len(errors), 1, (block, errors))
                self.assertIn(why, errors[0])
        self.assertEqual(orgspec.read_version("第 12 版（2026-1-5）"), (12, "2026-01-05", None))
        self.task()
        text = self.fails(register, self.lg(), self.draft(ASSIGN, edit=lambda t: t.replace("第 1 版（2026-10-19）", "第 2 版（2026-10-19）")))
        self.assertIn("包头“版本”写第 1 版，现在是第 2 版：已经登记过的包改了内容，用 docs.py revise", text)

    def test_a_reply_date_that_does_not_exist_goes_back(self):
        text = example(ASSIGN).replace("- 回复期限：两周内交付", "- 回复期限：2026-02-30 以前")
        self.assertEqual(self.lint(text), ["包头“回复期限”写的 2026-02-30 不是存在的日期：写存在的日期 YYYY-MM-DD，或者写“无”"])  # a request's is its deadline
        decision = re.sub(r"^- 回复期限：.*$", "- 回复期限：2026-02-30", example(REVERSIBLE), flags=re.M)
        self.assertEqual(self.lint(decision), ["包头“回复期限”写的 2026-02-30 不是存在的日期：写存在的日期 YYYY-MM-DD，或者写“无”；决策包的回复期限只作参考"])
        self.assertEqual(self.lint(example(ASSIGN).replace("- 回复期限：两周内交付", "- 回复期限：2026-11-30")), [])

    def test_reversibility_and_the_default_agree(self):
        rev, perm, hand = example(REVERSIBLE), example(PERMISSION), example(BY_HAND)
        lint = self.lint
        crit = "判据：1 花钱或占用额度，2 改权限、身份或机器的系统状态，3 离开本机或组织，4 定下付费运行所依赖的实验条件，5 销毁或移动数据"
        self.assertEqual(lint(rev.replace("- 可逆性：可逆\n", "")), ["包头缺“可逆性”：写“可逆”，或者“不可逆（判据 N）”，" + crit])
        self.assertEqual(lint(rev.replace("- 可逆性：可逆", "- 可逆性：不可逆")),
                         ["包头“可逆性”写的是“不可逆”：不可逆的要写判据号，比如“不可逆（判据 2）”"])
        self.assertEqual(lint(rev.replace("- 可逆性：可逆", "- 可逆性：不可逆（判据 9）")),
                         ["包头“可逆性”写的是“不可逆（判据 9）”：没有判据 9；写“可逆”，或者“不可逆（判据 N）”，" + crit])
        self.assertEqual(lint(rev.replace("- 可逆性：可逆", "- 可逆性：大概可逆")), ["包头“可逆性”写的是“大概可逆”：写“可逆”，或者“不可逆（判据 N）”"])
        # a decision one of whose options lists a category of the list, or that signs, is irreversible
        listed = rev.replace("| 续跑脚本要改 | 无 |", "| 续跑脚本要改 | 特权应用：Docker |")
        self.assertEqual(lint(listed), ["选项“按变体分目录”（特权应用：Docker）列了不可逆事项清单里的类别，包头“可逆性”要写不可逆，并写判据号"])
        both = rev.replace("| 无 | 无 |", "| 无 | 受保护路径：repo/eval/split.py |").replace("| 续跑脚本要改 | 无 |", "| 续跑脚本要改 | 花钱；对外发送：git push |")
        self.assertEqual(lint(both), ["选项“按轮次分目录”（受保护路径：repo/eval/split.py）、“按变体分目录”（花钱；对外发送：git push）"
                                      "列了不可逆事项清单里的类别，包头“可逆性”要写不可逆，并写判据号"])
        signing = rev.replace("## 待签版本\n无。", "## 待签版本\n规格 v3（链接），签的是研究任务 #2000 第 4 步。")
        self.assertEqual(lint(signing), ["这条决策用来签字（“待签版本”写了要签的版本），包头“可逆性”要写不可逆：签字关不走到期默认"])
        # what an irreversible decision, and one the researcher has a hand in, waits for: the researcher
        self.assertEqual(lint(rev.replace("- 可逆性：可逆", "- 可逆性：不可逆（判据 4）")),
                         ["可逆性是不可逆，包头“到期默认动作”就要写“等研究员”：不可逆的决策到期只能等研究员"])
        self.assertEqual(lint(rev.replace("- 执行方：RnD-p2-01\n- 研究员要做的事：无", "- 执行方：研究员\n- 研究员要做的事：把旧记录拷到 NAS")),
                         ["执行方是研究员，包头“到期默认动作”就要写“等研究员”：到期只能等研究员，不走到期默认"])
        self.assertEqual(lint(hand.replace("- 到期默认动作：等研究员", "- 到期默认动作：每天 30 美元")),
                         ["可逆性是不可逆，包头“到期默认动作”就要写“等研究员”：不可逆的决策到期只能等研究员"])
        self.assertEqual(lint(perm), [])
        # a reversible one names an option of its table
        self.assertEqual(lint(rev.replace("- 到期默认动作：按轮次分目录", "- 到期默认动作：等研究员")),
                         ["可逆的决策到期按包头的到期默认动作办：包头“到期默认动作”写选项表里的一个选项（按轮次分目录、按变体分目录），不写“等研究员”"])
        self.assertEqual(lint(rev.replace("- 到期默认动作：按轮次分目录", "- 到期默认动作：按建议办")),
                         ["包头“到期默认动作”写的“按建议办”不是选项表里的选项：写选项表第一列的名字之一（按轮次分目录、按变体分目录）"])
        self.assertEqual(lint(rev.replace("- 到期默认动作：按轮次分目录", "- 到期默认动作：按轮次分目录（工程的建议）")), [])
        self.assertEqual(lint(rev.replace("- 到期默认动作：按轮次分目录", "- 到期默认动作：按轮次分目录或按变体分目录")),
                         ["包头“到期默认动作”写的“按轮次分目录或按变体分目录”写了不止一个选项：写选项表第一列的名字之一（按轮次分目录、按变体分目录）"])
        self.assertTrue(lint(rev.replace("- 到期默认动作：按轮次分目录", "- 到期默认动作：无"))[0].startswith("决策队列的决策包，“到期默认动作”不写“无”："))
        self.assertEqual(lint(rev.replace("- 到期默认动作：按轮次分目录\n", "")),
                         ["包头缺“到期默认动作”：可逆的写到期按哪个选项办（选项表第一列的名字）；不可逆的、执行方是研究员或者研究员要亲手做事的写“等研究员”；研究员主动发起的写“无”"])
        # decided in a talk: it never waits
        talk = example(DECIDED)
        self.assertEqual(lint(talk), [])
        self.assertEqual(lint(talk.replace("- 到期默认动作：无", "- 到期默认动作：从搜索开始计时")),
                         ["研究员主动发起的决策已经定了，不会到期：包头“到期默认动作”写“无”"])
        table = ("## 选项\n| 选项 | 做法 | 代价 | 风险 | 不可逆的动作 |\n| --- | --- | --- | --- | --- |\n"
                 "| 从搜索开始计时 | 四周从搜索开始，试点期另设两周 | 试点观察期变短 | 两周可能看不出工程师是否主动使用 | 无 |\n"
                 "| 从试点开始计时 | 四周从试点开始 | 算法预算要多覆盖约一周 | 超出已签的 300 GPU 小时 | 无 |")
        self.assertIn(table, talk)
        self.assertEqual(lint(talk.replace(table, "## 选项\n无。")), [])               # options not compared in the talk

    def test_the_options_table_says_what_each_option_does_of_the_list(self):
        """The column 不可逆的动作: the categories of the list an option does, each with a note if wanted, 无 for
        none. Every one of the nine categories may be written, 受保护路径 included; a part that opens with no
        category is sent back; the column's name before 1.0.6 is sent back with the new one."""
        rev, lint = example(REVERSIBLE), self.lint
        names = "管理员权限、特权应用、系统和账号、权限、Docker 资源、对外发送、受保护路径、花钱、付费运行的条件"
        cell = lambda written: rev.replace("- 可逆性：可逆", "- 可逆性：不可逆（判据 2）").replace(
            "- 到期默认动作：按轮次分目录", "- 到期默认动作：等研究员").replace("| 续跑脚本要改 | 无 |", "| 续跑脚本要改 | %s |" % written)
        self.assertEqual(lint(cell("无")), [])
        not_listed = "不在不可逆事项清单里：清单里的类别是%s；先写类别，说明写在“：”后面，几类之间用“；”隔开" % names
        self.assertEqual(lint(cell("特权程序：Docker")), ["选项“按变体分目录”的不可逆的动作“特权程序：Docker”" + not_listed])
        self.assertEqual(lint(cell("对外发送：scp；先打包再传")), ["选项“按变体分目录”的不可逆的动作“先打包再传”" + not_listed])
        self.assertEqual(lint(cell("")),
                         ["选项“按变体分目录”的“不可逆的动作”是空的：写这个选项要做的、不可逆事项清单里的事，先写类别，可以带一句说明，"
                          "比如“特权应用：启动 Docker Desktop”；没有写“无”"])
        for name in self.spec.irreversible_names():                                # all nine, 受保护路径 too
            self.assertEqual(lint(cell(name)), [], name)
            self.assertEqual(lint(cell("%s：随便一句说明，hook 不看" % name)), [], name)
        read = lambda written: [(r["name"], r["qualifier"], r["text"]) for r in
                                orgspec.decision_parts(self.spec, cell(written), ledger_core.header_of(cell(written)))[0]["options"][1]["irreversible"]]
        self.assertEqual(read("受保护路径：repo/eval/split.py"), [("受保护路径", "repo/eval/split.py", "受保护路径：repo/eval/split.py")])
        # a note is kept whole: a comma, brackets or a full stop cut nothing off (it used to end at the first of them)
        self.assertEqual(read("对外发送：scp，ssh 到 CAMPUS-HPC"), [("对外发送", "scp，ssh 到 CAMPUS-HPC", "对外发送：scp，ssh 到 CAMPUS-HPC")])
        self.assertEqual(read("特权应用：Docker；Docker 资源：内存调到 10 GiB（研究员同意过）"),
                         [("特权应用", "Docker", "特权应用：Docker"), ("Docker 资源", "内存调到 10 GiB（研究员同意过）", "Docker 资源：内存调到 10 GiB（研究员同意过）")])
        # no application has to be named, and a note may be Chinese only, or sit in brackets
        self.assertEqual(read("特权应用：Podman"), [("特权应用", "Podman", "特权应用：Podman")])
        self.assertEqual(read("对外发送：作业送到集群上"), [("对外发送", "作业送到集群上", "对外发送：作业送到集群上")])
        self.assertEqual(read("花钱（约 8 美元）; 付费运行的条件"), [("花钱", "（约 8 美元）", "花钱（约 8 美元）"), ("付费运行的条件", "", "付费运行的条件")])
        former = rev.replace(" | 不可逆的动作 |", " | 要放行的不可逆动作 |")
        self.assertEqual(lint(former), ["选项表的“要放行的不可逆动作”一列改名“不可逆的动作”了（1.0.6）：把表头改过来；格子里照旧写这个选项要做的、"
                                        "不可逆事项清单里的类别，可以带一句说明，没有写“无”"])
        no_column = rev.replace(" | 不可逆的动作 |\n| --- | --- | --- | --- | --- |", " |\n| --- | --- | --- | --- |").replace(" | 无 | 无 |", " | 无 |").replace(
            "| 续跑脚本要改 | 无 |", "| 续跑脚本要改 |")
        self.assertEqual(lint(no_column), ["选项表少了“不可逆的动作”一列：写这个选项要做的、不可逆事项清单里的事，先写类别，可以带一句说明，"
                                           "比如“特权应用：启动 Docker Desktop”；没有写“无”"])
        one = rev.replace("| 按变体分目录 | runs/rnd/<变体>/<轮次>/ | 工程续跑时要跨目录找同一轮 | 续跑脚本要改 | 无 |\n", "")
        self.assertEqual(lint(one), ["决策队列的决策包，选项写成表，至少两个选项，其中一个可以是“不做”"])
        self.task()
        self.assertIn("一列改名“不可逆的动作”了（1.0.6）", self.fails(register, self.lg(), self.write(former)))   # a draft from before the upgrade
        self.assertEqual(len(self.fake.items), 1)
        with open(os.path.join(ROOT, "skills", "packages", "assets", "decision.md"), encoding="utf-8") as f:
            template = f.read()
        self.assertIn("| 选项 | 做法 | 代价 | 风险 | 不可逆的动作 |", orgspec.template_block(template))
        self.assertNotIn("要放行", template)
        self.assertEqual(orgspec.option_actions({"name": "A", "releases": [{"text": "花钱"}]}), [{"text": "花钱"}])     # a receipt from 1.0.4 or 1.0.5

    def test_the_package_check_says_nothing_about_hooks(self):
        """1.0.6: no hook matches what a package lists, so the check no longer asks for an application of a list, a
        word a command would show or a command prefix, and none of its messages says what a hook does."""
        perm = example(PERMISSION)
        text = perm.replace("| 特权应用：Docker |", "| 特权应用：容器工具 |").replace(
            "- 特权应用：Docker\n", "- 特权应用：容器工具\n- 花钱：调用模型服务的付费接口\n- `rsync`\n- 发给导师的周报先不发\n")
        parts, errors, warnings = orgspec.decision_parts(self.spec, text, ledger_core.header_of(text))
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(orgspec.lint_text(self.spec, text), ([], []))
        self.assertEqual([(f["kind"], f.get("category"), f["text"]) for f in parts["frozen"]][:4], [
            ("category", "privileged_app", "特权应用：容器工具"), ("category", "spend", "花钱：调用模型服务的付费接口"),
            ("text", None, "`rsync`"), ("text", None, "发给导师的周报先不发")])
        for name in ("qualifier_apps", "app_names", "read_releases", "RELEASE_KINDS", "RELEASE_COLUMN", "BACKTICKED"):
            self.assertFalse(hasattr(orgspec, name), name)
        with open(os.path.join(ROOT, "scripts", "orgspec.py"), encoding="utf-8") as f:
            source = f.read()
        for said in ("hook 按", "hook 不拦，只靠", "要 hook 拦下", "不走决策放行"):
            self.assertNotIn(said, source)

    def test_frozen_actions_are_categories_or_sentences(self):
        """冻结的动作, a line each: a category of the list, with a note if wanted, or a sentence. Nothing about how
        it is written is sent back (a command prefix needed two words until 1.0.6). A line that names several
        categories is read as each of them; before, only the first was."""
        rev, lint = example(REVERSIBLE), self.lint
        prefix = "- `python3 tools/relayout_runs.py`"
        frozen = lambda written: [(f["kind"], f.get("name"), f.get("qualifier"), f["text"]) for f in orgspec.decision_parts(
            self.spec, rev.replace(prefix, written), ledger_core.header_of(rev))[0]["frozen"]][:-1]
        for written in ("- `python3 *`", "- `rsync`", "- `* *`", "- 别动 gpu-01"):          # one word, wildcards, a few words: all fine
            self.assertEqual(lint(rev.replace(prefix, written)), [], written)
            self.assertEqual(frozen(written), [("text", None, None, written[2:])])
        self.assertEqual(frozen("- 特权应用：Docker（`open -a Docker`）\n- Docker 资源\n1. 注意：别动 gpu-01 的内存\n* 对外发送（给导师的周报）"), [
            ("category", "特权应用", "Docker（`open -a Docker`）", "特权应用：Docker（`open -a Docker`）"),
            ("category", "Docker 资源", "", "Docker 资源"),
            ("text", None, None, "注意：别动 gpu-01 的内存"),
            ("category", "对外发送", "（给导师的周报）", "对外发送（给导师的周报）")])
        # several categories on a line: every one of them is read
        self.assertEqual(frozen("- 特权应用：Docker；对外发送：git push; 花钱"), [
            ("category", "特权应用", "Docker", "特权应用：Docker"), ("category", "对外发送", "git push", "对外发送：git push"),
            ("category", "花钱", "", "花钱")])
        # a remark after a category stays with it; a sentence with a semicolon in it stays one sentence
        self.assertEqual(frozen("- 特权应用：Docker；等研究员定了再开；对外发送"), [
            ("category", "特权应用", "Docker；等研究员定了再开", "特权应用：Docker；等研究员定了再开"), ("category", "对外发送", "", "对外发送")])
        self.assertEqual(frozen("- 别重启 gpu-01；也别清它的缓存"), [("text", None, None, "别重启 gpu-01；也别清它的缓存")])
        self.assertEqual(frozen("- 特权应用的事先放一放"), [("text", None, None, "特权应用的事先放一放")])      # not the category: no colon after its name
        self.assertEqual(orgspec.decision_parts(self.spec, example(DECIDED), ledger_core.header_of(example(DECIDED)))[0]["frozen"], [])
        for empty in ("<决策定下以前谁都不做的动作>", ""):
            errors = lint(rev.replace(prefix + "\n- 把已有的运行记录挪到新的目录结构下", empty))
            self.assertEqual(errors, ["栏目“冻结的动作”是空的：写决策待定期间谁都不做的动作，一条一行：不可逆事项清单里的类别（可以带一句说明），"
                                      "或者一句话描述；没有写“无”"], empty)
        # what is registered is what the line every pool session is shown each turn says: both categories of the line
        self.task()
        item = self.quiet(register, self.lg(), self.draft(PERMISSION, edit=lambda t: t.replace(
            "- 特权应用：Docker\n", "- 特权应用：Docker；对外发送：scp、ssh 到 CAMPUS-HPC\n")))
        with open(ledger_core.gates_path(self.tmp), encoding="utf-8") as f:
            held = json.load(f)["pending"][item]["frozen"]
        self.assertEqual([(f["kind"], f.get("category"), f["text"]) for f in held], [
            ("category", "privileged_app", "特权应用：Docker"), ("category", "send_out", "对外发送：scp、ssh 到 CAMPUS-HPC"),
            ("text", None, "`docker build`"), ("text", None, "照某个选项改实验平台的计量和内存闸")])

    def test_the_irreversible_list(self):
        """B6: every category says what it covers and which criteria it meets; the decision template names every
        category; what is wrong in the list file is reported by orgspec.py check. 1.0.6: the list is what decision
        packages are written and checked against, no hook reads it, so it holds nothing a hook matched commands
        with or released them by (match rules, path groups, how a category is recognized, how it is released, the
        applications of a category)."""
        spec = self.spec
        self.assertEqual(spec.validate_irreversible(), [])
        self.assertEqual(sorted(k for k in spec.irreversible() if not k.startswith("_")), ["categories", "criteria", "schema_version"])
        for c in spec.irreversible_categories():
            self.assertTrue(c["covers"] and c["criteria"], c["name"])
            self.assertFalse(set(c) - {"id", "name", "covers", "criteria", "criteria_note", "sheet_rows"}, c["name"])
        self.assertEqual(spec.irreversible_names(), ["管理员权限", "特权应用", "系统和账号", "权限", "Docker 资源", "对外发送", "受保护路径", "花钱", "付费运行的条件"])
        for said in ("放行”一列", "recognized_by", "release", "apps"):
            self.assertNotIn(said, spec.irreversible()["_说明"])
        table = spec.irreversible_table()
        self.assertTrue(table.startswith("| 类别 | 包括 | 判据 |\n"), table)
        self.assertIn("| 特权应用 | 启动会装特权组件的应用，先列 Docker Desktop（open -a Docker） | 2 |", table)
        self.assertIn("| 受保护路径 |", table)
        section = spec.irreversible_section()
        self.assertTrue(section.endswith(table + "\n\n" + spec.IRREVERSIBLE_RULE), section)
        self.assertIn("清单里的事要先有一条研究员批准、所选选项里列了它的决策再做；决策待定期间，包里“冻结的动作”谁都不做。", section)
        self.assertIn("hook 不拦这些命令，也不按待定决策冻结", section)
        for claim in ("不走决策放行", "池会话里没有这样的决策就拒绝", "弹窗确认", "识别方式", "放行条件"):
            self.assertNotIn(claim, section)
        with open(os.path.join(ROOT, "skills", "packages", "assets", "decision.md"), encoding="utf-8") as f:
            template = f.read()
        self.assertIn("清单里的类别是%s，" % "、".join(spec.irreversible_names()), template.split("## 填写规则", 1)[1])
        self.assertNotIn("决策不会过期", template)
        broken = json.loads(json.dumps(spec.irreversible()))
        broken["categories"][0]["criteria"] = [7]
        broken["categories"][1]["covers"] = ""
        broken["categories"][2]["name"] = "系统：账号"
        path = os.path.join(self.tmp, "irreversible.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(broken, f, ensure_ascii=False)
        problems = orgspec.OrgSpec(irreversible_path=path).validate()
        for part in ("管理员权限：criteria 要写 criteria 里有的判据号", "特权应用：缺 covers",
                     "name 要是不重复的中文名，不带冒号和分号"):
            self.assertTrue(any(part in p for p in problems), (part, problems))
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(all(p.startswith("不可逆事项清单：") for p in problems))


if __name__ == "__main__":
    unittest.main()
