

    async init() {
      if (window.Alpine && Alpine.store('i18n')) {
        document.documentElement.lang = (Alpine.store('i18n').lang === 'en-US') ? 'en' : 'zh';
      }
      document.title = t('app_title');
      this.restoreAnalyticsSpan();
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
    },

    // 切换界面语言并持久化 (写入 lang.json + 更新运行态由后端处理)
    setLang(l) {
      if (window.Alpine && Alpine.store('i18n')) Alpine.store('i18n').lang = l;
      window.AIGATE_LANG = l;
      document.documentElement.lang = (l === 'en-US') ? 'en' : 'zh';
      document.title = t('app_title');
      fetch('/admin/api/lang', { method:'POST', headers: authHeaders(), body: JSON.stringify({ lang: l }) }).catch(function(){});
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
      if (tabId==='dashboard') { this.fetchStats(); this.fetchBalance(); this.fetchCache(); this.fetchStripReasoning(); this.fetchMaxHistoryTurns(); this.fetchConnParams(); if(!this.providersFormData || !this.isDirty) this.loadProvidersForm(); }
      if (tabId==='analytics') { this.fetchStats(); this.fetchBalance(); this.fetchDegradations(); }
      if (tabId==='records') { this.fetchLogs(); }
      if (tabId==='settings'||tabId==='providers') { if (tabId==='providers' && this.health.length===0) this.fetchHealth(); if(!this.providersFormData || !this.isDirty) this.loadProvidersForm(); this.fetchKeys(); this.fetchTooltipConfig(); this.fetchProxyConfig(); this.fetchCurrencyConfig(); this.fetchPeakSchedule(); this.fetchStripReasoning(); this.fetchStripTcProtocols(); this.fetchMaxHistoryTurns(); this.fetchAutoContinue(); this.fetchConnParams(); this.fetchCache(); this.fetchRecall(); this.fetchToolOutputLimit(); this.fetchDegradations(); }
    },

    startPoll() {
      clearInterval(this.pollTimer);
      this.pollTimer = setInterval(() => {
        this.fetchLogs();
        if (['dashboard','tokens','daily'].includes(this.activeTab) && this.stats) this.fetchStats();
        if (this.activeTab==='analytics') { this.fetchStats(); this.fetchDegradations(); } // 后端 15s 缓存, 倒计时随之跳动 (流式更新)
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
    async fetchLogs()    { try { const r=await fetch('/admin/api/logs', {headers:authHeaders()}); if(r.ok) { this.logs=await r.json(); this.clampLogPage(); } } catch(e){} },
    async fetchHealth()  { this.healthLoading=true; try { const r=await fetch('/admin/api/health', {headers:authHeaders()}); if(r.ok) this.health=await r.json(); } catch(e){} this.healthLoading=false; },
    healthOf(name) { const h=(this.health||[]).find(x=>x.provider===name); return h?(h.status_level||''):''; },