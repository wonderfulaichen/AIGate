# -*- coding: utf-8 -*-
"""B1 修复的浏览器实测：健康三态与「测试并恢复」按钮.

背景：`health_level()` 后端返回 `"ok"` / `"error"`（i18n.rs 文档写明），
但抽屉模板曾判 `==='err'`（该值不存在）→ 「测试并恢复」按钮**永远不出现**，
熔断后用户没法在抽屉里解除，只能回列表页找那个裸 ⟲ 图标。

本探针用**注入 health 数据**的方式验证前端三态与按钮可见性（不打真实上游）:
  ① healthState() 对 ok/error/缺失 分别返回 true/false/null
  ② 抽屉头部徽章按三态显示「正常/异常/未检测」（不会永远停在「未检测」）
  ③ 熔断（error）时「测试并恢复」按钮**可见** —— 这是本轮修的核心
  ④ 正常（ok）时该按钮不可见（不该给用户"恢复"错觉）
  ⑤ 零 pageerror

红线: 隔离实例 + 随机端口；绝不碰用户 8787。
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
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _pdir():
    for rel in (".openbitfun/tmp/vidtools", ".openbitfun/tmp/ppt", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);

  const out = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const res = {};

    const measure = async (label, level) => {
      // 注入 health: level 为 null 表示"该项不存在"
      st.health = level === null
        ? []
        : [{ provider: 'mockprov', status_level: level, status_text: '', latency_ms: 5, circuit: 'closed' }];
      await sleep(120);
      const stateVal = st.healthState('mockprov');
      const ofVal = st.healthOf('mockprov');
      // 打开抽屉并量按钮可见性
      const provs = st.providersFormData || [];
      const idx = provs.findIndex(x => x.name === 'mockprov');
      if (idx >= 0) st.openProviderDrawer(idx);
      await sleep(500);
      const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
      };
      // 「测试并恢复」按钮: 优先按 i18n 文案匹配（中/英都覆盖），
      // 再退回 title（test_recover_hint 里的「恢复」/「recover」字样）。
      let recoverVisible = false;
      for (const bt of Array.from(document.querySelectorAll('button'))) {
        const tx = (bt.textContent || '').trim();
        const tt = bt.getAttribute('title') || '';
        const isRecover = tx === '测试并恢复' || tx === 'Test & recover'
                       || tt.indexOf('恢复熔断') >= 0 || /close this provider/.test(tt);
        if (isRecover && vis(bt)) { recoverVisible = true; break; }
      }
      // 抽屉头部健康徽章文本
      const drawer = document.querySelector('.dr-card') || document.body;
      let badge = '';
      for (const el of Array.from(drawer.querySelectorAll('.bdg'))) {
        if (!vis(el)) continue;
        const tx = (el.textContent || '').trim();
        if (/正常|异常|未检测|Healthy|Error|Not checked/.test(tx)) { badge = tx; break; }
      }
      st.drawerProv = null;
      res[label] = {stateVal, ofVal, recoverVisible, badge};
    };

    await measure('ok', 'ok');
    await measure('error', 'error');   // ← 熔断/不可达
    await measure('none', null);       // ← 未采集
    return res;
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({out, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_health")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"m1": {"upstream_model": "m"}},
    }]}
    with open(os.path.join(wd, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso = os.path.join(wd, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(wd, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=wd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        d = _pdir()
        jsf = os.path.join(d, "probe_health_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, cwd=d)
        out = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in out.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{out[-1200:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}")
            return 1
        m = res.get("out") or {}
        for k, v in m.items():
            print(f"  [{k}] stateVal={v['stateVal']} ofVal={v['ofVal']!r} "
                  f"recoverVisible={v['recoverVisible']} badge={v['badge']!r}")
        print()
        ok_, err_, none_ = m.get("ok") or {}, m.get("error") or {}, m.get("none") or {}
        check(ok_.get("stateVal") is True, "① ok → healthState()=true", f"{ok_.get('stateVal')}")
        check(err_.get("stateVal") is False, "① error → healthState()=false", f"{err_.get('stateVal')}")
        check(none_.get("stateVal") is None, "① 缺失 → healthState()=null", f"{none_.get('stateVal')}")
        check(ok_.get("ofVal") == "ok" and err_.get("ofVal") == "err" and none_.get("ofVal") == "na",
              "① healthOf 三态 = ok/err/na",
              f"{ok_.get('ofVal')}/{err_.get('ofVal')}/{none_.get('ofVal')}")
        check(err_.get("badge") and ("异常" in err_.get("badge") or "Error" in err_.get("badge")),
              "② error 时抽屉显示「异常」（不再永远停在未检测）", f"{err_.get('badge')!r}")
        check(err_.get("recoverVisible") is True,
              "③ **核心**: 熔断时「测试并恢复」按钮可见（此前永远不出现）",
              f"{err_.get('recoverVisible')}")
        check(ok_.get("recoverVisible") is False,
              "④ 正常时不显示「恢复」按钮", f"{ok_.get('recoverVisible')}")
        check(not (res.get("errs") or []), "⑤ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
