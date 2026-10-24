// ── 设置项读写 ──
// 缓存 / 裁剪 / 续写 / 剥离 / recall / 截断阈值的 GET+POST
// 主要成员: fetchCache / setStripReasoning / setRecall
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    async fetchCache() {
      // ⚠️ 合并而不是整体替换: 后端若返回**不完整**的 JSON (旧版本 / 出错路径),
      // 整体替换会让所有字段变 undefined → 界面显示空白, 且**不报错**。
      // 合并保留已知旧值, 只覆盖后端实际给出的字段。
      try {
        const r=await fetch('/admin/api/cache', {headers:authHeaders()});
        if(r.ok) {
          const d = await r.json();
          this.cache = Object.assign({}, this.cache || {}, d);
        }
      } catch(e){}
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
    /// 数字输入框的统一入口: `@change="onNumInput($event, min, max, v => setXxx(v), oldVal)"`。
    ///
    /// 行为: 解析无效 (清空 / 非数字) → **不提交**, 把控件回填成旧值并 toast 说明;
    /// 解析有效 → 夹到 [min,max] 后调用 setter。
    /// ⚠️ 必须回填**旧值**而不是清空: 清空会让界面显示空白, 而实际配置没变 ——
    ///    又是"界面与现实不一致"。旧值由调用方传入 (它能从状态里取到)。
    onNumInput(ev, min, max, apply, oldVal) {
      const el = ev && ev.target;
      const v = this.parseNumInput(el ? el.value : '', { min, max });
      if (v == null) {
        toast(t('num_input_invalid', min, max), 'warn', 'num_invalid');
        if (el) el.value = (oldVal == null ? '' : String(oldVal));
        return;
      }
      return apply(v);
    },
    /// 数字输入框的安全解析 —— **空值/非法值不提交**, 而不是静默回落成 0。
    ///
    /// 为什么必须有这个 (2026-10-19 实测踩到): 输入框写的是
    /// `@change="setX(parseInt($event.target.value)||0)"`。用户想把 20 改成 30 时,
    /// 只要中间出现一次 `change` (全选后输入、或按清空键), `parseInt('')` 是 NaN,
    /// `NaN || 0` = **0** —— 于是"长会话历史裁剪"被**静默关掉**: 开关变灰、配置区消失、
    /// **没有任何提示**。实测确认: 清空输入框 → turns 从 20 变 0, 无 toast。
    /// 同类问题在 8 个数字输入框上完全一样 (裁剪/续写/截断/超时/重试×2/缓存×2)。
    ///
    /// 语义: 返回 `null` = "这次输入无效, 不要提交", 调用方原样保留旧值并把控件回填,
    /// 用户看得见自己输错了什么, 而不是功能悄悄消失。
    parseNumInput(raw, opts) {
      const s = String(raw == null ? '' : raw).trim();
      if (s === '') return null;                 // 清空 ≠ 归零, 更 ≠ 关功能
      const n = Number(s);
      if (!Number.isFinite(n)) return null;      // 非数字
      const min = opts && opts.min != null ? opts.min : null;
      const max = opts && opts.max != null ? opts.max : null;
      if (min != null && n < min) return min;    // 越界夹取 (与 min/max 属性同口径)
      if (max != null && n > max) return max;
      return n;
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
    // ── 用量热力图 (2026-10-11, 对标 dsh-our-free-model) ──
    // 数据源是日级 rollup（持久化、跨日志滚动窗口），故能覆盖 17 周 ——
    // 前端趋势图那边最大只到 logs.jsonl 的窗口，两者不是一回事。
    async fetchHeatmap() {
      // 3s 轮询里的接口 —— 必须带竞态守卫（见 ds-guard.js）：
      // 慢响应与下一拍交错回来会让热力图偶尔往回跳一下。
      const d = await this.guardedFetch('heatmap', '/admin/api/heatmap?weeks=17', { headers: authHeaders() });
      if (d) this.heatmap = d;
    },
    // ── 事件台账 (2026-10-11): 关键事件的时间线 ──
    // 用户原话:「熔断、报错等等连工作报告、异常通知都没做。出问题得用户自己去一点点找。」
    // 这里就是那份"工作报告": 熔断开合、上游连续失败、额度将尽、账号作废等按时间列出,
    // 每条带**原因** —— 不必再去日志里猜关键词。
    async fetchEvents() {
      // 同样带竞态守卫：事件台账是「按时间列出」的，旧响应后到会让列表**倒退**，
      // 看起来像"刚发生的事件又消失了"。用 guardedFetch 只认最后一次。
      const d = await this.guardedFetch('events', '/admin/api/events?limit=100', { headers: authHeaders() });
      if (d) {
          this.events = d.events || [];
          // 未读告警数 (托盘红点的同一数据源)。
          //
          // ⚠️ **不在这里做确认 (ack)** —— 权威的确认在**原生侧**(main.rs 事件循环:
          // 窗口可见即 ack)。原因: WebView 隐藏到托盘后**仍会轮询本接口**, 若靠前端
          // 判断"用户是否在看"就必须依赖 `visibilityState`, 而那是个 WebView 行为假设 ——
          // 一旦隐藏后仍报 visible, 红点会在用户没看的情况下被清掉, 正是要解决的「挂后台不知道」。
          // 原生侧用 `window.is_visible()` 是权威值, 不依赖任何 WebView 行为。
          this.eventsUnread = d.unread || 0;
      }
    },
    async clearEvents() {
      await confirmAsk(t('events_clear_confirm'), { danger: true, onOk: async () => {
        try { await fetch('/admin/api/events/clear', { method: 'POST', headers: authHeaders() }); } catch (e) {}
        await this.fetchEvents();
      }});
    },
    // 事件时间: 今天的只显示时分秒 (台账多数是今天的, 省掉日期更好扫读), 更早的带日期。
    eventTime(ts) {
      const d = new Date((Number(ts) || 0) * 1000);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      return sameDay ? `${hh}:${mm}:${ss}` : this.fmtDateTime(Math.floor(Number(ts) || 0));
    },
    // 事件人话: kind → i18n 文案, 未知取值照实显示 (上游/后端加新类别时吞掉就等于埋了线索).
    eventLabel(kind) {
      const map = {
        breaker_open: 'ev_breaker_open', breaker_closed: 'ev_breaker_closed',
        breaker_half_open: 'ev_breaker_half_open', breaker_reset: 'ev_breaker_reset',
        upstream_failing: 'ev_upstream_failing', upstream_recovered: 'ev_upstream_recovered',
        // 上游中途断流 (2026-10-13): 与"连续失败"不同 —— 它是"打通了、正在出字、被掐断"。
        upstream_cut: 'ev_upstream_cut',
        quota_low: 'ev_quota_low', account_dead: 'ev_account_dead',
        providers_reloaded: 'ev_providers_reloaded', started: 'ev_started',
        // 端口顺延原先漏在表外 → 显示成裸 `port_fallback`（本项目的老坑：漏一个键就露原文）。
        port_fallback: 'ev_port_fallback',
      };
      const k = map[kind];
      return k ? t(k) : (kind || '');
    },
    // 台账的整体一句话结论 —— 让用户不必逐条读完才知道"要不要担心".
    get eventsTakeaway() {
      const evs = this.events || [];
      if (!evs.length) return '';
      const errs = evs.filter(e => e.severity === 'err').length;
      const warns = evs.filter(e => e.severity === 'warn').length;
      if (errs) return t('events_takeaway_err', errs);
      if (warns) return t('events_takeaway_warn', warns);
      return t('events_takeaway_ok');
    },
    get eventsHasProblem() {
      return (this.events || []).some(e => e.severity === 'err');
    },
    // 事件按"要不要我处理"分两档 (2026-10-24, 按 WaLiAPI 的指标降级带思路)。
    //
    // 为什么: 真实数据里这张卡 8 条有 5 条是「服务已启动」，把真正的
    // 「上游中途断流」挤到看不见的地方 —— 卡片标题叫"关键事件"，内容却是启动流水账。
    // 启动/重载属于"系统正常运转的痕迹"，不是"需要关注的事"，应降级到折叠区。
    get eventsNotable() {
      return (this.events || []).filter(e => e.kind !== 'started' && e.kind !== 'providers_reloaded');
    },
    get eventsRoutine() {
      return (this.events || []).filter(e => e.kind === 'started' || e.kind === 'providers_reloaded');
    },
    // 该显示哪一档: 有值得关注的就只看它们（并提示还有多少例行记录）；
    // 一条都没有时才回落到例行记录 —— 否则空卡更让人困惑。
    get eventsShown() {
      const notable = this.eventsNotable;
      return notable.length ? notable : this.eventsRoutine;
    },
    // 静默降级计数: 解析失败导致优化/翻译未生效的次数. 用于区分
    // "这次确实没什么可省" 与 "我们根本没解析成功".
    async fetchDegradations() {
      // 同样在 3s 轮询里，补竞态守卫。
      const d = await this.guardedFetch('degradations', '/admin/api/degradations', { headers: authHeaders() });
      if (d) this.degradations = d;
    },
    get degradationTotal() {
      const d = this.degradations||{};
      return Object.values(d).reduce((a,b)=>a+(Number(b)||0),0);
    },
    // ── 降级可回取 (recall): 留存被优化剔离的原文, 供事后核查 ──
    async fetchRecall() {
      // 同 fetchCache: 合并而非整体替换, 避免后端返回不完整时字段变 undefined。
      try {
        const r=await fetch('/admin/api/recall', {headers:authHeaders()});
        if(r.ok) {
          const d = await r.json();
          this.recall = Object.assign({}, this.recall || {}, d);
        }
      } catch(e){}
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
    // ── thinking 提纯 (2026-10-17) ──
    // 提纯是**剥离的另一种模式**: 开 `rule` 时历史推理链被压成节点链再留下, 而不是整段删除。
    // ⚠️ `effective` = 配置开 **且** recall 也开 (recall 是硬性前提: 提纯有损, 原文是唯一
    //    可逆通道)。前端据此显示"需要先开降级可回取" —— 否则用户开了却没生效, 只能猜。
    async fetchThinkingPurify() {
      try {
        const r=await fetch('/admin/api/thinking-purify', {headers:authHeaders()});
        if(r.ok) { const d=await r.json(); this.thinkingPurify=d.mode||'off'; this.purifyRecallOn=!!d.recall_on; this.purifyEffective=!!d.effective; }
      } catch(e){}
    },
    async setThinkingPurify(val) {
      const mode = (val==='rule') ? 'rule' : 'off';
      // 同 setStripReasoning: 失败要提示, 否则开关弹回原位没人知道原因。
      try {
        const r=await fetch('/admin/api/thinking-purify',{method:'POST',headers:authHeaders(),body:JSON.stringify({mode})});
        if(r.ok) { const d=await r.json(); this.thinkingPurify=d.mode||'off'; this.purifyRecallOn=!!d.recall_on; this.purifyEffective=!!d.effective; }
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