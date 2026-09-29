// i18n 重复键 / 缺键检查 (独立工具版; 编译期门禁见 build.rs::check_i18n_tables, 逻辑同源).
// 教训: ① 中英表大量同行内联多键, 按行正则抓会漏近 2/3 (HANDOFF 7.5);
//       ② 本脚本旧版「先消费字符串、后试键」导致键位永不可达、恒记录 0 键 = 假 OK —— 已重写.
// 用法: node check_i18n.cjs [路径]   (默认 src/admin/i18n.js)
const fs = require('fs');
const target = process.argv[2] || 'src/admin/i18n.js';
const src = fs.readFileSync(target, 'utf8');

const anchor = 'const I18N = {';
const start = src.indexOf(anchor);
if (start < 0) { console.error(anchor + ' not found in ' + target); process.exit(1); }
const baseLine = 1 + src.slice(0, start).split('\n').length - 1;
const chars = Array.from(src.slice(start));
const lineOf = (i) => baseLine + chars.slice(0, i).filter((c) => c === '\n').length;

let st = 'normal';           // normal | line | block
let depth = 0;
let lang = null;
const tables = new Map();    // lang -> Set(keys)
const errors = [];
let i = chars.indexOf('{');
let done = false;

while (i < chars.length && !done) {
  const c = chars[i];
  const n = chars[i + 1];
  if (st === 'line') { if (c === '\n') st = 'normal'; i++; continue; }
  if (st === 'block') { if (c === '*' && n === '/') { st = 'normal'; i += 2; } else i++; continue; }
  // normal
  if (c === '/' && n === '/') { st = 'line'; i += 2; continue; }
  if (c === '/' && n === '*') { st = 'block'; i += 2; continue; }
  if (c === '`') { // 模板字面量: 原子跳过
    let k = i + 1;
    while (k < chars.length) {
      if (chars[k] === '\\') { k += 2; continue; }
      if (chars[k] === '`') break;
      k++;
    }
    i = Math.min(k + 1, chars.length);
    continue;
  }
  // try key: 字符串或标识符 + `:`
  let lit = null, j0 = i;
  if (c === "'" || c === '"') {
    const q = c;
    let k = i + 1, s = '';
    while (k < chars.length) {
      if (chars[k] === '\\') { s += chars[k + 1] || ''; k += 2; continue; }
      if (chars[k] === q) break;
      s += chars[k]; k++;
    }
    if (k < chars.length) { lit = s; j0 = k + 1; }
  } else if (/[A-Za-z_$]/.test(c)) {
    let k = i;
    while (k < chars.length && /[\w$]/.test(chars[k])) k++;
    lit = chars.slice(i, k).join('');
    j0 = k;
  }
  if (lit === null) {
    if (c === '{') depth++;
    else if (c === '}') {
      depth = Math.max(0, depth - 1);
      if (depth === 1) lang = null;
      if (depth === 0) done = true;
    }
    i++;
    continue;
  }
  // 有字面量: 跳空白看是不是键
  let j = j0;
  while (j < chars.length && /\s/.test(chars[j])) j++;
  if (j < chars.length && chars[j] === ':') {
    let v = j + 1;
    while (v < chars.length && /\s/.test(chars[v])) v++;
    if (v < chars.length && chars[v] === '{') {
      if (depth === 1) { lang = lit; if (!tables.has(lang)) tables.set(lang, new Set()); }
      depth++;
      i = v + 1;
      continue;
    }
    if (depth === 2 && lang !== null) {
      const set = tables.get(lang);
      if (set.has(lit)) errors.push(`第 ${lineOf(i)} 行: 表 \`${lang}\` 重复键 '${lit}'`);
      else set.add(lit);
    }
    i = j0;
    continue;
  }
  i = j0; // 值字面量, 跳过
}

if (!tables.has('zh-CN')) errors.push('缺少语言表 zh-CN');
if (!tables.has('en-US')) errors.push('缺少语言表 en-US');
if (tables.has('zh-CN') && tables.has('en-US')) {
  const zh = tables.get('zh-CN'), en = tables.get('en-US');
  const missEn = [...zh].filter((k) => !en.has(k)).sort();
  const missZh = [...en].filter((k) => !zh.has(k)).sort();
  if (missEn.length) errors.push(`zh-CN 有而 en-US 缺 ${missEn.length} 键: ${missEn.slice(0, 20).join(', ')}${missEn.length > 20 ? ', …' : ''}`);
  if (missZh.length) errors.push(`en-US 有而 zh-CN 缺 ${missZh.length} 键: ${missZh.slice(0, 20).join(', ')}${missZh.length > 20 ? ', …' : ''}`);
}

if (errors.length) {
  console.log(`FAIL (${errors.length}):`);
  for (const e of errors) console.log('  ' + e);
  process.exit(1);
}
const nz = tables.get('zh-CN') ? tables.get('zh-CN').size : 0;
const ne = tables.get('en-US') ? tables.get('en-US').size : 0;
console.log(`OK: zh-CN ${nz} 键 / en-US ${ne} 键, 无重复无缺键 (${target})`);
