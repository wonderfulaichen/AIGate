# -*- coding: utf-8 -*-
"""逐文件检查 HTML 部件标签配平, 找出跨文件/文件内未闭合导致嵌套错位的地方。"""
import io, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
D = os.path.join(ROOT, "src", "admin")

# 这些文件按设计跨文件开合, 单独看会不平衡 —— 只报数量供参考
CROSSING = {"head.html", "body-shell.html", "footer.html", "panel-price.html", "tail.html"}

TAGS = ["div", "span", "template", "table", "tr", "td", "th", "section", "nav", "main", "header", "details", "summary", "p", "a", "button", "label", "select", "textarea", "pre"]
for f in sorted(os.listdir(D)):
    if not f.endswith(".html"):
        continue
    t = io.open(os.path.join(D, f), encoding="utf-8").read()
    # 去掉注释与 script/style 内容
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<script\b.*?</script>", "", t, flags=re.S)
    t = re.sub(r"<style\b.*?</style>", "", t, flags=re.S)
    bad = []
    for tag in TAGS:
        o = len(re.findall(r"<%s\b" % tag, t))
        c = len(re.findall(r"</%s>" % tag, t))
        if o != c:
            bad.append("%s:%+d" % (tag, o - c))
    if bad:
        mark = "  (跨文件开合, 正常)" if f in CROSSING else "  <== 检查"
        print("%-32s %s%s" % (f, " ".join(bad), mark))
