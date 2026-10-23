# -*- coding: utf-8 -*-
"""「测试所用密钥」的无头浏览器实测.

背景 (2026-10-24): 面板密钥改完即存、其余字段停在草稿, 用户无法从界面看出
"这次测的是哪个密钥" —— 那正是"测试通过、保存后却报错"的困惑来源。

本探针验证前端三件事:
  ① 点「测试」时, 请求体带着表单里正在编辑的密钥 (api_key 字段);
  ② 结果横幅显示"测试所用密钥: …"那一行 (后端 key_source 落地到界面);
  ③ 零 pageerror (Alpine 表达式异常是静默的, 只能从这里抓).

判据用**网络请求体**与**渲染文本**, 不断言"代码里写了什么"。

红线: 隔离实例 + 随机端口 + 本地 mock 上游; 绝不碰用户 8787。
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
        body = json.dumps({"object": "list", "data": [{"id": "m1"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
  const errs = [];
  const testBodies = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  // 抓「测试」请求体: 看前端有没有把草稿密钥带上去
  p.on('request', r => {
    if (r.url().includes('/admin/api/providers/test')) {
      testBodies.push(r.postData() || '');
    }
  });

  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2200);

  // 切到「供应商」页签
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '供应商');
    if (t) t.click();
  });
  await p.waitForTimeout(2200);

  // 在草稿密钥输入框里填一个值 (模拟"用户正在编辑但还没保存")
  const filled = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (!st || !(st.providersFormData||[]).length) return {ok:false, why:'no providers'};
    const prov = st.providersFormData[0];
    prov._keyDraft = 'draft-secret-XYZ';
    return {ok:true, name: prov.name};
  });
  await p.waitForTimeout(300);

  // 点「测试」按钮
  const clicked = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (!st) return {ok:false};
    try { st.testProvider(0, false); return {ok:true}; }
    catch (e) { return {ok:false, err: String(e)}; }
  });
  await p.waitForTimeout(3000);

  // 读回结果与渲染文本
  const after = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    const prov = st && (st.providersFormData||[])[0];
    const res = prov && prov._testResult;
    // 抽屉里那一行说明 (若抽屉开着)
    let keyLine = '';
    const nodes = Array.from(document.querySelectorAll('div,span'))
      .filter(e => e.children.length === 0)
      .map(e => (e.textContent||'').trim())
      .filter(s => s.indexOf('测试所用密钥') >= 0);
    if (nodes.length) keyLine = nodes[0];
    return {
      key_source: res && res.key_source,
      key_checked: res && res.key_checked,
      success: res && res.success,
      keyLine,
      hasFn: typeof st.testKeySourceText,
    };
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({filled, clicked, after, testBodies, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_key_ui")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mockprov/m1": {"upstream_model": "m1"}},
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

        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(pdir, "probe_key_ui_shot.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, cwd=pdir)
        out = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in out.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{out[-1200:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  浏览器异常: {res['fatal']}")
            return 1

        print()
        check(res.get("filled", {}).get("ok") is True, "供应商页有数据", f"{res.get('filled')}")
        check(res.get("clicked", {}).get("ok") is True, "能调用 testProvider", f"{res.get('clicked')}")

        bodies = res.get("testBodies") or []
        check(len(bodies) >= 1, "确实发出了测试请求", f"{bodies}")
        if bodies:
            try:
                sent = json.loads(bodies[-1])
            except Exception:  # noqa: BLE001
                sent = {}
            check(sent.get("api_key") == "draft-secret-XYZ",
                  "① **核心**: 请求体带上了表单草稿密钥 (api_key)",
                  f"{sent}")
            check("mockprov" in (sent.get("name") or ""), "请求仍带 name (后端据此回落已保存链)",
                  f"{sent.get('name')}")

        after = res.get("after") or {}
        check(after.get("key_source") == "draft",
              "② 后端回带 key_source=draft (前端拿到了)", f"{after.get('key_source')}")
        check(after.get("hasFn") == "function",
              "前端有 testKeySourceText 方法 (typeof 返回 'function')",
              f"{after.get('hasFn')}")

        errs = res.get("errs") or []
        check(not errs, "零 pageerror", f"{errs[:3]}")
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
