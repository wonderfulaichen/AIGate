# -*- coding: utf-8 -*-
"""请求来源 / 关联键 / 逐次尝试 的端到端验证.

背景 (2026-10-21): 用户报「我明明没调用这个供应商的模型，结果却突然显示有调用」。
当时面板只能回答"某供应商有 N 条记录"，回答不了「是谁发的、是不是同一次请求的重试、
能不能拿去和上游对账」。本轮补了三样东西，本探针逐一验证它们**真的落进了日志**：

  ① client_name  —— 调用来源名 (显式头 > UA 特征 > UA 首段 > 未采集)
  ② trace_id     —— 本地关联键; 客户端带合法 X-Request-Id 就沿用, 否则自生成;
                    非法值 (含空格/CRLF/非 ASCII) 必须丢弃并重新生成 (防头注入)
  ③ attempts     —— 逐次上游尝试明细; 一次成功必须为空 (不占字节), 重试过才有值
  ④ upstream_request_id —— 上游自己给的请求号 (从响应头取)

判据方向 (纪律): 一律断言"日志里读得到的事实", 不断言"代码里写了什么"。

红线: 隔离实例 + 随机端口 + 本地 mock 上游; 绝不碰用户 8787。
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

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
_fails = []


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


# mock 上游的可编程行为: 前 N 次返回 500, 之后返回 200.
STATE = {"fail_times": 0, "seen": 0}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # 上游自己的请求号: 用来验证 upstream_request_id 采集.
        self.send_header("x-request-id", "up-abc-123")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        STATE["seen"] += 1
        if STATE["seen"] <= STATE["fail_times"]:
            self._json(500, {"error": {"message": "mock transient failure"}})
            return
        self._json(200, {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                      "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 11},
        })


def wait_ready(base, tries=60):
    for _ in range(tries):
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def post(base, path, body, headers=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1

    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{mport}/v1/chat/completions"

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_origin")
    # 每次运行必须**清空**隔离目录再起新实例。
    # 不清会读到上一轮运行的 logs.jsonl —— 探针按 trace_id/client_name 查找时会命中
    # 旧记录, 于是"改坏了实现"照样全绿 (本轮实际踩到: 日志 12 条 = 两次运行叠加)。
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov", "endpoint": upstream,
        "api_key_env": "MOCK_KEY", "api_key_default": "dummy",
        "models": {"m1": {"upstream_model": "m"}},
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
        if not wait_ready(base):
            print(f"{BAD}  隔离实例未就绪")
            return 1

        body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}],
                "stream": False}

        # ── 用例 1: 显式声明来源 + 合法 X-Request-Id, 一次成功 ──
        STATE["seen"] = 0
        STATE["fail_times"] = 0
        st, _, _ = post(base, "/v1/chat/completions", body,
                        {"x-aigate-client": "MyAgent", "x-request-id": "trace-good-1"})

        # ── 用例 2: 非法 X-Request-Id (含空格, 头注入形状) 必须被丢弃 ──
        st2, _, _ = post(base, "/v1/chat/completions", body,
                         {"user-agent": "claude-code/1.0", "x-request-id": "bad value here"})

        # ── 用例 3: 通过 UA 识别来源 + 触发一次重试 (上游先 500) ──
        STATE["seen"] = 0
        STATE["fail_times"] = 1
        st3, _, _ = post(base, "/v1/chat/completions", body,
                         {"user-agent": "Cursor/0.42"})

        time.sleep(1.2)  # 等异步落盘

        # ── 读日志 (磁盘是权威, 不是内存接口) ──
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
        print(f"\n  日志条数: {len(rows)}  (三次请求状态码: {st}, {st2}, {st3})")
        check(len(rows) >= 3, "三次请求都已落盘", f"实得 {len(rows)}")

        def find(pred):
            return next((r for r in rows if pred(r)), None)

        r1 = find(lambda r: r.get("trace_id") == "trace-good-1")
        # ① 显式来源 + 客户端关联键透传
        check(r1 is not None, "① 客户端的合法 X-Request-Id 被沿用为 trace_id")
        if r1:
            check(r1.get("client_name") == "MyAgent",
                  "① 显式头 x-aigate-client 优先于 UA", f"实得 {r1.get('client_name')!r}")
            check(r1.get("upstream_request_id") == "up-abc-123",
                  "④ 采到了上游自己的请求号", f"实得 {r1.get('upstream_request_id')!r}")
            check(len(r1.get("attempts") or []) == 1,
                  "③ 一次成功留下 1 条明细 (= 真实打过上游 1 次)",
                  f"实得 {r1.get('attempts')!r}")

        # ② 非法关联键必须被丢弃并重新生成
        bad = [r for r in rows if "bad value" in str(r.get("trace_id") or "")]
        check(not bad, "② 非法 X-Request-Id (含空格) 未被原样落盘", f"命中 {len(bad)} 条")
        r2 = find(lambda r: r.get("client_name") == "Claude Code")
        check(r2 is not None, "② UA 特征识别出来源 = Claude Code")
        if r2:
            tid = r2.get("trace_id") or ""
            check(tid.startswith("req_"), "② 非法关联键被替换为网关自生成值",
                  f"实得 {tid!r}")

        # ③ 重试明细: 1 次失败 + 1 次成功 = 2 条, 且顺序与结果正确
        r3 = find(lambda r: r.get("client_name") == "Cursor")
        check(r3 is not None, "③ UA 特征识别出来源 = Cursor")
        if r3:
            atts = r3.get("attempts") or []
            check(len(atts) == 2, "③ 重试过的请求留下 2 条尝试明细", f"实得 {atts}")
            if len(atts) == 2:
                check(atts[0].get("status") == 500 and atts[0].get("outcome") == "http_error",
                      "③ 第 1 次记为 500/http_error", f"实得 {atts[0]}")
                check(atts[1].get("status") == 200 and atts[1].get("outcome") == "ok",
                      "③ 第 2 次记为 200/ok (最终这次也入明细)", f"实得 {atts[1]}")
                check([a.get("seq") for a in atts] == [1, 2],
                      "③ 尝试序号从 1 递增", f"实得 {[a.get('seq') for a in atts]}")
                check(all(isinstance(a.get("ms"), int) for a in atts),
                      "③ 每次尝试都有耗时 (ms)", f"实得 {atts}")

        # ⑤ 体积纪律: 一次成功的记录不应因新字段显著变大
        if r1:
            size = len(json.dumps(r1, ensure_ascii=False))
            print(f"  [i] 单条成功记录 JSON 体积: {size} B")
            check(size < 2000, "⑤ 单条成功记录体积仍很小 (<2000B)", f"实得 {size}B")
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
