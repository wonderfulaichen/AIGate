// ── 模型表格编辑 ──
// 供应商表单载入 / 模型增删 / 批量应用 (思考档/协议/免费)
// 主要成员: loadProvidersForm / addModel / applyBulkEffort
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    async loadProvidersForm() {
       // 供应商表单首载 (providersFormData 为 null): 独立加载态供骨架使用 (不与 stats 的 loading 耦合).
       // 宽限期 250ms: 首载更快则骨架从不显示 (与 ds-core.init 对 loading 的口径一致), 不闪跳;
       // 一旦显示, 收口时保证最少驻留 1s. 非首载 (刷新/切页带缓存) 不置位 —— 内容不被清空, 无闪烁.
       const firstLoad = this.providersFormData === null;
       let skelShown = false;
       if (firstLoad) {
         this._provSkelT = setTimeout(() => {
           skelShown = true; this.provLoading = true; this._provSkelAt = Date.now();
         }, 250);
       }
       try {
         const r=await fetch('/admin/api/providers', {headers:authHeaders()});
         if(r.ok) {
           const d=await r.json();
           if(d.parsed&&d.parsed.providers) {
              this.providersFormData = d.parsed.providers.map(p => ({
                ...p,
                _origName: p.name,
                _expanded: false,
                _testing: false,
                _testResult: null,
                _errOpen: false,
                _filter: '',
                _selected: {},
                _bulkEffort: '',
                _bulkFmt: '',
                  _bulkFree: false,
                  _keyDraft: '',
                  _keySaving: false,
                  _keyMsg: '',
                  _keyOk: false,
                  _fetching: false,
                models: Object.entries(p.models||{}).map(([id, cfg]) => ({
                  model_id: id,
                  upstream_model: cfg.upstream_model||'',
                  reasoning_effort: cfg.reasoning_effort||'',
                  api_format: cfg.api_format||'',
                  origin: cfg.origin||'',
                  strip_toolcall_reasoning: !!cfg.strip_toolcall_reasoning,
                  price: cfg.price||null,
                  free: !!cfg.free || this.autoFree(id, cfg.upstream_model||''),
                  _isNew: false,
                  _removed: false,
                  _freeTouched: false,
                  _fmtTouched: false,
                  _metaExpanded: false,
                  }))
              }));
              this.isDirty = false;
              // 模型元信息 (上下文/视觉等): 表单加载完成后按需拉取 (24h 服务端缓存).
              this.fetchModelMeta();
            }
          }
        } catch(e){}
        finally {
          // 宽限期计时器收口; 骨架已显示则保证最少驻留 1s (与 ds-stats._endFirstLoad 同口径); 失败也收口, 不卡加载态.
          if (firstLoad) {
            if (this._provSkelT) { clearTimeout(this._provSkelT); this._provSkelT = null; }
            if (skelShown && this.provLoading) {
              const remain = 1000 - (Date.now() - this._provSkelAt);
              if (remain > 0) setTimeout(() => { this.provLoading = false; }, remain);
              else this.provLoading = false;
            }
          }
        }
      },
      addModel(pi) {
      const provName = (this.providersFormData[pi]&&this.providersFormData[pi].name)||'';
      // 预填「供应商/」前缀: 新模型中转 ID 自动成为"供应商/…"形式, 补全剩余即可; 也可直接改.
      this.providersFormData[pi].models.push({model_id:provName+'/',upstream_model:'',reasoning_effort:'',api_format:'',free:false,origin:'manual',_isNew:false,_removed:false,_freeTouched:false,_fmtTouched:false,_metaExpanded:false});
      this.markDirty();
    },
     deleteModel(pi, mi) {
        this.providersFormData[pi].models.splice(mi,1);
        this.markDirty();
      },
      deleteSelectedModels() {
        const prov = this.drawerProv;
        if (!prov || !prov._selected) return;
        const toRemove = [];
        Object.keys(prov._selected).forEach(k => { if (prov._selected[k]) toRemove.push(parseInt(k)); });
        if (!toRemove.length) return;
        toRemove.sort((a,b) => b - a).forEach(i => prov.models.splice(i, 1));
        prov._selected = {};
        this.markDirty();
      },
      markDirty(){ this.isDirty = true; },
      isAllSelected(prov){
        if(!prov.models.length) return false;
        const q=(prov._filter||'').toLowerCase();
        const idxs=prov.models.map((m,i)=>i).filter(i=> !q || (prov.models[i].model_id||'').toLowerCase().includes(q) || (prov.models[i].upstream_model||'').toLowerCase().includes(q));
        return idxs.length>0 && idxs.every(i=> prov._selected && prov._selected[i]);
      },
      toggleAll(prov, checked){
        const q=(prov._filter||'').toLowerCase();
        prov._selected = prov._selected||{};
        prov.models.forEach((m,i)=>{
          if(!q || (m.model_id||'').toLowerCase().includes(q) || (m.upstream_model||'').toLowerCase().includes(q)){
            prov._selected[i]=checked;
          }
        });
        this.markDirty();
      },
      selectedCount(prov){
        if(!prov._selected) return 0;
        return Object.values(prov._selected).filter(Boolean).length;
      },
      clearSelection(prov){ prov._selected={}; },
      applyBulkEffort(pi){
        const prov=this.providersFormData[pi];
        const v=prov._bulkEffort;
        if(v===undefined) return;
        (prov._selected||{});
        Object.keys(prov._selected||{}).forEach(k=>{
          if(prov._selected[k]){ prov.models[k].reasoning_effort=v; }
        });
        this.markDirty();
      },
      applyBulkFmt(pi){
        const prov=this.providersFormData[pi];
        const v=prov._bulkFmt;
        if(v===undefined) return;
        Object.keys(prov._selected||{}).forEach(k=>{
          if(prov._selected[k]){ prov.models[k].api_format=v; prov.models[k]._fmtTouched=true; }
        });
        this.markDirty();
      },
      applyBulkFree(pi, val){
        const prov=this.providersFormData[pi];
        Object.keys(prov._selected||{}).forEach(k=>{
          if(prov._selected[k]){ prov.models[k].free=val; prov.models[k]._freeTouched=true; }
        });
        this.markDirty();
      },
      async deleteSelected(pi){
        const prov=this.providersFormData[pi];
        const idxs=Object.keys(prov._selected||{}).filter(k=>prov._selected[k]).map(Number).sort((a,b)=>b-a);
        if(!idxs.length) return;
        if(!(await confirmAsk(t('confirm_delete_models', idxs.length), { danger: true }))) return;
        idxs.forEach(i=> prov.models.splice(i,1));
        prov._selected={};
        this.markDirty();
      },
      async clearRemoved(pi){
        const prov=this.providersFormData[pi];
        const idxs=prov.models.map((m,i)=> m._removed ? i : -1).filter(i=>i>=0).sort((a,b)=>b-a);
        if(!idxs.length) return;
        if(!(await confirmAsk(t('confirm_clear_removed_models', idxs.length), { danger: true }))) return;
        idxs.forEach(i=> prov.models.splice(i,1));
        this.markDirty();
        toast(t('removed_models_cleaned',idxs.length), 'success');
      },