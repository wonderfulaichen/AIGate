# -*- coding: utf-8 -*-
"""deck 逐页截图 -> 1920x1080 视频帧 (供介绍视频合成)。

与 shoot_deck.py 的分工: 那个是给人眼核对排版 (1600x900); 这个专供视频, 四点不同:
  ① 输出 1920x1080 —— 视频规格, 而非网页预览尺寸;
  ② 注入 CSS 隐藏**交互残留**: 左下翻页提示 `.hint` / 右下页码 `#pg` / 顶部进度条 `#bar`.
     它们在浏览器里是必要的操作提示, 进了视频就成了"这是录屏"的破绽;
  ③ 用 `w.show(n)` 直接跳页, 不再累积 `dispatchEvent(ArrowRight)` —— 后者依赖内部 i 的初始值,
     且每翻一页都会把数字重置为 0 重新滚动, 最后停在哪不完全可控 (实测截出过 9KB 空白页);
  ④ 给足虚拟时间让 countUp 收尾 —— deck 的数字滚动是 **260ms 延迟 + 1000ms 时长**,
     没等它跑完就截图, 抓到的是中间态数字 (实测), 那比没有数字更糟.

用法: python shoot_deck_hd.py 0 1 2 3 4 8 11 14 16 17      (0-based 页码)
"""
import functools, http.server, io, json, os, socketserver, subprocess, sys, threading, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "video", "deck_hd")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8964
W, H = 1920, 1080

# 注入 CSS 做两件事:
#  ① 隐藏交互残留 (.hint 翻页提示 / #pg 页码 / #bar 进度条) —— 进了视频就是"这是录屏"的破绽;
#  ② **彻底禁掉过渡并强制最终态**. deck 的翻页是 `.s{opacity:0;visibility:hidden;transition:.42s}`
#     配 `.s.on{opacity:1}`, 且子元素带**递增 transition-delay** (0.04s→0.32s).
#     实测: CSS 过渡不随 `--virtual-time-budget` 快进 —— 虚拟时间预算耗尽时过渡仍没跑完,
#     截图就停在 opacity:0 上 (表现为整页空白, std≈2). 而且**每次空白的页还不一样**
#     (先见第 2/14 页, 后见第 12 页), 是时序竞争而非某页有问题.
#     故直接 `transition:none` + `!important` 强制 .on 页的终态, 不依赖任何时间推进.
HIDE_CSS = (
    ".hint,#pg,#bar{display:none!important}"
    ".s,.s>*{transition:none!important;animation:none!important}"
    ".s.on{opacity:1!important;visibility:visible!important;transform:none!important}"
    ".s.on>*{opacity:1!important;transform:none!important}"
)

# 自己切 .on 类, **不调 deck 的 show()** —— show() 会把 .cnt 置 0 再排一个 260ms 后的
# countUp, 那个定时器同样可能赶在截图前没跑完, 于是抓到"0"(既非真值也非终值)。
# 这里直接把 data-to 写进文本, 一步到位。
SWITCH_JS = (
    "var all=d.querySelectorAll('.s');"
    "Array.prototype.forEach.call(all,function(el,k){el.classList.toggle('on',k===%d);});"
    "Array.prototype.forEach.call(d.querySelectorAll('.cnt'),function(el){"
    "el.textContent=el.getAttribute('data-to');});"
)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


TPL = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;background:#08080c;overflow:hidden}</style></head>
<body>
<iframe id="f" src="/product-deck.html" style="width:%dpx;height:%dpx;border:0;display:block"></iframe>
<script>
document.getElementById('f').addEventListener('load', function () {
  var w = document.getElementById('f').contentWindow, d = w.document;
  var st = d.createElement('style');
  st.textContent = %s;
  d.head.appendChild(st);
  %s
});
</script></body></html>
"""


def main(pages):
    os.makedirs(OUT, exist_ok=True)
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=DOCS))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)
    for n in pages:
        wrapper = os.path.join(DOCS, "_hd%d.html" % n)
        io.open(wrapper, "w", encoding="utf-8").write(
            TPL % (W, H, json.dumps(HIDE_CSS), SWITCH_JS % n))
        out = os.path.join(OUT, "p%02d.png" % (n + 1))
        if os.path.exists(out):
            os.remove(out)
        # 虚拟时间 8000ms: 覆盖 deck 载入 + countUp(260+1000) + 渲染余量
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--window-size=%d,%d" % (W, H), "--virtual-time-budget=8000",
                        "--screenshot=" + out, "http://127.0.0.1:%d/_hd%d.html" % (PORT, n)],
                       capture_output=True, text=True, timeout=180)
        os.remove(wrapper)
        ok = os.path.exists(out)
        print("第 %2d 页 -> %s" % (n + 1, "%d KB" % (os.path.getsize(out) // 1024) if ok else "失败"))
    srv.shutdown()


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    main(args or list(range(18)))