# -*- coding: utf-8 -*-
"""实测 CodeBuddy 积分端点 (harness credits.ts 同款):
  POST /v2/billing/meter/get-user-resource  body {}
响应只打印结构与数字, 不落盘 token。同时试 daily-checkin 状态端点看是否有消耗口径。
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

BASE = "https://copilot.tencent.com"
HEADERS = {
    "Authorization": "Bearer " + tok,
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Domain": "copilot.tencent.com",
    "X-Product": "CodeBuddy",
    "X-Product-Code": "codebuddy",
    "user-agent": "CodeBuddyIDE/1.106.1",
}

def post(path, body=None):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body or {}).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        code = getattr(e, "code", None)
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        return code or -1, f"{type(e).__name__}: {e} {body}"

# 1) 余额 (harness fetchCreditBalance 同款)
st, txt = post("/v2/billing/meter/get-user-resource")
print(f"[get-user-resource] HTTP {st}")
if st == 200:
    j = json.loads(txt)
    print("顶层键:", list(j.keys()))
    data = j.get("data")
    if isinstance(data, dict):
        print("data 键:", list(data.keys()))
        # 打印非敏感数字字段概览
        def walk(o, path="", depth=0):
            if depth > 4: return
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, (dict, list)):
                        walk(v, f"{path}.{k}", depth+1)
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        print(f"  {path}.{k} = {v}")
            elif isinstance(o, list):
                print(f"  {path}: list[{len(o)}]")
                if o: walk(o[0], f"{path}[0]", depth+1)
        walk(data)
    else:
        print("data:", str(data)[:600])
else:
    print(txt[:600])

# 2) 签到状态 (顺带看结构, 是否含累计/消耗)
st2, txt2 = post("/v2/billing/meter/checkin-activity-status")
print(f"\n[checkin-activity-status] HTTP {st2}: {txt2[:400]}")
