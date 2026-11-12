#!/usr/bin/env python3
"""Read the organisation rules of the agent-org plugin.

    python3 -I scripts/orgspec.py check            # validate rules.json and contract.json against terms.zh.json
    python3 -I scripts/orgspec.py show packages    # print one rendered section
    python3 -I scripts/orgspec.py lint <file>...   # check package drafts and docs for stale names

orgspec/rules.json holds the rules with stable English identifiers; Chinese names
live only in orgspec/terms.zh.json. Prose in rules.json may contain tokens such as
{pkg:request} or {seq:alg}; render() replaces them with the current names.
orgspec/contract.json holds what the plugin and a Feishu Project template agree on: the
slots scripts handle by meaning (class Contract).
Python 3.9 standard library only.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGSPEC_DIR = os.path.join(PLUGIN_ROOT, "orgspec")
TOKEN = re.compile(r"\{(pkg|rel|wi|seq|rank|node|field|role|channel|abbrev|state|sign):([a-z0-9_]+)\}")
RANK_NUM = {"p0": 0, "p1": 1, "p2": 2}


class OrgSpecError(Exception):
    pass


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise OrgSpecError("读不到 %s：%s" % (path, e))


def md_table(headers, rows):
    def cell(v):
        return str(v).replace("|", "｜").replace("\n", "<br>")
    lines = ["| %s |" % " | ".join(headers), "| %s |" % " | ".join("---" for _ in headers)]
    lines += ["| %s |" % " | ".join(cell(c) for c in r) for r in rows]
    return "\n".join(lines)


class OrgSpec:
    def __init__(self, rules_path=None, terms_path=None):
        self.rules_path = rules_path or os.path.join(ORGSPEC_DIR, "rules.json")
        self.terms_path = terms_path or os.path.join(ORGSPEC_DIR, "terms.zh.json")
        self.rules = _load(self.rules_path)
        self.terms = _load(self.terms_path)

    # ---------------------------------------------------------------- names
    def term(self, group, key):
        try:
            return self.terms[group][key]
        except KeyError:
            raise OrgSpecError("术语表里没有 %s:%s" % (group, key))

    def render(self, value):
        if isinstance(value, list):
            return [self.render(v) for v in value]
        if not isinstance(value, str):
            return value
        return TOKEN.sub(lambda m: self.term(m.group(1), m.group(2)), value)

    def pkg(self, pid):
        return self.term("pkg", pid)

    # ------------------------------------------------------------ sequences
    def sequences(self):
        """[(id, Chinese name, abbreviation)] in rule order."""
        return [(s["id"], self.term("seq", s["id"]), s["abbrev"]) for s in self.rules["sequences"]]

    def node_keys(self, seqs=None):
        """The knowledge-base node skeleton as keys: the top-level nodes, then a node per sequence
        under each node that has one (every sequence unless seqs names the enabled ones)."""
        tree = self.rules["docs_tree"]
        seqs = [s for s, _, _ in self.sequences()] if seqs is None else list(seqs)
        return list(tree["nodes"]) + ["%s/%s" % (n, s) for n in tree["per_sequence"] for s in seqs]

    @staticmethod
    def role_name(seq, rank):
        """The agent name of a sequence and rank: pmo-router and pmo-scribe for the PMO's +1 and +0."""
        if seq == "pmo" and rank == 1:
            return "pmo-router"
        if seq == "pmo" and rank == 0:
            return "pmo-scribe"
        return "%s-p%d" % (seq, rank)

    def _edge_targets(self, role, seq, rank, edges, seqs):
        out = []
        for e in edges:
            src = e["from"]
            if src != role and src != "p%d" % rank:
                continue
            to, scope = e["to"], e.get("scope")
            if to in ("p0", "p1", "p2"):
                r = int(to[1])
                for s in seqs:
                    if (scope == "same_sequence" and s != seq) or (scope == "other_sequences" and s == seq):
                        continue
                    out.append(self.role_name(s, r))
            elif to not in out:
                out.append(to)
        return out

    def dispatch_targets(self, role, seq, rank, seqs):
        """Whom a role dispatches work to: the assign edges of the dispatch graph in rules.json."""
        return self._edge_targets(role, seq, rank, self.rules["dispatch"]["assign"], seqs)

    def dispatch_table(self):
        """The dispatch graph as text for org/org.md: who assigns work to whom, and the message kinds."""
        d = self.rules["dispatch"]
        rank_cn = {"p0": "+0", "p1": "+1", "p2": "+2"}

        def end(x):
            if x in rank_cn:
                return "各序列 " + rank_cn[x]
            seq, _, rank = x.rpartition("-")
            names = {"pmo-router": "项目管理 +1（路由）", "pmo-scribe": "项目管理 +0（书记员）"}
            return names.get(x) or "%s %s" % (self.term("seq", seq), rank_cn.get(rank, rank))
        scope = {"same_sequence": "同序列", "other_sequences": "别的序列", None: ""}
        rows = [[end(e["from"]), end(e["to"]), scope[e.get("scope")]] for e in d["assign"]]
        kinds = []
        for kind, rule in d["kinds"].items():
            if rule.get("along"):
                how = "沿派活的边" + ("反向（交回派活方）" if rule.get("reverse") else "")
            elif rule.get("to"):
                how = "任何会话发给%s" % end(rule["to"])
            elif rule.get("from"):
                how = "%s发给任何会话" % end(rule["from"])
            else:
                how = "不限"
            kinds.append([self.term("msg", kind), how])
        return "\n".join([md_table(["派活方", "接活方", "范围"], rows), "",
                          md_table(["消息", "方向"], kinds), "",
                          d.get("note", "")])

    def message_header_re(self):
        """The header that starts every message between pool sessions: 【派活 #2101】Alg-p2-01 → Alg-p1-01."""
        names = sorted(self.terms["msg"].values(), key=len, reverse=True)
        name = r"[A-Za-z]+-p[0-2]-\d+"
        return re.compile(r"【(%s)(?:\s*#(\d{3,}))?[^】]*】\s*(?:(%s)\s*(?:→|->)\s*(%s))?" % (
            "|".join(re.escape(n) for n in names), name, name))

    def sequence(self, sid):
        for s in self.rules["sequences"]:
            if s["id"] == sid:
                break
        else:
            raise OrgSpecError("未知序列：%s" % sid)
        cn = self.term("seq", sid)
        ranks = {}
        for rid, row in s["ranks"].items():
            ranks[RANK_NUM[rid]] = {"label": row["label"], "given": self.render(row["given"]),
                                   "outputs": self.render(row["outputs"]), "escalate": self.render(row["escalate"])}
        if sorted(ranks) != [0, 1, 2]:
            raise OrgSpecError("%s 序列的职级分工不是 +0、+1、+2 三行" % cn)
        return {"id": sid, "cn": cn, "abbrev": s["abbrev"], "title": s["title"],
                "heading": "序列设置 - %s：%s" % (cn, s["title"]), "focus": self.render(s["focus"]),
                "intro": self.render(s["intro"]), "ranks": ranks, "notes": self.render(s.get("notes") or "")}

    # ------------------------------------------------------- whole-org text
    def rank_rows(self):
        return {RANK_NUM[r["id"]]: r for r in self.rules["ranks"]}

    def rank_table(self):
        rows = [["+%d" % RANK_NUM[r["id"]], r["given"], r["outputs"], r["context"], r["escalate"]]
                for r in self.rules["ranks"]]
        return md_table(["级别", "给定的上下文", "需要产出", "上下文维护标准", "什么时候上报"], rows)

    def general_rules(self):
        return [(g["head"], self.render(g["body"])) for g in self.rules["general_rules"]]

    def general_rules_heads(self):
        return [h for h, _ in self.general_rules()]

    def handoff_rules(self):
        return "\n".join("- " + self.render(r) for r in self.rules["handoff_rules"])

    def signoff_items(self):
        return "\n".join("- " + i for i in self.rules["only_humans"])

    def task_rules(self):
        """How a research task moves: who advances a step, the freeze, roll-backs, the two flows."""
        return "\n".join("- " + self.render(r) for r in self.rules["task_rules"])

    def sequence_overview(self):
        return md_table(["序列", "重心"], [[self.term("seq", s["id"]), self.render(s["focus"])]
                                          for s in self.rules["sequences"]])

    def package_table(self):
        rows = [[self.pkg(p["id"]), self.render(p["direction"]), self.render(p["receiver_action"]),
                 self.render(p["focus"]), self.render(p["not_write"])] for p in self.rules["packages"]]
        return md_table(["任务包", "方向", "接收方要做的事", "内容重点", "不写"], rows)

    def relation_matrix(self):
        rels = [r["id"] for r in self.rules["relations"]]
        heads = ["发出方定下的内容"] + ["%s（%s）" % (self.term("rel", r), self._rel_dir(r)) for r in rels]
        rows = [[m["item"]] + [m[r] for r in rels] for m in self.rules["relation_matrix"]]
        return md_table(heads, rows)

    def _rel_dir(self, rid):
        r = next(x for x in self.rules["relations"] if x["id"] == rid)
        a, b = "+%d" % RANK_NUM[r["from_rank"]], "+%d" % RANK_NUM[r["to_rank"]]
        return "%s → %s" % (a, b) if r["same_sequence"] else "%s → 其他序列 %s" % (a, b)

    def package_routes(self):
        rows = []
        for r in self.rules["package_routes"]:
            name = self.pkg(r["pkg"]) + ("（%s）" % self.term("rel", r["rel"]) if r.get("rel") else "")
            rows.append([name, r["from"], r["to"], r["via"]])
        return md_table(["包", "发出方", "接收方", "怎么送到"], rows)

    def routing_table(self):
        rows = [[r["id"], self.render(r["when"]), self.render(r["test"]), self.render(r["detail"])] for r in self.rules["routing"]]
        return md_table(["编号", "收到什么", "判据", "动作"], rows)

    # --------------------------------------------------------------- feishu
    def work_items(self):
        return self.rules["feishu"]["work_items"]

    def work_item(self, wid):
        for w in self.work_items():
            if w["id"] == wid:
                return w
        raise OrgSpecError("未知工作项类型：%s" % wid)

    def field_name(self, fid):
        return self.term("field", fid)

    def work_items_table(self):
        rows = []
        for w in self.work_items():
            replies = "、".join(self.pkg(p) for p in w.get("replies", [])) or "无"
            who = {"researcher": self.term("role", "researcher"), "pmo-scribe": self.term("role", "scribe")}.get(
                w.get("created_by"), w.get("created_by"))
            rows.append([self.term("wi", w["id"]), self.render(w["main_doc"]), replies, who])
        return md_table(["工作项类型", "主文档", "挂在下面的答复", "由谁创建"], rows)

    def docs_tree(self):
        t = self.rules["docs_tree"]
        lines = ["- 空间：%s。" % t["space"],
                 "- 一级节点：%s。" % "、".join(self.term("node", n) for n in t["nodes"]),
                 "- 按序列再分一层的节点：%s。" % "、".join(self.term("node", n) for n in t["per_sequence"])]
        for p in t["placement"]:
            pid, _, rel = p["what"].partition(":")
            name = self.pkg(pid) if pid in self.terms["pkg"] else self.term("wi", pid)
            if rel:
                name = "%s关系的%s" % (self.term("rel", rel), name)
            lines.append("- %s放在%s。" % (name, p["parent"]))
        lines += ["- 上传和归位统一由%s做。" % self.term("role", "scribe"),
                  "- " + t["archive"] + "。", "- " + self.render(t["granularity"]) + "。"]
        return "\n".join(lines)

    # ----------------------------------------------------------- validation
    def validate(self):
        problems = []

        def walk(v, where):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, "%s.%s" % (where, k))
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    walk(x, "%s[%d]" % (where, i))
            elif isinstance(v, str):
                for m in TOKEN.finditer(v):
                    if m.group(2) not in self.terms.get(m.group(1), {}):
                        problems.append("%s：术语表里没有 %s" % (where, m.group(0)))
        walk(self.rules, "rules")
        if [r["id"] for r in self.rules["ranks"]] != ["p0", "p1", "p2"]:
            problems.append("ranks 要依次是 p0、p1、p2")
        for s in self.rules["sequences"]:
            if s["id"] not in self.terms["seq"] or s["id"] not in self.terms["abbrev"]:
                problems.append("序列 %s 缺中文名或简写" % s["id"])
            if self.terms["abbrev"].get(s["id"]) != s["abbrev"]:
                problems.append("序列 %s 的简写和术语表不一致" % s["id"])
            if sorted(s["ranks"]) != ["p0", "p1", "p2"]:
                problems.append("序列 %s 的职级分工不全" % s["id"])
        pids = [p["id"] for p in self.rules["packages"]]
        if sorted(pids) != sorted(self.terms["pkg"]):
            problems.append("包的标识和术语表对不上：%s" % pids)
        for p in self.rules["packages"]:
            if p.get("answers") and p["answers"] not in pids:
                problems.append("%s 答复的包 %s 不存在" % (p["id"], p["answers"]))
            if not template_path(p["template"]):
                problems.append("找不到 %s 的模板 %s" % (p["id"], p["template"]))
        ids = [r["id"] for r in self.rules["routing"]]
        if len(ids) != len(set(ids)):
            problems.append("路由决策表的编号有重复")
        for w in self.work_items():
            if w["id"] not in self.terms["wi"]:
                problems.append("工作项类型 %s 缺中文名" % w["id"])
            for pid in w.get("packages", []) + w.get("replies", []):
                if pid not in pids:
                    problems.append("工作项类型 %s 用到的包 %s 不存在" % (w["id"], pid))
        tree = self.rules["docs_tree"]
        for n in tree["nodes"]:
            if n not in self.terms["node"]:
                problems.append("知识库节点 %s 缺中文名" % n)
        for n in tree["per_sequence"]:
            if n not in tree["nodes"]:
                problems.append("按序列分层的节点 %s 不在一级节点里" % n)
        d = self.rules.get("dispatch") or {}
        roles = {self.role_name(x["id"], r) for x in self.rules["sequences"] for r in (0, 1, 2)}
        ends = roles | {"p0", "p1", "p2"}
        for group in ("assign",):
            for e in d.get(group, []):
                for end in (e.get("from"), e.get("to")):
                    if end not in ends:
                        problems.append("dispatch.%s 里的 %s 不是角色或职级" % (group, end))
                if e.get("scope") not in (None, "same_sequence", "other_sequences"):
                    problems.append("dispatch.%s 的 scope 只能是 same_sequence 或 other_sequences" % group)
        for kind, rule in (d.get("kinds") or {}).items():
            if kind not in self.terms.get("msg", {}):
                problems.append("消息种类 %s 缺中文名（术语表的 msg）" % kind)
            for key in ("to", "from"):
                if rule.get(key) and rule[key] not in roles:
                    problems.append("dispatch.kinds.%s 的 %s 不是角色：%s" % (kind, key, rule[key]))
            if rule.get("along") and rule["along"] not in d:
                problems.append("dispatch.kinds.%s 沿用的 %s 不存在" % (kind, rule["along"]))
        if not d.get("kinds"):
            problems.append("规则文件缺 dispatch（派发图）")
        return problems


# -------------------------------------------------------------- contract
FIELD_KINDS = ("select", "multi_select", "text", "multi_text", "date", "link", "number", "bool", "user")


class Contract:
    """orgspec/contract.json: the slots scripts handle by meaning.

    A slot is a type, field, role or state the scripts must find in a space: the main-line
    type, the field that holds the main document, the role that signs, the state that means
    archived. Which object of a space fills a slot is the project's binding (feishu/ids.json).
    Flows, nodes and forms are not here: they stay in the space and are read at run time.
    """

    KINDS = ("fields", "roles", "states")

    def __init__(self, spec, path=None):
        self.spec = spec
        self.path = path or os.path.join(ORGSPEC_DIR, "contract.json")
        self.data = _load(self.path)

    @property
    def version(self):
        return self.data["contract_version"]

    @property
    def flow(self):
        return self.data["flow"]

    def types(self):
        return list(self.data["types"])

    def main_line(self):
        return next(t for t, d in self.data["types"].items() if d.get("main_line"))

    def slots(self, wid, kind):
        """The field, role or state slots a type must fill."""
        return list(self.data["types"][wid][kind])

    def types_with(self, kind, slot):
        return [t for t in self.types() if slot in self.data["types"][t][kind]]

    def field(self, slot):
        return self.data["fields"][slot]

    def signatures(self):
        """Field slots that hold the ID of the decision that signed a step."""
        return [s for s, d in self.data["fields"].items() if d.get("signature")]

    def role(self, slot):
        return self.data["roles"][slot]

    def name(self, kind, slot):
        """The name a slot goes by in the reference template; binding tries it first."""
        if kind == "types":
            return self.spec.term("wi", slot)
        if kind == "fields":
            return self.spec.term("field", slot)
        if kind == "states":
            return self.spec.term("state", slot)
        who = (self.data["roles"].get(slot) or {}).get("who")
        return self.spec.term("seq" if who == "sequence" else "role", slot)

    def option_names(self, slot):
        """The option names scripts write or compare for a select slot; [] when they use none."""
        opt = self.field(slot).get("options")
        if not opt:
            return []
        group = self.spec.terms[opt["group"]]
        return [group[i] for i in opt.get("ids", group)]

    def validate(self):
        problems = []
        d, terms = self.data, self.spec.terms
        if not isinstance(d.get("contract_version"), int) or d["contract_version"] < 1:
            problems.append("contract_version 要是正整数")
        mains = [t for t, x in d["types"].items() if x.get("main_line")]
        if len(mains) != 1:
            problems.append("约定里要正好有一个主线类型（main_line）")
        for slot, f in d["fields"].items():
            if slot not in terms["field"]:
                problems.append("字段槽位 %s 缺中文名" % slot)
            if f.get("type") not in FIELD_KINDS:
                problems.append("字段槽位 %s 的类型 %s 不认识" % (slot, f.get("type")))
            opt = f.get("options")
            if opt and (opt.get("group") not in terms or any(i not in terms[opt["group"]] for i in opt.get("ids", []))):
                problems.append("字段槽位 %s 的选项在术语表里找不到" % slot)
            if not self.types_with("fields", slot):
                problems.append("字段槽位 %s 没有类型用到" % slot)
        for slot, r in d["roles"].items():
            if r.get("who") not in ("sequence", "field", "researcher", "scribe"):
                problems.append("角色槽位 %s 的 who 只能是 sequence、field、researcher 或 scribe" % slot)
            if slot not in terms["seq" if r.get("who") == "sequence" else "role"]:
                problems.append("角色槽位 %s 缺中文名" % slot)
            if r.get("who") == "field":
                for wid in self.types_with("roles", slot):
                    if (r.get("field") or {}).get(wid) not in d["types"][wid]["fields"]:
                        problems.append("角色槽位 %s 在 %s 上要指明一个该类型的字段槽位" % (slot, wid))
        for wid, t in d["types"].items():
            if wid not in terms["wi"]:
                problems.append("类型槽位 %s 缺中文名" % wid)
            for kind, known in (("fields", d["fields"]), ("roles", d["roles"]), ("states", terms.get("state", {}))):
                for slot in t.get(kind, []):
                    if slot not in known:
                        problems.append("%s 的 %s 槽位 %s 没有定义" % (wid, kind, slot))
        flow = d.get("flow") or {}
        arch, gate, freeze = flow.get("archive_node") or {}, flow.get("sign_gate") or {}, flow.get("freeze") or {}
        for wid, t in d["types"].items():
            if arch.get("role") not in t.get("roles", []) or arch.get("state") not in t.get("states", []):
                problems.append("%s 要有归档节点的角色槽位 %s 和状态槽位 %s" % (wid, arch.get("role"), arch.get("state")))
        g = d["types"].get(gate.get("type")) or {}
        if not g or any(gate.get(k) not in g.get("fields", []) for k in ("result", "task")) \
                or any(s not in g.get("states", []) for s in gate.get("states", [])):
            problems.append("签字关引用的类型、字段或状态槽位不在约定里")
        elif gate.get("approved") not in terms.get((d["fields"][gate["result"]].get("options") or {}).get("group"), {}):
            problems.append("签字关的批准选项在术语表里找不到")
        main = d["types"][mains[0]] if len(mains) == 1 else None
        if main and (freeze.get("sealed_by") not in self.signatures() or freeze.get("sealed_by") not in main["fields"]
                     or freeze.get("checked_before") not in main["fields"]):
            problems.append("冻结核对引用的字段槽位不在主线类型上，或者 sealed_by 不是签字字段")
        for wid in flow.get("rollback_needs_decision", []):
            if wid not in d["types"]:
                problems.append("rollback_needs_decision 里的 %s 不是类型槽位" % wid)
        for state in flow.get("closed", []):
            if state not in terms.get("state", {}):
                problems.append("closed 里的状态槽位 %s 没有定义" % state)
        if sorted(d["types"]) != sorted(w["id"] for w in self.spec.work_items()):
            problems.append("类型槽位和规则文件 feishu.work_items 的类型对不上")
        return ["约定文件：" + x for x in problems]


# ------------------------------------------------------------------ lint
HEADER = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")
HEADER_KEYS = ["包类型", "发出方", "接收方", "关联工作项", "需要接收方做的事", "回复期限", "上下文范围"]
ADDED_ON_UPLOAD = {"状态包", "交接记录"}


def template_path(name):
    """Templates live in the plugin's packages skill, or next to a deployed copy."""
    for base in (os.path.join(PLUGIN_ROOT, "skills", "packages", "assets"), os.path.join(PLUGIN_ROOT, "templates")):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def template_block(text):
    """Return the first ```markdown block of a template file."""
    m = re.search(r"```markdown\n(.*?)\n```", text, re.S)
    return m.group(1) if m else ""


def headings(text, level=2):
    mark = "#" * level + " "
    return [line[len(mark):].strip() for line in text.splitlines() if line.startswith(mark)]


def dropped_sections(template_text, relation):
    """Sections marked 不写 for this relation in the template's 栏目随关系变化 table."""
    m = re.search(r"## 栏目随关系变化\n(.*?)(?:\n## |\Z)", template_text, re.S)
    if not m or not relation:
        return set()
    rows = [r for r in m.group(1).splitlines() if r.startswith("|")]
    if len(rows) < 3:
        return set()
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    if relation not in head:
        return set()
    col = head.index(relation)
    out = set()
    for r in rows[2:]:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) > col and cells[col].startswith("不写"):
            out.add(cells[0])
    return out


def lint_text(spec, text):
    """Return (errors, warnings) for one package draft or document."""
    errors, warnings = [], []
    for old in spec.terms.get("retired", {}):
        if re.search(re.escape(old) + "(?![含括装办])", text):
            errors.append("用了旧包名“%s”，现在叫“%s”" % (old, spec.terms["retired"][old]))
    header = {}
    for line in text.splitlines():
        m = HEADER.match(line)
        if m and m.group(1) not in header:
            header[m.group(1)] = m.group(2).strip()
        if line.startswith("## "):
            break
    if "包类型" not in header:
        return errors, warnings
    names = {v: k for k, v in spec.terms["pkg"].items()}
    pid = names.get(header["包类型"].split("（")[0].strip())
    if not pid:
        errors.append("包类型“%s”不是现行的包：只有%s" % (header["包类型"], "、".join(spec.terms["pkg"].values())))
        return errors, warnings
    keys = list(HEADER_KEYS)
    if pid in ("request", "delivery"):
        keys.insert(1, "关系")
    if pid == "decision":
        keys.insert(1, "渠道")
    for k in keys:
        if k not in header:
            errors.append("包头缺“%s”" % k)
    relation = header.get("关系", "").strip()
    rel_names = set(spec.terms["rel"].values())
    if "关系" in keys and relation and relation not in rel_names:
        errors.append("关系“%s”要写%s之一" % (relation, "、".join(sorted(rel_names))))
    if pid == "decision" and header.get("渠道") not in (None, *spec.terms["channel"].values()):
        errors.append("渠道要写%s之一" % "、".join(spec.terms["channel"].values()))
    tpl = template_path(next(p["template"] for p in spec.rules["packages"] if p["id"] == pid))
    if not tpl:
        warnings.append("找不到%s的模板，跳过栏目检查" % spec.pkg(pid))
        return errors, warnings
    with open(tpl, encoding="utf-8") as f:
        tpl_text = f.read()
    want = [h for h in headings(template_block(tpl_text)) if h not in dropped_sections(tpl_text, relation)]
    have = headings(text)
    for h in want:
        if h not in have:
            errors.append("缺栏目“%s”；没有内容写“无”" % h)
    for h in have:
        if h not in want and h not in ADDED_ON_UPLOAD:
            if h in headings(template_block(tpl_text)):
                errors.append("栏目“%s”在%s关系下不写，整节删掉" % (h, relation))
            else:
                errors.append("模板里没有栏目“%s”，不临时加栏目" % h)
    if pid == "request" and relation in (spec.term("rel", "assign"), spec.term("rel", "cross")):
        m = re.search(r"\n## 方法\n(.*?)(?:\n## |\Z)", text, re.S)
        if m and m.group(1).strip() not in ("由接收方决定。", "由接收方决定"):
            errors.append("%s关系的方法只写“由接收方决定”" % relation)
    return errors, warnings


def lint_files(spec, paths):
    bad = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print("%s：读不到（%s）" % (path, e))
            bad += 1
            continue
        errors, warnings = lint_text(spec, text)
        for w in warnings:
            print("%s：提醒：%s" % (path, w))
        for e in errors:
            print("%s：%s" % (path, e))
        bad += bool(errors)
    print("检查了 %d 个文件，%s" % (len(paths), "%d 个有问题" % bad if bad else "没有问题"))
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    spec = OrgSpec()
    if not argv or argv[0] == "check":
        problems = spec.validate() + Contract(spec).validate()
        for p in problems:
            print("问题：" + p)
        print("规则文件检查：%s" % ("%d 个问题" % len(problems) if problems else "没有问题"))
        return 1 if problems else 0
    if argv[0] == "lint" and len(argv) > 1:
        return lint_files(spec, argv[1:])
    if argv[0] == "show" and len(argv) > 1:
        fn = {"ranks": spec.rank_table, "packages": spec.package_table, "relations": spec.relation_matrix,
              "routes": spec.package_routes, "routing": spec.routing_table, "work-items": spec.work_items_table,
              "task-rules": spec.task_rules, "docs-tree": spec.docs_tree,
              "handoff": spec.handoff_rules, "overview": spec.sequence_overview}.get(argv[1])
        if not fn:
            print("可以显示：ranks packages relations routes routing work-items task-rules docs-tree handoff overview；"
                  "流程和字段在飞书空间里，用 space.py show 看")
            return 2
        print(fn())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
