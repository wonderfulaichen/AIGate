# -*- coding: utf-8 -*-
"""概览页额度告警：验证「额度用满/快用满」在概览页「建议」面板里出现。

为什么必须实测：这条链有三段，任一段断了用户都看不到提醒，且**不报错**：
  ① 概览页切进来时真的发起了后台查询（不 await，不阻塞首屏）；
  ② 后端 quota-all 返回全部供应商 × 全部工作区；
  ③ opAdvice() 真的把它转成了建议条目（且「没有请求」时也要出 ——
     那是用户最该知道的时刻）。

判据：
  ① 后端 quota-all 返回的工作区数与上游一致
  ② 概览页的 opAdvice() 里出现 k='quota' 的条目
  ③ 该条目文本包含"已用满"字样与真实工作区名（Default 100%）
  ④ 90% 以下的工作区**不**出现在告警里（aichen 64% 不该被报）
  ⑤ 页面把建议渲染成了可见 DOM（不是只有数据没有 UI）

用法：python tools/probe_oc_quota_advice.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_quota_advice")

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
  const p = await b.newPage({ viewport: { width: 1600, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 240)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(6000);

  const out = { errs };

  // 确认在概览页，并等后台额度查询回来
  out.tab = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    return { tab: d.activeTab, hasFn: typeof d.fetchOpencodeQuotaAll === 'function',
             hasAlerts: typeof d.ocQuotaAlerts === 'function' };
  });

  // 显式等额度快照到位（最多 40 秒：上游 1-2 秒/家）
  out.waited = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const t0 = Date.now();
    while (Date.now() - t0 < 40000) {
      if (d._ocQuotaAll && Object.keys(d._ocQuotaAll).length) break;
      await new Promise(r => setTimeout(r, 600));
    }
    return {
      ms: Date.now() - t0,
      providers: Object.keys(d._ocQuotaAll || {}),
      loading: !!d._ocQuotaAllLoading,
    };
  });

  // 数据层：建议条目
  out.advice = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const al = d.ocQuotaAlerts();
    const adv = d.opAdvice();
    return {
      alerts: al,
      // k 现在是 quota_full / quota_near（两个变体各一条）
      quotaAdvice: adv.filter(x => String(x.k || '').startsWith('quota')),
      allAdvice: adv.map(x => ({ k: x.k, tone: x.tone, text: x.text })),
      // 诊断：这四项是否为空（为空就说明 opAdvice 提前 return 了）
      diag: {
        totalRequests: (d.stats || {}).total_requests,
        statsNull: !d.stats,
        adviceLen: adv.length,
      },
    };
  });

  // DOM 层：建议面板真的画出来了
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
    const allCards = Array.from(document.querySelectorAll('.adv-card'));
    const grid = document.querySelector('.adv-grid');
    return {
      // 诊断：卡片是否存在（不管可见性）与为何不可见
      allCardCount: allCards.length,
      allCardTexts: allCards.map(c => (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)),
      gridExists: !!grid,
      gridVisible: grid ? isVis(grid) : false,
      gridChildCount: grid ? grid.children.length : -1,
      // 向上找第一个隐藏的祖先，定位是谁把它藏了
      hiddenBy: grid ? (() => {
        let n = grid;
        while (n && n !== document.body) {
          const cs = getComputedStyle(n);
          if (cs.display === 'none' || cs.visibility === 'hidden') {
            return (n.id || n.className || n.tagName) + ' :: ' + cs.display + '/' + cs.visibility;
          }
          n = n.parentElement;
        }
        return '';
      })() : '',
      cardTexts: allCards.filter(isVis).map(c => (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160)),
    };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def prep_providers(cookie):
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    if not os.path.exists(src):
        return None
    o = json.load(io.open(src, encoding="utf-8"))
    provs = o if isinstance(o, list) else o.get("providers") or []
    dst = os.path.join(WD, "providers.json")
    if isinstance(o, list):
        decl = {"providers": provs}
    else:
        decl = o
    # 只给 go 一个供应商，保持测试聚焦；其它家没有 Console 凭据会自动被跳过
    io.open(dst, "w", encoding="utf-8").write(
        json.dumps(decl, ensure_ascii=False, indent=2))
    return dst


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
        print(f"{SKIP}  没有真实 Console Cookie，无法测")
        return 0
    print("[准备] 真实 Cookie: 有")

    # 用部署配置（里面 go 已配好 Cookie + 两个工作区）
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    o = json.load(io.open(src, encoding="utf-8"))
    provs = o if isinstance(o, list) else o.get("providers") or []
    go = next((p for p in provs if p.get("name") == "go"), None)
    if not go:
        print(f"{BAD}  部署配置里没有 go")
        return 1
    ids = go.get("console_org_ids") or ([go["console_org_id"]] if go.get("console_org_id") else [])
    print(f"[准备] go 的工作区: {len(ids)} 个 -> {ids}")
    prep_providers(cookie)

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

        # ① 后端 quota-all
        with urllib.request.urlopen(base + "/admin/api/opencode/quota-all", timeout=120) as r:
            allq = json.loads(r.read().decode())
        back = (allq.get("providers") or {}).get("go") or {}
        bws = back.get("workspaces") or []
        print(f"\n[后端] quota-all: go -> {len(bws)} 个工作区")
        for w in bws:
            q = (w.get("quota") or {})
            mon = next((x for x in (q.get("windows") or []) if x.get("key") == "month"), {})
            print(f"   [{w.get('workspace')}] {w.get('name')!r} month={mon.get('used_pct')}%")
        check(len(bws) == len(ids),
              "后端返回的工作区数 == 配置里存的数量",
              f"backend={len(bws)} cfg={len(ids)}")

        # ② 前端
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

    tab = d.get("tab") or {}
    waited = d.get("waited") or {}
    advice = d.get("advice") or {}
    dom = d.get("dom") or {}
    alerts = advice.get("alerts") or []
    qadv = advice.get("quotaAdvice") or []

    print(f"\n[前端] 页签={tab.get('tab')} 方法就位={tab.get('hasFn')}/{tab.get('hasAlerts')}")
    print(f"[前端] 等额度快照 {waited.get('ms')}ms，供应商={waited.get('providers')}")
    print(f"[前端] 告警条目 {len(alerts)} 条:")
    for a in alerts:
        print(f"   {a.get('provider')}/{a.get('name') or a.get('workspace')} "
              f"{a.get('windowKey')} {a.get('pct')}%")
    print(f"[前端] 建议里的额度条目 {len(qadv)} 条:")
    for q in qadv:
        print(f"   [{q.get('tone')}] k={q.get('k')} {q.get('text')}")
    print(f"[前端] 诊断: stats.total_requests={advice.get('diag', {}).get('totalRequests')} "
          f"stats为空={advice.get('diag', {}).get('statsNull')} "
          f"建议总数={advice.get('diag', {}).get('adviceLen')}")
    print(f"[前端] 建议面板 DOM: 卡片数={dom.get('allCardCount')} "
          f"grid存在={dom.get('gridExists')} grid可见={dom.get('gridVisible')} "
          f"子元素={dom.get('gridChildCount')}")
    if dom.get("hiddenBy"):
        print(f"   被谁藏了: {dom['hiddenBy']}")
    for t0 in (dom.get("allCardTexts") or []):
        print(f"   ·(全部) {t0}")
    for t0 in (dom.get("cardTexts") or []):
        print(f"   ·(可见) {t0}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])
    check(bool(waited.get("providers")), "概览页后台真的发了额度查询",
          f"providers={waited.get('providers')}")
    check(len(alerts) >= 1, "检出至少一个高危工作区（>=90%）", f"alerts={alerts}")
    check(any((a.get("pct") or 0) >= 100 for a in alerts),
          "已用满的那个区被检出（>=100%）", f"alerts={alerts}")
    # 64% 的 aichen 不该被告警
    check(all((a.get("pct") or 0) >= 90 for a in alerts),
          "90% 以下的工作区**不**在告警里（aichen 64% 不该被报）",
          f"alerts={alerts}")
    check(len(qadv) >= 1, "opAdvice() 里出现额度条目（k 以 quota 开头）", str(qadv))
    full_txt = " ".join(q.get("text", "") for q in qadv)
    check("已用满" in full_txt, "额度条目文案含「已用满」", full_txt[:160])
    check(any((a.get("name") or "") in full_txt for a in alerts if a.get("name")),
          "额度条目文案含真实工作区名（如 Default）", full_txt[:160])
    check(bool(dom.get("gridExists")), "建议面板的 grid 存在", str(dom.get("hiddenBy")))
    check(bool(dom.get("gridVisible")), "建议面板可见（不是被 x-show 藏住）",
          str(dom.get("hiddenBy")))
    check((dom.get("allCardCount") or 0) >= 1, "建议卡片真的渲染出来了",
          str(dom.get("allCardTexts"))[:200])
    check(any("额度" in (t or "") for t in (dom.get("allCardTexts") or [])),
          "DOM 里能看到额度告警文本", str(dom.get("allCardTexts"))[:200])

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项失败:")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
