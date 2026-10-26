# -*- coding: utf-8 -*-
"""趋势图的三个指标开关（2026-10-27 重做）。

用户指出：上一版「总量/分项/缓存」三选一是**臆造**，参考项目（WaLiAPI
DashboardPage.tsx:39-41、1073-1100）是**三个独立开关**（输入/输出/缓存，
可任意组合），且与模型图例**正交**：开关决定"堆什么指标"，图例决定"堆哪些模型"。

判据：
  ① 三个开关都在（输入/输出/缓存），且**独立可点**（不是一个三选一）
  ② 默认：输入+输出开、缓存关（缓存与输入语义重叠，全开总量会虚高）
  ③ 点「缓存」→ 缓存开、其余不变（**不是**互斥切换）
  ④ 全部关掉 → 自动把刚点的那个恢复开（不留空白图）
  ⑤ 开关在**图表底部工具栏**，与「线/柱」按钮**相邻**（不再被甩到屏幕两端）
  ⑥ 开关带颜色圆点，且与图内分段同色（输入=蓝/输出=紫/缓存=绿）
  ⑦ 指标合计只显示**已打开**的指标

用法：python tools/probe_trend_metrics.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_trend_metrics")

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
  const p = await b.newPage({ viewport: { width: 1560, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 250)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  // 停轮询（否则注入数据会被 fetchStats 覆盖）
  out.pollStopped = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    if (d.pollTimer) { clearInterval(d.pollTimer); d.pollTimer = 0; return true; }
    return false;
  });

  // 进分析页 + 注入趋势数据
  out.nav = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('analytics');
    await new Promise(r => setTimeout(r, 400));
    d.analyticsTab = 'overview';
    d.stats = Object.assign({}, d.stats, {
      // ⚠️ 必须给 total_requests > 0：2026-10-27 起空态会**整块让位**
      //    （`stats.total_requests` 为 0 时只有一张空态卡，不渲染 KPI/控件/图表）。
      //    不给这一项 → 趋势图面板根本不存在 → 开关一个都找不到（5 项假失败）。
      total_requests: 128,
      total_prompt_tokens: 62000,
      total_completion_tokens: 12000,
      trends: [{ ts: 1, date: 'd1' }, { ts: 2, date: 'd2' }, { ts: 3, date: 'd3' }],
      model_trends: [
        { ts: 1, provider: 'p1', upstream_model: 'm1', prompt_tokens: 100, completion_tokens: 20, cached_tokens: 30 },
        { ts: 2, provider: 'p1', upstream_model: 'm1', prompt_tokens: 110, completion_tokens: 25, cached_tokens: 40 },
        { ts: 3, provider: 'p1', upstream_model: 'm1', prompt_tokens: 90, completion_tokens: 15, cached_tokens: 10 },
        { ts: 1, provider: 'p2', upstream_model: 'm2', prompt_tokens: 50, completion_tokens: 10, cached_tokens: 5 },
        { ts: 2, provider: 'p2', upstream_model: 'm2', prompt_tokens: 60, completion_tokens: 12, cached_tokens: 8 },
      ],
    });
    await new Promise(r => setTimeout(r, 1200));
    return { tab: d.analyticsTab };
  });
  await p.waitForTimeout(600);

  const snap = () => p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    // 开关按钮（底部工具栏里的**指标**开关；图型按钮也用同一 class，需排除）
    const allToggleBtns = Array.from(document.querySelectorAll('.trend-metric-toggle')).filter(isVis);
    const btns = allToggleBtns.filter(b => /输入|输出|缓存|Input|Output|Cached/.test(b.textContent || ''));
    const toolbar = document.querySelector('.trend-toolbar');
    // 图型按钮与**第一个指标开关**的距离（用来守"不再被甩到屏幕两端"）
    //
    // ⚠️ 两个坑（都实测踩过）：
    //  ① 不能找 `.an-chart-tabs .seg-i` —— 2026-10-27 起「线/柱」也改成了
    //    .trend-metric-toggle（与指标开关同款胶囊），没有 .seg-i 了。
    //  ② 必须量「**柱**（最后一个图型按钮）→ 输入（第一个指标开关）」的距离 ——
    //    量「线→输入」会把「柱」按钮本身的宽度算进去（实测 58px，全是假距离）。
    const allToggles = Array.from(document.querySelectorAll('.trend-metric-toggle')).filter(isVis);
    const modeBtns = allToggles.filter(b => /^(线|柱|Line|Bar)$/.test((b.textContent || '').trim()));
    const metricBtns = allToggles.filter(b => /输入|输出|缓存|Input|Output|Cached/.test(b.textContent || ''));
    const lastMode = modeBtns[modeBtns.length - 1];
    const firstMetric = metricBtns[0];
    let gap = -1;
    if (lastMode && firstMetric) {
      const a = lastMode.getBoundingClientRect();
      const c = firstMetric.getBoundingClientRect();
      gap = Math.round(c.left - a.right);
    }
    return {
      metrics: d.trendMetrics,
      toggleCount: btns.length,
      toggleLabels: btns.map(b => (b.textContent || '').trim()),
      toggleOn: btns.map(b => b.className.indexOf('on') >= 0),
      toggleDots: btns.map(b => {
        const i = b.querySelector('i');
        return i ? getComputedStyle(i).backgroundColor : '';
      }),
      summary: (d.trendMetricSummary || []).map(s => s.label),
      toolbarExists: !!toolbar,
      toolbarVisible: toolbar ? isVis(toolbar) : false,
      // 开关**必须在图表下方的工具栏里**，不能在标题行（.pnl-h）里。
      // 这是用户「离得这么远」的根因：两个 seg 分处 .pnl-h 两端，
      // 而 .pnl-h 是 justify-content:space-between → 被均分到整屏两侧。
      togglesInHeader: !!document.querySelector('.pnl-h .trend-metric-toggle'),
      togglesInToolbar: !!document.querySelector('.trend-toolbar .trend-metric-toggle'),
      // 开关与图表主体的垂直关系（开关应在图下方）
      //
      // ⚠️ SVG 是 `x-html` 注入的（在 .trend-fade 里），`document.querySelector('.model-trend-svg')`
      //    在某些时序下取不到 → 用**图表容器**（.trend-hover / .token-trend-body）更稳。
      togglesBelowChart: (() => {
        const chart = document.querySelector('.trend-hover') ||
                      document.querySelector('.token-trend-body');
        const tb = document.querySelector('.trend-toolbar');
        if (!chart || !tb) return null;
        const cb = chart.getBoundingClientRect().bottom;
        const tt = tb.getBoundingClientRect().top;
        return Math.round(tt) >= Math.round(cb) - 8;
      })(),
      gap,
      // 旧的"总量/分项"三选一按钮必须**不存在**
      oldDimButtons: Array.from(document.querySelectorAll('.seg-i')).filter(isVis)
        .map(b => (b.textContent || '').trim())
        .filter(t => /总量|分项|Total|Split/.test(t)),
    };
  });

  out.initial = await snap();

  // 点「缓存」→ 应变为开，且输入/输出**不变**（独立而非互斥）
  out.afterCache = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.toggleTrendMetric('cache');
    await new Promise(r => setTimeout(r, 500));
    return { metrics: d.trendMetrics };
  });
  out.afterCacheSnap = await snap();

  // 全部关掉 → 自动恢复刚点的那个
  out.afterAllOff = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    // 关掉 input / output（cache 已开）
    if (d.trendMetrics.input) d.toggleTrendMetric('input');
    await new Promise(r => setTimeout(r, 200));
    if (d.trendMetrics.output) d.toggleTrendMetric('output');
    await new Promise(r => setTimeout(r, 300));
    return { metrics: d.trendMetrics };
  });

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
    shutil.copy2(EXE, os.path.join(WD, "AIGate.exe"))

    port = free_port()
    env2 = dict(os.environ, PORT=str(port),
                AIGATE_DATA_DIR=os.path.join(WD, "data"),
                AIGATE_OPEN_ADMIN="1")
    proc = subprocess.Popen([os.path.join(WD, "AIGate.exe")], env=env2, cwd=WD,
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

        jsf = os.path.join(WD, "u.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd,
                           timeout=180)
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

    ini = d.get("initial") or {}
    ac = d.get("afterCache") or {}
    acs = d.get("afterCacheSnap") or {}
    ao = d.get("afterAllOff") or {}

    print(f"\n[初始] metrics={ini.get('metrics')} 开关数={ini.get('toggleCount')} "
          f"标签={ini.get('toggleLabels')}")
    print(f"       圆点色={ini.get('toggleDots')}")
    print(f"       合计行={ini.get('summary')}  工具栏存在={ini.get('toolbarExists')} "
          f"可见={ini.get('toolbarVisible')} 与线/柱间距={ini.get('gap')}px")
    print(f"[点缓存后] metrics={ac.get('metrics')} 开关亮={acs.get('toggleOn')} 合计={acs.get('summary')}")
    print(f"[全关后] metrics={ao.get('metrics')}")
    print(f"[旧三选一按钮残留] {ini.get('oldDimButtons')}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])

    # ① 三个独立开关
    check((ini.get("toggleCount") or 0) == 3,
          "有 3 个指标开关（输入/输出/缓存）", str(ini.get("toggleCount")))
    labels = " ".join(ini.get("toggleLabels") or [])
    check("输入" in labels and "输出" in labels and "缓存" in labels,
          "三个开关分别是 输入/输出/缓存", labels)

    # 旧的臆造三选一必须消失
    check(not (ini.get("oldDimButtons") or []),
          "旧的「总量/分项」三选一按钮已不存在", str(ini.get("oldDimButtons")))

    # ② 默认：输入+输出开、缓存关
    m = ini.get("metrics") or {}
    check(m.get("input") is True and m.get("output") is True,
          "默认输入+输出开", str(m))
    check(m.get("cache") is False,
          "默认缓存关（与输入语义重叠，全开总量会虚高）", str(m))

    # ③ 独立而非互斥
    acm = ac.get("metrics") or {}
    check(acm.get("cache") is True and acm.get("input") is True and acm.get("output") is True,
          "点「缓存」后三个**同时**开（独立开关，不是三选一）", str(acm))

    # ④ 全部关掉 → 至少留一个
    aom = ao.get("metrics") or {}
    check(any(aom.get(k) for k in ("input", "output", "cache")),
          "不能把三个都关掉（会留一张空白图）", str(aom))

    # ⑤ 工具栏在底部，且开关紧邻「线/柱」
    check(bool(ini.get("toolbarExists")) and bool(ini.get("toolbarVisible")),
          "底部工具栏存在且可见")
    # 关键：开关**不能在标题行里**（那是"两端分散"的根因）
    check(not ini.get("togglesInHeader"),
          "指标开关不在标题行（.pnl-h）里", str(ini.get("togglesInHeader")))
    check(bool(ini.get("togglesInToolbar")),
          "指标开关在底部工具栏（.trend-toolbar）里")
    check(ini.get("togglesBelowChart") is True,
          "开关位于图表**下方**（不是标题行两端）", str(ini.get("togglesBelowChart")))
    gap = ini.get("gap")
    check(gap is not None and 0 <= gap <= 24,
          "图型与第一个指标开关紧邻（不再被甩到屏幕两端）", f"间距 {gap}px")

    # ⑥ 颜色圆点
    dots = ini.get("toggleDots") or []
    check(len(dots) == 3 and len(set(dots)) >= 2,
          "开关有颜色圆点且互不相同", str(dots))

    # ⑦ 合计只显示已打开的
    summ = " ".join(ini.get("summary") or [])
    check("输入" in summ and "输出" in summ,
          "合计行显示已打开的指标（输入/输出）", summ)
    check("缓存" not in summ,
          "缓存没开时合计行不显示它", summ)
    summ2 = " ".join(acs.get("summary") or [])
    check("缓存" in summ2, "开了缓存后合计行出现缓存", summ2)

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
