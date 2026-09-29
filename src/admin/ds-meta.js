// ── 模型元信息 ──
// models.dev 上下文与视觉标签 + 参考价查询
// 主要成员: fetchModelMeta / metaFor / refCostFor
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    // ── 模型元信息 (models.dev): 点击展开 chips 面板 (上下文/输出/视觉/推理/工具) ──
    async fetchModelMeta() {
      this.modelMetaLoading = true;
      try {
        const names=new Set();
        // 注意: providersFormData 就是供应商数组本身 (非 {providers:[]}),
        // 旧版误写 .providers 导致表单模型名从未被收集, 仅统计接口的模型有元数据.
        (this.providersFormData||[]).forEach(p=>(p.models||[]).forEach(m=>{
          const u=(m.upstream_model||'').trim(); if(u) names.add(u);
          const id=(m.model_id||'').trim(); if(id) names.add(id);
        }));
        // 概览页统计的模型名带 "provider/" 中转前缀, 剥离后一并查询
        [...((this.stats?.top_models)||[]), ...((this.stats?.per_model)||[])].forEach(m=>{
          const raw=(m.model||'').trim(); if(!raw) return;
          names.add(raw.includes('/') ? raw.split('/').pop() : raw);
        });
        if(!names.size) { this.modelMetaLoading = false; return; }
        // 参考价查询对: 只查「本供应商 + 该模型」这一组合 (按 provider 严格匹配,
        // 避免取到同名模型在别家的价 —— 实测同名模型有几十家收录且报价差异极大).
        const costQueries=[];
        (this.providersFormData||[]).forEach(p=>{
          const pn=(p.name||'').trim(); if(!pn) return;
          (p.models||[]).forEach(m=>{
            const u=(m.upstream_model||'').trim();
            if(u) costQueries.push({provider:pn, name:u});
          });
        });
        // 签名去重: 轮询/重复进入不重发 (服务端有 24h 缓存, 也省一次往返)
        const sig=[...names].sort().join(',')+'|'+costQueries.map(q=>q.provider+'/'+q.name).sort().join(',');
        if(sig===this._metaSig && Object.keys(this.modelMeta).length) { this.modelMetaLoading = false; return; }
        this._metaSig=sig;
        const r=await fetch('/admin/api/model-meta',{method:'POST',headers:authHeaders(),body:JSON.stringify({names:[...names], costQueries})});
        if(r.ok){
          const d=await r.json();
          this.modelMeta=d.meta||{};
          this.modelCosts=d.costs||{};
        }
      } catch(e){}
      this.modelMetaLoading = false;
    },
    // 该模型的参考价 (USD/1M): 仅当 models.dev 里**同名供应商**收录了该模型才返回.
    // 不做跨供应商回退 —— 别家的价当参考只会误导.
    refCostFor(m) {
      const p=((this.drawerProv&&this.drawerProv.name)||'').trim();
      const u=(m&&m.upstream_model||'').trim();
      if(!p||!u) return null;
      const byProv=this.modelCosts[p];
      return (byProv&&byProv[u])||null;
    },
    metaFor(m) {
      const u=(m.upstream_model||'').trim();
      return this.modelMeta[u] ?? this.modelMeta[(m.model_id||'').trim()] ?? null;
    },
    fmtTok(n) { if(!n) return '—'; if(n>=1e6) return (Math.round(n/1e5)/10)+'M'; if(n>=1000) return Math.round(n/1000)+'K'; return String(n); },
    // 模型元信息 chips 面板内容 (数据源 models.dev, 仅数字/布尔/i18n 文案, x-html 安全)
    // 迷你趋势线。
    // ⚠️ SVG **呈现属性** (stroke=/stop-color=) 不解析 CSS 变量, 所以外观不能靠属性传色 ——
    // 改造前调用点因此写死了旧品牌色 (紫), 换主色后这条线不会跟着变。
    // 现改为**走 CSS 类**: 元素只挂 class, 取色交给 CSS 的 --spk (默认 --accent),
    // 于是它随主题与品牌色自动更新。若调用方要指定色, 传第 2 参, 会以内联 --spk 覆盖。
    sparklineSvg(values,color) {
      const vals=(values||[]).map(Number).filter(v=>Number.isFinite(v));
      if(vals.length<2) return '';
      const w=160,h=28,p=2,mx=Math.max(...vals),mn=Math.min(...vals),span=Math.max(mx-mn,1);
      // 迷你图用**直折线**: 本图 viewBox 仅 160x28 且以 preserveAspectRatio="none"
      // 非等比放大(横向约 5.6 倍), 平滑曲线的控制点算在这个坐标系里会被横向夸张成波浪;
      // 点密 + 画布矮, 平滑在这里只带来"糊"。曲线留给主趋势图。
      const pts=vals.map((v,i)=>`${p+(w-2*p)*i/(vals.length-1)},${h-p-(v-mn)/span*(h-2*p)}`);
      const d='M'+pts.join(' L');
      const area=`${p},${h} ${pts.join(' ')} ${w-p},${h}`;
      const gid='spk'+Math.abs((String(color||'').length)+vals.length);
      const style=color?` style="--spk:${color}"`:'';
      return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"${style}><defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="spk-stop" stop-opacity=".24"/><stop offset="1" class="spk-stop" stop-opacity="0"/></linearGradient></defs><polygon points="${area}" class="spk-fill" fill="url(#${gid})"/><polyline points="${pts.join(' ')}" class="spk-line"/></svg>`;
    },
    metaChipsHtml(m) {
      const meta=this.metaFor(m);
      if(!meta) return '';
      // 注: 色值直接拼进 style 字符串(非 CSS 声明块), 且会经品牌色渠道使用, 故保留真实色值。
      const chip=(l,v,c)=>'<span class="meta-chip" style="background:'+c+'1a;color:'+c+';border:1px solid '+c+'44"><span style="opacity:.7">'+l+'</span><b class="mono">'+v+'</b></span>';
      const tag=(l,c)=>'<span class="meta-tag" style="background:'+c+'1a;color:'+c+';border:1px solid '+c+'44">'+l+'</span>';
      let h='';
      if(meta.context) h+=chip(t('meta_ctx'), this.fmtTok(meta.context), '#38bdf8');
      if(meta.output) h+=chip(t('meta_out'), this.fmtTok(meta.output), '#818cf8');
      if(meta.vision) h+=tag(t('meta_vision'), '#c084fc');
      if(meta.reasoning) h+=tag(t('meta_reasoning'), '#fbbf24');
      if(meta.tool_call) h+=tag(t('meta_tools'), '#34d399');
      return h;
    },
    modelMetaStatusHtml(m) {
      if(this.metaFor(m)) return this.metaChipsHtml(m);
      if(this.modelMetaLoading) return '<span class="meta-pending">'+t('meta_loading')+'</span>';
      return '<span class="meta-pending">'+t('meta_unavailable')+'</span>';
    },