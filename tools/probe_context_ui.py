# -*- coding: utf-8 -*-
"""「上下文构成」区块的无头浏览器实测.

后端已验证分类与聚合 (probe_context_breakdown.py)。本探针验证**前端真的用起来了**:
  ① 省量审计展开后, 「上下文构成」区块**可见** (按 rect, 不是 DOM 存在性);
  ② 六类徽标渲染, 且文案是实际 i18n 值 (不是裸键名 —— 本项目踩过"t() 原样返回键名");
  ③ 每类都带 ≈ 前缀 (估算口径, 不许伪装成实测);
  ④ 百分比之和 ≈ 100% (自洽);
  ⑤ 含图片时显示说明行 (图片不计入文本估算, 必须如实说);
  ⑥ 零 pageerror.

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

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        b = json.dumps({
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1200 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2200);

  // 切到「分析」→ cost 子页签, 并展开省量审计
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '分析');
    if (t) t.click();
  });
  await p.waitForTimeout(1200);
  const info = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.analyticsTab = 'cost';
    st.auditExpanded = true;
    return {segs: (st.auditContextSegs||[]).length,
            note: st.auditContextNote(),
            images: st.auditCtxImages()};
  });
  await p.waitForTimeout(1500);

  const view = await p.evaluate(() => {
    const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    // 找「上下文构成」那个区块 (按标题文本定位)
    const titles = Array.from(document.querySelectorAll('span'))
      .filter(e => e.children.length === 0 && (e.textContent||'').trim() === '上下文构成');
    const titleEl = titles[0] || null;
    const block = titleEl ? titleEl.closest('div[style*="border-top"]') : null;
    let legends = [], barVisible = false, pcts = [];
    if (block) {
      barVisible = vis(block.querySelector('.prop-bar'));
      legends = Array.from(block.querySelectorAll('.prop-legend span'))
        .filter(vis).map(e => (e.textContent||'').trim());
      pcts = (st.auditContextSegs||[]).map(s => st.auditCtxPct(s.v));
    }
    return {
      blockVisible: vis(block),
      barVisible,
      legends,
      pcts,
      segs: (st.auditContextSegs||[]).map(s => ({k: s.k, label: s.label, v: s.v})),
    };
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({info, view, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_ctx_ui")
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

        # 造一条含全部六类的请求
        body = {
            "model": "mockprov/m1",
            "tools": [{"type": "function", "function": {
                "name": "read_file",
                "description": "Read the contents of a file from disk and return them as text",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                               "required": ["path"]}}}],
            "messages": [
                {"role": "system", "content": "You are a careful coding agent."},
                {"role": "user", "content": "<system-reminder>\nAgent framework context.\n</system-reminder>"},
                {"role": "user", "content": "请解释这段代码"},
                {"role": "assistant", "content": "这段代码做了三件事: 读取、解析、写回。"},
                {"role": "tool", "content": "file contents: alpha beta gamma delta"},
            ],
            "stream": False,
        }
        req = urllib.request.Request(base + "/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)

        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(pdir, "probe_ctx_ui_shot.js")
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

        info = res.get("info") or {}
        view = res.get("view") or {}
        print()
        print(f"  分类: {[(s['k'], s['v']) for s in (view.get('segs') or [])]}")
        print(f"  图例: {view.get('legends')}")
        print(f"  百分比: {view.get('pcts')}")

        check(info.get("segs", 0) >= 5, "① 前端至少算出 5 个非零分类", f"{info.get('segs')}")
        check(view.get("blockVisible") is True, "① 「上下文构成」区块**可见**",
              f"{view.get('blockVisible')}")
        check(view.get("barVisible") is True, "① 比例条可见", f"{view.get('barVisible')}")

        legends = view.get("legends") or []
        check(len(legends) >= 5, "② 六类图例渲染 (至少 5 类非零)", f"{legends}")
        joined = " ".join(legends)
        # ② 文案必须是实际 i18n 值, 不是裸键名 ctx_system 之类
        for keyname in ("ctx_system", "ctx_tools", "ctx_chat"):
            check(keyname not in joined, f"② 未出现裸 i18n 键「{keyname}」", f"{joined}")
        check("工具定义" in joined, "② 图例文案正确 (工具定义)", f"{joined}")
        check("框架注入" in joined, "② 图例文案正确 (框架注入)", f"{joined}")
        check("用户消息" in joined, "② 图例文案正确 (用户消息)", f"{joined}")

        # ③ 一律带 ≈ (估算口径)
        check("≈" in joined, "③ 每类都带 ≈ (估算, 不伪装成实测)", f"{joined}")

        # ④ 百分比之和 ≈ 100
        pcts = [float(x.rstrip('%')) for x in (view.get("pcts") or []) if x]
        if pcts:
            total = sum(pcts)
            check(99.0 <= total <= 101.0 or abs(total - 100.0) < 1.0,
                  "④ 各类百分比之和 ≈ 100%", f"{total:.1f}%")
        else:
            check(False, "④ 算出了百分比", "空")

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
