document.addEventListener('alpine:init', function() {
  Alpine.store('i18n', { lang: (window.AIGATE_LANG || 'zh-CN') });
  // 全局轻量 Toast 通知：toast(msg, type, key) / Alpine.store('toast').push(msg, type, key)
  // 传 key 时, 相同 key 的 toast 在 3s 窗口内合并计数 (×n), 防止重复刷屏.
  Alpine.store('toast', {
    items: [],
    push(msg, type, key) {
      type = type || 'info';
      if (key) {
        const ex = this.items.find(x => x.key === key);
        if (ex) {
          ex.msg = msg; ex.count = (ex.count || 1) + 1;
          if (ex._t) clearTimeout(ex._t);
          ex._t = setTimeout(() => this.dismiss(ex.id), 3000);
          return;
        }
      }
      const id = Date.now() + Math.random();
      const item = { id, msg, type, key: key || null, count: 1 };
      if (key) item._t = setTimeout(() => this.dismiss(id), 3000);
      else setTimeout(() => this.dismiss(id), 3000);
      this.items.push(item);
    },
    dismiss(id) { this.items = this.items.filter(x => x.id !== id); },
  });
  // 统一确认弹窗 (红按钮 + 后果文案 + loading): confirmAsk(body, {title, danger, onOk}) -> Promise<bool>
  // 不传 onOk: 点确认立即 resolve(true); 传 onOk: 弹窗转圈执行完才关闭, 抛错 resolve(false).
  Alpine.store('cf', {
    show: false, title: '', body: '', danger: true, loading: false,
    _res: null, _onOk: null,
    ask(title, body, danger) {
      this.title = title; this.body = body; this.danger = danger !== false;
      this.loading = false; this.show = true;
      const self = this;
      return new Promise(res => { self._res = res; });
    },
    settle(v) {
      if (!this.show) return;
      this.show = false; this.loading = false; this._onOk = null;
      const r = this._res; this._res = null; if (r) r(v);
    },
    confirmNow() {
      const fn = this._onOk;
      if (!fn) { this.settle(true); return; }
      const self = this;
      self.loading = true;
      Promise.resolve().then(fn).then(() => self.settle(true), () => self.settle(false));
    },
  });
  Alpine.data('dashboard', dashboard);
});
