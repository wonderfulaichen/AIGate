# -*- coding: utf-8 -*-
"""opencode 凭据录入流程探针（2026-10-24）。

## 为什么这么设计（本轮实测后的结论）

用户问「codebuddy 能一键登录，opencode 为什么不行」。查清的原因是**协议层差异**：
  · codebuddy 有 `auth/token?state=` —— 服务端能拿 state 换 token；
  · opencode 是标准 OAuth(PKCE)，授权码只发给它自己的回调域名，服务端拿不到；
  · session 是 HttpOnly Cookie（JS 读不到）；
  · 额度端点不认 API key（Bearer → 401，与无凭据相同）；
  · wry 0.38 在已有事件循环里新建 WebView 无法初始化（URL 停在 about:blank）。
参考项目 QuotaHub **也是手贴 Cookie**，它做对的是"少犯错"：大 Textarea、
独立页面、保存后立即验证。

所以本探针钉的就是"少犯错"那几件事。

## 判据

  ① 凭据块在 **API Key 分区**（与 Key 同层），不在「高级设置」折叠里
     —— 用户批评过"谁会把登录藏得这么深"
  ② Cookie 用 textarea（长串不会被视觉截断）
  ③ 「自动获取」组织 ID 真的能拿到 wrk_（不要求用户去地址栏抠）
  ④ 「测试连接」用草稿凭据就能验证（不用先保存）
  ⑤ 测试失败时给出**可操作**的错误（Cookie 过期 / 组织格式错）
  ⑥ 零 pageerror

③④ 需要真实 Cookie：从本机 `.openbitfun/tmp/oc_profile` 取；
缺该 profile 时这两条**显式跳过**，不假装通过。

用法: python tools/probe_oc_cred_flow.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_occred")

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
  const ctx = await chromium.launchPersistentContext(process.env.PROFILE,
    { channel: 'msedge', headless: true });
  const cs = await ctx.cookies('https://opencode.ai');
  const w = cs.find(c => c.name === '__Host-console_session');
  await ctx.close();
  console.log('C ' + JSON.stringify(w ? ('__Host-console_session=' + w.value) : null));
})().catch(() => console.log('C null'));
"""

UI_JS = r"""
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

  // ① 打开 go 抽屉，检查凭据块位置与控件类型
  out.layout = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const pi = (d.providersFormData || []).findIndex(x => x.name === 'go');
    if (pi < 0) return { err: 'no go' };
    d.openProviderDrawer(pi);
    await new Promise(r => setTimeout(r, 900));
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const ta = Array.from(document.querySelectorAll('textarea')).filter(isVis);
    const cookieTa = ta.find(t => (t.getAttribute('aria-label') || '').includes('Console Cookie'));
    // 该 textarea 是否位于「API Key」分区内
    const keySec = document.getElementById('drsec-key');
    const inKeySec = !!(keySec && cookieTa && keySec.contains(cookieTa));
    // 是否在「高级设置」折叠区里
    const advVisible = d.drawerAdvOpen === true;
    const advBlock = Array.from(document.querySelectorAll('[x-show*="drawerAdvOpen"]'));
    const inAdv = advBlock.some(el => el.contains(cookieTa));
    return {
      hasCookieTextarea: !!cookieTa,
      inKeySection: inKeySec,
      inAdvBlock: inAdv,
      advOpen: advVisible,
      fetchOrgBtn: !!Array.from(document.querySelectorAll('button'))
        .filter(isVis).find(x => /自动获取|Auto-detect/.test(x.textContent || '')),
      verifyBtn: !!Array.from(document.querySelectorAll('button'))
        .filter(isVis).find(x => /测试连接|Test connection/.test(x.textContent || '')),
    };
  });

  // ② 用真实 Cookie 走「自动获取 → 测试连接」
  out.flow = await p.evaluate(async (cookie) => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    if (!cookie) return { skipped: true };
    d.drawerProv.console_session = cookie;
    d.drawerProv.console_org_id = '';
    await d.ocFetchOrgId(d.drawerProv);
    await new Promise(r => setTimeout(r, 400));
    const afterOrg = { org: d.drawerProv.console_org_id, msg: d.ocVerifyMsg, ok: d.ocVerifyOk };
    await d.ocVerifyCred(d.drawerProv);
    await new Promise(r => setTimeout(r, 400));
    return { afterOrg, verify: { msg: d.ocVerifyMsg, ok: d.ocVerifyOk } };
  }, process.env.COOKIE || '');

  // ③ 用假 Cookie 测错误提示是否可操作
  out.badCookie = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.console_session = '__Host-console_session=st_fake';
    d.drawerProv.console_org_id = 'wrk_01KS5PNNQH9TAEBFMNJ5C8AF07';
    await d.ocVerifyCred(d.drawerProv);
    await new Promise(r => setTimeout(r, 300));
    return { msg: d.ocVerifyMsg, ok: d.ocVerifyOk };
  });

  // ④ 组织 ID 格式错 -> 明确提示
  out.badOrg = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.console_session = '__Host-console_session=st_fake';
    d.drawerProv.console_org_id = 'not-a-wrk';
    await d.ocVerifyCred(d.drawerProv);
    await new Promise(r => setTimeout(r, 300));
    return { msg: d.ocVerifyMsg };
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

    # 真实 Cookie（有就用，没有就跳过 ③④）
    cookie = None
    if os.path.isdir(PROFILE):
        jsf = os.path.join(WD, "_c.js")
        io.open(jsf, "w", encoding="utf-8").write(GET_COOKIE_JS)
        env = dict(os.environ, NODE_PATH=os.path.join(pd, "node_modules"), PROFILE=PROFILE)
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("C ")), None)
        if line:
            cookie = json.loads(line[2:])
    print(f"[准备] 真实 Cookie: {'有' if cookie else '无（③④ 将跳过）'}")

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
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"),
                    COOKIE=cookie or "")
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

    lay = d.get("layout") or {}
    flow = d.get("flow") or {}
    bc = d.get("badCookie") or {}
    bo = d.get("badOrg") or {}
    print("\nopencode 凭据录入（QuotaHub 式：手贴 + 自动获取 + 立即验证）")
    print(f"  [现场] 布局: {lay}")
    print(f"  [现场] 流程: {json.dumps(flow, ensure_ascii=False)[:400]}")
    print(f"  [现场] 假 Cookie: {bc}")
    print(f"  [现场] 坏组织 ID: {bo}")

    # ① 位置：必须在 Key 分区、不在高级折叠里
    check(lay.get("hasCookieTextarea") is True, "① Cookie 输入存在")
    check(lay.get("inKeySection") is True,
          "① 凭据块在「API Key」分区内（与 Key 同层，不再藏进高级设置）",
          f"inKeySection={lay.get('inKeySection')}")
    check(lay.get("inAdvBlock") is False,
          "① 凭据块**不在**「高级设置」折叠区里（新用户不必展开也能看到）",
          f"inAdvBlock={lay.get('inAdvBlock')}")
    check(lay.get("fetchOrgBtn") is True, "① 有「自动获取」组织 ID 按钮")
    check(lay.get("verifyBtn") is True, "① 有「测试连接」按钮")

    if flow.get("skipped"):
        print(f"  {SKIP}  ③④ 本机无 opencode 登录态，跳过真实凭据验证")
    else:
        ao = flow.get("afterOrg") or {}
        check(bool(ao.get("org")) and str(ao.get("org")).startswith("wrk_"),
              "③ 「自动获取」真的拿到了 wrk_ 组织 ID（用户不用去地址栏抠）",
              f"实际={ao.get('org')!r} msg={ao.get('msg')!r}")
        v = flow.get("verify") or {}
        check(v.get("ok") is True,
              "④ 「测试连接」用草稿凭据就验证成功（不用先保存）",
              f"msg={v.get('msg')!r}")
        if v.get("msg"):
            check("%" in v["msg"] or "已用" in v["msg"] or "%" in v["msg"],
                  "④ 成功提示里带上了本月用量（用户一眼知道通了）",
                  f"实际={v.get('msg')!r}")

    check(bc.get("ok") is False and bool(bc.get("msg")),
          "⑤ 假 Cookie → 明确失败且有提示", f"实际={bc}")
    check("过期" in (bc.get("msg") or "") or "无效" in (bc.get("msg") or "")
          or "401" in (bc.get("msg") or ""),
          "⑤ 失败提示指明是 Cookie 问题（可操作）",
          f"实际={bc.get('msg')!r}")
    check("wrk_" in (bo.get("msg") or ""),
          "⑤ 组织 ID 格式错 → 提示要求 wrk_ 开头",
          f"实际={bo.get('msg')!r}")

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
