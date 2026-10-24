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

    // ── 搜索框防抖（2026-10-24）──
    // 记录页搜索早就做了 200ms 防抖 + IME 组合输入安全（ds-records.js:270-293），
    // 但另外 3 个搜索框都是 `x-model` 直连 computed getter，每敲一个键就全量重算：
    //   · modelDetailFilter → filteredModelStats（遍历 per_model 全部 50 条 + aliases 匹配）
    //   · provTableFilter   → filteredProviderStats
    //   · provSearch        → visibleProviders（含 models.some 逐模型匹配，模板里被引用 7 处）
    // 中文输入法下每敲一个拼音字母都会触发一次，输入明显发滞。
    //
    // 实现：草稿(_Draft)实时跟手，提交值(真正的过滤条件)延迟 200ms 才更新。
    // 与记录页同一口径（行数少时 200ms 比 300ms 更跟手，且已有实现就是 200ms，
    // 不为了对齐文档的 300ms 而制造两套手感）。IME 期间挂起，上屏立即提交。
    _debounceSearch(field, value, draftKey) {
      this[draftKey] = value;                       // 草稿：输入框立即回显
      if (this._searchComposing) return;            // 拼音未上屏：不过滤
      const timerKey = '_t_' + field;
      clearTimeout(this[timerKey]);
      this[timerKey] = setTimeout(() => { this[field] = value; }, 200);
    },
    onProvTableSearchInput(e, draftKey) {
      this._debounceSearch('provTableFilter', (e && e.target) ? e.target.value : '', draftKey);
    },
    onModelDetailSearchInput(e) {
      this._debounceSearch('modelDetailFilter', (e && e.target) ? e.target.value : '', 'modelDetailFilterDraft');
    },
    onProvSearchInput(e) {
      this._debounceSearch('provSearch', (e && e.target) ? e.target.value : '', 'provSearchDraft');
    },
    // IME 组合输入：期间只更新草稿（由 x-model 负责），不提交过滤
    onSearchCompositionStart() { this._searchComposing = true; },
    // 上屏：立即提交（组合已定稿，不必再等 200ms）
    onSearchCompositionEnd(field, draftKey) {
      this._searchComposing = false;
      clearTimeout(this['_t_' + field]);
      this[field] = this[draftKey] || '';
    },

    // 供应商汇总表的搜索过滤（按名字，大小写不敏感）
    get filteredProviderStats() {
      const all = (this.stats && this.stats.per_provider) || [];
      const q = String(this.provTableFilter || '').trim().toLowerCase();
      if (!q) return all;
      return all.filter(p => String(p.provider || '').toLowerCase().includes(q));
    },

    // ── 供应商卡的快速统计（2026-10-24，对照 WaLiAPI 的 ChannelsPage.tsx:459-478）──
    // WaLiAPI 在渠道卡右侧并排显示「⚡调用数 / 成功率 87% / 延迟 120ms」，都带图标。
    // 我们此前卡上没有 —— 真因不是没设计，而是**进供应商页不拉 stats**
    // （已修：ds-core.js 的 switchTab 里补了 fetchStats）。
    //
    // 返回 null 表示"该供应商还没有统计数据" —— 卡片据此**不渲染**统计区，
    // 而不是显示 0/0%/0ms（未采集 ≠ 零）。这是本项目一贯的口径纪律。
    provQuickStats(name) {
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      if (!p || !(Number(p.requests) > 0)) return null;
      const n = Number(p.requests) || 0;
      const errs = Number(p.errors) || 0;
      const okPct = Math.max(0, Math.min(100, (n - errs) / n * 100));
      return {
        calls: n,
        okPct,
        okText: okPct.toFixed(okPct >= 99.95 ? 0 : 1) + '%',
        // 成功率着色与供应商明细表同阈值（99/95），保持两处口径一致
        okTone: okPct >= 99 ? 'var(--ok)' : okPct >= 95 ? 'var(--warn)' : 'var(--err)',
        latency: Number(p.avg_latency_ms) || 0,
      };
    },

    // 最近一次失败的**原因**（供供应商行显示"为什么全红"）。
    //
    // 返回 null 的条件有两个，分别对应两种不同处境，都不能显示成"有错误"：
    //   ① 该家没有统计数据 / 压根没有请求 → 未采集；
    //   ② 有数据但一条错都没有 → 健康。此时也不能显示绿色"正常"，因为
    //      "没有错误"既可能是真健康，也可能只是窗口内样本太少 —— 该判断属于
    //      供应商明细表的成功率列，行上只回答"有没有已知的失败原因"。
    //
    // 后端只在 logs 窗口内取最新一条错误；跨日的 rollup 不含错误正文，
    // 所以历史失败但窗口内全成功时这里会返回 null（未采集 ≤ 无错误）。
    provLastError(name) {
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      if (!p) return null;
      const msg = p.last_error;
      if (msg == null) return null;
      const s = String(msg).trim();
      return s ? s : null;
    },

    // 行上只放**能分辨问题类型**的那一小段：状态码优先 + 正文开头。
    // 为什么不直接显示全文: 上游错误常是一整页 HTML/JSON（实测 opencode 404
    // 返回 HTML 页面），行高会被撑到几十像素以上，把 10 行列表变成 5 行。
    // 全文仍在 title / aria-label 里，悬停或读屏可拿到。
    provErrShort(name) {
      const full = this.provLastError(name);
      if (!full) return '';
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      const code = p && p.last_error_status ? String(p.last_error_status) : '';
      // 正文压成单行、去掉空白折行，再截到 28 字符（含状态码约 34）
      const flat = full.replace(/\s+/g, ' ').trim();
      const cut = flat.length > 28 ? flat.slice(0, 28) + '…' : flat;
      return code ? code + ' ' + cut : cut;
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

    // ── 模型明细表"首屏 10 行 + 展开更多" (2026-10-24) ──
    // 实测: per_model 有 **50 条**(含历史 rollup 聚合项), 全渲染 = 3432px / 3.3 屏,
    // 占掉 cost 页签(5286px)的三分之二。用户扫这张表是看"谁慢/谁贵",
    // 前 10 条已覆盖绝大部分请求量, 其余按需展开。
    //
    // ⚠️ 实现要点(踩过坑): 不要写成 `get modelRowsShown() { return this.filteredModelStats.slice(...) }`
    // 这种**嵌套 getter** —— filteredModelStats 本身要遍历整个 per_model 做匹配,
    // 而 Alpine 每次重渲染都会重新求值, 50 行 × 每行多次引用 = 卡死页面(实测浏览器直接挂住)。
    // 这里改成**普通方法 + 由模板显式调用一次**。
    // ⚠️ 也不能在文件顶部写 `const MODEL_ROWS_INITIAL` —— 本文件是 dashboard() 的
    // **对象体片段**（被 concat! 拼进对象内部），在对象里写 const 会直接抛 ReferenceError。
    // 首屏行数就是下面这个方法里的字面量 10，改它只改一处。
    modelRowsShown() {
      const all = this.filteredModelStats || [];
      const limit = this.modelRowsAll ? all.length : 10;
      return all.length > limit ? all.slice(0, limit) : all;
    },
    // 还有多少行没显示（0 = 不用给展开按钮）
    modelRowsHiddenCount() {
      const n = (this.filteredModelStats || []).length;
      return this.modelRowsAll ? 0 : Math.max(0, n - 10);
    },

    // 这张表的展开态**不复用** topoOpenProviders —— 那个默认值是 true（拓扑要一眼看全），
    // 而复用会让首次进入分析页时 12 家供应商的模型全部展开，表格撑到 2270px / 3.6 屏
    // （实测过）。这里单独维护，**默认折叠**：先看清有哪几家，再按需下钻。
    provTableOpen(provider) {
      const v = this.provTableExpanded && this.provTableExpanded[provider];
      return v === true;   // 未设置 = 折叠
    },
    toggleProvTable(provider) {
      const cur = this.provTableExpanded || {};
      // ⚠️ 必须重建对象再赋值：Alpine 3 不追踪动态新增属性（改属性点不刷新）
      this.provTableExpanded = Object.assign({}, cur, { [provider]: !this.provTableOpen(provider) });
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
