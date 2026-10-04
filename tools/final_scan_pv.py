# -*- coding: utf-8 -*-
"""90s 成片终检: 空白帧扫描 + 联络表 (读 final90/ 的抽帧)。"""
import glob
import os
import sys

from PIL import Image, ImageDraw, ImageStat

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".openbitfun", "tmp", "video", "pv", "final90")
TIMES = [0.5, 3, 6, 9, 12, 13.13, 14.5, 15.469, 15.7, 17, 18.75, 21, 24, 26.7, 28.1,
         31, 34, 37.5, 40, 43, 46, 49, 51, 52.5, 55, 58, 61, 63.75, 67, 70, 73.1,
         75, 77, 79.5, 82, 84.5, 86.5, 88.5]

fs = sorted(glob.glob(os.path.join(D, "f*.png")))
bad = []
for i, f in enumerate(fs):
    im = Image.open(f).convert("L")
    s = ImageStat.Stat(im)
    t = TIMES[i] if i < len(TIMES) else -1
    # 88.5 起是片尾淡出, 允许低方差; 其余 std<2 判空白
    allow = 1.2 if t >= 88.0 else 2.0
    tag = ""
    if s.stddev[0] < allow:
        tag = " <-- 空白!"
        bad.append(t)
    print("t=%6.2fs mean=%6.1f std=%5.1f%s" % (t, s.mean[0], s.stddev[0], tag))
print("空白帧:", bad if bad else "无")

# 联络表
TW, TH, COLS = 480, 270, 5
rows = (len(fs) + COLS - 1) // COLS
sh = Image.new("RGB", (COLS * TW, rows * (TH + 16)), (18, 18, 20))
dr = ImageDraw.Draw(sh)
for k, f in enumerate(fs):
    im = Image.open(f).convert("RGB").resize((TW, TH), Image.LANCZOS)
    x, y = (k % COLS) * TW, (k // COLS) * (TH + 16)
    sh.paste(im, (x, y + 16))
    dr.text((x + 5, y + 2), ("%.2fs" % TIMES[k]) if k < len(TIMES) else ("f%d" % k),
            fill=(211, 211, 220))
out = os.path.join(D, "final_sheet.png")
sh.save(out)
print(out)
sys.exit(1 if bad else 0)
