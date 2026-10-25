# -*- coding: utf-8 -*-
"""「死代码接线」探针（2026-10-24）。

## 为什么有这个探针

用户批评「你只做了第一层修改，里面的其他层级没做」。查证发现最直接的物证是
**三处功能完整写好、却没有任何界面调用点**：

  ① `openLogin()` / `browserLogin()`（ds-provsave.js:375/386）
     + 完整登录弹窗模板（modal-add-fetchdiff.html:22-113，含多账号/选号策略/逐行登出）
     —— 但 `openLogin(` 在整个 src/ 下**只有定义处一次命中**，
        而弹窗靠 `x-show="loginProv"` 显示、`loginProv` 只被 `openLogin` 设为非空
        → 整个登录弹窗**永远不可达**，账号型供应商无法在界面上登录。

  ② `presetDirty()` / `presetReset()`（ds-provsave.js:284-298）+ 4 条 i18n
     —— HTML 里零引用。用户把内置预设改乱后没有任何提示，也不知道能退回默认值。

  ③ 顺带修的真 bug：`presetDirty` 里 `const t = this.presetOf(p)` 遮蔽了全局
     翻译函数 `t()`，导致 `presetReset` 里 `t('preset_reset_done')` 变成
     "调用预设对象" → 必然 TypeError（因为无调用点，一直没暴露）。

## 判据

  ① 预设列表里账号型预设行有「登录」按钮
  ② 点击它 → `loginProv` 被设置为该预设 id → **登录弹窗真的出现**
  ③ 列表页账号型供应商的展开区有登录/管理账号入口
  ④ 改乱内置预设后出现「已修改」徽章 + 「恢复默认」按钮
  ⑤ 点「恢复默认」→ endpoint 恢复成模板值、且**不抛 TypeError**
     （第 ⑤ 条专门守那个变量遮蔽 bug：修好前它会抛 `t is not a function`）
  ⑥ 零 pageerror

用法: python tools/probe_dead_code.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_deadcode")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
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


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2200);

  const out = { errs };

  // ① 打开「添加供应商」弹窗，看账号型预设行有没有「登录」按钮
  out.presetRow = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.showAddProvider = true;
    await new Promise(r => setTimeout(r, 800));
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const accPreset = (d.presets || []).find(x => x.account);
    if (!accPreset) return { err: 'no account preset' };
    const btns = Array.from(document.querySelectorAll('button')).filter(isVis);
    const loginBtn = btns.find(x => /登录|Sign in|Login/i.test(x.textContent || ''));
    return {
      presetId: accPreset.id,
      hasLoginBtn: !!loginBtn,
      loginBtnText: loginBtn ? loginBtn.textContent.trim() : null,
    };
  });

  // ② 点它 → loginProv 应被设置，登录弹窗应出现
  out.loginOpen = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const accPreset = (d.presets || []).find(x => x.account);
    if (!accPreset) return { err: 'no account preset' };
    d.openLogin(accPreset.id);
    await new Promise(r => setTimeout(r, 900));
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    // 登录弹窗是 x-show="loginProv" 那个 overlay
    const overlays = Array.from(document.querySelectorAll('.modal-overlay')).filter(isVis);
    const loginOverlay = overlays.find(o => /登录|Sign in/i.test(o.textContent || ''));
    return { loginProv: d.loginProv, overlayVisible: !!loginOverlay };
  });
  // 关掉
  await p.evaluate(() => { const d = document.querySelector('[x-data]')._x_dataStack[0];
    try { d.stopLoginPoll(); } catch(e) {} d.loginProv=''; d.showAddProvider=false; });
  await p.waitForTimeout(400);

  // ③ 列表页账号型供应商的登录入口
  out.rowEntry = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    // 找一个账号型供应商（buddy / workbuddy）
    const acc = (d.providersFormData || []).find(x => d.isAccountProv(x));
    if (!acc) return { err: 'no account provider' };
    if (!d.isProvExpanded(acc.name)) d.toggleProvExpand(acc.name);
    await new Promise(r => setTimeout(r, 900));
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const btns = Array.from(document.querySelectorAll('button')).filter(isVis);
    const entry = btns.find(x => /^(登录|Login)$/.test((x.textContent || '').trim())
                             || /管理账号|Manage accounts/.test(x.textContent || ''));
    return { provider: acc.name, hasEntry: !!entry,
             text: entry ? entry.textContent.trim() : null };
  });

  // ④⑤ 内置预设改乱 → 出现「已修改」+「恢复默认」；点恢复 → 值回来且不 TypeError
  out.presetDirty = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const pi = (d.providersFormData || []).findIndex(x => x.preset);
    if (pi < 0) return { err: 'no preset provider' };
    const prov = d.providersFormData[pi];
    d.openProviderDrawer(pi);
    await new Promise(r => setTimeout(r, 700));
    const tpl = d.presetOf(prov);
    const before = prov.endpoint;
    // 改乱
    prov.endpoint = 'https://tampered.example.com/v1/chat/completions';
    d.markDirty();
    await new Promise(r => setTimeout(r, 400));
    const dirty = d.presetDirty(prov);
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const btns = Array.from(document.querySelectorAll('button')).filter(isVis);
    const resetBtn = btns.find(x => /恢复默认|Reset to default/.test(x.textContent || ''));
    const badge = Array.from(document.querySelectorAll('.bdg')).filter(isVis)
      .find(x => /已修改|Modified/.test(x.textContent || ''));
    let resetErr = null;
    try { d.presetReset(d); } catch (e) { resetErr = String(e); }
    try { d.presetReset(prov); } catch (e) { resetErr = String(e); }
    await new Promise(r => setTimeout(r, 400));
    return {
      presetName: prov.name,
      dirtyFlag: dirty,
      hasResetBtn: !!resetBtn,
      hasModifiedBadge: !!badge,
      endpointRestored: prov.endpoint === (tpl ? tpl.endpoint : null),
      expectedEndpoint: tpl ? tpl.endpoint : null,
      actualEndpoint: prov.endpoint,
      resetErr,
      dirtyAfterReset: d.presetDirty(prov),
    };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 300) })));
"""


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
    shutil.copy2(os.path.join(DEPLOY_ROOT, "providers.json"), os.path.join(WD, "providers.json"))
    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(WD, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=WD,
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
        jsf = os.path.join(WD, "d.js")
        io.open(jsf, "w", encoding="utf-8").write(JS)
        env2 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env2, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-600:]}\n{(r.stderr or '')[-600:]}")
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal'][:250]}")
            return 1
    finally:
        proc.terminate()
        try: proc.wait(8)
        except Exception: proc.kill()

    pr = d.get("presetRow") or {}
    lo = d.get("loginOpen") or {}
    re_ = d.get("rowEntry") or {}
    pd_ = d.get("presetDirty") or {}
    print("死代码接线（三处已写好但无界面入口的功能）")
    print(f"  [现场] 预设行: {pr}")
    print(f"  [现场] 点登录: {lo}")
    print(f"  [现场] 列表入口: {re_}")
    print(f"  [现场] 预设改乱: {json.dumps(pd_, ensure_ascii=False)[:400]}")

    check(pr.get("hasLoginBtn") is True,
          "① 账号型预设行有「登录」按钮（原来没有调用点）",
          f"实际={pr}")
    check(lo.get("loginProv") == pr.get("presetId"),
          "② 点「登录」后 loginProv 被设置（弹窗的显示条件被满足）",
          f"loginProv={lo.get('loginProv')!r} 期望={pr.get('presetId')!r}")
    check(lo.get("overlayVisible") is True,
          "② 登录弹窗**真的出现**（原来永远不可达）",
          f"overlayVisible={lo.get('overlayVisible')}")

    check(re_.get("hasEntry") is True,
          "③ 列表页账号型供应商有登录/管理账号入口",
          f"实际={re_}")

    check(pd_.get("dirtyFlag") is True,
          "④ 改乱内置预设后 presetDirty 判为已修改", f"实际={pd_.get('dirtyFlag')}")
    check(pd_.get("hasModifiedBadge") is True,
          "④ 出现「已修改」徽章（原来 HTML 零引用）",
          f"hasModifiedBadge={pd_.get('hasModifiedBadge')}")
    check(pd_.get("hasResetBtn") is True,
          "④ 出现「恢复默认」按钮",
          f"hasResetBtn={pd_.get('hasResetBtn')}")
    check(pd_.get("resetErr") is None,
          "⑤ 点「恢复默认」**不抛异常**（修好 t 被遮蔽的 bug；修前会 TypeError）",
          f"error={pd_.get('resetErr')}")
    check(pd_.get("endpointRestored") is True,
          "⑤ endpoint 恢复成模板默认值",
          f"actual={pd_.get('actualEndpoint')!r} expected={pd_.get('expectedEndpoint')!r}")
    check(pd_.get("dirtyAfterReset") is False,
          "⑤ 恢复后 presetDirty 回到 false", f"实际={pd_.get('dirtyAfterReset')}")

    check(len(d.get("errs") or []) == 0, "⑥ 零 pageerror", str((d.get("errs") or [])[:2]))

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
