# -*- coding: utf-8 -*-
"""审查「转发优化剥离推理链」的边界与漏洞 —— 上游收到的 body 为唯一判据.

逐条实测 (每条都断言**协议合法性**, 而非"是否被剥"):
  ① assistant content=null, 全部内容在 reasoning_details → 剥完是否成空 assistant?
  ② assistant content="" (空串) + reasoning_content → 剥后仍合法?
  ③ assistant 只有 tool_calls 无 content 无推理 → 不应被动
  ④ 多模态 assistant (content 为数组) + 推理 → content 必须完好
  ⑤ 顶层 reasoning 字段 (非消息内) → 不应被误删
  ⑥ system/user 消息内的同名字段 → 不应被误删 (只针对 assistant)
  ⑦ tool 消息带 reasoning 字段 → 不应被误删
  ⑧ 剥离后 tool_calls 配对完整性 (function_call_id 不受影响)
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_strip_edge")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8979, 8978
RECV = []

class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        RECV.append(json.loads(self.rfile.read(n) or b"{}"))
        b = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO): shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "m", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
                          "api_key_env": "K", "api_key_default": "d",
                          "models": {"m/x": {"upstream_model": "x"}}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1", STRIP_HISTORY_REASONING="true")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

def send(msgs, extra_top=None):
    RECV.clear()
    body = {"model": "m/x", "messages": msgs}
    if extra_top: body.update(extra_top)
    r = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions",
                               data=json.dumps(body).encode(),
                               headers={"content-type": "application/json"})
    urllib.request.urlopen(r, timeout=20).read()
    time.sleep(0.25)
    return RECV[-1] if RECV else {}

try:
    for _ in range(60):
        try:
            # 就绪探测用免鉴权的 /health (/admin/api/* 现在要 Bearer)
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read(); break
        except Exception: time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    # ① content=null + reasoning_details (全部内容在此) → 剥后是否空壳
    # 判据: content 必须**存在且非 null** (可为空串) —— 依据 responses.rs 的实测记录:
    # content:null 且无 tool_calls 会被上游 400, 而 content:"" 满足协议.
    got = send([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None,
         "reasoning_details": [{"type": "reasoning.text", "text": "THINK_ONLY", "index": 0}]},
        {"role": "user", "content": "u2"},
    ])
    msgs = got.get("messages", [])
    a = next((m for m in msgs if m.get("role") == "assistant"), None)
    protocol_ok = a is not None and (a.get("content") is not None or a.get("tool_calls"))
    ck("① content=null+reasoning_details: 剥后不得留下协议非法的空 assistant",
       protocol_ok,
       f"上游收到 assistant={json.dumps(a, ensure_ascii=False)[:200] if a else None}")

    # ② content="" + reasoning_content
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": "", "reasoning_content": "T" * 200}])
    msgs = got.get("messages", [])
    a = next((m for m in msgs if m.get("role") == "assistant"), None)
    ck("② content=''+reasoning_content: 剥后 content 字段仍在",
       a is not None and "content" in a, json.dumps(a, ensure_ascii=False)[:160] if a else None)

    # ③ 只有 tool_calls → 不应被动
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]}])
    a = next((m for m in got.get("messages", []) if m.get("role") == "assistant"), None)
    ck("③ 仅 tool_calls 的 assistant 完好",
       a is not None and a.get("tool_calls") and a.get("tool_calls")[0].get("id") == "c1",
       json.dumps(a, ensure_ascii=False)[:160] if a else None)

    # ④ 多模态 content 数组 + 推理
    mm = [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "http://x/i.png"}}]
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": mm, "reasoning_content": "T" * 200}])
    a = next((m for m in got.get("messages", []) if m.get("role") == "assistant"), None)
    ck("④ 多模态 content 数组完好", a is not None and a.get("content") == mm,
       json.dumps(a, ensure_ascii=False)[:200] if a else None)

    # ⑤ 顶层 reasoning 字段不得被误删
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": "a", "reasoning_content": "T" * 100}],
               {"reasoning": {"effort": "high"}, "reasoning_content": "TOP_LEVEL"})
    ck("⑤ 顶层 reasoning / reasoning_content 未被误删",
       got.get("reasoning", {}).get("effort") == "high" and got.get("reasoning_content") == "TOP_LEVEL",
       json.dumps({k: v for k, v in got.items() if "reason" in k}, ensure_ascii=False)[:200])

    # ⑥ system / user 消息内的同名字段
    got = send([{"role": "system", "content": "s", "reasoning_content": "SYS_KEEP"},
                {"role": "user", "content": "u", "reasoning_content": "USER_KEEP"},
                {"role": "assistant", "content": "a", "reasoning_content": "T" * 100}])
    kept = all(m.get("reasoning_content") for m in got.get("messages", []) if m.get("role") in ("system", "user"))
    ck("⑥ system/user 的 reasoning_content 保留", kept,
       [m.get("reasoning_content", "")[:20] for m in got.get("messages", [])])

    # ⑦ tool 消息
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": None, "reasoning_content": "T" * 100,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "r",
                 "reasoning_content": "TOOL_KEEP"}])
    tm = next((m for m in got.get("messages", []) if m.get("role") == "tool"), None)
    ck("⑦ tool 消息的 reasoning_content 保留", tm is not None and tm.get("reasoning_content") == "TOOL_KEEP",
       json.dumps(tm, ensure_ascii=False)[:160] if tm else None)

    # ⑧ 剥离后 tool_call_id 配对不受影响
    got = send([{"role": "user", "content": "u"},
                {"role": "assistant", "content": "a", "reasoning_content": "T" * 100},
                {"role": "assistant", "content": None, "reasoning_content": "T" * 100,
                 "tool_calls": [{"id": "c9", "type": "function",
                                 "function": {"name": "f", "arguments": '{"a":1}'}}]},
                {"role": "tool", "tool_call_id": "c9", "content": "r"}])
    ids = [tc.get("id") for m in got.get("messages", []) for tc in (m.get("tool_calls") or [])]
    tools = [m.get("tool_call_id") for m in got.get("messages", []) if m.get("role") == "tool"]
    ck("⑧ tool_call_id 配对完整", ids == ["c9"] and tools == ["c9"], f"ids={ids} tools={tools}")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)