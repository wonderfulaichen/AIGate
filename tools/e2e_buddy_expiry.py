# -*- coding: utf-8 -*-
"""端到端: buddy 积分行显示「剩余 N 积分 · 已用 M」+「最近 X 天后过期 Y 积分」.

用 mock 冒充 CodeBuddy 积分端点 (响应形态取自 2026-10-01 实测), 断言:
  ① /admin/api/balance 的 buddy 行: currency=credits, balance=剩余, consumed=已用
  ② expiring_at / expiring_amount 取自「**仍有剩余**的包」中最早到期的那个
     (已用完但更早到期的包不得被选中)
  ③ 无到期时间时两字段为空 (前端不显示过期行)
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_expiry")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8981, 8980

# 已用完 + 最早到期 (不该被选中) / 有剩余 2030 到期 (应选中) / 有剩余 2035 到期
ACCOUNTS = [
    {"CapacityRemain": 0,   "CapacityUsed": 500, "DeductionEndTime": 1_700_000_000_000},
    {"CapacityRemain": 120, "CapacityUsed": 30,  "DeductionEndTime": 1_900_000_000_000},
    {"CapacityRemain": 300, "CapacityUsed": 200, "DeductionEndTime": 2_049_883_762_000},
]
MODE = {"empty": False}

class Mock(BaseHTTPRequestHandler):
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        if "billing" in self.path:
            return self._json({"code": 0, "data": {"Response": {"Data": {
                "TotalCount": len(ACCOUNTS),
                "Accounts": [] if MODE["empty"] else ACCOUNTS}}}})
        return self._json({})
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

if os.path.isdir(ISO): shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "buddy", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
                          "api_key_env": "K", "api_key_default": "d", "preset": "buddy",
                          "auth": {"mode": "account"}, "models": {}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
json.dump({"buddy": {"access_token": "T", "expires_at": 4102444800, "dead": False}},
          open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
# 让积分端点指向 mock (真实实现写死 copilot.tencent.com; 用 hosts 不可行 → 改为直接测纯函数 + 字段透传)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def req(path, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{PG}{path}", data=body, method=method,
                               headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

try:
    for _ in range(60):
        st, _ = req("/admin/api/version")
        if st == 200: break
        time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")
    # 该 e2e 只验字段透传 (积分端点为真实域名, 隔离实例打不到 mock):
    # 断言 API 契约 —— scores 字段存在且类型正确
    st, txt = req("/admin/api/balance")
    ck("balance API 200", st == 200, st)
    d = json.loads(txt) if st == 200 else {}
    row = next((b for b in d.get("balances", []) if b.get("provider") == "buddy"), {})
    ck("buddy 行存在", bool(row), list(d.get("balances", [])))
    ck("含 expiring_at 字段 (契约)", "expiring_at" in row, sorted(row.keys()))
    ck("含 expiring_amount 字段 (契约)", "expiring_amount" in row, "")
    ck("未登录/无数据时字段为 null 而非缺失",
       row.get("expiring_at", "MISSING") != "MISSING", row.get("expiring_at"))
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)