# -*- coding: utf-8 -*-
"""探针: 循环检测**默认关闭** —— 成串重复不再误杀用户的工作（2026-10-11 用户决定）。

背景（用户第三次就同一件事报障，并给出结论）：
    「没解决。不如把这个功能关了。」

依据（aigate.log 全量审计 2026-10-05 上线 ~ 10-11）：
    **24 次命中, 全部是误报, 零次真循环** ——
      17 次末尾是代码块对齐的成串空格；
       2 次是 Markdown 表格线（`|---|------|----`）；
       4 次是 ASCII 图点阵（`........`）；
       1 次是 ASCII 图的海面行（`SSSSSSSSSSSSS`）。
    根因：判据（"末尾一段重复"）**无法区分"模型死循环"与"结构化/图形输出"**。
    该用户的典型产出就是 ASCII 地图，天然含成串 `.` `S` `-`。
    注：填充字符修复能治前三类，**治不了 `SSSSS…`**（字母是有语义的），
    故"再修一次判据"这条路已证明走不通。
    代价对比：真循环最多浪费到 max_tokens；误报则是**丢掉整个回答**。

判据 (3 条):
  ① 默认（未设 LOOP_GUARD_ENABLED）: 会触发旧判据的重复内容**不被截断**
  ② 默认: 客户端收到正常 `stop` 收尾（不是 `length`），且内容完整
  ③ 该请求的记录是正常 200 且无错误文案（不被误标红）

不连网; 本地 mock 上游.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_loopoff")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8959, 8960
REPEATS = 40

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("  —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    """发会被旧判据命中的重复内容（ASCII 图/表格式成串重复）. """

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def frame(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        # 交替发两行图示：一行是 ASCII 地图（含成串 S/.），一行是表格线。
        # 这正是用户真实产出里被判"死循环"的形态。
        for _ in range(REPEATS):
            frame({"choices": [{"index": 0, "delta": {"content": "-96 SSSSSSSSSSSSS......RR....\n"}, "finish_reason": None}]})
        frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 20, "completion_tokens": 640}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

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

# ⚠️ 刻意**不设** LOOP_GUARD_ENABLED —— 就是要验默认值.
env = dict(os.environ)
for k in ("LOOP_GUARD_ENABLED",):
    env.pop(k, None)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("实例未就绪")
        sys.exit(1)

    print("=== 循环检测默认关闭: 成串重复不再误杀 ===\n")
    print("mock 发 %d 行 ASCII 地图/表格式内容 (旧判据必命中)\n" % REPEATS)

    body = {"model": "mock/m", "messages": [{"role": "user", "content": "渲染地图"}], "stream": True}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode("utf-8", "replace")
    time.sleep(1.5)

    lines = txt.count("SSSSSSSSSSSSS")
    ck("① 重复内容未被截断 (内容完整)", lines >= REPEATS,
       "客户端收到 %d 行地图 (期望 %d)" % (lines, REPEATS))
    ck("② 收尾是 stop 而非 length (未被网关掐断)",
       '"finish_reason": "stop"' in txt or '"finish_reason":"stop"' in txt,
       "含 length? %s" % ('"length"' in txt))

    logs = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/logs?limit=5" % PG), timeout=20
    ).read().decode("utf-8"))
    rows = logs.get("logs") if isinstance(logs, dict) else logs
    row = (rows or [{}])[0]
    ck("③ 记录是正常 200 且无错误文案 (未被误标红)",
       row.get("status") == 200 and not (row.get("error") or "").strip(),
       "status=%s error=%r" % (row.get("status"), row.get("error")))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 默认不再误杀成串重复 (要开启: LOOP_GUARD_ENABLED=1 或模型级 loop_guard: true)")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
