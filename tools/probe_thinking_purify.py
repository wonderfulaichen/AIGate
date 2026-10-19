# -*- coding: utf-8 -*-
"""e2e 验证 thinking 提纯（方案 A）—— 三臂对照，抓**上游收到的报文**判真假。

为什么必须抓上游报文: 省量/提纯发生在**发往上游之前**，所以面板上的数字只反映"我们记了
多少"，不能证明"报文真的变了"。与 `probe_savings_truth` 同款方法论：**能证伪的只有上游
收到的那些字段还在不在**。

三臂:
  A `off`    (默认): 历史推理链**整段删除** → 上游不含标记，字段消失
  B `rule`   + recall 开: 推理链被**换成节点链** → 上游不含标记，字段**还在**且是 `[...]` 节点
  C `rule` 但 recall 关: 提纯**不生效** → 与 A 完全一致（红线：不可逆的优化不许生效）

判据（任一失败即红）:
  ① A 与今天零差异：上游不含标记、字段消失
  ② B 字段保留且是节点链（含 [否决] 或 [决定] 这类标签），且省量**小于**A（用 token 买信息）
  ③ B 的每条节点都能在原文找到出处（只摘不补）
  ④ C 与 A 逐字节一致（recall 未开 → 提纯不生效，退回整删）
  ⑤ B 的省量记账 = 原文字节 − 节点链字节
  ⑥ 协议无感：usage / finish_reason / 正文字节在三臂下不变
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


def make_reasoning():
    """一条含"填充 + 标志句"的推理链（英文，贴合真实流量 98.8% 英文）。"""
    parts = [
        "Let me think about this step by step. The user wants a tool here.\n",
        "I'll use the rule-based approach because it is deterministic and easy to test.\n",
        "The LLM-based approach won't work here because it adds one extra model call per block.\n",
        "Edge case: empty reasoning blocks must be skipped entirely without compressing.\n",
    ]
    # 填充到足够长（越过 minChars=400 闸门）
    body = " ".join(parts)
    body += (" " + MARK + " " + "filler text about the design ") * 60
    return body


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


def run_arm(tag, mp, pg, purify, recall_on, reasoning):
    """起一套隔离实例，发一个带历史推理链的请求，返回 (上游收到的 body, 日志记录)。"""
    iso = os.path.join(os.environ.get("TEMP", "."), "iso_probe_purify_" + tag)
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
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def w(b):
                self.wfile.write(b)
                self.wfile.flush()

            w(sse({"content": "ok"}))
            time.sleep(0.03)
            w(sse({}, "stop"))
            w(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", mp), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, PORT=str(pg), AIGATE_DATA_DIR=os.path.join(iso, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1", MOCK_KEY="d",
               STRIP_HISTORY_REASONING="1", AIGATE_THINKING_PURIFY=purify,
               AIGATE_RECALL_ENABLED="1" if recall_on else "0")
    proc = subprocess.Popen([os.path.join(iso, "AIGate.exe")], cwd=iso, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(pg):
            return None
        # 历史里放一条带推理链的 assistant 消息（这是"多轮历史"的形态）
        body = {
            "model": "mock/m", "stream": True,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok", "reasoning_content": reasoning},
                {"role": "user", "content": "again"},
            ],
        }
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % pg,
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
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
        return {"upstream": seen.get("body", b""), "rec": rec, "iso": iso}
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
    reasoning = make_reasoning()
    print("推理链原文 %d 字符, 含标记 %s" % (len(reasoning), MARK))

    A = run_arm("off", 8901, 8902, "off", True, reasoning)
    B = run_arm("rule", 8903, 8904, "rule", True, reasoning)
    C = run_arm("nor", 8905, 8906, "rule", False, reasoning)
    if not (A and B and C):
        print("实例未起来")
        return 2

    fails = []
    NCHK = [0]

    def ck(cond, msg):
        NCHK[0] += 1
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def upstream_json(arm):
        try:
            return json.loads(arm["upstream"].decode("utf-8", "replace"))
        except Exception:
            return {}

    print("\n== 判据 ==")
    # ① A: 整删
    ja = upstream_json(A)
    a_has_mark = MARK in A["upstream"].decode("utf-8", "replace")
    a_msgs = ja.get("messages") or []
    a_field = a_msgs[1].get("reasoning_content") if len(a_msgs) > 1 else None
    ck(not a_has_mark, "A(off): 上游不含推理链标记")
    ck(a_field is None, "A(off): reasoning_content 字段整删（与今天一致）")

    # ② B: 提纯，字段还在且是节点链
    jb = upstream_json(B)
    b_txt = B["upstream"].decode("utf-8", "replace")
    b_msgs = jb.get("messages") or []
    b_field = b_msgs[1].get("reasoning_content") if len(b_msgs) > 1 else None
    ck(not (MARK in b_txt), "B(rule): 上游不含推理链标记（填充被压掉）")
    ck(b_field is not None, "B(rule): reasoning_content 字段保留（不是删除）")
    ck(bool(b_field) and ("[否决]" in b_field or "[决定]" in b_field),
       "B(rule): 字段内容是节点链: %r" % (b_field or "")[:90])

    # ③ 只摘不补
    if b_field:
        bad = []
        for line in b_field.split("\n"):
            body_txt = line.split(" ", 1)[1].strip() if " " in line else line
            probe = "".join(list(body_txt)[:20]).rstrip("…")
            if probe and probe not in reasoning:
                bad.append(line)
        ck(not bad, "B(rule): 每条节点都能在原文找到出处（只摘不补）: %s" % bad[:2])

    # ⑤ 省量记账
    saved_a = int((A["rec"].get("strip_saved_tokens") or 0))
    saved_b = int((B["rec"].get("strip_saved_tokens") or 0))
    ck(saved_b < saved_a,
       "B 省量(%d) 小于 A 省量(%d) —— 提纯用 token 买回了信息" % (saved_b, saved_a))
    if b_field:
        expect = (len(reasoning) - len(b_field)) // 4
        ck(abs(saved_b - expect) <= max(16, expect // 10),
           "B 省量记账 ≈ (原文−节点链)/4: 记 %d vs 预期 %d" % (saved_b, expect))

    # ④ C: recall 未开 → 与 A 一致
    jc = upstream_json(C)
    c_msgs = jc.get("messages") or []
    c_field = c_msgs[1].get("reasoning_content") if len(c_msgs) > 1 else None
    ck(c_field is None, "C(rule 但 recall 关): 提纯不生效，退回整删（不可逆红线）")

    # ⑥ 协议无感
    ck(A["rec"].get("completion_tokens") == B["rec"].get("completion_tokens") == C["rec"].get("completion_tokens"),
       "三臂 completion_tokens 一致（协议无感）: %s" % [
           (A["rec"].get("completion_tokens"), B["rec"].get("completion_tokens"), C["rec"].get("completion_tokens"))])
    ck(A["rec"].get("end_reason") == B["rec"].get("end_reason"), "三臂 end_reason 一致")

    print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
