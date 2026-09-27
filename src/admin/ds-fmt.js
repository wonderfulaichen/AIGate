// ── 格式化与审计 ──
// 金额/时间/耗时等格式化 + 省量审计面板的派生量
// 主要成员: fmtMoney / fmtMs / auditAppliedSegs / kvSegs
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)



    // Formatters
    fmtMoney(v) {
      const sym = this.moneySymbol(this.currencyConfig.currency);
      return sym + this.convertCny(v).toFixed(2);
    },
    // 极小金额友好显示: 换算后不足最小分位时给出 "<¥0.01", 而不是 0.00.
    // 省量费用常远小于 1 分 (单请求 1009 tok × 2元/1M ≈ 0.002), 一律 ¥0.00 会让人误以为没生效.
    fmtMoneyTiny(v) {
      const c = this.convertCny(v);
      if (v > 0 && c < 0.005) return '<' + this.moneySymbol(this.currencyConfig.currency) + '0.01';
      return this.fmtMoney(v);
    },
    // 单价格式化 (表格单元格用): 紧凑展示「未命中/输出 (命中)」, 未配置显示「未配置」.
    // 鼠标悬停由 fmtPriceDetail 给出逐项明细.
    fmtPrice(p) {
      if (!p) return t('price_not_set');
      const sym = this.moneySymbol(this.currencyConfig.currency);
      const c = x => this.convertCny(x);
      let s = sym + c(p.input_per_m) + '/' + sym + c(p.output_per_m);
      if (p.cache_read_per_m != null) s += ' · ' + t('cache_label') + sym + c(p.cache_read_per_m);
      // 空闲价: 任一 offpeak 字段 > 0 即视为分时段计费, 追加「空闲」档.
      if (p.input_per_m_offpeak > 0 || p.output_per_m_offpeak > 0 || p.cache_read_per_m_offpeak > 0) {
        s += '  ' + t('offpeak_label') + ' ' + sym + c(p.input_per_m_offpeak) + '/' + sym + c(p.output_per_m_offpeak);
        if (p.cache_read_per_m_offpeak > 0) s += ' · ' + t('cache_label') + sym + c(p.cache_read_per_m_offpeak);
      }
      return s;
    },
    // 单价明细 (按钮 tooltip 用): 与面板/官方价目表同构, 逐行列出高峰与空闲两档.
    fmtPriceDetail(p) {
      if (!p) return t('price_not_set');
      const sym = this.moneySymbol(this.currencyConfig.currency);
      const c = x => sym + this.convertCny(x || 0);
      const rows = [
        t('price_col_peak'),
        '  ' + t('price_row_cache_hit') + ' ' + c(p.cache_read_per_m != null ? p.cache_read_per_m : p.input_per_m),
        '  ' + t('price_row_cache_miss') + ' ' + c(p.input_per_m),
        '  ' + t('price_row_output') + ' ' + c(p.output_per_m),
      ];
      if (p.input_per_m_offpeak > 0 || p.output_per_m_offpeak > 0 || p.cache_read_per_m_offpeak > 0) {
        rows.push(t('price_col_offpeak'));
        rows.push('  ' + t('price_row_cache_hit') + ' ' + c(p.cache_read_per_m_offpeak != null ? p.cache_read_per_m_offpeak : p.input_per_m_offpeak));
        rows.push('  ' + t('price_row_cache_miss') + ' ' + c(p.input_per_m_offpeak));
        rows.push('  ' + t('price_row_output') + ' ' + c(p.output_per_m_offpeak));
      }
      return rows.join('\n');
    },
    // 供应商 logo: 复用全局 providerLogo() (内联官方 SVG, 缺失回退首字母徽标).
    providerLogo(p) {
      return window.providerLogo(p);
    },
    // 记录列表的时间列: 带日期 —— 日志窗口本身跨天(实测 09/13 → 09/16),
    // 只显示 HH:MM:SS 时, 同一条 22:45:42 无法判断是哪一天.
    fmtTs(ts) {
      const d=new Date(ts*1000);
      const p=n=>String(n).padStart(2,'0');
      return p(d.getMonth()+1)+'/'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
    },
    fmtDateTime(ts){ const d=new Date(ts*1000); const loc=(window.Alpine&&Alpine.store('i18n')&&Alpine.store('i18n').lang==='en-US')?'en-US':'zh-CN'; return d.toLocaleString(loc); },
    fmtMs(ms) {
      const n = Number(ms);
      // 无数据 ≠ 0: null/undefined/NaN/0 都不当成有效延迟.
      if (!Number.isFinite(n) || n <= 0) return '—';
      if (n < 1000) return Math.round(n) + 'ms';
      return (n / 1000).toFixed(1) + 's';
    },
    // 百分比格式化 (入参为 0..100 的百分值).
    //
    // 两处舍入都会把"事实"改掉, 必须挡住:
    //   · 非满值不得显示成 100% —— `toFixed(1)` 把 99.95%+ 抬成 "100.0%", `Math.round` 把 99.5%+ 抬成 "100%".
    //     实测: 231 条缓存命中率显示 100.0% 而真 100% 的一条都没有 (例 603264/603517 = 99.958%);
    //     成功率显示 100% 的同时旁边写着"错误请求 1".
    //   · 非零不得显示成 0.0% —— 同理, 舍入不该改变"有没有"这个事实.
    // 分母为 0 (没有数据) 由调用方给 `—`, 不走这里 —— "没有数据"也不是 0%.
    fmtPct(pct) {
      const v = Number(pct);
      if (!Number.isFinite(v)) return '—';
      if (v >= 100) return '100%';
      if (v > 99.9) return '>99.9%';
      if (v <= 0) return '0%';
      if (v < 0.1) return '<0.1%';
      return v.toFixed(1) + '%';
    },
    fmtT(n) { if(n<1000) return n; if(n<1000000) return (n/1000).toFixed(1)+'K'; return (n/1000000).toFixed(1)+'M'; },
    // 精确整数 + 千分位 (对照 cc-switch UsageHero 的主体大数字: 精确值配 toLocaleString).
    // 与 fmtT 的分工: 大字号用本函数给**精确值**, 旁边的小胶囊再用 fmtT 给**量级缩写** ——
    // 两者并列才既有精度又不费眼; 只用 fmtT 会让"44.5K"既当主值又当副档, 信息重复.
    fmtInt(n) { return Math.round(Number(n) || 0).toLocaleString(); },
    // 省量明细行: "标签 <tokens> ≈ <费用>". 费用为 0 (未配置价格/免费) 时省略.
    auditLine(labelKey, tokens, fee) {
      const tk = this.fmtT(tokens || 0);
      const f = fee || 0;
      return t(labelKey) + ' ' + tk + (f > 0 ? ' ≈ ' + this.fmtMoneyTiny(f) : '');
    },
    // 单条日志的优化省量合计 (tokens). 优先用后端下发的拆分, 回退到三个字段直加.
    logSavedTotal(log) {
      const b = log && log.saved_breakdown;
      if (b) return (b.strip||0)+(b.strip_toolcall||0)+(b.trim||0)+(b.resp_cache||0);
      return (log.strip_saved_tokens||0)+(log.trim_saved_tokens||0)+(log.resp_cache_saved_tokens||0);
    },
    // 更新日志条目的行内 markdown 渲染: 仅支持 **粗体** 与 `代码`.
    // 先 HTML 转义再套标签 —— CHANGELOG 是本地文件, 但仍按不可信文本处理.
    renderMdInline(s) {
      const esc = String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
      return esc
        .replace(/\*\*([^*]+)\*\*/g, '<strong style="color:var(--fg)">$1</strong>')
        .replace(/`([^`]+)`/g, '<code class="mono" style="font-size:var(--fs-11);background:rgba(255,255,255,.06);padding:1px 4px;border-radius:3px">$1</code>');
    },
    // 省量审计窗口标签: 明确覆盖的实际时间范围与采样覆盖度.
    // 「当前窗口」= 内存里的日志缓冲区 (启动时从磁盘加载历史), 因此可能跨越很久, 必须说清.
    auditWindowLabel() {
      const a = (this.stats && this.stats.audit) || {};
      const span = (a.window_start && a.window_end)
        ? (this.fmtShort(a.window_start) + ' → ' + this.fmtShort(a.window_end))
        : '';
      const reqs = a.requests || 0;
      const sampled = a.sampled_requests || 0;
      const base = t('audit_basis', reqs);
      const est = a.estimated_requests || 0;
      return span + (span ? ' · ' : '') + base + (reqs && sampled < reqs ? ' · ' + t('audit_sampled', sampled) : '') + (est ? ' · ' + t('audit_estimated', est) : '');
    },
    fmtShort(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, '0');
      return p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    },
    // 潜在可省占比: 分母必须是「已采样请求的输入」而非窗口总输入 ——
    // 潜在项只来自做过审计的请求, 用总输入会把占比按采样比例稀释 (老日志越多压得越低).
    auditPctSampled(v) {
      const a = (this.stats && this.stats.audit) || {};
      const base = a.sampled_input_tokens || 0;
      if (!v || !base) return '—';
      // 上限保护: 潜在量来自做过审计的请求, 而分母只累计已采样请求的输入;
      // 两者口径在小样本下可能不一致, 会算出 >100% 的失真值. 封顶并标注.
      const pct = v / base * 100;
      if (pct > 100) return '>100%';
      return this.fmtPct(pct);
    },
    // 输出/输入 token 比 (看输出相对规模).
    outInRatio() {
      const a = (this.stats && this.stats.audit) || {};
      const inp = a.input_tokens || 0;
      const out = (a.output || {}).output_tokens || 0;
      if (!inp) return '—';
      return (out / inp * 100).toFixed(2) + '%';
    },
    // 长输出请求贡献了全部输出的百分之多少 (输出是否集中在少数请求).
    outLongShare() {
      const o = ((this.stats && this.stats.audit) || {}).output || {};
      if (!o.output_tokens) return '—';
      // 占比类: 走 fmtPct, 避免 9999/10000 被舍入显示成 100%.
      return this.fmtPct((o.long_tokens || 0) / o.output_tokens * 100);
    },
    // 已生效省量占比: 分母用「反事实基线」= 实际发送 + 已省, 而非只算实际发送.
    // 省下的 token 本就不在 input_tokens 内 (响应缓存回放的 prompt_tokens 记为 0),
    // 只除以 input_tokens 会算出 >100%.
    auditAppliedPct() {
      const a = (this.stats && this.stats.audit) || {};
      const saved = a.applied_saved_tokens || 0;
      const base = (a.input_tokens || 0) + saved;
      // 无数据 ≠ 0: 窗口内没有输入量时给 '—', 不给 '0%' (那是"确实没省量"的表达).
      if (!base) return '—';
      if (!saved) return '0%';
      return this.fmtPct(saved / base * 100);
    },
    // 省量审计: KV 缓存命中率 = 命中 token / 输入总量 (比"写入量"普适, 后者仅 Anthropic 上报).
    kvHitRate() {
      const a = (this.stats && this.stats.audit) || {};
      const base = a.input_tokens || 0;
      if (!base) return '—';
      return this.fmtPct((a.cache_read_tokens || 0) / base * 100);
    },
    // 省量审计: 某潜在省量占当前窗口总输入 token 的百分比 (分母缺失时显示占位).
    // 省量构成 (供分段比例条): 只列非零项, 全零时不渲染条.
    get auditAppliedSegs() {
      const a=(this.stats&&this.stats.audit)||{};
      const items=[
        {k:'strip',label:t('saved_strip'),v:Number(a.applied_strip_tokens||0),color:CHART_SEMANTIC.strip},
        {k:'tc',label:t('saved_strip_tc'),v:Number(a.applied_strip_toolcall_tokens||0),color:CHART_SEMANTIC.stripTc},
        {k:'trim',label:t('saved_trim'),v:Number(a.applied_trim_tokens||0),color:CHART_SEMANTIC.trim},
        {k:'resp',label:t('saved_resp'),v:Number(a.applied_resp_cache_tokens||0),color:CHART_SEMANTIC.respCache},
      ].filter(x=>x.v>0);
      return items.length>1?items:[];
    },
    get auditPotentialSegs() {
      const a=(this.stats&&this.stats.audit)||{};
      const items=[
        {k:'exempt',label:t('audit_exempt_reasoning'),v:Number(a.exempt_reasoning_tokens||0),color:CHART_SEMANTIC.exempt},
        {k:'dup',label:t('audit_dup_blocks'),v:Number(a.dup_block_tokens||0),color:CHART_SEMANTIC.dup},
      ].filter(x=>x.v>0);
      return items.length>1?items:[];
    },
    get kvSegs() {
      const a=(this.stats&&this.stats.audit)||{};
      const hit=Number(a.cache_read_tokens||0), miss=Number(a.cache_miss_tokens||0), cre=Number(a.cache_creation_tokens||0);
      const items=[
        {k:'hit',label:t('audit_kv_read'),v:hit,color:CHART_SEMANTIC.kvHit},
        {k:'miss',label:t('audit_kv_miss'),v:miss,color:CHART_SEMANTIC.kvMiss},
        {k:'cre',label:t('audit_kv_creation'),v:cre,color:CHART_SEMANTIC.kvCreation},
      ].filter(x=>x.v>0);
      return items.length>1?items:[];
    },
    fmtB(b) {
      if(b===0) return '0B'; if(b<1024) return b+'B'; if(b<1048576) return (b/1024).toFixed(1)+'KB';
      return (b/1048576).toFixed(1)+'MB';
    },
  };
}
