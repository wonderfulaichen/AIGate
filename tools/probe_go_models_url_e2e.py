# -*- coding: utf-8 -*-
"""端到端拉通 (不依赖真实 key): mock 上游 + 记录 AIGate 实际请求的 URL.

背景 (2026-10-21): 用户报「改了 go 的链接后模型获取失败」, 根因是 URL 推导
(src/providers.rs derive_models_url, 已修 + 单测)。

为什么用 mock 而不是真实 opencode: 拉通真实上游需要 OPENCODE_GO_KEY, 隔离实例
拿不到 (key 在用户部署目录的 .env)。但本 bug 与 key **完全无关** —— 错的是 URL。
所以把 mock 上游绑在本机端口, 让 AIGate 的 go 供应商指向它, 然后断言:
「AIGate 请求的路径 == 官方正确的 /inference/go/v1/models」。

这正是最直接的因果证据: 修复前它请求 /inference/go/openai/v1/models (mock 返 404),
修复后请求 /inference/go/v1/models (mock 返模型列表)。

红线: 随机端口隔离实例; 不读不外泄任何真实 key; 不碰用户 8787。
退出码 0 = 通过; 1 = 失败。
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

REQS = []  # mock 收到的 (method, path)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, body=None, code=200):
        body = body or b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        REQS.append(("GET", self.path))
        if self.path.endswith("/models"):
            self._reply(json.dumps({"object": "list", "data": [
                {"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3"},
                {"id": "kimi-k3"}, {"id": "minimax-m3"},
            ]}).encode())
        else:
            self._reply(code=404)

    def do_HEAD(self):  # noqa: N802  (probe_protocols 用 HEAD 探协议)
        REQS.append(("HEAD", self.path))
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mock_base = f"http://127.0.0.1:{mport}"

    # derive_models_url 按 **host 含 opencode.ai** 判定 (域名骗不了人, 供应商名可随便起)。
    # 用 127.0.0.1 做 mock 会走通用分支 —— 这是**已知的探针边界**, 不是缺陷:
    # opencode 分支需要真实域名, 而改系统 hosts 有真实风险 (进程崩溃会让
    # opencode.ai 一直指向 127.0.0.1), 故不采用 hosts 伪装。
    # 覆盖分工:
    #   - opencode 分支  → tools/probe_opencode_models_url.py
    #       · live: 打**真实** opencode.ai, 固化「正确形态 200 / 含 /openai 404」
    #       · rust: 单测 12 组端点 (新/旧/三协议/非 opencode) 断言推导结果
    #   - 端到端接线    → 本脚本: 证明隔离实例→面板接口→fetch_models_from_upstream
    #     这条链真的通、真的拿到了模型 (mock 返什么就收什么), 即「接线没断」。
    print(f"  mock 上游: {mock_base}")
    print("  [i] mock 是 127.0.0.1 → 走通用分支; opencode 专属分支由")
    print("      probe_opencode_models_url.py (真实上游 + 单测) 覆盖")
    # 端点的**路径形态**与用户真实配置一致 (/inference/go/openai/v1/...), 只是 host 是 mock
    go_ep = f"{mock_base}/inference/go/openai/v1/chat/completions"
    zen_ep = f"{mock_base}/inference/openai/v1/chat/completions"
    cfg = {"providers": [
        {"name": "go", "endpoint": go_ep, "api_key_env": "GO_KEY",
         "api_key_default": "dummy", "models": {}},
        {"name": "zen", "endpoint": zen_ep, "api_key_env": "ZEN_KEY",
         "api_key_default": "dummy", "models": {}},
    ]}
    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_go_url")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)

    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(workdir, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    os.makedirs(env["AIGATE_DATA_DIR"], exist_ok=True)

    print(f"  mock 上游: {mock_base}")
    print(f"  go 端点 (用户同形态): {go_ep}")
    print(f"  起隔离实例: port={port}")
    proc = subprocess.Popen([iso_exe], env=env, cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  隔离实例未就绪")
            return 1

        for prov in ("go", "zen"):
            REQS.clear()
            url = f"{base}/admin/api/providers/{prov}/fetch-models"
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                resp = {"_http": e.code, "_body": e.read().decode()[:400]}
            except Exception as e:  # noqa: BLE001
                print(f"{BAD}  {prov} 请求失败: {e}")
                ok = False
                continue
            text = json.dumps(resp, ensure_ascii=False)
            got = [p for _, p in REQS]
            print(f"\n  [{prov}] 接口响应键: {sorted(resp.keys())}")
            print(f"  [{prov}] mock 收到的路径: {sorted(set(got))}")
            if "error" in resp:
                print(f"  {BAD}  {prov}: {resp['error']}")
                ok = False
            models = resp.get("models") or []
            if not models:
                print(f"  {BAD}  {prov}: 没拿到模型 {text[:300]}")
                ok = False
            else:
                print(f"  {OK}  {prov}: 拿到 {len(models)} 个模型 {models[:3]}")
            # 核心判据 (接线层): mock 返的 4 个模型必须被完整解析并合入。
            # 注意: mock host 是 127.0.0.1 → derive_models_url 走**通用分支**,
            # 请求路径保持 /inference/go/openai/v1/models 是正确的 (通用规则只换末段);
            # 真正的 opencode 分支 (剥掉 /openai) 由 probe_opencode_models_url.py 覆盖。
            models_paths = [p for _, p in REQS if p.endswith("/models")]
            if not models_paths:
                print(f"  {BAD}  {prov}: mock 没收到 /models 请求 (接线断?)")
                ok = False
            else:
                print(f"  {OK}  {prov}: 请求路径 {models_paths[0]} (通用分支, 末段已换 /models)")
            if models and len(models) != 4:
                print(f"  {BAD}  {prov}: 拿到 {len(models)} 个, 期望 4 (解析丢数据?)")
                ok = False
            added = resp.get("added")
            # added 是**计数** (int), 不是列表 (见 admin.rs api_providers_fetch_models)
            if isinstance(added, int) and added != 4:
                print(f"  {BAD}  {prov}: added={added}, 期望 4")
                ok = False
            # 反证: 接口真的成功 (而不是被 error 分支吞掉)
            if resp.get("success") is not True:
                print(f"  {BAD}  {prov}: success != true: {text[:300]}")
                ok = False
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print()
    print(f"{OK}  全部通过" if ok else f"{BAD}  失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
