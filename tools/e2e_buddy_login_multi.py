# -*- coding: utf-8 -*-
"""端到端: **浏览器一键登录**路径的多账号行为 (登录第二个号不得覆盖第一个).

为什么单独有这个探针: 这条路径原先端点写死 `copilot.tencent.com`, 无法被测试 ——
于是 2026-10-07 漏掉一个真 bug: 浏览器登录第二个账号后, 面板仍只显示 1 个
(该路径还在用覆盖式写入). 现在 `AIGATE_BUDDY_ENDPOINT` 可指向 mock, 回归才测得到.

mock 扮演官方端点:
  POST /v2/plugin/auth/state?platform=ide → {code:0, data:{state, authUrl}} (每次给新 state)
  GET  /v2/plugin/auth/token?state=SN     → 首次 {code:0,data:null} (未就绪), 之后给 token

断言 (判据 = 状态 API 与落盘文件, 不读代码):
  ① 登录第 1 个号 → 账号数 1
  ② 再登录第 2 个号 → 账号数 **2** (回归: 覆盖式写入会让它停在 1)
  ③ 顺序 = 优先级 (先登录的在前), 且第一个 active
  ④ 落盘 accounts.json 是数组且两个 token 都在 (凭据不丢)
"""
import base64, io, json, os, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_buddy_login")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8989, 8990

def jwt(sub, iat=1, nickname=None, username=None):
    """造一个 JWT 形状的 token (载荷带 sub/nickname/preferred_username) —— 官方凭据就是 JWT."""
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    claims = {"sub": sub, "iat": iat}
    if nickname: claims["nickname"] = nickname
    if username: claims["preferred_username"] = username
    return b64({"alg": "none", "typ": "JWT"}) + "." + b64(claims) + ".sig"

# state -> 该次登录返回的 token (按账号身份区分: A/B 是两个号)
TOKENS = {"S1": jwt("acct-A", 1, "甲账号", "13800000001"),
          "S2": jwt("acct-B", 1, "乙账号", "13800000002"),
          "S3": jwt("acct-A", 2, "甲账号", "13800000001")}

STATE = {"n": 0}
POLLED = {}   # state -> 已轮询次数 (首次返回 data:null, 模拟"用户还没点完")

class Mock(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        if self.path.startswith("/v2/plugin/auth/state"):
            STATE["n"] += 1
            sid = "S%d" % STATE["n"]
            POLLED[sid] = 0
            return self._json({"code": 0, "data": {"state": sid,
                                                   "authUrl": "http://127.0.0.1:%d/login?state=%s" % (PM, sid)}})
        return self._json({"code": 0, "data": None})
    def do_GET(self):
        # /v2/plugin/auth/token?state=SN
        q = self.path.split("state=")[-1] if "state=" in self.path else ""
        sid = urllib.parse.unquote(q)
        if not sid or sid not in POLLED:
            return self._json({"code": 0, "data": None})
        POLLED[sid] += 1
        if POLLED[sid] < 2:
            return self._json({"code": 0, "data": None})   # 先"未就绪"
        tok = TOKENS.get(sid) or jwt("acct-X", 9)
        return self._json({"code": 0, "data": {"accessToken": tok, "refreshToken": tok + ".rt",
                                               "expiresIn": 4752000}})
    def log_message(self, *a): pass

import urllib.parse
srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "buddyz", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
                          "api_key_env": "K", "api_key_default": "d", "preset": "buddy",
                          "auth": {"mode": "account"}, "models": {}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8").write("{}")

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn",
           AIGATE_NO_PROXY="1",
           AIGATE_BUDDY_ENDPOINT="http://127.0.0.1:%d" % PM)  # 测试接缝: 官方端点指向 mock
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def api(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                               headers={"content-type": "application/json"} if body else {})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")

def tokens_on_disk():
    fj = json.load(io.open(os.path.join(ISO, "data", "accounts.json"), encoding="utf-8"))
    return [x.get("access_token") for x in (fj.get("buddyz") or [])]

def accounts():
    return api("/admin/api/accounts").get("accounts", {}).get("buddyz", [])

def browser_login_once():
    """走完整浏览器登录: start → 轮询 status 到 ok."""
    d = api("/admin/api/accounts/login/start", {"name": "buddyz"})
    assert "auth_url" in d, d
    for _ in range(30):
        time.sleep(1)
        s = api("/admin/api/accounts/login/status", {"name": "buddyz"})
        if s.get("status") == "ok":
            return d["auth_url"]
        if s.get("status") == "failed":
            raise AssertionError("登录失败: %s" % s)
    raise AssertionError("登录轮询超时")

try:
    for _ in range(60):
        try:
            api("/admin/api/version"); break
        except Exception: time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    url1 = browser_login_once()
    a1 = accounts()
    ck("① 浏览器登录第 1 个号 → 账号数 1", len(a1) == 1, [(x.get("id","")[:6]) for x in a1])
    ck("①b start 返回官方登录链接 (来自 mock)", "state=" in url1, url1)
    ck("①c 状态里带账号身份指纹", bool(a1 and a1[0].get("identity")), a1[0].get("identity") if a1 else None)
    ck("①d **状态里带人可读账号名** (昵称/登录名, 不只指纹)",
       bool(a1 and a1[0].get("name") == "甲账号" and a1[0].get("username") == "13800000001"),
       {k: a1[0].get(k) for k in ("name", "username", "identity")} if a1 else None)

    browser_login_once()   # S2 = 另一个账号 (sub 不同)
    a2 = accounts()
    ck("② **登录另一个账号 → 账号数 2** (回归: 覆盖式写入会停在 1)", len(a2) == 2,
       [(x.get("id","")[:6], x.get("identity")) for x in a2])
    ck("③ 顺序 = 优先级 (先登录的在前且 active)",
       len(a2) == 2 and a2[0].get("active") is True and a2[1].get("active") is False,
       [x.get("active") for x in a2])

    # ④ 同一个账号再登录 (token 变了但 sub 相同) → 仍是一条, 只更新凭据
    tok1_before = tokens_on_disk()[0]
    browser_login_once()   # S3 = acct-A 的新 token
    a3 = accounts()
    ck("④ 同一账号重登 (新 token/同 sub) → 仍是 2 条, 不重复堆", len(a3) == 2,
       [(x.get("id","")[:6], x.get("identity")) for x in a3])
    ck("④b 该账号凭据被更新为新 token (确实是重登)",
       tokens_on_disk()[0] != tok1_before, [t[:12] for t in tokens_on_disk()])

    fj = json.load(io.open(os.path.join(ISO, "data", "accounts.json"), encoding="utf-8"))
    arr = fj.get("buddyz") or []
    ck("⑤ 落盘是数组且两条都在", len(arr) == 2, [x.get("identity") for x in arr])
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)