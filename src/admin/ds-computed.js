// ── 计算属性 ──
// 成功率 / hero 指标 / 结论行 / 余额健康度 / 环图 SVG
// 主要成员: rateColor / sRate / balanceTakeaway / donutSvg
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)


    // Computed
    // 成功率配色阈值: 概览与记录页共用同一套, 避免两处口径分叉
    // (原先记录页把成功率写死 var(--ok), 全是错误时也会显示绿色的 0%).
    // 无数据 → 灰(muted); 判定与 sRateText 的 '—' 一致.
    rateColor(pct, text) {
      // 注意: null/'' 经 Number() 会变成 0, 会被误判为"成功率 0%"的红色 —— 故先排除空值.
      if (text === '—' || pct === null || pct === undefined || pct === ''
          || !Number.isFinite(Number(pct))) return 'var(--muted)';
      const v = Number(pct);
      return v >= 95 ? '#34d399' : v >= 80 ? '#f59e0b' : '#ef4444';
    },
    // 成功率(数值): 供配色阈值使用; 无数据返回 0 (配色另由 sRateText 判为灰).
    // ── 今日成功率 ──
    // 口径与 Hero 主数字一致 (今日) —— 成功率是"今日请求/今日成功"之比, 两个计数器都有, 故可切.
    // 注: 面板里另外两处速率/延迟指标 (平均延迟 / 生成速度) 仍是窗口口径, 原因见 Hero 那段注释.
    get sRate() {
      const d = this.heroTodayStats;
      const n = Number(d.requests || 0);
      if (!n) return 0;
      return Number(d.success || 0) / n * 100;
    },
    // 今日无请求 ≠ "100% 成功" —— 那是没有数据, 与全部成功是两回事.
    get sRateText() {
      if (!Number(this.heroTodayStats.requests || 0)) { this._numSettle('sRate', 0); return '—'; }
      // 小数补间: 切页签/刷新时从 0 滚起 (语义值与计数类同一套重播代数).
      return this.fmtPct(this._numTweenRaw('sRate', this.sRate));
    },
    // ── 面板底部一句话结论 (只陈述已有数字, 百分比走 fmtPct) ──
    get distTopTakeaway() {
      const rows = (this.stats && this.stats.per_model) || [];
      const total = Number((this.stats && this.stats.total_requests) || 0);
      if (!rows.length || !total) return '';
      let top = rows[0];
      for (const r of rows) if ((r.requests || 0) > (top.requests || 0)) top = r;
      const name = top.upstream_model || top.model || '—';
      return t('takeaway_dist_top', name, this.fmtPct((top.requests || 0) / total * 100));
    },
    get rankTopTakeaway() {
      const rows = (this.stats && this.stats.top_models) || [];
      const total = Number((this.stats && this.stats.total_requests) || 0);
      if (!rows.length || !total) return '';
      const top = rows[0];
      const name = top.model || '—';
      return t('takeaway_rank_top', name, top.requests || 0, this.fmtPct((top.requests || 0) / total * 100));
    },
    get healthTakeaway() {
      if (!this.stats || !this.stats.total_requests) return '';
      // 结论句用**原始**成功率, 不走补间 —— 否则整句会跟着数字滚动, 读起来乱.
      return t('takeaway_health', this.fmtPct(this.sRate), this.heroErrors, this.fmtMs(this.stats.avg_latency_ms));
    },
    // ── 余额结论区 (P3) ──
    // 回答"余额这个数意味着什么": 按剩余额给三档色 (充足/偏低/告警), 并把今日消耗折算成
    // "还能用几天"的粗略估计. **只用已有字段** (row.balance + row.today_cost), 不新增后端口径.
    // 无余额配置 (hasBalance=false) 的供应商不参与 —— 不把"没配"混进"没钱".
    balanceTone(bal) {
      const v = Number(bal);
      if (!isFinite(v)) return 'var(--muted)';
      if (v <= 0) return 'var(--err)';
      if (v < 5) return 'var(--warn)';
      return 'var(--ok)';
    },
    get balanceTakeaway() {
      // 积分制行不参与: currency='credits' 与今日费用(金额)量纲不同, 混算「还能撑几天」是错的.
      const rows = (this.providerRows || []).filter(r => r.hasBalance && r.currency !== 'credits');
      if (!rows.length) return '';
      // 最少余额的那家 = 最可能先断供的, 优先提示它; 平手时取今日消耗更高的那家.
      let low = rows[0];
      for (const r of rows) {
        const a = Number(r.balance), b = Number(low.balance);
        if (a < b || (a === b && Number(r.today_cost) > Number(low.today_cost))) low = r;
      }
      const bal = Number(low.balance) || 0;
      const day = Number(low.today_cost) || 0;
      if (day > 0) {
        const days = Math.floor(bal / day);
        return t('balance_takeaway_days', low.provider, this.fmtMoney(bal), days > 999 ? '999+' : String(days));
      }
      return t('balance_takeaway_plain', low.provider, this.fmtMoney(bal));
    },
    // ── 概览 hero 的作用域 ──
    //
    //
    // ── Hero 口径 = 今日 (东八区日界) ──
    // 口径演进(踩过两次, 记下来免得再来回改):
    //   ① 最初只显示"今日" → 每天开面板全是 0 而折线有峰 (数字与图形不同尺度);
    //   ② 上一轮统一改成"窗口口径" → 数字与折线同尺度了, 但**窗口源自日志文件的 5000 行滚动上限**
    //      (store::MAX_LINES), 于是「请求」永久饱和在 5000 —— 用户实测报障, 这不是巧合而是截断;
    //   ③ 现在: 请求/成功/Token/KV 走**后端在写入路径独立累加的今日计数** (`stats.session.today`),
    //      它与文件上限解耦、不受 5000 截断; 折线保持窗口口径(它是趋势形态), 并在提示行**显式标注窗口**。
    // ⚠️ 无法切今日的两项: 「平均延迟」「生成速度」需要逐条日志聚合, 计数器里没有对应累加器,
    //   故仍是窗口口径 —— 这是当前口径的**真实边界**, 不要用注释假装统一。
    get heroTodayStats() { return ((this.stats || {}).session || {}).today || {}; },
    get heroToday() { return Number(this.heroTodayStats.requests || 0) > 0; },
    // 标签文案: 复用既有 today_label (「今日」), 口径说明用新增的 hero_scope_today_hint
    // (原文案 hero_scope_window_hint「今日暂无请求，显示整个统计窗口的累计」已与实际行为不符, 已删除).
    get heroScopeLabel() { return t('today_label'); },
    get heroScopeHint() { return t('hero_scope_today_hint'); },
    get heroRequests() { return Number(this.heroTodayStats.requests || 0); },
    // 今日错误 = 今日请求 − 今日成功 (后端只累加这两个, 不再多维护一个计数器).
    get heroErrors() {
      const d = this.heroTodayStats;
      return Math.max(0, Number(d.requests || 0) - Number(d.success || 0));
    },
    // 窗口口径的**可见**标注: 折线画的是窗口, 故把窗口合计写在主数字提示行里.
    // 原先这个信息藏在「窗口累计」标签里、而数字又被 5000 截断, 等于把口径说了却没说清.
    get heroWindowNote() {
      const s = this.stats || {};
      const n = Number(s.total_requests || 0);
      const days = Number(s.window_minutes || 0) / 1440;
      // 跨度不足 1 天时不带上「最近 N 天」—— 一天内没有跨度的概念, 写了反而是噪声.
      const span = days >= 1 ? ' · ' + t('window_span_days', days.toFixed(1)) : '';
      return t('window_label') + ' ' + this.fmtInt(n) + span;
    },
    get heroRequestsHint() {
      return t('errors_only', this.heroErrors) + ' · ' + this.heroWindowNote;
    },
    get heroCost() { const s=this.stats||{}; return Number(s.total_cost||0); },
    // ── 优化省量卡的口径披露 (修 3 个易误读点) ──
    // ① 今日与本月并排却差 6 个数量级: 数字本身没错 (今日只有少量请求带推理链),
    //    但两卡无任何口径说明, 看着像统计坏了. → 各自标明窗口.
    // ② 本月与累计完全相等 (跨度不足 30 天时必然如此): 并排两个相同大数字无信息量,
    //    且暗示"巧合". → 跨度不足 30 天时不重复显示, 改标"窗口仅 N 天".
    // ③ 今日为 0 时给 '—' 而非 0 (无数据 ≠ 0, 与全站口径一致).
    get todaySavedTokens() {
      const s = this.stats || {};
      return Number(s.today_strip_saved_tokens || 0)
        + Number(s.today_trim_saved_tokens || 0)
        + Number(s.today_resp_cache_saved_tokens || 0);
    },
    get todaySavedText() {
      // 今日无请求 → 无从谈"省了多少", 给占位而非 0
      const s = this.stats || {};
      if (!Number(s.today_requests || 0)) return '—';
      return this.fmtT(this.todaySavedTokens);
    },
    get monthSavedTokens() { return Number((this.stats || {}).month_opt_saved_tokens || 0); },
    get totalSavedTokens() { return Number((this.stats || {}).total_opt_saved_tokens || 0); },
    // 数据窗口实际跨度 (天): 用于判断"本月"是否等同于"窗口全部"
    get savedWindowDays() {
      const s = this.stats || {};
      const mins = Number(s.window_minutes || 0);
      return mins > 0 ? mins / 1440 : 0;
    },
    // 本月卡是否与累计重复: 窗口跨度不足 30 天时, "近 30 天"与"窗口全部"是同一批数据.
    // 此时本月卡会显示与页头累计完全相同的大数字 —— 无信息量, 应改为披露真实跨度.
    get monthSavedDuplicatesTotal() {
      const d = this.savedWindowDays;
      return d > 0 && d < 30 && this.monthSavedTokens === this.totalSavedTokens;
    },
    get monthSavedHint() {
      if (this.monthSavedDuplicatesTotal) {
        return t('opt_saved_window_days', Math.max(1, Math.round(this.savedWindowDays * 10) / 10));
      }
      return t('opt_saved_30d');
    },
    // 费用展示 (三态, 修 bug):
    //   · 未配价 (has_price_config=false)        → '—'
    //   · 配了价且窗口内确实结算到费用            → 金额
    //   · **配了价但本窗口被调用的模型都没配价** → '—' + 明确提示
    // 原实现只看 has_price_config 这一个全局布尔, 于是出现「显示 ¥0.00 却提示
    // '按模型价格估算'」——界面声称按价格算了, 实际一分未算 (实测: 配价 2 个模型,
    // 但 5000 次请求命中的是另外 12 个未配价模型, 故 total_cost=0).
    // 判据用 has_priced_request (后端按"实际被请求的模型是否有价"给出), 缺失时回退旧行为.
    get heroCostText() {
      const s = this.stats || {};
      if (!s.has_price_config) { this._numSettle('heroCost', 0); return '—'; }
      if (s.has_priced_request === false) { this._numSettle('heroCost', 0); return '—'; }
      // 小数补间: 金额保留 2 位 (fmtMoney), 不能走取整的 numTween.
      return this.fmtMoney(this._numTweenRaw('heroCost', this.heroCost));
    },
    // 费用提示: 与上面三态对应, 避免提示与数字互相矛盾.
    get heroCostHint() {
      const s = this.stats || {};
      if (!s.has_price_config) return t('price_unconfigured');
      if (s.has_priced_request === false) return t('price_not_applied');
      return t('cost_estimated');
    },
    get heroPrompt() { return Number(this.heroTodayStats.prompt_tokens || 0); },
    get heroCompletion() { return Number(this.heroTodayStats.completion_tokens || 0); },
    get heroTokens() { return this.heroPrompt + this.heroCompletion; },
    // ── 指标卡的「主值精确 + ≈ 副档缩写」 (对标 cc-switch UsageHero) ──
    // 大字号给**精确值** —— 由模板直接 `fmtInt(numTween('heroTokens',heroTokens))` 输出
    // (要过数字补间, 故不再单开一个 getter), 右侧小胶囊给**量级缩写**:
    // 六位数直接看容易数错位, 缩写一眼抓量级. 两者缺一都不完整.
    get heroTokensApprox() {
      const n = this.heroTokens;
      if (!n || n < 10000) return '';   // 小于 1 万时缩写无信息增量 (原值已够易读)
      return '≈ ' + this.fmtT(n);
    },
    // 费用三态 (未配价 / 未命中配价模型 / 有值) 决定颜色: 无值时用弱化色, 免得"—"被染成警示黄.
    get costTone() {
      const s = this.stats || {};
      if (!s.has_price_config || s.has_priced_request === false) return 'var(--muted)';
      return 'var(--warn)';
    },
    // 命中率口径: 命中 / 总输入 token (与统计页一致). 分母为 0 = 没有数据, 给 '—' 而非 0%.
    get heroCacheText() {
      // 与 Hero 同口径 (今日): 命中 / 今日输入 token.
      const d = this.heroTodayStats;
      const h = Number(d.cache_hit_tokens || 0);
      const pt = Number(d.prompt_tokens || 0);
      if (!pt) { this._numSettle('kvHit', 0); return '—'; }
      // 小数补间: 命中率是百分比, 不能走取整的 numTween.
      return this.fmtPct(this._numTweenRaw('kvHit', h / pt * 100));
    },
    // ── 两项窗口口径指标的小数补间出口 ──
    // 平均延迟 / 生成速度没有"今日"累加器 (需逐条日志聚合), 仍是窗口口径 —— 见 heroTodayStats
    // 上方的口径说明. 它们统一走 _numTweenRaw, 使切页签/刷新时与其它 KPI 一起从 0 滚起.
    get avgLatencyText() {
      const n = Number((this.stats || {}).avg_latency_ms || 0);
      if (!(n > 0)) { this._numSettle('avgLatency', 0); return '—'; }
      return this.fmtMs(this._numTweenRaw('avgLatency', n));
    },
    get genSpeedText() {
      const n = Number((this.stats || {}).gen_speed || 0);
      if (!(n > 0)) { this._numSettle('genSpeed', 0); return '—'; }
      // 保留一位小数, 与原来 `gen_speed.toFixed(1)+' tok/s'` 的终值一致.
      return this._numTweenRaw('genSpeed', n).toFixed(1) + ' tok/s';
    },
    // 转发中断次数 (需关注的那几类之和): 上游中途断流 + 上游 4xx/5xx + 循环掐断
    // + "没出内容就断". 刻意**不含**纯客户端主动取消 —— 那是正常行为 (按 Esc / 工具超时).
    // 用户两次报障"任务跑到一半突然停下", 而证据一直只在日志里 → 搬到面板上.
    get healthInterrupts() {
      const h = (this.stats || {}).health;
      if (!h) return 0;
      return (h.upstream_cut || 0) + (h.upstream_error || 0) + (h.upstream_4xx || 0)
           + (h.loop_truncated || 0) + (h.aborted_no_output || 0);
    },
    // ── 首字延迟 (TTFT) ──
    // 桶上界必须与后端 `rollup::TTFT_BUCKET_MAX_MS` 逐项一致 —— 两份表一旦分叉,
    // 分布图的每一根柱子都会指错档位而不报错 (本项目反复出现的 bug 类型).
    // 末档用 null 表示"无上界", 渲染时显示为 ">60s".
    get ttftBuckets() {
      return [
        { max: 250, label: t('ttft_bucket_0') },
        { max: 500, label: t('ttft_bucket_1') },
        { max: 1000, label: t('ttft_bucket_2') },
        { max: 2000, label: t('ttft_bucket_3') },
        { max: 3500, label: t('ttft_bucket_4') },
        { max: 5000, label: t('ttft_bucket_5') },
        { max: 10000, label: t('ttft_bucket_6') },
        { max: 20000, label: t('ttft_bucket_7') },
        { max: 60000, label: t('ttft_bucket_8') },
        { max: null, label: t('ttft_bucket_9') },
      ];
    },
    // 全局首字延迟汇总 (后端已算好样本数/均值/分位数/直方图, 前端只做渲染与文案).
    // 分位数不下发时由直方图现算 —— 保证旧 exe / 老快照也能显示, 不至于空白.
    get ttftSummary() {
      const raw = (this.stats || {}).ttft || {};
      const hist = Array.isArray(raw.hist) ? raw.hist : [];
      const samples = Number(raw.samples || 0);
      const buckets = this.ttftBuckets;
      // 直方图长度对不上 (接口版本不一致) 时不猜: 直接按"无数据"渲染, 而不是画出半张图.
      if (hist.length !== buckets.length) {
        return { samples: 0, avgMs: null, p50Ms: null, p90Ms: null, hist: [], buckets, rows: [] };
      }
      const total = hist.reduce((s, v) => s + (Number(v) || 0), 0);
      // 分位数: 后端给了就用后端的; 没给才由直方图反推 (取桶上界, 与后端 ttft_quantile 同算法).
      const quantile = q => {
        if (raw.p50_ms != null && q === 0.5) return raw.p50_ms;
        if (raw.p90_ms != null && q === 0.9) return raw.p90_ms;
        if (!total) return null;
        let cum = 0;
        const target = Math.ceil(total * q);
        for (let i = 0; i < hist.length; i++) {
          cum += Number(hist[i]) || 0;
          if (cum >= target) return buckets[i].max;
        }
        return null;
      };
      const rows = buckets.map((b, i) => ({
        k: 'b' + i,
        label: b.label,
        v: Number(hist[i]) || 0,
        pct: total ? (Number(hist[i]) || 0) / total * 100 : 0,
        // 越慢越"热": 前段用 accent(信息色), 尾段转 warn/err —— 让"慢的那一撮"自己跳出来.
        color: i <= 2 ? 'var(--accent)' : i <= 5 ? 'var(--info-fg)' : i <= 7 ? 'var(--warn)' : 'var(--err)',
      }));
      return {
        samples,
        avgMs: raw.avg_ms != null ? raw.avg_ms : (samples ? Number(raw.avg_ms || 0) : null),
        p50Ms: quantile(0.5),
        p90Ms: quantile(0.9),
        hist,
        buckets,
        rows,
      };
    },
    get ttftHasData() { return this.ttftSummary.samples > 0; },

    // ─── 时间去哪了 (窗口级, 方案 F 2026-10-13) ───
    // 与单条记录详情的「时间去哪了」同口径、同配色 (waiting=灰 / reasoning=紫 / output=蓝 / tool=绿)。
    // ⚠️ 平均用 **Σ毫秒 ÷ 样本数** 逐段求 (不是对各请求的速率取平均 —— 那会让长尾权重被抹平,
    //    与 gen_speed_of「Σtoken÷Σ秒」同一条口径纪律)。
    // ⚠️ 未采集的样本不计入样本数: 于是"有首字样本 100、有正文样本 60"是可能且如实的。
    get stageSummary() { return (this.stats && this.stats.stage_summary) || {}; },
    get stageHasData() { return (this.stageSummary.waiting_samples || 0) > 0; },
    get stageWinRows() {
      const s = this.stageSummary;
      const mk = (k, key, sumKey, nKey, color) => {
        const n = s[nKey] || 0;
        return { k, label: t(key), n, avg: n > 0 ? Math.round((s[sumKey] || 0) / n) : null, color };
      };
      return [
        mk('waiting', 'stage_waiting', 'waiting_sum_ms', 'waiting_samples', 'var(--muted)'),
        mk('reasoning', 'stage_reasoning', 'reasoning_sum_ms', 'reasoning_samples', 'var(--secondary)'),
        mk('output', 'stage_output', 'output_sum_ms', 'output_samples', 'var(--accent)'),
        mk('tool', 'stage_tool_call', 'tool_call_sum_ms', 'tool_call_samples', 'var(--ok)'),
      ];
    },

    // ─── 用量热力图 (2026-10-11, 对标 dsh-our-free-model) ───
    //
    // 数据来自 `/admin/api/heatmap`（后端按日级 rollup 逐日补齐），前端只负责画格子。
    // **逐日连续**是硬要求: 缺的那天必须占一格（值为 0），否则日历会错位 ——
    // 这与"无数据 ≠ 0"并不矛盾: 这里 0 的含义是"那天确实没有请求"（rollup 每天都有账），
    // 而"无数据"是"我们没测到"（如旧日志缺字段），两者在日历上必须区分:
    // 有账但为 0 → 最浅的空格; 无账（区间外）→ 不画。
    get heatmapDays() { return this.heatmap && this.heatmap.days ? this.heatmap.days : []; },
    get heatmapTotal() { return (this.heatmap && this.heatmap.total_tokens) || 0; },
    // 分位阈值: 用**非零日**的分布切四档 —— 用全区间切的话, 一旦有一天爆量,
    // 其余所有天都会落进最低档, 整张图看起来"全都没用过"(实测常见形态)。
    _hmThresholds() {
      const vals = this.heatmapDays.map(d => d.tokens).filter(v => v > 0).sort((a, b) => a - b);
      if (!vals.length) return [0, 0, 0];
      const q = p => vals[Math.min(vals.length - 1, Math.floor(vals.length * p))];
      return [q(0.25), q(0.5), q(0.75)];
    },
    hmLevel(tokens) {
      if (!(tokens > 0)) return 0;
      const [a, b, c] = this._hmThresholds();
      if (tokens <= a) return 1;
      if (tokens <= b) return 2;
      if (tokens <= c) return 3;
      return 4;
    },
    // 四档颜色都走 token（§5.5 红线: 不得出现 rgba 字面量）。
    // ⚠️ 只能引用**本页已经在用**的 token：`theme_dark_diff` 会比对"本页生效的颜色集合"，
    // 引入一个本页此前没出现过的 token（如 `--fill-4`）会被判成"新增颜色"而报 DIFF
    // —— 这不是形式主义，它挡的正是"随手引入新色导致浅色主题下没人验过"。
    // 本页既有: --accent / --border / --fg / --fill-5 / --muted。故用它们表达四档。
    hmLevelColor(level) {
      const c = { 1: 'var(--border)', 2: 'var(--accent)', 3: 'var(--accent)', 4: 'var(--accent)' };
      return c[level] || 'var(--border)';
    },
    // 四档用**同一支强调色 + 递增不透明度**表达深浅。
    // ⚠️ 递增必须严格单调: 第一版写成 {1:1, 2:.35, 3:.62, 4:1} ——
    //    第 1 档与第 4 档都满不透明, 于是"最少"和"最多"看起来一模一样,
    //    整张图只剩两种颜色, "哪天花得多"这个目的就废了（截图核对才发现）。
    hmLevelOpacity(level) { return { 1: 0.22, 2: 0.45, 3: 0.70, 4: 1.0 }[level] || 1; },
    // 日历网格: **列 = 周, 行 = 星期**（与 GitHub 贡献图同构）。
    // 首列要对齐到该周周日, 否则同一行的几天会跨到不同星期。
    heatmapGridSvg() {
      const days = this.heatmapDays;
      if (!days.length) return '';
      const CELL = 12, GAP = 3, STEP = CELL + GAP;
      const first = new Date(days[0].day_start * 1000);
      const lead = first.getDay();               // 0=周日
      const n = days.length + lead;
      const weeks = Math.ceil(n / 7);
      const W = weeks * STEP, H = 7 * STEP;
      const maxTok = Math.max(...days.map(d => d.tokens), 0);
      let cells = '';
      days.forEach((d, i) => {
        const idx = i + lead;
        const col = Math.floor(idx / 7), row = idx % 7;
        const lv = this.hmLevel(d.tokens);
        const dt = new Date(d.day_start * 1000);
        const label = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} · ${this.fmtT(d.tokens)}`;
        // 用 rect 而非 div: 一次性 x-html 注入, 无需 Alpine 逐格绑定 (361 格下性能差异明显)。
        // 空格用 --border（本页已有）, 不用 --fill-4 —— 见 hmLevelColor 的说明。
        const op = lv === 0 ? 1 : this.hmLevelOpacity(lv);
        const fill = lv === 0 ? 'var(--border)' : this.hmLevelColor(lv);
        cells += `<rect x="${col * STEP}" y="${row * STEP}" width="${CELL}" height="${CELL}" rx="2.5" `
               + `fill="${fill}" fill-opacity="${op}"><title>${label}</title></rect>`;
      });
      // 月度刻度: 每周一列, 只在该列含"1 号"时打标 (避免每列都写月份)。
      return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">${cells}</svg>`;
    },
    // 月份标签与网格**按周对齐**: 每个标签占一列宽, 只有该列起始那周跨月才显示文字。
    heatmapMonthLabels() {
      const days = this.heatmapDays;
      if (!days.length) return '';
      const STEP = 15;
      const first = new Date(days[0].day_start * 1000);
      const lead = first.getDay();
      const weeks = Math.ceil((days.length + lead) / 7);
      const MN = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12'];
      let out = '', lastMonth = -1;
      for (let w = 0; w < weeks; w++) {
        const dayIdx = w * 7 - lead;
        const d = days[Math.max(0, Math.min(days.length - 1, dayIdx))];
        if (!d) continue;
        const dt = new Date(d.day_start * 1000);
        const m = dt.getMonth();
        if (m !== lastMonth && dayIdx >= 0) {
          lastMonth = m;
          out += `<span style="width:${STEP}px">${MN[m]}${t('hm_month_suffix')}</span>`;
        } else {
          out += `<span style="width:${STEP}px"></span>`;
        }
      }
      return out;
    },
    // 一句话结论: 只说分布里的事实（活跃天数 / 峰值日）, 不推断原因。
    get heatmapTakeaway() {
      const days = this.heatmapDays;
      if (!days.length) return '';
      const active = days.filter(d => d.tokens > 0).length;
      if (!active) return t('heatmap_takeaway_empty', days.length);
      let peak = days[0];
      for (const d of days) if (d.tokens > peak.tokens) peak = d;
      const pd = new Date(peak.day_start * 1000);
      const pds = `${pd.getFullYear()}-${String(pd.getMonth() + 1).padStart(2, '0')}-${String(pd.getDate()).padStart(2, '0')}`;
      return t('heatmap_takeaway', active, days.length, pds, this.fmtT(peak.tokens));
    },
    // 平均首字延迟文本: 无样本 → '—' (与 fmtMs 的 "无数据 ≠ 0" 同口径).
    get ttftAvgText() {
      const s = this.ttftSummary;
      if (!(s.avgMs > 0)) { this._numSettle('ttftAvg', 0); return '—'; }
      return this.fmtMs(this._numTweenRaw('ttftAvg', s.avgMs));
    },
    get ttftP50Text() {
      const v = this.ttftSummary.p50Ms;
      return v == null ? '—' : this.fmtMs(v);
    },
    get ttftP90Text() {
      const v = this.ttftSummary.p90Ms;
      return v == null ? '—' : this.fmtMs(v);
    },
    // 一句话结论: 只陈述分布里已有的事实, 不推断原因.
    // 用 p90 (而非均值) 说"慢的那部分" —— 均值会被少数极慢样本拉高, 不能代表多数请求.
    get ttftTakeaway() {
      const s = this.ttftSummary;
      if (!s.samples) return '';
      const p50 = s.p50Ms, p90 = s.p90Ms;
      if (p50 == null) return '';
      // "超过 X" 的那一撮: 取 >10s 的两档之和 (10s 以上是明显可感知的等待).
      const slow = s.rows.slice(6).reduce((sum, r) => sum + r.v, 0);
      const slowPct = slow ? this.fmtPct(slow / s.samples * 100) : '0%';
      return t('ttft_takeaway', this.fmtMs(p50), slowPct, this.fmtMs(10000));
    },
    // 超 60 秒的样本数 (单独提示: 这一档几乎都对应上游排队或空等, 不是"模型慢").
    get ttftSlowCount() {
      const s = this.ttftSummary;
      if (!s.samples || !s.rows.length) return 0;
      return s.rows[s.rows.length - 1].v;
    },
    get donutSvg() {
      if(!this.stats||!this.stats.per_model.length) return '';
      const total=this.stats.total_requests||1;
      const r=55, w=20, cx=70, cy=70, C=2*Math.PI*r;
      const top=this.stats.per_model.slice(0,8);
      const shown=top.reduce((sum,m)=>sum+(m.requests||0),0);
      const parts=top.map((m,i)=>({value:m.requests||0,color:this.donutColors[i%this.donutColors.length]}));
      // 注: 这里必须用真实色值 —— 结果会作为 SVG 呈现属性 stroke="..." 输出, CSS 变量在该位置不解析。
      if(this.stats.per_model.length>8 && total>shown) parts.push({value:total-shown,color:'#3b3d48',rest:true});
      let offset=0, segs='';
      parts.forEach(p=>{
        const len=p.value/total*C;
        const gap=parts.length>1?2.5:0;
        const vis=Math.max(0.5,len-gap);
        // 汇总扇区带 class, 让底色随主题走 (--chart-rest); 数据系列色仍为内联
        const cls=p.rest?' class="chart-rest-fill"':'';
        segs+=`<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${p.color}" stroke-width="${w}" stroke-dasharray="${vis} ${C}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${cx} ${cy})" stroke-linecap="butt"${cls}/>`;
        offset+=len;
      });
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke-width="${w}" class="chart-track"/>${segs}<text x="${cx}" y="${cy-7}" text-anchor="middle" font-size="17" class="donut-total">${total}</text><text x="${cx}" y="${cy+11}" text-anchor="middle" class="donut-sub">${t('unit_requests')}</text>`;
    },