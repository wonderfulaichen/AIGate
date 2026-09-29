# -*- coding: utf-8 -*-
"""提取 product-deck.html 每页的文字内容 (供 pptx 重排时对照), 不含 base64/SVG。"""
import os
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
SRC = os.path.join(ROOT, "docs", "product-deck.html")

s = io.open(SRC, encoding="utf-8").read()
pos, i = [], 0
while True:
    i = s.find("<section class=", i)
    if i < 0:
        break
    pos.append(i)
    i += 1

for k in range(len(pos)):
    end = pos[k + 1] if k + 1 < len(pos) else len(s)
    body = s[pos[k]:end]
    body = re.sub(r"data:image/[^\s\"']+", "[IMG]", body)      # base64 内嵌图
    body = re.sub(r"<svg.*?</svg>", "[SVG]", body, flags=re.S)  # 内联矢量图
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    print("--- P%02d (%d chars) ---" % (k + 1, len(body)))
    print(body[:520])
    print()