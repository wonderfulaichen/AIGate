# -*- coding: utf-8 -*-
"""端到端: buddy 积分余额 + 浏览器登录 start/status (2026-10-01 两功能行为验收).

  ① GET /admin/api/balance → buddy 行 currency=credits / balance>0 / consumed 非空
     (打真实积分端点 /v2/billing/meter/get-user-resource, 只读)
  ② POST /admin/api/accounts/login/start → 200 + auth_url 指向官方登录页 (拿 state)
  ③ POST /admin/api/accounts/login/status → pending (浏览器未完成登录, 属预期)
  ④ 未登录账号的积分查询 → 友好错误而非崩溃 (第二家预设无账号)

凭据: %TEMP%\\cb_plain.json; 隔离实例, 不碰部署目录.
用法: python tools/e2e_buddy_credits_login.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_credits")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8990

cred = json.load(open(os.path.join(tempfile.gettempdir(), "cb_plain.json"), encoding="utf-8"))
tok = cred["auth"]["accessToken"]

PASS, FAIL = [], []
def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [
    {"name": "buddy", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d", "preset": "buddy",
     "auth": {"mode": "account"},
     "models": {}},
    {"name": "buddy2", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d", "preset": "buddy",
     "auth": {"mode": "account"},
     "models": {}},
]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
# buddy 播种真实账号 (积分查询凭据); buddy2 故意不播种 → 验友好错误
json.dump({"buddy": {"access_token": tok, "refresh_token": cred["auth"].get("refreshToken"),
                     "expires_at": int(cred["auth"]["expiresAt"] // 1000), "dead": False}},
          open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def req(path, data=None, method=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                               method=method,
                               headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

try:
    for _ in range(60):
        st, _ = req("/admin/api/version")
        if st == 200:
            break
        time.sleep(0.5)
    else:
        ck("instance", False, "30s 未就绪"); raise SystemExit(1)

    # ① 积分余额 (真实只读端点)
    st, txt = req("/admin/api/balance")
    data = json.loads(txt) if st == 200 else {}
    rows = {b["provider"]: b for b in data.get("balances", [])}
    buddy = rows.get("buddy", {})
    ck("① balance_api_200", st == 200 and "buddy" in rows, f"status={st} rows={list(rows)}")
    ck("② credits_currency", buddy.get("currency") == "credits", buddy.get("currency"))
    ck("③ remaining_positive", (buddy.get("balance") or 0) > 0, buddy.get("balance"))
    ck("④ consumed_present", buddy.get("consumed") is not None, buddy.get("consumed"))
    ck("⑤ no_error", buddy.get("error") is None, buddy.get("error"))
    # ④ 无账号预设 → 友好错误
    b2 = rows.get("buddy2", {})
    ck("⑥ unlogged_friendly_error", b2.get("error") and "登录" in b2["error"], b2.get("error"))

    # ② 登录 start (真实 auth/state, 只读免认证)
    st, txt = req("/admin/api/accounts/login/start", {"name": "buddy"})
    d = json.loads(txt) if txt else {}
    auth_url = d.get("auth_url") or ""
    ck("⑦ login_start_200", st == 200 and "error" not in d, txt[:200])
    ck("⑧ auth_url_official",
       "copilot.tencent.com" in auth_url or "codebuddy.cn" in auth_url, auth_url[:160])

    # ③ status: pending (浏览器未完成)
    time.sleep(1)
    st, txt = req("/admin/api/accounts/login/status", {"name": "buddy"})
    d = json.loads(txt) if txt else {}
    ck("⑨ status_pending", st == 200 and d.get("status") in ("pending", "failed"), d.get("status"))
    # 未知供应商 status → none
    st, txt = req("/admin/api/accounts/login/status", {"name": "nobody"})
    ck("⑩ status_none_for_unknown", '"none"' in txt, txt[:80])
    # 非账号型供应商 start → 拒绝
    st, txt = req("/admin/api/accounts/login/start", {"name": "nobody"})
    ck("⑪ start_reject_non_account", "error" in txt, txt[:120])
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
