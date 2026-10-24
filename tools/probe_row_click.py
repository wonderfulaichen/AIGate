# -*- coding: utf-8 -*-
"""验收：供应商列表整行/整卡可点，且**内部按钮不会误触**.

这是"加了整行可点就坏掉内部按钮"的经典坑：如果内部按钮不 @click.stop，
点「测试」会同时触发整行动作 —— 用户看到抽屉/展开区突然弹出，以为按钮坏了。

判据:
  ① 点表格行空白处 → 抽屉打开（表格视图保持"开抽屉"）
  ② 点卡片空白处 → **行内展开**（2026-10-24 改为行内展开，对照 WaLiAPI；
     抽屉改由「编辑」按钮进入 —— 那条路由 probe_prov_expand.py 的 ⑥ 覆盖）
  ③ 点「测试」按钮 → 抽屉**不打开**
  ④ 点「⋯」按钮 → 抽屉不打开，但菜单本身能开
  ⑤ 零 pageerror

⚠️ 踩过的坑（两次假失败都源于此）：
  页面里共有 4 张 table（记录/分析/供应商各一张，靠 x-show 隐藏），
  `document.querySelector('table tbody tr')` 抓到的是**隐藏的记录表**。
  必须只取可见那张 —— 用一个页面内辅助函数 `__pickRow()`，在每次 evaluate 里重建
  （不能用 addInitScript：它在导航后才注入，页面脚本早跑完了）。
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
  p.on('pageerror', e => errs.push(String(e).slice(0,250)));
  await p.goto('%(base)s/admin#providers', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3600);

  const out = {};
  const st = () => document.querySelector('[x-data]')._x_dataStack[0];

  // 表格视图
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].provViewMode = 'table'; });
  await p.waitForTimeout(900);
  out.beforeAny = await p.evaluate(() => !!document.querySelector('[x-data]')._x_dataStack[0].drawerProv);

  // ① 点行内「无按钮、无 @click」的单元格
  out.row = await p.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    let target = null, cellIdx = -1;
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    for (const t of tables) {
      const tr = Array.from(t.querySelectorAll('tbody tr')).find(vis);
      if (!tr) continue;
      const tds = Array.from(tr.querySelectorAll('td'));
      // 挑既没有 @click 也不含 button 的格
      const plain = tds.find(td => !td.getAttribute('@click') && !td.querySelector('button'));
      if (plain) { target = plain; cellIdx = tds.indexOf(plain); }
      break;
    }
    if (!target) return { noRow: true };
    target.click();
    return { clicked: true, cellIdx };
  });
  await p.waitForTimeout(700);
  out.rowOpened = await p.evaluate(() => !!document.querySelector('[x-data]')._x_dataStack[0].drawerProv);
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].drawerProv = null; });
  await p.waitForTimeout(500);

  // ③ 点「测试」按钮
  out.test = await p.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    for (const t of tables) {
      const tr = Array.from(t.querySelectorAll('tbody tr')).find(vis);
      if (!tr) continue;
      const btns = Array.from(tr.querySelectorAll('button'));
      const test = btns.find(x => /测试|⚡/.test(x.textContent || ''));
      if (!test) return { noBtn: true };
      test.click();
      return { clicked: true };
    }
    return { noTable: true };
  });
  await p.waitForTimeout(800);
  out.testOpened = await p.evaluate(() => !!document.querySelector('[x-data]')._x_dataStack[0].drawerProv);
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].drawerProv = null; });
  await p.waitForTimeout(400);

  // 卡片视图
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].provViewMode = 'card'; });
  await p.waitForTimeout(900);
  // ② 点卡片左上角留白
  out.card = await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    if (!card) return { noCard: true };
    const r = card.getBoundingClientRect();
    // 用真实坐标点击左上角内边距处（避开任何子元素）
    return { x: Math.round(r.left + 6), y: Math.round(r.top + 6) };
  });
  if (out.card && out.card.x) {
    await p.mouse.click(out.card.x, out.card.y);
    await p.waitForTimeout(700);
  }
  out.cardOpened = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    // 2026-10-24 行为变更：点卡片 = **行内展开**（不是开抽屉）。
    // 抽屉改由「编辑」按钮进入（见 probe_prov_expand.py 的 ⑥）。
    const names = Object.keys(st.provExpanded || {});
    return { drawer: !!st.drawerProv, expanded: names.length > 0, keys: names };
  });
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv = null; st.provExpanded = {};
  });
  await p.waitForTimeout(500);

  // ④ 点「⋯」
  out.more = await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    if (!card) return { noCard: true };
    const btns = Array.from(card.querySelectorAll('.plaza-acts button'));
    const more = btns.find(x => (x.textContent||'').trim() === '⋯');
    if (!more) return { noBtn: true };
    more.click();
    return { clicked: true };
  });
  await p.waitForTimeout(700);
  out.moreOpened = await p.evaluate(() => !!document.querySelector('[x-data]')._x_dataStack[0].drawerProv);
  out.menuVisible = await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    const pop = card ? card.querySelector('.cols-pop') : null;
    return !!(pop && pop.getBoundingClientRect().height > 0);
  });

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
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_rowclick")
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
            print(f"{BAD}  实例未就绪")
            return 1
        d = pdir()
        if not d:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(d, "probe_rowclick_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=220, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}")
            return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)}")
        print()

        check(res.get("beforeAny") is False, "① 初始抽屉是关的（前提）", "")
        check(res.get("rowOpened") is True,
              "① **点表格行空白处 → 抽屉打开**（300px 死热区变主入口）",
              f"row={res.get('row')} opened={res.get('rowOpened')}")
        check(res.get("testOpened") is False,
              "③ 点「测试」按钮 → **抽屉不打开**（@click.stop 生效）",
              f"opened={res.get('testOpened')}")
        co = res.get("cardOpened") or {}
        check(co.get("expanded") is True and co.get("drawer") is False,
              "② 点卡片空白处 → **行内展开**（不是开抽屉，2026-10-24 变更）",
              f"expanded={co.get('expanded')} drawer={co.get('drawer')} keys={co.get('keys')}")
        check(res.get("moreOpened") is False,
              "④ 点「⋯」按钮 → 抽屉不打开", f"opened={res.get('moreOpened')}")
        check(res.get("menuVisible") is True,
              "④ 「⋯」菜单本身能正常打开", f"visible={res.get('menuVisible')}")
        check(not (res.get("errs") or []), "⑤ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
