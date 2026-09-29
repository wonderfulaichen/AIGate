# -*- coding: utf-8 -*-
"""批量截图 wrapper 页 (headless Edge)。服务与浏览器同进程, 避免子进程被回收。"""
import functools, http.server, os, socketserver, subprocess, sys, threading, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
PREVIEW = os.path.join(ROOT, ".openbitfun", "tmp", "preview")
SHOTS = os.path.join(ROOT, ".openbitfun", "tmp", "shots")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8973


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main(names, width=1400, height=2200, prefix=""):
    os.makedirs(SHOTS, exist_ok=True)
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=PREVIEW))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)
    for n in names:
        out = os.path.join(SHOTS, prefix + n + ".png")
        if os.path.exists(out):
            os.remove(out)
        url = "http://127.0.0.1:%d/%s.html" % (PORT, n)
        r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--window-size=%d,%d" % (width, height),
                            "--virtual-time-budget=8000",
                            "--screenshot=" + out, url],
                           capture_output=True, text=True, timeout=150)
        ok = os.path.exists(out)
        print("%-26s %s" % (prefix + n, "%d 字节" % os.path.getsize(out) if ok else "失败 exit=%s" % r.returncode))
    srv.shutdown()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    w = next((int(a[4:]) for a in sys.argv if a.startswith("--w=")), 1400)
    h = next((int(a[4:]) for a in sys.argv if a.startswith("--h=")), 2200)
    pfx = next((a[5:] for a in sys.argv if a.startswith("--as=")), "")
    allnames = ["s_overview", "s_analytics", "s_records", "s_providers", "s_settings", "s_about",
                "s_light_overview", "s_light_analytics", "s_light_providers", "s_light_settings"]
    main(args or allnames, w, h, pfx)
