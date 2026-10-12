# -*- coding: utf-8 -*-
"""审查「历史裁剪」(MAX_HISTORY_TURNS) 的 tool 配对完整性.

风险: trim_history_turns 从尾部逐条保留到凑够 n 条 user, 可能在**消息中间**切断,
留下孤立的 `tool` 消息 (其配对的 assistant tool_calls 已被裁掉) 或孤立 tool_calls。
OpenAI 协议要求 tool 消息必须紧跟在带对应 tool_call_id 的 assistant 之后 → 上游 400。

断言: 裁剪后, 上游收到的 messages 里
  ① 每个 tool 消息都有前置 assistant 携带其 tool_call_id
  ② 每个 tool_calls 都有后续 tool 结果 (或至少不被裁在半途)
  ③ system 消息始终保留
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_trim_pair")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8977, 8976
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

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "m", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
                          "api_key_env": "K", "api_key_default": "d",
                          "models": {"m/x": {"upstream_model": "x"}}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1",
           MAX_HISTORY_TURNS="2", STRIP_HISTORY_REASONING="false")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

# 构造 4 轮对话, 每轮都带 tool 调用与结果 (裁剪 n=2 必然切断前面的轮次)
def turn(i):
    return [
        {"role": "user", "content": f"u{i}"},
        {"role": "assistant", "content": None, "reasoning_content": "r" * 50,
         "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": f"res{i}"},
        {"role": "assistant", "content": f"a{i}"},
    ]

MSGS = [{"role": "system", "content": "sys"}] + turn(1) + turn(2) + turn(3) + turn(4)

def check_integrity(msgs, tag):
    declared = {}   # tool_call_id -> 是否见过其 assistant 声明
    ok_pairs = True
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            declared[tc.get("id")] = True
        if m.get("role") == "tool":
            if not declared.get(m.get("tool_call_id")):
                ok_pairs = False
    sys_kept = any(m.get("role") == "system" for m in msgs)
    return ok_pairs, sys_kept, declared

try:
    for _ in range(60):
        try:
            # 就绪探测用免鉴权的 /health (/admin/api/* 现在要 Bearer)
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read(); break
        except Exception: time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    r = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions",
                               data=json.dumps({"model": "m/x", "messages": MSGS}).encode(),
                               headers={"content-type": "application/json"})
    urllib.request.urlopen(r, timeout=20).read()
    time.sleep(0.3)
    got = RECV[-1].get("messages", [])
    print("  裁剪后消息序列:", [(m.get("role"), (m.get("tool_calls") or [{}])[0].get("id") if m.get("tool_calls") else m.get("tool_call_id", "")) for m in got])

    ok_pairs, sys_kept, declared = check_integrity(got, "trim")
    ck("① tool 消息都有前置 assistant 声明其 tool_call_id", ok_pairs,
       f"declared={list(declared)}; 实际 tool 消息={[m.get('tool_call_id') for m in got if m.get('role')=='tool']}")
    ck("② system 消息始终保留", sys_kept, "")
    ck("③ 裁剪确实发生 (消息数 < 原始)", len(got) < len(MSGS), f"{len(MSGS)} -> {len(got)}")
    # ④ 裁剪后首条非 system 消息不应是「孤立的 tool 结果」(其 assistant 被裁)
    first_non_sys = next((m for m in got if m.get("role") != "system"), None)
    ck("④ 首条非 system 消息不是孤立 tool 结果",
       not (first_non_sys and first_non_sys.get("role") == "tool"),
       json.dumps(first_non_sys, ensure_ascii=False)[:120] if first_non_sys else None)

    # ⑤ 裁剪后仍存在至少一条 user (最新请求必留)
    ck("⑤ 至少保留一条 user 消息", any(m.get("role") == "user" for m in got), "")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)