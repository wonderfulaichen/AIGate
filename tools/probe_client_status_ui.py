# -*- coding: utf-8 -*-
"""「状态说明」在记录详情里的无头浏览器实测.

后端已验证 client_status 落盘 (probe_client_status.py)。本探针验证**前端把它显示出来**:
  ① 打开该条记录详情 → 「状态说明」一栏**可见** (按 rect);
  ② 文案含两个数字 (日志记 502 / 客户端实收 200), 用户能直接对上账;
  ③ 正常记录 (无 client_status) **不显示**该栏 (不产噪声);
  ④ 零 pageerror.

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
STATE = {"mode": "error"}


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

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if STATE["mode"] == "error":
            p = json.dumps({"error": {"message": "boom mid-stream", "type": "server_error"}})
            self.wfile.write(f"data: {p}\n\n".encode())
        else:
            c = json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "m",
                            "choices": [{"index": 0, "delta": {"content": "hi"},
                                         "finish_reason": None}]})
            self.wfile.write(f"data: {c}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2200);
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '记录');
    if (t) t.click();
  });
  await p.waitForTimeout(2500);

  const n = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    return (st.logs||[]).length;
  });
  const out = {};
  // 逐条打开: **必须在 Playwright 侧等待** —— Alpine 的 DOM 更新是异步的,
  // 在同一个 evaluate 里连续 openLogDetail 再读 DOM 会读到上一帧 (本轮实际踩到)。
  for (let i = 0; i < n; i++) {
    const meta = await p.evaluate((idx) => {
      const st = document.querySelector('[x-data]')._x_dataStack[0];
      const log = st.logs[idx];
      st.openLogDetail(log);
      return {status: log.status, client_status: log.client_status,
              helper: st.statusMismatchText(log)};
    }, i);
    await p.waitForTimeout(700); // 等 Alpine 渲染
    const view = await p.evaluate(() => {
      const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
      const card = document.querySelector('.log-modal-card');
      if (!card) return {hasItem: false, shown: null, text: ''};
      for (const it of Array.from(card.querySelectorAll('.log-detail-item'))) {
        const k = (it.querySelector('.log-detail-key')||{}).textContent || '';
        if (k.trim() === '状态说明') {
          return {hasItem: true, shown: vis(it),
                  text: ((it.querySelector('.log-detail-val')||{}).textContent || '').trim()};
        }
      }
      return {hasItem: false, shown: null, text: ''};
    });
    out[meta.status + ':' + (meta.client_status ?? 'null')] =
      Object.assign(meta, {hasItem: view.hasItem, shown: view.shown, text: view.text});
  }

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({res: out, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_cs_ui")
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

        def stream():
            body = json.dumps({"model": "mockprov/m1",
                               "messages": [{"role": "user", "content": "hi"}],
                               "stream": True}).encode()
            r = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                       headers={"Content-Type": "application/json"},
                                       method="POST")
            try:
                urllib.request.urlopen(r, timeout=60).read()
            except Exception:  # noqa: BLE001
                pass

        STATE["mode"] = "error"
        stream()
        time.sleep(1.2)
        STATE["mode"] = "ok"
        stream()
        time.sleep(1.5)

        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(pdir, "probe_cs_ui_shot.js")
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

        rows = res.get("res") or {}
        print()
        for k, v in rows.items():
            print(f"  {k}: hasItem={v['hasItem']} shown={v['shown']} text={v['text']!r}")

        # 找 502 那条 (client_status=200)
        mism = next((v for k, v in rows.items() if k.startswith("502:")), None)
        okr = next((v for k, v in rows.items() if k.startswith("200:null")), None)

        check(mism is not None, "① 找到语义 502 的那条记录", f"{list(rows)}")
        if mism:
            check(mism.get("shown") is True, "① **核心**: 「状态说明」一栏可见",
                  f"{mism}")
            t = mism.get("text") or ""
            check("502" in t and "200" in t,
                  "② 文案含两个数字 (日志 502 / 客户端 200)", f"{t!r}")
            check("状态说明" in (mism.get("helper") or "") or "502" in (mism.get("helper") or ""),
                  "② helper 也返回了说明文案", f"{mism.get('helper')!r}")

        check(okr is not None, "③ 找到正常 200 记录 (对照)", f"{list(rows)}")
        if okr:
            check(not okr.get("helper"),
                  "③ **对照**: 正常记录不产生说明 (无噪声)", f"{okr.get('helper')!r}")

        errs = res.get("errs") or []
        check(not errs, "④ 零 pageerror", f"{errs[:3]}")
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
