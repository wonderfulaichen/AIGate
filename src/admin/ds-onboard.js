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
      this.settingsSection = k;
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
      const provs = this.providersFormData || [];
      for (const p of provs) for (const m of (p.models || [])) if (!m._removed && m.upstream_model) return m.model_id || '';
      return '';
    },
    firstRequestCode() {
      const base = (window.location.origin || 'http://127.0.0.1:8787') + '/v1';
      const endpoint = base + '/chat/completions';
      const model = this.firstReqModel() || t('first_req_model_ph');
      if (this.firstReqTab === 'python') {
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
      return 'curl ' + endpoint + ' \\\n'
        + '  -H "Content-Type: application/json" \\\n'
        + "  -d '{\"model\":\"" + model + "\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":false}'";
    },
    copyFirstRequest() {
      if (!this.firstReqModel()) toast(t('first_req_no_model'), 'warn');
      this.copyText(this.firstRequestCode(), 'firstreq');
    },