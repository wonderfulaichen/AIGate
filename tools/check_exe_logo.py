# -*- coding: utf-8 -*-
"""查一个 exe 里到底嵌了**哪一版品牌标**。

依据(不依赖任何会被缓存/伪造的外部状态):
  · exe 图标 = RT_ICON 资源, 位图数据原样嵌在 exe 里 → 直接字节命中;
  · 面板标   = core.js 被 include_str! 烤进二进制 → 文本 path / 颜色都直接命中。
所以"时间戳 / commit 号 / 资源管理器里显示的图标"都不可信(前者见 HANDOFF §2.3, 后者受
Windows 图标缓存影响), 这个脚本才是判据。

用法: python tools/check_exe_logo.py [额外的 exe 路径...]
"""
import os
import struct
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
# ⚠️ 机器相关: 部署目录在仓库之外, 故这里保留绝对路径 (命令行传入的 exe 会一并检查)
DEPLOY = r"D:\Office software\Development Project\AI中转\AIGate.exe"
BUILD = os.path.join(ROOT, "target", "release", "AIGate.exe")

# 新标 / 旧标的指纹
NEW_MARK = b"M148 56 H52 V200 H148"          # 新标: 门框折线 path（母版 icon.svg 里的原文）
# ⚠️ 旧标记不能只写 "#0071e8" —— 那是浅色主题的 --primary token (base.css), 每份构建里都有,
#    会把新构建也误判成"含旧标"。必须带上下文: 旧标是渐变终点色, 所以连 "stop-color=" 一起匹配。
OLD_MARK = b'stop-color="#0071e8"'
SLICE_START, SLICE_LEN = 3040, 120           # 从 32px 位图里取一段做指纹（避开全同色区）


def ico_sizes(blob):
    n = struct.unpack_from("<H", blob, 4)[0]
    return [(struct.unpack_from("<BBBBHHII", blob, 6 + 16 * i)[0] or 256,
             struct.unpack_from("<BBBBHHII", blob, 6 + 16 * i)[7]) for i in range(n)]


def entry_slice(blob, size):
    off = next(o for s, o in ico_sizes(blob) if s == size) + 40   # 跳过 BITMAPINFOHEADER
    return blob[off + SLICE_START: off + SLICE_START + SLICE_LEN]


new_ico = open(os.path.join(ROOT, "icon.ico"), "rb").read()
# ⚠️ 「旧标」基线必须固定在**换标之前那个提交**上, 不能写 HEAD —— 新标一旦提交, HEAD:icon.ico
#    就等于新标, 于是"旧=True"恒真、判据失效 (2026-09-30 提交 88d7de3 后实测踩到)。
#    OLD_REF 就是换标前的最后一个提交 e0ce6d9。可用环境变量覆盖。
OLD_REF = os.environ.get("LOGO_OLD_REF", "e0ce6d9")
old_ico = subprocess.run(["git", "show", OLD_REF + ":icon.ico"], capture_output=True, cwd=ROOT).stdout
NEW_BMP = entry_slice(new_ico, 32)
OLD_BMP = entry_slice(old_ico, 32)
print("指纹: 新标位图 %d B / 旧标位图 %d B / 新标 path %d B\n"
      % (len(NEW_BMP), len(OLD_BMP), len(NEW_MARK)))

pairs = [("部署份", DEPLOY), ("构建份", BUILD)] + [("参数", a) for a in sys.argv[1:]]
for label, path in pairs:
    if not os.path.exists(path):
        print("%-6s 不存在: %s" % (label, path))
        continue
    blob = open(path, "rb").read()
    print("%-6s %s" % (label, path))
    print("       %d B · mtime %s" % (len(blob), time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))))
    print("       图标位图: 新=%s  旧=%s" % (NEW_BMP in blob, OLD_BMP in blob))
    print("       面板品牌标: 新=%s  旧=%s" % (NEW_MARK in blob, OLD_MARK in blob))