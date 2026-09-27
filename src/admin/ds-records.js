

    openLogDetail(log) { this._lastFocus = document.activeElement; this.selectedLog = log; },
    // 关闭详情: 焦点还给打开它的行 (键盘用户不迷路; 节点若已被轮询重建则静默跳过).
    closeLogDetail() {
      this.selectedLog = null;
      const f = this._lastFocus; this._lastFocus = null;
      if (f && f.focus) this.$nextTick(() => { try { f.focus(); } catch (e) {} });
    },
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
    // 复位到第 1 页 (筛选/搜索提交时调用); 顺带播报新的结果数 (既有文案 total_count, 无新增键).
    resetLogView() { this.logPage = 0; this._announceRecords(); },
    // 分页跳转 (键盘/按钮统一入口): 夹取越界后播报「第 p/共 N 页 · 共 M 条」.
    gotoLogPage(p) {
      this.logPage = p;
      this.clampLogPage();
      this._announceRecords();
    },
    // 记录页状态播报: 全部由既有文案与数字组成, 不新增 i18n 键.
    _announceRecords() {
      const total = this.filteredLogs.length;
      this.liveMsg = t('total_count', total) + ' · ' + (this.logPage + 1) + '/' + this.totalLogPages;
    },
    // ── 记录页搜索 (IME 安全) ──
    // 普通输入: 防抖 200ms 提交过滤 (值从 $event.target 取, 不依赖 x-model 的更新顺序).
    onLogSearchInput(e) {
      if (this.logSearchComposing) return;                 // 拼音未上屏: 只留草稿, 不过滤
      const v = (e && e.target) ? e.target.value : this.logSearchDraft;
      clearTimeout(this._logSearchT);
      this._logSearchT = setTimeout(() => { this.logSearch = v; this.resetLogView(); }, 200);
    },
    // 组合输入期间: 草稿由 x-model 实时更新, 过滤挂起 (compositionupdate 不做提交).
    onLogSearchCompositionStart() { this.logSearchComposing = true; clearTimeout(this._logSearchT); },
    onLogSearchCompositionUpdate() {},
    // 上屏: 立即提交 (组合已定稿, 无需等防抖).
    onLogSearchCompositionEnd() {
      this.logSearchComposing = false;
      clearTimeout(this._logSearchT);
      this.logSearch = this.logSearchDraft;
      this.resetLogView();
    },
    // 清空搜索 (筛选条重置按钮等): 草稿与提交值同步清零, 取消挂起的防抖.
    clearLogSearch() {
      clearTimeout(this._logSearchT);
      this.logSearchDraft = ''; this.logSearch = '';
      this.resetLogView();
    },
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