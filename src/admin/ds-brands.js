
    // 品牌徽章: 从上游模型名/供应商名识别品牌, 返回 {label, color}
    // 品牌徽章: 移植自 new-api MODEL_CATEGORY_RULES
    _brandColorCache: {},
    _MODEL_BRANDS: [
      { name:'Perplexity', kw:['perplexity','sonar-'] },
      { name:'NVIDIA', kw:['nvidia/','nvidia.','nemotron'] },
      { name:'OpenAI', kw:['openai/','openai.','gpt-','chatgpt-','codex-','dall-e-','whisper-','omni-moderation-','text-moderation-','text-embedding-ada-','text-embedding-3-','text-ada-','text-babbage-','text-curie-','davinci-','babbage-','computer-use-preview','sora'], re:/(?:^|[/.:])(?:o(?:1|3|4)(?=$|[-.:])|tts-)/ },
      { name:'Anthropic', kw:['anthropic','claude'] },
      { name:'Gemini', kw:['gemini','gemma','learnlm','imagen','veo','nano-banana','palm-'], re:/(?:^|[/.:])aqa$/ },
      { name:'xAI', kw:['x-ai/','xai/','xai-','grok'] },
      { name:'DeepSeek', kw:['deepseek'] },
      { name:'Qwen', kw:['qwen','qwq-','qvq-','tongyi','gte-'], re:/(?:^|[/.:])(?:text-embedding-v\d+|gui-plus|z-image)(?:$|[-_.:])/ },
      { name:'Moonshot', kw:['moonshot','kimi-'] },
      { name:'MiniMax', kw:['minimax','abab','hailuo'], re:/^(?:t2v|i2v|s2v)-01(?:-|$)/ },
      { name:'Doubao', kw:['doubao','volcengine','seedance','seedream','seed-1-'] },
      { name:'Zhipu', kw:['zhipu','zai-org','thudm','chatglm','cogview','cogvideo'], re:/(?:^|[/._-])glm(?=$|[-._])/ },
      { name:'Baidu', kw:['baidu','wenxin','ernie'] },
      { name:'Yi', kw:['01-ai/'], re:/(?:^|[/.:])yi(?=$|[-_])/ },
      { name:'iFlytek', kw:['iflytek','sparkdesk'] },
      { name:'Tencent', kw:['tencent','hunyuan'], re:/(?:^|[/.:])hy\d*(?=$|[-_.:])/ },
      { name:'Baichuan', kw:['baichuan'] },
      { name:'InternLM', kw:['internlm'] },
      { name:'StepFun', kw:['stepfun','step-'] },
      { name:'MiMo', kw:['xiaomi','mimo-'] },
      { name:'Mistral', kw:['mistral','mixtral','codestral','ministral','pixtral','magistral'] },
      { name:'Meta', kw:['meta-llama','llama-','llama2','llama3'] },
      { name:'Cohere', kw:['cohere','command-','c4ai-aya','aya-'], re:/(?:^|[/.:])command$/ },
      { name:'Jina', kw:['jinaai','jina-'] },
      { name:'BAAI', kw:['baai/','bge-'] },
      { name:'Black Forest Labs', kw:['black-forest-labs','flux.'] },
      { name:'Microsoft', kw:['microsoft/'], re:/(?:^|[/.:])phi(?=$|[-._])/ },
      { name:'Amazon', kw:['amazon/','amazon.','nova-','titan-'] },
      { name:'AI21 Labs', kw:['ai21','jamba'] },
      { name:'Stability AI', kw:['stabilityai','stable-diffusion','stable-image','sdxl-'] },
      { name:'Nous Research', kw:['nousresearch','hermes-'] },
      { name:'360 AI', kw:['360gpt','360zhinao'] },
      { name:'Midjourney', kw:['midjourney','mj_','mj-','swap_face'] },
      { name:'Kling', kw:['kling'] },
      { name:'Vidu', kw:['vidu'] },
      { name:'Suno', kw:['suno'] },
      { name:'Jimeng', kw:['jimeng'] },
    ],
    brandFor(upstream, providerName) {
      const s = (upstream || providerName || '').toLowerCase();
      for (const rule of this._MODEL_BRANDS) {
        if (rule.kw && rule.kw.some(k => s.includes(k))) return this._brandEntry(rule.name);
        if (rule.re && rule.re.test(s)) return this._brandEntry(rule.name);
      }
      return { label: '?', color: '#9ca3af' };
    },
    _brandEntry(label) {
      if (this._brandColorCache[label]) return this._brandColorCache[label];
      let h = 0; for (let i = 0; i < label.length; i++) h = label.charCodeAt(i) + ((h << 5) - h);
      const hue = Math.abs(h) % 360;
      const color = `hsl(${hue},65%,55%)`;
      const entry = { label, color };
      this._brandColorCache[label] = entry;
      return entry;
    },
    // 免费模型自动判定: 模型名/上游模型名含 free 或 免费 (与后端 is_free 回退逻辑一致)
    autoFree(modelId, upstream) {
      const s = (upstream || modelId || '').toLowerCase();
      return s.includes('free') || s.includes('免费');
    },
    // 中转 ID = 「供应商名/上游模型ID」. 上游 ID 自带斜杠时 (OpenRouter/commandcodeAI
    // 等用「作者/模型」命名, 如 deepseek/deepseek-v4-flash) **同样加前缀**, 得到
    // commandcodeAI/deepseek/deepseek-v4-flash —— 否则同名 ID 会在多家供应商间冲突,
    // 而路由表 (HashMap) 只保留最后一个, 其余条目静默失效 (改配置无效果且无提示).
    // 已带本供应商前缀时原样返回, 保证幂等 (重复拉取/导入不会叠加前缀).
    transitId(provName, upstreamId) {
      const p = (provName || '').trim();
      const u = (upstreamId || '').trim();
      if (!p) return u;
      if (u === p || u.startsWith(p + '/')) return u;
      return p + '/' + u;
    },
    // 该中转 ID 是否同时存在于**其他**供应商下. 路由表是 HashMap(一个 ID 一条路由),
    // 重复时只有最后加载的那家生效, 另一家改了协议/思考档位/价格都不会有任何效果 ——
    // 原先完全无提示, 用户会以为改了没生效是 bug. 返回告警文案 (无冲突返回空串).
    modelIdDupHint(modelId, selfProvName) {
      const id = (modelId || '').trim();
      if (!id) return '';
      const others = [];
      (this.providersFormData || []).forEach(p => {
        if (!p || p.name === selfProvName) return;
        if ((p.models || []).some(m => (m.model_id || '').trim() === id)) others.push(p.name);
      });
      if (!others.length) return '';
      return t('dup_model_id_hint').replace('{0}', others.join('、'));
    },
    // 与后端 default_api_format 规则保持一致: claude→anthropic, grok/gpt-5*/muse-spark→responses
    defaultApiFormat(modelId) {
      const s = (modelId || '').toLowerCase();
      if (s.includes('claude')) return 'anthropic';
      if (s.includes('grok')) return 'responses';
      if (s.startsWith('gpt-5') || s.startsWith('muse-spark')) return 'responses';
      return '';
    },
    async resetCircuit(provider) {
      try {
        const r=await fetch('/admin/api/circuit/reset',{method:'POST',headers:authHeaders(),body:JSON.stringify({provider})});
        if(r.ok){ await this.fetchHealth(); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },