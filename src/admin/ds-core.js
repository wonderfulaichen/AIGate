// ── 生命周期 ──
// init / switchTab / 轮询 / 基础数据拉取 (logs/health/changelog)
// 主要成员: init / switchTab / startPoll / fetchLogs
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)


    // stats 快照预热 (P4): 把上一次拿到的 stats 存 localStorage, 首帧先渲染它再后台刷新 ——
    // 消除"打开面板一片骨架"的等待感. 只用作**展示**, 不参与任何计算决策
    // (金额/口径仍以本次拉取的新数据为准); 版本升级时随旧缓存一起清理.
    _STATS_SNAP_KEY: 'aigate.statsSnapshot',
    saveStatsSnapshot() {
      if (!this.stats) return;
      try {
        // 只存渲染所需字段, 避免把整棵大树塞进 localStorage (趋势数组可能很长)
        const s = this.stats;
        localStorage.setItem(this._STATS_SNAP_KEY, JSON.stringify({
          total_requests: s.total_requests, total_cost: s.total_cost, error_count: s.error_count,
          total_prompt_tokens: s.total_prompt_tokens, total_completion_tokens: s.total_completion_tokens,
          total_cache_hit_tokens: s.total_cache_hit_tokens, avg_latency_ms: s.avg_latency_ms,
          gen_speed: s.gen_speed, gen_samples: s.gen_samples, has_price_config: s.has_price_config,
          per_model: (s.per_model || []).slice(0, 12), per_provider: (s.per_provider || []).slice(0, 12),
          top_models: (s.top_models || []).slice(0, 12), today_requests: s.today_requests,
          trends: [], source_window: s.source_window, window_minutes: s.window_minutes,
          audit: s.audit, session: s.session, _snapshot: true,
        }));
      } catch (e) { /* 配额/隐私模式: 快照是优化, 失败无妨 */ }
    },
    loadStatsSnapshot() {
      try {
        const raw = localStorage.getItem(this._STATS_SNAP_KEY);
        if (!raw) return null;
        const s = JSON.parse(raw);
        return (s && typeof s === 'object' && s._snapshot) ? s : null;
      } catch (e) { return null; }
    },
    // 快照仅用于"有数据可画" —— 折线/环图依赖 trends/per_model, 快照里 trends 是空的,
    // 故只在**首次加载期间**注入, 一旦真实数据到达立刻覆盖 (见 ds-stats.fetchStats).
    applyStatsSnapshot() {
      if (this.stats) return;
      const snap = this.loadStatsSnapshot();
      if (!snap) return;
      if (!(Number(snap.total_requests) > 0)) return;  // 别用"0 请求"的旧快照盖住空态判断
      this.stats = snap;
      this.loading = false;   // 有内容可看 → 不必再显示骨架
      // 注意: 这里**不设** _firstLoadDone —— 快照不是最新数据, 真实拉取仍算首次收口
      // (见 ds-stats.fetchStats 的 firstLoad 判据), 那时才按"骨架最少驻留"规则收尾.
    },

    async init() {
      // localStorage 版本键: 版本变化时清理旧 UI 缓存 —— 结构迁移后旧数据会毒害新逻辑.
      // 白名单保留跨版本稳定的个性化偏好; 其余 aigate* 键一律清除. 隐私模式静默降级.
      //
      // ⚠️ 2026-10-24 修：白名单曾漏 `aigate.pageSize` 与 `aigate.logCols` ——
      // 而这两个是**真实在写**的（core.js 每页条数 / ds-records.js 列显隐），
      // 于是用户每次版本升级都被静默重置，界面还不会提示"你的偏好被清了"。
      //
      // 现在改为**用集合声明"受保护偏好"**，并要求任何新增持久化键在此登记 ——
      // 判据由 `tools/probe_ui_invariants.py` 守着（它会扫源码里所有
      // `localStorage.setItem('aigate…')`，凡有写入却不在本集合内即报红）。
      // 这样"新增一个设置项忘了登记"会立刻被发现，而不是等用户被重置后才发现。
      try {
        const CK = 'aigate.cache-version';
        const VER = (window.AIGATE_VERSION && window.AIGATE_VERSION.version) || '';
        if (VER && localStorage.getItem(CK) !== VER) {
          // 受跨版本保留的个性化偏好（改这里时同步 probe_ui_invariants.py 的期望集合）。
          const KEEP = {
            [CK]: 1,
            'aigate.analyticsSpan': 1,   // 趋势粒度
            'aigate_drawer_adv': 1,      // 抽屉高级端点展开态
            'aigate.chartMode': 1,       // 趋势图模式
            'aigate.theme': 1,           // 主题
            'aigate.pageSize': 1,        // 每页条数（此前被误删）
            'aigate.logCols': 1,         // 记录表列显隐（此前被误删）
          };
          const drop = [];
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k && k.indexOf('aigate') === 0 && !KEEP[k]) drop.push(k);
          }
          drop.forEach(k => { try { localStorage.removeItem(k); } catch (e) {} });
          localStorage.setItem(CK, VER);
        }
      } catch (e) { /* privacy mode */ }
      // 骨架宽限期: 250ms 内首载完成 → 骨架永不显示 (避免快载闪一下); 超时才点亮并记录时刻.
      // 记录在此处 (init 是组件生命周期起点), 与 loading/skelReady 的消费方约定见 ds-state.js.
      setTimeout(() => { if (this.loading && !this.skelReady) { this.skelReady = true; this._skelShownAt = Date.now(); } }, 250);
      if (window.Alpine && Alpine.store('i18n')) {
        document.documentElement.lang = (Alpine.store('i18n').lang === 'en-US') ? 'en' : 'zh';
      }
      document.title = t('app_title');
      this.restoreAnalyticsSpan();
      this.restoreChartPrefs();
      this.restoreTheme();
      // 快照预热: 先铺上次的数据 (消除骨架等待), 随后的 fetchStats 会覆盖为最新值.
      this.applyStatsSnapshot();
      await this.fetchCurrencyConfig();
      await this.fetchPeakSchedule();
      // 哈希深链（2026-10-24）：优先按地址栏的 #tab 恢复，否则回概览。
      // 只在**合法页签名**时才用 —— 随便什么 hash 都当页签会让页面变成空白。
      const TAB_IDS = ['dashboard','analytics','records','providers','settings','about'];
      let startTab = 'dashboard';
      try {
        const raw = String(location.hash || '').replace(/^#/, '').trim();
        // 支持 `#analytics/cost`、`#settings/perf`（页签/子页）两种两段式
        const head = raw.split('/')[0];
        if (TAB_IDS.includes(head)) {
          startTab = head;
          const sub = raw.split('/')[1];
          if (sub) {
            if (head === 'analytics' && ['overview','cost'].includes(sub)) this.analyticsTab = sub;
            if (head === 'settings') this.settingsSection = sub;
          }
        }
      } catch (e) {}
      // 注：不需要另写"非法 hash 归一"—— 下面的 switchTab 会把当前页签写回地址栏，
      // 非法 hash 自然被覆盖成 #dashboard。曾加过一段显式归一，改坏验证时发现
      // 删掉它判据照样通过（说明它是冗余保险），故删除。
      await this.switchTab(startTab);
      // 监听浏览器前进/后退（含手动改地址栏）：切到对应页签。
      // 只绑一次，且用 replaceState 的写入不会再次触发 hashchange（同值不触发）。
      if (!this._hashBound) {
        this._hashBound = true;
        window.addEventListener('hashchange', () => {
          try {
            const head = String(location.hash || '').replace(/^#/, '').split('/')[0];
            if (head && TAB_IDS.includes(head) && head !== this.activeTab) this.switchTab(head);
          } catch (e) {}
        });
      }
      await this.fetchChangelog();
      await this.fetchSeenVersion();
      // 版本升级后首次打开（或首次运行）自动弹出更新亮点。
      if (this.version && this.version.version && this.seenVersion && this.seenVersion !== this.version.version) {
        this.showWhatsNew = true;
      } else if (!this.seenVersion && this.version && this.version.version) {
        this.seenVersion = this.version.version;
      }
      this.startPoll();
      window.addEventListener('beforeunload', (e)=>{
        if(this.isDirty){
          e.preventDefault();
          e.returnValue='';
        }
      });
      this.$watch('chartMode', ()=>{ this.computeModelTrend(); });
      // ── 供应商表单提示的**唯一出口**（2026-10-12 修「提示写了却永远不显示」）──
      //
      // 背景（审计实测）: `providersFormMsg` 被写入 20+ 次（添加预设/自定义添加/删除/
      // 恢复默认/启用停用/拉取成功/保存成功/保存失败/网络错误/名称重复…），
      // 但**任何 HTML 里都没有渲染点**（grep 全部 .html 零命中）——
      // 写好的文案全部"写了就丢"。后果是用户点「添加」后屏幕上只多一张卡、
      // **没有任何文字说"还没保存"**；保存失败也完全静默（他分不清成功和失败）。
      //
      // 修法: 不逐个改 20 个写入点（那样以后新增一处又会漏），而是在**唯一出口**接上 ——
      // 用 $watch 监听该变量，一旦被赋非空值就交给全局 toast 渲染。
      // 这样新增写入点**自动生效**，不需要记得同步。
      //
      // ⚠️ 已带 toast 的调用点（拉取失败/网络错误）会重复弹一次，故用 `providersFormOk`
      //    之外再加一个"该文案是否已 toast 过"的去重锚点：toast 的 key 用文案本身，
      //    相同的 3s 内自动合并计数（见 stores.js 的 push 实现），不会刷屏。
      this.$watch('providersFormMsg', (v) => {
        if (!v) return;
        toast(v, this.providersFormOk === false ? 'error' : 'info', 'pform:' + v);
      });
      // 同类的第二个死变量: `keyMsg`（清空密钥的成功/失败提示）也没有渲染点
      // （审计实测: ds-providers.js:24-26 写入, 全 HTML 零渲染）。
      // 抽屉内的密钥保存走的是 `drawerProv._keyMsg`（有渲染, 保留不动）。
      this.$watch('keyMsg', (v) => {
        if (!v) return;
        toast(v, 'info', 'keymsg:' + v);
      });
      // 快捷键: / 聚焦记录页搜索框 (输入控件内不劫持; 非记录页不切换页签, 避免误跳).
      this._onSlashKey = (e) => {
        if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
        const el = e && e.target;
        const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
        if (typing) return;
        if (this.activeTab !== 'records') return;
        const inp = this.$refs.logSearch;
        if (inp) { e.preventDefault(); inp.focus(); }
      };
      window.addEventListener('keydown', this._onSlashKey);
    },

    // 切换界面语言并持久化 (写入 lang.json + 更新运行态由后端处理)
    setLang(l) {
      if (window.Alpine && Alpine.store('i18n')) Alpine.store('i18n').lang = l;
      window.AIGATE_LANG = l;
      document.documentElement.lang = (l === 'en-US') ? 'en' : 'zh';
      document.title = t('app_title');
      fetch('/admin/api/lang', { method:'POST', headers: authHeaders(), body: JSON.stringify({ lang: l }) }).catch(function(){});
    },

    // ── 主题 (暗色 / 亮色 / 跟随系统) ──
    // 只切 <html class="light"> 一个类名: 全部颜色都走 base.css 的语义 token, 组件无需分主题
    // 写第二套样式。首帧类名由 head.html 的内联脚本按偏好写好 (避免先渲染暗色再切)。
    setTheme(mode) {
      if (mode !== 'dark' && mode !== 'light' && mode !== 'system') return;
      this.themeMode = mode;
      try { localStorage.setItem('aigate.theme', mode); } catch (e) {}
      this.applyTheme();
    },
    // 把 themeMode 落到 DOM。'system' 时读系统偏好。
    applyTheme() {
      const sysLight = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
      const light = this.themeMode === 'light' || (this.themeMode === 'system' && sysLight);
      document.documentElement.classList.toggle('light', light);
    },
    restoreTheme() {
      let m = 'dark';
      try { m = localStorage.getItem('aigate.theme') || 'dark'; } catch (e) {}
      if (m !== 'dark' && m !== 'light' && m !== 'system') m = 'dark'; // 白名单: 手改 localStorage 不会塞进非法值
      this.themeMode = m;
      this.applyTheme();
      // 跟随系统时实时响应 OS 深浅色切换。仅在 system 模式生效 —— 否则用户显式选的
      // 暗/亮会被系统变化悄悄覆盖 (那是"我的设置没生效"的典型来源)。
      if (window.matchMedia) {
        const mq = window.matchMedia('(prefers-color-scheme: light)');
        const onSys = () => { if (this.themeMode === 'system') this.applyTheme(); };
        if (mq.addEventListener) mq.addEventListener('change', onSys);
        else if (mq.addListener) mq.addListener(onSys);
      }
    },

    // 监听地址 (由 baseUrl 去掉协议与 /v1 后缀派生)
    listenAddr() {
      return (this.baseUrl || '').replace(/^https?:\/\//, '').replace(/\/v1$/, '').replace(/\/$/, '');
    },

    // 复制文本到剪贴板; 复制后短暂标记 copiedField 显示「已复制」
    copyText(text, key) {
      const self = this;
      const mark = function () {
        self.copiedField = key || 'default';
        if (self.copyTimer) clearTimeout(self.copyTimer);
        self.copyTimer = setTimeout(function () { self.copiedField = ''; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(mark).catch(function () { self.fallbackCopy(text); mark(); });
      } else {
        self.fallbackCopy(text); mark();
      }
    },
    fallbackCopy(text) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
      } catch (e) {}
    },

    switchTab(tabId) {
      // 未保存改动的**统一守卫**（2026-10-24 修）。
      //
      // 此前只有 `isDirty`（= providersFormData 的供应商草稿）被守卫，而
      // `peakDirty`（分时段计费）与 `tooltipDirty`（托盘 Tooltip）**不在**里面 ——
      // 于是：改了高峰时段 → 切到供应商页 → 本函数 L223 的 `fetchPeakSchedule()`
      // 用后端值覆盖本地草稿 → 改动**静默丢失**；而 `peakDirty` 只在保存成功时
      // 才复位（ds-config.js），所以黄色「未保存」还挂在界面上。
      // 用户既丢了改动，也不知道该不该再点保存 —— 这是本轮审计里最严重的静默数据丢失。
      //
      // 现在统一走 `hasUnsavedDrafts()`（见 ds-config.js），它返回"还有哪些草稿没保存"，
      // 确认弹窗里能点名，用户知道自己在丢什么。
      const dirtyKinds = (tabId !== this.activeTab) ? this.unsavedDraftKinds() : [];
      if (dirtyKinds.length) {
        // 统一确认弹窗 (异步): 用户确认后重入本函数继续切换.
        confirmAsk(t('unsaved_confirm_kinds', dirtyKinds.join(' · ')), { danger: true }).then(ok => {
          if (!ok) return;
          this.clearDirtyFlags();
          this.switchTab(tabId);
        });
        return;
      }
      this.activeTab = tabId;
      this.logPage = 0;
      // 哈希深链（2026-10-24）：把当前页签写进地址栏，刷新/分享能回到同一页。
      // 用 replaceState 而不是 pushState —— 面板是工具而非站点，用户点页签
      // 不该在浏览器历史里堆一堆记录（否则"后退"要点五六次才能退出）。
      //
      // ⚠️ 写 hash 时**必须带上子页**，否则会把子页信息冲掉：
      //   init 时先按 `#settings/peak` 设好 settingsSection，再调 switchTab('settings')，
      //   若这里只写 `#settings`，刚读到的 peak 就丢了（实测过）。
      //   同理 analytics 要带 analyticsTab。
      // 静默失败：某些 WebView 环境不允许改 URL，不能因此打断切页签。
      try {
        let h = '#' + tabId;
        if (tabId === 'analytics' && this.analyticsTab) h += '/' + this.analyticsTab;
        else if (tabId === 'settings' && this.settingsSection) h += '/' + this.settingsSection;
        if (location.hash !== h) history.replaceState(null, '', h);
      } catch (e) {}
      // 切回概览重播 KPI 数字补间: 补间槽是持久对象, 目标值没变时 numTween 会直接返回旧值,
      // 故必须显式打 replay 标 (否则只有"手动刷新"能重播, 切页签毫无数字反馈).
      // 首次加载传 false (守 numTween 的"初值即真值"约束, 避免首屏像加载动画).
      if (tabId==='dashboard') { this.fetchStats(null, false, this._firstLoadDone); this.fetchBalance(); this.fetchCache(); this.fetchStripReasoning(); this.fetchMaxHistoryTurns(); this.fetchConnParams(); this.fetchEvents(); if(!this.providersFormData || !this.isDirty) this.loadProvidersForm(); }
      if (tabId==='analytics') { this.fetchStats(null, false, this._firstLoadDone); this.fetchBalance(); this.fetchDegradations(); this.fetchHeatmap(); }
      // 记录页的统计来自 logs (不走 stats): 已加载过才重播 —— 首访让骨架→内容自然出现, 不从 0 滚.
      if (tabId==='records') { if (this.logsFetched) this.replayNumTweens(); this.fetchLogs(); }
      // ⚠️ 下面这批 fetch* 会**用后端值覆盖本地草稿**。只在对应草稿不脏时才调 ——
      // 否则即便过了上面的确认（用户选了"丢弃"），也会被这里二次覆盖成难以理解的中间态。
      // `clearDirtyFlags()` 已在确认分支把它们清掉，故此处只需按 dirty 判断。
      if (tabId==='settings'||tabId==='providers') {
        if (tabId==='providers' && this.health.length===0) this.fetchHealth();
        if(!this.providersFormData || !this.isDirty) this.refreshProviders();
        this.fetchKeys(); this.fetchAccounts(); this.fetchPresets();
        if (!this.tooltipDirty) this.fetchTooltipConfig();
        this.fetchProxyConfig();
        if (!this.currencyDirty) this.fetchCurrencyConfig();
        if (!this.peakDirty) this.fetchPeakSchedule();
        this.fetchStripReasoning(); this.fetchThinkingPurify(); this.fetchStripTcProtocols(); this.fetchMaxHistoryTurns(); this.fetchAutoContinue(); this.fetchConnParams(); this.fetchCache(); this.fetchRecall(); this.fetchToolOutputLimit(); this.fetchDegradations();
      }
    },

    // 主轮询 (3s)。2026-10-24 加上**页面可见性守卫**：
    // 改造前不管窗口是否被隐藏、标签是否切到后台，都照常拉 1000 条日志 + stats + 热力图，
    // 纯粹浪费带宽与 CPU（软件常驻托盘，用户可能几小时不看面板）。
    // 隐藏时**跳过整轮**；重新可见时立刻补一次，避免"打开就看到几分钟前的旧数"。
    //
    // ⚠️ 为什么用 document.hidden 而不是 visibilityState：这里判的是"浏览器标签页是否
    // 可见"，与 ds-settings-api.js:107 那处"红点 ack 不能用 visibilityState"是两件事
    //（那个说的是 WebView 最小化到托盘仍报 visible，所以要靠原生侧 is_visible()）。
    // 对轮询来说，document.hidden 已经能挡掉"窗口被最小化/切到别的标签"这类最常见场景。
    startPoll() {
      clearInterval(this.pollTimer);
      const tick = () => {
        // 隐藏时跳过 —— 但**不停止定时器**：定时器继续跑，恢复可见时下一拍就能继续，
        // 不必重新注册（重注册容易漏掉 clearInterval 而叠出多个定时器）。
        if (typeof document !== 'undefined' && document.hidden) return;
        this.fetchLogs();
        if (['dashboard','tokens','daily'].includes(this.activeTab) && this.stats) this.fetchStats();
        // 事件台账也要跟着轮询: 熔断/恢复是**异步发生**的, 只在切页签时拉一次
        // 就会"打开面板看到的是十分钟前的状态" —— 那等于没有通知。
        if (this.activeTab==='dashboard') this.fetchEvents();
        if (this.activeTab==='analytics') { this.fetchStats(); this.fetchDegradations(); this.fetchHeatmap(); } // 后端 15s 缓存, 倒计时随之跳动 (流式更新)
      };
      this.pollTimer = setInterval(tick, 3000);
      // 重新可见时补一次（用户切回来的第一眼就要是新的）
      if (!this._visBound && typeof document !== 'undefined') {
        this._visBound = true;
        document.addEventListener('visibilitychange', () => {
          if (!document.hidden) tick();
        });
      }
    },

    async fetchChangelog() { try { const r=await fetch('/admin/api/changelog', {headers:authHeaders()}); if(r.ok) { const data=await r.json(); this.changelog=data.versions||[]; } } catch(e){} },
    async fetchSeenVersion() { try { const r=await fetch('/admin/api/seen-version', {headers:authHeaders()}); if(r.ok) { const data=await r.json(); this.seenVersion=data.version||''; } } catch(e){} },
    // 关闭更新亮点弹窗并标记当前版本已读。
    async dismissWhatsNew() {
      this.showWhatsNew = false;
      const cur = this.version ? this.version.version : '';
      this.seenVersion = cur;
      try { await fetch('/admin/api/seen-version', { method:'POST', headers:authHeaders(), body: JSON.stringify({ version: cur }) }); } catch(e){}
    },
    async fetchLogs()    {
      // 竞态防护: 快速切换/轮询叠加时先 abort 上一次 —— 旧响应后到会覆盖新数据.
      if (this._logsAbort) { try { this._logsAbort.abort(); } catch(e){} }
      const ac = new AbortController(); this._logsAbort = ac;
      try { const r=await fetch('/admin/api/logs', {headers:authHeaders(), signal: ac.signal}); if(r.ok) { this.logs=await r.json(); this._markNewLogs(); this.clampLogPage(); } } catch(e){}
      finally {
        // 被后续请求取代 (abort): 首载归属交给后来者, 本次不置 logsFetched —— 否则会被
        // 一次被取消的请求把「无数据」提前显示成 0 条 (无数据 ≠ 0).
        if (this._logsAbort === ac) { this._logsAbort = null; this.logsFetched = true; }
      }
    },
    // 新到的日志行高亮 (数据驱动动效): 只在**最新一条的时间戳变大**时给那一行打标,
    // 由 CSS `.row-flash` 播一次 1.1s 主色底 (见 components.css).
    // 首载只记住不闪 —— 否则每次打开记录页整表都在闪, 反而看不出哪条是新的.
    _markNewLogs() {
      const top = this.logs.length ? Number(this.logs[0].timestamp) || 0 : 0;
      if (!top) { this._lastTopLogTs = 0; this._flashLogTs = 0; return; }
      if (!this._lastTopLogTs) { this._lastTopLogTs = top; return; }
      if (top <= this._lastTopLogTs) return;
      this._lastTopLogTs = top;
      this._flashLogTs = top;
      clearTimeout(this._flashLogTimer);
      this._flashLogTimer = setTimeout(() => { this._flashLogTs = 0; }, 1200);
    },
    async fetchHealth()  { this.healthLoading=true; try { const r=await fetch('/admin/api/health', {headers:authHeaders()}); if(r.ok) this.health=await r.json(); } catch(e){} this.healthLoading=false; },
    // 健康等级的**唯一取值来源**：后端 `i18n::health_level()` 返回 `"ok"` 或 `"error"`
    // （见 src/i18n.rs:494-508，文档写明"技术性 code, 不随语言变化"）。
    //
    // ⚠️ 2026-10-24 修：此前各处**比字面量**，出现了三个值混用 ——
    //   drawer-provider.html:14 判 'error'  (对)
    //   drawer-provider.html:38 判 'err'    (永远为假 → 「测试并恢复」按钮永不出现, 熔断后没法解除)
    //   ds-provtest.js:78      写 'error'  (对, 但另一处又写 'ok'/'error' 混着比)
    // 现在统一走本函数返回**布尔**, 调用方不再接触字面量 —— 这样"哪个值才对"只在一处定义。
    //
    // 返回值语义:
    //   null  = 还没有健康数据 (没测过 / health 未拉取) → 显示「未连接」
    //   true  = 正常
    //   false = 异常 (熔断 OR 不可达 —— 两者都归 "error", 与后端契约一致)
    healthState(name) {
      const h = (this.health || []).find((x) => x.provider === name);
      if (!h) return null;
      const lv = h.status_level || '';
      if (lv === '') return null;
      // 兼容防御: 早期模板曾用 `'err'` 做比较（drawer-provider.html 旧版 L38），
      // 若将来有代码误写 `'err'` 进 status_level，这里也按异常处理 ——
      // 免得"改了一处、别处还留着旧值"导致状态显示错。
      return lv === 'ok';
    },
    // 供模板直接用的三态判定（避免模板里写 === true / === false 这种脆弱比较）。
    healthOf(name) {
      const s = this.healthState(name);
      return s === null ? 'na' : (s ? 'ok' : 'err');
    },