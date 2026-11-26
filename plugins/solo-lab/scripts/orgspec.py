#!/usr/bin/env python3
"""Read the organisation rules of the solo-lab plugin.

    python3.12 -I scripts/orgspec.py check            # validate rules.json, contract.json and irreversible.json
    python3.12 -I scripts/orgspec.py show packages    # print one rendered section
    python3.12 -I scripts/orgspec.py lint <file>...   # check package drafts and docs for stale names

orgspec/rules.json holds the rules with stable English identifiers; Chinese names
live only in orgspec/terms.zh.json. Prose in rules.json may contain tokens such as
{pkg:request} or {seq:alg}; render() replaces them with the current names.
orgspec/contract.json holds what the plugin and a Feishu Project template agree on: the
slots scripts handle by meaning (class Contract).
orgspec/irreversible.json is the list of irreversible actions: each category, what it covers
and the criteria it meets. Decision packages name its categories; decision_parts() reads what
a decision package says about them, for the package check and for ledger.py. No hook reads it,
and nothing in it says how a command would be recognized (1.0.6).
Python 3.9 standard library only.
"""
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool          # the names of sessions are read in one place (standard library only: the hooks load it too)

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGSPEC_DIR = os.path.join(PLUGIN_ROOT, "orgspec")
TOKEN = re.compile(r"\{(pkg|rel|wi|seq|rank|node|field|role|channel|abbrev|state|sign):([a-z0-9_]+)\}")
RANK_NUM = {"p0": 0, "p1": 1}
# The dispatch graph's groups of edges (rules.json dispatch). assign is the hierarchy, and the roles a role
# opens and hands work to are read from it alone (dispatch_targets); direct holds the edges beside it: a +1 to
# the +1 of another sequence (跨序列需求, 1.0.12: until then every such message went through the entry), and a +0
# that holds a 下发 request to another +0 of its sequence (the closed parts of that request).
# 1.1.0: a sequence has two ranks: its lead, +1, and its members, +0. The PMO's lead is the entry; its members are
# two fixed roles, the router and the scribe, and they are named in the graph: a rank there (p1, p0) stands for
# the sequences' own roles only.
DISPATCH_GROUPS = ("assign", "direct")
RANKS = ("p0", "p1")
LEAD, MEMBER = 1, 0
ENTRY, ROUTER, SCRIBE = "pmo-p1", "pmo-router", "pmo-scribe"


def edge_groups(rule):
    """The groups of edges a message kind travels along: one name or a list of them."""
    along = rule.get("along")
    return [along] if isinstance(along, str) else list(along or [])


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
    def __init__(self, rules_path=None, terms_path=None, irreversible_path=None):
        self.rules_path = rules_path or os.path.join(ORGSPEC_DIR, "rules.json")
        self.terms_path = terms_path or os.path.join(ORGSPEC_DIR, "terms.zh.json")
        self.irreversible_path = irreversible_path or os.path.join(ORGSPEC_DIR, "irreversible.json")
        self.rules = _load(self.rules_path)
        self.terms = _load(self.terms_path)
        self._irreversible = None                  # read when first needed: only decision packages use it

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
        """The agent name of a sequence and rank: <seq>-p1 for its lead, <seq>-p0 for its members. The PMO's +0
        here is its scribe; its router, a +0 too, goes by its own name (roles_of)."""
        if seq == "pmo" and rank == MEMBER:
            return SCRIBE
        return "%s-p%d" % (seq, rank)

    @staticmethod
    def roles_of(seq):
        """[(role, rank)] of a sequence, its lead first; the PMO's are the entry, the router and the scribe."""
        if seq == "pmo":
            return [(ENTRY, LEAD), (ROUTER, MEMBER), (SCRIBE, MEMBER)]
        return [("%s-p%d" % (seq, LEAD), LEAD), ("%s-p%d" % (seq, MEMBER), MEMBER)]

    def ranks_of(self, seq):
        """The ranks a sequence has, highest first: [1, 0]."""
        s = next(x for x in self.rules["sequences"] if x["id"] == seq)
        return sorted((RANK_NUM[r] for r in s["ranks"]), reverse=True)

    def _edge_targets(self, role, seq, rank, edges, seqs):
        out = []
        own = role == "%s-p%d" % (seq, rank)          # the router and the scribe go by their names, not by a rank
        for e in edges:
            src = e["from"]
            if src != role and not (own and src == "p%d" % rank):
                continue
            to, scope = e["to"], e.get("scope")
            if to in RANK_NUM:
                r = RANK_NUM[to]
                for s in seqs:
                    if (scope == "same_sequence" and s != seq) or (scope == "other_sequences" and s == seq):
                        continue
                    name = self.role_name(s, r)
                    if name == "%s-p%d" % (s, r) and name not in out:
                        out.append(name)
            elif to not in out:
                out.append(to)
        return out

    def dispatch_targets(self, role, seq, rank, seqs):
        """Whom a role dispatches work to: the assign edges of the dispatch graph in rules.json."""
        return self._edge_targets(role, seq, rank, self.rules["dispatch"]["assign"], seqs)

    def dispatch_table(self):
        """The dispatch graph as text for org/org.md: who assigns work to whom, and the message kinds."""
        d = self.rules["dispatch"]
        rank_cn = {"p0": "+0", "p1": "+1"}

        def end(x):
            if x in rank_cn:
                return "各序列 " + rank_cn[x]
            seq, _, rank = x.rpartition("-")
            names = {"pmo-router": "项目管理 +0（路由）", "pmo-scribe": "项目管理 +0（书记员）"}
            return names.get(x) or "%s %s" % (self.term("seq", seq), rank_cn.get(rank, rank))
        scope = {"same_sequence": "同序列", "other_sequences": "别的序列", None: ""}
        rows = [[end(e["from"]), end(e["to"]), scope[e.get("scope")] + ("（%s）" % e["note"] if e.get("note") else "")]
                for group in DISPATCH_GROUPS for e in d.get(group, [])]
        kinds = []
        for kind, rule in d["kinds"].items():
            if rule.get("how"):
                how = rule["how"]
            elif rule.get("along"):
                how = "沿委托的边" + ("反向（交付给委托方）" if rule.get("reverse") else "")
            elif rule.get("to"):
                how = "任何会话发给%s" % end(rule["to"])
            elif rule.get("from"):
                how = "%s发给任何会话" % end(rule["from"])
            else:
                how = "不限"
            kinds.append([self.term("msg", kind), how])
        return "\n".join([md_table(["委托方", "承接方", "范围"], rows), "",
                          md_table(["消息", "方向"], kinds), "",
                          d.get("note", "")])

    def message_header_re(self):
        """The header that starts every message between pool sessions: 【委托 #2101】Alg-p1 → Alg-p0-01."""
        names = sorted(self.terms["msg"].values(), key=len, reverse=True)
        name = SESSION_IN_TEXT
        return re.compile(r"【(%s)(?:\s*#(\d{3,}(?:-\d{1,2}(?![\d-]))?))?[^】]*】\s*(?:(%s)\s*(?:→|->)\s*(%s))?" % (
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
        if sorted(s["ranks"]) != list(RANKS) or (sid == "pmo") != ("router" in s):
            raise OrgSpecError("%s 序列的职级分工不是 %s" % (cn, "入口、路由、书记员三行" if sid == "pmo" else "+0、+1 两行"))
        rows = {role: ranks[rank] for role, rank in self.roles_of(sid)}
        if sid == "pmo":                        # the router's row stands beside the two ranks
            row = s["router"]
            rows[ROUTER] = {"label": row["label"], "given": self.render(row["given"]),
                            "outputs": self.render(row["outputs"]), "escalate": self.render(row["escalate"])}
        return {"id": sid, "cn": cn, "abbrev": s["abbrev"], "title": s["title"],
                "heading": "序列设置 - %s：%s" % (cn, s["title"]), "focus": self.render(s["focus"]),
                "intro": self.render(s["intro"]), "ranks": ranks, "roles": rows, "notes": self.render(s.get("notes") or ""),
                "plan": self.render(s.get("plan") or "")}

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
        lines += ["- " + self.render(t["brief"]) + "。",
                  "- 文档由写它的会话自己上传；归档时的移动由%s做。" % self.term("role", "scribe"),
                  "- " + t["archive"] + "。", "- " + self.render(t["granularity"]) + "。"]
        return "\n".join(lines)

    # --------------------------------------------------------- irreversible
    def irreversible(self):
        """orgspec/irreversible.json: the criteria and the categories of irreversible actions."""
        if self._irreversible is None:
            self._irreversible = _load(self.irreversible_path)
        return self._irreversible

    def irreversible_categories(self):
        return list(self.irreversible().get("categories") or [])

    def irreversible_names(self):
        return [c["name"] for c in self.irreversible_categories()]

    def irreversible_category(self, name):
        """The category a package names by its Chinese name (特权应用); None for anything else."""
        name = (name or "").strip()
        return next((c for c in self.irreversible_categories() if c.get("name") == name), None)

    def criteria_heads(self):
        """The criteria as short phrases: 1 花钱或占用额度，2 改权限、身份或机器的系统状态，..."""
        crit = self.irreversible().get("criteria") or {}
        return "，".join("%s %s" % (k, v.split("：")[0]) for k, v in sorted(crit.items()))

    # No hook checks the list (1.0.6): the rule stands on its own.
    IRREVERSIBLE_RULE = ("清单里的事要先有一条研究员批准、所选选项里列了它的决策再做；决策待定期间，包里“冻结的动作”谁都不做。"
                         "hook 不拦这些命令，也不按待定决策冻结，命令的权限照会话自己的权限检查。上面两条规矩不变，靠各会话照规则办和包检查："
                         "选项里列了清单里类别的决策包必须写不可逆，到期只能等研究员。")

    def irreversible_table(self):
        """The list as a table: 类别、包括、判据."""
        rows = []
        for c in self.irreversible_categories():
            crit = "、".join(str(n) for n in c["criteria"]) + ("（%s）" % c["criteria_note"] if c.get("criteria_note") else "")
            rows.append([c["name"], c["covers"], crit])
        return md_table(["类别", "包括", "判据"], rows)

    def irreversible_section(self):
        """The body of org/org.md's section on irreversible actions: the criteria, the list and the rule
        that goes with it."""
        irr = self.irreversible()
        lines = ["一件事可逆不可逆，由决策包的作者按下面的判据判断：满足任一条的算不可逆，到期只能等研究员；都不满足的算可逆，"
                 "到期按决策包写的到期默认动作办。判据下面的清单是必须写不可逆的下限，清单以外的事由作者按判据判断。", ""]
        lines += ["%s. %s。" % (k, v) for k, v in sorted((irr.get("criteria") or {}).items())]
        lines += ["", self.irreversible_table(), "", self.IRREVERSIBLE_RULE]
        return "\n".join(lines)

    def validate_irreversible(self):
        """Problems in orgspec/irreversible.json, each prefixed with 不可逆事项清单."""
        try:
            irr = self.irreversible()
        except OrgSpecError as e:
            return [str(e)]
        problems = []
        crit = irr.get("criteria") or {}
        if sorted(crit) != [str(n) for n in range(1, len(crit) + 1)] or not crit:
            problems.append("criteria 要是从 1 起连续编号的判据")
        cats = irr.get("categories")
        if not isinstance(cats, list) or not cats:
            return ["不可逆事项清单：没有类别"]
        ids, names = set(), set()
        for c in cats:
            cid, name = c.get("id"), c.get("name")
            where = name or cid or "?"
            if not isinstance(cid, str) or not re.fullmatch(r"[a-z][a-z_]*", cid) or cid in ids:
                problems.append("%s：id 要是不重复的小写英文标识" % where)
            if not isinstance(name, str) or not name.strip() or re.search(r"[：:；;]", name) or name in names:
                problems.append("%s：name 要是不重复的中文名，不带冒号和分号（包里用它们隔开类别和限定）" % where)
            ids.add(cid)
            names.add(name)
            if not isinstance(c.get("covers"), str) or not c["covers"].strip():
                problems.append("%s：缺 covers" % where)
            if not c.get("criteria") or any(str(n) not in crit for n in c.get("criteria") or []):
                problems.append("%s：criteria 要写 criteria 里有的判据号" % where)
        return ["不可逆事项清单：" + p for p in problems]

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
        if [r["id"] for r in self.rules["ranks"]] != list(RANKS):
            problems.append("ranks 要依次是 p0、p1")
        for s in self.rules["sequences"]:
            if s["id"] not in self.terms["seq"] or s["id"] not in self.terms["abbrev"]:
                problems.append("序列 %s 缺中文名或简写" % s["id"])
            if self.terms["abbrev"].get(s["id"]) != s["abbrev"]:
                problems.append("序列 %s 的简写和术语表不一致" % s["id"])
            if sorted(s["ranks"]) != list(RANKS) or (s["id"] == "pmo") != ("router" in s):
                problems.append("序列 %s 的职级分工要是 p0、p1%s" % (s["id"], "，另有 router 一行" if s["id"] == "pmo" else ""))
            if s["id"] != "pmo" and not (s.get("plan") or "").strip():
                problems.append("序列 %s 缺方案的填写要点（plan）" % s["id"])
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
        roles = {role for x in self.rules["sequences"] for role, _ in self.roles_of(x["id"])}
        ends = roles | set(RANKS)
        for group in DISPATCH_GROUPS:
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
            for group in edge_groups(rule):
                if group not in d:
                    problems.append("dispatch.kinds.%s 沿用的 %s 不存在" % (kind, group))
        if not d.get("kinds"):
            problems.append("规则文件缺 dispatch（派发图）")
        return problems + self.validate_irreversible()


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

    @property
    def template(self):
        """The reference template a project space is made from: {name, version, url}."""
        return self.data.get("template") or {}

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

    def names(self, kind, slot):
        """Every name binding by name accepts for a slot: the reference template's. Only the latest template is
        supported (the researcher, 2026-11-23); a space made from an older one is changed or made again."""
        return [self.name(kind, slot)]

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
        plan = flow.get("plan_gate") or {}
        held = d["types"].get(plan.get("type")) or {}
        if plan and (plan.get("field") not in held.get("fields", []) or plan.get("role") not in held.get("roles", [])
                     or (d["fields"].get(plan.get("field")) or {}).get("type") != "link"):
            problems.append("方案关引用的类型、链接字段或角色槽位不在约定里")
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
# Every package says which version it is: 第 1 版（2026-11-16） when new; for a request or a decision
# `docs.py revise` takes the next one, the previous number plus one, and replaces the opening of the main
# document with it; a delivery is handed in again with reply, its number plus one.
VERSION = "版本"
HEADER_KEYS = ["包类型", VERSION, "发出方", "接收方", "关联工作项", "需要接收方做的事", "回复期限", "上下文范围"]
# A decision package's header also says who carries it out once it is decided (执行方: 研究员, or the
# session's name, mostly the sender's) and what the researcher must do by hand (研究员要做的事: 整理数据、
# 挂 API、开 Docker ..., 无 when nothing). The two agree: 研究员 has something to do, a session has 无.
# Required on decision packages only. The registration receipt keeps both; `ledger.py query queue` lists
# a decided decision until it is archived while the researcher carries it out.
# The session is one that answers for the matter (the researcher, 2026-11-16): a sequence's +1, the entry or
# the router (EXECUTOR_RANKS); a +0 and the scribe are sent back, and what a lower rank is to do the +1
# delegates with a request package. When one option needs the researcher's own hands the header says 研究员;
# should the researcher pick an option that does not, `workitem.py confirm --executor` hands the decision to a
# session, under the same check.
EXECUTOR = "执行方"
RESEARCHER = "研究员"
RESEARCHER_ACTION = "研究员要做的事"
NOTHING = "无"
EXECUTOR_RANKS = "研究员、各序列的 +1、入口和路由"
WHO_EXECUTES = ("写“研究员”，或者执行这条决策的那个会话的名字：各序列的 +1、入口或路由（多半是发出方的会话名），"
                "只写名字、不加说明，比如 Alg-p1")
WHAT_RESEARCHER_DOES = "写这条决策定下以后要研究员亲手做的事，没有写“%s”" % NOTHING
# How 研究员要做的事 is filled when the researcher carries the decision out: never 无 alone, which the
# check would send back as disagreeing with 执行方.
RESEARCHER_DOES = ("执行方是研究员，写清楚要研究员亲手做什么；不用他亲手做什么的，执行方改写执行这条决策的会话名，"
                   "“%s”写“%s”" % (RESEARCHER_ACTION, NOTHING))
# One session's name alone (Alg-p1), or written like 发出方: 算法 +1（Alg-p1）, the sequence and rank
# in front being the session's own. The name is ASCII only, so that a remark run on after it is no part of it.
SESSION_IN_TEXT = pool.NAME_IN_TEXT
EXECUTOR_SESSION = re.compile(r"(%s)|([^\s（()）<>、，,；;]+)\s*\+([0-2])\s*[（(]\s*(%s)\s*[）)]" % (SESSION_IN_TEXT, SESSION_IN_TEXT))
# 无 alone or followed by punctuation or a remark (无。 无，发出方自己办 无（…）) says nothing; 无需…, 无论… do not
# start with the word 无.
SAYS_NOTHING = re.compile(r"无(?:$|[\s（(，,。.；;：:、])")
# Other ways of writing nothing (没有, 不需要, 无需研究员动手, 暂无, N/A) are sent back to be written 无: the receipt
# would keep them as something the researcher must do.
OTHER_NOTHING = re.compile(r"(?i)(?:没有|不需要|不用|无需|暂无|n/?a\b|none\b|[-—/／]+$)")
ADDED_ON_UPLOAD = {"状态包", "交接记录"}
# A decision package also says how it ends when the researcher does not decide (the researcher, 2026-11-16).
# 可逆性: 可逆, or 不可逆 with the criteria it meets (不可逆（判据 2）). 到期默认动作: the option a reversible
# decision takes once it has waited the expiry time; 等研究员 for an irreversible one and for one the
# researcher carries out (or has something to do by hand); 无 for one the researcher decided in a talk,
# which never waits. A decision one of whose options lists a category of the list of irreversible actions,
# and a decision that signs, is irreversible; beyond the list its author judges by the criteria. A decision's
# 回复期限 is the sender's wish only: nothing happens when it passes (a request's is its deadline, written
# into 截止时间).
# The options table's last column, 不可逆的动作, says what of the list an option does: its categories, each
# with a note if wanted (特权应用：启动 Docker Desktop), 无 when none. 冻结的动作 lists what nobody does while
# the decision waits, a line each, a category or a sentence. Neither is matched against commands by any
# hook (1.0.6: the column was 要放行的不可逆动作 until then, and what it listed was released to a hook), so a
# note is free text; the rules, the line every pool session is shown each turn and this check keep them.
REVERSIBILITY = "可逆性"
DEFAULT_ACTION = "到期默认动作"
WAIT = "等研究员"
IRREVERSIBLE_COLUMN = "不可逆的动作"
FORMER_COLUMN = "要放行的不可逆动作"          # the column's name in 1.0.4 and 1.0.5
OPTIONS = "选项"
FROZEN = "冻结的动作"
SIGNED = "待签版本"
# Sections a decision package has to fill: 无 when there is nothing, never left empty.
NEW_SECTIONS = {
    "没有列入的选项": "写已知但没列进选项表的做法，每个写不列的原因",
    "约束的出处": "写限定选项的约束出自哪份文件、谁定的、研究员确认过没有",
    "已发生的偏向性工作": "写已经做了、偏向某个选项的工作和投入",
    FROZEN: "写决策待定期间谁都不做的动作，一条一行：不可逆事项清单里的类别（可以带一句说明），或者一句话描述",
}
# 结论清单 (the researcher, 2026-11-21). A decision package carries what was settled before it as context: the
# section 已有的决定和结论, written by `ledger.py settled … --into <草稿>` and never by hand. It opens with a line
# 范围：… that says what was looked at, lists every settled decision and concluded research task of that range
# (- #ID …: the index, for the reader to see what there is) and gives a card (### #ID …) for each one its author
# picked, cut from the work items and their documents as they stand. Nothing judges what a settled decision
# means for this one: the author says so in 建议, and cites there only what has a card. The header line 取代的决策
# (optional) names the decisions this one takes the place of; each needs its card. A card repeats the 认知层 and
# 背景 of the package it is about, so 背景 has a length it may not pass.
SETTLED = "已有的决定和结论"
SUPERSEDES = "取代的决策"
SETTLED_SCOPE = "范围："
INDEX_LINE = re.compile(r"^- #(\d+)\s", re.M)
CARD_HEAD = re.compile(r"^### #(\d+)\s", re.M)
BACKGROUND, BACKGROUND_MAX = "背景", 600
SETTLED_HOW = "运行 ledger.py settled <范围> --cards <挑出来的 ID> --into <草稿>，由脚本照工作项和主文档的原文写进来"
VERSION_VALUE = re.compile(r"第\s*(\d+)\s*版\s*[（(]\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*[）)]")
DATE_TEXT = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
CRITERIA_TEXT = re.compile(r"判据\s*([0-9](?:\s*[、,，/和及与]\s*[0-9])*)")
SAYS_WAIT = re.compile(r"等研究员(?:$|[\s（(，,。.；;：:、])")
SAYS_REVERSIBLE = re.compile(r"可逆(?:$|[\s（(，,。.；;：:])")
VERSION_HOW = "写“第 1 版（YYYY-MM-DD）”；已经登记的包要改，写上一版加 1，自己用 docs.py revise 换主文档"
VERSION_HOW_DELIVERY = "写“第 1 版（YYYY-MM-DD）”；交付包改了，版本号加 1，用 docs.py upload 重新交"
DEFAULT_HOW = ("可逆的写到期按哪个选项办（选项表第一列的名字）；不可逆的、执行方是研究员或者研究员要亲手做事的写“%s”；"
               "研究员主动发起的写“无”" % WAIT)


def real_date(y, m, d):
    try:
        datetime.date(int(y), int(m), int(d))
        return True
    except ValueError:
        return False


def read_version(value):
    """(number, date, None) of a package's 版本 line, 第 2 版（2026-11-16）; (None, None, how to write it)
    when it is written otherwise or names a date that does not exist."""
    m = VERSION_VALUE.fullmatch((value or "").strip())
    if not m:
        return None, None, "写成“第 N 版（YYYY-MM-DD）”：新写的包是第 1 版，修订稿写上一版加 1"
    if not real_date(*m.group(2, 3, 4)):
        return None, None, "日期 %s-%s-%s 不存在" % m.group(2, 3, 4)
    if int(m.group(1)) < 1:
        return None, None, "版本从第 1 版算起"
    return int(m.group(1)), "%04d-%02d-%02d" % tuple(int(x) for x in m.group(2, 3, 4)), None


def version_of(value):
    """The number in a 版本 line; None when the line is missing or written otherwise."""
    return read_version(value)[0]


def read_reversibility(value):
    """(reversible, criteria, why) of a decision package's 可逆性 line: 可逆 -> (True, [], None);
    不可逆（判据 2、3） -> (False, [2, 3], None); anything else -> (None, [], how to write it)."""
    value = (value or "").strip()
    if value.startswith("不可逆"):
        m = CRITERIA_TEXT.search(value)
        if not m:
            return None, [], "不可逆的要写判据号，比如“不可逆（判据 2）”"
        return False, sorted({int(x) for x in re.findall(r"[0-9]", m.group(1))}), None
    if SAYS_REVERSIBLE.match(value):
        return True, [], None
    return None, [], "写“可逆”，或者“不可逆（判据 N）”"


def section_of(text, name):
    """The text of a "## name" section, stripped; None when the document has no such section."""
    m = re.search(r"\n## %s[ \t]*\n(.*?)(?=\n## |\Z)" % re.escape(name), "\n" + text, re.S)
    return m.group(1).strip() if m else None


def empty_text(text):
    """A section or cell with nothing in it: empty, or the template's placeholder left as it was."""
    text = (text or "").strip()
    return not text or placeholder(text)


def first_table(text):
    """(header cells, data rows) of the first markdown table in text; None when it has none."""
    rows, started = [], False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip("|").split("|")])
            started = True
        elif started:
            break
    if not rows:
        return None
    return rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]


def read_category(spec, text):
    """({category, name, qualifier, text}, None) when text opens with a category of the list: the category alone
    (Docker 资源), with a note after a colon (特权应用：启动 Docker Desktop) or in brackets (花钱（约 8 美元））;
    (None, why) otherwise. The qualifier is the note as written, all of it: nothing matches it against commands,
    so it is cut nowhere and may be any text."""
    text = (text or "").strip()
    for name in sorted(spec.irreversible_names(), key=len, reverse=True):
        rest = text[len(name):].lstrip() if text.startswith(name) else None
        if rest is None or rest and rest[0] not in "：:（(":
            continue
        cat = spec.irreversible_category(name)
        return {"category": cat["id"], "name": name, "qualifier": rest.lstrip("：:").strip(), "text": text}, None
    return None, "不在不可逆事项清单里：清单里的类别是%s；先写类别，说明写在“：”后面，几类之间用“；”隔开" % "、".join(spec.irreversible_names())


def read_actions(spec, cell, option):
    """(actions, errors) of one cell of the options table's 不可逆的动作 column: the categories of the list the
    option does, separated by ；, each with a note if wanted; 无 when it does none. Every part has to open with
    a category: a part that does not is sent back, so that no category goes unread."""
    if empty_text(cell):
        return [], ["选项“%s”的“%s”是空的：写这个选项要做的、不可逆事项清单里的事，先写类别，可以带一句说明，"
                    "比如“特权应用：启动 Docker Desktop”；没有写“%s”" % (option, IRREVERSIBLE_COLUMN, NOTHING)]
    if SAYS_NOTHING.match(cell):
        return [], []
    actions, errors = [], []
    for part in [p.strip() for p in re.split(r"[；;]", cell) if p.strip()]:
        item, why = read_category(spec, part)
        if item:
            actions.append(item)
        else:
            errors.append("选项“%s”的%s“%s”%s" % (option, IRREVERSIBLE_COLUMN, part, why))
    return actions, errors


def read_options(spec, section):
    """(options, errors) of a decision package's 选项 section: options is [{name, approach, irreversible}] from
    its table, or None when the section has no table (研究员主动发起 may write 无)."""
    table = first_table(section)
    if table is None:
        if empty_text(section) or SAYS_NOTHING.match(section.strip()):
            return None, []
        return None, ["“%s”写成选项表，不写列表或一段话：表头照模板，第一列是选项名，书记员确认时照它记 --option；只有研究员主动发起、谈话里没比较过做法的才写“%s”" % (OPTIONS, NOTHING)]
    head, rows = table
    if IRREVERSIBLE_COLUMN not in head:
        if FORMER_COLUMN in head:
            return [], ["选项表的“%s”一列改名“%s”了（1.0.6）：把表头改过来；格子里照旧写这个选项要做的、不可逆事项清单里的类别，"
                        "可以带一句说明，没有写“%s”" % (FORMER_COLUMN, IRREVERSIBLE_COLUMN, NOTHING)]
        return [], ["选项表少了“%s”一列：写这个选项要做的、不可逆事项清单里的事，先写类别，可以带一句说明，"
                    "比如“特权应用：启动 Docker Desktop”；没有写“%s”" % (IRREVERSIBLE_COLUMN, NOTHING)]
    col, how = head.index(IRREVERSIBLE_COLUMN), head.index("做法") if "做法" in head else None
    options, errors = [], []
    for r in rows:
        name = r[0] if r else ""
        if not name:
            errors.append("选项表有一行没写选项名（第一列）")
            continue
        if any(o["name"] == name for o in options):
            errors.append("选项表里“%s”出现了两次：一个选项写一行，名字不重复" % name)
            continue
        actions, more = read_actions(spec, r[col] if col < len(r) else "", name)
        errors += more
        options.append({"name": name, "approach": r[how] if how is not None and how < len(r) else "", "irreversible": actions})
    return options, errors


def option_actions(option):
    """What of the list an option does, as its registration kept it: irreversible; releases in a receipt or a
    local decision state written by 1.0.4 or 1.0.5."""
    return option.get("irreversible") or option.get("releases") or []


def read_frozen(spec, section):
    """The items of a decision package's 冻结的动作, a line each: a line that opens with a category of the list
    is that category ({kind: category, category, name, qualifier, text}), and one that names several, separated
    by ；, is an item for each of them; a remark after a category stays with it. Any other line is a sentence
    ({kind: text, text}). Either way the whole line reaches the line every pool session is shown each turn;
    nothing here is matched against commands, so nothing is sent back."""
    lines = [re.sub(r"^\s*(?:[-*+]|\d+[.、)）])\s*", "", line).strip() for line in (section or "").splitlines()]
    lines = [t for t in lines if t]
    if empty_text(section) or len(lines) == 1 and SAYS_NOTHING.match(lines[0]):
        return []
    items = []
    for text in lines:
        found = []
        for part in [p.strip() for p in re.split(r"[；;]", text) if p.strip()]:
            item, _ = read_category(spec, part)
            if item:
                found.append(dict(item, kind="category"))
            elif found:                                   # a remark after a category
                last = found[-1]
                last["qualifier"] = "；".join(x for x in (last["qualifier"], part) if x)
                last["text"] = "%s；%s" % (last["text"], part)
            else:                                         # a sentence, kept whole
                found = [{"kind": "text", "text": text}]
                break
        items += found
    return items


def match_option(value, options):
    """The options a 到期默认动作 line names: the names written in it, a name inside a longer one left out."""
    hits = [o["name"] for o in options if o["name"] and o["name"] in value]
    return [n for n in hits if not any(n != m and n in m for m in hits)]


def decision_parts(spec, text, header=None):
    """What a decision package says about how it may end, read: (parts, errors, warnings).

    parts: version; reversible (True, False or None when unreadable) and criteria; default (the 到期默认动作
    line as written) and default_option (the option it names, for a reversible one); options [{name,
    approach, irreversible}] (None without a table); frozen [items of read_frozen]; signing (待签版本 names what
    is signed). errors: what the package check sends back. A missing line or section is left to the header
    and section checks; a rule that needs a line that is wrong waits until that line is right. The list of
    irreversible actions is the floor: a decision one of whose options lists a category of it, and one that
    signs, has to say 不可逆; whether anything beyond the list is reversible is its author's judgement by the
    criteria. warnings: none at present."""
    header = header if header is not None else {}
    errors, warnings = [], []
    channel = (header.get("渠道") or "").strip()
    queue, talk = channel == spec.term("channel", "queue"), channel == spec.term("channel", "researcher")
    crit = spec.irreversible().get("criteria") or {}
    reversible, criteria = None, []
    raw = header.get(REVERSIBILITY)
    how = "写“可逆”，或者“不可逆（判据 N）”，判据：%s" % spec.criteria_heads()
    if raw is not None:
        if raw == "":
            errors.append("包头“%s”没写内容：%s" % (REVERSIBILITY, how))
        elif placeholder(raw):
            errors.append("包头“%s”还是模板里的占位：%s" % (REVERSIBILITY, how))
        else:
            reversible, criteria, why = read_reversibility(raw)
            unknown = [str(n) for n in criteria if str(n) not in crit]
            if why or unknown:
                errors.append("包头“%s”写的是“%s”：%s" % (REVERSIBILITY, raw, why or "没有判据 %s；%s" % ("、".join(unknown), how)))
                reversible, criteria = None, []
    section = section_of(text, OPTIONS)
    options, option_errors = read_options(spec, section) if section is not None else (None, [])
    errors += option_errors
    if queue and section is not None and not option_errors and len(options or []) < 2:
        errors.append("决策队列的决策包，选项写成表，至少两个选项，其中一个可以是“不做”")
    for name, what in NEW_SECTIONS.items():
        sec = section_of(text, name)
        if sec is not None and empty_text(sec):
            errors.append("栏目“%s”是空的：%s；没有写“无”" % (name, what))
    frozen = read_frozen(spec, section_of(text, FROZEN) or "")
    signed = section_of(text, SIGNED)
    signing = signed is not None and not empty_text(signed) and not SAYS_NOTHING.match(signed)
    listing = [o for o in options or [] if o["irreversible"]]
    if reversible is True and listing:
        errors.append("选项%s列了不可逆事项清单里的类别，包头“%s”要写不可逆，并写判据号" % (
            "、".join("“%s”（%s）" % (o["name"], "；".join(r["text"] for r in o["irreversible"])) for o in listing), REVERSIBILITY))
    if reversible is True and signing:
        errors.append("这条决策用来签字（“%s”写了要签的版本），包头“%s”要写不可逆：签字关不走到期默认" % (SIGNED, REVERSIBILITY))
    default, default_option = header.get(DEFAULT_ACTION), None
    if default == "":
        errors.append("包头“%s”没写内容：%s" % (DEFAULT_ACTION, DEFAULT_HOW))
    elif default is not None and placeholder(default):
        errors.append("包头“%s”还是模板里的占位：%s" % (DEFAULT_ACTION, DEFAULT_HOW))
    elif default is not None and (queue or talk):
        who = read_executor(spec, header.get(EXECUTOR))[0]
        action = header.get(RESEARCHER_ACTION)
        known = action is not None and action != "" and not placeholder(action) and not OTHER_NOTHING.match(action)
        nothing_to_do = known and bool(SAYS_NOTHING.match(action))
        hands = who == RESEARCHER or known and not nothing_to_do
        wait, nothing = bool(SAYS_WAIT.match(default)), bool(SAYS_NOTHING.match(default))
        if talk:
            if not nothing:
                errors.append("研究员主动发起的决策已经定了，不会到期：包头“%s”写“无”" % DEFAULT_ACTION)
        elif nothing:
            errors.append("决策队列的决策包，“%s”不写“无”：%s" % (DEFAULT_ACTION, DEFAULT_HOW))
        elif wait:
            if reversible is True and who and who != RESEARCHER and nothing_to_do:
                errors.append("可逆的决策到期按包头的到期默认动作办：包头“%s”写选项表里的一个选项%s，不写“%s”" % (
                    DEFAULT_ACTION, "（%s）" % "、".join(o["name"] for o in options) if options else "", WAIT))
        elif reversible is False:
            errors.append("可逆性是不可逆，包头“%s”就要写“%s”：不可逆的决策到期只能等研究员" % (DEFAULT_ACTION, WAIT))
        elif hands:
            errors.append("%s，包头“%s”就要写“%s”：到期只能等研究员，不走到期默认" % (
                "执行方是研究员" if who == RESEARCHER else "研究员要亲手做事", DEFAULT_ACTION, WAIT))
        elif options and not option_errors:
            hits = match_option(default, options)
            if len(hits) == 1:
                default_option = hits[0]
            else:
                errors.append("包头“%s”写的“%s”%s：写选项表第一列的名字之一（%s）" % (
                    DEFAULT_ACTION, default, "写了不止一个选项" if hits else "不是选项表里的选项",
                    "、".join(o["name"] for o in options)))
    parts = {"version": version_of(header.get(VERSION)), "reversible": reversible, "criteria": criteria,
             "default": default, "default_option": default_option, "options": options, "frozen": frozen,
             "signing": signing}
    return parts, errors, warnings


def read_superseded(value):
    """([IDs], None) of a decision package's 取代的决策 line: #2003, or several (#2003、#2010); [] when the line
    is missing or says 无. (None, how to write it) for anything else."""
    value = (value or "").strip().rstrip("。.")
    if not value or SAYS_NOTHING.match(value):
        return [], None
    ids = re.findall(r"#(\d+)", value)
    if not ids or re.sub(r"#\d+|[\s、,，和]", "", value):
        return None, "写被取代的决策的工作项 ID，比如 #2003，几条用“、”隔开；没有写“无”"
    return list(dict.fromkeys(ids)), None


def settled_errors(text, header):
    """What is wrong with a decision package's 已有的决定和结论 section, its 取代的决策 line and the length of its
    背景. A missing section is left to the section check."""
    errors = []
    raw = header.get(SUPERSEDES)
    old, why = read_superseded(raw)
    if raw is not None and placeholder(raw.strip()):
        errors.append("包头“%s”还是模板里的占位：写这条决策定下以后不再算数的旧决策的工作项 ID，没有写“无”" % SUPERSEDES)
        old = []
    elif why:
        errors.append("包头“%s”写的是“%s”：%s" % (SUPERSEDES, raw, why))
        old = []
    background = section_of(text, BACKGROUND)
    if background and not placeholder(background) and len(background) > BACKGROUND_MAX:
        errors.append("“%s”有 %d 字，超过 %d 字：只留做决定需要的三到五句，细节放进“事实依据”；"
                      "以后别的决策包引这条决定时，这一段会原样摘过去" % (BACKGROUND, len(background), BACKGROUND_MAX))
    sec = section_of(text, SETTLED)
    if sec is None:
        return errors
    if empty_text(sec):
        errors.append("栏目“%s”是空的：%s；范围里一条都没有的，脚本会写“无”" % (SETTLED, SETTLED_HOW))
        return errors
    if not SAYS_NOTHING.match(sec) and not sec.startswith(SETTLED_SCOPE):
        errors.append("栏目“%s”不手写：%s" % (SETTLED, SETTLED_HOW))
        return errors
    listed, cards = set(INDEX_LINE.findall(sec)), set(CARD_HEAD.findall(sec))
    cited = [i for i in dict.fromkeys(re.findall(r"#(\d+)", section_of(text, "建议") or "")) if i in listed and i not in cards]
    if cited:
        errors.append("“建议”引了 %s，“%s”里只有清单里的一行，没有卡片：清单只用来看有哪些，不当依据；"
                      "重新运行 ledger.py settled，--cards 里加上它" % ("、".join("#" + i for i in cited), SETTLED))
    missing = [i for i in old if i not in cards]
    if missing:
        errors.append("包头“%s”写了 %s，“%s”里要有它的卡片，研究员才看得到被取代的是什么：重新运行 ledger.py settled，"
                      "--cards 里加上它" % (SUPERSEDES, "、".join("#" + i for i in missing), SETTLED))
    return errors


def read_executor(spec, value):
    """(who, None) when a decision package's 执行方 line names 研究员 or a session that may carry a decision
    out, else (None, how to write it). A session's abbreviation names a sequence, case aside as the ledger
    compares names (the name comes back spelled with the sequence's abbreviation), and a sequence and rank
    written in front of it (算法 +1（Alg-p1）) are its own. The session is a sequence's +1, the entry
    (项目管理 +1) or the router: a +0 and the scribe are sent back with what to write
    instead. `workitem.py confirm --executor` takes a session under the same check. A receipt that names
    another session from before 1.0.6 is read as it is: nothing here looks at receipts."""
    value = (value or "").strip().rstrip("。.")
    if value == RESEARCHER:
        return RESEARCHER, None
    m = EXECUTOR_SESSION.fullmatch(value)
    if not m:
        return None, WHO_EXECUTES
    head, rank, rest = pool.parse_name(m.group(1) or m.group(4))       # a name from before 1.1.0 is read as it is called now
    seq = next(((cn, abbrev, sid) for sid, cn, abbrev in spec.sequences() if abbrev.lower() == head.lower()), None)
    if not seq:
        return None, "序列的简写只有 %s，没有 %s" % ("、".join(abbrev for _, _, abbrev in spec.sequences()), head)
    name = "%s-p%d%s" % (seq[1], rank, "" if rest is None else "-" + rest)
    if m.group(4) and (m.group(2), m.group(3)) != (seq[0], str(rank)):
        return None, "%s 是%s +%s 的会话，前面写的序列和职级要和它一致，或者只写会话名" % (name, seq[0], rank)
    router, scribe = seq[2] == "pmo" and rest == "router", seq[2] == "pmo" and rest == "ledger"
    if rank != LEAD and not router:
        what = "书记员" if scribe else "%s +%s 的会话" % (seq[0], rank)
        return None, ("%s 是%s：执行方只收%s。改写负责这件事的 +1 的会话名（比如 %s-p1）；要下级干的活，由 +1 用需求包委托"
                      % (name, what, EXECUTOR_RANKS, seq[1]))
    return name, None


def executor_of(spec, value):
    """Who a decision package's 执行方 line names: 研究员, or a session that may carry a decision out, by
    name; None when it names neither, more than one, or a session of another rank (read_executor)."""
    return read_executor(spec, value)[0]


def placeholder(value):
    return value.startswith("<") and value.endswith(">")


def decision_errors(spec, header):
    """What is wrong with a decision package's 执行方 and 研究员要做的事 lines; a line that is missing
    is reported with the other header keys. Whether the two agree is checked once both are right; until
    then a 研究员要做的事 that names nothing for the researcher is told to say 无 only when the researcher
    does not carry the decision out."""
    errors = []
    action, who = header.get(RESEARCHER_ACTION), header.get(EXECUTOR)
    by, why = read_executor(spec, who)
    mine = by == RESEARCHER
    if action == "":
        errors.append("包头“%s”没写内容：%s" % (RESEARCHER_ACTION, RESEARCHER_DOES if mine else "没有要研究员亲手做的事，写“%s”" % NOTHING))
    elif action and placeholder(action):
        errors.append("包头“%s”还是模板里的占位：%s" % (RESEARCHER_ACTION, RESEARCHER_DOES if mine else WHAT_RESEARCHER_DOES))
    elif action and OTHER_NOTHING.match(action):
        errors.append("包头“%s”写的是“%s”：%s" % (RESEARCHER_ACTION, action, RESEARCHER_DOES if mine else
                                                "没有要研究员亲手做的事，只写“%s”；有的话，直接写要他做什么" % NOTHING))
    if who == "":
        errors.append("包头“%s”没写内容：%s" % (EXECUTOR, WHO_EXECUTES))
    elif who and placeholder(who):
        errors.append("包头“%s”还是模板里的占位：%s" % (EXECUTOR, WHO_EXECUTES))
    elif who is not None and not by:
        errors.append("包头“%s”写的是“%s”：%s" % (EXECUTOR, who, why))
    if errors or action is None or who is None:
        return errors
    nothing = bool(SAYS_NOTHING.match(action))
    if mine and nothing:
        errors.append("执行方是研究员，包头“%s”却写“%s”：写清楚要研究员亲手做什么；不用他亲手做什么的，执行方改写执行这条决策的会话名"
                      % (RESEARCHER_ACTION, NOTHING))
    elif not mine and not nothing:
        errors.append("执行方是 %s，包头“%s”就要写“%s”；要研究员亲手做这件事的，执行方改写“%s”" % (by, RESEARCHER_ACTION, NOTHING, RESEARCHER))
    return errors


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
        keys.insert(2, "关系")
    if pid == "decision":
        keys.insert(2, "渠道")
        at = keys.index("需要接收方做的事") + 1
        keys[at:at] = [EXECUTOR, RESEARCHER_ACTION, REVERSIBILITY, DEFAULT_ACTION]
    hints = {EXECUTOR: WHO_EXECUTES, VERSION: VERSION_HOW_DELIVERY if pid == "delivery" else VERSION_HOW, DEFAULT_ACTION: DEFAULT_HOW,
             RESEARCHER_ACTION: RESEARCHER_DOES if executor_of(spec, header.get(EXECUTOR)) == RESEARCHER else WHAT_RESEARCHER_DOES}
    if pid == "decision":
        hints[REVERSIBILITY] = "写“可逆”，或者“不可逆（判据 N）”，判据：%s" % spec.criteria_heads()
    for k in keys:
        if k not in header:
            errors.append("包头缺“%s”" % k + ("：" + hints[k] if k in hints else ""))
    if header.get(VERSION) is not None:
        why = read_version(header[VERSION])[2]
        if why:
            errors.append("包头“%s”写的是“%s”：%s" % (VERSION, header[VERSION], why))
    bad = [m.group(0) for m in DATE_TEXT.finditer(header.get("回复期限") or "") if not real_date(*m.groups())]
    if bad:
        errors.append("包头“回复期限”写的 %s 不是存在的日期：写存在的日期 YYYY-MM-DD，或者写“无”%s" % (
            "、".join(bad), "；决策包的回复期限只作参考" if pid == "decision" else ""))
    if pid == "decision":
        errors += decision_errors(spec, header)
        _, more, warned = decision_parts(spec, text, header)
        errors += more + settled_errors(text, header)
        warnings += warned
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
        # 1.1.0: the method of a 下发 request is the plan its +0 hands in; packages written before say 由接收方决定
        left = "由接收方决定" if relation == spec.term("rel", "cross") else "由接收方写成方案"
        if m and m.group(1).strip().rstrip("。") not in (left, "由接收方决定"):
            errors.append("%s关系的方法只写“%s”" % (relation, left))
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
              "handoff": spec.handoff_rules, "overview": spec.sequence_overview,
              "irreversible": spec.irreversible_section}.get(argv[1])
        if not fn:
            print("可以显示：ranks packages relations routes routing work-items task-rules docs-tree handoff overview irreversible；"
                  "流程和字段在飞书空间里，用 space.py show 看")
            return 2
        print(fn())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
