# -*- coding: utf-8 -*-
"""PV 配乐的客观检验 —— 代替"用耳朵听"这个我做不到的判断。

═══ 判据随风格换过一次 (2026-09-30, v7 时代) ═══
旧三条 (v2~v6): 拍点对比度 ≥2.5dB / 段落 4.5s 弧线 ≥4dB / 切口。
**其中第一条是为"节奏机器"风格定的假设, 对 v7 的连续管弦流是错的尺子** ——
把用户选的参考片 (群论之美宣传片) 拿来跑同一把尺:
    参考片拍点对比度 -1.46 dB (鼓不在拍上, 它是流淌的), 段落 4.5s 弧线 3.1 dB。
判据把标杆本身都拦下了 → 说明判据错了, 不是音乐错了。

按参考片实测画像重写为:
  ① **起音密度**: 谱通量 onset ≥ 2.5 次/秒 —— 音乐在"走"而不是一块静止的 pad
     (参考片 5.75/s; drone/pad-only 通常 <1)。
  ② **动态呼吸**: 1s 窗 RMS 极差 ≥ 10 dB —— 真正的编曲有起有落
     (参考片 14.7 dB; 一条直线的循环 <6)。
  ③ **闸门切口**: cue 给的切口时刻要有 ≥20dB 真静音 —— 这是我们片子的结构性要求,
     参考片没有闸门所以它在这条上不过 (无关, 它不适用)。
附带打印 (不作门禁): 调性匹配 / 频段占比 (低频主导) / 与参考画像的偏差。

用法: python tools/verify_pv_music.py [wav]
"""
import json
import math
import os
import struct
import sys
import wave

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 可选: python tools/verify_pv_music.py <music.wav>  —— 便于对副本做灵敏度自检
P = (sys.argv[1] if len(sys.argv) > 1
     else os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv", "music_v7-dscore.wav"))
if not os.path.exists(P):
    raise SystemExit("没有 %s —— 先跑 python tools/make_pv_music.py" % P)

CUES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pv_music_cues.json")
try:
    _cues = json.load(open(CUES, encoding="utf-8"))
    BEAT = float(_cues["beat_sec"])
    CUT_AT = float(_cues["gate_cut"])
    print("网格: beat=%.4fs (%.1f BPM) · 切口=%.3fs [from cues]"
          % (BEAT, 60.0 / BEAT, CUT_AT))
except Exception as e:
    # ⚠️ 回退必须大声警告 —— 静默回退曾让本脚本按 128BPM 误判整支 96BPM 的曲子
    print("!! 读 %s 失败 (%s) —— 切口位置未知!" % (CUES, e))
    CUT_AT = None

w = wave.open(P, "rb")
SR = w.getframerate()
n = w.getnframes()
nch = w.getnchannels()
raw = w.readframes(n)
w.close()
mono = []
if nch == 2:
    for i in range(0, len(raw), 4):
        l, r = struct.unpack_from("<hh", raw, i)
        mono.append((l + r) / 2 / 32768.0)
else:
    # ⚠️ 单声道按立体声解析会把时长读成一半、起音密度翻倍 (参考片是单声道导出, 实测踩过)
    for i in range(0, len(raw), 2):
        (v,) = struct.unpack_from("<h", raw, i)
        mono.append(v / 32768.0)

dur = len(mono) / SR
peak = max(abs(x) for x in mono)
rms = math.sqrt(sum(x * x for x in mono) / len(mono))
print("时长 %.3fs / 峰值 %.2f dBFS / 整体 RMS %.1f dBFS" %
      (dur, 20 * math.log10(peak), 20 * math.log10(rms)))


def win_rms(t0, half):
    a = max(0, int((t0 - half) * SR))
    b = min(len(mono), int((t0 + half) * SR))
    if b <= a:
        return 1e-9
    s = 0.0
    for i in range(a, b, 2):
        s += mono[i] * mono[i]
    return math.sqrt(s / max(1, (b - a) // 2))


# ── 判据 ①: 起音密度 (谱通量) ──
# 2048/512 STFT, 正向通量超 75 分位 ×1.6 计一次 onset。
# 证明音乐在走动 (参考 5.75/s), 而不是一块静止的 pad (drone 通常 <1)。
import numpy as np

FR, HOP = 2048, 512
x = np.array(mono)
frames = []
for i in range(0, len(x) - FR, HOP):
    frames.append(np.abs(np.fft.rfft(x[i:i + FR] * np.hanning(FR))))
S = np.array(frames).T
flux = np.maximum(0, np.diff(S, axis=1)).sum(axis=0)
thr = float(np.percentile(flux, 75))
onsets = int((flux > thr * 1.6).sum())
onset_rate = onsets / dur
ok_move = onset_rate >= 2.5

# ── 判据 ②: 动态呼吸 (1s 窗 RMS 极差) ──
# 1s 窗比 4.5s 更能反映编曲的起落 (参考片: 1s 窗 14.7dB, 4.5s 窗只有 3.1dB)。
seg1 = SR
rdb = []
for k in range(len(mono) // seg1):
    s = mono[k * seg1:(k + 1) * seg1]
    rdb.append(20 * math.log10(math.sqrt(sum(v * v for v in s) / len(s)) + 1e-12))
spread = max(rdb) - min(rdb)
ok_breath = spread >= 10.0

# ── 判据 ③: 闸门切口 (cue 时刻 ≥20dB 真静音) ──
drop = 0.0
ok_cut = True
if CUT_AT is not None:
    lo = int((CUT_AT - 0.6) * SR)
    hi = int((CUT_AT + 1.0) * SR)
    win = int(0.03 * SR)
    mins = []
    t = lo
    while t + win < hi:
        s = mono[t:t + win]
        mins.append((math.sqrt(sum(v * v for v in s) / len(s)), t / SR))
        t += win
    around = [m for m in mins if CUT_AT - 0.1 <= m[1] <= CUT_AT + 0.45]
    if around:
        q = min(around)
        ref = win_rms(CUT_AT - 0.55, 0.35)
        drop = 20 * math.log10(ref + 1e-9) - 20 * math.log10(q[0] + 1e-9)
        print("切口 %.2fs: 最静窗 %.3fs (%.1f dBFS) · 相对前段降 %.1f dB"
              % (CUT_AT, q[1], 20 * math.log10(q[0] + 1e-9), drop))
    ok_cut = drop >= 20.0

# ── 附带画像 (不作门禁, 与参考片对照用) ──
spec = np.abs(np.fft.rfft(x)) ** 2
f = np.fft.rfftfreq(len(x), 1 / SR)
total = spec.sum() + 1e-12
low = float(spec[f < 200].sum() / total * 100)
high = float(spec[f >= 2000].sum() / total * 100)
print("画像: 低频 %.1f%% (参考 63.1) · 高频 %.1f%% (参考 7.7) · "
      "1s 动态 %.1f dB (参考 14.7) · onset %.2f/s (参考 5.75)"
      % (low, high, spread, onset_rate))

print("\n=== 判定 ===")
print("  %s① 起音密度 %.2f/s (应 ≥2.5 —— 音乐在走, 不是静止 pad)"
      % ("OK " if ok_move else "!! ", onset_rate))
print("  %s② 动态呼吸 %.1f dB (应 ≥10 —— 有起有落, 不是一条直线)"
      % ("OK " if ok_breath else "!! ", spread))
print("  %s③ 闸门切口 %.1f dB (应 ≥20)" % ("OK " if ok_cut else "!! ", drop))
ok = ok_move and ok_breath and ok_cut
print("%s" % ("通过 —— 结构成立 (好不好听仍需人耳判断)" if ok else "有问题"))
sys.exit(0 if ok else 1)
