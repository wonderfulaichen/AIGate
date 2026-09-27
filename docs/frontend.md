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
├── body-shell.html            <body> 壳: skip 链接 + 顶栏 + 侧栏 + main 起点
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

## 2. 验证（改完必做，四道）

```powershell
node .openbitfun/tmp/verify_parts.js   # ① 切片拼接自洽 + 20 个 JS 部件语法 + include 顺序
node .openbitfun/tmp/check_i18n.cjs    # ② i18n 中英双表缺键/重复键 (build.rs 也拦)
node .openbitfun/tmp/test_pages.cjs    # ③ 纯函数单测 (页码/排序/粒度/分级)
node .openbitfun/tmp/test_ime.cjs      # ④ 搜索 IME 行为
cargo test --release && cargo build --release    # ⑤ 228 绿 + 0 警告
```

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

- 改 `--rs` 会按比例带动 `--rm/--r/--rl`；改色只改 `:root`，不要在组件里打补丁。
- `--fill-*` / `--shade-*` 是**逐值等价**收敛来的（原为 36 处重复 rgba 字面量）；
  新增档位请新增 token，不要把不同 alpha 合并成一档（那会改视觉）。
- 亮色换肤**不做**（历史结论）：`html.light{}` 只是预留结构，没有任何取值。

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
