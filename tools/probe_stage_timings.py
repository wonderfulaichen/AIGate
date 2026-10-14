# -*- coding: utf-8 -*-
"""探针: 「时间去哪了」—— 阶段耗时采集与口径（2026-10-13 方案 F, 对标 Astra-Gate）。

判据（mock 上游可控地构造各阶段，判据 = 记录里的 stages + 窗口级 stage_summary）：
  ① 有思考的流: reasoning_ms > 0 且 waiting/output 均有值
  ② **无思考**的流: reasoning_ms == 0（测得为 0, 不是 None —— 首块即正文）
 ③ 纯思考流（从未出正文）: output_ms **与 reasoning_ms 都为 null**（未采集, 不是 0）——
    reasoning 段的边界是"第一段可见正文", 没有正文该段从未结束, 报 0 就是假精确
  ④ 带 tool_calls 的流: tool_call_ms 有值（0 也算"有"）
  ⑤ 非流式请求: 整块 stages 全 None（未采集）→ 面板显示 "—" 而非 0ms
  ⑥ 窗口级汇总: waiting_samples 只数采集到的, 未采集的不计入
  ⑦ 各段之和不必等于 latency_ms（刻意不做自洽假数）—— 只断言各段非负
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
MP = 8882
PG = 8883
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_stages")

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
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            mode = ""
            for m in (body.get("messages") or []):
                c = m.get("content")
                if isinstance(c, str) and c.startswith("SCN:"):
                    mode = c[4:].strip()
            stream = bool(body.get("stream"))
            if not stream:
                # 非流式: 一次性 JSON（阶段耗时无从采集）
                out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                              "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def w(b):
                self.wfile.write(b); self.wfile.flush()

            if mode == "thinking":
                # 先思考后正文（两段可分辨）
                w(sse({"reasoning_content": "想"})); time.sleep(0.25)
                w(sse({"reasoning_content": "很久"})); time.sleep(0.25)
                w(sse({"content": "答"})); time.sleep(0.15)
                w(sse({"content": "案"}))
                w(sse({}, "stop"))
            elif mode == "nothink":
                # 无思考: 首块即正文 → reasoning 段应为 0
                w(sse({"content": "直"})); time.sleep(0.15)
                w(sse({"content": "接"}))
                w(sse({}, "stop"))
            elif mode == "onlythink":
                # 纯思考, 从未出正文 → output 段未采集 (None)
                w(sse({"reasoning_content": "只有思考"})); time.sleep(0.2)
                w(sse({"reasoning_content": "还在想"}))
                w(sse({}, "length"))
            elif mode == "toolcall":
                w(sse({"content": "看一下"})); time.sleep(0.1)
                w(sse({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "Read", "arguments": ""}}]})); time.sleep(0.12)
                w(sse({"tool_calls": [{"index": 0, "function": {"arguments": "{\"p\":\"a\"}"}}]}))
                w(sse({}, "tool_calls"))
            else:
                w(sse({"content": "ok"})); w(sse({}, "stop"))
            w(b"data: [DONE]\n\n")

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
        from adminauth import headers as admin_auth
        AH = admin_auth(os.path.join(ISO, "data"))

        def chat(mode, stream=True):
            body = json.dumps({"model": "mock/m", "stream": stream,
                               "messages": [{"role": "user", "content": "SCN:" + mode}]}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                        headers={"content-type": "application/json"}, method="POST"), timeout=60) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.read().decode("utf-8", "replace")
            except Exception as e:
                return "ERR:" + type(e).__name__

        def last_record():
            p = os.path.join(ISO, "data", "logs.jsonl")
            for _ in range(30):
                try:
                    rows = [x for x in io.open(p, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
                except Exception:
                    rows = []
                if rows:
                    try:
                        return json.loads(rows[-1])
                    except Exception:
                        pass
                time.sleep(0.2)
            return {}

        def stage_of(rec):
            return rec.get("stages") or {}

        # ① 有思考: 三段俱全且 reasoning > 0
        chat("thinking"); time.sleep(1.2)
        s = stage_of(last_record())
        chk("thinking-waiting-present", s.get("waiting_first_ms") is not None, s)
        chk("thinking-reasoning-positive",
            (s.get("reasoning_ms") or 0) > 0, "reasoning_ms=%s (应 >0, 上游思考了 0.5s)" % s.get("reasoning_ms"))
        chk("thinking-output-present", s.get("output_ms") is not None, s)
        chk("thinking-all-nonneg", all((s.get(k) or 0) >= 0 for k in
            ("waiting_first_ms", "reasoning_ms", "output_ms")), s)

        # ② 无思考: reasoning == 0 (测得为 0), 不是 None
        chat("nothink"); time.sleep(1.2)
        s = stage_of(last_record())
        chk("nothink-reasoning-is-zero", s.get("reasoning_ms") == 0,
            "reasoning_ms=%s (首块即正文 → 测得 0, 不是 None)" % s.get("reasoning_ms"))
        chk("nothink-output-present", s.get("output_ms") is not None, s)

        # ③ 纯思考 (从未出正文): output 与 reasoning 都未采集 → None
        chat("onlythink"); time.sleep(1.2)
        s = stage_of(last_record())
        chk("onlythink-output-is-null", s.get("output_ms") is None,
            "output_ms=%s (从未出正文 → 未采集, 必须是 null 而不是 0)" % s.get("output_ms"))
        chk("onlythink-reasoning-is-null", s.get("reasoning_ms") is None,
            "reasoning_ms=%s (思考段的边界是正文开头; 没有正文则该段从未结束 → null 而非 0)"
            % s.get("reasoning_ms"))
        chk("onlythink-waiting-present", s.get("waiting_first_ms") is not None, s)

        # ④ 工具调用: tool_call 段有值
        chat("toolcall"); time.sleep(1.2)
        s = stage_of(last_record())
        chk("toolcall-segment-present", s.get("tool_call_ms") is not None,
            "tool_call_ms=%s (产出过工具调用 → 应有值, 0 也算有)" % s.get("tool_call_ms"))

        # ⑤ 非流式: 整块未采集 (全 None) —— 面板要显示 "—" 而非 0ms
        chat("plain", stream=False); time.sleep(1.2)
        s = stage_of(last_record())
        chk("nonstream-all-null",
            all(s.get(k) is None for k in ("waiting_first_ms", "reasoning_ms", "output_ms", "tool_call_ms")),
            "非流式应整块未采集, 实得 %s" % s)

        # ⑥⑦ 窗口级汇总
        r = urllib.request.Request("http://127.0.0.1:%d/admin/api/stats" % PG, headers=AH)
        with urllib.request.urlopen(r, timeout=30) as resp:
            st = json.loads(resp.read().decode("utf-8"))
        ss = st.get("stage_summary") or {}
        chk("summary-present", ss.get("waiting_samples", 0) >= 4,
            "stage_summary=%s (至少 4 条流式采集到首字)" % {k: ss.get(k) for k in
                ("waiting_samples", "output_samples", "reasoning_samples", "tool_call_samples")})
        chk("summary-output-lt-waiting",
            (ss.get("output_samples") or 0) < (ss.get("waiting_samples") or 0),
            "正文样本(%s) < 首字样本(%s) —— 纯思考/非流式那条不应计入正文"
            % (ss.get("output_samples"), ss.get("waiting_samples")))
        chk("summary-sums-nonneg", all((ss.get(k) or 0) >= 0 for k in
            ("waiting_sum_ms", "reasoning_sum_ms", "output_sum_ms", "tool_call_sum_ms")), ss)

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