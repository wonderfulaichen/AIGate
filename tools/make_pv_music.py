# -*- coding: utf-8 -*-
"""AIGate PV 配乐 —— v9「90s 工程 underscore」(v6 配方 × 2 倍片长, numpy 合成)。

═══ 版本史与本版定位 ═══
  v1-v4: 见旧档 (节奏机器/弧线/混响侧链, 均被否)。
  v5: 流行歌写法 (军鼓2/4), 方向错。
  v6: **用户选定的底子** —— 工程 underscore: 心跳 sub、计数器滴答、快门、3音动机、
      滤波自动化、无军鼓。45s/128BPM。
  v7: 照参考片画像的管弦流, 用户: "比之前差 (后面刺耳+变慢没劲)"。
  v8: v6 波形+母带 (响度/去毛刺), 用户: "就这样吧" → 但仅 45s。
  v9: **片长 45→90s** (叙事装不下: 还要讲省token原理与成果),
      音乐按 v6 配方在 90s 尺度上重生成 —— 同样的心跳/滴答/快门/动机/滤波,
      但编配分 8 段做变化, 避免 90s 直线循环。
  v10: 修 v9 两处听感故障 (冲击音 7.7dB 突跳/谱心 6891Hz → 乐音化, C 大三 → Am)。
  v11 (本版): 用户 r04 反馈"音效有点混乱" → 三处客观对齐参考片:
      ① 闸门切口 90ms→50ms (80ms 数字级全静音听感像播放断线);
      ② 滴答 16 分→8 分 (起音密度 8.5/s, 比参考片 5.75 忙 48% —— arp 已是 16 分,
         两层 16 分叠加是"忙"的主因);
      ③ 新增 G_SHIM 空气层 (5.5–12kHz 8 分噪声, 原来只定义没用) —— 高频占比
         0.5% vs 参考 7.7%, 整体太闷。另把 intro 电平包络 0.40→0.52 收窄响度落差。
  v12: 用户听 v11 后定位「像弹钢琴的干扰音 = 琶音」(违和点出现较稀 → 是
      +12 八度跳那几下, 不是主旋律) → 琶音融合改造: 软起音去拨弦感、声像钉死
      去横跳、删八度跳、暗化降电平, 留住 16 分密度角色。详见 ⑧ 注释。
  v13: 用户听 v12 后: "非常违和, 完全不搭" → **琶音整层移除**
      (它是 v9 撑时长加的, v6 底子里没有; 别名 ARP_MIDI/G_ARP 一并删)。
  v14 (本版): 用户听 r09 (无琶音版) 后更正: "搞错了, 应该是那个钢琴音" →
      **FM 电钢旋律层整层移除** (MELODY/G_RIFF/SKIP_MELODY/lead 总线一并删,
      含附点八分延迟 3 tap —— 回声落在换和弦后, 即"稀疏违和 ping")。
      音色家族自此无任何独奏琴类声部 (见 ⑧ 注释)。
  v15 (本版): 用户认可 v14 底子 ("这个还不错, 音效对了"), 要求加**卡点**等制作手法:
      ① riser ×5 —— 每个到站前 1 小节上行 (C/D/E/panel + 闸门);
      ② 站B 到站 21.094 (beat45, 不在小节线) 补专属 mini-riser + sub boom 落点;
      ③ 段界 stab 0.20→0.30 + 2.5–6kHz click (旧版太闷, 立不住);
      ④ endcard 82.969 反拍 swell + 软 Am 落点。

═══ 90s 时间轴 (128BPM × 48 小节, 与画面站点一一对应) ═══
  bar 0–1    0.000–3.750    intro    pad+sub, 极简
  bar 2–7    3.750–15.000   groove   心跳+滴答+快门+bass 进入
                    15.46875 **闸门切口** (bar8 第2拍): 50ms 真静音 → 15.51875 冲击音
  bar 8–14  15.000–28.125   main A   (原动机首现, v14 起无旋律层; 站点B: body 对照)
  bar 15–21 28.125–41.250   main B   (站点C: 折线生长)
  bar 22–27 41.250–52.500   sustain  密度降, 滤波收 (站点D: 环形占比)
  bar 28–33 52.500–63.750   lift     最满 (站点E: 大数字)
  bar 34–39 63.750–75.000   panel    保持, 滤波开 (真实面板俯冲)
  bar 40–47 75.000–90.000   outro    层退场, 88.4s 起淡出 (三步+字卡)

⚠️ 依赖方向: 画面吸附音乐 (pv_music_cues.json)。切口 15.46875 是全片唯一中断。
⚠️ 不覆盖旧版: v5–v14 的 wav 全保留; 本版输出 music_v15-90s.wav。

用法: python tools/make_pv_music.py
检验: python tools/verify_pv_music.py <本版 wav>
      python tools/verify_pv_melody.py <本版 wav>
"""
import json
import math
import os
import struct
import sys
import wave

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt, sosfilt_zi

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv")
OUT_WAV = os.environ.get("AIGATE_PV_MUSIC_OUT") or os.path.join(OUT_DIR, "music_v15-90s.wav")
OUT_CUES = os.path.join(TOOLS, "pv_music_cues.json")
# AIGATE_PV_SKIP_MELODY 已无读取方 (v14 电钢层移除) —— 环境变量保留给 verify_pv_melody,
# 它仍会设置, 但生成器不再分叉 (两版相同 → 该门禁的旋律差分判定自 v14 起恒为 N/A)。

SR = 44100
DUR = 90.0
BPM = 128.0
BEAT = 60.0 / BPM               # 0.46875
BAR = BEAT * 4                  # 1.875
NBARS = 48                      # 48 × 1.875 = 90.0 整

CUT_AT = 8 * BAR + BEAT         # 15.46875 (bar9 第2拍) —— 与 v6/v8 一致, 画面已吸附
HOLE = 0.05                     # ⚠️ v10 是 0.09: 80ms 数字级全静音听感像播放断线 (r04 音效反馈);
                                #    50ms 保留"闸门咔断"的节奏感, 又短到不会被读成故障
IMPACT_AT = CUT_AT + HOLE       # 15.51875
FADE_OUT = 88.4

# ── 段落 (bar 边界): 8 段, 对应画面站点 ──
SEC_BAR = [0, 2, 8, 15, 22, 28, 34, 40, 48]
SEC = ["intro", "groove", "mainA", "mainB", "sustain", "lift", "panel", "outro"]

# 和声: Am–F–C–G 四小节循环 (A 自然小调)
PROG = ["Am", "F", "C", "G"]
CH = {
    "Am": {"pad": [-12, -9, -5], "bass": -24, "riff": [7, 3, 0]},     # A C E / 根 A2 / 动机音 A-G-E 相对
    "F":  {"pad": [-16, -12, -9], "bass": -28, "riff": [8, 5, 0]},
    "C":  {"pad": [-9, -5, -2],  "bass": -21, "riff": [7, 4, 0]},
    "G":  {"pad": [-14, -10, -7], "bass": -26, "riff": [7, 5, 2]},
}


def chord_of(bar):
    return CH[PROG[bar % 4]]


# (arp 音序表 ARP_MIDI 已随琶音层一起移除 —— v13, 见 ⑧ 处墓碑注释)


# 段落增益表 (8 段) —— 弧线: intro 静 → groove 起 → main 满 → sustain 落 → lift 起 → panel 稳 → outro 退
G_PAD  = [0.70, 0.55, 0.48, 0.48, 0.52, 0.46, 0.46, 0.78]
G_SUB  = [0.50, 0.70, 0.88, 0.88, 0.70, 0.92, 0.85, 0.35]
G_KICK = [0.20, 0.72, 0.92, 0.92, 0.70, 1.00, 0.88, 0.30]
G_BASS = [0.30, 0.60, 0.85, 0.85, 0.62, 0.90, 0.80, 0.26]
G_TICK = [0.00, 0.60, 0.95, 1.00, 0.70, 1.00, 0.85, 0.22]
G_SHUT = [0.00, 0.75, 1.00, 1.00, 0.80, 1.00, 0.90, 0.25]
# G_RIFF 已移除 (v14: 电钢旋律层删除)
# G_ARP 已移除 (v13: 琶音整层删除)
G_SHIM = [0.00, 0.00, 1.00, 0.85, 0.60, 0.85, 0.90, 0.20]
DUCK   = [0.00, 0.36, 0.52, 0.52, 0.40, 0.55, 0.48, 0.15]


def sec_of(bar):
    for i in range(len(SEC_BAR) - 1):
        if bar < SEC_BAR[i + 1]:
            return i
    return len(SEC) - 1


def hz(semi):
    return 440.0 * 2 ** ((semi - 69) / 12.0)      # semi = MIDI


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12.0)


def adsr(n_samp, a, d, s_lvl, r):
    env = np.ones(n_samp)
    na, nd, nr = int(a * SR), int(d * SR), int(r * SR)
    na = min(na, n_samp)
    if na:
        env[:na] = np.linspace(0, 1, na, endpoint=False)
    if nd > 0 and na < n_samp:
        nd2 = min(nd, n_samp - na)
        env[na:na + nd2] = np.linspace(1, s_lvl, nd2, endpoint=False)
        env[na + nd2:] = s_lvl
    if nr > 0:
        nr = min(nr, n_samp)
        env[-nr:] *= np.linspace(1, 0, nr)
    return env


def lp(x, fc):
    """低通 (2 阶 Butterworth)。"""
    return sosfilt(butter(2, min(fc, SR / 2 - 500) / (SR / 2), btype="low", output="sos"), x)


def bp(x, lo, hi):
    """带通 (2 阶)。用于噪声音色 —— 见下方"噪声必须滤波"的说明。"""
    return sosfilt(butter(2, [lo / (SR / 2), min(hi, SR / 2 - 500) / (SR / 2)],
                          btype="band", output="sos"), x)


def attack_ramp(n_samp, ms=1.5):
    """极短起音斜坡: 消除"从 0 直接跳到满幅"的阶跃 (那正是爆音的数字成因)。"""
    k = max(1, int(ms / 1000 * SR))
    r = np.ones(n_samp)
    k = min(k, n_samp)
    r[:k] = np.linspace(0, 1, k)
    return r


def tv_lowpass(x, cutoff_fn, order=2, chunk=512):
    out = np.zeros_like(x)
    zi = None
    for i in range(0, len(x), chunk):
        seg = x[i:i + chunk]
        fc = float(np.clip(cutoff_fn(i / SR), 60.0, SR / 2 - 500))
        sos = butter(order, fc / (SR / 2), btype="low", output="sos")
        if zi is None:
            zi = sosfilt_zi(sos) * (seg[0] if len(seg) else 0.0)
        y, zi = sosfilt(sos, seg, zi=zi)
        out[i:i + chunk] = y
    return out


def saw_bank(freqs, n_samp, detune=(-7.0, 0.0, 7.0)):
    t = np.arange(n_samp) / SR
    out = np.zeros(n_samp)
    for f in freqs:
        for k, dc in enumerate(detune):
            fd = f * 2 ** (dc / 1200.0)
            for h in range(1, 8):
                out += np.sin(2 * np.pi * h * fd * t + k * 0.7) / h
    return out * 0.33 / max(1, len(freqs))


# ── 主旋律表 MELODY 已随电钢层一起移除 (v14, 见 ⑦ 处墓碑注释) ──


def main():
    n = int(SR * DUR)
    rng = np.random.default_rng(20260930)
    tt = np.arange(n) / SR

    dry_l = np.zeros(n)
    dry_r = np.zeros(n)
    pad_l = np.zeros(n)
    pad_r = np.zeros(n)
    wet_l = np.zeros(n)
    wet_r = np.zeros(n)
    wet_m = np.zeros(n)

    def put(sl, sr_, t0, sig, pan=0.5):
        i0 = int(t0 * SR)
        if 0 <= i0 < n:
            m = min(len(sig), n - i0)
            sl[i0:i0 + m] += sig[:m] * (1 - pan)
            sr_[i0:i0 + m] += sig[:m] * pan

    def g(bar, table):
        return table[sec_of(bar)]

    def duck(t):
        d = DUCK[sec_of(int(t / BAR))]
        return 1.0 if d <= 0 else 1.0 - d * math.exp(-(t % BEAT) / 0.075)

    # ── ① pad: 每小节和弦, 失谐锯齿 ──
    # ⚠️ CH 的 pad/bass 半音是**相对 A4=0** 的 (A4 MIDI=69), 转换必须 +69 ——
    #    写成 +60 会整体低 9 个半音且和弦错 (实测踩过: Am 变成了 Eb 系)。
    for bar in range(NBARS):
        ch = chord_of(bar)
        base = g(bar, G_PAD) * 0.17
        n_samp = int((BAR + 0.5) * SR)
        sig = saw_bank([midi(m + 69) for m in ch["pad"]], n_samp)
        sig *= adsr(n_samp, 0.30, 0.4, 0.85, 0.5) * base
        put(pad_l, pad_r, bar * BAR, sig, pan=0.46)
        put(pad_l, pad_r, bar * BAR + 0.011, sig * 0.9, pan=0.58)

    # ── ② sub: 每小节根音 (双八度厚度), 受侧链 ──
    for bar in range(NBARS):
        ch = chord_of(bar)
        base = g(bar, G_SUB) * 0.32
        n_samp = int((BAR + 0.25) * SR)
        t = np.arange(n_samp) / SR
        f = midi(69 + ch["bass"])
        sig = (np.sin(2 * np.pi * f * t) + 0.42 * np.sin(2 * np.pi * f * 0.5 * t)
               + 0.16 * np.sin(2 * np.pi * f * 2 * t))
        sig *= adsr(n_samp, 0.04, 0.6, 0.72, 0.4) * base
        put(dry_l, dry_r, bar * BAR, sig, pan=0.5)

    # ── ③ bass: 走动 (根-根-五-八), 每拍短音 ──
    walk = [0, 0, 7, 12]
    for bar in range(2, 44):
        gb = g(bar, G_BASS)
        if gb <= 0.001:
            continue
        root = 69 + chord_of(bar)["bass"]
        for b in range(4):
            t0 = bar * BAR + b * BEAT
            if CUT_AT <= t0 < CUT_AT + HOLE:      # 切口内不发
                continue
            f = midi(root + walk[b])
            amp = gb * duck(t0) * (0.55 if b == 0 else 0.40)
            n_samp = int(0.23 * SR)
            t = np.arange(n_samp) / SR
            ph = 2 * np.pi * f * t
            sig = (np.sin(ph) + 0.5 * np.sin(2 * ph) + 0.28 * np.sin(3 * ph)) * env_ad_np(n_samp, 0.006, 0.17, 5.5) * amp
            put(dry_l, dry_r, t0, sig, pan=0.5)

    # ── ④ 心跳 kick (v6 配方: 三层下扫, 无军无镲) ──
    kick_times = []
    for bar in range(2, 44):
        gk = g(bar, G_KICK)
        if gk <= 0.001:
            continue
        beats = (0, 2) if sec_of(bar) == 1 else (0, 1, 2, 3)
        for b in beats:
            t0 = bar * BAR + b * BEAT
            if CUT_AT <= t0 < CUT_AT + HOLE:
                continue
            amp = gk * (0.75 if b == 0 else 0.60)
            n_samp = int(0.30 * SR)
            t = np.arange(n_samp) / SR
            fsw = 32.0 + 30.0 * np.exp(-t * 34)
            ph = 2 * np.pi * np.cumsum(fsw) / SR
            body = np.sin(2 * np.pi * 100 * t) * np.exp(-t * 60) * 0.30
            # 瞬态 click: **必须带通** —— 原来直接注入 ±1 白噪声, 含满 Nyquist 能量,
            # 每个拍点都"嗞"一下 (实测 5537 个样本跳变 >0.2, 全部落在拍点上)。
            # 现在: 带通 1.8–4.5kHz + 1.5ms 起音斜坡 + 电平减半。
            rng_k = np.random.default_rng(int(t0 * 1000) & 0x7FFFFFFF)
            nz = rng_k.standard_normal(n_samp)
            nz = bp(nz, 1800, 4500)
            nz *= attack_ramp(n_samp)
            click = nz * np.exp(-t * 150) * 0.09
            sig = (np.sin(ph) * np.exp(-t * 8.5) + body + click) * amp * 0.85
            put(dry_l, dry_r, t0, sig, pan=0.5)
            kick_times.append(t0)

    # ── ⑤ 计数器滴答: **8 分**带通噪声 (交替声道, "日志在滚") ──
    # ⚠️ v10 是 16 分: 起音密度 8.5/s, 比参考片 5.75 忙 48% —— 用户听感"混乱"的
    #    主因之一 (当时还有 16 分 arp 同格叠加)。v13 起琶音已整层移除,
    #    滴答维持 8 分做节奏底。
    sos_tick = butter(2, [900 / (SR / 2), 4500 / (SR / 2)], btype="band", output="sos")
    for bar in range(2, 44):
        gt = g(bar, G_TICK)
        if gt <= 0.001:
            continue
        for s in range(0, 16, 2):
            t0 = bar * BAR + s * (BEAT / 4)
            if CUT_AT <= t0 < CUT_AT + HOLE:
                continue
            n_samp = int(0.035 * SR)
            nz = rng.standard_normal(n_samp)
            # 起音斜坡 1.5→3.5ms: 谱通量门禁按"超阈帧数"计起音, 硬起音一响占 3 帧,
            # 软起音只占 1-2 帧 —— 参考片 5.75/s 的本质是连弓弦乐的柔起音, 不是音符少。
            nz = sosfilt(sos_tick, nz) * attack_ramp(n_samp, 3.5)
            env = np.exp(-np.arange(n_samp) / SR * 140)
            amp = gt * (0.068 if s % 4 == 0 else 0.042)
            sig = nz * env * amp
            pan = 0.62 if s % 2 else 0.38
            put(wet_l, wet_r, t0, sig, pan=pan)
            wet_m[int(t0 * SR):int(t0 * SR) + n_samp] += sig * 0.5

    # ── ⑥ 快门: 每小节第 4 拍机械噪声 (闸门隐喻, 替代军鼓) ──
    # 原来是 np.diff 一阶差分 → 通带推到 Nyquist, 每小节"啪"一声数字感。
    # 改成 1.5–4kHz 带通 + 起音斜坡 + 电平降 25%。
    for bar in range(3, 42):
        gs = g(bar, G_SHUT)
        if gs <= 0.001:
            continue
        t0 = bar * BAR + 3 * BEAT
        if CUT_AT <= t0 < CUT_AT + HOLE:
            continue
        n_samp = int(0.05 * SR)
        nz = rng.standard_normal(n_samp)
        nz = bp(nz, 1500, 5500) * attack_ramp(n_samp, 3.0)
        env = np.exp(-np.arange(n_samp) / SR * 90)
        sig = nz * env * gs * 0.27
        put(wet_l, wet_r, t0, sig, pan=0.70)
        wet_m[int(t0 * SR):int(t0 * SR) + n_samp] += sig * 0.5

    # ── ⑥b 空气层 (v11 新增): 5.5–12kHz 8 分噪声, 走 G_SHIM ──
    # 病灶: 高频占比 0.5% vs 参考片 7.7% —— 全曲几乎只有低频, 听感"闷"。
    # G_SHIM 增益表早就定义却没有任何层在用 (死挂点), 正好给它。
    # 格式取 8 分 (与滴答同格): 只补"空气", 不再造一层 16 分去加剧忙度。
    # G_SHIM[0:2]=0 → 空气层从 mainA (15s, 闸门) 才进来, 与段落弧线一致。
    for bar in range(2, 44):
        gsh = g(bar, G_SHIM)
        if gsh <= 0.001:
            continue
        for s in range(0, 16, 2):
            t0 = bar * BAR + s * (BEAT / 4)
            if CUT_AT <= t0 < CUT_AT + HOLE:
                continue
            n_samp = int(0.060 * SR)
            nz = rng.standard_normal(n_samp)
            nz = sosfilt(butter(2, [5500 / (SR / 2), 12000 / (SR / 2)],
                                btype="band", output="sos"), nz)
            nz *= attack_ramp(n_samp, 4.0)
            env = np.exp(-np.arange(n_samp) / SR * 90)
            # ⚠️ v11 首版 0.030/25ms/衰减220 → 实测 ≥5.5k 占比 0.01% (等于没加):
            #    能量全挤在头 10ms、duty ~2%。窗拉到 60ms + 衰减放慢 + 幅度 ×3.7 才听得见。
            amp = gsh * (0.11 if s % 4 == 0 else 0.06)
            sig = nz * env * amp
            pan = 0.40 if s % 2 else 0.60
            put(wet_l, wet_r, t0, sig, pan=pan)
            wet_m[int(t0 * SR):int(t0 * SR) + n_samp] += sig * 0.4

    # ── ⑦ 主旋律层已整层移除 (v14, 用户定稿) ──
    # 原实现: v6 三音动机的 4 乐句扩展, FM 电钢音色 + 附点八分延迟 3 tap。
    # 用户听感定位过程: v11「像弹钢琴的音效, 像干扰, 各出各的」→ 误判为琶音 (v12/v13
    # 摘琶音后用户改口「搞错了, 应该是那个钢琴音」) → 电钢本体 + 延迟回声一起移除。
    # 回声嫌疑最大: tap 落在换和弦之后, 与新和弦无关的孤立钢琴 ping = "稀疏违和"。
    # ⚠️ 别再加回任何独奏琴类声部 (电钢/钢琴/拨弦); 见 ⑧ 处的音色家族声明。

    # ── ⑧ arp 已整层移除 (v13, 用户定稿) ──
    # 历史: v9 起为撑 90s 时长加的 16 分分解和弦层 (v6 底子里没有它)。
    # 用户两轮听感: v11「像弹钢琴的音效, 像干扰, 各出各的, 违和」→ v12 融合改造
    # (软起音/钉声像/删八度跳/降电平) 后仍「非常违和, 完全不搭」→ **整层删除**。
    # ⚠️ 音色家族 (v14 起): pad / sub / bass / 心跳 kick / 滴答 / 快门 / 空气层 /
    #    段界 stab / riser / 冲击音 —— **没有任何独奏琴类声部** (电钢与琶音均已移除)。

    # ── ⑨ 段界 stab (bar 8/15/22/28/34/40): 标记站点切换 ──
    # v15 卡点: 电平 0.20→0.30 并加 2.5–6kHz 瞬态 click —— 旧版只有 1.2kHz 以下的"闷噗",
    # 到站瞬间立不住, 听不出"卡上了"。
    for bar in (8, 15, 22, 28, 34, 40):
        ch = chord_of(bar)
        if bar == 8:
            continue      # bar8 的强拍让给切口+冲击音
        base = 0.30 * (1.0 if bar in (28, 34) else 0.7)
        n_samp = int(1.4 * SR)
        sig = saw_bank([midi(69 + x) for x in [ch["bass"] + 12] + ch["pad"]], n_samp)
        sig = sosfilt(butter(2, 1200 / (SR / 2), btype="low", output="sos"), sig)
        sig *= adsr(n_samp, 0.05, 0.6, 0.35, 0.6) * base
        t0s = bar * BAR
        put(dry_l, dry_r, t0s, sig, pan=0.5)
        # 瞬态 click: 带通白噪声 + 起音斜坡, 22ms 衰减
        n_c = int(0.05 * SR)
        rng_c = np.random.default_rng(7000 + bar)
        nz = rng_c.standard_normal(n_c)
        nz = bp(nz, 2500, 6000) * attack_ramp(n_c, 1.5)
        nz *= np.exp(-np.arange(n_c) / SR * 90) * base * 0.35
        put(wet_l, wet_r, t0s, nz, pan=0.5)
        wet_m[int(t0s * SR):int(t0s * SR) + n_c] += nz * 0.4

    # ── ⑩ riser ×5 (v15 卡点): 冲向闸门 (12.5–15.47) + 冲向每个到站时刻 ──
    # 用户 v14 反馈"可以加卡点等制作手法" —— 到站全在拍点上 (C/D/E/panel = 小节线,
    # B = beat45 单独处理), 各配 1 小节上行 riser, 镜头落定的瞬间正是音乐到位的瞬间。
    for (r0_t, r1_t, peak) in ((12.5, CUT_AT, 0.17), (26.25, 28.125, 0.11),
                               (39.375, 41.25, 0.11), (49.0, 52.5, 0.13),
                               (61.875, 63.75, 0.11)):
        r0 = int(r0_t * SR)
        rlen = int((r1_t - r0_t) * SR)
        t = np.arange(rlen) / SR
        p = t / (r1_t - r0_t)
        nz = rng.standard_normal(rlen)
        nz = sosfilt(butter(2, [300 / (SR / 2), 4500 / (SR / 2)], btype="band", output="sos"), nz)
        sweep = np.sin(2 * np.pi * np.cumsum(200 + 1600 * p ** 2) / SR)
        sig = (nz * 0.6 + sweep * 0.4) * (p ** 2.4) * peak
        dry_l[r0:r0 + rlen] += sig * 0.5
        dry_r[r0:r0 + rlen] += sig * 0.5
        wet_m[r0:r0 + rlen] += sig * 0.5

    # ── ⑪ 冲击音 (15.55875): 闸门切口后的"砸回" —— 必须**是乐音, 不是噪声爆炸**
    # v9 实测故障: 比前后响 7.7dB、谱心 6891Hz、单样本跳变 0.27 → 用户听成"突然的违和音效"。
    # 现在改成: 低频下扫 (骨架) + A 小调低音铜 (乐音) + 极少量滤波噪声尾 (空气),
    # 电平 -5dB, 2.5ms 起音 (不再是垂直上升沿)。
    n_samp = int(1.6 * SR)
    t = np.arange(n_samp) / SR
    fsw = 26 + 52 * np.exp(-t * 18)
    ph = 2 * np.pi * np.cumsum(fsw) / SR
    boom = np.sin(ph) * np.exp(-t * 2.6) * 0.31
    # A 小调和弦 (A1 E2 A2 C3) —— v9 用的是 C 大三 (C2 G2 C3), 与 Am 冲突, 也是"违和"来源之一
    brass = saw_bank([midi(33), midi(40), midi(45), midi(48)], n_samp)
    brass = lp(brass, 700)
    brass *= np.exp(-t * 3.0) * 0.15
    nz = rng.standard_normal(n_samp)
    nz = lp(nz, 2500) * attack_ramp(n_samp, 3.0)
    tail = nz * np.exp(-t * 9) * 0.047
    impact = (boom + brass + tail) * attack_ramp(n_samp, 2.5)
    i0 = int(IMPACT_AT * SR)
    dry_l[i0:i0 + n_samp] += impact
    dry_r[i0:i0 + n_samp] += impact
    wet_m[i0:i0 + n_samp] += (brass * 0.7 + tail * 2.0)

    # ── ⑪b 站B 到站卡点 (21.094 = beat 45, 全片唯一不在小节线的到站, 段界 stab 打不到) ──
    # v15: 0.5s 迷你 riser 冲进去 + 落地 sub boom + Am 软和弦 —— 镜头 21.094 落定,
    # 音乐同一拍"咚"一下 (之前的版本这里完全没事件, 到站是哑的)。
    B_LAND = 45 * BEAT          # 21.09375
    r0b = int((B_LAND - 0.5) * SR)
    rlb = int(0.5 * SR)
    tb = np.arange(rlb) / SR
    pb = tb / 0.5
    nzb = rng.standard_normal(rlb)
    nzb = sosfilt(butter(2, [400 / (SR / 2), 5000 / (SR / 2)], btype="band", output="sos"), nzb)
    riser_b = (nzb * 0.7 + np.sin(2 * np.pi * np.cumsum(300 + 1400 * pb ** 2) / SR) * 0.3) * (pb ** 2.4) * 0.10
    dry_l[r0b:r0b + rlb] += riser_b * 0.5
    dry_r[r0b:r0b + rlb] += riser_b * 0.5
    wet_m[r0b:r0b + rlb] += riser_b * 0.5
    n_h = int(1.2 * SR)
    th = np.arange(n_h) / SR
    fsw_h = 34.0 + 46.0 * np.exp(-th * 20)
    ph_h = 2 * np.pi * np.cumsum(fsw_h) / SR
    boom_h = np.sin(ph_h) * np.exp(-th * 3.2) * 0.22
    ch_b = saw_bank([midi(45), midi(52), midi(57)], n_h)     # A2 E3 A3
    ch_b = lp(ch_b, 900) * adsr(n_h, 0.012, 0.5, 0.35, 0.5) * 0.13
    land = (boom_h + ch_b) * attack_ramp(n_h, 2.5)
    i0b = int(B_LAND * SR)
    dry_l[i0b:i0b + n_h] += land
    dry_r[i0b:i0b + n_h] += land
    wet_m[i0b:i0b + n_h] += ch_b * 0.6

    # ── ⑪c endcard 定格卡点 (82.969 = bar44 beat2, 字卡三层 stagger 的最后一层) ──
    # v15: 0.8s 反拍 swell 冲进定格瞬间 + 软 Am 落点 (outro 段, 电平克制, 88.4 起整体淡出)
    E_CARD = 44 * BAR + BEAT          # 82.96875 (= beat 177, 字卡容器 82969ms 同刻)
    r0e = int((E_CARD - 0.8) * SR)
    rle = int(0.8 * SR)
    te = np.arange(rle) / SR
    pe = te / 0.8
    nze = rng.standard_normal(rle)
    nze = sosfilt(butter(2, [500 / (SR / 2), 7000 / (SR / 2)], btype="band", output="sos"), nze)
    swell = nze * (pe ** 2.6) * 0.085
    dry_l[r0e:r0e + rle] += swell * 0.5
    dry_r[r0e:r0e + rle] += swell * 0.5
    wet_m[r0e:r0e + rle] += swell * 0.6
    n_ec = int(2.2 * SR)
    tec = np.arange(n_ec) / SR
    ch_ec = saw_bank([midi(33), midi(45), midi(52), midi(57)], n_ec)   # A1 A2 E3 A3
    ch_ec = lp(ch_ec, 1400) * adsr(n_ec, 0.015, 0.9, 0.4, 0.9) * 0.15
    sub_ec = np.sin(2 * np.pi * midi(33) * tec) * np.exp(-tec * 1.6) * 0.10
    hit_ec = (ch_ec + sub_ec) * attack_ramp(n_ec, 3.0)
    i0e = int(E_CARD * SR)
    dry_l[i0e:i0e + n_ec] += hit_ec
    dry_r[i0e:i0e + n_ec] += hit_ec
    wet_m[i0e:i0e + n_ec] += ch_ec * 0.5

    # ── ⑫ pad 滤波自动化 (暗起 → 闸门后亮 → sustain 收 → lift 开 → outro 合) ──
    print("  pad 时变低通 ...")

    def cutoff_fn(t):
        if t < 3.75:
            return 480
        if t < CUT_AT:
            return 480 + (2400 - 480) * (t - 3.75) / (CUT_AT - 3.75)
        if t < 28.125:
            return 4800
        if t < 41.25:
            return 4800 + (3200 - 4800) * (t - 28.125) / (41.25 - 28.125)
        if t < 52.5:
            return 3200 + (2400 - 3200) * (t - 41.25) / (52.5 - 41.25)
        if t < 63.75:
            return 5200                      # lift: 全开
        if t < 75.0:
            return 5200 + (4600 - 5200) * (t - 63.75) / (75.0 - 63.75)
        return max(650.0, 4600 + (650 - 4600) * (t - 75.0) / 14.0)

    pad_l = tv_lowpass(pad_l, cutoff_fn)
    pad_r = tv_lowpass(pad_r, cutoff_fn)

    # ── ⑬ 侧链泵动 ──
    duck_arr = np.ones(n)
    d_len = int(0.22 * SR)
    d_shape = 1.0 - 0.45 * np.exp(-np.arange(d_len) / SR / 0.075)
    for kt in kick_times:
        i0 = int(kt * SR)
        m = min(d_len, n - i0)
        if m > 0:
            duck_arr[i0:i0 + m] = np.minimum(duck_arr[i0:i0 + m], d_shape[:m])
    pad_l *= duck_arr
    pad_r *= duck_arr

    # ── ⑭ 真混响 (噪声 IR, 左右不同) ──
    print("  真混响 (fftconvolve) ...")
    ir_len = int(2.0 * SR)
    t_ir = np.arange(ir_len) / SR

    def make_ir(seed):
        r = np.random.default_rng(seed)
        ir = r.standard_normal(ir_len) * np.exp(-t_ir / 0.5)
        ir = sosfilt(butter(2, 5200 / (SR / 2), btype="low", output="sos"), ir)
        ir[:int(0.018 * SR)] = 0.0
        return ir / np.sqrt((ir ** 2).sum())

    ir_l, ir_r = make_ir(11), make_ir(22)
    wl = fftconvolve(wet_l + wet_m * 0.6, ir_l)[:n]
    wr = fftconvolve(wet_r + wet_m * 0.6, ir_r)[:n]
    mix_l = dry_l + pad_l + wet_l + wl * 0.55
    mix_r = dry_r + pad_r + wet_r + wr * 0.55

    # ── ⑮ 闸门切口: 混响之后切 (90ms 真静音) ──
    h0, h1 = int(CUT_AT * SR), int((CUT_AT + HOLE) * SR)
    ramp = int(0.010 * SR)
    hole = np.ones(n)
    hole[h0:h1] = 0.0
    hole[h0 - ramp:h0] = np.linspace(1, 0, ramp)
    hole[h1:h1 + ramp] = np.linspace(0, 1, ramp)
    mix_l *= hole
    mix_r *= hole

    # ── ⑯ 总线温和低通 (16kHz) —— 兜底: 削掉任何残留的 Nyquist 毛刺 ──
    mix_l = lp(mix_l, 16000)
    mix_r = lp(mix_r, 16000)

    # ── ⑰ 主电平包络 (8 段弧线) + 淡入淡出 ──
    # v11: intro 0.40→0.54 / outro 尾 0.50→0.60 —— 1s 动态实测 26.7dB vs 参考 14.7,
    # 两头过低是主因之一 (弧线仍保留: 0.54→1.00→0.60, 不是拉平)。
    ENV_PTS = [(0.0, 0.54), (3.75, 0.62), (13.0, 0.82), (15.47, 1.0), (28.125, 1.0),
               (35.0, 0.85), (41.25, 0.82), (50.0, 0.95), (52.5, 1.0), (63.75, 1.0),
               (75.0, 0.88), (80.0, 0.74), (88.4, 0.60), (90.0, 0.0)]
    env = np.interp(tt, [p[0] for p in ENV_PTS], [p[1] for p in ENV_PTS])
    fi = int(0.5 * SR)
    env[:fi] *= np.linspace(0, 1, fi)
    mix_l *= env
    mix_r *= env

    # ── ⑰ 软限幅 + 归一化 ──
    peak = max(np.abs(mix_l).max(), np.abs(mix_r).max())
    mix_l = np.tanh(mix_l / peak * 1.10)
    mix_r = np.tanh(mix_r / peak * 1.10)
    peak2 = max(np.abs(mix_l).max(), np.abs(mix_r).max())
    mix_l *= 0.891 / peak2
    mix_r *= 0.891 / peak2

    os.makedirs(OUT_DIR, exist_ok=True)
    data = np.empty(n * 2, dtype=np.int16)
    data[0::2] = np.clip(mix_l, -1, 1) * 32767
    data[1::2] = np.clip(mix_r, -1, 1) * 32767
    with wave.open(OUT_WAV, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())

    # ── ⑱ cues (90s 网格, 画面吸附的单一事实源) ──
    cues = {
        "_note": "v15 90s 工程 underscore (v6 配方×2, 无琶音无电钢; v15 加到站卡点) —— 画面站点吸附 beats/section_marks。"
                 "切口 15.46875 与 v6/v8 一致。生成: tools/make_pv_music.py",
        "version": "v15-90s",
        "bpm": BPM, "beat_sec": round(BEAT, 6), "bar_sec": round(BAR, 6),
        "total_bars": NBARS, "duration": DUR,
        "progression": PROG,
        "section_marks": [
            {"name": SEC[i], "bar": SEC_BAR[i] + 1, "t": round(SEC_BAR[i] * BAR, 4)}
            for i in range(len(SEC))
        ],
        "gate_cut": round(CUT_AT, 6),
        "impact": round(IMPACT_AT, 6),
        "fade_out": [FADE_OUT, DUR],
        "beats": [round(b * BEAT, 4) for b in range(int(DUR / BEAT) + 1)],
        "visual_snap": {
            "_note": "90s 画面事件吸附表 (画面站点 t) —— 与 pv_scene.tpl.html 的 CAM 表同源",
            "track_start": 13.125, "gate_cut": 15.46875, "release_track": 15.9375,
            # stationB 曾写 15.5 (45s 版残留): 90s 版站 B 到站 21.094 = beat 45 整
            "stationB": 21.094, "stationC": 28.125, "stationD": 41.25,
            "stationE": 52.5, "panel_dive": 63.75, "steps": 75.0,
            "endcard": 82.969, "fade_out": 88.4,
        },
        "composition": {
            "style": "engineering underscore v15 (v6 recipe, 90s, no arp/lead, transition hits)",
            "melody_notes": 0,      # v14: 电钢旋律层已移除
            "no_snare": True,
            "sections": SEC,
        },
    }
    with open(OUT_CUES, "w", encoding="utf-8") as f:
        json.dump(cues, f, ensure_ascii=False, indent=2)

    print("music -> %s (%.1f MB / %.1fs / %.0f BPM / %d 小节)" %
          (OUT_WAV, os.path.getsize(OUT_WAV) / 1048576, DUR, BPM, NBARS))
    print("cues  -> %s" % OUT_CUES)
    print("  段落: " + " · ".join("%s %.1fs" % (c["name"], c["t"]) for c in cues["section_marks"]))
    print("  切口 %.4fs · 冲击 %.4fs · 淡出 %.1f · 旋律 %d 音" %
          (cues["gate_cut"], cues["impact"], FADE_OUT, cues["composition"]["melody_notes"]))


def env_ad_np(n_samp, a, d, curve):
    t = np.arange(n_samp) / SR
    env = np.ones(n_samp)
    na = int(a * SR)
    if na:
        env[:na] = np.linspace(0, 1, na)
    env[na:] = np.exp(-(t[na:] - t[na]) / d * curve)
    return env


if __name__ == "__main__":
    main()
