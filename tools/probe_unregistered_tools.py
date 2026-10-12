# -*- coding: utf-8 -*-
"""
「假成功」检测端到端探针（2026-10-12，对标 dsh-our-free-model 的同一处修复）。

场景：本轮只产出**调用方从未声明的工具调用**、无任何可见正文。旧实现以
`200 · 错误 —` 收尾 —— 界面显示成功，而那轮对调用方毫无意义（它没法执行一个
自己没注册的工具）。现在网关必须如实报告为 502 且带错误文案。

断言（6 组）：
  1 未声明的工具 + 无正文        → 502 + 错误文案含工具名
  2 未声明的工具 + **有正文**    → 200（正文+附带调用仍是有效回答，不误伤）
  3 工具已声明                  → 200（差集为空，不是缺陷）
  4 未采集工具名单              → 200（不因"采集不到"误报）
  5 普通无工具回答              → 200（本判定不碰正常流）
  6 /admin/api/deg 出现 upstream_unregistered_tools
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT = 8871
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_urt")

FAIL = []
APP = []


def chk(n, cond, extra=""):
    line = "%s %s %s" % ("PASS" if cond else "FAIL", n, extra)
    APP.append(line)
    sys.stdout.flush()
    print(line)
    if not cond:
        FAIL.append(n)


# 声明结果落盘，供主体读取（进程内共享内存）
RESULT = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        # 场景由**用户消息**里的标记决定: 网关会把 request.model 改写成上游真实 id
        # (日志实测 `remapped model 'mock/m-unregistered' -> 'm'`), 所以早期按 model
        # 后缀取场景永远失败 —— 上游收到的 model 已被替换。消息内容网关不动。
        mode = ""
        for m in reversed(body.get("messages") or []):
            c = m.get("content")
            c = c if isinstance(c, str) else ""
            if c.startswith("SCN:"):
                mode = c[4:].strip()
                break
        declared = sorted({t.get("function", {}).get("name") for t in (body.get("tools") or [])
                           if t.get("function", {}).get("name")})
        RESULT["declared"] = declared
        RESULT["requests"] = RESULT.get("requests", 0) + 1
        RESULT.setdefault("declared_by_mode", {})
        RESULT["declared_by_mode"][mode] = declared
        # 场景：模型要调的工具名 + 是否给可见正文
        tools, content = {
            "unregistered": (["Bash"], ""),
            "withtext": (["Bash"], "TAIL-DONE"),
            "declared": (["Read"], ""),
            "notools": (["Bash"], ""),
            "normal": ([], "TAIL-DONE"),
        }.get(mode, ([], "TAIL-DONE"))
        deltas = []
        if content:
            deltas.append({"index": 0, "delta": {"content": content}, "finish_reason": None})
        for i, name in enumerate(tools):
            deltas.append({
                "index": 0,
                "delta": {"tool_calls": [{"index": i, "id": "call_%d" % i,
                                          "type": "function",
                                          "function": {"name": name, "arguments": "{}"}}]},
                "finish_reason": "tool_calls",
            })
        if not tools:
            deltas.append({"index": 0, "delta": {}, "finish_reason": "stop"})
        # 与 tools/e2e_finish_norm.py 的 mock 同形: 不设 Content-Length, 逐帧写.
        # (实测: 设 Content-Length 的那版让网关把响应读成 null —— 保持已验证可用的形态.)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for d in deltas:
            self.wfile.write(
                ("data: " + json.dumps({
                    "id": "chatcmpl-" + uuid.uuid4().hex[:8],
                    "object": "chat.completion.chunk", "model": "m",
                    "choices": [d],
                }) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.startswith("/admin/api/degradations"):
            deg = {"ok": 1}
            p = os.path.join(ISO, "data", "degradations.json")
            if os.path.exists(p):
                try:
                    deg = json.load(io.open(p, encoding="utf-8"))
                except Exception:
                    pass
            data = json.dumps(deg).encode("utf-8")
        else:
            data = b'{"error":"nf"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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


def post(admin, path, obj=None):
    import urllib.request
    data = json.dumps(obj or {}).encode("utf-8")
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (admin, path), data=data,
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, resp.read()


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    prov = {"providers": [{
        "name": "mock",
        "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PORT,
        "api_key_env": "MOCK_KEY",
        "api_key_default": "sk-test",
        "models": {("mock/m-" + s): {"upstream_model": "m"}
                   for s in ("unregistered", "withtext", "declared", "notools", "normal")},
    }]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)

    env = dict(os.environ, PORT=str(PORT + 10),
               AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG=os.environ.get("RUST_LOG", "info"),
               AIGATE_NO_PROXY="1")

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ADMIN = PORT + 10
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=open(os.path.join(ISO, "out.log"), "wb"),
                            stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % (PORT + 10),
                                            timeout=2) as r:
                    r.read()
                break
            except Exception:
                time.sleep(0.5)
        if not wait_port(PORT + 10):
            print(io.open(os.path.join(ISO, "out.log"), encoding="utf-8", errors="replace").read()[-3000:])
            return 2

        # 先用真实的一轮对话确认模型路由通了，再谈判定

        def send(mode, tools=True, stream=True):
            body = {"model": "mock/m-" + mode,
                    "messages": [{"role": "user", "content": "SCN:" + mode}],
                    "stream": stream}
            if tools:
                body["tools"] = [{"type": "function", "function": {
                    "name": "Read", "parameters": {"type": "object"}}}]
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/v1/chat/completions" % ADMIN,
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST"), timeout=60) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        # 捕获最近一次客户端响应体，供末尾诊断
        def send2(mode, tools=True, stream=True):
            st, txt = send(mode, tools, stream)
            RESULT["last_body"] = txt
            return st, txt

        import urllib.request

        # ① 未声明的工具 + 无正文 → 网关**如实记账为 502** 且在错误文案里点名该工具。
        # 注意客户端侧仍是 200：透传流一旦开始就无法收回已发出的字节，所以判据是
        # **记录**（status/error），不是客户端状态码 —— 这正是本次修复要改的那个「假成功」。
        st, txt = send2("unregistered")
        if st == 404:
            print("网关侧 404：模型未注册。out.log tail:")
            print(io.open(os.path.join(ISO, "out.log"), encoding="utf-8", errors="replace").read()[-4000:])
            return 3
        rec = read_last_record("mock/m-unregistered")
        print("--- ① record ---", json.dumps(rec, ensure_ascii=False)[:400])
        chk("unregistered-no-text-records-502", rec.get("status") == 502,
            "status=%s" % rec.get("status"))
        chk("unregistered-error-names-tool",
            "Bash" in str(rec.get("error") or "") and "未声明" in str(rec.get("error") or ""),
            str(rec.get("error"))[:200])

        # ② 未声明的工具 + 有正文 → 200（不误伤有效回答）
        st, txt = send2("withtext")
        print("--- ② body (有正文) ---")
        print(txt[:600])
        chk("unregistered-with-text-is-200", st == 200, "status=%d" % st)
        chk("unregistered-with-text-has-body", "TAIL-DONE" in txt, txt[:120])

        # ③ 工具已声明 → 200
        st, txt = send2("declared")
        chk("declared-tool-is-200", st == 200, "status=%d" % st)

        # ④ 未采集名单（请求无 tools）→ 200
        st, txt = send2("notools", tools=False)
        chk("no-tools-collected-is-200", st == 200, "status=%d" % st)

        # ⑤ 普通无工具回答 → 200
        st, txt = send2("normal")
        chk("normal-answer-is-200", st == 200, "status=%d" % st)
        chk("normal-answer-has-done", "[DONE]" in txt, txt[:80])

        # 先验证流是否真能透传（非流式）
        st, txt = send2("normal", tools=False, stream=False)
        print("--- 非流式 status=%d body=%r ---" % (st, txt[:300]))
        # ⑥ 降级项出现在 /admin/api/deg（带 admin token，ISO 实例启动时生成在 data/.admin_token）
        tok = ""
        tp = os.path.join(ISO, "data", ".admin_token")
        if os.path.exists(tp):
            tok = io.open(tp, encoding="utf-8", errors="replace").read().strip()
        st, raw = get(ADMIN, "/admin/api/degradations", tok)
        print("--- ⑥ deg status=%d raw ---" % st, raw[:300])
        try:
            d = json.loads(raw)
        except Exception:
            d = {"_raw": raw[:200].decode("utf-8", "replace")}
        chk("degradation-recorded",
            "upstream_unregistered_tools" in json.dumps(d, ensure_ascii=False),
            json.dumps(d, ensure_ascii=False)[:200])

        # ⑦ 反向确认上游真的收到我们发的请求（证明 §① 不是网关自己造的）
        chk("upstream-saw-all-5", RESULT.get("requests", 0) >= 5,
            "requests=%s" % RESULT.get("requests"))
        # ⑧ 上游收到的工具名单与客户端发的完全一致（证明网关没改客户端请求）
        chk("upstream-saw-client-tools",
            RESULT.get("declared_by_mode", {}).get("unregistered") == ["Read"],
            "by_mode=%s" % RESULT.get("declared_by_mode"))

        # ⑨ 对照组: 其余四个场景必须仍记账为 200（证明判定没有误伤）
        for mode in ("withtext", "declared", "notools", "normal"):
            r = read_last_record("mock/m-" + mode)
            chk("control-%s-stays-200" % mode, r.get("status") == 200,
                "status=%s error=%s" % (r.get("status"), r.get("error")))

        print("\n--- ① 原始响应体全文（用于核对流是否真被透传）---")
        print(txt1 if (txt1 := RESULT.get("last_body")) else "(未捕获)")
        print("\n".join(APP))
        print("---- TOTAL: %d 项, 失败 %d 项" % (len(APP), len(FAIL)))
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



def read_last_record(want_model=""):
    """读 ISO 实例 data/logs.jsonl 的最后一条记录（网关的记账口径）。

    记录在流关闭时才落盘，故轮询等待（最多 5s）—— 首次实现没等，
    读到空文件下的 {}，把「已生效的修复」误报成 FAIL。
    """
    p = os.path.join(ISO, "data", "logs.jsonl")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            lines = io.open(p, encoding="utf-8", errors="replace").read().strip().split("\n")
        except Exception:
            lines = []
        for ln in reversed([x for x in lines if x.strip()]):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if not want_model or rec.get("model") == want_model:
                return rec
        time.sleep(0.2)
    return {}


def get(admin, path, token=""):
    hdrs = {"Authorization": "Bearer " + token} if token else {}
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (admin, path), headers=hdrs,
                                method="GET")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


if __name__ == "__main__":
    sys.exit(main())
