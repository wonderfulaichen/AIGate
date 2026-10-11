# -*- coding: utf-8 -*-
"""探针: POST /v1/messages/count_tokens 是否可用且口径诚实。

背景（2026-10-11 对标 dsh-our-free-model 后补的缺口）:
    Claude Code 等 Anthropic 系客户端发送前会调该端点估算上下文占用;
    我们此前**没有这个路由** → 404（轻则降级、重则报错）。

判据:
  ① 路由存在且返回 200（不再 404）
  ② 返回 `input_tokens` 为正整数
  ③ **汉字不被低估**: 4 个汉字应 ≈4（若按字节/4 会得 3）
  ④ 未知模型仍 404（不能变成绕过路由表的探针）
  ⑤ 打该端点**不产生上游请求**, 也不落请求记录（纯本地估算, 不消耗额度）
  ⑥ 含图片时如实给出 `_note`（不静默给偏小的数）
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_count_tokens")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8971, 8972

FAILS = []
UPSTREAM_CALLS = {"n": 0}


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        UPSTREAM_CALLS["n"] += 1
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        body = json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 0}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        UPSTREAM_CALLS["n"] += 1
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "mock",
        "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mock/m": {"upstream_model": "m"}},
    }]
}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ct(payload, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:%d/v1/messages/count_tokens" % PG,
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("隔离实例未就绪")
        sys.exit(1)

    print("=== POST /v1/messages/count_tokens ===\n")

    before = UPSTREAM_CALLS["n"]
    st, body = ct({"model": "mock/m", "messages": [{"role": "user", "content": "你好世界"}]})
    print("  响应:", json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body)
    ck("① 路由存在且 200 (不再 404)", st == 200, "HTTP %s" % st)
    tok = body.get("input_tokens") if isinstance(body, dict) else None
    ck("② 返回 input_tokens 为正整数", isinstance(tok, int) and tok > 0, "input_tokens=%s" % tok)
    ck("③ 汉字未被低估 (4 个汉字 ≈ 4, 不是按字节算的 3)", tok == 4, "input_tokens=%s" % tok)

    time.sleep(0.6)
    ck("⑤ 打该端点未产生任何上游请求 (纯本地估算)",
       UPSTREAM_CALLS["n"] == before,
       "上游调用数 %d -> %d" % (before, UPSTREAM_CALLS["n"]))

    # ④ 未知模型
    st2, b2 = ct({"model": "no/such-model", "messages": [{"role": "user", "content": "x"}]})
    ck("④ 未知模型仍 404", st2 == 404, "HTTP %s" % st2)

    # ⑥ 含图片 → 如实提示
    st3, b3 = ct({"model": "mock/m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "abcd"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
    ]}]})
    note = (b3 or {}).get("_note") if isinstance(b3, dict) else None
    ck("⑥ 含图片时如实给 _note (不静默给偏小的数)",
       st3 == 200 and isinstance(note, str) and "图片" in note,
       "HTTP %s _note=%s" % (st3, note))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— count_tokens 可用, 口径诚实, 不打上游")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
