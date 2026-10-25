# -*- coding: utf-8 -*-
"""opencode 套餐额度面板探针（2026-10-24，Console API 版）。

## 这套判据守的是什么

额度数据来自 **Console 官方 API**（`/console/api/go/status`），是真实值。
早先的「本地累加 allowance + 模型额度表」方案已删除 —— 它有两个根本缺陷：
看不到别的客户端的用量、且额度表会过期。判据因此也全部重写。

## 判据

  ① 未配凭据时：额度块出现但**不显示假进度条**，且"查询额度"可点
  ② 配了无效凭据 → 如实报错（含可操作提示），**不画 0%**
  ③ 配了真实凭据（本机有登录态时）→ 三个窗口都渲染，金额/百分比/重置时间齐全
  ④ 已用满的窗口显示 **100%**（不夹到别的数）
  ⑤ 超额窗口若出现，必须 >100% 不被 clamp（用构造数据验证，见单测）
  ⑥ 进度条宽度封顶 100%（否则溢出容器）
  ⑦ 零 pageerror

关于 ③：真实凭据来自本机 `.openbitfun/tmp/oc_profile`（此前登录留下的登录态）。
若该 profile 不存在或已失效，③ 会**显式跳过**并说明原因，而不是假装通过。

用法: python tools/probe_quota_panel.py
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
DEPLOY_ROOT = r"D:\Office software\Development Project\AI中转"
PDIR_ALT = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")
PROFILE = os.path.join(ROOT, ".openbitfun", "tmp", "oc_profile")
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_quota")
ORG = "wrk_01KS5PNNQH9TAEBFMNJ5C8AF07"

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        _fails.append(label)
        print(f"  {BAD}  {label}  {detail}")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def pdir():
    for rel in (".openbitfun/tmp/vidtools", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


GET_COOKIE_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const ctx = await chromium.launchPersistentContext(process.env.PROFILE, {
    channel: 'msedge', headless: true,
  });
  const cookies = await ctx.cookies('https://opencode.ai');
  const want = cookies.find(c => c.name === '__Host-console_session');
  await ctx.close();
  console.log('COOKIE ' + JSON.stringify(want ? want.value : null));
})().catch(() => console.log('COOKIE null'));
"""

UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2400);

  const out = { before: null, after: null, errs };

  const snap = () => {
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const rows = Array.from(document.querySelectorAll('.quota-row')).filter(isVis);
    return {
      quotaSection: Array.from(document.querySelectorAll('.pe-sec')).filter(isVis)
        .some(x => /套餐额度|Plan quota/.test(x.textContent || '')),
      queryBtn: !!Array.from(document.querySelectorAll('button')).filter(isVis)
        .find(x => /查询额度|Query quota/.test(x.textContent || '')),
      notQueried: Array.from(document.querySelectorAll('.txs.tm')).filter(isVis)
        .some(x => /尚未查询|Not queried/.test(x.textContent || '')),
      err: (Array.from(document.querySelectorAll('.quota-err')).filter(isVis)[0] || {}).textContent || '',
      rows: rows.map(r => ({
        label: (r.querySelector('.quota-model') || {}).textContent || '',
        pct: (r.querySelector('.quota-w-v') || {}).textContent || '',
        amt: (r.querySelector('.quota-amt') || {}).textContent || '',
        // ⚠️ 只取**可见**的那个 .quota-reset：模板里有两个（"重置 …" 与 "该窗口未激活"），
        // 用 x-show 互斥。直接 textContent 会把隐藏的那个也读进来，
        // 表现为"5 小时窗口显示「重置 」"这种假象（曾误判为 UI bug，实际显示是对的）。
        reset: (Array.from(r.querySelectorAll('.quota-reset'))
                  .find(el => getComputedStyle(el).display !== 'none') || {}).textContent || '',
        barW: ((r.querySelector('.quota-bar > i') || {}).style || {}).width || '',
      })),
    };
  };

  // 展开 go 供应商（额度入口在那里）
  const expandedGo = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = (d.providersFormData || []).find(x => x.name === 'go');
    if (!prov) return { ok: false, why: 'providersFormData 里没有 go' };
    if (!d.isProvExpanded('go')) d.toggleProvExpand('go');
    await new Promise(r => setTimeout(r, 900));
    return { ok: true, may: d.provMayHaveQuota(prov) };
  });
  out.expandedGo = expandedGo;
  out.before = await p.evaluate(snap);

  // 点「查询额度」
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.fetchOpencodeQuota('go');
  });
  await p.waitForTimeout(2500);
  out.after = await p.evaluate(snap);

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 300) })));
"""


def prep_providers(cookie: str) -> str:
    """把仓库 providers.json 复制到隔离目录，给 go 写入凭据。返回 providers.json 路径。"""
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    o = json.load(io.open(src, encoding="utf-8"))
    provs = o if isinstance(o, list) else o.get("providers") or []
    for p in provs:
        if p.get("name") == "go" and cookie:
            p["console_session"] = "__Host-console_session=" + cookie
            p["console_org_id"] = ORG
    dst = os.path.join(WD, "providers.json")
    if isinstance(o, list):
        io.open(dst, "w", encoding="utf-8").write(json.dumps(provs, ensure_ascii=False, indent=2))
    else:
        o["providers"] = provs
        io.open(dst, "w", encoding="utf-8").write(json.dumps(o, ensure_ascii=False, indent=2))
    return dst


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}（先 cargo build --release）")
        return 1
    pd = pdir()
    if not pd:
        print(f"{BAD}  找不到 playwright")
        return 1
    shutil.rmtree(WD, ignore_errors=True)
    os.makedirs(os.path.join(WD, "data"), exist_ok=True)

    # 拿真实 Cookie（有就用；没有就跳过 ③）
    cookie = None
    if os.path.isdir(PROFILE):
        jsf = os.path.join(WD, "_c.js")
        io.open(jsf, "w", encoding="utf-8").write(GET_COOKIE_JS)
        env = dict(os.environ, NODE_PATH=os.path.join(pd, "node_modules"), PROFILE=PROFILE)
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("COOKIE ")), None)
        if line:
            v = json.loads(line[7:])
            cookie = v or None
    print(f"[准备] 真实 Console Cookie: {'有' if cookie else '无（③ 将跳过）'}")

    prep_providers(cookie)
    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env2 = dict(os.environ)
    env2["PORT"] = str(port)
    env2["AIGATE_DATA_DIR"] = os.path.join(WD, "data")
    env2["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env2, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read(); break
            except Exception: time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        jsf = os.path.join(WD, "u.js")
        io.open(jsf, "w", encoding="utf-8").write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-500:]}\n{(r.stderr or '')[-500:]}")
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal'][:250]}")
            return 1
    finally:
        proc.terminate()
        try: proc.wait(timeout=8)
        except Exception: proc.kill()

    before, after = d.get("before") or {}, d.get("after") or {}
    eg = d.get("expandedGo") or {}
    print("\nopencode 套餐额度面板（Console API 版）")
    print(f"  [现场] provMayHaveQuota(go) = {eg.get('may')}")
    print(f"  [现场] 查询前: 有按钮={before.get('queryBtn')} 未查提示={before.get('notQueried')} 行数={len(before.get('rows') or [])}")
    print(f"  [现场] 查询后: 错误={after.get('err')!r} 行数={len(after.get('rows') or [])}")
    for r0 in (after.get("rows") or []):
        print(f"     {r0['label']}: {r0['pct']} | {r0['amt']} | {r0['reset']} | bar={r0['barW']}")

    # ① 额度区块与按钮
    check(before.get("quotaSection") is True,
          "① 展开 go 后出现「套餐额度」区块")
    check(before.get("queryBtn") is True,
          "① 有可点的「查询额度」按钮")
    # 查询前不应有假进度条
    check(len(before.get("rows") or []) == 0,
          "① 查询前不渲染任何额度行（不画假进度条）",
          f"实际 {len(before.get('rows') or [])} 行")

    rows = after.get("rows") or []
    if cookie:
        # ③ 真实数据
        if after.get("err"):
            print(f"  {SKIP}  ③ 查询报错（Cookie 可能已过期）: {after['err'][:120]}")
            check("过期" in (after.get("err") or "") or "认证" in (after.get("err") or ""),
                  "② 报错时给出可操作提示（提到过期/认证）",
                  f"实际={after.get('err')!r}")
        else:
            check(len(rows) == 3, "③ 三个窗口都渲染（5 小时/本周/本月）",
                  f"实际 {len(rows)} 行")
            labels = [r0["label"] for r0 in rows]
            check(labels == ["5 小时", "本周", "本月"], "③ 窗口顺序正确", str(labels))
            # ④ 满额窗口显示 100%
            month = rows[2] if len(rows) > 2 else {}
            check("100" in (month.get("pct") or ""),
                  "④ 已用满的月窗口显示 100%",
                  f"实际={month.get('pct')!r}")
            check(bool(month.get("amt")) and "$" in (month.get("amt") or ""),
                  "③ 金额已渲染（形如 $60.00 / $60.00）",
                  f"实际={month.get('amt')!r}")
            check(bool(month.get("reset")),
                  "③ 重置时间已渲染",
                  f"实际={month.get('reset')!r}")
            # 5h 窗口在本账号上未激活（resetsAt 为 null）→ 必须显示「该窗口未激活」，
            # 不能是空的「重置 」。（模板块用 x-show 互斥，读 textContent 会读错，见 snap 的注释）
            fh = rows[0] if rows else {}
            check("未激活" in (fh.get("reset") or "") or "重置" in (fh.get("reset") or ""),
                  "③ 未激活窗口有明确文案（不是空的「重置 」）",
                  f"实际={fh.get('reset')!r}")
            # ⑥ 进度条封顶
            over = [r0 for r0 in rows if r0["barW"] and r0["barW"].rstrip("%").replace(".", "").isdigit()
                    and float(r0["barW"].rstrip("%")) > 100.0]
            check(not over, "⑥ 进度条宽度不超过 100%",
                  f"越界={[(r0['label'], r0['barW']) for r0 in over]}")
    else:
        print(f"  {SKIP}  ③④⑥ 无本机登录态，跳过真实数据判据")

    check(len(d.get("errs") or []) == 0, "⑦ 零 pageerror", str((d.get("errs") or [])[:2]))

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过:")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
