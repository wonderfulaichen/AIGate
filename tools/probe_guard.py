import sys, os, json, time, subprocess, shutil, signal, io, threading, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adminauth

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(REPO, ".openbitfun", "tmp", "iso_guard")
PORT = 8913
UP_PORT = 8914

# 同进程线程内的 mock 上游: 记录收到的请求体 (给"上游实收"判据用),
# 并返回 SSE 流式 chat completion (网关 /v1/chat/completions 需要流式响应).
UP_LOG = os.path.join(ISO, "up.log")

class UpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        with io.open(UP_LOG, "a", encoding="utf-8") as f:
            f.write(body + "\n====\n")
        # 回显收到的请求体文本 (含网关脱敏后的占位符), 用于验证"响还原"——
        # 网关会在出站流里把 [API_KEY_1] 还原回原值, 客户端应看到原值而非占位符.
        try:
            echo = json.loads(body).get("messages", [{}])[-1].get("content", "")
        except Exception:
            echo = body
        deltas = [
            {"index": 0, "delta": {"role": "assistant", "content": "echo:" + echo}, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": "stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for d in deltas:
            self.wfile.write(("data: " + json.dumps({
                "id": "chatcmpl-" + uuid.uuid4().hex[:8],
                "object": "chat.completion.chunk", "model": "gpt-4o",
                "choices": [d],
            }) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "2")
        self.end_headers(); self.wfile.write(b"ok")

def start_up():
    srv = ThreadingHTTPServer(("127.0.0.1", UP_PORT), UpHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def start_gw():
    env = dict(os.environ); env["PORT"] = str(PORT); env["AIGATE_DATA_DIR"] = os.path.join(ISO, "data")
    os.environ["G_P"] = "dummy-key-for-test"
    exe = os.path.join(REPO, "target", "release", "AIGate.exe")
    return subprocess.Popen([exe], cwd=ISO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def up_log_text():
    p = os.path.join(ISO, "up.log")
    if not os.path.exists(p):
        return ""
    return io.open(p, encoding="utf-8", errors="replace").read()

def wait_ready(base, hdr, timeout=20):
    import urllib.request, urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/health", headers=hdr), timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False

def write_guard(enabled, dry_run):
    cfg = {"enabled": enabled, "dry_run": dry_run, "rules": {}}
    with open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)

def main():
    shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "config"), exist_ok=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    # 最小 providers.json: 一个 openai 兼容供应商, 指向 mock 上游.
    # ⚠️ `models` 是 dict (不是 list) —— 与网关的 ProviderConfig 结构一致, list 会被忽略 → unknown model.
    prov = {"providers": [{"name":"gpt","endpoint":"http://127.0.0.1:%d/v1/chat/completions" % UP_PORT,
                           "api_key_env":"G_P",
                           "models":{"gpt-4o":{"upstream_model":"gpt-4o","api_format":"openai"}}}]}
    with open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)
    os.environ["G_P"] = "dummy-key-for-test"
    up = start_up()
    # 启动隔离网关实例 (exe 复制到 ISO, 与已知可用探针一致)
    env = dict(os.environ); env["PORT"] = str(PORT); env["AIGATE_DATA_DIR"] = os.path.join(ISO, "data")
    exe = os.path.join(ISO, "AIGate.exe")
    shutil.copy(os.path.join(REPO, "target", "release", "AIGate.exe"), exe)
    base = "http://127.0.0.1:%d" % PORT
    proc = subprocess.Popen([exe], cwd=ISO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_ready(base, {}):
        print("FAIL gateway did not become ready in time")
        proc.terminate(); up.shutdown(); sys.exit(1)
    time.sleep(0.5)
    # ⚠️ 必须在网关就绪后读 token: 网关首次启动才生成 data/.admin_token,
    #    提前读 (启动前) 会得到空 dict → 所有 /admin/api 调用 401.
    hdr = adminauth.headers(os.path.join(ISO, "data"))
    fails = []
    def check(name, cond):
        print(("PASS" if cond else "FAIL") + " " + name)
        if not cond: fails.append(name)

    SK = "sk-abcdefghijklmnopqrstuvwxyz012345"
    PK = "-----BEGIN PRIVATE KEY-----\nMIIBxyz\n-----END PRIVATE KEY-----"

    # ① 默认关 (无 guard.json): 密钥原样发往上游, 不含占位符
    body = {"model":"gpt-4o","stream":True,"messages":[{"role":"user","content":"my key is "+SK}]}
    r = adminauth.post_json(base + "/v1/chat/completions", body, hdr)
    up_log = up_log_text()
    check("default_off_forwards_secret_verbatim", SK in up_log and "[API_KEY_1]" not in up_log)

    # ② dry_run (开启但默认动作 warn): 上游仍收到原值, 但台账有 guard_hit 事件
    open(os.path.join(ISO, "up.log"), "w").close()
    write_guard(True, True)
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    r = adminauth.post_json(base + "/v1/chat/completions", body, hdr)
    up_log = up_log_text()
    check("dry_run_forwards_verbatim", SK in up_log and "[API_KEY_1]" not in up_log)
    ev = adminauth.get_json(base + "/admin/api/events", hdr)
    check("dry_run_emits_guard_hit_event", any(e.get("kind")=="guard_hit" for e in ev.get("events", [])))

    # ③ Redact (显式开启 api_key=redact): 上游收到占位符, 不含原值; 客户端收到**还原值**
    write_guard(True, False)
    cfg = json.load(open(os.path.join(ISO, "config", "guard.json"), encoding="utf-8"))
    cfg["rules"]["api_key"] = {"enabled": True, "action": "redact"}
    json.dump(cfg, open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8"))
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    open(os.path.join(ISO, "up.log"), "w").close()
    resp = adminauth.post_json(base + "/v1/chat/completions", body, hdr)
    up_log = up_log_text()
    check("redact_upstream_gets_placeholder", "[API_KEY_1]" in up_log and SK not in up_log)
    check("redact_client_gets_restored_secret", SK in json.dumps(resp))

    # ④ Block (私钥=block): 请求被拒 400, 上游没收到任何东西
    open(os.path.join(ISO, "up.log"), "w").close()
    cfg = json.load(open(os.path.join(ISO, "config", "guard.json"), encoding="utf-8"))
    cfg["rules"]["private_key"] = {"enabled": True, "action": "block"}
    json.dump(cfg, open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8"))
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    r = adminauth.post_json(base + "/v1/chat/completions",
                            {"model":"gpt-4o","stream":True,"messages":[{"role":"user","content":"pk:\n"+PK}]}, hdr)
    up_log = up_log_text()
    blocked = (r.get("status") == 400) or ("error" in r) or ("私钥" in str(r))
    check("block_rejects_request_400", blocked and PK not in up_log)

    # ⑤ 低置信类别默认关: 邮箱不在请求里时不应误改 (用 dry_run 验证台账无 email 命中)
    open(os.path.join(ISO, "up.log"), "w").close()
    cfg = json.load(open(os.path.join(ISO, "config", "guard.json"), encoding="utf-8"))
    cfg["rules"].pop("private_key", None)
    json.dump(cfg, open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8"))
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    body2 = {"model":"gpt-4o","stream":True,"messages":[{"role":"user","content":"contact a@b.com please"}]}
    adminauth.post_json(base + "/v1/chat/completions", body2, hdr)
    ev = adminauth.get_json(base + "/admin/api/events", hdr)
    check("low_confidence_email_not_triggered_by_default", not any(e.get("kind")=="guard_hit" and "email" in e.get("detail","").lower() for e in ev.get("events", [])))

    # ⑥ 未采集 ≠ 0: 不配置 guard (删文件) 时, 请求体逐字节不变 (再次确认默认关)
    os.remove(os.path.join(ISO, "config", "guard.json"))
    adminauth.post_json(base + "/admin/api/providers/reload", {}, hdr)
    time.sleep(1)
    open(os.path.join(ISO, "up.log"), "w").close()
    adminauth.post_json(base + "/v1/chat/completions", body, hdr)
    up_log = up_log_text()
    check("no_config_means_byte_identical", SK in up_log)

    proc.send_signal(signal.SIGTERM)
    up.shutdown()
    time.sleep(1)
    print("RESULT %d/%d" % (len(fails), 6))
    if fails:
        print("FAILED: " + ", ".join(fails))
        sys.exit(1)
    print("ALL GREEN")

if __name__ == "__main__":
    main()
