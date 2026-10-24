# -*- coding: utf-8 -*-
"""验收：供应商拖拽排序（对照 WaLiAPI 的 ChannelsPage.tsx:293-330）.

关键背景：providers.json 里是**数组**，顺序即优先级（后端注释明确）。后端保存时
**原样写盘**（admin.rs:1569-1575 直接 write 前端传来的 json_str，不重排），
所以改表单数组顺序再保存即可 —— 不需要新增后端接口。

判据:
  ① 表格行可拖拽（draggable=true）且带手柄
  ② 上移/下移按钮（键盘可达的替代：拖拽对键盘用户不可达）
  ③ 点下移 → 顺序真的变了（不是只改 UI 标记）
  ④ 顺序变化后 isDirty 置位（提示要保存）
  ⑤ **保存后顺序持久化**（写回 providers.json）
  ⑥ 卡片视图同样可拖拽
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_reorder")
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
  await p.waitForTimeout(4200);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('providers'); st.provViewMode = 'table';
  });
  await p.waitForTimeout(1800);

  const out = {};
  const readOrder = () => p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return (st.providersFormData || []).map(x => x.name);
  });

  // ① 表格行可拖 + 手柄
  out.grip = await p.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    let tr = null;
    for (const t of tables) { tr = Array.from(t.querySelectorAll('tbody tr')).find(vis); if (tr) break; }
    if (!tr) return { noRow: true };
    return {
      draggable: tr.getAttribute('draggable'),
      hasGrip: !!tr.querySelector('.pv-grip'),
      moveBtns: tr.querySelectorAll('.pv-mv').length,
    };
  });

  out.orderBefore = await readOrder();

  // ③ 把第 1 个下移
  out.move = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const firstName = (st.providersFormData||[])[0].name;
    st.provMove(firstName, 1);
    return { moved: firstName };
  });
  await p.waitForTimeout(700);
  out.orderAfterMove = await readOrder();
  out.dirtyAfterMove = await p.evaluate(() => !!document.querySelector('[x-data]')._x_dataStack[0].isDirty);
  await p.screenshot({ path: `%(out)s/table_reorder.png` });

  // ⑤ 保存到磁盘：调用保存并读回 providers.json
  out.saved = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    await st.saveProvidersForm();
    return { msg: st.providersFormMsg || '' };
  });
  await p.waitForTimeout(1200);
  out.orderAfterSave = await readOrder();

  // ⑥ 卡片视图也可拖
  out.cardDrag = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.provViewMode = 'card';
    return new Promise(res => setTimeout(() => {
      const c = document.querySelector('.plaza-card');
      res({ draggable: c ? c.getAttribute('draggable') : null });
    }, 900));
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
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_reorder")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    # 复制配置（不污染用户目录）
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
        jsf = os.path.join(d, "probe_reorder_tmp.js")
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
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:800]}")
        print()

        g = res.get("grip") or {}
        check(g.get("draggable") == "true", "① 表格行可拖拽", f"{g}")
        check(g.get("hasGrip") is True, "① 有拖拽手柄", f"hasGrip={g.get('hasGrip')}")
        check(g.get("moveBtns") == 2, "② 有上移/下移按钮（键盘可达的替代）", f"btns={g.get('moveBtns')}")
        ob = res.get("orderBefore") or []
        oa = res.get("orderAfterMove") or []
        check(ob and oa and ob[0] == oa[1] and ob[1] == oa[0],
              "③ 下移后顺序真的变了", f"before={ob[:3]} after={oa[:3]}")
        check(res.get("dirtyAfterMove") is True, "④ 顺序变化后标记未保存", "")
        os_ = res.get("orderAfterSave") or []
        check(os_ and os_ == oa,
              "⑤ **保存后顺序保持**（没有被引擎重排）", f"afterSave={os_[:3]}")
        cd = res.get("cardDrag") or {}
        check(cd.get("draggable") == "true", "⑥ 卡片视图也可拖拽", f"{cd}")
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
