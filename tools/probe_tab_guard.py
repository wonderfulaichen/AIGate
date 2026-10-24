# -*- coding: utf-8 -*-
"""页签/子页签非法值必须被归一，不得让页面空白（2026-10-24 修）。

缺陷模式：**面板可见性由枚举值驱动**时，非法值不是"无操作"而是**全隐藏**。
`x-show="analyticsTab==='overview'"` / `x-show="settingsSection==='perf'"` 这类
写法下，把枚举写成不存在的值 → 所有面板都不显示 → 页面只剩头部，
用户看到的是"加载失败"而不是"页签不存在"。

实际触发途径（都不是理论问题）：
  · 删掉一个页签后，别处仍引用旧值（本轮删 `perf` 就差点如此）；
  · 旧书签 / 手改地址栏 `#settings/garbage`；
  · 外部拼接、控制台调用、将来的持久化。

判据：
  ① setAnalyticsTab('perf') 后仍有可见面板（不是空白）
  ② ① 之后 hash 不得写入非法值
  ③ setSettingsSection('garbage') 后设置内容区仍有可见面板
  ④ 打开 `#settings/garbage` 后设置分区被归一到合法值
  ⑤ 正常值不得被归一跳过（只认"非法被拦"，不能顺便把合法的也改掉）

用法: python tools/probe_tab_guard.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_tabguard")

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
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pdir():
    for rel in (".openbitfun/tmp/vidtools", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


JS = r"""
const { chromium } = require('playwright');

const VIS = `(el) => { let n = el; while (n && n !== document.body) {
  const cs = getComputedStyle(n);
  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  n = n.parentElement; } return true; }`;

(async () => {
  const b = await chromium.launch();
  const out = {};

  // ── ① ② setAnalyticsTab('perf') ──
  {
    const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
    const errs = [];
    p.on('pageerror', e => errs.push(String(e).slice(0, 160)));
    await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
    await p.waitForTimeout(6000);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('analytics'));
    await p.waitForTimeout(1500);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].setAnalyticsTab('perf'));
    await p.waitForTimeout(1600);
    out.ana = await p.evaluate(() => {
      const isVis = (el) => { let n = el; while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement; } return true; };
      const d = document.querySelector('[x-data]')._x_dataStack[0];
      const panels = Array.from(document.querySelectorAll('.pnl')).filter(isVis).length;
      const titles = Array.from(document.querySelectorAll('.pnl-ht')).filter(isVis)
        .map(x => (x.textContent || '').trim());
      return { tabValue: String(d.analyticsTab), hash: String(location.hash),
               visiblePanels: panels, titles: titles.slice(0, 6) };
    });
    out.anaErrs = errs;
    await p.close();
  }

  // ── ③ setSettingsSection('garbage') ──
  {
    const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
    await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
    await p.waitForTimeout(6000);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('settings'));
    await p.waitForTimeout(1500);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].setSettingsSection('garbage'));
    await p.waitForTimeout(1400);
    out.set = await p.evaluate(() => {
      const isVis = (el) => { let n = el; while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement; } return true; };
      const d = document.querySelector('[x-data]')._x_dataStack[0];
      const navItems = Array.from(document.querySelectorAll('.set-nav-i')).filter(isVis).length;
      const content = document.querySelector('.set-content');
      const contentPanels = content
        ? Array.from(content.querySelectorAll('.pnl')).filter(isVis).length : 0;
      return { sectionValue: String(d.settingsSection), hash: String(location.hash),
               navItems, contentPanels };
    });
    await p.close();
  }

  // ── ④ 直接打开 #settings/garbage ──
  {
    const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
    await p.goto(process.env.BASE + '/admin#settings/garbage',
                 { waitUntil: 'domcontentloaded', timeout: 25000 });
    await p.waitForTimeout(6000);
    out.hash = await p.evaluate(() => {
      const isVis = (el) => { let n = el; while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement; } return true; };
      const d = document.querySelector('[x-data]')._x_dataStack[0];
      const content = document.querySelector('.set-content');
      return { sectionValue: String(d.settingsSection),
               activeTab: String(d.activeTab),
               contentPanels: content
                 ? Array.from(content.querySelectorAll('.pnl')).filter(isVis).length : 0 };
    });
    await p.close();
  }

  // ── ⑤ 合法值不得被改掉 ──
  {
    const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
    await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
    await p.waitForTimeout(6000);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('analytics'));
    await p.waitForTimeout(1200);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].setAnalyticsTab('cost'));
    await p.waitForTimeout(1400);
    const a = await p.evaluate(() => String(document.querySelector('[x-data]')._x_dataStack[0].analyticsTab));
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('settings'));
    await p.waitForTimeout(1200);
    await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].setSettingsSection('peak'));
    await p.waitForTimeout(1400);
    const s = await p.evaluate(() => String(document.querySelector('[x-data]')._x_dataStack[0].settingsSection));
    out.valid = { analyticsTab: a, settingsSection: s };
    await p.close();
  }

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
    src_pv = os.path.join(DEPLOY_ROOT, "providers.json")
    if not os.path.isfile(src_pv):
        print(f"{BAD}  找不到 {src_pv}")
        return 1
    shutil.copy2(src_pv, os.path.join(WD, "providers.json"))
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(DEPLOY_ROOT, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    proc = subprocess.Popen([iso], env=env, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        jsf = os.path.join(WD, "t.js")
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
            print(f"{BAD}  页面脚本异常: {d['fatal'][:300]}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()

    print("页签/子页签非法值归一")
    ana = d.get("ana") or {}
    st = d.get("set") or {}
    hs = d.get("hash") or {}
    vd = d.get("valid") or {}
    print(f"  [现场] setAnalyticsTab('perf') -> {ana.get('tabValue')!r}, "
          f"可见面板 {ana.get('visiblePanels')}, hash={ana.get('hash')!r}")
    print(f"  [现场] setSettingsSection('garbage') -> {st.get('sectionValue')!r}, "
          f"内容面板 {st.get('contentPanels')}")
    print(f"  [现场] 打开 #settings/garbage -> {hs.get('sectionValue')!r}, "
          f"tab={hs.get('activeTab')!r}, 内容面板 {hs.get('contentPanels')}")

    check(ana.get("tabValue") in ("overview", "cost"),
          "① setAnalyticsTab('perf') 后被归一到合法页签",
          f"实际={ana.get('tabValue')!r}")
    check((ana.get("visiblePanels") or 0) > 0,
          "① 归一后分析页仍有可见面板（不是空白）",
          f"可见面板={ana.get('visiblePanels')}")
    check("perf" not in (ana.get("hash") or ""),
          "② 非法页签不得写进地址栏 hash",
          f"hash={ana.get('hash')!r}")
    check(st.get("sectionValue") in ("perf", "personal", "peak", "conn", "monitor"),
          "③ setSettingsSection('garbage') 后被归一到合法分区",
          f"实际={st.get('sectionValue')!r}")
    check((st.get("contentPanels") or 0) > 0,
          "③ 归一后设置内容区仍有可见面板",
          f"内容面板={st.get('contentPanels')}")
    check(hs.get("sectionValue") in ("perf", "personal", "peak", "conn", "monitor"),
          "④ 直接打开 #settings/garbage 后分区被归一",
          f"实际={hs.get('sectionValue')!r}")
    check((hs.get("contentPanels") or 0) > 0,
          "④ 归一后设置页有可见内容（不是只有左导航）",
          f"内容面板={hs.get('contentPanels')}")
    check(vd.get("analyticsTab") == "cost" and vd.get("settingsSection") == "peak",
          "⑤ 合法值不被归一误伤（cost / peak 保持不变）",
          f"实际={vd}")

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
