// ── 供应商汇总表 (2026-10-24) ──
// 按供应商聚合 + 展开看该供应商下的模型。
// 主要成员: filteredProviderStats / provModelsOf / toggleProvTable / provSuccessText
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)
//
// ⚠️ 口径纪律（这些是踩过的坑, 别改回去）:
//   · 成功率 = (requests - errors) / requests。**不额外减 429** ——
//     后端把 429 单独计到 `rate_limited`、不计入 `errors`
//     （"上游让你慢点" ≠ "请求失败"）。再减一次会把限流误算成成功。
//   · requests = 0 → 显示 '—'，不是 0% —— "没请求过"与"全都失败"是两件事。
//   · 缓存命中率 = hit/(hit+miss)，分母 0 → '—'（未采集 ≠ 0）。

    // 供应商汇总表的搜索过滤（按名字，大小写不敏感）
    get filteredProviderStats() {
      const all = (this.stats && this.stats.per_provider) || [];
      const q = String(this.provTableFilter || '').trim().toLowerCase();
      if (!q) return all;
      return all.filter(p => String(p.provider || '').toLowerCase().includes(q));
    },

    // 某供应商下的模型（取自 per_model 的同 provider 行，按请求数降序）。
    // 为什么不另建聚合: per_model 本来就带 provider 字段，重算一遍会得到两份口径。
    provModelsOf(provider) {
      const rows = (this.stats && this.stats.per_model) || [];
      return rows
        .filter(m => m.provider === provider)
        .slice()
        .sort((a, b) => (Number(b.requests) || 0) - (Number(a.requests) || 0));
    },

    // 展开/收起某个供应商。**复用** topoOpenProviders —— 拓扑图与这张表说的是
    // 同一件事（哪个供应商在拖后腿），两套状态各记一份必然漂移。
    // ⚠️ 必须重建对象再赋值：Alpine 3 不追踪动态新增属性（改属性点不更新）。
    toggleProvTable(provider) {
      const cur = this.topoOpenProviders || {};
      this.topoOpenProviders = Object.assign({}, cur, { [provider]: !cur[provider] });
    },

    // 成功率: 见文件头口径纪律。null = 无请求，显示 '—' 而不是 0%（未采集 ≠ 0）
    provSuccessText(p) {
      const n = Number(p && p.requests) || 0;
      if (n <= 0) return '—';
      const errs = Number(p && p.errors) || 0;
      const pct = Math.max(0, Math.min(100, ((n - errs) / n) * 100));
      return pct.toFixed(1) + '%';
    },
    provSuccessTone(p) {
      const n = Number(p && p.requests) || 0;
      if (n <= 0) return 'var(--muted)';
      const errs = Number(p && p.errors) || 0;
      const pct = Math.max(0, Math.min(100, ((n - errs) / n) * 100));
      if (pct >= 99) return 'var(--ok)';
      if (pct >= 95) return 'var(--warn)';
      return 'var(--err)';
    },
