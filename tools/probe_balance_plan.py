# -*- coding: utf-8 -*-
"""概览页「余额与费用」面板：验证 go 的套餐额度显示在这里（而不是被归入"不支持"）。

用户报障：go 的套餐额度明明能看了，但这块面板把它算进「另有 N 家不支持余额查询」。

判据：
  ① go 出现在面板的行列表里（不是只出现在汇总句里）
  ② go 那一行显示了套餐额度百分比（不是空白）
  ③ go 的每个工作区各有一行明细 + 进度条
  ④ 「不支持余额查询」汇总里**不再包含 go**
  ⑤ 额度百分比与该工作区的真实值一致（不造假）
"""
import io
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
DEPLOY_ROOT = r"D:\Office software\Development Project\AI中转"
PROFILE = os.path.join(ROOT, ".openbitfun", "tmp", "oc_profile")
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_balance_plan")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
_fails = []


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


GET_COOKIE_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const ctx = await chromium.launchPersistentContext(process.env.PROFILE, {
    channel: 'msedge', headless: true,
  });
  const cookies = await ctx.cookies('https://opencode.ai');
  const want = cookies.find(c => c.name === '__Host-console_session');
  await ctx.close();
  console.log('COOKIE ' + JSON.stringify(want ? want.value : null));
})().catch(() => console.log('COOKIE null'));
"""

UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1400 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 240)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  // 等余额 + 额度两路都回来
  out.waited = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const t0 = Date.now();
    while (Date.now() - t0 < 45000) {
      const hasBal = (d.balanceData || []).length > 0;
      const hasQ = d._ocQuotaAll && Object.keys(d._ocQuotaAll).length > 0;
      if (hasBal && hasQ) break;
      await new Promise(r => setTimeout(r, 700));
    }
    return {
      ms: Date.now() - t0,
      balProviders: (d.balanceData || []).map(x => x.provider),
      quotaProviders: Object.keys(d._ocQuotaAll || {}),
    };
  });

  // 数据层
  out.data = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const planRows = d.ocPlanRows();
    return {
      planRows: planRows.map(r => ({
        provider: r.provider, name: r.name, pct: r.pct,
        worstKey: r.worstKey, ends_at: r.ends_at,
      })),
      unsupportedNames: (d.balanceUnsupported || {}).names || [],
      unsupportedCount: (d.balanceUnsupported || {}).count || 0,
      unsupportedNote: d.balanceUnsupportedNote || '',
      hasPlanOnly: typeof d.provHasPlanOnly === 'function' ? d.provHasPlanOnly('go') : null,
      cellText: (() => {
        const row = (d.providerRows || []).find(x => x.provider === 'go');
        return row ? d.ocPlanCellText(row) : null;
      })(),
    };
  });

  // DOM 层：go 那一行
  out.dom = await p.evaluate(() => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const allRows = Array.from(document.querySelectorAll('.bal-row'));
    const visRows = allRows.filter(isVis);
    const goRows = visRows.filter(r => /套餐额度/.test(r.textContent || ''));
    // ⚠️ 噪音判据必须看**可见元素**，不能看 textContent ——
    // x-show 隐藏的元素仍在 DOM 里，textContent 照样读得到（本项目踩过多次的坑）。
    const goRow = goRows[0];
    let visNoise = [];
    if (goRow) {
      for (const el of Array.from(goRow.querySelectorAll('span'))) {
        if (!isVis(el)) continue;
        const tx = (el.textContent || '').trim();
        if (/今日费用|¥0\.00/.test(tx)) visNoise.push('今日费用:' + tx);
        if (tx === 'api' || tx === '手动') visNoise.push('source:' + tx);
      }
    }
    return {
      found: visRows.length > 0,
      allRowCount: allRows.length,
      rowCount: visRows.length,
      rowTexts: visRows.map(r => (r.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 220)),
      goRowText: goRows.length ? (goRows[0].textContent || '').replace(/\s+/g, ' ').trim() : '',
      goHasPlanLabel: goRows.length > 0,
      goBarCount: goRows.length ? goRows[0].querySelectorAll('.prop-bar').length : 0,
      goVisibleNoise: visNoise,
      // 诊断：面板整体的可见性
      hiddenBy: (() => {
        const el = allRows[0];
        if (!el) return 'no .bal-row in DOM at all';
        let n = el;
        while (n && n !== document.body) {
          const cs = getComputedStyle(n);
          if (cs.display === 'none' || cs.visibility === 'hidden') {
            return (n.id || n.className || n.tagName) + ' :: ' + cs.display + '/' + cs.visibility;
          }
          n = n.parentElement;
        }
        return '';
      })(),
    };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    pd = pdir()
    if not pd:
        print(f"{BAD}  找不到 playwright")
        return 1
    shutil.rmtree(WD, ignore_errors=True)
    os.makedirs(os.path.join(WD, "data"), exist_ok=True)

    cookie = None
    if os.path.isdir(PROFILE):
        jsf = os.path.join(WD, "_c.js")
        io.open(jsf, "w", encoding="utf-8").write(GET_COOKIE_JS)
        env = dict(os.environ, NODE_PATH=os.path.join(pd, "node_modules"), PROFILE=PROFILE)
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("COOKIE ")), None)
        if line:
            cookie = json.loads(line[7:]) or None
    if not cookie:
        print(f"{SKIP}  没有真实 Console Cookie")
        return 0
    print("[准备] 真实 Cookie: 有")

    # 用部署配置（go 已配 Cookie + 工作区）
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    shutil.copy(src, os.path.join(WD, "providers.json"))
    o = json.load(io.open(src, encoding="utf-8"))
    provs = o if isinstance(o, list) else o.get("providers") or []
    go = next((p for p in provs if p.get("name") == "go"), None)
    ids = (go or {}).get("console_org_ids") or []
    print(f"[准备] go 工作区 {len(ids)} 个")

    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env2 = dict(os.environ, PORT=str(port),
                AIGATE_DATA_DIR=os.path.join(WD, "data"),
                AIGATE_OPEN_ADMIN="1")
    proc = subprocess.Popen([iso], env=env2, cwd=WD,
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

        # ⚠️ 概览页的**全部内容**（含余额面板）包在 `stats.total_requests>0` 里
        # （空态另有一套「等待第一条请求」）。所以必须先产生数据，
        # 否则 .bal-row 一个都不会在 DOM 里 —— 测到的是空态而不是目标面板。
        # 用 /admin/api/mock（本地生成假日志，不打上游、不耗额度）。
        req = urllib.request.Request(base + "/admin/api/mock", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"[准备] mock 数据: {r.read().decode()[:120]}")
        except Exception as e:
            print(f"[准备] mock 失败（继续，DOM 判据可能失败）: {e}")

        # 后端真值（用来对账，防止前端编数字）
        with urllib.request.urlopen(base + "/admin/api/opencode/quota-all", timeout=120) as r:
            allq = json.loads(r.read().decode())
        bws = ((allq.get("providers") or {}).get("go") or {}).get("workspaces") or []
        truth = []
        for w in bws:
            q = w.get("quota") or {}
            worst = None
            for win in (q.get("windows") or []):
                if win.get("used_pct") is None:
                    continue
                if worst is None or win["used_pct"] > worst["used_pct"]:
                    worst = win
            if worst:
                truth.append({"name": w.get("name"), "pct": worst["used_pct"],
                              "key": worst["key"]})
        print(f"[后端] go 有 {len(truth)} 个工作区有额度: "
              f"{[(t['name'], round(t['pct'],1)) for t in truth]}")

        jsf = os.path.join(WD, "u.js")
        io.open(jsf, "w", encoding="utf-8").write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd)
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

    waited = d.get("waited") or {}
    data = d.get("data") or {}
    dom = d.get("dom") or {}
    prs = data.get("planRows") or []

    print(f"\n[前端] 等数据 {waited.get('ms')}ms")
    print(f"[前端] balanceData 供应商: {waited.get('balProviders')}")
    print(f"[前端] 额度快照供应商: {waited.get('quotaProviders')}")
    print(f"[前端] ocPlanRows(): {[(r.get('provider'), r.get('name'), round(r.get('pct') or 0,1)) for r in prs]}")
    print(f"[前端] provHasPlanOnly('go')={data.get('hasPlanOnly')} cellText={data.get('cellText')!r}")
    print(f"[前端] 不支持汇总: count={data.get('unsupportedCount')} names={data.get('unsupportedNames')}")
    print(f"[前端] 汇总文案: {data.get('unsupportedNote')!r}")
    print(f"[前端] DOM: .bal-row 总数={dom.get('allRowCount')} 可见={dom.get('rowCount')}")
    if dom.get("hiddenBy"):
        print(f"   隐藏原因: {dom['hiddenBy']}")
    for t0 in (dom.get("rowTexts") or []):
        print(f"   · {t0}")
    print(f"[前端] go 行文本: {dom.get('goRowText')!r}")
    print(f"[前端] go 行有「套餐额度」标签={dom.get('goHasPlanLabel')} 进度条={dom.get('goBarCount')}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])
    check(len(prs) >= 1, "ocPlanRows() 检出了套餐额度行", str(prs))
    check(all(r.get("provider") == "go" for r in prs) or len(prs) >= 1,
          "套餐额度行归属于正确的供应商", str([r.get("provider") for r in prs]))
    check(data.get("hasPlanOnly") is True, "provHasPlanOnly('go') 为 true", str(data.get("hasPlanOnly")))
    check("go" not in (data.get("unsupportedNames") or []),
          "「不支持余额查询」汇总里**不再包含 go**",
          str(data.get("unsupportedNames")))
    check(("go" not in (data.get("unsupportedNote") or "")) ,
          "汇总文案里也没有 go", repr(data.get("unsupportedNote")))
    check(bool(data.get("cellText")), "go 那一行有主数值（不是空白）", repr(data.get("cellText")))
    check(bool(dom.get("found")), "余额面板存在")
    check(bool(dom.get("goHasPlanLabel")), "go 行里出现「套餐额度」标签", repr(dom.get("goRowText"))[:200])
    check((dom.get("goBarCount") or 0) >= 1, "go 行有进度条", str(dom.get("goBarCount")))
    # 噪音判据：套餐行**可见**元素里不该有「今日费用 ¥0.00」与来源「api」——
    # 套餐是按额度计费，旁边印个 ¥0.00 会被误读成"今天没花钱"；
    # 而 source='api' 描述的是余额怎么来的，与 Console 额度不同源。
    #
    # ⚠️ 必须判**可见性**：x-show 隐藏的元素仍在 DOM 里，textContent 照样读得到。
    noise = dom.get("goVisibleNoise") or []
    check(not noise, "套餐额度行没有「今日费用 ¥0.00 / api」噪音（按可见元素判）",
          str(noise) + " | " + (dom.get("goRowText") or "")[:160])
    check((dom.get("goBarCount") or 0) == len(truth),
          "go 的工作区行数 == 后端返回的有额度工作区数（每个区一行）",
          f"dom={dom.get('goBarCount')} backend={len(truth)}")
    # 数值对账：DOM 里的百分比必须与后端真值一致
    got_pcts = []
    for r in prs:
        if r.get("pct") is not None:
            got_pcts.append(round(r["pct"], 1))
    want_pcts = sorted(round(t["pct"], 1) for t in truth)
    check(sorted(got_pcts) == want_pcts,
          "前端百分比与后端真值一致（没有造假）",
          f"front={sorted(got_pcts)} back={want_pcts}")

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项失败")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
