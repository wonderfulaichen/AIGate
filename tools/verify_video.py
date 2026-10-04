# -*- coding: utf-8 -*-
"""对**成片**做交付前检查 —— 不信任"文件写出来了"就等于成功。

四件事:
  ① 逐秒采样, 找"整段空白/死掉"的区间 (亮度均值为 0 或方差近 0 的秒);
  ② 抽帧拼联络表, 人眼过一遍 18 页是否都在、有没有半张页面的中间态;
  ③ 每个页面边界附近多抽几帧, 看翻页交叉淡化是否正常 (有没有两边都不见的空档);
  ④ 抽 t=0.1s / t=0.5s 的帧核字体是否落到真身 (FOUT 检查)。

用法: python verify_video.py <mp4>
"""
import os
import subprocess
import sys

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VDIR = os.path.join(ROOT, ".openbitfun", "tmp", "video")


def find_ffmpeg():
    import shutil
    cands = [shutil.which("ffmpeg")]
    w = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(w):
        for d in os.listdir(w):
            if d.startswith("Gyan.FFmpeg"):
                for sub in os.listdir(os.path.join(w, d)):
                    p = os.path.join(w, d, sub, "bin", "ffmpeg.exe")
                    if os.path.exists(p):
                        cands.append(p)
    cands.append(r"C:\Program Files (x86)\Lenovo\LegionZone\2.0.30.9243\SEGamingAI\services\editor\ffmpeg.exe")
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise SystemExit("找不到 ffmpeg")


def grab(ff, mp4, t, out):
    """按时间戳抽一帧 (输入侧 -ss 快速定位)。"""
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", "%.3f" % t, "-i", mp4, "-frames:v", "1", out],
                   check=True)
    return out


def main():
    mp4 = sys.argv[1]
    ff = find_ffmpeg()
    tmp = os.path.join(VDIR, "qc")
    os.makedirs(tmp, exist_ok=True)

    # 时长
    import shutil
    fp = shutil.which("ffprobe") or os.path.join(os.path.dirname(ff), "ffprobe.exe")
    dur = 113.0
    if os.path.exists(fp):
        r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", mp4], capture_output=True, text=True)
        dur = float(r.stdout.strip())
    print("成片 %s\n时长 %.3fs / %.1f MB\n" % (os.path.basename(mp4), dur, os.path.getsize(mp4) / 1048576))

    # ① 逐秒亮度/细节, 找空白段
    print("=== ① 逐秒采样, 找空白段 ===")
    blank = []
    rows = []
    for s in range(int(dur)):
        p = grab(ff, mp4, s + 0.5, os.path.join(tmp, "s%03d.png" % s))
        a = np.asarray(Image.open(p).convert("L"), dtype=np.float64)
        g = np.asarray(Image.open(p).convert("RGB"), dtype=np.float64)
        mean, std = a.mean(), a.std()
        colour = float(np.abs(g - g.mean(axis=(0, 1))).mean())   # 有没颜色 (纯黑/纯色 = 0)
        rows.append((s, mean, std, colour))
        if mean < 2 and std < 2:
            blank.append(s)
    for s, mean, std, colour in rows:
        flag = "  <-- 疑似空白" if (mean < 2 and std < 2) else ""
        if s % 6 == 0 or flag:
            print("  t=%3ds  亮度 %6.2f  方差 %6.2f  色彩 %6.2f%s" % (s, mean, std, colour, flag))
    print("  空白秒: %s" % (blank if blank else "无"))

    # ② 每页中点抽帧, 拼联络表
    print("\n=== ② 每页中点联络表 ===")
    PAGE_SEC = [6.0, 6.0, 7.0, 6.0, 6.0, 6.0, 6.5, 7.0, 7.0, 6.0, 6.0, 6.5, 6.0, 6.5, 6.0, 6.0, 6.0, 6.5]
    bounds, acc = [], 0.0
    for d in PAGE_SEC:
        bounds.append((acc, acc + d))
        acc += d
    mids = []
    for k, (a, b) in enumerate(bounds):
        mid = a + (b - a) * 0.55        # 偏后一点: 让入场动效都跑完
        p = grab(ff, mp4, mid, os.path.join(tmp, "m%02d.png" % (k + 1)))
        mids.append((k + 1, mid, p))
        print("  第 %2d 页  t=%.2fs -> %s" % (k + 1, mid, os.path.basename(p)))

    cols, tw, th = 3, 620, 349
    rowsn = (len(mids) + cols - 1) // cols
    from PIL import ImageDraw
    sheet = Image.new("RGB", (cols * tw, rowsn * (th + 20)), (16, 16, 18))
    dr = ImageDraw.Draw(sheet)
    for k, (num, mid, p) in enumerate(mids):
        im = Image.open(p).convert("RGB").resize((tw, th), Image.LANCZOS)
        x, y = (k % cols) * tw, (k // cols) * (th + 20)
        sheet.paste(im, (x, y + 20))
        dr.text((x + 6, y + 4), "p%02d  t=%.2fs" % (num, mid), fill=(210, 210, 220))
    sp = os.path.join(VDIR, "qc_pages.png")
    sheet.save(sp)
    print("  联络表: %s" % sp)

    # ③ 翻页交叉淡化: 抽查几个边界
    print("\n=== ③ 页面边界交叉淡化 ===")
    edges = [6.0, 12.0, 25.0, 45.5, 67.5, 90.5]
    strip = []
    for e in edges:
        for off in (-0.3, 0.0, 0.15, 0.3, 0.55, 0.9):
            p = grab(ff, mp4, e + off, os.path.join(tmp, "e%05.0f.png" % ((e + off) * 1000)))
            a = np.asarray(Image.open(p).convert("L"), dtype=np.float64)
            strip.append((e + off, a.mean(), a.std()))
    for t, mean, std in strip:
        tag = "  <-- 偏暗" if mean < 6 else ""
        print("  t=%6.2fs  亮度 %6.2f  方差 %6.2f%s" % (t, mean, std, tag))

    # ④ FOUT 检查
    print("\n=== ④ 开头字体 (t=0.1 / 0.5s) ===")
    for t in (0.1, 0.5, 1.0):
        p = grab(ff, mp4, t, os.path.join(tmp, "fout_%03d.png" % int(t * 100)))
        a = np.asarray(Image.open(p).convert("L"), dtype=np.float64)
        print("  t=%.1fs  亮度 %6.2f  方差 %6.2f  -> %s" % (t, a.mean(), a.std(), os.path.basename(p)))


if __name__ == "__main__":
    main()
