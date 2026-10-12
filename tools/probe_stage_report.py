# -*- coding: utf-8 -*-
"""探针: 「测试」要能说清**哪个环节**坏了 + 错误归因不误导（2026-10-12 用户要求）。

用户原话：「我建议我们软件测试和报错应该显示是哪个环节出问题了。」
背景事故：上游被 Cloudflare 拦返回 `403 error code: 1010`，旧实现
① 拿假密钥 `Bearer probe` 测（测不出密钥对不对）；
② 一个笼统的"测试失败"分不清环节；
③ 按状态码把 1010 翻成「权限不足（无权访问该模型或功能）」→ 排查方向被带偏。

判据（mock 上游按请求头/路径模拟各环节故障，逐个核对**结论的环节归因**）：
  ① 密钥错 (上游 401)          → stage=upstream, key_checked=true（不再被说成"可达就完事"）
  ② 无密钥 (面板/环境都空)     → stage=key, 且文案说明"请求还没发出去"
  ③ 连不上 (端口没人听)        → stage=connect
  ④ 边缘拦截 (403 + "error code: 1010") → stage=edge（**不得**报成模型权限）
  ⑤ 应用层 403 (真的余额/权限)  → stage=upstream 且**不得**误报边缘拦截
  ⑥ 限流 429                  → stage=upstream 且 reachable=true（限流证明上游在正常工作）
  ⑦ 正常 200                  → stage=upstream, reachable=true, key_checked=true
  ⑧ 转发路径的错误文案也带环节归因（同一个 1010 经 /v1/chat/completions 时要说明是边缘拦的）
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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8878   # mock 上游
PG = 8879   # 网关
DEAD = 8880  # 故意没人监听的端口 (连不上)
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_stage")

FAIL = []
TRACE = []   # 上游收到的 /models 请求的 authorization
SCEN = {"mode": "ok"}   # 当前模拟的场景


def chk(n, cond, extra=""):
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


def make_handler(trace, scen):
    """构造 mock: /v1/models 按 scen 返回不同形态; /v1/chat/completions 用于 ⑧。"""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            trace.append((self.path, self.headers.get("Authorization")))
            m = scen["mode"]
            if m == "ok":
                self._send(200, json.dumps({"object": "list", "data": [{"id": "m1"}]}))
            elif m == "badkey":
                self._send(401, json.dumps({"error": {"message": "Invalid API key", "type": "authentication_error"}}))
            elif m == "edge":
                # 实测原文形态: 纯文本 + Cloudflare 码
                self._send(403, "error code: 1010\n", "text/plain")
            elif m == "edge_html":
                self._send(403, "<html><head><title>Attention Required! | Cloudflare</title></head></html>", "text/html")
            elif m == "app403":
                self._send(403, json.dumps({"code": "INSUFFICIENT_BALANCE", "message": "Insufficient account balance"}))
            elif m == "rate":
                self._send(429, json.dumps({"error": {"message": "Rate limit exceeded, retry in 30s"}}))
            elif m == "srv":
                self._send(502, "bad gateway")
            else:
                self._send(200, "{}")

        def do_POST(self):
            trace.append((self.path, self.headers.get("Authorization")))
            self._send(403, "error code: 1010\n", "text/plain")

    return H


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


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    prov = {"providers": [
        # 密钥来自面板 (keys.json 由本探针写入)
        {"name": "withkey", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "ABSENT_KEY_1", "models": {"withkey/m": {"upstream_model": "m"}}},
        # 完全没有密钥 (env 不存在 + 无默认值 + 面板无值)
        {"name": "nokey", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "ABSENT_KEY_2", "models": {"nokey/m": {"upstream_model": "m"}}},
        # 指向没人监听的端口 → 连接失败
        {"name": "deadport", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % DEAD,
         "api_key_env": "ABSENT_KEY_3", "api_key_default": "k", "models": {"deadport/m": {"upstream_model": "m"}}},
    ]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)
    with io.open(os.path.join(ISO, "data", "keys.json"), "w", encoding="utf-8") as f:
        json.dump({"withkey": "REALKEY-1"}, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    for k in ("ABSENT_KEY_1", "ABSENT_KEY_2", "ABSENT_KEY_3"):
        env.pop(k, None)
    trace = TRACE
    srv = ThreadingHTTPServer(("127.0.0.1", MP), make_handler(trace, SCEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as admin_auth

        def test_provider(name, endpoint):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/admin/api/providers/test" % PG,
                data=json.dumps({"endpoint": endpoint, "name": name}).encode(),
                headers={"content-type": "application/json",
                         **admin_auth(os.path.join(ISO, "data"))}, method="POST")
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))

        EP = "http://127.0.0.1:%d/v1/chat/completions" % MP
        EP_DEAD = "http://127.0.0.1:%d/v1/chat/completions" % DEAD

        # ① 密钥错 → 上游环节, 且**确实用了真实密钥**(上游应收到 REALKEY-1)
        trace.clear()
        SCEN["mode"] = "badkey"
        r = test_provider("withkey", EP)
        chk("badkey-stage-upstream", r.get("stage") == "upstream",
            "stage=%s status=%s" % (r.get("stage"), str(r.get("status"))[:80]))
        chk("badkey-uses-real-key", r.get("key_checked") is True and
            any(a == "Bearer REALKEY-1" for _, a in trace),
            "key_checked=%s trace=%s" % (r.get("key_checked"), trace[:2]))
        chk("badkey-status-mentions-401", "401" in str(r.get("status")), str(r.get("status"))[:80])

        # ② 无密钥 → 凭据环节, 且说明"请求还没发出去"
        trace.clear()
        r = test_provider("nokey", EP)
        chk("nokey-stage-key", r.get("stage") == "key",
            "stage=%s status=%s" % (r.get("stage"), str(r.get("status"))[:100]))
        chk("nokey-says-not-sent", "还没发出去" in str(r.get("status")),
            str(r.get("status"))[:120])
        chk("nokey-no-upstream-call", not trace, "trace=%s" % (trace[:2],))

        # ③ 连不上 → 连接环节
        SCEN["mode"] = "ok"
        r = test_provider("deadport", EP_DEAD)
        chk("deadport-stage-connect", r.get("stage") == "connect",
            "stage=%s status=%s" % (r.get("stage"), str(r.get("status"))[:80]))
        chk("deadport-not-reachable", r.get("reachable") is False, str(r.get("reachable")))

        # ④ 边缘拦截 (实测原文形态) → 边缘环节, 且**不得**说成模型权限
        trace.clear()
        SCEN["mode"] = "edge"
        r = test_provider("withkey", EP)
        chk("edge-stage", r.get("stage") == "edge",
            "stage=%s status=%s" % (r.get("stage"), str(r.get("status"))[:120]))
        s = str(r.get("status"))
        chk("edge-name-1010", "1010" in s, s[:120])
        chk("edge-says-not-permission", "不是密钥或模型权限问题" in s, s[:160])

        # ④b HTML 挑战页也归边缘环节
        SCEN["mode"] = "edge_html"
        r = test_provider("withkey", EP)
        chk("edge-html-stage", r.get("stage") == "edge", str(r.get("status"))[:100])

        # ⑤ 应用层 403 (真余额/权限) → 上游环节, 且**不误报**边缘
        SCEN["mode"] = "app403"
        r = test_provider("withkey", EP)
        chk("app403-stage-upstream", r.get("stage") == "upstream",
            "stage=%s status=%s" % (r.get("stage"), str(r.get("status"))[:100]))
        chk("app403-not-misread-as-edge", "边缘防护" not in str(r.get("status")),
            str(r.get("status"))[:100])

        # ⑥ 429 → 上游环节 + 可达 (限流证明上游在正常工作)
        SCEN["mode"] = "rate"
        r = test_provider("withkey", EP)
        chk("rate-stage-upstream-reachable",
            r.get("stage") == "upstream" and r.get("reachable") is True,
            "stage=%s reachable=%s" % (r.get("stage"), r.get("reachable")))

        # ⑦ 正常 → 上游环节 + 可达 + 真钥匙
        trace.clear()
        SCEN["mode"] = "ok"
        r = test_provider("withkey", EP)
        chk("ok-stage-upstream", r.get("stage") == "upstream" and r.get("reachable") is True,
            "stage=%s reachable=%s status=%s" % (r.get("stage"), r.get("reachable"), r.get("status")))
        chk("ok-key-checked", r.get("key_checked") is True, str(r.get("key_checked")))

        # ⑧ 转发路径的错误文案也带环节归因 (同一 403/1010 经 chat 入口)
        SCEN["mode"] = "edge"
        body = json.dumps({"model": "withkey/m", "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                    headers={"content-type": "application/json"}, method="POST"), timeout=40) as rr:
                txt = rr.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
        chk("forward-error-names-edge", "边缘防护" in txt and "1010" in txt, txt[:200])

        # ⑨ 后端 stage 字段与 i18n 标签都在 (面板据此显示环境前缀)
        SCEN["mode"] = "ok"
        r = test_provider("withkey", EP)
        chk("stage-text-present", bool(r.get("stage_text")), str(r.get("stage_text")))

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (len(FAIL) + 13, len(FAIL)))
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
