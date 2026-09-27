
    async fetchCache() {
      try { const r=await fetch('/admin/api/cache', {headers:authHeaders()}); if(r.ok) this.cache=await r.json(); } catch(e){}
    },
    async setCacheEnabled(val) {
      await this.setCacheConfig({enabled: val});
    },
    // 运行时更新缓存配置 (开关 / TTL / 条目上限), 仅发送提供的字段. 成功回写整份 stats.
    async setCacheConfig(cfg) {
      try {
        const r = await fetch('/admin/api/cache', {method:'POST', headers:authHeaders(), body:JSON.stringify(cfg)});
        if (r.ok) this.cache = await r.json();
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e) { toast(t('network_error'), 'error', 'net_err'); }
    },
    // 常态截断超长 tool 输出 (0 = 关闭). 与"紧急瘦身"不同: 不等请求体超上限即生效.
    async fetchToolOutputLimit() {
      try { const r=await fetch('/admin/api/tool-output-limit',{headers:authHeaders()}); if(r.ok) { const d=await r.json(); this.toolOutputLimit=Math.max(0,d.bytes|0); } } catch(e){}
    },
    async setToolOutputLimit(bytes) {
      // 开启前确认: 截断有损, 但被截掉的原文可由「降级可回取」留存回看.
      if (bytes > 0) {
        await confirmAsk(t('tool_trunc_confirm'), { danger: false, onOk: () => this._saveToolOutputLimit(bytes) });
        return;
      }
      await this._saveToolOutputLimit(bytes);
    },
    async _saveToolOutputLimit(bytes) {
      try {
        const r=await fetch('/admin/api/tool-output-limit',{method:'POST',headers:authHeaders(),body:JSON.stringify({bytes:bytes})});
        if(r.ok) { const d=await r.json(); this.toolOutputLimit=Math.max(0,d.bytes|0); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    // 静默降级计数: 解析失败导致优化/翻译未生效的次数. 用于区分
    // "这次确实没什么可省" 与 "我们根本没解析成功".
    async fetchDegradations() {
      try { const r=await fetch('/admin/api/degradations',{headers:authHeaders()}); if(r.ok) this.degradations=await r.json(); } catch(e){}
    },
    get degradationTotal() {
      const d = this.degradations||{};
      return Object.values(d).reduce((a,b)=>a+(Number(b)||0),0);
    },
    // ── 降级可回取 (recall): 留存被优化剔离的原文, 供事后核查 ──
    async fetchRecall() {
      try { const r=await fetch('/admin/api/recall', {headers:authHeaders()}); if(r.ok) this.recall=await r.json(); } catch(e){}
    },
    async setRecall(val) {
      // 开启前必须明确告知隐私影响: 留存内容是推理链/tool 输出明文, 会写到磁盘.
      if (val) {
        await confirmAsk(t('recall_confirm_enable'), { danger: false, onOk: () => this._setRecallDo(val) });
        return;
      }
      await this._setRecallDo(val);
    },
    async _setRecallDo(val) {
      try {
        const r=await fetch('/admin/api/recall',{method:'POST',headers:authHeaders(),body:JSON.stringify({enabled:val})});
        if(r.ok) { const d=await r.json(); this.recall.enabled=!!d.enabled; await this.fetchRecall(); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async openRecallPanel() {
      this.showRecall = true;
      this.recallDetail = null;
      await this.fetchRecall();
      try {
        const r=await fetch('/admin/api/recall/list', {headers:authHeaders()});
        if(r.ok) { const d=await r.json(); this.recallEntries=d.entries||[]; }
      } catch(e){}
    },
    closeRecallPanel() { this.showRecall=false; this.recallDetail=null; this.recallCopied=false; },
    // 查看某条留存原文 (取回即计入后端 recalls 计数 —— 这是"留了到底有没有用"的依据).
    async viewRecall(id) {
      try {
        const r=await fetch('/admin/api/recall/entry/'+encodeURIComponent(id), {headers:authHeaders()});
        const d=await r.json();
        if(r.ok && d.content!=null) {
          const e=this.recallEntries.find(x=>x.id===id) || {id:id, bytes:0, ts:0, kind:''};
          this.recallDetail={entry:e, content:d.content};
          this.recallCopied=false;
          this.fetchRecall();
        } else { toast(d.error||t('recall_empty'), 'error', 'recall_view'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async copyRecallContent() {
      if(!this.recallDetail) return;
      const ok = await this.copyText(this.recallDetail.content||'', 'recall');
      if(ok) { this.recallCopied=true; setTimeout(()=>this.recallCopied=false, 1500); }
    },
    async clearRecall() {
      await confirmAsk(t('recall_confirm_clear'), { danger: true, onOk: () => this._clearRecallDo() });
    },
    async _clearRecallDo() {
      try {
        const r=await fetch('/admin/api/recall/clear',{method:'POST',headers:authHeaders()});
        if(r.ok) { this.recallEntries=[]; this.recallDetail=null; await this.fetchRecall(); toast(t('recall_cleared'), 'success'); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    // 字节数人类可读 (B / KB / MB). 与 fmtT (token 数) 不同: 这里必须说"字节", 不能混作 token.
    fmtBytes(n) {
      const v = Number(n)||0;
      if(v < 1024) return v+' B';
      if(v < 1024*1024) return (v/1024).toFixed(1)+' KB';
      return (v/1024/1024).toFixed(2)+' MB';
    },
    // 秒级时间戳 -> 本地 MM/DD HH:MM:SS (与记录页时间列口径一致).
    fmtTime(ts) {
      const n = Number(ts)||0;
      if(!n) return '—';
      const d = new Date(n*1000);
      const p = x => String(x).padStart(2,'0');
      return p(d.getMonth()+1)+'/'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
    },
    async clearCache() {
      try {
        const r=await fetch('/admin/api/cache/clear',{method:'POST',headers:authHeaders()});
        if(r.ok) { this.cache=await r.json(); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchStripReasoning() {
      try { const r=await fetch('/admin/api/strip-reasoning', {headers:authHeaders()}); if(r.ok) { const d=await r.json(); this.stripReasoning=!!d.enabled; } } catch(e){}
    },
    async setStripReasoning(val) {
      // 失败必须提示: 开关按 :class 绑状态, 失败时开关会弹回原位, 不提示用户就不知为何没生效.
      try {
        const r=await fetch('/admin/api/strip-reasoning',{method:'POST',headers:authHeaders(),body:JSON.stringify({enabled:val})});
        if(r.ok) { const d=await r.json(); this.stripReasoning=!!d.enabled; }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchStripTcProtocols() {
      try { const r=await fetch('/admin/api/strip-toolcall-protocols', {headers:authHeaders()}); if(r.ok) { const d=await r.json(); this.stripTcChat=!!d.chat; this.stripTcResp=!!d.responses; } } catch(e){}
    },
    async setStripTcProtocols(patch) {
      // 同 setStripReasoning: 开关按 :class 绑状态, 失败时弹回原位, 必须提示.
      try {
        const r=await fetch('/admin/api/strip-toolcall-protocols',{method:'POST',headers:authHeaders(),body:JSON.stringify(patch)});
        if(r.ok) { const d=await r.json(); this.stripTcChat=!!d.chat; this.stripTcResp=!!d.responses; }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchMaxHistoryTurns() {
      try { const r=await fetch('/admin/api/max-history-turns', {headers:authHeaders()}); if(r.ok) { const d=await r.json(); this.maxHistoryTurns=Math.max(0, d.turns|0); } } catch(e){}
    },
    async setMaxHistoryTurns(val) {
      const turns = Math.max(0, val|0);
      try {
        const r=await fetch('/admin/api/max-history-turns',{method:'POST',headers:authHeaders(),body:JSON.stringify({turns:turns})});
        if(r.ok) { const d=await r.json(); this.maxHistoryTurns=Math.max(0, d.turns|0); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchAutoContinue() {
      try { const r=await fetch('/admin/api/auto-continue', {headers:authHeaders()}); if(r.ok) { const d=await r.json(); this.autoContinue=Math.max(0, d.count|0); } } catch(e){}
    },
    async setAutoContinue(val) {
      const count = Math.min(5, Math.max(0, val|0));
      try {
        const r=await fetch('/admin/api/auto-continue',{method:'POST',headers:authHeaders(),body:JSON.stringify({count:count})});
        if(r.ok) { const d=await r.json(); this.autoContinue=Math.max(0, d.count|0); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async fetchConnParams() {
      try {
        const [rt,st,ry] = await Promise.all([
          fetch('/admin/api/auto-continue',{headers:authHeaders()}),
          fetch('/admin/api/stream-timeout',{headers:authHeaders()}),
          fetch('/admin/api/retry',{headers:authHeaders()}),
        ]);
        if(rt.ok){ const d=await rt.json(); this.autoContinue=Math.max(0,d.count|0); }
        if(st.ok){ const d=await st.json(); this.streamTimeoutSecs=Math.max(30,d.secs|0); }
        if(ry.ok){ const d=await ry.json(); this.retryMax=Math.max(0,d.max|0); this.retryBackoffMs=Math.max(0,d.backoff_ms|0); }
      } catch(e){}
    },
    async setStreamTimeout(secs) {
      try {
        const r=await fetch('/admin/api/stream-timeout',{method:'POST',headers:authHeaders(),body:JSON.stringify({secs:secs})});
        if(r.ok) { const d=await r.json(); this.streamTimeoutSecs=Math.max(30,d.secs|0); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },
    async setRetry(max, backoffMs) {
      try {
        const r=await fetch('/admin/api/retry',{method:'POST',headers:authHeaders(),body:JSON.stringify({max:max|0, backoff_ms:backoffMs|0})});
        if(r.ok) { const d=await r.json(); this.retryMax=Math.max(0,d.max|0); this.retryBackoffMs=Math.max(0,d.backoff_ms|0); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },