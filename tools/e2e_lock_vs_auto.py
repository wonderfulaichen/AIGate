# -*- coding: utf-8 -*-
"""端到端: 显式 api_format 锁定 vs 自动。

验证用户报的场景: 供应商探测到多协议时,
  ① 显式 api_format=openai 的模型, 客户端打 /v1/responses → 上游必须收到 chat/completions
  ② 未标注 (自动) 的模型, 客户端打 /v1/responses → 上游收到 /responses (尊重客户端)
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_lock")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8991, 8992
SEEN = []


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n)
        SEEN.append({"path": self.path, "len": len(raw)})
        body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
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
# 供应商探测到三个协议 (模拟 deepseek 那种 openai+responses 实际能力)
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "relay", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "probed_protocols": ["openai", "responses"],
     "models": {
         "relay/locked": {"upstream_model": "m", "api_format": "openai"},
         "relay/auto": {"upstream_model": "m"},
     }},
]}, ensure_ascii=False))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

RESP = {"model": None, "stream": False, "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
]}


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=json.dumps(body).encode(),
                                headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def run(label, model, entry, expect):
    SEEN.clear()
    b = dict(RESP); b["model"] = model
    st = post(entry, b)
    time.sleep(0.4)
    got = SEEN[-1]["path"] if SEEN else "(未到上游)"
    ok = expect in got
    print("  %-34s 入口=%-11s 上游实收=%-18s status=%s  %s"
          % (label, entry.replace("/v1/", ""), got.replace("/v1/", ""), st, "OK" if ok else "[X]"))
    return ok


try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)

    print("=== ① 显式锁定 openai (客户端打 responses 也必须转 chat) ===")
    ok1 = run("显式 api_format=openai + responses", "relay/locked", "/v1/responses", "/chat/completions")
    ok1b = run("显式 api_format=openai + messages", "relay/locked", "/v1/messages", "/chat/completions")
    ok1c = run("显式 api_format=openai + chat", "relay/locked", "/v1/chat/completions", "/chat/completions")

    print("\n=== ② 自动 (未标注): 尊重客户端入口 ===")
    ok2 = run("自动 + 客户端打 responses", "relay/auto", "/v1/responses", "/responses")
    ok2b = run("自动 + 客户端打 chat", "relay/auto", "/v1/chat/completions", "/chat/completions")

    print("\n=== 结果 ===")
    allok = ok1 and ok1b and ok1c and ok2 and ok2b
    print("  %s" % ("PASS" if allok else "FAIL"))
    if not ok1 or not ok1b:
        print("  ↑ ① 失败 = 显式锁定被无视 (用户手动改协议不生效)")
    if not ok2:
        print("  ↑ ② 失败 = 自动模式被破坏")
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
