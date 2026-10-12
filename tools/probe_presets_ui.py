# -*- coding: utf-8 -*-
"""实机核对「内置预设 + 登录」前端链路 (隔离实例跑新构建份, 不碰部署).

核对点 (改前端必须截图/实测核对 —— 生成成功 ≠ 渲染正确):
  ① 供应商页「新增」弹窗: 预设卡片渲染 (CodeBuddy/说明/添加按钮), 点添加 → 变「已添加」
  ② 卡片视图 buddyx: 「内置」徽章 + 「登录」按钮 + 删除 ✕ disabled + 「停用」按钮
  ③ 点「登录」→ 登录弹窗 (标题/三个字段) → 填 token 提交 → 按钮变「重新登录」(状态刷新)
  ④ 页面无 JS pageerror
用法: python tools/probe_presets_ui.py
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_presets_ui")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8999, 9000

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


# ── 隔离实例 ──
if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
PROV = {"providers": [
    {"name": "buddyx", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "preset": "buddy",
     "auth": {"mode": "account", "refresh": {"endpoint": "http://127.0.0.1:%d/refresh" % PM,
                                              "method": "POST",
                                              "headers": {"X-Refresh-Token": "{refresh_token}"}}},
     "models": {"buddyx/m": {"upstream_model": "m"}}},
    {"name": "plain2", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "models": {"plain2/m": {"upstream_model": "m"}}},
]}
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps(PROV, indent=2))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1700, height: 1100 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto('http://127.0.0.1:%(pg)d/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2000);

  const clickText = (txt, sel) => p.evaluate(({ txt, sel }) => {
    const els = Array.from(document.querySelectorAll(sel || 'button,a,div,span'));
    const t = els.find(e => e.children.length === 0 && (e.textContent || '').trim() === txt);
    if (t) { t.click(); return true; }
    return false;
  }, { txt, sel });

  // 切「供应商」页签
  await clickText('供应商');
  await p.waitForTimeout(1500);

  // ① 打开「新增」弹窗
  await clickText('新增');
  await p.waitForTimeout(600);
  const addModal = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return { open: false };
    const txt = (m.innerText || '');
    const btns = Array.from(m.querySelectorAll('button'));
    const addBtn = btns.find(x => ['添加', '已添加'].includes((x.textContent || '').trim()));
    return {
      open: true,
      has_preset_title: txt.includes('内置预设'),
      has_codebuddy: txt.includes('CodeBuddy'),
      has_desc: txt.includes('登录一次'),
      has_custom_title: txt.includes('自定义'),
      add_btn_exists: !!addBtn,
      add_btn_text: addBtn ? (addBtn.textContent || '').trim() : '',
      add_btn_disabled: addBtn ? addBtn.disabled : null,
    };
  });
  // 点预设的「添加」
  const clickedAdd = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none');
    if (!m) return false;
    const addBtn = Array.from(m.querySelectorAll('button'))
      .find(x => (x.textContent || '').trim() === '添加' && !x.disabled);
    if (addBtn) { addBtn.click(); return true; }
    return false;
  });
  await p.waitForTimeout(400);
  // 弹窗应自动关闭 (addFromPreset 设 showAddProvider=false)
  const modalClosed = await p.evaluate(() =>
    Array.from(document.querySelectorAll('.modal-overlay-add'))
      .every(x => getComputedStyle(x).display === 'none' || x.style.display === 'none'
                  || !x.offsetParent));

  // ② 卡片视图: 新添加的模板条目 (preset=buddy, name=buddy) 徽章/按钮
  //    注意隔离配置里预设实例叫 buddyx; 新添加的名字是 buddy —— 两者都查
  const card = await p.evaluate(() => {
    const txt = document.body.innerText;
    const cards = Array.from(document.querySelectorAll('.plaza-card'));
    const c = cards.find(x => (x.innerText || '').includes('buddy') && (x.innerText || '').includes('内置'));
    const btns = c ? Array.from(c.querySelectorAll('button')) : [];
    const del = btns.find(x => (x.textContent || '').trim() === '✕');
    return {
      found: !!c,
      has_badge_builtin: txt.includes('内置'),
      has_login_btn: btns.some(x => ['登录', '重新登录'].includes((x.textContent || '').trim())),
      has_disable_btn: btns.some(x => (x.textContent || '').trim() === '停用'),
      del_disabled: del ? del.disabled : null,
      del_title: del ? (del.title || '') : '',
    };
  });

  // ③ 打开登录弹窗 → 填 token → 提交 → 按钮变「重新登录」
  const loginOpened = await p.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('.plaza-card'));
    const c = cards.find(x => (x.innerText || '').includes('buddy') && (x.innerText || '').includes('内置'));
    if (!c) return false;
    const btn = Array.from(c.querySelectorAll('button'))
      .find(x => (x.textContent || '').trim() === '登录');
    if (btn) { btn.click(); return true; }
    return false;
  });
  await p.waitForTimeout(500);
  const loginModal = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (!m) return { open: false };
    // 一键登录按钮必须渲染 (buddy 是账号型); 静态 Key 提示不得出现.
    // 另查"游离文本": 早前模板破损时 span 落在 template 外, 裸渲染"浏览器登录"文字却没有按钮.
    const browserBtn = Array.from(m.querySelectorAll('button'))
      .find(x => (x.textContent || '').includes('浏览器登录'));
    const bodyTxt = (m.querySelector('.modal-body') || {}).innerText || '';
    const strayText = bodyTxt.includes('浏览器登录') && !browserBtn;
    return {
      open: true,
      has_tok: !!m.querySelector('textarea'),
      field_count: m.querySelectorAll('textarea,input').length,
      has_submit: Array.from(m.querySelectorAll('button')).some(x => (x.textContent || '').trim() === '登录'),
      has_browser_btn: !!browserBtn,
      static_hint: bodyTxt.includes('静态 API Key'),
      stray_text: strayText,
    };
  });
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (m) {
      const ta = m.querySelectorAll('textarea')[0];
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      // JWT 形状 (载荷带 sub) —— 面板据此显示账号身份指纹
      // btoa 只吃 Latin1 → 先 UTF-8 编码 (昵称可能含中文)
      const b64 = o => btoa(String.fromCharCode.apply(null, new TextEncoder().encode(JSON.stringify(o))))
        .replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_');
      setter.call(ta, b64({alg:'none'}) + '.' + b64({sub:'aaaa1111-1111', nickname:'甲甲甲'}) + '.sig');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  await p.waitForTimeout(200);
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (m) {
      const btn = Array.from(m.querySelectorAll('button'))
        .find(x => (x.textContent || '').trim() === '登录' && !x.disabled);
      if (btn) btn.click();
    }
  });
  await p.waitForTimeout(800);
  const afterLogin = await p.evaluate(() => {
    const txt = document.body.innerText;
    return { has_relogin: txt.includes('重新登录') };
  });

  // 多账号 UI: 重新打开登录弹窗 → 应列出已登录账号 (使用中) 且主按钮变「再添加一个账号」
  await p.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find(x => (x.textContent || '').trim() === '重新登录');
    if (btn) btn.click();
  });
  await p.waitForTimeout(600);
  const acctUI = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (!m) return { open: false };
    const txt = m.innerText || '';
    const rows = Array.from(m.querySelectorAll('.acct-row')).map(r => {
      const t = r.innerText || '';
      return { identity: (t.match(/(甲甲甲|乙乙乙)/) || [])[0] || '',
               active: t.includes('使用中'), has_promote: t.includes('设为首选') };
    });
    return {
      open: true,
      rows_info: rows,
      has_list: txt.includes('已登录账号'),
      has_active_tag: txt.includes('使用中'),
      has_add_more: txt.includes('再添加一个账号'),
      has_identity: (m.innerText||'').includes('甲甲甲') && ((m.querySelector('[title*=aaaa1111]')||{}).title||'').includes('aaaa1111'),
      rows: Array.from(m.querySelectorAll('button'))
        // 「登出」按钮实际文案是「登出此账号」(i18n logout_this) —— 用 includes, 别用全等
        .filter(x => (x.textContent || '').includes('登出')).length,
    };
  });

  // ── 第二个账号 (不同 sub) + 切换当前账号 ──
  const fillAndSubmit = async (sub) => {
    await p.evaluate((sub) => {
      const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
        .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
      if (!m) return;
      const ta = m.querySelectorAll('textarea')[0];
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      // btoa 只吃 Latin1 → 先 UTF-8 编码 (昵称可能含中文)
      const b64 = o => btoa(String.fromCharCode.apply(null, new TextEncoder().encode(JSON.stringify(o))))
        .replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_');
      setter.call(ta, b64({alg:'none'}) + '.' + b64({sub, nickname: sub==='aaaa1111-1111' ? '甲甲甲' : '乙乙乙'}) + '.sig');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }, sub);
    await p.waitForTimeout(200);
    await p.evaluate(() => {
      const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
        .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
      if (!m) return;
      const btn = Array.from(m.querySelectorAll('button'))
        .find(x => (x.textContent || '').trim() === '登录' && !x.disabled);
      if (btn) btn.click();
    });
    await p.waitForTimeout(700);
  };
  await fillAndSubmit('bbbb2222-2222');
  // 提交成功后弹窗会关闭 → 重新打开 (卡片上的「重新登录」) 才能看到列表并切换
  await p.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find(x => (x.textContent || '').trim() === '重新登录');
    if (btn) btn.click();
  });
  await p.waitForTimeout(600);
  const afterSecond = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (!m) return { open: false };
    return { open: true, rows: Array.from(m.querySelectorAll('.acct-row')).map(r => r.innerText || '') };
  });
  // 点第二行的「设为首选」(把 #2 切成当前) —— 按钮文案 10-08 轮从「设为当前账号」改为「设为首选」
  await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (!m) return;
    const rows = Array.from(m.querySelectorAll('.acct-row'));
    const r = rows[1];
    const btn = r && Array.from(r.querySelectorAll('button')).find(b => (b.textContent || '').includes('设为首选'));
    if (btn) btn.click();
  });
  await p.waitForTimeout(700);
  const afterSwitch = await p.evaluate(() => {
    const m = Array.from(document.querySelectorAll('.modal-overlay-add'))
      .find(x => getComputedStyle(x).display !== 'none' && (x.innerText || '').includes('账号登录'));
    if (!m) return { open: false };
    return { open: true, rows: Array.from(m.querySelectorAll('.acct-row')).map(r => {
      const t = r.innerText || '';
      return { identity: (t.match(/(甲甲甲|乙乙乙)/) || [])[0] || '', active: t.includes('使用中') };
    }) };
  });

  console.log(JSON.stringify({ addModal, clickedAdd, modalClosed, card, loginOpened, loginModal, afterLogin, acctUI, afterSecond, afterSwitch, errs }, null, 2));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
""" % {"pg": PG}

d = os.path.join(ROOT, ".openbitfun", "tmp", "vidtools")
tmp = os.path.join(d, "_probe_presets.js")
with io.open(tmp, "w", encoding="utf-8") as f:
    f.write(JS)
env2 = dict(os.environ)
env2["NODE_PATH"] = os.path.join(d, "node_modules")

try:
    # 等实例就绪
    for _ in range(60):
        try:
            # 就绪探测用**免鉴权**的 /health (它只回答是否在线); /admin/api/* 现在要 Bearer
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    r = subprocess.run(["node", tmp], cwd=d, capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace", env=env2)
    print(r.stdout)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-1500:])

    try:
        out = json.loads(r.stdout)
    except Exception:
        out = {}

    am = out.get("addModal", {})
    ck("ui_addmodal_open", am.get("open") is True, am)
    ck("ui_preset_section", am.get("has_preset_title") and am.get("has_codebuddy") and am.get("has_desc"), am)
    ck("ui_custom_section", am.get("has_custom_title") is True, am)
    ck("ui_preset_add_btn", am.get("add_btn_exists") and am.get("add_btn_text") in ("添加", "已添加"), am)
    # 两种预期态: 预设未添加 → 按钮可点且点击生效; 已添加 (本探针环境 buddyx 即预设实例)
    #   → 按钮渲染为「已添加」禁用态 (presetAdded 去重正确).
    if am.get("add_btn_text") == "添加":
        ck("ui_preset_add_click", out.get("clickedAdd") is True, "")
    else:
        ck("ui_preset_add_disabled_state", am.get("add_btn_disabled") is True, am)
    cd = out.get("card", {})
    ck("ui_card_badges", cd.get("found") and cd.get("has_badge_builtin"), cd)
    ck("ui_card_login_btn", cd.get("has_login_btn"), cd)
    ck("ui_card_disable_btn", cd.get("has_disable_btn"), cd)
    ck("ui_del_disabled", cd.get("del_disabled") is True and "停用" in (cd.get("del_title") or ""), cd)
    ck("ui_login_modal", out.get("loginOpened") and out.get("loginModal", {}).get("open"), out.get("loginModal"))
    ck("ui_login_browser_btn", out.get("loginModal", {}).get("has_browser_btn") is True, out.get("loginModal"))
    ck("ui_login_no_static_hint", out.get("loginModal", {}).get("static_hint") is False, out.get("loginModal"))
    ck("ui_login_no_stray_text", out.get("loginModal", {}).get("stray_text") is False, out.get("loginModal"))
    ck("ui_login_fields", out.get("loginModal", {}).get("field_count", 0) >= 3, out.get("loginModal"))
    ck("ui_login_submit_relogin", out.get("afterLogin", {}).get("has_relogin"), out.get("afterLogin"))
    au = out.get("acctUI", {})
    ck("ui_acct_identity_shown", au.get("has_identity") is True, au)
    ck("ui_acct_list_rendered", au.get("open") and au.get("has_list"), au)
    ck("ui_acct_active_tag", au.get("has_active_tag") is True, au)
    ck("ui_acct_add_more_btn", au.get("has_add_more") is True, au)
    ck("ui_acct_row_logout_btn", (au.get("rows") or 0) >= 1, au)
    ck("ui_acct_second_login_added", len(out.get("afterSecond", {}).get("rows") or []) == 2,
       out.get("afterSecond"))
    sw = out.get("afterSwitch", {})
    rows = sw.get("rows") or []
    ck("ui_acct_switch_btn_works", len(rows) == 2 and rows[0].get("identity") == "乙乙乙"
       and rows[0].get("active") is True and rows[1].get("active") is False, rows)
    ck("ui_no_pageerror", not out.get("errs"), out.get("errs"))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
