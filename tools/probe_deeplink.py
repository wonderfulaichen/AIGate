# -*- coding: utf-8 -*-
"""期 4 验收：哈希深链（页签 / 分析子页 / 设置分区）.

为什么必须用浏览器实测:
  · "刷新后还在原页签"是运行时行为，静态看代码无法证明（init 顺序、异步 fetch
    都可能把页签改回去）；
  · "非法 hash 不会白屏"是**安全性**判据 —— 若直接把 hash 当页签用，
    用户手抖把地址改成 `#xyz` 就会得到一个空白面板。

判据:
  ① 切页签 → 地址栏 hash 同步
  ② **刷新后停在原页签**（不是回概览）
  ③ 直接打开 #analytics/cost → 分析页 + 省量与性能子页签
  ④ 直接打开 #settings/peak → 设置页 + 分时段计费分区
  ⑤ 切设置分区 / 分析子页签 → hash 跟着变
  ⑥ **非法 hash（#xyz）不白屏**，回落到概览且正文有内容
  ⑦ 零 pageerror

红线: 隔离实例 + 随机端口，绝不碰用户 8787。
"""
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
DEPLOY = r"D:\Office software\Development Project\AI中转"
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


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
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,300)));
  const BASE = '%(base)s/admin';
  const out = {};

  const state = () => p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab,
             analyticsTab: st.analyticsTab, settingsSection: st.settingsSection,
             bodyLen: (document.querySelector('main')||document.body).innerText.trim().length };
  });

  // ── ① 切页签 → hash 同步 ──
  await p.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3200);
  out.initial = await state();
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('records'));
  await p.waitForTimeout(700);
  out.afterSwitch = await state();

  // ── ② 刷新后停在原页签 ──
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('analytics'));
  await p.waitForTimeout(700);
  const beforeReload = await state();
  await p.reload({ waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(3200);
  out.afterReload = await state();
  out.beforeReload = beforeReload;

  // ── ③ 直接打开 #analytics/cost ──
  // ⚠️ 用**新页面**打开，不复用前面操作过的页面 —— 同一个 page 里 goto 到同域
  // 只改 hash 不会重新加载，读到的可能是上一轮残留状态（第一版就是这样假失败的）。
  const p3 = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p3.on('pageerror', e => errs.push('[deep-an] ' + String(e).slice(0,200)));
  await p3.goto(BASE + '#analytics/cost', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p3.waitForTimeout(3600);
  out.deepAnalytics = await p3.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab, analyticsTab: st.analyticsTab,
             settingsSection: st.settingsSection };
  });
  await p3.close();

  // ── ④ 直接打开 #settings/peak ──
  const p4 = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p4.on('pageerror', e => errs.push('[deep-set] ' + String(e).slice(0,200)));
  await p4.goto(BASE + '#settings/peak', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p4.waitForTimeout(3600);
  out.deepSettings = await p4.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab, analyticsTab: st.analyticsTab,
             settingsSection: st.settingsSection };
  });
  await p4.close();

  // ── ⑤ 点点看 hash 变不变（用干净页面）──
  const p5 = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p5.on('pageerror', e => errs.push('[click] ' + String(e).slice(0,200)));
  await p5.goto(BASE + '#settings/perf', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p5.waitForTimeout(3400);
  await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.setSettingsSection('monitor');
  });
  await p5.waitForTimeout(500);
  out.afterSectionClick = await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab, settingsSection: st.settingsSection,
             analyticsTab: st.analyticsTab };
  });
  await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('analytics');
    st.setAnalyticsTab('overview');
  });
  await p5.waitForTimeout(600);
  out.afterAnalyticsClick = await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab, analyticsTab: st.analyticsTab,
             settingsSection: st.settingsSection };
  });

  // ── ⑤b 切走再切回，子页应保留 ──
  // 这是"写 hash 时必须带子页"真正发挥作用的场景：
  // 停在 #settings/peak → 切到概览 → 再切回设置，应该还在 peak（而不是被重置成 perf）。
  await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('settings');
    st.setSettingsSection('peak');
  });
  await p5.waitForTimeout(500);
  await p5.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('dashboard'));
  await p5.waitForTimeout(600);
  await p5.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('settings'));
  await p5.waitForTimeout(700);
  out.sectionRoundTrip = await p5.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab, settingsSection: st.settingsSection };
  });
  await p5.close();

  // ── ⑥ 非法 hash 不白屏 ──
  // ⚠️ goto 到"同 URL 不同 hash"**不会重新加载**，只会触发 hashchange ——
  // 那样测到的是上一页的残留状态（第一版就在这里假失败）。必须开新页面。
  const p2 = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p2.on('pageerror', e => errs.push('[bogus] ' + String(e).slice(0,200)));
  await p2.goto(BASE + '#totally-bogus', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p2.waitForTimeout(3200);
  out.bogus = await p2.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { hash: location.hash, activeTab: st.activeTab,
             bodyLen: (document.querySelector('main')||document.body).innerText.trim().length };
  });
  await p2.close();

  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_deeplink")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    shutil.copy2(os.path.join(DEPLOY, "providers.json"), os.path.join(wd, "providers.json"))
    iso = os.path.join(wd, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port); env["AIGATE_DATA_DIR"] = os.path.join(DEPLOY, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=wd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read(); break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪"); return 1
        d = pdir()
        if not d:
            print(f"{BAD}  找不到 playwright"); return 1
        jsf = os.path.join(d, "probe_deeplink_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1

        for k in ("initial", "afterSwitch", "beforeReload", "afterReload",
                  "deepAnalytics", "deepSettings", "afterSectionClick",
                  "afterAnalyticsClick", "bogus"):
            print(f"  {k}: {json.dumps(res.get(k), ensure_ascii=False)}")
        print()

        check((res.get("afterSwitch") or {}).get("hash") == "#records",
              "① 切页签 → 地址栏 hash 同步",
              f"{(res.get('afterSwitch') or {}).get('hash')}")
        ar = res.get("afterReload") or {}
        # 刷新后页签必须还在。hash 允许带子页后缀（#analytics/overview 是合法形态 ——
        # switchTab 会把当前子页一起写进 hash，正是为了避免冲掉子页信息）。
        check(ar.get("activeTab") == "analytics"
              and str(ar.get("hash") or "").startswith("#analytics"),
              "② **刷新后停在原页签**（不是回概览）",
              f"activeTab={ar.get('activeTab')} hash={ar.get('hash')}")
        da = res.get("deepAnalytics") or {}
        check(da.get("activeTab") == "analytics" and da.get("analyticsTab") == "cost",
              "③ 直接打开 #analytics/cost → 分析页 + 省量与性能",
              f"{da.get('activeTab')}/{da.get('analyticsTab')}")
        ds = res.get("deepSettings") or {}
        check(ds.get("activeTab") == "settings" and ds.get("settingsSection") == "peak",
              "④ 直接打开 #settings/peak → 设置页 + 分时段计费",
              f"{ds.get('activeTab')}/{ds.get('settingsSection')}")
        check((res.get("afterSectionClick") or {}).get("hash") == "#settings/monitor",
              "⑤ 切设置分区 → hash 跟着变",
              f"{(res.get('afterSectionClick') or {}).get('hash')}")
        check((res.get("afterAnalyticsClick") or {}).get("hash") == "#analytics/overview",
              "⑤ 切分析子页签 → hash 跟着变",
              f"{(res.get('afterAnalyticsClick') or {}).get('hash')}")
        rt = res.get("sectionRoundTrip") or {}
        check(rt.get("settingsSection") == "peak" and rt.get("hash") == "#settings/peak",
              "⑤b 切走再切回，设置子页保留（写 hash 时带子页）",
              f"section={rt.get('settingsSection')} hash={rt.get('hash')}")
        bg = res.get("bogus") or {}
        check(bg.get("activeTab") == "dashboard" and (bg.get("bodyLen") or 0) > 100,
              "⑥ **非法 hash 不白屏**，回落概览且正文有内容",
              f"tab={bg.get('activeTab')} bodyLen={bg.get('bodyLen')}")
        check(not (res.get("errs") or []), "⑦ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()  # noqa: BLE001
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
