    // ── opencode 套餐额度（2026-10-24）──
    //
    // ## 为什么没有"刷新额度"按钮
    //
    // opencode **不提供额度查询接口**。官方机制是每条响应在
    // `usage.opencode.allowance` 里报告"本请求计了多少额度"，用户去 Console 看总账。
    // 所以我们的额度是**本地累加**出来的，不是查来的 —— 加个"刷新"按钮只会骗人，
    // 因为它刷新不出任何新数据（数据只随我们自己发请求增长）。
    //
    // ## 额度表的时效
    //
    // 月额度是官方公布的固定值（GLM-5.3=$15、GLM-5.3-Flash=$60…），
    // 抄录在 `src/opencode_quota.rs`，带 `TABLE_DATE`。
    // 官方明说额度会调整，所以面板必须**显示抄录日期**，让用户能判断是否过期。

    // 官方三档比例：5h = 月额度 20%、周 = 50%、月 = 100%。
    // 与 `opencode_quota::window_limits` 同一口径（那边算上限，这边算百分比）。
    // 重复一份是刻意的：前端要用它做除法，不宜为一个除法再拉一次接口。
    // ⚠️ 改比例必须两处同改（后端有单测钉住 20/50/100）。
    _quotaRatios: { fiveHour: 0.20, weekly: 0.50, monthly: 1.0 },

    // 某供应商的 opencode 额度明细（按消耗降序，后端已排序）。
    // 返回 `null` 表示**该家没有额度数据**（非 opencode / 旧日志）——
    // 前端据此整块不渲染，而不是画一条 0% 的假进度条。
    provQuotaRows(name) {
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      if (!p || !p.opencode_quota || !p.opencode_quota.length) return null;
      return p.opencode_quota;
    },

    // 某供应商是否**是 opencode 系**（有额度数据即可判定）。
    // 用它决定是否显示"额度"入口 —— 没有数据的家用不着这个入口。
    provHasQuota(name) {
      return !!this.provQuotaRows(name);
    },

    // 把「已用美元」换算成某档窗口的**已用百分比**（0~100，可 >100 表示超额）。
    //
    // 返回 `null` 的条件：
    //  · 该模型不在额度表里（`monthly_usd` 缺失）→ 算不出百分比，不猜；
    //  · 模型不限量（后端已滤掉，这里兜底）。
    //
    // 注意**不做 clamp**：超额（200%）必须如实显示，那是"该充值/换模型"的信号，
    // 夹到 100% 会让用户以为刚好用完。
    quotaPct(usedUsd, monthlyUsd, windowKey) {
      if (!monthlyUsd || monthlyUsd <= 0) return null;
      const r = this._quotaRatios[windowKey];
      if (!r) return null;
      const limit = monthlyUsd * r;
      if (limit <= 0) return null;
      return (Number(usedUsd) || 0) / limit * 100;
    },

    // 百分比 → 色调。阈值沿用项目既有口径（与供应商成功率同一套 99/95 思路）：
    // 这里语义相反（越少越好），故用 80/100 两档：
    //  · < 80%  绿（还有余量）
    //  · >= 80% 琥珀（快用完）
    //  · >= 100% 红（已超额）
    quotaTone(pct) {
      if (pct == null) return 'var(--muted)';
      if (pct >= 100) return 'var(--err)';
      if (pct >= 80) return 'var(--warn)';
      return 'var(--ok)';
    },

    // 百分比文案。**未采集显示 "—"**，不显示 0%。
    quotaPctText(usedUsd, monthlyUsd, windowKey) {
      const pct = this.quotaPct(usedUsd, monthlyUsd, windowKey);
      if (pct == null) return '—';
      // ⚠️ **不能**用 `this.fmtPct()`（2026-10-24 实测）：
      // 它是给「缓存命中率 / 成功率」这类**上限天然是 100%** 的指标设计的，
      // 第 294 行 `if (v >= 100) return '100%'` 会把 112.5% 压成 "100%"。
      // 而额度消耗**超额是真实且重要的状态** —— 把它显示成 "100%" 会让用户
      // 以为"刚好用完"，实际已经超支、可能已被限流。
      // 故额度用自己的格式化：超额如实给出一位小数。
      if (pct >= 100) return pct.toFixed(1) + '%';
      if (pct <= 0) return '0%';
      if (pct < 0.1) return '<0.1%';
      return pct.toFixed(1) + '%';
    },

    // 额度表的抄录日期（后端 `TABLE_DATE`，经 stats 透传或硬编码回退）。
    // 为什么要显示：官方明说额度会调整，不标日期的话用户无法判断这份百分比是否可信。
    quotaTableDate() {
      const s = this.stats && this.stats.opencode_quota_table_date;
      return s || '';
    },
