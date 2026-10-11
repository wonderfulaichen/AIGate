# -*- coding: utf-8 -*-
"""探针: 供应商表单的提示是否**真的显示出来**（2026-10-12 用户报「设计不人性化」）。

审计发现的决定性事实（本探针钉住它）:
    `providersFormMsg` / `providersFormOk` / `keyMsg` 被写入 20+ 次, 但**任何 HTML 里都没有
    渲染点**（grep 全部 .html 零命中）—— 写好的文案全部"写了就丢"。后果:
      · 点「添加」后屏幕上只多一张卡, **没有任何文字说"还没保存"**;
      · **保存失败完全静默** —— 用户分不清成功和失败;
      · 清空密钥失败 / 登出失败同样静默。

判据（无头浏览器 + 真实隔离实例, **走真实点击**, 只看界面 DOM）:
  ① 点「添加」预设后: 出现可见 toast, 且含「保存」字样（告知还需保存）
  ② 保存成功: 出现可见 toast
  ③ **保存失败**: 出现**错误样式**的可见 toast（不再静默）← 最关键
  ④ 清空密钥: 成功/失败都有可见 toast
  ⑤ 抽屉里两个「保存」文字不同（保存密钥 vs 保存配置）—— 语义可分辨

用法: python tools/probe_form_feedback.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_formfb")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8979

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": []}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

JS = r"""
const path = require('path');
const PW = path.join(__ROOT__, '.openbitfun','tmp','vidtools','node_modules','playwright');
const { chromium } = require(PW);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 取组件对象: 必须用 Alpine 的**公开 API** `Alpine.$data(el)`。
// ⚠️ 不能读 `el._x_dataStack[0]` —— 那是内部结构, 且拿到的代理上方法上下文不全
//    （实测直接调会报 `t is not a function`）。根元素是 `.layout`（body-shell.html:3）。
const DATA = `(() => {
  const el = document.querySelector('.layout');
  return (window.Alpine && el) ? Alpine.$data(el) : null;
})()`;

// 点一个「文本完全匹配」且可见的按钮
async function clickText(p, txt) {
  return await p.evaluate((t) => {
    const els = Array.from(document.querySelectorAll('button'));
    const b = els.find(x => (x.textContent||'').trim() === t && x.offsetParent !== null && !x.disabled);
    if (b) { b.click(); return true; }
    return false;
  }, txt);
}
async function toasts(p) {
  return await p.evaluate(() => Array.from(document.querySelectorAll('.toast-it'))
    .map(x => ({ text: (x.innerText||'').trim(), cls: x.className })));
}
async function clearToasts(p) {
  await p.evaluate(() => { if (window.Alpine && Alpine.store('toast')) Alpine.store('toast').items = []; });
}
async function gotoSettings(p) {
  await p.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.seg-i,.nav-i,button,a'))
      .find(x => (x.textContent||'').trim() === '供应商');
    if (el) el.click();
  });
  await sleep(1500);
}

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  await p.goto('http://127.0.0.1:__PORT__/admin', { waitUntil: 'domcontentloaded' });
  await sleep(2500);
  await gotoSettings(p);
  const out = {};

  // ① 打开「新增」→ 点第一个预设的「新增/添加」
  await clearToasts(p);
  await clickText(p, '新增');
  await sleep(900);
  const presetClicked = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return false;
    const btn = Array.from(m.querySelectorAll('button'))
      .find(x => ['新增','添加'].includes((x.textContent||'').trim()) && !x.disabled);
    if (btn) { btn.click(); return true; }
    return false;
  });
  await sleep(1000);
  out.preset_clicked = presetClicked;
  out.after_add = await toasts(p);

  // 关掉弹窗
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (m) { const c = Array.from(m.querySelectorAll('button')).find(x => (x.textContent||'').trim()==='取消'); if (c) c.click(); }
  });
  await sleep(600);

  // ② 保存成功
  await clearToasts(p);
  await clickText(p, '保存配置');
  await sleep(1500);
  out.after_save_ok = await toasts(p);

  // ③ 保存失败: 先造一个重复名（后端会拒绝）
  await clearToasts(p);
  await p.evaluate(() => {
    const st = Alpine.$data(document.querySelector('.layout'));
    const arr = st.providersFormData || [];
    if (arr.length >= 2) { arr[1].name = arr[0].name; st.markDirty(); }
    else if (arr.length === 1) { arr[0].name = ''; st.markDirty(); }  // 空名 → 前端预校验拦下
  });
  await sleep(300);
  await clickText(p, '保存配置');
  await sleep(1500);
  out.after_save_fail = await toasts(p);

  // ④ 清空密钥（成功路径）
  await clearToasts(p);
  const cleared = await p.evaluate(async () => {
    const st = Alpine.$data(document.querySelector('.layout'));
    if (!st.providersFormData || !st.providersFormData.length) return false;
    st.clearProviderKey(0);
    return true;
  });
  await sleep(1200);
  out.after_clear_key = await toasts(p);

  // ⑤ 抽屉里两个「保存」文字不同
  await p.evaluate(() => {
    const st = Alpine.$data(document.querySelector('.layout'));
    if (st.providersFormData && st.providersFormData.length) st.openProviderDrawer(0);
  });
  await sleep(1500);
  out.drawer_buttons = await p.evaluate(() => {
    const d = document.querySelector('.dr-foot');
    const foot = d ? Array.from(d.querySelectorAll('button')).map(x => (x.textContent||'').trim()) : [];
    const keyBtn = Array.from(document.querySelectorAll('button.btn-p'))
      .filter(x => x.offsetParent !== null)
      .map(x => (x.textContent||'').trim());
    return { foot, primary_visible: keyBtn };
  });

  out.errs = errs;
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
"""


def run_js():
    js = os.path.join(ISO, "t.js")
    code = JS.replace("__ROOT__", repr(ROOT)).replace("__PORT__", str(PG))
    io.open(js, "w", encoding="utf-8").write(code)
    r = subprocess.run(["node", js], capture_output=True, text=True, timeout=300, encoding="utf-8")
    if r.returncode != 0:
        print("node stderr:", (r.stderr or "")[-800:])
    try:
        return json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        print(r.stdout[-1500:])
        return {}


try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("隔离实例未就绪")
        sys.exit(1)

    out = run_js()

    def txt(key):
        return " | ".join(x.get("text", "") for x in (out.get(key) or []))

    ck("① 点「添加」预设后出现可见提示", bool(out.get("preset_clicked")) and bool(txt("after_add")),
       "clicked=%s toast=%s" % (out.get("preset_clicked"), txt("after_add")[:140] or "(无)"))
    ck("① 该提示说明了还需保存（含「保存」字样）", "保存" in txt("after_add"), txt("after_add")[:140])

    ck("② 保存成功后有可见提示", bool(txt("after_save_ok")), txt("after_save_ok")[:140] or "(无)")

    t_fail = out.get("after_save_fail") or []
    ck("③ ★保存失败**有可见提示**（不再静默）", bool(t_fail), txt("after_save_fail")[:180] or "(无 ← 静默)")
    ck("③ 且是**错误样式**（用户能分辨成功/失败）",
       any("toast-error" in (x.get("cls") or "") for x in t_fail), str(t_fail)[:180])

    ck("④ 清空密钥后有可见提示（不静默）", bool(txt("after_clear_key")), txt("after_clear_key")[:140] or "(无)")

    db = out.get("drawer_buttons") or {}
    foot = db.get("foot") or []
    ck("⑤ 抽屉底部按钮是「关闭」而非「取消」（不再让用户以为改动静默丢弃）",
       any("关闭" in x for x in foot), "foot=%s" % foot)
    ck("⑤ 抽屉里「保存密钥」与底部「保存配置」文字可分辨",
       any("保存密钥" in x for x in (db.get("primary_visible") or [])),
       "primary=%s" % (db.get("primary_visible") or []))

    ck("无 pageerror", not out.get("errs"), str(out.get("errs"))[:200])

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 表单提示真的显示出来了（含保存失败的错误提示）, 且两种「保存」语义可分辨")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
