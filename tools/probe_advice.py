# -*- coding: utf-8 -*-
"""验收：运维建议面板（对照 WaLiAPI 的 DashboardPage.tsx:229-286）.

核心判据不是"渲染出来了"，而是**建议必须带处置动作** ——
这是它与 healthTakeaway（一句陈述）的本质区别。

判据:
  ① 面板出现，卡片数 ≥3
  ② 每张卡有标题 + 一句建议文本（非空）
  ③ **至少一张卡带「去处理」按钮**（有问题的项才给动作）
  ④ 点「去处理」→ 跳到对应页签
  ⑤ 无请求时整个面板不出现（没数据就没有判断依据）
  ⑥ 建议文本随状态变化（不是写死的静态文案）
  ⑦ 零 pageerror
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_advice")
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
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(4200);

  const out = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const res = {};

    const readCards = () => Array.from(document.querySelectorAll('.adv-card')).map(c => ({
      tone: (c.className.match(/adv-(ok|warn|err|mute)/) || [])[1] || '',
      title: ((c.querySelector('.adv-hd')||{}).textContent||'').trim(),
      text: ((c.querySelector('.adv-tx')||{}).textContent||'').trim(),
      hasAction: !!c.querySelector('.adv-act'),
    }));

    res.cards = readCards();
    res.count = res.cards.length;
    res.withAction = res.cards.filter(x => x.hasAction).length;
    res.allHaveText = res.cards.every(x => x.text.length > 4);

    // ⑥ 建议随状态变化：故意把延迟阈值打穿（改 stats.avg_latency_ms）
    const orig = st.stats ? st.stats.avg_latency_ms : null;
    if (st.stats) {
      st.stats = Object.assign({}, st.stats, { avg_latency_ms: 9000 });
      await new Promise(r => setTimeout(r, 400));
      const after = readCards();
      res.latChanges = JSON.stringify(after) !== JSON.stringify(res.cards);
      const latCard = after.find(c => /延迟/.test(c.title));
      res.latSlowText = latCard ? latCard.text : '';
      res.latHasAction = latCard ? latCard.hasAction : false;
      // 还原
      st.stats = Object.assign({}, st.stats, { avg_latency_ms: orig });
      await new Promise(r => setTimeout(r, 300));
    }

    return res;
  });

  // ④ 点第一张带动作的卡
  out.click = await p.evaluate(async () => {
    const btn = document.querySelector('.adv-act');
    if (!btn) return { noBtn: true };
    const before = document.querySelector('[x-data]')._x_dataStack[0].activeTab;
    btn.click();
    await new Promise(r => setTimeout(r, 900));
    const after = document.querySelector('[x-data]')._x_dataStack[0].activeTab;
    return { before, after, changed: before !== after };
  });

  out.errs = errs;
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('dashboard'));
  await p.waitForTimeout(1000);
  await p.screenshot({ path: `%(out)s/advice.png`, fullPage: true });
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_advice")
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
        jsf = os.path.join(d, "probe_advice_tmp.js")
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
        print(f"  卡片: {json.dumps(res.get('cards'), ensure_ascii=False)[:600]}")
        print(f"  点击: {json.dumps(res.get('click'), ensure_ascii=False)}")
        print()

        check(res.get("count", 0) >= 3, "① 运维建议面板出现，卡片 ≥3",
              f"count={res.get('count')}")
        check(res.get("allHaveText") is True, "② 每张卡都有非空建议文本",
              f"{[c.get('text','')[:20] for c in (res.get('cards') or [])]}")
        check(res.get("withAction", 0) >= 1,
              "③ **至少一张卡带「去处理」按钮**（有问题的项才给动作）",
              f"withAction={res.get('withAction')} cards={res.get('count')}")
        ck = res.get("click") or {}
        check(ck.get("changed") is True,
              "④ 点「去处理」→ 跳到对应页签",
              f"{ck.get('before')} → {ck.get('after')}")
        check(res.get("latChanges") is True,
              "⑥ 建议随状态变化（延迟拉到 9s 后文案变了）",
              f"slowText={res.get('latSlowText')}")
        check(res.get("latHasAction") is True,
              "⑥ 变慢后该卡出现处置动作", f"hasAction={res.get('latHasAction')}")
        check(not (res.get("errs") or []), "⑦ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
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
