# -*- coding: utf-8 -*-
"""用**差分法**验证主旋律真的存在且真的在走 —— 代替"用耳朵听"这个我做不到的判断。

为什么不能直接在混音上数音高变化:
  试过。做法是高通后逐帧自相关求基频, 数音高变化次数 —— **它是假的**:
  拿"去掉旋律"的副本做灵敏度自检, 它照样通过 (120 次 vs 正式版 117 次)。
  因为它量到的其实是自相关抖动 + arp 的音高跳动, 跟有没有旋律没关系。

差分法为什么成立:
  让生成器跑两遍 —— 一遍正常, 一遍置 AIGATE_PV_SKIP_MELODY=1 跳过主旋律, 其余全同。
  两版逐样本相减, 剩下的**就是旋律本身加它的延迟回声** (其他层在两次里逐样本一致,
  相减为 0)。在这条残差上测音高变化, 才是真的在测旋律。

判据:
  ① 残差中"有效音高帧"占窗口比例 ≥ 25%  —— 旋律确实响着 (不是被埋掉)
  ② 残差中音高变化次数 ≥ 25            —— 旋律确实在走 (不是一条长音)
  ③ 残差能量不应过小 (RMS ≥ -40 dBFS)   —— 旋律在混音里听得见

⚠️ 这仍然只证明"有旋律线且它在动", **证明不了"好听"** —— 那不归机器判。

用法: python tools/verify_pv_melody.py
耗时: 约 40s (要重跑一遍生成器做基线)
"""
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
MAIN = (os.path.abspath(sys.argv[1]) if len(sys.argv) > 1
        else os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv", "music.wav"))

WIN_A, WIN_B = 15.0, 42.0      # 旋律所在窗口 (15.0s 主段起, 42s 收)


def load_mono(path):
    w = wave.open(path, "rb")
    n = w.getnframes()
    raw = w.readframes(n)
    w.close()
    out = [0.0] * n
    for i in range(n):
        l, r = struct.unpack_from("<hh", raw, i * 4)
        out[i] = (l + r) / 2 / 32768.0
    return out


def pitch_stats(x, sr, a, b):
    """在 [a,b) 窗口内数: 有效音高帧 / 音高变化次数 / 残差 RMS。"""
    hp = [x[i] - 2 * x[i - 1] + x[i - 2] for i in range(2, len(x), 4)]
    srh = sr // 4
    FR, HOP = 512, 512
    lmin, lmax = int(srh / 900), int(srh / 250)
    pcs = []
    i = int(a * srh)
    end = min(len(hp) - FR, int(b * srh))
    total = 0
    while i < end:
        total += 1
        fr = hp[i:i + FR]
        e = sum(v * v for v in fr)
        if e < 1e-9:
            i += HOP
            continue
        best, bl = 0.0, 0
        for lag in range(lmin, lmax):
            s = 0.0
            for k in range(0, FR - lag, 2):
                s += fr[k] * fr[k + lag]
            if s > best:
                best, bl = s, lag
        if bl and best / (e * 0.5) > 0.30:
            pcs.append(round(12 * math.log2((srh / bl) / 440.0)) % 12)
        i += HOP
    changes = sum(1 for p, q in zip(pcs, pcs[1:]) if p != q)
    seg = x[int(a * sr):int(b * sr)]
    rms = math.sqrt(sum(v * v for v in seg) / max(1, len(seg)))
    return len(pcs), total, changes, rms


def main():
    if not os.path.exists(MAIN):
        raise SystemExit("没有 music.wav —— 先跑 python tools/make_pv_music.py")
    # v14 N/A 分支: 生成器在 cues 里显式声明 melody_notes=0 = 电钢旋律层**按用户定稿移除**
    # (不是事故 —— 事故场景是"表还在、声部坏了", 那时 melody_notes 仍 >0, 本门禁照常把关)。
    # 背景: 用户两轮听感定位违和源为电钢 (含附点八分延迟回声), v14 整层删除。
    cues_path = os.path.join(TOOLS, "pv_music_cues.json")
    if os.path.exists(cues_path):
        import json
        with open(cues_path, encoding="utf-8") as f:
            cues = json.load(f)
        if cues.get("composition", {}).get("melody_notes") == 0:
            print("N/A —— 旋律层已按用户定稿移除 (cues.composition.melody_notes=0, v14)。")
            print("       本门禁只在生成器声明存在旋律时生效; 勿据此判「音乐完成」。")
            sys.exit(0)
    print("生成「无旋律」基线 (AIGATE_PV_SKIP_MELODY=1) ...")
    tmpdir = tempfile.mkdtemp(prefix="aigate_pv_")
    base = os.path.join(tmpdir, "baseline.wav")
    env = dict(os.environ)
    env["AIGATE_PV_SKIP_MELODY"] = "1"
    env["AIGATE_PV_MUSIC_OUT"] = base
    env["PYTHONIOENCODING"] = "utf-8"     # 否则子进程的中文输出在 Windows 上按 GBK 解码会崩
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "make_pv_music.py")],
                       env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(base):
        print(r.stdout[-1500:]); print(r.stderr[-1500:])
        raise SystemExit("基线生成失败")

    a = load_mono(MAIN)
    b = load_mono(base)
    if len(a) != len(b):
        raise SystemExit("两版长度不一致 (%d vs %d) —— 生成器有随机性?" % (len(a), len(b)))
    # 差分: 两版只有主旋律一处不同, 相减即得旋律+它的延迟回声
    diff = [x - y for x, y in zip(a, b)]
    dmax = max(abs(v) for v in diff)
    print("差分残差峰值 %.4f (应远大于 0 —— 否则两版根本没差)" % dmax)
    if dmax < 1e-4:
        raise SystemExit("两版几乎相同 —— SKIP_MELODY 开关没生效?")

    nf, total, changes, rms = pitch_stats(diff, 44100, WIN_A, WIN_B)
    cover = nf / max(1, total)
    print("\n旋律差分检测 (%d–%ds):" % (WIN_A, WIN_B))
    print("  有效音高帧 %d / %d (%.1f%%)" % (nf, total, cover * 100))
    print("  音高变化 %d 次" % changes)
    print("  残差 RMS %.1f dBFS" % (20 * math.log10(rms + 1e-12)))

    ok_cover = cover >= 0.25
    ok_move = changes >= 25
    ok_loud = 20 * math.log10(rms + 1e-12) >= -40.0
    print("\n=== 判定 ===")
    print("  %s① 旋律覆盖率 %.1f%% (应 ≥25)" % ("OK " if ok_cover else "!! ", cover * 100))
    print("  %s② 音高变化 %d 次 (应 ≥25)" % ("OK " if ok_move else "!! ", changes))
    print("  %s③ 残差 RMS %.1f dBFS (应 ≥-40, 即混音里听得见)"
          % ("OK " if ok_loud else "!! ", 20 * math.log10(rms + 1e-12)))
    ok = ok_cover and ok_move and ok_loud
    print("%s" % ("通过 —— 存在一条在走动的主旋律 (好听与否仍需人耳判断)" if ok
                  else "有问题 —— 旋律缺失或不动"))
    shutil.rmtree(tmpdir, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
