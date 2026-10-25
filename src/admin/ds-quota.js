    // ── opencode 套餐额度（2026-10-24，走 Console API）──
    //
    // 数据来源：`GET https://opencode.ai/console/api/go/status`
    // 需要 Cookie `__Host-console_session` + 头 `x-org-id: wrk_...`。
    // 实测返回三档 limitMicroCents / usedMicroCents / resetsAt，是**官方真实值**。
    //
    // ⚠️ 为什么不走"服务端换 token"（像 CodeBuddy 那样）：
    //   · opencode 是标准 OAuth(PKCE)，授权码只发给它自己的回调域名，
    //     本地服务端拿不到 code；
    //   · session 是 HttpOnly Cookie，JS 的 document.cookie 读不到；
    //   · 额度端点不认 API key（Bearer 返回 401，与无凭据相同）。
    //
    // 所以「一键登录」不是服务端换 token，而是**弹一个 WebView 让用户自己
    // 走完整条 OAuth**，后端从 WebView2 的 CookieManager 取 HttpOnly Cookie。
    // 见 `ocLoginStart()` 与后端 `src/oc_login.rs`。
    // 弹窗不可用（缺 WebView2 运行时等）时，手动粘贴仍然保留作为兜底。

    // 额度查询结果缓存（按供应商名）。`null` = 还没查过或查询失败。
    // 为什么放前端而不是复用 stats：额度是一次**外部网络请求**，
    // 不该拖慢每次统计刷新，也不该因 Console 挂了就让统计页出错。
    // ── 实际请求 URL 预览（2026-10-24）──
    //
    // 为什么值得做：90% 的渠道配错是 Base URL 多写/少写 `/v1`
    // （参考项目把这叫"最便宜的防错"，见 HANDOFF ㉚ 对照清单第 5 项）。
    // 用户填完看不到最终会打哪个地址，只能靠试。
    //
    // 口径必须与后端一致：后端是 `endpoint` 原样使用（chat/completions 已含），
    // 另两个协议端点走 `endpoint_anthropic` / `endpoint_responses`，
    // 未填时由后端按 `endpoint` 推导。这里**只展示实际会用的值**，
    // 推导不出来就显示"未配置"，不猜。
    provEndpointPreview(prov) {
      if (!prov) return [];
      const out = [];
      const ep = (prov.endpoint || '').trim();
      if (ep) out.push({ k: 'fmt.openai', v: ep });
      const anth = (prov.endpoint_anthropic || '').trim();
      const resp = (prov.endpoint_responses || '').trim();
      if (anth) out.push({ k: 'fmt.anthropic', v: anth });
      if (resp) out.push({ k: 'fmt.responses', v: resp });
      // 一个都没填 → 返回空数组，界面据此不渲染（不显示"未配置"占位噪音）
      return out;
    },

    _ocQuota: {},
    _ocQuotaLoading: {},
    _ocQuotaErr: {},

    // 从额度错误提示「去填写凭据」→ 打开该供应商抽屉并滚到凭据区。
    //
    // 为什么不只给一句"请去编辑里填"：用户看到"额度查不到"时，
    // 下一步动作应当**一步可达**。原来凭据藏在「编辑 → 高级设置 → 折叠」四层，
    // 用户批评过"谁会把登录藏得这么深"。现在它在 API Key 分区（一级可见），
    // 这个按钮负责把人送到那里。
    openProviderCredDrawer(name) {
      const list = this.providersFormData || [];
      const pi = list.findIndex(p => p.name === name);
      if (pi < 0) return;
      this.openProviderDrawer(pi);
      // 滚到凭据块（等抽屉渲染完）
      setTimeout(() => {
        try {
          const keySec = document.getElementById('drsec-key');
          if (keySec) keySec.scrollIntoView({ block: 'center', behavior: 'smooth' });
        } catch (e) {}
      }, 250);
    },

    // ── 凭据录入的两个辅助动作（2026-10-24，照 QuotaHub 的"少犯错"思路）──
    //
    // 为什么要它们：实测该账号下有**两个**组织（`wrk_...` 与 `org_...`），
    // 只有 `wrk_` 能查 Go 额度；而 Cookie 是一长串 HttpOnly 值。
    // 让用户手抄两个东西必然出错，于是：
    //   ① 「自动获取」组织 ID —— 用 Cookie 问上游（/console/api/orgs）
    //   ② 「测试连接」—— 用草稿凭据直接试查一次，粘完立刻知道成不成
    //      （否则要先保存、再点查询、再回来看错误，走三步才知道填错）
    ocFetchOrgBusy: false,
    ocVerifyBusy: false,
    ocVerifyMsg: '',
    ocVerifyOk: false,

    async ocFetchOrgId(prov) {
      if (!prov || this.ocFetchOrgBusy) return;
      const cookie = (prov.console_session || '').trim();
      if (!cookie) {
        this.ocVerifyOk = false;
        this.ocVerifyMsg = t('oc_need_cookie');
        return;
      }
      this.ocFetchOrgBusy = true;
      try {
        const r = await fetch('/admin/api/opencode/orgs', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
          body: JSON.stringify({ cookie }),
        });
        const j = await r.json();
        if (j && j.error) {
          this.ocVerifyOk = false;
          this.ocVerifyMsg = String(j.error);
        } else {
          const ids = (j && j.orgs) || [];
          if (ids.length) {
            prov.console_org_id = ids[0];
            this.markDirty();
            this.ocVerifyOk = true;
            this.ocVerifyMsg = ids.length > 1
              ? t('oc_org_fetched_multi', ids.length, ids[0])
              : t('oc_org_fetched', ids[0]);
          }
        }
      } catch (e) {
        this.ocVerifyOk = false;
        this.ocVerifyMsg = '获取失败: ' + String(e).slice(0, 100);
      } finally {
        this.ocFetchOrgBusy = false;
      }
    },

    async ocVerifyCred(prov) {
      if (!prov || this.ocVerifyBusy) return;
      this.ocVerifyBusy = true;
      this.ocVerifyMsg = '';
      try {
        const r = await fetch('/admin/api/opencode/verify', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
          body: JSON.stringify({
            cookie: (prov.console_session || '').trim(),
            org_id: (prov.console_org_id || '').trim(),
          }),
        });
        const j = await r.json();
        if (j && j.ok) {
          this.ocVerifyOk = true;
          const q = j.quota || {};
          const win = (q.windows || []).find(w => w.key === 'month') || {};
          // 成功时把关键数字说出来：用户一眼知道"通了、本月用了多少"
          const pct = (win.used_pct == null) ? '—' : win.used_pct.toFixed(1) + '%';
          this.ocVerifyMsg = t('oc_verify_ok', pct);
        } else {
          this.ocVerifyOk = false;
          this.ocVerifyMsg = (j && j.error) || t('oc_verify_fail');
        }
      } catch (e) {
        this.ocVerifyOk = false;
        this.ocVerifyMsg = '测试失败: ' + String(e).slice(0, 100);
      } finally {
        this.ocVerifyBusy = false;
      }
    },

    // ── 一键登录（2026-10-24，WebView2 弹窗）──
    //
    // 后端另起线程弹一个 Win32 + WebView2 原生窗口（不是 wry，原因见
    // src/oc_login_win.rs 顶部），里面就是 opencode 登录页，用户完成
    // Google/GitHub 授权；后端轮询 WebView2 的 CookieManager 取
    // `__Host-console_session`（HttpOnly，JS 读不到），并从跳转 URL 提取
    // `wrk_...` 组织 ID。拿到后**直接写进该供应商配置并落盘**，
    // 所以这里拿到 done 就立刻查额度（凭据不回传前端，故不回填输入框）。
    //
    // 轮询而不是长连接：一次登录可能拖几十秒（用户要输密码/2FA），
    // 长连接在中间任何一次网络抖动上都要重连，轮询更稳。
    ocLoginBusy: false,
    ocLoginMsg: '',
    ocLoginOk: false,

    async ocLoginStart(prov) {
      if (!prov || this.ocLoginBusy) return;
      this.ocLoginBusy = true;
      this.ocLoginOk = false;
      this.ocLoginMsg = t('oc_login_waiting');
      const name = prov.name;
      try {
        const r = await fetch('/admin/api/opencode/login/start', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
          body: JSON.stringify({ provider: name }),
        });
        const j = await r.json();
        if (j && j.error) {
          this.ocLoginOk = false;
          this.ocLoginMsg = String(j.error);
          return;
        }
        // 轮询：最多 5 分钟（用户可能去输密码 / 过 2FA / 切账号）
        const deadline = Date.now() + 5 * 60 * 1000;
        while (Date.now() < deadline) {
          await new Promise(res => setTimeout(res, 1000));
          const sr = await fetch('/admin/api/opencode/login/status', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            body: JSON.stringify({ provider: name }),
          });
          const sj = await sr.json();
          const stage = (sj && sj.stage) || '';
          if (stage === 'done') {
            this.ocLoginOk = true;
            this.ocLoginMsg = t('oc_login_ok');
            // 后端已落盘。这里**不回填 Cookie**（后端不回传凭据，凭据不该经过前端），
            // 也不整份 reload —— `loadProvidersForm()` 会重建整个数组、让
            // `drawerProv` 变成悬空引用（抽屉引用的还是旧对象）。
            // 只做两件事：把组织 ID 显示出来（用户能核对），标记该条已有凭据。
            if (sj.org_id) prov.console_org_id = sj.org_id;
            this.ocVerifyOk = true;
            this.ocVerifyMsg = '';
            this.fetchOpencodeQuota(name);
            return;
          }
          if (stage === 'failed') {
            this.ocLoginOk = false;
            // 弹窗起不来（缺 WebView2 运行时）与"授权失败"是两回事，分开说
            const err = String((sj && sj.error) || '');
            this.ocLoginMsg = err || t('oc_login_failed');
            return;
          }
          if (stage === 'cancelled') {
            this.ocLoginOk = false;
            this.ocLoginMsg = t('oc_login_cancelled');
            return;
          }
        }
        this.ocLoginOk = false;
        this.ocLoginMsg = t('oc_login_timeout');
      } catch (e) {
        this.ocLoginOk = false;
        this.ocLoginMsg = '登录失败: ' + String(e).slice(0, 100);
      } finally {
        this.ocLoginBusy = false;
      }
    },

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
