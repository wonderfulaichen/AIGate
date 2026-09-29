# -*- coding: utf-8 -*-
"""对照实测: 同一段对话分别从 /v1/responses 与 /v1/chat/completions 进来, 上游实际收到什么。

用户现象: 相同内容用 chat 协议正常, 换 responses 协议即 400。
两条路径的差别在于 responses 入口**不是原样转发**, 而是 responses_to_openai_request 转换后
再走 chat 管线 —— 所以嫌疑在转换产物。本脚本把上游收到的 body 完整 dump 出来对比。
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_respconv")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT_GW, PORT_MOCK = 8971, 8972

SEEN = []       # 上游收到的 body


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n)
        try:
            b = json.loads(raw)
        except Exception:
            b = {"__raw__": raw[:400].decode("utf-8", "replace")}
        SEEN.append({"path": self.path, "len": len(raw), "body": b})
        body = json.dumps({"error": {"message": "invalid request error trace_id: mock",
                                     "type": "invalid_request_error"}}).encode()
        self.send_response(400)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PORT_MOCK), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [{
    "name": "mock",
    "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PORT_MOCK,
    "api_key_env": "MK", "api_key_default": "d",
    "models": {"mock/m": {"upstream_model": "m"}},
}]}))

env = dict(os.environ)
env.update(PORT=str(PORT_GW), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# 同一段对话的两种表述 —— 含工具调用与推理项 (responses 客户端会把上一轮 output 回传)
TOOLS = [{"type": "function", "name": "read_file",
          "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}]
CHAT_BODY = {
    "model": "mock/m", "stream": False,
    "messages": [
        {"role": "user", "content": "读一下 a.rs"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"p\":\"a.rs\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "fn main(){}"},
    ],
    "tools": [{"type": "function", "function": {"name": "read_file",
               "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}}],
}
RESP_BODY = {
    "model": "mock/m", "stream": False,
    "instructions": "you are a coding agent",
    "input": [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "读一下 a.rs"}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "need to read the file"}]},
        {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{\"p\":\"a.rs\"}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "fn main(){}"},
    ],
    "tools": TOOLS,
}


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT_GW, path),
                                data=json.dumps(body).encode(),
                                headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT_GW, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)

    print("=" * 78)
    print("① 客户端走 chat 入口  POST /v1/chat/completions")
    print("=" * 78)
    SEEN.clear()
    post("/v1/chat/completions", CHAT_BODY)
    time.sleep(0.4)
    for s in SEEN:
        print("  上游收到: path=%s  body_len=%d" % (s["path"], s["len"]))
        print("  body top-level keys: %s" % sorted(s["body"].keys()))
        msgs = s["body"].get("messages")
        if isinstance(msgs, list):
            print("  messages: %d 条" % len(msgs))
            for i, m in enumerate(msgs):
                c = m.get("content")
                print("    [%d] role=%-9s content=%s%s" % (
                    i, m.get("role"),
                    ("None" if c is None else ("str(%d)" % len(c) if isinstance(c, str) else type(c).__name__)),
                    ("  tool_calls=%d" % len(m["tool_calls"])) if m.get("tool_calls") else ""))

    print()
    print("=" * 78)
    print("② 客户端走 responses 入口  POST /v1/responses   (同一段对话)")
    print("=" * 78)
    SEEN.clear()
    st, txt = post("/v1/responses", RESP_BODY)
    time.sleep(0.4)
    print("  网关返回 status=%s" % st)
    for s in SEEN:
        print("  上游收到: path=%s  body_len=%d" % (s["path"], s["len"]))
        print("  body top-level keys: %s" % sorted(s["body"].keys()))
        msgs = s["body"].get("messages")
        if isinstance(msgs, list):
            print("  messages: %d 条" % len(msgs))
            for i, m in enumerate(msgs):
                c = m.get("content")
                print("    [%d] role=%-9s content=%s%s  keys=%s" % (
                    i, m.get("role"),
                    ("None" if c is None else ("str(%d)" % len(c) if isinstance(c, str) else type(c).__name__)),
                    ("  tool_calls=%d" % len(m["tool_calls"])) if m.get("tool_calls") else "",
                    sorted(m.keys())))
        print("  完整 body:")
        print(json.dumps(s["body"], ensure_ascii=False, indent=2)[:1800])
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
