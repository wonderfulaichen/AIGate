# -*- coding: utf-8 -*-
"""探针: 会话/轮次关联 (2026-10-13 方案 F 第二阶段, 对标 Astra-Gate 链路追踪).

判据（mock 上游, 判据 = data/logs.jsonl 里每条记录的 session 字段）：
  ① 同一 session 头的连续三轮 → 同 session_key、turn=1/2/3 递增
  ② 带 tool_call id 回报的请求 → answers_to 精确含那个 id, 且 turn 递增
  ③ **Anthropic 的 tool_result (也是 user 角色) 仍计入会话**，且 turn 递增
     但它**不是新开一轮** (answers_to 非空 → 前端据此分段) —— 曾因只按 role
     判定而有把同轮拆成两轮的风险
 ④ turn 语义 = 会话内第 N **跳** (不是"轮"): 一轮"输入→工具调用→结果→再答"
     是 4 跳; 工具结果那一跳既不是新轮又确实是一次请求, 用"轮"装不下
  ④ 不同 session 头 → 不同 session_key (不串会话)
  ⑤ 无 session 头 → 回落内容指纹 (sha256:), **且指纹不含内容原文**
  ⑥ 未知/空 messages → session_key 为空串 (不硬造)
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8884
PG = 8885
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_session")

FAIL = []
NCHK = [0]


def chk(n, cond, extra=""):
    NCHK[0] += 1
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


def sse(delta, finish=None):
    return ("data: " + json.dumps({
        "id": "c-" + uuid.uuid4().hex[:6], "object": "chat.completion.chunk", "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n").encode("utf-8")


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            # 一律产出一次工具调用, id 固定 —— 供下一轮"回报"它
            self.wfile.write(sse({"tool_calls": [{"index": 0, "id": "call_FIXED_1",
                                                  "type": "function",
                                                  "function": {"name": "Read",
                                                               "arguments": "{}"}}]},
                                 "tool_calls"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
    return H


def wait_port(p, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", p), 1); s.close(); return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": [{
            "name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
            "api_key_env": "K", "api_key_default": "d",
            "models": {"mock/m": {"upstream_model": "m"}},
        }]}, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    srv = ThreadingHTTPServer(("127.0.0.1", MP), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2

        def send(messages, session="sess-A"):
            body = {"model": "mock/m", "stream": True, "messages": messages}
            h = {"Content-Type": "application/json"}
            if session:
                h["x-claude-code-session-id"] = session
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/v1/chat/completions" % PG, data=json.dumps(body).encode(),
                        headers=h, method="POST"), timeout=40) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.read().decode("utf-8", "replace")
            except Exception as e:
                return "ERR:" + type(e).__name__

        def records():
            p = os.path.join(ISO, "data", "logs.jsonl")
            deadline = time.time() + 6
            while time.time() < deadline:
                try:
                    rows = [json.loads(x) for x in
                            io.open(p, encoding="utf-8", errors="replace").read().split("\n")
                            if x.strip()]
                except Exception:
                    rows = []
                want = getattr(records, "want", 0)
                if want and len(rows) >= want:
                    return rows
                if not want and rows:
                    return rows
                time.sleep(0.2)
            return rows if not getattr(records, "want", 0) else []

        # ① 同一会话三轮, 每轮一条 user → turn 1/2/3
        for i in range(3):
            send([{"role": "user", "content": "第 %d 轮" % i}])
            time.sleep(0.4)
        rows = records()
        turns = [r.get("session", {}).get("turn") for r in rows]
        keys = {r.get("session", {}).get("session_key") for r in rows}
        chk("turns-increment-1-2-3", turns == [1, 2, 3], "turns=%s" % turns)
        chk("same-session-key", keys == {"sess-A"}, "keys=%s" % keys)
        chk("session-source-header",
            all(r.get("session", {}).get("session_source") == "header" for r in rows), rows[:1])

        # ② Anthropic 形态的 tool_result 回报 (角色也是 user!) → turn 递增但**不是新轮**
        before_n = len(rows)
        send([{"role": "user", "content": "第 4 轮"},
              {"role": "assistant", "content": [{"type": "tool_use", "id": "call_FIXED_1",
                                                 "name": "Read", "input": {}}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_FIXED_1",
                                            "content": "文件内容"}]}])
        time.sleep(1.0)
        records.want = before_n + 1
        rows = records()
        last = rows[-1]
        s = last.get("session") or {}
        chk("anthropic-tool-result-answers-to",
            s.get("answers_to") == ["call_FIXED_1"], "answers_to=%s" % s.get("answers_to"))
        chk("anthropic-tool-result-turn-increments", s.get("turn") == 4,
            "turn=%s (第 4 跳; 工具结果回报也计入会话, 前端按 answers_to 分段)"
            % s.get("turn"))
        chk("anthropic-tool-result-same-session", s.get("session_key") == "sess-A",
            "session_key=%s" % s.get("session_key"))

        # ③ 同内容不同 session → 不串
        send([{"role": "user", "content": "第 1 轮"}], session="sess-B")
        time.sleep(1.0)
        records.want = len(rows) + 1
        rows = records()
        s = rows[-1].get("session") or {}
        chk("different-session-not-mixed", s.get("session_key") == "sess-B", s)
        chk("different-session-turn-restarts", s.get("turn") == 1,
            "turn=%s (新会话应从 1 起)" % s.get("turn"))

        # ④ 无 session 头 → 内容指纹; 且**不含内容原文** (隐私纪律)
        send([{"role": "user", "content": "唯一内容标记 UNIQUE_PHRASE_XYZ"}], session=None)
        time.sleep(1.0)
        records.want = len(rows) + 1
        rows = records()
        s = rows[-1].get("session") or {}
        chk("fingerprint-fallback", str(s.get("session_key", "")).startswith("sha256:"),
            "session_key=%s" % s.get("session_key"))
        chk("fingerprint-source", s.get("session_source") == "fingerprint",
            "session_source=%s" % s.get("session_source"))
        chk("fingerprint-hides-content",
            "UNIQUE_PHRASE_XYZ" not in json.dumps(s),
            "指纹不得泄露内容原文: %s" % json.dumps(s)[:120])

        # ⑤ 空 messages → session_key 空串 (不硬造)
        send([], session=None)
        time.sleep(1.0)
        records.want = len(rows) + 1
        rows = records()
        s = rows[-1].get("session") or {}
        chk("empty-messages-no-key", s.get("session_key") == "",
            "session_key=%r (空 messages 不得硬造指纹)" % s.get("session_key"))

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (NCHK[0], len(FAIL)))
        return 0 if not FAIL else 1
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        if not os.environ.get("KEEP_ISO"):
            shutil.rmtree(ISO, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())