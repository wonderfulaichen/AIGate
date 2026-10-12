# -*- coding: utf-8 -*-
"""探针: 响应缓存/并发去重会不会**跨供应商串内容**?

用户问题 (2026-10-11):「我如果跑多个任务跑同一个模型，不会路由错信息吧？」

为什么值得单独测: 缓存键 = **注入后请求体**的哈希 (见 cache.rs::make_key),
而去掉噪声字段后剩下的内容里**没有 provider** —— 注入只把 `model` 换成 upstream_model。
于是两个不同供应商、只要 upstream_model 同名且 messages 相同, 就是**同一个缓存键**。
流式不参与缓存, 所以只有 `stream:false` 的请求才可能撞上。

判据 (全部走真实 HTTP + 两个独立 mock 上游):
  ① 缓存开启时: alpha 的请求先打, beta 的**同一请求**不得拿到 alpha 的答案  ← 核心
  ② 对照组: 缓存关闭时两者各自打自己的上游 (证明 mock 能区分, 不是"都一样")
  ③ 并发同供应商的**相同**请求: 去重是**有意**行为, 应共享一次上游调用 (不算 bug)
  ④ 记录: 两条请求各自的 provider 落盘正确 (记账没串)

用法: python tools/probe_cache_cross_provider.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_cache_cross")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PA, PB = 8967, 8968, 8969

FAILS = []
HITS = {"alpha": 0, "beta": 0}


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def make_mock(tag, port):
    class M(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n)
            HITS[tag] += 1
            # 回显收到的 user 标记 —— 让"每个并发请求拿到的是不是自己的那一份"可判。
            try:
                req = json.loads(raw.decode("utf-8", "replace"))
                msgs = req.get("messages") or []
                echo = msgs[-1].get("content") if msgs else ""
            except Exception:
                echo = ""
            want_stream = False
            try:
                want_stream = bool(json.loads(raw.decode("utf-8", "replace")).get("stream"))
            except Exception:
                pass

            if want_stream:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                def frame(o):
                    self.wfile.write(b"data: " + json.dumps(o).encode() + b"\n\n")
                    self.wfile.flush()
                # 分两帧吐, 并把本次收到的标记回显进去
                frame({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                frame({"choices": [{"index": 0, "delta": {"content": "FROM-%s|ECHO=%s" % (tag.upper(), echo)},
                                    "finish_reason": None}]})
                frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            body = json.dumps({
                "id": "chatcmpl-" + tag,
                "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "FROM-" + tag.upper()}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), M)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


srv_a = make_mock("alpha", PA)
srv_b = make_mock("beta", PB)


def cache_stat():
    """读 /admin/api/cache —— 2026-10-12 起要 Bearer, token 由隔离实例生成在 data/.admin_token"""
    try:
        tok = io.open(os.path.join(ISO, "data", ".admin_token"),
                      encoding="utf-8", errors="replace").read().strip()
    except Exception:
        tok = ""
    req = urllib.request.Request("http://127.0.0.1:%d/admin/api/cache" % PG)
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def start_iso(cache_on):
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    # 两个供应商的 upstream_model **同名** —— 这正是用户问的"同一个模型"
    io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
        "providers": [
            {"name": "alpha", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PA,
             "api_key_env": "K", "api_key_default": "d",
             "models": {"alpha/shared": {"upstream_model": "m-shared"}}},
            {"name": "beta", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PB,
             "api_key_env": "K", "api_key_default": "d",
             "models": {"beta/shared": {"upstream_model": "m-shared"}}},
        ]
    }))
    env = dict(os.environ)
    env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1",
               CACHE_ENABLED="1" if cache_on else "0")
    env.pop("AIGATE_ADMIN_TOKEN", None)
    p = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            return p
        except Exception:
            time.sleep(0.5)
    print("隔离实例未就绪")
    sys.exit(1)


MSG = [{"role": "user", "content": "say the same thing"}]


def ask(model, stream=False, timeout=30):
    body = json.dumps({"model": model, "messages": MSG, "stream": stream}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode("utf-8", "replace")
        return r.status, t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


try:
    print("=== ① 缓存**开启**时: 两个供应商、同一 upstream_model、同一 messages ===\n")
    proc = start_iso(cache_on=True)
    try:
        time.sleep(0.5)
        st1, t1 = ask("alpha/shared")
        st2, t2 = ask("beta/shared")
        print("  alpha 响应片段:", t1[:120].replace("\n", " "))
        print("  beta  响应片段:", t2[:120].replace("\n", " "))
        ok_a = "FROM-ALPHA" in t1
        ok_b = "FROM-BETA" in t2
        ck("① alpha 拿到自己的上游答案", ok_a, "HTTP %s" % st1)
        ck("① ★beta 拿到**自己**的上游答案 (未串成 alpha 的)", ok_b,
           "beta 得到 %s" % ("FROM-ALPHA ← 串了!" if "FROM-ALPHA" in t2 else t2[:80]))
        hit = cache_stat()
        print("  缓存统计: hits=%d misses=%d entries=%d" % (hit["hits"], hit["misses"], hit["entries"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        proc = None

    print("\n=== ② 对照: 缓存**关闭**时两者各打自己的上游 ===\n")
    HITS["alpha"] = HITS["beta"] = 0
    proc = start_iso(cache_on=False)
    try:
        time.sleep(0.5)
        st1, t1 = ask("alpha/shared")
        st2, t2 = ask("beta/shared")
        ck("② 关闭缓存: alpha 与 beta 都拿到各自的答案",
           "FROM-ALPHA" in t1 and "FROM-BETA" in t2 and HITS["alpha"] >= 1 and HITS["beta"] >= 1,
           "alpha 上游命中 %d 次, beta %d 次" % (HITS["alpha"], HITS["beta"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        proc = None

    print("\n=== ③ 并发同供应商相同请求 (去重是设计行为) ===\n")
    HITS["alpha"] = HITS["beta"] = 0
    proc = start_iso(cache_on=True)
    try:
        time.sleep(0.5)
        res = []

        def one():
            res.append(ask("alpha/shared"))

        ths = [threading.Thread(target=one) for _ in range(5)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        bodies = [r[1] for r in res]
        ck("③ 并发 5 个相同请求都拿到正确内容",
           all("FROM-ALPHA" in b for b in bodies),
           "上游实际收到 %d 次 (去重生效则 <5)" % HITS["alpha"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        proc = None

    # ── ④ 用户的真实场景: 多个**流式**任务并发跑同一个模型, 各问各的 ──
    # 编程工具走的是流式; 流式不参与缓存/去重 (设计如此), 所以这里验的是
    # "并发流式会不会彼此串内容"。每个请求带不同标记, 答案必须与自己的标记对应。
    print("\n=== ④ 并发流式: 多个任务跑同一模型, 各问各的 (用户场景) ===\n")
    proc = start_iso(cache_on=True)
    try:
        time.sleep(0.5)
        got = {}
        lock = threading.Lock()

        def one_stream(i):
            body = json.dumps({
                "model": "alpha/shared",
                "messages": [{"role": "user", "content": "TASK-%d" % i}],
                "stream": True,
            }).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                         headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = b""
                    for _ in range(200):
                        ch = r.read(256)
                        if not ch:
                            break
                        raw += ch
                        if b"[DONE]" in raw:
                            break
                with lock:
                    got[i] = (r.status, raw.decode("utf-8", "replace"))
            except Exception as e:
                with lock:
                    got[i] = (0, str(e))

        ths = [threading.Thread(target=one_stream, args=(i,)) for i in range(6)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        # 每个流式响应必须含自己的答案与自己的标记 (证明没串到别人的请求上)
        bad = []
        for i in range(6):
            st, t = got.get(i, (0, ""))
            if st != 200 or "FROM-ALPHA" not in t:
                bad.append("task%d: HTTP %s 无内容" % (i, st))
            elif ("ECHO=TASK-%d" % i) not in t:
                bad.append("task%d: 回显不是自己的标记 ← 串了" % i)
        ck("④ 6 个并发流式请求各自拿到**自己**的答案 (无串扰)",
           not bad, "; ".join(bad) if bad else "全部 HTTP 200 且回显=自己的标记")
        # 流式不应产生缓存命中 (设计: 流式不缓存)
        hit = cache_stat()
        ck("④ 流式未参与缓存 (hits 应为 0)", hit["hits"] == 0,
           "hits=%d entries=%d 上游收到 %d 次" % (hit["hits"], hit["entries"], HITS["alpha"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 跨供应商未串内容")
finally:
    srv_a.shutdown()
    srv_b.shutdown()
