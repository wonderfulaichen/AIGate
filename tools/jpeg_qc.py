# -*- coding: utf-8 -*-
"""测 ffmpeg 把 PNG 帧转 JPEG 的实际画质损失 —— 这是管道真正会引入的损耗。

为什么不在 node 里比两张 screenshot: 那一版两张图的星尘 canvas 在两次拍摄间又画过帧,
像素源不同, PSNR 测的是"画面变了"而不是"编解码差了"(实测 31.5 dB 就是这么来的假警报)。
这里固定同一张 PNG 当基准, 只让 ffmpeg 编码一遍再比 —— 单一变量。
用法: python jpeg_qc.py <ffmpeg> <png>
"""
import os, subprocess, sys
import numpy as np
from PIL import Image

ff, png = sys.argv[1], sys.argv[2]
outdir = os.path.dirname(png)
ref = np.asarray(Image.open(png).convert("RGB"), dtype=np.float64)

print("ffmpeg -q:v 档位 -> 体积 / PSNR (对照无损 PNG)")
best = None
for q in (1, 2, 3, 4, 5, 8, 12):
    jp = os.path.join(outdir, "ff_q%02d.jpg" % q)
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", png, "-q:v", str(q), jp], check=True)
    arr = np.asarray(Image.open(jp).convert("RGB"), dtype=np.float64)
    mse = np.mean((ref - arr) ** 2)
    psnr = float("inf") if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)
    kb = os.path.getsize(jp) / 1024
    pngkb = os.path.getsize(png) / 1024
    maxd = int(np.abs(ref - arr).max())
    pct = 100.0 * (np.abs(ref - arr).max(axis=2) > 16).mean()
    print("  -q:v %-2d  %7.0f KB   PSNR %6.2f dB   最大差 %3d   差>16 占比 %.4f%%" % (q, kb, psnr, maxd, pct))
    if best is None or (psnr >= 42 and kb < best[1]):
        best = (q, kb, psnr)
print("\nPNG 基准: %.0f KB" % (os.path.getsize(png) / 1024))
print("建议档位: -q:v %d  (%.0f KB, PSNR %.2f dB)" % best)
