# -*- coding: utf-8 -*-
"""端到端: 选号策略 order vs pace (判据 = 上游**实收的 Authorization**).

场景: 账号型供应商两个号 A/B.
  - A: 额度 1000, 窗口 1 天后重置  ← "不用就浪费", pace 应优先用 A
  - B: 额度 1000, 窗口 30 天后重置 (面板里 A 在前, 且 order 模式就用 A)
  为了让差异可见, 面板顺序设为 **B 在前 A 在后**:
    · order  → 用 B (严格按面板顺序)
    · pace   → 用 A (快重置且剩余多, 更该被消耗)
  这样两种策略选出的号不同, 才能证明策略真的生效.

配额数据经余额端点注入 (mock 返回不同 CapacityRemain/CycleEndTime), 走
balance.rs 的 record_quota -> cached_quota_view 链路, 不额外打上游.
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_picking")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8987, 8988
SEEN = []

# token -> 该账号的额度/窗口 (mock 余额端点按 token 区分)
QUOTA = {
    "A": (1000.0, 1),    # 剩 1000, 1 天后重置 → 该先用
    "B": (1000.0, 30),   # 剩 1000, 30 天后重置 → 不急
}

class Mock(BaseHTTPRequestHandler):
    def _pack(self, remain, days):
        now = datetime.datetime.now()
        return {
            "CapacityRemain": remain, "CapacityUsed": 0,
            "CapacitySize": remain, "CycleCapacityRemain": remain, "CycleCapacitySize": remain,
            "CycleStartTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "CycleEndTime": (now + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
            "DeductionEndTime": 2049883762000,
        }

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        # 余额端点 (通用余额查询走 GET + Bearer)。
        # 真实形态: **一次查询返回全部资源包**, 不是每账号查一次.
        # mock 按 token 区分额度: tok_A 剩 1000/1 天重置, tok_B 剩 1000/30 天重置.
        if self.path.startswith("/balance"):
            auth = (self.headers.get("authorization") or "").replace("Bearer ", "")
            packs = []
            if auth.endswith("tok_A"):
                packs.append(self._pack(1000.0, 1))
            elif auth.endswith("tok_B"):
                packs.append(self._pack(1000.0, 30))
            return self._json(200, {"data": {"Response": {"Data": {
                "TotalCount": len(packs), "Accounts": packs}}}})
        return self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        # chat 请求
        SEEN.append((self.headers.get("authorization") or "").replace("Bearer ", ""))
        return self._json(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                             "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

def start_instance(picking):
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    json.dump({"providers": [{"name": "pickx",
                              "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
                              "api_key_env": "K", "api_key_default": "d",
                              "balance_endpoint": "http://127.0.0.1:%d/balance" % PM,
                              "auth": {"mode": "account"},
                              "models": {"pickx/m": {"upstream_model": "m"}}}]},
              open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
    open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8").write("{}")
    env = dict(os.environ)
    env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="info", AIGATE_NO_PROXY="1",
               AIGATE_ACCOUNT_PICKING=picking)
    return subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=open(os.path.join(ISO,"out.log"),"w",encoding="utf-8"), stderr=subprocess.STDOUT)

def api(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                               headers={"content-type": "application/json"} if body else {})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")

def chat():
    SEEN.clear()
    body = json.dumps({"model": "pickx/m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    r = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                               headers={"content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status

def run(picking):
    p = start_instance(picking)
    try:
        for _ in range(60):
            try:
                api("/admin/api/version"); break
            except Exception: time.sleep(0.5)
        else:
            raise SystemExit("实例未就绪")
        # 面板顺序: B 在前, A 在后 (order 模式会选 B)
        api("/admin/api/accounts/login", {"name": "pickx", "access_token": "tok_B", "expires_in_secs": 86400 * 30})
        api("/admin/api/accounts/login", {"name": "pickx", "access_token": "tok_A", "expires_in_secs": 86400 * 30})
        # 触发一次余额查询 → 写入配额快照 (选号依赖它)
        api("/admin/api/balance")
        time.sleep(0.3)
        st = chat()
        return st, list(SEEN)
    finally:
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()

try:
    st_o, seen_o = run("order")
    ck("① order: 严格按面板顺序 → 用首位 B", seen_o == ["tok_B"], f"seen={seen_o} status={st_o}")
    ck("①b order: 请求成功", st_o == 200, st_o)

    st_p, seen_p = run("pace")
    ck("② **pace: 改用「快重置且剩余多」的 A**（面板首位是 B）", seen_p == ["tok_A"],
       f"seen={seen_p} status={st_p}")
    ck("②b pace: 请求成功", st_p == 200, st_p)
finally:
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)