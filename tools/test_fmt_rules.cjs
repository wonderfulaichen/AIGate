// 前后端协议推断规则一致性测试.
// 背景: 两处规则曾漂移 (前端简化版无供应商条件、缺网关规则), 导致 ① 建模型时写入错误协议
// ② 面板显示与后端实际转发不符 (实测 11/650)。此测试逐例比对, 防止再度漂移。
//
// 做法: 从 Rust 源读 default_api_format 的判定分支表 + 从 JS 读真实函数, 跑同一组用例比对。
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');

// ── 前端: 抽真实函数 (harness 同 verify_parts 的 obj-body) ──
const jsSrc = fs.readFileSync(path.join(ROOT, 'src', 'admin', 'ds-brands.js'), 'utf8');
const G = {
  t: (k) => k, toast: () => {}, copyText: () => {}, fetch: () => Promise.resolve({}),
  authHeaders: () => ({}), errToastMsg: () => Promise.resolve(''), copyBadge: () => {},
  confirmAsk: () => Promise.resolve(true), copyToClipboard: () => Promise.resolve(true),
  PROVIDER_SVG: {}, PROVIDER_LOGO_REGEX: [], _plogo: () => '', _logoWrap: () => '',
  providerLogo: () => '', CHART_PALETTE: [],
};
const frontendObj = new Function('G', 'with (G) { return {' + jsSrc + '}; }')(G);
const feFmt = frontendObj.defaultApiFormat;
if (typeof feFmt !== 'function') { console.log('FAIL: defaultApiFormat not found'); process.exit(1); }

// ── 后端: 解析 Rust 源里的期望行为 ──
// 不执行 Rust, 而是把 providers.rs 的规则表**显式复刻**在这里并断言其字符串存在于源码中,
// 这样两边规则一旦改动而不同步, 测试即失败。
const rs = fs.readFileSync(path.join(ROOT, 'src', 'providers.rs'), 'utf8');
const needed = [
  'if id.contains("claude")', 'Some("anthropic")',
  'if id.contains("grok")', 'Some("responses")',
  'id.starts_with("minimax")', 'id.contains("-plus")', 'id.contains("-max")',
  'id.starts_with("gpt-5")', 'id.starts_with("muse-spark")',
  '"go" =>', '"zen" =>', 'id.starts_with("gpt-")',
];
let fail = 0;
for (const frag of needed) {
  if (!rs.includes(frag)) {
    console.log('FAIL: 后端规则片段缺失 (providers.rs 已改? 需同步本测试): ' + frag);
    fail = 1;
  }
}
if (!fail) console.log('OK   后端规则片段齐备 (providers.rs)');

// ── 逐例比对: 前端函数 vs 后端规则 (按上面片段复刻的语义) ──
function backend(provider, modelId) {
  const id = String(modelId || '').toLowerCase();
  const p = String(provider || '').toLowerCase();
  if (id.includes('claude')) return 'anthropic';
  if (id.includes('grok')) return 'responses';
  if (p === 'go') {
    if (id.startsWith('minimax') || (id.startsWith('qwen3') && (id.includes('-plus') || id.includes('-max')))) return 'anthropic';
    if (id.startsWith('gpt-5') || id.startsWith('muse-spark')) return 'responses';
    return '';
  }
  if (p === 'zen') {
    if (id.startsWith('qwen3') && (id.includes('-plus') || id.includes('-max'))) return 'anthropic';
    if (id.startsWith('gpt-')) return 'responses';
    return '';
  }
  return '';
}

const CASES = [
  ['deepseek', 'gpt-5.6-sol', '非 go/zen 的 gpt-5*: 两端都应是 openai(空)'],
  ['deepseek', 'muse-spark-1.2', '非 go/zen 的 muse-spark: 两端都应是 openai(空)'],
  ['commandcodeAI', 'gpt-5.4', '跨国中转商 gpt-5: 两端都应是 openai(空)'],
  ['go', 'minimax-m2.7', 'go minimax → anthropic'],
  ['go', 'qwen3.6-max', 'go qwen3-max → anthropic'],
  ['go', 'qwen3.5-plus', 'go qwen3-plus → anthropic'],
  ['go', 'gpt-5.6-luna', 'go gpt-5 → responses'],
  ['go', 'muse-spark-1.3', 'go muse-spark → responses'],
  ['go', 'glm-5.2', 'go glm → openai(空)'],
  ['go', 'kimi-k2.7-code', 'go kimi → openai(空)'],
  ['zen', 'qwen3.6-plus', 'zen qwen3-plus → anthropic'],
  ['zen', 'gpt-6-astra', 'zen gpt- → responses'],
  ['zen', 'claude-sonnet-4-6', 'zen claude → anthropic'],
  ['zen', 'deepseek-v4-flash', 'zen deepseek → openai(空)'],
  ['x', 'claude-opus-4', '任意供应商 claude → anthropic'],
  ['x', 'grok-4', '任意供应商 grok → responses'],
  ['GO', 'MiniMax-M2.5', '大小写不敏感 → anthropic'],
  ['', '', '空输入不崩'],
];
console.log('\n=== 逐例比对 (供应商, 模型) → 前端 / 后端 ===');
for (const [p, m, desc] of CASES) {
  const f = feFmt.call(frontendObj, m, p);
  const b = backend(p, m);
  const ok = f === b;
  if (!ok) fail = 1;
  console.log('  ' + (ok ? 'OK  ' : 'FAIL') + ' ' + String(p || '(空)').padEnd(16) + ' ' + String(m || '(空)').padEnd(20) + ' 前端=' + String(f || '(空)').padEnd(10) + ' 后端=' + String(b || '(空)').padEnd(10) + (ok ? '' : '  ← ' + desc));
}

// ── 中转 ID 前缀剥离 (回归): 带 `供应商/` 前缀时 startsWith 类规则曾永不命中 ──
console.log('\n=== 中转 ID 前缀剥离 (startsWith 类规则) ===');
const PFX = [
  ['go/minimax-m2.7', 'go', 'anthropic', '带前缀也要命中 minimax 规则'],
  ['go/gpt-5.6-luna', 'go', 'responses', '带前缀也要命中 gpt-5 规则'],
  ['go/glm-5.2', 'go', '', 'glm 不受影响仍为空'],
  ['minimax-m2.7', 'go', 'anthropic', '不带前缀同样命中'],
  ['go/claude-x', 'go', 'anthropic', 'claude 用 includes, 带前缀本就命中'],
  ['zen/qwen3.6-plus', 'zen', 'anthropic', 'zen + 前缀'],
  ['zen/gpt-6-astra', 'zen', 'responses', 'zen gpt- 前缀'],
];
for (const [mid, prov, want, desc] of PFX) {
  const got = feFmt.call(frontendObj, mid, prov);
  const ok = got === want;
  if (!ok) fail = 1;
  console.log('  ' + (ok ? 'OK  ' : 'FAIL') + ' ' + String(mid).padEnd(18) + ' prov=' + prov.padEnd(6)
    + ' 期望=' + String(want || '(空)').padEnd(10) + ' 实得=' + String(got || '(空)').padEnd(10) + (ok ? '' : ' ← ' + desc));
}

// ── effectiveApiFormat 的回落链 ──
console.log('\n=== effectiveApiFormat 回落链 (配置 > 供应商 > 推断 > openai) ===');
const E = [
  [{ api_format: 'responses', model_id: 'm' }, { name: 'go' }, 'responses', '模型级优先'],
  [{ api_format: '', model_id: 'minimax-x' }, { name: 'go', api_format: 'anthropic' }, 'anthropic', '供应商级次之'],
  [{ api_format: '', model_id: 'gpt-5.6-x' }, { name: 'go' }, 'responses', '推断第三'],
  [{ api_format: '', model_id: 'glm-5' }, { name: 'random' }, 'openai', '兜底 openai'],
];
for (const [m, prov, want, desc] of E) {
  const got = frontendObj.effectiveApiFormat.call(frontendObj, m, prov);
  const ok = got === want;
  if (!ok) fail = 1;
  console.log('  ' + (ok ? 'OK  ' : 'FAIL') + ' ' + String(desc).padEnd(34) + ' 期望=' + String(want).padEnd(10) + ' 实得=' + got);
}
// 推断标记
const inf1 = frontendObj.apiFormatInferred.call(frontendObj, { api_format: '' }, { name: 'go' });
const inf2 = frontendObj.apiFormatInferred.call(frontendObj, { api_format: 'responses' }, { name: 'go' });
const inf3 = frontendObj.apiFormatInferred.call(frontendObj, { api_format: '' }, { name: 'go', api_format: 'anthropic' });
console.log('  %s apiFormatInferred: 未标注=%s (期望 true) / 模型级标注=%s (期望 false) / 供应商级=%s (期望 false)',
  (inf1 === true && inf2 === false && inf3 === false) ? 'OK  ' : 'FAIL', inf1, inf2, inf3);
if (!(inf1 === true && inf2 === false && inf3 === false)) fail = 1;

// ── 「协议由编程工具决定, 网关只负责支持」的核心: 探测能力自动参与路由 ──
// 背景: 早期实现要求用户逐模型勾选 api_formats 候选集, 而真实配置 233 个模型**无一**勾过
// —— 探测到的上游能力被白白浪费。以下断言固化"零手工声明即生效", 并与后端
// providers.rs::available_api_formats 的语义对齐 (改名/改规则即需同步本测试)。
console.log('\n=== 探测能力自动参与 (availableApiFormats / effectiveApiFormat) ===');
for (const frag of ['pub fn available_api_formats', 'pub fn known_formats', 'pub fn is_known_format']) {
  if (!rs.includes(frag)) {
    console.log('FAIL: 后端缺少 ' + frag + ' (providers.rs 已改? 需同步本测试)');
    fail = 1;
  }
}
const ALL3 = ['openai', 'anthropic', 'responses'];
const A = [
  // [model, prov, 期望可用集, 期望默认(chat入口), 说明]
  [{ model_id: 'm' }, { name: 'relay', probed_protocols: ALL3 },
    ALL3, 'openai', '零手工配置: 探测三协议全可用, 默认走 openai'],
  [{ model_id: 'm' }, { name: 'relay', probed_protocols: ['openai'] },
    ['openai'], 'openai', '探测单协议'],
  [{ model_id: 'm' }, { name: 'relay', probed_protocols: [] },
    [], 'openai', '探测为空数组 = 无信息 (不当成"支持 0 个协议")'],
  [{ model_id: 'claude-x' }, { name: 'relay', probed_protocols: ['openai', 'responses'] },
    ['openai', 'responses', 'anthropic'], 'openai', '探测 ∪ 推断 (claude 补上 anthropic)'],
  [{ model_id: 'claude-x', api_format: '' }, { name: 'relay', probed_protocols: ['openai', 'responses'] },
    ['openai', 'responses', 'anthropic'], 'openai', '空串 api_format 不算钉死 (拉取后即写空串)'],
  [{ model_id: 'minimax-x' }, { name: 'go', probed_protocols: [] },
    ['anthropic'], 'anthropic', '探测为空 → 回落推断 (go minimax 走 /messages)'],
  [{ model_id: 'm', api_formats: ['anthropic'] }, { name: 'relay', probed_protocols: ALL3 },
    ['anthropic'], 'anthropic', '显式候选集收窄探测结果 (显式即约束)'],
  [{ model_id: 'm', api_formats: [] }, { name: 'relay', probed_protocols: ALL3 },
    ALL3, 'openai', '空候选集 = 未声明, 不遮蔽探测'],
  [{ model_id: 'claude-x', api_format: 'anthropic' }, { name: 'relay', probed_protocols: ['openai', 'anthropic'] },
    ['openai', 'anthropic'], 'anthropic', '单值 api_format 锁死 → 默认走 anthropic (不再被 openai 覆盖)'],
  // ── 显式单值 = 硬约束 (回归: 曾把用户的手动设置无视掉) ──
  [{ model_id: 'm', api_format: 'openai' }, { name: 'relay', probed_protocols: ALL3 },
    ALL3, 'openai', '显式 openai + 探测三协议 → 锁定 openai'],
  [{ model_id: 'm', api_format: 'anthropic' }, { name: 'relay', probed_protocols: ['openai', 'responses'] },
    ['openai', 'responses', 'anthropic'], 'anthropic', '显式 anthropic 优先于探测到的 openai'],
  // ── 静态层 _static_formats (后端下发) 参与可用集 ──
  [{ model_id: 'm' }, { name: 'mini', _static_formats: ['anthropic'] },
    ['anthropic'], 'anthropic', '静态层 (host 目录/URL 信号) 无探测也生效'],
  [{ model_id: 'm' }, { name: 'relay', probed_protocols: ['openai'], _static_formats: ['anthropic'] },
    ['openai', 'anthropic'], 'openai', '静态层与探测取并集'],
  [{ model_id: 'm', api_formats: ['responses'] }, { name: 'mini', _static_formats: ['anthropic'] },
    ['responses'], 'responses', '显式候选集压过静态层'],
];
for (const [m, prov, wantAvail, wantEff, desc] of A) {
  const gotAvail = frontendObj.availableApiFormats.call(frontendObj, m, prov);
  const gotEff = frontendObj.effectiveApiFormat.call(frontendObj, m, prov);
  const ok = JSON.stringify(gotAvail) === JSON.stringify(wantAvail) && gotEff === wantEff;
  if (!ok) fail = 1;
  console.log('  ' + (ok ? 'OK  ' : 'FAIL') + ' ' + desc.padEnd(44)
    + ' 可用=' + JSON.stringify(gotAvail) + ' 默认=' + gotEff
    + (ok ? '' : '  ← 期望可用=' + JSON.stringify(wantAvail) + ' 默认=' + wantEff));
}
// 显式候选集时不再算"自动推断"
const inf4 = frontendObj.apiFormatInferred.call(frontendObj,
  { model_id: 'm', api_formats: ['openai', 'anthropic'] }, { name: 'relay' });
const inf4ok = inf4 === false;
if (!inf4ok) fail = 1;
console.log('  ' + (inf4ok ? 'OK  ' : 'FAIL') + ' apiFormatInferred: 显式候选集 → false' + (inf4ok ? '' : ' 实得=' + inf4));

console.log('\n' + (fail ? 'FAILED: 前后端协议规则已漂移, 必须同步' : 'all protocol-rule checks passed'));
process.exit(fail);
