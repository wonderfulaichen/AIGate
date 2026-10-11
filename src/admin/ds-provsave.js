// ── 保存与拉取 ──
// providers.json 落盘 + 上游模型拉取 + 新增供应商
// 主要成员: saveProvidersForm / fetchProviderModels / confirmAddProvider
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

     // 拉取模型: 后端仅写入内存注册表 (即时生效) 并返回模型列表; 这里合并进表单,
    // 待用户点"保存配置"才持久化 —— 避免直接写盘冲掉表单里其它未保存改动.
    //
    // ⚠️ 拉取前确认: 刷新/拉取都会用文件覆盖当前表单, 若用户有未保存草稿
    // (改名/改端点/改档位) 会被**静默丢弃**。故未保存时先二次确认。
    async fetchProviderModels(pi) {
      if (this.isDirty) {
        if (!(await confirmAsk(t('confirm_discard_draft'), { danger: true }))) return;
      }
      const prov = this.providersFormData[pi];
      prov._fetching = true;
      try {
        const r = await fetch('/admin/api/providers/'+encodeURIComponent(prov.name)+'/fetch-models', {method:'POST', headers:authHeaders()});
        const d = await r.json();
        if (r.ok && d.success) {
          // 去重: 基于 upstream_model (与后端 add_models 一致), 而非 model_id.
          const existingUpstream = new Set(prov.models.map(m=>m.upstream_model||m.model_id));
          // 后端顺带探测到的"该上游实测支持哪些协议" (拉取模型时并行探测, 零额外操作).
          // 用途: 作为新增模型的 api_formats 建议值 —— 协议既不在模型元信息里
          // (models.dev 模型级无协议字段), 也无法从官方元数据推断出"还支持哪些"
          // (实测 DeepSeek 的 /v1/responses 同样可用, 但元数据只标主协议),
          // 故只能问上游本身。探测为空时不建议 (不猜)。
          const probed = Array.isArray(d.probed_protocols) ? d.probed_protocols : [];
          // 探测结果记到**供应商级**: 探测探的是端点路径 (`/chat/completions` 等),
          // 故是服务方级能力, 同一供应商下所有模型共享 —— 这也是界面「实测支持」徽章的来源。
          // 存到 prov 上后, 保存配置时随表单一起落盘, 之后每次打开面板都能看到。
          // 探测为空 (WAF/不可判别) 时**清空**该字段而非保留旧值: 免得展示过期结论。
          prov.probed_protocols = probed.length ? probed.slice() : null;
          // 这里**不再**给新模型写 api_formats 建议值, 也不再写推断出的 api_format.
          //
          // 理由: 后端 pick_api_format / available_api_formats 已把这两个来源当成**自动能力**
          // 参与路由 —— 客户端打 /v1/messages 进来就在可用集里挑 anthropic 走原生直通,
          // 无需任何手工声明。而 api_formats 的语义是"显式即约束"(优先于探测), 一旦写入就把
          // 该模型钉死, 反而关掉了自动匹配; api_format 同理会把"推断"变成"显式",
          // 既让面板误显示为已标注, 也遮蔽了探测到的其他协议 —— 与"协议由编程工具决定"相悖。
          let added=0;
          const addedItems=[];
          for (const id of (d.models||[])) {
            if (!existingUpstream.has(id)) {
              // 中转 ID 一律生成为「供应商/上游模型ID」(上游自带斜杠也照加, 见 transitId),
              // 避免跨供应商同名 ID 冲突导致路由被静默覆盖; 不满意可直接改.
              const prefixed = this.transitId(prov.name, id);
              prov.models.push({ model_id:prefixed, upstream_model:id, reasoning_effort:'', api_format:'',
                // 两个协议字段都留空 = 不限制: 实际协议由「探测能力 ∪ 推断」自动决定 (见上方说明).
                // 面板协议列会以虚线 + 徽章如实展示"自动判定"与"可用协议集".
                api_formats: null,
                origin:'fetched', _isNew:true, _removed:false, free:this.autoFree(id,id), _freeTouched:false, _fmtTouched:false });
              existingUpstream.add(id);
              added++;
              addedItems.push({ model_id:prefixed, upstream_model:id });
            }
          }
          // 探测到多协议时告知用户: 该上游同时支持这几种协议, 且已**自动生效**
          // (客户端打哪个入口就用哪个) —— 用户无需再做任何配置。
          if (probed.length > 1) {
            toast(t('probed_protocols', probed.map(f=>t('fmt.'+f)).join(' / ')), 'info', 'probe_ok');
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
          // 失败必须置 Ok=false 并 toast: 否则点了"获取"毫无反馈 (用户实测报的正是这个现象)。
          this.providersFormOk = false;
          this.providersFormMsg = d.error || t('fetch_failed');
          toast(this.providersFormMsg, 'error', 'fetch_fail');
        }
      } catch(e) {
        this.providersFormOk = false;
        this.providersFormMsg = t('network_error');
        toast(this.providersFormMsg, 'error', 'net_err');
      }
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
             // 多协议候选集: 仅当勾选 ≥2 个才写 (勾 1 个等价单协议, 由上面的 api_format 表达,
             // 免得同一件事两处字段并存而含义重叠). 顺序保留勾选先后 = 优先级.
             if (Array.isArray(m.api_formats) && m.api_formats.length > 1) {
               cfg.api_formats = m.api_formats.slice();
             }
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
              // 账号形式凭据 (auth.mode=account): 透传保存. 不带 auth 的供应商本字段为
              // undefined → JSON.stringify 自动省略, 不会给存量配置塞空字段.
              auth: p.auth||undefined,
              // 内置预设标记与停用态: 同样只在有值时落盘 (preset 受保护, 见 api_providers_save).
              preset: p.preset||undefined,
              disabled: p.disabled||undefined,
              balance_endpoint: p.balance_endpoint||undefined,
              api_format: p.api_format||undefined,
              prompt_cache: p.prompt_cache,
              openai_cache_control: p.openai_cache_control,
              max_request_body_bytes: p.max_request_body_bytes,
              headers: p.headers||undefined,
              // 按模型族覆盖 UA 的规则 (WorkBuddy 国际版按模型产品线分档; 见后端同名字段).
              // ⚠️ 必须在这里透传 —— 本函数是保存白名单, 不入白名单的字段会在保存时**被丢掉**,
              // 表现是"预设加进来能登录、一保存就 400"(UA 没了)。
              ua_by_model_prefix: (Array.isArray(p.ua_by_model_prefix) && p.ua_by_model_prefix.length) ? p.ua_by_model_prefix : undefined,
              // 上游要求首条为 system (WorkBuddy 国际版实测 400/11128) → 让网关补.
              require_system_first: p.require_system_first||undefined,
              // 实测支持的协议 (供应商级, 由「拉取模型」时的探测写入). 仅在有值时落盘,
              // 免得给未探测过的供应商塞空字段. 用途: 面板展示「实测支持」+ 供用户判断.
              probed_protocols: (Array.isArray(p.probed_protocols) && p.probed_protocols.length) ? p.probed_protocols.slice() : undefined,
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

    // ── 内置预设 (2026-10-01): 添加 / 受保护语义 / 停用 ──
    // 预设只是模板: 添加 = 组装条目进表单 (带 preset 标记), 走既有保存链落盘;
    // 之后由 presetDirty/presetReset 维护「已修改 / 恢复默认」, 删除被后端兜底拒绝.
    presetOf(p){ return (this.presets||[]).find(x=>x.id===p.preset) || null; },
    presetDirty(p){
      const t=this.presetOf(p); if(!t) return false;
      if((p.endpoint||'')!==t.endpoint) return true;
      if((p.api_key_env||'')!==t.api_key_env) return true;
      if(JSON.stringify(p.auth||null)!==JSON.stringify(t.auth||null)) return true;
      return false;
    },
    presetReset(p){
      const t=this.presetOf(p); if(!t) return;
      // name 不回改 (账号按 name 索引, 改名会错位); models 保留 (拉取结果非手改).
      p.endpoint=t.endpoint; p.api_key_env=t.api_key_env; p.auth=t.auth||null;
      this.markDirty();
      this.providersFormOk=true; this.providersFormMsg=t('preset_reset_done');
      setTimeout(()=>this.providersFormMsg='',3000);
    },
    presetAdded(t){ return (this.providersFormData||[]).some(p=>p.preset===t.id); },
    addFromPreset(t){
      if (this.presetAdded(t)) return;
      // 身份头与 UA: 优先用预设自带的 (账号型预设各产品不同 —— 国内版
      // X-IDE-Name=CodeBuddy, 国际版 WorkBuddy; 漏掉这些头会让上游把用量归因到
      // 错误的产品, 甚至拒绝请求)。非账号型预设 (deepseek) 没有这些字段 → null,
      // 与手工添加的行为一致。
      const lg = t.login || null;
      this.providersFormData.push({
        name: t.name, _origName: t.name,
        endpoint: t.endpoint,
        api_key_env: t.api_key_env,
        api_key_default: null,
        auth: t.auth || null,
        preset: t.id,
        balance_endpoint: null,
        headers: lg ? {
          'X-Domain': lg.api_domain,
          'X-Product-Code': lg.product_code,
          'X-Product': lg.attribution_name,
          'X-IDE-Name': lg.attribution_name,
          'X-IDE-Type': lg.attribution_name,
          'X-IDE-Version': lg.client_version,
          'X-Agent-Purpose': 'conversation',
          'user-agent': lg.user_agent,
        } : null,
        // 按模型族覆盖 UA (WorkBuddy 国际版: gpt/gemini/claude 走国际形态,
        // glm/hy/kimi/minimax 走国内形态)。空数组 → null, 保持旧行为。
        ua_by_model_prefix: (t.ua_by_model_prefix && t.ua_by_model_prefix.length) ? t.ua_by_model_prefix : null,
        // 上游硬性要求首条是 system 时, 由网关补一条 (WorkBuddy 国际版实测 400/11128)。
        // 漏了这个字段 = 预设能登录但一发请求就 400。
        require_system_first: t.require_system_first ? true : null,
        _expanded: true, _testing: false, _testResult: null,
        _filter: '', _selected: {},
        models: [],
      });
      this.showAddProvider = false;
      this.markDirty();
      this.providersFormOk = true;
      this.providersFormMsg = t('preset_added_hint');
      setTimeout(()=>this.providersFormMsg='',3000);
    },
    toggleProvDisabled(p){
      p.disabled = !p.disabled;
      this.markDirty();
      this.providersFormOk = true;
      this.providersFormMsg = p.disabled ? t('prov_disabled_badge') : t('prov_enable');
      setTimeout(()=>this.providersFormMsg='',3000);
    },

    // ── 账号登录 (账号型供应商: 粘贴凭据 → accounts.json) ──
    // ── 选号策略 (order / pace) ──
    async fetchAcctPicking(){
      try{
        const r=await fetch('/admin/api/accounts/picking',{headers:authHeaders()});
        if(r.ok){ const d=await r.json(); this.acctPicking=d.picking||'order'; }
      }catch(e){}
    },
    async setAcctPicking(m){
      try{
        const r=await fetch('/admin/api/accounts/picking',{method:'POST',headers:authHeaders(),
          body:JSON.stringify({picking:m})});
        const d=await r.json();
        if(d.error){ this.loginMsg=d.error; return; }
        this.acctPicking=m;
        if(d.accounts) this.accountsStatus=d.accounts;
        // 策略变了当前账号可能变 → 余额行同步刷新.
        try { await this.fetchBalance(); } catch(e) {}
        this.loginMsg=d.message||''; setTimeout(()=>{ this.loginMsg=''; },3000);
      }catch(e){ this.loginMsg=String(e); }
    },
    openLogin(name){
      this.stopLoginPoll();
      this.loginProv=name; this.loginTok=''; this.loginRt=''; this.loginExp='';
      this.loginMsg=''; this.loginBrowserPending=false;
    },
    stopLoginPoll(){
      if(this.loginPollTimer){ clearInterval(this.loginPollTimer); this.loginPollTimer=null; }
      this.loginBrowserPending=false;
    },
    // 浏览器一键登录 (推荐): 开官方登录页 → 后台轮询会话 → 成功自动写凭据并刷新状态.
    // 官方流是轮询式的 (auth/state → 浏览器登录 → auth/token), 无本地回调, 见 accounts.rs.
    async browserLogin(){
      if(!this.loginProv || this.loginBrowserPending) return;
      this.loginMsg=''; this.loginBusy=true;
      // 记录登录前的账号指纹集合: 登录成功后据此判断"是新增了账号"还是"又是同一个账号"
      // (在浏览器里没退出当前账号时, 登录页会直接把同一个号的凭据再发回来 ——
      //  2026-10-07 用户报障"再登录一个还是 1 个"就是这个原因, 必须给出可操作提示).
      const before=(this.accountsStatus&&this.accountsStatus[this.loginProv]||[]).map(a=>a.id).join(',');
      // ⚠️ **必须先开窗口再 await**（2026-10-12 修用户报障「点了闪一下、不跳转浏览器」）：
      // 浏览器只在**用户手势的同步调用栈内**允许 window.open；一旦先 await 一个 fetch，
      // 后续 window.open 会被弹窗拦截器**静默丢弃** —— 而且多数浏览器返回的不是 null，
      // 而是一个"已关闭的窗口代理"（truthy），于是旧代码的 `if(!opened)` 分支也不会进，
      // 结果既不跳转、也不提示，就是用户看到的"闪一下、没反应"。
      //
      // 做法: 先用一个占位页**同步**打开窗口占住"用户手势"名额，拿到 URL 后再导航过去。
      // 这样被拦截时 `opened` 才可靠为 null，走提示分支；同时后端报错时也能把窗口关掉。
      const win = window.open('about:blank', '_blank');
      try{
        const r=await fetch('/admin/api/accounts/login/start',{method:'POST',headers:authHeaders(),
          body:JSON.stringify({name:this.loginProv})});
        const d=await r.json();
        if(d.error){
          // 后端拒绝（如"不是账号形式"）: 关掉占位窗口，别留一个空白标签页。
          try { if (win) win.close(); } catch(e) {}
          this.loginMsg=d.error; return;
        }
        if(win){
          try { win.location.href = d.auth_url; } catch(e) { win.location = d.auth_url; }
          this.loginMsg='';   // 成功打开, 清掉旧提示
        }else{
          // 真的被拦截（同步 open 也返回 null）→ 明确告知并给出可点链接。
          this.loginMsg=t('login_browser_blocked')+' '+d.auth_url;
        }
        this.loginBrowserPending=true;
        const started=Date.now();
        this.loginPollTimer=setInterval(async ()=>{
          if(Date.now()-started > 330000){ this.stopLoginPoll(); this.loginMsg=t('login_browser_timeout'); return; }
          try{
            const s=await fetch('/admin/api/accounts/login/status',{method:'POST',headers:authHeaders(),
              body:JSON.stringify({name:this.loginProv})});
            const sd=await s.json();
            if(sd.status==='ok'){
              this.stopLoginPoll();
              const prov=this.loginProv;
              await this.fetchAccounts();
              // 新增/更新账号后余额会变 → 概览行同步刷新 (后端已清缓存).
              try { await this.fetchBalance(); } catch(e) {}
              const after=(this.accountsStatus&&this.accountsStatus[prov]||[]).map(a=>a.id).join(',');
              this.loginProv=''; this.loginTok=''; this.loginRt=''; this.loginExp='';
              if(after===before){
                // 账号没变 → 同一个号重登: 不是"加上了", 得说清楚 + 给出怎么做
                this.loginMsg=t('acc_same_updated');
              }else{
                this.loginMsg=t('login_success');
                setTimeout(()=>{ this.loginMsg=''; },3000);
              }
            }else if(sd.status==='failed'){
              this.stopLoginPoll();
              this.loginMsg=sd.error||t('login_browser_failed');
            }
          }catch(e){}
        }, 2000);
      }catch(e){
        try { if (win) win.close(); } catch(err) {}
        this.loginMsg=t('network_error');
      }
      finally{ this.loginBusy=false; }
    },
    async submitLogin(){
      if(!this.loginProv || !this.loginTok.trim()) return;
      this.loginBusy=true; this.loginMsg='';
      try{
        const body={ name:this.loginProv, access_token:this.loginTok.trim() };
        if(this.loginRt.trim()) body.refresh_token=this.loginRt.trim();
        const days=Number(this.loginExp);
        if(days>0) body.expires_in_secs=Math.floor(days*86400);
        const r=await fetch('/admin/api/accounts/login',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
        const d=await r.json();
        if(d.error){ this.loginMsg=d.error; return; }
        this.accountsStatus=d.accounts||this.accountsStatus;
        this.loginProv=''; this.loginTok=''; this.loginRt=''; this.loginExp='';
        this.loginMsg=t('login_success');
        setTimeout(()=>{ this.loginMsg=''; },3000);
      }catch(e){ this.loginMsg=t('network_error'); }
      finally{ this.loginBusy=false; }
    },
    // ── 切换当前使用的账号 / 调整优先级 (数组顺序即优先级) ──
    // 「设为当前账号」与拖拽排序都走同一个后端原语 (ids 即新顺序), 不做两套逻辑.
    async acctReorder(name, ids, promotedId){
      try{
        const r=await fetch('/admin/api/accounts/reorder',{method:'POST',headers:authHeaders(),
          body:JSON.stringify({name, ids})});
        const d=await r.json();
        if(d.error){ this.loginMsg=d.error; return; }
        this.accountsStatus=d.accounts||{};
        // 切换/增删账号后概览的余额行必须跟着更新 —— 后端已清掉该供应商的余额缓存,
        // 这里主动拉一次; 否则用户看到的还是切换前的数字 (会以为切换没生效).
        try { await this.fetchBalance(); } catch(e) {}
        // 切换"看起来没生效"的唯一真实情形: 置顶的号当前**不可用** (冷却中/已作废),
        // 选号会自动跳过它、用下一个可用的. 此时必须说清楚, 否则用户以为切换是假的.
        const list=this.accountsStatus[name]||[];
        const target=promotedId && list.find(a=>a.id===promotedId);
        if(target && !target.active){
          this.loginMsg = target.dead ? t('acc_promote_dead')
                        : target.cooling ? t('acc_promote_cooling')
                        : t('acc_promote_skipped');
          return; // 不自动消失: 这是需要用户处理的状态
        }
        this.loginMsg=d.message||'';
        setTimeout(()=>{ this.loginMsg=''; },3000);
      }catch(e){ this.loginMsg=String(e); }
    },
    // 把某个账号放到最前 = 切换当前使用的账号 (第一个可用账号即当前用的).
    acctPromote(name, id){
      const list=(this.accountsStatus&&this.accountsStatus[name]||[]).map(a=>a.id);
      const ids=[id].concat(list.filter(x=>x!==id));
      if(ids.length) this.acctReorder(name, ids, id);
    },
    // 拖拽排序: 把第 from 行落到第 to 行的位置.
    acctDrop(name, to){
      const from=this.acctDragFrom;
      this.acctDragFrom=-1;
      if(from<0 || from===to) return;
      const ids=(this.accountsStatus&&this.accountsStatus[name]||[]).map(a=>a.id);
      if(from>=ids.length || to>=ids.length) return;
      const [moved]=ids.splice(from,1);
      ids.splice(to,0,moved);
      this.acctReorder(name, ids, moved);
    },
    // 登出: 传 id 只登出该账号 (多账号常态); 不传则清空该供应商全部账号.
    async logoutProv(name, id){
      try{
        const body = id ? {name, id} : {name};
        const r=await fetch('/admin/api/accounts/logout',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
        const d=await r.json();
        if(!d.error){
          this.accountsStatus=d.accounts||{};
          // 登出后余额(聚合值/当前账号值)会变 → 主动刷新概览行.
          try { await this.fetchBalance(); } catch(e) {}
          this.keyMsg=d.message||t('logout');
          setTimeout(()=>{ this.keyMsg=''; },3000);
        }
      }catch(e){}
    },
    isAccountProv(p){ return !!(p.auth && p.auth.mode==='account'); },