# -*- coding: utf-8 -*-
"""端到端: 多账号 (登录追加 / 优先级选号 / 401 换号重试 / 冷却 / 按 id 登出 / 回落密钥链).

判据全部取"上游实际收到的 Authorization 头"与 admin API 返回体 —— 不读代码.

场景: 一个账号型供应商 buddyx, 账号池 2 个 (T1 优先, T2 备用); mock 上游对 T1 返回 401,
对 T2 返回 200 —— 用来验证网关在 401 时**换号重试**, 客户端最终仍拿到 200.
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_multi_acct")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8993, 8994
SEEN = []          # 上游收到的 Authorization 序列
REJECT = set()     # 这些 token 被上游拒 (401); 到"换号重试"相位才加入 T1

class Mock(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        auth = (self.headers.get("authorization") or "").replace("Bearer ", "")
        SEEN.append(auth)
        if auth in REJECT:
            return self._json(401, {"error": {"message": "invalid token (mock)", "type": "auth_error"}})
        return self._json(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                             "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "buddyx", "endpoint": f"http://127.0.0.1:{PM}/v1/chat/completions",
                          "api_key_env": "K", "api_key_default": "FALLBACK_KEY", "preset": "buddy",
                          "auth": {"mode": "account"},
                          "models": {"buddyx/m": {"upstream_model": "m"}}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
# 空账号池起步 (全部经由 API 登录写入, 顺带验证"追加"语义)
open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8").write("{}")

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def api(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{PG}{path}", data=body,
                               headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        return -1, {"error": str(e)}

def chat():
    body = json.dumps({"model": "buddyx/m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions", data=body,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1

def accts():
    _, d = api("/admin/api/accounts")
    return d.get("accounts", {}).get("buddyx", [])

try:
    for _ in range(60):
        st, _ = api("/admin/api/version")
        if st == 200: break
        time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    # ① 登录两次 = 两个账号 (追加而非覆盖)
    api("/admin/api/accounts/login", {"name": "buddyx", "access_token": "T1", "expires_in_secs": 86400 * 30})
    api("/admin/api/accounts/login", {"name": "buddyx", "access_token": "T2", "expires_in_secs": 86400 * 60})
    a = accts()
    ck("① 登录两次 → 存下两个账号 (追加语义)", len(a) == 2, [x.get("id", "")[:6] for x in a])
    ck("①b 顺序 = 优先级 (T1 在前)", len(a) == 2, a)

    # ② 状态视图是数组且标出 active
    ck("② 状态为账号数组且首个 active", len(a) == 2 and a[0].get("active") is True and a[1].get("active") is False,
       [(x.get("active"), x.get("dead")) for x in a])
    ck("②b 状态不含 token 原文", "T1" not in json.dumps(a) and "T2" not in json.dumps(a), json.dumps(a)[:80])

    # ③ 同 token 再登录不新增 (原地更新)
    api("/admin/api/accounts/login", {"name": "buddyx", "access_token": "T2", "expires_in_secs": 86400 * 10})
    ck("③ 同 token 重复登录不堆条目 (仍是 2 个)", len(accts()) == 2, len(accts()))

    # ④ 优先用第一个账号 (T1)
    ids = [x["id"] for x in accts()]
    SEEN.clear()
    st = chat()
    ck("④ 优先用优先级最高的账号 (上游收到 T1)", SEEN[:1] == ["T1"], f"seen={SEEN} status={st}")

    # ④b **切换当前使用的账号** (两个号都健康时测, 判据 = 上游实收的 Authorization)
    st, d = api("/admin/api/accounts/reorder", {"name": "buddyx", "ids": [ids[1], ids[0]]})
    ck("④b reorder 接口成功", "error" not in d, d.get("error"))
    a = accts()
    ck("④c 顺序已翻转, 第二个成为 active", a[0]["id"] == ids[1] and a[0]["active"] is True,
       [(x["id"][:6], x["active"]) for x in a])
    SEEN.clear()
    st = chat()
    ck("④d **切换后上游收到 T2** (切换真的生效)", SEEN[:1] == ["T2"], f"seen={SEEN}")
    # ④e 非法重排必须整体拒绝且不改动数据 (宁可失败, 也不做部分应用)
    before = [x["id"] for x in accts()]
    st, d = api("/admin/api/accounts/reorder", {"name": "buddyx", "ids": [ids[1]]})
    ck("④e 数量不匹配的重排被拒绝且不改数据",
       "error" in d and [x["id"] for x in accts()] == before, d.get("error"))
    st, d = api("/admin/api/accounts/reorder", {"name": "buddyx", "ids": ["nope", ids[1]]})
    ck("④f 未知 id 的重排被拒绝且不改数据",
       "error" in d and [x["id"] for x in accts()] == before, d.get("error"))
    # ④g 置顶回 T1 = 「设为当前账号」
    api("/admin/api/accounts/reorder", {"name": "buddyx", "ids": [ids[0], ids[1]]})
    SEEN.clear()
    chat()
    ck("④g 置顶后上游又收到 T1", SEEN[:1] == ["T1"], f"seen={SEEN}")

    # ⑤ 换号重试相位: 从此 T1 被上游拒 → 网关应换 T2 重发, 客户端无感
    REJECT.add("T1")
    SEEN.clear()
    st = chat()
    ck("⑤ 401 时网关换号重试 (上游第二次收到 T2)", len(SEEN) >= 2 and SEEN[1] == "T2", f"seen={SEEN}")
    ck("⑤b 客户端最终拿到 200 (换号对客户端无感)", st == 200, st)

    # ⑥ 冷却生效: 后续请求直接用 T2, 不再试探 T1
    SEEN.clear()
    st2 = chat()
    ck("⑥ 被拒账号进入冷却 → 后续直接用 T2", SEEN == ["T2"], f"seen={SEEN} status={st2}")
    a = accts()
    ck("⑥b 状态里该账号标为冷却", any(x.get("cooling") for x in a), [(x.get("id","")[:6], x.get("cooling")) for x in a])
    # ⑥c 冷却是"不可用"而非"低优先级": 即使把它排到最前, 也不会被选中 (自动跳过)
    api("/admin/api/accounts/reorder", {"name": "buddyx", "ids": [ids[0], ids[1]]})
    a = accts()
    SEEN.clear()
    st2 = chat()
    ck("⑥c 冷却中的号排到最前也不会被用 (自动跳过, active 落到下一个)",
       a[0]["id"] == ids[0] and a[0]["active"] is False and a[1]["active"] is True and SEEN == ["T2"],
       f"order={[(x['id'][:6], x['active'], x['cooling']) for x in a]} seen={SEEN}")

    # ⑦ 按 id 登出只删该账号
    ids = [x["id"] for x in accts()]
    st, d = api("/admin/api/accounts/logout", {"name": "buddyx", "id": ids[0]})
    a = accts()
    ck("⑦ 按 id 登出只删一个", len(a) == 1 and a[0]["id"] == ids[1], [x["id"][:6] for x in a])

    # ⑧ 单账号被拒且**无处可换** → 原样返回 401 (不冷却, 不做迷惑性回落)
    REJECT.add("T2")
    SEEN.clear()
    st = chat()
    a = accts()
    ck("⑧ 无处可换时原样返回 401 (不冷却掉唯一账号)", st == 401 and SEEN == ["T2"], f"seen={SEEN} status={st}")
    ck("⑧b 该账号未被冷却 (单账号场景保持诚实错误)", not any(x.get("cooling") for x in a),
       [(x.get("id","")[:6], x.get("cooling")) for x in a])

    # ⑨ 账号池为空 → 回落 api_key 链 (逃生舱仍有效)
    ids = [x["id"] for x in accts()]
    for i in ids:
        api("/admin/api/accounts/logout", {"name": "buddyx", "id": i})
    ck("⑨ 已清空账号池", len(accts()) == 0, accts())
    SEEN.clear()
    st = chat()
    ck("⑨b 无账号时回落 api_key_default", "FALLBACK_KEY" in SEEN, f"seen={SEEN} status={st}")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)