/**
 * AIGate 介绍片 —— deck 逐帧确定性录制 (html-to-video-pipeline Mode B)
 *
 * 为什么不用 recordVideo (Mode A):
 *   实时录屏是可变帧率 (~25fps) 且与动画抢 CPU, 1080p 出片会发糊/丢帧。逐帧截图是精确
 *   fps、每帧都渲染完整、不受机器负载影响, 且时长由构造保证 (frames/fps)。
 *
 * 为什么是**单一会话连续录**:
 *   deck 18 页在同一个文档里, 顺序 show(0..17) 就是真实放映的样子。一次会话产出一条
 *   帧序列 → 只需一次 ffmpeg 编码, 完全绕开多段拼接的时基陷阱 (skill 里那条
 *   "8s 变 35s" 的坑)。
 *
 * 三层时间轴, 各用各的驱动 (这是本脚本的核心):
 *   ① canvas 星尘/光斑 + countUp(countUp 用 Date.now + setInterval) —— 由 CDP
 *      **虚拟时钟**逐帧发放 budget 驱动, 使 30fps 的 rAF 节奏与真实播放一致;
 *   ② CSS 过渡 (.s 翻页淡入 + 子元素递增 transition-delay + 曲线描边 + 进度条填充)
 *      —— 实测**不随虚拟时钟推进** (opacity 停在 0.110 不动), 必须用
 *      getAnimations() 手动 seek;
 *   ③ 每个动画的 seek 目标 = "它诞生至今的时长", 由 __birth 表记录。
 *      这样每个动画的 currentTime 单调递增, 旧页的退场过渡会被推到终态 (而不是被
 *      重置回 opacity:1 反过来盖住新页) —— 正是 skill 里 "只能向前、不能倒退" 那条。
 *
 * 用法:
 *   node capture.js                 # 全 18 页
 *   node capture.js 1 3 9           # 只录这几页 (1-based), 用于试拍
 */
const { pathToFileURL } = require('url');
const path = require('path');
const fs = require('fs');

// Playwright 约 195MB (含 Chromium), 刻意不入库 —— 装在本地草稿区 .openbitfun/tmp/vidtools。
// 故此脚本不靠 node_modules 就近查找, 显式从两处候选解析 (仓库根 / 草稿区)。
function loadPlaywright() {
  const root = path.resolve(__dirname, '..');
  const candidates = [
    'playwright',
    path.join(root, '.openbitfun', 'tmp', 'vidtools', 'node_modules', 'playwright'),
  ];
  for (const c of candidates) {
    try { return require(c); } catch (e) { if (e.code !== 'MODULE_NOT_FOUND') throw e; }
  }
  throw new Error(
    '找不到 playwright。先装:\n' +
    '  cd .openbitfun/tmp/vidtools && npm install playwright && npx playwright install chromium');
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(__dirname, '..');
const DECK = path.join(ROOT, 'docs', 'product-deck.html');
const VDIR = path.join(ROOT, '.openbitfun', 'tmp', 'video');
const FRAMES = path.join(VDIR, 'intro_frames');

const FPS = 30;
const W = 1920, H = 1080;

// 帧格式: JPEG 而不是 PNG —— 纯性能取舍, 不是画质妥协。
// 实测 (1080p, 第 3/9 页): page.screenshot 出 PNG 要 605~830ms/帧, 出 JPEG 只要 128~147ms
//   (PNG 的无损压缩在 1080p 渐变底上是最坏的输入; JPEG 直接把这一项从 ~35 分钟压到 ~7 分钟)。
// 画质用一个独立实验兜住 (tools/jpeg_qc.py, 同一张 PNG 只过一遍 ffmpeg 比 PSNR, 单一变量):
//   浏览器 q95 这一档 127KB/帧。而整条管道真正的损失在 ffmpeg 那一步 —— 见 encode 段。
// ⚠️ 别用 webp: 实测 1051ms/帧 (无损) / 274ms (q92), 比 PNG 还慢。
const SHOT = { type: 'jpeg', quality: 95 };
const FRAME_EXT = 'jpg';

// 每页时长 = 入场动效 (~2.2s: 子元素 delay 至 .22s + 过渡 .55~.65s, 曲线页最慢到 ~2.1s)
//          + 停留阅读时间。有计数/图表/界面细节的页给多一点。
const PAGE_SEC = [6.0, 6.0, 7.0, 6.0, 6.0, 6.0, 6.5, 7.0, 7.0, 6.0,
                  6.0, 6.5, 6.0, 6.5, 6.0, 6.0, 6.0, 6.5];

// 交互残留: 翻页提示 / 页码 / 顶部进度条 —— 进了视频就是"这是录屏"的破绽。
// 与原 shoot_deck_hd.py 同一口径 (那份的门禁 CSS 还额外强制终态, 这里不需要:
// 我们靠虚拟时钟 + seek, 不是靠 !important 抢时间)。
const HIDE_CSS = '.hint,#pg,#bar{display:none!important}';

const FREEZE_JS = () => {
  const st = document.createElement('style');
  st.id = '__freeze';
  st.textContent = '*,*::before,*::after{animation-play-state:paused!important}';
  const attach = () => (document.head || document.documentElement).appendChild(st);
  if (document.head || document.documentElement) attach();
  else document.addEventListener('DOMContentLoaded', attach, { once: true });
};

// 在页面上下文里维护"动画诞生时刻"表 + 逐帧 seek。now 用虚拟时钟的 performance.now()。
const seekAll = (elapsedMs) => {
  if (!window.__birth) window.__birth = new Map();
  const born = window.__birth;
  const list = document.getAnimations();
  for (const a of list) {
    if (!born.has(a)) born.set(a, elapsedMs);
    const t = elapsedMs - born.get(a);
    try { a.pause(); a.currentTime = t; } catch (e) { /* 已失效的动画 */ }
  }
  return list.length;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const only = process.argv.slice(2).map(Number).filter((n) => n >= 1 && n <= 18);
  const pages = only.length ? only.map((n) => n - 1) : PAGE_SEC.map((_, k) => k);

  fs.rmSync(FRAMES, { recursive: true, force: true });
  fs.mkdirSync(FRAMES, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1,
  });
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);

  page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));

  await page.addInitScript(FREEZE_JS);

  // 先让真实时钟把页面载完 (虚拟时钟暂停时 goto 会直接超时)。
  await page.goto(pathToFileURL(DECK).href, { waitUntil: 'domcontentloaded' });
  await page.evaluate((css) => {
    const st = document.createElement('style');
    st.textContent = css;
    document.head.appendChild(st);
  }, HIDE_CSS);

  // 停表。此后 performance.now() / Date.now() 只在我发放 budget 时前进。
  await cdp.send('Emulation.setVirtualTimePolicy', { policy: 'pause' });

  const tick = async () => {
    const expired = new Promise((r) => cdp.once('Emulation.virtualTimeBudgetExpired', r));
    await cdp.send('Emulation.setVirtualTimePolicy', {
      policy: 'pauseIfNetworkFetchesPending',
      budget: 1000 / FPS,
    }).catch(() => {});
    await Promise.race([expired, sleep(5000)]).catch(() => {});
  };

  let frameNo = 0;
  const t0 = Date.now();

  for (const p of pages) {
    // 每页从"干净的诞生表"开始 —— 否则上一页的动画会带着旧 birth 继续被 seek。
    await page.evaluate((n) => {
      window.__birth = new Map();
      window.show(n);
    }, p);

    const total = Math.round(PAGE_SEC[p] * FPS);
    for (let f = 0; f < total; f++) {
      if (f > 0) await tick();
      const elapsed = (f / FPS) * 1000;
      const nAnim = await page.evaluate(seekAll, elapsed);

      const out = path.join(FRAMES, `f${String(frameNo).padStart(5, '0')}.${FRAME_EXT}`);
      await page.screenshot({ path: out, ...SHOT });

      if (f === 0 || f === total - 1)
        console.log(`  第 ${p + 1} 页 ${f === 0 ? '首' : '末'}帧  anims=${nAnim}  ${out}`);
      frameNo++;
    }
  }

  await browser.close();
  const secs = frameNo / FPS;
  console.log(`\n录制完成: ${frameNo} 帧 / ${secs.toFixed(1)}s / ${pages.length} 页`);
  console.log(`帧序列: ${FRAMES}`);
  console.log(`墙钟耗时: ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  console.log(JSON.stringify({ frames: frameNo, fps: FPS, seconds: secs, pages: pages.map((n) => n + 1) }));
})();
