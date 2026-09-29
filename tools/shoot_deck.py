# -*- coding: utf-8 -*-
"""把演示文稿逐页截图, 人眼核对排版 (生成成功 != 排版正确)。"""
import functools, http.server, io, os, socketserver, subprocess, sys, threading, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "deck")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8963


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


TPL = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#08080c">
<iframe id="f" src="/product-deck.html" style="width:100%;height:{h}px;border:0;display:block"></iframe>
<script>
var n={n};
document.getElementById('f').addEventListener('load',function(){{
  var d=document.getElementById('f').contentDocument, k=0;
  var t=setInterval(function(){{
    if(k++>=n){{ clearInterval(t); return; }}
    d.dispatchEvent(new KeyboardEvent('keydown',{{key:'ArrowRight'}}));
  }},80);
}});
</script></body></html>
"""

os.makedirs(OUT, exist_ok=True)
socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=DOCS))
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

pages = [int(a) for a in sys.argv[1:]] or list(range(12))
for n in pages:
    w = os.path.join(DOCS, "_wd%d.html" % n)
    io.open(w, "w", encoding="utf-8").write(TPL.format(n=n, h=900))
    out = os.path.join(OUT, "d%02d.png" % n)
    if os.path.exists(out):
        os.remove(out)
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1600,900", "--virtual-time-budget=9000",
                    "--screenshot=" + out, "http://127.0.0.1:%d/_wd%d.html" % (PORT, n)],
                   capture_output=True, text=True, timeout=120)
    print("第 %2d 页: %s" % (n + 1, "%d KB" % (os.path.getsize(out) // 1024) if os.path.exists(out) else "失败"))
    os.remove(w)
srv.shutdown()
