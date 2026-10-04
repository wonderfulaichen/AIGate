# -*- coding: utf-8 -*-
"""把视频帧序列拼成一张联络表, 一眼看动效走向 (逐帧读单图太费事)。

用法: python contact_sheet.py [帧号...]
      # 默认按样片的代表帧拼; 传帧号则只拼这些
"""
import os, sys
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下
FR = os.path.join(ROOT, ".openbitfun", "tmp", "video", "intro_frames")
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "video", "sheet.png")

frames = [int(a) for a in sys.argv[1:]] or [0, 10, 20, 30, 60, 120, 205, 209,
                                            210, 215, 225, 250, 300, 419,
                                            420, 430, 470, 560, 614]
TW, TH = 480, 270
COLS = 5
rows = (len(frames) + COLS - 1) // COLS
sheet = Image.new("RGB", (COLS * TW, rows * (TH + 18)), (18, 18, 20))
dr = ImageDraw.Draw(sheet)
for k, f in enumerate(frames):
    p = os.path.join(FR, "f%05d.png" % f)
    if not os.path.exists(p):
        continue
    im = Image.open(p).convert("RGB").resize((TW, TH), Image.LANCZOS)
    x, y = (k % COLS) * TW, (k // COLS) * (TH + 18)
    sheet.paste(im, (x, y + 18))
    dr.text((x + 6, y + 3), "f%05d  t=%.2fs" % (f, f / 30.0), fill=(200, 200, 210))
sheet.save(OUT)
print(OUT, sheet.size)
