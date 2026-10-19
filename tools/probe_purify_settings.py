# -*- coding: utf-8 -*-
"""实测面板上的「thinking 提纯」开关 + 后端 API 往返 + 未生效提示的可见性。

判据:
  ① 设置页有该行（标题文案在）
  ② 默认 off（关闭态）
  ③ 后端 GET 返回 mode/recall_on/effective 三个字段（前端要靠 effective 显示"未生效原因"）
  ④ 开成 rule 后 effective=false（recall 未开 → 提纯不生效 —— 硬红线必须能在面板上看出来）
  ⑤ **rule 且未生效时，"未生效"提示可见**（红线不能只存在于后端；用户看不到 = 没防住）
  ⑥ rule 且已生效时，显示"已生效"而不是"未生效"
  ⑦ 非法值回退 off

⚠️ ⑤⑥ 的可见性判定按**带 x-show 的那个元素**判，不是按 `.row-d` 找第一个 ——
   同一行里有两个 `.row-d`（描述 + 提示），按 class 找会命中描述 div 而误报"提示没显示"
   （我自己第一版检测就这么错的，产品其实是对的）。
"""
import io
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
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_purify_ui")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8911

fails = []
NCHK = [0]


def ck(cond, msg):
    NCHK[0] += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def wait_port(p, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", p), 1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    import shutil
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    os.makedirs(os.path.join(ISO, "config"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    with open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"providers": []}))

    # 默认配置: recall 关（生产默认）→ 提纯即使开也不该生效
    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as AH
        AH_ = AH(os.path.join(ISO, "data"))
        base = "http://127.0.0.1:%d" % PG

        def api(path, body=None):
            req = urllib.request.Request(base + path, data=(json.dumps(body).encode() if body is not None else None),
                                         headers={**AH_, "content-type": "application/json"},
                                         method=("POST" if body is not None else "GET"))
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                return 0, {"err": str(e)}

        st1, g1 = api("/admin/api/thinking-purify")
        ck(st1 == 200 and g1.get("mode") == "off", "默认 mode=off: %s" % g1)
        ck(g1.get("effective") is False, "默认 effective=False: %s" % g1)
        ck("recall_on" in g1, "GET 返回 recall_on (前端要据此显示未生效原因): %s" % g1)

        # 开成 rule —— recall 仍关 → 必须仍不生效
        st2, g2 = api("/admin/api/thinking-purify", {"mode": "rule"})
        ck(st2 == 200 and g2.get("mode") == "rule", "切到 rule: %s" % g2)
        ck(g2.get("effective") is False,
           "recall 未开 → rule 也不生效 (不可逆红线): %s" % g2)

        # 非法值必须回退 off，而不是半生效
        st3, g3 = api("/admin/api/thinking-purify", {"mode": "bogus"})
        ck(g3.get("mode") == "off", "非法值回退 off: %s" % g3)

        # ── 浏览器侧: 未生效提示的可见性（红线必须看得见）──
        # 用 node 脚本直接判, 不 import python 侧 playwright (Python 包名与 node 包不同名, 容易误判"没有")。
        d = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            js = PURIFY_UI_JS % {
                "page": json.dumps(os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin_settings.html").replace("\\", "/")),
                "static": json.dumps({n: io.open(os.path.join(ROOT, "src", "admin_static", n), encoding="utf-8").read()
                                      for n in ("alpine.min.js", "tailwind.js")}),
            }
            tmp = os.path.join(d, "_probe_purify_ui.js")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(js)
            env = dict(os.environ, NODE_PATH=os.path.join(d, "node_modules"), PYTHONIOENCODING="utf-8")
            rr = subprocess.run(["node", tmp], cwd=d, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace", env=env)
            try:
                ui = json.loads(rr.stdout.strip())
            except Exception:
                ui = {"errs": ["probe output not json: " + rr.stdout[:120]]}
            ck(not ui.get("errs"), "设置页无 pageerror: %s" % ui.get("errs"))
            ck(ui.get("found"), "面板含「thinking 提纯」行")
            ck(ui.get("warnVisible") is True, "rule+未生效 → 「未生效」提示可见: %s" % ui.get("warn"))
            ck(ui.get("okVisible") is False, "rule+未生效 → 不显示「已生效」")
        else:
            print("（跳过浏览器判定: 无 node playwright）")

        print("\n%s (%d 项判据, %d 失败)" % ("ALL PASS" if not fails else "FAILED", NCHK[0], len(fails)))
        return 1 if fails else 0
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        if not os.environ.get("KEEP_ISO"):
            shutil.rmtree(ISO, ignore_errors=True)


PURIFY_UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const STATIC = %(static)s;
  const p = await b.newPage({ viewport: { width: 1560, height: 1200 } });
  const errs = []; p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.route('**/admin/static/*', r => { const n = r.request().url().split('/').pop();
    if (STATIC[n]) return r.fulfill({ status: 200, contentType: 'application/javascript', body: STATIC[n] });
    return r.fulfill({ status: 404, body: '' }); });
  await p.goto('file:///' + %(page)s, { waitUntil: 'load' });
  await p.waitForTimeout(2500);
  // 切到设置页, 再把提纯置成 "开了但 recall 没开" 这一组合 —— 即红线场景
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.activeTab = 'settings'; st.thinkingPurify = 'rule'; st.purifyRecallOn = false; st.purifyEffective = false;
  });
  await p.waitForTimeout(900);
  const out = await p.evaluate(() => {
    const row = Array.from(document.querySelectorAll('.set-row')).find(r => (r.innerText || '').indexOf('提纯') >= 0);
    const vis = e => { let x = e; while (x) { if (getComputedStyle(x).display === 'none') return false; x = x.parentElement; } return getComputedStyle(e).display !== 'none'; };
    // ⚠️ 必须按 x-show 属性定位, 不按 .row-d class —— 同一行有两个 .row-d (描述 + 提示)
    const warn = row ? Array.from(row.querySelectorAll('[x-show]')).find(e => /未生效/.test(e.innerText || '')) : null;
    const ok = row ? Array.from(row.querySelectorAll('[x-show]')).find(e => /已生效/.test(e.innerText || '')) : null;
    return {
      found: !!row,
      warnVisible: warn ? vis(warn) : null,
      warn: warn ? (warn.innerText || '').slice(0, 80) : null,
      okVisible: ok ? vis(ok) : null,
    };
  });
  console.log(JSON.stringify({ errs, ...out }));
  await b.close();
})();
"""


if __name__ == "__main__":
    sys.exit(main())
