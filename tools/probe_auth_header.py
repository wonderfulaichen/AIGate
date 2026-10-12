# -*- coding: utf-8 -*-
"""探针: 声明式鉴权头 (auth.header / auth.prefix) —— 一处声明, 全路径生效。

背景 (2026-10-12, 用户问「难道每次遇到新供应商都要硬编码适配鉴权」):
网关原先把 `Authorization: Bearer <key>` 写死在**七个**发 key 的点上。于是鉴权头
不叫 Authorization / 不用 Bearer 前缀的供应商 (Anthropic x-api-key、Azure api-key)
只能把密钥硬编码进 `headers` 静态表 —— 那会把密钥写进配置文件。

判据 (四组, mock 上游**实收头**为唯一证据):
  ① 默认 (无 auth 段)          → 上游实收 `Authorization: Bearer KEY`   —— 不改存量行为
  ② header=x-api-key, prefix="" → 上游实收 `x-api-key: KEY` (裸值), 且**没有** Authorization
  ③ header=Authorization, prefix="Token " → 上游实收 `Authorization: Token KEY`
  ④ 拉取模型同样跟随声明 (证明不是只改了转发这一处)
  ⑤ 空 key 不挂鉴权头 (Zen 的 public 形态) —— 不得凭空多一个空 Bearer
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
MP = 8876   # mock 上游
PG = 8877   # 网关
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_authhdr")

FAIL = []
SEEN = []   # 每个上游请求收到的关键头


def chk(n, cond, extra=""):
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _record(self):
        SEEN.append({
            "path": self.path,
            "authz": self.headers.get("Authorization"),
            "xapikey": self.headers.get("x-api-key"),
            "apikey": self.headers.get("api-key"),
            "has_authz": self.headers.get("Authorization") is not None,
        })

    def do_GET(self):
        # 拉取模型: /v1/models
        self._record()
        body = json.dumps({"object": "list", "data": [{"id": "up-m1"}, {"id": "up-m2"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._record()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps({
            "id": "cc-" + uuid.uuid4().hex[:6], "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


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


def chat(model):
    body = json.dumps({"model": model, "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_models(name):
    req = urllib.request.Request("http://127.0.0.1:%d/admin/api/providers/%s/fetch-models"
                                 % (PG, name), data=b"{}",
                                 headers={"content-type": "application/json",
                                          **admin_auth(os.path.join(ISO, "data"))}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


from adminauth import headers as admin_auth  # noqa: E402  (放这儿是因为 ISO 在上面才定)

PROVIDERS = {
    "providers": [
        # ① 默认形态 (存量配置全是这样): 无 auth 段
        {"name": "defp", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "K_DEF", "api_key_default": "KEYDEF",
         "models": {"defp/m": {"upstream_model": "m"}}},
        # ② Anthropic 官方形态: x-api-key, 无前缀
        {"name": "xkp", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "K_X", "api_key_default": "KEYX",
         "auth": {"mode": "api_key", "header": "x-api-key", "prefix": ""},
         "models": {"xkp/m": {"upstream_model": "m"}}},
        # ③ 自定义前缀
        {"name": "tokp", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "K_T", "api_key_default": "KEYT",
         "auth": {"mode": "api_key", "header": "Authorization", "prefix": "Token "},
         "models": {"tokp/m": {"upstream_model": "m"}}},
        # ⑤ 空 key (Zen 的 public 形态): 两处都不给 key
        {"name": "nokey", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "K_NONE_ABSENT", "api_key_default": "",
         "models": {"nokey/m": {"upstream_model": "m"}}},
    ]
}


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(PROVIDERS, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    for k in ("K_DEF", "K_X", "K_T", "K_NONE_ABSENT"):
        env.pop(k, None)
    srv = ThreadingHTTPServer(("127.0.0.1", MP), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2

        # ① 默认: Authorization: Bearer
        SEEN.clear()
        chat("defp/m")
        time.sleep(0.4)
        h = SEEN[-1] if SEEN else {}
        chk("default-sends-bearer", h.get("authz") == "Bearer KEYDEF", "authz=%r" % h.get("authz"))
        chk("default-no-extra-header", h.get("xapikey") is None,
            "x-api-key=%r" % h.get("xapikey"))

        # ② x-api-key 裸值, 且不发 Authorization
        SEEN.clear()
        chat("xkp/m")
        time.sleep(0.4)
        h = SEEN[-1] if SEEN else {}
        chk("xapikey-sends-raw-key", h.get("xapikey") == "KEYX", "x-api-key=%r" % h.get("xapikey"))
        chk("xapikey-omits-authorization", not h.get("has_authz"),
            "Authorization=%r" % h.get("authz"))

        # ③ 自定义前缀
        SEEN.clear()
        chat("tokp/m")
        time.sleep(0.4)
        h = SEEN[-1] if SEEN else {}
        chk("custom-prefix", h.get("authz") == "Token KEYT", "authz=%r" % h.get("authz"))

        # ④ 拉取模型跟随声明 (不是只改了转发)
        SEEN.clear()
        fetch_models("xkp")
        time.sleep(0.4)
        h = SEEN[-1] if SEEN else {}
        chk("fetch-models-follows-auth", h.get("xapikey") == "KEYX" and not h.get("has_authz"),
            "path=%s x-api-key=%r authz=%r" % (h.get("path"), h.get("xapikey"), h.get("authz")))

        # ⑤ 空 key 不挂鉴权头
        SEEN.clear()
        chat("nokey/m")
        time.sleep(0.4)
        h = SEEN[-1] if SEEN else {}
        chk("empty-key-sends-no-auth", not h.get("has_authz") and h.get("xapikey") is None,
            "authz=%r x-api-key=%r" % (h.get("authz"), h.get("xapikey")))

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (len(FAIL) + 8, len(FAIL)))
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
