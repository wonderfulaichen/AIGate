# -*- coding: utf-8 -*-
"""证明「浅色模式改造」没有改变暗色视觉。

做法: 对每个被改的文件, 取 git HEAD 版本与工作区版本, 各自解析出**实际生效的颜色集合**:
  · 直接字面量 (rgba/hex)
  · var(--token) → 用**同版本** base.css 的 :root 表解析成字面量
然后比对两版集合的差异。集合相同 = 暗色下每个颜色值都没变。

排除 "--token: 值" 定义行本身 (那是声明, 不是使用) —— 否则新增 token 定义会被算成"新颜色"。
"""
import io, os, re, subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级

COLOR = re.compile(r"rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*[\d.]+\s*)?\)|#[0-9a-fA-F]{3,8}\b")
VAR = re.compile(r"var\(\s*(--[A-Za-z0-9-]+)\s*\)")
DEFLINE = re.compile(r"^\s*--[A-Za-z0-9-]+\s*:")


def strip_comments(text):
    """去掉 CSS 注释。必须做: ① 定义行尾注释会把下一个 token 名粘进上一段 (分号切割),
    ② 注释正文里的示例色 (如"如 rgba(255,255,255,.26) 会消失")会被误当"在用的颜色"。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def root_table(css_text):
    """从 css 文本的第一个 :root{...} 里取出 token → 原值。"""
    t = {}
    m = re.search(r":root\s*\{(.*?)\n\}", strip_comments(css_text), re.S)
    if not m:
        return t
    for decl in m.group(1).split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            k = k.strip()
            if k.startswith("--"):
                t[k] = v.strip()
    return t


def colors_of(text, table):
    """收集非定义行上的实际颜色 (字面量 + var 解析值)。"""
    out = set()
    for line in strip_comments(text).split("\n"):
        if DEFLINE.match(line):
            continue
        for c in COLOR.findall(line):
            out.add(c.lower())
        for v in VAR.findall(line):
            val = table.get(v)
            if val:
                for c in COLOR.findall(val):
                    out.add(c.lower())
    return out


def head_text(path):
    r = subprocess.run(["git", "show", "HEAD:" + path], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


FILES = ["src/admin/base.css", "src/admin/components.css", "src/admin/pages.css",
         "src/admin/drawer-provider.html", "src/admin/page-overview.html",
         "src/admin/page-analytics.html", "src/admin/modal-whatsnew-recall.html"]

base_head = head_text("src/admin/base.css")
base_new = io.open(ROOT + r"\src\admin\base.css", encoding="utf-8").read()
tbl_head, tbl_new = root_table(base_head or ""), root_table(base_new)
print("token 表: HEAD %d 个 / 新版 %d 个 (新增 %d)\n"
      % (len(tbl_head), len(tbl_new), len(set(tbl_new) - set(tbl_head))))

all_gone, all_added = set(), set()
for f in FILES:
    h = head_text(f)
    if h is None:
        print("SKIP %s (HEAD 无此文件)" % f)
        continue
    n = io.open(ROOT + "\\" + f.replace("/", "\\"), encoding="utf-8").read()
    ch, cn = colors_of(h, tbl_head), colors_of(n, tbl_new)
    gone, added = ch - cn, cn - ch
    all_gone |= gone
    all_added |= added
    flag = "OK  " if not gone and not added else "DIFF"
    print("%s %-40s HEAD %3d 色 / 新 %3d 色" % (flag, f.split("/")[-1], len(ch), len(cn)))
    for c in sorted(gone):
        print("       消失: %s" % c)
    for c in sorted(added):
        print("       新增: %s" % c)

print("\n=== 汇总 ===")
print("消失的颜色 %d 个 / 新增的颜色 %d 个" % (len(all_gone), len(all_added)))
if not all_gone and not all_added:
    print("PASS: 暗色下颜色集合完全一致 (零视觉变化)")
else:
    print("以上差异需逐条确认是否可接受")
