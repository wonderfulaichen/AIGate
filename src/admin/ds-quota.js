    // ── opencode 套餐额度（2026-10-24，走 Console API）──
    //
    // ## 数据来源（重要，曾被搞错一次）
    //
    // 我最初断言「opencode 没有额度查询接口，只能自己累加 `usage.opencode.allowance`」。
    // **那是错的**，已实测推翻：
    //   inference 端点确实没有，但 **Console 网页 API** 有：
    //   `GET https://opencode.ai/console/api/go/status`
    //   （需要 Cookie `__Host-console_session` + 头 `x-org-id: wrk_...`）
    // 实测返回 HTTP 200，含三个窗口的 limitMicroCents / usedMicroCents / resetsAt。
    //
    // 所以现在的额度是**官方真实值**，不是估算：
    //   · 不需要累加、不需要维护「模型→月额度」表（那表会过期）；
    //   · 能看到**在别的客户端**用掉的额度（累加方案永远看不到）。
    //
    // 累加方案已删除 —— 它算不准，留着只会让用户看到两个不同的数。

    // 额度查询结果缓存（按供应商名）。`null` = 还没查过或查询失败。
    // 为什么放前端而不是复用 stats：额度是一次**外部网络请求**，
    // 不该拖慢每次统计刷新，也不该因 Console 挂了就让统计页出错。
    _ocQuota: {},
    _ocQuotaLoading: {},
    _ocQuotaErr: {},

    // 查一次额度（用户点「查询额度」或展开时触发）。
    async fetchOpencodeQuota(name) {
      if (this._ocQuotaLoading[name]) return;
      this._ocQuotaLoading[name] = true;
      this._ocQuotaErr[name] = '';
      try {
        const r = await fetch('/admin/api/opencode/quota?provider=' + encodeURIComponent(name),
                              { headers: authHeaders() });
        const j = await r.json();
        if (j && j.error) {
          this._ocQuotaErr[name] = String(j.error);
          this._ocQuota[name] = null;
        } else {
          this._ocQuota[name] = (j && j.quota) || null;
        }
      } catch (e) {
        this._ocQuotaErr[name] = '查询失败: ' + String(e).slice(0, 120);
        this._ocQuota[name] = null;
      } finally {
        this._ocQuotaLoading[name] = false;
        // Alpine 3 不追踪既有对象的深层属性新增 → 整体替换一次触发重渲染。
        this._ocQuota = Object.assign({}, this._ocQuota);
        this._ocQuotaErr = Object.assign({}, this._ocQuotaErr);
        this._ocQuotaLoading = Object.assign({}, this._ocQuotaLoading);
      }
    },

    ocQuota(name) { return this._ocQuota[name] || null; },
    ocQuotaErr(name) { return this._ocQuotaErr[name] || ''; },
    ocQuotaLoading(name) { return !!this._ocQuotaLoading[name]; },

    // 该供应商**是否可能支持**额度查询。
    // 判据：配了 Console 凭据，或名字里有 opencode（zen/go）。
    // 为什么不用"是不是 opencode"写死：用户可能给任意供应商配 Console 凭据
    // （比如把 zen 也接进去），硬编码会把这种用法挡掉。
    provMayHaveQuota(prov) {
      if (!prov) return false;
      if (prov.console_session && prov.console_org_id) return true;
      const n = String(prov.name || '').toLowerCase();
      return n === 'go' || n === 'zen' || n.includes('opencode');
    },

    // 窗口 → 中文标签。key 是 Console 的字段名（fiveHour/week/month）。
    ocWindowLabel(key) {
      const m = { fiveHour: 'quota_5h', week: 'quota_week', month: 'quota_month' };
      return t(m[key] || key);
    },

    // 已用百分比 → 色调。**超额（>100%）用红色**，不 clamp。
    // 阈值语义与项目既有习惯一致：>80% 提醒、>100% 告警。
    ocTone(pct) {
      if (pct == null) return 'var(--muted)';
      if (pct >= 100) return 'var(--err)';
      if (pct >= 80) return 'var(--warn)';
      return 'var(--ok)';
    },

    // 百分比文案。**未采集显示 "—"**，不显示 0%。
    //
    // ⚠️ 不能用 `this.fmtPct()`：它给"缓存命中率"设计，第 294 行
    // `if (v >= 100) return '100%'` 会把 112.5% 压成 "100%" ——
    // 而额度超额是"该充值/该报障"的真实信号，压掉会误导。
    // （这条已被探针钉住：超额必须如实显示。）
    //
    // 注意 `used_pct` 是**后端字段**（`QuotaWindow.used_pct`），不是前端算的 ——
    // 早先我把它写成后端方法，导致 serde 不下发、前端全显示 "—"（已修，有单测钉住）。
    ocPctText(pct) {
      if (pct == null) return '—';
      if (pct <= 0) return '0%';
      if (pct < 0.1) return '<0.1%';
      return pct.toFixed(1) + '%';
    },

    // 金额文案：小额给 4 位（$0.00412 这种），大额给 2 位。
    ocUsd(v) {
      if (v == null) return '—';
      const n = Number(v) || 0;
      return n < 1 ? '$' + n.toFixed(4) : '$' + n.toFixed(2);
    },

    // 重置时间：ISO8601 → 本地可读 + "还有多久"。
    // 为什么要显示：额度是滚动窗口，用户最想知道"什么时候恢复"。
    ocResetText(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      if (!Number.isFinite(d.getTime())) return '';
      const now = Date.now();
      const left = d.getTime() - now;
      const fmt = d.toLocaleString();
      if (left <= 0) return t('quota_reset_soon');
      const h = Math.floor(left / 3600000);
      const leftTxt = h >= 24
        ? t('quota_left_days', Math.floor(h / 24))
        : t('quota_left_hours', Math.max(1, h));
      return fmt + ' · ' + leftTxt;
    },
