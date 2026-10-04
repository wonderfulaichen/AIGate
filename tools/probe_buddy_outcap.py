# -*- coding: utf-8 -*-
"""探测 CodeBuddy 输出额度: max_tokens=32768 / 不带 max_tokens(平台默认)。

背景: e2e_buddy_toolargs 首轮 finish=length 截断 arguments, 但那次 max_tokens=8192
是**脚本自己设的** —— 需要分清 8192 是平台硬上限还是请求值, 决定客户端解法:
  · 32768 放行 → 客户端把 max_tokens 设大即解;
  · 平台仍按 ~8192 截 → 平台硬上限, 客户端只能分块写/换供应商。
prompt 强制先思考, 确保 reasoning 占额度的场景被复现。
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


def probe(label, extra):
    body = {
        "model": "deepseek-v4-flash",
        "stream": True,
        "messages": [
            {"role": "system", "content": "你是一个编程助手。用 write_file 工具写出完整文件，不要省略。"},
            {"role": "user", "content": "先详细推理每个方法的实现要点（逐个方法列出设计考虑），"
                                        "然后用 write_file 写 PipelineOverlapProbe.java："
                                        "8 个方法（半径窗口数组初始化、中心点采样、重叠逐格比较、"
                                        "一致性校验、差值统计、排序、报告生成、main），"
                                        "每个方法带完整实现和中文注释。"},
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
    body.update(extra)
    conn = http.client.HTTPSConnection("copilot.tencent.com", 443,
                                       context=ssl.create_default_context(), timeout=300)
    t0 = time.time()
    conn.request("POST", "/v2/chat/completions", body=json.dumps(body).encode(), headers=HEADERS)
    r = conn.getresponse()
    if r.status != 200:
        print(f"[{label}] HTTP {r.status}: {r.read(300)!r}")
        return
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
            a = (tc.get("function") or {}).get("arguments") or ""
            if tc.get("index") == 0:
                acc.append(a)
    conn.close()
    joined = "".join(acc)
    try:
        json.loads(joined)
        ok = True
    except Exception:
        ok = False
    print(f"[{label}] finish={finish!r} 帧≈{frames} 耗时={time.time()-t0:.0f}s | "
          f"字符: reasoning={reasoning} content={content} args={len(joined)} | "
          f"args_json_ok={ok}")
    if not ok:
        print(f"    尾部120: {joined[-120:]!r}")


probe("max=32768", {"max_tokens": 32768})
probe("no-max(默认)", {})
