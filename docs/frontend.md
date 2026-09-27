# 前端开发规约 (AIGate 管理面板)

> 改前端**之前先读这一份**。这里的每条都对应一次踩过的坑，不是风格偏好。
> 面板是单页应用，**没有构建步骤**：源码是 `src/admin/` 下的切片，由 Rust 端
> `concat!(include_str!(...))` 按固定顺序拼回单串编译进二进制。

## 1. 目录与拼接

```
src/admin/
├── head.html                  <head> + meta 引用 (不含 CSS 内容)
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
cargo test --release && cargo build --release    # ⑦ 247 绿 + 0 警告
```

改颜色 / 主题 / 硬编码色时追加三条（**不需要浏览器**，纯解析即可判定，比肉眼可靠）：

```powershell
python .openbitfun\tmp\theme_complete.py  # ⑧ 浅色完整性: 主题相关 token 是否有浅色取值
python .openbitfun\tmp\theme_dark_diff.py # ⑨ 暗色零变化: 逐文件比对 HEAD 与新版的"实际生效颜色集合"
python .openbitfun\tmp\theme_contrast.py  # ⑩ 浅色可读性: 关键前景/背景组合的 WCAG 对比度
```

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
| 主色 | `--primary` `--primary-h` `--primary-fg` `--indigo` |
| 状态四元组 | `--ok/-bg/-fg/-bd`、`--warn/…`、`--err/…`、`--neutral/…`、`--info/…` |
| 圆角 | `--rs`(母值 6px) `--rm` `--r` `--rl` `--pill`（后三者由 `--rs` calc 派生） |
| 字阶 | `--fs-9` … `--fs-18` |
| 间距 | `--sp-1..4` |
| 动效 | `--dur-fast`(.15s) `--dur`(.2s) `--dur-slow`(.35s) `--ease-out` |
| 遮罩/规则线 | `--overlay`(模态) `--overlay-soft`(轻) `--rule-strong`(图表十字线) |
| 表面与骨架 | `--sb-bg`(侧栏) `--tip-bg`(悬停气泡) `--track`(转圈轨道) `--skeleton` `--sh-sm` |
| 主色软底梯度 | `--primary-soft`(.12) `--primary-soft-deep`(.08) `--primary-badge`(.15) `--primary-soft-h`(.18) `--primary-bd-soft`(.25) `--primary-bd`(.30) `--row-hover`(.04) |
| 其他表面 | `--violet` `--purple-soft` `--tgl-off` `--err-h` `--err-bd-mid` `--warn-bg-strong` `--toast-shadow` `--toast-err-bg` `--toast-ok-bg` `--toast-warn-bg` |
| 顶栏/侧栏 | `--hdr-bg` `--sb-bg` |
| 质感层 | `--card-glass`(玻璃卡底) `--card-nest`(嵌套块底) `--glass-blur` `--elev-1/2/3`(静置/悬停/浮层) `--glow-*`(状态柔光) `--tint-*`(渐变叠层) `--inset-hi`(顶边内高光) |

- 改 `--rs` 会按比例带动 `--rm/--r/--rl`；改色只改 `:root` 与 `html.light`，不要在组件里打补丁。
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
  时长 `--dur*`/`--ease-out`、奖牌色 `--rank-*`。
- **刻意不覆盖**的两类：品牌 logo 与 `CHART_PALETTE` / `CHART_SEMANTIC` 数据系列色 ——
  它们在两种主题下都可读，属"数据"而非"主题"；只有图表底板 `--chart-rest` 随主题走。
- 对照度要求：前景/背景组合需满足 WCAG AA（正文 ≥4.5，次要文字与徽标 ≥3.0）。
  半透明 token 会被按其上层底色合成后再算 —— 改浅色配色时照此复核。

### 4.2 表面质感与视觉原语（`components.css` 末段）

设计语言：**玻璃卡片 + 阴影分层 + 状态柔光**（对标 cc-switch 的 `Card`/`glass` 体系）。
原设计是"纯色块 + 1px 边框"，层级只能靠边框区分，扁且无纵深。

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

### 4.5 尺寸体系：字号 / 间距 / 圆角（全面重做的关键一环）

**字号地板是"看起来像老旧密集后台"的头号原因**，改质感与骨架都压不住。原字阶
`--fs-9:9px` / `--fs-10:10px` / `--fs-11:11px`（11px 被用了 **89 处**），而现代界面正文不低于 12~13px。
故整体上移一档，最小档 11px、**正文基准 15px**：

| token | 原 | 现 |
|---|---|---|
| `--fs-9` / `--fs-10` / `--fs-11` | 9 / 10 / 11 | 11 / 11.5 / **12.5** |
| `--fs-12` / `--fs-125` / `--fs-13` | 12 / 12.5 / 13 | 13 / 13.5 / 14 |
| `--fs-14`（body） / `--fs-15` / `--fs-18` | 14 / 15 / 18 | **15** / 16 / 19.5 |

配套：
- `--rs` 6px → 7px（卡片圆角 12 → 14px，与 cc-switch 的 `rounded-xl` 重量对齐）；
  `--sp` 增加 `--sp-5:20px` `--sp-6:24px` `--sp-7:32px`。
- 面板内边距 16 → 22px；面板标题 13 → 15px；页头标题 `clamp(1.5rem,2.8vw,1.875rem)`；
  表格单元格 11 → 14px 内边距、字阶升到 `--fs-13`（行高明显放宽）。
- `.main` 留白 24px → `28px 32px 40px`。
- **CSS 里写死的 `font:Xpx` 简写必须一并抬**（`.about-endpoint-method` / `.ov-api-method` /
  `.dist-pct` 等 6 处）—— 否则它们会成为新的"小字地板"，改了 token 也白改。
- 数字输入框隐藏原生上下箭头（`.inp[type=number]`）：它随系统主题变样式、在深色面板里显脏。

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
4. 同一面板内**数字与图形必须同口径**（概览 hero 主数字与迷你折线同用窗口累计）。
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
