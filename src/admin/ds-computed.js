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
    get heroCostText() { return (this.stats&&this.stats.has_price_config) ? this.fmtMoney(this.heroCost) : '—'; },
    get heroPrompt() { const s=this.stats||{}; return Number(s.total_prompt_tokens||0); },
    get heroCompletion() { const s=this.stats||{}; return Number(s.total_completion_tokens||0); },
    get heroTokens() { return this.heroPrompt + this.heroCompletion; },
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