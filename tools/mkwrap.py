# -*- coding: utf-8 -*-
"""生成截图用 wrapper。页面以目标页签**启动** (见 build_preview.py 的 ?tab 变体),
wrapper 只负责设定主题并等渲染完成 —— 不做任何 DOM 强制显隐 (那会跟框架打架、搞坏布局)。
"""
import io, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
P = os.path.join(ROOT, ".openbitfun", "tmp", "preview")

TPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>preview {name}</title></head>
<body style="margin:0;background:#0b0d12">
<iframe id="pv" src="/admin_{tab}.html?{query}" style="width:100%;height:{h}px;border:0;display:block"></iframe>
<script>
try {{ localStorage.setItem('aigate.theme', '{theme}'); }} catch (e) {{}}
</script>
</body></html>
"""


def make(name, tab, theme="dark", query="sc=done", w=1400, h=2100):
    # 宽度交给 iframe 撑满, 由 shoot.py 的 --window-size 决定实际视口 ——
    # 若在这里写死像素宽, 窄屏截图会只裁掉右边, 媒体查询仍按大宽度求值 (踩过).
    src = "admin.html" if tab == "dashboard" else "admin_%s.html" % tab
    html = TPL.format(name=name, tab=tab, query=query, h=h, theme=theme)
    html = html.replace("/admin_%s.html" % tab, "/" + src)
    io.open(os.path.join(P, name + ".html"), "w", encoding="utf-8").write(html)


TABS = ["dashboard", "analytics", "records", "providers", "settings", "about"]
for tab in TABS:
    q = "sc=done&logs=40" if tab == "records" else "sc=done"
    make("s_" + tab, tab, query=q)
    make("s_light_" + tab, tab, theme="light", query=q)
# 抽屉变体 (模型表最密集, 必须单独核对)
make("s_drawer", "drawer", query="sc=done")
make("s_light_drawer", "drawer", theme="light", query="sc=done")
# 记录详情变体 (阶段瀑布: 模型侧四段 + 网关去程五段)
make("s_recorddetail", "recorddetail", query="sc=done")
make("s_light_recorddetail", "recorddetail", theme="light", query="sc=done")
print("wrote %d wrappers" % (len(TABS) * 2 + 2))
