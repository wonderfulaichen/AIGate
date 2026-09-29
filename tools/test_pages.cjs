// p3-table 单测: 从 core.js 抽真实 pageNumbers / logSortValue / granularityForDays 跑边界.
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'src', 'admin', 'core.js'), 'utf8');

// 只在顶层定义处求值 (core.js 是拼接进 <script> 的顶层函数集合, 可整段 eval)
const __out = {};
const fn = new Function('__out', src + '\n;Object.assign(__out, {pageNumbers, logSortValue, granularityForDays, spanOfRange, rateTone, statusTone, bcls, PAGE_SIZE_OPTIONS});');
fn(__out);

let fail = 0;
const eq = (name, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  const ok = g === w;
  console.log((ok ? 'OK   ' : 'FAIL ') + name + (ok ? '' : `  got=${g} want=${w}`));
  if (!ok) fail = 1;
};

const { pageNumbers, logSortValue, granularityForDays, spanOfRange, rateTone, bcls } = __out;

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

console.log(fail ? 'FAILED' : 'all page/sort/granularity checks passed');
process.exit(fail);
