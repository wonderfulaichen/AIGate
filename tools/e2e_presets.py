# -*- coding: utf-8 -*-
"""端到端: 内置预设供应商 (preset 标记) + 面板登录/登出 + 停用 —— 2026-10-01 需求轮.

覆盖 (HANDOFF §8 内置预设轮):
  ① GET  /admin/api/presets           内置目录下发 (buddy, account=true, auth 段齐)
  ② providers/save 删除 preset 条目   → 后端兜底拒绝「内置预设不可删除」
  ③ providers/save disabled=true      → 200, 模型路由消失 (404) 且条目保留; 改回 false 恢复
  ④ POST /admin/api/accounts/login    → 落盘 accounts.json (token/refresh/到期), 状态 API 可见
  ⑤ 对非账号供应商 login               → 明确报错
  ⑥ POST /admin/api/accounts/logout   → 账号删除 + 落盘

隔离实例, 不碰部署目录 (红线: AI 只构建不部署).
用法: python tools/e2e_presets.py
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_presets")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8997, 8998          # 避开 8991/8992 (reccount) 与 8995/8996 (accounts)

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
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

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))

PROV = {"providers": [
    {"name": "buddyx", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "preset": "buddy",
     "auth": {"mode": "account", "refresh": {"endpoint": "http://127.0.0.1:%d/refresh" % PM,
                                              "method": "POST",
                                              "headers": {"X-Refresh-Token": "{refresh_token}"}}},
     "models": {"buddyx/m": {"upstream_model": "m"}}},
    {"name": "plain2", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "models": {"plain2/m": {"upstream_model": "m"}}},
]}
PJ = os.path.join(ISO, "providers.json")
io.open(PJ, "w", encoding="utf-8").write(json.dumps(PROV, indent=2))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def save_providers(prov_obj):
    return req("/admin/api/providers/save", {"json": json.dumps(prov_obj)})


def chat(model):
    return req("/v1/chat/completions",
               {"model": model, "messages": [{"role": "user", "content": "hi"}]})


try:
    for _ in range(60):
        st, _ = req("/admin/api/version")
        if st == 200:
            break
        time.sleep(0.5)
    else:
        ck("instance_ready", False, "30s not ready")
        raise SystemExit(1)

    # ① 预设目录
    st, txt = req("/admin/api/presets")
    ok = st == 200 and '"buddy"' in txt and '"account":true' in txt.replace(" ", "")
    ck("presets_api", ok, txt[:200])
    ck("presets_has_refresh", "plugin/auth/token/refresh" in txt, "")

    # ② 删除保护: 从 providers 数组里拿掉 buddyx → 拒绝
    st, txt = save_providers({"providers": [PROV["providers"][1]]})
    ck("preset_delete_blocked", st == 200 and "不可删除" in txt and "error" in txt, txt[:200])
    # 文件未被坏保存覆盖
    disk = json.load(io.open(PJ, encoding="utf-8"))
    ck("preset_still_on_disk", any(p.get("preset") == "buddy" for p in disk["providers"]))

    # ③ 停用: disabled=true → 模型 404 且条目保留; 改回恢复
    p2 = json.loads(json.dumps(PROV))
    p2["providers"][0]["disabled"] = True
    st, txt = save_providers(p2)
    ck("disable_save", st == 200 and "error" not in txt, txt[:200])
    time.sleep(0.3)
    st, _ = chat("buddyx/m")
    ck("disabled_model_404", st == 404, st)
    st, _ = chat("plain2/m")
    ck("other_provider_ok", st == 200, st)
    p2["providers"][0]["disabled"] = False
    st, txt = save_providers(p2)
    ck("enable_save", st == 200 and "error" not in txt, txt[:200])
    time.sleep(0.3)
    st, _ = chat("buddyx/m")
    ck("reenabled_model_ok", st == 200, st)

    # ④ 登录: 落盘 + 脱敏状态
    st, txt = req("/admin/api/accounts/login",
                  {"name": "buddyx", "access_token": "TOK_E2E_1",
                   "refresh_token": "RT_E2E_1", "expires_in_secs": 86400})
    ck("login_api", st == 200 and "error" not in txt, txt[:200])
    ck("login_redacted", "TOK_E2E_1" not in txt and "RT_E2E_1" not in txt, txt[:300])
    fj = json.load(io.open(os.path.join(ISO, "data", "accounts.json"), encoding="utf-8"))
    # 多账号 (2026-10-07): 值为账号数组
    b0 = (fj.get("buddyx") or [{}])[0]
    ck("login_persisted", b0.get("access_token") == "TOK_E2E_1"
       and b0.get("refresh_token") == "RT_E2E_1", list(fj.keys()))
    st, txt = req("/admin/api/accounts")
    ck("login_status_visible", '"buddyx"' in txt and '"dead":false' in txt.replace(" ", ""), txt[:200])

    # ⑤ 非账号供应商 → 拒绝
    st, txt = req("/admin/api/accounts/login", {"name": "plain2", "access_token": "X"})
    ck("login_reject_non_account", st == 200 and "error" in txt, txt[:200])

    # ⑥ 登出 → 账号删除
    st, txt = req("/admin/api/accounts/logout", {"name": "buddyx"})
    ck("logout_api", st == 200 and "error" not in txt, txt[:200])
    fj = json.load(io.open(os.path.join(ISO, "data", "accounts.json"), encoding="utf-8"))
    ck("logout_persisted", "buddyx" not in fj, list(fj.keys()))
    st, txt = req("/admin/api/accounts")
    ck("logout_status_gone", '"buddyx"' not in txt, txt[:200])

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
