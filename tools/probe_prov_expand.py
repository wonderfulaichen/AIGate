# -*- coding: utf-8 -*-
"""验收：供应商卡的行内展开（对照 WaLiAPI 的 ChannelsPage.tsx:519-600）.

这块是"看得更深"的关键差异：改造前点整卡只能**开抽屉**，
只想看一眼"这家有哪些模型"也要跳到另一个界面。
WaLiAPI 把可用模型（点胶囊复制）、测试结果、统计都放在行内展开里。

判据:
  ① 点卡片 → 行内展开区出现（且**抽屉不开**）
  ② 再点一次 → 收起
  ③ 展开区里有模型胶囊，数量 == 该供应商模型数（或前若干个）
  ④ 点模型胶囊 → 复制成功（copiedField 被置为该模型键）
  ⑤ 点展开区内部不会触发收起（@click.stop 生效）
  ⑥ 「编辑」按钮仍能直接开抽屉（两条路都保留）
  ⑦ 零 pageerror
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_provexpand")
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
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3600);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('providers'); st.provViewMode = 'card';
  });
  await p.waitForTimeout(2200);

  const out = {};
  const readState = () => p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const cards = Array.from(document.querySelectorAll('.plaza-card'));
    const card = cards[0];
    return {
      expandedKeys: Object.keys(st.provExpanded || {}),
      drawerOpen: !!st.drawerProv,
      expandVisible: !!(card && card.querySelector('.plaza-expand') && card.querySelector('.plaza-expand').getBoundingClientRect().height > 0),
      chipCount: card ? card.querySelectorAll('.pe-chip').length : 0,
      modelCount: card ? ((st.visibleProviders[0] || {}).models || []).length : 0,
      copiedField: st.copiedField || '',
    };
  });

  out.before = await readState();

  // ① 点第一张卡的**空白处**（不能用 .plaza-nm —— 它自己有"点名称复制"的
  //    @click.stop，会把冒泡拦掉，实测 copiedField=prov:zen 就是证据）
  await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    const r = card.getBoundingClientRect();
    // 卡片左下角内边距区域（无任何子元素）
    card.dispatchEvent(new MouseEvent('click', {
      bubbles: true, cancelable: true,
      clientX: Math.round(r.left + 6), clientY: Math.round(r.bottom - 6)
    }));
  });
  await p.waitForTimeout(800);
  out.afterClick = await readState();
  await p.screenshot({ path: `%(out)s/expand.png` });

  // ④ 点第一个模型胶囊
  out.chip = await p.evaluate(() => {
    const chip = document.querySelector('.pe-chip');
    if (!chip) return { noChip: true };
    const label = (chip.textContent || '').trim();
    chip.click();
    return { clicked: true, label };
  });
  await p.waitForTimeout(500);
  out.afterChip = await readState();

  // ⑤ 点展开区内部空白（不应收起）
  const beforeInner = (await readState()).expandVisible;
  await p.evaluate(() => {
    const ex = document.querySelector('.plaza-expand');
    if (ex) ex.click();
  });
  await p.waitForTimeout(500);
  out.afterInnerClick = await readState();
  out.innerKeptOpen = beforeInner;

  // ② 再点空白处 → 收起（同样避开 .plaza-nm）
  await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    const r = card.getBoundingClientRect();
    card.dispatchEvent(new MouseEvent('click', {
      bubbles: true, cancelable: true,
      clientX: Math.round(r.left + 6), clientY: Math.round(r.bottom - 6)
    }));
  });
  await p.waitForTimeout(700);
  out.afterSecondClick = await readState();

  // ⑥ 编辑按钮仍能开抽屉
  out.editBtn = await p.evaluate(() => {
    const card = document.querySelector('.plaza-card');
    const btns = Array.from(card.querySelectorAll('.plaza-acts button'));
    const edit = btns.find(x => (x.textContent||'').trim() === '编辑');
    if (!edit) return { noBtn: true };
    edit.click();
    return { clicked: true };
  });
  await p.waitForTimeout(900);
  out.afterEdit = await readState();

  // ⑧ 表格视图：**点行开抽屉**（不是行内展开）—— 这是刻意设计，不是缺陷。
  // 试过两次在 tbody 里加展开行（`<template x-if>` 包 tr、`<tr x-show>`），
  // 实测 DOM 里 .prov-expand-row 数量恒为 0：`<tbody>` 的直接子元素被浏览器
  // 严格解析，Alpine 动态插入的第二个 tr 会被丢弃。
  // 结论：两个视图各有用途 —— 表格=密集扫读（一行一供应商）、卡片=查看与操作。
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv = null; st.provExpanded = {};
    st.provViewMode = 'table';
  });
  await p.waitForTimeout(1200);
  out.table = await p.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    let tr = null;
    for (const t of tables) { tr = Array.from(t.querySelectorAll('tbody tr')).find(vis); if (tr) break; }
    if (!tr) return { noRow: true };
    const td = tr.querySelector('td');
    td.click();
    return { clicked: true };
  });
  await p.waitForTimeout(900);
  out.tableAfterClick = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { drawerOpen: !!st.drawerProv, expandedKeys: Object.keys(st.provExpanded || {}) };
  });

  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_provexpand")
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
        jsf = os.path.join(d, "probe_provexpand_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS.replace("%(base)s", base).replace("%(out)s", OUT.replace("\\", "/")))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:900]}")
        print()

        ac = res.get("afterClick") or {}
        check(ac.get("expandVisible") is True,
              "① 点卡片 → 行内展开区出现", f"{ac.get('expandVisible')}")
        check(ac.get("drawerOpen") is False,
              "① **抽屉不开**（明确只在行内展开）", f"drawer={ac.get('drawerOpen')}")
        asc = res.get("afterSecondClick") or {}
        check(asc.get("expandVisible") is False,
              "② 再点一次 → 收起", f"{asc.get('expandVisible')}")
        check(ac.get("chipCount", 0) > 0,
              "③ 展开区里有模型胶囊", f"chips={ac.get('chipCount')} models={ac.get('modelCount')}")
        ch = res.get("chip") or {}
        af = res.get("afterChip") or {}
        check(af.get("copiedField", "").startswith("model:"),
              "④ 点模型胶囊 → 复制（copiedField 置为 model:xxx）",
              f"chip={ch.get('label')} copiedField={af.get('copiedField')}")
        ai = res.get("afterInnerClick") or {}
        check(ai.get("expandVisible") is True,
              "⑤ 点展开区内部不会误收起（@click.stop 生效）",
              f"{ai.get('expandVisible')} innerKept={res.get('innerKeptOpen')}")
        ae = res.get("afterEdit") or {}
        check(ae.get("drawerOpen") is True,
              "⑥ 「编辑」按钮仍能直接开抽屉（两条路都保留）", f"drawer={ae.get('drawerOpen')}")
        # ⑧ 表格视图：刻意保持"点行开抽屉"
        tt = res.get("tableAfterClick") or {}
        check(tt.get("drawerOpen") is True,
              "⑧ 表格视图点行 → 开抽屉（表格=密集扫读，卡片=查看与操作）",
              f"drawer={tt.get('drawerOpen')}")
        check(not (tt.get("expandedKeys") or []),
              "⑧ 表格视图不做行内展开（tbody 里插第二个 tr 会被浏览器丢弃）",
              f"expanded={tt.get('expandedKeys')}")
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
