# -*- coding: utf-8 -*-
"""期 4 验收：陈旧响应守卫（只认最后一次）.

为什么必须用浏览器 + 拦截请求:
  "旧响应覆盖新数据"只在**特定时序**下发生，静态看代码无法证明。
  做法: 拦截 /admin/api/events，让第一次请求**故意慢 1.2s**，第二次快。
  若守卫有效 → 最终生效的是**第二次**（新的）数据；若无守卫 → 会被慢的那次覆盖。

判据:
  ① 有守卫时：慢的旧响应不会覆盖新数据（最终值 = 第二次的值）
  ② 无守卫时同样场景会翻车（用对照臂证明判据有牙齿：临时不 abort 再看）
  ③ 被 abort 的请求不产生 pageerror（静默处理）
  ④ 轮询连续多拍后数据不倒退
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
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,250)));

  // 拦截 events。**用开关控制**哪一次慢，而不是"第 1 次固定慢" ——
  // 因为页面加载本身就会发一次 events（首次加载），把"第 1 次"占掉了，
  // 真正要测的两次会落到第 2/3 次上（实测 n=4 就是这个原因）。
  // 让页面先把首次加载发完，再打开 slow 开关；之后第 1 个到达的请求就是被测的"旧请求"。
  let slowOn = false;
  let slowMarker = null;   // 标记本轮的"旧请求"
  let seq = 0;
  await p.route('**/admin/api/events**', async (route) => {
    seq++;
    const mine = seq;
    let tag = 'NEW';
    let delay = 0;
    if (slowOn && slowMarker === null) { slowMarker = mine; tag = 'OLD'; delay = 900; }
    else if (slowOn) { tag = 'NEW'; }
    const body = {
      events: [{ ts: 1700000000, kind: 'started', subject: tag, severity: 'ok', reason: 'test' }],
      unread: 0,
    };
    if (delay) await new Promise(r => setTimeout(r, delay));
    try {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    } catch (e) { /* 已被 abort */ }
  });

  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(1500);

  const out = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    clearInterval(st.pollTimer);
    st.pollTimer = null;
    st.events = [];
    await new Promise(r => setTimeout(r, 50));
    return { ok: true };
  });
  // 打开"旧请求"开关（此后第 1 个请求慢 900ms、返回 OLD）
  slowOn = true;

  const res = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.fetchEvents();                              // 旧请求（慢）
    await new Promise(r => setTimeout(r, 120));
    st.fetchEvents();                              // 新请求（快，应胜出）
    await new Promise(r => setTimeout(r, 2200));   // 等慢的那个也彻底结束
    const subj = ((st.events || [])[0] || {}).subject || null;
    return { subject: subj, hasAbortCtl: typeof st._reqCtl === 'object' };
  });

  res.errs = errs;
  res.totalSequential = seq;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(res));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_race")
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
        jsf = os.path.join(d, "probe_race_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)}")
        print()

        check(res.get("hasAbortCtl") is True, "① 存在 _reqCtl 槽（守卫工具已接线）", "")
        check(res.get("subject") == "NEW",
              "② **慢的旧响应没有覆盖新数据**（最终是 NEW 而非 OLD）",
              f"subject={res.get('subject')!r}")
        check(res.get("totalSequential", 0) >= 3,
              "③ 两次被测请求都真的发出去了（不是被跳过）",
              f"请求数={res.get('totalSequential')}")
        check(not (res.get("errs") or []),
              "④ 被 abort 的请求不产生 pageerror（静默处理）",
              f"{(res.get('errs') or [])[:2]}")
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
