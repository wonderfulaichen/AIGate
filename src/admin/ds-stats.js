
    async fetchStats(rangeOverride)   {
      try {
        const range=rangeOverride || (this.activeTab==='dashboard'?'window':(this.analyticsRange||'1d'));
        const params=new URLSearchParams({granularity:this.trendGranularity,range});
        const r=await fetch('/admin/api/stats?'+params.toString(), {headers:authHeaders()});
        if(r.ok) {
          this.stats=await r.json();
          this.loading=false;
          this.statsError=false;
          this.fetchModelMeta(); await this.$nextTick();
          this.computeModelTrend();
          if(this.$refs.trendScroll){ this.$refs.trendScroll.scrollLeft=this.$refs.trendScroll.scrollWidth; }
        } else {
          // 非 2xx 也是失败: 必须复位 loading, 否则界面停在假的「加载中」.
          this.loading=false; this.statsError=true;
        }
      } catch(e) {
        // 网络异常同理 —— 原实现这里是空 catch, 导致 loading 永远为 true.
        this.loading=false; this.statsError=true;
      }
    },
    retryFetchStats() { this.statsError=false; this.loading=true; this.fetchStats(); },
    isLogError(log) { return Number(log&&log.status)>=400 || !!(log&&log.error); },
    get analysisWindowMinutes() {
      return Math.max(1, Number(this.stats&&this.stats.window_minutes||1));
    },
    // 活跃跨度(分钟): 窗口内首个到最后一个**有数据**的桶.
    // 用整个窗口当分母会把大量空闲时段算进去 —— 实测 29 天窗口算出"平均 RPM 0.06",
    // 读不出任何信息; 按实际活跃区间算才有意义.
    get analysisActiveMinutes() {
      const all=(this.stats&&this.stats.trends)||[];
      const live=all.filter(x=>x&&x.ts&&((x.requests||0)>0||(x.total_prompt_tokens||0)>0||(x.total_completion_tokens||0)>0));
      if(live.length<2) return this.analysisWindowMinutes;
      const bucket=Math.max(1,(all[1].ts||0)-(all[0].ts||0));
      // 钳到窗口以内: 桶跨度按整天向外取整 (日粒度下首桶从当天 0 点算起), 会略大于真实数据跨度;
      // 活跃跨度不可能超过窗口本身, 否则算出的 RPM 比按整窗口算的还低, 反而失真.
      return Math.max(1, Math.min(this.analysisWindowMinutes, (live[live.length-1].ts-live[0].ts+bucket)/60));
    },
    get analysisRpm() { return Number(this.stats&&this.stats.total_requests||0)/this.analysisActiveMinutes; },
    get analysisTpm() { return ((Number(this.stats&&this.stats.total_prompt_tokens||0)+Number(this.stats&&this.stats.total_completion_tokens||0))/this.analysisActiveMinutes); },
    get filteredModelStats() {
      const q=(this.modelDetailFilter||'').toLowerCase().trim();
      return (this.stats&&this.stats.per_model||[]).filter(m=>{
        if(this.modelProviderFilter && this.modelProviderName(m)!==this.modelProviderFilter) return false;
        return !q || (m.model||'').toLowerCase().includes(q) || (m.upstream_model||'').toLowerCase().includes(q) || (m.provider||'').toLowerCase().includes(q) || (m.aliases||[]).some(a=>a.toLowerCase().includes(q));
      });
    },
    modelProviderName(model) { return (model&&model.provider)||'—'; },
    modelErrorCount(model) { return Number(model&&model.errors||0); },
    get modelDetailProviders() { return [...new Set((this.stats&&this.stats.per_model||[]).map(m=>this.modelProviderName(m)))].sort(); },
    // 统一时间范围与粒度: 小时(24h)/天(30d)/月(12m). 每个选项同时决定窗口与桶大小, 避免两套控件重叠混乱.
    setSpan(span) {
      this.analyticsSpan = span;
      const map = { hour: ['1d','hour'], day: ['29d','day'], month: ['365d','month'] };
      const m = map[span];
      if (m) { this.analyticsRange = m[0]; this.trendGranularity = m[1]; }
      try { localStorage.setItem('aigate.analyticsSpan', span); } catch (e) { /* privacy mode */ }
      this.fetchStats();
    },
    // 恢复上次选择的时间范围; 非法值回退 day. 仅恢复状态, 不额外 fetch (走既有首载).
    restoreAnalyticsSpan() {
      const map = { hour: ['1d','hour'], day: ['29d','day'], month: ['365d','month'] };
      let span = 'day';
      try { span = localStorage.getItem('aigate.analyticsSpan') || 'day'; } catch (e) { span = 'day'; }
      if (!map[span]) span = 'day';
      this.analyticsSpan = span;
      this.analyticsRange = map[span][0];
      this.trendGranularity = map[span][1];
    },
    // ── 更新亮点：取最新一条 changelog 作为弹窗内容 ──
    get whatsNewEntry() {
      return (this.changelog && this.changelog.length) ? this.changelog[0] : null;
    },