// p3-table 单测: 从 core.js 抽真实 pageNumbers / logSortValue / granularityForDays 跑边界.
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'src', 'admin', 'core.js'), 'utf8');

// 只在顶层定义处求值 (core.js 是拼接进 <script> 的顶层函数集合, 可整段 eval)
const __out = {};
const fn = new Function('__out', src + '\n;Object.assign(__out, {pageNumbers, logSortValue, granularityForDays, spanOfRange, rateTone, statusTone, bcls, genSpeedParts, PAGE_SIZE_OPTIONS});');
fn(__out);

let fail = 0;
const eq = (name, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  const ok = g === w;
  console.log((ok ? 'OK   ' : 'FAIL ') + name + (ok ? '' : `  got=${g} want=${w}`));
  if (!ok) fail = 1;
};

const { pageNumbers, logSortValue, granularityForDays, spanOfRange, rateTone, bcls, genSpeedParts } = __out;

// 页码省略
eq('pages: 单页', pageNumbers(0, 1), [0]);
eq('pages: 7 页全列', pageNumbers(3, 7), [0, 1, 2, 3, 4, 5, 6]);
eq('pages: 首页', pageNumbers(0, 20), [0, 1, '…', 19]);
eq('pages: 居中', pageNumbers(10, 20), [0, '…', 9, 10, 11, '…', 19]);
eq('pages: 末页', pageNumbers(19, 20), [0, '…', 18, 19]);
eq('pages: 越界夹取 (负数)', pageNumbers(-5, 20), [0, 1, '…', 19]);
eq('pages: 越界夹取 (超界)', pageNumbers(999, 20), [0, '…', 18, 19]);
eq('pages: 无重复页码', (() => { const a = pageNumbers(10, 20); return a.filter(x => x !== '…').length === new Set(a.filter(x => x !== '…')).size; })(), true);

// 排序取值
eq('sort: time 数值', logSortValue({ timestamp: 100 }, 'time'), 100);
eq('sort: tokens 求和', logSortValue({ prompt_tokens: 10, completion_tokens: 5 }, 'tokens'), 15);
eq('sort: tokens 缺字段按 0', logSortValue({}, 'tokens'), 0);
eq('sort: cache 无输入给 -1 (排最后)', logSortValue({ prompt_tokens: 0, prompt_cache_hit_tokens: 5 }, 'cache'), -1);
eq('sort: cache 比值', logSortValue({ prompt_tokens: 100, prompt_cache_hit_tokens: 25 }, 'cache'), 0.25);
eq('sort: 文本小写化', logSortValue({ model: 'DeepSeek/Chat' }, 'model'), 'deepseek/chat');
eq('sort: null 安全', logSortValue(null, 'time'), '');

// 粒度推导
eq('gran: 1 天 → hour', granularityForDays(1), 'hour');
eq('gran: 31 天 → day', granularityForDays(31), 'day');
eq('gran: 32 天 → month', granularityForDays(32), 'month');
eq('gran: 非法值 → hour (<=1)', granularityForDays('x'), 'hour');

// 反向识别
eq('span: 29d → day', spanOfRange('29d'), 'day');
eq('span: 自定义 → 空', spanOfRange('45d'), '');

// 成功率分级 (单一事实源)
eq('rate: 100 → ok', rateTone(100), 'ok');
eq('rate: 90 → ok', rateTone(90), 'ok');
eq('rate: 70 → warn', rateTone(70), 'warn');
eq('rate: 69.9 → err', rateTone(69.9), 'err');
eq('rate: NaN → neutral', rateTone('x'), 'neutral');
eq('bcls 映射', bcls('warn'), 'bdg-w');
eq('bcls 未知 tone → neutral', bcls('nope'), 'bdg-n');

// ── 纯生成吐字速度 (前端判据, 必须与后端 gen_speed_of 同口径) ──
// 修复前的真实故障: 详情页用 `completion_tokens/genMs`, 漏剔除思考 ——
// 实测 1229 输出里 1223 是思考, 详情显示 44.3 tok/s 而真实正文只有 0.2 (虚高 204 倍);
// 另有 2 条 out==rt 的纯思考记录显示 41.3 tok/s (真值不存在)。
// 这几条断言就是钉住"分子剔除思考 + 正文为 0 不给速度"。
eq('genSpeed: 剔除思考 token (分子=正文)',
  Math.round(genSpeedParts({ first_token_ms: 1000, latency_ms: 3000, completion_tokens: 1229, reasoning_tokens: 1223 }).speed * 10) / 10,
  3.0);
eq('genSpeed: 无思考时与旧口径一致',
  Math.round(genSpeedParts({ first_token_ms: 1000, latency_ms: 3000, completion_tokens: 100 }).speed * 10) / 10,
  50.0);
eq('genSpeed: 纯思考 (out==rt) → 不可测', genSpeedParts({ first_token_ms: 100, latency_ms: 2000, completion_tokens: 8192, reasoning_tokens: 8192 }).speed, null);
eq('genSpeed: 思考 > 输出 (脏数据) 不出现负速度', genSpeedParts({ first_token_ms: 100, latency_ms: 2000, completion_tokens: 10, reasoning_tokens: 99 }).speed, null);
eq('genSpeed: 无首字延迟 (非流式) → 不可测', genSpeedParts({ latency_ms: 2000, completion_tokens: 100 }).speed, null);
eq('genSpeed: 首字 >= 总耗时 → 不可测', genSpeedParts({ first_token_ms: 2000, latency_ms: 2000, completion_tokens: 100 }).speed, null);
eq('genSpeed: 空记录安全', genSpeedParts(null).speed, null);
eq('genSpeed: tokens 用于加权聚合 (与返回的 ms/tokens 自洽)',
  (() => { const p = genSpeedParts({ first_token_ms: 0 + 250, latency_ms: 1250, completion_tokens: 500, reasoning_tokens: 300 });
           return [p.tokens, Math.round(p.ms), Math.round(p.speed)]; })(),
  [200, 1000, 200]);

console.log(fail ? 'FAILED' : 'all page/sort/granularity checks passed');
process.exit(fail);
