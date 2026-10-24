// ── 请求竞态守卫（2026-10-24）──
// 主要成员: guardedFetch / _abortPrev
// (本文件是 dashboard() 对象体的一段, 由 admin.rs 的 concat! 按序拼接; 详见 docs/frontend.md)
//
// 为什么需要这个: 3 秒轮询里的 fetchEvents / fetchHeatmap / fetchDegradations
// 此前**完全没有守卫** —— 慢响应与下一拍的新请求会交错回来，旧数据覆盖新数据，
// 表现为"数字偶尔往回跳一下"。而 ds-stats.js / ds-core.js 里那两个高频接口
// 各自手写了一遍 AbortController，规则相同但代码重复。
//
// 这里把规则收成一处：**同一个 key 的请求只认最后一次**。
//   · 新请求发出前 abort 旧的（省带宽，也让旧请求尽早结束）；
//   · 被 abort 的那次**静默返回 null**（既不算失败、也不能写状态）；
//   · 返回值 null 表示"本次结果不应被采用"，调用方直接 return。
//
// ⚠️ 为什么不用自增序号而用 AbortController: 序号只能丢弃旧响应，但请求仍会跑完、
// 仍会占连接；abort 能真的取消。ds-provform.js 用序号是因为那里要跨多个 async 步骤
// 判断"整段流程是否过期"，不只是单个请求 —— 两种场景各用各的，不是重复造轮子。

    // 取某个 key 的「当前在途请求」，供下方 abort 用
    _guardedAbort(key) {
      const m = this._reqCtl || (this._reqCtl = {});
      const cur = m[key];
      if (cur) { try { cur.abort(); } catch (e) {} m[key] = null; }
    },

    /// 带竞态守卫的 fetch。返回解析后的 JSON；**被取代/失败返回 null**。
    /// @param key  用于区分不同接口的并发槽（如 'events' / 'heatmap'）
    async guardedFetch(key, url, opts) {
      const m = this._reqCtl || (this._reqCtl = {});
      this._guardedAbort(key);
      const ac = new AbortController();
      m[key] = ac;
      try {
        const r = await fetch(url, Object.assign({}, opts || {}, { signal: ac.signal }));
        if (!r.ok) return null;
        const data = await r.json();
        // 走到这里说明没被 abort；但仍要确认自己是当前那个槽
        // （理论上 abort 会直接抛，这里再判一次只花一次比较，能挡住极端时序）
        if (m[key] !== ac) return null;
        return data;
      } catch (e) {
        // AbortError = 被后来的请求取代：静默，不写状态
        if (e && e.name === 'AbortError') return null;
        return null;
      } finally {
        if (m[key] === ac) m[key] = null;
      }
    },
