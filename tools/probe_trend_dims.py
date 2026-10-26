# -*- coding: utf-8 -*-
"""趋势图「看什么」维度切换（2026-10-26）。

此前趋势图只有"输入+输出总量"一个维度，于是两个问题看不出来：
  ① 缓存省下来的量随时间怎么变（本项目最核心的卖点，却只有静态命中率数字）
  ② 涨的是输入还是输出（对应的优化手段完全不同）

本轮加了 total / split / cache 三个维度。判据：
  ① total：序列是**模型名**，值与旧口径一致（输入+输出）
  ② split：序列变成**输入/输出/缓存三条**（固定语义色）
  ③ cache：序列是模型名但值只取缓存
  ④ 三个维度画出的 SVG **互不相同**（否则是"点了没反应"）
  ⑤ split 时三条之和 ≈ total 的值 + 缓存（口径可对）
  ⑥ 非法维度值归一到 total（防图表空白）
  ⑦ 缓存口径说明只在非 total 维度出现

用法：python tools/probe_trend_dims.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_trend_dims")

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
  const p = await b.newPage({ viewport: { width: 1560, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 250)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  // 进分析页 overview（趋势图在那儿）
  out.nav = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('analytics');
    await new Promise(r => setTimeout(r, 300));
    d.analyticsTab = 'overview';
    await d.fetchStats(null, false, true);
    await new Promise(r => setTimeout(r, 2000));
    return { tab: d.analyticsTab, rows: (d.stats && d.stats.model_trends || []).length };
  });

  // ⚠️ 必须先停 3s 轮询再注入：否则注入的假数据会在下一个 tick 被真实
  //    fetchStats 覆盖 —— 实测表现为"最后一个维度(cac)读到的是真实数据"
  //   （序列变回真实模型名、合计 12552），于是"口径可对"判据必然失败。
  //   这不是代码 bug，是探针的时序问题。
  out.pollStopped = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    if (d.pollTimer) { clearInterval(d.pollTimer); d.pollTimer = 0; return true; }
    return false;
  });

  // 注入一份**形状完整**的 model_trends（含 cached_tokens），这样三个维度
  // 才有可区分的数值。判据是"维度切换真的生效且口径可对"，不是"有真实缓存"。
  out.injected = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.stats = Object.assign({}, d.stats, {
      trends: [{ ts: 1, date: 'd1' }, { ts: 2, date: 'd2' }, { ts: 3, date: 'd3' }],
      model_trends: [
        { ts: 1, provider: 'p1', upstream_model: 'm1', prompt_tokens: 100, completion_tokens: 20, cached_tokens: 30 },
        { ts: 2, provider: 'p1', upstream_model: 'm1', prompt_tokens: 110, completion_tokens: 25, cached_tokens: 40 },
        { ts: 3, provider: 'p1', upstream_model: 'm1', prompt_tokens: 90, completion_tokens: 15, cached_tokens: 10 },
        { ts: 1, provider: 'p2', upstream_model: 'm2', prompt_tokens: 50, completion_tokens: 10, cached_tokens: 5 },
        { ts: 2, provider: 'p2', upstream_model: 'm2', prompt_tokens: 60, completion_tokens: 12, cached_tokens: 8 },
      ],
    });
    return { needed: true, rows: (d.stats.model_trends || []).length };
  });

  // 逐维度：切过去，记录序列与 SVG
  //
  // ⚠️ 两个坑（都实测踩过）：
  //  ① `_setTrendHtml` 有 160ms 淡入延迟 —— 切完**必须等**再读
  //     modelTrendSvgHtml，否则读到的是上一维度的图（表现为"三张图一模一样"）。
  //  ② 不能用 `h*31|0` 这种简易 hash 判"图是否不同" —— 实测三个维度差异很小时
  //     会碰撞出同一个值，造成"点了没反应"的假失败。改存**完整 SVG 字符串**比较。
  const snap = async (dim) => p.evaluate(async (d0) => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.setTrendDim(d0);
    await new Promise(r => setTimeout(r, 500));
    const md = d.modelTrendData || {};
    return {
      dim: d.trendDimField,
      seriesLabels: (md.series || []).map(s => s.label),
      seriesColors: (md.series || []).map(s => s.color),
      seriesTotals: (md.series || []).map(s => (s.values || []).reduce((a, b) => a + Number(b || 0), 0)),
      svgLen: (d.modelTrendSvgHtml || '').length,
      svg: (d.modelTrendSvgHtml || '').slice(0, 60000),
      // y 轴刻度文本：最能体现"三个维度的量纲不同"
      axes: ((d.modelTrendSvgHtml || '').match(/class="chart-axis"[^>]*>([^<]*)</g) || [])
        .map(s => (s.split('>')[1] || '').replace('<', '')).slice(0, 5).join('|'),
    };
  }, dim);

  out.total = await snap('total');
  out.split = await snap('split');
  out.cache = await snap('cache');

  // 非法值归一
  out.invalid = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.setTrendDim('不存在的维度');
    await new Promise(r => setTimeout(r, 400));
    const after = d.trendDimField;
    // 回到 total 以便后续判断
    d.setTrendDim('total');
    return { after };
  });

  // 口径说明：只在非 total 时出现
  out.cacheNote = await p.evaluate(async () => {
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
    const find = () => !!Array.from(document.querySelectorAll('.chart-summary'))
      .filter(isVis).find(e => /缓存|KV Cache/i.test(e.textContent || ''));
    d.setTrendDim('total'); await new Promise(r => setTimeout(r, 400));
    const onTotal = find();
    d.setTrendDim('cache'); await new Promise(r => setTimeout(r, 400));
    const onCache = find();
    d.setTrendDim('total'); await new Promise(r => setTimeout(r, 400));
    return { onTotal, onCache };
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
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)

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

    nav = d.get("nav") or {}
    tot = d.get("total") or {}
    spl = d.get("split") or {}
    cac = d.get("cache") or {}
    inv = d.get("invalid") or {}
    cn = d.get("cacheNote") or {}

    print(f"\n[现场] 页签={nav.get('tab')} model_trends 行数={nav.get('rows')} "
          f"停轮询={d.get('pollStopped')} 注入={d.get('injected', {}).get('needed')}")
    for nm, s in [("total", tot), ("split", spl), ("cache", cac)]:
        print(f"  {nm}: dim={s.get('dim')} 序列={s.get('seriesLabels')}")
        print(f"        合计={s.get('seriesTotals')} svgLen={s.get('svgLen')} hash={s.get('svgHash')}")
    print(f"  非法值归一后 = {inv.get('after')}")
    print(f"  缓存口径说明: total 时={cn.get('onTotal')} cache 时={cn.get('onCache')}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])

    # ① 三个维度都真的切过去了
    check(tot.get("dim") == "total", "total 维度生效", str(tot.get("dim")))
    check(spl.get("dim") == "split", "split 维度生效", str(spl.get("dim")))
    check(cac.get("dim") == "cache", "cache 维度生效", str(cac.get("dim")))

    # ② split 的序列必须是三条固定指标（不是模型名）
    sl = spl.get("seriesLabels") or []
    check(len(sl) == 3, "split 有 3 条序列（输入/输出/缓存）", str(sl))
    check(any("输入" in x or "Input" in x for x in sl), "split 含「输入」序列", str(sl))
    check(any("输出" in x or "Output" in x for x in sl), "split 含「输出」序列", str(sl))
    check(any("缓存" in x or "Cached" in x for x in sl), "split 含「缓存」序列", str(sl))

    # ③ 三个维度画出的图必须互不相同（不是"点了没反应"）
    svgs = [tot.get("svg") or "", spl.get("svg") or "", cac.get("svg") or ""]
    check(len(set(svgs)) == 3, "三个维度画出的 SVG 互不相同（按完整字符串比较）",
          "len=%s" % [len(s) for s in svgs])
    axes = [tot.get("axes"), spl.get("axes"), cac.get("axes")]
    check(len(set(axes)) == 3, "三个维度的 y 轴刻度不同（量纲真的换了）", str(axes))

    # 轮询已停 → 三个维度必须都读到注入的数据（序列标签一致）
    inj_labels = set(tot.get("seriesLabels") or [])
    check(inj_labels == set(["m1", "m2"]),
          "total 维度读到注入的数据（否则是轮询覆盖了）", str(tot.get("seriesLabels")))
    check(set(cac.get("seriesLabels") or []) == inj_labels,
          "cache 维度也读到注入的数据（不是被轮询覆盖的真实数据）", str(cac.get("seriesLabels")))

    # ④ 口径可对：split 三条之和 == total 各序列之和 + 缓存之和
    t_sum = sum(tot.get("seriesTotals") or [])
    s_sum = sum(spl.get("seriesTotals") or [])
    c_sum = sum(cac.get("seriesTotals") or [])
    # total 计的是 (输入+输出)；split 计的是 (输入+输出+缓存)
    check(abs(s_sum - (t_sum + c_sum)) <= max(1, t_sum * 0.001),
          "split(三项之和) == total(输入+输出) + cache(缓存) —— 口径可对",
          f"split={s_sum} total={t_sum} cache={c_sum}")

    # ⑤ 非法值归一
    check(inv.get("after") == "total", "非法维度值归一到 total（防图表空白）", str(inv.get("after")))

    # ⑥ 缓存口径说明只在非 total 时出现
    check(cn.get("onCache") is True, "cache 维度下有缓存口径说明")
    check(cn.get("onTotal") is False, "total 维度下不显示缓存口径说明（不制造噪音）")

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
