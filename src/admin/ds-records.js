

    openLogDetail(log) { this.selectedLog=log; },
    // 单条请求的 KV 缓存命中率: 命中 token / 总输入 token (与聚合口径一致); 无输入或纯缓存命中显示占位.
    logCacheRate(log) {
      if (log && log.cached) return t('badge.cached');
      const hit=Number(log&&log.prompt_cache_hit_tokens||0), pt=Number(log&&log.prompt_tokens||0);
      if (!pt || hit<=0) return '—';
      return this.fmtPct(hit/pt*100);
    },
    // 单条请求的纯生成速度 (tok/s): 排除排队与 TTFT, 需要首 token 延迟已知的流式请求.
    logGenSpeed(log) {
      if (!log || !log.first_token_ms || log.first_token_ms>=log.latency_ms || !log.completion_tokens) return '—';
      const genMs=log.latency_ms-log.first_token_ms;
      if (genMs<=0) return '—';
      return (log.completion_tokens/genMs*1000).toFixed(1)+' '+t('unit_tokps');
    },
    get filteredLogs() {
      let r = this.logs;
      if (this.logProviderFilter) r = r.filter(l=>l.provider===this.logProviderFilter);
      if (this.logStatusFilter==='success') r = r.filter(l=>!this.isLogError(l));
      if (this.logStatusFilter==='error') r = r.filter(l=>this.isLogError(l));
      if (this.logTimeFilter) {
        const win = {'1h':3600,'24h':86400,'7d':604800}[this.logTimeFilter]||0;
        if (win) { const cutoff = Date.now()/1000 - win; r = r.filter(l=>l.timestamp>=cutoff); }
      }
      if(this.logSearch) {
        const q=this.logSearch.toLowerCase();
        r = r.filter(l=>l.model.toLowerCase().includes(q)||l.provider.toLowerCase().includes(q)||(l.error&&l.error.toLowerCase().includes(q))||String(l.status).includes(q));
      }
      return r;
    },
    resetLogView() { this.logPage = 0; },
    // 数据收缩后把当前页夹回有效范围: 日志缓冲区会滚动淘汰旧记录 (每 3s 轮询刷新),
    // 用户若停在第 5 页而列表缩到 2 页, 原实现会渲染出一个空白表格且毫无提示 ——
    // 看起来像"筛选把结果筛没了", 实际只是页码越界.
    clampLogPage() {
      const max = this.totalLogPages - 1;
      if (this.logPage > max) this.logPage = Math.max(0, max);
      if (this.logPage < 0) this.logPage = 0;
    },
    get totalLogPages() { return Math.ceil(this.filteredLogs.length/this.pageSize)||1; },
    get paginatedLogs() { return this.filteredLogs.slice(this.logPage*this.pageSize,(this.logPage+1)*this.pageSize); },

    // 独立错误列表: 直接来自 /admin/api/errors (已按时间倒序 + 仅错误), 与请求窗口解耦.
    get recordErrorCount() { return this.logs.filter(l=>Number(l.status)>=400 || !!l.error).length; },
    get recordSuccessRate() { return this.logs.length ? (this.logs.length-this.recordErrorCount)/this.logs.length*100 : 0; },
    get recordSuccessRateText() { return this.logs.length ? this.fmtPct(this.recordSuccessRate) : '—'; },
    get recordTokenCount() { return this.logs.reduce((s,l)=>s+(Number(l.prompt_tokens)||0)+(Number(l.completion_tokens)||0),0); },