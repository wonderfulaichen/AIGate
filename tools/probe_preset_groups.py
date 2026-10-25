# -*- coding: utf-8 -*-
"""内置供应商目录「按地区分组 + 图标 + 领 Key 入口」探针（2026-10-24）。

用户批评「应该有内置供应商的啊」。查证：
  · WaLiAPI 有 31 条 preset / 18 字段（区域分组 / icon_key / key_signup_url / 端点多选 …）
  · AIGate 原来只有 **3 条 / 8 字段**，且平铺一列

本轮扩充到 13 条，并补 region / icon_key / key_signup_url 三个字段。

判据：
  ① 预设数 >= 10（扩充生效）
  ② presetGroups() 按 国际/国内/本地 分组，且**空组不渲染**
  ③ 每个分组内的条目都真的属于该地区（分组不是假的）
  ④ 分组顺序固定（国际 → 国内 → 本地）
  ⑤ 预设行有图标占位（icon_key 命中已知品牌用品牌符，否则首字母）
  ⑥ 有 key_signup_url 的预设：抽屉里出现「前往获取 API Key」横条
  ⑦ 无 signup 的预设不显示横条（不编地址）
  ⑧ 零 pageerror

用法: python tools/probe_preset_groups.py
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
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
DEPLOY_ROOT = r"D:\Office software\Development Project\AI中转"
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_pgroups")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        _fails.append(label)
        print(f"  {BAD}  {label}  {detail}")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


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
  const p = await b.newPage({ viewport: { width: 1500, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2200);

  const out = { errs };

  // ①②③④ 分组结构（纯数据，不依赖渲染）
  out.groups = await p.evaluate(() => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const all = d.presets || [];
    const gs = d.presetGroups();
    return {
      total: all.length,
      regions: all.map(x => x.region),
      groupOrder: gs.map(g => g.region),
      groupSizes: gs.map(g => ({ r: g.region, n: g.items.length })),
      // 每个分组内的条目是否都属于该 region
      consistent: gs.every(g => g.items.every(it => (it.region || 'custom') === g.region)),
      // 空组是否被过滤
      noEmpty: gs.every(g => g.items.length > 0),
      // 各分组条目数之和 == 总数（不重不漏）
      sumOk: gs.reduce((n, g) => n + g.items.length, 0) === all.length,
      withSignup: all.filter(x => x.key_signup_url).map(x => x.id),
      iconKeys: all.map(x => ({ id: x.id, k: x.icon_key || '' })),
    };
  });

  // ⑤ 图标占位渲染
  out.icons = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.showAddProvider = true;
    await new Promise(r => setTimeout(r, 900));
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const icons = Array.from(document.querySelectorAll('.prov-idx')).filter(isVis)
      .map(x => (x.textContent || '').trim());
    // 分组标题文案
    const heads = Array.from(document.querySelectorAll('.txs')).filter(isVis)
      .map(x => (x.textContent || '').trim())
      .filter(t => /^(国际|国内|本地|自定义|International|China|Local|Custom)$/.test(t));
    return { iconCount: icons.length, sample: icons.slice(0, 8), heads };
  });
  await p.evaluate(() => { const d = document.querySelector('[x-data]')._x_dataStack[0];
    d.showAddProvider = false; });
  await p.waitForTimeout(400);

  // ⑥⑦ signup 横条：分别对「有 signup」与「无 signup」的预设开抽屉
  const probeDrawer = async (presetId) => {
    return await p.evaluate(async (pid) => {
      const d = document.querySelector('[x-data]')._x_dataStack[0];
      // 确保该预设已在表单里
      const pt = (d.presets || []).find(x => x.id === pid);
      if (!pt) return { err: 'no preset ' + pid };
      let pi = (d.providersFormData || []).findIndex(x => x.preset === pid);
      if (pi < 0) { d.addFromPreset(pt); await new Promise(r => setTimeout(r, 500));
        pi = (d.providersFormData || []).findIndex(x => x.preset === pid); }
      if (pi < 0) return { err: 'not added' };
      d.openProviderDrawer(pi);
      await new Promise(r => setTimeout(r, 800));
      const isVis = (el) => { let n = el; while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement; } return true; };
      const link = Array.from(document.querySelectorAll('a')).filter(isVis)
        .find(a => /获取 API Key|Get an API key/.test(a.textContent || ''));
      const res = { hasBar: !!link, href: link ? link.getAttribute('href') : null };
      d.closeProviderDrawer();
      await new Promise(r => setTimeout(r, 400));
      return res;
    }, presetId);
  };
  const withSignup = (out.groups.withSignup || [])[0] || null;
  out.signupOn = withSignup ? await probeDrawer(withSignup) : { err: 'no signup preset' };
  out.signupOff = await probeDrawer('ollama');

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 300) })));
"""


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}（先 cargo build --release）")
        return 1
    pd = pdir()
    if not pd:
        print(f"{BAD}  找不到 playwright")
        return 1
    shutil.rmtree(WD, ignore_errors=True)
    os.makedirs(os.path.join(WD, "data"), exist_ok=True)
    shutil.copy2(os.path.join(DEPLOY_ROOT, "providers.json"), os.path.join(WD, "providers.json"))
    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(WD, "data")
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read(); break
            except Exception: time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        jsf = os.path.join(WD, "g.js")
        io.open(jsf, "w", encoding="utf-8").write(JS)
        env2 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env2, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-600:]}\n{(r.stderr or '')[-600:]}")
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal'][:250]}")
            return 1
    finally:
        proc.terminate()
        try: proc.wait(8)
        except Exception: proc.kill()

    g = d.get("groups") or {}
    ic = d.get("icons") or {}
    so = d.get("signupOn") or {}
    sf = d.get("signupOff") or {}
    print("内置供应商目录（按地区分组 + 图标 + 领 Key）")
    print(f"  [现场] 预设总数={g.get('total')} 分组={g.get('groupOrder')} 各组={g.get('groupSizes')}")
    print(f"  [现场] 有 signup 的={g.get('withSignup')}")
    print(f"  [现场] 图标: {ic.get('iconCount')} 个，样例={ic.get('sample')}，分组标题={ic.get('heads')}")
    print(f"  [现场] signup 横条: 有signup={so} / 无signup={sf}")

    check((g.get("total") or 0) >= 10,
          "① 内置预设已扩充到 10 条以上（原来只有 3 条）", f"实际={g.get('total')}")
    go = g.get("groupOrder") or []
    check(go[:3] == ["international", "domestic", "local"],
          "④ 分组顺序固定：国际 → 国内 → 本地", f"实际={go}")
    check(g.get("consistent") is True,
          "③ 每个分组内的条目都真属于该地区（分组不是假的）")
    check(g.get("noEmpty") is True, "② 空组被过滤（不渲染空标题）")
    check(g.get("sumOk") is True,
          "② 各分组条目数之和 == 总数（不重不漏）", f"groups={g.get('groupSizes')}")

    check((ic.get("iconCount") or 0) >= 10,
          "⑤ 预设行有图标占位", f"实际={ic.get('iconCount')}")
    heads = ic.get("heads") or []
    check("国际" in heads and "国内" in heads,
          "⑤ 页面上真的渲染出地区分组标题", f"实际={heads}")

    check(so.get("hasBar") is True,
          "⑥ 有 key_signup_url 的预设显示「前往获取 API Key」横条",
          f"实际={so}")
    check(bool(so.get("href")) and str(so.get("href")).startswith("http"),
          "⑥ 横条链接是有效 http 地址", f"href={so.get('href')!r}")
    check(sf.get("hasBar") is False,
          "⑦ 无 signup 的预设不显示横条（不编造地址）", f"实际={sf}")

    check(len(d.get("errs") or []) == 0, "⑧ 零 pageerror", str((d.get("errs") or [])[:2]))

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过:")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
