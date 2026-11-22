"""生成 Solo-Lab 会话分工图的 SVG：README 和飞书文档里的架构图都用它。

用法：python3.12 -I docs/diagrams/architecture.py  （输出到脚本旁边）
    architecture.board.svg   中文，不带底色：写进飞书文档的画板
    architecture.zh.svg      中文，白底：README.md 引用
    architecture.en.svg      英文，白底：README.en.md 引用
要改图，改下面的 CARDS、TEXT 和 conns()，再重新生成；不直接改 SVG 里的坐标。只用标准库。
"""
import os

W, H = 380, 112          # 卡片尺寸
COL = {"A": 40, "B": 570, "C": 1100}                    # 列左边界，列间距 150
ROW = {1: 40, 2: 248, 3: 456, 4: 664, 5: 872}           # 行上边界，行间距 96
CANVAS_W, CANVAS_H = 1520, 1024

TITLE_PX, SUB_PX, LABEL_PX = 17, 13, 13
SUB_COLOR = "#646A73"

PAL = {
    "grey":   dict(fill="#F5F6F8", stroke="#B4B9C2", chip="#E3E5EA", icon="#5F6673",
                   title="#1F2329", line="#9AA0AB", label="#646A73"),
    "blue":   dict(fill="#EEF3FF", stroke="#5B8DEF", chip="#D9E5FF", icon="#2F6BE6",
                   title="#1C3D8F", line="#5B8DEF", label="#3F74E0"),
    "green":  dict(fill="#EAF8F1", stroke="#3DBE8B", chip="#CFF0E0", icon="#1A9A68",
                   title="#0F5F45", line="#3DBE8B", label="#1A9A68"),
    "purple": dict(fill="#F4EFFF", stroke="#9A7BEF", chip="#E4DAFF", icon="#6F4BD8",
                   title="#45279C", line="#9A7BEF", label="#7A56DC"),
}


# ---------- 图标（以圆心为原点，画在 ±10 的范围内） ----------

def _s(c):
    return f'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'


def icon_person(c):
    return (f'<circle cx="0" cy="-4.5" r="4" {_s(c)}/>'
            f'<path d="M-8.5 9.5 C-8.5 4.5 -4.5 2.5 0 2.5 C4.5 2.5 8.5 4.5 8.5 9.5" {_s(c)}/>')


def icon_chat(c):
    return (f'<rect x="-9.5" y="-8.5" width="19" height="13.5" rx="3.5" {_s(c)}/>'
            f'<polyline points="-4.5,5 -4.5,9.5 0.5,5" {_s(c)}/>')


def icon_route(c):
    return (f'<circle cx="-7.5" cy="0" r="2.4" {_s(c)}/>'
            f'<circle cx="7.5" cy="-6" r="2.4" {_s(c)}/>'
            f'<circle cx="7.5" cy="6" r="2.4" {_s(c)}/>'
            f'<polyline points="-5.1,0 0,0 0,-6 5.1,-6" {_s(c)}/>'
            f'<polyline points="0,0 0,6 5.1,6" {_s(c)}/>')


def icon_six(c):
    dots = []
    for cy in (-4.5, 4.5):
        for cx in (-7.5, 0, 7.5):
            dots.append(f'<circle cx="{cx}" cy="{cy}" r="2.5" fill="{c}"/>')
    return "".join(dots)


def icon_ledger(c):
    return (f'<rect x="-7.5" y="-9.5" width="15" height="19" rx="2.8" {_s(c)}/>'
            f'<line x1="-3.5" y1="-4" x2="3.5" y2="-4" {_s(c)}/>'
            f'<line x1="-3.5" y1="0" x2="3.5" y2="0" {_s(c)}/>'
            f'<line x1="-3.5" y1="4" x2="0.5" y2="4" {_s(c)}/>')


def icon_folder(c):
    return f'<polygon points="-9.5,-7.5 -3.5,-7.5 -1,-4.5 9.5,-4.5 9.5,7.5 -9.5,7.5" {_s(c)}/>'


def icon_terminal(c):
    return (f'<rect x="-9.5" y="-8" width="19" height="16" rx="3.2" {_s(c)}/>'
            f'<polyline points="-5,-3 -1.5,0 -5,3" {_s(c)}/>'
            f'<line x1="1" y1="3.5" x2="5" y2="3.5" {_s(c)}/>')


def icon_check(c):
    return (f'<rect x="-8.5" y="-8.5" width="17" height="17" rx="3.8" {_s(c)}/>'
            f'<polyline points="-4,0.2 -1.2,3.4 4.4,-3.4" {_s(c)}/>')


def icon_layers(c):
    return (f'<polygon points="0,-8.5 9.5,-4 0,0.5 -9.5,-4" {_s(c)}/>'
            f'<polyline points="-9.5,0.3 0,4.8 9.5,0.3" {_s(c)}/>'
            f'<polyline points="-9.5,4.6 0,9.1 9.5,4.6" {_s(c)}/>')


# ---------- 卡片 ----------

CARDS = [
    # id, 行, 列, 颜色, 图标；标题和小字在 TEXT 里，两种语言各一份
    ("user",   1, "B", "grey",   icon_person),
    ("entry",  2, "B", "blue",   icon_chat),
    ("router", 3, "A", "blue",   icon_route),
    ("p1",     3, "B", "purple", icon_six),
    ("ledger", 3, "C", "blue",   icon_ledger),
    ("local",  4, "A", "green",  icon_folder),
    ("p0",     4, "B", "purple", icon_terminal),
    ("meegle", 4, "C", "green",  icon_check),
    ("wiki",   5, "B", "green",  icon_layers),
]

# 卡片里放字的宽度是 W - 82 - 16 = 282，字宽照 text_w 估。放不下就加宽卡片，不缩字号。
TEXT = {
    "zh": {
        "user":   ("用户（+2）", ["说话、签字、拍板"]),
        "entry":  ("入口 PMO-p1", ["项目管理 +1 · 只和用户、路由往来", "管用户的决策队列"]),
        "router": ("路由 PMO-p0-router", ["判断一句话归哪件事", "在各序列之间转发"]),
        "p1":     ("六个序列的 +1", ["产品 · 算法 · 工程 · 基础设施 · 质量 · 外联", "定目标和验收标准 · 确认方案 · 验收"]),
        "ledger": ("书记员 PMO-p0-ledger", ["唯一写工作项的会话", "登记 · 确认节点 · 归档"]),
        "local":  ("本地项目目录", ["代码 · 运行记录", "任务包的草稿"]),
        "p0":     ("三个序列的 +0", ["工程 · 基础设施 · 质量", "先交方案，确认后执行，做完交付"]),
        "meegle": ("飞书项目", ["任务台账", "五类工作项，按节点流程走"]),
        "wiki":   ("飞书知识库", ["任务包 · 需求文档", "实验方案 · 结论"]),
        "talk": "说话、签字", "relay": "转录用户的话", "ask": "汇报需要用户决定的事、任务进展中的里程碑事件",
        "forward": "转给负责的会话", "report": "进展、要拍板的事", "delegate": "委托", "deliver": "方案、交付",
        "register": "各会话的登记请求", "write": "登记、确认节点", "draft": "各会话写草稿", "upload": "各会话上传自己的文档",
    },
    "en": {
        "user":   ("User (+2)", ["Talks, signs off, decides"]),
        "entry":  ("Entry PMO-p1", ["PMO +1 · user and router only", "Keeps the user's decision queue"]),
        "router": ("Router PMO-p0-router", ["Works out what a message is about", "Forwards between sequences"]),
        "p1":     ("+1 leads of six sequences", ["Product · Algorithm · Engineering", "Infrastructure · Quality · Liaison"]),
        "ledger": ("Scribe PMO-p0-ledger", ["Only session that writes work items", "Registers · confirms nodes · archives"]),
        "local":  ("Local project directory", ["Code · run records", "Task package drafts"]),
        "p0":     ("+0 of three sequences", ["Engineering · Infrastructure · Quality", "Plan first, then execute and deliver"]),
        "meegle": ("Feishu Project", ["Task ledger", "Five work item types, node workflows"]),
        "wiki":   ("Feishu Wiki", ["Task packages · requirements", "Experiment plans · conclusions"]),
        "talk": "talks, signs off", "relay": "the user's words, transcribed", "ask": "reports decisions needed and milestones",
        "forward": "forwards messages", "report": "progress, decisions", "delegate": "delegate", "deliver": "plan, delivery",
        "register": "register requests", "write": "register, confirm nodes", "draft": "write drafts", "upload": "sessions upload their documents",
    },
}


def card(cid, row, col, color, icon, title, subs):
    p = PAL[color]
    x, y = COL[col], ROW[row]
    tx = x + 82
    if len(subs) == 2:
        ty, sys_ = y + 40, [y + 64, y + 85]
    else:
        ty, sys_ = y + 51, [y + 75]
    out = [f'<g id="card-{cid}">']
    out.append(f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="14" ry="14" '
               f'fill="{p["fill"]}" stroke="{p["stroke"]}" stroke-width="2"/>')
    out.append(f'<circle cx="{x + 44}" cy="{y + 56}" r="22" fill="{p["chip"]}"/>')
    out.append(f'<g transform="translate({x + 44},{y + 56})">{icon(p["icon"])}</g>')
    out.append(f'<text x="{tx}" y="{ty}" font-size="{TITLE_PX}" font-weight="700" fill="{p["title"]}">{title}</text>')
    for sy, s in zip(sys_, subs):
        out.append(f'<text x="{tx}" y="{sy}" font-size="{SUB_PX}" fill="{SUB_COLOR}">{s}</text>')
    out.append('</g>')
    return "\n  ".join(out)


# ---------- 连线 ----------

def cx(col):
    return COL[col] + W // 2


def cy(row):
    return ROW[row] + H // 2


def top(row):
    return ROW[row]


def bottom(row):
    return ROW[row] + H


def left(col):
    return COL[col]


def right(col):
    return COL[col] + W


def text_w(text, px):
    """估出来的字宽：中文、全角标点和间隔号“·”每字 1em，空格 0.25em，其余约 0.56em。"""
    return sum(px if ord(ch) > 0x2E7F or ch == "·" else px * 0.25 if ch == " " else px * 0.56 for ch in text)


def conn(color, pts, text, lx, ly, anchor="start"):
    """pts: 折点列表；标签用 start 对齐，x 由本函数按 anchor 换算。"""
    p = PAL[color]
    w = text_w(text, LABEL_PX)
    if anchor == "middle":
        lx = lx - w / 2
    elif anchor == "end":
        lx = lx - w
    points = " ".join(f"{a},{b}" for a, b in pts)
    return (f'<polyline points="{points}" fill="none" stroke="{p["line"]}" stroke-width="2" '
            f'stroke-linejoin="round" marker-end="url(#arrow-{color})"/>\n  '
            f'<text x="{lx:.1f}" y="{ly:g}" font-size="{LABEL_PX}" fill="{p["label"]}">{text}</text>')


B, A, C = cx("B"), cx("A"), cx("C")
GAP_AB = (right("A") + left("B")) / 2      # 495
GAP_BC = (right("B") + left("C")) / 2      # 1025


def mid_y(r1, r2):
    """两行之间空隙的中线，返回 13px 文字的基线。"""
    return (bottom(r1) + top(r2)) / 2 + 4.5


def conns(t):
    """八组连线。一来一回画成两条线，错开 36 到 48，标签各贴各的线。"""
    return [
        # 1 用户 → 入口
        conn("grey", [(B, bottom(1)), (B, top(2))], t["talk"], B + 12, mid_y(1, 2)),
        # 2 入口 → 路由（外圈）；路由 → 入口（内圈）
        conn("blue", [(left("B"), cy(2) - 18), (A - 40, cy(2) - 18), (A - 40, top(3))],
             t["relay"], (A + 40 + left("B")) / 2, cy(2) - 18 - 9, "middle"),
        conn("blue", [(A + 40, top(3)), (A + 40, cy(2) + 18), (left("B"), cy(2) + 18)],
             t["ask"], (A + 40 + left("B")) / 2, cy(2) + 18 + 19, "middle"),
        # 3 路由 → +1；+1 → 路由
        conn("blue", [(right("A"), cy(3) - 18), (left("B"), cy(3) - 18)], t["forward"], GAP_AB, cy(3) - 18 - 9, "middle"),
        conn("purple", [(left("B"), cy(3) + 18), (right("A"), cy(3) + 18)], t["report"], GAP_AB, cy(3) + 18 + 19, "middle"),
        # 4 +1 → +0；+0 → +1
        conn("purple", [(B - 24, bottom(3)), (B - 24, top(4))], t["delegate"], B - 24 - 12, mid_y(3, 4), "end"),
        conn("purple", [(B + 24, top(4)), (B + 24, bottom(3))], t["deliver"], B + 24 + 12, mid_y(3, 4)),
        # 5 +1 → 书记员
        conn("purple", [(right("B"), cy(3)), (left("C"), cy(3))], t["register"], GAP_BC, cy(3) - 9, "middle"),
        # 6 书记员 → 飞书项目
        conn("blue", [(C, bottom(3)), (C, top(4))], t["write"], C + 12, mid_y(3, 4)),
        # 7 +0 → 本地项目目录
        conn("purple", [(left("B"), cy(4)), (right("A"), cy(4))], t["draft"], GAP_AB, cy(4) - 9, "middle"),
        # 8 +0 → 飞书知识库
        conn("purple", [(B, bottom(4)), (B, top(5))], t["upload"], B + 12, mid_y(4, 5)),
    ]


def marker(color):
    c = PAL[color]["line"]
    return (f'<marker id="arrow-{color}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" '
            f'markerWidth="11" markerHeight="11" orient="auto-start-reverse">'
            f'<path d="M0 0.8 L10 5 L0 9.2 Z" fill="{c}"/></marker>')


def check(lang):
    """字放不放得下：卡片里的每一行、两列之间的横线标签。放不下的在这里报出来，不等看图才发现。"""
    t, room, gap, bad = TEXT[lang], W - 82 - 16, COL["B"] - COL["A"] - W, []
    for cid, *_ in CARDS:
        title, subs = t[cid]
        for text, px in [(title, TITLE_PX)] + [(s, SUB_PX) for s in subs]:
            if text_w(text, px) > room:
                bad.append("%s 卡片里放不下（%d > %d）：%s" % (lang, text_w(text, px), room, text))
    for key in ("forward", "report", "register", "draft"):
        if text_w(t[key], LABEL_PX) > gap - 8:
            bad.append("%s 两列之间放不下（%d > %d）：%s" % (lang, text_w(t[key], LABEL_PX), gap - 8, t[key]))
    bend = left("B") - (A + 40) - 8                # 入口和路由之间那两条折线的横段
    for key in ("relay", "ask"):
        if text_w(t[key], LABEL_PX) > bend:
            bad.append("%s 折线的横段上放不下（%d > %d）：%s" % (lang, text_w(t[key], LABEL_PX), bend, t[key]))
    return bad


def build(lang="zh", background=False):
    t = TEXT[lang]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'width="{CANVAS_W}" height="{CANVAS_H}" font-family="\'Noto Sans SC\', \'PingFang SC\', sans-serif">',
        "<defs>\n  " + "\n  ".join(marker(c) for c in ("grey", "blue", "purple")) + "\n</defs>",
    ]
    if background:                     # GitHub 的深色主题下，透明底上的灰字看不清
        parts.append(f'<rect x="0" y="0" width="{CANVAS_W}" height="{CANVAS_H}" fill="#FFFFFF"/>')
    parts += [
        "<!-- 连线和线上的标签 -->",
        '<g id="connectors">\n  ' + "\n  ".join(conns(t)) + "\n</g>",
        "<!-- 卡片 -->",
    ]
    parts += [card(cid, row, col, color, icon, *t[cid]) for cid, row, col, color, icon in CARDS]
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    problems = check("zh") + check("en")
    for line in problems:
        print("出错：" + line)
    if problems:
        raise SystemExit(1)
    for name, lang, background in (("architecture.board.svg", "zh", False), ("architecture.zh.svg", "zh", True),
                                   ("architecture.en.svg", "en", True)):
        with open(os.path.join(here, name), "w", encoding="utf-8") as f:
            f.write(build(lang, background))
        print("wrote", name)
