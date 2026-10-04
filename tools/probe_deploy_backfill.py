# -*- coding: utf-8 -*-
"""行为实测: 部署网关 (8787) 对 buddy 流式 tool_call 是否做了 id 回填.

判据 (§2.3 行为实测, 不信 commit/build_time):
  CodeBuddy 原生只有首帧带 id, 后续分片 id=null/缺失 ——
  · 全部分片都带 id        → 部署份**含** backfill_tool_call_ids (工作区构建);
  · 后续分片 id 缺失       → 部署份**不含** (旧于反代轮修复) —— 按 id 分组的客户端
                            会把 null 分片串组 → args 拼坏 → invalid JSON。
小 max_tokens 控成本 (1 次真实 buddy 调用)。
"""
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"

body = {
    "model": "buddy/deepseek-v4-flash",
    "stream": True,
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "请调用 get_time 工具获取当前时间, 不要自己回答"}],
    "tools": [{"type": "function", "function": {
        "name": "get_time", "description": "获取当前时间",
        "parameters": {"type": "object", "properties": {"tz": {"type": "string"}},
                       "required": []}}}],
    "tool_choice": "auto",
}
req = urllib.request.Request(BASE + "/v1/chat/completions",
                             data=json.dumps(body).encode(),
                             headers={"content-type": "application/json"})
t0 = time.time()
try:
    resp = urllib.request.urlopen(req, timeout=120)
except Exception as e:
    print("请求失败:", type(e).__name__, e)
    sys.exit(1)

n = missing_id = with_id = 0
first_id = None
ids_seen = set()
done = False
while True:
    line = resp.readline()
    if not line:
        break
    s = line.decode("utf-8", "replace").rstrip("\n")
    if not s.startswith("data:"):
        continue
    p = s[5:].strip()
    if p == "[DONE]":
        done = True
        break
    try:
        j = json.loads(p)
    except Exception:
        continue
    d = ((j.get("choices") or [{}])[0]).get("delta") or {}
    for tc in d.get("tool_calls") or []:
        n += 1
        cid = tc.get("id")
        if cid:
            with_id += 1
            ids_seen.add(cid)
            if first_id is None:
                first_id = cid
        else:
            missing_id += 1
print(f"耗时 {time.time()-t0:.1f}s done={done}")
print(f"tool 分片: 总 {n} / 带 id {with_id} / 缺 id {missing_id} / 不同 id 数 {len(ids_seen)}")
if n == 0:
    print("没收到 tool_calls (模型没调工具), 需重跑")
    sys.exit(2)
if missing_id == 0 and n > 1:
    print("判定: 部署份 **含 id 回填** (每个分片都有 id)")
elif missing_id > 0:
    print(f"判定: 部署份 **无 id 回填** —— 后续分片缺 id ({missing_id}/{n}), "
          f"首帧 id={first_id!r}; 按 id 分组的客户端会串组")
