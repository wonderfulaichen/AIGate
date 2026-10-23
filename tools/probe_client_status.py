# -*- coding: utf-8 -*-
"""「客户端实收状态 vs 语义状态」的端到端验证 (流式提交边界).

背景 (2026-10-24): 第 ⑦ 项「流式提交边界」。
先取证: 真实 5000 条日志里**假 200 案例为 0** —— 这个风险在实操中没发生过。
但机制上确实存在: 流式请求的 HTTP 头在收到上游首字节时就已发出, 此后流内报错
**无法回退修改 HTTP 状态**, 只能以 SSE error 事件呈现。

于是留下的真缺口是「日志说 502、客户端说 200」对不上账。
本探针验证这个缺口已补:
  ① mock 上游回 200 + 流内 error 事件;
  ② 客户端**确实收到 HTTP 200** (这是事实, 无法改变);
  ③ 日志 `status` 记 502 (语义结论, 供统计告警);
  ④ 日志 `client_status` 记 200 (客户端真实感知);
  ⑤ 两者不同 → `status_mismatch` 成立, 面板能解释"为什么对不上"。

另加对照臂:
  ⑥ 正常流 (200 + 正常内容) **不该**出现 client_status (不占字节、不产生噪声)。

红线: 隔离实例 + 随机端口 + mock 上游; 绝不碰用户 8787。
"""
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
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
_fails = []

# 行为开关: 决定 mock 回"流内错误"还是"正常流"
STATE = {"mode": "error"}


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if STATE["mode"] == "error":
            # 关键: HTTP 200 已发出, 但流内是错误事件 (上游的"假 200")
            payload = json.dumps({
                "error": {"message": "upstream exploded mid-stream", "type": "server_error"}
            })
            self.wfile.write(f"data: {payload}\n\n".encode())
        else:
            chunk = json.dumps({
                "id": "x", "object": "chat.completion.chunk", "model": "m",
                "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
            })
            self.wfile.write(f"data: {chunk}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_client_status")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mockprov/m1": {"upstream_model": "m1"}},
    }]}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = data_dir
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso_exe], env=env, cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  隔离实例未就绪")
            return 1

        def stream_once():
            body = json.dumps({"model": "mockprov/m1",
                               "messages": [{"role": "user", "content": "hi"}],
                               "stream": True}).encode()
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, r.read().decode(errors="replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(errors="replace")

        # ── 用例 A: 上游 200 + 流内错误 ──
        STATE["mode"] = "error"
        code, text = stream_once()
        print(f"\n  A) 客户端收到 HTTP {code}；流内容片段: {text[:120]!r}")
        check(code == 200, "② 客户端确实收到 HTTP 200 (事实, 无法回退)", f"{code}")
        time.sleep(1.5)

        # ── 用例 B: 正常流 (对照臂) ──
        STATE["mode"] = "ok"
        code2, _ = stream_once()
        check(code2 == 200, "对照臂: 正常流也是 200", f"{code2}")
        time.sleep(1.5)

        # 读日志
        lp = os.path.join(data_dir, "logs.jsonl")
        rows = []
        with open(lp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
        print(f"  日志条数: {len(rows)}")
        check(len(rows) >= 2, "两条请求都已落盘", f"{len(rows)}")

        # 找流内错误那条: status=502
        err_row = next((r for r in rows if r.get("status") == 502), None)
        ok_row = next((r for r in rows if r.get("status") == 200), None)

        # ③ 语义状态 502
        check(err_row is not None, "③ 流内错误 → 日志 status=502 (语义结论)")
        if err_row:
            print(f"  流内错误那条: status={err_row.get('status')} "
                  f"client_status={err_row.get('client_status')} "
                  f"end_reason={err_row.get('end_reason')}")
            check(err_row.get("end_reason") == "error_event",
                  "③ end_reason 记为 error_event", f"{err_row.get('end_reason')}")
            # ④ 客户端实收 200
            check(err_row.get("client_status") == 200,
                  "④ **核心**: client_status=200 (客户端真实感知已记录)",
                  f"{err_row.get('client_status')}")
            # ⑤ 两者不同 → 面板可解释
            check(err_row.get("status") != err_row.get("client_status"),
                  "⑤ 两者不同 → 前端 statusMismatchText 会显示说明")

        # ⑥ 对照臂: 正常流不该有 client_status
        check(ok_row is not None, "对照臂: 有 status=200 的正常记录")
        if ok_row:
            check(ok_row.get("client_status") is None,
                  "⑥ **对照**: 正常流没有 client_status (不占字节、不产噪声)",
                  f"{ok_row.get('client_status')}")
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项判据未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
