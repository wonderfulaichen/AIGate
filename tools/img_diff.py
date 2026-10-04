# -*- coding: utf-8 -*-
"""量化比较两张同画面截图的差异, 并自动裁出"细节最多"的区域放大看。

为什么不用肉眼看整图缩略: 1080p 缩到缩略图后 JPEG 的块效应与文字发虚都看不出来。
故 ① 用 PSNR 给客观数字; ② 自动找边缘最密的窗口裁出来放大, 那才是压缩瑕疵该出现的地方。
用法: python img_diff.py <a.png> <b.jpg> <out_crop.png>
"""
import os, sys
import numpy as np
from PIL import Image

a, b, out = sys.argv[1], sys.argv[2], sys.argv[3]
A = np.asarray(Image.open(a).convert("RGB"), dtype=np.float64)
B = np.asarray(Image.open(b).convert("RGB"), dtype=np.float64)
H, W, _ = A.shape

mse = np.mean((A - B) ** 2)
psnr = float("inf") if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)
print("PSNR = %.2f dB   (>=40 肉眼难辨, >=45 几乎无损)" % psnr)

# 边缘密度: 灰度沿 x/y 的梯度幅值, 决定"哪块窗口细节最多"
g = np.asarray(Image.open(a).convert("L"), dtype=np.float64)
gy = np.abs(np.diff(g, axis=0))[:, :-1]
gx = np.abs(np.diff(g, axis=1))[:-1, :]
grad = gy + gx
cw, ch = 900, 300
best, bx, by = -1, 0, 0
for y in range(0, H - ch, 60):
    for x in range(0, W - cw, 60):
        s = grad[y:y + ch, x:x + cw].sum()
        if s > best:
            best, bx, by = s, x, y
print("细节最密区域: (%d,%d) 尺寸 %dx%d" % (bx, by, cw, ch))

# 上下并排: 上 = 参考(PNG), 下 = 待检(JPEG), 2x 最近邻放大
ref = Image.open(a).convert("RGB").crop((bx, by, bx + cw, by + ch))
tst = Image.open(b).convert("RGB").crop((bx, by, bx + cw, by + ch))
S = 2
canvas = Image.new("RGB", (cw * S, ch * 2 * S + 8), (255, 0, 0))
canvas.paste(ref.resize((cw * S, ch * S), Image.NEAREST), (0, 0))
canvas.paste(tst.resize((cw * S, ch * S), Image.NEAREST), (0, ch * S + 8))
canvas.save(out)
print("裁剪对比: %s  (上=参考, 下=待检)" % out)

# 逐像素最大差, 看有没局部崩坏
d = np.abs(A - B).max(axis=2)
print("最大像素差 = %d   差值>16 的像素占比 = %.4f%%" % (d.max(), 100.0 * (d > 16).mean()))
