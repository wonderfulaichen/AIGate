# 前端开发规约 (AIGate 管理面板)

> 改前端**之前先读这一份**。这里的每条都对应一次踩过的坑，不是风格偏好。
> 面板是单页应用，**没有构建步骤**：源码是 `src/admin/` 下的切片，由 Rust 端
> `concat!(include_str!(...))` 按固定顺序拼回单串编译进二进制。

## 1. 目录与拼接

```
src/admin/
├── head.html                  <head> + meta 引用 (不含 CSS 内容)
├── font.css                   内嵌 Inter (变量字体 latin 子集, base64) —— 由脚本生成, 勿手改
├── base.css                   :root token / reset / 全局 / 动效基础
├── components.css             组件类 (徽章/卡片/按钮/表格/骨架/设置行)
├── pages.css                  页签专属 + [x-cloak] + toast
├── body-shell.html            <body> 壳: skip 链接 + 顶栏(品牌+胶囊导航+全局控件) + main 起点
├── page-{overview,analytics,records,settings,providers,about}.html   六个页签模板
├── footer.html                页脚
├── modal-whatsnew-recall.html / modal-add-fetchdiff.html / drawer-provider.html / panel-price.html
├── i18n.js                    I18N 中英双表 (见 §3)
├── stores.js                  Alpine.store: i18n / toast / cf
├── core.js                    顶层工具: t/authHeaders/fmt*/图表 SVG/纯函数 (无组件状态依赖)
├── ds-state.js                dashboard() 的 return { 起始 + 全部状态字段
├── ds-{onboard,core,brands,settings-api,meta,stats,trend,config,providers,
│       price,provform,provtest,provsave,computed,records,fmt}.js   方法组
└── tail.html                  </script> + toast 容器 + 确认弹窗 + </body>
```

**拼接顺序 = `src/admin.rs` 的 `concat!` 参数顺序 = `.openbitfun/tmp/split_manifest.json`**
（由 `verify_parts.js` 校验三者一致）。

- **严禁调整 include 顺序** —— 顺序即字节序。`ds-state.js` 必须在所有 ds-* 之前
  （它开着 `return {`），`ds-fmt.js` 必须在最后（它收尾 `};` + `}`）。
- 新增方法组请追加到 `ds-fmt.js` 之前，并在 `admin.rs` 与 manifest 同步登记。
- `cargo build` 无 Node 依赖；前端改动**必须重构建**才生效（`include_str!`）。

## 2. 验证（改完必做）

```powershell
node .openbitfun/tmp/verify_parts.js     # ① 切片拼接自洽 + 20 个 JS 部件语法 + include 顺序
node .openbitfun/tmp/check_i18n.cjs      # ② i18n 中英双表缺键/重复键 (build.rs 也拦)
node .openbitfun/tmp/test_pages.cjs      # ③ 纯函数单测 (页码/排序/粒度/分级)
node .openbitfun/tmp/test_ime.cjs        # ④ 搜索 IME 行为
node .openbitfun/tmp/test_provload.cjs   # ⑤ 供应商加载并发收口
node .openbitfun/tmp/test_fmt_rules.cjs  # ⑥ 前后端协议规则一致 (含探测能力自动参与路由)
cargo test --release && cargo build --release    # ⑦ 252 绿 + 0 警告
```

改颜色 / 主题 / 硬编码色时追加三条（**不需要浏览器**，纯解析即可判定，比肉眼可靠）：

```powershell
python .openbitfun\tmp\theme_complete.py  # ⑧ 浅色完整性: 主题相关 token 是否有浅色取值
python .openbitfun\tmp\theme_dark_diff.py # ⑨ 暗色零变化: 逐文件比对 HEAD 与新版的"实际生效颜色集合"
python .openbitfun\tmp\theme_contrast.py  # ⑩ 浅色可读性: 关键前景/背景组合的 WCAG 对比度
python .openbitfun\tmp\balance.py         # ⑪ 标签配平: 逐文件查未闭合标签 (会吞掉后续页面)
```

- `theme_complete.py` / `theme_contrast.py` 里各有一处**会随 token 改名而失配**的地方：
  `ALLOWED` 允许清单（与主题无关的 token）和 `PAIRS` 的对比度组合。**改 token 名时记得同步这两个脚本**
  （本轮改 `--indigo`→`--accent`、新增 `--icon-*`/`--grid-*` 后都同步过），
  否则门禁会报"该覆盖却漏掉"或"无法解析颜色"这类**假失败**。

`theme_dark_diff.py` 的口径值得记住：它把 `var(--token)` 用**各自版本**的 `:root` 解析成字面量，
再比对颜色集合 —— 集合相同即证明"这次只换了写法，没动暗色视觉"。若出现差异，逐条确认是否
刻意（例如统一某个 .28→.30 的 alpha）。

**i18n 双表在编译期强制校验**（`build.rs::check_i18n_tables`）：重复键、缺键、
语言表缺失都会让 `cargo build` 直接失败。所以**不必手工数键**，编译通过即为对齐。

浏览器实测（改视觉/交互时）：

```powershell
python .openbitfun\tmp\build_preview.py                 # src/admin/ + fetch 桩 → preview/admin.html
python .openbitfun\tmp\build_base_preview.py            # HEAD 版同位基线 (A/B 用)
python -m http.server 8898 --bind 127.0.0.1             # cwd = .openbitfun/tmp/preview
```
再用内置浏览器开 `http://127.0.0.1:8898/wrap.html`（iframe 固定 1200 宽；
**直开面板会得到假的窄屏布局**）。桩支持 `?sc=empty|partial|done&logs=N&delay=N`。

## 3. i18n

- 文案**只加在 `src/admin/i18n.js`**，中英各一条，键名相同。
- 取值统一走 `t('key')` / `t('key', arg0, arg1)`（占位符是 `{0}` `{1}`）。
- 涉及**新文案**（用户能看见的新句子）先列清单给用户过目再写死 —— 这是本项目红线。
- 复用既有键优先于新增：例如结论区直接用 `takeaway_health`，不新造一遍同义句。

## 4. 设计 token（`base.css` 的 `:root`）

**禁止**在组件里写颜色/圆角/字号字面量，一律用 token：

| 用途 | token |
|---|---|
| 面 | `--bg` `--card` `--elevated` `--input` |
| 文字 | `--fg` `--muted` |
| 描边 | `--border-subtle`(.06) `--border`(.08) `--border-t`(.10) `--border-s`(.12) |
| 填充层级 | `--fill-1`(.02) `--fill-15`(.025) `--fill-2`(.03) `--fill-3`(.04) `--fill-4`(.05) `--fill-5`(.06) |
| 暗部遮罩 | `--shade-1` `--shade-2` `--shade-3` |
| 主色 | `--primary`(**填充面**, Apple 蓝深档) `--primary-h` `--primary-fg` `--accent`(**文字/图标/线**, Apple 蓝原值) `--secondary`(次强调) |
| 状态四元组 | `--ok/-bg/-fg/-bd`、`--warn/…`、`--err/…`、`--neutral/…`、`--info/…`（= Apple 系统色） |
| 圆角 | `--rs`(6) `--rm`(10) `--r`(14) `--rl`(18) `--pill`（**四档 + 胶囊**, 见 §4.5） |
| 字阶 | `--fs-9` … `--fs-18` + `--fs-kpi`(34) + `--fs-hero`(48)（见 §4.5） |
| 图标字形 | `--icon-sm`(16) `--icon-md`(22) `--icon-lg`(30) —— **图标轴, 不属字阶** |
| 间距 | `--sp-1..7` |
| 动效 | `--dur-quick`(.12s 按下/悬停) `--dur-fast`(.16s) `--dur`(.24s) `--dur-slow`(.4s) `--dur-spring`(.32s) `--ease-out`(Apple 式快出慢停) `--ease-spring`(过冲 1.8，**只给开关滑块**) |
| 遮罩/规则线 | `--overlay`(模态) `--overlay-soft`(轻) `--rule-strong`(图表十字线) |
| 表面与骨架 | `--sb-bg`(侧栏) `--tip-bg`(悬停气泡) `--track`(转圈轨道) `--skeleton` `--sh-sm` |
| 品牌软底梯度 | `--primary-soft`(.12) `--primary-soft-deep`(.08) `--primary-badge`(.15) `--primary-soft-h`(.18) `--primary-bd-soft`(.25) `--primary-bd`(.30) `--row-hover`(.04) |
| 其他表面 | `--violet` `--secondary-soft` `--tgl-off` `--err-h`(危险按钮**悬停文字色**) `--err-bd-mid` `--warn-bg-strong` `--toast-shadow` `--toast-err-bg` `--toast-ok-bg` `--toast-warn-bg` |
| 主题/侧栏 | `--hdr-bg` `--sb-bg` |
| 字体族 | `--font-ui`(**首位是内嵌 Inter**) `--font-mono`(**仅代码类内容**) |
| **容器描边** | `--surface-border` —— 容器用**这一个**, 不画实体描边（见 §4.1c） |
| 画布氛围 | `--amb`(低饱和径向渐变, 见 §4.1b) |
| 质感层 | `--card-glass`(玻璃卡底) `--card-nest`(嵌套块底) `--glass-blur` `--elev-1/2/3`(静置/悬停/浮层) `--glow-*`(状态柔光) `--tint-*`(渐变叠层) `--inset-hi`(顶边内高光) |

- 圆角**只允许**用 `--rs/--rm/--r/--rl/--pill` 五档（`50%` 仅用于真圆）—— 见 §4.5 的归并表。
- 改色只改 `:root` 与 `html.light`，不要在组件里打补丁。
- `--fill-*` / `--shade-*` 是**逐值等价**收敛来的（原为 36 处重复 rgba 字面量）；
  新增档位请新增 token，不要把不同 alpha 合并成一档（那会改视觉）。
- **硬性红线：组件里不得出现 `rgba(255,255,255,*)` / `rgba(0,0,0,*)` 字面量**。
  这类"白叠加 / 黑叠加"在浅色下方向要反转：写死会在白底上**彻底消失**（如十字线、转圈轨道、
  白叠加填充）或**过重**（如模态遮罩）。要用就建 token 并在 `html.light` 给反转值。

### 4.1 主题（暗色 / 亮色 / 跟随系统）

- 机制：只切 `<html>` 上的一个类名 `light`，**视觉全部由 token 层响应**，组件不分主题写两套样式。
- 首帧类名由 `head.html` 的**同步内联脚本**按偏好（`aigate.theme`）写下 —— 必须早于渲染，
  否则会先闪一下暗色；后续切换走 `ds-core.js::setTheme / applyTheme / restoreTheme`。
- 新增颜色 token 时**必须同时在 `html.light` 给出覆盖值**，否则浅色下静默继承暗色值
  （最典型：`--fill-*` 忘了覆盖 → 浅色下仍是白叠加 → 相关控件全部看不见）。
  唯一允许不覆盖的是与主题无关者：字号 `--fs-*`、间距 `--sp-*`、圆角 `--r*`/`--pill`、
  时长 `--dur*`/`--ease-out`、奖牌色 `--rank-*`、**字体族 `--font-ui`/`--font-mono`**。
  （`theme_complete.py` 的 `ALLOWED` 里对应 `--font-ui`/`--font-mono` 两条。）
- **刻意不覆盖**的两类：品牌 logo 与 `CHART_PALETTE` / `CHART_SEMANTIC` 数据系列色 ——
  它们在两种主题下都可读，属"数据"而非"主题"；只有图表底板 `--chart-rest` 随主题走。
- 对照度要求：前景/背景组合需满足 WCAG AA（正文 ≥4.5，次要文字与徽标 ≥3.0）。
  半透明 token 会被按其上层底色合成后再算 —— 改浅色配色时照此复核。

### 4.1a 字体：内嵌 Inter（**不做苹果感就靠它**）

面板**不引任何外链字体**，而是把 **Inter 内嵌进二进制**（`src/admin/font.css`，变量字体 latin 子集，
48KB woff2 → 65KB base64）。它由脚本生成，**不要手改 base64**。

**为什么必须内嵌**：Windows 上 `system-ui` 只会落到 **Segoe UI**，与苹果的 SF 差得远 ——
光靠系统栈**永远做不出苹果观感**。Inter 是按 SF/Helvetica 血统设计的公认网页替代，OFL 许可可内嵌。

- 字体族**只在 `base.css` 的 `:root` 定义两行**：`--font-ui`（**首位 `'Inter'`** → 系统栈兜底 →
  显式 CJK 回退）与 `--font-mono`（**只给代码类内容**：`ui-monospace` → `SF Mono` → `Cascadia`/`Consolas`）。
  组件里一律 `var(--font-ui)` / `var(--font-mono)`，**不得再写字面量**。
- **中文继续走系统 CJK 字体**（Windows=微软雅黑 UI，macOS=PingFang）—— 与苹果自身做法一致
  （SF 管拉丁、PingFang 管中文），且避免中文全量字体带来的数 MB 体积。
- ⚠️ **数字不要用 `--font-mono`**（这是本轮的重要修正）：苹果的数字排版是 **SF 的 tabular figures**
  —— 比例字 + `font-variant-numeric:tabular-nums`。等宽字是"终端/机器"的语言，全站数字用等宽
  正是面板读起来"俗"的主因之一。`--font-mono` 只留给端点 URL / HTTP 方法 / 模型 ID / JSON / `.mono`。
- `font:` **简写**里也可直接用（`font:700 var(--fs-kpi)/1.15 var(--font-ui)`）—— 全部 var 替换，
  要么整体生效要么整体失效，故这两个 token 必须始终定义在 `:root`。
- 内联 SVG 的 `font-family` **属性不能写 var()**（`core.js::_plogo` 的兜底首字母徽标），
  故那里写的是展开后的系统栈字面量 —— 这是唯一的例外，改字体栈时需同步。
- **新增切片要登记三处**：`admin.rs` 的 `concat!`、`split_manifest.json`、本文档 §1 的目录表。
  `verify_parts.js` 会校验 include 顺序与 manifest 一致（拼接顺序即字节序）。
- 若将来要换字体：改 `font.css` 的生成脚本重出 base64 即可；体积敏感的话优先缩子集范围。

### 4.1b 画布层次、氛围渐变与"选对语义层"（Apple）

`--bg` 是"画布"，`--card` 是"卡片"，`--elevated` 是"浮层"。取值直接对齐 Apple 系统语义色：

| 档 | 暗色（macOS 窗口） | 亮色（分组背景） |
|---|---|---|
| `--bg`（画布） | `#1C1C1E` | `#F2F2F7` |
| `--card`（卡片） | `#2C2C2E` | `#fff` |
| `--elevated`（浮层） | `#3A3A3C` | `#fff` |
| 嵌套块 `--card-nest` | `#3A3A3C` 半透明 | `#F2F2F7` 半透明 |

⚠️ **"苹果真值"有多个语义层，选错就会难看** —— 这是本轮踩过的坑：

- `#000` 是 **iOS 分组表**的语义（黑底 + `#1C1C1E` 分组块）。照搬到**桌面控制台**上会显得硬，
  而且"底 vs 卡"只差一档，页面空处全黑压得很重 —— 这正是上一版被判"一般般"的根因。
- 桌面应用该用 **macOS 窗口**语义：`#1C1C1E` 底 + `#2C2C2E` 卡 + `#3A3A3C` 浮层。
  两条都是苹果真值，差别只在**用在哪**。
- 改 `--bg` 时**必须同步 `--hdr-bg` / `--sb-bg` / `--card-nest`** —— 这三者是与画布同色的半透明叠加，
  不改就会"顶栏比画布浅、嵌套块比卡片浅"，层次立刻反掉。

**氛围渐变**（`--amb`）：`body` 铺三段低饱和径向渐变（蓝/紫/青）。它承担两个职责：

1. 苹果的"桌面/壁纸"底 —— 毛玻璃是"透过它看到后面的东西"；
2. **让玻璃成立** —— `backdrop-filter` 只有在"背后真有明暗与色相变化"时才有视觉意义。
   若画布是**纯色**，模糊一个纯色 = 零可见效果、只付 GPU 成本（本项目早期就是如此）。

- 用 `background-image` 而非额外 DOM：零元素、零重排、随主题走一个 token。
- **alpha 调过两轮，最终定在暗色 `.20/.15/.13`、亮色 `.17/.13/.11`**（范围 1500/1300/1400px）：
  第一版用 `.20` 时读起来像"AI 光斑"（范围小 + alpha 高 = 聚光灯），压到 `.10` 后又几乎看不见、
  背景发死。**结论：范围要大、alpha 要中** —— 大范围 + 中等 alpha 才是"环境光"，小范围高 alpha 就是光斑。
- ⚠️ 不要在画布上再叠其它装饰（噪点/网格）—— 早期铺过工程网格，母题与产品不符且把背景读成"图纸"。
- `--glass-blur` 提到 `blur(30px) saturate(180%)` —— 苹果材质的模糊半径更大且**提饱和**。
- ⚠️ **粘性元素（`position:sticky`）的底色不能用实色 `var(--bg)`**：画布是氛围渐变，实色底会把
  它整块切断、形成一条横贯的硬边暗带（设置页 `.st-nav` 实测报障）。一律走与顶栏同一材质：
  `background:var(--hdr-bg)` + `backdrop-filter:var(--glass-blur)`。
- ⚠️ **介绍页 `site/index.html` 与演示稿 `docs/product-deck.html` 的语义 token 与本文件同源**
  （表面 / 文本 / 描边 / 强调 / 状态 / 材质 / 氛围 / 阴影 / 几何 / 动效 / 字体族）—— 两者都刻意
  **不搬**面板的 `--sp-*` / `--fs-*`（尺度不同，各自列在「落地页专属」/「演示稿专属」段里；
  演示稿另把 `--fs-h1/h2/claim/lead/kpi/fig` 收成投屏档，单处微调的小尺寸才留字面量）。
  改上面任一组 token 时**三处都要改**，否则又会回到「产品是苹果风、介绍页/演示稿不是」的老问题
  （2026-09-29 已因此返工一次）。
- ⚠️ **演示稿的装饰层超标最严重，2026-09-29 一并按本节判据收敛**（判据 = 「不要在画布上再叠其它装饰」
  + 「小范围高 alpha 是光斑」）：删除 `.fx-mouse-glow`（跟随鼠标的高饱和光晕）、封面自带的 `.mesh`
  （三块渐变色块聚光灯）与 `.grid-bg`（工程网格）；`h1` 去渐变文字、`.pg` 与进度条去渐变字/扫光、
  `.nm` 去旋转锥形光晕、数字全部去等宽字改 `tabular-nums`；章节纹理从红/青/紫/琥珀/绿五套高饱和
  收成中性发丝线 `--tex`（只留图案差异，保住 reduced-motion 下的章节可辨性）；canvas 章节色相收敛
  到与 `--amb` 同源的蓝/紫/青三色族，`al` 由 `.30` 收到 `.17~.22`，星尘 alpha 由 `.35~.80` 收到 `.10~.28`。
- ⚠️ **`theme_dark_diff.py` 的口径**：它按"零视觉变化"设计，用于判断"只换写法没动视觉"。
  在**刻意**改视觉的轮次（本轮的品牌色/中性色/层级/材质）它会大面积报差异 —— 这是预期内的，
  判据是**逐条确认差异是否都是本轮的刻意项**，不是要求它零差异。

### 4.1c 容器描边：用 `--surface-border`（Apple）

**苹果的分层靠填充差，容器本身不画边框**（白卡放在 `#F2F2F7` 上、`#2C2C2E` 放在 `#1C1C1E` 上，
本就分得清）。描边只出现在两处：**控件**（输入框/下拉/胶囊）与**行与行之间的发丝线**。

- 因此新增一个 token：`--surface-border`（暗色 `rgba(255,255,255,.09)` 收边，亮色 `transparent`）。
- **容器类用 `--surface-border`，控件类继续用 `--border`** —— 二者不要混用。
  已切换的 19 个容器类：`.pnl` `.hero` `.hero-cluster` `.ov-kpi` `.ov-summary-card` `.ov-secondary-item`
  `.ov-health-item` `.an-stat-strip` `.an-performance` `.rec-stat` `.set-row` `.plaza-card` `.skel-card`
  `.m-card` `.modal-card` `.log-modal-card` `.si` `.sc` `.about-capability`。
- 这是"盒子感"消失的直接原因 —— 改造前 40 处 `border:1px solid var(--border)` 里约一半是容器。

### 4.2 表面质感与视觉原语（`components.css` 末段）

设计语言：**玻璃卡片 + 阴影分层 + 状态柔光**。
原设计是"纯色块 + 1px 边框"，层级只能靠边框区分，扁且无纵深。

> **品牌色 = Apple systemBlue**：`--primary` 是**填充面**（按钮底）—— 苹果原值 `#0A84FF` 配白字只有
> 3.6:1 不达 AA，故压深到 `#0b6fe0`（暗）/ `#0071e8`（亮），观感仍是苹果蓝。
> `--accent` 是**文字/图标/线**，用苹果原值 `#0A84FF` / `#007AFF`。**二者刻意不同深，不要合并**。
> 靛蓝 `#6366f1` 已不再当品牌色（它曾是 AI 生成界面的招牌色），降级为 `--secondary` / `--info` 语义色。
> 状态色也全部换成苹果系统色（systemGreen / systemOrange / systemRed / systemTeal）。

| 原语 | 用途 |
|---|---|
| `.pnl` `.ov-kpi` `.ov-summary-card` `.plaza-card` | 玻璃表面（`--card-glass` + `--glass-blur` + `--elev-1`） |
| `.ov-summary-card` `.ov-secondary-item` `.ov-health-item` | **嵌套块**用 `--card-nest`（比卡片更深一档，形成内凹） |
| `.lift` | 悬停抬升：位移 1px + 阴影升档（位移刻意极小，列表里位移过大会晃眼） |
| `.hl` / `.hl-ok` / `.hl-warn` / `.hl-err` | 选中/激活态：彩色描边 + 同色柔光（只换边框色看着仍像静态） |
| `.tint` / `.tint-primary` / `.tint-ok` + `.tint-on` | 高亮卡片的同色淡渐变叠加（需 `position:relative` 容器 + 作首个子元素） |
| `.ibox` / `.ov-icon` / `.plaza-ic` | 图标方块（语义色底 + 居中字形 + 悬停微缩放） |
| `.pbar` | 进度条原语（轨道 + `>i` 填充 + 宽度过渡） |
| `.icon-btn` | 图标按钮（必须配 `title` + `aria-label`，否则只剩符号不可读） |

- 图标方块的内高光走 `--inset-hi`（暗色白叠加、亮色极淡黑）—— 别写死 `rgba(255,255,255,.06)`。
- 模态/抽屉/价格面板统一 `background:var(--elevated)` + `box-shadow:var(--elev-3)`，入场用 `popIn`
  （`prefers-reduced-motion` 下已关闭）。
- 新增表面类时**照抄这组 token**，不要在组件里另写阴影/模糊：那正是改造前"扁平无层次"的成因。

### 4.3 外壳骨架：顶栏导航（无侧栏）

骨架（对标 cc-switch：顶部导航 + 全宽内容，**没有左侧栏**）：

```
[◆ AIGate v0.6.1] [概览|分析|记录 ‖ 供应商 ‖ 设置|关于]   …弹性…   [主题 ▾][语言 ▾]
─────────────────────────────────────────────────────────────────────────────
main: 全宽内容（.main，自身滚动）
```

- 由 `body-shell.html` 定义；`.hdr` 56px 玻璃底，`.navseg` 是分段胶囊（容器 `--fill-4` + `rounded-12px`，
  激活项 `--card-glass` + `--elev-1` —— 即"胶囊里浮起一枚"）。分组用 `.navseg-sep` 细分隔线，**不用分组标题**
  （横排下太占位）。
- 为什么拆掉侧栏：导航原先独占 200px 一整列纵向空间，而数据面板更缺**横向**空间。
- 窄屏（≤820px）：`.hdr-name/.hdr-ver` 隐藏、`.navseg-i span` 隐藏 → 导航退化为纯图标横排。
- 导航项必须带 `:aria-current="activeTab===X?'page':null"`；`nav` 带 `:aria-label="t('nav_aria')"`。
- **新增导航项时同步四处**：`navseg` 内的 `<a>`、`switchTab` 的分支、页面模板的 `x-show`、i18n 的 `nav.*`。

### 4.4 动效陷阱：`both` 填充会让内容消失

`.stagger>*{animation:...both}` 在动画**未推进**时元素停在 `opacity:0`（`both` = 开始时也用首帧）。
实测：headless 的不可见 iframe 里整块内容肉眼不可见；后台标签页被节流时同样有风险。
**结论：不要给数据密集的列表/卡片新加 `stagger`**；确需入场动效时优先用不带 `both` 的写法。
（概览页 `.ov-side` 的既有用法保留，但别扩散。）

`.page-enter` / `.fade-in` 就属于"不带 `both`"的写法，也**不要给它加 `both`**：页面根长期挂着
`transform` 会变成 fixed/absolute 后代的包含块，浮层会跟着根一起位移。

### 4.4a 数据驱动动效（JS）的三条硬约束

CSS 那套（`.page-enter` / `.stagger` / transition）只管**结构性**动效；"数据变了"引发的动效
在 JS 里，规则不同，且**不受 `@media(prefers-reduced-motion)` 管辖**：

| 约束 | 原因 | 落地位置 |
|---|---|---|
| **禁用 `requestAnimationFrame`** | 后台标签/headless 里 rAF 会停摆，补间卡在半途且永不收尾 | `ds-fmt.js::_numTweenRaw`（`numTween` 是其取整包装）用 `setInterval` + 按真实流逝时间求进度（被节流也会自愈落终值） |
| **时长 ≤400ms** | 概览按 3s 轮询；补间更长就永远追不上下一次更新，变成"数字一直在动" | `numTween` 固定 380ms |
| **JS 自己判 `prefers-reduced-motion`** | CSS 的降级规则管不到 JS；靠它才能让残障用户不被强制动画 | `core.js::prefersReducedMotion()`，`numTween` / `_setTrendHtml` 各自过一遍 |

另外两条同源的注意点：

- **初值即真值**：`numTween` 只在"目标变了"时才补间，首帧直接显示真实数字 ——
  否则每次开面板数字都从 0 滚起，读起来像加载动画。
- **`x-html` 整段替换无法靠 CSS 过渡**：趋势图的交叉淡入由 `ds-trend.js::_setTrendHtml` 打
  `trendFading` 标实现，且**只在内容真变了才播** —— 3s 轮询下数据常常没变，少了判据就是每 3s
  一次频闪。透明度底也刻意压在 `.45`（不是 0），避免"淡到全黑再淡回"。

**幅度地板（实测教训）**：写动效时容易只关心"有没有"，而"看不看得出来"取决于**幅度 × 时长**。
本轮踩过并被用户实测反馈的两个值，别再回退：

| 项 | 回退到 | 为什么读不出来 | 现取值 |
|---|---|---|---|
| 入场位移 | 4px | 60Hz 屏一帧约 8px，"动了一帧"≈没动 | 8px |
| 按钮按下 | `scale(.97)` | 30px 高按钮只有约 1px 收缩 | `translateY(2px) scale(.94)` |
| 开关过冲 | 过冲 1.56 / 240ms | 过冲段仅约 70ms、幅度约 2.3px | 过冲 1.8 / `--dur-spring` .32s |

另有一类"看起来没生效"其实是**条件触发**：数字补间 / 趋势淡入 / 新日志行闪烁都只在数据变化时播，
面板不跑流量时它们**永远静止**。判断"动效坏了"之前先确认触发条件是否满足。

**把静默动效变成可感知**（五条已落地的通路，改这些地方时别把它们弄丢）：

| 通路 | 机制 | 为什么需要 |
|---|---|---|
| 手动刷新重播 | `_numReplayGen` 代数 +1，`numTween` 发现自己的 `gen` 落后就从 0 重滚 | 刷新后数据常常没变，否则点了刷新毫无数字反馈 |
| 切范围/图型重播 | `_trendForceFade` 标记，`_setTrendHtml` 消费；绕开"内容相同就早退" | 用户刚点了控件，内容碰巧同形也必须给反馈 |
| 切页签重播 | 概览/分析走 `fetchStats(range, manual, replay)` 的 `replay` 标（与 `manual` **分离**：只重播、不弹骨架）；记录页统计来自 logs，改在 `logsFetched` 已为真时补 `replayNumTweens()` | 补间槽是持久对象（`x-show` 切 `display` 不销毁 DOM），切页签目标值没变就永不重播 |
| 零值占位 | `numText(key, value, fmt)` 统一出口：值 ≤0 给 `—` 并 `_numSettle` 落值 | 口径"无数据 ≠ 0"；顺带避免归零后从**中间值**起滚 |
| 服务心跳 | `.hb` 低频呼吸（2.6s），"服务运行中"徽章用 | 常驻活跃读数 |

- **代数必须用数字而非布尔**：布尔会被第一个消费它的 key 关掉，其余 key 全部漏掉重播。
- **`_numSettle` 是必需的**：不给归零落值，补间槽会停在旧数字上；实测归零后立刻恢复，
  会从中间值起滚（8223→5000）而不是从 0 起滚。
- **语义值必须走 `_numTweenRaw`（不取整）**：`numTween` 会 `Math.round`，直接用于
  成功率 / 延迟 / 速度 / 金额会把 76.9% 显示成 77% —— 改掉事实，与 `fmtPct` 的舍入红线冲突。
  `numTween` 现为 `_numTweenRaw` 的取整包装；语义值配各自既有格式化函数（`fmtPct` / `fmtMs` /
  `fmtMoney` / `toFixed(1)`），保证**终值与不做补间时逐字节一致**。
- **语义值另有模板级出口 `numTextFmt(key, value, fmt)`**：`fmt` ∈ `int/num/t/money/pct/dec2`。
  与 `numText` 的分工是**不把 0 当"无数据"**（0 就是 0 —— 记录页"窗口请求 0 条"是事实，不是缺数据），
  且支持小数。分析页 5 项、记录页 4 项统计都走它。
- **心跳与"禁止环境动效"的边界**：看板规范禁止的是**纯装饰**的环境动效（背景流光/粒子）。
  心跳承载真实语义（进程存活）、幅度极小、位于页头角落，属"状态指示"而非装饰 —— 这个区分要守住，
  别再往页面上加类似的东西（数字 odometer 滚动、图表脉冲、背景流动光效都在禁止之列）。

### 4.5 尺寸体系：字号 / 间距 / 圆角

**字号地板是"看起来像老旧密集后台"的头号原因**，改质感与骨架都压不住。历史上做过两轮：
先整体上移一档（最小档 9→11px、正文 14→15px），本轮再**把层级拉开**。

**当前字阶（本轮最终）**：

| token | 值 | 用途 |
|---|---|---|
| `--fs-9` | 11px | 极小徽标 / kbd / 图表环心标签 |
| `--fs-10` | 11.5px | 次级小标签 / 端点方法 / 图表轴标签 |
| `--fs-11` | 12.5px | **标签主力档**（引用最多的一档） |
| `--fs-12` | 13px | 次要文字 / 按钮 / 输入框 |
| `--fs-125` | 13.5px | 微调档 |
| `--fs-13` | 14px | 表格单元格 / 正文（列表区） |
| `--fs-14` | 15px | **body 基准** |
| `--fs-15` | **20px** | **面板标题 / 次级数值** ← 本轮 16→20 |
| `--fs-18` | **26px** | **页面标题 / 关于页标题** ← 本轮 19.5→26 |
| `--fs-kpi` | **34px** | KPI 主数字 ← 本轮新增 |
| `--fs-hero` | **48px** | Hero 主数字 ← 本轮新增 |

- **为什么把 `--fs-15`/`--fs-18` 提这么多**：改造前它们是 16 / 19.5px，而正文是 15px ——
  **面板标题只比正文大 1px**，`--fs-18` 与正文（15）之间也几乎没有梯度。中间层整体塌陷，
  层级只在"页面标题 / Hero 数字"这两处突然跳出来。现在 15 → 20 → 26 → 34 → 48，
  **跨度 3.2 倍**（改造前 2.3 倍）。
- **`--fs-9`…`--fs-13` 刻意保持不动** —— 它们占全部字号引用的 **93%**，是密度所在；
  动它们等于重排全站（收益低、风险高）。层级问题出在标题区，就只修标题区。
- **主数字另开两档**（`--fs-kpi` / `--fs-hero`）而不是复用正文档：数字与文字是两个轴，
  混用会让"改正文字号"顺带改掉所有大数字。
- **图标字形走图标轴 `--icon-sm/md/lg`（16/22/30px），不进字阶** ——
  否则调标题会连带放大图标方块里的字形。
- 硬编码字号已清零（除 `font:` 简写）—— **CSS 里不许再写 `font-size:Npx`**，一律用 token。

**几何：圆角收敛为 4 档 + 胶囊**（取值对齐 Apple 的连续圆角观感）：

| token | 值 | 用途 |
|---|---|---|
| `--rs` | 6px | 输入框 / 小徽标 / 行内代码 / kbd / 细指示条 |
| `--rm` | 10px | 下拉 / 小图标方块 / 小容器 / 版本徽标 |
| `--r` | 14px | 嵌套块 / 按钮 / 表格容器 / 提示气泡 / 中号图标方块 |
| `--rl` | 18px | 面板 / 主卡片 / 弹窗 / 抽屉 / Hero / 设置行 / 大图标瓦片 |
| `--pill` | 999px | 开关轨道 / 进度条 / 胶囊徽标 / 滚动条 |
| `50%` | — | 仅真圆（圆点），**不参与本尺度** |

> 取值演进：`7 → 4/6/8/10 → 6/10/14/18`。最后一档是苹果化时调的 —— 小控件仍偏方、容器更圆，
> 比 `4/6/8/10` 柔和得多。**改这一组会全站生效**，不要再往组件里写圆角字面量。

- 改造前全站有 **17 种以上**圆角字面量（2/5/6/7/8/9/10/11/12/13/14/17/18/99/999px…），
  只有 12 处走 token —— **没有几何语言**正是"看起来不成体系"的直接原因。本轮归并了 80 处。
- `--rm/--r/--rl` **不再是 `--rs` 的 calc 派生**，改为各自独立取值（便于按档微调）。

配套：
- 面板内边距 16 → 22px；表格单元格 11 → 14px 内边距、字阶升到 `--fs-13`。
- `.main` 留白 `28px 32px 40px`。
- 数字输入框隐藏原生上下箭头（`.inp[type=number]`）：它随系统主题变样式、在深色面板里显脏。
- **长文本说明用 `.prose-note`**（`max-width:76ch` + `line-height:1.8`）—— 整幅宽面板里
  若让口径/免责段落铺满 1400px，一行 90+ 字，眼睛会跳行。

**注意**：字阶已不再是"逐值等价"的收敛（那条注释是拆分期的约束，现已作废）。

### 4.6 截图验证工作流（headless Edge）

```powershell
python .openbitfun\tmp\build_preview.py              # 拼接 + fetch 桩 + 生成 ?tab 变体
python .openbitfun\tmp\mkwrap.py                     # 生成 wrapper (设主题 + 100% 宽 iframe)
python .openbitfun\tmp\shoot.py s_dashboard s_records --h=1700   # 截图到 .openbitfun/tmp/shots/
python .openbitfun\tmp\shoot.py s_dashboard --w=780 --as=narrow_ # 窄屏 (前缀避免覆盖)
```

三条必须记住的约束（都踩过）：
1. **服务与浏览器必须在同一进程内跑完** —— 沙箱会在命令结束后回收子进程，跨调用复用实例必然失败。
2. **页签切换不能靠载入后点侧栏** —— headless 下 iframe 不可见时响应式刷新被节流，
   Alpine 处理了状态变更但 DOM 不更新。正解是 `?tab` 变体让页面**以目标页签启动**。
3. **`?tab` 变体必须保留一次 `fetchStats()`** —— 原 init 里 `switchTab('dashboard')` 还负责首次取数，
   直接换目标会跳过它，首载完成逻辑不跑、`loading` 恒为真，页面永远停在骨架。

### 4.7 页面级组件模式

| 位置 | 模式 | 要点 |
|---|---|---|
| 概览页 | `.hero` 指标带 | 主数字 + `≈` 副档 + `.hero-cluster`（竖分隔指标簇）+ 全宽 `.hero-spark` + `.hero-stats`（`.mini-stat` × 4） |
| 设置页 | `.set-row` 卡片 | 只改 `.set-row` 自身 + `:has()` 让只装开关行的 `.pnl` 透明，**不改 17 处行结构** |
| 供应商抽屉 | `.dr` 全屏面板 | 头/底栏玻璃化；`.dr-inner` 把表单类内容收在 1400px（超宽屏下文字不会拉成一行） |
| 空态 | `.empty-*` | 56px 圆角图标方块 + 标题 + 描述 + 按钮 |

**新增 `:has()` 用法前先确认它在本项目可用**（设置页 P3 起已在用，Chrome/Edge 105+ 支持）。
用 `:has()` 做"条件性降级"（如"含开关行的面板不画框"）比改几十处 HTML 更稳。

### 4.8 浮层必须在 `x-data` 之内（硬性约束）

**`</main></div>` 由 `panel-price.html` 收尾，不要在 `footer.html` 里提前关闭。**

Alpine 从 `x-data` 根往下遍历，**框外的元素根本不被处理** —— 不是"降级"，是完全不工作：
`x-if` 不求值、`x-show` 不生效、`x-text` 不填。后果是整个浮层消失且**没有任何报错**。

拼接顺序（`admin.rs`）决定的现实是：`footer.html` 之后还有
`modal-whatsnew-recall` / `modal-add-fetchdiff` / `drawer-provider` / `panel-price` 四个 HTML 部件，
以及（现已移入的）Toast 层与确认弹窗。因此关闭标签必须放在**它们之后**：

```
… 各页模板 → footer → 四个浮层部件 → Toast + 确认弹窗 → </main></div> → <script> → 全部 JS → </script>
```

- 新增浮层 HTML 时**放在 `panel-price.html` 里**（它是 HTML 段的最后一件），别放进 `tail.html`
  —— `tail.html` 在 `</script>` 之后，放那里等于又跑出作用域。
- 自查：`document.querySelector('<你的浮层>').closest('[x-data]')` 必须非空。
- 浮层用 `position:fixed`，移入 `<main>`（`overflow:auto`、无 `transform`）不影响定位；
  但**若将来给 `.main` 或其祖先加 `transform`/`filter`/`will-change`，fixed 会改为相对该祖先定位** —— 届时需重新评估。

### 4.9 不要给同一元素同时用 `x-text` 与子元素

`x-text` 执行 `el.textContent = value`，**会销毁全部子节点**。写法如
`<th x-text="标签"><span x-text="图标()"></span></th>` 有双重后果：
① 图标被抹掉（用户可见：排序箭头永远不显示）；② 被抹掉的子元素脱离文档后**失去 Alpine 作用域**，
求值抛 `ReferenceError`（实测每次加载 7 个未捕获异常）。

规则：需要"文本 + 子元素"时，把文本也放进自己的 `<span>`，父元素不写 `x-text`。

### 4.11 部件拼接是一份文档：标签必须逐文件配平

全部 HTML 部件由 `concat!` 拼成**一份文档**，所以**一个文件里未闭合的 `</div>` 会吞掉其后所有页面**
（实测：分析页一个未闭合的 `g2 stagger` 导致关于页整体发暗、设置页层次错乱）。

改完 HTML 必须跑 `python .openbitfun\tmp\balance.py`：逐文件比对 `<tag` 与 `</tag>` 计数。
只有 `body-shell.html`（开 `<main>`）与 `panel-price.html`（关 `</main>`）跨文件，其余文件必须为零差。

**插入容器时按标签计数校验，不要按字符串后缀判断** —— 曾因为"只给最后一格补了 `</div>`"
而遗漏 4 处，触发上述污染。

### 4.12 图表：外观走 CSS 类，只有数据系列色用内联

SVG 外观（网格、坐标文字、环底、环心文字、圆点描边、汇总扇区）**一律用 CSS 类 + token**，
不要写死十六进制色 —— 写死会在浅色主题下失效，且切主题必须重算 SVG 才变色。

| 类 | 用途 |
|---|---|
| `.chart-grid` | 坐标网格（虚线，`--border`） |
| `.chart-base` | 贴合 0 的基线（`--border-s`） |
| `.chart-axis` | 轴标签（`--muted`，等宽 + tabular-nums） |
| `.chart-dot` | 趋势圆点（用 `--card` 描边"打孔"浮出） |
| `.chart-track` / `.donut-total` / `.donut-sub` / `.chart-rest-fill` | 环图轨道 / 中心数字 / 中心标签 / 汇总扇区 |

**只有数据系列色保留内联属性**（它由 `CHART_PALETTE` / `CHART_SEMANTIC` 决定，是数据不是主题）。

**取色原则**：`CHART_PALETTE` **直接采用 Apple 系统色**（iOS/macOS 真值），首位是 systemBlue
—— 与品牌强调色同源，主序列因此天然带上品牌色。苹果这套系统色本身就被设计成"彼此可区分且和谐"，
比自造调色板更稳；前后相接处刻意选远色相（蓝↔橙、紫↔绿、粉↔青）。
`CHART_SEMANTIC`（省量/缓存语义色）必须**一起改**，否则同一语义在环图与趋势图上会撞色。

**迷你趋势线（`sparklineSvg`）**：外观走 CSS 类 `.spk-line` / `.spk-stop`，取色用 `--spk`（默认 `--accent`）。
⚠️ 这是踩过的坑：SVG **呈现属性**（`stroke=` / `stop-color=`）**不解析 CSS 变量**，
所以改造前调用点写死了色值（旧品牌紫）—— 换主色后那条线不会跟着变，属于"看着没改"的隐性缺陷。
现在元素只挂 class，第 2 参（可选）用于覆盖 `--spk`。

**多序列层次**：`computeModelTrend` 里 `keys` 已按用量降序，故 `si===0` 即主序列：

| 序列 | 描边不透明度 | 面积不透明度 |
|---|---|---|
| 主序列（si=0） | `1` | ≤2 系列 `.22` / ≤3 `.14` / 更多 `.10` |
| 其余 | `.78 − si*.09`，下限 `.42` | ≤2 `.13` / 更多 `.045` |

- **线宽仍恒为 2px**（`.chart-line` + `vector-effect:non-scaling-stroke`），层次只靠不透明度表达 ——
  这是刻意的：改线宽会让同一张图里"哪条是主线"依赖像素粗细，缩放下不可靠。
- 只有主序列给到可感知的"面"，其余压到很淡 —— 多层渐变叠加会把整图糊成色块。

两条经验：
- **不画静态圆点**：点位交给悬停十字线 + 浮层（见 §4.14）。每桶每系列画点会把曲线淹没成散点图。
- **面积不透明度随系列数自适应**（数值见上表）：多系列同以高不透明度叠加会互相压暗、发浑。
  少系列给足"面"的存在感，多系列压淡让"线"承担可读性。

### 4.13 品牌图标：用真实资产，不要占位符

- **顶栏与关于页的品牌图标来自 `brandLogoSvg()`**（`core.js`），与仓库根目录的 `icon.svg` 同源
  —— 网关枢纽造型（中心节点 + 四向连接），恰好对应"AI Gate"这个产品。
- 原先这两处用的是**文字符号 `◆`** 占位 —— 项目本就有正式图标，占位符没有任何理由留着。
- **内联 SVG 而非 `<img>`**：零额外请求、可直接用 CSS 控尺寸。
  ⚠️ 渐变 `id` 用自增序号（`_brandSeq`）保证唯一 —— 同页会出现两次（顶栏 + 关于页），
  重复 id 会让第二处引用到第一处的 def（颜色相同故不显，但属脏写法）。
- 容器（`.hdr-logo` / `.about-logo`）**只负责尺寸与圆角**，不要给它 background/color/font-size ——
  SVG 自带圆角方块底与配色；容器圆角要与 SVG 里的 `rx` 对齐，外阴影才贴得住图形轮廓。
- **改图标配色时三处要一起改**：`brandLogoSvg()`、`icon.svg`、并**由 `icon.svg` 重生成 `icon.ico`**
  （Edge 渲染 SVG → PIL 打包多尺寸）。否则会出现"面板是蓝的、任务栏是紫的"这种不一致。

### 4.14 趋势图的形态约定（对齐 cc-switch）

- **不画静态圆点**。点位交给悬停十字线 + 浮层；点多了会把曲线淹没成散点图。
- **网格只横向、虚线、低不透明**（`.chart-grid`：`dasharray 3 3` + `opacity .45`）。
- **轴标签 11.5px 无衬线 + tabular-nums**，不要轴线与刻度线。
- **图例底部居中，色块用短线段**（`14×3px` 圆角条）而非圆点 —— 与折线语义一致。
- **面积不透明度按系列数自适应**，且**主序列与其余分档**（完整数值见 §4.12 的表）。
  目标观感是"几乎只剩线"：cc 用 `0.2 → 0` 止于 95%，起跳过高会让多系列叠成色块、压掉曲线脉络。
- ⚠️ 改 `viewBox`（`W/H/left/right/top/bottom`）时**必须同步 `onTrendHover` 里的坐标反算**，
  否则悬停十字线偏位。两处目前是：画布定义 与 悬停换算各一份 `W=900, left=56, right=20`。

## 5. 状态色与分级（单一事实源）

- `bcls(tone)` / `bcolor(tone)` / `statusTone(tone)`（`core.js`）：tone ∈
  `ok|warn|err|info|neutral` → 徽标类 / 圆点色。**不要手写 `:class="x?'bdg-s':'bdg-e'"`**。
- `rateTone(pct)`：成功率分级（100/≥90 ok、≥70 warn、其余 err）。同一数字在任何页面同色 ——
  历史缺陷「概览按阈值变色而记录页写死绿色，全错时显示绿色 0%」不得重演。
- `CHART_PALETTE`（按序取色）/ `CHART_SEMANTIC`（语义色，与文案一一对应）：
  图表序列一律从这里取；供应商 logo 兜底色 `PROVIDER_LOGO_PALETTE` 属品牌标识，单独维护。

## 6. 数字口径红线

1. **不改任何统计口径**。没有数据 ≠ 0：空值显示 `—`，不要显示 `0` 或 `100%`。
2. 百分比一律走 `fmtPct`（非满值不显示 100%、非零不显示 0.0%、无数据显示 —）。
3. 延迟/耗时走 `fmtMs`（`null/undefined/0/NaN` → `—`）。
4. **口径必须可见，且要选"不受存储上限截断"的来源**（本条踩过两次，务必读完）：
   - 「同一面板内数字与图形同口径」这条原则本身没错，但它**推不出**"所以主数字就该用窗口累计"——
     因为**窗口源自 `logs.jsonl`，而该文件有 `store::MAX_LINES = 5000` 行滚动上限**
     （`store.rs` 的 `rotate()` 只保留最近 5000 行）。于是窗口口径的「请求」会**永久饱和在 5000**，
     看起来像 bug，实为截断。**任何"累计/总计"类指标都不能从日志文件现算。**
   - 概览 Hero 现已切到**今日口径**，数据来自 `stats.session.today`
     （后端在 `LogBuffer::push` 写入路径**原子累加、跨日归零**，与文件上限彻底解耦）。
   - ⚠️ **仍无法切今日的两项**：「平均延迟」「生成速度」需要逐条日志聚合，计数器里没有累加器，
     故仍是窗口口径。这是**当前口径的真实边界** —— 不要用注释假装统一（历史上正是这样写的，
     注释声称"数字与图形同口径"，实现却早已不是）。
   - 折线保持窗口口径（它是趋势形态），**并在主数字提示行显式标注窗口合计**，让两个口径都可见。
5. 改完视觉务必做 **A/B 计算样式比对**：同桩数据、同尺寸，逐元素逐属性比
   `backgroundColor/color/border*/radius/fontSize/...`。纯重构要**零差异**。

## 7. Alpine 使用陷阱（都踩过）

1. **`<template x-for>` / `<template x-if>` 严格单根**：并列两个根元素 → 第二个永不渲染。
   需要多元素就包一层 `<span>`/`<div>`。
2. **字符串型 `:style` 会整串替换 `cssText`**：同元素上的静态 `style` 会被静默清空。
   一个元素只写一个 `:style`，把静态部分拼进表达式。
3. **`x-show` 隐藏的元素仍在 DOM 里**：不要把"隐藏分支"当作不存在（会读到残影文本）。
   需要真正替换时用 `x-text`/`x-if` 表达式切换，而非两个并列元素 + `x-show`。
4. **`getBoundingClientRect()` 的 `top` 是相对视口**：判断"是否在容器内可见"必须与
   容器 rect 相减，不能只看绝对值（会把"已滚到底、目标可见"误判成"被遮挡 1341px"）。
5. **`offsetParent` 对 `position:fixed` 元素恒为 `null`**：判断浮层显隐用
   `getComputedStyle(el).display`。
6. 复制/剪贴板：一律用 `copyToClipboard()`（含 `execCommand` 回退）——
   面板可能以 `http://局域网IP` 打开，非安全上下文下 `navigator.clipboard` 不可用。
7. 焦点归还：开浮层时存 `document.activeElement`，关闭时 `$nextTick` 里 `focus()` 回去。

## 8. 加载态

- 三态由 `loading`（首次加载）+ `skelReady`（250ms 宽限期）+ `_refreshing`（仅手动刷新）
  共同表达：`skelReady` 让快载不闪骨架；`_endFirstLoad()` 保证骨架一旦显示最少驻留 1s；
  后台轮询**不置位**这两个标记 —— 内容永不被清空，无 3s 频闪。
- 骨架 `.skel` / `.skel-card` / `.skel-line` / `.skel-value` / `.skel-row`：
  `prefers-reduced-motion` 下退化为静态底色（`.spin` 与 `.skel` 是唯一保留的动效）。
- 骨架块应与真实内容**布局对齐**，且页头/面板标题在加载期间保持稳定（不闪）。

## 9. 前端偏好持久化

| key | 用途 | 在版本清理白名单 |
|---|---|---|
| `aigate.cache-version` | 版本键（版本变化时清旧 UI 缓存） | 是（自身） |
| `aigate.analyticsSpan` | 分析页时间范围（含 `custom:N`） | 是 |
| `aigate.chartMode` | 图型 line/bar | 是 |
| `aigate.theme` | 主题 `dark`/`light`/`system` | 是 |
| `aigate.pageSize` | 记录页每页条数 | 是 |
| `aigate.logCols` | 记录页列显隐 | 否（可重建，清掉无害） |
| `aigate_drawer_adv` | 供应商抽屉高级端点折叠 | 是 |

新增偏好键：**优先加入 `KEEP` 白名单**（`ds-core.js::init`），否则升级时会被清掉。
读取时一律 `try/catch`（隐私模式抛错）并做**白名单校验**（防手工改坏，
如 `pageSize` 只接受 `PAGE_SIZE_OPTIONS` 内的值）。

## 10. 提交与交付

- 多行提交信息**写文件再 `git commit -F`**（PowerShell 下 `-m` 里的换行会截断参数）。
- 中文脚本**写成文件再跑**（PowerShell 无 heredoc，内联引号必炸）。
- 删除代码按**内容断言**定位，不硬编码行号/区间切片（历史上切掉过整片 CSS 与函数头）。
- 交付 = 重构建 `cargo build --release` + 替换部署 exe（改名再拷，全程不停服务，
  按端口查 PID，**绝不用 `taskkill //IM`**）。
- 面板改动要写 `CHANGELOG.md`；「更新亮点」弹窗只在 `Cargo.toml` 版本号变化时弹，
  故发版要**升版本号**才有人看到。
