# -*- coding: utf-8 -*-
"""设置页前端回归探针 —— 钉住本轮修的三类 bug。

背景: 用户说"设置页前端有一些 bug 还没修"。排查后发现三类真问题 (细节见 CHANGELOG):
  ① **数字输入框清空 → 静默关掉功能**: `parseInt('')||0` = 0。实测「长会话历史裁剪」
     从 20 变 0、开关变灰、配置区消失、**无任何提示**。8 个输入框全有此问题。
  ② **两块"改完要点保存"的开关无未保存标记**: 高峰计价 / Tooltip 的开关点了立即变样,
     但没落库 —— 用户以为已生效, 刷新才发现变回去。
  ③ **fetchCache/fetchRecall 整体替换**: 后端返回不完整 JSON 时字段变 undefined, 界面空白且不报错。

判据 (读真实 DOM + 状态, 不看源码):
  ① 清空数字输入 → 值**保持不变**（不再静默归零）, 且给出提示
  ② 输入合法值 → 正常保存
  ③ 输入越界值 → 夹到边界（min/max 与 input 属性同口径）
  ④ 高峰/Tooltip 开关点一下 → 出现「未保存」标记
  ⑤ 保存后「未保存」标记消失
  ⑥ 后端返回不完整 JSON 时 fetchCache 不把界面清空（合并语义）
  ⑦ 全程无 pageerror / __ERRS
"""
import io
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
  const p = await b.newPage({ viewport: { width: 1500, height: 1200 } });
  const errs = []; p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.route('**/admin/static/*', r => { const n = r.request().url().split('/').pop();
    if (STATIC[n]) return r.fulfill({ status: 200, contentType: 'application/javascript', body: STATIC[n] });
    return r.fulfill({ status: 404, body: '' }); });
  await p.goto('file:///' + %(page)s, { waitUntil: 'load' });
  await p.waitForTimeout(2200);
  await p.evaluate(() => { document.querySelector('[x-data]')._x_dataStack[0].activeTab = 'settings'; });
  await p.waitForTimeout(700);

  const out = { errs };
  const st = () => p.evaluate(() => {
    const s = document.querySelector('[x-data]')._x_dataStack[0];
    return { turns: s.maxHistoryTurns, peakDirty: s.peakDirty, tooltipDirty: s.tooltipDirty,
             cacheEntries: s.cache && s.cache.entries };
  });
  // 找「长会话历史裁剪」的输入框并设值+触发 change
  const setTurns = (v) => p.evaluate(val => {
    const row = Array.from(document.querySelectorAll('.set-row')).find(r => /长会话历史裁剪/.test(r.innerText || ''));
    const inp = row.nextElementSibling.querySelector('input[type=number]');
    inp.value = val;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    return inp.value;
  }, v);

  out.before = await st();

  // ① 清空 → 不得归零
  await setTurns('');
  await p.waitForTimeout(500);
  out.afterClear = await st();
  out.clearToast = await p.evaluate(() => {
    const t = Array.from(document.querySelectorAll('*')).filter(e => /toast|Toast/.test('' + e.className));
    return t.map(e => (e.innerText || '').slice(0, 90)).filter(Boolean).slice(0, 3);
  });
  out.inputValueAfterClear = await p.evaluate(() => {
    const row = Array.from(document.querySelectorAll('.set-row')).find(r => /长会话历史裁剪/.test(r.innerText || ''));
    const wrap = row.nextElementSibling;
    const inp = wrap && wrap.querySelector('input[type=number]');
    return inp ? inp.value : null;
  });

  // ② 合法值
  await setTurns('30');
  await p.waitForTimeout(500);
  out.afterValid = await st();

  // ③ 越界 (超过 max=200)
  await setTurns('9999');
  await p.waitForTimeout(500);
  out.afterOutOfRange = await st();

  // ④ 高峰开关 → 未保存标记
  await p.evaluate(() => {
    const row = Array.from(document.querySelectorAll('.set-row')).find(r => /高峰\/空闲/.test(r.innerText || ''));
    if (row) row.querySelector('button.tgl').click();
  });
  await p.waitForTimeout(350);
  out.peakAfterClick = await st();

  // ⑤ 保存后标记消失
  await p.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button')).find(x => (x.innerText || '').trim() === '保存');
    if (btn) btn.click();
  });
  await p.waitForTimeout(700);
  out.peakAfterSave = await st();

  // ⑥ fetchCache 遇到不完整 JSON 时不清空界面
  out.partialCache = await p.evaluate(async () => {
    const s = document.querySelector('[x-data]')._x_dataStack[0];
    const before = s.cache && s.cache.entries;
    const orig = window.fetch;
    window.fetch = function (u, o) {
      if (String(u).indexOf('/admin/api/cache') >= 0) {
        return Promise.resolve(new Response(JSON.stringify({ enabled: true }), { status: 200,
          headers: { 'Content-Type': 'application/json' } }));
      }
      return orig.apply(window, arguments);
    };
    await s.fetchCache();
    window.fetch = orig;
    return { before, after: s.cache && s.cache.entries };
  });

  out.errsAfter = await p.evaluate(() => (window.__ERRS || []).slice(0, 5));
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
""" % {"page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_settings.html").replace("\\", "/")),
       "static": json.dumps({n: io.open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
                             for n in ("alpine.min.js", "tailwind.js")})}

tmp = os.path.join(D, "_probe_settings_page.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env = dict(os.environ, NODE_PATH=os.path.join(D, "node_modules"))
r = subprocess.run(["node", tmp], cwd=D, capture_output=True, text=True, timeout=180,
                   encoding="utf-8", errors="replace", env=env)
if r.returncode != 0:
    print("STDERR:", r.stderr[-1000:])
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

b0 = data.get("before") or {}
ac = data.get("afterClear") or {}
av = data.get("afterValid") or {}
ao = data.get("afterOutOfRange") or {}
pc = data.get("peakAfterClick") or {}
ps = data.get("peakAfterSave") or {}
pm = data.get("partialCache") or {}

print("\n== 判据 ==")
ck(not data.get("errs") and not data.get("errsAfter"), "无 pageerror / __ERRS")
# ① 清空不归零
ck(ac.get("turns") == b0.get("turns"),
   "① 清空输入后 turns 不变 (%s → %s) —— 不再静默关掉功能" % (b0.get("turns"), ac.get("turns")))
ck(bool(data.get("clearToast")), "① 清空时给出提示: %s" % data.get("clearToast"))
ck(str(data.get("inputValueAfterClear")) == str(b0.get("turns")),
   "① 输入框被回填成旧值 (%s)" % data.get("inputValueAfterClear"))
# ② 合法值生效
ck(av.get("turns") == 30, "② 输入 30 → turns=30, 实得 %s" % av.get("turns"))
# ③ 越界夹取
ck(ao.get("turns") == 200, "③ 输入 9999 → 夹到 max=200, 实得 %s" % ao.get("turns"))
# ④ 未保存标记
ck(pc.get("peakDirty") is True, "④ 高峰开关点一下 → 出现未保存标记")
# ⑤ 保存后清除
ck(ps.get("peakDirty") is False, "⑤ 保存后未保存标记消失")
# ⑥ 不完整 JSON 不清空
ck(pm.get("after") is not None and pm.get("after") == pm.get("before"),
   "⑥ 后端返回不完整 JSON 时界面不被清空 (%s → %s)" % (pm.get("before"), pm.get("after")))

print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
sys.exit(1 if fails else 0)
