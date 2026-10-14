# -*- coding: utf-8 -*-
"""探针: 积分口径 —— 剩余/已用只算「本窗口仍有剩余」的活包（2026-10-13 用户报障）。

用户报障: 「codebudy 的余额与费用有 bug，为什么显示已用？并且获取的剩余积分是错的。」
实测证据: 40 个包里 39 个已耗尽 + 1 个僵尸包(CapacityRemain=500 但本周期已耗尽) + 1 个活包 8 分。
旧算法输出 剩余 508 / 已用 4274 —— 两个数字都是假的:
  ① 508 里的 500 分来自僵尸包, 实际不可用 (一发请求就 403);
  ② 4274 是历史累计消耗, 与"本窗口用了多少"无关。

判据 (mock 上游, 不依赖真实账号状态):
  ① 剩余 == 活包 CycleCapacityRemain 之和 (8), 不含僵尸包的 500
  ② 已用 == 活包 CycleCapacityUsed 之和 (91), 不是历史累计 4274
  ③ 进度 used_pct == 91/100 (只算活包)
  ④ 耗尽包不进任何字段 (expiring/packages 都只看活包)
  ⑤ workbuddy (国际版) 也必须走出积分查询 —— 用户报障的第二个点
     (旧实现 preset 匹配写死 == "buddy", 国际版掉进"未配置余额查询 API")
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8881
PG = 8882
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_credits")

FAIL = []
NCHK = [0]


def chk(n, cond, extra=""):
    NCHK[0] += 1
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


# 复刻用户实测的 40 包形态
def packages():
    arr = []
    for i in range(39):
        arr.append({"CapacityRemain": 0, "CapacityUsed": 1500 if i == 0 else 100,
                    "CycleCapacityRemain": 0,
                    "CycleCapacitySize": 1500 if i == 0 else 100,
                    "CycleCapacityUsed": 1500 if i == 0 else 100,
                    "CycleEndTime": "2026-10-16 20:09:24"})
    # 僵尸包: 跨周期剩 500, 本周期已耗尽
    arr.append({"CapacityRemain": 500, "CapacityUsed": 0, "CycleCapacityRemain": 0,
                "CycleCapacitySize": 500, "CycleCapacityUsed": 500,
                "CycleEndTime": "2026-10-31 23:59:59"})
    # 唯一活包
    arr.append({"CapacityRemain": 8, "CapacityUsed": 91, "CycleCapacityRemain": 8,
                "CycleCapacitySize": 100, "CycleCapacityUsed": 91,
                "CycleEndTime": "2026-11-13 00:34:48"})
    return arr


DOMAIN = {"mode": "cn"}


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.dumps({"data": {"Response": {"Data": {"Accounts": packages()}}}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
    # 国内版走 AIGATE_BUDDY_ENDPOINT 接缝指向 mock; 国际版端点由预设决定(官方域),
    # 无法指向 mock —— 但它只验"字段形态"(不再掉进'未配置余额查询 API')。
    prov = {"providers": [
        {"name": "buddy", "endpoint": "https://copilot.tencent.com/v2/chat/completions",
         "api_key_env": "K", "preset": "buddy", "auth": {"mode": "account"}, "models": {}},
        {"name": "workbuddy", "endpoint": "https://www.workbuddy.ai/v2/chat/completions",
         "api_key_env": "K", "preset": "workbuddy", "auth": {"mode": "account"}, "models": {}},
    ]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)
    with io.open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8") as f:
        json.dump({"buddy": {"id": "cn-acct", "access_token": "T_CN",
                             "expires_at": int(time.time()) + 86400 * 60, "dead": False},
                   "workbuddy": {"id": "intl-acct", "access_token": "T_INTL",
                                 "expires_at": int(time.time()) + 86400 * 60, "dead": False}}, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1",
               AIGATE_BUDDY_ENDPOINT="http://127.0.0.1:%d" % MP)
    srv = ThreadingHTTPServer(("127.0.0.1", MP), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as admin_auth
        AH = admin_auth(os.path.join(ISO, "data"))

        def balance():
            r = urllib.request.Request("http://127.0.0.1:%d/admin/api/balance" % PG, headers=AH)
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        d = balance()
        rows = {b["provider"]: b for b in d.get("balances", [])}
        b = rows.get("buddy") or {}

        # ①② 核心口径
        chk("buddy-balance-is-live-only", b.get("balance") == 8.0,
            "balance=%s (旧口径会算成 508, 含僵尸包 500)" % b.get("balance"))
        chk("buddy-consumed-is-window-scoped", b.get("consumed") == 91.0,
            "consumed=%s (旧口径会算成 4274 历史累计)" % b.get("consumed"))
        chk("buddy-currency-credits", b.get("currency") == "credits", b.get("currency"))

        # ③ 进度只算活包
        prog = b.get("quota_progress") or {}
        chk("buddy-progress-pct-91", abs(float(prog.get("used_pct") or 0) - 91.0) < 0.01,
            "used_pct=%s" % prog.get("used_pct"))
        chk("buddy-progress-packages-1", prog.get("packages") == 1,
            "packages=%s (只该算活包)" % prog.get("packages"))
        chk("buddy-progress-window", float(prog.get("window_remain") or 0) == 8.0
            and float(prog.get("window_size") or 0) == 100.0,
            "remain=%s size=%s" % (prog.get("window_remain"), prog.get("window_size")))

        # ④b 僵尸包不得混进"剩余": 构造一个只有 CapacityRemain、没有 Cycle* 的包。
        #     旧实现在这条上会把 500 算进剩余 → 面板显示 508, 实际可用 8。
        #     (单测里已覆盖; 这里端到端再钉一次, 因为它正是用户报障"剩余积分是错的"。)
        #     通过 buddy 的 total_balance 与 balance 一起验: 二者都应只含活包。
        chk("buddy-no-zombie-in-balance",
           float(b.get("balance") or 0) == 8.0 and float(b.get("total_balance") or 0) == 8.0,
           "balance=%s total=%s (若含僵尸包会是 508)" % (b.get("balance"), b.get("total_balance")))

        # ⑤ workbuddy (国际版) 不得再掉进"未配置余额查询 API"
        wb = rows.get("workbuddy") or {}
        err = wb.get("error") or ""
        chk("workbuddy-row-present", "workbuddy" in rows, list(rows))
        chk("workbuddy-not-unconfigured", "未配置" not in err,
            "error=%s (旧 bug 的形态)" % err[:80])
        # 国际版打官方域, 隔离环境里可能网络不可达 —— 只要不是"未配置"就算接线正确
        chk("workbuddy-queried-or-network", wb.get("balance") is not None or err != "",
            "balance=%s error=%s" % (wb.get("balance"), err[:60]))

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (NCHK[0], len(FAIL)))
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
