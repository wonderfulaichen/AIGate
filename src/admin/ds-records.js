// ── 记录页 ──
// 筛选/搜索/排序/列显隐/分页 + 单条详情与派生指标
// 主要成员: filteredLogs / toggleLogSort / pageNumbers / logCacheRate
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)


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
      // P3: 点表头排序. 排序在**筛选之后**、分页之前 —— 否则只排当前页, 看着像乱序.
      // 默认不排序 (保持后端返回的时间倒序), 点一次才启用.
      if (this.logSortKey) {
        const k = this.logSortKey, dir = this.logSortDir === 'asc' ? 1 : -1;
        r = r.slice().sort((a, b) => {
          const va = logSortValue(a, k), vb = logSortValue(b, k);
          if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
          return String(va).localeCompare(String(vb)) * dir;
        });
      }
      return r;
    },
    // 点表头: 同列再点切换升/降; 换列则从降序开始 (日志最常看"最大/最慢").
    toggleLogSort(key) {
      if (!key) return;
      if (this.logSortKey === key) this.logSortDir = this.logSortDir === 'desc' ? 'asc' : 'desc';
      else { this.logSortKey = key; this.logSortDir = 'desc'; }
      this.resetLogView();
    },
    logSortIcon(key) {
      if (this.logSortKey !== key) return '';
      return this.logSortDir === 'desc' ? '↓' : '↑';
    },
    // 列显隐 (P3): 记住偏好, 便于窄屏只留关心的列. 至少保留一列, 避免全隐藏后白屏.
    toggleLogCol(k) {
      const next = !this.logColsShown[k];
      const wouldKeep = Object.keys(this.logColsShown).filter(x => x !== k && this.logColsShown[x]);
      if (!next && wouldKeep.length === 0) { toast(t('log_cols_min'), 'warn'); return; }
      this.logColsShown[k] = next;
      try { localStorage.setItem('aigate.logCols', JSON.stringify(this.logColsShown)); } catch (e) {}
    },
    restoreLogCols() {
      const def = { status: true, error: true, model: true, reasoning: true, provider: true, tokens: true, cache: true, latency: true };
      try {
        const s = JSON.parse(localStorage.getItem('aigate.logCols') || 'null');
        if (s && typeof s === 'object') {
          // 只接受已知列, 防止旧版本残留的键名把新版列弄丢
          for (const k in def) if (typeof s[k] === 'boolean') def[k] = s[k];
        }
      } catch (e) {}
      this.logColsShown = def;
    },
    // 复位到第 1 页 (筛选/搜索提交时调用); 顺带播报新的结果数 (既有文案 total_count, 无新增键).
    resetLogView() { this.logPage = 0; this._announceRecords(); },
    // 每页条数 (P3): 改后回到第 1 页 (否则可能落到越界页) 并全局记忆.
    // 只接受白名单内的值 —— 否则组件状态取非法值而持久化被拒, 两者不一致.
    setPageSize(n) {
      const v = parseInt(n, 10);
      if (PAGE_SIZE_OPTIONS.indexOf(v) < 0) return;
      this.pageSize = v;
      savePageSize(v);
      this.resetLogView();
    },
    // 页码省略算法 (纯函数在 core.js) —— 模板里直接调, 保持渲染与算法分离.
    pageNumbers(cur, total) { return pageNumbers(cur, total); },
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