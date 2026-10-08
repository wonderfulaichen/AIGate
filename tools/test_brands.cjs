#!/usr/bin/env node
/**
 * 品牌识别行为锁 (重写 _MODEL_BRANDS 前后必须一致).
 *
 * 为什么要它 (2026-10-08): ds-brands.js 的品牌表原注释自认「移植自 new-api」,
 * 而 new-api 是 **AGPL-3.0 + 附加署名条款** —— 混进 GPL-3.0 项目会抬升整体义务.
 * 故重写为独立实现。重写**必须不改变识别结果**, 否则就是"顺手改了功能".
 *
 * 用法: node tools/test_brands.cjs
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'src', 'admin', 'ds-brands.js');
const src = fs.readFileSync(SRC, 'utf8');

// 从源码里取出 _MODEL_BRANDS 数组字面量并求值 (不打桩, 直接跑真实数据).
const start = src.indexOf('_MODEL_BRANDS: [');
if (start < 0) { console.error('FAIL: 找不到 _MODEL_BRANDS'); process.exit(1); }
const arrStart = src.indexOf('[', start);
// 括号配平找到数组结尾
let depth = 0, end = -1;
for (let i = arrStart; i < src.length; i++) {
  if (src[i] === '[') depth++;
  else if (src[i] === ']') { depth--; if (depth === 0) { end = i + 1; break; } }
}
if (end < 0) { console.error('FAIL: _MODEL_BRANDS 括号不配平'); process.exit(1); }
const literal = src.slice(arrStart, end);
const BRANDS = eval(literal); // 本地数据, 无外部输入

// 判定逻辑: 复刻 ds-brands.js 里 `brandOf` 的真实语义 —— **单趟遍历, 命中即返回**,
// 每个 rule 先看 kw 再看 re. 顺序敏感 (靠前的 rule 优先), 故必须照抄同一遍历方式,
// 否则测的不是真实行为.
function detect(name) {
  const n = (name || '').toLowerCase();
  for (const b of BRANDS) {
    if (b.kw && b.kw.some(k => n.includes(k))) return b.name;
    if (b.re && new RegExp(b.re).test(n)) return b.name;
  }
  return null;
}

// 期望表: 用真实模型 id / 供应商名 —— 这些是**公开事实**, 写死作为回归基线.
// ⚠️ 顺序敏感的例子保留在表里 (360gpt-pro 命中 OpenAI 的 `gpt-` 是既有真实行为,
//    重写**不得**顺手改掉 —— 那属于改功能, 不属于本次的许可证清理).
const CASES = [
  ['gpt-5.3-codex', 'OpenAI'], ['o3-mini', 'OpenAI'], ['tts-1', 'OpenAI'],
  ['claude-sonnet-5', 'Anthropic'], ['gemini-3.1-pro', 'Gemini'], ['gemma-3', 'Gemini'],
  ['grok-4.6', 'xAI'], ['deepseek-v4.1-flash', 'DeepSeek'], ['qwen3.8-max', 'Qwen'],
  ['kimi-k3', 'Moonshot'], ['minimax-m3', 'MiniMax'], ['doubao-pro', 'Doubao'],
  ['glm-5.3', 'Zhipu'], ['ernie-5', 'Baidu'], ['hunyuan-t1', 'Tencent'],
  ['hy3', 'Tencent'], ['mistral-large-4', 'Mistral'], ['llama-3.1-70b', 'Meta'],
  ['command-r-plus', 'Cohere'], ['flux.1-dev', 'Black Forest Labs'],
  ['phi-4', 'Microsoft'], ['nova-pro', 'Amazon'], ['jamba-1.5', 'AI21 Labs'],
  ['hermes-3', 'Nous Research'], ['sonar-pro', 'Perplexity'], ['nemotron-3.5', 'NVIDIA'],
  ['midjourney-v6', 'Midjourney'], ['kling-v2', 'Kling'], ['suno-v4', 'Suno'],
  ['jimeng-3', 'Jimeng'], ['step-2', 'StepFun'], ['mimo-v2.6', 'MiMo'],
  ['bge-m3', 'BAAI'], ['jina-embeddings-v3', 'Jina'], ['internlm2', 'InternLM'],
  ['baichuan4', 'Baichuan'], ['sparkdesk-v4', 'iFlytek'], ['01-ai/Yi-34B', 'Yi'],
  ['360gpt-pro', 'OpenAI'], // ← 顺序敏感: 先命中 OpenAI 的 `gpt-`, 这是既有行为
  ['stable-diffusion-xl', 'Stability AI'],
  ['vidu-2', 'Vidu'], ['seedream-3', 'Doubao'], ['abab6.5', 'MiniMax'],
  ['cogview-4', 'Zhipu'], ['wenxin-4', 'Baidu'],
  // 未知品牌 → 不该误判
  ['some-random-model', null], ['', null],
];

let pass = 0, fail = 0;
for (const [input, want] of CASES) {
  const got = detect(input);
  if (got === want) { pass++; }
  else { fail++; console.log(`FAIL  ${JSON.stringify(input)} → 期望 ${want}, 实际 ${got}`); }
}

console.log(`\n品牌条目数: ${BRANDS.length}`);
console.log(`== ${pass} passed, ${fail} failed ==`);
process.exit(fail ? 1 : 0);
