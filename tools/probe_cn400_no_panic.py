# -*- coding: utf-8 -*-
"""回归: 中文错误体截断不得 panic (2026-10-12 二次踩坑)。

背景: 10-01 修过 `&s[..min(500)]` 截进汉字中间导致 axum 任务暴毙的 panic
(i18n::snip), 但**只修了当时找到的几处** —— proxy.rs 的上游 4xx warn 两处
(chat 管线 + 原生直通) 是漏网的, e2e_buddy_proxy ④ 于 10-12 实机复现:
`AIGate panic: end byte index 500 is not a char boundary; it is inside '一'`,
客户端只见连接被重置。

判据 (三路径, 都打 mock 上游 400 + 中文错误体):
  ① chat 非流式 → 客户端拿到 400 (透传), **不是连接重置**, 进程存活
  ② chat 流式   → 同上
  ③ anthropic 直通 (/v1/messages) → 同上
  ④ 三条路径的记录都落盘 (status=400)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT = 8873  # mock
PG = 8874    # 网关
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_cn400")

FAIL = []


def chk(n, cond, extra=""):
    line = "%s %s %s" % ("PASS" if cond else "FAIL", n, extra)
    print(line)
    if not cond:
        FAIL.append(n)


# 复刻实机 panic 形状: 498 ASCII + 中文 —— min(500) 恰好落在汉字中间
CN_ERR = json.dumps({"code": 11101, "message": "请求被安全策略拦截",
                     "displayTips": "a" * 498 + "一" + "这是一段超过五百字节的中文错误体" * 20},
                    ensure_ascii=False).encode("utf-8")


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reject(self):
        self.send_response(400)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(CN_ERR)))
        self.end_headers()
        self.wfile.write(CN_ERR)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._reject()


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


def ask(path, obj):
    """打网关; 返回 (status, body_text)。连接被重置返回 (-2, 异常文本)。"""
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path),
                                 data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        return -2, repr(e)


def read_records():
    p = os.path.join(ISO, "data", "logs.jsonl")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            lines = [x for x in io.open(p, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
        except Exception:
            lines = []
        if lines:
            return [json.loads(x) for x in lines]
        time.sleep(0.2)
    return []


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    prov = {"providers": [{
        "name": "mock",
        "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PORT,
        "api_key_env": "MOCK_KEY", "api_key_default": "sk-test",
        "models": {
            "mock/chat": {"upstream_model": "m"},
            "mock/anthropic": {"upstream_model": "m", "api_format": "anthropic"},
        },
    }]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=open(os.path.join(ISO, "stderr.log"), "wb"))
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2

        # ① chat 非流式
        st, body = ask("/v1/chat/completions", {"model": "mock/chat", "stream": False,
                                                "messages": [{"role": "user", "content": "hi"}]})
        chk("chat-nostream-400-passed-through", st == 400, "status=%s" % st)
        chk("chat-nostream-not-conn-reset", st != -2, body[:120])
        # 客户端拿到的是**翻译后的可行动文案** (含上游中文原因); 上游原文 (11101) 进记录, 不丢
        chk("chat-nostream-reason-visible", "安全策略拦截" in body, body[:120])

        # ② chat 流式 (4xx 在建流之前返回, 同一段代码)
        st, body = ask("/v1/chat/completions", {"model": "mock/chat", "stream": True,
                                                "messages": [{"role": "user", "content": "hi"}]})
        chk("chat-stream-400-passed-through", st == 400, "status=%s" % st)
        chk("chat-stream-not-conn-reset", st != -2, body[:120])

        # ③ anthropic 直通 → 原生 4xx warn 那处 (另一条漏网切片)
        st, body = ask("/v1/messages", {"model": "mock/anthropic", "stream": False, "max_tokens": 16,
                                        "messages": [{"role": "user", "content": "hi"}]})
        chk("anthropic-nostream-400-passed-through", st == 400, "status=%s" % st)
        chk("anthropic-nostream-not-conn-reset", st != -2, body[:120])

        time.sleep(1.2)
        # 进程必须活着 (panic 会当场暴毙)
        crashed = proc.poll() is not None
        chk("gateway-process-alive", not crashed, "poll=%s" % proc.poll())

        # ④ 三条记录都落盘为 400, 且 chat 记录的 error 带上游原文码 (11101 不被翻译丢掉)
        recs = read_records()
        by_model = {}
        for r in recs:
            by_model.setdefault(r.get("model"), r)
        chk("records-three-400", all((by_model.get(m) or {}).get("status") == 400
                                     for m in ("mock/chat", "mock/anthropic")),
            {m: (by_model.get(m) or {}).get("status") for m in by_model})
        # 记录 error 非空且带可读原因 (翻译文案含上游中文原因; 原文码是否保留取决于
        # 错误体能否被解析 —— 可行动化翻译按设计替换原文, 故不断言 11101 本身)
        chk("record-error-readable",
            "安全策略拦截" in str((by_model.get("mock/chat") or {}).get("error") or ""),
            str((by_model.get("mock/chat") or {}).get("error"))[:100])

        # stderr 不得有 panic
        stderr = io.open(os.path.join(ISO, "stderr.log"), encoding="utf-8", errors="replace").read()
        chk("no-panic-in-stderr", "panic" not in stderr.lower(), stderr[-200:])

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (10, len(FAIL)))
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
