# -*- coding: utf-8 -*-
"""浅色 token 覆盖完整性: 主题相关的每个 token 都必须在 html.light 里给出取值。

为什么这条最要紧: 漏覆盖**不会报错**, 只会让浅色下静默继承暗色值 ——
最典型是 --fill-* 若忘了覆盖, 浅色下仍是"白叠加"(白压白), 相关控件全部看不见。
与主题无关的 (字号/间距/圆角/时长/奖牌色) 允许不覆盖, 见 ALLOWED。
"""
import os
import io, re, sys, fnmatch
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
PATH = os.path.join(ROOT, "src", "admin", "base.css")
CSS = re.sub(r"/\*.*?\*/", "", io.open(PATH, encoding="utf-8").read(), flags=re.S)

# 与主题无关, 允许只在 :root 定义
ALLOWED = ["--fs-*", "--sp-*", "--rs", "--rm", "--r", "--rl", "--pill",
           "--dur-quick", "--dur-fast", "--dur", "--dur-slow", "--dur-spring", "--ease-out", "--ease-spring",
           "--rank-1", "--rank-2", "--rank-3",
           # 字体族与主题无关 (系统字体栈, 暗/亮同一套)
           "--font-ui", "--font-mono",
           # 图标字形尺寸与主题无关
           "--icon-*",
           # 模糊半径与主题无关 (深色浅色都用同一档); 玻璃底 --card-glass 才是主题相关的
           "--glass-blur"]


def block(sel):
    m = re.search(sel + r"\s*\{(.*?)\n\}", CSS, re.S)
    if not m:
        print("FAIL: 找不到 %s 块 (选择器改了? 需同步本脚本)" % sel)
        sys.exit(1)
    d = {}
    for decl in m.group(1).split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            k = k.strip()
            if k.startswith("--"):
                d[k] = v.strip()
    return d


root, light = block(r":root"), block(r"html\.light")
missing = [k for k in root if k not in light and not any(fnmatch.fnmatch(k, p) for p in ALLOWED)]
extra = [k for k in light if k not in root]

print(":root %d 个 token / html.light 覆盖 %d 个 / 允许不覆盖 %d 个"
      % (len(root), len(light), len(root) - len(light)))

fail = 0
if missing:
    fail = 1
    print("\nFAIL: 该覆盖却漏掉 (浅色下会继承暗色值):")
    for k in sorted(missing):
        print("   %-24s dark=%s" % (k, root[k]))
else:
    print("OK   主题相关 token 全部有浅色取值 (0 遗漏)")

if extra:
    fail = 1
    print("\nFAIL: html.light 里多出来的键 (疑似拼写错误):")
    for k in sorted(extra):
        print("   %s" % k)

sys.exit(fail)
