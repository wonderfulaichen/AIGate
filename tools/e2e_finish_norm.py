# -*- coding: utf-8 -*-
"""端到端: finish_reason 空串归一为 null (2026-10-01 buddy「Arguments are invalid JSON」根因修复).

实锤因果链 (见 HANDOFF §8): CodeBuddy 对每个分片帧发 finish_reason:"" (网关日志 8050 次) →
obt types/openai.rs:247 无空串过滤 → lib.rs:1255 `Some(cause)` 每帧 finalize →
累积中的 tool_call 被切成 raw_len=0/1/2/7 碎片 → invalid JSON + 重试 superseded.

本测用 mock 上游复刻非标帧, 断言网关输出:
  ① 分片帧 finish_reason "" → null (标准中间帧形态)
  ② 终帧 "tool_calls" 原样透传
  ③ id 回填不受影响 (分片全带 id)
  ④ 裸 null finish_reason 帧原样 (不误伤标准流)
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_finnorm")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8993, 8992

# mock: 每个 tool_calls 分片帧带 finish_reason:"" (复刻 buddy 非标), 末帧 tool_calls
FRAMES = []
def mk(idx, args, fr, tid=None):
    tc = {"index": idx, "function": {"arguments": args}}
    if tid:
        tc["id"] = tid
        tc["function"]["name"] = "write_file"
    FRAMES.append({"id": "c", "object": "chat.completion.chunk", "model": "m",
                   "choices": [{"index": 0, "delta": {"tool_calls": [tc]},
                                "finish_reason": fr}]})
mk(0, "", "", "call_x_1")           # 首帧: id+name, finish=""
mk(0, '{"path":', "", None)         # 分片, finish=""
mk(0, ' "a.java", "content": "hello"}', "", None)
FRAMES.append({"id": "c", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
FRAMES.append({"id": "c", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        try:
            for f in FRAMES:
                self.wfile.write(("data: " + json.dumps(f) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{
    "name": "mock", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
    "api_key_env": "K", "api_key_default": "d",
    "models": {"mock/m": {"upstream_model": "m"}},
}]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PASS, FAIL = [], []
def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))

try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("隔离实例未起来")

    req = urllib.request.Request(
        f"http://127.0.0.1:{PG}/v1/chat/completions",
        data=json.dumps({"model": "mock/m", "stream": True,
                         "messages": [{"role": "user", "content": "x"}]}).encode(),
        headers={"content-type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    shard_fr, final_fr, null_fr, ids, args_acc = [], [], [], [], []
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
        j = json.loads(p)
        ch = (j.get("choices") or [{}])[0]
        fr = ch.get("finish_reason")
        d = ch.get("delta") or {}
        tcs = d.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                ids.append(tc.get("id"))
                args_acc.append((tc.get("function") or {}).get("arguments") or "")
            # 区分三种: "finish_reason": null (归一目标) / 字段缺席 / 其他值
            if "finish_reason" not in ch:
                shard_fr.append("(absent)")
            elif ch["finish_reason"] is None:
                shard_fr.append("(null)")
            else:
                shard_fr.append(ch["finish_reason"])
        elif fr == "tool_calls":
            final_fr.append(fr)
        elif fr is None and not d:
            null_fr.append("(null)")
        if ch.get("finish_reason") == "tool_calls":
            final_fr.append("tool_calls")

    ck("① 分片帧空串归一为 null", len(shard_fr) == 3 and all(f == "(null)" for f in shard_fr),
       f"收到分片 finish={shard_fr}")
    ck("② 终帧 tool_calls 原样", "tool_calls" in final_fr, final_fr)
    ck("③ id 回填: 分片全带 id", len(ids) == 3 and all(ids), ids)
    ck("④ 标准 null 帧不误伤 + [DONE] 正常", done and null_fr, f"null帧={null_fr} done={done}")
    joined = "".join(args_acc)
    try:
        v = json.loads(joined)
        ck("⑤ 客户端按 index 拼接的 args 完整", v.get("path") == "a.java", joined)
    except Exception as e:
        ck("⑤ 客户端按 index 拼接的 args 完整", False, f"{e} | {joined!r}")
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
