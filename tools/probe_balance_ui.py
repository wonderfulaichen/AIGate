# -*- coding: utf-8 -*-
"""余额四态分色显示的无头浏览器实测.

后端已验证 state 三态分离 (probe_balance_state.py)。本探针验证**前端真的用起来了**:
  ① 未配置 (unsupported) → 灰色 (var(--muted)), 显示"未配置余额查询"
  ② 查询失败 (failed)     → 红色 (var(--err)),   显示"查询失败，点刷新重试"
  ③ 手动设置 (manual)     → 灰色,               显示"手动设置的值（非上游实测）"
  ④ 三种状态的颜色**互不相同** (未配置≠失败, 这是本轮核心: 旧实现两者同色)
  ⑤ 状态文案行可见 (按 rect)
  ⑥ 零 pageerror

判据读**计算后的样式** (computed style), 不是 grep CSS (本项目明确禁止后者)。

红线: 隔离实例 + 随机端口 + mock 上游; 绝不碰用户 8787。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
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


def _playwright_dir():
    for rel in (".openbitfun/tmp/vidtools", ".openbitfun/tmp/ppt", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"error": "boom"}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);

  // 概览页在面板里是「概览」页签 —— 必须切过去, 余额卡才会渲染。
  // (不切的话 providerRows 有数据但 DOM 不存在, 探针会读到空 —— 本轮实际踩到。)
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '概览');
    if (t) t.click();
  });
  await p.waitForTimeout(1200);

  // 直接注入三种状态的余额数据, 观察渲染 (不走网络, 只测前端映射)
  const injected = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.balanceData = [
      {provider:'noapi',   balance:null, currency:'CNY', source:'api',
       error:'未配置余额查询 API', state:'unsupported', last_updated: Math.floor(Date.now()/1000)},
      {provider:'broken',  balance:null, currency:'CNY', source:'api',
       error:'HTTP 500', state:'failed', last_updated: Math.floor(Date.now()/1000)},
      {provider:'handset', balance:42.5, currency:'CNY', source:'manual',
       error:null, state:'manual', last_updated: Math.floor(Date.now()/1000)},
    ];
    return {rows: (st.providerRows||[]).length,
            states: (st.providerRows||[]).map(r => r.state)};
  });
  await p.waitForTimeout(1200);

  const view = await p.evaluate(() => {
    const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const out = {};
    for (const r of (st.providerRows||[])) {
      // 定位策略: 用**状态文案**直接定位那个 div (文本是稳定的判据),
      // 再往上找到该供应商的余额块, 从中取数字 span 的颜色。
      // (不要靠"按 provider 名找父块" —— 那条会命中页面 header, 本轮实际踩到。)
      const want = st.balanceStateText(r);
      let stateDiv = null;
      if (want) {
        stateDiv = Array.from(document.querySelectorAll('div'))
          .filter(d => (d.textContent||'').trim() === want)
          .find(d => d.children.length === 0);
      }
      // 数字 span: 找紧邻的 mono 数值 span —— 从状态 div 往上找所属行块
      let numSpan = null;
      if (stateDiv) {
        const row = stateDiv.parentElement;
        if (row) {
          numSpan = Array.from(row.querySelectorAll('span'))
            .find(s => s.className && s.className.indexOf('mono') >= 0
                    && (s.textContent||'').trim().length > 0
                    && (s.textContent||'').indexOf('更新于') < 0);
        }
      }
      out[r.provider] = {
        state: r.state,
        text: want,
        color: numSpan ? getComputedStyle(numSpan).color : null,
        stateVisible: stateDiv ? vis(stateDiv) : false,
        stateText: stateDiv ? (stateDiv.textContent||'').trim().slice(0,40) : '',
      };
    }
    return out;
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({injected, view, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_bal_ui")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
    cfg = {"providers": [{
        "name": "noapi", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"noapi/m1": {"upstream_model": "m1"}},
    }]}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(workdir, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso_exe], env=env, cwd=workdir,
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
            print(f"{BAD}  隔离实例未就绪")
            return 1

        # 先发一个请求 —— 概览页在「还没有任何请求」时显示的是**引导空态**,
        # 余额卡根本不渲染 (实测踩到: bodyHasWaiting=true, 余额卡不在 DOM 里)。
        # 有了请求记录后概览才进正常布局, 这时注入余额数据才有意义。
        body1 = json.dumps({"model": "noapi/m1",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": False}).encode()
        req1 = urllib.request.Request(base + "/v1/chat/completions", data=body1,
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
        try:
            urllib.request.urlopen(req1, timeout=30).read()
        except Exception:  # noqa: BLE001
            pass  # 上游不可达没关系, 只要记录落了一条
        time.sleep(1.5)

        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(pdir, "probe_bal_ui_shot.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, cwd=pdir)
        out = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in out.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{out[-1500:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  浏览器异常: {res['fatal']}")
            return 1

        view = res.get("view") or {}
        print()
        for name, v in view.items():
            print(f"  {name}: state={v['state']} color={v['color']} "
                  f"stateVisible={v['stateVisible']} text={v['stateText']!r}")

        check(len(view) == 3, "三行余额都已渲染", f"{list(view)}")

        noapi = view.get("noapi") or {}
        broken = view.get("broken") or {}
        handset = view.get("handset") or {}

        check(noapi.get("text") == "未配置余额查询（该供应商无公开余额接口）",
              "① 未配置 → 文案正确", f"{noapi.get('text')!r}")
        check(broken.get("text") == "查询失败，点刷新重试",
              "② 失败 → 文案正确", f"{broken.get('text')!r}")
        check(handset.get("text") == "手动设置的值（非上游实测）",
              "③ 手动 → 文案正确", f"{handset.get('text')!r}")

        # ④ 颜色: 未配置(灰) 与 失败(红) 必须不同 —— 本轮核心
        c_noapi = noapi.get("color")
        c_broken = broken.get("color")
        check(c_noapi is not None and c_broken is not None,
              "④ 读到了计算后的颜色 (computed style)", f"{c_noapi} / {c_broken}")
        check(c_noapi != c_broken,
              "④ **核心**: 未配置 与 失败 颜色不同 (旧实现两者同色)",
              f"unsupported={c_noapi} failed={c_broken}")

        # ⑤ 状态行可见
        for nm, v in (("noapi", noapi), ("broken", broken), ("handset", handset)):
            check(v.get("stateVisible") is True, f"⑤ {nm} 的状态说明行可见",
                  f"{v.get('stateText')!r}")

        errs = res.get("errs") or []
        check(not errs, "⑥ 零 pageerror", f"{errs[:3]}")
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项判据未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
