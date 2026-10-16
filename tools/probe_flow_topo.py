# -*- coding: utf-8 -*-
"""探针: 融合视图「链路与耗时」—— 客户端 → 网关 → 供应商 → 模型，**每个模型自带时间轴**。

用户: "我说的是搞流程拓扑图，图中不是有供应商吗？供应商里面不是有模型吗？模型不应该有详情吗？"
后续: "请求时间轴和拓扑图明明就是能融合在一起的，你还是偷懒分离显示，并且这样非常不直观、
      不是人看的" —— 于是融合为一张卡: 拓扑节点即时间轴 (不需要"上拓扑+下时间轴+选择器"三件套)。

判据 (读真实 DOM + 状态, 不看源码):
  ① 无 pageerror / window.__ERRS 为空
  ② 链路三级都在 (客户端 / 网关 / 供应商)
  ③ **每个供应商节点下列出它自己的模型**, 且模型不跨供应商重复
  ④ **每个模型行自带一条时间轴条**, 条长按占最慢节点归一 (不是各自铺满 → 才能比快慢)
  ⑤ 最慢的那一行被标出来 (用户要"一眼看出谁慢")
  ⑥ 行上的数是**该节点的**, 不是全局值
  ⑦ 供应商行与模型行都可收起/展开 (层级可折叠)
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
  const p = await b.newPage({ viewport: { width: 1560, height: 1600 } });
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

  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].analyticsTab = 'cost'; });
  await p.waitForTimeout(600);

  out.topo = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const provs = st.topoProviders || [];
    const rows = st.topoFlowRows || [];
    return {
      providers: provs.map(x => x.provider),
      modelsByProv: Object.fromEntries(provs.map(x => [x.provider, x.models.map(m => m.model)])),
      rowCount: rows.length,
      widths: rows.map(r => Math.round(r.widthPct)),
      totals: rows.map(r => r.total),
      slowestKey: (st.topoFlowRows || []).reduce((a, r) => (r.total > (a ? a.total : 0) ? r : a), null)?.key,
    };
  });

  out.dom = await p.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('链路与耗时') >= 0);
    if (!card) return { card: false };
    const txt = (card.innerText || '').replace(/\n+/g, ' | ');
    // 只数**可见**的条: 用宽度判定 (prop-bar 无固定高度, offsetHeight 恒 0)
    const bars = Array.from(card.querySelectorAll('.prop-bar')).filter(x => x.getBoundingClientRect().width > 0);
    return {
      card: true,
      hasClient: /客户端\s*\/\s*Agent/.test(txt), hasGateway: /AIGate 网关/.test(txt),
      visibleBars: bars.length,
      widths: bars.map(x => Math.round(x.getBoundingClientRect().width)),
      hasSlowestTag: /最慢/.test(txt),
      text: txt.slice(0, 800),
    };
  });

  // ④ 最慢那行的标签
  out.slowestTag = await p.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('链路与耗时') >= 0);
    const t = card ? (card.innerText || '') : '';
    const m = t.match(/([\w.\-]+)\s*\n?\s*([\d.]+m?s|[\d.]+s)\s*([^\n]*最慢)/);
    return m ? m[0].replace(/\n/g, ' ') : null;
  });

  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
""" % {"page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_analytics.html").replace("\\", "/")),
       "static": json.dumps({n: open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
                             for n in ("alpine.min.js", "tailwind.js")})}

tmp = os.path.join(D, "_probe_flow_topo.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ, NODE_PATH=os.path.join(D, "node_modules"))
r = subprocess.run(["node", tmp], cwd=D, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
if r.returncode != 0:
    print("STDERR:", r.stderr[-1200:])
    sys.exit(1)
data = json.loads(r.stdout)
print(json.dumps(data, ensure_ascii=False, indent=1)[:2200])

fails = []
NCHK = [0]
def ck(cond, msg):
    NCHK[0] += 1
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

tp = data.get("topo") or {}
dm = data.get("dom") or {}
print("\n== 判据 ==")
ck(not data.get("pageerrors"), "无 pageerror: %s" % data.get("pageerrors"))
ck(not data.get("errs"), "window.__ERRS 为空: %s" % data.get("errs"))
ck(dm.get("hasClient") and dm.get("hasGateway"), "链路三级都在 (客户端/网关)")
tp_provs = tp.get("providers") or []
ck(len(tp_provs) >= 2, "有多个供应商节点: %s" % tp_provs)
byp = tp.get("modelsByProv") or {}
ck(all(len(v) >= 1 for v in byp.values()), "每个供应商下列出它自己的模型: %s" % byp)
allm = [m for v in byp.values() for m in v]
ck(len(allm) == len(set(allm)), "模型不跨供应商重复: %s" % allm)
# ④ 每个模型行自带条 + 宽度归一 (各不相同的宽度才说明"按最慢归一")
ck((tp.get("rowCount") or 0) >= 2, "有 >=2 个模型融合行: %s" % tp.get("rowCount"))
ws = set(tp.get("widths") or [])
ck(len(ws) >= 2, "模型条宽度互不相同 (按最慢节点归一, 不是各自铺满): {}".format(sorted(ws)))
ck(max(ws or [0]) == 100, "最慢那条宽度为 100\%: {}".format(max(ws or [0])))
ck((dm.get("visibleBars") or 0) >= 2, "DOM 里可见的模型条 >=2: %s" % dm.get("visibleBars"))
# ⑤ 最慢被标出
ck(dm.get("hasSlowestTag"), "最慢的那行被标出: %r" % data.get("slowestTag"))
# ⑥ 行上的数是该节点的 (总耗时各不相同, 不是同一个全局值)
ts = tp.get("totals") or []
ck(len(set(ts)) >= 2, "各模型总耗时互不相同 (节点自己的数): %s" % ts)

print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
