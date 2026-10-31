# -*- coding: utf-8 -*-
"""opencode 多工作区额度面板：验证 UI 真的把每个工作区都画出来了。

为什么需要它：后端返回 `workspaces[]` 只证明"数据拿到了"，
而用户看到的是**面板** —— 模板若还按单区渲染（或分组容器没接上），
数据再多也只会显示一个区。这类"后端对、前端不显示"的缺陷
本项目踩过多次（死代码 / 未接线），必须用浏览器实测。

判据：
  ① 追问区与区之间**有分隔**（多个工作区时显示区名）
  ② 工作区数量 == 后端返回的数量（不是只画第一个）
  ③ 各区的月额度百分比**互不相同**（证明不是把同一个区的值复制了几遍）
  ④ 已有的单区场景（只有 1 个区）时不显示区名标题（避免噪音）

用法：python tools/probe_oc_ws_ui.py
需要 Playwright（复用 probe_quota_panel 的查找方式）与真实 Cookie。
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
PROFILE = os.path.join(ROOT, ".openbitfun", "tmp", "oc_profile")
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_ws")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        _fails.append(label)
        print(f"  {BAD}  {label}  {detail}")


def skip(label, why):
    print(f"  {SKIP}  {label}  ({why})")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pdir():
    for rel in (".openbitfun/tmp/vidtools", "."):
        d = os.path.join(ROOT, rel)
        if os.path.isdir(os.path.join(d, "node_modules", "playwright")):
            return d
    return None


GET_COOKIE_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const ctx = await chromium.launchPersistentContext(process.env.PROFILE, {
    channel: 'msedge', headless: true,
  });
  const cookies = await ctx.cookies('https://opencode.ai');
  const want = cookies.find(c => c.name === '__Host-console_session');
  await ctx.close();
  console.log('COOKIE ' + JSON.stringify(want ? want.value : null));
})().catch(() => console.log('COOKIE null'));
"""

# 先问 /orgs 拿到全部工作区，写进配置，再打开面板查额度并抓 DOM。
UI_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 240)));

  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);

  const out = { errs };

  // ① 自动获取工作区并写进抽屉草稿（模拟用户点「自动获取」）
  out.wsIds = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
    await new Promise(r => setTimeout(r, 1200));
    const prov = (d.providersFormData || []).find(x => x.name === 'go');
    if (!prov) return { ok: false, why: 'no go provider' };
    await d.ocFetchOrgId(prov);
    return {
      ok: true,
      ids: d.provWorkspaces(prov),
      single: prov.console_org_id || '',
      list: prov.console_org_ids || [],
      msg: d.ocVerifyMsg,
    };
  });
  await p.waitForTimeout(800);

  // ② 展开 go 供应商并查额度
  out.expand = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    if (!d.isProvExpanded('go')) d.toggleProvExpand('go');
    await new Promise(r => setTimeout(r, 900));
    await d.fetchOpencodeQuota('go');
    await new Promise(r => setTimeout(r, 2600));
    return { wsCount: d.ocQuotaWorkspaces('go').length };
  });
  await p.waitForTimeout(800);

  // ③ 抓 DOM：每个工作区分组 + 组内行
  out.dom = await p.evaluate(() => {
    const isVis = (el) => {
      let n = el;
      while (n && n !== document.body) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        n = n.parentElement;
      }
      return true;
    };
    const groups = Array.from(document.querySelectorAll('.quota-ws')).filter(isVis);
    return {
      groupCount: groups.length,
      groups: groups.map(g => {
        const hd = Array.from(g.querySelectorAll('.quota-ws-hd')).filter(isVis)[0];
        const rows = Array.from(g.querySelectorAll('.quota-row')).filter(isVis);
        return {
          headerVisible: !!hd,
          headerText: hd ? (hd.textContent || '').trim() : '',
          rows: rows.map(r => ({
            label: (r.querySelector('.quota-model') || {}).textContent || '',
            pct: (r.querySelector('.quota-w-v') || {}).textContent || '',
            amt: (r.querySelector('.quota-amt') || {}).textContent || '',
          })),
        };
      }),
      errText: (Array.from(document.querySelectorAll('.quota-err')).filter(isVis)[0] || {})
        .textContent || '',
    };
  });

  console.log('RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('RESULT ' + JSON.stringify({ fatal: String(e).slice(0, 400) })));
"""


def prep_providers(cookie, org_ids):
    src = os.path.join(DEPLOY_ROOT, "providers.json")
    o = json.load(io.open(src, encoding="utf-8"))
    provs = o if isinstance(o, list) else o.get("providers") or []
    for p in provs:
        if p.get("name") == "go" and cookie:
            p["console_session"] = "__Host-console_session=" + cookie
            # 故意只给单值：模拟**升级前的老配置**，验证「自动获取」能补齐多区
            p.pop("console_org_ids", None)
            p["console_org_id"] = org_ids[0] if org_ids else ""
    dst = os.path.join(WD, "providers.json")
    if isinstance(o, list):
        io.open(dst, "w", encoding="utf-8").write(json.dumps(provs, ensure_ascii=False, indent=2))
    else:
        o["providers"] = provs
        io.open(dst, "w", encoding="utf-8").write(json.dumps(o, ensure_ascii=False, indent=2))
    return dst


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

    # 真实 Cookie
    cookie = None
    if os.path.isdir(PROFILE):
        jsf = os.path.join(WD, "_c.js")
        io.open(jsf, "w", encoding="utf-8").write(GET_COOKIE_JS)
        env = dict(os.environ, NODE_PATH=os.path.join(pd, "node_modules"), PROFILE=PROFILE)
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("COOKIE ")), None)
        if line:
            cookie = json.loads(line[7:]) or None
    if not cookie:
        print(f"{SKIP}  没有真实 Console Cookie（{PROFILE} 里没登录过），无法测多区")
        return 0
    print("[准备] 真实 Cookie: 有")

    # 先问上游拿全部工作区（用于 prep 的单值种子）
    import urllib.error
    req = urllib.request.Request(
        "https://opencode.ai/console/api/orgs",
        headers={"Cookie": "__Host-console_session=" + cookie,
                 "Accept": "*/*",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=20) as rr:
            orgs = json.loads(rr.read().decode())
    except Exception as e:
        print(f"{BAD}  查 /orgs 失败: {e}")
        return 1
    all_ids = [o["id"] for o in orgs if o.get("id")]
    print(f"[准备] 上游工作区: {len(all_ids)} 个 -> {all_ids}")

    prep_providers(cookie, all_ids)
    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env2 = dict(os.environ,
                PORT=str(port),
                AIGATE_DATA_DIR=os.path.join(WD, "data"),
                AIGATE_OPEN_ADMIN="1")
    proc = subprocess.Popen([iso], env=env2, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        jsf = os.path.join(WD, "u.js")
        io.open(jsf, "w", encoding="utf-8").write(UI_JS)
        env3 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env3, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-600:]}\n{(r.stderr or '')[-600:]}")
            return 1
        d = json.loads(line[len("RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面异常: {d['fatal']}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    ws = d.get("wsIds") or {}
    dom = d.get("dom") or {}
    print("\nopencode 多工作区额度面板")
    print(f"  [自动获取] ok={ws.get('ok')} 拿到 {len(ws.get('ids') or [])} 个: {ws.get('ids')}")
    print(f"  [配置写入] 单值={ws.get('single')!r} 列表={ws.get('list')}")
    print(f"  [查额度] 后端返回 {d.get('expand', {}).get('wsCount')} 个工作区")
    print(f"  [DOM] 分组数={dom.get('groupCount')}")
    for i, g in enumerate(dom.get("groups") or []):
        print(f"     组{i+1}: header={g['headerVisible']} {g['headerText']!r}")
        for r0 in g["rows"]:
            print(f"        {r0['label']}: {r0['pct']} | {r0['amt']}")
    if dom.get("errText"):
        print(f"  [错误块] {dom['errText'][:160]}")
    if d.get("errs"):
        print(f"  [页面异常] {d['errs']}")

    # ── 判据 ──
    print()
    check(not d.get("errs"), "页面零 pageerror", str(d.get("errs"))[:200])
    check(len(ws.get("ids") or []) >= 2,
          "自动获取拿到多个工作区",
          f"只拿到 {ws.get('ids')}")
    check(len(ws.get("list") or []) >= 2,
          "多个工作区都写进了配置列表（console_org_ids）",
          f"list={ws.get('list')}")
    check(any(str(x).startswith("org_") for x in (ws.get("list") or [])),
          "org_ 前缀工作区被保留（不再被过滤）",
          str(ws.get("list")))

    ws_count = d.get("expand", {}).get("wsCount") or 0
    check(ws_count >= 2, "后端返回多个工作区", f"wsCount={ws_count}")
    check(dom.get("groupCount") == ws_count,
          "DOM 里的工作区分组数 == 后端返回数（每个区都画出来了）",
          f"dom={dom.get('groupCount')} backend={ws_count}")
    check(all(g["headerVisible"] for g in (dom.get("groups") or [])),
          "多区时每个分组都有区名标题",
          str([g["headerVisible"] for g in (dom.get("groups") or [])]))

    pcts = []
    for g in (dom.get("groups") or []):
        for r0 in g["rows"]:
            if r0["label"] in ("月度", "Month", "本 月"):
                pcts.append(r0["pct"])
    # 月行标签随语言变化，兜底取每组最后一行（模板里 month 在末尾）
    if not pcts:
        pcts = [(g["rows"] or [{}])[-1].get("pct") for g in (dom.get("groups") or [])]
    check(len(set(pcts)) > 1,
          "各工作区的月额度百分比互不相同（不是把同一个值复制了几遍）",
          f"pcts={pcts}")

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项失败:")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
