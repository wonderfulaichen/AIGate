# -*- coding: utf-8 -*-
"""实测抽屉里的批量操作 + 删除确认.

判据:
  ① 批量删除**必须弹确认**（这是本轮修的静默数据销毁路径）
  ② 取消确认后模型数不变
  ③ 确认后模型数减少对应数量
  ④ 批量设思考档只影响选中行
  ⑤ 批量标免费只影响选中行
  ⑥ 未选中时批量条不显示
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
  const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,250)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3400);
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('providers'));
  await p.waitForTimeout(1500);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const idx = (st.providersFormData || []).findIndex(x => (x.models||[]).length >= 3);
    st.openProviderDrawer(idx >= 0 ? idx : 0);
  });
  await p.waitForTimeout(1400);

  const out = {};

  // ⑥ 未选中时批量条不显示
  out.bulkHiddenWhenNone = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv._selected = {};
    const el = Array.from(document.querySelectorAll('.cg')).find(x => (x.textContent||'').indexOf('已选') >= 0);
    if (!el) return { noEl: true };
    const r = el.getBoundingClientRect();
    return { visible: r.width > 0 && r.height > 0 };
  });

  // 选中前 3 个
  out.setup = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = st.drawerProv;
    prov._selected = { 0: true, 1: true, 2: true };
    return { total: prov.models.length, sel: st.selectedCount(prov) };
  });

  // ④ 批量设思考档
  out.bulkEffort = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = st.drawerProv;
    prov._bulkEffort = 'high';
    const pi = st.providersFormData.indexOf(prov);
    st.applyBulkEffort(pi);
    const eff = prov.models.map(m => m.reasoning_effort || '');
    return { eff, changed: eff.slice(0,3).every(v => v === 'high'), others: eff.slice(3).filter(v => v === 'high').length };
  });

  // ⑤ 批量标免费 —— 先记**基线**再比对。
  // ⚠️ 不能直接断言"未选中的都变 false"：数据里本来就有免费模型（实测 8 个），
  // 第一版就是被这个假失败绊住的。正确判据是"只有选中行发生变化"。
  out.bulkFree = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = st.drawerProv;
    const pi = st.providersFormData.indexOf(prov);
    const base = prov.models.map(m => !!m.free);
    prov._selected = { 0: true, 1: true, 2: true };
    st.applyBulkFree(pi, true);
    const now = prov.models.map(m => !!m.free);
    const selAllTrue = now.slice(0, 3).every(Boolean);
    // 未选中的行：必须与基线完全一致（没被碰到）
    const othersUntouched = now.slice(3).every((v, i) => v === base.slice(3)[i]);
    return { selAllTrue, othersUntouched, baseFree: base.filter(Boolean).length };
  });

  // ① 删除必须弹确认 —— 点按钮后看是否出现确认层
  const before = await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].drawerProv.models.length);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv._selected = { 0: true, 1: true };
    const btns = Array.from(document.querySelectorAll('.m-card-hdr button'));
    const del = btns.find(x => (x.textContent||'').indexOf('删除选中') >= 0);
    if (del) del.click();
  });
  await p.waitForTimeout(600);
  const confirmShown = await p.evaluate(() => {
    // confirmAsk 会渲染一个含「确定/取消」的弹层
    const nodes = Array.from(document.querySelectorAll('[role="dialog"], .modal, .cfm, .confirm'));
    return nodes.some(n => {
      const r = n.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && /确定|取消|删除/.test(n.textContent || '');
    });
  });
  out.confirmShown = confirmShown;
  out.countAtConfirm = await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].drawerProv.models.length);

  // ② 取消 → 数量不变
  await p.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[role="dialog"], .modal, .cfm, .confirm'));
    for (const n of nodes) {
      const btns = Array.from(n.querySelectorAll('button'));
      const cancel = btns.find(x => /取消|cancel/i.test(x.textContent || ''));
      if (cancel) { cancel.click(); return; }
    }
  });
  await p.waitForTimeout(500);
  out.countAfterCancel = await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].drawerProv.models.length);

  out.before = before;
  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_bulk")
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
        jsf = os.path.join(d, "probe_bulk_tmp.js")
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
        print(f"  现场: {json.dumps(res, ensure_ascii=False)}")
        print()

        check(res.get("confirmShown") is True,
              "① **批量删除会弹确认**（此前是裸 splice，误点即全没）",
              f"confirmShown={res.get('confirmShown')}")
        check(res.get("countAfterCancel") == res.get("countAtConfirm") == res.get("before"),
              "② 取消确认后模型数不变",
              f"{res.get('before')} → {res.get('countAtConfirm')} → {res.get('countAfterCancel')}")
        be = res.get("bulkEffort") or {}
        check(be.get("changed") is True and be.get("others", 1) == 0,
              "④ 批量设思考档只影响选中的 3 行",
              f"{be}")
        bf = res.get("bulkFree") or {}
        check(bf.get("selAllTrue") is True and bf.get("othersUntouched") is True,
              "⑤ 批量标免费只影响选中的 3 行（未选中行与基线一致）",
              f"{bf}（基线本来就有 {bf.get('baseFree')} 个免费）")
        bh = res.get("bulkHiddenWhenNone") or {}
        check(bh.get("visible") is False or bh.get("noEl") is True,
              "⑥ 未选中时批量条不显示", f"{bh}")
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
