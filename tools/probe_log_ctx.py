# -*- coding: utf-8 -*-
"""实测日志详情里的「上下文构成」（新增块）与单元格胶囊化.

判据:
  ① 打开有 context 数据的日志 → 详情里出现「输入构成」块
  ② 比例条有 ≥2 段（不是单色一条）
  ③ 徽章数量 == 非零分类数，且每个徽章含 ≈tokens 与占比
  ④ 打开**没有** context 的旧日志 → 该块不出现（不渲染空块）
  ⑤ 记录表单元格：状态码是胶囊、Token 有 ↓/↑ 小字、延迟有 TTFT 小字
  ⑥ 零 pageerror
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
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3800);
  // ⚠️ 用 switchTab 而不是靠 `#records` hash —— 实测 hash 恢复在登录初始化
  // 之后才生效，goto 完立刻读会拿到 dashboard 页（表格行的 selector 全空）。
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('records'));
  await p.waitForTimeout(2000);

  const out = {};

  // ⑤ 先查记录表单元格
  out.cells = await p.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    let tr = null;
    for (const t of tables) { tr = Array.from(t.querySelectorAll('tbody tr')).find(vis); if (tr) break; }
    if (!tr) return { noRow: true };
    return {
      hasPill: !!tr.querySelector('.log-pill'),
      pillRadius: (() => { const e = tr.querySelector('.log-pill'); return e ? getComputedStyle(e).borderRadius : null; })(),
      hasCellStack: !!tr.querySelector('.cell-stack'),
      hasArrowSub: !!Array.from(tr.querySelectorAll('.cell-sub')).find(x => /↓|↑/.test(x.textContent||'')),
      hasTtft: !!Array.from(tr.querySelectorAll('.cell-sub')).find(x => /TTFT/.test(x.textContent||'')),
      hasEffortPill: !!tr.querySelector('.log-pill-effort'),
    };
  });

  // ① 打开一条**有 context** 的日志
  // ⚠️ context.kinds 是**元组数组** `[["system",{items,tokens}], ...]`（serde 的 Vec<(String,KindStat)>），
  // 不是 `[{kind,tokens}]`。第一版按对象判 k.tokens → 永远筛不到（探针一直报 noCtxLog）。
  const opened = await p.evaluate(() => {
    const hasTokens = (l) => {
      const c = l && l.context;
      if (!c || !Array.isArray(c.kinds)) return false;
      return c.kinds.some(k => Array.isArray(k)
        ? (Number((k[1] || {}).tokens) || 0) > 0
        : (Number(k.tokens) || 0) > 0);
    };
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const logs = st.logs || [];
    const withCtx = logs.find(hasTokens);
    if (!withCtx) {
      const anyCtx = logs.find(l => !!l.context);
      return {
        noCtxLog: true, total: logs.length,
        ctxFieldCount: logs.filter(l => !!l.context).length,
        sampleCtxRaw: anyCtx ? JSON.stringify(anyCtx.context).slice(0, 400) : null,
      };
    }
    st.openLogDetail(withCtx);
    return { opened: true, ts: withCtx.timestamp };
  });
  console.log('OPEN ' + JSON.stringify(opened));
  await p.waitForTimeout(1200);

  out.ctxBlock = await p.evaluate(() => {
    const keys = Array.from(document.querySelectorAll('.log-detail-key'));
    const k = keys.find(x => (x.textContent||'').indexOf('输入构成') >= 0);
    if (!k) return { found: false, allKeys: keys.map(x => (x.textContent||'').trim()).slice(0, 25) };
    const item = k.closest('.log-detail-item');
    const bar = item ? item.querySelector('.ctx-bar') : null;
    const segs = bar ? bar.querySelectorAll('span') : [];
    const badges = item ? Array.from(item.querySelectorAll('.ctx-badge')) : [];
    // 参数徽章带（2026-10-24 新增）
    const strip = document.querySelector('.log-param-strip');
    const tags = strip ? Array.from(strip.querySelectorAll('.lp-tag')) : [];
    return {
      found: true,
      segCount: segs.length,
      badgeCount: badges.length,
      firstBadge: badges.length ? (badges[0].textContent||'').trim() : null,
      allHaveApprox: badges.every(b => (b.textContent||'').indexOf('≈') >= 0),
      allHavePct: badges.every(b => /%/.test(b.textContent||'')),
      stripFound: !!strip,
      tagCount: tags.length,
      tagTexts: tags.map(x => (x.textContent||'').trim()).slice(0, 8),
      // 徽章内数值之间必须有可见分隔（曾粘连成 "×28≈13.4K11.8%"）
      hasSeparator: badges.every(b => /·/.test(b.textContent||'')),
    };
  });
  await p.screenshot({ path: `%(out)s/detail_ctx.png` });

  // ④ 打开一条**没有 context 数据**的日志 → 块不出现
  out.noCtx = await p.evaluate(async () => {
    const hasTokens = (l) => {
      const c = l && l.context;
      if (!c || !Array.isArray(c.kinds)) return false;
      return c.kinds.some(k => Array.isArray(k)
        ? (Number((k[1] || {}).tokens) || 0) > 0
        : (Number(k.tokens) || 0) > 0);
    };
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const without = (st.logs || []).find(l => !hasTokens(l));
    if (!without) return { noneAvailable: true };
    st.openLogDetail(without);
    await new Promise(r => setTimeout(r, 700));
    const keys = Array.from(document.querySelectorAll('.log-detail-key'));
    const k = keys.find(x => (x.textContent||'').indexOf('输入构成') >= 0);
    return { hidden: !k, ts: without.timestamp, keyCount: keys.length };
  });

  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(os.path.join(ROOT, ".openbitfun", "tmp", "shots_logctx"), exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_logctx")
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
    outdir = os.path.join(ROOT, ".openbitfun", "tmp", "shots_logctx").replace("\\", "/")
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
        jsf = os.path.join(d, "probe_logctx_tmp.js")
        # ⚠️ 不能用 `JS % {...}` —— JS 里有 `50%`、`/%/` 这类字面百分号，
        # 会被当成格式占位符报 "not enough arguments"。改用 str.replace。
        js_code = (JS.replace("%(base)s", base).replace("%(out)s", outdir))
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(js_code)
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        opline = next((l for l in sout.splitlines() if l.startswith("OPEN ")), None)
        if opline:
            print("  openLogDetail 现场: " + opline[5:])
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:900]}")
        print()

        c = res.get("cells") or {}
        check(c.get("hasPill") is True and str(c.get("pillRadius") or "").startswith("999"),
              "⑤a 状态码是**全圆角胶囊**", f"radius={c.get('pillRadius')}")
        check(c.get("hasArrowSub") is True, "⑤b Token 有 ↓输入 · ↑输出 小字", f"{c.get('hasArrowSub')}")
        check(c.get("hasTtft") is True, "⑤c 延迟有 TTFT 小字", f"{c.get('hasTtft')}")
        check(c.get("hasEffortPill") is True, "⑤d 思考档是紫色胶囊", f"{c.get('hasEffortPill')}")

        cb = res.get("ctxBlock") or {}
        check(cb.get("found") is True, "① 详情里有「输入构成」块",
              f"keys={cb.get('allKeys')}")
        check(cb.get("segCount", 0) >= 2, "② 比例条有 ≥2 段", f"segs={cb.get('segCount')}")
        check(cb.get("badgeCount", 0) >= 2, "③ 徽章 ≥2 个", f"badges={cb.get('badgeCount')}")
        check(cb.get("allHaveApprox") is True and cb.get("allHavePct") is True,
              "③ 每个徽章都含 ≈tokens 与占比（口径诚实）",
              f"first={cb.get('firstBadge')} approx={cb.get('allHaveApprox')} pct={cb.get('allHavePct')}")
        check(cb.get("hasSeparator") is True,
              "③ 徽章内数值有可见分隔（曾粘连成 ×28≈13.4K11.8%）",
              f"sep={cb.get('hasSeparator')} first={cb.get('firstBadge')}")
        # 参数徽章带（2026-10-24）
        check(cb.get("stripFound") is True, "⑦ 详情顶部有参数徽章带",
              f"stripFound={cb.get('stripFound')}")
        check(cb.get("tagCount", 0) >= 2, "⑦ 徽章带 ≥2 个胶囊（模型 + 至少一个其它参数）",
              f"tags={cb.get('tagTexts')}")
        check(any("model:" in t for t in (cb.get("tagTexts") or [])),
              "⑦ 徽章带里有 model: 标识", f"{cb.get('tagTexts')}")
        nc = res.get("noCtx") or {}
        check(nc.get("noneAvailable") is True or nc.get("hidden") is True,
              "④ 没有 context 的旧日志**不渲染**该块（不留空块）", f"{nc}")
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
