// ── 接入引导与设置导航 ──
// 3 步接入清单 (由真实数据推导) + 供应商总览 + 设置页分区表
// 主要成员: onboardSteps / providerRows / settingsSections
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)


    // 供应商总览: 以有请求记录的全部供应商为主序 (per_provider, 按请求数降序, 稳定不抖),
    // 余额数据仅作补充字段 merge (配了 balance_endpoint 的显示余额, 未配的显示"—").
    get providerRows() {
      // 供应商总览: 以余额查询结果 (balanceData) 为主序, 费用/优化省量数据按供应商名 merge.
      const costMap = {};
      (this.stats && this.stats.per_provider || []).forEach(p => {
        if (!p.provider || String(p.provider).trim().length === 0 || p.provider === '-') return; // 跳过空名/"-" 占位符
        costMap[p.provider] = {
          today_cost: p.today_cost || 0,
          total_cost: p.total_cost || 0,
          today_opt_saved_tokens: p.today_opt_saved_tokens || 0,
          today_strip_saved_tokens: p.today_strip_saved_tokens || 0,
          today_trim_saved_tokens: p.today_trim_saved_tokens || 0,
          today_resp_cache_saved_tokens: p.today_resp_cache_saved_tokens || 0,
        };
      });
      const rows = [];
      const seen = {};
      (this.balanceData || []).forEach(b => {
        if (!b.provider || String(b.provider).trim().length === 0) return; // 跳过空名供应商
        if (seen[b.provider]) return;
        seen[b.provider] = true;
        const c = costMap[b.provider] || {
          today_cost: 0, total_cost: 0,
          today_opt_saved_tokens: 0, today_strip_saved_tokens: 0,
          today_trim_saved_tokens: 0, today_resp_cache_saved_tokens: 0,
        };
        rows.push({
          provider: b.provider,
          balance: b.balance, currency: b.currency, source: b.source, error: b.error,
          consumed: b.consumed,
          expiring_at: b.expiring_at, expiring_amount: b.expiring_amount,
          // 本窗口额度进度 (仅积分制且上游给了窗口字段时才有值; 老接口没有 → undefined)
          quota_progress: b.quota_progress,
          account_label: b.account_label, account_count: b.account_count,
          total_balance: b.total_balance, total_consumed: b.total_consumed,
          // 状态与采集时刻 (2026-10-24): 必须透传 ——
          //   state 用于分色与文案 (未配置/失败/手动 三种处境处置完全不同);
          //   last_updated 用于显示"这个数是什么时候取的" (陈旧值不该伪装成实时)。
          // 旧实现在这里只 merge 了数值字段, 把这两项丢了 —— 前端于是无法区分
          // "没有余额"的四种不同处境, 也不知道数字有多旧。
          state: b.state || 'ok',
          last_updated: b.last_updated,
          hasBalance: b.balance !== null && b.balance !== undefined,
          today_cost: c.today_cost, total_cost: c.total_cost,
          today_opt_saved_tokens: c.today_opt_saved_tokens,
          today_strip_saved_tokens: c.today_strip_saved_tokens,
          today_trim_saved_tokens: c.today_trim_saved_tokens,
          today_resp_cache_saved_tokens: c.today_resp_cache_saved_tokens,
        });
      });
      return rows;
    },

    // ── 套餐额度行（2026-10-26）──
    //
    // # 为什么需要它
    //
    // 「余额与费用」面板原来只认**余额 API**（`balance_endpoint`）。
    // opencode 的 go/zen 是**套餐**制：没有余额接口，但 Console 有真实额度。
    // 于是它们被归进「不支持余额查询」，而用户明明刚在供应商页看到了额度 ——
    // 面板却在说"这家不支持"，这是自相矛盾。
    //
    // # 做法
    //
    // 把 `providerRows` 里 state==='unsupported' **但有套餐额度**的行挑出来，
    // 用额度渲染（每个工作区一行：进度条 + 已用/上限 + 重置时间）。
    // 没有套餐额度的仍走原来的"未配置"汇总，不伪造。
    //
    // # 为什么不在 providerRows 里就地改
    //
    // providerRows 还供 `balanceTakeaway`（"还能撑几天"）与
    // `balanceUnsupported`（汇总行）使用，那两处必须继续只看**余额**语义。
    // 混进去会让"还能撑几天"把套餐额度当成余额算天数 —— 量纲完全不同。
    ocPlanRows() {
      const all = this._ocQuotaAll || {};
      const out = [];
      for (const row of (this.providerRows || [])) {
        const v = all[row.provider];
        if (!v) continue;
        const wsList = (v.workspaces || []).filter(w => w.quota);
        if (!wsList.length) continue;
        for (const w of wsList) {
          // 取"最紧"的一档作为该工作区的代表百分比（与 ocQuotaAlerts 同口径）
          let worst = null;
          for (const win of (w.quota.windows || [])) {
            if (win.used_pct == null) continue;
            if (!worst || win.used_pct > worst.used_pct) worst = win;
          }
          out.push({
            provider: row.provider,
            workspace: w.workspace,
            name: w.name || '',
            pct: worst ? worst.used_pct : null,
            worstKey: worst ? worst.key : '',
            windows: w.quota.windows || [],
            ends_at: w.quota.ends_at,
            cancel_at_period_end: !!w.quota.cancel_at_period_end,
          });
        }
      }
      // 同一供应商的多个工作区排在一起；组内按已用百分比降序（最紧的在前）
      out.sort((a, b) => {
        if (a.provider !== b.provider) return a.provider.localeCompare(b.provider);
        return (b.pct || 0) - (a.pct || 0);
      });
      return out;
    },

    /// 该供应商是否**只有套餐额度**（没有余额接口）。
    /// 这类家不该出现在「不支持余额查询」汇总里 —— 它们有额度可看。
    provHasPlanOnly(prov) {
      const row = (this.providerRows || []).find(r => r.provider === prov);
      if (!row) return false;
      if (row.state !== 'unsupported') return false;
      const v = (this._ocQuotaAll || {})[prov];
      return !!(v && (v.workspaces || []).some(w => w.quota));
    },

    // ── 设置页分区导航 (P3) ──
    // 分区表即目录: 一处声明, 导航与锚点共用 (新增分区只加一行). 与抽屉的 dr-secnav 同手法.
    get settingsSections() {
      return [
        { k: 'perf', label: t('section.perf_opt') },
        { k: 'personal', label: t('section.personal') },
        { k: 'peak', label: t('section.peak') },
        { k: 'conn', label: t('section.conn') },
        { k: 'monitor', label: t('section.monitor') },
      ];
    },
    // 设置页分区切换（2026-10-24 改为左导航 + 右内容的双栏，对齐 WaLiAPI）。
    // 原来是"一页滚到底 + 顶部胶囊跳到锚点"：5 个分区 2623px / 2.5 屏，
    // 找一项设置要滚很久，且顶部胶囊只是滚动锚点、看不出"当前在哪个分区"。
    // 现在一次只显示一个分区的设置，左栏导航常驻并高亮当前分区。
    setSettingsSection(k) {
      // ⚠️ 必须校验（2026-10-24 实测修）。面板可见性由枚举值驱动时，
      // 非法值不是"无操作"而是**全隐藏** —— 设置页会只剩左导航、右侧一片空白。
      // 触发途径与分析页同源：旧书签、手改地址栏、外部拼接、将来的持久化。
      // 合法值直接以分区表为准（`settingsSections` 是唯一声明处，
      // 避免这里再维护第二份 key 列表而漂移）。
      const valid = this.settingsSections.map(s => s.k);
      this.settingsSection = valid.includes(k) ? k : valid[0];
      // 哈希深链（2026-10-24）：把分区也写进地址栏 → `#settings/peak` 可直接分享/刷新还原。
      // 只在设置页时才写，避免在别的页签改分区污染 hash。
      try {
        if (this.activeTab === 'settings') {
          const h = '#settings/' + k;
          if (location.hash !== h) history.replaceState(null, '', h);
        }
      } catch (e) {}
      // 切分区后回到内容顶部 —— 否则从长分区切到短分区会停在空白处
      try {
        const m = document.querySelector('main');
        if (m) m.scrollTop = 0;
      } catch (e) {}
    },
    scrollSettingsSec(k) {
      const el = document.getElementById('setsec-' + k);
      if (!el) return;
      // 用 main 容器的相对定位滚动, 而非 el.scrollIntoView —— 后者会把目标顶到
      // 容器最上沿, 正好被 sticky 的分区导航盖住 (实测目标停在视口下方 1341px 处).
      const main = el.closest('.main');
      const nav = document.querySelector('.st-nav');
      const off = (nav ? nav.offsetHeight : 0) + 12;
      if (main) {
        const top = main.scrollTop + el.getBoundingClientRect().top - main.getBoundingClientRect().top - off;
        main.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
      } else {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      // 焦点交给目标分区标题: 键盘用户 Tab 一次即可进入该分区控件.
      el.setAttribute('tabindex', '-1');
      el.focus({ preventScroll: true });
    },
    // ── 接入引导 (Get started): 完成态由真实数据推导, 3/3 自动收起为页头徽章 ──
    // 信号: ① 已配供应商 (providersFormData) ② 有已填上游 ID 的模型 ③ 发出过请求 (stats).
    get onboardSteps() {
      const provs = this.providersFormData || [];
      return [
        { k: 'provider', done: provs.length > 0 },
        { k: 'model', done: provs.some(p => (p.models || []).some(m => !m._removed && m.upstream_model)) },
        { k: 'request', done: !!(this.stats && this.stats.total_requests > 0) },
      ];
    },
    get onboardDone() { return this.onboardSteps.filter(s => s.done).length; },
    get onboardAllDone() { return this.onboardDone >= 3; },
    // 当前待办步 (未完成里的第一个) —— 供高亮; 全部完成时无意义, 返回 99 保证不高亮.
    get onboardFirstPending() { const i = this.onboardSteps.findIndex(s => !s.done); return i < 0 ? 99 : i; },
    onboardAction(k) {
      if (k === 'request') { this.copyFirstRequest(); return; }
      this.switchTab('providers');
    },
    // 第一次请求示例: 模型取第一个「已填上游 ID」的中转 ID; 无模型时用占位符 (复制时 toast 提醒).
    firstReqModel() {
      // 用户在下拉里选过的优先（2026-10-24：原来只能自动取第一个模型，
      // 代码块里的 model 改不了 —— 而用户真正要粘到自己项目里的往往是另一个）
      if (this.firstReqModelSel) return this.firstReqModelSel;
      const provs = this.providersFormData || [];
      for (const p of provs) for (const m of (p.models || [])) if (!m._removed && m.upstream_model) return m.model_id || '';
      return '';
    },
    // 可选模型清单（按供应商分组，与 WaLiAPI 的"按来源分组下拉"同一思路）
    firstReqModelGroups() {
      const out = [];
      for (const p of (this.providersFormData || [])) {
        const ms = (p.models || []).filter(m => !m._removed && (m.model_id || '')).map(m => m.model_id);
        if (ms.length) out.push({ provider: p.name, models: ms });
      }
      return out;
    },
    // 协议端点：三种协议各不同（WaLiAPI 的 usage 页正是这么分的）。
    // ⚠️ base 与 path 的分工要明确，否则拼接会重复或丢段：
    //   · chat / responses：base 含 /v1，path 只给 "/chat/completions" 这样的尾段
    //   · anthropic：SDK 的 base_url **不带 /v1**（Anthropic 客户端自己加），
    //     所以 base 去掉 /v1、path 给 "/v1/messages"
    // 第一版两者都带 /v1，拼出来是 ".../v1/v1/messages"（实测代码块里不含 /v1/messages
    // 正是因为拼接后变成了别的字符串）。
    firstReqEndpoint() {
      const root = (window.location.origin || 'http://127.0.0.1:8787');
      const p = this.firstReqProto || 'chat';
      if (p === 'anthropic') return { base: root, path: '/v1/messages', label: 'Anthropic Messages' };
      if (p === 'responses') return { base: root + '/v1', path: '/responses', label: 'OpenAI Responses' };
      return { base: root + '/v1', path: '/chat/completions', label: 'OpenAI Chat' };
    },
    setFirstReqProto(p) { this.firstReqProto = p; },
    setFirstReqModel(v) { this.firstReqModelSel = v; },
    firstRequestCode() {
      const ep = this.firstReqEndpoint();
      const base = ep.base;
      // 现在 base 与 path 的分工已明确（见 firstReqEndpoint 注释），直接拼即可 ——
      // 不要再对 path 做 replace，否则 anthropic 的 /v1/messages 会被削成 /messages。
      const endpoint = base + ep.path;
      const model = this.firstReqModel() || t('first_req_model_ph');
      const proto = this.firstReqProto || 'chat';
      if (this.firstReqTab === 'python') {
        if (proto === 'anthropic') {
          return 'import anthropic  # pip install anthropic\n'
            + '\n'
            + 'client = anthropic.Anthropic(base_url="' + base + '", api_key="local")\n'
            + 'msg = client.messages.create(\n'
            + '    model="' + model + '",\n'
            + '    max_tokens=256,\n'
            + '    messages=[{"role": "user", "content": "ping"}],\n'
            + ')\n'
            + 'print(msg.content)';
        }
        return 'from openai import OpenAI  # pip install openai\n'
          + '\n'
          + 'client = OpenAI(base_url="' + base + '", api_key="local")\n'
          + 'resp = client.chat.completions.create(\n'
          + '    model="' + model + '",\n'
          + '    messages=[{"role": "user", "content": "ping"}],\n'
          + ')\n'
          + 'print(resp.choices[0].message.content)';
      }
      if (this.firstReqTab === 'node') {
        return '(async () => {\n'
          + '  const res = await fetch("' + endpoint + '", {\n'
          + '    method: "POST",\n'
          + '    headers: { "Content-Type": "application/json" },\n'
          + '    body: JSON.stringify({\n'
          + '      model: "' + model + '",\n'
          + '      messages: [{ role: "user", content: "ping" }],\n'
          + '      stream: false,\n'
          + '    }),\n'
          + '  });\n'
          + '  console.log(await res.json());\n'
          + '})();';
      }
      if (proto === 'anthropic') {
        return 'curl ' + endpoint + ' \\\n'
          + '  -H "Content-Type: application/json" \\\n'
          + '  -H "x-api-key: local" \\\n'
          + '  -H "anthropic-version: 2023-06-01" \\\n'
          + "  -d '{\"model\":\"" + model + "\",\"max_tokens\":256,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}'";
      }
      return 'curl ' + endpoint + ' \\\n'
        + '  -H "Content-Type: application/json" \\\n'
        + "  -d '{\"model\":\"" + model + "\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":false}'";
    },
    copyFirstRequest() {
      if (!this.firstReqModel()) toast(t('first_req_no_model'), 'warn');
      this.copyText(this.firstRequestCode(), 'firstreq');
    },