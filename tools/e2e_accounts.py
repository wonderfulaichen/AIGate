# -*- coding: utf-8 -*-
"""端到端: 账号化供应商配置 (auth.mode=account) —— 续期链 / 面板状态 API / key 链回落.

覆盖 (对应 HANDOFF §8 账号化核心机制):
  ① GET  /admin/api/accounts        状态视图 (脱敏: 响应全文不得含任何 token 原文)
  ② POST /admin/api/accounts/refresh 强制续期 -> mock 收到 X-Refresh-Token,
                                     accounts.json 落新 access_token, expires_at 更新
  ③ 转发 Authorization 账号优先      上游收到 Bearer <新token> (而非 api_key_default)
  ④ 无 auth 供应商走原 api_key 链    (回归: 账号化不碰存量行为)
  ⑤ 续期 401 -> dead 置位, 状态 API 可见, 且**转发回落 key 链不中断**
  ⑥ 对非账号供应商调 refresh -> 明确报错

隔离实例 + mock 上游, 不碰部署目录 (红线: AI 只构建不部署).
用法: python tools/e2e_accounts.py
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_accounts")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8995, 8996          # 网关 / mock (避开 e2e_record_count 的 8991/8992)

MODE = {"refresh": "ok"}      # ok / 401
REFRESH_HITS = []             # (path, x_refresh_token)
CHAT_AUTH = []                # 上游收到的 Authorization

OLD_TOK, NEW_TOK = "OLDTOK_SEED", "NEWTOK_AFTER_REFRESH"
RT1, RT2 = "RT_SEED_1", "RT_ROTATED_2"


class Mock(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        if self.path.endswith("/refresh"):
            REFRESH_HITS.append((self.path, self.headers.get("X-Refresh-Token")))
            if MODE["refresh"] == "401":
                return self._json(401, {"code": 401, "msg": "refresh token invalid"})
            return self._json(200, {"code": 0, "data": {
                "accessToken": NEW_TOK, "refreshToken": RT2, "expiresIn": 3600}})
        # chat 上游
        CHAT_AUTH.append(self.headers.get("authorization"))
        body = json.dumps({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ── 隔离目录 ──
if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))

base_ep = "http://127.0.0.1:%d" % PM
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "acct", "endpoint": base_ep + "/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d",
     "auth": {"mode": "account", "expiry_margin_secs": 3600,
              "refresh": {"endpoint": base_ep + "/refresh", "method": "POST",
                          "headers": {"X-Refresh-Token": "{refresh_token}"}}},
     "models": {"acct/m": {"upstream_model": "m"}}},
    {"name": "plain", "endpoint": base_ep + "/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d",
     "models": {"plain/m": {"upstream_model": "m"}}},
]}, indent=2))

# 播种: acct 已过期 (硬过期 -> 请求/续期都必须真打 refresh)
io.open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8").write(json.dumps({
    "acct": {"access_token": OLD_TOK, "refresh_token": RT1,
             "expires_at": int(time.time()) - 10, "dead": False},
}, indent=2))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def _auth():
    """每次调用都重读 admin token：隔离实例启动时才创建 data/.admin_token，
    导入期读一次会拿到空（/admin/api/* 现已整体要求 Bearer，见 main.rs::require_admin_token）。"""
    try:
        tok = io.open(os.path.join(ISO, "data", ".admin_token"), encoding="utf-8").read().strip()
    except Exception:
        return {}
    return {"Authorization": "Bearer " + tok} if tok else {}


def req(path, data=None, method=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    h = {"content-type": "application/json"} if body else {}
    h.update(_auth())
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                               method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def chat(model):
    st, txt = req("/v1/chat/completions",
                  {"model": model, "messages": [{"role": "user", "content": "hi"}]})
    return st, txt


try:
    # 等就绪
    for _ in range(60):
        st, _ = req("/admin/api/version")
        if st == 200:
            break
        time.sleep(0.5)
    else:
        ck("instance_ready", False, "网关 30s 未就绪")
        raise SystemExit(1)

    # ① 状态视图脱敏
    st, txt = req("/admin/api/accounts")
    leaks = [s for s in (OLD_TOK, NEW_TOK, RT1, RT2) if s in txt]
    ck("accounts_api_status", st == 200 and '"acct"' in txt, st)
    ck("accounts_api_redacted", not leaks, "leak=%s" % leaks)
    ck("accounts_api_pre_refresh", st == 200 and '"dead":false' in txt.replace(" ", ""), txt[:200])

    # ② 强制续期
    st, txt = req("/admin/api/accounts/refresh", {"name": "acct"})
    ck("refresh_api_ok", st == 200 and "error" not in txt, txt[:200])
    ck("refresh_hit_mock", any(h[1] == RT1 for h in REFRESH_HITS), REFRESH_HITS)
    fj = json.load(io.open(os.path.join(ISO, "data", "accounts.json"), encoding="utf-8"))
    # 多账号 (2026-10-07): 每个供应商的值是**账号数组** (旧文件加载时自动升级).
    acc0 = fj["acct"][0]
    ck("refresh_persisted", acc0["access_token"] == NEW_TOK
       and acc0["refresh_token"] == RT2, acc0.get("access_token"))
    ck("refresh_expiry_advanced", (acc0.get("expires_at") or 0) > int(time.time()) + 3000)

    # ③ 转发: 账号 token 优先于 api_key_default
    CHAT_AUTH.clear()
    st, txt = chat("acct/m")
    ck("chat_via_account", st == 200 and CHAT_AUTH and CHAT_AUTH[-1] == "Bearer " + NEW_TOK,
       "%s / %s" % (st, CHAT_AUTH[-1] if CHAT_AUTH else None))

    # ④ 无 auth 供应商: 原 key 链回归
    CHAT_AUTH.clear()
    st, txt = chat("plain/m")
    ck("chat_plain_keychain", st == 200 and CHAT_AUTH and CHAT_AUTH[-1] == "Bearer d",
       "%s / %s" % (st, CHAT_AUTH[-1] if CHAT_AUTH else None))

    # ⑤ 401 -> dead + 转发回落 key 链
    MODE["refresh"] = "401"
    st, txt = req("/admin/api/accounts/refresh", {"name": "acct"})
    ck("refresh_401_error", st == 200 and "error" in txt, txt[:200])
    st, txt = req("/admin/api/accounts")
    ck("dead_flagged", '"dead":true' in txt.replace(" ", ""), txt[:300])
    CHAT_AUTH.clear()
    st, txt = chat("acct/m")
    ck("dead_fallback_keychain", st == 200 and CHAT_AUTH and CHAT_AUTH[-1] == "Bearer d",
       "%s / %s" % (st, CHAT_AUTH[-1] if CHAT_AUTH else None))

    # ⑥ 非账号供应商调 refresh -> 明确报错
    st, txt = req("/admin/api/accounts/refresh", {"name": "plain"})
    ck("refresh_reject_non_account", st == 200 and "error" in txt, txt[:200])

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
