# -*- coding: utf-8 -*-
"""拉取 CodeBuddy 权威模型配置 GET /v3/config — 找 deepseek-v4-flash 的
maxInputTokens/maxOutputTokens/supportsToolCall/reasoning (harness buddy-adapter.ts:93
声明的权威来源; 本机 CodeBuddy CN main.js 的 CloudProductProvider 也调它)。
凭据只读 %TEMP%\\cb_plain.json, 响应只打印目标模型条目, 不落盘。
"""
import json
import os
import ssl
import sys
import tempfile
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cred = json.load(open(os.path.join(tempfile.gettempdir(), "cb_plain.json"), encoding="utf-8"))
tok = cred["auth"]["accessToken"]

HEADERS = {
    "X-Domain": "copilot.tencent.com",
    "X-Product-Code": "codebuddy",
    "X-Product": "CodeBuddy",
    "X-Agent-Purpose": "conversation",
    "X-IDE-Name": "CodeBuddy",
    "X-IDE-Type": "CodeBuddy",
    "X-IDE-Version": "1.106.1",
    "user-agent": "CodeBuddyIDE/1.106.1",
    "authorization": "Bearer " + tok,
    "accept": "application/json",
}

for path in ("/v3/config", "/plugin/v3/config", "/v3/config?platform=ide"):
    url = "https://copilot.tencent.com" + path
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            raw = r.read().decode("utf-8", "replace")
        print(f"[{path}] HTTP {r.status}, {len(raw)} bytes")
        try:
            j = json.loads(raw)
        except Exception as e:
            print("  非 JSON:", raw[:300]); continue
        # 找 models 数组 (data.models 或 models)
        models = None
        for key_path in (("data", "models"), ("data", "data", "models"), ("models",)):
            cur = j
            for k in key_path:
                cur = cur.get(k) if isinstance(cur, dict) else None
                if cur is None: break
            if isinstance(cur, list):
                models = cur; break
        if models is None:
            print("  未找到 models; 顶层键:", list(j.keys())[:10],
                  "data键:", list((j.get("data") or {}).keys())[:15] if isinstance(j.get("data"), dict) else type(j.get("data")).__name__)
            continue
        print(f"  models 共 {len(models)} 个")
        for m in models:
            if isinstance(m, dict) and "deepseek-v4" in str(m.get("id", m.get("modelId", ""))):
                print("  ── deepseek 条目 ──")
                print(json.dumps(m, ensure_ascii=False, indent=2))
        ids = [m.get("id") or m.get("modelId") for m in models if isinstance(m, dict)]
        print("  id 样例:", ids[:25])
        break
    except Exception as e:
        print(f"[{path}] 失败: {type(e).__name__}: {e}")
