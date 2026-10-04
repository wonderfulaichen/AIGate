# -*- coding: utf-8 -*-
"""把 capture_deck_video.js 产出的帧序列编码成 MP4, 并做交付前校验。

为什么 `-framerate` 必须放在 `-i` 之前: 图片序列本身不带时间戳, 这个参数就是"帧率"的唯一来源;
写后面会被当成输出帧率, 输入仍按默认 25fps 解析 —— 113s 的片子会变成 113*30/25 = 135.6s。

为什么编码前先看输入: ffmpeg 的 stderr 里 "Stream #0:0" 那行会报它把序列读成了多少帧/多少 fps,
不一致时当场就能发现 (比出片后再去量时长便宜)。

用法: python encode_video.py [--png]      # 默认读 .jpg 帧; --png 则读 .png
"""
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下
VDIR = os.path.join(ROOT, ".openbitfun", "tmp", "video")
FRAMES = os.path.join(VDIR, "intro_frames")
OUT = os.path.join(VDIR, "aigate-intro.mp4")

FPS = 30

# 片尾淡黑。deck 自己只管页间过渡, 最后一页是硬切 —— 介绍片结尾硬切太生硬。
# 用 ffmpeg 的 fade 而不是回录制脚本里改: 它是纯编码期效果, 加它不必重录 (13 分钟)。
# 不做片头淡入: deck 首页自己就有淡入 (实测 t=0 方差 6.97 -> t=0.6s 26.36), 再加一层是重复。
FADE_OUT = 0.7

# ffmpeg 解析: 优先 PATH, 其次 winget 安装位置, 最后那个借用的 LegionZone 版 (它没有 libx264)。
def find_ffmpeg(need_x264=True):
    import shutil
    cands = [shutil.which("ffmpeg")]
    winget = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget):
        for d in os.listdir(winget):
            if d.startswith("Gyan.FFmpeg"):
                for sub in os.listdir(os.path.join(winget, d)):
                    p = os.path.join(winget, d, sub, "bin", "ffmpeg.exe")
                    if os.path.exists(p):
                        cands.append(p)
    cands.append(r"C:\Program Files (x86)\Lenovo\LegionZone\2.0.30.9243\SEGamingAI\services\editor\ffmpeg.exe")
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        if need_x264:
            enc = subprocess.run([p, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
            if "libx264" not in enc:
                continue
        return p
    raise SystemExit("找不到带 libx264 的 ffmpeg —— winget install Gyan.FFmpeg 可补齐")


def find_ffprobe():
    import shutil
    p = shutil.which("ffprobe")
    if p:
        return p
    d = os.path.dirname(find_ffmpeg())
    q = os.path.join(d, "ffprobe.exe")
    return q if os.path.exists(q) else None


def main():
    ext = "png" if "--png" in sys.argv else "jpg"
    pattern = os.path.join(FRAMES, "f%05d." + ext)
    if not os.path.exists(os.path.join(FRAMES, "f00000." + ext)):
        raise SystemExit("没有帧序列: %s —— 先跑 tools/capture_deck_video.js" % pattern)

    nframes = len([f for f in os.listdir(FRAMES) if f.endswith("." + ext)])
    expected = nframes / FPS
    ff = find_ffmpeg()
    fp = find_ffprobe()
    print("输入 %d 帧 (%s) / 预期 %.2fs / ffmpeg=%s" % (nframes, ext, expected, os.path.basename(ff)))

    # 帧尺寸先核一遍 —— 尺寸不对时 ffmpeg 会默默按第一帧缩放其余帧, 出片变形
    try:
        from PIL import Image
        w, h = Image.open(os.path.join(FRAMES, "f00000." + ext)).size
        print("帧尺寸 %dx%d" % (w, h))
        if (w, h) != (1920, 1080):
            raise SystemExit("帧尺寸不是 1920x1080 —— 检查录制时的 viewport")
    except ImportError:
        pass

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "info",
           "-framerate", str(FPS),                              # 必须在 -i 前
           "-i", pattern,
           "-vf", "fade=t=out:st=%.2f:d=%.2f" % (expected - FADE_OUT, FADE_OUT),
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p",
           "-movflags", "+faststart",                            # 尾部 moov 前移, 否则预览器播不了
           OUT]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise SystemExit("编码失败 rc=%d" % r.returncode)

    # ffmpeg 自己报的输入解析结果 —— 帧率/帧数对不上在这里就能看见
    for line in r.stderr.splitlines():
        if "Stream #0:0" in line or "Video:" in line and "fps" in line:
            print("  ffmpeg: " + line.strip())

    # ── 校验 (skill: Verify — always) ──
    print("\n=== 校验 ===")
    ok = True
    if fp:
        p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", OUT], capture_output=True, text=True)
        dur = float(p.stdout.strip())
        drift = abs(dur - expected)
        tag = "OK " if drift < 0.05 else "!! "
        ok &= drift < 0.05
        print("  %s时长 %.3fs (预期 %.3fs, 偏差 %.3fs)" % (tag, dur, expected, drift))
    else:
        print("  -- 无 ffprobe, 跳过时长校验")

    p = subprocess.run([ff, "-v", "error", "-i", OUT, "-f", "null", "-"],
                       capture_output=True, text=True)
    if p.returncode == 0 and not p.stderr.strip():
        print("  OK 全解码无错误")
    else:
        ok = False
        print("  !! 全解码报错: %s" % p.stderr.strip()[:400])

    size = os.path.getsize(OUT)
    print("  %s体积 %.1f MB" % ("OK " if size > 1e6 else "!! ", size / 1048576))
    print("\n%s -> %s" % ("通过" if ok else "有问题", OUT))
    print(json.dumps({"out": OUT, "frames": nframes, "fps": FPS,
                      "seconds": round(expected, 3), "bytes": size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
