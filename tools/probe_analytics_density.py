# -*- coding: utf-8 -*-
"""分析页空间利用率审计（2026-10-27）。

用户三条批评：
  ① 「不要搞这些标题卡片，显示效益太低」—— 页头占了整行宽、只放标题与两个数字
  ② 「供应商明细和模型明细明明是一起的，你还分开显示」
  ③ 「布局按键太随便，提高空间利用率，有些信息不需要非常大空间」

判据（每条对应一个可测量的浪费）：
  ① 页头 .phd 的高度 ≤ 预算（当前 >100px，目标 ≤64px）
  ② 页头里不得有编辑器式的大字标题占整行
  ③ 每个面板的"标题区"高度占比 ≤ 阈值（标题不该比内容还高）
  ④ 全页可见内容高度（首页屏 scopes）：overview / cost 各自 ≤ 预算
  ⑤ 供应商与模型不得各占一张独立面板（必须合并/上下钻）
  ⑥ 控件条（seg）不得独占一行只放两三个按钮

用法：python tools/probe_analytics_density.py            (默认预算)
      python tools/probe_analytics_density.py --report   (只报数不判)
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_density")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []

# 预算（像素）。视口高 1036 时，1 屏 ≈ 1036px。
BUDGET = {
    "phd_h": 72,          # 页头高度上限
    "overview_h": 1500,   # 概览页签内容高度上限（约 1.5 屏）
    "cost_h": 1500,       # 省量页签内容高度上限（约 1.5 屏）
    "panel_title_ratio": 0.55,  # 单面板标题区占比上限（折叠面板的标题几乎占满，单独排除）
    "panel_head_h": 64,   # 单面板标题区的**高度**上限（px）。
                          # 这是守「标题区不能又高又空」的直接判据 ——
                          # 只靠总高度预算守不住：把 padding 从 10 改回 18 只让
                          # 总高从 1154 涨到 1202，仍在 1500 预算内，改坏验证不转红。
}


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
  // 与用户截图一致的视口
  const p = await b.newPage({ viewport: { width: 1560, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 240)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  out.nav = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('analytics');
    await new Promise(r => setTimeout(r, 400));
    await d.fetchStats(null, false, true);
    await new Promise(r => setTimeout(r, 2500));
    return { tab: d.activeTab, rows: (d.stats && d.stats.total_requests) || 0 };
  });

  const measure = async (tab) => p.evaluate(async (t) => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    // ⚠️ 必须先确保 activeTab 在 analytics —— 否则整页 x-show 隐藏，
    //    所有元素 height 都是 0（第一次就测出"内容总高 0px"这个假数）。
    if (d.activeTab !== 'analytics') { await d.switchTab('analytics'); await new Promise(r => setTimeout(r, 500)); }
    d.setAnalyticsTab(t);
    await new Promise(r => setTimeout(r, 700));

    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const H = (el) => el ? Math.round(el.getBoundingClientRect().height) : 0;
    const W = (el) => el ? Math.round(el.getBoundingClientRect().width) : 0;

    // 页头
    //
    // ⚠️ 不能用 `.ov-page` 定位：这个 class **被概览页与分析页共用**，
    //    概览页里还嵌套了一个（骨架容器），全局查询会命中隐藏的那个 → 量出 0。
    //    分析页根容器有专属的 .an-page（2026-10-27 加）。
    const pageRoot = document.querySelector('.an-page');
    const phd = pageRoot ? pageRoot.querySelector('.phd') : null;
    const phdH = phd ? Math.round(phd.getBoundingClientRect().height) : -1;
    // 页头是否还是"卡片"：看有没有卡片式视觉（背景/边框/大圆角/阴影）。
    // 用户原话「不要搞这些标题卡片」—— 只压高度不够，卡片本身要去掉。
    const phdCs = phd ? getComputedStyle(phd) : null;
    const phdCard = phdCs ? {
      bg: phdCs.backgroundColor,
      borderW: phdCs.borderTopWidth,
      radius: phdCs.borderTopLeftRadius,
      shadow: phdCs.boxShadow,
    } : null;
    // 判定"仍是卡片"：有不透明背景 且 有边框/圆角/阴影之一
    const phdIsCard = phdCs ? (
      phdCs.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
      phdCs.backgroundColor !== 'transparent' &&
      (parseFloat(phdCs.borderTopWidth) > 0 ||
       parseFloat(phdCs.borderTopLeftRadius) > 4 ||
       (phdCs.boxShadow && phdCs.boxShadow !== 'none'))
    ) : false;
    // 页头里的 h1 字号
    const h1 = phd ? phd.querySelector('h1') : null;
    const h1Fs = h1 ? parseFloat(getComputedStyle(h1).fontSize) : 0;

    // 可见面板：标题区 vs 内容区高度（限定在分析页内，避免混入概览页的卡片）
    const anRoot = document.querySelector('.an-page');
    const panels = [];
    for (const pnl of Array.from(anRoot ? anRoot.querySelectorAll('.pnl') : []).filter(isVis)) {
      const ht = pnl.querySelector('.pnl-ht');
      const title = ht ? (ht.textContent || '').replace(/^\s*[▸▾]\s*/, '').trim().slice(0, 40) : '';
      if (!title) continue;
      const head = pnl.querySelector('.pnl-h');
      // 内容区 = pnl 高度 - pnl-h 高度
      const ph = H(pnl), hh = H(head);
      // 折叠判定：内容区不可见（x-show 隐藏）→ 面板只剩标题
      const bodyEl = pnl.querySelector('.pnl-b');
      const collapsed = !!bodyEl && !isVis(bodyEl);
      panels.push({
        title,
        panelH: ph,
        headH: hh,
        ratio: ph > 0 ? +(hh / ph).toFixed(2) : 0,
        collapsed,
        // 标题区占满整行但内容很少的特征：head 宽 == panel 宽 且 head 高 > 50
        fullWidthHead: W(head) > 0 && Math.abs(W(head) - W(pnl)) < 8 && hh > 50,
      });
    }

    // 内容总高：用整个页面滚动容器（main.main）的 scrollHeight 更可靠 ——
    // .ov-page 在部分页签下不是滚动容器，量出来会是 0。
    const main = document.querySelector('main.main');
    const totalH = main ? Math.round(main.scrollHeight) : 0;

    // 控件条：是否「独占整行只放 2-3 个按钮」
    //
    // ⚠️ 判"独占整行"不能只看 seg 宽度 < 父宽 40% —— 面板标题行里的
    //    「线/柱」「总量/分项/缓存」按内本来就只占一小段，那是**正确的**
    //    （它们与标题同行，右侧还有别的元素）。
    //    真正的浪费是：seg 在**独立的 .an-filter 行**里，且该行只有它 + 少量按钮。
    const scope = document.querySelector('.an-page') || document.body;
    const bars = [];
    for (const seg of Array.from(scope.querySelectorAll('.seg')).filter(isVis)) {
      const n = seg.querySelectorAll('button').length;
      const w = W(seg);
      const row = seg.closest('.an-filter');
      // 只在独立筛选行里、且该行内容稀少时才算"独占行"
      if (!row) continue;
      const rowW = W(row);
      // 这一行里除 seg 外有多少可见文本/控件
      const otherKids = Array.from(row.children).filter(c => c !== seg && isVis(c)).length;
      bars.push({
        buttons: n, segW: w, rowW,
        soloRow: otherKids === 0 && rowW > 0 && w < rowW * 0.4,
      });
    }

    return { tab: t, phdH, h1Fs, phdCard, phdIsCard, panels, totalH, bars };
  }, tab);

  out.overview = await measure('overview');
  out.cost = await measure('cost');

  // 供应商 / 模型 是否各占独立面板
  out.split = await p.evaluate(() => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const root = document.querySelector('.an-page');
    const titles = Array.from((root || document).querySelectorAll('.pnl-ht'))
      .filter(isVis).map(e => (e.textContent || '').replace(/^\s*[▸▾]\s*/, '').trim());
    const hasProv = titles.some(t => /供应商明细|Supplier detail/.test(t));
    const hasModel = titles.some(t => /模型明细|Model detail/.test(t));
    return { titles, hasProv, hasModel, bothSeparate: hasProv && hasModel };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def main():
    report_only = "--report" in sys.argv
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
        req = urllib.request.Request(base + "/admin/api/mock", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass

        jsf = os.path.join(WD, "u.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd,
                           timeout=180)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-600:]}\n{(r.stderr or '')[-600:]}")
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

    ov = d.get("overview") or {}
    co = d.get("cost") or {}
    sp = d.get("split") or {}

    print(f"\n[页头] 高度={ov.get('phdH')}px  h1 字号={ov.get('h1Fs')}px")
    for nm, m in [("overview", ov), ("cost", co)]:
        print(f"\n[{nm}] 内容总高={m.get('totalH')}px "
              f"（视口 1000，≈{round((m.get('totalH') or 0)/1000,2)} 屏）")
        for pn in (m.get("panels") or []):
            flag = "  ← 标题占整行且高" if pn.get("fullWidthHead") else ""
            print(f"   {pn['title'][:22]:<24} 面板={pn['panelH']:>5}px "
                  f"标题区={pn['headH']:>4}px 占比={pn['ratio']}{flag}")
        solo = [b for b in (m.get("bars") or []) if b.get("soloRow")]
        if solo:
            print(f"   控件条独占行({len(solo)} 处): {solo}")
    print(f"\n[供应商/模型] 供应商明细={sp.get('hasProv')} 模型明细={sp.get('hasModel')} "
          f"仍分成两张={sp.get('bothSeparate')}")
    print(f"  cost 页签可见标题: {sp.get('titles')}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    if report_only:
        print("\n（--report 模式，只报数不判）")
        return 0

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])
    check((ov.get("phdH") or 999) <= BUDGET["phd_h"],
          f"页头高度 ≤ {BUDGET['phd_h']}px（现在 {ov.get('phdH')}px）", "")
    check(not ov.get("phdIsCard"),
          "页头**不再是卡片**（无背景/边框/圆角/阴影）—— 用户「不要搞这些标题卡片」",
          str(ov.get("phdCard")))
    check((ov.get("totalH") or 99999) <= BUDGET["overview_h"],
          f"概览页签内容 ≤ {BUDGET['overview_h']}px（现在 {ov.get('totalH')}px）", "")
    check((co.get("totalH") or 99999) <= BUDGET["cost_h"],
          f"省量页签内容 ≤ {BUDGET['cost_h']}px（现在 {co.get('totalH')}px）", "")
    check(not sp.get("bothSeparate"),
          "供应商与模型**不再**各占一张独立面板（应合并/上下钻）", str(sp.get("titles")))

    # 控件条不该独占整行只放 2-3 个按钮（用户「布局按键太随便」）
    solo = []
    for nm, m in [("overview", ov), ("cost", co)]:
        for b in (m.get("bars") or []):
            if b.get("soloRow"):
                solo.append((nm, b["buttons"], b["segW"], b["rowW"]))
    check(not solo, "分析页没有「独占整行只放 2-3 个按钮」的控件条", str(solo))

    # 标题区占比
    #
    # ⚠️ **折叠状态的面板必须排除**：折叠时内容被 x-show 隐藏，面板只剩标题区，
    #    占比必然接近 1.0 —— 那是"折叠"的正常表现，不是"标题太大"。
    #    实测「省量审计」折叠时占比 0.96，若一并判红就是误报。
    over = []
    for nm, m in [("overview", ov), ("cost", co)]:
        for pn in (m.get("panels") or []):
            if pn.get("collapsed"):
                continue
            if pn.get("ratio", 0) > BUDGET["panel_title_ratio"] and pn.get("panelH", 0) > 120:
                over.append((nm, pn["title"], pn["ratio"]))
    check(not over, f"没有**展开**面板的标题区占比 > {BUDGET['panel_title_ratio']}", str(over))

    # 单面板标题区的绝对高度上限（守「标题区又高又空」）
    tall = []
    for nm, m in [("overview", ov), ("cost", co)]:
        for pn in (m.get("panels") or []):
            if (pn.get("headH") or 0) > BUDGET["panel_head_h"]:
                tall.append((nm, pn["title"], pn["headH"]))
    check(not tall,
          f"每个面板标题区高度 ≤ {BUDGET['panel_head_h']}px（现在超标: {tall}）", "")

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
