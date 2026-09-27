// ── 价格面板 ──
// 单模型价格编辑 (高峰/空闲/缓存档) + 面板间复制粘贴
// 主要成员: openPricePanel / priceFromPanel / applyPriceToSelected
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)

    // 单价编辑面板 (行列与供应商官方价目表一致: 项目 × 空闲/高峰时段)
    // 面板表单 → price 对象. 七项全为 0 视为「无价」, 返回 null.
    priceFromPanel(){
      const n = v => { const x = parseFloat(v); return Number.isFinite(x) ? x : 0; };
      const f = {
        cache_read_per_m: n(this.pricePanel.cache_read_per_m),
        cache_read_per_m_offpeak: n(this.pricePanel.cache_read_per_m_offpeak),
        input_per_m: n(this.pricePanel.input_per_m),
        input_per_m_offpeak: n(this.pricePanel.input_per_m_offpeak),
        output_per_m: n(this.pricePanel.output_per_m),
        output_per_m_offpeak: n(this.pricePanel.output_per_m_offpeak),
        cache_creation_per_m: n(this.pricePanel.cache_creation_per_m),
      };
      if(Object.values(f).every(v => v === 0)) return null;
      return {
        input_per_m: f.input_per_m, output_per_m: f.output_per_m,
        cache_read_per_m: f.cache_read_per_m || null,
        cache_creation_per_m: f.cache_creation_per_m || null,
        input_per_m_offpeak: f.input_per_m_offpeak,
        output_per_m_offpeak: f.output_per_m_offpeak,
        cache_read_per_m_offpeak: f.cache_read_per_m_offpeak,
      };
    },
    // price 对象 → 面板表单 (0 / 缺失统一显示为空, 便于区分"未填"与"填 0").
    priceToPanel(p){
      const q = p || {};
      const nz = v => (typeof v === 'number' && v !== 0) ? v : null;
      this.pricePanel = {
        cache_read_per_m: q.cache_read_per_m ?? null,
        cache_read_per_m_offpeak: nz(q.cache_read_per_m_offpeak),
        input_per_m: q.input_per_m ?? null,
        input_per_m_offpeak: nz(q.input_per_m_offpeak),
        output_per_m: q.output_per_m ?? null,
        output_per_m_offpeak: nz(q.output_per_m_offpeak),
        cache_creation_per_m: q.cache_creation_per_m ?? null,
      };
    },
    openPricePanel(model){
      // 价格面板叠在抽屉之上, 用独立焦点槽 (_priceLastFocus), 不覆盖抽屉的 _lastFocus.
      this._priceLastFocus = document.activeElement;
      this.priceToPanel(model.price);
      this.pricePanelModel = model;
    },
    // models.dev 收录的参考价 (USD/1M) -> 换算为内部基准 CNY/1M 展示.
    //
    // 只展示、**不写入** price: models.dev 是单一价且以 USD 计价, 而 AIGate 内部按 CNY
    // 计价, 部分官方供应商还是峰谷双价 (DeepSeek 空闲价 = 高峰价一半, 另有独立缓存命中价).
    // 实测 DeepSeek flash: 官方高峰 2.0/8.0/0.04 元, models.dev 换算约 1.08/4.32/0.02 ——
    // 两档都不吻合; pro 偏差达 44%. 自动填入会把已配准的官方价格改错, 故交用户自行判断.
    // 且必须按**同名供应商**取价: 同名模型有几十家收录, 报价从 0 到 0.435 不等.
    refPriceRows(){
      const m = this.pricePanelModel;
      const c = m ? this.refCostFor(m) : null;
      if(!c) return [];
      // USD -> CNY: rates[code] = 1 单位该币种 = 多少 CNY (与 currency.rs 语义一致).
      const usdToCny = Number((this.currencyConfig.rates||{}).USD) || 0;
      if(!(usdToCny > 0)) return [];
      const fmt = v => '¥' + (v >= 1 ? v.toFixed(3).replace(/0+$/,'').replace(/\.$/,'') : v.toFixed(4)) + ' / 1M';
      const rows = [];
      if(c.input != null) rows.push({label: t('price_row_cache_miss'), value: fmt(c.input * usdToCny)});
      if(c.output != null) rows.push({label: t('price_row_output'), value: fmt(c.output * usdToCny)});
      if(c.cache_read != null) rows.push({label: t('price_row_cache_hit'), value: fmt(c.cache_read * usdToCny)});
      if(c.cache_write != null) rows.push({label: t('price_row_cache_write'), value: fmt(c.cache_write * usdToCny)});
      return rows;
    },
    closePricePanel(){
      const was = this.pricePanelModel;
      this.pricePanelModel = null;
      // 焦点归还 (仅本次确实打开过): 回到触发按钮, 键盘用户不迷路.
      if (was) {
        const f = this._priceLastFocus; this._priceLastFocus = null;
        if (f && f.focus) this.$nextTick(() => { try { f.focus(); } catch (e) {} });
      }
    },
    savePricePanel(){
      if(!this.pricePanelModel) return;
      this.pricePanelModel.price = this.priceFromPanel();
      this.markDirty();
      this.closePricePanel();
    },
    // 复制当前面板价格到剪贴板 (内存缓冲区, 跨模型/跨供应商可用; null 亦为有效值=无价).
    copyPriceFromPanel(){
      this.priceClipboard = this.priceFromPanel();
      this.priceClipboardSet = true;
      this.priceClipMsg = t('price_copied');
      setTimeout(()=>{ this.priceClipMsg=''; }, 2000);
    },
    // 把剪贴板价格填回面板 (不落盘, 仍需点"保存"确认).
    pastePriceToPanel(){
      if(!this.priceClipboardSet) return;
      this.priceToPanel(this.priceClipboard);
    },
    // 把剪贴板价格应用到当前勾选的模型 (整批覆盖).
    applyPriceToSelected(pi){
      const prov = this.providersFormData[pi];
      if(!prov || !this.priceClipboardSet) return;
      const idxs = Object.keys(prov._selected||{}).filter(k=>prov._selected[k]).map(Number);
      if(!idxs.length) return;
      for(const i of idxs){
        if(!prov.models[i]) continue;
        prov.models[i].price = this.priceClipboard ? JSON.parse(JSON.stringify(this.priceClipboard)) : null;
      }
      this.markDirty();
      toast(t('price_applied', idxs.length), 'success');
    },
    selectedIndexes(prov){
      if(!prov || !prov._selected) return [];
      return Object.keys(prov._selected).filter(k=>prov._selected[k]).map(Number);
    },
    clearPricePanel(){
      if(!this.pricePanelModel) return;
      this.pricePanelModel.price = null;
      this.markDirty();
      this.closePricePanel();
    },