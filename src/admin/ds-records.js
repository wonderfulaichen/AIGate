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
    // 单条请求的纯生成速度 (tok/s): 排除排队与 TTFT, 并**剔除思考 token**.
    // 判据全部收敛在 core.js 的 genSpeedText (与后端 gen_speed_of 同口径) ——
    // 这里原先自己写了一份 `.completion_tokens/genMs`, 漏了剔除思考, 实测虚高最高 204 倍
    // (且纯思考行会显示一个根本不存在的速度)。不要再在这里复刻判据。
    logGenSpeed(log) { return genSpeedText(log); },
    // 本轮"为什么结束"的人话 (2026-10-11 新增; 用户第四次报"跑一会就停下"之后补的可见性).
    //
    // 为什么非要单开一列: 三种"停止"在别的字段上**完全同形** —— 都可能是 `200 + 无错误`,
    // 于是用户只能猜。只有这个字段能把它们分开:
    //   · 上游报的 finish_reason (stop / tool_calls / length / …) = 上游交代了原因;
    //   · `done` = 上游只发了 [DONE]、**没说**为什么停 (最可疑的一类, 此前与正常收尾同形);
    //   · `eof`  = 上游连终止帧都没发就断了;
    //   · `loop` / `idle` / `error_event` / `abort` = 网关或链路侧的主动行为。
    // 缺字段一律显示 '—' (口径红线: 无数据 ≠ 0, 更不得猜成"正常结束")。
    logEndReason(log) {
      const k = log && log.end_reason;
      if (!k) return '—';
      const map = {
        stop: 'end_stop', tool_calls: 'end_tool_calls', function_call: 'end_tool_calls',
        length: 'end_length', content_filter: 'end_content_filter',
        done: 'end_done', eof: 'end_eof', loop: 'end_loop', abort: 'end_abort',
        idle: 'end_idle', error_event: 'end_error_event', cache: 'end_cache',
        // 上游中途断流（2026-10-13）：原先与 idle 合并成一句含糊的
        // 「空闲超时 / 上游连接错误」，用户只能从延迟数字反推是哪一种。
        upstream_cut: 'end_upstream_cut',
      };
      const key = map[k];
      // 未知取值照实显示: 上游可能报出新枚举, 吞掉它就等于把线索埋了。
      return key ? t(key) : k;
    },
    // ── 「时间去哪了」(方案 F, 2026-10-13) ──
    // 四段耗时拆分。判据: null = 未采集 → "—"; 0 = 实测为 0 → "0ms" (走 msText)。
    // 只有流式路径采集得到 (非流式/缓存回放/原生直通为整块 null)。
    //
    // ⚠️ 本文件里的格式化一律走 `this.xxx`: fmtMs/msText 是 dashboard 对象的**方法**,
    //    不是全局函数 —— 裸调会抛 ReferenceError, 而 Alpine 对表达式异常是**静默吞掉**的
    //    (DOM 保持空), 表现就是"比例条和图例永远空白"。这一处曾真的这样错了很久
    //    (2026-10-15 用 headless dump-dom 抓 window.__ERRS 才定位到)。
    _stageSegs(log) {
      const s = (log && log.stages) || {};
      return [
        { k: 'waiting', label: t('stage_waiting'), v: s.waiting_first_ms, color: 'var(--muted)' },
        { k: 'reasoning', label: t('stage_reasoning'), v: s.reasoning_ms, color: 'var(--secondary)' },
        { k: 'output', label: t('stage_output'), v: s.output_ms, color: 'var(--accent)' },
        { k: 'tool', label: t('stage_tool_call'), v: s.tool_call_ms, color: 'var(--ok)' },
      ];
    },
    hasStageData(log) {
      return this._stageSegs(log).some(s => s.v != null);
    },
    // 堆叠比例条: 只把**已采集**的段按比例铺开。全部未采集时不画 (外层 x-show 已挡住)。
    // 复用现有 `.prop-bar` 样式 (分段比例条, 与分析页同形), 不新增 CSS。
    stageBarHtml(log) {
      const segs = this._stageSegs(log).filter(s => s.v != null);
      const total = segs.reduce((a, s) => a + Math.max(0, Number(s.v) || 0), 0);
      if (!segs.length || total <= 0) return '<span class="empty"></span>';
      return segs.map(s => {
        const pct = Math.max(0, Number(s.v) || 0) / total * 100;
        return '<span style="flex-grow:' + Math.max(pct, 0.5).toFixed(2) + ';background:' + s.color + '" title="'
          + s.label + ' ' + this.msText(s.v) + '"></span>';
      }).join('');
    },
    stageLegendHtml(log) {
      return this._stageSegs(log).map(s => {
        // 未采集的段照实显示 "—" 并压暗 —— 不写 0, 也不隐藏 (隐藏会让用户以为该段不存在);
        // 实测为 0 的段照常给色给值 (0ms), 与"没测到"分得开。
        const missing = s.v == null;
        const val = this.msText(s.v);
        const style = 'display:inline-flex;align-items:center;gap:5px'
          + (missing ? ';opacity:.55' : '');
        const dot = 'width:7px;height:7px;border-radius:var(--rs);flex:0 0 7px;background:'
          + (missing ? 'var(--muted)' : s.color);
        return '<span style="' + style + '"><i style="' + dot + '"></i>' + s.label
          + ' <b class="mono">' + val + '</b></span>';
      }).join('');
    },
    // ── 网关去程瀑布 (2026-10-15) ──
    // 与「时间去哪了」的区别: 那四段是**客户端视角**(等首字/思考/正文/工具), 首段里就包着
    // 整段网关去程; 这五段是**网关视角**, 互不重叠, 顺序铺满"进门 → 上游响应头":
    //   路由/鉴权 → 隐私护栏 → 协议转换 → 其他网关工作 → 上游首字节
    // 「其他网关工作」= overhead − (路由+护栏+转换): 注入/瘦身/缓存键等零散工作。
    // 用**减法**而不是再挂一个计时器 —— 三者本就是 overhead 的子集, 分开测还得对齐边界。
    _gwSegs(log) {
      const s = (log && log.stages) || {};
      const num = v => (v == null ? null : Number(v) || 0);
      const route = num(s.route_ms), guard = num(s.guard_ms), conv = num(s.convert_ms), oh = num(s.overhead_ms);
      // 差值在子段之和 > overhead 时夹到 0: 各计时器独立取整, 允许 1-2ms 漂移,
      // 不为它编一个负数出来 (负段宽会画出诡异的条)。
      const other = oh == null ? null : Math.max(0, oh - ((route || 0) + (guard || 0) + (conv || 0)));
      return [
        { k: 'route', label: t('gw_route'), v: route, color: 'var(--muted)' },
        { k: 'guard', label: t('gw_guard'), v: guard, color: 'var(--info-fg)' },
        // convert 的 null 有两种含义, 靠 na 措辞分开: 直通(没转换) vs 其他段没测到。
        { k: 'convert', label: t('gw_convert'), v: conv, color: 'var(--secondary)', na: t('gw_convert_direct') },
        { k: 'gw_other', label: t('gw_other'), v: other, color: 'var(--neutral-fg)' },
        { k: 'ttfb', label: t('gw_ttfb'), v: num(s.upstream_ttfb_ms), color: 'var(--warn-fg)' },
      ];
    },
    hasGatewayData(log) { return this._gwSegs(log).some(s => s.v != null); },
    gwBarHtml(log) {
      const segs = this._gwSegs(log).filter(s => s.v != null && s.v > 0);
      const total = segs.reduce((a, s) => a + s.v, 0);
      if (!segs.length || total <= 0) return '<span class="empty"></span>';
      return segs.map(s => {
        const pct = s.v / total * 100;
        return '<span style="flex-grow:' + Math.max(pct, 0.5).toFixed(2) + ';background:' + s.color + '" title="'
          + s.label + ' ' + this.msText(s.v) + '"></span>';
      }).join('');
    },
    gwLegendHtml(log) {
      return this._gwSegs(log).map(s => {
        // 未采集 → "—"; 协议转换例外: null 是"直通"(确实没转换), 措辞必须不同。
        const missing = s.v == null;
        const val = missing ? (s.na || '—') : this.msText(s.v);
        const style = 'display:inline-flex;align-items:center;gap:5px' + (missing ? ';opacity:.55' : '');
        const dot = 'width:7px;height:7px;border-radius:var(--rs);flex:0 0 7px;background:'
          + (missing ? 'var(--muted)' : s.color);
        return '<span style="' + style + '"><i style="' + dot + '"></i>' + s.label
          + ' <b class="mono">' + val + '</b></span>';
      }).join('');
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
    // 与 isLogError 同口径 (499 不算错误) —— 不要再写一份 status>=400 的内联判据.
    get recordErrorCount() { return this.logs.filter(l=>this.isLogError(l)).length; },
    get recordSuccessRate() { return this.logs.length ? (this.logs.length-this.recordErrorCount)/this.logs.length*100 : 0; },
    // 成功率(文本): 走小数补间 —— 切页签/刷新时与其它 KPI 一起从 0 滚起.
    get recordSuccessRateText() {
      if (!this.logs.length) { this._numSettle('recRate', 0); return '—'; }
      return this.fmtPct(this._numTweenRaw('recRate', this.recordSuccessRate));
    },
    get recordTokenCount() { return this.logs.reduce((s,l)=>s+(Number(l.prompt_tokens)||0)+(Number(l.completion_tokens)||0),0); },