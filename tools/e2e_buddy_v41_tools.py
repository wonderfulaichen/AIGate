# -*- coding: utf-8 -*-
"""复刻用户故障形态: deepseek-v4.1-flash + reasoning_effort=high + 并行多工具
穿网关 vs 直连, 验 tool_call arguments 拼接完整性 (丢前缀/缺段/JSON 合法性)
+ finish_reason 空串帧统计。

背景 (2026-10-01): 用户会话 (obt→AIGate→buddy) 连续 4 次 attempt 重试 (superseded),
obt 报「Arguments are invalid JSON」。已排除: 网关丢帧 (unparseable=0)、
max_tokens 截断 (日志 finish_reason=length 仅 1 次)。嫌疑: v4.1+high 思考流与
tool_calls 混流时的分片形态, 或 finish_reason="" 空串 (日志 8050 次) 的影响。
max_tokens=32768 充足, 排除截断干扰 —— 若仍坏, 是分片/结构问题。
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_v41")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8994
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
}

UPSTREAM = "deepseek-v4.1-flash"
N_GATE, N_DIRECT = 3, 2

BODY = {
    "model": UPSTREAM,
    "stream": True,
    "max_tokens": 32768,
    "reasoning_effort": "high",
    "messages": [
        {"role": "system", "content": "你是编程助手。需要并行调研时，一次性发出全部工具调用。"},
        {"role": "user", "content": "请**同时**发出 4 个 Grep 工具调用（一次 response 里并行），"
                                    "分别搜索 src/ 下的: 'max_tokens'、'context_window'、"
                                    "'tool_call'、'finish_reason'，然后停止等待结果。"},
    ],
    "tools": [
        {"type": "function", "function": {
            "name": "Grep", "description": "搜索文件内容",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string"}, "path": {"type": "string"},
                "output_mode": {"type": "string"}, "head_limit": {"type": "integer"},
                "-n": {"type": "boolean"}},
                "required": ["pattern"]}}},
        {"type": "function", "function": {
            "name": "Read", "description": "读文件",
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string"}}, "required": ["file_path"]}}},
        {"type": "function", "function": {
            "name": "write_file", "description": "写文件",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}}},
    ],
    "tool_choice": "auto",
}


def collect(readline, label, cap=60000):
    tcs, frames = [], []
    finish_vals = []
    reasoning = content = 0
    done = False
    while len(frames) < cap:
        line = readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").rstrip("\n")
        if not s.startswith("data:"):
            continue
        p = s[5:].strip()
        if p == "[DONE]":
            done = True
            break
        frames.append(p)
        try:
            j = json.loads(p)
        except Exception:
            continue
        ch = (j.get("choices") or [{}])[0]
        fr = ch.get("finish_reason")
        if fr is not None:
            finish_vals.append(fr)
        d = ch.get("delta") or {}
        reasoning += len(d.get("reasoning_content") or "")
        content += len(d.get("content") or "")
        for tc in d.get("tool_calls") or []:
            tcs.append(tc)
    print(f"  [{label}] 帧={len(frames)} done={done} tool分片={len(tcs)} "
          f"reasoning字符={reasoning} content字符={content} "
          f"finish_reason值={finish_vals if finish_vals else '无'}")
    return tcs, frames, done


def stitch(tcs):
    acc, order = {}, []
    for tc in tcs:
        idx = tc.get("index")
        if idx is None:
            continue
        if idx not in acc:
            acc[idx] = {"id": None, "name": None, "args": []}
            order.append(idx)
        f = tc.get("function") or {}
        if tc.get("id"):
            acc[idx]["id"] = tc["id"]
        if f.get("name"):
            acc[idx]["name"] = (acc[idx]["name"] or "") + f["name"]
        if f.get("arguments"):
            acc[idx]["args"].append(f["arguments"])
    return {i: {"id": acc[i]["id"], "name": acc[i]["name"],
                "args": "".join(acc[i]["args"])} for i in order}


def report(label, stitched):
    ok_all = True
    for i, v in sorted(stitched.items()):
        raw = v["args"]
        starts_ok = raw.lstrip().startswith("{")
        try:
            json.loads(raw)
            ok = True
            note = ""
        except Exception as e:
            ok = False
            note = f"{type(e).__name__}: {str(e)[:80]} | 尾部100={raw[-100:]!r}"
        ok_all = ok_all and ok
        print(f"    [{i}] name={v['name']!r} id={'有' if v['id'] else '无'} "
              f"len={len(raw)} 以'{{'开头={starts_ok} json_ok={ok} {note}")
    return ok_all


# ── 隔离网关 ──
if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{
    "name": "buddy",
    "endpoint": "https://copilot.tencent.com/v2/chat/completions",
    "api_key_env": "BUDDY_ACCESS_TOKEN",
    "api_key_default": tok,
    "headers": HEADERS,
    "models": {"buddy/" + UPSTREAM: {"upstream_model": UPSTREAM}},
}]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"),
    ensure_ascii=False, indent=1)

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

results = []
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("隔离实例未起来")

    print(f"═══ A. 穿网关 ×{N_GATE} (v4.1 + effort=high + 并行工具) ═══")
    body_g = dict(BODY)
    body_g["model"] = "buddy/" + UPSTREAM
    for k in range(N_GATE):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PG}/v1/chat/completions",
            data=json.dumps(body_g).encode(),
            headers={"content-type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            tcs, frames, done = collect(resp.readline, f"A{k+1}")
            st = stitch(tcs)
            n_idx = len(st)
            ok = report(f"A{k+1}", st)
            results.append((f"A{k+1}", ok, n_idx, done))
        except Exception as e:
            print(f"  [A{k+1}] 失败: {type(e).__name__}: {e}")
            results.append((f"A{k+1}", False, 0, False))
        time.sleep(1)

    print(f"\n═══ B. 直连 ×{N_DIRECT} ═══")
    for k in range(N_DIRECT):
        conn = http.client.HTTPSConnection("copilot.tencent.com", 443,
                                           context=ssl.create_default_context(), timeout=300)
        h = dict(HEADERS)
        h.update({"content-type": "application/json",
                  "authorization": "Bearer " + tok,
                  "accept": "text/event-stream"})
        conn.request("POST", "/v2/chat/completions",
                     body=json.dumps(BODY).encode(), headers=h)
        r = conn.getresponse()
        if r.status != 200:
            print(f"  [B{k+1}] HTTP {r.status}")
            results.append((f"B{k+1}", False, 0, False))
            continue
        tcs, frames, done = collect(r.readline, f"B{k+1}")
        conn.close()
        st = stitch(tcs)
        ok = report(f"B{k+1}", st)
        results.append((f"B{k+1}", ok, len(st), done))
        time.sleep(1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("\n═══ 汇总 ═══")
for name, ok, n, done in results:
    print(f"  {name}: json_ok={ok} 工具数={n} done={done}")
bad_a = sum(1 for n, ok, _, _ in results if n.startswith("A") and not ok)
bad_b = sum(1 for n, ok, _, _ in results if n.startswith("B") and not ok)
print(f"\nA(穿网关) 坏 {bad_a}/{N_GATE}   B(直连) 坏 {bad_b}/{N_DIRECT}")
if bad_a > bad_b:
    print("→ 网关环节嫌疑 (穿网关坏率更高)")
elif bad_a == 0 and bad_b == 0:
    print("→ 两路全好: 该形态未复现, 需加大变量 (prompt 规模/轮次) 或上 dump 抓真会话")
else:
    print("→ 坏率相当: 上游形态问题, 网关透传")
