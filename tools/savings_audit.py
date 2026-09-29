# -*- coding: utf-8 -*-
"""省量的真实数据审计: 三省法各省多少、折算多少金额、占总花费多少。

用途: 产品演示若要以"省量"为核心卖点, 数字必须站得住 —— 先看真实日志再说怎么写。
"""
import io, json, os, sys, collections
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
print("日志条数: %d" % len(rows))

FIELDS = ["strip_saved_tokens", "trim_saved_tokens", "resp_cache_saved_tokens",
          "strip_toolcall_saved_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
          "prompt_cache_creation_tokens", "prompt_tokens", "completion_tokens"]
print("\n=== 各字段总量 ===")
for k in FIELDS:
    tot = sum(int(d.get(k) or 0) for d in rows)
    nz = sum(1 for d in rows if int(d.get(k) or 0) > 0)
    print("  %-30s 合计 %16s  非零 %5d 条 (%.1f%%)"
          % (k, format(tot, ","), nz, 100.0 * nz / max(1, len(rows))))

# 省量三类
strip = sum(int(d.get("strip_saved_tokens") or 0) for d in rows)
trim = sum(int(d.get("trim_saved_tokens") or 0) for d in rows)
rcache = sum(int(d.get("resp_cache_saved_tokens") or 0) for d in rows)
print("\n=== 三类省量 ===")
print("  剥离推理链 %s tokens" % format(strip, ","))
print("  历史裁剪   %s tokens" % format(trim, ","))
print("  响应缓存   %s tokens" % format(rcache, ","))
print("  合计       %s tokens" % format(strip + trim + rcache, ","))

# 输入规模与命中率
pt = sum(int(d.get("prompt_tokens") or 0) for d in rows)
hit = sum(int(d.get("prompt_cache_hit_tokens") or 0) for d in rows)
miss = sum(int(d.get("prompt_cache_miss_tokens") or 0) for d in rows)
ct = sum(int(d.get("completion_tokens") or 0) for d in rows)
print("\n=== 输入/输出规模 ===")
print("  prompt 合计  %s" % format(pt, ","))
print("  其中命中     %s  (%.2f%%)" % (format(hit, ","), 100.0 * hit / max(1, hit + miss)))
print("  其中未命中   %s" % format(miss, ","))
print("  completion   %s" % format(ct, ","))

# 金额: 分别按命中价与未命中价估算 (缺省价: 命中 0.04, 未命中 2.0 元/M —— 见 pricing 内置表)
print("\n=== 省量折算金额 (两种口径) ===")
for name, tk in [("剥离", strip), ("裁剪", trim), ("响应缓存", rcache), ("合计", strip + trim + rcache)]:
    print("  %-8s 按命中价 0.04元/M = %7.2f 元   |   按未命中价 2.0元/M = %8.2f 元"
          % (name, tk / 1e6 * 0.04, tk / 1e6 * 2.0))

# 实付估算 (对照, 说明省量占多大比重)
paid = miss / 1e6 * 2.0 + hit / 1e6 * 0.04 + ct / 1e6 * 8.0
print("\n=== 口径对照 ===")
print("  按上列默认价粗估实付: %.2f 元" % paid)
print("  省量(命中价口径)占实付: %.1f%%" % (100.0 * (strip + trim + rcache) / 1e6 * 0.04 / max(0.01, paid)))
print("  省量(未命中价口径)占实付: %.1f%%" % (100.0 * (strip + trim + rcache) / 1e6 * 2.0 / max(0.01, paid)))

# ── 结构化输出 ──
# 供产品演示 (gen_deck.py) 直接读取: 演示稿里的"实测数字"必须与审计脚本同源,
# 否则日志一涨, 稿子里的数字就悄悄过期 (已踩过一次: 3.84亿 → 4.03亿)。
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "savings_audit.json")
tc = sum(int(d.get("strip_toolcall_saved_tokens") or 0) for d in rows)
summary = {
    "logs": len(rows),
    "strip": strip, "trim": trim, "resp_cache": rcache, "total_saved": strip + trim + rcache,
    "strip_toolcall": tc,
    "strip_plain": strip - tc,
    "toolcall_share_pct": 100.0 * tc / max(1, strip),
    "prompt_tokens": pt, "cache_hit": hit, "cache_miss": miss, "completion": ct,
    "hit_rate_pct": 100.0 * hit / max(1, hit + miss),
    "in_out_ratio": (pt / max(1, ct)),
    "saved_share_of_input_pct": 100.0 * (strip + trim + rcache) / max(1, pt),
    "req_with_savings_pct": 100.0 * sum(
        1 for d in rows
        if (int(d.get("strip_saved_tokens") or 0) + int(d.get("trim_saved_tokens") or 0)
            + int(d.get("resp_cache_saved_tokens") or 0)) > 0) / max(1, len(rows)),
}
io.open(OUT, "w", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False, indent=2))
print("\n已写出 %s" % OUT)
