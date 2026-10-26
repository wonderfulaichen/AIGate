# -*- coding: utf-8 -*-
"""分析页补齐项的回归探针（2026-10-26）。

覆盖本轮补的四件事（都是"数据早就在、界面没了"或"界面有了但没说清"）：

  ① **网关去程五段**（`gwStageWinRows`）—— 此前计算端完好、HTML 端零引用。
     补在网关节点里（不新增卡片），让"慢在网关哪一段"可见。
  ② **供应商表补 429 限流 / 首字延迟** —— 后端 `rate_limited` / `ttft_avg_ms`
     早就有，分析页一个都没画。
  ③ **失败原因行内可见** —— 不必点开展开就能看到"为什么红"。
  ④ **上下文构成提为一级面板** —— 此前埋在默认折叠的「省量审计」第三段。
  ⑤ **TTFT 近似值口径披露** —— p50/p90 是按直方图桶上界反推的近似值，
     界面此前只给数字不给口径。

为什么用真实实例（不是静态预览）：①④⑤ 依赖后端算好的 `gateway_stages` /
`audit.context` / `ttft`，静态桩里没有 → 会假失败。

用法：python tools/probe_analytics_gaps.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_ana_gaps")
DEPLOY = r"D:\Office software\Development Project\AI中转\providers.json"

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


UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1560, height: 1400 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 250)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  // 切到 cost 页签（这四项都在 cost 页签）
  out.tab = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('analytics');
    await new Promise(r => setTimeout(r, 300));
    d.analyticsTab = 'cost';
    await d.fetchStats(null, false, true);
    await new Promise(r => setTimeout(r, 2500));
    return { analyticsTab: d.analyticsTab };
  });
  await p.waitForTimeout(1500);

  const isVis = `(el) => {
    let n = el;
    while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement;
    }
    return true;
  }`;

  // ── ① 网关去程五段 ──
  //
  // ⚠️ mock 生成的假日志**不含** stage 计时（那是真实请求才有的），
  // 所以直接读真实状态会 hasData=false、模板不渲染 → 测不到接线。
  // 这里改为**注入合成样本**再验证 DOM：判据是"数据在 → 界面在"，
  // 而这正是本轮要修的东西（此前计算端完好、HTML 端零引用）。
  out.gwStages = await p.evaluate(async (isVisSrc) => {
    const isVis = eval(isVisSrc);
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    // 注入一份形状与后端一致的数据（GatewayStageSummary）
    d.stats = Object.assign({}, d.stats, {
      gateway_stages: {
        route:         { samples: 10, avg_ms: 3,   p50_ms: 3,   p95_ms: 6 },
        guard:         { samples: 10, avg_ms: 1,   p50_ms: 1,   p95_ms: 2 },
        convert:       { samples: 10, avg_ms: 2,   p50_ms: 2,   p95_ms: 4 },
        upstream_ttfb: { samples: 10, avg_ms: 900, p50_ms: 800, p95_ms: 1400 },
        overhead:      { samples: 10, avg_ms: 6,   p50_ms: 5,   p95_ms: 12 },
      },
    });
    await new Promise(r => setTimeout(r, 700));
    const rows = d.gwStageWinRows || [];
    const hasData = d.gwStageHasData;
    const box = document.querySelector('.topo-gw-stages');
    const stageEls = box ? Array.from(box.querySelectorAll('.topo-gw-stage')).filter(isVis) : [];
    return {
      hasData,
      rowCount: rows.length,
      rowKeys: rows.map(r => r.k),
      boxExists: !!box,
      boxVisible: box ? isVis(box) : false,
      stageElCount: stageEls.length,
      stageTexts: stageEls.map(e => (e.textContent || '').replace(/\s+/g, ' ').trim()),
      barCount: box ? box.querySelectorAll('.topo-bar').length : 0,
      // 条长归一：最慢那段应是 100%，其余按比例
      barWidths: box ? Array.from(box.querySelectorAll('.topo-bar > i')).map(i => i.style.width) : [],
    };
  }, isVis);

  // ── ② 供应商表的 429 / 首字列 ──
  // ⚠️ 必须**限定在供应商表内**：页面还有一张模型明细表（13 列），
  // 用全局 `tbody tr` 会把两张表的行混在一起 → 单元格数不一致是假失败。
  out.provTable = await p.evaluate((isVisSrc) => {
    const isVis = eval(isVisSrc);
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    // 供应商表 = 含「限流 429」表头的那张表
    let tbl = null;
    for (const t of Array.from(document.querySelectorAll('table'))) {
      const hs = Array.from(t.querySelectorAll('thead th')).map(x => (x.textContent || ''));
      if (hs.some(h => /限流|429/.test(h))) { tbl = t; break; }
    }
    if (!tbl) return { found: false, headers: [], rowCount: 0, cellCounts: [] };
    const ths = Array.from(tbl.querySelectorAll('thead th')).map(t2 => (t2.textContent || '').replace(/\s+/g, ' ').trim());
    // 只取主行：展开区那几行的 td 带 colspan（不是常规单元格）
    const bodyRows = Array.from(tbl.querySelectorAll('tbody > tr, tbody > template + tr'))
      .filter(isVis)
      .filter(r => !r.querySelector('td[colspan]'));
    return {
      found: true,
      headers: ths,
      has429: ths.some(h => /限流|429/.test(h)),
      hasTtft: ths.some(h => /首字|TTFT/.test(h)),
      rowCount: bodyRows.length,
      cellCounts: bodyRows.slice(0, 6).map(r => r.querySelectorAll('td').length),
      colCount: ths.length,
      // 数值层：后端字段是否真的进了行
      sampleRates: (d.filteredProviderStats || []).slice(0, 3).map(p => ({
        prov: p.provider, rl: p.rate_limited, ttft: p.ttft_avg_ms,
      })),
    };
  }, isVis);

  // ── ③ 失败原因行内可见 ──
  out.errInline = await p.evaluate((isVisSrc) => {
    const isVis = eval(isVisSrc);
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const withErr = (d.filteredProviderStats || []).filter(p => d.provLastError(p.provider));
    // 找包含该错误文本的可见元素
    let found = false, sample = '';
    if (withErr.length) {
      const msg = d.provLastError(withErr[0].provider);
      for (const el of Array.from(document.querySelectorAll('span'))) {
        if (!isVis(el)) continue;
        const tx = (el.textContent || '').trim();
        if (msg && tx.includes(msg)) { found = true; sample = tx.slice(0, 120); break; }
      }
    }
    return { providersWithErr: withErr.length, found, sample };
  }, isVis);

  // ── ④ 上下文构成是否一级可见 ──
  out.ctxPanel = await p.evaluate((isVisSrc) => {
    const isVis = eval(isVisSrc);
    const panels = Array.from(document.querySelectorAll('.pnl')).filter(isVis);
    // 找标题是"上下文构成"的面板，且**不在**省量审计折叠区里
    let topLevel = null, insideAudit = null;
    for (const pn of panels) {
      const ht = pn.querySelector('.pnl-ht');
      const t = ht ? (ht.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (!/上下文|Context/.test(t)) continue;
      // 祖先里有没有「省量审计」面板
      let n = pn.parentElement, inAudit = false;
      while (n && n !== document.body) {
        if (n.classList && n.classList.contains('pnl')) {
          const h2 = n.querySelector('.pnl-ht');
          const t2 = h2 ? (h2.textContent || '') : '';
          if (/省量|Saving audit/i.test(t2)) { inAudit = true; break; }
        }
        n = n.parentElement;
      }
      if (inAudit) insideAudit = t; else if (!topLevel) topLevel = t;
    }
    return { topLevel, insideAudit };
  }, isVis);

  // ── ⑤ TTFT 近似值口径 ──
  // 同样注入合成样本：mock 日志没有真实首字计时。
  // 判据是"有数据时界面必须披露口径"，而不是"有数据"本身。
  out.ttftHint = await p.evaluate(async (isVisSrc) => {
    const isVis = eval(isVisSrc);
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.stats = Object.assign({}, d.stats, {
      ttft: {
        samples: 20,
        avg_ms: 850,
        p50_ms: 800,
        p90_ms: 1900,
        // ⚠️ hist 长度必须 == ttftBuckets 的长度（10 —— ds-computed.js:428-441 有 10 个桶，
        // 最后一个是 max:null 的溢出桶）。长度不一致时 ttftSummary 直接按"无数据"返回。
        hist: [1, 2, 3, 4, 3, 3, 2, 1, 1, 0],
      },
    });
    await new Promise(r => setTimeout(r, 700));
    let hint = '';
    for (const el of Array.from(document.querySelectorAll('div,span'))) {
      if (!isVis(el)) continue;
      const tx = (el.textContent || '').trim();
      if (/近似|桶上界|bucket/i.test(tx) && tx.length < 200) { hint = tx; break; }
    }
    return { hasData: d.ttftHasData, hint };
  }, isVis);

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    pd = pdir()
    if not pd:
        print(f"{BAD}  找不到 playwright")
        return 1
    shutil.rmtree(WD, ignore_errors=True)
    os.makedirs(os.path.join(WD, "data"), exist_ok=True)
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    if os.path.exists(DEPLOY):
        shutil.copy(DEPLOY, os.path.join(WD, "providers.json"))

    port = free_port()
    env2 = dict(os.environ, PORT=str(port),
                AIGATE_DATA_DIR=os.path.join(WD, "data"),
                AIGATE_OPEN_ADMIN="1")
    proc = subprocess.Popen([iso], env=env2, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1

        # 造数据：mock 日志（让 per_provider/per_model/gateway_stages 有内容）
        req = urllib.request.Request(base + "/admin/api/mock", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            print("[准备] mock 数据已生成")
        except Exception as e:
            print(f"[准备] mock 失败（继续）: {e}")

        jsf = os.path.join(WD, "u.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-700:]}\n{(r.stderr or '')[-700:]}")
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal']}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    gw = d.get("gwStages") or {}
    pt = d.get("provTable") or {}
    ei = d.get("errInline") or {}
    cp = d.get("ctxPanel") or {}
    th = d.get("ttftHint") or {}

    print(f"\n[① 网关去程五段] hasData={gw.get('hasData')} 计算行数={gw.get('rowCount')} "
          f"keys={gw.get('rowKeys')}")
    print(f"   DOM: 容器存在={gw.get('boxExists')} 可见={gw.get('boxVisible')} "
          f"段元素={gw.get('stageElCount')} 条={gw.get('barCount')}")
    print(f"   条长: {gw.get('barWidths')}")
    for s in (gw.get("stageTexts") or []):
        print(f"   · {s}")
    print(f"\n[② 供应商表] 找到={pt.get('found')} 表头={pt.get('headers')}")
    print(f"   主行数={pt.get('rowCount')} 各行单元格数={pt.get('cellCounts')} 表头列数={pt.get('colCount')}")
    print(f"   前三家数值: {pt.get('sampleRates')}")
    print(f"\n[③ 失败原因] 有错误的供应商数={ei.get('providersWithErr')} 行内可见={ei.get('found')}")
    if ei.get("sample"):
        print(f"   样例: {ei.get('sample')}")
    print(f"\n[④ 上下文构成] 一级面板={cp.get('topLevel')!r} 仍埋在审计里={cp.get('insideAudit')!r}")
    print(f"\n[⑤ TTFT 口径] hasData={th.get('hasData')} 提示={th.get('hint')!r}")
    if d.get("errs"):
        print(f"\n[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])

    # ① 网关五段
    check(gw.get("rowCount") == 5, "gwStageWinRows 计算出 5 段", str(gw.get("rowCount")))
    check(bool(gw.get("hasData")), "注入样本后 gwStageHasData 为真")
    check(bool(gw.get("boxExists")), "网关节点里有五段明细容器（HTML 已接线）")
    check(bool(gw.get("boxVisible")), "五段明细**可见**（不是只算不画）")
    check((gw.get("stageElCount") or 0) == 5, "DOM 里有 5 个段元素", str(gw.get("stageElCount")))
    check((gw.get("barCount") or 0) == 5, "每段有进度条", str(gw.get("barCount")))
    # 条长归一：注入数据里 upstream_ttfb 是 900ms（最慢），应是 100%
    bw = gw.get("barWidths") or []
    check(any(w.startswith("100") for w in bw),
          "最慢那段条长为 100%（按组内归一，可横向比快慢）", str(bw))

    # ② 供应商表
    check(bool(pt.get("found")), "找到供应商表（含 429 列的那张）")
    check(bool(pt.get("has429")), "表头有「限流 429」列", str(pt.get("headers")))
    check(bool(pt.get("hasTtft")), "表头有「首字延迟」列", str(pt.get("headers")))
    if pt.get("rowCount"):
        cc = pt.get("cellCounts") or []
        ncol = pt.get("colCount") or 0
        check(len(set(cc)) == 1,
              "供应商表每行单元格数一致（列没错位）", str(cc))
        check(cc and cc[0] == ncol,
              "每行单元格数 == 表头列数", f"cells={cc[:1]} cols={ncol}")
    else:
        check(False, "供应商表有数据行", "0 行")

    # ③ 失败原因
    if (ei.get("providersWithErr") or 0) > 0:
        check(bool(ei.get("found")), "失败原因**行内可见**（不用展开）", ei.get("sample") or "")
    else:
        print(f"  (跳过) 没有带错误的供应商，无法验失败原因行内显示")

    # ④ 上下文构成
    check(bool(cp.get("topLevel")), "上下文构成是**一级面板**（默认可见）", str(cp.get("insideAudit")))
    check(not cp.get("insideAudit"),
          "上下文构成不再只存在于折叠的审计卡里", str(cp.get("insideAudit")))

    # ⑤ TTFT 口径
    check(bool(th.get("hasData")), "注入样本后 ttftHasData 为真")
    check(bool(th.get("hint")),
          "TTFT 分位数有近似值口径披露（p50/p90 是桶上界反推）", repr(th.get("hint"))[:160])

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项失败")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
