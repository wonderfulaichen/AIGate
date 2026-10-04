/**
 * PV 排版门禁 —— 抓「字被截出画面」这类错误。
 *
 * 为什么需要它: 画面里所有标注都挂在一个 6000px 宽的世界层上, 随摄影机 translate+scale。
 * 同一个标注在不同景别下的屏幕位置完全不同 —— 全景 (z≈0.60) 时在画面内, 跟拍 (z≈1.08)
 * 时可能整条掉出下边缘。第一版就把闸门字幕放在了世界 y=1002, 跟拍时下缘被切掉
 * (用户截图: "把字体都给截出去了")。靠肉眼看关键帧抓不干净, 必须逐时刻量化。
 *
 * 判据 (对每个可见标注、每个采样时刻):
 *   ① 屏幕包围盒完整落在视口内 (留 MARGIN 安全边)
 *   ② 文本没有溢出自身盒子 (scrollWidth > clientWidth 时 nowrap 文本会被裁)
 *
 * 用法: node tools/verify_pv_layout.js
 * 依赖: playwright (同 capture_pv.js, 装在 .openbitfun/tmp/vidtools)
 */
const { pathToFileURL } = require('url');
const path = require('path');
const fs = require('fs');

function loadPlaywright() {
  const root = path.resolve(__dirname, '..');
  for (const c of ['playwright',
    path.join(root, '.openbitfun', 'tmp', 'vidtools', 'node_modules', 'playwright')]) {
    try { return require(c); } catch (e) { if (e.code !== 'MODULE_NOT_FOUND') throw e; }
  }
  throw new Error('找不到 playwright —— cd .openbitfun/tmp/vidtools && npm install playwright');
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(__dirname, '..');
// 可选: node verify_pv_layout.js <scene.html>  —— 便于对"故意做坏"的副本做灵敏度自检
const SCENE = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(ROOT, '.openbitfun', 'tmp', 'video', 'pv', 'scene.html');
const W = 1920, H = 1080;
const MARGIN = 28;            // 安全边: 贴边也算不合格 (播放器可能裁边)
const STEP = 0.25;            // 采样间隔 (秒)
const DURATION = 90.0;
const SEL = '.anno,.dim,#endcard .logo,#endcard .tagline,#endcard .links';

(async () => {
  if (!fs.existsSync(SCENE)) throw new Error('没有场景: 先跑 python tools/build_pv_scene.py');
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: W, height: H } });
  await page.goto(pathToFileURL(SCENE).href, { waitUntil: 'load' });

  const bad = [];
  const seen = new Map();      // id → 最早出现时刻 (给报告用)
  for (let t = 0; t <= DURATION + 1e-9; t += STEP) {
    // ⚠️ seek() 收的是**毫秒**; 传秒的话每个采样点都落在 t≈0, 所有标注 opacity=0,
    //    门禁会假绿 (第一版就这么写错, 是"故意做坏副本"自检抓出来的)。
    const hits = await page.evaluate(({ sel, w, h, margin, t }) => {
      window.seek(t * 1000);
      const out = [];
      for (const el of document.querySelectorAll(sel)) {
        const op = parseFloat(getComputedStyle(el).opacity || '0');
        if (op <= 0.05) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) continue;
        const over = {
          left: r.left < margin, right: r.right > w - margin,
          top: r.top < margin, bottom: r.bottom > h - margin,
        };
        const clipped = el.scrollWidth > el.clientWidth + 2;
        if (over.left || over.right || over.top || over.bottom || clipped) {
          out.push({
            id: el.id || el.className, op: Math.round(op * 100) / 100,
            rect: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
            over, clipped, scrollW: el.scrollWidth, clientW: el.clientWidth,
          });
        }
      }
      return out;
    }, { sel: SEL, w: W, h: H, margin: MARGIN, t });
    for (const x of hits) {
      const key = x.id;
      if (!seen.has(key)) seen.set(key, t);
      bad.push(Object.assign({ t }, x));
    }
  }
  await browser.close();

  if (!bad.length) {
    console.log('排版 OK —— ' + (Math.round(DURATION / STEP) + 1) +
      ' 个采样时刻, 全部标注完整落在画面内 (安全边 ' + MARGIN + 'px)');
    process.exit(0);
  }

  console.log('排版问题 (共 ' + bad.length + ' 处):');
  const byId = new Map();
  for (const b of bad) {
    if (!byId.has(b.id)) byId.set(b.id, []);
    byId.get(b.id).push(b);
  }
  for (const [id, list] of byId) {
    const first = list[0], last = list[list.length - 1];
    const ov = Object.entries(first.over).filter(([, v]) => v).map(([k]) => k).join('/');
    console.log('  %s: %d 个时刻越界 (%ss–%ss)  越界边=%s  首例 rect=[%s]%s',
      id, list.length, first.t.toFixed(2), last.t.toFixed(2), ov || '-',
      first.rect.join(', '), first.clipped ? '  文本溢出盒子' : '');
  }
  process.exit(1);
})();
