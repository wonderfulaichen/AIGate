# -*- coding: utf-8 -*-
"""提取 product-deck.html 的设计 token (配色/字体), 供 pptx 重排时延续同一套视觉。"""
import os
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
SRC = os.path.join(ROOT, "docs", "product-deck.html")
s = io.open(SRC, encoding="utf-8").read()

m = re.search(r":root\s*\{(.*?)\}", s, re.S)
print("=== :root ===")
print(re.sub(r"\s+", " ", m.group(1)).strip() if m else "(not found)")
print()

print("=== body 背景 ===")
for mm in re.finditer(r"(^|\})\s*(body|html)\s*\{([^}]{0,400})\}", s, re.S):
    print(" ", re.sub(r"\s+", " ", mm.group(3))[:360])
print()

print("=== 字体族 ===")
seen = set()
for mm in re.finditer(r"font-family\s*:\s*([^;}\n]{0,160})", s):
    v = mm.group(1).strip()
    if v not in seen:
        seen.add(v)
        print(" ", v[:150])
print()

print("=== 字号 (rem/px, 出现频次前 12) ===")
sizes = re.findall(r"font-size\s*:\s*([0-9.]+(?:rem|px|em))", s)
from collections import Counter
for v, c in Counter(sizes).most_common(12):
    print("  %-10s x%d" % (v, c))
print()

print("=== html 基准字号 ===")
for mm in re.finditer(r"(^|\})\s*html\s*\{([^}]{0,300})\}", s, re.S):
    print(" ", re.sub(r"\s+", " ", mm.group(2))[:280])