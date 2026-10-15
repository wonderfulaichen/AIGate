# -*- coding: utf-8 -*-
"""实测「省量」是不是真的 —— 用户质疑: "你确定我们软件是真的省token？我去对比了上游的
token 好像基本一致啊？"

**先解释为什么"对比上游"这个方法本身不可能看出来**:
省量发生在**发往上游之前** (历史推理链被剥掉后才出网), 所以上游从头到尾只看到剥完的
那一份。我们面板显示的「输入 TOKENS」就是**上游上报的原值**(逐字节相同), 两边必然一致 ——
与"有没有省"无关。这个对比是个恒等式, 既不能证实也不能证伪。

**能证伪的只有一件事: 上游收到的报文里, 那些字段到底还在不在。**
本探针就用 mock 上游把它抓下来看, 并做**正反对照**:

  用例 A (默认开剥离): 上游收到的 body **不该**含推理链标记 → 省量是真的
  用例 B (STRIP_HISTORY_REASONING=0): 上游收到的 body **必须**含标记 → 证明确实是剥离起作用,
        而不是"标记本来就没发出去"(否则用例 A 是假通过)

判据 (任一失败即红):
  ① A: 上游收到的报文不含推理链标记 (剥离真的发生在出网之前)
  ② A: 上游收到的字节数 < 客户端发出的字节数 (报文确实变小了)
  ③ A: 日志记的省量 token 与"实际少掉的字节数/4"吻合 (记账与实际一致, 不虚报)
  ④ A: mock 上报的 prompt_tokens (由收到的字节数算出) 明显小于客户端发出的量
  ⑤ B: 上游收到的报文**含**标记 且 日志省量为 0 (对照组)
  ⑥ A vs B: 上游侧看到的 token 数在 A 下显著更少 (上游真的"少收"了)
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

MARK = "ZZREASONZZ"
# 推理链内容: 纯 ASCII, 便于 chars≈bytes, 让 /4 估算可与真实体量对照
FILLER = "the quick brown fox jumps over the lazy dog 0123456789 " * 8000  # ~440KB

FAIL = []
NCHK = [0]


def chk(cond, msg):
    NCHK[0] += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def sse(delta, finish=None):
    return ("data: " + json.dumps({
        "id": "c-" + uuid.uuid4().hex[:6], "object": "chat.completion.chunk", "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n").encode("utf-8")


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


def run_case(tag, mp, pg, strip_on):
    """起一套隔离实例, 发一个带大推理链历史的请求, 返回 (上游收到的 body, 日志记录, 客户端发出的字节数)."""
    iso = os.path.join(os.environ.get("TEMP", "."), "iso_probe_savings_" + tag)
    if os.path.isdir(iso):
        shutil.rmtree(iso, ignore_errors=True)
    os.makedirs(os.path.join(iso, "data"))
    os.makedirs(os.path.join(iso, "config"), exist_ok=True)
    shutil.copy(EXE, os.path.join(iso, "AIGate.exe"))

    with io.open(os.path.join(iso, "providers.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": [{
            "name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % mp,
            "api_key_env": "MOCK_KEY", "api_key_default": "d",
            "models": {"mock/m": {"upstream_model": "m", "api_format": "openai"}},
        }]}, f)

    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n)
            seen["body"] = raw
            # 模拟真实上游: usage 里报的 prompt_tokens 由**收到的报文**算出
            # (这样"上游少收了多少"就有了可对照的数)
            pt = len(raw) // 4
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def w(b):
                self.wfile.write(b)
                self.wfile.flush()

            w(sse({"content": "ok"}))
            time.sleep(0.03)
            w(b'data: ' + json.dumps({
                "id": "c-end", "object": "chat.completion.chunk", "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": 2, "total_tokens": pt + 2},
            }).encode("utf-8") + b"\n\n")
            w(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", mp), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = dict(os.environ, PORT=str(pg), AIGATE_DATA_DIR=os.path.join(iso, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1", MOCK_KEY="d")
    env["STRIP_HISTORY_REASONING"] = "1" if strip_on else "0"
    proc = subprocess.Popen([os.path.join(iso, "AIGate.exe")], cwd=iso, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(pg):
            print("实例未起来 (%s)" % tag)
            return None
        # 请求体: 第一条 assistant 消息带一条很大的 reasoning_content (含标记),
        # 这正是"编程工具把上一轮思考原样回传"的形态。
        body = {
            "model": "mock/m", "stream": True,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok",
                 "reasoning_content": MARK + " " + FILLER},
                {"role": "user", "content": "again"},
            ],
        }
        sent = json.dumps(body).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % pg, data=sent,
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        # 日志写入是异步的
        rec = {}
        lp = os.path.join(iso, "data", "logs.jsonl")
        for _ in range(50):
            try:
                rows = [x for x in io.open(lp, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
                if rows:
                    rec = json.loads(rows[-1])
                    break
            except Exception:
                pass
            time.sleep(0.2)
        return {"body": seen.get("body", b""), "rec": rec, "sent": sent,
                "sent_len": len(sent), "upstream_len": len(seen.get("body", b""))}
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        if not os.environ.get("KEEP_ISO"):
            shutil.rmtree(iso, ignore_errors=True)


def main():
    if not os.path.exists(EXE):
        print("缺少构建产物: %s (先 cargo build --release)" % EXE)
        return 2
    a = run_case("on", 8894, 8895, True)
    b = run_case("off", 8896, 8897, False)
    if not a or not b:
        return 2

    print("\n== 用例 A (默认: 剥离开) ==")
    print("   客户端发出 %d B, 上游收到 %d B" % (a["sent_len"], a["upstream_len"]))
    ra = a["rec"]
    sa = int(ra.get("strip_saved_tokens") or 0)
    print("   日志: strip_saved_tokens=%d, prompt_tokens=%d (上游上报)"
          % (sa, int(ra.get("prompt_tokens") or 0)))
    print("   日志: req_bytes_orig=%s, req_bytes=%s (新增字段, 供用户自查)"
          % (ra.get("req_bytes_orig"), ra.get("req_bytes")))

    print("\n== 用例 B (对照: 剥离关) ==")
    print("   客户端发出 %d B, 上游收到 %d B" % (b["sent_len"], b["upstream_len"]))
    rb = b["rec"]
    sb = int(rb.get("strip_saved_tokens") or 0)
    print("   日志: strip_saved_tokens=%d, prompt_tokens=%d (上游上报)"
          % (sb, int(rb.get("prompt_tokens") or 0)))
    print("   日志: req_bytes_orig=%s, req_bytes=%s"
          % (rb.get("req_bytes_orig"), rb.get("req_bytes")))

    mark = MARK.encode()
    in_a = mark in a["body"]
    in_b = mark in b["body"]

    print("\n== 判据 ==")
    chk(not in_a, "A: 上游收到的报文**不含**推理链标记 (剥离真的发生在出网之前)")
    chk(a["upstream_len"] > 0 and a["upstream_len"] < a["sent_len"],
        "A: 上游收到的字节数 (%d) < 客户端发出的 (%d)" % (a["upstream_len"], a["sent_len"]))
    # 省量 token 应与"实际少掉的字节/4"吻合 (估算口径就是 ÷4)
    removed = a["sent_len"] - a["upstream_len"]
    expect = removed // 4
    chk(sa > 0 and abs(sa - expect) <= max(64, expect // 50),
        "A: 日志省量 %d 与实少字节/4 (%d) 吻合 (记账不虚报)" % (sa, expect))
    ua = int(ra.get("prompt_tokens") or 0)
    ub = int(rb.get("prompt_tokens") or 0)
    chk(ua > 0 and ua < ub, "A 的上游 token (%d) < B 的上游 token (%d)" % (ua, ub))
    # 对照组: 关掉后标记必须原样送达, 且省量为 0 —— 否则用例 A 是假通过
    chk(in_b and sb == 0, "B(对照): 上游**含**标记且省量为 0 → 证明 A 是剥离起作用, 不是标记本来就没发")
    chk(sb == 0 and a["upstream_len"] < b["upstream_len"],
        "A/B 上游侧字节差 %d B → 这一份就是真省下的量" % (b["upstream_len"] - a["upstream_len"]))

    # ── 新增: 日志里的可核对字段必须真的落盘, 且与 mock 实测字节一致 ──
    # 这是用户能自查的关键 —— 没有它, "省了多少"就只能靠相信面板那一栏。
    ra_o, ra_s = int(ra.get("req_bytes_orig") or 0), int(ra.get("req_bytes") or 0)
    rb_o, rb_s = int(rb.get("req_bytes_orig") or 0), int(rb.get("req_bytes") or 0)
    chk(ra_o > 0 and ra_s > 0, "A: 日志落盘了请求体字节 (orig=%d, sent=%d)" % (ra_o, ra_s))
    # 实发字节应≈上游收到的字节 (允许护栏/续写等微小差异)
    chk(abs(ra_s - a["upstream_len"]) <= max(64, a["upstream_len"] // 20),
        "A: 日志实发 %d ≈ 上游实收 %d (差 %d B)" % (ra_s, a["upstream_len"], abs(ra_s - a["upstream_len"])))
    # 原始字节应≈客户端发出的 (允许注入 system 等少量增加)
    chk(abs(ra_o - a["sent_len"]) <= max(4096, a["sent_len"] // 10),
        "A: 日志原始 %d ≈ 客户端发出 %d" % (ra_o, a["sent_len"]))
    chk(ra_o > ra_s, "A: 原始 %d > 实发 %d → 确实改小了 (差 %d B)" % (ra_o, ra_s, ra_o - ra_s))
    # 字节差 ÷4 应与省量 token 同量级 (交叉核对口径成立)
    chk(abs((ra_o - ra_s) // 4 - sa) <= max(64, sa // 5),
        "A: 字节差/4 (%d) 与省量 token (%d) 吻合 → 两栏可互相核对" % ((ra_o - ra_s) // 4, sa))
    # 对照组: 关掉剥离 → 原始 ≈ 实发 (允许注入导致的少量增加), 且字节差可忽略
    # ⚠️ 不能断言"完全相等": 网关仍会注入 system/思考档位等, 实测 23B 差。判据要按"有无实质缩减"。
    chk(rb_o > 0 and rb_s > 0 and (rb_o - rb_s) < max(256, rb_o // 100),
        "B(对照): 原始 %d ≈ 实发 %d (差 %d B, 仅注入量) → 未改动是**实测值**, 不是未采集"
        % (rb_o, rb_s, rb_o - rb_s))

    print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not FAIL else "FAILED", NCHK[0], len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
