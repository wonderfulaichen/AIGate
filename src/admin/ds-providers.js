
    async fetchProxyConfig() { try { const r=await fetch('/admin/api/proxy-config', {headers:authHeaders()}); if(r.ok) this.proxyStatus=await r.json(); } catch(e){} },
    // 密钥是供应商的子资源: 按 provider 维度保存/清空.
    async clearProviderKey(pi) {
      const prov = this.providersFormData[pi];
      prov._keyDraft='';
      try {
        const r=await fetch('/admin/api/keys',{method:'PUT',headers:authHeaders(),body:JSON.stringify({provider:prov.name,value:''})});
        const d=await r.json();
        if(r.ok) { this.keyMsg=t('key_cleared',prov.name); this.fetchKeys(); setTimeout(()=>this.keyMsg='',3000); }
        else this.keyMsg=d.error||t('save_failed');
      } catch(e){ this.keyMsg=t('network_error'); }
    },
    async saveDrawerKey() {
      const prov = this.drawerProv;
      if (!prov || !(prov._keyDraft||'').trim()) return;
      prov._keySaving = true; prov._keyMsg = ''; prov._keyOk = false;
      try {
        const r = await fetch('/admin/api/keys', { method:'PUT', headers:authHeaders(), body:JSON.stringify({provider:prov.name, value:prov._keyDraft.trim()}) });
        const d = await r.json();
        if (r.ok) { prov._keyDraft=''; prov._keyMsg=t('saved'); prov._keyOk=true; this.fetchKeys(); }
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
    provProtocols(prov){
      const s=new Set((prov.models||[]).map(m=>(m.api_format||'')===''?'OpenAI':m.api_format));
      return [...s];
    },
    get visibleProviders(){
      if(!this.providersFormData) return [];
      return this.providersFormData.filter(p=>{
        return this.provMatches(p);
      });
    },
    keyStatusText(name){
      const key=this.keysByProvider[name];
      if(!key || !key.configured) return t('key_unconfigured');
      return key.suffix ? t('key_configured_masked',key.suffix) : t('key_configured');
    },
    openProviderDrawer(pi){
      const prov=this.providersFormData[pi];
      if(!prov) return;
      if(prov._filter===undefined) prov._filter='';
      if(prov._brandFilter===undefined) prov._brandFilter='';
      if(prov._effFilter===undefined) prov._effFilter='';
      try { this.drawerAdvOpen = localStorage.getItem('aigate_drawer_adv') === '1'; } catch(e) {}
      if (!this.drawerProv) this._lastFocus = document.activeElement; // 记录触发按钮, 关闭时归还
      this.drawerProv=prov;
    },
    // ── 抽屉分节完成度 (参照 new-api channel-mutate-drawer 精简版) ──
    // basic=名称+endpoint; key=已保存 Key; models=存在已填上游 ID 的模型 (与接入引导同口径).
    drawerSecDone(k, p){
      if(!p) return false;
      if(k==='basic') return !!(String(p.name||'').trim() && String(p.endpoint||'').trim());
      if(k==='key'){ const kb=this.keysByProvider[p.name]; return !!(kb && kb.configured); }
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