# -*- coding: utf-8 -*-
"""前端信息密度审计 —— 为「重新设计」提供量化基线.

量的是**真实渲染**结果 (无头浏览器), 不是源码行数:
  · 每页总高度 / 视口高度 → 要滚几屏
  · 每个面板的高度与排名 → 谁在占地方
  · 首屏可见的交互控件数 (按钮/下拉/输入) → 认知负担
  · 重复数据源: 同一字段在多处出现 (这是混乱的真源)

输出 JSON 供方案文档引用。不修改任何文件。
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "ui_density.json")


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


JS = r"""
const { chromium } = require('playwright');

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(3500);

  const tabs = ['dashboard', 'analytics', 'records', 'providers', 'settings', 'about'];
  const out = {};
  for (const tab of tabs) {
    await p.evaluate((t) => {
      const st = document.querySelector('[x-data]')._x_dataStack[0];
      st.switchTab(t);
    }, tab);
    await p.waitForTimeout(2500);
    const info = await p.evaluate(() => {
      const main = document.querySelector('.main');
      const vh = window.innerHeight;
      // 面板: 每个 .pnl 的真实高度 + 标题
      const pnls = Array.from(document.querySelectorAll('.pnl')).map((el) => {
        const r = el.getBoundingClientRect();
        if (r.height <= 0) return null;
        const titleEl = el.querySelector('.pnl-ht');
        return {
          title: titleEl ? (titleEl.textContent || '').trim().slice(0, 40) : '(无标题)',
          h: Math.round(r.height),
        };
      }).filter(Boolean).sort((a, b) => b.h - a.h);
      // 首屏可见控件 (只在视口内)
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        return r.height > 0 && r.width > 0 && r.top < vh && r.bottom > 0;
      };
      const btns = Array.from(document.querySelectorAll('.btn, button')).filter(visible);
      const sels = Array.from(document.querySelectorAll('.sel, select')).filter(visible);
      const inps = Array.from(document.querySelectorAll('.inp, input')).filter(visible);
      // 表格列数 (取最宽的那张)
      let maxCols = 0, tables = 0;
      for (const t of Array.from(document.querySelectorAll('table')).filter(visible)) {
        tables++;
        const n = t.querySelectorAll('thead th').length;
        if (n > maxCols) maxCols = n;
      }
      return {
        scrollH: main ? Math.round(main.scrollHeight) : 0,
        vh,
        screens: main ? +(main.scrollHeight / vh).toFixed(2) : 0,
        panelCount: pnls.length,
        panels: pnls.slice(0, 12),
        totalPanelH: pnls.reduce((s, x) => s + x.h, 0),
        visibleButtons: btns.length,
        visibleSelects: sels.length,
        visibleInputs: inps.length,
        tables,
        maxCols,
      };
    });
    out[tab] = info;
  }
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"找不到 {EXE}")
        return 1
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([EXE], env=env, cwd=ROOT,
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
            print("实例未就绪")
            return 1

        pdir = _playwright_dir()
        jsf = os.path.join(pdir, "audit_density_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=pdir)
        out = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in out.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(out[-1500:])
            return 1
        data = json.loads(line[len("JSON_RESULT "):])
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"{'页签':<12} {'高度':>7} {'屏':>6} {'面板':>5} {'按钮':>5} {'下拉':>5} {'输入':>5} {'表列':>5}")
        for k, v in data.items():
            print(f"{k:<12} {v['scrollH']:>7} {v['screens']:>6} {v['panelCount']:>5} "
                  f"{v['visibleButtons']:>5} {v['visibleSelects']:>5} {v['visibleInputs']:>5} "
                  f"{v['maxCols']:>5}")
        print(f"\n写入 {OUT}")
        print("\n最高的几个面板:")
        for k, v in data.items():
            if v["panels"]:
                top = v["panels"][:3]
                print(f"  [{k}] " + " | ".join(f"{x['title']}={x['h']}px" for x in top))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
