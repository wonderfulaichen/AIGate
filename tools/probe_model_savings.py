# -*- coding: utf-8 -*-
"""实测「模型明细」表真的把**省了多少**显示出来了 —— 用户报障的回归探针。

起因 (2026-10-16 用户)：「你这个明细怎么不把省了多少给显示了？这个我都不知道省了多少。」
供应商表早就有「优化省 Tokens」列, 模型表没有, 且 `ModelStats` 里也确实缺字段 —— 前端无从显示。
补字段/补列之后最容易犯的错有三个, 本探针逐条盯住:
  ① 列压根没渲染 (表头少了/i18n 缺键 → 整格空白, **源码看着是对的**);
  ② 省量是"共用的一个数"(把全局省量平摊给每个模型) → 两行数字完全相同;
  ③ 免费/月套餐模型被折算出一个非零金额 (凭空造钱), 或反过来把"确实省了但折 0"显示成 "—"。

判据 (任一失败即红):
  ① 无 pageerror / window.__ERRS 为空 (i18n 缺键会让表头整格变空而不报错, 故还要看表头文字)
  ② 表头同时含「优化省 Tokens」与「优化省金额」
  ③ 每行单元格数 == 表头列数 (加列时漏改一处就会错位)
  ④ 至少两行的省量**互不相同** (证明是逐模型聚合, 不是共用值)
  ⑤ 存在一行金额显示 "—" (不计费模型: 没有费用基数)
  ⑥ 存在一行金额是真实金额 (非 "—"、非 ¥0.00)
  ⑦ 省量列用 K/M 缩写 (与同表其他 token 列同一种读法), 不是裸长数字

用法: python tools/build_preview.py && python tools/probe_model_savings.py
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
  async function open(q) {
    const p = await b.newPage({ viewport: { width: 1900, height: 1100 } });
    const errs = [];
    p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
    // 预览页的 /admin/static/* 是绝对路径 (线上由 Rust 提供), file:// 下必须喂进去,
    // 否则 Alpine 不加载、整页是死的 (实测坑, 见 probe_stage_waterfall.py)。
    await p.route('**/admin/static/*', route => {
      const name = route.request().url().split('/').pop();
      if (STATIC[name]) return route.fulfill({ status: 200, contentType: 'application/javascript', body: STATIC[name] });
      return route.fulfill({ status: 404, body: '' });
    });
    await p.goto('file:///' + %(page)s + q, { waitUntil: 'load' });
    await p.waitForTimeout(2500);
    const jsErrs = await p.evaluate(() => (window.__ERRS || []).slice(0, 10));
    return { p, errs, jsErrs };
  }

  const out = {};
  // ?sc=done: 有数据; ?sc=empty: 无数据 (用来核对不画假 0)
  const done = await open('?sc=done');
  out.done = await done.p.evaluate(() => {
    const panels = Array.from(document.querySelectorAll('.pnl'));
    const pnl = panels.find(x => /模型明细/.test(x.innerText || ''));
    if (!pnl) return { panel: false };
    const table = pnl.querySelector('table');
    if (!table) return { panel: true, table: false };
    const heads = Array.from(table.querySelectorAll('thead th')).map(t => (t.innerText || '').trim());
    const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr => ({
      cells: Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim()),
    }));
    return {
      panel: true, table: true, heads,
      rows: rows.slice(0, 6),
      // 末两列 = 省量 / 已省金额
      savedTokens: rows.map(r => r.cells[r.cells.length - 2]),
      savedFees: rows.map(r => r.cells[r.cells.length - 1]),
      bodyText: (table.innerText || '').replace(/\n+/g, ' | ').slice(0, 600),
    };
  });
  out.done.pageerrors = done.errs;
  out.done.errs = done.jsErrs;

  const empty = await open('?sc=empty');
  out.empty = await empty.p.evaluate(() => {
    const panels = Array.from(document.querySelectorAll('.pnl'));
    const pnl = panels.find(x => /模型明细/.test(x.innerText || ''));
    const table = pnl ? pnl.querySelector('table') : null;
    return {
      rows: table ? Array.from(table.querySelectorAll('tbody tr')).map(tr => (tr.innerText || '').trim()) : [],
      text: pnl ? (pnl.innerText || '').replace(/\n+/g, ' | ').slice(0, 300) : '',
    };
  });
  out.empty.pageerrors = empty.errs;
  out.empty.errs = empty.jsErrs;

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
""" % {"page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_analytics.html").replace("\\", "/")),
       "static": json.dumps({
           n: open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
           for n in ("alpine.min.js", "tailwind.js")})}

tmp = os.path.join(D, "_probe_model_savings.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ)
env["NODE_PATH"] = os.path.join(D, "node_modules")
r = subprocess.run(["node", tmp], cwd=D, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
if r.returncode != 0:
    print("STDERR:", r.stderr[-2000:])
    sys.exit(1)
try:
    data = json.loads(r.stdout)
except Exception:
    print(r.stdout[:2000])
    print("!! 探针输出不是 JSON, 无法判定")
    sys.exit(1)

fails = []
def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

d = data.get("done") or {}
e = data.get("empty") or {}
heads = d.get("heads") or []
rows = d.get("rows") or []
toks = d.get("savedTokens") or []
fees = d.get("savedFees") or []

print("== 判据 ==")
ck(not d.get("pageerrors"), "无 pageerror: %s" % d.get("pageerrors"))
ck(not d.get("errs"), "window.__ERRS 为空: %s" % d.get("errs"))
ck(d.get("table"), "模型明细面板与表格都在")
ck(any("优化省" in h for h in heads), "表头含省量列: %s" % heads[-3:])
ck(any("金额" in h for h in heads), "表头含省金额列: %s" % heads[-2:])
widths = {len(r.get("cells") or []) for r in rows}
ck(len(widths) == 1 and widths.pop() == len(heads),
   "每行单元格数 == 表头列数 (%d): %s" % (len(heads), widths))
ck(len(set(toks)) >= 2, "各行省量互不相同 (逐模型聚合, 非共用值): %s" % toks[:6])
ck(any(x == "—" for x in fees), "存在「—」金额 (不计费模型没有费用基数): %s" % fees[:6])
ck(any(x not in ("—", "", "¥0.00") for x in fees), "存在真实金额: %s" % fees[:6])
ck(all(("K" in x or "M" in x or x in ("0", "—", "")) for x in toks),
   "省量列用 K/M 缩写: %s" % toks[:6])
# 负例: 无数据时不画假 0
ck(not (e.get("errs") or []), "空场景无表达式异常: %s" % e.get("errs"))
ck(all("优化省" not in (row or "") for row in (e.get("rows") or [])),
   "空场景未出现省量数据行: %s" % (e.get("rows") or [])[:2])

print("\n== 表头 ==")
print("   " + " | ".join(heads))
print("\n== 前 3 行末两列 (省量 / 已省金额) ==")
for r in rows[:3]:
    print("   %-22s %-10s %s" % ((r.get("cells") or [""])[0], (r.get("cells") or ["", ""])[-2],
                                 (r.get("cells") or ["", ""])[-1]))
print("\n== 表体文本 ==")
print("   " + str(d.get("bodyText"))[:400])

print("\n%s (13 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", len(fails)))
sys.exit(1 if fails else 0)
