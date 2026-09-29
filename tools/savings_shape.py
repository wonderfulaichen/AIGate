# -*- coding: utf-8 -*-
"""推理链省量的**真实分布**分析 —— 为产品演示提供可站得住的数字。

不看总量, 看形状: 省量集中度、单请求量级、带 tool_calls 那层的占比、以及
「不省的那 34.6% 请求是什么」—— 这些才是能讲清楚的依据。
"""
import io, json, os, statistics, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⚠️ 机器相关: 读的是本机部署实例的真实请求日志 (在仓库之外), 故保留绝对路径
LOGS = r"D:\Office software\Development Project\AI中转\data\logs.jsonl"
rows = []
with io.open(LOGS, encoding="utf-8", errors="replace") as f:
    for ln in f:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass

ok = [r for r in rows if r.get("status") == 200]
print("日志 %d 条, 其中 200 成功 %d 条" % (len(rows), len(ok)))


def g(r, k):
    return int(r.get(k) or 0)


strip = [g(r, "strip_saved_tokens") for r in ok]
tc = [g(r, "strip_toolcall_saved_tokens") for r in ok]
pt = [g(r, "prompt_tokens") for r in ok]

# 1) 省量集中度: 多少比例的请求贡献了多少比例的省量
saved_pairs = sorted([s for s in strip if s > 0], reverse=True)
total_saved = sum(saved_pairs)
print("\n=== 省量集中度 (仅看有省量的请求) ===")
print("  有省量的请求: %d 条 (%.1f%%), 共省 %s tokens"
      % (len(saved_pairs), 100.0 * len(saved_pairs) / max(1, len(ok)), format(total_saved, ",")))
acc = 0
for frac in (0.05, 0.10, 0.20, 0.50):
    n = max(1, int(len(saved_pairs) * frac))
    seg = sum(saved_pairs[:n])
    print("  省量最大的 %4.0f%% 请求 (前 %d 条) 贡献了 %5.1f%% 的省量"
          % (frac * 100, n, 100.0 * seg / max(1, total_saved)))

# 2) 单请求量级分层 —— "中位数 11.7 万" 这类数字才让人有概念
nz = [s for s in strip if s > 0]
if nz:
    print("\n=== 有省量请求的量级分布 (tokens) ===")
    for p in (10, 25, 50, 75, 90, 99):
        print("  p%-3d = %10s" % (p, format(int(statistics.quantiles(nz, n=100)[p - 1]), ",")))
    print("  中位数 = %s   平均 = %s   最大 = %s"
          % (format(int(statistics.median(nz)), ","), format(int(statistics.mean(nz)), ","),
             format(max(nz), ",")))

# 3) 两层各自的量级 (默认层 vs tool_calls 层)
plain = [s - t for s, t in zip(strip, tc)]
tot_tc, tot_plain = sum(tc), sum(plain)
print("\n=== 两层剥离的量级 ===")
print("  tool_calls 层   %s tokens  (%.1f%% of 剥离总量)"
      % (format(tot_tc, ","), 100.0 * tot_tc / max(1, tot_tc + tot_plain)))
print("  默认层(普通)    %s tokens  (%.1f%%)"
      % (format(tot_plain, ","), 100.0 * tot_plain / max(1, tot_tc + tot_plain)))
print("  → 说明: 大头来自带 tool_calls 的那些轮次, 而它们默认**仅 chat 协议**剥")

# 4) 省量占该请求输入的比例 —— 剥离到底占上下文多大比重
print("\n=== 剥离量占该请求输入的比例 (有省量请求) ===")
ratios = sorted(100.0 * s / p for s, p in zip(strip, pt) if s > 0 and p > 0)
if ratios:
    print("  中位数 %.1f%%   p25 %.1f%%   p75 %.1f%%   p90 %.1f%%"
          % (statistics.median(ratios), ratios[len(ratios) // 4],
             ratios[len(ratios) * 3 // 4], ratios[int(len(ratios) * 0.9)]))

# 5) 输入量级: 长会话的上下文有多大 (说明"重复搬运"的规模)
if pt:
    print("\n=== 单请求输入 tokens 分布 (全部成功请求) ===")
    q = statistics.quantiles(pt, n=100)
    for p in (25, 50, 75, 90, 99):
        print("  p%-3d = %10s" % (p, format(int(q[p - 1]), ",")))
    print("  最大 = %s" % format(max(pt), ","))
    big = sum(1 for x in pt if x >= 100000)
    print("  ≥10 万 tokens 输入的请求: %d 条 (%.1f%%)  ← 长会话的典型量级"
          % (big, 100.0 * big / len(pt)))

# 6) 按模型看
print("\n=== 按模型: 省量前 8 (tokens 总量) ===")
agg = {}
for r in ok:
    m = r.get("model") or "?"
    a = agg.setdefault(m, [0, 0, 0])
    a[0] += g(r, "strip_saved_tokens")
    a[1] += 1
    a[2] += g(r, "strip_toolcall_saved_tokens")
for m, (s, n, t) in sorted(agg.items(), key=lambda x: -x[1][0])[:8]:
    print("  %-42s 省 %12s  (%d 次请求, 其中 tool_calls 层 %s)"
          % (m[:42], format(s, ","), n, format(t, ",")))

# ── 结构化输出 (供产品演示读取) ──
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "savings_shape.json")
_nz = sorted([s for s in strip if s > 0])
_pt = [p for p in pt if p > 0]
summary = {
    "logs": len(rows), "ok": len(ok),
    "req_with_savings": len(saved_pairs),
    "req_with_savings_pct": 100.0 * len(saved_pairs) / max(1, len(ok)),
    "saved_total": total_saved,
    # 集中度: 省量最大的前 N% 请求贡献了多少比例
    "top20_pct_share": 100.0 * sum(saved_pairs[:max(1, int(len(saved_pairs) * 0.2))]) / max(1, total_saved),
    "top10_count": max(1, int(len(saved_pairs) * 0.1)),
    "top10_share": 100.0 * sum(saved_pairs[:max(1, int(len(saved_pairs) * 0.1))]) / max(1, total_saved),
    # 单请求省量量级
    "median_saved": int(statistics.median(_nz)) if _nz else 0,
    "p90_saved": int(statistics.quantiles(_nz, n=100)[89]) if _nz else 0,
    "max_saved": max(_nz) if _nz else 0,
    # 剥离占该请求输入的比重
    "median_strip_ratio": statistics.median([100.0 * s / p for s, p in zip(strip, pt) if s > 0 and p > 0])
    if any(s > 0 and p > 0 for s, p in zip(strip, pt)) else 0.0,
    "p90_strip_ratio": statistics.quantiles(
        sorted(100.0 * s / p for s, p in zip(strip, pt) if s > 0 and p > 0), n=100)[89]
    if any(s > 0 and p > 0 for s, p in zip(strip, pt)) else 0.0,
    # 输入量级
    "median_input": int(statistics.median(_pt)) if _pt else 0,
    "p90_input": int(statistics.quantiles(_pt, n=100)[89]) if _pt else 0,
    "big_input_pct": 100.0 * sum(1 for x in _pt if x >= 100000) / max(1, len(_pt)),
    # 两层
    "toolcall_tokens": tot_tc,
    "toolcall_share": 100.0 * tot_tc / max(1, tot_tc + tot_plain),
    "plain_tokens": tot_plain,
    "plain_share": 100.0 * tot_plain / max(1, tot_tc + tot_plain),
    # 省量前几名模型
    "top_models": [{"model": m, "saved": s, "reqs": n} for m, (s, n, _t)
                   in sorted(agg.items(), key=lambda x: -x[1][0])[:5] if s > 0],
}
io.open(OUT, "w", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False, indent=2))
print("\n已写出 %s" % OUT)
