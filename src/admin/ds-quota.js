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
    /// 逐工作区的额度（按供应商名）。每项形如
    /// `{workspace, name, quota|null, error|null}`。
    /// 与 `_ocQuota`（兼容用的"第一个成功的区"）分开存：
    /// 一个区失败不能影响别的区显示。
    _ocQuotaWs: {},
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
            // 2026-10-26：**全部**工作区都填进去（一个账号可有多套独立订阅）。
            // 原来只取 ids[0]，其他区的额度就永远看不到。
            prov.console_org_ids = ids.slice();
            prov.console_org_id = ids[0];
            this.markDirty();
            this.ocVerifyOk = true;
            this.ocVerifyMsg = ids.length > 1
              ? t('oc_org_fetched_multi', ids.length, ids[0])
              : t('oc_org_fetched', ids[0]);
            // 立刻落盘：额度查询读的是**已保存配置**（不是抽屉草稿）。
            // 不保存就会出现"面板上明明有两个区、查额度只回来一个"的错位 ——
            // 用户看到的是草稿，查到的是旧配置，非常难理解。
            this._ocAutosave(prov);
          } else {
            this.ocVerifyOk = false;
            this.ocVerifyMsg = t('oc_no_workspace');
          }
        }
      } catch (e) {
        this.ocVerifyOk = false;
        this.ocVerifyMsg = '获取失败: ' + String(e).slice(0, 100);
      } finally {
        this.ocFetchOrgBusy = false;
      }
    },

    /// 自动获取后立刻落盘（静默，不打断用户）。
    ///
    /// # 为什么必须落盘
    ///
    /// 额度查询（`/admin/api/opencode/quota`）读的是**后端注册表里已保存的配置**，
    /// 不是抽屉草稿。不保存就会出现：面板上明明列着两个工作区、查额度却只回来一个
    /// —— 用户看到的是草稿、查到的是旧配置，这个错位没法解释也没法自查。
    ///
    /// # 为什么静默而不是弹确认
    ///
    /// 「自动获取」本身就是用户主动发起的动作，紧接着落盘是它的自然结果
    /// （与「测试连接」不同：那个只验证、不该改盘）。失败时把原因写进
    /// 同一个提示位，用户能立刻看到。
    async _ocAutosave(prov) {
      if (typeof this.saveProvidersForm !== 'function') return;
      try {
        await this.saveProvidersForm();
        // 保存成功：把原来的"已获取 N 个工作区"补一句"已保存"
        this.ocVerifyMsg = this.ocVerifyMsg + ' · ' + t('oc_workspaces_saved');
      } catch (e) {
        // 保存失败不能吞掉 —— 否则用户以为已经生效了
        this.ocVerifyOk = false;
        this.ocVerifyMsg = t('oc_workspaces_save_failed', String(e).slice(0, 100));
      }
    },

    /// 「测试连接」：逐个工作区试查一次，把每个区的结果都说出来。
    ///
    /// 为什么要逐个：一个账号可能有多个工作区（各自独立的 Go 订阅），
    /// 只验第一个会让"另一个区其实是坏的"完全看不出来 —— 而用户
    /// 恰恰是因为某个区有问题才来点这个按钮的。
    async ocVerifyCred(prov) {
      if (!prov || this.ocVerifyBusy) return;
      const cookie = (prov.console_session || '').trim();
      const list = this.provWorkspaces(prov).filter(x => (x || '').trim());
      if (!list.length) {
        this.ocVerifyOk = false;
        this.ocVerifyMsg = t('oc_no_workspace');
        return;
      }
      this.ocVerifyBusy = true;
      this.ocVerifyMsg = '';
      try {
        const results = [];
        for (const org of list) {
          let one;
          try {
            const r = await fetch('/admin/api/opencode/verify', {
              method: 'POST',
              headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
              body: JSON.stringify({ cookie, org_id: org }),
            });
            one = await r.json();
          } catch (e) {
            one = { ok: false, error: '请求失败: ' + String(e).slice(0, 80) };
          }
          const q = one && one.quota;
          const win = q && (q.windows || []).find(w => w.key === 'month');
          const pct = (win && win.used_pct != null)
            ? win.used_pct.toFixed(1) + '%'
            : '—';
          results.push({ org, ok: !!(one && one.ok), pct, err: (one && one.error) || '' });
        }
        const okCount = results.filter(x => x.ok).length;
        this.ocVerifyOk = okCount === results.length;
        if (results.length === 1) {
          // 单区：沿用原来的简洁文案
          this.ocVerifyMsg = results[0].ok
            ? t('oc_verify_ok', results[0].pct)
            : (results[0].err || t('oc_verify_fail'));
        } else {
          // 多区：逐区列出，让"哪个区有问题"一眼可见
          const parts = results.map(x => x.ok ? (x.pct) : t('oc_verify_fail'));
          this.ocVerifyMsg = this.ocVerifyOk
            ? t('oc_verify_ok_multi', results.length, parts.join(' / '))
            : t('oc_verify_partial', okCount, results.length,
                results.filter(x => !x.ok).map(x => x.err).join('; '));
        }
      } finally {
        this.ocVerifyBusy = false;
      }
    },

    // ── 工作区列表编辑（2026-10-26）──
    //
    // 抽屉草稿里维护 `console_org_ids`（数组）。为了让**旧配置**（只有单值
    // `console_org_id`）也能正常显示与编辑，读取时统一走 provWorkspaces() ——
    // 它把两者合并（列表优先、单值兜底），与后端 ProviderConfig::console_workspaces
    // 的口径一致。写入时只写 console_org_ids，并同步单值 = 第一个
    // （让还不知道列表字段的地方仍看到一个正确的区）。

    /// 抽屉里当前的工作区列表（合并列表 + 单值，去重去空）。
    ///
    /// ⚠️ 保留**末尾的空串**：那是用户点了「+ 新增」还没填的空槽。
    /// 去掉它会让新增按钮"点了没反应"（空行立刻被过滤掉）。
    provWorkspaces(prov) {
      if (!prov) return [];
      const out = [];
      const push = (v) => {
        const t = String(v || '').trim();
        if (t && !out.includes(t)) out.push(t);
      };
      if (Array.isArray(prov.console_org_ids)) prov.console_org_ids.forEach(push);
      push(prov.console_org_id);
      // 空槽数：由 _wsBlank 记录（新增按钮 +1、保存时归零）
      const blanks = (prov.__wsBlank | 0);
      for (let i = 0; i < blanks; i++) out.push('');
      return out;
    },

    /// 把编辑结果写回（丢掉空串，只保留真实工作区）。
    ///
    /// 空槽数按"**末尾连续的空串**"重算 —— 用户新增的空行总在最后，
    /// 一旦填上就不再算空槽。注意不能把"中间的空串"算作空槽：
    /// 那多半是用户清空某一行的中间状态，重算会把它挪到末尾、光标乱跳。
    _commitWorkspaces(prov, list) {
      if (!prov) return;
      const clean = [];
      let blanks = 0;
      for (const v of list) {
        const t = String(v || '').trim();
        if (!t) continue;
        if (!clean.includes(t)) clean.push(t);
      }
      // 末尾连续空串数
      for (let i = list.length - 1; i >= 0; i--) {
        if (String(list[i] || '').trim()) break;
        blanks++;
      }
      prov.console_org_ids = clean;
      prov.console_org_id = clean[0] || '';
      prov.__wsBlank = Math.min(blanks, 1); // 最多保留 1 个空槽，避免点了多下累积一堆空行
      this.markDirty();
    },

    setProvWorkspace(idx, val) {
      const list = this.provWorkspaces(this.drawerProv);
      if (idx < 0 || idx >= list.length) return;
      list[idx] = val;
      this._commitWorkspaces(this.drawerProv, list);
    },

    addProvWorkspace() {
      if (!this.drawerProv) return;
      this.drawerProv.__wsBlank = (this.drawerProv.__wsBlank | 0) + 1;
      this.markDirty();
    },

    removeProvWorkspace(idx) {
      const list = this.provWorkspaces(this.drawerProv);
      if (idx < 0 || idx >= list.length) return;
      list.splice(idx, 1);
      this._commitWorkspaces(this.drawerProv, list);
    },

    clearProvWorkspaces() {
      if (!this.drawerProv) return;
      this.drawerProv.console_session = '';
      this.drawerProv.console_org_id = '';
      this.drawerProv.console_org_ids = [];
      this.drawerProv.__wsBlank = 0;
      this.markDirty();
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
    //
    // 2026-10-26：返回的是**全部工作区**（一个账号可以有多个独立的 Go 订阅）。
    // 逐个区的结果分开存，一个区失败不影响别的区显示。
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
          this._ocQuotaWs[name] = [];
        } else {
          this._ocQuotaWs[name] = (j && j.workspaces) || [];
          // 兼容字段：后端仍给 quota（= 第一个成功的区）
          this._ocQuota[name] = (j && j.quota) || null;
        }
      } catch (e) {
        this._ocQuotaErr[name] = '查询失败: ' + String(e).slice(0, 120);
        this._ocQuota[name] = null;
        this._ocQuotaWs[name] = [];
      } finally {
        this._ocQuotaLoading[name] = false;
        // Alpine 3 不追踪既有对象的深层属性新增 → 整体替换一次触发重渲染。
        this._ocQuota = Object.assign({}, this._ocQuota);
        this._ocQuotaWs = Object.assign({}, this._ocQuotaWs);
        this._ocQuotaErr = Object.assign({}, this._ocQuotaErr);
        this._ocQuotaLoading = Object.assign({}, this._ocQuotaLoading);
      }
    },

    ocQuota(name) { return this._ocQuota[name] || null; },
    /// 全部工作区的额度（含各自的 error）。空数组 = 没查过或整体失败。
    ocQuotaWorkspaces(name) { return this._ocQuotaWs[name] || []; },
    ocQuotaErr(name) { return this._ocQuotaErr[name] || ''; },
    ocQuotaLoading(name) { return !!this._ocQuotaLoading[name]; },

    /// 工作区显示名：优先上游给的 name，否则退化成 "默认工作区" / ID 尾段。
    ///
    /// 为什么不直接显示完整 ID：`wrk_01KS5PNNQH9TAEBFMNJ5C8AF07` 有 30 字符，
    /// 占满一行还看不出是哪个区。上游给了 `name`（Default / aichen）就用它，
    /// 没有才退回 ID —— 但**保留可辨识的尾段**而不是整串。
    ocWsLabel(ws) {
      if (!ws) return '';
      if (ws.name) return ws.name;
      const id = String(ws.workspace || '');
      // wrk_01KS... / org_01M4... → 去掉前 4 字符前缀，取尾 6 位
      const tail = id.length > 10 ? id.slice(-6) : id;
      return id ? ('…' + tail) : '';
    },

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
