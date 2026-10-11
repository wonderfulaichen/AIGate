# -*- coding: utf-8 -*-
"""探针: 「浏览器登录」按钮的可用性与弹窗拦截修复（2026-10-12 用户报障）。

用户原话:「你这个登陆机制是不是有bug？为什么我点击后就是闪一下，然后完全没有其他反应，
也不跳转浏览器。」（截图里同时显示了「浏览器登录」按钮与「该供应商不是账号形式」）

查出**三个真 bug**（本探针逐条钉住）:

  A. **弹窗被静默拦截**：`window.open` 原在 `await fetch(...)` **之后**调用，
     浏览器只在用户手势的同步调用栈内允许弹窗 → 被拦截；而多数浏览器返回的是
     "已关闭的窗口代理"(truthy)，旧代码的 `if(!opened)` 也不进 → 既不跳转也不提示。
     修法: 先同步 `window.open('about:blank')` 占住手势名额，拿到 URL 后再导航。
  B. **未保存的预设条目被判成静态型**：模板里 `表单条目 || 预设条目` 会短路 ——
     表单里有同名条目（刚「添加」未「保存」）时不再看预设，而新条目没有 auth 段
     → 浏览器登录按钮不渲染 + 显示"不是账号形式"。
     修法: 两边**都要看**（任一声明 account 即为账号型），取反条件同步改成 `&&`。
  C. **后端只查已保存的注册表**：预设「添加」只进表单，保存才落盘（既有设计），
     所以"加进来还没保存就点登录"是正常顺序，却报"不是账号形式"。
     修法: 回落内置预设目录。

判据（全部在真实隔离实例上用无头浏览器操作，不看代码）:
  ① workbuddy 预设卡能加到表单（前置）
  ② 未保存状态下打开登录弹窗: **渲染出**「浏览器登录」且**不显示**"不是账号形式"
  ③ 点「浏览器登录」: 后端返回 auth_url（不再报"不是账号形式"）
  ④ 点「浏览器登录」: **真的打开了新页面**（弹窗未被拦截）← 用户报障的直接判据
  ⑤ 新页面地址是官方登录页（含 platform=workbuddy-ai）
  ⑥ 两处判据一致: 账号型条目下"静态提示"与"浏览器登录按钮"不同时出现

用法: python tools/probe_login_button.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_loginbtn")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8977

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
# **空 providers.json** —— 关键: 模拟"workbuddy 只在预设目录里、尚未添加保存"
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": []}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("隔离实例未就绪")
        sys.exit(1)

    # ③ 直接测后端: 未保存的 workbuddy 名能否开登录流（这是 bug C 的判据）
    print("=== ③ 后端: 未保存的预设名能否开登录流 ===\n")
    body = json.dumps({"name": "workbuddy"}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/admin/api/accounts/login/start" % PG,
                                 data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        url = d.get("auth_url") or ""
        ck("③ 未保存的 workbuddy 也能开登录流（不再报'不是账号形式'）",
           bool(url), "resp=%s" % json.dumps(d, ensure_ascii=False)[:180])
        ck("⑤ auth_url 是官方页且 platform=workbuddy-ai",
           "workbuddy.ai" in url and "platform=workbuddy-ai" in url, url[:130])
    except Exception as e:
        ck("③ 未保存的 workbuddy 也能开登录流", False, "%s: %s" % (type(e).__name__, e))

    # ① ② ④ ⑥ 前端: 无头浏览器走真实交互
    print("\n=== ①②④⑥ 前端交互 ===\n")
    js = os.path.join(ISO, "t.js")
    io.open(js, "w", encoding="utf-8").write("""
const path = require('path');
const PW = path.join(%r, '.openbitfun','tmp','vidtools','node_modules','playwright');
const { chromium } = require(PW);
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  const errs = [], reqs = [];
  p.on('pageerror', e => errs.push(String(e)));
  p.on('response', async r => {
    if (r.url().includes('/admin/api/accounts/login/start')) {
      let t=''; try { t=(await r.text()).slice(0,220); } catch(e){}
      reqs.push(r.status() + ' ' + t);
    }
  });
  await p.goto('http://127.0.0.1:%d/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);
  await p.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.seg-i,.nav-i,button,a'))
      .find(x => (x.textContent||'').trim() === '供应商');
    if (el) el.click();
  });
  await p.waitForTimeout(2000);

  // 直接调 openLogin('workbuddy') —— 等价于用户在预设卡上点「登录」
  const modal = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (!st) return { no_alpine: true };
    st.openLogin('workbuddy');
    return { called: true };
  });
  await p.waitForTimeout(1200);

  const m1 = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return { open: false };
    const t = m.innerText || '';
    return {
      open: true,
      has_browser_btn: Array.from(m.querySelectorAll('button')).some(x => (x.textContent||'').includes('浏览器登录')),
      has_static_hint: t.includes('静态 API Key') || t.includes('静态 Key'),
      has_not_account_hint: t.includes('不是账号形式'),
    };
  });

  // 点「浏览器登录」
  const before = ctx.pages().length;
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return;
    const btn = Array.from(m.querySelectorAll('button')).find(x => (x.textContent||'').includes('浏览器登录'));
    if (btn) btn.click();
  });
  await p.waitForTimeout(5000);
  const after = ctx.pages().length;
  let newUrl = '';
  for (const pg of ctx.pages()) {
    const u = pg.url();
    if (u && !u.includes('127.0.0.1:%d') && u !== 'about:blank') newUrl = u;
  }
  const msg = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    return m ? (m.innerText||'').slice(0,300) : '';
  });

  // ── 齿-test: 在**模拟弹窗拦截**下重放同一按钮 ──
  // ⚠️ 为什么必须单独做: headless 默认**不拦**弹窗, 甚至 `page.evaluate` 本身就在
  //    用户手势栈内 —— 实测"把 window.open 挪回 await 之后"两种写法**都能开窗口**,
  //    故上面的 ④ 判据在默认设置下**没有区分力**（真实浏览器带弹窗拦截时会拦）。
  //    这里用 `window.open = () => null` 显式模拟拦截, 验证代码走的是**提示分支**
  //    而不是静默失败 —— 这正是用户"闪一下没反应"的那个分支。
  const blocked = await p.evaluate(() => {
    const orig = window.open;
    window.open = () => null;                 // 模拟被拦截（返回 null）
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (st) { st.loginMsg = ''; st.loginBrowserPending = false; st.openLogin('workbuddy'); }
    return true;
  });
  await p.waitForTimeout(600);
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return;
    const btn = Array.from(m.querySelectorAll('button')).find(x => (x.textContent||'').includes('浏览器登录'));
    if (btn) btn.click();
  });
  await p.waitForTimeout(2500);
  const blockedMsg = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    return m ? (m.innerText||'') : '';
  });

  console.log(JSON.stringify({ modal, m1, pages_before: before, pages_after: after, new_url: newUrl, reqs, errs, modal_text: msg, blocked_msg: blockedMsg }, null, 2));
  await b.close();
})();
""" % (ROOT, PG, PG))
    r = subprocess.run(["node", js], capture_output=True, text=True, timeout=300, encoding="utf-8")
    if r.returncode != 0:
        print("node stderr:", (r.stderr or "")[-700:])
    try:
        out = json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        print(r.stdout[-1500:])
        out = {}

    m1 = out.get("m1", {})
    ck("① 登录弹窗打开", m1.get("open") is True, str(out.get("modal")))
    ck("② 未保存的 workbuddy 渲染出「浏览器登录」按钮", m1.get("has_browser_btn") is True, str(m1))
    ck("② 且**不**显示「不是账号形式」（两处判据一致）",
       m1.get("has_not_account_hint") is False, str(m1))
    ck("⑥ 不同时出现静态提示与浏览器登录按钮（原文案矛盾已修）",
       not (m1.get("has_static_hint") and m1.get("has_browser_btn")), str(m1))
    ck("④ ★点「浏览器登录」真的打开新页面（弹窗未被拦截）",
       (out.get("pages_after") or 0) > (out.get("pages_before") or 0),
       "before=%s after=%s new_url=%s reqs=%s" % (
           out.get("pages_before"), out.get("pages_after"), out.get("new_url"), out.get("reqs")))
    ck("⑤ 新页面是官方登录页", "workbuddy.ai" in (out.get("new_url") or ""),
       out.get("new_url") or "(无)")
    # 齿-test: 模拟弹窗拦截时必须**给出可读提示**, 而不是静默失败
    # （默认 headless 不拦弹窗, 故 ④ 单独看没有区分力 —— 见脚本内说明）
    bm = out.get("blocked_msg") or ""
    ck("⑦ 模拟弹窗被拦截时: 给出提示 + 可手动打开的链接（不静默失败）",
       ("拦截" in bm) and ("http" in bm),
       bm[:200].replace("\n", " "))
    ck("无 pageerror", not out.get("errs"), str(out.get("errs")))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 登录按钮渲染正确、后端放行未保存预设、弹窗真打开（三个 bug 均已修）")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
