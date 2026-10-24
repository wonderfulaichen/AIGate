# -*- coding: utf-8 -*-
"""验收：概览页的接入面板（按 WaLiAPI 的 UsagePage 重做）.

改造前只有一个「Base URL + 复制」+「一整段代码」。WaLiAPI 是字段级可复制：
协议三选 → Base URL / 端点 / Key / Model 各自带复制。

判据:
  ① 有协议三选（Chat / Responses / Anthropic）
  ② 切协议 → Base URL 与端点跟着变
  ③ Model 下拉按供应商分组（optgroup），且列出的模型来自真实配置
  ④ 每个字段（Base URL / 端点 / Model）都有自己的复制按钮
  ⑤ 没有模型时给引导（红字 + 「去配置」跳转）
  ⑥ 代码块仍随协议/模型变化
  ⑦ 零 pageerror
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_usage")
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
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0,300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(4000);

  const out = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const res = {};

    // ① 协议三选
    const segs = Array.from(document.querySelectorAll('.seg-i')).filter(x => /Chat|Responses|Anthropic/.test(x.textContent||''));
    res.protoButtons = segs.map(x => (x.textContent||'').trim());

    // 找接入面板（含 .acc-field 的 pnl）
    const field = document.querySelector('.acc-field');
    const panel = field ? field.closest('.pnl') : null;
    res.hasPanel = !!panel;
    res.fieldCount = panel ? panel.querySelectorAll('.acc-field').length : 0;

    const readVals = () => {
      const f = Array.from(document.querySelectorAll('.acc-field'));
      return f.map(x => ((x.querySelector('.acc-val')||{}).textContent||'').trim());
    };
    res.valsChat = readVals();

    // ② 切到 anthropic
    st.setFirstReqProto('anthropic');
    await new Promise(r => setTimeout(r, 400));
    res.valsAnthropic = readVals();

    // ③ Model 下拉分组
    const dl = panel ? panel.querySelector('.acc-field select') : null;
    res.hasModelSelect = !!dl;
    res.optgroups = dl ? Array.from(dl.querySelectorAll('optgroup')).map(g => g.label) : [];
    res.optionCount = dl ? dl.querySelectorAll('option').length : 0;

    // ④ 每字段独立复制按钮
    res.copyBtnCount = panel ? Array.from(panel.querySelectorAll('.acc-field button')).filter(x => /复制|Copy/.test(x.textContent||'')).length : 0;

    // ⑥ 代码块随协议变
    st.setFirstReqProto('chat');
    await new Promise(r => setTimeout(r, 300));
    const codeChat = (panel ? (panel.querySelector('.fr-code pre')||{}).textContent : '') || '';
    st.setFirstReqProto('anthropic');
    await new Promise(r => setTimeout(r, 300));
    const codeAnth = (panel ? (panel.querySelector('.fr-code pre')||{}).textContent : '') || '';
    res.codeDiffers = codeChat !== codeAnth;
    res.codeHasMessages = codeChat.indexOf('/v1/chat/completions') >= 0;
    res.codeAnthHasMessages = codeAnth.indexOf('/v1/messages') >= 0;

    st.setFirstReqProto('chat');
    return res;
  });

  out.errs = errs;
  await p.screenshot({ path: `%(out)s/usage_panel.png`, fullPage: true });
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_usage")
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
        jsf = os.path.join(d, "probe_usage_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS.replace("%(base)s", base).replace("%(out)s", OUT.replace("\\", "/")))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1200:]}"); return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}"); return 1
        print(f"  现场: {json.dumps(res, ensure_ascii=False)[:800]}")
        print()

        check(len(res.get("protoButtons") or []) == 3,
              "① 协议三选（Chat / Responses / Anthropic）",
              f"{res.get('protoButtons')}")
        check(res.get("hasPanel") is True and res.get("fieldCount", 0) >= 3,
              "② 接入面板有 ≥3 个字段块（Base URL / 端点 / Model）",
              f"fields={res.get('fieldCount')}")
        vc = res.get("valsChat") or []
        va = res.get("valsAnthropic") or []
        check(vc and va and vc[0] != va[0],
              "③ 切协议 → Base URL 跟着变（anthropic 不带 /v1）",
              f"chat={vc[:2]} anthropic={va[:2]}")
        check(res.get("hasModelSelect") is True,
              "④ Model 是下拉（可选）", f"select={res.get('hasModelSelect')}")
        check(len(res.get("optgroups") or []) > 0,
              "④ 下拉按供应商分组（optgroup）", f"groups={res.get('optgroups')}")
        check(res.get("optionCount", 0) > 1,
              "④ 下拉里有真实模型", f"options={res.get('optionCount')}")
        check(res.get("copyBtnCount", 0) >= 3,
              "⑤ 每个字段都有自己的复制按钮（字段级复制）",
              f"copyBtns={res.get('copyBtnCount')}")
        check(res.get("codeDiffers") is True,
              "⑥ 代码块随协议变化", f"differs={res.get('codeDiffers')}")
        check(res.get("codeHasMessages") is True and res.get("codeAnthHasMessages") is True,
              "⑥ chat 代码用 /v1/chat/completions、anthropic 用 /v1/messages",
              f"chat={res.get('codeHasMessages')} anth={res.get('codeAnthHasMessages')}")
        check(not (res.get("errs") or []), "⑦ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
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
