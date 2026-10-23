# -*- coding: utf-8 -*-
"""记录详情「调用来源 / 关联键 / 上游尝试」的无头浏览器实测.

为什么必需 (本项目已踩出五类"看着有内容其实是错的"静默缺陷):
字段落进日志 ≠ 用户能看见。本轮最容易踩的两个坑:
  · 插了 DOM 但 Alpine 表达式抛异常 → 元素静默空白 (本项目真实事故过);
  · 用 `x-show` 控制, 元素仍在 DOM 里 —— 按"存在性"数会数出并不显示的东西。

故本探针:
  ① 起**隔离实例** + mock 上游, 造出一条"先 500 后 200"的重试记录;
  ② 用无头浏览器打开记录页 → 点开详情;
  ③ 断言: 来源名/关联键/上游请求号**文本内容**正确;
  ④ 断言尝试明细按 `getBoundingClientRect()` **可见**(不是 offsetHeight 恒 0 的坑);
  ⑤ 断言零 pageerror (Alpine 表达式异常是静默的, 只能从这里抓)。

红线: 隔离实例 + 随机端口, 绝不碰用户 8787。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
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
    """找带 playwright 的 node_modules 所在目录 (脚本要放这里才能 require 到)。

    仓库把 playwright 放在 `.openbitfun/tmp/vidtools/node_modules`(视频/截图工具链),
    不在仓库根。这里按固定候选 + `node_modules/playwright` 存在性判定, 找不到返回 None。
    """
    for rel in (".openbitfun/tmp/vidtools", ".openbitfun/tmp/ppt", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


STATE = {"seen": 0, "fail_times": 1}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        STATE["seen"] += 1
        if STATE["seen"] <= STATE["fail_times"]:
            body = json.dumps({"error": {"message": "mock transient"}}).encode()
            self.send_response(500)
        else:
            body = json.dumps({
                "id": "x", "object": "chat.completion", "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2},
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", "up-ui-777")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);

  // 切到「记录」页签
  await p.evaluate(() => {
    const els = Array.from(document.querySelectorAll('button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent||'').trim() === '记录');
    if (t) t.click();
  });
  await p.waitForTimeout(2500);

  // 点第一条记录的详情入口
  const opened = await p.evaluate(() => {
    const root = document.querySelector('[x-data]');
    const st = root && root._x_dataStack && root._x_dataStack[0];
    if (!st || !(st.logs||[]).length) return {ok:false, why:'no logs', n: st?(st.logs||[]).length:-1};
    const log = st.logs[0];
    st.openLogDetail(log);
    return {ok:true, trace: log.trace_id, client: log.client_name,
            atts: (log.attempts||[]).length, up: log.upstream_request_id};
  });
  await p.waitForTimeout(800);

  // 收集详情里的可见文本 (只取真正渲染出来、宽高非 0 的元素)
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const dump = await p.evaluate((visSrc) => {
    const vis = eval('(' + visSrc + ')');
    const card = document.querySelector('.log-modal-card');
    if (!card) return {found:false};
    const items = Array.from(card.querySelectorAll('.log-detail-item'))
      .filter(vis)
      .map(it => ({
        key: (it.querySelector('.log-detail-key')||{}).textContent || '',
        val: (it.querySelector('.log-detail-val')||{}).textContent || '',
      }));
    const modalText = card.innerText || '';
    return {found:true, items, modalText, attemptRowCount:
      Array.from(card.querySelectorAll('.log-detail-item')).filter(vis)
        .filter(it => (it.textContent||'').includes('#1')).length};
  }, vis.toString());

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({opened, dump, errs}));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1

    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_origin_ui")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "MOCK_KEY", "api_key_default": "dummy",
        "models": {"m1": {"upstream_model": "m"}},
    }]}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)

    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = data_dir
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

        # 造一条"先 500 后 200"的重试记录
        body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        req = urllib.request.Request(
            base + "/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "x-aigate-client": "UIProbeAgent",
                     "x-request-id": "trace-ui-1"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.2)

        js = JS % {"base": base}
        # 脚本必须落在**能找到 playwright 的目录**。本仓库的 playwright 装在
        # `.openbitfun/tmp/vidtools/node_modules`(截图/视频工具链共用), 不在仓库根 ——
        # 放错地方会报 "Cannot find module 'playwright'"(node 按脚本目录逐级找 node_modules)。
        pdir = _playwright_dir()
        if not pdir:
            print(f"{BAD}  找不到 playwright 安装位置 (查过 .openbitfun/tmp/*/node_modules)")
            return 1
        js_path = os.path.join(pdir, "probe_origin_ui_shot.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)
        # 必须显式指定 UTF-8: node 输出含中文, 默认按系统代码页(GBK)解码会抛
        # UnicodeDecodeError (Windows 上的老坑)。
        r = subprocess.run(["node", js_path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, cwd=pdir)
        out = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in out.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回结果:\n{out[-1500:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  浏览器脚本异常: {res['fatal']}")
            return 1

        opened = res.get("opened") or {}
        dump = res.get("dump") or {}
        print(f"\n  记录数: {opened.get('n')}  打开详情: {opened.get('ok')}")
        check(opened.get("ok") is True, "记录页有数据并能打开详情", f"{opened}")
        check(dump.get("found") is True, "详情弹窗已渲染", f"{dump.get('found')}")

        items = dump.get("items") or []
        kv = {i["key"]: i["val"] for i in items}
        print(f"  详情字段 ({len(items)} 项): " + ", ".join(kv.keys()))
        check(any("调用来源" in k for k in kv), "详情里有「调用来源」字段", f"{list(kv)}")
        check(any("关联键" in k for k in kv), "详情里有「关联键」字段", f"{list(kv)}")
        check(any("上游请求号" in k for k in kv), "详情里有「上游请求号」字段", f"{list(kv)}")

        joined = json.dumps(kv, ensure_ascii=False)
        check("UIProbeAgent" in joined, "调用来源显示为本次请求的真实来源", joined[:300])
        check("trace-ui-1" in joined, "关联键显示为客户端传入的值", joined[:300])
        check("up-ui-777" in joined, "上游请求号显示为上游返回的值", joined[:300])

        # 尝试明细: 必须有 #1 / #2 两行且可见
        check(dump.get("attemptRowCount", 0) >= 1,
              "尝试明细行可见 (按 rect 判可见, 不是 DOM 存在性)",
              f"rows={dump.get('attemptRowCount')}")
        modal = dump.get("modalText") or ""
        check("#1" in modal and "#2" in modal, "两次尝试都显示 (#1/#2)", modal[:400])
        check("上游尝试" in modal, "尝试区块标题已渲染", modal[:200])

        errs = res.get("errs") or []
        check(not errs, "无 pageerror (Alpine 表达式异常会被静默吞掉)", f"{errs[:3]}")
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
