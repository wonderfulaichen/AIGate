// P0 拆分校验 (持续版): ① 落盘切片按 manifest 拼接自洽 ② JS 逐文件(harness)语法 ③ 全 script 拼接语法
// ④ admin.rs include 顺序 == manifest 顺序.
// 注: 「拼接 == 拆分前 admin.html 字节」是 P0 一次性校验 (基线 admin_orig.html), P1 起源码
// 正常演进, 字节必然不同 —— 该检查已完成使命, 不再适用 (用法见 git 历史).
// 用法: node verify_parts.js
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, 'src', 'admin');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'split_manifest.json'), 'utf8'));

let fail = 0;
const err = (m) => { console.log('FAIL: ' + m); fail = 1; };

// ① 自洽: manifest 覆盖的切片存在且非空, 拼接可生成完整面板
const orig = fs.readFileSync(path.join(__dirname, 'admin_orig.html'));
let joined = Buffer.alloc(0);
for (const e of manifest) {
  const p = path.join(SRC, e.file);
  if (!fs.existsSync(p)) { err('missing slice: ' + e.file); continue; }
  const b = fs.readFileSync(p);
  if (b.length === 0) err('empty slice: ' + e.file);
  joined = Buffer.concat([joined, b]);
}
const hasDoc = joined.includes(Buffer.from('<!DOCTYPE html>')) && joined.includes(Buffer.from('</html>'));
if (!hasDoc) err('joined panel invalid: no doctype/closing html');
console.log((fail ? '' : 'OK ') + 'slices assemble into full panel (' + joined.length + ' bytes, ' + manifest.length + ' files; P0 baseline was ' + orig.length + ' B)');

// ② JS 逐文件 harness 解析
const harness = {
  standalone: (s) => s,
  'fn-head': (s) => s + '\n  };\n}',
  'obj-body': (s) => 'void({' + s + '});',
  'fn-tail': (s) => 'function __h(){return {' + s,
};
let jsCount = 0;
for (const e of manifest) {
  if (!e.file.endsWith('.js')) continue;
  if (!harness[e.mode]) { err(e.file + ': unknown mode ' + e.mode); continue; }
  const src = fs.readFileSync(path.join(SRC, e.file), 'utf8');
  try {
    new Function(harness[e.mode](src));
    jsCount++;
  } catch (ex) {
    err(e.file + ' [' + e.mode + ']: ' + ex.message);
  }
}
console.log((fail ? '' : 'OK ') + 'js parts parsed: ' + jsCount);

// ③ 全 script 拼接解析 (i18n 起 至 ds-fmt 止, 即 <script> 内实际内容)
const jsFiles = manifest.filter((e) => e.file.endsWith('.js'));
const full = jsFiles.map((e) => fs.readFileSync(path.join(SRC, e.file), 'utf8')).join('');
try {
  new Function(full);
  console.log('OK full script parses (' + full.length + ' chars)');
} catch (ex) {
  err('full script: ' + ex.message);
}

// ④ admin.rs 的 include 顺序 == manifest 顺序
const adminRs = fs.readFileSync(path.join(ROOT, 'src', 'admin.rs'), 'utf8');
const m = adminRs.match(/const ADMIN_HTML: &str = concat!\(([\s\S]*?)\);/);
if (!m) { err('admin.rs: concat not found'); }
else {
  const order = [...m[1].matchAll(/include_str!\("admin\/([^"]+)"\)/g)].map((x) => x[1]);
  const expect = manifest.map((e) => e.file);
  if (JSON.stringify(order) !== JSON.stringify(expect)) {
    err('admin.rs order mismatch:\n  got: ' + order.join(',') + '\n  exp: ' + expect.join(','));
  } else console.log('OK admin.rs include order matches manifest (' + order.length + ')');
}

process.exit(fail);
