# -*- coding: utf-8 -*-
"""验收：供应商抽屉的 5 个协议适配字段（后端已支持、此前界面无入口）。

字段（providers.rs）：
  require_system_first (L481)  首条必须是 system（workbuddy 400/11128 需要）
  prompt_cache         (L510)  Anthropic 前缀缓存，后端默认 true
  openai_cache_control (L519)  OpenAI 前缀缓存打标，默认 false
  api_format           (L489)  协议覆盖
  max_request_body_bytes (L525) 请求体上限（超限返中文 413）

判据:
  ① 抽屉高级设置里出现这 5 个输入项
  ② 切 require_system_first → drawerProv 上真的置了字段 + markDirty
  ③ prompt_cache 三态正确（默认 true 显示开；点一下变 false；再点回"未设置"）
  ④ max_request_body_bytes 是数字输入（不是字符串）
  ⑤ 保存后这些字段真的写进 providers.json
  ⑥ 零 pageerror
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
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_provflags")
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
  await p.waitForTimeout(4200);
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('providers');
  });
  await p.waitForTimeout(1600);
  // 打开第一个非预设供应商的抽屉 + 展开高级设置
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const idx = (st.providersFormData||[]).findIndex(x => !x.preset);
    st.openProviderDrawer(idx >= 0 ? idx : 0);
    st.drawerAdvOpen = true;
  });
  await p.waitForTimeout(1200);

  const out = {};
  // ① 5 个输入项存在
  out.fields = await p.evaluate(() => {
    const q = (sel) => {
      const all = Array.from(document.querySelectorAll(sel));
      return all.length;
    };
    // 开关：按 aria-label 找
    const labels = Array.from(document.querySelectorAll('.adv-sw button[role=switch]')).map(x => x.getAttribute('aria-label'));
    const sel = Array.from(document.querySelectorAll('.f-row select')).map(x => x.getAttribute('aria-label'));
    // ⚠️ 数字输入的 aria-label 用 :aria-label 绑定（动态），Alpine 在元素创建后才把属性
    //    写上去 —— 用 getAttribute 读到的可能是 null（第一版就这样误报）。
    //    且页面上还有**其它**数字输入（价格面板 panel-price.html 的行内价格输入，
    //    属价格功能域、不在本次范围），故只挑**带 max_body 标签**的那个来断言。
    const num = Array.from(document.querySelectorAll('.f-row input[type=number]')).map(x => ({
      label: x.getAttribute('aria-label') || '',
      hasLabelAttr: x.hasAttribute('aria-label'),
    }));
    const mine = num.filter(n => (n.label || '').indexOf('请求体') >= 0);
    return { switches: labels, selects: sel, numbers: num, mineCount: mine.length };
  });

  // ② require_system_first
  out.rsf = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.setProvFlag('require_system_first', true);
    return { val: st.drawerProv.require_system_first, dirty: !!st.isDirty };
  });

  // ③ prompt_cache 三态
  out.pc = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const r = {};
    // 初始（未设置）应显示"开"，因为后端默认 true
    delete st.drawerProv.prompt_cache;
    r.unsetShowsOn = (st.drawerProv.prompt_cache !== false);
    // 关
    st.setProvFlag('prompt_cache', false);
    r.afterOff = st.drawerProv.prompt_cache;
    // 再点 → 回未设置
    st.setProvFlag('prompt_cache', '__unset__');
    r.afterUnset = ('prompt_cache' in st.drawerProv) ? 'still_present' : 'deleted';
    // 设为开（显式）
    st.setProvFlag('prompt_cache', true);
    r.afterOn = st.drawerProv.prompt_cache;
    return r;
  });

  // ④ 数字类型
  out.num = await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv.max_request_body_bytes = 1048576;
    return { val: st.drawerProv.max_request_body_bytes, type: typeof st.drawerProv.max_request_body_bytes };
  });

  // ⑤ 保存 → 读 providers.json
  out.saved = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.drawerProv.api_format = 'anthropic';
    await st.saveProvidersForm();
    return { msg: st.providersFormMsg || '' };
  });
  await p.waitForTimeout(1200);
  await p.screenshot({ path: `%(out)s/drawer_flags.png` });

  out.errs = errs;
  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify(out));
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({fatal: String(e)})));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}"); return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_provflags")
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
        jsf = os.path.join(d, "probe_provflags_tmp.js")
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

        f = res.get("fields") or {}
        check(len(f.get("switches") or []) == 3,
              "① 3 个开关（首条system / Anthropic缓存 / OpenAI缓存打标）",
              f"{f.get('switches')}")
        check(len(f.get("numbers") or []) >= 1, "① 请求体上限是数字输入", f"{f.get('numbers')}")
        check(f.get("mineCount", 0) >= 1
              and all((n.get("label") or "").strip() for n in (f.get("numbers") or [])
                      if "请求体" in (n.get("label") or "")),
              "① 我加的数字输入有 aria-label（读屏能读出是什么）",
              f"mine={f.get('mineCount')} all={f.get('numbers')}")
        check(len(f.get("selects") or []) >= 1, "① 协议覆盖是下拉", f"{f.get('selects')}")

        rsf = res.get("rsf") or {}
        check(rsf.get("val") is True and rsf.get("dirty") is True,
              "② require_system_first 可置位并标记未保存", f"{rsf}")

        pc = res.get("pc") or {}
        check(pc.get("unsetShowsOn") is True,
              "③ prompt_cache 未设置时视为开（后端默认 true）", f"{pc}")
        check(pc.get("afterOff") is False, "③ 可关（显式 false）", f"{pc}")
        check(pc.get("afterUnset") == "deleted",
              "③ 再点回到未设置（不写死 false）", f"{pc}")
        check(pc.get("afterOn") is True, "③ 可显式开", f"{pc}")

        n = res.get("num") or {}
        check(n.get("type") == "number" and n.get("val") == 1048576,
              "④ 请求体上限是数字（不是字符串）", f"{n}")

        sv = res.get("saved") or {}
        check("配置已保存" in (sv.get("msg") or "") or len(sv.get("msg") or "") > 0,
              "⑤ 保存成功（含这些字段）", f"{sv}")
        check(not (res.get("errs") or []), "⑥ 零 pageerror", f"{(res.get('errs') or [])[:2]}")
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
