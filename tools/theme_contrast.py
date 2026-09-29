# -*- coding: utf-8 -*-
"""浅色配色的可读性检查: 对关键「前景/背景」组合算 WCAG 对比度, 并与暗色基准对照。

不能截图时的替代验证 —— 浅色模式最大的风险是「文字读不出来」(深字配深底 / 浅字配浅底),
而这是可以算的。阈值参考 WCAG AA: 正文 ≥4.5, 大字号/次要信息 ≥3.0。
"""
import os
import io, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
CSS = io.open(os.path.join(ROOT, "src", "admin", "base.css"), encoding="utf-8").read()
CSS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def block(sel):
    m = re.search(sel + r"\s*\{(.*?)\n\}", CSS, re.S)
    d = {}
    for decl in m.group(1).split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            k = k.strip()
            if k.startswith("--"):
                d[k] = v.strip()
    return d


DARK, LIGHT = block(r":root"), block(r"html\.light")


def parse(c):
    """→ (r,g,b,a)；支持 #rgb/#rrggbb/rgba()。透明色返回 alpha=0（需与底色合成）。"""
    c = c.strip()
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", c)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4) or 1)
    raise ValueError("无法解析颜色: " + c)


def over(fg, bg):
    """把带透明的 fg 合成到不透明 bg 上 (token 常是半透明叠加)."""
    if fg[3] >= 1:
        return fg[:3]
    return tuple(fg[i] * fg[3] + bg[i] * (1 - fg[3]) for i in range(3))


def lum(rgb):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg_s, bg_s, table):
    # 先解析 token 引用到字面量
    fg_lit = table.get(fg_s, fg_s)
    bg_lit = table.get(bg_s, bg_s)
    bg_base = parse(table.get("--bg"))[:3]  # 画布底色 (半透明底的实际背景)
    bg = over(parse(bg_lit), bg_base)
    fg = over(parse(fg_lit), bg)
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# (前景 token, 背景 token, 说明, 最低要求)
PAIRS = [
    ("--fg", "--bg", "正文 / 画布", 4.5),
    ("--fg", "--card", "正文 / 卡片", 4.5),
    ("--muted", "--bg", "次要文字 / 画布", 3.0),
    ("--muted", "--card", "次要文字 / 卡片", 3.0),
    ("--primary-fg", "--primary", "主按钮文字 / 主色", 4.5),
    ("--accent", "--primary-soft", "主色文字 / 主色软底", 3.0),
    ("--accent", "--primary-badge", "主色文字 / 徽标底", 3.0),
    ("--ok-fg", "--ok-bg", "成功徽标 字/底", 3.0),
    ("--err-fg", "--err-bg", "错误徽标 字/底", 3.0),
    ("--warn-fg", "--warn-bg", "警告徽标 字/底", 3.0),
    ("--info-fg", "--info-bg", "信息徽标 字/底", 3.0),
    ("--neutral-fg", "--neutral-bg", "中性徽标 字/底", 3.0),
    ("--err-fg", "--toast-err-bg", "错误 Toast 字/底", 3.0),
    ("--ok-fg", "--toast-ok-bg", "成功 Toast 字/底", 3.0),
    ("--warn-fg", "--toast-warn-bg", "警告 Toast 字/底", 3.0),
    ("--fg", "--elevated", "正文 / 浮层", 4.5),
    ("--err-fg", "--err-bg-strong", "危险按钮 字/底 (红字+淡红底)", 4.5),
    ("--primary", "--card", "主色(填充) / 卡片", 3.0),
]

print("%-30s %10s %10s   %s" % ("组合", "暗色", "浅色", "判定"))
print("-" * 74)
bad = 0
for fg, bg, desc, need in PAIRS:
    try:
        rd = ratio(fg, bg, DARK)
        rl = ratio(fg, bg, LIGHT)
    except Exception as e:
        print("%-30s  解析失败: %s" % (desc, e))
        bad += 1
        continue
    ok = rl >= need
    if not ok:
        bad += 1
    print("%-30s %9.2f %10.2f   %s (需 ≥%.1f)" % (desc, rd, rl, "OK " if ok else "不足", need))

print("-" * 74)
print("浅色下不达标的组合: %d 个" % bad)
