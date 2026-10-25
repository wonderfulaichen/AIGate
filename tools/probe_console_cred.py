# -*- coding: utf-8 -*-
"""Console 凭据录入探针（2026-10-24）。

钉住「凭据能被录入并落盘、且真的能查出额度」这条链路。
没有这条链路，额度功能对用户等于不可用（只能手改 providers.json）。

判据：
  ① 抽屉里有 Console Cookie（password 类型）与组织 ID 两个输入
  ② 输入后保存 → **落盘**（重新拉 /admin/api/providers 能读到）
  ③ 只填 Cookie 不填组织 ID → 查询时如实报错（不静默失败）
  ④ 空值不落盘（不能给每个供应商塞一堆空串）
  ⑤ 凭据不参与代理转发：没配凭据的供应商照样能保存/使用
  ⑥ 零 pageerror

为什么必须端到端：保存走的是 `ds-provsave.js` 的**白名单**，
不入白名单的字段会被静默丢弃（本项目踩过这个坑，见该文件内注释）。
单测证明不了白名单是否收录了新字段。

用法: python tools/probe_console_cred.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_consolecred")

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
  const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
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

  // ① 打开 go 的抽屉，检查两个输入是否存在且类型正确
  out.formFields = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const list = d.providersFormData || [];
    const pi = list.findIndex(x => x.name === 'go');
    if (pi < 0) return { err: 'providersFormData 里没有 go' };
    d.openProviderDrawer(pi);
    d.drawerAdvOpen = true;   // 高级区默认折叠，展开才能看到
    await new Promise(r => setTimeout(r, 900));
    const q = (sel) => Array.from(document.querySelectorAll(sel));
    // Cookie 输入已从 password input 改成 **textarea**（2026-10-24）。
    // 为什么改：Cookie 是一长串（st_... 几十字符，可能还带 auth=），
    // 单行 password 框会把它截断，用户容易以为没贴全。
    // 参考项目 QuotaHub 也用 Textarea。判据相应改为找 textarea。
    const cookieInput = q('textarea[aria-label]').find(i =>
      (i.getAttribute('aria-label') || '').includes('Console Cookie'));
    const orgInput = q('input[aria-label]').find(i =>
      (i.getAttribute('aria-label') || '').includes('组织 ID'));
    return {
      cookieFound: !!cookieInput,
      cookieType: cookieInput ? cookieInput.tagName.toLowerCase() : null,
      orgFound: !!orgInput,
    };
  });

  // ② 只填 Cookie、不填 org → **保存后**查询应报错
  //
  // ⚠️ 必须先保存：后端查额度读的是**已落盘的配置**，不是抽屉里的草稿。
  // 第一版只改 drawerProv 就查，后端读到的仍是旧配置（无 Cookie），
  // 于是永远报「未配置 Cookie」，把"缺 org"这条判据盖住了 —— 假失败。
  out.onlyCookie = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.console_session = '__Host-console_session=st_fake_for_test';
    d.drawerProv.console_org_id = '';
    d.markDirty();
    await d.saveProvidersForm();
    await d.fetchOpencodeQuota('go');
    return { err: d.ocQuotaErr('go') || '', quota: !!d.ocQuota('go') };
  });

  // ③ 填 org 但格式错（不以 wrk_ 开头）→ 同样要先保存再查
  out.badOrg = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.console_session = '__Host-console_session=st_fake_for_test';
    d.drawerProv.console_org_id = 'not-a-wrk-id';
    d.markDirty();
    await d.saveProvidersForm();
    await d.fetchOpencodeQuota('go');
    return { err: d.ocQuotaErr('go') || '' };
  });

  // ④ 填合法形状的凭据（假的 Cookie，真实 org）→ 保存 → 检查落盘
  out.saved = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.console_session = '__Host-console_session=st_saved_for_probe';
    d.drawerProv.console_org_id = 'wrk_01KS5PNNQH9TAEBFMNJ5C8AF07';
    d.markDirty();
    await d.saveProvidersForm();
    // 重新拉一次，确认写进磁盘（而不是只在内存里）
    const r = await fetch('/admin/api/providers', { headers: authHeaders() });
    const j = await r.json();
    const go = ((j.parsed && j.parsed.providers) || []).find(x => x.name === 'go') || {};
    return {
      session: go.console_session || '',
      org: go.console_org_id || '',
    };
  });

  // ⑤ 空值不落盘：清空后保存，配置里不应出现空串
  out.cleared = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const pi = (d.providersFormData || []).findIndex(x => x.name === 'go');
    d.providersFormData[pi].console_session = '';
    d.providersFormData[pi].console_org_id = '';
    if (d.drawerProv) { d.drawerProv.console_session = ''; d.drawerProv.console_org_id = ''; }
    d.markDirty();
    await d.saveProvidersForm();
    const r = await fetch('/admin/api/providers', { headers: authHeaders() });
    const j = await r.json();
    const go = ((j.parsed && j.parsed.providers) || []).find(x => x.name === 'go') || {};
    return {
      hasSession: Object.prototype.hasOwnProperty.call(go, 'console_session'),
      hasOrg: Object.prototype.hasOwnProperty.call(go, 'console_org_id'),
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
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    shutil.copy2(src, os.path.join(WD, "providers.json"))
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
        jsf = os.path.join(WD, "c.js")
        io.open(jsf, "w", encoding="utf-8").write(JS)
        env2 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env2, cwd=pd)
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

    ff = d.get("formFields") or {}
    oc = d.get("onlyCookie") or {}
    bo = d.get("badOrg") or {}
    sv = d.get("saved") or {}
    cl = d.get("cleared") or {}
    print("Console 凭据录入")
    print(f"  [现场] 抽屉字段: cookie={ff.get('cookieFound')}({ff.get('cookieType')}) org={ff.get('orgFound')}")
    print(f"  [现场] 只填 Cookie: err={oc.get('err')!r}")
    print(f"  [现场] org 格式错:  err={bo.get('err')!r}")
    print(f"  [现场] 保存后读回: session={sv.get('session')!r} org={sv.get('org')!r}")
    print(f"  [现场] 清空后: has_session={cl.get('hasSession')} has_org={cl.get('hasOrg')}")

    check(ff.get("cookieFound") is True, "① 抽屉有 Console Cookie 输入",
          str(ff))
    check(ff.get("cookieType") == "textarea",
          "① Cookie 用 textarea（长串不被视觉截断；QuotaHub 同款）",
          f"实际={ff.get('cookieType')}")
    check(ff.get("orgFound") is True, "① 抽屉有组织 ID 输入")

    check(bool(oc.get("err")), "③ 只填 Cookie 不填 org → 如实报错",
          f"err={oc.get('err')!r}")
    check("组织" in (oc.get("err") or "") or "org" in (oc.get("err") or "").lower(),
          "③ 报错内容指明缺的是组织 ID", f"err={oc.get('err')!r}")
    check(oc.get("quota") is False, "③ 缺凭据时不返回任何额度（不画假数据）")

    check(bool(bo.get("err")) and "wrk_" in (bo.get("err") or ""),
          "③ org 不以 wrk_ 开头 → 报错并说明格式",
          f"err={bo.get('err')!r}")

    # ② 落盘（这条守的是 ds-provsave 白名单是否收录了新字段）
    check("st_saved_for_probe" in (sv.get("session") or ""),
          "② Cookie 保存后**落盘**（重新读得到）",
          f"实际={sv.get('session')!r} —— 空说明 ds-provsave 白名单漏了字段")
    check(sv.get("org") == "wrk_01KS5PNNQH9TAEBFMNJ5C8AF07",
          "② 组织 ID 保存后落盘", f"实际={sv.get('org')!r}")

    # ④ 空值不落盘
    check(cl.get("hasSession") is False and cl.get("hasOrg") is False,
          "④ 清空后配置里不残留空串字段",
          f"has_session={cl.get('hasSession')} has_org={cl.get('hasOrg')}")

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
