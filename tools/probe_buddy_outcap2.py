# -*- coding: utf-8 -*-
"""决定性探测: 造一个**必然超 8192 token** 的输出, max_tokens=32768, 看平台放不放行.

判定:
  · finish='tool_calls' 且帧数 > 9000 且 json_ok → 平台放行 >8192, 客户端把 max_tokens
    设大即可绕开截断 (上一轮 8192 撞墙是**脚本自己设的**);
  · finish='length' 且帧数 ≈ 8192 → 平台硬顶 8192, 客户端只能分块写/换供应商。
"""
import http.client
import json
import os
import ssl
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CRED = os.path.join(tempfile.gettempdir(), "cb_plain.json")
cred = json.load(open(CRED, encoding="utf-8"))
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
    "content-type": "application/json",
    "authorization": "Bearer " + tok,
    "accept": "text/event-stream",
}

body = {
    "model": "deepseek-v4-flash",
    "stream": True,
    "messages": [
        {"role": "system", "content": "你是一个编程助手。用 write_file 工具写出完整文件，不要省略、不要截断、不要用省略号。"},
        {"role": "user", "content": "用 write_file 写文件 big_probe.txt：content 必须是 6000 行"
                                    "日志样例（每行形如 `2026-10-01T12:00:00Z level=INFO svc=probe-42 "
                                    "msg=step completed idx=N dur_ms=123`，idx 从 1 递增到 6000，"
                                    "逐行完整写出，不许合并、不许省略）。path 填 `big_probe.txt`。"},
    ],
    "tools": [{"type": "function", "function": {
        "name": "write_file",
        "description": "写入一个文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }}],
    "tool_choice": "auto",
}

# max_tokens 可用命令行覆盖: 带值探测该值; "none" 探测平台默认
# 用法: python probe_buddy_outcap2.py [32768|none]
_arg = sys.argv[1] if len(sys.argv) > 1 else "32768"
if _arg.lower() != "none":
    body["max_tokens"] = int(_arg)
print(f"探测: max_tokens={body.get('max_tokens', '<不带, 平台默认>')}")

conn = http.client.HTTPSConnection("copilot.tencent.com", 443,
                                   context=ssl.create_default_context(), timeout=600)
t0 = time.time()
conn.request("POST", "/v2/chat/completions", body=json.dumps(body).encode(), headers=HEADERS)
r = conn.getresponse()
print("HTTP", r.status)
if r.status != 200:
    print(r.read(400))
    sys.exit(0)
finish = None
frames = 0
reasoning = content = 0
acc = []
while True:
    line = r.readline()
    if not line:
        break
    s = line.decode("utf-8", "replace").rstrip("\n")
    if not s.startswith("data:"):
        continue
    p = s[5:].strip()
    if p == "[DONE]":
        break
    frames += 1
    try:
        j = json.loads(p)
    except Exception:
        continue
    ch = (j.get("choices") or [{}])[0]
    if ch.get("finish_reason"):
        finish = ch["finish_reason"]
    d = ch.get("delta") or {}
    reasoning += len(d.get("reasoning_content") or "")
    content += len(d.get("content") or "")
    for tc in d.get("tool_calls") or []:
        if tc.get("index") == 0:
            acc.append((tc.get("function") or {}).get("arguments") or "")
conn.close()
joined = "".join(acc)
try:
    v = json.loads(joined)
    ok = True
    lines = v.get("content", "").count("\n") + 1
except Exception as e:
    ok = False
    lines = f"parse失败: {type(e).__name__}"
print(f"finish={finish!r} 帧≈{frames} 耗时={time.time()-t0:.0f}s")
print(f"字符: reasoning={reasoning} content={content} args={len(joined)} json_ok={ok} 行数={lines}")
if not ok:
    print("尾部150:", repr(joined[-150:]))
if ok and finish == "tool_calls" and frames > 9000:
    print("判定: 平台放行 >8192 —— 客户端设大 max_tokens 即可避免截断")
elif finish == "length" and 8000 <= frames <= 8400:
    print("判定: 平台硬顶 ~8192 —— 客户端只能分块写 / 换 output 上限高的供应商")
else:
    print("判定: 介于两者之间或未撞墙 —— 看上面帧数与 finish 再定性")
