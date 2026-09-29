// 供应商表单加载态回归测试: 复现"骨架永久常驻"的并发交错, 并验证修法.
// 用假 setTimeout/clearTimeout 精确控制 250ms 宽限期与 1s 驻留的时序.
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
// 允许通过 PROVFORM_SRC 指定被测文件 (用于证明测试能钉住旧写法: 置为该变体应失败)
const TARGET = process.env.PROVFORM_SRC || path.join(ROOT, 'src', 'admin', 'ds-provform.js');
const src = fs.readFileSync(TARGET, 'utf8');
console.log('# 被测文件: ' + TARGET);

// 从对象体取出 loadProvidersForm (harness 与 verify_parts 的 obj-body 一致).
// ⚠️ 方法体里调用的是**全局函数** authHeaders() 而非 this.authHeaders() ——
//    它不是组件方法. 故必须用 with 把全局符号注入作用域, 挂在 this 上无效
//    (踩过: 缺它会被方法内的 try/catch 静默吞掉, 表现为"数据没载入"却无报错).
const GLOBALS = {
  authHeaders: () => ({ 'Content-Type': 'application/json' }),
  t: (k) => k,
  toast: () => {},
  confirmAsk: () => Promise.resolve(true),
  copyBadge: () => Promise.resolve(true),
  copyToClipboard: () => Promise.resolve(true),
  savePageSize: () => {},
  fmtT: (v) => String(v),
  fmtMoney: (v) => String(v),
  fmtMs: (v) => String(v),
  escapeHtml: (s) => String(s),
};
function evalBody(src) {
  const fn = new Function('G', 'with (G) { return {' + src + '}; }');
  return fn(GLOBALS);
}
const load = evalBody(src).loadProvidersForm;
if (typeof load !== 'function') { console.log('FAIL: loadProvidersForm missing'); process.exit(1); }

// ---- 假计时器 ----
function FakeClock() {
  this.now = 0;
  this.seq = 0;
  this.timers = new Map();
  this.setTimeout = (fn, ms) => {
    const id = ++this.seq;
    this.timers.set(id, { at: this.now + (ms || 0), fn });
    return id;
  };
  this.clearTimeout = (id) => { this.timers.delete(id); };
  this.tick = (t) => {
    this.now = t;
    let guard = 0;
    for (;;) {
      const due = [...this.timers.entries()].filter(([, v]) => v.at <= this.now)
        .sort((a, b) => a[1].at - b[1].at);
      if (!due.length || ++guard > 100) break;
      const [id, v] = due[0];
      this.timers.delete(id);
      v.fn();
    }
  };
}

function makeComp() {
  const c = {
    providersFormData: null,
    provLoading: false,
    _provSkelAt: 0,
    _provLoadSeq: 0,
    isDirty: false,
    // loadProvidersForm 的依赖: 缺任何一个都会被它的 try/catch 静默吞掉,
    // 表现为"数据没载入"却无报错 —— 测试桩必须补齐.
    authHeaders() { return { 'Content-Type': 'application/json' }; },
    t(k) { return k; },
    fetchModelMeta() {},
    autoFree() { return false; },
  };
  c.loadProvidersForm = load.bind(c);   // 挂真实方法 (与 Alpine 的 this 绑定一致)
  return c;
}

// fetch 桩: 手动控制何时 resolve
function installFetch() {
  const plan = [];
  global.fetch = () => new Promise((res) => plan.push(() => res({
    ok: true,
    json: async () => ({ parsed: { providers: [{ name: 'p1', endpoint: 'http://x', models: {} }] } }),
  })));
  // resolveAt 后必须让出微任务队列 (真实 fetch 的 then 链需要若干 tick 才走完)
  return {
    plan,
    resolveAt: async (i) => {
      if (!plan[i]) return;
      plan[i]();
      for (let k = 0; k < 12; k++) await Promise.resolve();
    },
  };
}

let fail = 0;
const check = (name, cond, extra) => {
  console.log((cond ? 'OK   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || '')));
  if (!cond) fail = 1;
};

(async () => {
  // ===== 场景 1: 快载 (宽限期内完成) → 骨架从不显示 =====
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const p = c.loadProvidersForm();
    clock.tick(50);
    await f.resolveAt(0);
    await p;
    clock.tick(2000);
    check('快载: provLoading 始终 false', c.provLoading === false, 'got=' + c.provLoading);
    check('快载: 数据已载入', Array.isArray(c.providersFormData) && c.providersFormData.length === 1);
  }

  // ===== 场景 2: 慢载 (超宽限期) → 骨架显示且最少驻留 1s =====
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const p = c.loadProvidersForm();
    clock.tick(300);
    check('慢载: 300ms 时骨架亮', c.provLoading === true);
    clock.tick(400);
    await f.resolveAt(0);
    await p;
    // 骨架于 250ms 显示 → 最少驻留到 1250ms; 数据在 400ms 到达 → remain = 1000-(400-250) = 850ms
    clock.tick(1100);   // 距显示 850ms, 仍应驻留
    check('慢载: 距显示不足 1s 仍驻留 (1100ms)', c.provLoading === true, 'got=' + c.provLoading);
    clock.tick(1400);   // 越过 1250ms
    check('慢载: 满 1s 后收口', c.provLoading === false, 'got=' + c.provLoading);
  }

  // ===== 场景 3: 并发交错 (旧写法永久常驻的时序) =====
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const pA = c.loadProvidersForm();   // A 起计时器
    const pB = c.loadProvidersForm();   // B 也进 firstLoad 分支 (A 未赋值)
    check('并发: 两次调用都被接受 (序号=2)', c._provLoadSeq === 2, 'seq=' + c._provLoadSeq);
    await f.resolveAt(0);
    await pA;                            // A 的 finally: 非最新 → 不动状态
    clock.tick(260);                     // B 的宽限期到期 → 骨架亮
    check('并发: B 宽限期到期后骨架亮', c.provLoading === true, 'got=' + c.provLoading);
    await f.resolveAt(1);
    await pB;
    clock.tick(2000);
    check('并发: 最终必定收口 (不永久常驻)', c.provLoading === false, 'got=' + c.provLoading);
    check('并发: 数据已载入', !!c.providersFormData);
  }

  // ===== 场景 4: 串行调用, 第二次非首载不置骨架 =====
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const pA = c.loadProvidersForm();
    await f.resolveAt(0);
    await pA;
    check('串行: A 完成后 provLoading=false', c.provLoading === false);
    const pB = c.loadProvidersForm();
    await f.resolveAt(1);
    await pB;
    clock.tick(2000);
    check('串行: 非首载不再置骨架', c.provLoading === false, 'got=' + c.provLoading);
  }

  // ===== 场景 5: 宽限期内完成 —— 计时器必须被清, 推进很久也不亮 =====
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const p = c.loadProvidersForm();
    await f.resolveAt(0);
    await p;
    clock.tick(5000);
    check('宽限期内完成: 计时器已清, 骨架不亮', c.provLoading === false, 'got=' + c.provLoading);
  }

  // ===== 场景 6: 旧请求的计时器不得接管状态 (核心回归) =====
  // A 先返回并被 B 取代后, A 的计时器即使仍活着也不得置 provLoading.
  {
    const clock = new FakeClock();
    global.setTimeout = clock.setTimeout; global.clearTimeout = clock.clearTimeout;
    const c = makeComp(); const f = installFetch();
    const pA = c.loadProvidersForm();
    const pB = c.loadProvidersForm();
    f.resolveAt(1);                       // 先让 B 完成 (B 是最新)
    await pB;
    check('B 完成后 provLoading=false', c.provLoading === false, 'got=' + c.provLoading);
    clock.tick(2000);                     // A 的计时器到期
    check('被取代的 A 的计时器不得点亮骨架', c.provLoading === false, 'got=' + c.provLoading);
    await f.resolveAt(0);
    await pA;
    clock.tick(2000);
    check('A 收尾后状态仍正确', c.provLoading === false, 'got=' + c.provLoading);
  }

  console.log(fail ? 'FAILED' : 'all provider-loading checks passed');
  process.exit(fail);
})();
