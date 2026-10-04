# -*- coding: utf-8 -*-
"""把 capture_pv.js 的帧序列编成 **无声视频**, 音乐单独交付 —— 两者分开给剪辑软件。

为什么默认不混音 (2026-09-30 按用户要求改):
  用户有剪辑软件, 画面与音乐要各自独立: 分开交付才能自己配时间、换曲、调音量。
  故本脚本默认只出画面 (aigate-pv-silent.mp4), 音乐由 make_pv_music.py 单独产出
  (music.wav)。需要一条带声的预览时才加 --mux。

为什么片尾 fade 放在这一步 (而不是录制脚本里):
  deck/PV 自己只管页间过渡, 最后一页是硬切; fade 是纯编码期效果, 加它不必重录。

⚠️ `-framerate` 必须在 `-i` 之前: 图片序列自身不带时间戳, 它是帧率唯一来源;
   写到后面会被当输出帧率, 输入仍按 25fps 解析 —— 45s 会变成 54s。

用法:
  python tools/encode_pv.py          # 只出无声画面 (默认, 给剪辑软件)
  python tools/encode_pv.py --mux    # 额外出一条带音乐的预览 (响度归一到 -16 LUFS)
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PVD = os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv")
FRAMES = os.path.join(PVD, "frames")
# 定稿音轨 = v15 (90s: v14 底子 + 到站卡点 riser/boom/stab click/endcard swell)。--mux 混这条; 旧版 wav 全保留。
MUSIC = os.path.join(PVD, "music_v15-90s.wav")


def _next_out(stem):
    """生成不冲突的版本化输出名 (用户: 「别覆盖啊」) —— 扫描既有 r\d+ 取 max+1。"""
    import re
    mx = 0
    try:
        for f in os.listdir(PVD):
            m = re.match(re.escape(stem) + r"-r(\d+)\.mp4$", f)
            if m:
                mx = max(mx, int(m.group(1)))
    except FileNotFoundError:
        pass
    return os.path.join(PVD, "%s-r%02d.mp4" % (stem, mx + 1))


OUT_SILENT = _next_out("aigate-pv-silent")
OUT_MUX = _next_out("aigate-pv")

FPS = 30
DURATION = 90.0
FADE_OUT = 1.5          # 88.5–90s, 与 v9 音乐的 88.4s 淡出对齐
TARGET_LUFS = -16.0     # 网络视频常规响度; 低于它观众要手动调大音量


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
    q = os.path.join(os.path.dirname(find_ffmpeg()), "ffprobe.exe")
    return q if os.path.exists(q) else None


def measure_lufs(ff, path):
    """ebur128 集成响度 (LUFS)。测不到时返回 None。"""
    p = subprocess.run([ff, "-hide_banner", "-nostats", "-i", path,
                        "-filter_complex", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    loud = None
    for line in p.stderr.splitlines():
        if "I:" in line and "LUFS" in line:
            try:
                loud = float(line.split("I:")[1].split("LUFS")[0].strip())
            except ValueError:
                pass
    return loud


def probe(fp, out):
    p = subprocess.run([fp, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name",
                        "-of", "json", out], capture_output=True, text=True)
    return json.loads(p.stdout)


def main():
    do_mux = "--mux" in sys.argv
    pattern = os.path.join(FRAMES, "f%05d.jpg")
    if not os.path.exists(os.path.join(FRAMES, "f00000.jpg")):
        raise SystemExit("没有帧序列 —— 先跑 node tools/capture_pv.js")

    nframes = len([f for f in os.listdir(FRAMES) if f.endswith(".jpg")])
    ff = find_ffmpeg()
    fp = find_ffprobe()
    print("输入 %d 帧 (预期 %.2fs) / ffmpeg=%s" % (nframes, nframes / FPS, os.path.basename(ff)))
    if abs(nframes / FPS - DURATION) > 0.01:
        raise SystemExit("帧数 %d × 1/%ds ≠ %.1fs —— 录制不完整, 重跑 capture_pv.js"
                         % (nframes, FPS, DURATION))

    ok = True
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "info",
           "-framerate", str(FPS), "-i", pattern,
           "-vf", "fade=t=out:st=%.2f:d=%.2f" % (DURATION - FADE_OUT, FADE_OUT),
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-an",
           "-movflags", "+faststart",
           OUT_SILENT]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise SystemExit("编码失败 rc=%d" % r.returncode)
    for line in r.stderr.splitlines():
        if "Video:" in line and "fps" in line:
            print("  ffmpeg: " + line.strip())

    print("\n=== 无声画面校验 ===")
    if fp:
        info = probe(fp, OUT_SILENT)
        dur = float(info["format"]["duration"])
        drift = abs(dur - DURATION)
        ok &= drift < 0.1
        print("  %s时长 %.3fs (预期 %.3fs)" % ("OK " if drift < 0.1 else "!! ", dur, DURATION))
        types = sorted(s["codec_type"] for s in info["streams"])
        only_video = types == ["video"]
        ok &= only_video
        print("  %s流 %s (应为 video 单条)" % ("OK " if only_video else "!! ", types))

    p = subprocess.run([ff, "-v", "error", "-i", OUT_SILENT, "-f", "null", "-"],
                       capture_output=True, text=True)
    clean = p.returncode == 0 and not p.stderr.strip()
    ok &= clean
    print("  %s全解码%s" % ("OK " if clean else "!! ", "无错误" if clean else ": " + p.stderr.strip()[:300]))

    size = os.path.getsize(OUT_SILENT)
    print("  %s体积 %.1f MB" % ("OK " if size > 1e6 else "!! ", size / 1048576))

    # ── 音乐 (独立交付物): 只报状态, 不混进去 ──
    print("\n=== 音乐 (独立交付) ===")
    if os.path.exists(MUSIC):
        loud = measure_lufs(ff, MUSIC)
        print("  OK %s (%.1f MB)" % (MUSIC, os.path.getsize(MUSIC) / 1048576))
        if loud is not None:
            print("     原始响度 %.1f LUFS (混音时会静态增益到 %.1f)" % (loud, TARGET_LUFS))
    else:
        print("  -- 没有 music.wav —— 需要时跑 python tools/make_pv_music.py")

    # ── 可选: 带声预览 ──
    if do_mux:
        if not os.path.exists(MUSIC):
            raise SystemExit("--mux 需要 music.wav")
        gain_db = 0.0
        measured = measure_lufs(ff, MUSIC)
        if measured is not None:
            gain_db = max(-12.0, min(18.0, TARGET_LUFS - measured))
            print("\n混音: 音乐 %.1f LUFS → 目标 %.1f, 静态增益 %+.1f dB" % (measured, TARGET_LUFS, gain_db))
        # 静态增益而不是 loudnorm 动态模式: 后者逐段调增益, 会抹平本片的段落动态
        # (开场轻/高光满/收尾退是叙事手段)。静态增益只整体平移, 段落关系完整保留。
        # ⚠️ 静态增益上限受峰值限制: 音乐峰值 -1 dBFS, 增益超过 +0 dB 就撞限幅器,
        #    再大只会削峰 (实测 +5.3 dB 只到 -17.1 LUFS)。想更响必须上压缩, 刻意不做。
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "info",
               "-i", OUT_SILENT, "-i", MUSIC,
               "-filter_complex", "[1:a]volume=%.2fdB,alimiter=limit=0.891[a]" % gain_db,
               "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
               "-shortest", "-movflags", "+faststart", OUT_MUX]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-2000:])
            raise SystemExit("混音失败 rc=%d" % r.returncode)
        print("\n=== 带声预览校验 ===")
        if fp:
            info = probe(fp, OUT_MUX)
            types = sorted(s["codec_type"] + ":" + s["codec_name"] for s in info["streams"])
            has_av = "audio" in "".join(types) and "video" in "".join(types)
            ok &= has_av
            print("  %s流 %s" % ("OK " if has_av else "!! ", types))
        loud = measure_lufs(ff, OUT_MUX)
        if loud is not None:
            off = abs(loud - TARGET_LUFS)
            ok &= off < 2.5
            print("  %s响度 %.1f LUFS (目标 %.1f, 偏差 %.1f)"
                  % ("OK " if off < 2.5 else "!! ", loud, TARGET_LUFS, off))
        print("  OK %s (%.1f MB)" % (OUT_MUX, os.path.getsize(OUT_MUX) / 1048576))

    print("\n%s" % ("通过" if ok else "有问题"))
    print("  画面 -> %s" % OUT_SILENT)
    if do_mux:
        print("  预览 -> %s" % OUT_MUX)
    print(json.dumps({"silent": OUT_SILENT, "music": MUSIC if os.path.exists(MUSIC) else None,
                      "muxed": OUT_MUX if do_mux else None,
                      "frames": nframes, "seconds": DURATION, "pass": ok}, ensure_ascii=False))


if __name__ == "__main__":
    main()
