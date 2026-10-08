# -*- coding: utf-8 -*-
"""实测: 网关转发 OpenCode(zen/go) 请求时, 客户端的 x-opencode-* 头有没有被丢掉.

背景 (2026-10-08 读 zen-gate / dsh-zen-proxy 后的怀疑):
  上游 zen/go 免费车道按 **session** 计配额, 且实测「六个身份头缺一不可」:
      user-agent: opencode/<ver>=1.18.0>   ← 缺 → 403 FreeTierError
      authorization: Bearer public
      x-opencode-client / x-opencode-project / x-opencode-session / x-opencode-request
  而我们的 `filter_headers` 实测**只放行 user-agent**, 其余全部丢弃 —— 代码里有个
  `inject_opencode_session` 补救(从原始头取回客户端值), 但它的判据 `is_opencode_go`
  **只认 name=="go" 或 endpoint 含 opencode.ai/zen/go** → `zen` 供应商不适用。

判据: mock 上游收到的 headers (上游实际看到什么), 不看代码.

断言:
  ① zen provider 下, 客户端带的 x-opencode-session 必须原样到达上游
  ② go  provider 下同样 (既有补救路径的回归)
  ③ 客户端没带时, 网关应兜底注入 (否则上游把每个请求当"无会话")
  ④ 其它 x-opencode-* 头 (client/project/request) 也应透传 —— 它们是身份的一部分
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_oc_headers")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8985, 8986
SEEN = []   # 每个请求收到的 headers (只留关心的几个)

class Mock(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        return self._json(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                             "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
# 两个供应商: zen 与 go, 都指向 mock (用 endpoint 里的 opencode.ai 字样无关紧要, 我们测 header 行为)
json.dump({"providers": [
    {"name": "zen", "endpoint": "http://127.0.0.1:%d/zen/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "public",
     "models": {"zen/m": {"upstream_model": "m"}}},
    {"name": "go", "endpoint": "http://127.0.0.1:%d/zen/go/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "public",
     "models": {"go/m": {"upstream_model": "m"}}},
]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def chat(model, extra_headers=None):
    SEEN.clear()
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()
    h = {"content-type": "application/json"}
    h.update(extra_headers or {})
    r = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body, headers=h)
    with urllib.request.urlopen(r, timeout=20) as resp:
        resp.read()
    time.sleep(0.2)
    return SEEN[-1] if SEEN else {}

try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/admin/api/version" % PG, timeout=2).read(); break
        except Exception: time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    CLIENT_SES = "ses_abcdef123456" + "A" * 10
    hdrs = {
        "x-opencode-session": CLIENT_SES,
        "x-opencode-client": "desktop",
        "x-opencode-project": "global",
        "x-opencode-request": "msg_abcdef123456" + "B" * 10,
    }

    # ① zen: 客户端自带的 session 必须到达上游
    got = chat("zen/m", hdrs)
    ck("① zen: 客户端 x-opencode-session 透传到上游", got.get("x-opencode-session") == CLIENT_SES,
       f"上游收到={got.get('x-opencode-session')!r}")
    ck("①b zen: x-opencode-client / project / request 也透传",
       got.get("x-opencode-client") == "desktop" and got.get("x-opencode-project") == "global"
       and got.get("x-opencode-request") == hdrs["x-opencode-request"],
       {k: got.get(k) for k in ("x-opencode-client", "x-opencode-project", "x-opencode-request")})

    # ② go: 同一组头 (既有补救路径回归)
    got = chat("go/m", hdrs)
    ck("② go: 客户端 x-opencode-session 透传 (既有补救路径)", got.get("x-opencode-session") == CLIENT_SES,
       f"上游收到={got.get('x-opencode-session')!r}")

    # ③ 客户端不带时, 网关应兜底 (否则上游把每个请求当"无会话")
    got_z = chat("zen/m")
    got_g = chat("go/m")
    ck("③ zen: 客户端未带 session → 网关兜底注入",
       bool(got_z.get("x-opencode-session")), f"上游收到={got_z.get('x-opencode-session')!r}")
    ck("③b go: 客户端未带 session → 网关兜底注入",
       bool(got_g.get("x-opencode-session")), f"上游收到={got_g.get('x-opencode-session')!r}")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)