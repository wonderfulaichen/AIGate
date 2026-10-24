# -*- coding: utf-8 -*-
"""验收：记录表纵向 sticky 表头.

为什么必须实测: `position:sticky` 极易被**祖先的 overflow** 破坏 ——
外层 `overflow-x:auto` 会创建新的滚动上下文，sticky 就改相对那个 div 生效，
而那个 div 高度跟着表格走、自己从不滚动 → 表现为"设了 sticky 但毫无效果"。
静态看 CSS 完全看不出这个问题。

判据:
  ① 滚动前表头在表格内正常位置
  ② 把 main 滚下去 ~600px 后，表头**仍贴在可视区顶部附近**（而不是滚出视野）
  ③ 表头有不透明背景（否则行内容会透上来）
  ④ 横向滚动时首列仍固定（sticky 两层共存）
  ⑤ 零 pageerror
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_sticky")
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
  const p = await b.newPage({ viewport: { width: 1600, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,250)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(3200);
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('records'));
  await p.waitForTimeout(1600);

  const probe = async () => p.evaluate(() => {
    const th = document.querySelector('.rec-table thead th');
    const wrap = document.querySelector('.rec-table') ? document.querySelector('.rec-table').parentElement : null;
    if (!th) return { none: true };
    const r = th.getBoundingClientRect();
    const cs = getComputedStyle(th);
    const main = document.querySelector('main');
    return {
      thTop: Math.round(r.top),
      thVisible: r.bottom > 0 && r.top < window.innerHeight,
      bg: cs.backgroundColor,
      pos: cs.position,
      tableTop: Math.round(document.querySelector('.rec-table').getBoundingClientRect().top),
      mainScroll: Math.round(main.scrollTop),
      wrapOverflowX: wrap ? getComputedStyle(wrap).overflowX : null,
      wrapScrollH: wrap ? wrap.scrollHeight : null,
      wrapClientH: wrap ? wrap.clientHeight : null,
    };
  });

  const before = await probe();
  await p.screenshot({ path: `%(out)s/before.png` });

  // 滚动**表格自己的容器**（不是 main）—— 实现改成了表格区域独立滚动，
  // 因为 sticky 的参照必须是"真正在滚的那个容器"。
  const scrolled = await p.evaluate(() => {
    const wrap = document.querySelector('.rec-scroll');
    if (!wrap) return { none: true };
    wrap.scrollTop = 600;
    return { scrollTop: wrap.scrollTop, sh: wrap.scrollHeight, ch: wrap.clientHeight };
  });
  await p.waitForTimeout(700);
  const after = await probe();
  after.wrapScrollTop = scrolled.scrollTop;
  await p.screenshot({ path: `%(out)s/after_scroll.png` });

  // 横向滚动测试首列是否仍固定
  const horiz = await p.evaluate(() => {
    const wrap = document.querySelector('.rec-table').parentElement;
    if (!wrap) return { none: true };
    wrap.scrollLeft = 200;
    const td = document.querySelector('.rec-table tbody tr td.th-sticky');
    const rr = td ? td.getBoundingClientRect() : null;
    const wr = wrap.getBoundingClientRect();
    return { tdLeft: rr ? Math.round(rr.left) : null, wrapLeft: Math.round(wr.left),
             scrollLeft: wrap.scrollLeft };
  });

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({ before, after, horiz, errs }));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_sticky")
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
        jsf = os.path.join(d, "probe_sticky_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base, "out": OUT.replace("\\", "/")})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        bf = res.get("before") or {}
        af = res.get("after") or {}
        ho = res.get("horiz") or {}
        print(f"  滚动前: {json.dumps(bf, ensure_ascii=False)}")
        print(f"  滚动后: {json.dumps(af, ensure_ascii=False)}")
        print(f"  横滚:   {json.dumps(ho, ensure_ascii=False)}")
        print()

        check(bf.get("pos") == "sticky", "① 表头是 position:sticky", f"pos={bf.get('pos')}")
        check((af.get("wrapScrollTop") or 0) > 300, "② 表格容器确实滚了（前提条件）",
              f"wrapScrollTop={af.get('wrapScrollTop')}")
        # 核心判据：表格容器滚动后，表头**留在原地**而表格内容确实向上走了。
        # ⚠️ 不能判"thTop 落在 0~80px" —— sticky 是相对**滚动容器**定位的，
        # 容器本身在页面里位于屏幕 430px 处，表头就停在那，不是屏幕顶部。
        # 正确的判据是：th 的屏幕位置不变，而表格顶边明显上移（说明内容在滚、表头没跟）。
        th_same = abs((af.get("thTop") or 0) - (bf.get("thTop") or 0)) <= 2
        table_moved = abs((af.get("tableTop") or 0) - (bf.get("tableTop") or 0)) > 100
        check(af.get("thVisible") is True and th_same and table_moved,
              "② **滚动后表头留在原地、表格内容上移**（纵向 sticky 真的生效）",
              f"thTop {bf.get('thTop')}→{af.get('thTop')} tableTop {bf.get('tableTop')}→{af.get('tableTop')}")
        check(bf.get("bg") not in (None, "", "rgba(0, 0, 0, 0)", "transparent"),
              "③ 表头有不透明背景（否则行会透上来）", f"bg={bf.get('bg')}")
        check(ho.get("tdLeft") is not None and abs((ho.get("tdLeft") or 0) - (ho.get("wrapLeft") or 0)) <= 2,
              "④ 横向滚动时首列仍固定（两层 sticky 共存）",
              f"tdLeft={ho.get('tdLeft')} wrapLeft={ho.get('wrapLeft')}")
        check(not (res.get("errs") or []), "⑤ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
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
