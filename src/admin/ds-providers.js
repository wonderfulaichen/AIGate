// ── 供应商表单 (列表侧) ──
// 密钥增删 / 抽屉开合 / 分节完成度 / 可见性筛选 / 徽章复制
// 主要成员: openProviderDrawer / drawerSecDone / copyBadge
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    async fetchProxyConfig() { try { const r=await fetch('/admin/api/proxy-config', {headers:authHeaders()}); if(r.ok) this.proxyStatus=await r.json(); } catch(e){} },
    // 可点击徽章复制 (P2): 徽章内就地复制短值 (key 后缀 / 模型 ID / 供应商名等).
    // 复用全局 copyToClipboard (含非安全上下文回退), 与其它复制按钮共用 copiedField 状态位.
    copyBadge(val, key) { return copyBadge.call(this, val, key); },
    // 徽章列表溢出 (P2): 只显示前 n 项, 其余折成 "+N"; 返回 {shown, more, title}.
    // 原先把所有协议/品牌徽章平铺, 模型多时会把表格行撑成多行 (行高跳动).
    badgeList(list, n) {
      const arr = (list || []).map(x => String(x));
      const k = (n == null ? 2 : n);
      return { shown: arr.slice(0, k), more: Math.max(0, arr.length - k), title: arr.join(' · ') };
    },
    // 密钥是供应商的子资源: 按 provider 维度保存/清空.
    //
    // ⚠️ 2026-10-24 改为**可撤销的草稿态**，不再即时落盘。
    // 改造前：点「清除」→ 立即 PUT 空值 → 后端当场删掉 Key，无确认、无撤销。
    // Key 是本项目最不可再生的一项数据（封了/丢了自己去厂商后台重签），
    // 而误点一次就失效、失败时只有一句 toast —— 这个风险与收益完全不成比例。
    // 现在：点「清除」只置草稿标记 + 清空输入框，真正删除发生在「保存密钥」时；
    // 期间显示「将清除，可撤销」，一点即回滚。
    requestClearKey(pi) {
      const prov = this.drawerProv || this.providersFormData[pi];
      if (!prov) return;
      prov._clearRequested = true;
      prov._keyDraft = '';
      prov._keyDraftTouched = false;
    },
    undoClearKey(pi) {
      const prov = this.drawerProv || this.providersFormData[pi];
      if (!prov) return;
      prov._clearRequested = false;
    },
    async clearProviderKey(pi) {
      // 真正落盘（仅由「保存密钥」或确认后的路径调用）。
      const prov = this.providersFormData[pi] || this.drawerProv;
      prov._keyDraft='';
      try {
        const r=await fetch('/admin/api/keys',{method:'PUT',headers:authHeaders(),body:JSON.stringify({provider:prov.name,value:''})});
        const d=await r.json();
        // ⚠️ `keyMsg` 曾是死变量（无渲染点）→ 失败完全静默。现由 ds-core.js 的统一出口
        // 弹 toast；这里只需保证**成功与失败都赋值**（失败不再落进 catch 的空档）。
        if(r.ok) { this.keyMsg=t('key_cleared',prov.name); this.fetchKeys(); prov._clearRequested=false; setTimeout(()=>this.keyMsg='',3000); }
        else { this.keyMsg=t('key_clear_failed',prov.name)+(d.error?('：'+d.error):''); }
      } catch(e){ this.keyMsg=t('network_error'); }
    },
    async saveDrawerKey() {
      const prov = this.drawerProv;
      if (!prov) return;
      // 草稿处于「待清除」：这里才真正删除（且先给一次确认 —— 不可再生数据）。
      if (prov._clearRequested) {
        if (!(await confirmAsk(t('confirm_clear_key', prov.name), { danger: true }))) return;
        prov._keySaving = true; prov._keyMsg = ''; prov._keyOk = false;
        await this.clearProviderKey(this.providersFormData.indexOf(prov));
        prov._keySaving = false;
        prov._keyMsg = prov._keyMsg || t('key_cleared', prov.name);
        prov._keyOk = true;
        setTimeout(()=>{ if(prov) prov._keyMsg=''; }, 3000);
        return;
      }
      if (!(prov._keyDraft||'').trim()) return;
      prov._keySaving = true; prov._keyMsg = ''; prov._keyOk = false;
      try {
        const r = await fetch('/admin/api/keys', { method:'PUT', headers:authHeaders(), body:JSON.stringify({provider:prov.name, value:prov._keyDraft.trim()}) });
        const d = await r.json();
        if (r.ok) { prov._keyDraft=''; prov._clearRequested=false; prov._keyMsg=t('saved'); prov._keyOk=true; this.fetchKeys(); }
        else { prov._keyMsg=d.error||t('save_failed'); prov._keyOk=false; }
      } catch(e) { prov._keyMsg=t('network_error'); prov._keyOk=false; }
      prov._keySaving = false;
      setTimeout(()=>{ if(prov) prov._keyMsg=''; }, 3000);
    },
    // 删除供应商 (仅从表单移除, 需点"保存配置"后生效; 后端级联清除其密钥).
    async deleteProvider(pi) {
      const prov = this.providersFormData[pi];
      if (!(await confirmAsk(t('confirm_delete_provider', prov.name), { danger: true }))) return;
      this.providersFormData.splice(pi,1);
      this.providersFormMsg = t('provider_removed',prov.name);
      this.providersFormOk = true;
      this.markDirty();
      setTimeout(()=>this.providersFormMsg='',4000);
    },
    modelVisible(m, prov){
      const q=(prov._filter||'').toLowerCase();
      if(q && !((m.model_id||'').toLowerCase().includes(q) || (m.upstream_model||'').toLowerCase().includes(q))) return false;
      const brand=prov._brandFilter||'';
      if(brand){
        const b=this.brandFor(m.upstream_model,prov.name);
        if(b.label!==brand) return false;
      }
      const eff=prov._effFilter||'';
      if(eff && (m.reasoning_effort||'')!==eff) return false;
      // 价格筛选: 'free' 仅看免费 (用户标记 或 名称含 free/免费), 'paid' 仅看付费
      const free=prov._freeFilter||'';
      if(free==='free' && !this.isFree(m)) return false;
      if(free==='paid' && this.isFree(m)) return false;
      // 下架筛选: 'active' 仅未下架, 'removed' 仅下架. 缺省 (空) 行为
      // 同 'active' — 已下架模型默认隐藏, 避免「拉取后被标下架」一直占视线.
      const rm=prov._removedFilter||'active';
      if(rm==='active' && m._removed) return false;
      if(rm==='removed' && !m._removed) return false;
      // 来源筛选: manual/fetched; fetched 含 origin 缺失 (兼容旧配置).
      const og=prov._originFilter||'';
      if(og==='manual' && m.origin!=='manual') return false;
      if(og==='fetched' && m.origin==='manual') return false;
      return true;
    },
    isFree(m){
      // 与 Rust is_free 一致: 显式 free:true → 免; free:false → 付; 否则
      // 按 upstream_model / model_id 字符串 (小写) 是否含 'free' 或 '免费' 自动识别.
      if(m && m.free===true) return true;
      if(m && m.free===false) return false;
      const s=((m&&m.upstream_model)||(m&&m.model_id)||'').toLowerCase();
      return s.includes('free') || s.includes('免费');
    },
    drawerBrands(){
      if(!this.drawerProv) return [];
      const seen=new Map();
      (this.drawerProv.models||[]).forEach(m=>{
        const b=this.brandFor(m.upstream_model,this.drawerProv.name);
        if(!seen.has(b.label)) seen.set(b.label,b);
      });
      return [...seen.values()];
    },
    filteredModelCount(){
      if(!this.drawerProv) return 0;
      return (this.drawerProv.models||[]).filter(m=>this.modelVisible(m,this.drawerProv)).length;
    },
    totalModelCount(){
      return (this.providersFormData||[]).reduce((n,p)=>n+((p.models||[]).length),0);
    },
    provMatches(prov){
      const q=(this.provSearch||'').toLowerCase().trim();
      if(!q) return true;
      if((prov.name||'').toLowerCase().includes(q)) return true;
      if((prov.endpoint||'').toLowerCase().includes(q)) return true;
      return (prov.models||[]).some(m=>((m.model_id||'').toLowerCase().includes(q))||((m.upstream_model||'').toLowerCase().includes(q)));
    },
    // 该供应商下模型**实际可用**的协议集合 (列表页绿色徽章的数据源).
    // 用 availableApiFormats (探测能力 ∪ 配置/推断) 而非直读 m.api_format:
    // 后者在「拉取模型」后多为空 (协议改由探测能力自动决定), 直读会一律显示 OpenAI 而与实际不符。
    provProtocols(prov){
      const s=new Set();
      (prov.models||[]).forEach(m=>{
        const list=this.availableApiFormats(m,prov);
        (list.length?list:['openai']).forEach(f=>s.add(t('fmt.'+f)));
      });
      return [...s];
    },
    get visibleProviders(){
      if(!this.providersFormData) return [];
      return this.providersFormData.filter(p=>{
        return this.provMatches(p);
      });
    },
    // 凭据就绪判定 (列表 badge 颜色 / 抽屉 key 分节完成度共用):
    // 账号形式供应商看账号状态 (有条目且未作废即算就绪 —— 过期由后端自动续期, 不算缺凭据),
    // 其余看 API key.
    // 凭据就绪判定 (列表 badge 颜色 / 抽屉 key 分节完成度共用):
    // 账号形式供应商看账号状态 (有条目且未作废即算就绪 —— 过期由后端自动续期, 不算缺凭据),
    // 其余看 API key.
    // 账号状态是**数组** (多账号, 2026-10-07): 取本次会真正被选中的那个 (active) 代表该行.
    // 全部不可用 (都 dead/冷却) 时退回第一个, 让用户看到"为什么不可用".
    _acct(name){
      const a=this.accountsStatus&&this.accountsStatus[name];
      if(!a || !a.length) return null;
      return a.find(x=>x.active) || a[0];
    },
    acctCount(name){
      const a=this.accountsStatus&&this.accountsStatus[name];
      return a ? a.length : 0;
    },
    // 账号行的 tooltip: 昵称 + 登录名 + 身份指纹 (分辨同名账号要靠后两者).
    acctTitle(a){
      if(!a) return '';
      const parts=[];
      if(a.name) parts.push(t('acc_name')+': '+a.name);
      if(a.username) parts.push(t('acc_username')+': '+a.username);
      if(a.identity) parts.push(t('acc_identity_hint')+': '+a.identity);
      return parts.join(' · ');
    },
    // 单个账号的到期文案 (登录弹窗列表用): 天/时/分, 过期与无期限各有说法.
    acctExpiryText(a){
      if(!a) return '';
      if(a.expires_in_secs==null) return t('acc_noexp');
      if(a.expires_in_secs<=0) return t('acc_expired');
      const s=a.expires_in_secs;
      if(s>=86400) return t('acc_active_d', Math.ceil(s/86400));
      if(s>=3600) return t('acc_active_h', Math.ceil(s/3600));
      return t('acc_active_m', Math.ceil(s/60));
    },
    hasCred(name){
      const acc=this._acct(name);
      if(acc) return !acc.dead && !acc.cooling;
      const k=this.keysByProvider[name];
      return !!(k&&k.configured);
    },
    keyStatusText(name){
      // 账号形式 (auth.mode=account): 显示账号状态, 不显示 API key 行.
      // 多账号: 「2 个账号 · 剩余 30 天」(active 那个的到期) —— 账号数在卡片徽章里另显.
      const list=this.accountsStatus&&this.accountsStatus[name];
      const acc=this._acct(name);
      if(acc){
        const one=(x)=>{
          if(x.dead) return t('acc_dead');
          if(x.cooling) return t('acc_cooling');
          if(x.expires_in_secs!=null){
            if(x.expires_in_secs<=0) return t('acc_expired');
            // 剩余时长: ≥1 天按天, ≥1 小时按小时, 否则分钟 (向上取整, 避免显示 0 分钟).
            const s=x.expires_in_secs;
            if(s>=86400) return t('acc_active_d', Math.ceil(s/86400));
            if(s>=3600) return t('acc_active_h', Math.ceil(s/3600));
            return t('acc_active_m', Math.ceil(s/60));
          }
          return t('acc_noexp');
        };
        if(list && list.length>1) return t('acc_n_of', list.length, one(acc));
        return one(acc);
      }
      const key=this.keysByProvider[name];
      if(!key || !key.configured) return t('key_unconfigured');
      return key.suffix ? t('key_configured_masked',key.suffix) : t('key_configured');
    },
    // ── 行内展开（2026-10-24，对照 WaLiAPI 的 ChannelsPage.tsx:519-600）──
    // 之前点整行/整卡只能**开抽屉**，看一眼模型列表也要跳到另一个界面。
    // WaLiAPI 的做法是就地展开：可用模型（点胶囊复制）、Key 状态、测试结果、统计
    // 全在卡里看到 —— 只有"改配置"才进表单。
    // 现在点行/卡 = 展开/收起；「编辑」按钮仍然直接开抽屉（两条路都保留）。
    toggleProvExpand(name){
      // ⚠️ 重建对象再赋值：Alpine 3 不追踪动态新增属性（本项目多处踩过）
      const cur = this.provExpanded || {};
      const next = Object.assign({}, cur);
      if (next[name]) delete next[name]; else next[name] = true;
      this.provExpanded = next;
    },
    isProvExpanded(name){ return !!(this.provExpanded && this.provExpanded[name]); },
    // 复制单个模型 ID（WaLiAPI 的 handleCopyModel：点胶囊即复制）
    copyModelId(id){
      this.copyText(String(id || ''), 'model:' + id);
    },
    openProviderDrawer(pi){
      const prov=this.providersFormData[pi];
      if(!prov) return;
      if(prov._filter===undefined) prov._filter='';
      if(prov._brandFilter===undefined) prov._brandFilter='';
      if(prov._effFilter===undefined) prov._effFilter='';
      // 鉴权头草稿随抽屉打开回填 (来自已保存的 auth.header / auth.prefix)。
      // 直接用 `|| ''` 无法区分"没写"与"显式空串", 但那正是这两个草稿的职责:
      // 空草稿 = 未声明, 由下面的 setter 决定写什么进 auth。
      if(prov._authHeader===undefined) prov._authHeader=(prov.auth&&prov.auth.header)||'';
      if(prov._authPrefix===undefined) prov._authPrefix=(prov.auth&&prov.auth.prefix)||'';
      try { this.drawerAdvOpen = localStorage.getItem('aigate_drawer_adv') === '1'; } catch(e) {}
      if (!this.drawerProv) this._lastFocus = document.activeElement; // 记录触发按钮, 关闭时归还
      this.drawerProv=prov;
    },
    // ── 鉴权头声明 (auth.header / auth.prefix) ──
    // 语义 (与后端 AuthConfig::header_name / prefix_value 对齐):
    //   header 留空        → 不写字段 (缺省 Authorization)
    //   header 填了        → 写字段
    //   prefix 留空 + 头仍是缺省 Authorization → 不写字段 (标准 Bearer 形态)
    //   prefix 留空 + 头是自定义             → 写**显式空串** (x-api-key / api-key 都不带前缀)
    //   这条规则是为了消掉输入框的固有歧义: 用户填了 x-api-key 却没法输入"无前缀",
    //   若写成 Bearer 会被上游拒; 而自定义头配非 Bearer 前缀是极少数 (可用还原默认兜底)。
    setAuthHeader(v){
      const p=this.drawerProv; if(!p) return;
      const val=String(v||'').trim();
      p.auth=p.auth||{mode:'api_key'};
      if(val) p.auth.header=val; else delete p.auth.header;
      this.setAuthPrefix(p._authPrefix); // 头名变了 → 前缀规则跟着重算
    },
    setAuthPrefix(v){
      const p=this.drawerProv; if(!p) return;
      const val=String(v||'');
      p.auth=p.auth||{mode:'api_key'};
      const hdr=String(p.auth.header||'').trim();
      if(val) { p.auth.prefix=val; }
      else if(hdr && hdr.toLowerCase()!=='authorization') { p.auth.prefix=''; } // 自定义头 + 无前缀
      else { delete p.auth.prefix; }                                          // 缺省头 → 用默认 Bearer
      // 清空到"什么都没声明"时, 把空的 auth 段整个去掉 (避免写进 providers.json 一个空对象)
      if(!p.auth.header && p.auth.prefix===undefined && (p.auth.mode||'api_key')==='api_key' && !p.auth.refresh) delete p.auth;
    },
    // ── 抽屉分节完成度 (参照 new-api channel-mutate-drawer 精简版) ──
    // basic=名称+endpoint; key=已保存 Key; models=存在已填上游 ID 的模型 (与接入引导同口径).
    drawerSecDone(k, p){
      if(!p) return false;
      if(k==='basic') return !!(String(p.name||'').trim() && String(p.endpoint||'').trim());
      if(k==='key'){ return this.hasCred(p.name); }
      if(k==='models') return (p.models||[]).some(m=>!m._removed && m.upstream_model);
      return false;
    },
    drawerSecCount(p){ return ['basic','key','models'].filter(k=>this.drawerSecDone(k,p)).length; },
    scrollDrawerSec(k){
      const el=document.getElementById('drsec-'+k);
      if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
    },
    toggleDrawerAdv(){
      this.drawerAdvOpen=!this.drawerAdvOpen;
      try { localStorage.setItem('aigate_drawer_adv', this.drawerAdvOpen ? '1' : '0'); } catch(e) {}
    },
    closeProviderDrawer(){
      const f = this._lastFocus; this._lastFocus = null;
      this.drawerProv=null;
      if (f && f.focus) this.$nextTick(() => { try { f.focus(); } catch (e) {} });
    },