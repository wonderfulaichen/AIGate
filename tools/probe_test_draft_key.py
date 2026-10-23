# -*- coding: utf-8 -*-
"""「测试」必须测**当前草稿** —— 端到端验证.

背景 (2026-10-24): 面板的密钥编辑是"改完即存"的, 但端点/协议/模型表都停在表单草稿里。
旧实现 `api_providers_test` 只按**已保存的 provider** 找凭据, 于是用户改完端点再点
「测试」时, "我眼前改的东西"与"实际被测的东西"不是同一份 —— 测试通过、保存后却报错,
而界面毫无提示。

本探针用 mock 上游**按密钥分流应答**, 逐一验证:
  ① 不传 api_key  → 用已保存凭据测, key_source = "saved"
  ② 传草稿 api_key → 用它测, key_source = "draft"  (本轮核心)
  ③ 草稿密钥与已保存密钥**同时存在**时, 草稿优先 (旧实现会走已保存的)
  ④ 空串 api_key   → 语义是"没有草稿密钥", 回落已保存链 (不当作"草稿空密钥"去测)
  ⑤ 假密钥探活路径 (不传 name) → key_source = "none", key_checked = false

关键判据设计: mock 对不同密钥返回**不同状态码**, 于是"这次到底用了哪个密钥"
从响应内容就能读出来, 不需要窥探内部状态。

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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
_fails = []

SAVED_KEY = "saved-key-AAA"      # 已保存凭据 → mock 返 200
DRAFT_KEY = "draft-key-BBB"      # 草稿凭据   → mock 返 200
BOGUS_KEY = "bogus-key-ZZZ"      # 无效凭据   → mock 返 401


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

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization") or ""
        # 按密钥分流: 让"这次用了哪个密钥"从状态码就能读出来
        if BOGUS_KEY in auth:
            body = json.dumps({"error": {"message": "invalid api key"}}).encode()
            code = 401
        elif SAVED_KEY in auth or DRAFT_KEY in auth:
            body = json.dumps({"object": "list", "data": [{"id": "m1"}]}).encode()
            code = 200
        else:
            # 假密钥探活 (Bearer probe)
            body = json.dumps({"error": {"message": "missing key"}}).encode()
            code = 401
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


def post(base, payload):
    req = urllib.request.Request(base + "/admin/api/providers/test",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1

    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_draft_key")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 已保存凭据: 走 keys.json (面板值), 与 api_key_env 解耦, 避免环境变量干扰
    with open(os.path.join(data_dir, "keys.json"), "w", encoding="utf-8") as f:
        json.dump({"mockprov": SAVED_KEY}, f)

    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "MOCKPROV_KEY", "api_key_default": "dummy",
        "models": {"m1": {"upstream_model": "m1"}},
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

        # ① 不传 api_key → 用已保存凭据 (200 = SAVED_KEY 被接受)
        st, r1 = post(base, {"endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
                             "name": "mockprov"})
        print(f"\n  ① 不传草稿密钥: stage={r1.get('stage')} key_source={r1.get('key_source')} "
              f"key_checked={r1.get('key_checked')}")
        check(r1.get("key_source") == "saved", "① 未传草稿 → key_source=saved",
              f"{r1.get('key_source')}")
        check(r1.get("key_checked") is True, "① 用了真实密钥", f"{r1.get('key_checked')}")
        check(r1.get("success") is True, "① 已保存密钥被上游接受 (200)", f"{r1.get('status')}")

        # ② 传草稿密钥 (与已保存不同, mock 也返 200) → 草稿优先
        st, r2 = post(base, {"endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
                             "name": "mockprov", "api_key": DRAFT_KEY})
        print(f"  ② 传草稿密钥: key_source={r2.get('key_source')} success={r2.get('success')}")
        check(r2.get("key_source") == "draft", "② **核心**: 传了草稿 → key_source=draft",
              f"{r2.get('key_source')}")

        # ③ 决定性判据: 草稿密钥是**无效**的, 已保存密钥是有效的。
        #    旧实现走已保存链 → 会返回 200/成功; 新实现走草稿 → 必须返回 401/失败。
        #    这一条直接区分"草稿优先"与"仍用已保存", 不靠任何内部状态。
        st, r3 = post(base, {"endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
                             "name": "mockprov", "api_key": BOGUS_KEY})
        print(f"  ③ 草稿密钥无效: stage={r3.get('stage')} success={r3.get('success')} "
              f"key_source={r3.get('key_source')} status={str(r3.get('status'))[:60]}")
        check(r3.get("key_source") == "draft", "③ 草稿优先 (key_source=draft)",
              f"{r3.get('key_source')}")
        check(r3.get("success") is False,
              "③ **决定性**: 无效草稿密钥 → 测试失败 (旧实现会用已保存密钥而误报成功)",
              f"success={r3.get('success')} stage={r3.get('stage')}")
        check(r3.get("stage") == "upstream", "③ 401 归因到 upstream 环节", f"{r3.get('stage')}")

        # ④ 空串 → 回落已保存链 (不把"清空输入框"当成"拿空密钥去测")
        st, r4 = post(base, {"endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
                             "name": "mockprov", "api_key": ""})
        print(f"  ④ 空串草稿密钥: key_source={r4.get('key_source')} success={r4.get('success')}")
        check(r4.get("key_source") == "saved", "④ 空串 → 回落已保存链", f"{r4.get('key_source')}")
        check(r4.get("success") is True, "④ 空串时用已保存密钥测 (200)", f"{r4.get('status')}")

        # ⑤ 不传 name → 假密钥探活, 明确标注未校验密钥
        st, r5 = post(base, {"endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions"})
        print(f"  ⑤ 不传 name: key_source={r5.get('key_source')} "
              f"key_checked={r5.get('key_checked')}")
        check(r5.get("key_source") == "none", "⑤ 无 name → key_source=none", f"{r5.get('key_source')}")
        check(r5.get("key_checked") is False, "⑤ 未用真实密钥 (如实标注)", f"{r5.get('key_checked')}")

        # ⑥ 隐私: 响应里绝不回显密钥原文 (草稿密钥只进请求栈帧)
        joined = json.dumps([r1, r2, r3, r4, r5], ensure_ascii=False)
        check(DRAFT_KEY not in joined and SAVED_KEY not in joined and BOGUS_KEY not in joined,
              "⑥ 响应体不含任何密钥原文 (隐私)")
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
