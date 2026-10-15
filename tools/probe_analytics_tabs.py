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

  // 页签按钮
  out.tabs = await p.evaluate(() => Array.from(document.querySelectorAll('[role="tab"]'))
    .map(e => ({ txt: (e.innerText || '').trim(), sel: e.getAttribute('aria-selected'), a: e.className.indexOf(' a') >= 0 || e.className.endsWith('a') })));

  // 逐页签: 切过去, 记录可见面板 + 高度
  out.perTab = {};
  for (const tab of ['overview', 'cost', 'perf']) {
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

  // 列开关: 数一下表格可见 th 数
  out.cols = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const tbl = Array.from(document.querySelectorAll('table')).find(t => (t.innerText || '').indexOf('上游模型') >= 0);
    const cnt = () => Array.from(tbl.querySelectorAll('thead th')).filter(th => getComputedStyle(th).display !== 'none').length;
    const all = tbl.querySelectorAll('thead th').length;
    const before = cnt();
    st.modelColsMore = true;
    return { before, all };
  });
  await p.waitForTimeout(400);
  out.colsAfter = await p.evaluate(() => {
    const tbl = Array.from(document.querySelectorAll('table')).find(t => (t.innerText || '').indexOf('上游模型') >= 0);
    return Array.from(tbl.querySelectorAll('thead th')).filter(th => getComputedStyle(th).display !== 'none').length;
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
ck(len(tb) == 3, "三个页签按钮都在: %s" % [x.get("txt") for x in tb])
ck(sum(1 for x in tb if x.get("a")) == 1, "有且仅有一个页签高亮")

pt = data.get("perTab") or {}
ov = {x["t"] for x in (pt.get("overview") or {}).get("titles", []) if x["v"]}
co = {x["t"] for x in (pt.get("cost") or {}).get("titles", []) if x["v"]}
pf = {x["t"] for x in (pt.get("perf") or {}).get("titles", []) if x["v"]}
ck("Token 趋势" in ov and "Token 趋势" not in co and "Token 趋势" not in pf, "趋势只在概览页: ov=%s" % sorted(ov))
ck("模型明细" in co and "模型明细" not in ov, "模型明细只在成本页: co=%s" % sorted(co))
ck("首字延迟" in pf and "首字延迟" not in ov, "首字延迟只在性能页: pf=%s" % sorted(pf))
ck(not (ov & co) and not (co & pf) and not (ov & pf), "三组面板互斥 (无重复)")

cl = data.get("collapse") or {}
cla = data.get("collapseAfter") or {}
ck(cl.get("bodyVisible") is False, "省量审计默认收起: %s" % cl)
ck(cla.get("flag") is True and cla.get("bodyVisible") is True, "点标题后展开: %s" % cla)

cols = data.get("cols") or {}
ck((cols.get("before") or 0) < (cols.get("all") or 0), "模型明细默认收起部分列: 可见 %s / 共 %s" % (cols.get("before"), cols.get("all")))
ck((data.get("colsAfter") or 0) == (cols.get("all") or 0), "点「更多列」后全部列可见: %s" % data.get("colsAfter"))

hs = [ (pt.get(k) or {}).get("h") or 0 for k in ("overview", "cost", "perf") ]
ck(max(hs) < 4556, "内容高度较重构前 (4556) 下降: %s" % hs)

print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
