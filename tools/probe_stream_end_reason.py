# -*- coding: utf-8 -*-
"""探针: 记录里的「结束方式」(end_reason) —— 回答"输出为什么停了"。

背景（2026-10-11, 用户第四次报同一症状「怎么输出一会又停了？什么问题？」）：
    网关侧**什么都查不出来** —— 18:59 重启后 25 个请求全部 200、零 WARN、零截断，
    可用户确实看到输出停在半路。根因不是"网关没干坏事", 而是**记录里根本没有
    "这轮为什么结束"这个信息** —— 于是三种完全不同的停止在记录上长得一模一样：
        · 模型自己说完 (finish_reason=stop)
        · 上游只发了 [DONE]、没说为什么停   ← 最可疑的一类
        · 上游连终止帧都没发就断 (eof)
    用户原话:「因为软件导致的停止等各种软件操作不会出报告的, 我都不知道是不是软件导致的」。

判据 (5 条):
  ① 上游给 stop      → end_reason = "stop"
  ② 上游只给 [DONE]   → end_reason = "done"  ★ 与 ① 必须分得开, 这就是本探针的牙齿
  ③ 上游给 length    → end_reason = "length"（"答不完"不能被洗成"答完了"）
  ④ 客户端中途断开    → end_reason = "abort" + 499
  ⑤ 字段确实落进 logs.jsonl（不只是 API 内存里）

不连网: 本地 mock 上游。凭据/端口全在隔离目录里。
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_endreason")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8961, 8962

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    """按上游模型名分流, 模拟四种结束形态."""

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            model = (json.loads(raw.decode("utf-8", "replace")) or {}).get("model") or ""
        except Exception:
            model = ""
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def frame(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        def text(t):
            frame({"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]})

        try:
            self._emit(model, frame, text)
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            # 客户端中途断开是 m_abort 的**预期行为**, 不是 mock 出错 (别打出假 traceback).
            pass

    def _emit(self, model, frame, text):
        frame({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        text("开始回答。 TAIL-START")

        if model == "m_stop":
            text("按上游交代正常收尾。 TAIL-STOP")
            frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 20, "completion_tokens": 32}})
            self.wfile.write(b"data: [DONE]\n\n")
        elif model == "m_done":
            # ★ 关键形态: 有内容、有 [DONE], 但**从未给过 finish_reason** ——
            #   在旧记录里它与"正常说完"完全同形。
            text("上游没说自己为什么停, 直接收了尾。 TAIL-DONE")
            self.wfile.write(b"data: [DONE]\n\n")
        elif model == "m_length":
            text("输出额度用尽, 只能到此为止。 TAIL-LENGTH")
            frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                   "usage": {"prompt_tokens": 20, "completion_tokens": 8}})
        elif model == "m_abort":
            # 客户端会在读到这里之后断开: 慢速继续吐, 给断开留出窗口。
            for _ in range(40):
                text("还在生成…… TAIL-ABORT")
                time.sleep(0.1)
        else:
            frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})

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
     "api_key_env": "K", "api_key_default": "d",
     "models": {
         "mock/" + m: {"upstream_model": m}
         for m in ("m_stop", "m_done", "m_length", "m_abort")
     }},
]}))

env = dict(os.environ)
env.pop("LOOP_GUARD_ENABLED", None)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stream(model, read_secs=None):
    """走网关发一条流式请求; read_secs 非 None 时读够就主动断开 (模拟客户端中途取消)."""
    body = json.dumps({"model": "mock/" + model,
                       "messages": [{"role": "user", "content": "hi"}],
                       "stream": True}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if read_secs is None:
            return r.read().decode("utf-8", "replace")
        time.sleep(read_secs)
        # 直接关底层连接: 让网关侧的流被 Drop (这就是"客户端中断").
        r.fp.close()
        try:
            r.close()
        except Exception:
            pass
    return ""


def recs():
    raw = urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/logs?limit=50" % PG), timeout=20
    ).read().decode("utf-8")
    j = json.loads(raw)
    return j if isinstance(j, list) else (j.get("logs") or [])


def find(rows, model):
    for r in rows:
        if r.get("model") == "mock/" + model:
            return r
    return None


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

    print("=== 「结束方式」可观测性: 三种同形停止必须分得开 ===\n")

    txt_stop = stream("m_stop")
    time.sleep(0.6)
    txt_done = stream("m_done")
    time.sleep(0.6)
    txt_len = stream("m_length")
    time.sleep(0.6)
    # 客户端中途断开 (读 1.2s 就走)
    stream("m_abort", read_secs=1.2)
    time.sleep(2.0)

    rows = recs()
    r_stop, r_done, r_len, r_abort = (find(rows, m) for m in
                                      ("m_stop", "m_done", "m_length", "m_abort"))

    ck("① 上游给了 stop → end_reason=stop", (r_stop or {}).get("end_reason") == "stop",
       "实得 %r" % (r_stop or {}).get("end_reason"))
    # ★ 本探针的牙齿: 旧实现下这条必然是 "stop"(或缺失) —— 因为"只发 [DONE]"与
    #   "正常说完"当时同形 (都置 clean_finish). 只有真的分开记才能过。
    ck("② 上游只发 [DONE]、未说原因 → end_reason=done (与 ① 分开)",
       (r_done or {}).get("end_reason") == "done",
       "实得 %r" % (r_done or {}).get("end_reason"))
    ck("③ 上游给了 length → end_reason=length", (r_len or {}).get("end_reason") == "length",
       "实得 %r" % (r_len or {}).get("end_reason"))
    ck("④ 客户端中途断开 → end_reason=abort 且 499",
       (r_abort or {}).get("end_reason") == "abort" and (r_abort or {}).get("status") == 499,
       "实得 %r / %r" % ((r_abort or {}).get("end_reason"), (r_abort or {}).get("status")))

    # ② 的"有内容"必须还在 (别为了记形态把响应搞坏).
    # 判据用 ASCII 标记: mock 的 json.dumps 默认把中文转成 \uXXXX, 直接找中文会假红.
    ck("④b ②的响应内容完整送达 (未被形态采集影响)",
       "TAIL-DONE" in txt_done, "len=%d" % len(txt_done))
    # `done` 不是错误: 上游没交代 ≠ 失败, 不该标红
    ck("④c `done` 不写错误文案 (未交代 ≠ 失败)",
       not ((r_done or {}).get("error") or "").strip(),
       "error=%r" % (r_done or {}).get("error"))

    # ⑤ 落盘: 直接读 logs.jsonl (API 只是内存缓冲, 不算"真的记下来了")
    disk = []
    p = os.path.join(ISO, "data", "logs.jsonl")
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8", errors="replace").read().splitlines():
            if line.strip():
                try:
                    disk.append(json.loads(line))
                except Exception:
                    pass
    on_disk = find(disk, "m_done")
    ck("⑤ end_reason 确实落进 logs.jsonl", (on_disk or {}).get("end_reason") == "done",
       "磁盘上 %d 行, m_done 的 end_reason=%r" % (len(disk), (on_disk or {}).get("end_reason")))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 记录页「结束方式」能区分 模型说完 / 上游没交代 / 额度用尽 / 客户端取消")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
