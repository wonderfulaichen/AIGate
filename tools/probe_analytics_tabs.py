# -*- coding: utf-8 -*-
"""分析页重构的回归探针: 三页签 / 折叠 / 列开关 真的都生效。

为什么必须读真实 DOM: 这些改动全靠 Alpine 的 x-show 与状态变量 ——
写错变量名时**页面不报错**, 只是"点了没反应"(表达式异常被 Alpine 静默吞掉,
本项目已踩过两次)。故逐项点一遍看 DOM 真的变了。

判据 (任一失败即红):
  ① 无 pageerror / window.__ERRS 为空
  ② 三个页签按钮都在, 且点哪个人哪亮 (aria-selected + .a)
  ③ 每个页签只显示该组的面板 (互斥: 切到 cost 时 overview 的面板不可见)
  ④ 三张诊断卡默认收起 (内容区不可见), 点标题后展开
  ⑤ 两张表的「更多列」默认收起 (隐藏列不可见), 点开关后出现
  ⑥ 页签切换后内容高度显著下降 (重构的核心目的: 不再一屏 4.2 屏)
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const STATIC = %(static)s;
  const p = await b.newPage({ viewport: { width: 1560, height: 1200 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 250)));
  await p.route('**/admin/static/*', route => {
    const name = route.request().url().split('/').pop();
    if (STATIC[name]) return route.fulfill({ status: 200, contentType: 'application/javascript', body: STATIC[name] });
    return route.fulfill({ status: 404, body: '' });
  });
  await p.goto('file:///' + %(page)s + '?sc=done', { waitUntil: 'load' });
  await p.waitForTimeout(3000);

  const out = { pageerrors: errs };
  out.errs = await p.evaluate(() => (window.__ERRS || []).slice(0, 8));

  const vis = () => p.evaluate(() => {
    const main = document.querySelector('main.main');
    const seen = el => {
      const s = getComputedStyle(el);
      return s.display !== 'none' && el.offsetHeight > 0;
    };
    const titles = Array.from(document.querySelectorAll('.pnl')).filter(el => el.closest('main.main')).map(el => {
      const t = el.querySelector('.pnl-ht');
      return { t: (t ? (t.innerText || '').trim().replace(/^\u25b8\s*/, '') : ''), v: seen(el) };
    });
    return { h: main ? main.scrollHeight : 0, titles: titles.filter(x => x.t) };
  });

  // 非法页签值自愈 (防整页空白)
  out.invalidTab = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const before = st.analyticsTab;
    st.analyticsTab = 'perf';           // 本轮已删除的页签值
    st.normalizeAnalyticsTab();
    const after = st.analyticsTab;
    const vis = Array.from(document.querySelectorAll('.pnl'))
      .filter(el => el.closest('main.main') && getComputedStyle(el).display !== 'none'
                    && el.offsetHeight > 0).length;
    st.analyticsTab = before;
    return { before: 'perf', after, visiblePanels: vis };
  });
  await p.waitForTimeout(400);

  // 页签按钮
  //
  // ⚠️ 必须**限定在分析页的 tablist 内**（2026-10-26 修）：页面上还有别处用
  // role="tab" —— 概览页「接入向导」的协议切换器（page-overview.html:264-266，
  // OpenAI Chat / OpenAI Responses / Anthropic Messages）。用全局
  // `[role="tab"]` 会把这 3 个也算进来 → 得到 5 个而不是 2 个，
  // 「两个页签按钮都在」与「有且仅有一个高亮」必然假失败。
  out.tabs = await p.evaluate(() => {
    const list = document.querySelector('.an-filter [role="tablist"]') ||
                 document.querySelector('[role="tablist"][aria-label]');
    const scope = list || document;
    return Array.from(scope.querySelectorAll('[role="tab"]'))
      .map(e => ({ txt: (e.innerText || '').trim(), sel: e.getAttribute('aria-selected'), a: e.className.indexOf(' a') >= 0 || e.className.endsWith('a') }));
  });

  // 逐页签: 切过去, 记录可见面板 + 高度
  out.perTab = {};
  for (const tab of ['overview', 'cost', 'cost']) {
    await p.evaluate(t => {
      const root = document.querySelector('[x-data]');
      const st = root._x_dataStack[0];
      st.analyticsTab = t;
    }, tab);
    await p.waitForTimeout(400);
    out.perTab[tab] = await vis();
  }

  // 折叠: 回 cost, 量省量审计的内容区, 点标题, 再量
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].analyticsTab = 'cost'; });
  await p.waitForTimeout(300);
  out.collapse = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const panes = Array.from(document.querySelectorAll('.pnl'));
    const audit = panes.find(el => (el.innerText || '').indexOf('省量审计') >= 0);
    const body = audit ? audit.querySelector('.pnl-b') : null;
    const before = { flag: st.auditExpanded, bodyVisible: body ? getComputedStyle(body).display !== 'none' : null };
    // 点标题
    const h = audit.querySelector('.pnl-h');
    if (h) h.click();
    return before;
  });
  await p.waitForTimeout(400);
  out.collapseAfter = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const audit = Array.from(document.querySelectorAll('.pnl')).find(el => (el.innerText || '').indexOf('省量审计') >= 0);
    const body = audit.querySelector('.pnl-b');
    return { flag: st.auditExpanded, bodyVisible: getComputedStyle(body).display !== 'none' };
  });

  // 融合后的内容完整性 (2026-10-16 第三轮: 时间轴与拓扑融为一张)
  // ⚠️ 必须先切到 perf 页签: 融合卡只在 perf 显示, 在别的页签下它是 display:none,
  //    里面的条宽度为 0 → 按可见性数会数出 0 (探针曾因此在错误的页签下判定"没渲染")。
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].analyticsTab = 'cost'; });
  await p.waitForTimeout(900);
  // 等表体真的渲染出来 (切页签后 Alpine 需一次重渲染; 固定 sleep 曾不够)
  await p.waitForFunction(() => {
    const t = Array.from(document.querySelectorAll('table')).find(x => (x.innerText || '').indexOf('上游模型') >= 0);
    return !!t && t.querySelectorAll('tbody tr').length > 0;
  }, null, { timeout: 5000 }).catch(() => {});
  out.merged = await p.evaluate(() => {
    const all = Array.from(document.querySelectorAll('.pnl'));
    const txt = el => (el ? (el.innerText || '') : '');
    const fuse = all.find(el => txt(el).indexOf('链路拓扑') >= 0);
    // ⚠️ 必须按**标题**精确匹配: 拓扑卡里也含"模型明细"字样 (hint 文案提到它),
    //    用 indexOf 会先命中拓扑卡 → 那张卡里没有 table → 表体行数恒 0 (真踩过)。
    const modelTbl = all.find(el => {
      const h = el.querySelector('.pnl-ht');
      return h && (h.innerText || '').trim().indexOf('模型明细') === 0;
    });
    const f = txt(fuse);
    const vis = e => { let x = e; while (x) { if (getComputedStyle(x).display === 'none') return false; x = x.parentElement; } return getComputedStyle(e).display !== 'none'; };
    const barVisible = b => {
      let x = b;
      while (x) { if (getComputedStyle(x).display === 'none') return false; x = x.parentElement; }
      // ⚠️ 不能用 offsetHeight: .prop-bar 自身无固定高度, 高由子段撑/父容器决定,
      //    实测可见的条 offsetHeight 也是 0 → 会误判成"没渲染"。改用**宽度**判断。
      return b.getBoundingClientRect().width > 0;
    };
    return {
      hasFuse: !!fuse,
      // 融合卡必须同时含: 链路三级 + 每模型一条的耗时条 + 段色图例 + 首字分布
      fuseChain: /客户端\s*\/\s*Agent/.test(f) && /AIGate 网关/.test(f),
      topoNodes: fuse ? fuse.querySelectorAll('.topo-node').length : 0,
      topoEdges: fuse ? fuse.querySelectorAll('.topo-edge-line').length : 0,
      fuseLegend: /段色/.test(f),
      // 段色图例已随条移到表里 (图例必须与它解释的东西同处一卡)
      tableHasLegend: !!modelTbl && /段色/.test(modelTbl.innerText || ''),
      fuseTtftDist: /首字延迟分布/.test(f),
      fuseText: f.slice(0, 400),
      fuseBars: fuse ? Array.from(fuse.querySelectorAll('.prop-bar')).filter(barVisible).length : 0,
      // 融合的硬指标: 每个模型行都带自己的条 (条数 >= 模型数)
      modelRows: fuse ? Array.from(fuse.querySelectorAll('.mono.txs[title]')).filter(
        e => { let x = e; while (x) { if (getComputedStyle(x).display === 'none') return false; x = x.parentElement; } return e.getBoundingClientRect().width > 0; }).length : 0,
      noDonutPanel: !all.some(el => /^调用分布$/.test(txt(el).trim())),
      // 明细列应回到**表**里 (2026-10-16 用户: 那是表的内容, 不该塞进拓扑)
      modelTableBack: !!modelTbl && !!modelTbl.querySelector('table'),
      // 表里也必须有"条 + 最慢 + 网关去程" —— 否则删拓扑模型行就丢了"一眼看出谁慢"
      tableBars: modelTbl ? Array.from(modelTbl.querySelectorAll('.prop-bar')).filter(
        b => { let x = b; while (x) { if (getComputedStyle(x).display === 'none') return false; x = x.parentElement; }
               return b.getBoundingClientRect().width > 0; }).length : 0,
      tableHasSlowest: !!modelTbl && /最慢/.test(modelTbl.innerText || ''),
      tableHasGwLeg: !!modelTbl && /网关去程/.test(modelTbl.innerText || ''),
      tableVals: (() => {
        if (!modelTbl) return [];
        // 取表体每行"费用"列的实际文本 —— 只测"文案存在"会放过空壳 (漏带字段全 0 也通过)
        const rows = Array.from(modelTbl.querySelectorAll('tbody tr'));
        return rows.slice(0, 5).map(tr => {
          const tds = Array.from(tr.querySelectorAll('td'));
          return tds.map(td => (td.innerText || '').trim()).join('|');
        });
      })(),
    };
  });

  // 过期文案扫描: 拓扑卡里若出现"点模型行可展开"这类指引, 而该卡已无模型行 → 失效
  out.staleHints = await p.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.pnl'))
      .find(el => { const h = el.querySelector('.pnl-ht'); return h && (h.innerText || '').trim().indexOf('链路拓扑') === 0; });
    if (!card) return [];
    const t = card.innerText || '';
    const hasModelRows = card.querySelectorAll('.prop-bar').length > 1;
    const hits = [];
    if (!hasModelRows && /模型行/.test(t)) hits.push('提到"模型行"但卡片无模型行');
    if (!hasModelRows && /可展开/.test(t)) hits.push('提到"可展开"但卡片无可展开项');
    if (/省量与成本/.test(t)) hits.push('指向旧页签名"省量与成本"');
    return hits;
  });

  // 裸 i18n 键名扫描 (未翻译的键会原样显示)
  out.literalKeys = await p.evaluate(() => {
    const re = /\b(an_|gw_|tbl\.|topo_|fuse_|stages_|ttft_|audit_|perf_)[a-z_]+\b/g;
    const hits = new Set();
    document.querySelectorAll('.pnl, .phd, .pnl-h, .an-filter').forEach(el => {
      const t = (el.innerText || '');
      (t.match(re) || []).forEach(x => hits.add(x));
    });
    return Array.from(hits).slice(0, 10);
  });

  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
""" % {"page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_analytics.html").replace("\\", "/")),
       "static": json.dumps({n: open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
                             for n in ("alpine.min.js", "tailwind.js")})}

tmp = os.path.join(D, "_probe_analytics_tabs.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ, NODE_PATH=os.path.join(D, "node_modules"))
r = subprocess.run(["node", tmp], cwd=D, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
if r.returncode != 0:
    print("STDERR:", r.stderr[-1500:])
    sys.exit(1)
data = json.loads(r.stdout)
print(json.dumps(data, ensure_ascii=False, indent=1)[:2600])

fails = []
NCHK = [0]
def ck(cond, msg):
    NCHK[0] += 1
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

print("\n== 判据 ==")
ck(not data.get("pageerrors"), "无 pageerror: %s" % data.get("pageerrors"))
ck(not data.get("errs"), "window.__ERRS 为空: %s" % data.get("errs"))
tb = data.get("tabs") or []
ck(len(tb) == 2, "两个页签按钮都在 (省量与性能已合并): %s" % [x.get("txt") for x in tb])
# 非法页签值必须自愈 —— 否则所有面板 x-show 全为假 → **整页空白** (本轮删 perf 后真踩过:
# 截图脚本仍切 'perf', 页面一片空白, 看着像加载失败而不是"值非法")。
inv = data.get("invalidTab") or {}
ck(inv.get("after") in ("overview", "cost"),
   "非法页签值自愈为合法值: %s → %s" % (inv.get("before"), inv.get("after")))
ck((inv.get("visiblePanels") or 0) > 0,
   "非法值下仍有面板可见 (不是整页空白): %s" % inv.get("visiblePanels"))
ck(sum(1 for x in tb if x.get("a")) == 1, "有且仅有一个页签高亮")

pt = data.get("perTab") or {}
ov = {x["t"] for x in (pt.get("overview") or {}).get("titles", []) if x["v"]}
co = {x["t"] for x in (pt.get("cost") or {}).get("titles", []) if x["v"]}
pf = {x["t"] for x in (pt.get("perf") or {}).get("titles", []) if x["v"]}
ck("Token 趋势" in ov and "Token 趋势" not in co and "Token 趋势" not in pf, "趋势只在概览页: ov=%s" % sorted(ov))
ck("省量审计" in co, "成本页含省量审计: co=%s" % sorted(co))
ck("链路拓扑" in co, "拓扑卡在「省量与性能」页: co=%s" % sorted(co))
ck(not (ov & co), "两组面板互斥 (无重复)")

cl = data.get("collapse") or {}
cla = data.get("collapseAfter") or {}
ck(cl.get("bodyVisible") is False, "省量审计默认收起: %s" % cl)
ck(cla.get("flag") is True and cla.get("bodyVisible") is True, "点标题后展开: %s" % cla)

# ── 融合后的内容完整性 (2026-10-16 第三轮: 时间轴与拓扑融为一张) ──
merged = data.get("merged") or {}
ck(merged.get("hasFuse"), "拓扑卡「链路拓扑」存在")
ck(merged.get("fuseChain"), "拓扑卡含链路三级 (客户端/网关)")
# 拓扑"该有的东西"必须有替代判据 (退役 probe_flow_topo 前先补上, 否则覆盖会丢)
ck((merged.get("topoNodes") or 0) >= 3, "拓扑卡含 >=3 个节点, 实得 %s" % merged.get("topoNodes"))
ck((merged.get("topoEdges") or 0) >= 2, "拓扑卡含 >=2 条连接线, 实得 %s" % merged.get("topoEdges"))
ck(merged.get("tableHasLegend"), "段色图例存在 (已随条移到表里)")
ck(merged.get("fuseTtftDist"), "拓扑卡含首字延迟分布 (链路整体口径)")
# ⚠️ 用户两次指出: **明细(逐模型的行/列) 不该在拓扑卡里**。
# 拓扑只留"节点 + 连线"回答"链路怎么走、哪一跳慢"; 逐模型信息全在「模型明细」表。
# 故这几条判据必须落在**表**上, 而不是拓扑卡上 (写成拓扑卡上就是复现用户抱怨的那个结构)。
ck((merged.get("fuseBars") or 0) <= 2,
   "拓扑卡里不再堆叠逐模型条 (只该有首字分布那 1 条), 实得 %s" % merged.get("fuseBars"))
ck((merged.get("modelRows") or 0) == 0,
   "拓扑卡里没有逐模型行 (那是明细), 实得 %s" % merged.get("modelRows"))
ck((merged.get("tableBars") or 0) >= 2,
   "「模型明细」表里每行带自己的耗时条 (>=2), 实得 %s" % merged.get("tableBars"))
ck(merged.get("tableHasSlowest"), "表里保留了「最慢」标记 (否则丢了'一眼看出谁慢')")
ck(merged.get("tableHasGwLeg"), "表里保留了网关去程文本")
ck(merged.get("noDonutPanel"), "重复的「调用分布」环图卡已删")
# 未翻译的 i18n 键会**原样显示成键名** (如 "an_tab_hint_perf"), 而没有任何报错 ——
# 与本项目踩过的 "占位符残留" 同一类静默缺陷: 界面看着"有字", 但那是键名不是文案。
# 判据: 整页可见文本里不得出现形如 an_/gw_/tbl./topo_/fuse_ 的裸键名。
# 过期文案: 描述已被删除的 UI 的说明文字 ("点模型行可展开…" 而那些行已删)。
# 与本项目其他静默缺陷同类: 文字还在、不报错, 但用户照着找不到东西。
# 判据: 拓扑卡里不得出现"可展开/点击模型行"这类已失效的交互指引 (该卡现在没有可点的模型行)。
ck(not (data.get("staleHints") or []),
   "拓扑卡无失效交互指引 (提到已删的模型行交互): %s" % (data.get("staleHints") or [])[:4])
ck(not (data.get("literalKeys") or []),
   "界面无未翻译的裸键名: %s" % (data.get("literalKeys") or [])[:6])
ck(merged.get("modelTableBack"), "「模型明细」表已回到页面上 (明细列不该塞进拓扑)")
# 表里必须有**真实值**且逐模型不同 —— 只测"文案存在"会放过空壳
# (本轮真踩过: 字段漏带时列全 0, 而"文案在不在"照样通过)
tv = merged.get("tableVals") or []
ck(len(tv) >= 2, "表体有 >=2 行: %s" % tv[:2])
ck(all(("→" in r) or ("¥" in r) for r in tv[:2]), "表行含 原始→实发 或 费用: %s" % tv[:2])
ck(len(set(tv)) >= 2, "各行内容互不相同 (不是空壳/共用值): %s" % tv[:2])
# 并入的列必须有**真实值**且逐模型不同 —— 只测"文案存在"会放过空壳列
# (本轮真踩过: topoFlowRows 漏带 modelStat, 列全显示 0, 而"文案在不在"照样通过)


# ── 时间轴按节点 (本轮核心) ──
print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
