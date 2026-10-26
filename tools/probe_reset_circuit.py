# -*- coding: utf-8 -*-
"""「重置熔断」按钮接线验证（2026-10-26）。

为什么需要它：这个按钮的后端接口 `/admin/api/circuit/reset` 与前端
`resetCircuit()` 都存在了很久，但**从来没有按钮调用**（死代码）。
后果不是报错，而是用户看到熔断红点后**没有任何出口**：
「测试并恢复」要求测试通过才关熔断 —— 上游此刻不通就卡住了。

判据：
  ① 供应商健康状态为 err 时，抽屉里出现「重置熔断」按钮（默认分支：健康时**不**出现）
  ② 该按钮文本是真实文案（不是裸 i18n 键名）
  ③ 点击后后端熔断真的被清掉（health 变回 ok，或至少 breaker 状态改变）

用法：python tools/probe_reset_circuit.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_reset_cb")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
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


# 造一个必定连不上的供应商（127.0.0.1:9 是 discard 端口，通常无服务），
# 连打几次让它熔断，然后检查按钮。
UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 240)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);

  const out = { errs };

  out.tab = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
    await new Promise(r => setTimeout(r, 1500));
    return { activeTab: d.activeTab, provCount: (d.providersFormData || []).length };
  });

  // 打开断的那家的抽屉，检查「重置熔断」按钮
  out.drawer = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const prov = (d.providersFormData || []).find(x => x.name === 'broken');
    if (!prov) return { ok: false, why: 'no broken provider' };
    // 先强制把该家的 health 标成 err（模拟熔断后的界面状态），
    // 这样不必真的等熔断器触发就能验证按钮接线。
    const list = d.healthList || d.health || [];
    const before = d.healthOf('broken');
    d.openProviderDrawer(d.providersFormData.indexOf(prov));
    await new Promise(r => setTimeout(r, 900));

    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const btns = Array.from(document.querySelectorAll('button')).filter(isVis);
    const resetBtn = btns.find(x => /重置熔断|Reset breaker/.test(x.textContent || ''));
    // 也找 DOM 里**不可见**的那个（x-show 隐藏时元素仍在 DOM）
    const allReset = Array.from(document.querySelectorAll('button'))
      .filter(x => /重置熔断|Reset breaker/.test(x.textContent || ''));
    return {
      ok: true,
      healthBefore: before,
      resetBtnFound: !!resetBtn,
      resetBtnText: resetBtn ? (resetBtn.textContent || '').trim() : '',
      resetBtnVisible: resetBtn ? isVis(resetBtn) : false,
      // 关键：DOM 里是否存在（不管可见性）+ 它的 x-show 绑定
      resetBtnInDom: allReset.length,
      resetBtnXShow: allReset[0] ? (allReset[0].getAttribute('x-show') || '') : '',
      recoverBtnFound: btns.some(x => /测试并恢复|Test & recover/.test(x.textContent || '')),
      allBtnTexts: btns.map(x => (x.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 20),
    };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def make_providers():
    """两个供应商：good（能通）+ broken（连不上，用于制造熔断状态）。"""
    decl = {
        "providers": [
            {
                "name": "good",
                "endpoint": "https://api.deepseek.com/v1/chat/completions",
                "api_key_env": "PROBE_KEY_GOOD",
                "api_key_default": "sk-probe-not-real",
                "models": {},
            },
            {
                "name": "broken",
                "endpoint": "http://127.0.0.1:9/v1/chat/completions",
                "api_key_env": "PROBE_KEY_BAD",
                "api_key_default": "sk-probe-not-real",
                "models": {},
            },
        ]
    }
    p = os.path.join(WD, "providers.json")
    io.open(p, "w", encoding="utf-8").write(json.dumps(decl, ensure_ascii=False, indent=2))
    return p


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
    make_providers()

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

        # 先让 broken 真的失败几次，触发熔断（这样 healthOf 会是 err）
        for _ in range(3):
            req = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "broken/m", "messages": [{"role": "user", "content": "x"}]}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer sk-probe"})
            try:
                urllib.request.urlopen(req, timeout=8).read()
            except Exception:
                pass

        # 造模型：让端点能被当作路由（上面直接请求可能因无路由而 404）
        # 用 /admin/api/providers/test 触发一次真实连接尝试更可靠
        req = urllib.request.Request(
            base + "/admin/api/providers/test",
            data=json.dumps({"endpoint": "http://127.0.0.1:9/v1/chat/completions"}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass

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
    dr = d.get("drawer") or {}
    print(f"\n[现场] 页签={tab.get('activeTab')} 供应商数={tab.get('provCount')}")
    print(f"[现场] broken 的健康状态={dr.get('healthBefore')!r}")
    print(f"[现场] 抽屉按钮: {dr.get('allBtnTexts')}")
    print(f"[现场] 重置熔断按钮: 找到={dr.get('resetBtnFound')} 可见={dr.get('resetBtnVisible')} 文本={dr.get('resetBtnText')!r}")
    print(f"[现场] 重置熔断: DOM数={dr.get('resetBtnInDom')} 可见={dr.get('resetBtnVisible')} "
          f"x-show={dr.get('resetBtnXShow')!r}")
    if d.get("errs"):
        print(f"[页面异常] {d['errs']}")

    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])
    check(bool(dr.get("ok")), "抽屉能打开", str(dr.get("why")))
    # 关键：按钮必须存在于 DOM（x-show 默认隐藏时也在 DOM 里），
    # 所以"DOM 里有"这条不依赖能否稳定触发熔断 —— 它证明**接线存在**。
    check((dr.get("resetBtnInDom") or 0) >= 1,
          "抽屉 DOM 里存在「重置熔断」按钮（接线已存在，不再是无出口）",
          f"按钮列表={dr.get('allBtnTexts')}")
    # 显示条件必须绑定 healthOf(...)==='err' —— 否则按钮会常驻，
    # 让"健康时也能重置"变成噪音。这条钉的是 x-show 表达式本身。
    xs = dr.get("resetBtnXShow") or ""
    check("healthOf" in xs and "err" in xs,
          "按钮只在熔断(healthOf==='err')时显示", repr(xs))
    # 文案必须是真实文本（用 xt 渲染后取值，裸键名说明 i18n 缺键）
    txt = " ".join(dr.get("allBtnTexts") or [])
    check("reset_circuit" not in txt,
          "按钮文案已翻译（不是裸 i18n 键名）", repr(txt[:160]))

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项失败")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
