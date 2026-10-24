# -*- coding: utf-8 -*-
"""opencode 套餐额度面板探针（2026-10-24）。

钉住的核心口径（都是查官方文档后确认的事实，不是我们的发明）：
  · opencode **没有**额度查询接口，面板的用量只能来自本地累加
    `usage.opencode.allowance`；
  · 限额内 `cost` 报 0（官方明写），所以**不能用 cost 算额度**；
  · 三档是滚动窗口：5 小时 = 月额度 20%、周 = 50%、月 = 100%，
    三条各自独立，合成一条看不出"5h 快满了但月还早"；
  · 额度表是外部数据，面板必须显示抄录日期。

判据：
  ① 有 allowance 的供应商显示额度块；没数据的整块不渲染（不画 0% 假条）
  ② 三档都有（5 小时 / 本周 / 本月），且是**三条独立**进度条
  ③ 消耗百分比与后端累加值一致（用构造数据验证换算）
  ④ 超额（>100%）如实显示，不 clamp 到 100%
  ⑤ 额度表日期可见（用户能判断数据是否过期）
  ⑥ 零 pageerror

用法: python tools/probe_quota_panel.py
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
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_quota")

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
  await p.goto(process.env.BASE + '/admin', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await p.waitForTimeout(5500);
  await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    await d.switchTab('providers');
    await d.fetchStats();
  });
  await p.waitForTimeout(2600);

  // 展开所有有额度数据的供应商
  const expanded = await p.evaluate(async () => {
    const d = document.querySelector('[x-data]')._x_dataStack[0];
    const withQuota = (d.stats && d.stats.per_provider || [])
      .filter(x => x.opencode_quota && x.opencode_quota.length).map(x => x.provider);
    for (const n of withQuota) {
      if (!d.isProvExpanded(n)) d.toggleProvExpand(n);
    }
    await new Promise(r => setTimeout(r, 1200));
    return {
      providers: withQuota,
      backend: (d.stats && d.stats.per_provider || [])
        .filter(x => x.opencode_quota && x.opencode_quota.length)
        .map(x => ({ provider: x.provider, rows: x.opencode_quota })),
      tableDate: d.stats ? d.stats.opencode_quota_table_date : null,
    };
  });
  await p.waitForTimeout(900);

  const ui = await p.evaluate(() => {
    const isVis = (el) => { let n = el; while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      n = n.parentElement; } return true; };
    const rows = Array.from(document.querySelectorAll('.quota-row')).filter(isVis);
    return {
      quotaBlocks: Array.from(document.querySelectorAll('.quota-list')).filter(isVis).length,
      rows: rows.map(r => ({
        model: (r.querySelector('.quota-model') || {}).textContent || '',
        bars: Array.from(r.querySelectorAll('.quota-w')).map(w => ({
          label: (w.querySelector('.quota-w-l') || {}).textContent || '',
          val: (w.querySelector('.quota-w-v') || {}).textContent || '',
          // 进度条宽度（读内联 style 的 width，避免 offsetWidth 在隐藏时全 0）
          width: ((w.querySelector('.quota-bar > i') || {}).style || {}).width || '',
        })),
      })),
      // 额度表日期：找含日期的说明文字
      sourceNotes: Array.from(document.querySelectorAll('.txs.tm'))
        .filter(isVis).map(x => (x.textContent || '').trim())
        .filter(t => /\d{4}-\d{2}-\d{2}/.test(t)),
    };
  });

  console.log('RESULT ' + JSON.stringify({ expanded, ui, errs }));
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
    data = os.path.join(WD, "data")
    os.makedirs(data, exist_ok=True)
    src_pv = os.path.join(DEPLOY_ROOT, "providers.json")
    if not os.path.isfile(src_pv):
        print(f"{BAD}  找不到 {src_pv}")
        return 1
    shutil.copy2(src_pv, os.path.join(WD, "providers.json"))

    now = int(time.time())
    # 构造 4 条带 allowance 的 go 请求：
    #   glm-5.3      月额度 $15 → 5h 窗口上限 $3；用 $2.7 = 90%（琥珀）
    #   deepseek-v4.1-flash 月额度 $80 → 月上限 $80；用 $90 = 112.5%（超额，验不 clamp）
    # 另加一条**不带** allowance 的（应被排除）
    def lg(model, prov, allowance, src, ago):
        return {
            "timestamp": now - ago, "model": f"{prov}/{model}", "provider": prov,
            "upstream_model": model, "endpoint": "", "status": 200,
            "latency_ms": 100, "body_len": 10, "prompt_tokens": 10,
            "completion_tokens": 10, "allowance_usd": allowance,
            "billing_source": src,
        }
    logs = [
        lg("glm-5.3", "go", 2.7, "go", 600),            # 10 分钟前
        lg("glm-5.3", "go", 0.5, "go", 2 * 86400),      # 2 天前（只进周/月）
        lg("deepseek-v4.1-flash", "go", 90.0, "go", 600),
        lg("glm-5.3", "go", 5.0, "balance", 600),       # balance → 不计入套餐额度
        lg("glm-5.3", "go", None, None, 600),           # 无 allowance → 不计入
    ]
    with io.open(os.path.join(data, "logs.jsonl"), "w", encoding="utf-8") as f:
        for l in logs:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")

    port = free_port()
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = data
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso], env=env, cwd=WD,
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
        jsf = os.path.join(WD, "q.js")
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
            print(f"{BAD}  页面异常: {d['fatal'][:300]}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()

    ex = d["expanded"]
    ui = d["ui"]
    print("opencode 套餐额度面板")
    print(f"  [现场] 后端有额度的供应商: {ex['providers']}")
    for b0 in ex["backend"]:
        for row in b0["rows"]:
            print(f"    {b0['provider']}/{row['model']}: "
                  f"5h=${row['used_5h_usd']:.4f} 周=${row['used_weekly_usd']:.4f} "
                  f"月=${row['used_monthly_usd']:.4f} 额度=${row.get('monthly_usd')} "
                  f"samples={row['samples']}")
    print(f"  [现场] 额度表日期: {ex['tableDate']!r}")

    rows = ui.get("rows") or []
    print(f"  [现场] 页面额度行: {len(rows)}")
    for r0 in rows:
        print(f"    {r0['model']}: " + " | ".join(
            f"{b1['label']}={b1['val']}({b1['width']})" for b1 in r0["bars"]))

    # ① 额度块渲染
    check(len(ex["providers"]) > 0, "① 后端确实产出了额度数据（构造样本生效）")
    check((ui.get("quotaBlocks") or 0) > 0, "① 页面渲染了额度块",
          f"quotaBlocks={ui.get('quotaBlocks')}")

    # ② 三档独立
    if rows:
        r0 = rows[0]
        check(len(r0["bars"]) == 3,
              "② 每个模型三档（5 小时/本周/本月）都在",
              f"实际 {len(r0['bars'])} 档: {[b1['label'] for b1 in r0['bars']]}")
        labels = [b1["label"] for b1 in r0["bars"]]
        check(labels == ["5 小时", "本周", "本月"],
              "② 三档顺序为 5 小时 → 本周 → 本月",
              f"实际={labels}")

    # ③ 换算正确：glm-5.3 月额度 15 → 5h 上限 3 → 用 2.7 = 90%
    glm = next((r0 for r0 in rows if "glm-5.3" in r0["model"] and "flash" not in r0["model"]), None)
    if glm:
        five = glm["bars"][0]["val"] if glm["bars"] else ""
        check("90" in five,
              "③ 5h 档百分比换算正确（$2.7 / ($15×20%) = 90%）",
              f"实际={five!r}")

    # ④ 超额不 clamp
    ds = next((r0 for r0 in rows if "deepseek-v4.1-flash" in r0["model"]), None)
    if ds:
        month = ds["bars"][2]["val"] if len(ds["bars"]) > 2 else ""
        barw = ds["bars"][2]["width"] if len(ds["bars"]) > 2 else ""
        check("112" in month or "113" in month or "112.5" in month,
              "④ 超额如实显示（$90 / $80 = 112.5%，不 clamp 到 100%）",
              f"实际文案={month!r}")
        check(barw.startswith("100%"),
              "④ 但**进度条宽度**封顶 100%（否则会溢出容器）",
              f"实际宽度={barw!r}")

    # ⑤ 额度表日期可见
    check(bool(ex.get("tableDate")),
          "⑤ 后端下发了额度表日期（用户可判断数据时效）",
          f"实际={ex.get('tableDate')!r}")
    check(len(ui.get("sourceNotes") or []) > 0,
          "⑤ 页面上能看到额度表日期",
          f"找到的含日期说明={ui.get('sourceNotes')}")

    # ⑥ 零 pageerror
    check(len(d.get("errs") or []) == 0, "⑥ 零 pageerror", str((d.get("errs") or [])[:2]))

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
