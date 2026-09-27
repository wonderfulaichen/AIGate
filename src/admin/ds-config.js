
    async fetchBalance() {
      this.balanceLoading = true;
      try {
        const r = await fetch('/admin/api/balance', {headers: authHeaders()});
        if (r.ok) {
          const data = await r.json();
          this.balanceData = data.balances || [];
        }
      } catch(e) {}
      this.balanceLoading = false;
    },
    async clearAllLogs() {
      // 不可恢复: 服务端会一并重写日志文件与删除 rollup 账本, 故必须二次确认.
      // 文案里的条数取实时的 logs.length —— 导出只覆盖已加载的这部分, 写明它才能避免
      // 用户误以为"先导出就没损失"(实测磁盘 4290 条时导出仅 1000 条, 覆盖率 23%).
      if(this.clearingLogs) return;                                  // 进行中, 忽略重复点击
      await confirmAsk(t('confirm_clear_logs', this.logs.length), { danger: true, onOk: () => this._clearAllLogsDo() });
    },
    async _clearAllLogsDo() {
      this.clearingLogs = true;
      try {
        const r=await fetch('/admin/api/logs',{method:'DELETE',headers:authHeaders()});
        if(r.ok){ this.logs=[]; this.stats=null; this.logPage=0; }
        // 失败必须提示: 数据没被清掉却无声无息, 用户会以为已经清空.
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
      finally { this.clearingLogs = false; }                         // 失败也要复位, 否则按钮永久禁用
    },

    exportLogs() {
      const blob = new Blob([JSON.stringify(this.logs, null, 2)], {type:'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `aigate-logs-${new Date().toISOString().slice(0,10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    },
    get logProviders() {
      const names = new Set();
      for (const l of this.logs) if (l.provider) names.add(l.provider);
      return [...names].sort();
    },

    // Settings API
    async fetchTooltipConfig() {
      try {
        const r=await fetch('/admin/api/tooltip-config', {headers:authHeaders()});
        if(r.ok) this.tooltipConfig=await r.json();
      } catch(e){}
    },
    async saveTooltipConfig() {
      this.tooltipMsg='';
      try {
        const r=await fetch('/admin/api/tooltip-config',{method:'POST',headers:authHeaders(),body:JSON.stringify({config:this.tooltipConfig})});
        const d=await r.json();
        if(r.ok) {
          this.tooltipMsg=t('saved');
          setTimeout(()=>this.tooltipMsg='',3000);
          toast(t('saved'), 'success');
        } else {
          this.tooltipMsg=d.error||t('save_failed');
          toast(this.tooltipMsg, 'error');
        }
      } catch(e){ this.tooltipMsg=t('network_error'); toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchKeys() {
      try { const r=await fetch('/admin/api/keys', {headers:authHeaders()});
        if(r.ok){ const d=await r.json(); const m={}; for(const v of (d.providers||[])) m[v.provider]=v; this.keysByProvider=m; } } catch(e){}
    },
    // —— 费用显示币种 ——
    async fetchCurrencyConfig() {
      try {
        const r=await fetch('/admin/api/currency', {headers:authHeaders()});
        if(r.ok) {
          const d=await r.json();
          if(d && d.rates && Object.keys(d.rates).length) this.currencyConfig=d;
          this.currencyRateEdit = this.currencyConfig.rates[this.currencyConfig.currency] || 1;
        }
      } catch(e){}
    },
    async saveCurrencyConfig() {
      this.currencyMsg='';
      try {
        const r=await fetch('/admin/api/currency',{method:'POST',headers:authHeaders(),body:JSON.stringify({config:this.currencyConfig})});
        const d=await r.json();
        if(r.ok && d.success!==false) {
          this.currencyMsg=t('saved');
          setTimeout(()=>this.currencyMsg='',3000);
          toast(t('saved'), 'success');
        } else {
          this.currencyMsg=d.error||t('save_failed');
          toast(this.currencyMsg, 'error');
        }
      } catch(e){ this.currencyMsg=t('network_error'); toast(t('network_error'), 'error', 'net_err'); }
    },
    onCurrencyChange() {
      this.currencyRateEdit = this.currencyConfig.rates[this.currencyConfig.currency] || 1;
      this.saveCurrencyConfig();
    },
    onRateEdit() {
      const v=parseFloat(this.currencyRateEdit);
      if(!isNaN(v) && v>0) {
        this.currencyConfig.rates[this.currencyConfig.currency]=v;
        this.saveCurrencyConfig();
      }
    },
    // 分时段计费配置 (高峰日 / 高峰时段 / 时区).
    async fetchPeakSchedule() {
      try {
        const r = await fetch('/admin/api/peak-schedule', {headers:authHeaders()});
        if (r.ok) {
          const d = await r.json();
          if (d && Array.isArray(d.windows)) {
            this.peakSchedule = {
              enabled: d.enabled !== false,
              tz_offset_hours: (typeof d.tz_offset_hours === 'number') ? d.tz_offset_hours : 8,
              weekdays: Array.isArray(d.weekdays) ? d.weekdays : [1,2,3,4,5,6,7],
              windows: d.windows.map(w => ({start:w.start||'', end:w.end||''})),
            };
          }
        }
      } catch(e){}
    },
    togglePeakWeekday(d) {
      const i = this.peakSchedule.weekdays.indexOf(d);
      if (i >= 0) this.peakSchedule.weekdays.splice(i, 1);
      else { this.peakSchedule.weekdays.push(d); this.peakSchedule.weekdays.sort((a,b)=>a-b); }
    },
    addPeakWindow() { this.peakSchedule.windows.push({start:'09:00', end:'12:00'}); },
    removePeakWindow(i) { this.peakSchedule.windows.splice(i, 1); },
    async savePeakSchedule() {
      this.peakMsg='';
      try {
        const r = await fetch('/admin/api/peak-schedule', {method:'POST', headers:authHeaders(), body:JSON.stringify({config:this.peakSchedule})});
        const d = await r.json();
        this.peakMsgOk = !!(r.ok && d.success !== false);
        this.peakMsg = this.peakMsgOk ? t('saved') : (d.error || t('save_failed'));
        setTimeout(()=>this.peakMsg='', 3000);
      } catch(e){ this.peakMsgOk=false; this.peakMsg=t('network_error'); }
    },
    // CNY 金额换算到当前显示币种 (rates[code] = 1 单位该币种 = 多少 CNY).
    convertCny(v) {
      const r = this.currencyConfig.rates[this.currencyConfig.currency] || 1;
      return (v || 0) / r;
    },
    // 币种符号; 未知币种回退到代码本身.
    moneySymbol(code) {
      const sym = { CNY:'¥', USD:'$', EUR:'€', JPY:'¥', GBP:'£', HKD:'HK$' };
      return sym[code] || (code + ' ');
    },