# -*- coding: utf-8 -*-
"""探针 + 截图核对: 用量热力图（隔离实例跑新构建, 真实数据来自日级 rollup）。

为什么要单独写: 纯视觉改动不截图就等于没验（项目纪律）。而且热力图有**日历语义**
（列=周/行=星期/逐日连续）, 光看"渲染出格子了"不够 —— 必须核对:
  ① 格子数 = 周数×7（含补齐的空格），不是"有多少天数据就多少格"
  ② 星期行对齐（首列补到周日, 否则同一行的几天会跨星期）
  ③ 亮/暗双主题都真的画出来了（不是空白/不是全同色）
  ④ 后端逐日补齐（含 0 值那天）

用法: python tools/probe_heatmap_shot.py
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
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_heatmap")
SHOTS = os.path.join(ROOT, ".openbitfun", "tmp", "shots")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8973

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
os.makedirs(SHOTS, exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "mock", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d", "models": {"mock/m": {"upstream_model": "m"}},
    }]
}))

# 造一份**含断档**的 rollup 账本: 让"空档补零"和"分档着色"都能被看见。
# ⚠️ 日期键必须是**东八区日界的秒**(= UTC 0 点 - 8h), 与 rollup.rs 的 day_start 一致 ——
#    用 UTC 日界造数会全部落到相邻那天, 探针就看到"总量 0"(本探针第一版就踩了这个)。
# 字段照真实行(RollupEntry)写全; 未列出的字段靠 #[serde(default)] 兜底。
TZ = 8 * 3600
today = ((int(time.time()) + TZ) // 86400) * 86400 - TZ
lines = []
for back in range(0, 60):
    if back % 7 == 3:            # 每 7 天故意留一天断档 —— 日历上必须是空格
        continue
    day = today - back * 86400
    # 造出量级差异, 让四档都有样本
    tok = [0, 12_000, 90_000, 400_000, 1_500_000][back % 5]
    lines.append(json.dumps({
        "d": day,
        "e": [{
            "provider": "mock", "upstream": "m", "aliases": ["mock/m"], "price": None,
            "requests": 5, "errors": 0, "latency_sum_ms": 1000, "body_bytes": 100,
            "prompt_tokens": tok // 2, "completion_tokens": tok - tok // 2,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "strip_saved_tokens": 0, "trim_saved_tokens": 0, "resp_cache_saved_tokens": 0,
        }],
    }, ensure_ascii=False))
io.open(os.path.join(ISO, "data", "daily_stats.jsonl"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    print("=== 用量热力图: 后端口径 ===\n")
    d = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/admin/api/heatmap?weeks=17" % PG, timeout=20).read())
    days = d.get("days") or []
    print("  返回天数:", len(days), " 周数:", d.get("weeks"), " 合计:", d.get("total_tokens"))
    ck("① 逐日补齐: 天数 == weeks*7", len(days) == 17 * 7, "实得 %d" % len(days))
    zeros = [x for x in days if x["tokens"] == 0]
    ck("② 断档那天返回 0（不是缺项）", len(zeros) > 0, "0 值天数 %d" % len(zeros))
    ck("③ 有量级差异（四档都能出现）",
       len({x["tokens"] for x in days}) >= 4,
       "不同取值 %d 种" % len({x["tokens"] for x in days}))
    # 日期连续性
    step_ok = all(days[i + 1]["day_start"] - days[i]["day_start"] == 86400
                  for i in range(len(days) - 1))
    ck("④ 日期逐日连续（步长恰为 86400）", step_ok)

    print("\n=== 前端渲染 + 截图核对 ===\n")
    js = os.path.join(ISO, "shot.js")
    io.open(js, "w", encoding="utf-8").write("""
const path = require('path');
const PW = path.join(%r, '.openbitfun','tmp','vidtools','node_modules','playwright');
const { chromium } = require(PW);
const OUT = %r;
(async () => {
  const b = await chromium.launch();
  const res = {};
  for (const theme of ['dark','light']) {
    const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
    const errs = [];
    p.on('pageerror', e => errs.push(String(e)));
    await p.addInitScript(t => { try { localStorage.setItem('aigate.theme', t); } catch(e){} }, theme);
    await p.goto('http://127.0.0.1:%d/admin', { waitUntil: 'domcontentloaded' });
    await p.waitForTimeout(2000);
    await p.evaluate(() => {
      const el = Array.from(document.querySelectorAll('.seg-i,.nav-i,button,a'))
        .find(x => (x.textContent||'').trim() === '分析');
      if (el) el.click();
    });
    await p.waitForTimeout(2500);
    const info = await p.evaluate(() => {
      const grid = document.querySelector('.hm-grid svg');
      const rects = grid ? grid.querySelectorAll('rect') : [];
      const fills = new Set();
      rects.forEach(r => fills.add(r.getAttribute('fill')));
      const box = grid ? grid.getBoundingClientRect() : null;
      return {
        has_svg: !!grid,
        rect_count: rects.length,
        distinct_fills: Array.from(fills),
        width: box ? Math.round(box.width) : 0,
        height: box ? Math.round(box.height) : 0,
        legend_swatches: document.querySelectorAll('.hm-sw').length,
        takeaway: (document.querySelector('.panel-takeaway')||{}).innerText || '',
      };
    });
    // 截热力图面板: 从 .hm-wrap 往上找到那个 .pnl（面板容器）。
    // ⚠️ 别用固定层数的 parentElement —— 层数一变就会截到整页（第一版就截成了页面顶部）。
    const el2 = await p.evaluateHandle(() => {
      let n = document.querySelector('.hm-wrap');
      while (n && !(n.classList && n.classList.contains('pnl'))) n = n.parentElement;
      return n || document.querySelector('.hm-wrap');
    });
    const shot = path.join(OUT, 'heatmap_' + theme + '.png');
    try { await el2.asElement().screenshot({ path: shot }); } catch(e) {
      await p.screenshot({ path: shot });
    }
    info.errors = errs.slice(0,3);
    info.shot = shot;
    res[theme] = info;
    await p.close();
  }
  console.log(JSON.stringify(res, null, 2));
  await b.close();
})();
""" % (ROOT, SHOTS, PG))
    r = subprocess.run(["node", js], capture_output=True, text=True, timeout=240, encoding="utf-8")
    print(r.stdout[-2500:] if r.stdout else "")
    if r.returncode != 0:
        print("node stderr:", (r.stderr or "")[-800:])
    try:
        out = json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        out = {}
    for theme in ("dark", "light"):
        t = out.get(theme, {})
        ck("⑤ [%s] 热力图渲染出 SVG" % theme, t.get("has_svg") is True, str(t.get("errors")))
        ck("⑥ [%s] 格子数 == 17*7 (含补齐空格)" % theme,
           t.get("rect_count") == 119, "实得 %s" % t.get("rect_count"))
        ck("⑦ [%s] 至少有 2 种填充色 (不是一片同色)" % theme,
           len(t.get("distinct_fills") or []) >= 2, str(t.get("distinct_fills")))
        ck("⑧ [%s] 无 pageerror" % theme, not t.get("errors"), str(t.get("errors")))
    ck("⑨ 两主题截图都已产出",
       os.path.exists(os.path.join(SHOTS, "heatmap_dark.png"))
       and os.path.exists(os.path.join(SHOTS, "heatmap_light.png")),
       SHOTS)

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 热力图: 逐日补齐 + 日历对齐 + 双主题渲染, 截图见 %s" % SHOTS)
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
