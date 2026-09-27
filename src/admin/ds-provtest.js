// ── 连通性测试与拉取差异 ──
// 单供应商测试 + 拉取模型的新增/已存在/下架三态
// 主要成员: testProvider / fetchDiffGroups / applyFetchDiff
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

      importModels(pi, event){
        const prov=this.providersFormData[pi];
        const file=event.target.files[0];
        if(!file) return;
        const reader=new FileReader();
        reader.onload=()=>{
          const text=reader.result||'';
          const lines=text.split(/\r?\n/).map(l=>l.trim()).filter(Boolean);
          let added=0;
          for(const line of lines){
            if(line.startsWith('#')) continue;
            const parts=line.split(/[,;\t|]/).map(s=>s.trim()).filter(Boolean);
            if(!parts.length) continue;
            // 中转 ID 一律补全「供应商/」前缀 (与拉取模型一致), 免手动命名且避免跨供应商同名冲突.
            let model_id=this.transitId(prov.name, parts[0]);
            const upstream=parts[1]||model_id;
            const effort=parts[2]||'';
            const fmt=parts[3]||'';
            if(prov.models.some(m=>m.model_id===model_id)) continue;
            prov.models.push({model_id, upstream_model:upstream, reasoning_effort:effort, api_format:fmt, free:this.autoFree(model_id, upstream), _freeTouched:false, _fmtTouched:!!fmt});
            added++;
          }
          this.markDirty();
          toast(t('models_imported',added), 'success');
        };
        reader.readAsText(file);
        event.target.value='';
      },
      exportModels(pi){
        const prov=this.providersFormData[pi];
        const selected=Object.keys(prov._selected||{}).filter(k=>prov._selected[k]).map(k=>prov.models[k]).filter(Boolean);
        const list = selected.length? selected : prov.models;
        const csv=list.map(m=> [m.model_id, m.upstream_model||'', m.reasoning_effort||'', m.api_format||''].join(',')).join('\n');
        const blob=new Blob([csv],{type:'text/csv'});
        const url=URL.createObjectURL(blob);
        const a=document.createElement('a'); a.href=url; a.download=prov.name+'-models.csv'; a.click(); URL.revokeObjectURL(url);
      },
      async testProvider(pi) {
       const prov = this.providersFormData[pi];
       prov._testing = true;
       prov._testResult = null;
       prov._errOpen = false;
       try {
         const r=await fetch('/admin/api/providers/test',{method:'POST',headers:authHeaders(),body:JSON.stringify({endpoint:prov.endpoint})});
         if(r.ok) prov._testResult = await r.json();
         else prov._testResult = {success:false,status:t('request_failed'),latency_ms:0};
       } catch(e){ prov._testResult = {success:false,status:t('network_error'),latency_ms:0}; }
       const result=prov._testResult||{};
       // 仅更新连通性状态; circuit 字段由 fetchHealth 从 breakers 同步, 避免被覆盖成 'closed' 导致重置按钮消失.
       const current=this.health.find(h=>h.provider===prov.name);
       if(current){ current.status_level=result.success?'ok':'error'; current.status_text=result.status||''; current.latency_ms=result.latency_ms||0; }
       prov._testing = false;
       // 测试完成后刷新健康数据, 确保 circuit 状态与后端 breakers 一致.
       await this.fetchHealth();
     },
     // 测试错误展示: 首行截断 96 字 (参照 new-api channel-actions), 点击展开全文 + 复制.
     testErrFirst(s){ const str=String(s||'').split('\n')[0]; return str.length>96?str.slice(0,96)+'…':str; },
     copyTestErr(prov){ if(prov&&prov._testResult) this.copyText(String(prov._testResult.status||''), 'terr'); },
     // 记录页供应商单元格 → 跳供应商页并打开该供应商抽屉 (表单未加载时先加载).
     openLogProvider(log){
       const self=this;
       const go=()=>{
         self.switchTab('providers');
         const i=(self.providersFormData||[]).findIndex(p=>p.name===log.provider);
         if(i>=0) self.openProviderDrawer(i);
       };
       if(!this.providersFormData){ this.loadProvidersForm().then(go); } else go();
     },
     // ── 拉取结果差异视图: 按上游 ID 前缀 (末段首个 '-' 前) 分组, 组头整组勾选 ──
     fetchDiffGroups(){
       const fd=this.fetchDiff; if(!fd) return [];
       const map={};
       for(const it of fd.added){
         const up=String(it.upstream_model||'');
         const seg=up.indexOf('/')>=0?up.slice(up.lastIndexOf('/')+1):up;
         const p=seg.split('-')[0]||seg||'?';
         (map[p]||(map[p]=[])).push(it);
       }
       return Object.keys(map).sort().map(k=>({prefix:k, items:map[k]}));
     },
     fetchDiffSelCount(){ return this.fetchDiff ? this.fetchDiff.added.filter(it=>this.fetchDiffSel[it.model_id]).length : 0; },
     fetchDiffGroupAll(prefix, on){
       const g=this.fetchDiffGroups().find(x=>x.prefix===prefix); if(!g) return;
       for(const it of g.items) this.fetchDiffSel[it.model_id]=!!on;
     },
     // 应用所选: 保留勾选的新增模型, 未勾选的从表单移除 (尚未保存, 只影响表单).
     applyFetchDiff(){
       const fd=this.fetchDiff;
       if(fd){
         const prov=this.providersFormData[fd.pi];
         const drop=new Set(fd.added.filter(it=>!this.fetchDiffSel[it.model_id]).map(it=>it.model_id));
         if(prov && drop.size){
           prov.models=prov.models.filter(m=>!drop.has(m.model_id));
           prov._newModels=Math.max(0,(prov._newModels||0)-drop.size);
           this.markDirty();
         }
       }
       this.showFetchDiff=false; this.fetchDiff=null;
     },
     // 取消: 不导入任何新增 (全部移除); 已存在的下架标记保留 (反映上游事实).
     cancelFetchDiff(){
       const fd=this.fetchDiff;
       if(fd && fd.added.length){
         const prov=this.providersFormData[fd.pi];
         const drop=new Set(fd.added.map(it=>it.model_id));
         if(prov){
           prov.models=prov.models.filter(m=>!drop.has(m.model_id));
           prov._newModels=Math.max(0,(prov._newModels||0)-fd.added.length);
           this.markDirty();
         }
       }
       this.showFetchDiff=false; this.fetchDiff=null;
     },