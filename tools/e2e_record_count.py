# -*- coding: utf-8 -*-
"""端到端: 请求记录条数一致性 —— 覆盖协议分支与客户端中途断开。

用户报障: "编程工具请求好多次, 网关记录才 2~3 次"。
第一版覆盖 7 种形态 (chat/流式/失败/并发去重/404/协议转换) 全部一一对应,
但漏了**编程工具真正走的路径**:
  · Claude Code / Cursor 大量走 /v1/messages (Anthropic 协议) 流式
  · 用户按 Esc / 工具超时会**中途取消请求** —— 断连后还记不记?

本版新增:
  ⑧ /v1/messages 非流式      ⑨ /v1/messages 流式
  ⑩ /v1/responses 流式        ⑪ 模型不存在 (路由失败)
  ⑫ 非法 JSON 请求体           ⑬ 流式 + 客户端中途断开 (最可疑)

判定: 每种形态「记录数 == 发出数」。
"""
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_reccount")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8991, 8992

MODE = {"v": "ok"}          # ok / 500 / 404 / slow
HITS = []


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        HITS.append(self.path)
        m = MODE["v"]
        if m in ("500", "404"):
            code = 500 if m == "500" else 404
            body = b'{"error":"boom"}' if m == "500" else b'{"error":{"message":"no model"}}'
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            b = json.loads(raw)
        except Exception:
            b = {}
        stream = bool(b.get("stream")) or m == "slow"
        if stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            delay = 0.4 if m == "slow" else 0.0
            try:
                for i in range(10 if m == "slow" else 3):
                    self.wfile.write(
                        ('data: {"choices":[{"delta":{"content":"x%d"}}]}\n\n' % i).encode())
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception:
                pass          # 客户端断开时写失败是预期路径
            return
        body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"mock/m": {"upstream_model": "m"}}},
    {"name": "dead", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d", "models": {"dead/m": {"upstream_model": "m"}}},
    {"name": "slow", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"slow/m": {"upstream_model": "m"}}},
]}))
io.open(os.path.join(ISO, "data", "cache_enabled.flag"), "w").write("1")

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _auth():
    """每次调用都重读 token：隔离实例启动时才创建 data/.admin_token，
    导入期读一次会拿到空（当年这个探针就是因此被 401 打回）。"""
    try:
        tok = io.open(os.path.join(ISO, "data", ".admin_token"), encoding="utf-8").read().strip()
    except Exception:
        return {}
    return {"Authorization": "Bearer " + tok} if tok else {}


def logs():
    req = urllib.request.Request("http://127.0.0.1:%d/admin/api/logs" % PG, headers=_auth())
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))


def post(path, body, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **_auth()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def post_raw(path, raw, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=raw,
                                 headers={"content-type": "application/json", **_auth()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def post_then_rst(path, body, read_bytes=300):
    """发流式请求, 读到一点就 RST 断连 —— 模拟用户按 Esc / 工具取消。"""
    payload = json.dumps(body).encode()
    s = socket.create_connection(("127.0.0.1", PG), timeout=15)
    head = ("POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n" % (path, len(payload)))
    s.sendall(head.encode() + payload)
    got = b""
    try:
        while len(got) < read_bytes:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
    except Exception:
        pass
    # SO_LINGER=0 → close 时发 RST, 让服务端立刻感知断连 (而非优雅 FIN)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()
    return len(got)


CASES = []


def count_before():
    return len(logs())


def case(label, n_sent, fn, wait=1.5):
    before = count_before()
    fn()
    time.sleep(wait)
    got = count_before() - before
    CASES.append((label, n_sent, got))
    flag = "OK " if got == n_sent else "!! "
    print("  %s%-40s 发出=%-3d 记录=%-3d %s" % (
        flag, label, n_sent, got, "" if got == n_sent else "<== 差 %d" % (n_sent - got)))


def count_prov(prov):
    return sum(1 for l in logs() if l.get("provider") == prov)


try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    print("=== 请求记录条数一致性 (扩展: 协议分支 + 客户端断开) ===\n")

    chat = {"model": "mock/m", "messages": [{"role": "user", "content": "hi"}]}
    anth = lambda body: {"max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], **body}

    case("① 非流式 chat x3", 3, lambda: [
        post("/v1/chat/completions", dict(chat, messages=[{"role": "user", "content": "a%d" % i}]))
        for i in range(3)])
    case("② 流式 chat x3", 3, lambda: [
        post("/v1/chat/completions", dict(chat, stream=True, messages=[{"role": "user", "content": "s%d" % i}]))
        for i in range(3)])

    MODE["v"] = "500"
    case("③ 上游 500 x2", 2, lambda: [
        post("/v1/chat/completions", dict(chat, messages=[{"role": "user", "content": "e%d" % i}]))
        for i in range(2)])
    MODE["v"] = "ok"

    case("④ 连接失败 x2", 2, lambda: [
        post("/v1/chat/completions", {"model": "dead/m", "messages": [{"role": "user", "content": "d%d" % i}]})
        for i in range(2)])

    def c5():
        body = {"model": "mock/m", "messages": [{"role": "user", "content": "same-body-dup"}]}
        ths = [threading.Thread(target=lambda: post("/v1/chat/completions", body)) for _ in range(5)]
        [t.start() for t in ths]
        [t.join() for t in ths]
    case("⑤ 并发去重 (同 body 同时 5 个)", 5, c5)

    MODE["v"] = "404"
    case("⑥ 上游 404 x1", 1, lambda: post("/v1/chat/completions", dict(chat, messages=[{"role": "user", "content": "404"}])))
    MODE["v"] = "ok"

    case("⑦ responses→chat 转换 x1", 1, lambda: post("/v1/responses", {
        "model": "mock/m", "stream": False,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}))

    # ── 新增: 编程工具真正走的路径 ──
    case("⑧ /v1/messages 非流式 x2", 2, lambda: [
        post("/v1/messages", anth({"model": "mock/m", "stream": False, "n": i})) for i in range(2)])

    case("⑨ /v1/messages 流式 x2", 2, lambda: [
        post("/v1/messages", anth({"model": "mock/m", "stream": True, "n": i})) for i in range(2)])

    case("⑩ /v1/responses 流式 x1", 1, lambda: post("/v1/responses", {
        "model": "mock/m", "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}))

    case("⑪ 模型不存在 (路由失败) x1", 1, lambda: post(
        "/v1/chat/completions", {"model": "nope/none", "messages": [{"role": "user", "content": "hi"}]}))

    case("⑫ 非法 JSON 请求体 x1", 1, lambda: post_raw("/v1/chat/completions", b'{"model": broken'))

    # ⑬ 流式 + 客户端中途断开: 单独用 slow 供应商计数, 免得被别的记录干扰
    MODE["v"] = "slow"
    before_slow = count_prov("slow")
    got = post_then_rst("/v1/chat/completions",
                        {"model": "slow/m", "stream": True,
                         "messages": [{"role": "user", "content": "cancel-me"}]})
    time.sleep(7.0)          # 等慢流跑完 + 落盘余量
    after_slow = count_prov("slow")
    MODE["v"] = "ok"
    n13 = after_slow - before_slow
    CASES.append(("⑬ 流式+客户端中途断开 x1", 1, n13))
    print("  %s%-40s 发出=%-3d 记录=%-3d   (读到 %d 字节后 RST)" % (
        "OK " if n13 == 1 else "!! ", "⑬ 流式+客户端中途断开 x1", 1, n13, got))

    # ── ⑭ 思考强度落记录 (用户报障: 编程工具调了档位, 详情却显示 —) ──
    # 三例分别验证: 客户端档位 / 显式关闭 / 无思考无配置 的采集与展示回落链.
    print("\n=== ⑭ 思考强度落记录 ===")
    effort_ok = True

    def show(label, cond, detail):
        global effort_ok
        if not cond:
            effort_ok = False
        print("  %s%-46s %s" % ("OK " if cond else "!! ", label, detail))

    # A) 客户端带档位: 原始字段与展示字段都应是 high
    post("/v1/chat/completions", dict(chat, reasoning_effort="high",
                                      messages=[{"role": "user", "content": "effort-high"}]))
    time.sleep(1.2)
    la = logs()[0]
    show("A 客户端 reasoning_effort=high",
         la.get("effort") == "high" and la.get("reasoning_effort") == "high",
         "effort=%r 展示=%r" % (la.get("effort"), la.get("reasoning_effort")))

    # B) 显式关闭: effort=off, 展示为"已关闭"文案 (而非回落面板配置)
    post("/v1/chat/completions", dict(chat, thinking=False,
                                      messages=[{"role": "user", "content": "effort-off"}]))
    time.sleep(1.2)
    lb = logs()[0]
    show("B 客户端 thinking:false → off",
         lb.get("effort") == "off" and "已关闭" in (lb.get("reasoning_effort") or ""),
         "effort=%r 展示=%r" % (lb.get("effort"), lb.get("reasoning_effort")))

    # C) 无思考 + 面板无配置: effort 应为 null, 展示为空 (详情页显示 —)
    post("/v1/chat/completions", dict(chat, messages=[{"role": "user", "content": "effort-none"}]))
    time.sleep(1.2)
    lc = logs()[0]
    show("C 无思考无配置 → 展示 —",
         (lc.get("effort") is None) and (lc.get("reasoning_effort") == ""),
         "effort=%r 展示=%r" % (lc.get("effort"), lc.get("reasoning_effort")))

    print("\n=== 结果 ===")
    bad = [(l, n, g) for l, n, g in CASES if n != g]
    if bad:
        print("  FAIL —— 以下形态记录数不符 (这些就是漏记的路径):")
        for l, n, g in bad:
            print("    %s: 发出 %d, 记录 %d" % (l, n, g))
    if not effort_ok:
        print("  FAIL —— 思考强度采集/展示链不符 (⑭)")
    ok = (not bad) and effort_ok
    if ok:
        print("  PASS —— 13 种形态记录一一对应 + 思考强度三例采集/展示全部正确")
    sys.exit(0 if ok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
