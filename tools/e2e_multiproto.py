# -*- coding: utf-8 -*-
"""端到端: 同一模型 ID 声明多协议候选集后, 按**客户端入口**挑实际上游协议.

场景: 模型 `mock/multi` 设 api_formats: ["responses","openai"] (两个端点都可用).
  · 客户端打 /v1/responses         → 应走 responses 端点 (原生直通)
  · 客户端打 /v1/chat/completions  → 应走 chat 端点 (原生直通)
对照: 单协议模型 (只有 api_format: "responses") 打 chat 入口时, 会转换后仍发到 responses 端点.
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_mp")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT_GW, PORT_MOCK = 8913, 8914

HITS = []   # 记录 mock 收到的 (path, model)


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n)
        try:
            b = json.loads(raw)
        except Exception:
            b = {}
        HITS.append((self.path, b.get("model"), b.get("stream")))
        p = self.path
        self.send_response(200)
        if "/responses" in p:
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            for e in [
                {"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
                {"type": "response.output_text.delta", "output_index": 0, "delta": "R"},
                {"type": "response.completed", "response": {"id": "r", "status": "completed",
                 "usage": {"input_tokens": 5, "output_tokens": 1}, "output": []}},
            ]:
                self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n"); self.wfile.flush()
        else:
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            for e in [
                {"choices": [{"delta": {"role": "assistant"}, "index": 0}]},
                {"choices": [{"delta": {"content": "C"}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            ]:
                self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n"); self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PORT_MOCK), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
prov = {"providers": [{
    "name": "mock",
    "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PORT_MOCK,
    "api_key_env": "MK", "api_key_default": "d",
    "endpoint_responses": "http://127.0.0.1:%d/v1/responses" % PORT_MOCK,
    "models": {
        # 多协议: 两个端点都声明可用
        "mock/multi": {"upstream_model": "m1", "api_formats": ["responses", "openai"]},
        # 对照: 只有 responses
        "mock/resp-only": {"upstream_model": "m2", "api_format": "responses"},
    },
}]}
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps(prov))

env = dict(os.environ)
env.update(PORT=str(PORT_GW), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def call(path, model, body_extra=None):
    body = {"model": model, "stream": True}
    if path == "/v1/chat/completions":
        body["messages"] = [{"role": "user", "content": "hi"}]
    else:
        body["input"] = "hi"
    if body_extra:
        body.update(body_extra)
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT_GW, path),
                                data=json.dumps(body).encode(),
                                headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except Exception as e:
        return "ERR:%s" % e


try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT_GW, timeout=2).read(); break
        except Exception:
            time.sleep(0.5)

    print("=== 同一模型 mock/multi (api_formats: [responses, openai]) ===")
    HITS.clear()
    call("/v1/responses", "mock/multi")
    time.sleep(0.3)
    print("  客户端打 /v1/responses        → mock 收到路径: %s" % (HITS[-1][0] if HITS else "(未收到)"))
    r1 = HITS[-1][0] if HITS else ""

    HITS.clear()
    call("/v1/chat/completions", "mock/multi")
    time.sleep(0.3)
    print("  客户端打 /v1/chat/completions → mock 收到路径: %s" % (HITS[-1][0] if HITS else "(未收到)"))
    r2 = HITS[-1][0] if HITS else ""

    print("\n=== 对照: 单协议 mock/resp-only (api_format: responses) ===")
    HITS.clear()
    call("/v1/chat/completions", "mock/resp-only")
    time.sleep(0.3)
    print("  客户端打 /v1/chat/completions → mock 收到路径: %s" % (HITS[-1][0] if HITS else "(未收到)"))
    r3 = HITS[-1][0] if HITS else ""

    print("\n=== 判定 ===")
    ok = True
    if "/responses" in r1:
        print("  OK  多协议 + responses 入口 → 走 responses 端点 (原生直通)")
    else:
        print("  [X] 期望走 /responses, 实得 %s" % r1); ok = False
    if "/chat/completions" in r2:
        print("  OK  多协议 + chat 入口 → 走 chat 端点 (原生直通, 这才是'由请求决定协议')")
    else:
        print("  [X] 期望走 /chat/completions, 实得 %s" % r2); ok = False
    if "/responses" in r3:
        print("  OK  单协议仍走 responses (向后兼容, 行为未变)")
    else:
        print("  [X] 单协议行为被改变, 实得 %s" % r3); ok = False
    print("\n=== 结果: %s ===" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
