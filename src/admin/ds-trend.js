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
    // ── 趋势图的「看什么指标」──
    //
    // # 2026-10-27 重做（用户指出我之前做错了）
    //
    // 上一版我发明了「总量 / 分项 / 缓存」三选一 —— 那是**臆造**，参考项目没有这回事。
    // 参考项目（WaLiAPI DashboardPage.tsx:39-41、1073-1100）的真实设计是**三个独立开关**：
    //   showInputToken / showOutputToken / showCachedToken
    // 可**任意组合**（只开输入 = 输入曲线；三个都开 = 三段堆叠），且与「模型图例」
    // **正交**：开关决定"柱里堆什么指标"，模型图例决定"堆哪些模型"。
    //
    // # 为什么这个更正重要
    //
    // 旧的三选一模型有两个硬伤：
    //   ① 选「分项」时，图上只剩三条**全站**序列 —— 模型维度整个消失了，
    //      用户看不到"哪个模型在涨"，而那才是趋势图的主要用途；
    //   ② 选「总量」时，缓存又被并进总量里 —— 恰恰是本项目最该被看见的东西。
    //
    // 现在：每条模型序列的**每一段**都可按指标拆分，三个开关同时作用。
    //
    // 颜色是**固定语义**（与参考项目一致）：输入=蓝、输出=紫、缓存=绿。
    // 指标开关与模型图例各管各的，互不干扰。
    //
    // ⚠️ 关于"缓存"的性质：cached_tokens 是**上游 KV Cache 命中量**，
    //    它是 prompt 的**子集**（命中部分被上游缓存、折扣计费），
    //    所以"输入+输出+缓存"三者相加在语义上是有重叠的。
    //    参考项目也是三个独立开关而非互斥（它同样把 cached 单列）。
    //    我们照它做，但**默认只开输入+输出**，缓存默认关闭 ——
    //    这样默认看到的总量是干净的（输入+输出），要研究缓存时再打开。

    /// 三个指标开关的状态（可任意组合）。
    get trendMetrics() {
      const m = this.trendMetricState || {};
      return {
        input: m.input !== false,
        output: m.output !== false,
        cache: m.cache === true,   // 缓存默认关（与输入有重叠，见上方注释）
      };
    },

    /// 某条记录在指定指标上的值。
    trendRowValues(r) {
      return {
        in: Number(r && r.prompt_tokens || 0),
        out: Number(r && r.completion_tokens || 0),
        cache: Number(r && r.cached_tokens || 0),
      };
    },

    /// 按当前开关，算出一条记录要计入的总量（用于排序与筛选 Top N）。
    trendRowScalar(v) {
      const m = this.trendMetrics;
      let s = 0;
      if (m.input) s += v.in;
      if (m.output) s += v.out;
      if (m.cache) s += v.cache;
      return s;
    },

    /// 切换一个指标开关（单一入口：改状态 + 重算 + 强制淡入给反馈）。
    /// 至少保留一个指标 —— 全关会让图变成空白（无信息可画），那不是"用户想看的"。
    toggleTrendMetric(k) {
      if (!['input', 'output', 'cache'].includes(k)) return;
      const cur = this.trendMetrics;
      const next = { input: cur.input, output: cur.output, cache: cur.cache };
      next[k] = !next[k];
      const on = ['input', 'output', 'cache'].filter(x => next[x]);
      if (!on.length) {
        // 全部关掉 → 把刚点的这个重新打开（等价于"至少留一个"）
        next[k] = true;
      }
      this.trendMetricState = next;
      this.saveChartPrefs();
      this._trendForceFade = true;
      this.computeModelTrend();
    },

    computeModelTrend() {
      const metrics = this.trendMetrics;
      const rows=this.stats?.model_trends||[]; const sums={};
      rows.forEach(r=>{
        const k=(r.provider||'')+'/'+(r.upstream_model||'');
        const v=this.trendRowValues(r);
        sums[k]=(sums[k]||0)+this.trendRowScalar(v);
      });
      const allKeys=Object.keys(sums).sort((a,b)=>sums[b]-sums[a]).slice(0,6);
      const keys=allKeys.filter(k=>!this.hiddenModels[k]);
      this.modelTrendData={keys:allKeys,sums,dates:[],series:[],xPct:[],trendByTs:new Map(),
                          metrics, metricTotals:{input:0,output:0,cache:0}};
      if(!rows.length||!keys.length){this._setTrendHtml('');return;}
      const trends=(this.stats.trends||[]).filter(x=>x&&x.ts);
      const dates=[...new Map(trends.map(r=>[r.ts,r.date])).entries()].sort((a,b)=>a[0]-b[0]);
      if(!dates.length){this._setTrendHtml('');return;}
      // 画布与内边距 (viewBox 坐标); 比例由 CSS 等比缩放维持, 不再用 preserveAspectRatio 拉伸.
      const W=900,H=260,left=56,right=20,top=22,bottom=40,pw=W-left-right,ph=H-top-bottom;
      const palette=CHART_PALETTE;
      // ⚠️ colorOf 只定义一处 —— 同名 const 在同一作用域重复声明会让**整个片段**
      //    语法错误，结果是页面上所有 ds-*.js 方法一起消失（2026-10-26 已实测踩过）。
      const map=new Map(rows.map(r=>[r.ts+'/'+r.provider+'/'+r.upstream_model,r]));
      // 取数：每条模型序列的每桶值 = 当前开关下的指标和
      const values = keys.map(k=>dates.map(([ts])=>{
        const [p,...u]=k.split('/');
        const r=map.get(ts+'/'+p+'/'+u.join('/'));
        if(!r) return 0;
        const v=this.trendRowValues(r);
        return this.trendRowScalar(v);
      }));
      // 指标配色（固定语义，与开关圆点一致）：输入=蓝、输出=紫、缓存=绿。
      // ⚠️ 2026-10-27 撤掉 METRIC_SHADE：早先我用"模型色相 + 指标明暗"两个通道
      //    同时编码，实测在柱状堆叠里**分不清哪段是哪个模型**（整柱看起来
      //    就是三色，而实际有 3 个模型）。指标只影响"段的值"，不再切颜色。
      const METRIC_LABEL = { input:'trend_dim_in', output:'trend_dim_out', cache:'trend_dim_cache' };
      const METRIC_KEYS = ['input','output','cache'].filter(k=>metrics[k]);
      const labelOf = (k) => k.split('/').slice(1).join('/') || k;
      const colorOf = (k) => palette[allKeys.indexOf(k) % palette.length];
      // 指标合计（底部工具栏右侧显示）：按**可见模型**算。
      const metricTotals = { input:0, output:0, cache:0 };
      for (const k of keys) {
        const [p,...u]=k.split('/');
        for (const r of rows) {
          if (r.provider !== p) continue;
          const up = r.upstream_model || r.model;
          if (up !== u.join('/')) continue;
          const v = this.trendRowValues(r);
          metricTotals.input += v.in; metricTotals.output += v.out; metricTotals.cache += v.cache;
        }
      }
      this.modelTrendData.metricTotals = metricTotals;
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
      this.modelTrendData.series=keys.map((k,si)=>({key:k,label:labelOf(k),color:colorOf(k),values:values[si]}));
      this.modelTrendData.trendByTs=new Map(trends.map(r=>[r.ts,r]));
      // 指标分段的每桶值（柱状模式用）：每个模型在每桶的各指标值。
      // 堆叠顺序：先指标、再模型（与参考项目一致 —— 柱高 = 选中指标之和）。
      const segsByKey = {};
      keys.forEach(k=>{
        const [p,...u]=k.split('/');
        segsByKey[k]=dates.map(([ts])=>{
          const r=map.get(ts+'/'+p+'/'+u.join('/'));
          if(!r) return {input:0,output:0,cache:0};
          const v=this.trendRowValues(r);
          return { input: metrics.input? v.in:0, output: metrics.output? v.out:0, cache: metrics.cache? v.cache:0 };
        });
      });
      this.modelTrendData.segsByKey = segsByKey;
      this.modelTrendData.metricLabel = METRIC_LABEL;
      this.modelTrendData.metricKeys = METRIC_KEYS;
      // viewBox + width:100% + height:auto: 等比缩放, 圆点与文字不被拉扁.
      let out='<svg viewBox="0 0 '+W+' '+H+'" style="display:block;width:100%;height:auto" class="model-trend-svg" role="img" aria-label="'+this.escapeHtml(t('token_trend_title'))+'">';
      for(let i=0;i<=4;i++){const yy=top+ph*i/4;out+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+yy+'" y2="'+yy+'" class="chart-grid"/><text x="'+(left-8)+'" y="'+(yy+3)+'" text-anchor="end" class="chart-axis">'+this.escapeHtml(this.fmtT(max*(1-i/4)))+'</text>';}
      if(this.chartMode==='bar'){
        // 堆叠柱：**每段 = 一个模型**（段色 = 模型色），段的值 = 该模型在当前
        // 指标开关下的合计。这是参考项目的做法（DashboardPage.tsx:935-955：
        // 柱内按模型分段、每段用 TREND_LINE_COLORS[seg.colorIdx]）。
        //
        // ⚠️ 2026-10-27 修正：我第一版写成「按模型循环、每个模型内再按指标切三段
        //    + opacity 深浅」——结果是每根柱有 3×N 段，颜色靠 opacity 区分，
        //    图上**分不清哪段是哪个模型**（截图证实：整柱看起来就是
        //    蓝/橙/紫三段，而实际有 3 个模型）。指标应该影响"段的值"，
        //    不该再切一刀颜色 —— 那会把两个维度混在一个视觉通道里。
        //
        // 指标构成怎么看？→ 悬停浮层的指标分解行（.tt-metrics），或切到折线看总量。
        const bw=Math.min(28,pw/n*.7);
        const cumul=new Array(n).fill(0);
        keys.forEach((k,ki)=>{
          const base=colorOf(k);
          const vals=values[ki]||[];
          vals.forEach((v,i)=>{
            const val=Number(v||0);
            if(!val) return;
            const y0=y(cumul[i]); cumul[i]+=val; const y1=y(cumul[i]);
            const barH=Math.max(0,y0-y1);
            out+='<rect x="'+(x(i)-bw/2)+'" y="'+y1+'" width="'+bw+'" height="'+barH+'" rx="2" fill="'+base+'"/>';
          });
        });
      }else{
        // 折线：每个模型一条（指标已合进该模型的值里）。
        // ⚠️ 折线模式下**不做**指标分段 —— 同一模型的三条指标线会互相穿插，
        //    比柱状更难读。要看指标构成请切柱状（与参考项目只有堆叠柱一致）。
        values.forEach((vals,si)=>{const k=keys[si],color=colorOf(k);const pts=vals.map((v,i)=>[x(i),y(v)]);
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
            ? (keys.length<=2?.22:(keys.length<=3?.14:.10))
            : (keys.length<=2?.13:.045);
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
      // 指标分解（按当前开关分列）—— 参考项目的浮层也是这么分的
      // （DashboardPage.tsx:1038-1054 表头按 输入/输出/缓存 分列）。
      // 我们这里给**该桶的指标合计**（全站），因为模型已经逐行列在上面了。
      const mk = (d.metricKeys||[]);
      if (mk.length > 1 && d.segsByKey) {
        const per = { input:0, output:0, cache:0 };
        for (const k in d.segsByKey) {
          const sv = d.segsByKey[k] && d.segsByKey[k][i];
          if (!sv) continue;
          per.input += Number(sv.input||0);
          per.output += Number(sv.output||0);
          per.cache += Number(sv.cache||0);
        }
        const cells = mk.map(m =>
          '<span class="tt-metric"><i style="background:'+this.trendMetricColor(m)+'"></i>'
          + this.escapeHtml(t(d.metricLabel ? d.metricLabel[m] : 'trend_dim_' + m))
          + ' <b>' + this.fmtT(per[m]||0) + '</b></span>').join('');
        html += '<div class="tt-metrics">' + cells + '</div>';
      }
      const tr=d.trendByTs&&d.trendByTs.get?d.trendByTs.get((d.dates[i]||[])[0]):null;
      if(tr){
        const cost=this.stats?.has_price_config?this.fmtMoney(tr.total_cost||0):t('chart_no_price');
        html+='<div class="tt-f">'+t('trend_bucket_metrics',tr.requests||0,tr.errors||0,this.fmtT(tr.total_prompt_tokens||0),this.fmtT(tr.total_completion_tokens||0),this.escapeHtml(cost))+'</div>';
      }
      return html;
    },
    /// 指标的固定语义色（与开关圆点、图内分段一致）。
    trendMetricColor(m) {
      return { input:'var(--accent)', output:'var(--secondary)', cache:'var(--ok)' }[m] || 'var(--muted)';
    },
    /// 指标开关列表（供模板渲染底部工具栏左侧）。
    get trendMetricToggles() {
      const m = this.trendMetrics;
      return [
        { k:'input',  label:t(this.trendMetricLabelKey('input')),  color:this.trendMetricColor('input'),  on:m.input },
        { k:'output', label:t(this.trendMetricLabelKey('output')), color:this.trendMetricColor('output'), on:m.output },
        { k:'cache',  label:t(this.trendMetricLabelKey('cache')),  color:this.trendMetricColor('cache'),  on:m.cache },
      ];
    },
    /// 指标合计行（底部工具栏右侧；只显示**已打开**的指标）。
    get trendMetricSummary() {
      const d = this.modelTrendData || {};
      const tot = d.metricTotals || {};
      const m = this.trendMetrics;
      const out = [];
      for (const k of ['input','output','cache']) {
        if (!m[k]) continue;
        const v = Number(tot[k]||0);
        if (!v && k === 'cache') continue;   // 缓存为 0 且没数据时不显示（避免"0"看着像实测值）
        out.push({ k, label:t(this.trendMetricLabelKey(k)), color:this.trendMetricColor(k), v:this.fmtT(v) });
      }
      return out;
    },
    /// 指标的 i18n 键（input→trend_dim_in、output→trend_dim_out）。
    //
    // ⚠️ 不能拼成 'trend_dim_' + 'input' —— 实际键名是 trend_dim_in / trend_dim_out，
    // 拼错会让界面直接显示裸键名（实测踩过：合计行显示 "trend_dim_input"）。
    trendMetricLabelKey(k) {
      return { input:'trend_dim_in', output:'trend_dim_out', cache:'trend_dim_cache' }[k] || 'trend_dim_in';
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
    // 图表偏好持久化 (P3): 记住"上次看的图型 (折线/柱状)"与三个指标开关,
    // 重开面板不必再点一次.
    // 仅存展示偏好 —— 不涉及范围/粒度 (那两个由时间筛选统一管, 已在 restoreAnalyticsSpan).
    saveChartPrefs() {
      try {
        localStorage.setItem('aigate.chartMode', this.chartMode || 'line');
        localStorage.setItem('aigate.trendMetrics', JSON.stringify(this.trendMetrics));
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
    restoreChartPrefs() {
      try {
        const m = localStorage.getItem('aigate.chartMode');
        if (m === 'line' || m === 'bar') this.chartMode = m;
        const dm = localStorage.getItem('aigate.trendMetrics');
        if (dm) {
          const p = JSON.parse(dm);
          if (p && typeof p === 'object') {
            this.trendMetricState = {
              input: p.input !== false,
              output: p.output !== false,
              cache: p.cache === true,
            };
          }
        }
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