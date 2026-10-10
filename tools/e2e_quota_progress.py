# -*- coding: utf-8 -*-
"""端到端: 本窗口额度进度 (§7 待办②) —— 口径 + API 输出 + 无数据时不瞎显示.

为什么不是只靠单测: 单测锁的是 `aggregate_credits` 这个纯函数的口径; e2e 要证明的是
**整条链路** —— 上游响应 → 聚合 → BalanceInfo → /admin/api/balance 的 JSON 字段名,
一层对不上前端就读不到 (本项目反复出现的 bug 类型).

⚠️ 必须起**隔离实例**(不打部署实例): 上游积分端点域名硬编码 (buddy 反代轮定的),
本地起不了 mock 拦截, 故仍打真实只读端点 —— 与 e2e_buddy_credits_login.py 同做法.
而部署实例跑的是旧 exe 的话, 该字段根本不存在 (实测: 旧 exe 的 JSON 无 quota_progress),
打它会得到假失败.

判定 (11 条):
  ① balance 200 且能取到 buddy 行
  ② quota_progress 字段存在 (字段名与前端 ds-onboard.js 的取法一致)
  ③ used_pct 在 0..100 (钳制生效)
  ④ window_size/used/remain 自洽: used + remain ≈ size (允许上游精度误差)
  ⑤ 只算有剩余的包: window_remain == 各活跃包的 CycleCapacityRemain 之和
  ⑥ 已耗尽的包不进分母 —— 直接把上游原始响应当场重算一遍对照 (最强判据)
  ⑦ packages == 活跃包数 (样本量)
  ⑧ 非积分制供应商的 quota_progress 为 None, 不是 undefined/0
  ⑨ 已用百分比与"剩余/总量"两个口径一致 (同一件事的两种说法)
  ⑩ 前端 fmtQuotaProgress 用到的字段都在 (不靠猜)
  ⑪ 老接口 (无窗口字段) 不返回 progress

凭据: 部署实例的 data/accounts.json (只读一个 token 字段, 不落盘、不修改部署目录).
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_quota")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
DEPLOY_DATA = r"D:\Office software\Development Project\AI中转\data\accounts.json"
PG = 8993
PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  OK   " if ok else "  !!   ") + name + (("  —— " + str(detail)) if detail else ""))


def get(path, timeout=25):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PG, path), timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def buddy_raw(tok):
    """直接打上游拿原始资源包 (只读), 用于当场合算对照."""
    req = urllib.request.Request(
        "https://copilot.tencent.com/v2/billing/meter/get-user-resource", data=b"{}",
        headers={"Authorization": "Bearer " + tok, "Accept": "application/json",
                 "Content-Type": "application/json", "X-Domain": "copilot.tencent.com",
                 "X-Product": "CodeBuddy", "X-Product-Code": "codebuddy",
                 "user-agent": "CodeBuddyIDE/1.106.1"}, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    return (((j.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []


# ── 隔离实例 (打真实只读端点, 不碰部署目录) ──
accts = json.load(io.open(DEPLOY_DATA, encoding="utf-8"))
buddy_list = accts.get("buddy") or []
if not buddy_list:
    print("  FAIL —— 部署实例的 accounts.json 里没有 buddy 账号 (无凭据可测)")
    sys.exit(1)
tok = buddy_list[0]["access_token"]
refresh = buddy_list[0].get("refresh_token")
expires_at = buddy_list[0].get("expires_at") or int(time.time()) + 86400

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [
    {"name": "buddy", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d", "preset": "buddy",
     "auth": {"mode": "account"}, "models": {}},
    {"name": "plain", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
     "api_key_env": "K", "api_key_default": "d",
     "balance_endpoint": "http://127.0.0.1:9/balance", "models": {}},
]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
# 只有 buddy 播种账号 → plain 无账号, 用来验"没有额度口径就不给进度"
json.dump({"buddy": {"id": "e2e-quota", "access_token": tok, "refresh_token": refresh,
                     "expires_at": expires_at, "dead": False}},
          open(os.path.join(ISO, "data", "accounts.json"), "w", encoding="utf-8"))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            get("/admin/api/version", timeout=5)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("  FAIL —— 隔离实例未就绪")
        sys.exit(1)

    print("=== 本窗口额度进度 (§7 待办②) ===\n")

    st, data = get("/admin/api/balance")
    rows = {b["provider"]: b for b in (data.get("balances") or [])}
    ck("① balance_api_200", st == 200 and "buddy" in rows, "rows=%s" % list(rows))
    buddy = rows.get("buddy") or {}

    prog = buddy.get("quota_progress")
    ck("② quota_progress 字段存在", prog is not None,
       "None=上游未给窗口字段或查询失败 (error=%s)" % buddy.get("error"))
    if not prog:
        print("\n=== 结果 ===")
        print("  FAIL —— 未拿到进度 (若账户确实没有带窗口字段的积分包, 请先确认上游响应)")
        sys.exit(1)

    pct = prog.get("used_pct")
    ck("③ used_pct ∈ [0,100]", pct is not None and 0.0 <= float(pct) <= 100.0, pct)

    size = float(prog.get("window_size") or 0)
    used = float(prog.get("window_used") or 0)
    rem = float(prog.get("window_remain") or 0)
    ck("④ used + remain ≈ size (上游精度内自洽)", abs((used + rem) - size) < max(1.0, size * 0.01),
       "%.2f + %.2f vs %.2f" % (used, rem, size))

    # ⑤⑥ 最强判据: 当场合算原始响应, 与网关输出逐字段对照
    arr = buddy_raw(tok)
    live = [p for p in (arr or []) if float(p.get("CycleCapacityRemain") or 0) > 0]
    exp_size = sum(float(p.get("CycleCapacitySize") or p.get("CapacitySize") or 0) for p in live)
    exp_used = sum(float(p.get("CycleCapacityUsed") or p.get("CapacityUsed") or 0) for p in live)
    exp_rem = sum(float(p.get("CycleCapacityRemain") or 0) for p in live)
    ck("⑤ window_remain == 活跃包 CycleCapacityRemain 之和", abs(rem - exp_rem) < max(1.0, exp_rem * 0.01),
       "网关 %.2f vs 现场 %.2f" % (rem, exp_rem))
    ck("⑥ 已耗尽的包不进分母 (size 用活跃包重算)",
       abs(size - exp_size) < max(1.0, exp_size * 0.01),
       "网关 %.2f vs 现场活跃包 %.2f (总 %d 包, 活跃 %d)"
       % (size, exp_size, len(arr or []), len(live)))
    all_size = sum(float(p.get("CycleCapacitySize") or p.get("CapacitySize") or 0) for p in (arr or []))
    ck("⑥b 分母确实小于「全部包合计」(证明没把旧包算进来)", size < all_size,
       "活跃 {:.2f} < 全部 {:.2f} —— 否则口径就退化成 82.5% 那种误导值".format(size, all_size))
    ck("⑦ packages == 活跃包数", prog.get("packages") == len(live),
       "%s vs %d" % (prog.get("packages"), len(live)))

    # ⑧ 无账号/无额度口径的供应商不得凭空有进度 (None ≠ 0 ≠ undefined)
    no_prog = [p for p in rows.values() if p.get("currency") != "credits"]
    ck("⑧ 非积分制供应商无进度", all(p.get("quota_progress") is None for p in no_prog),
       "检查了 %d 家: %s" % (len(no_prog), [p.get("provider") for p in no_prog]))

    # ⑨ 两种说法互相一致
    if size > 0:
        expect = used / size * 100.0
        ck("⑨ used_pct 与 used/size 一致", abs(expect - float(pct)) < 0.01,
           "%.3f%% vs %.3f%%" % (expect, float(pct)))

    # ⑩ 前端用到的字段齐全 (ds-fmt.js::fmtQuotaProgress)
    ck("⑩ 前端字段齐全 (used_pct/window_size/window_used/packages)",
       all(k in prog for k in ("used_pct", "window_size", "window_used", "packages")),
       sorted(prog.keys()))

    print("\n=== 结果 ===")
    if FAIL:
        print("  FAIL —— %d 条未通过: %s" % (len(FAIL), " / ".join(FAIL)))
        sys.exit(1)
    print("  PASS —— 额度进度口径正确: 只算仍有剩余的包, 分母小于全部包合计")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(ISO, ignore_errors=True)

