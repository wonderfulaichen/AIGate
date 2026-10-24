# -*- coding: utf-8 -*-
"""供应商行「统计定宽对齐 + 失败原因胶囊」探针（2026-10-24）。

钉住两件事：
  ① **三项统计的列对齐**：所有有统计的行，.prov-stats 的**左边缘必须相同**。
     改造前右边缘就对齐了（右对齐），但每项起点随数字位数游走 —— 竖着扫一列
     比不出高低，等于三个指标无法纵向比较。
  ② **失败原因只在真有错误时出现，且不挤动统计列**：
     - `.prov-errpill` 的内容必须来自后端的 last_error（不是前端编的）；
     - 胶囊出现时 `.prov-stats` 的左边缘**不得**跟着左移（第一版把它放行尾，
       实测 buddy/Joule 的统计左边缘从 1045 掉到 753/770，全页对齐被破坏）。

为什么必须用真实实例而不是预览桩：预览桩没有 per_provider 数据，
量不到任何统计列；胶囊更是完全不会出现。

用法: python tools/probe_prov_cols.py
退出码 0 = 全过; 1 = 有判据转红。
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
DEPLOY_DATA = os.path.join(DEPLOY_ROOT, "data")
OUT = os.path.join(ROOT, ".openbitfun", "tmp", "shots_provcols")

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


JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1250 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await p.waitForTimeout(5000);
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2600);

  const out = await p.evaluate(() => {
    const R = e => { const b = e.getBoundingClientRect();
      return { l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width) }; };
    const rows = Array.from(document.querySelectorAll('.prov-row'));
    const detail = rows.map(row => {
      const st = row.querySelector('.prov-stats');
      const pill = row.querySelector('.prov-errpill');
      const name = (row.querySelector('.prov-name') || {}).textContent || '';
      const calls = row.querySelector('.prov-stat-u');
      const rate = row.querySelector('.prov-stat-r');
      const lat = row.querySelector('.prov-stat-t');
      const main = row.querySelector('.prov-main');
      return {
        name,
        hasStats: !!st,
        stats: st ? R(st) : null,
        // 名称区右边缘：用来兜底判"统计有没有被挤到名称区左边"
        nameRight: main ? Math.round(main.getBoundingClientRect().right) : null,
        pillText: pill ? (pill.textContent || '').replace(/\s+/g, ' ').trim() : null,
        pillTitle: pill ? pill.getAttribute('title') : null,
        pillAria: pill ? pill.getAttribute('aria-label') : null,
        // 套餐/免费徽章
        flatBadge: !!row.querySelector('.tag-flat'),
        flatTitle: row.querySelector('.tag-flat') ? row.querySelector('.tag-flat').getAttribute('title') : null,
        colW: { u: calls ? Math.round(calls.getBoundingClientRect().width) : null,
                r: rate ? Math.round(rate.getBoundingClientRect().width) : null,
                t: lat ? Math.round(lat.getBoundingClientRect().width) : null },
      };
    });
    // 后端真值，用来核对胶囊内容不是前端编的
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const pp = (d.stats && d.stats.per_provider) || [];
    const backend = pp.map(x => ({ provider: x.provider, last_error: x.last_error || null,
                                   status: x.last_error_status || 0,
                                   billing: x.billing,
                                   requests: x.requests || 0 }));
    return { rows: detail, backend, rowCount: rows.length,
             // 数据里是否存在免费模型（per_model 里 billing=false）
             hasFreeModel: ((d.stats && d.stats.per_model) || []).some(m => m.billing === false),
             freeModels: ((d.stats && d.stats.per_model) || [])
                 .filter(m => m.billing === false)
                 .map(m => ({ provider: m.provider, model: m.model })) };
  });
  out.errs = errs;
  console.log('JSON_RESULT ' + JSON.stringify(out));
  await b.close();
})().catch(e => console.log('JSON_RESULT ' + JSON.stringify({ fatal: String(e) })));
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}（先 cargo build --release）")
        return 1
    pd = pdir()
    if not pd:
        print(f"{BAD}  找不到 playwright")
        return 1
    wd = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_provcols")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "data"), exist_ok=True)
    src_pv = os.path.join(DEPLOY_ROOT, "providers.json")
    if not os.path.isfile(src_pv):
        print(f"{BAD}  找不到部署 providers.json: {src_pv}")
        return 1
    shutil.copy2(src_pv, os.path.join(wd, "providers.json"))
    iso = os.path.join(wd, "AIGate.exe")
    shutil.copy2(EXE, iso)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = DEPLOY_DATA
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=wd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        jsf = os.path.join(wd, "probe.js")
        io.open(jsf, "w", encoding="utf-8").write(JS)
        env2 = dict(os.environ, BASE=base, NODE_PATH=os.path.join(pd, "node_modules"))
        r = subprocess.run(["node", jsf], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env2, cwd=pd)
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("JSON_RESULT ")), None)
        if not line:
            print(f"{BAD}  playwright 未返回:\n{(r.stdout or '')[-800:]}\n{(r.stderr or '')[-800:]}")
            return 1
        d = json.loads(line[len("JSON_RESULT "):])
        if d.get("fatal"):
            print(f"{BAD}  页面脚本异常: {d['fatal'][:300]}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()

    rows = d["rows"]
    withstats = [r for r in rows if r["hasStats"]]
    print("供应商行统计对齐 + 失败原因")
    print(f"  [现场] 共 {d['rowCount']} 行, 其中 {len(withstats)} 行有统计")

    # ── ① 列对齐 ───────────────────────────────────────────────
    lefts = sorted({r["stats"]["l"] for r in withstats})
    check(len(withstats) >= 2, f"① 至少 2 行有统计可比对（实测 {len(withstats)}）")
    # 允许 8px 内浮动：末行序号是 #10 比 #1 宽，会把整行内容整体推移
    check(max(lefts) - min(lefts) <= 8,
          "① 所有有统计的行，统计区左边缘一致（≤8px）",
          f"实测左边缘集合={lefts}")

    widths = {}
    for k in ("u", "r", "t"):
        ws = {r["colW"][k] for r in withstats if r["colW"].get(k)}
        widths[k] = sorted(ws)
    check(all(len(v) == 1 for v in widths.values()),
          "① 三项统计各自**定宽**（调用/成功率/延迟在每行等宽）",
          f"实测宽度={widths}")

    # ── ② 失败原因胶囊 ─────────────────────────────────────────
    backend = {b["provider"]: b for b in d["backend"]}
    pill_rows = [r for r in rows if r["pillText"]]
    with_err = {k: v for k, v in backend.items() if v.get("last_error")}
    print(f"  [现场] 后端有 last_error 的: {list(with_err)}；页面有胶囊的: "
          f"{[r['name'] for r in pill_rows]}")

    # 胶囊数量与后端一致 —— 多一个就是前端编的, 少一个就是漏渲染
    check({r["name"] for r in pill_rows} == set(with_err),
          "② 胶囊出现与否**与后端 last_error 完全一致**（不多不少）",
          f"后端={sorted(with_err)} 页面={sorted(r['name'] for r in pill_rows)}")

    if pill_rows:
        # 胶囊正文必须是后端那条错误的开头（不是前端自造文案）
        for r in pill_rows:
            want = with_err.get(r["name"], {}).get("last_error") or ""
            got_txt = (r["pillText"] or "")
            got_title = (r["pillTitle"] or "")
            head = want.replace("\n", " ").strip()[:10]
            check(head[:6] in got_title or head[:6] in got_txt,
                  f"② 「{r['name']}」胶囊内容来自后端真实错误（不是编的）",
                  f"期望含 {head[:6]!r}, 实际 title={got_title[:40]!r}")
            # title 必须**完整包含**原文（行上截断，悬停可取全文）。
            # 2026-10-24 起 title = 「可操作建议 + 原文」，故判"包含"而非"相等"：
            # 要求相等会把"多给了一句建议"误判成失败，而建议正是本次要加的东西。
            check(want.strip() in got_title,
                  f"② 「{r['name']}」title **完整包含**上游原文（行上才有截断）",
                  f"title={got_title[:60]!r} 后端原文={want[:40]!r}")
            # 建议是可选的（映射不上时为空），但只要 title 里有建议，就应排在原文之前
            if got_title.strip() != want.strip():
                check(got_title.strip().startswith(("额度", "触发", "凭据", "渠道", "端点", "接口", "网络", "上游")),
                      f"② 「{r['name']}」title 里建议排在原文之前（便于先看该做什么）",
                      f"title 开头={got_title[:24]!r}")
            check(bool(r["pillAria"]) and len(r["pillAria"]) > 0,
                  f"② 「{r['name']}」胶囊有 aria-label（读屏可读）")

        # 关键回归：胶囊不得挤动统计列
        pill_stats = [r["stats"] for r in pill_rows if r["hasStats"]]
        plain_stats = [r["stats"] for r in rows if r["hasStats"] and not r["pillText"]]
        if pill_stats and plain_stats:
            all_l = sorted({s["l"] for s in pill_stats + plain_stats})
            check(max(all_l) - min(all_l) <= 8,
                  "② **有胶囊的行统计仍与无胶囊行对齐**（胶囊不挤动统计列）",
                  f"实测有胶囊={[s['l'] for s in pill_stats]} 无胶囊={[s['l'] for s in plain_stats]}")
        else:
            # 全部行都有胶囊（或全都没有）时无法做"有 vs 无"的对照。
            # 此时退化为"所有行统计左边缘一致" —— 仍能抓住"胶囊把某几行推歪"的情况，
            # 但抓不住"所有行被同样推歪"。故额外用一条兜底：统计左边缘不得小于
            # 名称区右边缘（即统计必须真的在名称区右侧，没被挤到左边去）。
            all_l = sorted({s["l"] for s in pill_stats})
            check(len(all_l) <= 1 or max(all_l) - min(all_l) <= 8,
                  "② 有胶囊的行之间统计左边缘一致（无'无胶囊'对照时的退化判据）",
                  f"实测={all_l}")
            over = [r for r in rows if r["hasStats"] and r.get("nameRight")
                    and r["stats"]["l"] < r["nameRight"]]
            check(not over,
                  "② 统计区不得被胶囊挤到名称区左边",
                  f"越界行={[r['name'] for r in over]}")

        # 胶囊正文必须被限长（上游会返回整页 HTML，不截断会撑爆行）
        for r in pill_rows:
            n = len(r["pillText"] or "")
            check(n <= 60, f"② 「{r['name']}」行上胶囊正文化截断（≤60 字符，实测 {n}）")
    else:
        print("  （本次数据里没有失败供应商，② 的内容判据跳过）")

    check(len(d["errs"]) == 0, "③ 零 pageerror", str(d["errs"][:2]))

    # ── ④ 套餐/免费徽章 ────────────────────────────────────────
    #
    # ⚠️ 判据设计修正（2026-10-24 实测）：第一版拿 `per_provider.billing === false`
    # 当预期集合，实测**恒为空集** —— 因为 billing 的口径是"组内任一中转 ID 不在
    # 免费名单即视为计费"，而真实的 10 家里每家都至少有一个计费模型，故
    # provider 级 billing 永远为 True。空集对空集 = 判据永远"通过"，是假通过。
    #
    # 改为断言**徽章机制本身可用**：只要数据里存在免费模型（per_model 里有
    # billing=false 的模型），就说明这条链路有真实样本；此时若页面一个徽章都没有，
    # 说明徽章没接上。若整个数据里确实没有任何免费模型，则显式报告"本次无样本"，
    # 而不是默默通过。
    flat = [r for r in rows if r.get("flatBadge")]
    has_free_model = d.get("hasFreeModel") is True
    print(f"  [现场] 数据里是否有免费模型: {has_free_model}；"
          f"页面有套餐徽章的: {[r['name'] for r in flat]}")
    if has_free_model:
        # 有免费模型 → 至少应有一家供应商显示徽章（否则说明徽章根本没接上）。
        # 这是真正能抓住"判据用错口径导致永远不显示"的那一条 —— 第一版就栽在这里。
        check(len(flat) > 0,
              "④ 数据里有免费模型 → 至少一家应显示套餐徽章（徽章真的接上了）",
              "有免费模型但页面零徽章 —— 判据口径可能用错了（如用 provider 级 billing）")
        # 有徽章的供应商必须真的含免费模型（不能凭空多出来）
        free_providers = {m["provider"] for m in d.get("freeModels", [])}
        check({r["name"] for r in flat} <= free_providers,
              "④ 有徽章的供应商必须真的含免费模型（不凭空出现）",
              f"徽章行={sorted(r['name'] for r in flat)} 含免费模型的={sorted(free_providers)}")
    else:
        print("  （数据里没有免费模型 → ④ 的'应有徽章'部分无样本，只做负例断言）")
    # 负例恒可验：没有任何统计的行不得凭空长出徽章
    no_stats = [r for r in rows if not r["hasStats"]]
    check(not any(r.get("flatBadge") for r in no_stats),
          "④ 无统计数据的行不得凭空显示套餐徽章（未采集 ≠ 套餐型）",
          f"越界行={[r['name'] for r in no_stats if r.get('flatBadge')]}")
    for r in flat:
        check(bool(r.get("flatTitle")),
              f"④ 「{r['name']}」套餐徽章有 title（解释「已省 —」的原因）")

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
