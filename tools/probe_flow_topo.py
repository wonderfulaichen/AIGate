# -*- coding: utf-8 -*-
"""探针: 流程拓扑图 客户端 → 网关 → 供应商 → 模型(详情可展开)。

用户: "我说的是搞流程拓扑图，图中不是有供应商吗？供应商里面不是有模型吗？
      模型不应该有详情吗？"

三级结构缺一层都算没做到:
  ① 客户端节点 (总量/错误/成功率)
  ② 网关节点 (自身开销 p95 / 上游首字节 p95)
  ③ **每个供应商一个节点**, 且节点里列出**它自己的模型** (不是全站模型混在一起)
  ④ 模型节点可展开**自己的**耗时拆分 (四段 + 网关五段) —— "模型不应该有详情吗?"
  ⑤ 展开的详情必须是**该模型的**数, 不是全局值 (与上一轮的"假数"坑同一条纪律)

探针读真实 DOM + 点击交互后的状态, 不看源码。
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
  const p = await b.newPage({ viewport: { width: 1560, height: 1500 } });
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

  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].analyticsTab = 'perf'; });
  await p.waitForTimeout(500);

  // 拓扑结构: 供应商 → 它的模型
  out.topo = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const provs = st.topoProviders || [];
    return {
      providers: provs.map(x => x.provider),
      modelsByProv: Object.fromEntries(provs.map(x => [x.provider, x.models.map(m => m.model)])),
      totalModels: provs.reduce((a, x) => a + x.models.length, 0),
    };
  });

  // DOM: 供应商节点与模型行真的渲染出来了
  out.dom = await p.evaluate(() => {
    const all = Array.from(document.querySelectorAll('.pnl'));
    const card = all.find(e => (e.innerText || '').indexOf('流程拓扑') >= 0);
    if (!card) return { card: false };
    const txt = (card.innerText || '').replace(/\n+/g, ' | ');
    // 节点数: 客户端 + 网关 + 各供应商 (ov-secondary-item)
    return {
      card: true, nodes: card.querySelectorAll('.ov-secondary-item').length,
      hasClient: /客户端\s*\/\s*Agent/.test(txt), hasGateway: /AIGate 网关/.test(txt),
      text: txt.slice(0, 700),
    };
  });

  // ④ 展开某个模型 → 出现它自己的耗时拆分
  const first = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const pr = (st.topoProviders || [])[0];
    const m = pr && pr.models[0];
    return m ? { key: m.key, provider: m.provider, model: m.model } : null;
  });
  out.firstModel = first;
  out.beforeExpand = await p.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('流程拓扑') >= 0);
    // ⚠️ 必须只数**可见**的条: Alpine 的 x-show 只是 display:none, 节点仍在 DOM 里 ——
    //    只数总数会恒等 (13→13), 探针曾因此误报"详情没有内容"。
    const vis = Array.from(card.querySelectorAll('.prop-bar')).filter(b => {
      let e = b;
      while (e && e !== card) {
        if (getComputedStyle(e).display === 'none') return false;
        e = e.parentElement;
      }
      return getComputedStyle(b).display !== 'none' && b.offsetHeight > 0;
    });
    return { bars: vis.length };
  });
  if (first) {
    await p.evaluate(k => { document.querySelector('[x-data]')._x_dataStack[0].topoModelToggle(k); }, first.key);
    await p.waitForTimeout(450);
  }
  out.afterExpand = await p.evaluate(k => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('流程拓扑') >= 0);
    const vis = Array.from(card.querySelectorAll('.prop-bar')).filter(b => {
      let e = b;
      while (e && e !== card) {
        if (getComputedStyle(e).display === 'none') return false;
        e = e.parentElement;
      }
      return getComputedStyle(b).display !== 'none' && b.offsetHeight > 0;
    });
    const rows = st.topoModelStageRows({ provider: k.provider, model: k.model }) || [];
    const gw = st.topoModelGwRows({ provider: k.provider, model: k.model }) || [];
    return {
      open: st.topoModelOpen(k.key),
      bars: vis.length,
      stageVals: rows.filter(r => r.avg != null).map(r => r.k + ':' + r.avg),
      gwVals: gw.filter(r => r.avg != null).map(r => r.k + ':' + r.avg),
    };
  }, first || {key:'',provider:'',model:''});
  // 与全局值对比: 模型详情里若出现全局数, 就是"假数"
  out.global = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.axisNode = null;
    return (st.axisStageRows || []).filter(r => r.avg != null).map(r => r.k + ':' + r.avg);
  });
  // 供应商收起/展开
  out.provToggle = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const countModels = () => {
      const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('流程拓扑') >= 0);
      // 数"可见的模型详情展开区"= 可见的 prop-bar 数
      return Array.from(card.querySelectorAll('.prop-bar')).filter(b => {
        let e = b;
        while (e && e !== card) { if (getComputedStyle(e).display === 'none') return false; e = e.parentElement; }
        return getComputedStyle(b).display !== 'none' && b.offsetHeight > 0;
      }).length;
    };
    const before = countModels();
    const def = st.topoProvOpen('provA');
    st.topoProvToggle('provA');
    return { defaultOpen: def, visibleModelsBefore: before };
  });
  await p.waitForTimeout(450);
  const after = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const open = st.topoProvOpen('provA');
    const card = Array.from(document.querySelectorAll('.pnl')).find(e => (e.innerText || '').indexOf('流程拓扑') >= 0);
    const n = Array.from(card.querySelectorAll('.prop-bar')).filter(b => {
      let e = b;
      while (e && e !== card) { if (getComputedStyle(e).display === 'none') return false; e = e.parentElement; }
      return getComputedStyle(b).display !== 'none' && b.offsetHeight > 0;
    }).length;
    return { afterToggle: open, visibleModelsAfter: n };
  });
  Object.assign(out.provToggle, after);

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
    print("STDERR:", r.stderr[-1500:])
    sys.exit(1)
data = json.loads(r.stdout)
print(json.dumps(data, ensure_ascii=False, indent=1)[:2400])

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
# ① ② 客户端与网关节点
ck(dm.get("hasClient"), "有客户端节点")
ck(dm.get("hasGateway"), "有网关节点")
# ③ 供应商节点 + 每个供应商列出**它自己的**模型
ck(len(tp.get("providers") or []) >= 2, "有多个供应商节点: %s" % tp.get("providers"))
byp = tp.get("modelsByProv") or {}
ck(all(len(v) >= 1 for v in byp.values()), "每个供应商节点下都有模型: %s" % byp)
# 模型不能跨供应商串 (每个模型只属于自己的供应商)
allm = [m for v in byp.values() for m in v]
ck(len(allm) == len(set(allm)), "模型不跨供应商重复: %s" % allm)
ck((dm.get("nodes") or 0) >= 4, "DOM 里节点数 >=4 (客户端+网关+供应商): %s" % dm.get("nodes"))
# ④ 模型详情可展开
fm = data.get("firstModel") or {}
be = data.get("beforeExpand") or {}
ae = data.get("afterExpand") or {}
ck(bool(fm), "取到第一个模型节点: %s" % fm)
ck(ae.get("open") is True, "点模型后展开态为真")
ck((ae.get("bars") or 0) > (be.get("bars") or 0),
   "展开后多出比例条 (详情真的有内容): %s → %s" % (be.get("bars"), ae.get("bars")))
ck((ae.get("stageVals") or []) and (ae.get("gwVals") or []),
   "详情含模型四段与网关五段: %s / %s" % (ae.get("stageVals"), ae.get("gwVals")))
# ⑤ 详情必须是该模型的数, 不是全局值
gl = data.get("global") or []
ck(ae.get("stageVals") != gl,
   "模型详情与全局值不同 (没拿全局冒充节点): 模型 %s vs 全局 %s" % (ae.get("stageVals"), gl))
# ⑥ 供应商可收起 (默认展开 —— 两级结构要一眼看全, 但收起要能用)
pv = data.get("provToggle") or {}
ck(pv.get("defaultOpen") is True, "供应商默认展开: %s" % pv.get("defaultOpen"))
ck(pv.get("afterToggle") is False, "点供应商标题后收起: %s" % pv.get("afterToggle"))
ck((pv.get("visibleModelsAfter") or 0) < (pv.get("visibleModelsBefore") or 0),
   "收起后其模型行确实不可见: %s → %s" % (pv.get("visibleModelsBefore"), pv.get("visibleModelsAfter")))

print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
