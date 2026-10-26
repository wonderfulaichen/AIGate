// ── 趋势图 ──
// 模型趋势 SVG 计算 / 悬停浮层 / 图例 / 图表偏好
// 主要成员: computeModelTrend / onTrendHover / trendTooltipHtml
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    // 趋势图交叉淡入 (数据驱动): x-html 是**整段替换**, 没法靠 CSS 过渡, 只能由 JS 打标.
    // 只在内容真的变了才播 —— 3s 轮询下数据常常没变, 少了这个判据就会变成每 3s 一次频闪.
    // 空图/首次/降级直接换 (空态是另一块 DOM, 对它做淡出没有意义).
    // **force 通路**: 切时间范围/图型时置 _trendForceFade, 让"内容碰巧相同"也重播一次 ——
    // 用户刚点了控件, 必须有反馈, 否则读作"点了没反应".
    // ⚠️ 有 pending 的淡入时不能走"内容相同就早退": 那个 pending 定时器握的是**旧** html,
    //    放它跑完就会把已显示的旧图又贴回来 (所以 pending 时先清定时器再判重).
    _setTrendHtml(html) {
      const force = this._trendForceFade;
      this._trendForceFade = false;
      const pending = !!this._trendFadeTimer;
      if (!pending && !force && this.modelTrendSvgHtml === html) return;
      clearTimeout(this._trendFadeTimer); this._trendFadeTimer = 0;
      if (!html || !this.modelTrendSvgHtml || prefersReducedMotion()) {
        this.modelTrendSvgHtml = html; this.trendFading = false; return;
      }
      this.trendFading = true;
      this._trendFadeTimer = setTimeout(() => {
        this._trendFadeTimer = 0;
        this.modelTrendSvgHtml = html;
        this.trendFading = false;
      }, 160);
    },
    // ── 趋势图的「看什么」维度（2026-10-26）──
    //
    // # 为什么需要
    //
    // 此前趋势图只有**一个**维度：输入+输出的总量。于是有两个问题看不出来：
    //   ① 缓存省下来的量随时间怎么变（这是本项目最核心的卖点，却只有静态命中率数字）
    //   ② 涨的是输入还是输出（两者对应的优化手段完全不同：输入大要瘦身上下文，
    //      输出大要约束生成长度）
    // 参考项目的趋势图有「输入 / 输出 / 缓存」三个独立开关，可任意组合堆叠。
    //
    // # 三种取值
    //   total = 输入 + 输出（默认，与旧行为一致）
    //   split = 输入、输出、缓存各自一个序列（可堆叠对比）
    //   cache = 只看缓存命中量
    //
    // ⚠️ 关于「未采集 ≠ 0」：后端 ModelTrend.cached_tokens 是**本轮新增**字段，
    //    历史日志/旧快照里没有它 → serde(default) 兜底为 0。所以"缓存为 0"
    //    可能是真的没命中，也可能是那批数据没记录。这个区分写在界面提示里
    //    （trend_cache_note），不假装 0 一定是实测值。
    get trendDimField() { return this.trendDim || 'total'; },

    /// 取某条趋势记录在该维度下的**一组**值（数组，供 split 拆成多序列）。
    /// 返回 [{key,label,color,v}]
    trendRowValues(r) {
      const p = Number(r && r.prompt_tokens || 0);
      const c = Number(r && r.completion_tokens || 0);
      const h = Number(r && r.cached_tokens || 0);
      return { in: p, out: c, cache: h };
    },

    /// 该维度下用于**排序/筛选 Top N** 的标量（split 用三项之和）。
    trendRowScalar(v, dim) {
      if (dim === 'cache') return v.cache;
      if (dim === 'split') return v.in + v.out + v.cache;
      return v.in + v.out;
    },

    computeModelTrend() {
      const dim = this.trendDimField;
      const rows=this.stats?.model_trends||[]; const sums={};
      rows.forEach(r=>{
        const k=(r.provider||'')+'/'+(r.upstream_model||'');
        const v=this.trendRowValues(r);
        sums[k]=(sums[k]||0)+this.trendRowScalar(v, dim);
      });
      const allKeys=Object.keys(sums).sort((a,b)=>sums[b]-sums[a]).slice(0,6);
      const keys=allKeys.filter(k=>!this.hiddenModels[k]);
      this.modelTrendData={keys:allKeys,sums,dates:[],series:[],xPct:[],trendByTs:new Map()};
      if(!rows.length||!keys.length){this._setTrendHtml('');return;}
      const trends=(this.stats.trends||[]).filter(x=>x&&x.ts);
      const dates=[...new Map(trends.map(r=>[r.ts,r.date])).entries()].sort((a,b)=>a[0]-b[0]);
      if(!dates.length){this._setTrendHtml('');return;}
      // 画布与内边距 (viewBox 坐标); 比例由 CSS 等比缩放维持, 不再用 preserveAspectRatio 拉伸.
      const W=900,H=260,left=56,right=20,top=22,bottom=40,pw=W-left-right,ph=H-top-bottom;
      const palette=CHART_PALETTE;
      // ⚠️ colorOf 统一在下方定义（要同时处理"模型序列"与 split 的"三条固定指标"），
      //    这里不再另起一份 —— 同名 const 在同一作用域重复声明会让**整个片段**
      //    语法错误，结果是页面上所有 ds-*.js 方法一起消失（已实测踩过）。
      const map=new Map(rows.map(r=>[r.ts+'/'+r.provider+'/'+r.upstream_model,r]));
      // ── 序列构造：两种形态 ──
      //  · 非 split：按**模型**分序列（每条一个模型，值与旧行为口径一致）
      //  · split  ：按**指标**分序列（输入 / 输出 / 缓存三条，汇总所有显示的模型）
      //
      // 为什么 split 不按模型拆：6 个模型 × 3 个指标 = 18 条序列，堆叠图会糊成一片，
      // 而用户此刻想回答的是"涨的是输入还是输出"—— 那是**全站层面**的问题，
      // 不是"哪个模型"。参考项目在拆指标时也是全站三条。
      const isSplit = (dim === 'split');
      const values = isSplit
        ? (() => {
            // 三条：输入 / 输出 / 缓存（各自汇总 keys 里的模型）
            const acc = [ [], [], [] ];
            dates.forEach(([ts]) => {
              let si = 0, so = 0, sc = 0;
              for (const k of keys) {
                const [p, ...u] = k.split('/');
                const r = map.get(ts + '/' + p + '/' + u.join('/'));
                if (!r) continue;
                const v = this.trendRowValues(r);
                si += v.in; so += v.out; sc += v.cache;
              }
              acc[0].push(si); acc[1].push(so); acc[2].push(sc);
            });
            return acc;
          })()
        : keys.map(k=>dates.map(([ts])=>{
            const [p,...u]=k.split('/');
            const r=map.get(ts+'/'+p+'/'+u.join('/'));
            if(!r) return 0;
            const v=this.trendRowValues(r);
            return this.trendRowScalar(v, dim);
          }));
      // 序列的键与标签：split 时用指标名，否则用模型名
      const seriesKeys = isSplit ? ['__in', '__out', '__cache'] : keys;
      const labelOf = (k) => {
        if (k === '__in') return t('trend_dim_in');
        if (k === '__out') return t('trend_dim_out');
        if (k === '__cache') return t('trend_dim_cache');
        return k.split('/').slice(1).join('/') || k;
      };
      // split 的颜色：三条固定色（输入=accent、输出=secondary、缓存=ok），
      // 不跟随模型调色板 —— 三条指标是固定语义，配色固定才好认。
      const colorOf = (k) => {
        if (k === '__in') return 'var(--accent)';
        if (k === '__out') return 'var(--secondary)';
        if (k === '__cache') return 'var(--ok)';
        return palette[allKeys.indexOf(k) % palette.length];
      };
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
      this.modelTrendData.series=seriesKeys.map((k,si)=>({key:k,label:labelOf(k),color:colorOf(k),values:values[si]}));
      this.modelTrendData.trendByTs=new Map(trends.map(r=>[r.ts,r]));
      // viewBox + width:100% + height:auto: 等比缩放, 圆点与文字不被拉扁.
      let out='<svg viewBox="0 0 '+W+' '+H+'" style="display:block;width:100%;height:auto" class="model-trend-svg" role="img" aria-label="'+this.escapeHtml(t('token_trend_title'))+'">';
      for(let i=0;i<=4;i++){const yy=top+ph*i/4;out+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+yy+'" y2="'+yy+'" class="chart-grid"/><text x="'+(left-8)+'" y="'+(yy+3)+'" text-anchor="end" class="chart-axis">'+this.escapeHtml(this.fmtT(max*(1-i/4)))+'</text>';}
      if(this.chartMode==='bar'){const bw=Math.min(28,pw/n*.7);const cumul=new Array(n).fill(0);values.forEach((vals,si)=>{const k=seriesKeys[si],color=colorOf(k);vals.forEach((v,i)=>{if(!v)return;const y0=y(cumul[i]);cumul[i]+=v;const y1=y(cumul[i]);const barH=y0-y1;out+='<rect x="'+(x(i)-bw/2)+'" y="'+y1+'" width="'+bw+'" height="'+barH+'" rx="3" fill="'+color+'"/>';});});}else{
        values.forEach((vals,si)=>{const k=seriesKeys[si],color=colorOf(k);const pts=vals.map((v,i)=>[x(i),y(v)]);
          // 平滑曲线 (smooth() 此前只定义未使用 -> 图上一直是折角)
          const lineD=smooth(pts);
          const gid='tg'+si;
          // 多序列层次 (本轮): keys 已按用量降序, 故 si=0 即**主序列**。主序列满强度,
          // 其余按排名递减**描边不透明度** —— 线宽仍固定 2px (frontend.md 的约定),
          // 只靠不透明度做层级, 让"主脉络"先被读到, 而不是 N 条同级线互相打架.
          const lead = si===0;
          const lineOp = lead ? 1 : Math.max(.42, .78 - si*.09);
          // 面积不透明度随系列数收敛: 少系列给足"面"的存在感, 多系列压淡让"线"承担可读性。
          // 且**只有主序列给到可感知的面**, 其余压到很淡 —— 多层渐变叠加会把整图糊成色块.
          const areaOp = lead
            ? (seriesKeys.length<=2?.22:(seriesKeys.length<=3?.14:.10))
            : (seriesKeys.length<=2?.13:.045);
          out+='<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'
            +'<stop offset="0" stop-color="'+color+'" stop-opacity="'+areaOp+'"/>'
            +'<stop offset="1" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>';
          // 面积沿同一条平滑曲线闭合到基线, 与折线严格同形
          const areaD=lineD+' L'+x(n-1)+','+(top+ph)+' L'+x(0)+','+(top+ph)+' Z';
          out+='<path d="'+areaD+'" fill="url(#'+gid+')"/>';
          out+='<path d="'+lineD+'" stroke="'+color+'" stroke-opacity="'+lineOp+'" class="chart-line"/>';
          // 不画静态圆点 (对齐 cc-switch: 其图表全程无 dot, 靠悬停 activeDot)。
          // 点位由悬停十字线 + 浮层指示, 曲线本身保持干净。
        });
      }
      out+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+(top+ph)+'" y2="'+(top+ph)+'" class="chart-base"/>';
      const step=Math.max(1,Math.ceil(n/8));dates.forEach((d,i)=>{if(i%step===0||i===n-1)out+='<text x="'+x(i)+'" y="'+(H-12)+'" text-anchor="middle" class="chart-axis">'+this.escapeHtml(d[1]||'')+'</text>';});out+='</svg>';
      this._setTrendHtml(out);
    },
    // ── Token 趋势悬停: 十字线 + 该时段各模型用量浮层 ──
    onTrendHover(ev) {
      const d=this.modelTrendData; if(!d||!d.dates||!d.dates.length) return;
      const el=ev.currentTarget; const r=el.getBoundingClientRect(); if(!r.width) return;
      const W=900, left=56, right=20, pw=W-left-right, n=d.dates.length;
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
      try {
        localStorage.setItem('aigate.chartMode', this.chartMode || 'line');
        localStorage.setItem('aigate.trendDim', this.trendDimField);
      } catch (e) {}
    },
    // 切换图型: 单一入口 (写偏好 + 重算), 避免模板里两处手写 chartMode=...;computeModelTrend().
    setChartMode(m) {
      if (m !== 'line' && m !== 'bar') return;
      this.chartMode = m;
      this.saveChartPrefs();
      this._trendForceFade = true;   // 切图型必须给反馈 (见 _setTrendHtml 的 force 说明)
      this.computeModelTrend();
    },
    /// 切换「看什么」维度（单一入口：改状态 + 重算 + 强制淡入给反馈）。
    /// 非法值一律归一到 total —— 与 setAnalyticsTab / setSettingsSection 同一条纪律：
    /// 面板可见性由枚举值驱动时，非法值不是"无操作"而是**图表空白**。
    setTrendDim(d) {
      const valid = ['total', 'split', 'cache'];
      this.trendDim = valid.includes(d) ? d : 'total';
      this.saveChartPrefs();
      this._trendForceFade = true;
      this.computeModelTrend();
    },
    restoreChartPrefs() {
      try {
        const m = localStorage.getItem('aigate.chartMode');
        if (m === 'line' || m === 'bar') this.chartMode = m;
        const d = localStorage.getItem('aigate.trendDim');
        if (d === 'total' || d === 'split' || d === 'cache') this.trendDim = d;
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