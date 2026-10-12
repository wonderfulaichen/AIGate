# -*- coding: utf-8 -*-
"""实测推理链剥离的**实际语义**: 到底剥哪一层 (HANDOFF §5.4 警告"极易记反").

构造一个含三类消息的请求, 看上游实际收到什么:
  ① assistant 无 tool_calls + 有 reasoning_content  → 期望: 剥 (默认浅层)
  ② assistant 有 tool_calls + 有 reasoning_content  → 期望: 由 STRIP_TOOLCALL_ON_CHAT 控制
  ③ assistant 普通正文 (无 reasoning)               → 期望: 不动
另测: 关闭 STRIP_HISTORY_REASONING 时是否完全不剥 (对照).

判据只看**上游收到的 body**, 不读代码注释 (注释可能与实现漂移)。
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_strip_sem")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8983, 8982

RECV = []

class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        RECV.append(json.loads(self.rfile.read(n) or b"{}"))
        b = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def boot(env_extra):
    if os.path.isdir(ISO): shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    json.dump({"providers": [{"name": "m", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
                              "api_key_env": "K", "api_key_default": "d",
                              "models": {"m/x": {"upstream_model": "x"}}}]},
              open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
    env = dict(os.environ)
    env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    env.update(env_extra)
    p = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            # 就绪探测用免鉴权的 /health (/admin/api/* 现在要 Bearer)
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read(); break
        except Exception: time.sleep(0.5)
    return p

BODY = {"model": "m/x", "messages": [
    {"role": "user", "content": "u1"},
    {"role": "assistant", "content": "a1", "reasoning_content": "PLAIN_CHAIN_" + "x"*400},
    {"role": "user", "content": "u2"},
    {"role": "assistant", "content": None, "reasoning_content": "TOOL_CHAIN_" + "y"*400,
     "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "res"},
    {"role": "user", "content": "u3"},
]}

def run(tag):
    RECV.clear()
    r = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions",
                               data=json.dumps(BODY).encode(),
                               headers={"content-type": "application/json"})
    urllib.request.urlopen(r, timeout=20).read()
    time.sleep(0.3)
    s = json.dumps(RECV[-1], ensure_ascii=False)
    plain_stripped = "PLAIN_CHAIN_" not in s
    tool_stripped = "TOOL_CHAIN_" not in s
    print(f"  [{tag}] 无tool_calls链被剥={plain_stripped}  带tool_calls链被剥={tool_stripped}")
    return plain_stripped, tool_stripped

print("=== 默认 (STRIP_HISTORY_REASONING=true, STRIP_TOOLCALL_ON_CHAT=true) ===")
p = boot({})
a, b = run("default")
p.terminate(); p.wait(timeout=5)
print(f"  => 默认: 浅层(无tool_calls)剥={a}, 带tool_calls层剥={b}")

print("\n=== 仅关 STRIP_TOOLCALL_ON_CHAT (保留浅层剥离) ===")
p = boot({"STRIP_TOOLCALL_ON_CHAT": "false"})
c, d = run("chat-tc-off")
p.terminate(); p.wait(timeout=5)
print(f"  => 浅层剥={c}, 带tool_calls层剥={d}")

print("\n=== 全关 STRIP_HISTORY_REASONING ===")
p = boot({"STRIP_HISTORY_REASONING": "false"})
e, f = run("all-off")
p.terminate(); p.wait(timeout=5)
print(f"  => 浅层剥={e}, 带tool_calls层剥={f}")

print("\n=== 结论 (对照 HANDOFF §5.4 表格) ===")
print(f"  默认行为: 浅层={a} / toolcall层={b}   (§5.4 期望: 两层都剥 → True/True)")
print(f"  关 chat 开关: 浅层={c} / toolcall层={d} (§5.4 期望: 浅层仍剥, toolcall 层不剥 → True/False)")
print(f"  总开关关: 浅层={e} / toolcall层={f}     (期望: 都不剥 → False/False)")
ok = (a and b) and (c and not d) and (not e and not f)
print("\n判定: " + ("与 §5.4 一致 ✓" if ok else "!! 与 §5.4 不符, 需修正文档或实现"))
srv.shutdown()
sys.exit(0 if ok else 1)