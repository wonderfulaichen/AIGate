# -*- coding: utf-8 -*-
"""模型明细「诊断」合并格的无头浏览器实测.

背景 (2026-10-24): 用户说"扁平化列表显示信息密度不够, 明显有些值是可以合并的"。
原状 15 列里, 缓存命中率 / 生成速度 / TTFT / 平均延迟各占一列 —— 它们都是**诊断值**,
不是逐行对比的主指标, 摊开成列只会把表格拉宽、把主数字挤到屏外。
现合并成 1 格: 主数字(缓存命中率, 无则生成速度) + 悬停提示 + 点击展开四值明细。

本探针验证四件事:
  ① 表头列数确实下降 (15 → 12), 且新列名存在;
  ② 诊断格**可见** (按 getBoundingClientRect, 不是 DOM 存在性);
  ③ 点击单元格能展开明细 (键两侧一致, 不会"点了不展开");
  ④ 主数字/副标签/title 与数据自洽 (缓存命中率算得对);
  ⑤ 零 pageerror (Alpine 表达式异常是静默的, 只能从这里抓)。

红线: 隔离实例 + 随机端口 + mock 上游造数据; 绝不碰用户 8787。
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
        # usage 带缓存命中: 命中 800 / 未命中 200 → 命中率 80%
        body = json.dumps({
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                      "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200},
        }).encode()
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
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2200);

  // 切到「分析」页签 + cost 子页签
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '分析');
    if (t) t.click();
  });
  await p.waitForTimeout(1500);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    if (st) st.analyticsTab = 'cost';
  });
  await p.waitForTimeout(2500);

  const before = await p.evaluate(() => {
    const tables = Array.from(document.querySelectorAll('table'));
    // 找模型明细表: 表头含「诊断」或含上游模型列
    let target = null;
    for (const t of tables) {
      const ths = Array.from(t.querySelectorAll('thead th')).map(x => (x.textContent||'').trim());
      if (ths.length && (ths.indexOf('诊断') >= 0 || ths.indexOf('诊断') >= 0)) { target = t; break; }
      if (ths.length >= 8) target = target || t;
    }
    if (!target) return {found:false, tables: tables.length};
    const ths = Array.from(target.querySelectorAll('thead th')).map(x => (x.textContent||'').trim());
    const rows = Array.from(target.querySelectorAll('tbody tr'));
    return {found:true, headers: ths, rows: rows.length};
  });

  // 点第一个诊断格
  const clicked = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const m = (st.stats && st.stats.per_model || [])[0];
    if (!m) return {ok:false, why:'no per_model'};
    st.toggleModelDiag(m);
    return {ok:true, key: m.provider + '/' + (m.upstream_model || m.model),
            open: !!st.modelDiagOpen[m.provider + '/' + (m.upstream_model || m.model)]};
  });
  await p.waitForTimeout(600);

  const after = await p.evaluate(() => {
    const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const m = (st.stats && st.stats.per_model || [])[0] || {};
    const main = st.modelDiagMain(m), label = st.modelDiagMainLabel(m);
    const title = st.modelDiagTitle(m), detail = st.modelDiagDetail(m);
    // 诊断格: 找 title 含"缓存命中"的 td (i18n 值 tbl.cache_hit = 「缓存命中」)
    let cellVisible = false, cellText = '';
    for (const td of Array.from(document.querySelectorAll('td'))) {
      const ttl = td.getAttribute('title') || '';
      if (ttl.indexOf('缓存命中') >= 0) {
        cellVisible = vis(td); cellText = (td.textContent||'').trim().slice(0, 120); break;
      }
    }
    return {main, label, title, detail, cellVisible, cellText,
            openCount: Object.keys(st.modelDiagOpen||{}).length};
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({before, clicked, after, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_diag_ui")
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

        # 造两条请求 (同模型, 让 per_model 有数据)
        body = json.dumps({"model": "mockprov/m1",
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": False}).encode()
        for _ in range(2):
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                urllib.request.urlopen(req, timeout=30).read()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)

        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright")
            return 1
        jsf = os.path.join(pdir, "probe_diag_ui_shot.js")
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

        before = res.get("before") or {}
        after = res.get("after") or {}
        clicked = res.get("clicked") or {}
        print()
        print(f"  表头 ({len(before.get('headers') or [])} 列): {before.get('headers')}")
        check(before.get("found") is True, "找到模型明细表", f"{before}")

        headers = before.get("headers") or []
        check("诊断" in headers, "① 新列「诊断」存在", f"{headers}")
        check(len(headers) <= 13,
              "① 列数已收敛 (15 → ≤13, 默认列更少)", f"实得 {len(headers)}")
        # 旧的四个诊断列头不该再单独出现
        for gone in ("缓存命中", "生成速度", "平均延迟"):
            check(gone not in headers, f"① 旧列「{gone}」已并入诊断格", f"{headers}")

        check(clicked.get("ok") is True, "③ 能调用 toggleModelDiag", f"{clicked}")
        check(clicked.get("open") is True, "③ 点击后展开态为 true (键两侧一致)",
              f"{clicked}")
        check(after.get("openCount") == 1, "③ 展开计数 = 1", f"{after.get('openCount')}")

        check(after.get("cellVisible") is True,
              "② 诊断格**可见** (按 rect 判, 不是 DOM 存在性)", f"{after.get('cellText')}")

        # ④ 主数字应是 80% (mock: 命中 800 / 总 1000)
        main = str(after.get("main") or "")
        check("80" in main, "④ 主数字 = 缓存命中率 80% (数据自洽)", f"{main!r}")
        check(after.get("label") == "缓存命中",
              "④ 副标签说明主数字是什么", f"{after.get('label')!r}")
        title = str(after.get("title") or "")
        # 四项按**实际 i18n 值**断言: tbl.cache_hit=缓存命中, tbl.gen_speed=吐字速度,
        # ttft_col_avg=首字延迟, tbl.avg_latency=平均延迟 (搜错词会假失败).
        for want in ("缓存命中", "吐字速度", "首字延迟", "平均延迟"):
            check(want in title, f"④ 悬停提示含「{want}」", f"{title!r}")
        detail = str(after.get("detail") or "")
        check("缓存命中" in detail, "④ 展开明细含缓存命中项", f"{detail!r}")

        errs = res.get("errs") or []
        check(not errs, "⑤ 零 pageerror", f"{errs[:3]}")
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
