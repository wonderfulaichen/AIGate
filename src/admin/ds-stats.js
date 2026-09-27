
    async fetchStats(rangeOverride, manual)   {
      if (manual) this._refreshing = true;
      // 竞态防护: 快速切换时间范围/手动刷新叠加时先 abort 上一次 —— 旧响应后到会覆盖新范围的数据.
      if (this._statsAbort) { try { this._statsAbort.abort(); } catch(e){} }
      const ac = new AbortController(); this._statsAbort = ac;
      try {
        const range=rangeOverride || (this.activeTab==='dashboard'?'window':(this.analyticsRange||'1d'));
        const params=new URLSearchParams({granularity:this.trendGranularity,range});
        const r=await fetch('/admin/api/stats?'+params.toString(), {headers:authHeaders(), signal: ac.signal});
        if(r.ok) {
          // 数据先落位 (数字即时可算), loading 的切换交给 _endFirstLoad 统一管理:
          // 骨架已显示 → 最少驻留 1s 再切内容; 从未显示 (宽限期内完成) → 立即切.
          const firstLoad = this.loading;
          this.stats=await r.json();
          this.statsError=false;
          if (firstLoad) this._endFirstLoad(); else this.loading=false;
          this.fetchModelMeta(); await this.$nextTick();
          this.computeModelTrend();
          if(this.$refs.trendScroll){ this.$refs.trendScroll.scrollLeft=this.$refs.trendScroll.scrollWidth; }
        } else {
          // 非 2xx 也是失败: 必须复位 loading, 否则界面停在假的「加载中」(错误态不驻留, 立即可见).
          this.loading=false; this.statsError=true;
        }
      } catch(e) {
        // 被后续请求 abort: 既非网络异常也不该标错误态, 静默返回 (数据以最后一次为准).
        if (e && e.name === 'AbortError') return;
        // 网络异常同理 —— 原实现这里是空 catch, 导致 loading 永远为 true.
        this.loading=false; this.statsError=true;
      } finally {
        if (this._statsAbort === ac) this._statsAbort = null;
        if (manual) this._refreshing = false;
      }
    },
    // 首次加载收口: 骨架从未显示 → 立即结束; 已显示 → 保证自显示起满 1s (避免骨架闪现即撤).
    _endFirstLoad() {
      if (!this.skelReady) { this.loading = false; return; }
      const remain = 1000 - (Date.now() - this._skelShownAt);
      if (remain > 0) { setTimeout(() => { this.loading = false; }, remain); }
      else this.loading = false;
    },
    retryFetchStats() { this.statsError=false; this.loading=true; this.skelReady=true; this._skelShownAt=Date.now(); this.fetchStats(); },
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
    // 统一时间范围与粒度. P3: 由「三档固定组合」升级为「快捷范围 + 自定义区间」——
    // 每个范围**自动推导**合适粒度 (≤1 天→hour, ≤31 天→day, 更长→month), 不再要求用户
    // 先选范围再单独选粒度 (两个控件表达同一件事, 且合法组合有限).
    setSpan(span) {
      const m = SPAN_PRESETS[span];
      if (!m) return;
      this.applyRange(m[0], m[1]);
      try { localStorage.setItem('aigate.analyticsSpan', span); } catch (e) { /* privacy mode */ }
      this.fetchStats();
    },
    // 自定义天数区间: 粒度按跨度自动推导 (与快捷档同一张表, 避免两处阈值分叉).
    setRangeDays(days) {
      const d = Math.max(1, Math.min(3650, Math.round(Number(days) || 0)));
      const g = granularityForDays(d);
      this.applyRange(d + 'd', g);
      try { localStorage.setItem('aigate.analyticsSpan', 'custom:' + d); } catch (e) {}
      this.fetchStats();
    },
    // 写状态 (范围 + 粒度 + 高亮档); 供 setSpan / setRangeDays / restore 共用.
    applyRange(range, granularity) {
      this.analyticsRange = range;
      this.trendGranularity = granularity;
      this.analyticsSpan = spanOfRange(range);
    },
    // 当前粒度提示 (P3): 让"我选了几天"与"系统用了什么粒度"同时可见, 不必猜.
    get granularityLabel() { return t('granularity_auto', this.trendGranularityLabel); },
    // 恢复上次选择; 非法值回退 day. 仅恢复状态, 不额外 fetch (走既有首载).
    restoreAnalyticsSpan() {
      let raw = 'day';
      try { raw = localStorage.getItem('aigate.analyticsSpan') || 'day'; } catch (e) { raw = 'day'; }
      // 自定义区间 (custom:30) 与快捷档 (hour/day/month) 统一解析
      const m = /^custom:(\d+)$/.exec(raw);
      if (m) {
        const d = Math.max(1, Math.min(3650, parseInt(m[1], 10) || 30));
        this.applyRange(d + 'd', granularityForDays(d));
        return;
      }
      if (SPAN_PRESETS[raw]) {
        this.applyRange(SPAN_PRESETS[raw][0], SPAN_PRESETS[raw][1]);
        return;
      }
      this.applyRange(SPAN_PRESETS.day[0], SPAN_PRESETS.day[1]);
    },
    // 当前窗口是否是某个快捷档 (供 UI 高亮; 自定义区间则都不高亮 —— 反向识别).
    get analyticsSpanIsPreset() { return !!SPAN_PRESETS[this.analyticsSpan]; },
    // 自定义区间输入: 当前天数 (仅当处于自定义档时有值, 否则空 —— 不假装知道用户想要几天).
    get customDays() {
      const m = /^(\d+)d$/.exec(this.analyticsRange || '');
      if (!m || this.analyticsSpanIsPreset) return '';
      return m[1];
    },
    // ── 更新亮点：取最新一条 changelog 作为弹窗内容 ──
    get whatsNewEntry() {
      return (this.changelog && this.changelog.length) ? this.changelog[0] : null;
    },