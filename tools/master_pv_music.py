# -*- coding: utf-8 -*-
"""v8 母带 —— 把 v6 (用户: "最接近") 做音色/响度修正, **编曲一个音符都不动**。

═══ 为什么是母带而不是重写 ═══
v7 被否 ("比之前差: 后面刺耳 + 变慢没劲"), 用户选 v6 最接近。
细分频段实测 v6 本身不刺 (presence 2-5k 仅 1.9%, 参考 3.4%),
v6 的真实差距是 **响度**: 参考片 -14.5 LUFS, v6 约 -21 —— 同音量下就是"没劲"。
所以: 修响度 (静态增益 + 软限幅) + 预防性去毛刺 (≥5kHz 轻削 2.5dB, 450Hz 盒声轻削 2dB),
其余全部保持 —— 用户喜欢的节奏/动机/滤波开合一个都不碰。

═══ 与参考片的量化差距 (music_profile.py + band_fine.py 实测) ═══
  参考: 低63.1/中29.2/高7.7 · 1s动态14.7dB · onset5.75 · -14.5 LUFS
  v6:   低56.4/中39.3/高4.3  · 1s动态11.7dB · onset6.98 · ≈-21 LUFS
  本版目标: 响度 -16 LUFS (网页常规), 高频毛刺削掉, 其余忠实 v6。

用法: python tools/master_pv_music.py [输入wav] [--target -16]
产物: .openbitfun/tmp/video/pv/music_v8-v6master.wav (不覆盖任何旧版)
      tools/pv_music_cues.json 恢复 v6 网格 (128BPM / 切口 15.46875)
"""
import json
import os
import shutil
import subprocess
import sys
import wave

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
PVD = os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv")

SRC = os.path.join(PVD, "music_v6-engineer.wav")
TARGET_LUFS = -16.0
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--target" and i + 1 < len(args):
        TARGET_LUFS = float(args[i + 1])
    elif not a.startswith("--"):
        SRC = a
OUT = os.path.join(PVD, "music_v8-v6master.wav")
OUT_TMP = os.path.join(PVD, "_v8_iter.wav")      # 迭代实测用临时文件 (收尾删除)

if not os.path.exists(SRC):
    raise SystemExit("没有源文件: %s" % SRC)


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    winget = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget):
        for d in os.listdir(winget):
            if d.startswith("Gyan.FFmpeg"):
                for sub in os.listdir(os.path.join(winget, d)):
                    q = os.path.join(winget, d, sub, "bin", "ffmpeg.exe")
                    if os.path.exists(q):
                        return q
    raise SystemExit("找不到 ffmpeg")


def measure_lufs(path):
    ff = find_ffmpeg()
    r = subprocess.run([ff, "-hide_banner", "-nostats", "-i", path,
                        "-filter_complex", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    out = None
    for line in r.stderr.splitlines():
        if "I:" in line and "LUFS" in line:
            try:
                out = float(line.split("I:")[1].split("LUFS")[0].strip())
            except ValueError:
                pass
    return out


def load(path):
    w = wave.open(path, "rb")
    sr, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
    raw = w.readframes(w.getnframes())
    w.close()
    assert sw == 2 and sr == 44100, "只处理 44.1k/16bit"
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return x.reshape(-1, nch) if nch == 2 else x.reshape(-1, 1), nch


def eq_curve(freqs):
    """母带 EQ (dB): 450Hz 盒声轻削 -2dB (钟形) + ≥5kHz 去毛刺 -2.5dB (平滑架式)。
    零相位 FFT 实现 —— 离线母带, 线性相位比最小相位更干净。"""
    f = np.maximum(freqs, 1.0)
    lf = np.log2(f)
    bell = -2.0 * np.exp(-((lf - np.log2(450.0)) / 0.55) ** 2 / 2.0)
    shelf = -2.5 / (1.0 + np.exp(-(lf - np.log2(5000.0)) * 5.0))
    db = bell + shelf
    db[0] = 0.0                       # DC 不动
    return db


def main():
    print("源: %s" % SRC)
    i_before = measure_lufs(SRC)
    x, nch = load(SRC)
    n = x.shape[0]
    print("输入响度: %s LUFS" % ("%.1f" % i_before if i_before else "?"))

    # ── ① 零相位 EQ ──
    freqs = np.fft.rfftfreq(n, 1 / 44100)
    g = 10.0 ** (eq_curve(freqs) / 20.0)
    y = np.empty_like(x)
    for c in range(nch):
        y[:, c] = np.fft.irfft(np.fft.rfft(x[:, c]) * g, n)
    print("EQ: 450Hz -2dB 钟形 + 5kHz↑ -2.5dB 架式 (线性相位)")

    # ── ② 响度增益 (静态, 不做动态 loudnorm: 会压平编曲的呼吸) ──
    # ⚠️ 单趟增益到不了目标: 软限幅+峰值归一会吃掉一部分 (实测 -16 目标只到 -17.6)。
    #    故做 3 轮"增益→限幅→实测"迭代, 每轮用残差修正, 收敛到目标 ±0.5 LUFS。
    if i_before is not None:
        for rnd in range(3):
            need = TARGET_LUFS - (i_before if rnd == 0 else measure_lufs(OUT_TMP))
            if abs(need) < 0.4:
                break
            if rnd == 0:
                y *= 10.0 ** (max(-12.0, min(18.0, need)) / 20.0)
            else:
                y *= 10.0 ** (max(-6.0, min(9.0, need)) / 20.0)
            # 软限幅
            th = 0.86
            a = np.abs(y)
            over = a > th
            y[over] = np.sign(y[over]) * (th + (1 - th) * np.tanh((a[over] - th) / (1 - th)))
            peak = np.abs(y).max()
            if peak > 0.891:
                y *= 0.891 / peak
            data = np.empty(n * nch, dtype=np.int16)
            for c in range(nch):
                data[c::nch] = np.clip(y[:, c], -1, 1) * 32767
            with wave.open(OUT_TMP, "wb") as wf:
                wf.setnchannels(nch)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(data.tobytes())
            print("  轮 %d: %+.1f dB → 实测 %.1f LUFS" % (rnd + 1, need, measure_lufs(OUT_TMP) or -99))

    # ── ④ 写出 (v8 独立文件, 不覆盖) ──
    data = np.empty(n * nch, dtype=np.int16)
    for c in range(nch):
        data[c::nch] = np.clip(y[:, c], -1, 1) * 32767
    with wave.open(OUT, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(data.tobytes())
    if os.path.exists(OUT_TMP):
        os.remove(OUT_TMP)
    i_after = measure_lufs(OUT)
    print("峰值 %.2f dBFS" % (20 * np.log10(np.abs(y).max())))
    print("输出: %s" % OUT)
    print("输出响度: %s LUFS (目标 %.1f)" % ("%.1f" % i_after if i_after else "?", TARGET_LUFS))

    # ── ⑤ cues 恢复 v6 网格 (128BPM, 与 v6/v8 编曲一致) ──
    BEAT = 60.0 / 128.0
    BAR = BEAT * 4
    cues = {
        "_note": "v8 = v6 编曲的母带版 (响度/去毛刺, 编曲未动)。"
                 "网格 = v6 的 128BPM。生成: tools/master_pv_music.py",
        "version": "v8-v6master",
        "bpm": 128.0, "beat_sec": round(BEAT, 6), "bar_sec": round(BAR, 6),
        "total_bars": 24, "duration": 45.0,
        "section_marks": [
            {"name": "intro", "bar": 1, "t": 0.0},
            {"name": "groove", "bar": 3, "t": round(2 * BAR, 4)},
            {"name": "build", "bar": 8, "t": round(7 * BAR, 4)},
            {"name": "reveal", "bar": 9, "t": round(8 * BAR, 4)},
            {"name": "sustain", "bar": 15, "t": round(14 * BAR, 4)},
            {"name": "outro", "bar": 21, "t": round(20 * BAR, 4)},
        ],
        "gate_cut": round(8 * BAR + BEAT, 6),        # 15.46875
        "impact": round(8 * BAR + BEAT + 0.09, 6),   # 15.55875
        "fade_out": [43.4, 45.0],
        "beats": [round(b * BEAT, 4) for b in range(int(45.0 / BEAT) + 1)],
        "visual_snap": {
            "_note": "v6 网格的画面吸附表 (v7 的 96BPM 表已废弃)",
            "track_start": 13.125, "gate_cut": 15.46875, "release_track": 15.9375,
            "measurements": 26.25, "panel_dive": 40.0, "fade_out": 43.4,
        },
        "composition": {
            "base": "v6 engineering underscore (music_v6-engineer.wav)",
            "master": "linear-phase EQ (450Hz -2dB, 5k+ -2.5dB) + static gain + soft limit",
            "arrangement_changed": False,
        },
    }
    with open(os.path.join(TOOLS, "pv_music_cues.json"), "w", encoding="utf-8") as f:
        json.dump(cues, f, ensure_ascii=False, indent=2)
    print("cues -> pv_music_cues.json (128BPM / 切口 15.46875)")


if __name__ == "__main__":
    main()
