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
      try {
        const CK = 'aigate.cache-version';
        const VER = (window.AIGATE_VERSION && window.AIGATE_VERSION.version) || '';
        if (VER && localStorage.getItem(CK) !== VER) {
          const KEEP = { [CK]: 1, 'aigate.analyticsSpan': 1, 'aigate_drawer_adv': 1, 'aigate.chartMode': 1, 'aigate.theme': 1 };
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
      await this.switchTab('dashboard');
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
      if(this.isDirty && tabId!==this.activeTab){
        // 统一确认弹窗 (异步): 用户确认后重入本函数继续切换.
        confirmAsk(t('unsaved_confirm'), { danger: true }).then(ok => {
          if (!ok) return;
          this.isDirty = false;
          this.switchTab(tabId);
        });
        return;
      }
      this.activeTab = tabId;
      this.logPage = 0;
      // 切回概览重播 KPI 数字补间: 补间槽是持久对象, 目标值没变时 numTween 会直接返回旧值,
      // 故必须显式打 replay 标 (否则只有"手动刷新"能重播, 切页签毫无数字反馈).
      // 首次加载传 false (守 numTween 的"初值即真值"约束, 避免首屏像加载动画).
      if (tabId==='dashboard') { this.fetchStats(null, false, this._firstLoadDone); this.fetchBalance(); this.fetchCache(); this.fetchStripReasoning(); this.fetchMaxHistoryTurns(); this.fetchConnParams(); this.fetchEvents(); if(!this.providersFormData || !this.isDirty) this.loadProvidersForm(); }
      if (tabId==='analytics') { this.fetchStats(null, false, this._firstLoadDone); this.fetchBalance(); this.fetchDegradations(); this.fetchHeatmap(); }
      // 记录页的统计来自 logs (不走 stats): 已加载过才重播 —— 首访让骨架→内容自然出现, 不从 0 滚.
      if (tabId==='records') { if (this.logsFetched) this.replayNumTweens(); this.fetchLogs(); }
      if (tabId==='settings'||tabId==='providers') { if (tabId==='providers' && this.health.length===0) this.fetchHealth(); if(!this.providersFormData || !this.isDirty) this.refreshProviders(); this.fetchKeys(); this.fetchAccounts(); this.fetchPresets(); this.fetchTooltipConfig(); this.fetchProxyConfig(); this.fetchCurrencyConfig(); this.fetchPeakSchedule(); this.fetchStripReasoning(); this.fetchThinkingPurify(); this.fetchStripTcProtocols(); this.fetchMaxHistoryTurns(); this.fetchAutoContinue(); this.fetchConnParams(); this.fetchCache(); this.fetchRecall(); this.fetchToolOutputLimit(); this.fetchDegradations(); }
    },

    startPoll() {
      clearInterval(this.pollTimer);
      this.pollTimer = setInterval(() => {
        this.fetchLogs();
        if (['dashboard','tokens','daily'].includes(this.activeTab) && this.stats) this.fetchStats();
        // 事件台账也要跟着轮询: 熔断/恢复是**异步发生**的, 只在切页签时拉一次
        // 就会"打开面板看到的是十分钟前的状态" —— 那等于没有通知。
        if (this.activeTab==='dashboard') this.fetchEvents();
        if (this.activeTab==='analytics') { this.fetchStats(); this.fetchDegradations(); this.fetchHeatmap(); } // 后端 15s 缓存, 倒计时随之跳动 (流式更新)
      }, 3000);
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
    healthOf(name) { const h=(this.health||[]).find(x=>x.provider===name); return h?(h.status_level||''):''; },