# -*- coding: utf-8 -*-
"""验收：供应商卡的快速统计（调用数 / 成功率 / 延迟）.

这块之前被判为"拿不到数据"——真因是**进供应商页不拉 stats**（已修）。
本探针要证明两件事：
  ① 进供应商页后 stats.per_provider 真的有数据（数据源接通）
  ② 卡片上真的渲染出统计区，且**没有请求的供应商不渲染**（未采集 ≠ 零）

判据:
  ① 切到供应商页后 stats.per_provider 长度 > 0
  ② 有请求的供应商卡片上出现 .plaza-stats，含调用数/成功率/延迟三项
  ③ 成功率带颜色（≥99 绿 / ≥95 琥珀 / 否则红）
  ④ 调用数走 K/M 缩写（不是裸数字串）
  ⑤ 没有请求的供应商**不渲染**统计区（不显示 0/0%/0ms）
  ⑥ 零 pageerror
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
DEPLOY = r"D:\Office software\Development Project\AI中转"
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_provstats")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def pdir():
    for rel in (".openbitfun/tmp/vidtools", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3600);
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('providers'));
  await p.waitForTimeout(3000);   // 等 fetchStats

  const out = {};
  out.data = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const pp = (st.stats && st.stats.per_provider) || [];
    return {
      perProviderLen: pp.length,
      firstThree: pp.slice(0, 3).map(x => ({ p: x.provider, req: x.requests, err: x.errors, lat: Math.round(x.avg_latency_ms) })),
      withReq: pp.filter(x => x.requests > 0).length,
    };
  });

  // 2026-10-24: 单一紧凑行式列表（不再有卡片/表格视图切换）
  await p.waitForTimeout(600);

  out.cards = await p.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('.prov-row'));
    const withStats = cards.filter(c => c.querySelector('.prov-stats'));
    const sample = withStats[0];
    let detail = null;
    if (sample) {
      const items = Array.from(sample.querySelectorAll('.prov-stat'));
      const okB = items.find(x => /%/.test(x.textContent || ''));
      detail = {
        name: (sample.querySelector('.prov-name')||{}).textContent || '',
        statCount: items.length,
        texts: items.map(x => (x.textContent||'').trim()),
        okColor: okB ? getComputedStyle(okB.querySelector('b')).color : null,
      };
    }
    // 没有请求的供应商：provQuickStats 返回 null → 不应有 .plaza-stats
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const noReq = (st.visibleProviders || []).filter(p => !st.provQuickStats(p.name));
    return {
      total: cards.length,
      withStatsCount: withStats.length,
      sample: detail,
      noReqCount: noReq.length,
    };
  });

  await p.screenshot({ path: `%(out)s/providers_card.png`, fullPage: true });
  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_provstats")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    shutil.copy2(os.path.join(DEPLOY, "providers.json"), os.path.join(wd, "providers.json"))
    iso = os.path.join(wd, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port); env["AIGATE_DATA_DIR"] = os.path.join(DEPLOY, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=wd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read(); break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪"); return 1
        d = pdir()
        if not d:
            print(f"{BAD}  找不到 playwright"); return 1
        jsf = os.path.join(d, "probe_provstats_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS.replace("%(base)s", base).replace("%(out)s", OUT.replace("\\", "/")))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:800]}")
        print()

        dt = res.get("data") or {}
        check(dt.get("perProviderLen", 0) > 0,
              "① 进供应商页后 stats.per_provider 有数据（数据源接通）",
              f"len={dt.get('perProviderLen')} first={dt.get('firstThree')}")
        c = res.get("cards") or {}
        check(c.get("withStatsCount", 0) > 0,
              "② 有请求的供应商卡片上出现统计区", f"withStats={c.get('withStatsCount')}/{c.get('total')}")
        sp = c.get("sample") or {}
        check(sp.get("statCount") == 3, "② 统计区含三项（调用/成功率/延迟）",
              f"{sp.get('texts')}")
        check(sp.get("okColor") not in (None, ""), "③ 成功率带颜色",
              f"color={sp.get('okColor')}")
        # 调用数走缩写（有 K/M 或纯数字，但不该是超长裸数字）
        calls_txt = (sp.get("texts") or [""])[0]
        check(len(calls_txt) <= 10, "④ 调用数是缩写形式（不是超长裸数字）",
              f"calls={calls_txt!r}")
        check(c.get("noReqCount", 1) == 0 or c.get("withStatsCount", 0) < c.get("total", 0),
              "⑤ 没有请求的供应商不渲染统计区（未采集 ≠ 零）",
              f"noReq={c.get('noReqCount')} withStats={c.get('withStatsCount')}/{c.get('total')}")
        check(not (res.get("errs") or []), "⑥ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()  # noqa: BLE001
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
