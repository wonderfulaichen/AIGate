# -*- coding: utf-8 -*-
"""实测「等首字」现在是**真正的首字延迟**, 且四段首尾相接 (拼成一条连续时间轴)。

背景 (本轮修的真 bug):
  `proxy.rs::stage_timings` 里原为 `st.waiting_first_ms = Some(t0.elapsed())`,
  而该函数在**流结束时**调用 —— `t0.elapsed()` 是「首字 → 流结束」, 不是「请求 → 首字」。
  于是面板上标着「等首字」的那一段画的其实是首字**之后**的时长:
    实测 2917 条里 14/14 与 `latency − first_token_ms` 吻合, 与真正的 TTFT 只有 1 条吻合。
  这不仅标签错, 还让四段拼不成连续时间轴 —— 而"把一条请求的时间轴拆开看"正是该功能的理由。

判据 (mock 上游 + 隔离实例, 用**构造好的**时序):
  ① 等首字 ≈ first_token_ms (同一个量, 不是"首字之后")
  ② 等首字 + 思考 + 正文 + 工具 ≈ 总耗时 (首尾相接, 允许空帧造成的少量余量)
  ③ 等首字 明显小于 总耗时 (若 ≈ 总耗时说明又变回"首字之后"了)
  ④ 网关侧五段之和 ≤ 等首字 (去程是它的**内部**, 不是并列项)
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
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP, PG = 8898, 8899
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_axis")

FAIL = []
NCHK = [0]


def chk(cond, msg):
    NCHK[0] += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def sse(delta, finish=None, usage=None):
    d = {"id": "c-" + uuid.uuid4().hex[:6], "object": "chat.completion.chunk", "model": "m",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        d["usage"] = usage
    return ("data: " + json.dumps(d) + "\n\n").encode("utf-8")


def wait_port(p, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", p), 1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    if not os.path.exists(EXE):
        print("缺少构建产物: %s" % EXE)
        return 2
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    os.makedirs(os.path.join(ISO, "config"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": [{
            "name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
            "api_key_env": "MOCK_KEY", "api_key_default": "d",
            "models": {"mock/m": {"upstream_model": "m", "api_format": "openai"}},
        }]}, f)

    # 上游时序: 等 0.6s 才发首字 (reasoning), 再 0.4s 发正文, 再 0.4s 结束
    TTFB_DELAY, THINK_DELAY, OUT_DELAY = 0.6, 0.4, 0.4

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def w(b):
                self.wfile.write(b)
                self.wfile.flush()

            time.sleep(TTFB_DELAY)
            w(sse({"reasoning_content": "thinking..."}))
            time.sleep(THINK_DELAY)
            w(sse({"content": "hello"}))
            time.sleep(OUT_DELAY)
            w(sse({}, "stop", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
            w(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", MP), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1", MOCK_KEY="d")
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        body = {"model": "mock/m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        rec = {}
        lp = os.path.join(ISO, "data", "logs.jsonl")
        for _ in range(50):
            try:
                rows = [x for x in io.open(lp, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
                if rows:
                    rec = json.loads(rows[-1])
                    break
            except Exception:
                pass
            time.sleep(0.2)

        st = rec.get("stages") or {}
        lat = int(rec.get("latency_ms") or 0)
        ft = int(rec.get("first_token_ms") or 0)
        wait = st.get("waiting_first_ms")
        think = st.get("reasoning_ms")
        out = st.get("output_ms")
        gw = {k: st.get(k) for k in ("route_ms", "guard_ms", "convert_ms", "upstream_ttfb_ms", "overhead_ms")}
        print("\n实测: latency=%d first_token=%d stages=%s" % (lat, ft, st))

        print("\n== 判据 ==")
        chk(wait is not None, "等首字已采集: %s" % wait)
        # ① 等首字 == first_token_ms (同一个量, 两条路径同源)
        chk(wait is not None and abs(int(wait) - ft) <= max(30, ft * 0.1),
            "等首字(%s) ≈ 首字延迟(%d) —— 同一个量" % (wait, ft))
        # ③ 必须明显小于总耗时 (若≈总耗时 → 又变回"首字之后"的 bug)
        chk(wait is not None and int(wait) < lat * 0.8,
            "等首字(%s) 明显小于总耗时(%d) —— 不是'首字之后'" % (wait, lat))
        # ② 四段首尾相接: 之和**不超过**总耗时, 且覆盖大部分。
        #    ⚠️ 不能断言"≈ 总耗时": 上游发完正文后可能隔一段时间才发终止帧,
        #    那段空档既不属于思考也不属于正文生成 —— 设计上早已声明"各段之和不必等于总耗时"
        #    (proxy.rs::stage_timings 文档)。本条只钉两件硬事实: 不超总耗时 (超了就是重复计入)、
        #    且不是"只覆盖一小截"(那说明某段漏采)。
        segs = [wait, think, out, st.get("tool_call_ms")]
        known = [int(x) for x in segs if x is not None]
        ssum = sum(known)
        chk(ssum <= lat + 60,
            "四段之和(%d) 不超过总耗时(%d) —— 无重复计入" % (ssum, lat))
        chk(ssum >= lat * 0.6,
            "四段之和(%d) 覆盖总耗时(%d) 的 60%% 以上 —— 没有整段漏采" % (ssum, lat))
        # ④ 网关五段之和 ≤ 等首字 (去程是它的内部)
        gwsum = sum(int(v) for v in gw.values() if v is not None)
        chk(wait is not None and gwsum <= int(wait) + 50,
            "网关五段之和(%d) ≤ 等首字(%s) —— 去程是它的内部" % (gwsum, wait))
        # 预期量级: 上游等 0.6s 才发首字 → 等首字应 ≈600ms
        chk(wait is not None and int(wait) >= 500,
            "等首字 ≈ 上游的首字节延迟 600ms, 实得 %s" % wait)

        print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not FAIL else "FAILED", NCHK[0], len(FAIL)))
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
