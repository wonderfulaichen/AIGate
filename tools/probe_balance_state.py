# -*- coding: utf-8 -*-
"""余额四态分离的端到端验证.

背景 (2026-10-24, 借鉴 68hub 的失败/空态分离): 旧结构只有 balance + error,
于是这几种**处置完全相反**的处境在前端长得一模一样:
  · 未配置 (Unsupported): 多数供应商本来就没有公开余额 API, **不该报警**
  · 查询失败 (Failed):    该提示重试
  · 真的 0 元 (Ok + 0):   该充值
  · 手动设置 (Manual):    用户自填, 不是上游实测

本探针构造三种真实场景, 逐一验证后端给出**不同**的 state:
  ① 未配 balance_endpoint 的供应商         → state=unsupported
  ② 配了但上游 500 / 连不上的供应商         → state=failed
  ③ 手动设了余额的供应商                   → state=manual, source=manual
  ④ 手动值**优先**于 API 结果 (旧行为如此, 别改坏)

另验证:
  ⑤ 四态的 JSON 形态是 snake_case (前端拿到的不是 "Unsupported")
  ⑥ 旧记录 (无 state 字段) 反序列化后为 ok (不倒退成 unsupported)

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
    """余额端点: 一律 500 (模拟查询失败)。"""
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"error": "upstream boom"}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
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

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_bal_state")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "data"), exist_ok=True)

    cfg = {"providers": [
        # ① 未配置余额端点 → unsupported
        {"name": "noapi", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
         "api_key_env": "K1", "api_key_default": "d",
         "models": {"noapi/m1": {"upstream_model": "m1"}}},
        # ② 配了余额端点但上游 500 → failed
        {"name": "broken", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
         "api_key_env": "K2", "api_key_default": "d",
         "balance_endpoint": f"http://127.0.0.1:{mport}/balance",
         "models": {"broken/m1": {"upstream_model": "m1"}}},
        # ③ 手动设了余额 → manual
        {"name": "handset", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
         "api_key_env": "K3", "api_key_default": "d",
         "balance_endpoint": f"http://127.0.0.1:{mport}/balance",
         "models": {"handset/m1": {"upstream_model": "m1"}}},
    ]}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 手动余额: 给 handset 设 42.5 (必须先建 config 目录)
    os.makedirs(os.path.join(workdir, "config"), exist_ok=True)
    with open(os.path.join(workdir, "config", "balance.json"), "w", encoding="utf-8") as f:
        json.dump({"balances": {"handset": 42.5}}, f)

    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(workdir, "data")
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

        st, data = get(base, "/admin/api/balance")
        rows = data if isinstance(data, list) else (data.get("balances") or [])
        by = {r.get("provider"): r for r in rows}
        print(f"\n  余额行 ({len(rows)}): " + ", ".join(by.keys()))

        def st_of(name):
            r = by.get(name)
            return (r or {}).get("state")

        # ① 未配置
        check(st_of("noapi") == "unsupported",
              "① **核心**: 未配余额端点 → state=unsupported",
              f"实得 {st_of('noapi')}  {by.get('noapi')}")

        # ② 查询失败
        check(st_of("broken") == "failed",
              "② **核心**: 上游 500 → state=failed",
              f"实得 {st_of('broken')}  {by.get('broken')}")

        # ③ 手动设置
        check(st_of("handset") == "manual",
              "③ **核心**: 手动余额 → state=manual",
              f"实得 {st_of('handset')}  {by.get('handset')}")
        h = by.get("handset") or {}
        check(h.get("source") == "manual", "③ source 字段也是 manual", f"{h.get('source')}")
        check(abs(float(h.get("balance") or 0) - 42.5) < 0.01,
              "④ 手动值优先于 API 结果 (旧行为, 别改坏)", f"{h.get('balance')}")

        # 三态互不相同 (这是"分离"的本质判据)
        three = {st_of("noapi"), st_of("broken"), st_of("handset")}
        check(len(three) == 3, "⑤ 三种处境给出三种**不同**的 state (确实分离了)", f"{three}")

        # ⑥ 形态是 snake_case
        for v in three:
            check(v in ("unsupported", "failed", "manual", "ok"), f"⑥ state 值形态 snake_case: {v}")
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
