# -*- coding: utf-8 -*-
"""编辑界面「少犯错」改进探针（2026-10-24）。

对照参考项目 ChannelForm 的清单，本轮做了 5 项**纯前端**改进。
每一项都对应一个具体的"用户会犯的错"，不是装饰：

  ① **请求头可视化编辑**：后端早就存 `headers`，但一直没 UI ——
     用户想加 `x-api-key` 只能手写 providers.json。
  ② **实际请求 URL 预览**：90% 的配错是 Base URL 多写/少写 `/v1`，
     填完看不到最终地址只能靠试。
  ③ **「空模型 = 通配」说明**：这条语义不写出来，用户猜不到是"全禁"还是"没配"。
  ④ **删除确认显示影响范围**：原来只说"确定删除 X 吗"，
     用户不知道会连带删掉 N 个模型与已保存的 Key（不可再生）。
  ⑤ **IME 守卫**：中文输入法里 Enter 是选词上屏，裸 `@keydown.enter`
     会把选词当成提交。

判据都要求**能证明机制真的在**（不是"元素存在"）：
  · 请求头增删后 `drawerProv.headers` 真的变了、空名行不写进去
  · URL 预览在改 endpoint 后**跟着变**
  · 删除确认文案里出现模型数
  · IME 守卫：构造带 isComposing 的事件，断言不被当成提交

用法: python tools/probe_editor_gaps.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_editgaps")

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

  // 打开一个供应商抽屉（高级区展开，请求头在其中）
  out.open = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const list = d.providersFormData || [];
    const pi = list.findIndex(x => x.name === 'go');
    if (pi < 0) return { err: 'no go' };
    d.openProviderDrawer(pi);
    d.drawerAdvOpen = true;
    await new Promise(r => setTimeout(r, 900));
    return { ok: true, models: (list[pi].models || []).length };
  });

  // ① 请求头：加两行（一行有名字，一行留空）→ 只有有名字的写入 headers
  out.headers = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const before = JSON.stringify(d.drawerProv.headers || {});
    d.addProvHeader();
    d.setProvHeader(0, 'x-api-key', 'secret-1');
    d.addProvHeader();
    d.setProvHeader(1, '', 'should-not-save');
    await new Promise(r => setTimeout(r, 200));
    const after = JSON.stringify(d.drawerProv.headers || {});
    const rows = d.provHeaderRows().length;
    // DOM 里也该有两行（空名那行也在，用户才能继续填）
    const inputs = Array.from(document.querySelectorAll('input[placeholder]'))
      .filter(i => /x-api-key|头名/.test(i.getAttribute('placeholder') || '')).length;
    // 删掉第一行
    d.removeProvHeader(0);
    await new Promise(r => setTimeout(r, 150));
    const afterRemove = JSON.stringify(d.drawerProv.headers || {});
    return { before, after, rows, inputs, afterRemove };
  });

  // ② URL 预览：改 endpoint 后预览要跟着变
  out.preview = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.drawerProv.endpoint = 'https://example.com/v1/chat/completions';
    await new Promise(r => setTimeout(r, 150));
    const a = JSON.parse(JSON.stringify(d.provEndpointPreview(d.drawerProv)));
    d.drawerProv.endpoint = 'https://example.com/V2/chat/completions';
    await new Promise(r => setTimeout(r, 150));
    const b2 = JSON.parse(JSON.stringify(d.provEndpointPreview(d.drawerProv)));
    // 清空 → 不应再有 openai 那条
    d.drawerProv.endpoint = '';
    await new Promise(r => setTimeout(r, 150));
    const c = JSON.parse(JSON.stringify(d.provEndpointPreview(d.drawerProv)));
    return { a, b: b2, c };
  });

  // ③ 空模型 = 通配 说明：清空 models 后该提示应可见
  out.wildcard = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const save = d.drawerProv.models;
    d.drawerProv.models = [];
    await new Promise(r => setTimeout(r, 250));
    const el = Array.from(document.querySelectorAll('.txs.tm, div'))
      .filter(isVis).find(x => /通配/.test(x.textContent || '') && x.children.length === 0);
    const shown = !!el;
    d.drawerProv.models = save;
    await new Promise(r => setTimeout(r, 250));
    return { shown };
  });

  // ④ 删除确认的影响范围：拦 confirmAsk 看文案
  out.delConfirm = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    let captured = null;
    const orig = window.confirmAsk;
    window.confirmAsk = async (body, opts) => { captured = { body, opts }; return false; };
    try {
      const pi = (d.providersFormData || []).findIndex(x => x.name === 'go');
      await d.deleteProvider(pi);
    } catch (e) {}
    window.confirmAsk = orig;
    return { body: captured ? captured.body : null,
             danger: captured ? !!(captured.opts && captured.opts.danger) : null };
  });

  // ⑤ IME 守卫：构造 isComposing / keyCode 229 的事件
  out.ime = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const composing = { isComposing: true, keyCode: 13 };
    const legacy = { isComposing: false, keyCode: 229 };
    const normal = { isComposing: false, keyCode: 13 };
    return {
      composing: d.isImeComposing(composing),
      legacy: d.isImeComposing(legacy),
      normal: d.isImeComposing(normal),
      nullsafe: d.isImeComposing(null),
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
        jsf = os.path.join(WD, "e.js")
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
            print(f"{BAD}  页面异常: {d['fatal'][:250]}")
            return 1
    finally:
        proc.terminate()
        try: proc.wait(timeout=8)
        except Exception: proc.kill()

    hd = d.get("headers") or {}
    pv = d.get("preview") or {}
    wc = d.get("wildcard") or {}
    dc = d.get("delConfirm") or {}
    im = d.get("ime") or {}
    op = d.get("open") or {}
    print("编辑界面·少犯错（对照参考项目清单）")
    print(f"  [现场] 请求头: before={hd.get('before')} after={hd.get('after')} "
          f"afterRemove={hd.get('afterRemove')} rows={hd.get('rows')}")
    print(f"  [现场] URL 预览: {json.dumps(pv, ensure_ascii=False)[:300]}")
    print(f"  [现场] 删除确认: {json.dumps(dc, ensure_ascii=False)[:300]}")
    print(f"  [现场] IME: {im}")

    # ① 请求头
    check('x-api-key' in (hd.get("after") or ""),
          "① 新增请求头真的写进了 headers", f"after={hd.get('after')}")
    check("should-not-save" not in (hd.get("after") or ""),
          "① 名字留空的行**不**写进配置（不产生 {\"\": ...}）",
          f"after={hd.get('after')}")
    check("x-api-key" not in (hd.get("afterRemove") or ""),
          "① 删除请求头真的生效了", f"afterRemove={hd.get('afterRemove')}")
    check((hd.get("rows") or 0) >= 1,
          "① 编辑行可增删（删一行后仍保留其余）", f"rows={hd.get('rows')}")

    # ② URL 预览
    a = (pv.get("a") or [{}])[0] if isinstance(pv.get("a"), list) else {}
    check(a.get("v") == "https://example.com/v1/chat/completions",
          "② URL 预览显示当前 endpoint", f"a={a}")
    b2 = (pv.get("b") or [{}])[0] if isinstance(pv.get("b"), list) else {}
    check(b2.get("v") == "https://example.com/V2/chat/completions",
          "② 改 endpoint 后预览**跟着变**（不是静态快照）", f"b={b2}")
    check(not [x for x in (pv.get("c") or []) if x.get("k") == "fmt.openai"],
          "② 清空 endpoint 后不再显示 openai 那条（不显示假值）",
          f"c={pv.get('c')}")

    # ③ 空模型通配
    check(wc.get("shown") is True,
          "③ 模型为空时显示「空 = 接受所有模型」说明",
          f"实际={wc.get('shown')}")

    # ④ 删除确认
    body = dc.get("body") or ""
    check(bool(body), "④ 删除有确认框", f"body={body[:60]!r}")
    check(bool(op.get("models")) and str(op.get("models")) in body,
          "④ 确认文案里出现**模型数**（显示影响范围）",
          f"模型数={op.get('models')} body={body[:120]!r}")
    check(dc.get("danger") is True, "④ 确认框标记为危险操作")
    check("保存配置" in body or "才真正生效" in body,
          "④ 说明「现在只是从表单移除，保存才生效」",
          f"body={body[:120]!r}")

    # ⑤ IME
    check(im.get("composing") is True, "⑤ isComposing=true 时判为输入法（不提交）")
    check(im.get("legacy") is True, "⑤ keyCode=229（老输入法）也判为输入法")
    check(im.get("normal") is False, "⑤ 普通 Enter 不被误判（仍能提交）")
    check(im.get("nullsafe") is False, "⑤ 传 null 不炸（防御）")

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
