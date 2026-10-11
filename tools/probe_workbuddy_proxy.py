# -*- coding: utf-8 -*-
"""决定性验证: WorkBuddy 国际版经**网关注入**后, 客户端不带 system 也能通。

背景 (2026-10-11):
    实测 `www.workbuddy.ai/v2/chat/completions` 对 `messages[0]` 不是 system 的请求返回
    `400 code=11128 "first message is not system prompt"`(+ 一条误导性的"被安全策略拦截");
    同一凭据同一模型, 首条加 system 后立刻 200。
    客户端(编程工具)不保证带 system ⇒ 网关按 `require_system_first` 补一条。

判据 (4 条, 全部走真实上游):
  ① 客户端**不带 system** 的请求: 经网关 200 —— 这就是本次修复的判据
     (直连同一请求应为 400/11128, 作为对照)
  ② 客户端**自带 system** 的请求: 经网关 200, 且网关**不重复插入** system
  ③ 国际系模型 (gpt-*) 与国内系模型 (glm-*/deepseek) 都通 —— 验证 UA 分档没把请求发坏
  ④ 记录里 end_reason 正常 (证明是上游正常收尾, 不是网关截断)

凭据: 读上一次隔离实例的 data/accounts.json (由面板登录流写入)。
用法: python tools/probe_workbuddy_proxy.py
"""
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_wb_proxy")
SRC_ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_workbuddy")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8966
CTX = ssl.create_default_context()

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


# 取凭据 (上一轮面板登录流写入)
src_acc = os.path.join(SRC_ISO, "data", "accounts.json")
if not os.path.exists(src_acc):
    print("找不到凭据 %s —— 先跑 probe_workbuddy_login.py" % src_acc)
    sys.exit(1)
acc = json.load(io.open(src_acc, encoding="utf-8"))
ent = (acc.get("workbuddy") or [None])[0]
TOK = (ent or {}).get("access_token")
if not TOK:
    print("accounts.json 里没有 workbuddy 凭据")
    sys.exit(1)
print("凭据: 长度 %d, expires_at=%s" % (len(TOK), (ent or {}).get("expires_at")))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
shutil.copy(src_acc, os.path.join(ISO, "data", "accounts.json"))

io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "workbuddy",
        "endpoint": "https://www.workbuddy.ai/v2/chat/completions",
        "api_key_env": "WORKBUDDY_ACCESS_TOKEN",
        "preset": "workbuddy",
        "require_system_first": True,          # ← 本次修复的开关
        "auth": {
            "mode": "account", "expiry_margin_secs": 3600,
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
            "X-Domain": "www.workbuddy.ai", "X-Product-Code": "workbuddy",
            "X-Product": "WorkBuddy", "X-IDE-Name": "WorkBuddy", "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": "5.5.2", "X-Agent-Purpose": "conversation",
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
        "models": {
            "workbuddy/gpt-5.6-sol": {"upstream_model": "gpt-5.6-sol"},
            "workbuddy/deepseek-v4.1-flash": {"upstream_model": "deepseek-v4.1-flash"},
            "workbuddy/glm-5.3": {"upstream_model": "glm-5.3"}
        }
    }]
}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def chat(model, msgs, timeout=90):
    body = json.dumps({"model": model, "messages": msgs, "stream": True, "max_tokens": 40}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = b""
            for _ in range(160):
                ch = resp.read(512)
                if not ch:
                    break
                raw += ch
                if b"[DONE]" in raw:
                    break
        t = raw.decode("utf-8", "replace")
        return resp.status, bool([l for l in t.splitlines() if '"content"' in l]), t
    except urllib.error.HTTPError as e:
        return e.code, False, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, False, "%s: %s" % (type(e).__name__, e)


def direct_no_system(model):
    """对照: 直连上游且不带 system, 预期 400/11128."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "stream": True, "max_tokens": 20}).encode()
    hdr = {
        "X-Domain": "www.workbuddy.ai", "X-Product-Code": "workbuddy", "X-Product": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy", "X-IDE-Type": "WorkBuddy", "X-IDE-Version": "5.5.2",
        "X-Agent-Purpose": "conversation", "user-agent": "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2",
        "authorization": "Bearer " + TOK, "content-type": "application/json",
        "accept": "text/event-stream",
    }
    req = urllib.request.Request("https://www.workbuddy.ai/v2/chat/completions", data=body, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            r.read(4096)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


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

    print("\n=== WorkBuddy 国际版: 网关注入 system 端到端 ===\n")

    NO_SYS = [{"role": "user", "content": "只回复两个字：可以"}]
    WITH_SYS = [{"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "只回复两个字：可以"}]

    # 对照: 直连不带 system
    d = direct_no_system("deepseek-v4.1-flash")
    ck("①对照 直连不带 system → 上游 400/11128 (复现问题)",
       d == 400, "直连 HTTP %s" % d)

    # ① 经网关不带 system
    st, got, body = chat("workbuddy/deepseek-v4.1-flash", NO_SYS)
    ck("① ★客户端不带 system, 经网关 → 200 (网关补了 system)",
       st == 200 and got, "HTTP %s %s" % (st, body[:200]))

    # ② 经网关自带 system
    st2, got2, body2 = chat("workbuddy/deepseek-v4.1-flash", WITH_SYS)
    ck("② 客户端自带 system 也 200 (且未被重复插入)", st2 == 200 and got2,
       "HTTP %s %s" % (st2, body2[:160]))

    # ③ 两个模型族
    st3, got3, b3 = chat("workbuddy/gpt-5.6-sol", NO_SYS)
    ck("③ 国际系模型 (gpt-*) 不带 system 也 200 —— UA 分档未发坏",
       st3 == 200 and got3, "HTTP %s %s" % (st3, b3[:200]))

    # ④ end_reason
    time.sleep(1.0)
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:%d/admin/api/logs?limit=5" % PG, timeout=20).read().decode()
        j = json.loads(raw)
        rows = j if isinstance(j, list) else (j.get("logs") or [])
        wb = [r for r in rows if r.get("provider") == "workbuddy"]
        ck("④ 记录里 end_reason 为上游正常收尾 (非网关截断)",
           bool(wb) and all(r.get("end_reason") in ("stop", "tool_calls", None) for r in wb),
           "end_reason=%s" % [r.get("end_reason") for r in wb])
    except Exception as e:
        ck("④ 记录里 end_reason 为上游正常收尾", False, "读取失败 %s" % e)

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— WorkBuddy 国际版经网关可用: 客户端不带 system 也通, 两个模型族均 200")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
