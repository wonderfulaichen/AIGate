// ── 供应商品牌与模型 ID ──
// 品牌色与 logo 解析 / 中转 ID 生成 / 免费判定 / 熔断重置
// 主要成员: providerLogo / transitId / modelIdDupHint
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

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
    // 协议推断: **必须与后端 providers.rs::default_api_format 逐条一致**。
    //
    // 历史问题: 这里曾是手写的简化版 (只有 claude/grok/gpt-5*/muse-spark 四条, 且不看供应商),
    // 与后端的两套网关规则 (go / zen) 漂移, 后果有二:
    //  ① 建模型时自动写入错误协议 —— 后端认为 openai, 前端却把 `gpt-5-x` 标成 responses
    //     (无视供应商), 在非 go/zen 供应商上直接错;
    //  ② 面板显示与后端实际转发协议不符 (实测 11/650 个模型如此)。
    // 现与后端同规则, 并由单测 (test_fmt_rules.cjs) 逐例比对防再度漂移。
    // providerName 可省略: 省略时只应用与供应商无关的通用规则 (claude / grok)。
    defaultApiFormat(modelId, providerName) {
      const prov = (providerName || '').toLowerCase();
      // 容忍传入中转 ID (形如 `go/minimax-m2.7`): 先剥掉 `供应商/` 前缀再匹配,
      // 否则 startsWith 类规则 (gpt-5* / minimax* / qwen3*) 对带前缀的 ID 永不命中.
      let raw = String(modelId || '');
      if (prov && raw.toLowerCase().startsWith(prov + '/')) raw = raw.slice(prov.length + 1);
      const id = raw.toLowerCase();
      // 通用规则 (任意供应商)
      if (id.includes('claude')) return 'anthropic';
      if (id.includes('grok')) return 'responses';
      // 网关专属规则 — 与后端 providers.rs 的 match provider 分支一一对应
      if (prov === 'go') {
        if (id.startsWith('minimax') || (id.startsWith('qwen3') && (id.includes('-plus') || id.includes('-max')))) return 'anthropic';
        if (id.startsWith('gpt-5') || id.startsWith('muse-spark')) return 'responses';
        return '';
      }
      if (prov === 'zen') {
        if (id.startsWith('qwen3') && (id.includes('-plus') || id.includes('-max'))) return 'anthropic';
        if (id.startsWith('gpt-')) return 'responses';
        return '';
      }
      return '';
    },
    // 面板「协议」列应显示的**实际生效协议** (而非配置值): 配置 → 供应商 → 推断 → openai.
    // 原实现直接绑定配置字段, 未标注的模型一律显示「OpenAI」, 与后端推断不符时界面即在说假话。
    //
    // 推断用**上游真名**而非中转 ID: 中转 ID 形如 `go/minimax-m2.7`, 带前缀后
    // `startsWith('gpt-5')` 之类的规则会失效 (实测 `go/gpt-5.4`.startsWith('gpt-5') === false)。
    // 上游真名缺失时才回退中转 ID (去掉 `供应商/` 前缀, 还原成能被规则匹配的形态)。
    // 这与后端一致: 后端也是先按中转 ID 查、再按 upstream_model 查 (见 resolve_api_format)。
    effectiveApiFormat(m, prov) {
      if (m && m.api_format) return m.api_format;
      const pf = prov && prov.api_format;
      if (pf) return pf;
      const pname = (prov && prov.name) || '';
      const up = (m && m.upstream_model) || '';
      if (up) {
        const byUp = this.defaultApiFormat(up, pname);
        if (byUp) return byUp;
      }
      const mid = (m && m.model_id) || '';
      // 去掉「供应商/」前缀再匹配: 否则 startsWith 类规则对中转 ID 永远不命中.
      const bare = mid.startsWith(pname + '/') ? mid.slice(pname.length + 1) : mid;
      return this.defaultApiFormat(bare, pname) || 'openai';
    },
    // 该模型的协议是否来自推断 (未显式标注) —— 面板据此加弱化标记, 让用户知道"这是自动判断的".
    apiFormatInferred(m, prov) {
      return !(m && m.api_format) && !(prov && prov.api_format);
    },
    // 多协议候选集开关 (勾选即把该协议加入/移出候选集).
    //
    // 规则与后端 providers.rs::pick_api_format 对应:
    //  · 候选集只有 1 项 = 等价于单一协议 (向后兼容), 故允许;
    //  · 候选集为空 → 清掉字段 (回到"未声明", 由 api_format / 推断决定), 而不是存个空数组;
    //  · 顺序 = 优先级: 客户端入口协议不在候选集时用**第一项** —— 保持勾选先后可见.
    toggleModelFormat(m, f) {
      if (!m) return;
      const cur = Array.isArray(m.api_formats) ? m.api_formats.slice() : [];
      const i = cur.indexOf(f);
      if (i >= 0) cur.splice(i, 1);
      else cur.push(f);
      m.api_formats = cur.length ? cur : null;
      m._fmtTouched = true;
      this.markDirty();
    },
    async resetCircuit(provider) {
      try {
        const r=await fetch('/admin/api/circuit/reset',{method:'POST',headers:authHeaders(),body:JSON.stringify({provider})});
        if(r.ok){ await this.fetchHealth(); }
        else { toast(await errToastMsg(r, t('save_failed')), 'error', 'save_fail'); }
      } catch(e){ toast(t('network_error'), 'error', 'net_err'); }
    },