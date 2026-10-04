# -*- coding: utf-8 -*-
"""端到端: 通过 AIGate 反代 CodeBuddy (腾讯 copilot.tencent.com)。

背景 (2026-10-01 侦察, 源自 gitee iJetLi/deepseek-harness-codearts 的反代要件):
  - 端点只支持**流式** (非流式回 400 code=11101 "Non-stream chat request..." — 已知限制, 固化);
  - 请求需带一整套归属头 (X-Domain/X-Product-Code/X-Product/X-IDE-*/X-Agent-Purpose + 伪装 UA);
  - tool_call id 只在首帧携带, 后续分片 id=None → 网关必须回填 (backfill_tool_call_ids)。

凭据: 从本机 CodeBuddy CN IDE 解出的 accessToken (temp\cb_plain.json, 由用户会话内
解密步骤生成), 本脚本只读取, 不落仓库。

覆盖:
  ① 流式 chat 穿网关 (200 + 内容 + [DONE])
  ② 流式 + tools: 每个 tool_calls 分片都必须带 id 且回填为同一 id (修复回归)
  ③ 请求记录: status/model/effort 落盘, 思考强度展示链正确
  ④ 非流式 → 上游 400 的诚实透传 (已知限制的固化: 记录存在且带 11101 语义)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_buddy")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8997
CRED = os.path.join(tempfile.gettempdir(), "cb_plain.json")

if not os.path.exists(CRED):
    raise SystemExit(f"缺凭据 {CRED} —— 先跑会话内的解密步骤 (gcm_open.py)")
cred = json.load(open(CRED, encoding="utf-8"))
tok = cred["auth"]["accessToken"]
exp_ms = int(cred["auth"].get("expiresAt") or 0)
left_days = (exp_ms / 1000 - time.time()) / 86400
print(f"凭据: token len={len(tok)} 有效期剩 {left_days:.0f} 天")
if left_days < 1:
    raise SystemExit("token 已过期/即将过期, 先重新登录 CodeBuddy CN IDE")

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

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io_open = open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8")
json.dump({"providers": [{
    "name": "buddy",
    "endpoint": "https://copilot.tencent.com/v2/chat/completions",
    "api_key_env": "BUDDY_ACCESS_TOKEN",
    "api_key_default": tok,
    "headers": HEADERS,
    "models": {
        "buddy/deepseek-v4-flash": {"upstream_model": "deepseek-v4-flash"},
    },
}]}, io_open, ensure_ascii=False, indent=1)
io_open.close()

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
err_f = open(os.path.join(ISO, "stderr.log"), "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=err_f)

BASE = f"http://127.0.0.1:{PG}"
MODEL = "buddy/deepseek-v4-flash"
FAILS = []


def post_stream(body, timeout=120):
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp
    except urllib.error.HTTPError as e:
        return e.code, e
    except ConnectionResetError as e:
        # 本地连接被网关重置 = 进程可能崩了 —— 返回哨兵, 由调用方打印 stderr
        print("  [conn-reset]", e)
        return -2, None


def read_sse(resp, limit=4000):
    frames, text, tool_frames, done = [], [], [], False
    while len(frames) < limit:
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
        frames.append(p)
        try:
            j = json.loads(p)
        except Exception:
            continue
        d = ((j.get("choices") or [{}])[0]).get("delta") or {}
        if d.get("content"):
            text.append(d["content"])
        for tc in d.get("tool_calls") or []:
            tool_frames.append(tc)
    return frames, "".join(text), tool_frames, done


def logs():
    return json.loads(urllib.request.urlopen(BASE + "/admin/api/logs", timeout=20)
                      .read().decode("utf-8"))


def check(label, cond, detail=""):
    if not cond:
        FAILS.append(label)
    print("  %s%-48s %s" % ("OK " if cond else "!! ", label, detail))


try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("隔离实例未起来")

    print("=== ① 流式 chat 穿网关 ===")
    n0 = len(logs())
    st, resp = post_stream({"model": MODEL, "stream": True, "max_tokens": 64,
                            "messages": [{"role": "user", "content": "只回复两个字:ok"}]})
    frames, text, _, done = read_sse(resp)
    check("① HTTP 200 + [DONE] + 有内容", st == 200 and done and text.strip(),
          f"status={st} frames={len(frames)} text={text[:40]!r}")

    print("\n=== ② tool_call id 回填 (每帧都必须有 id, 且同一 index 同一 id) ===")
    st, resp = post_stream({"model": MODEL, "stream": True, "max_tokens": 512,
                            "reasoning_effort": "high",
                            "messages": [{"role": "user", "content": "请调用 get_time 工具获取当前时间, 不要自己回答"}],
                            "tools": [{"type": "function", "function": {
                                "name": "get_time", "description": "获取当前时间",
                                "parameters": {"type": "object", "properties": {"tz": {"type": "string"}},
                                               "required": []}}}],
                            "tool_choice": "auto"})
    _, _, tcs, done = read_sse(resp)
    ids_by_idx = {}
    missing = 0
    for tc in tcs:
        idx = tc.get("index")
        cid = tc.get("id")
        if not cid:
            missing += 1
            continue
        ids_by_idx.setdefault(idx, set()).add(cid)
    check("② HTTP 200 + 收到 tool_calls 分片", st == 200 and done and len(tcs) > 0,
          f"status={st} 分片数={len(tcs)}")
    check("②b 所有分片都带 id (回填生效)", len(tcs) > 0 and missing == 0,
          f"缺失 id 的分片={missing}")
    check("②c 每个 index 只对应一个 id (不串)", all(len(v) == 1 for v in ids_by_idx.values()),
          f"ids={ {k: sorted(v) for k, v in ids_by_idx.items()} }")

    print("\n=== ③ 记录与思考强度 ===")
    time.sleep(1.2)
    recs = logs()
    check("③ 记录数 +2 (两条请求都落盘)", len(recs) >= n0 + 2, f"{n0} -> {len(recs)}")
    top = recs[0]
    check("③b 最新记录 model/status/effort",
          top.get("model") == MODEL and top.get("status") == 200 and top.get("effort") == "high",
          f"model={top.get('model')} status={top.get('status')} effort={top.get('effort')!r} "
          f"展示={top.get('reasoning_effort')!r}")

    print("\n=== ④ 非流式 → 已知限制 400 (11101) 的诚实透传 ===")
    st, resp = post_stream({"model": MODEL, "stream": False,
                            "messages": [{"role": "user", "content": "hi"}]})
    if st == -2:
        crashed = proc.poll() is not None
        print("  网关连接被重置, 进程存活 =", not crashed)
        err_f.flush()
        tail = open(os.path.join(ISO, "stderr.log"), encoding="utf-8",
                    errors="replace").read()[-3000:]
        print("---- stderr tail ----")
        print(tail)
        FAILS.append("④ 网关在非流式路径崩溃" if crashed else "④ 客户端连接被重置")
        print("\n=== 结果 ===")
        print("  FAIL:", FAILS)
        sys.exit(1)
    body = resp.read().decode("utf-8", "replace") if resp is not None and hasattr(resp, "read") else ""
    time.sleep(1.2)
    top = logs()[0]
    check("④ 网关如实返回 400 且记录带上游原文",
          st == 400 and top.get("status") == 400 and "11101" in (top.get("error") or ""),
          f"status={st} err={(top.get('error') or '')[:60]!r}")

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL:", FAILS)
        sys.exit(1)
    print("  PASS —— CodeBuddy 反代四组断言全过 (含 tool_call id 回填与 11101 固化)")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
