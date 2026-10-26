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

    // 该供应商是否**含不按量计费（套餐/免费）的模型**（2026-10-24）。
    //
    // ⚠️ 设计修正：第一版用 `ProviderStats.billing === false`（整家全免费）判断，
    // 实测**永远不会成立** —— billing 的口径是「组内**任一**中转 ID 不在免费名单
    // 即视为计费」，而真实的 10 家里每家都至少有一个计费模型，
    // 故 provider 级 billing 恒为 True。徽章因此永远不显示（探针当场抓到）。
    //
    // 正确的判据是「这家**有没有**不按量计费的模型」：只要有一个，模型明细里
    // 那个模型的「已省」就会是 "—"，徽章正是为了解释它。
    // 数据源用 per_model 的 billing（模型级，才是真正决定 "—" 的粒度）。
    provIsFlatRate(name) {
      const rows = (this.stats && this.stats.per_model) || [];
      return rows.some(m => m.provider === name && m.billing === false);
    },

    // 该供应商下不按量计费的模型数（徽章 title 里说清"几个"，避免以偏概全）。
    provFlatRateCount(name) {
      const rows = (this.stats && this.stats.per_model) || [];
      return rows.filter(m => m.provider === name && m.billing === false).length;
    },

    // 最近一次失败的**原因**（供供应商行显示"为什么全红"）。
    //
    // 返回 null 的条件有两个，分别对应两种不同处境，都不能显示成"有错误"：
    //   ① 该家没有统计数据 / 压根没有请求 → 未采集；
    //   ② 有数据但一条错都没有 → 健康。此时也不能显示绿色"正常"，因为
    //      "没有错误"既可能是真健康，也可能只是窗口内样本太少 —— 该判断属于
    //      供应商明细表的成功率列，行上只回答"有没有已知的失败原因"。
    //
    // 数据来源有两处（2026-10-24）：logs 窗口内的最新错误，以及 rollup 落盘的
    // 「该日最近一次错误」。后者让「记录已滚出窗口」的供应商（如 zen）也能显示原因。
    provLastError(name) {
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      if (!p) return null;
      const msg = p.last_error;
      if (msg == null) return null;
      const s = String(msg).trim();
      return s ? s : null;
    },

    // 失败原因的**可操作建议**（2026-10-24，对照参考项目的 invalidationText 思路）。
    //
    // 为什么需要: 只给原文「429 [quota_exceeded]: 该免费模型今…」，用户看完
    // 仍然不知道该做什么。参考项目把 reason 映射成「去哪、做什么」。
    //
    // 纪律：**永远保留原文**。这里只额外给一句建议，不替换证据 ——
    // 映射不上时返回 ''（模板据此不渲染建议行），绝不编一句通用话术，
    // 否则用户会把我们的猜测当成上游的结论。
    provErrAdvice(name) {
      const full = this.provLastError(name);
      if (!full) return '';
      const rows = (this.stats && this.stats.per_provider) || [];
      const p = rows.find(x => x.provider === name);
      const code = Number(p && p.last_error_status) || 0;
      const raw = String(full);
      const low = raw.toLowerCase();

      // ⚠️ 局部变量绝不能叫 `t` —— 会遮蔽全局的翻译函数 `t()`，
      // 表现为整个页面 `TypeError: t is not a function`（探针抓到过）。

      // 顺序要紧：先判"额度"，再判"限流" —— 两者都是 429，
      // 但"额度耗尽"必须换号/充值，"频率超限"只需等一会儿，处置完全不同。
      if (code === 402 || /quota_exceeded|insufficient|余额不足|额度已用尽|额度不足/i.test(raw)) {
        return t('prov_advice_quota');
      }
      if (code === 429) {
        if (/rate|频率|稍后|too many|rpm|tpm/i.test(raw)) return t('prov_advice_rate');
        return t('prov_advice_quota');
      }
      if (code === 401 || code === 403) return t('prov_advice_auth');
      // 渠道校验（WorkBuddy 国际版实测 400 + code 11-128）：必须重新登录。
      if (/11-128|request illegal|渠道校验/i.test(raw)) return t('prov_advice_relogin');
      // 404 + HTML 页面：端点路径变了（我们踩过 opencode 把 /zen/go 改成 /go）。
      if (code === 404) {
        return /<html|<!doctype|text\/html/i.test(low) ? t('prov_advice_endpoint') : t('prov_advice_notfound');
      }
      if (/tls|handshake|unexpected eof|certificate/i.test(low)) return t('prov_advice_network');
      if (code >= 500) return t('prov_advice_upstream');
      return '';
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

    // 失败原因悬停提示 = **建议在前、原文在后**。
    // 原文必须完整保留（截断只在行上做），否则排障时无证据可查。
    provErrTitle(name) {
      const full = this.provLastError(name);
      if (!full) return '';
      const advice = this.provErrAdvice(name);
      return advice ? advice + '\n\n原文：' + full : full;
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

    // ── 模型行"首屏 10 行 + 展开更多" ──
    // 实测: per_model 有 **50 条**(含历史 rollup 聚合项), 全渲染 = 3432px / 3.3 屏。
    // 用户扫这张表是看"谁慢/谁贵", 前 10 条已覆盖绝大部分请求量, 其余按需展开。
    //
    // ⚠️ 实现要点(踩过坑): 不要写成 getter（`get xxx() { return this.filteredModelStats.slice(...) }`）
    // —— filteredModelStats 本身要遍历整个 per_model 做匹配, 而 Alpine 每次重渲染
    // 都会重新求值, 50 行 × 每行多次引用 = 卡死页面(实测浏览器直接挂住)。
    // 必须写成**普通方法 + 由模板显式调用一次**。
    // ⚠️ 也不能在文件顶部写 `const XXX = 10` —— 本文件是 dashboard() 的
    // **对象体片段**（被 concat! 拼进对象内部），在对象里写 const 会直接抛 ReferenceError。
    //
    // 2026-10-27 合并进供应商表后：全局的 modelRowsShown / modelRowsHiddenCount /
    // modelRowsAll 已无消费方（模型表现为供应商的展开区，逐家分页），故删除。
    // 逐家版本见下方 provModelsShown / provModelsHiddenCount。

    // ── 供应商展开区里的模型行（2026-10-27 合并模型明细后新增）──
    //
    // 合并后这里就是原来的「模型明细表」，但**每一家单独限制首屏 10 行** ——
    // 一家可能有 40+ 个模型（实测 per_model 50 条里同一家最多 40 余条），
    // 展开一次就把 3 屏全撑出来，比原来两张表还糟。
    //
    // 与 modelRowsShown 的区别：那个是全局一张表的分页；这里是**逐供应商**的，
    // 所以用 `perProvModelAll[provider]` 记各自的"看全部"状态，
    // 不能用同一个布尔（那会让点开 A 家的"更多"，B 家也跟着全展开）。
    //
    // ⚠️ 同样必须是**普通方法**而不是 getter（见 modelRowsShown 上方的踩坑说明：
    //    嵌套 getter 会让 Alpine 每次重渲染重算整张表，实测卡死浏览器）。
    provModelsShown(provider) {
      const all = this.provModelsOf(provider) || [];
      const open = (this.perProvModelAll || {})[provider];
      const limit = open ? all.length : 10;
      return all.length > limit ? all.slice(0, limit) : all;
    },
    provModelsHiddenCount(provider) {
      const n = (this.provModelsOf(provider) || []).length;
      if ((this.perProvModelAll || {})[provider]) return 0;
      return Math.max(0, n - 10);
    },
    toggleProvModels(provider) {
      if (!this.perProvModelAll) this.perProvModelAll = {};
      // Alpine 3 不追踪既有对象的深层属性新增 → 整体替换一次触发重渲染
      this.perProvModelAll = Object.assign({}, this.perProvModelAll, {
        [provider]: !this.perProvModelAll[provider],
      });
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
