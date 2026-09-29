// p1-ime-search 行为测试: 从 ds-records.js 抽真实方法体, 挂假组件验证 IME 语义.
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'src', 'admin', 'ds-records.js'), 'utf8');

// 对象体片段 → 可取方法表 (与 verify_parts 同款 harness); 注入 t() 全局 (播报文案用)
const methods = new Function('t', 'return {' + src + '}')((k, v) => '共 ' + v + ' 条');
for (const k of ['onLogSearchInput', 'onLogSearchCompositionStart', 'onLogSearchCompositionUpdate',
                 'onLogSearchCompositionEnd', 'clearLogSearch', 'resetLogView']) {
  if (typeof methods[k] !== 'function') { console.log('FAIL: missing ' + k); process.exit(1); }
}

function makeComp() {
  const c = {
    logSearch: '', logSearchDraft: '', logSearchComposing: false, _logSearchT: null,
    logPage: 5,
    // _announceRecords 依赖 (播报功能): 桩给静态值, 本测试不校验播报内容
    filteredLogs: [{}, {}, {}], totalLogPages: 2, liveMsg: '',
    logs: [],
  };
  // 绑定真实方法: 按属性描述符取, 跳过 getter (取值会执行 getter 且 this 是裸对象)
  const descs = Object.getOwnPropertyDescriptors(methods);
  for (const k of Object.keys(descs)) {
    const d = descs[k];
    if (!d.get && typeof d.value === 'function') c[k] = d.value.bind(c);
  }
  return c;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fail = 0;
const check = (name, cond) => { console.log((cond ? 'OK   ' : 'FAIL ') + name); if (!cond) fail = 1; };

(async () => {
  // 1) 拼音输入中 (composing): 草稿更新但不过滤, 防抖不提交
  let c = makeComp();
  c.onLogSearchCompositionStart();
  const e1 = { target: { value: 'shen' } };
  c.logSearchDraft = 'shen'; // 模拟 x-model 已更新
  c.onLogSearchInput(e1);
  await sleep(320);
  check('composing: logSearch 保持空 (未上屏不过滤)', c.logSearch === '');
  check('composing: 页码不被重置 (过滤未发生)', c.logPage === 5);

  // 2) 上屏 (compositionend): 立即提交
  c.logSearchDraft = '深度';
  c.onLogSearchCompositionEnd();
  check('compositionend: 立即提交草稿', c.logSearch === '深度');
  check('compositionend: 重置到第 1 页', c.logPage === 0);

  // 3) 普通输入 (非 composing): 防抖 200ms 后提交, 期间不提交
  c = makeComp();
  c.onLogSearchInput({ target: { value: 'dee' } });
  check('普通输入: 防抖窗口内未立即提交', c.logSearch === '');
  await sleep(320);
  check('普通输入: 防抖到期后提交', c.logSearch === 'dee');
  check('普通输入: 提交时重置页码', c.logPage === 0);

  // 4) clearLogSearch: 双值清零 + 取消挂起的防抖 (提交不得在清空后发生)
  c = makeComp();
  c.onLogSearchInput({ target: { value: 'abc' } }); // 挂起 200ms
  c.clearLogSearch();                                // 立即清空 (含取消计时器)
  await sleep(320);
  check('clearLogSearch: 草稿为空', c.logSearchDraft === '');
  check('clearLogSearch: 挂起的提交被取消 (logSearch 仍为空)', c.logSearch === '');

  // 5) 连续快速输入: 只提交最后一帧
  c = makeComp();
  c.onLogSearchInput({ target: { value: 'a' } });
  await sleep(120);
  c.onLogSearchInput({ target: { value: 'ab' } });
  await sleep(120);
  c.onLogSearchInput({ target: { value: 'abc' } });
  await sleep(320);
  check('防抖合并: 最终只提交最后一帧', c.logSearch === 'abc');

  process.exit(fail);
})();
