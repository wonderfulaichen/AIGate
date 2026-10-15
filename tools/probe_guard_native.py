#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e: 隐私护栏在「原生直通」路径 (/v1/messages, 上游 Anthropic 协议) 也一致生效.

背景: 方案 B 护栏此前只在 chat_completions 管线扫请求体 + 还原响应; 而 /v1/messages、
/v1/responses 入口在「上游为原生协议」时走 relay_native_passthrough 字节级直通, 那段
**漏扫也漏还原** —— 即客户端从原生协议入口发密钥, 护栏完全不生效 (不脱敏、不记 warn、
客户端直接看到占位符漏出). 本探针复现并回归验证该缺口已补齐.

判据与 chat 探针一致:
  ① 默认关: 请求体逐字节不变发往上游, 不含占位符.
  ② Redact: 上游收到 [API_KEY_1], 不含原值; 客户端收到**还原值**.
  ③ Block: 私钥命中 → 400 拒, 上游没收到任何东西.

隔离方式同已知可用探针 (exe 复制到 ISO, 同进程线程 mock 上游, 临时 data 目录).
"""
import sys, os, json, time, subprocess, shutil, io, threading, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adminauth

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(REPO, ".openbitfun", "tmp", "iso_guard_native")
PORT = 8915
UP_PORT = 8916

UP_LOG = os.path.join(ISO, "up.log")

# Anthropic 原生上游 mock: 记录收到的请求体, 并回显正文 (含脱敏后的占位符),
# 用 Anthropic SSE 形态返回 —— 网关 NativeTapStream 原样透传字节, 还原在出站前做.
class UpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        with io.open(UP_LOG, "a", encoding="utf-8") as f:
            f.write(body + "\n====\n")
        try:
            echo = json.loads(body).get("messages", [{}])[-1].get("content", "")
        except Exception:
            echo = body
        # Anthropic SSE: message_start + content_block_delta(text) + message_delta + message_stop
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: message_start\n")
        self.wfile.write(b'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n')
        self.wfile.flush()
        self.wfile.write(b"event: content_block_delta\n")
        self.wfile.write(('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"echo:' + echo + '"}}\n\n').encode("utf-8"))
        self.wfile.flush()
        self.wfile.write(b"event: message_delta\n")
        self.wfile.write(b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n')
        self.wfile.flush()
        self.wfile.write(b"event: message_stop\n")
        self.wfile.write(b"data: {\"type\":\"message_stop\"}\n\n")
        self.wfile.flush()
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "2")
        self.end_headers(); self.wfile.write(b"ok")

def start_up():
    srv = ThreadingHTTPServer(("127.0.0.1", UP_PORT), UpHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def up_log_text():
    p = os.path.join(ISO, "up.log")
    return io.open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""

def wait_ready(base, timeout=20):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/health"), timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False

def write_guard(cfg):
    with open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)

def main():
    shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "config"), exist_ok=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    # 供应商声明上游可用 anthropic 协议 → /v1/messages 入口走原生直通.
    # models 是 dict (与网关 ProviderConfig 结构一致), 单值 api_format 硬约束为 anthropic.
    prov = {"providers": [{"name":"claude","endpoint":"http://127.0.0.1:%d/v1/chat/completions" % UP_PORT,
                           "api_key_env":"G_P",
                           "models":{"claude-x":{"upstream_model":"claude-x",
                                                  "api_formats":["anthropic"],"api_format":"anthropic"}}}]}
    with open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)
    os.environ["G_P"] = "dummy-key-for-test"
    up = start_up()
    env = dict(os.environ); env["PORT"] = str(PORT); env["AIGATE_DATA_DIR"] = os.path.join(ISO, "data")
    exe = os.path.join(ISO, "AIGate.exe")
    shutil.copy(os.path.join(REPO, "target", "release", "AIGate.exe"), exe)
    base = "http://127.0.0.1:%d" % PORT
    proc = subprocess.Popen([exe], cwd=ISO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_ready(base):
        print("FAIL gateway did not become ready in time")
        proc.terminate(); up.shutdown(); sys.exit(1)
    time.sleep(0.5)
    # token 必须在网关就绪后读 (首次启动才生成 data/.admin_token)
    hdr = adminauth.headers(os.path.join(ISO, "data"))

    fails = []
    def check(name, cond):
        print(("PASS" if cond else "FAIL") + " " + name)
        if not cond: fails.append(name)

    SK = "sk-abcdefghijklmnopqrstuvwxyz012345"

    def msg_body(text):
        return {"model":"claude-x","stream":True,
                "messages":[{"role":"user","content":text}]}

    def post(path, body):
        return adminauth.post_json(base + path, body, hdr)

    # ① 默认关: /v1/messages 直通, 密钥原样发往上游 (无占位符)
    open(UP_LOG, "w").close()
    post("/v1/messages", msg_body("my key is " + SK))
    up_log = up_log_text()
    check("native_default_off_forwards_secret_verbatim", SK in up_log and "[API_KEY_1]" not in up_log)

    # ② Redact: 上游收占位符, 客户端收还原值
    write_guard({"enabled":True,"dry_run":False,
                 "rules":{"api_key":{"enabled":True,"action":"redact"}}})
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    open(UP_LOG, "w").close()
    resp = post("/v1/messages", msg_body("my key is " + SK))
    up_log = up_log_text()
    check("native_redact_upstream_gets_placeholder", "[API_KEY_1]" in up_log and SK not in up_log)
    check("native_redact_client_gets_restored_secret", SK in json.dumps(resp))

    # ③ Block: 私钥命中 → 400, 上游无收到
    open(UP_LOG, "w").close()
    write_guard({"enabled":True,"dry_run":False,
                 "rules":{"private_key":{"enabled":True,"action":"block"}}})
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    r = post("/v1/messages",
             {"model":"claude-x","stream":True,
              "messages":[{"role":"user","content":"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"}]})
    up_log = up_log_text()
    blocked = (r.get("status") == 400) or ("error" in r) or ("私钥" in str(r))
    check("native_block_rejects_request_400", blocked and "BEGIN PRIVATE KEY" not in up_log)

    proc.send_signal(__import__("signal").SIGTERM)
    up.shutdown()
    time.sleep(1)
    print("RESULT %d/%d" % (len(fails), 3))
    if fails:
        print("FAILED: " + ", ".join(fails))
        sys.exit(1)
    print("ALL GREEN")

if __name__ == "__main__":
    main()
