# -*- coding: utf-8 -*-
"""实测记录页渲染行数 —— 区分「网关没记够」与「前端没渲染够」。

量三件事:
  ① Alpine 状态里 logs 数组长度 (前端实际拿到的记录数)
  ② 记录表格 tbody 的行数 (实际渲染的行数)
  ③ 页脚「共 N 条 / 第 p/共 M 页」与每页条数
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG = 8787

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1700, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto('http://127.0.0.1:%(pg)d/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2000);

  // 点顶栏「记录」页签
  const clicked = await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent || '').trim() === '记录');
    if (t) { t.click(); return true; }
    return false;
  });
  await p.waitForTimeout(3000);

  const info = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    // 记录页表格: 找带 log 行点击处理的那张表
    let rows = [];
    const tables = Array.from(document.querySelectorAll('table'));
    for (const t of tables) {
      const rs = Array.from(t.querySelectorAll('tbody tr'));
      if (rs.length > rows.length) rows = rs;
    }
    const foot = Array.from(document.querySelectorAll('.fb,.foot,p,span'))
      .map(e => (e.textContent || '').trim())
      .filter(s => /共|条|页/.test(s) && s.length < 120);
    return {
      alpine_logs_len: st ? (st.logs || []).length : null,
      alpine_pageSize: st ? st.pageSize : null,
      alpine_logPage: st ? st.logPage : null,
      alpine_filtered_len: st ? (st.filteredLogs || []).length : null,
      alpine_timeFilter: st ? st.logTimeFilter : null,
      alpine_statusFilter: st ? st.logStatusFilter : null,
      alpine_provFilter: st ? st.logProviderFilter : null,
      table_rows_rendered: rows.length,
      sample_rows: rows.slice(0, 5).map(r => (r.innerText || '').replace(/\s+/g, ' ').slice(0, 90)),
      footer_texts: foot.slice(0, 6),
      activeTab: st ? st.activeTab : null,
    };
  });
  console.log(JSON.stringify({ clicked, errs, info }, null, 2));
  await b.close();
})();
""" % {"pg": PG}

d = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")
tmp = os.path.join(d, "_probe_rows2.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ)
env["NODE_PATH"] = os.path.join(d, "node_modules")
r = subprocess.run(["node", tmp], cwd=d, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
print(r.stdout)
if r.returncode != 0:
    print("STDERR:", r.stderr[-1500:])
