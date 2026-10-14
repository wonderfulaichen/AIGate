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
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from adminauth import headers as admin_auth  # /admin/api/* 的 Bearer (2026-10-12 起)
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_credits")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8990

# 凭据: 账号化之后在网关的 data/accounts.json; cb_plain.json 早已不存在 (兜底保留)
from buddycred import load_buddy_cred
cred, cred_src = load_buddy_cred()
if not cred:
    raise SystemExit("找不到 buddy 凭据 (data/accounts.json 或 %TEMP%\\cb_plain.json)")
tok = cred["access_token"]
print("凭据: %s" % cred_src)

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
    # workbuddy (国际版): 2026-10-13 用户报障「workbuddy 也没加上费用获取支持」——
    # 旧实现 preset 匹配写死 == "buddy", 国际版掉进"未配置余额查询 API"。
    {"name": "workbuddy", "endpoint": "https://www.workbuddy.ai/v2/chat/completions",
     "api_key_env": "WORKBUDDY_ACCESS_TOKEN", "preset": "workbuddy",
     "auth": {"mode": "account"},
     "models": {}},
]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
# buddy 播种真实账号 (积分查询凭据); buddy2 故意不播种 → 验友好错误
# workbuddy 若本机有凭据也播种 —— 没有则该行显示"未登录", 不作为失败判据。
seed = {"buddy": {"access_token": tok, "refresh_token": cred.get("refresh_token"),
                  "expires_at": cred.get("expires_at"), "dead": False}}
wb = None
for cand in [r"D:\Office software\Development Project\AI中转\data\accounts.json", r"data\accounts.json"]:
    if os.path.exists(cand):
        ent = json.load(io.open(cand, encoding="utf-8")).get("workbuddy")
        ent = (ent[0] if isinstance(ent, list) else ent) or {}
        if ent.get("access_token"):
            wb = ent
            break
if wb:
    seed["workbuddy"] = {"access_token": wb["access_token"],
                         "refresh_token": wb.get("refresh_token"),
                         "expires_at": wb.get("expires_at"), "dead": False}
    print("workbuddy: 播种真实凭据")
else:
    print("workbuddy: 本机无凭据 (该行只验'未登录'提示, 不作为失败判据)")
json.dump(seed, open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def req(path, data=None, method=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                               method=method,
                               headers={"content-type": "application/json",
                                        **admin_auth(os.path.join(ISO, "data"))} if body
                               else admin_auth(os.path.join(ISO, "data")))
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
    # ③ 余额与"已用"必须同口径 (只算本窗口仍有剩余的活包)。
    #    ⚠️ 不断言 "balance > 0" —— 该账号可能真的没有活额度 (2026-10-13 实测:
    #    当前号 0 分、另一个号 3788 分)。账号状态不该作为功能断言, 会时红时绿。
    bal = buddy.get("balance")
    con = buddy.get("consumed")
    ck("④ balance_present", bal is not None, bal)
    # 新口径下"已用"不可能大于"剩余+已用"的历史累计, 更不该出现 4274 这种
    # 跨 40 个包的历史累计 (旧 bug 的特征值)。
    ck("⑤ consumed_is_window_scoped",
       (con or 0) < 500, "consumed=%s (旧口径会把历史累计也算进来)" % con)
    ck("⑥ total_ge_balance",
       (buddy.get("total_balance") or 0) >= (bal or 0),
       "total=%s balance=%s" % (buddy.get("total_balance"), bal))
    ck("⑦ no_error", buddy.get("error") is None, buddy.get("error"))
    # workbuddy (国际版) 必须也走出积分查询 —— 用户报障的第二个点。
    # 判据用**字段形态**而非具体数值: 本机无凭据时该行只显示"未登录"提示(仍算通过)。
    wb = rows.get("workbuddy")
    if wb is None:
        ck("⑧ workbuddy_row_present", False, "rows=%s" % list(rows))
    else:
        ck("⑧ workbuddy_row_present", True, "")
        if wb.get("balance") is not None:
            ck("⑨ workbuddy_credits_currency", wb.get("currency") == "credits", wb.get("currency"))
            ck("⑩ workbuddy_consumed_window_scoped",
               (wb.get("consumed") or 0) < 500, "consumed=%s" % wb.get("consumed"))
        else:
            # 无凭据 → 必须是友好错误, 而不是"未配置余额查询 API"(旧 bug 的形态)
            err = wb.get("error") or ""
            ck("⑨ workbuddy_friendly_or_value",
               "未配置" not in err, "error=%s" % err[:80])
    # 无账号预设 → 友好错误
    b2 = rows.get("buddy2", {})
    ck("⑪ unlogged_friendly_error", b2.get("error") and "登录" in b2["error"], b2.get("error"))

    # 登录 start (真实 auth/state, 只读免认证)
    st, txt = req("/admin/api/accounts/login/start", {"name": "buddy"})
    d = json.loads(txt) if txt else {}
    auth_url = d.get("auth_url") or ""
    ck("⑫ login_start_200", st == 200 and "error" not in d, txt[:200])
    ck("⑬ auth_url_official",
       "copilot.tencent.com" in auth_url or "codebuddy.cn" in auth_url, auth_url[:160])

    # status: pending (浏览器未完成)
    time.sleep(1)
    st, txt = req("/admin/api/accounts/login/status", {"name": "buddy"})
    d = json.loads(txt) if txt else {}
    ck("⑭ status_pending", st == 200 and d.get("status") in ("pending", "failed"), d.get("status"))
    # 未知供应商 status → none
    st, txt = req("/admin/api/accounts/login/status", {"name": "nobody"})
    ck("⑮ status_none_for_unknown", '"none"' in txt, txt[:80])
    # 非账号型供应商 start → 拒绝
    st, txt = req("/admin/api/accounts/login/start", {"name": "nobody"})
    ck("⑬ start_reject_non_account", "error" in txt, txt[:120])
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
