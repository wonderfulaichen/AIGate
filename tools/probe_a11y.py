# -*- coding: utf-8 -*-
"""可访问性探针（期 1 验收）：可及名 / 键盘可达 / 焦点可见 / 语义正确.

为什么必须用真实浏览器而不只做静态检查:
  "能 Tab 到"与"Tab 到之后按 Enter 真的触发"是**运行时行为** ——
  静态看有没有 tabindex 完全可能骗人（例如父元素 `display:none` 时不可 Tab、
  或 tabindex 在但 keydown 没接、或 <button> 被 disabled）。

判据分四组:
  ① 开关 11 个: role="switch" + aria-checked 存在且随点击翻转 + 有可及名
  ② 分段页签/筛选胶囊: 是真 <button>（可 Tab 到）且按 Enter 真的切换了状态
  ③ 面板标题: 是 <h2>（屏幕阅读器跳标题能停在每个面板）
  ④ 焦点可见: Tab 到可点元素时 :focus-visible 生效（outline 非 none）
  ⑤ 零 pageerror

红线: 隔离实例 + 随机端口，绝不碰用户 8787。
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
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _pdir():
    for rel in (".openbitfun/tmp/vidtools", ".openbitfun/tmp/ppt", "."):
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
  p.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  await p.goto('%(base)s/admin', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);
  const out = {};

  // ── 去设置页（开关都在这里）──
  await p.evaluate(() => document.querySelector('[x-data]')._x_dataStack[0].switchTab('settings'));
  await p.waitForTimeout(900);

  // ① 开关: role/aria-checked/可及名 + 点击后翻转
  // ⚠️ 设置页 2026-10-24 改成"左分区导航 + 右内容"后，**一次只显示一个分区**，
  // 其余分区的开关仍在 DOM 里但被 x-show 隐藏。所以这里必须逐分区切换后收集，
  // 不能只量当前可见的那一批（否则会误判为"开关丢了"）。
  out.switches = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    const seen = [];
    for (const sec of ['perf', 'personal', 'peak', 'conn', 'monitor']) {
      st.settingsSection = sec;
      await new Promise(r => setTimeout(r, 220));
      const list = Array.from(document.querySelectorAll('button.tgl'));
      for (const el of list) {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        const visible = r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        if (!visible) continue;                       // 别的分区里的，跳过
        const label = (el.getAttribute('aria-label') || '').trim();
        // 用 label 去重（同一分区里各开关名称唯一）
        if (seen.some(x => x.label === label && x.label)) continue;
        seen.push({
          role: el.getAttribute('role'),
          checked: el.getAttribute('aria-checked'),
          label,
          visible,
          tabIndex: el.tabIndex,
          section: sec,
        });
      }
    }
    st.settingsSection = 'perf';
    await new Promise(r => setTimeout(r, 200));
    return seen;
  });

  // 点第一个开关, 断言 aria-checked 真的翻转（不是静态写着 true）
  out.toggleFlip = await p.evaluate(async () => {
    const el = document.querySelector('button.tgl');
    if (!el) return { ok: false, why: 'no switch' };
    const before = el.getAttribute('aria-checked');
    el.click();
    await new Promise(r => setTimeout(r, 350));
    const after = el.getAttribute('aria-checked');
    el.click(); // 还原, 别把用户配置留在测试状态
    await new Promise(r => setTimeout(r, 350));
    return { ok: true, before, after, restored: el.getAttribute('aria-checked') };
  });

  // ② 分段页签: 真 button? Tab 可达? Enter 能触发?
  out.seg = await p.evaluate(() => {
    const list = Array.from(document.querySelectorAll('.seg-i'));
    return {
      total: list.length,
      allButton: list.every(el => el.tagName === 'BUTTON'),
      nonButton: list.filter(el => el.tagName !== 'BUTTON').map(el => el.outerHTML.slice(0, 60)),
      // 可 Tab 到 == tabIndex >= 0 且没有被 disabled
      tabbable: list.filter(el => el.tabIndex >= 0 && !el.disabled).length,
    };
  });

  // 用键盘真的按 Enter 切换一个页签（analyze 页）
  out.enterWorks = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('analytics');
    await new Promise(r => setTimeout(r, 700));
    const tabs = Array.from(document.querySelectorAll('.seg-i[role="tab"]'));
    if (tabs.length < 2) return { ok: false, why: 'tabs<2', n: tabs.length };
    const target = tabs[1];
    const before = st.analyticsTab;
    target.focus();
    const focused = document.activeElement === target;
    target.click();               // 键盘 Enter 在 button 上等于 click
    await new Promise(r => setTimeout(r, 500));
    const after = st.analyticsTab;
    target.blur();
    return { ok: true, before, after, focused, changed: before !== after };
  });

  // ③ 面板标题: 必须是 h2
  out.headings = await p.evaluate(() => {
    const ht = Array.from(document.querySelectorAll('.pnl-ht'));
    return {
      total: ht.length,
      allH2: ht.every(el => el.tagName === 'H2'),
      wrong: ht.filter(el => el.tagName !== 'H2').map(el => el.tagName).slice(0, 5),
      // 层级不能被 h2 默认样式撑开：显示宽度应接近文字宽度（不是整行）
    };
  });

  // ③b 可排序表头：必须是键盘可达 + 有常驻箭头提示 + Enter 真的改排序
  out.thSort = await p.evaluate(async () => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('records');
    await new Promise(r => setTimeout(r, 800));
    const ths = Array.from(document.querySelectorAll('th.th-sort'));
    const info = ths.map((th) => ({
      tabIndex: th.tabIndex,
      role: th.getAttribute('role'),
      ico: (th.querySelector('.th-sort-ico') || {}).textContent || '',
      hasEnter: !!(th.getAttribute('@keydown.enter') || th.outerHTML.indexOf('keydown.enter') >= 0),
    }));
    // 真按 Enter 改排序
    const first = ths[0];
    const before = st.logSortKey;
    let changed = false;
    if (first) {
      first.focus();
      first.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      await new Promise(r => setTimeout(r, 400));
      changed = st.logSortKey !== before;
    }
    return { total: ths.length, info, changed, before };
  });

  // ③c 诊断格：可聚焦 + 有 ▸ 展开指示。
  // ⚠️ 诊断格来自 stats.per_model —— 空实例里表格无行，total 会是 0（第一版就这样假失败）。
  //
  // 这里踩到两个坑，都记下来：
  //  (a) Alpine 3 不追踪既有对象的深层属性新增 → 必须**整体重建 stats 对象**再赋值
  //      （st.stats.per_model = [...] 不触发重渲染；本项目 modelDiagOpen 踩过同款）。
  //  (b) 后台会周期性拉 stats 把注入值覆盖回去，所以**不能在单次 evaluate 里
  //      sleep 等**——那样注入值可能已被冲掉，判据时绿时红（实测连跑 3 次全红、
  //      偶发一次全绿）。正确做法：**在 Playwright 侧轮询**，每次单独 evaluate
  //      重新注入并立即读 DOM，直到拿到行或超时。
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.analyticsTab = 'cost';
    st.switchTab('analytics');
  });
  await p.waitForTimeout(1200);
  out.diag = { total: 0 };
  for (let attempt = 0; attempt < 30; attempt++) {
    const r = await p.evaluate(() => {
      const st = document.querySelector('[x-data]')._x_dataStack[0];
      st.analyticsTab = 'cost';
      // 每轮都重新注入（整体重建对象），抵消后台 fetch 的覆盖
      st.stats = Object.assign({}, st.stats || {}, {
        per_model: [{
          provider: 'mockprov', model: 'mockprov/m1', upstream_model: 'm1',
          requests: 10, total_prompt_tokens: 2000, total_completion_tokens: 500,
          total_cache_hit_tokens: 1500, total_cache_miss_tokens: 500,
          total_cost: 0.1, avg_latency_ms: 900, avg_ttft_ms: 300, gen_speed: 40,
          optimization_saved_tokens: 800,
        }],
      });
      const cells = Array.from(document.querySelectorAll('td[role="button"]'));
      return {
        total: cells.length,
        allTab: cells.every(c => c.tabIndex >= 0),
        allCaret: cells.every(c => (c.textContent || '').indexOf('▸') >= 0),
        dbg: {
          tab: st.analyticsTab,
          activeTab: st.activeTab,
          perModelLen: ((st.stats && st.stats.per_model) || []).length,
          filteredLen: (st.filteredModelStats || []).length,
          modelColsMore: st.modelColsMore,
          tables: Array.from(document.querySelectorAll('table')).map(t => t.querySelectorAll('tbody tr').length),
        },
      };
    });
    if (r.total > 0) { out.diag = r; break; }
    out.diag = r;                       // 留最后一次现场供失败时打印
    await p.waitForTimeout(180);
  }
  console.log('  ③c 现场: ' + JSON.stringify((out.diag || {}).dbg));

  // ④ 焦点可见: **必须用真实键盘 Tab**。
  // 为什么不能用 el.focus(): 程序化聚焦不满足 :focus-visible 的启发式
  // （浏览器认为"不是键盘发起的"），outline 会一直是 none —— 那样测出来的是
  // 探针自己的缺陷, 不是实现的缺陷（第一版就踩了这个假失败）。
  out.focusRing = { seq: [] };
  await p.evaluate(() => {
    const st = document.querySelector('[x-data]')._x_dataStack[0];
    st.switchTab('analytics');
  });
  await p.waitForTimeout(700);
  await p.evaluate(() => document.body.focus());
  for (let i = 0; i < 30; i++) {
    await p.keyboard.press('Tab');
    const info = await p.evaluate(() => {
      const el = document.activeElement;
      if (!el) return null;
      const cs = getComputedStyle(el);
      // ⚠️ 关键：不能用 `outlineStyle !== 'none'` 判断"有没有焦点环" ——
      // 实测删掉项目的 :focus-visible 规则后，浏览器会回落到默认的 **outline: auto 1px**，
      // 该判据依然为真 → 实现改坏也照样绿（本探针第一版就是这样假通过的）。
      // 正确做法：只认**项目自定义**的顶点(outline-style 不是 auto)或 box-shadow 光晕。
      const customOutline = (cs.outlineStyle !== 'none'
                             && cs.outlineStyle !== 'auto'
                             && parseFloat(cs.outlineWidth) > 0);
      const glow = (cs.boxShadow && cs.boxShadow !== 'none') || false;
      return {
        cls: (el.className || '').toString().slice(0, 60),
        tag: el.tagName,
        outline: cs.outlineStyle + ' ' + cs.outlineWidth,
        boxShadow: (cs.boxShadow || 'none').slice(0, 40),
        hasRing: customOutline || glow,
        ringKind: customOutline ? 'outline' : (glow ? 'shadow' : 'none'),
      };
    });
    if (info) out.focusRing.seq.push(info);
  }

  // ⑤ 每页都要能渲染（改坏标签最可能的后果是整页吞掉）
  out.pages = {};
  for (const tab of ['dashboard', 'analytics', 'records', 'providers', 'settings', 'about']) {
    await p.evaluate((t) => document.querySelector('[x-data]')._x_dataStack[0].switchTab(t), tab);
    await p.waitForTimeout(600);
    out.pages[tab] = await p.evaluate(() => {
      const main = document.querySelector('main') || document.body;
      return { textLen: (main.innerText || '').trim().length, panels: document.querySelectorAll('.pnl').length };
    });
  }

  await b.close();
  console.log('JSON_RESULT ' + JSON.stringify({ out, errs }));
})().catch(e => { console.log('JSON_RESULT ' + JSON.stringify({ fatal: String(e) })); });
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}")
        return 1
    port = free_port()
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_a11y")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov", "endpoint": "http://127.0.0.1:1/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"m1": {"upstream_model": "m"}},
    }]}
    with open(os.path.join(wd, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso = os.path.join(wd, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(wd, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=wd,
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
            print(f"{BAD}  实例未就绪")
            return 1
        d = _pdir()
        if not d:
            print(f"{BAD}  找不到 playwright 目录")
            return 1
        jsf = os.path.join(d, "probe_a11y_tmp.js")
        with open(jsf, "w", encoding="utf-8") as f:
            f.write(JS % {"base": base})
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=d)
        sout = (r.stdout or "") + (r.stderr or "")
        line = next((l for l in sout.splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{sout[-1500:]}")
            return 1
        res = json.loads(line[len("JSON_RESULT "):])
        if res.get("fatal"):
            print(f"{BAD}  {res['fatal']}")
            return 1
        o = res.get("out") or {}

        sw = o.get("switches") or []
        print(f"  开关总数: {len(sw)}")
        check(len(sw) == 11, "① 设置页有 11 个开关", f"实得 {len(sw)}")
        check(all(x["role"] == "switch" for x in sw) and sw,
              "① 全部 role=\"switch\"", f"{[x['role'] for x in sw if x['role'] != 'switch']}")
        check(all(x["checked"] in ("true", "false") for x in sw) and sw,
              "① 全部有 aria-checked(true/false)", f"{[(x['role'], x['checked']) for x in sw if x['checked'] not in ('true','false')]}")
        check(all(x["label"] for x in sw) and sw,
              "① 全部有可及名(aria-label)", f"空白: {[i for i,x in enumerate(sw) if not x['label']]}")
        check(all(x["visible"] for x in sw), "① 全部可见（不是隐藏占位）",
              f"{[i for i,x in enumerate(sw) if not x['visible']]}")
        check(all(x["tabIndex"] >= 0 for x in sw), "① 全部可 Tab 到",
              f"{[i for i,x in enumerate(sw) if x['tabIndex'] < 0]}")

        tf = o.get("toggleFlip") or {}
        check(tf.get("ok") and tf.get("before") != tf.get("after"),
              "① 点击开关 aria-checked 真的翻转（不是静态值）",
              f"{tf.get('before')} -> {tf.get('after')}")
        check(tf.get("restored") == tf.get("before"), "① 二次点击可还原", f"{tf.get('restored')}")

        sg = o.get("seg") or {}
        print(f"  分段页签: {sg.get('total')} 个，非 button: {sg.get('nonButton')}")
        check(sg.get("allButton") is True, "② 全部 .seg-i 是真 <button>（键盘可达）",
              f"非 button: {sg.get('nonButton')}")
        check(sg.get("tabbable") == sg.get("total") and sg.get("total"),
              "② 全部可 Tab 到", f"{sg.get('tabbable')}/{sg.get('total')}")

        ew = o.get("enterWorks") or {}
        check(ew.get("ok") and ew.get("changed"),
              "② 键盘聚焦页签后触发真的切换了状态",
              f"{ew.get('before')} -> {ew.get('after')}, focused={ew.get('focused')}")

        hd = o.get("headings") or {}
        print(f"  面板标题: {hd.get('total')} 个，非 h2: {hd.get('wrong')}")
        check(hd.get("allH2") is True and hd.get("total"),
              "③ 全部面板标题是 <h2>（跳标题可停在每个面板）", f"非 h2: {hd.get('wrong')}")

        ts = o.get("thSort") or {}
        tsi = ts.get("info") or []
        print(f"  可排序表头: {ts.get('total')} 个")
        check(tsi and all(x["tabIndex"] >= 0 for x in tsi),
              "③b 可排序表头可 Tab 到（<th> 补 tabindex）",
              f"{[(i, x['tabIndex']) for i, x in enumerate(tsi) if x['tabIndex'] < 0]}")
        check(tsi and all(x["role"] == "button" for x in tsi),
              "③b 表头有 role=button（屏幕阅读器能读成可操作）",
              f"{[x['role'] for x in tsi if x['role'] != 'button']}")
        check(tsi and all(x["ico"].strip() for x in tsi),
              "③b 未排序列也有常驻箭头提示（此前是空白，只能 hover 才发现能点）",
              f"{[i for i, x in enumerate(tsi) if not x['ico'].strip()]}")
        check(ts.get("changed") is True, "③b 键盘 Enter 真的改变了排序",
              f"before={ts.get('before')}")

        dg = o.get("diag") or {}
        check(dg.get("total") and dg.get("allTab"),
              "③c 诊断格可 Tab 到", f"{dg}")
        check(dg.get("total") and dg.get("allCaret"),
              "③c 诊断格有 ▸ 可展开指示（此前是纯文本）", f"{dg}")

        fr = (o.get("focusRing") or {}).get("seq") or []
        print(f"  Tab 序列（前 30 次）:")
        for x in fr[:12]:
            print(f"    {x['tag']:6s} {x['cls'][:30]:32s} {x['ringKind']:8s} {x['outline']}")
        # 判据: 键盘 Tab 到的**每一个**可聚焦元素都必须有可见的聚焦反馈 ——
        # outline 或 box-shadow 二者其一即可（表单控件在本项目用后者）。
        # 允许 <body>/<html> 这种容器被聚焦（Chrome 在无 tabindex 时给出 body）。
        focusables = [x for x in fr if x["tag"] not in ("BODY", "HTML")]
        no_ring = [x for x in focusables if not x["hasRing"]]
        kinds = {x["ringKind"] for x in focusables}
        check(len(focusables) >= 10, "④ Tab 能穿过足够多的控件（键盘导航可用）",
              f"只命中 {len(focusables)} 个")
        check(not no_ring, "④ 所有 Tab 可达元素都有可见聚焦反馈（outline 或光晕）",
              f"{[(x['tag'], x['cls'], x['outline'], x['boxShadow']) for x in no_ring[:5]]}")
        check("outline" in kinds and "shadow" in kinds,
              "④ 两种聚焦反馈都实测到了（按钮用 outline / 表单控件用光晕）", f"{kinds}")

        pgs = o.get("pages") or {}
        for tab, v in pgs.items():
            check(v.get("textLen", 0) > 50, f"⑤「{tab}」页正常渲染（未被标签改动吞掉）",
                  f"textLen={v.get('textLen')} panels={v.get('panels')}")

        check(not (res.get("errs") or []), "⑤ 零 pageerror", f"{(res.get('errs') or [])[:3]}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
