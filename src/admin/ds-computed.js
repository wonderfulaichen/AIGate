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
    // 口径与**同面板**的「平均延迟」「生成速度」保持一致, 都用窗口累计 ——
    // 原先此处用今日、而旁边两项用窗口, 同一张卡片里混了两个尺度.
    get sRate() {
      if(!this.stats||!this.stats.total_requests) return 0;
      return (this.stats.total_requests-(this.stats.error_count||0))/this.stats.total_requests*100;
    },
    // 窗口内无请求 ≠ "100% 成功" —— 那是没有数据, 与全部成功是两回事.
    get sRateText() {
      if(!this.stats||!this.stats.total_requests) return '—';
      return this.fmtPct(this.sRate);
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
      return t('takeaway_health', this.sRateText, this.heroErrors, this.fmtMs(this.stats.avg_latency_ms));
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
      const rows = (this.providerRows || []).filter(r => r.hasBalance);
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
    // **主数字一律用「窗口累计」口径**, 与卡片里的迷你折线严格同口径
    // (折线画的就是 stats.trends 整个窗口) —— 这是本面板最看重的一条: 数字与图形不得打架.
    //
    // 历史: 最初六个数只显示"今日", 每天开始打开面板全是 0 而折线有峰 (口径不一致);
    // 上一版改成"今日有请求就显示今日", 修好了全 0, 但引入了新矛盾 ——
    // 今日有请求时主数字是今日口径 (例: 22), 旁边的折线却仍是窗口口径 (峰值可达数百),
    // 同一张卡片里两个尺度. 且注释声称"现在数字与图形同口径", 与实现不符.
    // 现统一为窗口口径, 今日用量以文字形式附在提示里 (见 heroRequestsHint), 不再抢主位.
    get heroToday() { return Number((this.stats&&this.stats.today_requests)||0) > 0; },
    get heroScopeLabel() { return t('window_label'); },
    get heroScopeHint() { return t('hero_scope_window_hint'); },
    get heroRequests() { const s=this.stats||{}; return Number(s.total_requests||0); },
    get heroErrors() { const s=this.stats||{}; return Number(s.error_count||0); },
    get heroRequestsHint() {
      // 提示只补主数字**没有**的信息: 主数字已是"请求总数", 故这里只给错误数,
      // 不再重复一遍总数 (原先渲染成「错误 70 · 总计 2619」而主数字正是 2619,
      // 同屏把同一个数说了两遍, 白占提示行三分之一).
      // 今日有请求时把今日用量附在末尾 —— 它是补充, 不与主数字混排取值.
      const base = t('errors_only', this.heroErrors);
      return this.heroToday
        ? base + ' · ' + t('today_label') + ' ' + Number((this.stats && this.stats.today_requests) || 0)
        : base;
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
      if (!s.has_price_config) return '—';
      if (s.has_priced_request === false) return '—';
      return this.fmtMoney(this.heroCost);
    },
    // 费用提示: 与上面三态对应, 避免提示与数字互相矛盾.
    get heroCostHint() {
      const s = this.stats || {};
      if (!s.has_price_config) return t('price_unconfigured');
      if (s.has_priced_request === false) return t('price_not_applied');
      return t('cost_estimated');
    },
    get heroPrompt() { const s=this.stats||{}; return Number(s.total_prompt_tokens||0); },
    get heroCompletion() { const s=this.stats||{}; return Number(s.total_completion_tokens||0); },
    get heroTokens() { return this.heroPrompt + this.heroCompletion; },
    // ── 指标卡的「≈ 副档位」与「大数字」 (对标 cc-switch UsageHero) ──
    // 手法: 主数字给精确值, 副档位给一个**更易读的量级** (如 44,500 → ≈44.5K).
    // 这样既保留精确性, 又让人一眼抓住量级 —— 原实现只有精确值, 六位数要自己数字位.
    get heroTokensApprox() {
      const n = this.heroTokens;
      if (!n || n < 10000) return '';   // 小于 1 万时不副档 (原值已够易读, 加了反而啰嗦)
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
      const s=this.stats||{};
      const h=Number(s.total_cache_hit_tokens||0);
      const pt=Number(s.total_prompt_tokens||0);
      if(!pt) return '—';
      return this.fmtPct(h/pt*100);
    },
    get donutSvg() {
      if(!this.stats||!this.stats.per_model.length) return '';
      const total=this.stats.total_requests||1;
      const r=55, w=20, cx=70, cy=70, C=2*Math.PI*r;
      const top=this.stats.per_model.slice(0,8);
      const shown=top.reduce((sum,m)=>sum+(m.requests||0),0);
      const parts=top.map((m,i)=>({value:m.requests||0,color:this.donutColors[i%this.donutColors.length]}));
      // 注: 这里必须用真实色值 —— 结果会作为 SVG 呈现属性 stroke="..." 输出, CSS 变量在该位置不解析。
      if(this.stats.per_model.length>8 && total>shown) parts.push({value:total-shown,color:'#3b3d48'});
      let offset=0, segs='';
      parts.forEach(p=>{
        const len=p.value/total*C;
        segs+=`<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${p.color}" stroke-width="${w}" stroke-dasharray="${len} ${C}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${cx} ${cy})" stroke-linecap="butt"/>`;
        offset+=len;
      });
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#2a2d37" stroke-width="${w}"/>${segs}<text x="${cx}" y="${cy-8}" text-anchor="middle" fill="#cdd0d4" font-size="16" font-weight="700">${total}</text><text x="${cx}" y="${cy+10}" text-anchor="middle" fill="#6b7280" font-size="10">${t('unit_requests')}</text>`;
    },