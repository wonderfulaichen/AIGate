/**
 * AIGate PV —— 逐帧录制 (比 capture_deck_video.js 简单: 场景是 seek() 纯函数)
 *
 * 场景 HTML (pv_scene.tpl.html) 的全部状态由 window.seek(tMs) 计算,
 * 不依赖 CSS 过渡 / rAF / setInterval —— 因此**不需要** deck 录制那套
 * 虚拟时钟 + getAnimations 手动 seek: 逐帧调 seek(t) 再截图即是确定性的。
 *
 * 依赖: playwright (装在 .openbitfun/tmp/vidtools, 同 capture_deck_video.js)。
 * 用法: node tools/capture_pv.js          # 全片 45s
 *       node tools/capture_pv.js 26 36    # 只录 [26s, 36s) 试拍
 */
const { pathToFileURL } = require('url');
const path = require('path');
const fs = require('fs');

function loadPlaywright() {
  const root = path.resolve(__dirname, '..');
  const candidates = [
    'playwright',
    path.join(root, '.openbitfun', 'tmp', 'vidtools', 'node_modules', 'playwright'),
  ];
  for (const c of candidates) {
    try { return require(c); } catch (e) { if (e.code !== 'MODULE_NOT_FOUND') throw e; }
  }
  throw new Error('找不到 playwright —— cd .openbitfun/tmp/vidtools && npm install playwright && npx playwright install chromium');
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(__dirname, '..');
const SCENE = path.join(ROOT, '.openbitfun', 'tmp', 'video', 'pv', 'scene.html');
const FRAMES = path.join(ROOT, '.openbitfun', 'tmp', 'video', 'pv', 'frames');

const FPS = 30;
const W = 1920, H = 1080;
const DURATION = 90.0;             // 与 v9 音乐 (48 小节 × 1.875s) 一致
const SHOT = { type: 'jpeg', quality: 95 };   // 理由同 capture_deck_video.js: JPEG 快 4.7x

(async () => {
  if (!fs.existsSync(SCENE)) throw new Error('没有场景: 先跑 python tools/build_pv_scene.py');

  // 场景是 seek() 纯函数, 但有少量 CSS filter/transform 需要一帧稳定 —— 每帧前 await rAF。
  const args = process.argv.slice(2).map(Number);
  const from = Number.isFinite(args[0]) ? args[0] : 0;
  const to = Number.isFinite(args[1]) ? args[1] : DURATION;
  // --keep: 保留既有帧 (只重录某一段时用, 免得把整条序列擦掉)
  const KEEP = process.argv.includes('--keep');
  // --settle: 每帧 seek 后等 N ms 再截图 (仅探针用) —— 面板数字是 400ms 补间,
  // 正式录制是逐帧连续推进 (帧间 ~250ms 墙钟, 补间自然追踪), 不需要等;
  // 单点探针若不等, 截到的是补间未起步的旧值 (假绿/假冻结)。
  const SETTLE = process.argv.includes('--settle') ? 550 : 0;

  if (!KEEP) {
    fs.rmSync(FRAMES, { recursive: true, force: true });
  }
  fs.mkdirSync(FRAMES, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    // 场景里的面板是**真 iframe** (panel.html), 父页要读它的 DOM 来驱动数字:
    // file:// 下跨文件访问默认被拦, 必须开这个开关 (Playwright 才能改面板内容)。
    args: ['--allow-file-access-from-files'],
  });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  // 面板 HTML 里的 static 资源是绝对路径 (/admin/static/*) → file:// 下会 404 →
  // Alpine 不加载 → 面板永远停在骨架 (实测: 文本只剩 211 字)。接到真文件。
  const STATIC_DIR = path.join(ROOT, 'src', 'admin_static');
  await ctx.route('**/admin/static/**', (route) => {
    const name = route.request().url().split('/').pop();
    const f = path.join(STATIC_DIR, name);
    if (fs.existsSync(f)) return route.fulfill({ path: f, contentType: 'application/javascript' });
    return route.fulfill({ status: 404, body: '' });
  });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));

  await page.goto(pathToFileURL(SCENE).href, { waitUntil: 'load' });
  const ok = await page.evaluate(() => typeof window.seek === 'function');
  if (!ok) throw new Error('场景没有 window.seek —— 模板坏了');

  // 等面板 iframe 真正渲染出内容 (Alpine 起来 + 预览桩返回)。
  // 不等的话前若干帧拍到的是骨架屏 (文本长度会明显偏小)。
  await page.waitForFunction(() => {
    try {
      const f = document.getElementById('panelFrame');
      if (!f || !f.contentDocument) return false;
      return (f.contentDocument.body.innerText || '').length > 800;
    } catch (e) { return false; }
  }, { timeout: 30000 }).catch(() => console.log('  !! 面板 iframe 未就绪 (跳过等待)'));
  console.log('  panel ready: %s', await page.evaluate(() => {
    try { return document.getElementById('panelFrame').contentDocument.body.innerText.length; }
    catch (e) { return 'n/a'; }
  }));

  const f0 = Math.round(from * FPS);
  const f1 = Math.round(to * FPS);
  const t0 = Date.now();

  for (let f = f0; f < f1; f++) {
    await page.evaluate((t) => { window.seek(t); }, (f / FPS) * 1000);
    if (SETTLE > 0) await new Promise((r) => setTimeout(r, SETTLE));
    // 帧号用**绝对帧序** (f, 不是 f-f0) —— 这样只重录 [from,to) 时能直接覆盖对应帧,
    // 与新建的帧序列编号一致, 编码脚本无需区分。
    const out = path.join(FRAMES, `f${String(f).padStart(5, '0')}.jpg`);
    await page.screenshot({ path: out, ...SHOT });
    if ((f - f0) % 300 === 0) console.log(`  t=${(f / FPS).toFixed(1)}s -> ${out}`);
  }

  await browser.close();
  const n = f1 - f0;
  console.log(`\n录制完成: ${n} 帧 / [${from}s, ${to}s) / 墙钟 ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  console.log(JSON.stringify({ frames: n, fps: FPS, from, to }));
})();
