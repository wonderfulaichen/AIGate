# -*- coding: utf-8 -*-
"""端到端: 「今日」计数跨进程重启不归零 (修用户报障"重启后今日只有 2~3 次")。

背景: 面板 KPI 走 stats.session.today (内存原子计数器)。原设计为避开 5000 行日志
窗口封顶, 但代价是**进程重启即从 0 开始** —— 于是"今日"实际是"本轮"。
用户实测重启后显示 15 而全天实为 155。

判定: 
  ① 第一进程发 N 条 → today_requests == N
  ② 杀掉进程, 同一数据目录起第二个进程 → today_requests 仍 == N (而非 0)
  ③ 第二进程再发 M 条 → today_requests == N + M (继续累加, 不重头)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_today")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8995, 8996
N1, N2 = 4, 3


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"mock/m": {"upstream_model": "m"}}},
]}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")


def start():
    p = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            return p
        except Exception:
            time.sleep(0.5)
    raise SystemExit("实例未起来")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    time.sleep(1.0)


def post(i):
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=json.dumps({"model": "mock/m",
                                                  "messages": [{"role": "user", "content": "t%d" % i}]}).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def today():
    st = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/stats" % PG), timeout=20).read().decode("utf-8"))
    return ((st.get("session") or {}).get("today") or {}).get("requests"), st.get("today_requests")


try:
    print("=== 今日计数跨重启一致性 ===\n")
    p1 = start()
    for i in range(N1):
        post(i)
    time.sleep(1.2)
    a1, t1 = today()
    print("  进程① 发 %d 条 → session.today=%s  today_requests=%s  %s"
          % (N1, a1, t1, "OK" if a1 == N1 else "!!"))
    stop(p1)

    p2 = start()
    time.sleep(1.0)
    a2, t2 = today()
    print("  进程② 起来后 (未发请求) → session.today=%s  today_requests=%s  %s"
          % (a2, t2, "OK (未归零)" if a2 == N1 else "!! 归零了"))
    for i in range(N2):
        post(i)
    time.sleep(1.2)
    a3, t3 = today()
    print("  进程② 再发 %d 条 → session.today=%s  today_requests=%s  %s"
          % (N2, a3, t3, "OK" if a3 == N1 + N2 else "!!"))
    stop(p2)

    ok = (a1 == N1) and (a2 == N1) and (a3 == N1 + N2)
    print("\n=== 结果 ===")
    if ok:
        print("  PASS —— 今日计数跨重启保持 (重启不再归零, 且继续累加)")
    else:
        print("  FAIL —— 期望 %d / %d / %d, 实得 %s / %s / %s" % (N1, N1, N1 + N2, a1, a2, a3))
    sys.exit(0 if ok else 1)
finally:
    srv.shutdown()
