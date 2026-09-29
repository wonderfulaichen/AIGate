# -*- coding: utf-8 -*-
"""端到端: 上游返回笼统 400 时, 客户端看到的错误是否带上"请求体多大"的提示。

为什么必须端到端: 这条修复的价值全在"用户能不能从错误里看出真实原因",
单测只验证了 oversize_hint 自身的阈值与措辞。
两条对照: 小体积 400 不应有提示; 大体积 400 必须有提示且数字正确。
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_hint")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT_GW, PORT_MOCK = 8951, 8952

# 上游: 无论收到什么, 一律回那条实测见过的笼统 400
BODY = json.dumps({"error": {"message": "invalid request error trace_id: 454a6fc99e7de02fc5cb486f2ab10fd4",
                             "type": "invalid_request_error"}}).encode()


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        self.rfile.read(n)
        self.send_response(400)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

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
    "models": {"mock/big": {"upstream_model": "big"},
               "mock/small": {"upstream_model": "small"}},
}]}))

env = dict(os.environ)
env.update(PORT=str(PORT_GW), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def post(model, pad_bytes):
    body = {"model": model, "stream": False,
            "messages": [{"role": "user", "content": "x" * pad_bytes}]}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PORT_GW,
                                data=json.dumps(body).encode(),
                                headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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

    ok = True
    print("=== 小请求体 (~2KB) 的 400 ===")
    st, txt = post("mock/small", 2000)
    print("  status=%s" % st)
    print("  %s" % txt[:260])
    if "MB" in txt:
        print("  [X] 小体积不该出现体积提示"); ok = False
    else:
        print("  OK  无体积提示 (不产生噪声)")

    print("\n=== 大请求体 (~2.4MB) 的 400 ===")
    st2, txt2 = post("mock/big", 2_400_000)
    print("  status=%s" % st2)
    print("  %s" % txt2[:400])
    if "MB" in txt2 and "tokens" in txt2:
        print("  OK  带上了请求体体积与量级提示")
    else:
        print("  [X] 缺少体积提示"); ok = False

    print("\n=== 结果: %s ===" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
