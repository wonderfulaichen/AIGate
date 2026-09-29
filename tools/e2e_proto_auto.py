# -*- coding: utf-8 -*-
"""端到端: 协议自动匹配的三条关键路径。

背景 (用户实际报障): 客户端用 Responses 协议发给 AIGate, 网关却走了 chat。
根因是 `pick_api_format` 在"无能力证据"时无脑兜底 openai, 且静态层 (URL 路径/内置目录)
没有进入可用集 —— 于是绝大多数未探测的供应商都被静默改成 chat。

本脚本验证修复后的三种情形:
  ① 端点带 /responses 信号      → 客户端打 responses 应**原样直通** /responses
  ② 上游只认 chat (responses 回 404) → 应回退到 chat, 且**转换产出合法**
     (字符串 content + 无空 assistant)
  ③ 无任何信号 + 无探测          → 应**尊重客户端选择**, 不再静默改协议
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_protoauto")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8981, 8982

SEEN = []
# 上游只支持 chat: /responses 与 /messages 回 404
CHAT_ONLY_404 = True


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n)
        try:
            b = json.loads(raw)
        except Exception:
            b = {}
        SEEN.append({"path": self.path, "len": len(raw), "body": b})
        if CHAT_ONLY_404 and self.path.endswith(("/responses", "/messages")):
            body = b'{"error":{"message":"not found","type":"invalid_request_error"}}'
            self.send_response(404)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
# 三个供应商: ① 端点带 /responses 信号 ② 纯 chat (无信号) ③ 带 /anthropic/ 信号
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "sig", "endpoint": "http://127.0.0.1:%d/v1/responses" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"sig/m": {"upstream_model": "m"}}},
    {"name": "plain", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"plain/m": {"upstream_model": "m"}}},
    {"name": "anth", "endpoint": "http://127.0.0.1:%d/anthropic/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"anth/m": {"upstream_model": "m"}}},
    # ④ 显式声明只支持 openai: 客户端打 responses 时**必须转换** —— 用这条验证转换产出合法
    {"name": "fixed", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "models": {"fixed/m": {"upstream_model": "m", "api_format": "openai"}}},
]}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

RESP = {"model": None, "stream": False, "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]},
    {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "c1", "output": "r"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
]}


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=json.dumps(body).encode(),
                                headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def run(label, model, entry, expect_path):
    SEEN.clear()
    b = dict(RESP); b["model"] = model
    st = post(entry, b)
    time.sleep(0.4)
    got = SEEN[-1]["path"] if SEEN else "(未到上游)"
    ok = expect_path in got
    print("  %-38s 入口=%-16s 上游实收=%-22s status=%s  %s"
          % (label, entry.replace("/v1/", ""), got.replace("/v1/", ""), st, "OK" if ok else "[X]"))
    return ok, (SEEN[-1]["body"] if SEEN else {})


try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)

    print("=== ① 端点带 /responses 信号: 应原样直通 ===")
    ok1, body1 = run("信号=responses + 客户端打 responses", "sig/m", "/v1/responses", "/responses")
    # 直通必须保真: 仍是 Responses 形状 (input/instructions), 未被翻成 messages
    faithful = ("input" in body1) and ("messages" not in body1)
    print("     直通保真 (input 形状保留): %s" % ("是" if faithful else "**否**"))

    print("\n=== ② 无信号 + 无探测: 尊重客户端选择 (新行为), 不再静默改协议 ===")
    ok2, body2 = run("无信号 + 客户端打 responses", "plain/m", "/v1/responses", "/responses")
    print("     ↑ 上游实收 responses = 我们不再擅自翻成 chat; 若该上游确实不支持, 它应如实报错")
    print("       (旧行为会静默改成 chat, 用户因而看不出真实失败点)")

    print("\n=== ③ 端点带 /anthropic/ 信号: 客户端打 anthropic 应直通 ===")
    ok3, _ = run("信号=anthropic + 客户端打 messages", "anth/m", "/v1/messages", "/messages")

    print("\n=== ④ 显式只支持 openai: 客户端打 responses 必须转换, 且产出合法 ===")
    ok4, body4 = run("显式 api_format=openai + 客户端打 responses", "fixed/m", "/v1/responses",
                     "/chat/completions")
    msgs = body4.get("messages") or []
    strings_ok = all(isinstance(m.get("content"), str) or m.get("content") is None for m in msgs)
    no_empty_asst = not any(m.get("role") == "assistant" and m.get("content") in (None, "")
                            and not m.get("tool_calls") for m in msgs)
    print("     messages: %s" % [(m.get("role"), type(m.get("content")).__name__) for m in msgs])
    print("     纯文本 content 均为字符串: %s" % ("是" if strings_ok else "**否**"))
    print("     无 content/tool_calls 皆空的 assistant: %s" % ("是" if no_empty_asst else "**否**"))

    print("\n=== 结果 ===")
    allok = ok1 and faithful and ok2 and ok3 and ok4 and strings_ok and no_empty_asst
    print("  %s" % ("PASS" if allok else "FAIL"))
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
