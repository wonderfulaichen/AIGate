
    computeModelTrend() {
      const rows=this.stats?.model_trends||[]; const sums={};
      rows.forEach(r=>{const k=(r.provider||'')+'/'+(r.upstream_model||'');sums[k]=(sums[k]||0)+Number(r.prompt_tokens||0)+Number(r.completion_tokens||0);});
      const allKeys=Object.keys(sums).sort((a,b)=>sums[b]-sums[a]).slice(0,6);
      const keys=allKeys.filter(k=>!this.hiddenModels[k]);
      this.modelTrendData={keys:allKeys,sums,dates:[],series:[],xPct:[],trendByTs:new Map()};
      if(!rows.length||!keys.length){this.modelTrendSvgHtml='';return;}
      const trends=(this.stats.trends||[]).filter(x=>x&&x.ts);
      const dates=[...new Map(trends.map(r=>[r.ts,r.date])).entries()].sort((a,b)=>a[0]-b[0]);
      if(!dates.length){this.modelTrendSvgHtml='';return;}
      // 画布与内边距 (viewBox 坐标); 比例由 CSS 等比缩放维持, 不再用 preserveAspectRatio 拉伸.
      const W=900,H=280,left=52,right=18,top=18,bottom=36,pw=W-left-right,ph=H-top-bottom;
      const palette=CHART_PALETTE;
      const colorOf=k=>palette[allKeys.indexOf(k)%palette.length];
      const map=new Map(rows.map(r=>[r.ts+'/'+r.provider+'/'+r.upstream_model,r]));
      const values=keys.map(k=>dates.map(([ts])=>{const [p,...u]=k.split('/');const r=map.get(ts+'/'+p+'/'+u.join('/'));return r?Number(r.prompt_tokens||0)+Number(r.completion_tokens||0):0;}));
      // 自适应裁剪: 去掉首尾**没有任何数据**的桶, 否则稀疏数据下大半画布是贴着 0 的直线,
      // 真正的信息被挤到一角 (实测 29 天窗口里只有最近 3 天有数据 → 3/4 画布无信息).
      // 两端各多留一个空桶: 让人看得出数据"从哪开始、到哪结束", 而不是被硬切在边缘.
      const hasData=i=>values.some(vals=>Number(vals[i]||0)>0);
      let lo=-1, hi=-1;
      for(let i=0;i<dates.length;i++){ if(hasData(i)){ if(lo<0)lo=i; hi=i; } }
      if(lo>=0){
        lo=Math.max(0,lo-1); hi=Math.min(dates.length-1,hi+1);
        if(hi-lo+1 < dates.length){
          this.modelTrendTrimmed=true;
          dates.splice(hi+1);
          dates.splice(0,lo);
          values.forEach(vals=>{ vals.splice(hi+1); vals.splice(0,lo); });
        }
      }
      const stackMax=Math.max(...dates.map((_,i)=>values.reduce((s,vals)=>s+vals[i],0)),1);
      const max=this.chartMode==='bar'?stackMax:Math.max(...values.flat(),1);
      const n=dates.length;
      const x=i=>left+(n===1?pw/2:pw*i/(n-1));
      const y=v=>top+ph-v/max*ph;
      const smooth=pts=>{if(pts.length<2)return '';let d='M'+pts[0][0]+','+pts[0][1];for(let i=0;i<pts.length-1;i++){const cx=(pts[i][0]+pts[i+1][0])/2;d+=' C'+cx+','+pts[i][1]+' '+cx+','+pts[i+1][1]+' '+pts[i+1][0]+','+pts[i+1][1];}return d;};
      // 暴露给悬停浮层 (按桶取各序列用量, 无需重算).
      this.modelTrendData.dates=dates;
      this.modelTrendData.xPct=dates.map((_,i)=>x(i)/W*100);
      this.modelTrendData.series=keys.map((k,si)=>({key:k,label:k.split('/').slice(1).join('/')||k,color:colorOf(k),values:values[si]}));
      this.modelTrendData.trendByTs=new Map(trends.map(r=>[r.ts,r]));
      // viewBox + width:100% + height:auto: 等比缩放, 圆点与文字不被拉扁.
      let out='<svg viewBox="0 0 '+W+' '+H+'" style="display:block;width:100%;height:auto" class="model-trend-svg" role="img" aria-label="'+this.escapeHtml(t('token_trend_title'))+'">';
      for(let i=0;i<=4;i++){const yy=top+ph*i/4;out+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+yy+'" y2="'+yy+'" stroke="#2a2d37" stroke-width=".7"/><text x="'+(left-8)+'" y="'+(yy+3)+'" text-anchor="end" font-size="9" fill="#6b7280">'+this.escapeHtml(this.fmtT(max*(1-i/4)))+'</text>';}
      if(this.chartMode==='bar'){const bw=Math.min(28,pw/n*.7);const cumul=new Array(n).fill(0);values.forEach((vals,si)=>{const k=keys[si],color=colorOf(k);vals.forEach((v,i)=>{if(!v)return;const y0=y(cumul[i]);cumul[i]+=v;const y1=y(cumul[i]);const barH=y0-y1;out+='<rect x="'+(x(i)-bw/2)+'" y="'+y1+'" width="'+bw+'" height="'+barH+'" fill="'+color+'"/>';});});}else{
        values.forEach((vals,si)=>{const k=keys[si],color=colorOf(k);const pts=vals.map((v,i)=>[x(i),y(v)]);
          const ptsStr=pts.map(p=>p[0]+','+p[1]).join(' ');
          const gid='tg'+si;
          out+='<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+color+'" stop-opacity=".25"/><stop offset="1" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>';
          const areaD='M'+x(0)+','+(top+ph)+' L'+pts.map(p=>p[0]+','+p[1]).join(' L')+' L'+x(n-1)+','+(top+ph)+' Z';
          out+='<path d="'+areaD+'" fill="url(#'+gid+')"/>';
          out+='<polyline points="'+ptsStr+'" fill="none" stroke="'+color+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>';
          vals.forEach((v,i)=>{if(!v)return;out+='<circle cx="'+x(i)+'" cy="'+y(v)+'" r="3.5" fill="'+color+'" stroke="#16161c" stroke-width="1.5"/>';});
        });
      }
      const step=Math.max(1,Math.ceil(n/8));dates.forEach((d,i)=>{if(i%step===0||i===n-1)out+='<text x="'+x(i)+'" y="'+(H-12)+'" text-anchor="middle" font-size="9" fill="#6b7280">'+this.escapeHtml(d[1]||'')+'</text>';});out+='</svg>';
      this.modelTrendSvgHtml=out;
    },
    // ── Token 趋势悬停: 十字线 + 该时段各模型用量浮层 ──
    onTrendHover(ev) {
      const d=this.modelTrendData; if(!d||!d.dates||!d.dates.length) return;
      const el=ev.currentTarget; const r=el.getBoundingClientRect(); if(!r.width) return;
      const W=900, left=52, right=18, pw=W-left-right, n=d.dates.length;
      const vbX=((ev.clientX-r.left)/r.width)*W;
      let i=n===1?0:Math.round((vbX-left)*(n-1)/pw);
      i=Math.max(0,Math.min(n-1,i));
      if(this.trendHover&&this.trendHover.idx===i) return; // 同一桶不重算, 避免每像素触发重排
      const leftPct=d.xPct[i]||0;
      this.trendHover={idx:i,leftPct,align:leftPct<16?'left':leftPct>62?'right':'center'};
    },
    onTrendHoverEnd() { this.trendHover=null; },
    get trendTooltipHtml() {
      const d=this.modelTrendData||{}; const h=this.trendHover;
      if(!h||!d.series||!d.dates||!d.dates.length) return '';
      const i=h.idx, date=(d.dates[i]||[])[1]||'';
      const shown=d.series.map(s=>({label:s.label,color:s.color,v:(s.values||[])[i]||0})).filter(s=>s.v>0).sort((a,b)=>b.v-a.v);
      const total=shown.reduce((s,r)=>s+r.v,0);
      let html='<div class="tt-h">'+this.escapeHtml(date)+' · '+t('trend_total')+' <b>'+this.fmtT(total)+'</b> tok</div>';
      if(!shown.length){html+='<div class="tt-r"><span class="tt-n tm">'+t('trend_no_requests')+'</span></div>';}
      // P3: 序列过多时折叠 —— 取前 15 直接列出, 其余汇总成一行「其他 N 项」(按值降序).
      // 原实现把所有非零序列全列, 模型一多浮层就长出屏幕, 反而读不到关键项.
      const TOP_N = 15;
      const head = shown.slice(0, TOP_N);
      const rest = shown.slice(TOP_N);
      head.forEach(s=>{html+='<div class="tt-r"><i style="background:'+s.color+'"></i><span class="tt-n">'+this.escapeHtml(s.label)+'</span><span class="tt-v">'+this.fmtT(s.v)+'</span></div>';});
      if(rest.length){
        const rv=rest.reduce((a,b)=>a+b.v,0);
        html+='<div class="tt-r tt-rest"><i></i><span class="tt-n tm">'+this.escapeHtml(t('trend_other_n',rest.length))+'</span><span class="tt-v tm">'+this.fmtT(rv)+'</span></div>';
      }
      const tr=d.trendByTs&&d.trendByTs.get?d.trendByTs.get((d.dates[i]||[])[0]):null;
      if(tr){
        const cost=this.stats?.has_price_config?this.fmtMoney(tr.total_cost||0):t('chart_no_price');
        html+='<div class="tt-f">'+t('trend_bucket_metrics',tr.requests||0,tr.errors||0,this.fmtT(tr.total_prompt_tokens||0),this.fmtT(tr.total_completion_tokens||0),this.escapeHtml(cost))+'</div>';
      }
      return html;
    },
    get trendLegendItems() {
      const d=this.modelTrendData||{}; const keys=d.keys||[]; const sums=d.sums||{};
      const palette=CHART_PALETTE;
      const total=Object.keys(sums).reduce((a,k)=>a+(sums[k]||0),0)||1;
      return keys.map((k,i)=>({k,label:k.split('/').slice(1).join('/')||k,color:palette[i%palette.length],tokens:sums[k]||0,pct:(sums[k]||0)/total*100,hidden:!!this.hiddenModels[k]}));
    },
    // 图头合计 (P3): 把"这张图一共多少"写在标题旁, 不必挨个加图例数字.
    // 口径 = 图例各序列之和 (与图例逐项可对), 未展示的隐藏序列也计入 —— 它仍是窗口内用量.
    get trendHeadTotal() {
      const sums = (this.modelTrendData && this.modelTrendData.sums) || {};
      let n = 0;
      for (const k in sums) n += Number(sums[k]) || 0;
      return n ? t('trend_head_total', this.fmtT(n)) : '';
    },
    // 图表偏好持久化 (P3): 记住"上次看的图型 (折线/柱状)", 重开面板不必再点一次.
    // 仅存展示偏好 —— 不涉及范围/粒度 (那两个由时间筛选统一管, 已在 restoreAnalyticsSpan).
    saveChartPrefs() {
      try { localStorage.setItem('aigate.chartMode', this.chartMode || 'line'); } catch (e) {}
    },
    // 切换图型: 单一入口 (写偏好 + 重算), 避免模板里两处手写 chartMode=...;computeModelTrend().
    setChartMode(m) {
      if (m !== 'line' && m !== 'bar') return;
      this.chartMode = m;
      this.saveChartPrefs();
      this.computeModelTrend();
    },
    restoreChartPrefs() {
      try {
        const m = localStorage.getItem('aigate.chartMode');
        if (m === 'line' || m === 'bar') this.chartMode = m;
      } catch (e) {}
    },
    toggleModel(k){ this.hiddenModels[k]=!this.hiddenModels[k]; this.computeModelTrend(); },
    // 图表上下文标签: 只在图表的展示区间**不同于整个窗口**时才输出
    // (自适应裁剪掉了首尾空桶时, 明确写出实际展示的区间, 免得让人以为画的是整个窗口).
    // 粒度 · 时区由页头统一承担 —— 原先这里也输出一份, 与分析页页头完全重复:
    // 同一屏出现两次「天 粒度 · UTC+8」. 无裁剪时返回空串, 该行自然不占位.
    get trendContextLabel() {
      const d = (this.modelTrendData && this.modelTrendData.dates) || [];
      if (!this.modelTrendTrimmed || d.length < 2) return '';
      return t('trend_shown_range', (d[0][1] || ''), (d[d.length - 1][1] || ''));
    },
    get trendGranularityLabel() {
      return t(this.trendGranularity==='hour'?'granularity_hour':this.trendGranularity==='month'?'granularity_month':'granularity_day');
    },
    get tzLabel() { return t('tz_label'); },
    escapeHtml(value) { return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },