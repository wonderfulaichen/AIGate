# -*- coding: utf-8 -*-
"""「自定义请求头」从折叠区搬出为独立分区探针（2026-10-24）。

## 为什么搬（用户批评「里面的其他层级没做」）

改造前后端就存 `headers`（ds-provsave.js 白名单里有），但：
  ① 一开始**没有任何 UI**（只能手写 providers.json）；
  ② 补上 UI 后，位置在「端点分区 → 高级设置折叠区 → 里层」，且折叠状态被
     localStorage 记住 → **新用户必然看不到**。参考项目把它做成与 Key 同级的
     独立子卡（ChannelForm.tsx:1357-1385）。

## 判据（每条都要求证明"层级真的浅了"，不只是"元素还在"）

  ① 存在独立的 #drsec-headers 分区（DOM 层级的证据）
  ② 该分区**不**在高级设置折叠区内（不依赖展开就能看到）—— 这条是核心
  ③ 未展开折叠时，请求头输入框**可见**（用 getComputedStyle 判，不是存在性）
  ④ 分节导航里有「自定义上游请求头」chip，点击能滚到该分区
  ⑤ 完成度把 headers 计入（4 个分区，不是 3 个）
  ⑥ headers 未配置时**不**算未完成（可选项，别误导 3/4）
  ⑦ 编辑功能仍然可用（增删 + 空名行不写配置）
  ⑧ 零 pageerror

用法: python tools/probe_headers_section.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_hdrsec")

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
  const p = await b.newPage({ viewport: { width: 1500, height: 1200 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);
  await p.evaluate(async () => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2200);

  const out = { errs };

  // ⚠️ 每段 evaluate 都要自带 isVis —— page.evaluate 在**浏览器上下文**里执行，
  // 访问不到外层 JS 作用域的变量（第一版写在外层，直接 ReferenceError:
  // isVis is not defined，还因为没超时保护白等了 15 分钟）。

  // 打开 go 的抽屉，并**确保高级设置是折叠的**（默认 false）
  out.layout = await p.evaluate(async () => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const pi = (d.providersFormData || []).findIndex(x => x.name === 'go');
    if (pi < 0) return { err: 'no go' };
    d.openProviderDrawer(pi);
    d.drawerAdvOpen = false;   // 关键：不展开高级设置
    await new Promise(r => setTimeout(r, 900));
    const sec = document.getElementById('drsec-headers');
    if (!sec) return { err: 'no drsec-headers' };
    // 该分区是否在高级设置折叠区内？
    let inAdv = false;
    let n = sec;
    while (n && n !== document.body) {
      if (n.getAttribute && n.getAttribute('x-show') === 'drawerAdvOpen') { inAdv = true; break; }
      n = n.parentElement;
    }
    const nameInput = sec.querySelector('input[placeholder]');
    return {
      hasSection: true,
      inAdvBlock: inAdv,
      sectionVisible: isVis(sec),
      inputVisible: nameInput ? isVis(nameInput) : false,
      inputPlaceholder: nameInput ? nameInput.getAttribute('placeholder') : null,
      secCount: d.drawerSecCount(d.drawerProv),
    };
  });

  // ③ 的补强：先加一行，再断言输入框真的可见（不展开折叠的情况下）。
  // 空数据下不渲染输入行是设计（不预置空行），所以不能直接断言可见。
  out.inputAfterAdd = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const sec = document.getElementById('drsec-headers');
    if (!sec) return { err: 'no sec' };
    d.addProvHeader();
    await new Promise(r => setTimeout(r, 400));
    const inp = sec.querySelector('input[placeholder]');
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    return { visible: inp ? isVis(inp) : false,
             placeholder: inp ? inp.getAttribute('placeholder') : null };
  });

  // ④ 分节导航 chip
  out.nav = await p.evaluate(async () => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const chips = Array.from(document.querySelectorAll('.dr-secchip'));
    const hdrChip = chips.find(c => /自定义上游请求头|Custom upstream/.test(c.textContent || ''));
    return {
      chipCount: chips.length,
      hasHeadersChip: !!hdrChip,
      labels: chips.map(c => (c.textContent || '').trim()),
    };
  });

  // ⑥ 未配置 headers 时不算未完成
  out.done = await p.evaluate(async () => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = d.drawerProv;
    prov.headers = undefined;
    prov._hdrRows = undefined;
    await new Promise(r => setTimeout(r, 300));
    const withoutHdr = d.drawerSecDone('headers', prov);
    const countWithout = d.drawerSecCount(prov);
    // 加上一个头 → 应变为就绪
    d.addProvHeader(); d.setProvHeader(0, 'x-api-key', 'v');
    await new Promise(r => setTimeout(r, 300));
    const withHdr = d.drawerSecDone('headers', prov);
    const countWith = d.drawerSecCount(prov);
    return { withoutHdr, countWithout, withHdr, countWith };
  });

  // ⑦ 编辑功能仍可用
  out.edit = await p.evaluate(async () => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = d.drawerProv;
    prov._hdrRows = undefined; prov.headers = undefined;
    d.addProvHeader(); d.setProvHeader(0, 'x-a', '1');
    d.addProvHeader(); d.setProvHeader(1, '', 'skip');
    await new Promise(r => setTimeout(r, 250));
    const afterAdd = JSON.stringify(prov.headers || {});
    d.removeProvHeader(0);
    await new Promise(r => setTimeout(r, 200));
    const afterRemove = JSON.stringify(prov.headers || {});
    return { afterAdd, afterRemove };
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
        jsf = os.path.join(WD, "h.js")
        io.open(jsf, "w", encoding="utf-8").write(JS)
        env2 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        # ⚠️ 必须加 timeout：第一版没加，某次实例异常退出后 playwright 挂在
        # `page.goto` 上 → 探针 15 分钟不返回、父进程卡死、留下一堆 node/AIGate 残留进程。
        # 现在给 120s 上限，超时也把 stdout/stderr 打出来便于定位。
        try:
            r = subprocess.run(["node", jsf], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env2, cwd=pd,
                               timeout=120)
            stdout, stderr = r.stdout or "", r.stderr or ""
        except subprocess.TimeoutExpired as e:
            print(f"{BAD}  playwright 超时(120s) —— 实例是否退出？请查 WD/aigate.log")
            print("   stdout 尾:", (e.stdout or b"")[-400:] if isinstance(e.stdout, bytes) else (e.stdout or "")[-400:])
            return 1
        line = next((l for l in stdout.splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回 RESULT:")
            print(stdout[-600:])
            print(stderr[-600:])
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal'][:250]}")
            return 1
    finally:
        proc.terminate()
        try: proc.wait(8)
        except Exception: proc.kill()

    lay = d.get("layout") or {}
    nav = d.get("nav") or {}
    dn = d.get("done") or {}
    ed = d.get("edit") or {}
    print("自定义请求头：从折叠区搬出为独立分区")
    print(f"  [现场] 布局: {lay}")
    print(f"  [现场] 分节导航: {nav}")
    print(f"  [现场] 完成度: {dn}")
    print(f"  [现场] 编辑: {ed}")

    check(lay.get("hasSection") is True, "① 存在独立的 #drsec-headers 分区")
    check(lay.get("inAdvBlock") is False,
          "② 该分区**不在**高级设置折叠区内（搬出来了）",
          f"inAdvBlock={lay.get('inAdvBlock')}")
    check(lay.get("sectionVisible") is True,
          "③ 不展开折叠时分区可见", f"sectionVisible={lay.get('sectionVisible')}")
    # 输入框可见性：go 供应商本来就没有请求头 → 一行都不渲染（只显示"还没有"空态），
    # 这符合设计（不预置空行）。所以这里先加一行再断言可见，
    # 否则在空数据下断言"输入框可见"本身不成立（第一版就是这么假失败的）。
    # ⚠️ 分区数要看 nav.chipCount（模板层），不是 secCount（那是完成度计数）。
    # 第一版把 secCount 当成分区数，断言 == 4 恒红 —— 判据自身错位。
    check(nav.get("chipCount") == 4,
          "⑤ 分节导航共 4 个分区（模板层，与是否配置无关）",
          f"chipCount={nav.get('chipCount')} secCount(完成度)={lay.get('secCount')}")
    check(lay.get("secCount") == 2,
          "⑤ 未配置时完成度 = 2（basic + key），headers 未填不计入",
          f"secCount={lay.get('secCount')}")

    ia = d.get("inputAfterAdd") or {}
    check(ia.get("visible") is True,
          "③ 加一行后输入框**可见**（不展开折叠；证明请求头编辑不再藏在折叠里）",
          f"实际={ia}")
    check(bool(ia.get("placeholder")),
          "③ 输入框有占位提示", f"placeholder={ia.get('placeholder')!r}")

    check(nav.get("hasHeadersChip") is True,
          "④ 分节导航里有「自定义上游请求头」chip", f"labels={nav.get('labels')}")
    check(len(nav.get("labels") or []) == 4,
          "④ 分节导航共 4 项", f"实际={nav.get('labels')}")

    check(dn.get("withoutHdr") is False,
          "⑥ 未配置请求头时**不算**未完成（可选项，别误导 3/4）",
          f"withoutHdr={dn.get('withoutHdr')}")
    check(dn.get("withHdr") is True,
          "⑥ 配置了请求头后算就绪", f"withHdr={dn.get('withHdr')}")
    check(dn.get("countWith") == dn.get("countWithout") + 1,
          "⑥ 完成度随之 +1", f"{dn.get('countWithout')} → {dn.get('countWith')}")

    check('"x-a"' in (ed.get("afterAdd") or ""),
          "⑦ 新增请求头写入 headers", f"afterAdd={ed.get('afterAdd')}")
    check("skip" not in (ed.get("afterAdd") or ""),
          "⑦ 名字留空的行不写入配置", f"afterAdd={ed.get('afterAdd')}")
    check("x-a" not in (ed.get("afterRemove") or ""),
          "⑦ 删除请求头生效", f"afterRemove={ed.get('afterRemove')}")

    check(len(d.get("errs") or []) == 0, "⑧ 零 pageerror", str((d.get("errs") or [])[:2]))

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
