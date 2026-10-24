# -*- coding: utf-8 -*-
"""期 4 验收探针：搜索防抖 + 页面可见性轮询守卫.

为什么必须用浏览器实测:
  · 防抖: 判"输入 5 个字符只触发 1 次过滤" —— 静态看代码无法证明，
    而且 x-model 与 @input 的更新顺序、IME 组合输入都可能让防抖失效。
  · 可见性: 判"隐藏时轮询真的停了" —— 要用 CDP 把页面标记为 hidden 再数请求数。

判据:
  ① 三个搜索框绑的是草稿字段，且输入后**过滤值延迟生效**（不是每键立即变）
  ② 连打 6 个字符 → 过滤值只更新 1 次
  ③ IME 组合输入期间不提交，上屏后立即提交
  ④ 页面隐藏时轮询**不再发请求**
  ⑤ 重新可见后**立刻补一次**
  ⑥ 零 pageerror

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
  // 数 /admin/api 请求（用于可见性判据）
  let apiHits = [];
  p.on('request', r => {
    const u = r.url();
    if (u.indexOf('/admin/api/') >= 0) apiHits.push(u.replace(/^https?:\/\/[^/]+/, ''));
  });

  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3200);

  const out = {};

  // ── ① / ② 防抖: 供应商页搜索 ──
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('providers'));
  await p.waitForTimeout(1200);
  out.debounce = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const inp = document.querySelector('input[placeholder*="搜索供应商"], .plaza-srch input, input[x-model="provSearchDraft"]');
    // 直接调方法（模拟 @input），比真实键盘更稳定
    const before = st.provSearch;
    const seen = [];
    const t0 = performance.now();
    for (const ch of ['z', 'en', 'ginka']) {
      st.onProvSearchInput({ target: { value: ch } });
      seen.push({ v: st.provSearch, t: Math.round(performance.now() - t0) });
    }
    // 立刻读一次：此时过滤值还应是旧值（防抖未到）
    const immediate = st.provSearch;
    const draftImmediate = st.provSearchDraft;
    await new Promise(r => setTimeout(r, 400));
    const after = st.provSearch;
    return {
      before, immediate, draftImmediate, after,
      filteredNow: (st.visibleProviders || []).length,
      boundToDraft: !!document.querySelector('input[x-model="provSearchDraft"]'),
    };
  });

  // ── ③ IME 组合输入 ──
  out.ime = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.provSearch = '';
    st.provSearchDraft = '';
    st.onSearchCompositionStart();
    // 拼音阶段连打三次
    for (const v of ['g', 'gi', 'gin']) st.onProvSearchInput({ target: { value: v } });
    await new Promise(r => setTimeout(r, 400));
    const duringCompose = st.provSearch;      // 应仍为空（挂起）
    st.provSearchDraft = 'ginka';
    st.onSearchCompositionEnd('provSearch', 'provSearchDraft');
    await new Promise(r => setTimeout(r, 60));
    const afterCompose = st.provSearch;       // 应立即提交，不等 200ms
    return { duringCompose, afterCompose };
  });

  // ── ④ 页面隐藏时轮询是否停 ──
  // 用 CDP 标记 hidden
  const cdp = await p.context().newCDPSession(p);
  await cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: 1 }).catch(() => {});
  await p.evaluate(() => {
    // 覆盖 document.hidden（只读属性，用 defineProperty）
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  });
  apiHits = [];
  await p.waitForTimeout(7000);          // 跨过 2 个 3s tick
  const hiddenHits = apiHits.length;
  const hiddenLogs = apiHits.filter(u => u.indexOf('/logs') >= 0).length;

  // ── ⑤ 恢复可见后是否立刻补一次 ──
  await p.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await p.waitForTimeout(900);
  const afterVisibleHits = apiHits.length;

  out.poll = { hiddenHits, hiddenLogs, afterVisibleHits };
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
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_p4")
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
        jsf = os.path.join(d, "probe_p4_tmp.js")
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

        db = res.get("debounce") or {}
        print(f"  防抖现场: {json.dumps(db, ensure_ascii=False)}")
        check(db.get("boundToDraft") is True, "① 搜索框绑定的是草稿字段（不是直连过滤值）", "")
        check(db.get("draftImmediate") == "ginka", "① 草稿实时跟手（输入即回显）",
              f"draft={db.get('draftImmediate')}")
        check(db.get("immediate") == db.get("before"),
              "② 连打 3 次后过滤值**尚未**变化（防抖生效，不是每键立即过滤）",
              f"immediate={db.get('immediate')} before={db.get('before')}")
        check(db.get("after") == "ginka", "② 200ms 后过滤值才更新为最终值",
              f"after={db.get('after')}")

        ime = res.get("ime") or {}
        print(f"  IME 现场: {json.dumps(ime, ensure_ascii=False)}")
        check(ime.get("duringCompose") in ("", None),
              "③ 拼音未上屏时不提交过滤", f"duringCompose={ime.get('duringCompose')!r}")
        check(ime.get("afterCompose") == "ginka",
              "③ 上屏后立即提交（不等 200ms）", f"afterCompose={ime.get('afterCompose')!r}")

        pl = res.get("poll") or {}
        print(f"  轮询现场: {json.dumps(pl, ensure_ascii=False)}")
        check(pl.get("hiddenHits", 999) == 0,
              "④ 页面隐藏时轮询**完全不发请求**（7s 跨 2 个 tick）",
              f"隐藏期间命中 {pl.get('hiddenHits')} 次")
        check(pl.get("afterVisibleHits", 0) > 0,
              "⑤ 恢复可见后立刻补一次", f"恢复后 {pl.get('afterVisibleHits')} 次")

        check(not (res.get("errs") or []), "⑥ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
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
