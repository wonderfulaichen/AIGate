# -*- coding: utf-8 -*-
"""实测记录详情的两条瀑布 + 分析页的网关面板/拓扑**真的渲染出来了** —— 光看源码不算数。

为什么需要这个探针: 「时间去哪了」/「网关去程」两条瀑布的 HTML 是**字符串拼出来**再交给
`x-html` 的, 拼串函数里任何异常都会被 Alpine 的 `with (scope) { ... }` 求值器 try/catch
吞掉 —— 结果是 `x-html` 保持空串, 页面上什么都不显示, 而控制台/源码都看不出问题。
(2026-10-15 真踩过: 拼串里裸调 `fmtMs(...)`, 而它是 data 对象的**方法**不是全局函数,
在 `with` 之外的方法体里根本不可见 → ReferenceError → 图例整块空白。用本探针的
`window.__ERRS` 才定位到。护栏: 该错误改回裸调用时本探针 6 项判据转红。)

判据 (任一失败即红):
  记录详情 (admin_recorddetail.html)
  ① 无 pageerror / window.__ERRS 为空
  ② 四个拼串函数均不抛异常, 且输出非空串
  ③ msText(0)=='0ms' (实测 0 当实测), fmtMs(0)=='—' (延迟列口径不变)
  ④ DOM 里两条 .prop-bar 各含 >=1 段, 图例圆点有底色
  ⑤ 未采集段显示 "—"; 直通的协议转换段显示「直通」
  分析页 (admin_analytics.html)
  ⑥ ?sc=done: 「网关自身开销」面板可见、比例条有段、文字含 p95 15ms/980ms
  ⑦ ?sc=done: 「链路拓扑」可见, 节点 >=4, 含窗口量 42 与各提供商
  ⑧ ?sc=empty: 网关面板**整个不出现** (无样本不拿 0 充数)

用法: python tools/build_preview.py && python tools/probe_stage_waterfall.py
"""
import os
import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  // 预览页里 <script defer src="/admin/static/..."> 是**绝对路径** (线上由 Rust 提供),
  // 直接 file:// 打开会两个 ERR_FILE_NOT_FOUND → Alpine 根本没加载 → 整页是死的.
  // 故把这两个静态资源就地喂进去, 预览才等价于线上。
  const STATIC = %(static)s;
  async function prep() {
    const p = await b.newPage({ viewport: { width: 1700, height: 1100 } });
    const errs = [];
    p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
    // 预览页里 <script defer src="/admin/static/..."> 是**绝对路径** (线上由 Rust 提供),
    // 直接 file:// 打开会两个 ERR_FILE_NOT_FOUND → Alpine 根本没加载 → 整页是死的.
    // 故把这两个静态资源就地喂进去, 预览才等价于线上。
    await p.route('**/admin/static/*', route => {
      const name = route.request().url().split('/').pop();
      if (STATIC[name]) return route.fulfill({ status: 200, contentType: 'application/javascript', body: STATIC[name] });
      return route.fulfill({ status: 404, body: '' });
    });
    return { p, errs };
  }

  // ── 记录详情变体: 两条瀑布 ──
  const r1 = await prep();
  await r1.p.goto('file:///' + %(rd_page)s, { waitUntil: 'load' });
  await r1.p.waitForTimeout(2500);
  const rd = { pageerrors: r1.errs, errs: await r1.p.evaluate(() => (window.__ERRS || []).slice(0, 10)) };

  // Alpine 状态: 详情弹窗确实开在第一条记录上, 且拼串函数能跑通
  rd.state = await r1.p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (!st) return { ok: false };
    const log = st.selectedLog;
    let probe = {};
    // 直接调四个拼串函数: 有没有抛异常、出来的是不是空串, 一步看清
    try { probe.stageBar = String(st.stageBarHtml(log)); } catch (e) { probe.stageBarErr = String(e); }
    try { probe.stageLegend = String(st.stageLegendHtml(log)); } catch (e) { probe.stageLegendErr = String(e); }
    try { probe.gwBar = String(st.gwBarHtml(log)); } catch (e) { probe.gwBarErr = String(e); }
    try { probe.gwLegend = String(st.gwLegendHtml(log)); } catch (e) { probe.gwLegendErr = String(e); }
    return { ok: true, hasLog: !!log, probe, msText0: String(st.msText(0)), fmtMs0: String(st.fmtMs(0)) };
  });

  // 真实 DOM: 弹窗里两条 .prop-bar 各画了几个段、图例文字是什么
  rd.dom = await r1.p.evaluate(() => {
    const modal = document.querySelector('.log-modal-card');
    if (!modal) return { modal: false };
    const bars = Array.from(modal.querySelectorAll('.prop-bar')).map(b => ({
      segs: b.querySelectorAll('span[style*="flex-grow"]').length,
      html: b.innerHTML.slice(0, 60),
    }));
    // 图例: 每条图例项取文字 + 圆点底色
    const legends = [];
    modal.querySelectorAll('span').forEach(s => {
      if (s.children.length !== 2) return;
      const dot = s.querySelector('i');
      if (!dot) return;
      const bg = getComputedStyle(dot).backgroundColor;
      const txt = (s.textContent || '').replace(/\s+/g, ' ').trim();
      if (txt) legends.push({ txt, bg });
    });
    return { modal: true, bars, legends, text: (modal.innerText || '').replace(/\n+/g, ' | ').slice(0, 900) };
  });

  // 请求体字节行 (2026-10-16, 用户报障"省量对不上上游"): 必须真的渲染出"原始 → 实发"。
  // 这是用户唯一能自查省量的地方 —— 它若空白/报错, 用户就只能相信省量那一栏。
  rd.reqBytes = await r1.p.evaluate(() => {
    const modal = document.querySelector('.log-modal-card');
    if (!modal) return { modal: false };
    const keys = Array.from(modal.querySelectorAll('.log-detail-key'));
    const k = keys.find(e => (e.innerText || '').indexOf('请求体') >= 0);
    if (!k) return { row: false, text: (modal.innerText || '').slice(0, 200) };
    const val = k.parentElement.querySelector('.log-detail-val');
    return { row: true, key: (k.innerText || '').trim(), val: val ? (val.innerText || '').trim() : null,
             title: k.getAttribute('title') || '' };
  });

  // ── 分析页变体: 耗时拆分 (客户端 + 网关两侧) + 链路拓扑 ──
  // sc=done 才有 gateway_stages / per_provider; sc=empty 用来验证"无样本时该侧整个不出现"
  // (而不是画一排 0 / 0ms 的假数据)。
  //
  // ⚠️ 结构变了两次, 探针必须跟着走, 否则会一直"假失败":
  //   2026-10-16 ①: 分析页分三个页签, 这两块挪到「性能与延迟」页且默认折叠
  //   2026-10-16 ②: 「时间去哪了」与「网关自身开销」合并成一张「耗时拆分」卡 (上下两行),
  //                  拓扑里逐提供商列表也已去掉 (改指向供应商表)
  // 故这里: 切页签 → 展开 → 读合并卡里的**网关侧那一行**。
  async function analytics(q) {
    const r = await prep();
    await r.p.goto('file:///' + %(an_page)s + q, { waitUntil: 'load' });
    await r.p.waitForTimeout(2500);
    const o = { pageerrors: r.errs, errs: await r.p.evaluate(() => (window.__ERRS || []).slice(0, 10)) };
    o.expand = await r.p.evaluate(() => {
      const st = document.querySelector('[x-data]')._x_dataStack[0];
      st.analyticsTab = 'perf';
      // 两张诊断卡都要展开才读得到正文 (默认收起是设计)
      st.gwTopoExpanded = true;
      st.auditExpanded = true;
      return { topo: false };
    });
    await r.p.waitForTimeout(500);
    o.dom = await r.p.evaluate(() => {
      const all = Array.from(document.querySelectorAll('.pnl'));
      const txt = el => (el ? (el.innerText || '') : '');
      // 合并卡: 「耗时拆分」(含客户端行 + 网关行)
      const split = all.find(el => /耗时拆分/.test(txt(el)));
      const gwRow = split ? Array.from(split.querySelectorAll('.prop-bar'))[1] || null : null;
      const topo = all.find(el => /链路拓扑/.test(txt(el)));
      const vis = e => !!e && getComputedStyle(e).display !== 'none';
      return {
        splitPanel: !!split, splitVisible: vis(split),
        splitRows: split ? (txt(split).match(/客户端视角|网关视角/g) || []) : [],
        // 第 2 条 bar 是网关侧 (第 1 条是客户端侧)
        gwBars: gwRow ? gwRow.querySelectorAll('span[style*="flex-grow"]').length : 0,
        gwText: split ? txt(split).replace(/\n+/g, ' | ').slice(0, 600) : '',
        topoPanel: !!topo, topoVisible: vis(topo),
        topoText: txt(topo).replace(/\n+/g, ' | ').slice(0, 700),
        topoNodes: topo ? topo.querySelectorAll('.ov-secondary-item').length : 0,
      };
    });
    o.collapsedByDefault = o.expand;
    return o;
  }
  const anDone = await analytics('?sc=done');
  const anEmpty = await analytics('?sc=empty');

  console.log(JSON.stringify({ rd, anDone, anEmpty }, null, 2));
  await b.close();
})();
""" % {"rd_page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_recorddetail.html").replace("\\", "/")),
       "an_page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_analytics.html").replace("\\", "/")),
       "static": json.dumps({
           n: open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
           for n in ("alpine.min.js", "tailwind.js")})}

tmp = os.path.join(D, "_probe_stage_waterfall.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ)
env["NODE_PATH"] = os.path.join(D, "node_modules")
r = subprocess.run(["node", tmp], cwd=D, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
print(r.stdout)
if r.returncode != 0:
    print("STDERR:", r.stderr[-2000:])
    sys.exit(1)

try:
    data = json.loads(r.stdout)
except Exception:
    print("!! 探针输出不是 JSON, 无法判定")
    sys.exit(1)

fails = []
NCHK = [0]
def ck(cond, msg):
    NCHK[0] += 1
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

rd = data.get("rd") or {}
st = rd.get("state") or {}
pr = st.get("probe") or {}
dom = rd.get("dom") or {}
legends = dom.get("legends") or []
bars = dom.get("bars") or []
an = data.get("anDone") or {}
ad = an.get("dom") or {}
ae = (data.get("anEmpty") or {}).get("dom") or {}

print("== 记录详情: 两条瀑布 ==")
ck(not rd.get("pageerrors"), "无 pageerror: %s" % rd.get("pageerrors"))
ck(not rd.get("errs"), "window.__ERRS 为空: %s" % rd.get("errs"))
ck(st.get("hasLog"), "详情弹窗有 selectedLog")
# 拼串函数本身必须能跑通 (这是空白图例的根因所在)
ck(not any(k.endswith("Err") for k in pr),
   "四个拼串函数均无异常: " + str({k: v for k, v in pr.items() if k.endswith("Err")}))
ck("flex-grow" in (pr.get("stageBar") or ""), "「时间去哪了」拼串含段: %r" % (pr.get("stageBar") or "")[:90])
ck("flex-grow" in (pr.get("gwBar") or ""), "「网关去程」拼串含段: %r" % (pr.get("gwBar") or "")[:90])
# msText: 0 是实测值, null 才是未采集
ck(st.get("msText0") == "0ms", "msText(0) == '0ms' (实测 0 不当未采集), 实得 %r" % st.get("msText0"))
ck(st.get("fmtMs0") == "—", "fmtMs(0) 仍为 '—' (延迟列口径不变), 实得 %r" % st.get("fmtMs0"))
# DOM 上两条瀑布都真的画出了段
ck(len(bars) >= 2, "弹窗里可见 >=2 条 .prop-bar, 实得 %d" % len(bars))
ck(all(b.get("segs", 0) >= 1 for b in bars[:2]) if len(bars) >= 2 else False,
   "两条瀑布各含 >=1 个段, 实得 %s" % [b.get("segs") for b in bars])
ck(any("310ms" in (l.get("txt") or "") for l in legends),
   "图例出现实测值 310ms, 实得 %s" % [l.get("txt") for l in legends][:8])
ck(any("—" in (l.get("txt") or "") for l in legends), "未采集段显示 '—'")
ck(any("直通" in (l.get("txt") or "") for l in legends), "直通的协议转换段显示「直通」")
ck(any(l.get("bg") not in ("rgba(0, 0, 0, 0)", "transparent", "") for l in legends),
   "图例圆点有底色 (段颜色真的生效)")
# 请求体字节行: 用户自查省量的唯一入口 (2026-10-16 加)
rb = rd.get("reqBytes") or {}
ck(rb.get("row"), "详情里有「请求体」行 (key=%r)" % rb.get("key"))
ck("2.2MB" in (rb.get("val") or "") and "1.1MB" in (rb.get("val") or ""),
   "该行给出 原始 → 实发 两个实测值: %r" % rb.get("val"))
ck("省" in (rb.get("val") or ""), "该行含缩减量措辞: %r" % rb.get("val"))
ck((rb.get("title") or "") != "", "该行有 tooltip 说明 (口径可发现)")
# ⚠️ 占位符残留检查 (真踩过): i18n 的 t() 用 `{0}`/`{1}`, 不是 `%s` —— 写错时页面会
# 直接显示字面量 "省 %s · %s%", 比空白更糟 (看着就像坏了)。而"关键词都存在"的弱判据
# **抓不住它**, 故这里单独钉一条: 渲染结果里不得出现任何未替换的占位符。
ALLTXT = (rb.get("val") or "") + "|" + (rd.get("dom") or {}).get("text", "")
ck("%s" not in ALLTXT and "{0}" not in ALLTXT and "{1}" not in ALLTXT,
   "无未替换的占位符 (%s / {0} / {1})")

print("\n== 分析页: 耗时拆分 (两侧) + 链路拓扑 ==")
ck(not an.get("pageerrors"), "无 pageerror: %s" % an.get("pageerrors"))
ck(not an.get("errs"), "window.__ERRS 为空: %s" % an.get("errs"))
# 诊断卡默认收起 (2026-10-16) —— 初始态必须为 false, 否则"默认收起"没生效
ec = an.get("collapsedByDefault") or {}
ck(ec.get("topo") is False, "链路拓扑默认收起: %s" % ec)
ck(ad.get("splitVisible"), "'耗时拆分' 卡可见 (有样本就该显示)")
# 合并卡必须**同时**含客户端行与网关行 —— 这是合并后最容易丢的一半
ck(sorted(set(ad.get("splitRows") or [])) == ["客户端视角", "网关视角"],
   "合并卡同时含两侧行: %s" % ad.get("splitRows"))
ck((ad.get("gwBars") or 0) >= 1, "网关侧比例条含 >=1 个段, 实得 %s" % ad.get("gwBars"))
# 桩里 overhead p95=15 / ttfb p95=980; 两段都采到了就必须在文字里出现
ck("15ms" in (ad.get("gwText") or "") and "980ms" in (ad.get("gwText") or ""),
   "卡内给出 p95 15ms / 980ms, 实得 %r" % (ad.get("gwText") or "")[:160])
# 参考卡点: 桩里护栏均值 0.4ms → 取整为 0。这是**实测 0**, 必须显示 "0ms" 而不是 "—",
# 否则窗口级面板上会有一格看起来"没采集"(而它其实是全窗口最快的一段)。
ck("0ms" in (ad.get("gwText") or ""), "实测 0ms 显示 '0ms' 而非 '—', 实得 %r" % (ad.get("gwText") or "")[:200])
ck(ad.get("topoVisible"), "'链路拓扑' 面板可见")
# 拓扑只留链式三跳 (客户端 → 网关 → 提供商); 逐提供商明细已移至供应商表 (2026-10-16 精简)
ck((ad.get("topoNodes") or 0) == 3, "拓扑为三跳节点 (客户端/网关/提供商), 实得 %s" % ad.get("topoNodes"))
tt = ad.get("topoText") or ""
ck("42" in tt and "p95" in tt, "拓扑给出窗口量 42 与 p95, 实得 %r" % tt[:200])
ck("供应商表" in tt, "拓扑指向供应商表而非重复列表, 实得 %r" % tt[-160:])
# 关键负例: 无样本时该侧必须**整个不出现**, 而不是画一排 0 / 0ms 的假数据。
# 合并卡里两侧各自 x-show —— 故判据是"网关行不出现", 不是"整张卡消失"。
ck("网关视角" not in (ae.get("gwText") or ""),
   "无样本时网关行整个不出现: %r" % (ae.get("gwText") or "")[:200])
ck(not (ae.get("errs") or []), "空场景无表达式异常: %s" % ae.get("errs"))

print("\n== 记录详情图例 ==")
for l in legends[:12]:
    print("   %-28s %s" % (l.get("txt"), l.get("bg")))
print("\n== 弹窗文本片段 ==\n   " + str(dom.get("text"))[:400])
print("\n== 网关面板文本 ==\n   " + str(ad.get("gwText"))[:400])
print("\n== 拓扑文本 ==\n   " + str(ad.get("topoText"))[:500])

print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
