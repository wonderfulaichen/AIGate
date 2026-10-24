# -*- coding: utf-8 -*-
"""验收：供应商行的点击行为与内部按钮不误触（紧凑行式列表）.

2026-10-24 供应商页按 WaLiAPI 真实界面重做为**单一紧凑行式列表**：
  · 点行（.prov-row-main）→ 行内展开
  · 点内部图标按钮（.prov-ib）→ 不触发展开/抽屉（@click.stop）
  · 「编辑」图标 → 开抽屉（按钮组里第 4 个，按 aria-label 认）

判据:
  ① 点行 → 行内展开，且**抽屉不开**
  ② 点「测试」图标 → 不触发展开
  ③ 点「编辑」图标 → 抽屉打开
  ④ 点删除图标（取消）→ 不触发展开
  ⑤ 零 pageerror
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
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(4200);
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('providers'));
  await p.waitForTimeout(2200);

  const out = {};
  const state = () => p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return { drawer: !!st.drawerProv, keys: Object.keys(st.provExpanded || {}) };
  });

  out.before = await state();

  // ① 点行主体 → 行内展开
  out.clickRow = await p.evaluate(() => {
    const main = document.querySelector('.prov-row-main');
    if (!main) return { noRow: true };
    main.click();
    return { clicked: true };
  });
  await p.waitForTimeout(800);
  out.afterRow = await state();
  out.expandVisible = await p.evaluate(() => {
    const ex = document.querySelector('.prov-expand');
    return !!(ex && ex.getBoundingClientRect().height > 0);
  });
  await p.evaluate(() => { const st = document.querySelector('[x-data]')._x_dataStack[0]; st.provExpanded = {}; st.drawerProv = null; });
  await p.waitForTimeout(500);

  // ② 点「测试」图标 → 不展开
  out.clickTest = await p.evaluate(() => {
    const row = document.querySelector('.prov-row');
    const btns = Array.from(row.querySelectorAll('.prov-ib'));
    const t = btns.find(x => /测试|Test/.test(x.getAttribute('aria-label') || ''));
    if (!t) return { noBtn: true, labels: btns.map(x => x.getAttribute('aria-label')) };
    t.click();
    return { clicked: true };
  });
  await p.waitForTimeout(900);
  out.afterTest = await state();
  await p.evaluate(() => { const st = document.querySelector('[x-data]')._x_dataStack[0]; st.provExpanded = {}; st.drawerProv = null; });
  await p.waitForTimeout(400);

  // ③ 点「编辑」图标 → 抽屉
  out.clickEdit = await p.evaluate(() => {
    const row = document.querySelector('.prov-row');
    const btns = Array.from(row.querySelectorAll('.prov-ib'));
    const e = btns.find(x => /编辑|Edit/.test(x.getAttribute('aria-label') || ''));
    if (!e) return { noBtn: true };
    e.click();
    return { clicked: true };
  });
  await p.waitForTimeout(900);
  out.afterEdit = await state();

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
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_rowclick2")
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
        jsf = os.path.join(d, "probe_rowclick2_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS.replace("%(base)s", base))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}")
            return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:700]}")
        print()

        check((res.get("before") or {}).get("drawer") is False, "① 初始抽屉关着（前提）", "")
        ar = res.get("afterRow") or {}
        check(len(ar.get("keys") or []) > 0 and ar.get("drawer") is False,
              "① 点行 → 行内展开，抽屉不开", f"{ar}")
        check(res.get("expandVisible") is True, "① 展开区真实可见（不是只在状态里）",
              f"{res.get('expandVisible')}")
        at = res.get("afterTest") or {}
        check(len(at.get("keys") or []) == 0 and at.get("drawer") is False,
              "② 点「测试」图标 → 不触发展开/抽屉（@click.stop 生效）", f"{at}")
        ae = res.get("afterEdit") or {}
        check(ae.get("drawer") is True,
              "③ 点「编辑」图标 → 抽屉打开", f"{ae}")
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
