# -*- coding: utf-8 -*-
"""探针: 网关主动截断（循环检测）必须在**记录里留下可读原因**（2026-10-11）。

背景（用户报障）: "因为软件导致的停止等各种软件操作不会出报告的。我都不知道是不是软件导致的问题。"
取证: 日志里 18 条 `model loop detected` 对应的记录**全部**是 `status=200 + error=''`,
      记录页显示 `200 / 错误详情 —` —— 用户完全看不出是网关掐的。
根因: 循环截断走的是「发 [DONE] → 客户端关连接 → Drop 兜底落盘」这条路径，
      而 Drop 见 `clean_finish=true` 就写空 error（以为"正常收尾不该标红"），
      同时该路径**绕过了** None 分支的收尾日志（循环分支里直接 `return Poll::Ready(None)`），
      于是 loop_aborted 分支写的文案从未被写出。

判据 (5 条):
  ① 循环被触发 → 客户端收到 `finish_reason=length` + [DONE]（既有行为，不能被改坏）
  ② 该请求**必须有记录**（不能人间蒸发）
  ③ 记录的 error 必须非空，且**指明是网关截断**
  ④ error 文案必须与 None 分支口径一致（同一件事不能有两种说法）
  ⑤ 对照: 正常请求的记录 error 仍为空（不能被这次修复误标红）

不连网; 本地 mock 上游.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_loopvis")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8963, 8964

# 与 record 文案保持一致的期望串 (两处必须同源)
LOOP_MSG = "model loop detected, stream truncated by AIGate"

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("  —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    """按请求内容分流: [LOOP] → 死循环; 否则正常回答."""

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            content = (json.loads(raw.decode("utf-8")).get("messages") or [{}])[-1].get("content", "")
        except Exception:
            content = ""
        loop = "[LOOP]" in (content or "")

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def frame(obj):
            # 网关截断后客户端会断开 → 后续写入抛 ConnectionAborted; 属预期, 静默停止.
            try:
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()
            except (ConnectionError, OSError):
                raise _ClientGone()

        if loop:
            try:
                # 触发死循环: 同一段正文重复足够多次 (min_repeat 默认 8)
                for _ in range(40):
                    frame({"choices": [{"index": 0, "delta": {"content": "循环输出"}, "finish_reason": None}]})
                frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 400}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except _ClientGone:
                pass
            return

        frame({"choices": [{"index": 0, "delta": {"content": "正常回答"}, "finish_reason": None}]})
        frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


class _ClientGone(Exception):
    """网关主动截断后客户端断开 → 停止继续写 mock 响应 (预期路径, 非错误)."""


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"mock/m": {"upstream_model": "m"}}},
]}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def post(content):
    body = {"model": "mock/m", "messages": [{"role": "user", "content": content}], "stream": True}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("实例未就绪")
        sys.exit(1)

    print("=== 网关主动截断是否留痕 ===\n")

    # ① 循环请求: 客户端读完整流 (模拟"收完 [DONE] 后关闭")
    loop_body = post("[LOOP] 请写一段话")
    # ⑤ 对照: 正常请求
    post("正常问一句")
    time.sleep(1.5)

    ck("① 客户端收到 finish_reason=length 收尾",
       '"finish_reason": "length"' in loop_body or '"finish_reason":"length"' in loop_body,
       "尾部 %r" % loop_body[-90:])
    ck("①b 客户端收到 [DONE]", "[DONE]" in loop_body)

    logs = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/logs?limit=10" % PG), timeout=20
    ).read().decode("utf-8"))
    rows = logs.get("logs") if isinstance(logs, dict) else logs
    print("   记录 %d 条:" % len(rows or []))
    for r in (rows or []):
        print("      status=%s in=%s out=%s err=%r" % (r.get("status"), r.get("prompt_tokens"),
                                                       r.get("completion_tokens"), (r.get("error") or "")[:70]))
    # ⚠️ 识别方式必须与 error 文案**无关** —— 否则"文案缺失"会让"记录是否存在"一起失败,
    #    两条断言混成一条 (实测踩过: 回退文案修复后, ② 因找不到行而误报"没人间蒸发").
    #    截断请求的稳定特征: 没有 usage (in=out=0) —— 因为它在 usage 帧之前就被掐了.
    loop_row = None
    normal_row = None
    for r in (rows or []):
        e = r.get("error") or ""
        if (r.get("prompt_tokens") or 0) == 0 and (r.get("completion_tokens") or 0) == 0:
            loop_row = r          # 截断: 无 usage
        elif not e and (r.get("completion_tokens") or 0) > 0:
            normal_row = r        # 正常: 有 usage 且无错误

    ck("② 循环截断的请求有记录 (未人间蒸发)", loop_row is not None, "实得 %s" % ("有" if loop_row else "无"))
    ck("③ 记录 error 非空且指明是网关截断",
       bool(loop_row and (loop_row.get("error") or "").strip()),
       "error=%r" % ((loop_row or {}).get("error")))
    ck("④ 文案与收尾分支口径一致", bool(loop_row and LOOP_MSG in (loop_row.get("error") or "")),
       "期望含 %r, 实得 %r" % (LOOP_MSG, (loop_row or {}).get("error")))
    ck("⑤ 正常请求不被误标红", normal_row is not None and not (normal_row.get("error") or "").strip(),
       "正常记录 error=%r" % ((normal_row or {}).get("error")))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 网关主动截断在记录里可见且指明原因")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
