// ── 保存与拉取 ──
// providers.json 落盘 + 上游模型拉取 + 新增供应商
// 主要成员: saveProvidersForm / fetchProviderModels / confirmAddProvider
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

     // 拉取模型: 后端仅写入内存注册表 (即时生效) 并返回模型列表; 这里合并进表单,
    // 待用户点"保存配置"才持久化 —— 避免直接写盘冲掉表单里其它未保存改动.
    async fetchProviderModels(pi) {
      const prov = this.providersFormData[pi];
      prov._fetching = true;
      try {
        const r = await fetch('/admin/api/providers/'+encodeURIComponent(prov.name)+'/fetch-models', {method:'POST', headers:authHeaders()});
        const d = await r.json();
        if (r.ok && d.success) {
          // 去重: 基于 upstream_model (与后端 add_models 一致), 而非 model_id.
          const existingUpstream = new Set(prov.models.map(m=>m.upstream_model||m.model_id));
          let added=0;
          const addedItems=[];
          for (const id of (d.models||[])) {
            if (!existingUpstream.has(id)) {
              // 中转 ID 一律生成为「供应商/上游模型ID」(上游自带斜杠也照加, 见 transitId),
              // 避免跨供应商同名 ID 冲突导致路由被静默覆盖; 不满意可直接改.
              const prefixed = this.transitId(prov.name, id);
              prov.models.push({ model_id:prefixed, upstream_model:id, reasoning_effort:'', api_format:this.defaultApiFormat(id), origin:'fetched', _isNew:true, _removed:false, free:this.autoFree(id,id), _freeTouched:false, _fmtTouched:false });
              existingUpstream.add(id);
              added++;
              addedItems.push({ model_id:prefixed, upstream_model:id });
            }
          }
          // 标记已下架: 仅按【上游模型ID】(upstream_model, 缺省回落中转ID) 比对,
          // 中转ID 是用户可改的别名, 不能作为与上游清单比对的依据.
          const removedSet = new Set(d.removed||[]);
          let removedMarked=0;
          for(const m of prov.models){
            const effectiveUpstream = m.upstream_model || m.model_id;
            const wasRemoved = removedSet.has(effectiveUpstream);
            if(wasRemoved && !m._removed) removedMarked++;
            m._removed = wasRemoved;
            if(wasRemoved) m._isNew=false;
          }
          if(added>0 || removedMarked>0) this.markDirty();
          // 8 秒后自动清除 NEW 高亮
          if(added>0){
            prov._newModels = (prov._newModels||0) + added;
            setTimeout(()=>{ for(const m of prov.models) m._isNew=false; }, 8000);
          }
          this.providersFormOk = true;
          this.providersFormMsg = d.message + (removedSet.size ? t('removed_models_suffix',removedSet.size) : '');
          // 打开差异结果弹窗 (三 Tab): 上游有清单可对照才开.
          if((d.models||[]).length || (d.removed||[]).length){
            const newUpstream = new Set(addedItems.map(x=>x.upstream_model));
            const existingList = (d.models||[]).filter(id=>!newUpstream.has(id));
            const removedList = Array.from(removedSet);
            const sel={}; addedItems.forEach(it=>{ sel[it.model_id]=true; });
            this.fetchDiff = { pi:pi, added:addedItems, existing:existingList, removed:removedList, message:this.providersFormMsg };
            this.fetchDiffSel = sel;
            this.fetchDiffTab = addedItems.length ? 'new' : (removedList.length ? 'removed' : 'existing');
            this.showFetchDiff = true;
          }
        } else {
          this.providersFormOk = false;
          this.providersFormMsg = d.error || t('fetch_failed');
        }
      } catch(e) { this.providersFormMsg = t('network_error'); }
      prov._fetching = false;
      if (this.providersFormMsg) setTimeout(() => { this.providersFormMsg = ''; }, 4000);
    },
    async saveProvidersForm() {
      this.providersFormMsg='';
      this._saving=true;
      try {
      // 前端预校验: name 非空且唯一 (后端也会校验拦截).
      const names = this.providersFormData.map(p=>(p.name||'').trim());
      const seen = new Set();
      for (const n of names) {
        if (!n) { this.providersFormMsg=t('provider_name_required'); this.providersFormOk=false; return; }
        if (seen.has(n)) { this.providersFormMsg=t('provider_name_duplicate',n); this.providersFormOk=false; return; }
        seen.add(n);
      }
      try {
         const providers = this.providersFormData.map(p => {
           const models = {};
           for (const m of p.models) {
             if (!m.model_id) continue;
             const cfg = {};
             if (m.upstream_model) cfg.upstream_model = m.upstream_model;
             if (m.reasoning_effort) cfg.reasoning_effort = m.reasoning_effort;
             // 协议: 仅当显式选 anthropic 才写字段, 选 openai(空)则省略回落默认.
             if (m.api_format) cfg.api_format = m.api_format;
             // 免费: 仅在与自动判定结果不同时才写显式字段, 保持配置干净;
             // 取消自动判定的免费需用户手动勾掉开关 (_freeTouched) 才写 free: false
              const autoFree = this.autoFree(m.model_id, m.upstream_model);
              if (m.free && !autoFree) cfg.free = true;
              else if (!m.free && autoFree && m._freeTouched) cfg.free = false;
              if (m.origin) cfg.origin = m.origin;
              // 剥离带工具调用的推理链: 逐模型设置 (中转供应商下各模型协议不同).
              if (m.strip_toolcall_reasoning) cfg.strip_toolcall_reasoning = true;
              // 单价 (元/百万 tokens): 仅写入非空字段, 避免污染配置.
              if (m.price) {
                const pr = { input_per_m: m.price.input_per_m||0, output_per_m: m.price.output_per_m||0 };
                if (m.price.cache_read_per_m != null) pr.cache_read_per_m = m.price.cache_read_per_m;
                if (m.price.cache_creation_per_m != null) pr.cache_creation_per_m = m.price.cache_creation_per_m;
                if (m.price.input_per_m_offpeak) pr.input_per_m_offpeak = m.price.input_per_m_offpeak;
                if (m.price.output_per_m_offpeak) pr.output_per_m_offpeak = m.price.output_per_m_offpeak;
                if (m.price.cache_read_per_m_offpeak) pr.cache_read_per_m_offpeak = m.price.cache_read_per_m_offpeak;
                cfg.price = pr;
              }
              models[m.model_id] = Object.keys(cfg).length ? cfg : {};
           }
            return {
              name: p.name,
              endpoint: p.endpoint,
              endpoint_anthropic: p.endpoint_anthropic||undefined,
              endpoint_responses: p.endpoint_responses||undefined,
              api_key_env: p.api_key_env,
              api_key_default: p.api_key_default||undefined,
              balance_endpoint: p.balance_endpoint||undefined,
              api_format: p.api_format||undefined,
              prompt_cache: p.prompt_cache,
              openai_cache_control: p.openai_cache_control,
              max_request_body_bytes: p.max_request_body_bytes,
              headers: p.headers||undefined,
              models: models,
            };
         });
         const json = JSON.stringify({providers:providers}, null, 2);
         const oldNames = this.providersFormData.map(p => p._origName || p.name);
         const r=await fetch('/admin/api/providers/save',{method:'POST',headers:authHeaders(),body:JSON.stringify({json:json,oldNames:oldNames})});
         const d=await r.json();
         this.providersFormOk = !!d.message;
         this.providersFormMsg=d.message||d.error||t('save_failed');
         // 保存成功但存在重复中转 ID: 追加告警. 这些条目在面板上可见可改却永不生效
         // (路由表一个 ID 只留一条), 原先完全无提示, 用户会误以为"改了没反应"是 bug.
         if(d.message && (d.duplicate_ids||[]).length) {
           this.providersFormMsg += ' ' + t('dup_model_id_saved', d.duplicate_ids.length);
         }
          if(d.message) {
            // 配置保存成功后, 同步保存所有有草稿的 key
            await this.saveAllProviderKeys();
            this.isDirty=false;
            // 保存即持久化, 清除各供应商"新模型"未保存提示; 同步 _origName
            this.providersFormData.forEach(p=>{ p._newModels=0; p._origName=p.name; });
            setTimeout(()=>this.providersFormMsg='',3000);
          }
       } catch(e){ this.providersFormMsg=t('network_error'); }
       } finally { this._saving=false; }
       },
       async saveAllProviderKeys() {
       // 遍历所有供应商, 保存有草稿的 key
       const keysToSave = this.providersFormData
         .filter(p => (p._keyDraft||'').trim() !== '')
         .map(p => ({ provider: p.name, value: p._keyDraft.trim() }));
       if (keysToSave.length === 0) return;
       let saved = 0, failed = 0;
       for (const { provider, value } of keysToSave) {
         try {
           const r = await fetch('/admin/api/keys', {
             method: 'PUT',
             headers: authHeaders(),
             body: JSON.stringify({ provider, value })
           });
           const d = await r.json();
           if (r.ok) {
             // 找到对应供应商并清除草稿
             const prov = this.providersFormData.find(p => p.name === provider);
             if (prov) prov._keyDraft = '';
             saved++;
           } else {
             console.error(`保存 ${provider} 的 key 失败:`, d.error);
             failed++;
           }
         } catch (e) {
           console.error(`保存 ${provider} 的 key 网络错误:`, e);
           failed++;
         }
       }
       if (saved > 0) {
         this.keyMsg = t('provider_keys_saved',saved);
         this.fetchKeys();
         setTimeout(() => this.keyMsg = '', 3000);
       }
       if (failed > 0) {
         this.keyMsg = t('provider_keys_save_failed',failed);
         setTimeout(() => this.keyMsg = '', 3000);
       }
       },
        confirmAddProvider() {
        if (!this.addProvName || !this.addProvEndpoint) return;
        this.providersFormData.push({
          name: this.addProvName,
          _origName: this.addProvName,
          endpoint: this.addProvEndpoint,
          endpoint_anthropic: this.addProvAnthropicEndpoint||null,
          endpoint_responses: this.addProvResponsesEndpoint||null,
          api_key_env: this.addProvName.toUpperCase().replace(/-/g,'_')+'_KEY',
          api_key_default: null,
          balance_endpoint: this.addProvBalanceEndpoint || null,
          headers: null,
          _expanded: true,
          _testing: false,
          _testResult: null,
          _filter: '',
          _selected: {},
          models: [],
        });
        this.showAddProvider = false;
        this.addProvName = ''; this.addProvEndpoint = ''; this.addProvBalanceEndpoint = ''; this.addProvAnthropicEndpoint=''; this.addProvResponsesEndpoint='';
       this.providersFormOk = true;
       this.providersFormMsg = t('add_provider_hint');
       this.markDirty();
       setTimeout(()=>this.providersFormMsg='',3000);
     },