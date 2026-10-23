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
      async testProvider(pi, recover) {
       const prov = this.providersFormData[pi];
       prov._testing = true;
       prov._testResult = null;
       prov._errOpen = false;
       try {
         // 带上 name: 后端据此用**真实密钥**测（这样"密钥对不对"才在被测项里）。
         // 不传 name 时后端退回假密钥探活, 并在结果里标 key_checked=false。
         //
         // 带上 api_key (草稿里正在编辑的密钥): 面板的密钥编辑是"改完即存"的, 但端点/
         // 协议/模型表都停在表单草稿里。用户改完端点再点「测试」时, 若只按已保存的
         // provider 找凭据, "我眼前改的东西"与"实际被测的东西"就不是同一份 ——
         // 测试通过、保存后却报错, 而界面毫无提示。草稿优先让"测的"与"看的"对齐。
         // 空串 = 用户清空了输入框 → 语义是"没有草稿密钥", 后端会回落已保存链。
         //
         // recover=true（「测试并恢复」）: 测试成功后顺手关熔断 —— 由用户显式触发,
         // 因为"上游通了"这个证据是他自己看到的判断, 不做成自动行为。
         const r=await fetch('/admin/api/providers/test',{method:'POST',headers:authHeaders(),
           body:JSON.stringify({
             endpoint:prov.endpoint,
             name:prov.name,
             api_key:(prov._keyDraft||'').trim(),
             recover:!!recover
           })});
         if(r.ok) prov._testResult = await r.json();
         else prov._testResult = {success:false,stage:'local',stage_text:t('stage_local'),status:t('request_failed'),latency_ms:0};
       } catch(e){ prov._testResult = {success:false,stage:'local',stage_text:t('stage_local'),status:t('network_error'),latency_ms:0}; }
       const result=prov._testResult||{};
       // 「测试并恢复」成功 → 明说熔断已关 (把用户点它的目的与结果对上)
       if(result.recovered){
         toast(t('test_recovered'), 'ok');
         await this.fetchHealth();
       }
       // 仅更新连通性状态; circuit 字段由 fetchHealth 从 breakers 同步, 避免被覆盖成 'closed' 导致重置按钮消失.
       const current=this.health.find(h=>h.provider===prov.name);
       if(current){ current.status_level=result.success?'ok':'error'; current.status_text=result.status||''; current.latency_ms=result.latency_ms||0; }
       prov._testing = false;
       // 测试完成后刷新健康数据, 确保 circuit 状态与后端 breakers 一致.
       await this.fetchHealth();
     },
     // 测试所用密钥的来源说明 (2026-10-24): 把"这次测的是哪个密钥"变成可见事实。
     // 后端 key_source 三态: draft=表单草稿 / saved=已保存链 / none=未使用(假密钥探活)。
     // 为什么必须显示: 面板密钥改完即存、其余字段停在草稿, 用户无法从界面看出
     // "测的与我改的是否同一份" —— 这正是"测试通过、保存后不能用"的困惑来源。
     testKeySourceText(s){
       const k = s && s.key_source;
       if (k === 'draft') return t('test_key_draft');
       if (k === 'saved') return t('test_key_saved');
       if (s && s.key_checked === false) return t('test_key_none');
       return '';
     },
     // 环节前缀: 结果横幅首要展示"哪一环"（用户明确要求报错看得出环节）。
     // 后端给 stage/stage_text; 老接口没这字段时返回空串（不显示前缀）。
     testStage(s){
       const r = s||{};
       if(!r.stage) return '';
       return '[' + (r.stage_text || t('stage_'+r.stage) || r.stage) + '] ';
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