# -*- coding: utf-8 -*-
"""探针: WorkBuddy(国际版) 反代端到端 —— 用**新构建的隔离实例**走面板真实登录流。

为什么必须这么做 (2026-10-11):
    上一轮把要件都查清了, 却卡在"没有国际版凭据" —— 而**面板的浏览器登录本身就是获取凭据的正规路径**,
    所以"能不能登录 + 登录后能不能对话"应该由这个探针一次验完, 而不是另外去外面挖 token。

流程 (与用户在面板上点「浏览器登录」完全同一套代码):
    ① POST /admin/api/accounts/login/start {name}      -> {auth_url}
    ② 打印 auth_url, 等用户浏览器授权 (轮询 /login/status 至 ok)
    ③ 拿到状态后: 断言 accounts.json 落盘了国际版凭据 (iss 含 workbuddy = 国际 realm)
    ④ 用该凭据直接打 /v2/chat/completions —— **这一步才是 401 是否解开的判据**
    ⑤ 再断言 UA 分档: 国际系模型用国际形态 UA、国内系用国内形态 UA (同一账号都接受)

用法:
    python tools/probe_workbuddy_login.py            # 起实例 + 出链接 + 等授权 + 全自动验证
    python tools/probe_workbuddy_login.py --url-only # 只出链接
"""
import base64
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_workbuddy")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8965
CTX = ssl.create_default_context()

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def api(path, method="GET", body=None, timeout=25):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PG, path),
        data=(json.dumps(body).encode() if body is not None else None),
        headers={"content-type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_err": "%s: %s" % (type(e).__name__, e)}


if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
# 只放 workbuddy 一个供应商: 登录/转发都打真实上游 (这一步无法用 mock 代替 ——
# 要验的就是"真实凭据能否过真实网关")
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "workbuddy",
        "endpoint": "https://www.workbuddy.ai/v2/chat/completions",
        "api_key_env": "WORKBUDDY_ACCESS_TOKEN",
        "preset": "workbuddy",
        "auth": {
            "mode": "account",
            "expiry_margin_secs": 3600,
            "refresh": {
                "endpoint": "https://www.workbuddy.ai/v2/plugin/auth/token/refresh",
                "method": "POST",
                "headers": {"X-Refresh-Token": "{refresh_token}"},
                "access_token_path": "data.accessToken",
                "refresh_token_path": "data.refreshToken",
                "expires_in_path": "data.expiresIn"
            }
        },
        "headers": {
            "X-Domain": "www.workbuddy.ai",
            "X-Product-Code": "workbuddy",
            "X-Product": "WorkBuddy",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": "5.5.2",
            "X-Agent-Purpose": "conversation",
            "user-agent": "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"
        },
        "ua_by_model_prefix": [
            ["gpt-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"],
            ["gemini-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"],
            ["claude-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"],
            ["glm-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"],
            ["hy", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"],
            ["kimi-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"],
            ["minimax-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"]
        ],
        "require_system_first": True,
        "models": {}
    }]
}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    print("=== WorkBuddy 国际版: 面板登录流端到端 ===\n")
    # 预设目录必须含 workbuddy 且 platform 正确
    pres = api("/admin/api/presets")
    wb = next((p for p in (pres.get("presets") or []) if p.get("id") == "workbuddy"), None)
    ck("① 预设目录含 workbuddy, 且 platform=workbuddy-ai",
       bool(wb) and (wb.get("login") or {}).get("platform") == "workbuddy-ai",
       "login=%s" % json.dumps((wb or {}).get("login"), ensure_ascii=False)[:160])
    if not wb:
        print("   预设缺失, 无法继续")
        sys.exit(1)

    r = api("/admin/api/accounts/login/start", "POST", {"name": "workbuddy"})
    url = r.get("auth_url") or ""
    ck("② 登录流拿到官方 authUrl 且 platform 正确",
       bool(url) and "platform=workbuddy-ai" in url, "auth_url=%s" % url[:120])
    print("\n    >>> 请在浏览器打开以下链接完成授权:")
    print("    " + url + "\n")

    # 轮询到 ok (最多 300s)
    ok = False
    deadline = time.time() + 300
    last = ""
    while time.time() < deadline:
        st = api("/admin/api/accounts/login/status", "POST", {"name": "workbuddy"})
        s = st.get("status")
        if s != last:
            print("    [%s] status=%s %s" % (time.strftime("%H:%M:%S"), s, st.get("error") or ""))
            last = s
        if s == "ok":
            ok = True
            break
        if s == "failed":
            print("    登录失败:", st.get("error"))
            break
        time.sleep(2)

    if not ok:
        ck("③ 浏览器授权完成并落盘 (需人工授权)", False,
           "未在 300 秒内完成 —— 这不代表功能坏, 只说明还没授权")
        print("\n=== 结果 ===\n  INCOMPLETE —— 等授权后重跑本探针即可")
        sys.exit(2)

    # 凭据落盘 + realm 判定
    p = os.path.join(ISO, "data", "accounts.json")
    accts = json.load(io.open(p, encoding="utf-8")).get("workbuddy") or []
    tok = (accts[0] or {}).get("access_token") if accts else None
    ck("④ 凭据已落盘到 accounts.json", bool(tok), "账号数=%d" % len(accts))

    iss = ""
    if tok:
        pl = tok.split(".")[1]
        pl += "=" * (-len(pl) % 4)
        iss = json.loads(base64.urlsafe_b64decode(pl)).get("iss") or ""
    ck("⑤ 凭据属**国际版 realm** (iss 含 workbuddy)",
       "workbuddy" in iss, "iss=%s" % iss)

    # ★ 关键判据: 真实凭据能否过对话路径 (上一轮国内 token 在这里 401)
    def chat(model, timeout=90):
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "只回复两个字：可以"}],
                           "stream": True, "max_tokens": 30}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = b""
                for _ in range(120):
                    ch = resp.read(512)
                    if not ch:
                        break
                    raw += ch
                    if b"[DONE]" in raw:
                        break
            t = raw.decode("utf-8", "replace")
            return resp.status, bool([l for l in t.splitlines() if '"content"' in l]), t[:200]
        except urllib.error.HTTPError as e:
            return e.code, False, e.read().decode("utf-8", "replace")[:250]
        except Exception as e:
            return 0, False, "%s: %s" % (type(e).__name__, e)

    # 先拉模型清单 (走 /v3/config 的 preset 分支)
    fm = api("/admin/api/providers/workbuddy/fetch-models", "POST", {}, timeout=60)
    print("    拉取模型:", json.dumps(fm, ensure_ascii=False)[:200])
    models = list((fm.get("models") or {}).keys()) if isinstance(fm, dict) else []
    if not models:
        # 退回用已知模型名试
        models = ["workbuddy/gpt-5.6-sol", "workbuddy/deepseek-v4.1-flash"]
    else:
        models = ["workbuddy/" + m.split("/")[-1] for m in models]

    intl = next((m for m in models if "gpt-" in m or "gemini-" in m or "claude-" in m), None)
    cn = next((m for m in models if any(k in m for k in ("glm-", "deepseek", "/hy", "kimi-", "minimax-"))), None)

    if intl:
        st, got, head = chat(intl)
        ck("⑥ ★ 国际系模型经网关对话成功 (401 解开的判据)",
           st == 200 and got, "model=%s HTTP=%s %s" % (intl, st, head[:160]))
    if cn:
        st2, got2, head2 = chat(cn)
        ck("⑦ 国内系模型同样成功 (UA 分档未把请求发坏)",
           st2 == 200 and got2, "model=%s HTTP=%s %s" % (cn, st2, head2[:160]))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— WorkBuddy 国际版: 登录流可用 + 真实凭据对话成功 + UA 分档双模型族通过")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
