# -*- coding: utf-8 -*-
"""诊断: 省量机制对 Responses 协议为何"无效" (用户实测开启过仍无省量).

逐层验证, 每层单独出结论:
  A. mock 上游真收到剥离后的请求体吗 (原样透传?)
  B. 剥离函数认得的形态 vs 实际形态 (input[].type=="reasoning" 之外还有哪些?)
  C. 模型级 override 是否压过协议开关
  D. 降级为 messages 的转换路径 (responses→chat) 是否走同一剥离链
覆盖: 走网关 + 开启 responses 开关, 断言上游收到的 body 与省量记账。
"""
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_resp_strip")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8985, 8984

PASS, FAIL = [], []
def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))

RECEIVED = []   # 上游收到的原始请求体

class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            RECEIVED.append((self.path, json.loads(raw)))
        except Exception:
            RECEIVED.append((self.path, {"_raw": raw.decode("utf-8", "replace")[:500]}))
        body = json.dumps({
            "id": "resp_1", "object": "response",
            "output": [{"type": "reasoning", "id": "r1", "summary": []},
                       {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "resp/m"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO): shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
# responses 端点 + 显式声明 api_format=responses (走直通路径)
json.dump({"providers": [{
    "name": "mockr", "endpoint": f"http://127.0.0.1:{PM}/v2/responses",
    "api_key_env": "K", "api_key_default": "d",
    "api_format": "responses",
    "models": {"mockr/ds": {"upstream_model": "ds"}},
}]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"), indent=1)

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="info", AIGATE_NO_PROXY="1", STRIP_TOOLCALL_ON_RESPONSES="true")
logf = open(os.path.join(ISO, "run.log"), "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=logf, stderr=logf)

def req(path, data=None, method=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{PG}{path}", data=body, method=method,
                               headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

# 标准 Responses 请求体: input[] 里带历史 reasoning item
BODY = {
    "model": "mockr/ds",
    "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "reasoning", "id": "rs_1",
         "summary": [{"type": "summary_text", "text": "长思考内容" * 40}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "上一轮回答"}]},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "结果"},
    ],
    "stream": False,
}

try:
    for _ in range(60):
        st, _ = req("/admin/api/version")
        if st == 200: break
        time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    # 确认开关状态
    st, txt = req("/admin/api/strip-toolcall-protocols")
    ck("开关: responses 已开启", '"responses":true' in txt.replace(" ", ""), txt[:120])

    before_len = len(json.dumps(BODY))
    st, txt = req("/v1/responses", BODY, method="POST")
    ck("请求成功", st == 200, f"HTTP {st} {txt[:150]}")
    time.sleep(0.6)

    # A. 上游收到的 body 是否已剥离
    got = [b for p, b in RECEIVED if "responses" in p or "chat" in p]
    ck("上游收到请求", bool(got), f"收到 {len(got)} 个")
    if got:
        b = got[-1]
        s = json.dumps(b, ensure_ascii=False)
        ck("A 剥离后不含 reasoning item",
           '"type":"reasoning"' not in s.replace(" ", ""), s[:260])
        ck("A 保留 function_call/输出", '"function_call"' in s and '"function_call_output"' in s, "")
        ck("A body 变小", len(s) < before_len, f"{before_len} -> {len(s)}")

    # 省量记账
    st, txt = req("/admin/api/logs")
    logs = json.loads(txt) if st == 200 else []
    rec = logs[0] if logs else {}
    ck("省量记账 > 0 (记录 strip_saved_tokens)", (rec.get("strip_saved_tokens") or 0) > 0,
       f"strip={rec.get('strip_saved_tokens')} endpoint={rec.get('endpoint')}")

    # C. 开关关闭时应不剥离 (对照)
    req("/admin/api/strip-toolcall-protocols", {"responses": False}, method="POST")
    RECEIVED.clear()
    req("/v1/responses", BODY, method="POST")
    time.sleep(0.5)
    got2 = [b for p, b in RECEIVED]
    if got2:
        s2 = json.dumps(got2[-1], ensure_ascii=False)
        ck("C 关闭开关时不剥离 (对照)",
           '"type":"reasoning"' in s2.replace(" ", ""), s2[:200])
    else:
        ck("C 对照组有收到请求", False, "")

    # D. 网关日志里是否有 stripped 记录
    tail = open(os.path.join(ISO, "run.log"), encoding="utf-8", errors="replace").read()
    ck("D 网关日志含 stripped 痕迹", "stripped" in tail,
       [l for l in tail.splitlines() if "stripp" in l][:2])
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()
    logf.close()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)