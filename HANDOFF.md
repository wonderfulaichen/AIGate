# 交接文档 — AIGate

> 最后更新 **2026-09-30**（品牌标重做 + 演示稿 Apple 化 + 根目录清理，同一轮）。
> 本轮三件事：① 品牌标重做为「**闸门通路**」单色实心蓝，并把散落的画法收敛成**单一母版**
> （`icon.svg`）+ 五处矢量落点 + exe 光栅，新增门禁 `verify_logo.py`（§5.8）；② 演示稿改用与
> 面板/落地页同源的 Apple 设计语言（装饰层按 §4.1b 收敛）；③ 根目录遗留物清理（三分法见 §5.7）。
> 工作区那 **10 个文件已按用户要求提交**（见 §2.1）。网关逻辑未动，只改 `build.rs` 的图标生成。
> exe **已部署**（§2.1 部署状态），`CHANGELOG.md` 也已补节并烤入。
>
> ✅ **临换对话前的收尾（2026-09-30）**：① 演示稿「没改」的分歧**已结** —— 用户确认"其实已经是新的了"（§2.1a）；
> ② 演示稿生成器**已回灌**，产物与生成器对齐，**今起产物不许手改**（§2.1b / §2.1c）；
> ③ 10 个文件已提交。**现在没有卡点。**
>
> 🔁 同日还做了一遍复核：部署份哈希/PID/线上版本、未推送提交数（15）、工作区文件数（10）
> 与**未跟踪列表（已清空）**都已按实测更正；顺手查出 `verify_logo.py` PART D 的 mtime 误报（见 §2.1 复测补记）。
>
> ✅ **工具链已入库**（2026-09-30）：门禁 / 生成器 / 取证脚本 + 4 个口径快照 JSON 全在 `tools/`，受版本管理（见 §4）。
> ⚠️ 仍要留意：`site/index.html` **不要再动**；`.openbitfun/` 仍是**本地草稿区**（隔离实例 / 截图 / 视频 / 预览），不入库。
>
> 上一轮（09-29 重建部署轮）已闭环 §2.2「CHANGELOG 未重烤」—— 重烤 + 重部署完成。
>
> 新对话接手时建议顺序：**§1 是什么 → §2 立刻要做的事 → §3 现状与硬事实 → §5 口径红线 → §6 操作手册**。

---

## 1. 这个项目是什么

本地 LLM 网关（Rust + axum，单 exe，前端编译期内嵌，零外部依赖）。
客户端（Cursor / Claude Code / Trae…）只认一个 `base_url`，网关负责：协议匹配、模型路由、
省量、计量、可观测、韧性。

**产品主张（演示与对外表述的主线）**：编程工具的账单本质是「重复上下文的搬运费」——
实测输入:输出 ≈ **380:1**、输入 **98.8%** 命中 KV 缓存。故第一目标是
**让重复上下文不再重复传一遍**，而不是并列一堆功能。

> 对外表述**只谈 token，不谈金额**（用户明确要求：价格会变，token 不会）。
> 演示稿已按此清理完毕 —— 正文**零**"金额/费用/价格/成本"字样，别再写回去。

版本 **0.7.0**。仓库 `github.com/wonderfulaichen/AIGate`。

---

## 2. 立刻要做的事（按优先级）

> **最新一轮（2026-09-30）**：演示稿改为与面板/落地页同源的 Apple 设计语言 + 根目录清理 + 品牌标重做，
> 三者**均已提交**，见 §2.1 与 §5.7；演示稿的生成器也已回灌（§2.1b）。
> （09-29 重建部署轮已闭环 §2.2 —— CHANGELOG 重烤 + 重部署；推送 §2.4 仍被代理未开阻塞。）

### 2.1 ✅ 本轮改动已提交（2026-09-30：品牌标重做 + 演示稿 Apple 化 + 根目录清理）

这轮的 **10 个文件**已提交（2026-09-30，用户明确要求后提交；`git status` 当时已无未跟踪残留）：

| 文件 | 改了什么 |
|---|---|
| `icon.svg` | **品牌标母版**换成「闸门通路」（单一事实源，见 §5.8） |
| `src/admin/core.js` | `brandLogoSvg()` 换新标：去渐变、去 gid（面板顶栏 + 关于页） |
| `site/index.html` | 顶栏品牌标换新标（原先还是**旧靛蓝**） |
| `docs/product-deck.html` | 演示稿 Apple 化 + 封面/流向 hub 两处品牌标换新（原先也是旧靛蓝）。⚠️ 2026-09-30 又由**回灌后的 `gen_deck.py` 重跑对齐**（§2.1b）—— 重跑产物与提交内容**逐字节同哈希**（`6300C36C…`） |
| `build.rs` | `generate_ico` 改多档尺寸 + 光栅重画（几何与母版**逐点同源**，见 §5.8） |
| `icon.ico` | 由 `build.rs` 重新生成：6 档 16/24/32/48/64/128，图案 = 新标 |
| `docs/frontend.md` | §4.1b 补「三处同源」约定 + 本轮 deck 装饰层收敛记录 |
| `.gitignore` | 新增 `_trash/`、`.zcodeignore` 两条（见 §5.7） |
| `HANDOFF.md` | 本文档 |
| `CHANGELOG.md` | 追加「品牌标重做：闸门通路」小节（已重建并烤入 exe） |

**本轮验证**（全部实测通过）：`verify_parts.js`（切片/JS 语法/include 顺序）✓ ·
`verify_logo.py` PART A/B/C 全绿（矢量落点逐属性 + 旧标残留 + 光栅逐控制点）✓ ·
`cargo test --release` **253 绿** · `cargo build --release` 无警告 ✓ ·
exe 位图 vs 矢量标像素对照：**均值差 1.48 / 最大 59**（3072 样本，最大差只落在边缘抗锯齿像素）✓ ·
28px 实机截图核对（面板顶栏 / 落地页顶栏）与 82px（deck 封面）✓

> ⚠️ **2026-09-30 复测补记（换新对话前刚测）**：`verify_logo.py` 现在 **PART D 报 FAIL**，
> 原因是 **mtime 顺序**不是内容 —— 那 10 个改动文件被某个工具**批量重写过一遍**，
> mtime 全是 `2026-09-30 03:04:42`，于是 `build.rs`(03:04:42) 比 `icon.ico`(01:38:20) 新，
> 触发门禁的「`icon.ico` 比 `build.rs` 旧 = 被缓存吞了」判据。
> **内容是对的**：PART D 其余三项全绿（6 档尺寸 [16,24,32,48,64,128] / 98902 B / 32px 像素与母版一致），
> 且 `check_exe_logo.py` 判定**部署份与构建份的图标位图 + 面板品牌标都是新标**（2026-09-30 复测）。
> **自愈路径**：下次 `cargo build` 时 `build.rs` 更新 → `generate_ico` 会重新生成 `icon.ico`（同字节），
> mtime 顺序恢复正常，门禁即转绿。**不要为了消警去手动 touch `icon.ico`**（那等于把这道门禁关掉）。
> 复测还顺带确认：10 个文件**都是真内容差异**（`git hash-object` vs `HEAD` 逐文件比对，无行尾假阳性），
> 行尾全是 LF（无 CRLF 混入）。

**部署状态（2026-09-30 02:08 已完成）**：按 §6 换入部署目录 —— 旧版本留存见 §3.3「可回滚的旧版本在哪」
（⚠️ 部署目录里**找不到** `.bak_2026*`；实际可用的是 `AI中转\新建文件夹\AIGate-0.7.0.exe`，与部署份同哈希）；
部署份 11,433,472 B 与构建份**逐字节同哈希**（`704FC5D8…`）；我部署时启动的实例是 PID 1788（02:08:43，
cwd = 部署目录），**此后又被重启过一次，当前 PID 50692（02:11:35 启动）**，部署份文件本身未变；线上验收：`/admin/api/changelog` 含新小节 ✓；`/admin` 返回的面板 HTML
含新标 path `M148 56 H52 V200 H148` 且**不再含旧标渐变** ✓；`check_exe_logo.py` 判定部署份的
图标位图与面板标均为**新** ✓。

**仍未做**：`assets/screenshots/` 两张面板截图仍是换标前拍的（README 在引用它们）。

> ⚠️ **`docs/aigate-deck.pptx` 与它的生成器明确「不要再动」**（用户明确要求）——
> 封面那个「方块 + ◇」的占位形**保持原样**，别因为"它不是真标"就去改它。
> 换标只覆盖：`icon.svg` / 面板 / 介绍页 / 演示稿 HTML / exe 图标（§5.8 的五处落点）。

**动作**：✅ **已提交**（2026-09-30，用户明确要求）。原先那条"不要提交，除非用户明确要求"的规则仍然适用。

### 2.1a ✅ 已结：演示稿「没改」的口径分歧（2026-09-30 用户确认「其实已经是新的了」）

**结论**：deck 的改动一直在磁盘上，用户看到旧样子是缓存 / 看的是 PPTX 或旧截图。
**本条不再是卡点，别再"修"它**。当时的实测证据留档如下：

| 检查项 | 结果 |
|---|---|
| `docs/product-deck.html` 副本数 | **全仓 + 工作区外（桌面 / 下载 / `D:\` / 部署目录）都只有 1 份** |
| 新标门框 path `M148 56 H52 V200 H148` | **有**（封面 + 流向 hub 两处） |
| Apple 同源 token（`--card-glass` / `--fs-h1` / `--amb`） | **有**（整块 `:root` 已换） |
| 旧靛蓝 `#6366f1` | **无** |
| 鼠标光晕 / 工程网格 | **已删**（仅注释留说明，无 live 规则、无 DOM） |
| mtime · 体积 | 09-30 03:04:42 · 909,970 B（≈889 KB）。⚠️ 该 mtime 是"被批量重写"的时刻，不是改动时刻 —— **同批 10 个文件 mtime 全等** |

> ⚠️ 仍然成立的红线：**不要再动 `site/index.html`** —— 上一轮"改错文件"就是把 deck 的需求
> 做成了 site 的 Apple 化。

### 2.1b ✅ 已结：生成器回灌（2026-09-30，用户选「回灌生成器」）

**问题**：`docs/product-deck.html` 是 `gen_deck.py` 的产物，而本轮的 Apple 化 + 新标是**直接改在产物上的**
（生成器仍是旧样子）→ 谁重跑一次生成器就全部覆盖（§7 首条的坑）。

**做法**：把产物的 CSS / JS / 品牌标 / 装饰 DOM / 文案**整体回灌**生成器，然后用
「重跑生成器 + 与改前产物逐行逐字节比对」证明回灌成立。回灌的具体落点：

| 层 | 回灌内容 |
|---|---|
| CSS 常量 | 换成产物那份 Apple 同源 token + 组件样式（含删掉 `.fx-mouse-glow` / `.s>.mesh` / `.s>.grid-bg` 等规则） |
| JS 常量 | 换成产物那份（章节配色收敛到 Apple 三色族、星尘 alpha 收到 .10~.28、`chStars` 200→120、去掉 `--glow-rgba` 与整段鼠标光晕逻辑） |
| 品牌标 | 封面 + 流向 hub 两处 `svg` 换成母版新标（旧靛蓝渐变 + 四节点那版删掉） |
| 装饰 DOM | 封面去掉 `.mesh`/`.grid-bg` 两个空 div；页尾去掉 `.fx-mouse-glow` 那个 div；氛围层注释同步 |
| 文案 | 上手页「从爱发电获取」+ 页脚「下载：afdian.com/… · 仓库：…」（生成器里原本还写着"从发行版下载"、缺下载链接 —— 这是产物上早已做过、生成器没跟上的改动） |

**回灌后重跑的结果**：**逐字节完全一致** —— 18 页 / 6 张截图 / 888 KB，`sha256 = 6300C36C…`
（重跑产物与改前产物同哈希；955 行逐行比对 **0 差异**）。这是回灌成立的硬证据。

**过程中查出的一个真问题（已修）**：第一轮重跑冒出 5 行差异，全是"剥离集中在哪一层"那个占比
从 97.5% 变成 97.7%。根因**不是数据刷新**，而是**产物与生成器的口径本就不一致**：

| 来源 | 字段 / 算式 | 值 |
|---|---|---|
| `savings_audit.json` | `toolcall_share_pct` = `strip_toolcall / total_saved` | **97.53%** → 显示 97.5 |
| `savings_shape.json` | `toolcall_share` = `toolcall_tokens / saved_total` | 97.69% → 显示 97.7 |

两个脚本连"省量总量"这个分母都差约 1 千万 tokens。产物里的 97.5% 是 `fd2bfac`
**特意按 audit 对齐**过的（那条提交信息里写明），而生成器那两处**硬编码着 97.7%**、
shape 页又直接读 shape 的字段 → **谁重跑一次就把这个对齐冲掉**。
**修法**：生成器统一改用 audit 的 `N_TOOLCALL_SHARE` / 新增的 `N_PLAIN_SHARE`（→ 97.5 / 2.5），
口径理由写进生成器注释与 §4.4。**以 audit 为准，别再动回 shape 那个字段。**

**验收**：`shoot_deck_hd.py 8 10 12`（强制终态那套）重拍相关页，第 9 页实测显示 **97.5% / 2.5%**，
排版无变化 ✓。

✅ **已解决（2026-09-30）**：工具链整体搬进仓库 `tools/`（详见 §4 与 §7）—— **43 个文件**
（37 个脚本 + 4 个口径快照 JSON + 2 个门禁输入 `split_manifest.json` / `admin_orig.html`）已纳入版本管理，
不再只存在于本地磁盘。搬完后全部重跑验证：生成器产物与提交版**逐字节一致**、12 道门禁全绿、
端到端 `e2e_proto_auto_any.py` PASS。

### 2.1c ⚠️ 今起的新纪律：产物不许手改

`docs/product-deck.html` 只能由 `gen_deck.py` 生成。**要改样式/文案就改生成器再重跑** ——
否则下一次重跑会把产物上的手改全部覆盖（这正是 2.1b 要修的东西）。

### 2.2 ✅ 部署 exe 的「更新日志」页缺本轮小节（本轮已解决）

原问题：`CHANGELOG.md` 由 `include_str!` **烤进二进制**，而它在 09-29 01:30 才写定，
比 09-28 23:09:35 那份 exe 晚 —— 故旧 exe 的「关于页 → 更新日志」缺协议锁定轮的 3 个小节。

**本轮已修复**：确认 `CHANGELOG.md` 定稿（3 小节均在）后 `cargo build --release` 重建，
按 §6 部署并重启。实测闭环（`cl_dump.py` 对构建份 + 线上 `/admin/api/changelog` 双验）：

```
构建份 target/release/AIGate.exe 烤入 changelog: 含「手动锁定的协议」= True
                                              含「推理链省 token」  = True
                                              含「清除金额相关表述」= True
线上 http://127.0.0.1:8787/admin/api/changelog: 同上三项均 True
```

**教训**：改完 `CHANGELOG.md`（或任何 `include_str!` 资源）**必须重建**，否则线上仍是旧文档。

### 2.3 校验部署份的**唯一可靠方式**（别信时间戳/commit 号）

本仓库 `build.rs` 用 `git rev-parse --short HEAD` 在**编译期**盖章，而工作树可能**先于提交**就已改好
（09-28 就因此误判过一次）。判定 exe 内容一律：

```powershell
python tools/e2e_proto_auto_any.py "<exe 完整路径>"   # 行为实测
python tools/cl_dump.py "<exe>" out.json             # 烤入资源, 比哈希
python tools/check_exe_logo.py [exe...]              # 品牌标: 新/旧, 直接字节命中
```

⚠️ **资源管理器里显示的图标不能作为判据** —— 它受 Windows 图标缓存影响：exe 换了同名路径之后，
列表里可能继续显示旧标，要等缓存刷新（重启资源管理器 / 改名或移动文件）才更新。
"这个 exe 到底嵌了哪一版标"一律用 `check_exe_logo.py`（图标位图 + 面板 path 双线索字节命中）。

**⚠️ `/admin/api/version` 的 `build_time` 也不可信**（本轮实测）：它取自 **git 提交时间**，
不是真实构建时刻 —— 刚重建的 exe 仍报旧时间戳。唯一判据是**行为实测**或**烤入资源哈希**。

本轮实测（2026-09-30 换标轮）：`target/release/AIGate.exe` 与部署份 `AI中转\AIGate.exe`
**逐字节同哈希**（`704FC5D8…`，11,433,472 B）—— 部署份含本轮品牌标重做 + CHANGELOG 重烤。
（旧一版数字 `2D7D9146…` / 11,234,816 B 是 09-29 那份，已不在磁盘上，见 §3.3。）

**⚠️ 中文校验的编码坑**：PowerShell `Invoke-WebRequest ... .Content` 按 Latin-1 解码，
中文会乱码导致 `Contains('中文')` **假阴性**。必须走字节流显式 UTF-8：

```powershell
$b = (New-Object System.Net.WebClient).DownloadData('http://127.0.0.1:8787/admin/api/changelog')
[System.Text.Encoding]::UTF8.GetString($b).Contains('手动锁定的协议')
```

### 2.4 15 个提交未推送（仍待办，代理未开）

`origin/main` 停在 `30d54f2`，本地领先 **15 个**（最早 `4a311ee`，最新 = `HEAD` = `e0ce6d9`）。
（旧文档写的"8 个"是当时的口径，之后又落了 7 个；**以 `git rev-list --count origin/main..HEAD` 现数为准**。）
推送命令（代理配置有讲究，见 §6）：

```powershell
git -c http.proxy= -c https.proxy=http://127.0.0.1:7890 push origin main
```

**本轮实测：7890 仍未监听**（`Test-NetConnection` 返回 False），故推送未执行。

### 2.5 工作区已无未跟踪文件（2026-09-30 实测更正）

`git status --porcelain` 现只有 **10 个 ` M`（已改未提交，见 §2.1）**，**没有任何 `??`**。
逐项更正旧记录：

| 旧记录 | 2026-09-30 实测 |
|---|---|
| 「未跟踪：`HANDOFF.md`」 | ❌ 已更正 —— `HANDOFF.md` **已入库**（`git ls-files` 命中），当前是 ` M` 状态 |
| 「未跟踪：`assets/68hub/`、`assets/cc-switch/`」 | ❌ 已更正 —— **两个目录都不存在**（`assets/` 下只剩 `screenshots/`），故也不在未跟踪列表里 |

注意：**验证脚本自 2026-09-30 起已入库 `tools/`**（见 §4）—— 换机器 / 换新对话都取得到。
`.openbitfun/` 被忽略，它现在只放**本地产物**：隔离实例 `iso_*`、截图 `shots/`、视频 `video/`、
预览 `preview/`、图标核对 `logo_check/`。

---

## 3. 现状与硬事实（2026-09-30 复测本轮相关行；未复测的行仍是 09-29 口径）

### 3.1 代码与验证

| 项 | 值 |
|---|---|
| 版本 | 0.7.0（`Cargo.toml`） |
| 单元测试 | **253 通过 / 0 失败**（`cargo test --release`，2026-09-30 实测；比上轮多 1 条 = 本轮新增的锁定回归测试） |
| release 构建 | **0 警告** |
| CHANGELOG 首节点 | `## [0.7.0] - 2026-09-28`，节点内 **55 小节 / 276 条目**（2026-09-30 实测口径：第一个 `##` 到下一个 `##` 之间的 `### ` 行与 `- ` 行；**会随写入增长**，引用前先数） |
| 前端门禁 | 11 道（见 §4）；本轮跑了关键 3 道 + 全部 11 道，全过 |
| 工作区 | **干净**（2026-09-30 提交后）。本轮提交了 §2.1 那 10 个文件 |

### 3.2 前端架构（最近重做）

- `src/admin.html` 已删除；改为 `src/admin/` 下 **37 个切片**，由 `admin.rs` 的
  `concat!(include_str!(...))` 按序拼成单串编译进二进制。
- **拼接成一份文档** → 标签必须逐文件配平；一个未闭合的 `</div>` 会吞掉其后所有页面。
  门禁 `balance.py` 专治这类「跨文件污染」（曾真实发生：分析页一处未闭合导致关于页发暗）。
- 骨架：**顶栏分段胶囊导航**（无左侧栏）+ 全宽内容。
- 主题：暗 / 亮 / 跟随系统，只切 `<html class="light">`，视觉全部走 token。
- 规约全在 **`docs/frontend.md`**（改前端前必读）：token 表、Alpine 陷阱、加载态契约、
  偏好 key、数字口径红线、图表约定、浮层必须在 `x-data` 之内、`x-text` 不可与子元素并用。
- **协议列（本轮改）**：下拉的「自动」= 空值（未标注，跟随客户端入口）；「OpenAI / Anthropic /
  Responses」= 显式锁定。**空值选项的文案旧版误写作「OpenAI」**，导致用户以为默认被固定成 chat
  —— 已修，别再改回去。

### 3.3 部署与数据

| 项 | 路径 / 值 |
|---|---|
| 部署目录 | `D:\Office software\Development Project\AI中转\` |
| 部署 exe | `AI中转\AIGate.exe` |
| **可回滚的旧版本在哪** | ⚠️ **部署目录里没有 `.bak_2026*`**（§6 备份步骤产出的临时备份已不在，2026-09-30 实测目录里只有 `AIGate.exe` / `aigate.old` / 配置与 data）。实际留存的是用户自己的版本归档：`AI中转\新建文件夹\AIGate-<版本>.exe`，共 10 份（0.4.2 / 0.4.3 / 0.4.7 / 0.5.0 / 0.5.1 / 0.5.3 / 0.5.4 / 0.5.5 / 0.5.7 / **0.7.0**）。其中 `AIGate-0.7.0.exe` 与当前部署份**逐字节同哈希** → 它是"当前版本的副本"，**不是上一个版本**；09-28/09-29 那两份 exe **没有留存**。**换 exe 前一律先按 §6 自造备份**，别假设有现成的 |
| **部署份 vs 构建份** | **逐字节同哈希** `704FC5D8…`，11,433,472 B（文件 mtime 09-30 02:06:30）—— 含品牌标重做 + CHANGELOG 重烤 |
| 数据目录 | `AI中转\data\`（`logs.jsonl` **5000 行**（= 该文件行数上限）/ 4900 条成功，3.4 MB；`aigate.log`） |
| 监听 | `127.0.0.1:8787`，当前**在跑**（进程 **50692**，09-30 02:11:35 启动，cwd = 部署目录） |
| 运行实例报告版本 | 线上 `/admin/api/version` 2026-09-30 实测：`v0.7.0 · commit e0ce6d9 · built 2026-09-29T17:38:20Z`（= 09-30 01:38 CST）。盖章值仅备查（见 §2.3） |
| 运行实例的前端 | **已是新版**（`fmt.auto`、`_static_formats` 实测可搜到） |
| 运行实例的更新日志 | **两轮小节都在**（2026-09-30 线上实测，五个探针串全部 True）：`品牌标重做` / `闸门通路` / `手动锁定的协议` / `推理链省 token` / `清除金额相关表述` |
| 已配供应商 | 7 家：`zen` `go` `deepseek` `Openrouter` `openAI-gpt` `ginka` `commandcodeAI`（共 652 个模型） |
| 模型配价情况 | **0 个模型配价** → 面板金额一律显示「—」（用户明确不需要配价，价格会变） |

> 文件名含中文（「AI中转」等），脚本里**不要用 shell 通配**，直接写完整路径。

### 3.4 关键配置事实

- `commandcodeAI` 的 `probed_protocols` 为 `None`（从未点过「拉取模型」），且所有模型
  `api_format` / `api_formats` 为空。**旧实现下这会静默把客户端协议改成 openai** ——
  即用户打 `/v1/responses` 被当成 chat 发出去。修复见 §5.1。
- 全库 652 个模型**无一**勾选 `api_formats` 候选集（这正说明「不该让用户手工勾选」）。
- 显式 `api_format` 只有 7 个模型有（全在 `openAI-gpt` 下，值均为 `responses`）——
  所以本轮「显式单值锁定」的修复**不会误伤存量配置**。

---

## 4. 验证工装（`tools/`，**已纳入仓库**，2026-09-30 起）

> 迁移口径：**脚本与它的输入在 `tools/`（入库），产物输出仍写 `.openbitfun/tmp/`（本地草稿区，不入库）**。
> 脚本内部一律用 `__file__` 推导仓库根，可在任意工作目录调用（个别脚本的用法行还写着旧路径，不影响运行）。
> ⚠️ **3 处刻意保留的绝对路径**（指向仓库之外的本机资源，已就地注释）：
> `check_exe_logo.py` 的部署份 exe、`savings_audit.py` / `savings_shape.py` 的真实日志
> `AI中转\data\logs.jsonl` —— 换机器要改这三行。

### 4.1 前端门禁（改前端后全跑）

```powershell
node tools/verify_parts.js       # 切片拼接自洽 + 20 JS 部件语法 + include 顺序
node tools/check_i18n.cjs        # i18n 中英双表缺键/重复键（build.rs 也拦）
node tools/test_pages.cjs        # 页码/排序/粒度/分级 纯函数
node tools/test_ime.cjs          # 搜索 IME 行为
node tools/test_provload.cjs     # 供应商加载并发收口
node tools/test_fmt_rules.cjs    # 前后端协议规则同源
python tools/balance.py          # 标签配平（会吞掉后续页面）
python tools/theme_complete.py   # 浅色 token 覆盖完整
python tools/theme_dark_diff.py  # 暗色零变化（比对生效颜色集合）
python tools/theme_contrast.py   # 浅色 WCAG 对比度
```

### 4.2 截图工作流（headless Edge）

```powershell
python tools/build_preview.py            # 拼接 + fetch 桩 + 生成 ?tab 变体
python tools/mkwrap.py                   # 生成 wrapper（设主题 + 100% 宽 iframe）
python tools/shoot.py s_dashboard --h=1200
python tools/shoot.py s_dashboard --w=780 --as=narrow_
python tools/shoot_deck.py 2             # 演示文稿第 3 页
```

三条硬约束（都踩过）：
1. **服务与浏览器必须在同一进程内跑完** —— 沙箱会在命令结束后回收子进程，跨调用复用实例必然失败。
2. **页签切换不能靠"载入后点侧栏"** —— headless 下 iframe 不可见时响应式刷新被节流，
   Alpine 数据变了 DOM 不动。正解是 `build_preview.py` 生成 `?tab` 变体，让页面**以目标页签启动**。
3. **`?tab` 变体必须保留一次 `fetchStats()`** —— 原 init 里 `switchTab('dashboard')` 还负责首次取数，
   直接换目标会跳过它，`loading` 恒为真 → 页面永远停在骨架。

### 4.3 端到端脚本（隔离实例 + mock 上游）

常用：`e2e_proto_auto.py`（协议匹配四条路径）、`e2e_resp_convert.py`（responses↔chat 转换产出对照）、
`e2e_oversize_hint.py`（4xx 体积提示）、`e2e_resp_strip.py`（Responses 剥离）、
`e2e_changelog.py`（更新日志接口）、`e2e_multiproto.py`（多协议共存）。

**「判定某份 exe 到底是什么」取证工具**（都接受任意 exe 路径，见 §2.3）：

| 脚本 | 用途 |
|---|---|
| `e2e_proto_auto_any.py <exe>` | 协议四条路径，可指向部署份/备份份做对照 |
| `ver_probe.py <exe> <tag>` | 起隔离实例读 `/admin/api/version`（看盖章） |
| `cl_dump.py <exe> <out.json>` | dump 烤入 changelog，**比哈希**即知两份 exe 是否同版 |

**本轮新增（协议锁定轮）**：

| 脚本 | 用途 |
|---|---|
| `e2e_lock_vs_auto.py` | 显式 `api_format` 锁定 vs 自动模式（各入口 → 上游实收路径） |
| `capture_strip_diff.py` | 抓真实「剥离前/后」请求体对照 → `strip_diff.json` |
| `capture_multiturn.py` | 跑 10 轮真实会话，开/关剥离各一遍 → `multiturn.json` |
| `savings_audit.py` | 统计三类省量总量 → `savings_audit.json`（**演示稿数字来源**） |
| `savings_shape.py` | 省量**分布形状**（集中度/量级/两层占比） → `savings_shape.json` |

### 4.4 演示文稿生成

`gen_deck.py` → `docs/product-deck.html`（**18 页**单文件，6 张截图 base64 内嵌）。

> ⚠️ **产物不许手改**（2026-09-30 立的规矩，见 §2.1b/§2.1c）：演示稿只能由生成器产出。
> 手改产物 → 下次重跑即被覆盖。改样式/文案一律改 `gen_deck.py` 再重跑。
> 回灌是否成立，用「重跑生成器 + 与改前产物逐行对比」自证（当时只剩 5 行数据差异）。
> ✅ 生成器与门禁脚本都在 `tools/`（**已入库**）；截图/视频等产物仍写 `.openbitfun/tmp/`（本地草稿区）。

改完必须截图核对（生成成功 ≠ 排版正确）。**优先用 `shoot_deck_hd.py`**，它比 `shoot_deck.py` 可靠：

```powershell
python tools/shoot_deck_hd.py 8 10 12     # 0-based 页码, 输出 1920x1080 到 video/deck_hd/
python tools/shoot_deck.py 0 8 17         # 1600x900 预览用, 偶发空白/中间态
```

- `shoot_deck_hd.py` 注入 CSS **禁掉全部过渡并强制 `.on` 终态**，且**直接写 `data-to` 真值** ——
  绕开"CSS 过渡不随 `--virtual-time-budget` 快进"与"countUp 没跑完截到中间态"两个坑。
- `shoot_deck.py` 靠累积 `dispatchEvent(ArrowRight)` 导航，stop 在哪不完全可控，
  实测抓到过"上一页还在淡出、新页没进来"的中间态（2026-09-30 复核时第 9 页即如此）→ 只当预览用。

**数字来源纪律（重要）**：演示稿里的实测数字**全部由脚本产出 JSON，`gen_deck.py` 只读**
（`savings_audit.json` / `savings_shape.json` / `multiturn.json` / `strip_diff.json`）。
日志会增长，**写死的数字必过期**（已踩过：稿里 3.84 亿 / 233 个模型，实际早已 4.04 亿 / 652 个）。
改动数字前先重跑对应的采集脚本。**重跑生成器时数字会自动刷新**（那是数据更新，不是回灌出错）。

⚠️ **但"剥离集中在哪一层"这个占比有两个来源、口径不同**（audit 97.53% vs shape 97.69%，见 §2.1b）——
全稿统一用 **audit**（生成器里的 `N_TOOLCALL_SHARE` / `N_PLAIN_SHARE`）。
`fd2bfac` 已按 audit 对齐过一次，**别改用 shape 的字段把 97.5% 变回 97.7%**。

**截图链路的一个坑**：批量 `shoot_deck.py` 多页时偶尔产出 9 KB 空白图（headless 时序竞争）。
判定真假的唯一办法是**单独重截那一页** —— 单独截图正常即说明是时序问题，不是内容问题
（本轮 4 页出现过，逐页重截均正常）。

### 4.5 品牌标同源门禁（2026-09-30 新增）

```powershell
python tools/verify_logo.py     # 四部分全绿才算过
```

- **PART A** 五处矢量落点与母版 `icon.svg` **逐属性**比对（rect + 两条 path + 颜色）。
- **PART B** 旧标残留扫描（`#6366f1`/`#4f46e5` 旧靛蓝、旧四节点、`rx=48`、`url(#ag)`）。
- **PART C** 把母版 path 展开成点列，与 `build.rs` 的 `GATE_PTS`/`ARROW_PTS`/`LOGO_*` **逐控制点**比对
  —— 证明 exe 光栅不是"另一个版本"，而是同一套几何的换算。
- **PART D** 验 `icon.ico` 实体：尺寸集合 == `ICO_SIZES`、抽 32px 那档解几个像素（圆角透明 / 底色 /
  描边色）、并查 **`icon.ico` 是否比 `build.rs` 旧**（旧则说明改动被那句"已存在且更新就跳过"吞了）。
  ⚠️ **最后这条 mtime 判据会被"批量重写文件"误触发**（2026-09-30 实测）：某工具把 10 个改动文件
  一次性重写了，mtime 全等于 `03:04:42`，`build.rs` 因此反超 `icon.ico`(01:38:20)，PART D 报 FAIL
  而**内容其实是对的**。故 PART D 报 FAIL 时**先看另外三项 + 跑 `check_exe_logo.py`** 判内容，
  别只凭这一条 mtime 判据下结论（自愈：下次 `cargo build` 重建 `icon.ico` 即转绿）。

理由：换标时实测同一个标存在**三种画法**（icon.svg 是新蓝、site/deck 还是旧靛蓝、build.rs 里第三份），
根因就是"改了 A 忘改 B"且没有任何机制报错。**改标必须跑这条门禁**。

---

## 5. 口径与不变量（新对话最容易踩）

### 5.1 协议：由客户端入口决定，网关只负责支持

- 规则：客户端打哪个入口（`/v1/messages`、`/v1/responses`、`/v1/chat/completions`）就是它的协议意图；
  上游支持就**原样直传**（逐字节保真），不支持才转换。
- 上游能力来源（并集）：① 模型级 `api_formats`（显式即约束）② 拉取模型时的探测结果
  ③ **静态层**（URL 路径信号 + 内置 host 目录）④ 配置/推断。
- **可用集为空 = "不知道"，不是"不支持"** → 尊重客户端选择，**不得静默改协议**。
  这是 `2c5cf8a` 修的核心；旧的「无脑兜底 openai」是 bug，别再改回去。
- 但**用户显式锁定**是另一回事：显式单值 `api_format` 必须**优先**于客户端入口（见 §5.2）。
- 探测用 **HEAD**（无请求体 → 零 token），判据 `404/501 = 不存在`，其余（含 405）= 存在；
  带**对照哨兵**防「所有路径同状态」的假结果。
- `resolve_endpoint` 会把 `/chat/completions` 改写为 `/messages` 或 `/responses`。

### 5.2 显式单值 `api_format` = 硬约束（本轮修的 bug，别再改回去）

`pick_api_format` 的**第 0 步**（在「客户端协议命中可用集」之前）：

- 若**未**声明 `api_formats` 候选集、且模型有显式单值 `api_format` → **直接返回它**
  （`normalize_format` 后校验是已知协议）。
- 若声明了 `api_formats` 候选集 → 仍以**候选集**为准（它表达更细：允许客户端在集合内自选）。

**修的是什么**：原先第 1 步「客户端协议命中可用集就直通」跑在读显式配置**之前**，于是供应商
探测到多协议时（真实配置里 `deepseek` 就是 `openai,responses`），用户手动把模型固定成 `openai`，
客户端打 `/v1/responses` **仍被放行成 responses** —— 手动设置等于没有。

**前端镜像**：`ds-brands.js::effectiveApiFormat` 与 `apiFormatInferred` 必须同步这套语义
（已同步，并由 `test_fmt_rules.cjs` 逐例把关）。前端与后端任一改动而不同步即会「面板显示与
实际转发不符」—— 这是本项目反复出现的 bug 类型。

**另一个钳制点**：`available_api_formats`（能力并集）的顺序是
① 显式 `api_formats` ② 探测 ③ **静态层** ④ 配置/推断。前端 `availableApiFormats` 曾漏掉
**③ 静态层**，导致「后端算出可用 anthropic、前端算出空 → 面板显示 OpenAI 实际发 /messages」。
现由后端随 `/admin/api/providers` 下发只读字段 `_static_formats`（下划线前缀 + 保存端白名单，
不会被写回 `providers.json`），前端并入。

### 5.3 省量口径（对外表述必须一致）

- 实测（真实日志，**数字会随日志增长**；引用前重跑 `savings_audit.py`）：
  输入:输出 ≈ **380:1**；输入命中 KV ≈ **98.8%**；剥离省下 ≈ **4.04 亿 tokens
  = 输入总量 24.0%**；受益请求 **65.4%**（3248/4900 条成功请求）。
- **省量以 token 计** —— 价格会变，token 不会。金额只作参考并标注为估算。
  **面板不上价时一律显示「—」**（不是 0：「没配价」≠「免费」）。
- **剥离量级**（`savings_shape.py`）：单请求省量中位数 **12.05 万 tokens**、
  剥离占该请求输入中位数 **37.1%**（p90 达 97%）、单请求输入中位数 **31.5 万**、
  省量前 20% 请求贡献 **51%** 的总省量。
- **省量的 97.7% 来自「带 tool_calls」那一层**，而该层**默认仅 chat 协议开启** ——
  所以 responses 直通路径的省量恒为 0（实测 1296 条对照）。
- **所有省量都是估算**：`被剥离内容的字符数 ÷ 4`（不是真实分词器；中文实际约 3 字节/token，
  故真实省量只会更高）。对外别说成"分词实测"。
- **剥离是「有损且不可逆」**（`proxy.rs` 注释与实现一致）—— 故提供可选的「降级可回取」
  留档原文（默认关，因含思考明文）。项目里自称「完全无损」的只有**响应缓存**与并发去重，
  两者不要混为一谈。
- **剥离判定按「上游协议」而非供应商**：Anthropic 恒不剥（thinking 块必须连签名回传，
  剥了必 400）—— 这条**优先于模型级开关**。开关粒度按**模型**而非供应商
  （同一中转下混着多种协议）。详见 §5.5。

### 5.4 剥离判定（`resolve_strip_toolcall`，别记反）

| 情形 | 判定 |
|---|---|
| Anthropic 协议 | **绝不剥**（优先于模型级开关） |
| chat · **不含** tool_calls 的 assistant | 剥（默认开） |
| chat · **带** tool_calls | 剥（默认开，`STRIP_TOOLCALL_ON_CHAT`）—— 省量大头 |
| Responses · `reasoning` item | **默认不剥**，可开（`STRIP_TOOLCALL_ON_RESPONSES`，未经实测） |

**⚠️ 极易记反**：默认剥的是**不含** `tool_calls` 的 assistant 推理链；带 `tool_calls` 的
由上述协议开关单独控制。演示稿曾把这条**写反**（说成"只剥带 tool_calls 的"），已修 —— 别再改回去。

**⚠️ 也没有「只改追加区所以缓存不坏」这种机制**：剥离从 index 0 遍历**整个**历史
（`proxy.rs:3451`），无任何前缀/尾部位置判断。98.8% 的命中率是**实测聚合值**，
不是剥离代码保证的性质。这句因果解释曾在演示稿里编造过，已删。

### 5.5 前端红线（`docs/frontend.md` 有全表）

- 组件里**不得出现** `rgba(255,255,255,*)` / `rgba(0,0,0,*)` 字面量（浅色下会消失或过重）→ 用 token。
- 浮层**必须在 `x-data` 作用域内**（`</main></div>` 由 `panel-price.html` 收尾，
  不要在 `footer.html` 提前关闭；新增浮层放 `panel-price.html`，别放 `tail.html`）。
- **不要给同一元素同时用 `x-text` 与子元素**（`x-text` 会销毁子节点，且子元素失去作用域）。
- **不要用 `animation ... both`** 做入场（未推进时停在首帧 `opacity:0` → 内容全丢）；用 `transition`。
- 数字计数**不要用 `requestAnimationFrame`**（headless/后台标签下会停摆，永久停在 0）；
  用 `setInterval` + 超时兜底，且**初值即真实数字**。
- 图表外观走 CSS 类 + token，只有**数据系列色**保留内联。

### 5.6 CHANGELOG 格式是硬约束

`parse_changelog` 只识别 `## [` / `### ` / `- ` **三种行，其余整行丢弃**。
所以：**不要写 `####` 子标题、不要用 Markdown 表格、不要把内容写成纯段落** ——
否则关于页与「更新亮点」弹窗里会**凭空消失且不报错**。
空小节同样渲染成空白。由单测 `changelog_only_uses_parseable_markup` 强制把关。
（本轮我自己又犯了三次：5 个 `####`、3 个表格、5 个纯段落小节，全靠这道门禁抓出。）

### 5.7 仓库根目录清理口径（2026-09-30 定）

根目录会自己"长垃圾"（日志、探测快照、空目录、设备名文件）。每次清理都按**三分法**走，不凭印象删：

- **可直接删 —— 纯产物**：能由构建/探测重新生成，且不含任何决策信息。
  含 `build.log` / `test.log` / `full_test.log`、`resources.rc`（`build.rs` 每次构建**先写出再编译**，
  见 `write_version_rc`，故删了下次构建自会重建）、`NUL`、空目录。
- **移到 `_trash/<日期>/` 待复核 —— 有内容但一过性**：探测快照 `served_check.html`、
  一次性脚本 `transform.py`、探索稿 `design-preview/`、工具记忆 `.zcode/`。
  `_trash/` 已入 `.gitignore`；**整目录确认无用后再手动删**，不要当场抹掉。
- **一律不动**：`.openbitfun/`（本地草稿区：隔离实例 / 截图 / 视频 / 预览 —— 工装脚本已移到 `tools/`）、
  `data/`（运行实例的密钥与日志）、
  `target/`（cargo 构建缓存，清了下次全量重编）、`参考源码/`、`assets/`、`.codebuddy/`。

两条 `.gitignore` 约定（2026-09-30 新增）：

- `_trash/` —— 归档区不入库。
- `.zcodeignore` —— ZCode 工具自己的排除清单，**内容 ≠ `.gitignore`**（含工具默认规则），
  故「保留文件 + 加忽略」，不要合并两者、也不要删文件；不加它每次 `git status` 都会冒一个 `??`。

**两个实测坑**：

1. `NUL` 是 Windows 设备名，`Remove-Item` 与回收站删除都会失败（实测报"未能将 NUL 移动到回收站"）。
   删它必须走 .NET + 扩展路径：`[System.IO.File]::Delete("\\?\<绝对路径>\NUL")`。
2. 编辑器重写行尾后 `git status` 会报 ` M` 但**内容零差异**（worktree / index / HEAD 三个
   `git hash-object` 完全相同）。别把它当成改动去提交；`git add -- <file>` 重挂索引即可恢复干净
   （2026-09-30 在 `README.md` / `site/index.html` / `src/admin/ds-records.js` 上各遇到一次）。

### 5.8 品牌标：单一母版 + 五处同源 + exe 光栅（2026-09-30 定）

- **母版只有一份**：`icon.svg`（256 空间）。别处一律从它派生，**不得各画一份**：
  面板 `core.js::brandLogoSvg()`、`site/index.html`、`docs/product-deck.html`（封面 + 流向 hub 两处）、
  `build.rs` 的 exe 光栅。改一处必须五处同步，跑 §4.5 的门禁。
- **造型** = 「闸门通路」：圆角方块底（`rx 56`，占 23%）+ 门框（右缘敞开）+ 一枚穿过的箭头。
  单色实心 `#0a84ff`（= Apple systemBlue = 面板 `--accent`）+ 纯白负形，**不做渐变**。
- ⚠️ **比例是按 28px 实机反推出来的，三处别改回去**（改回去会被读成字母 E）：
  ① 门框右缘不留短柱（28px 下直接消失）；② 箭杆与门框左柱留 ≥40 单位间隙；
  ③ **门框 20 / 箭头 32 拉开线宽主次** —— 等粗时三根横线会被眼睛并成 E。
- ⚠️ **exe 图标不是从 svg 转的**，是 `build.rs` 里手写逐像素（圆角矩形 SDF + 门框胶囊 + 箭头多边形
  + 3×3 超采样抗锯齿）。改标必须同时改那里的 `LOGO_*` / `GATE_PTS` / `ARROW_PTS`（坐标与 SVG 同一套）。
- ⚠️ **`generate_ico` 带缓存**：「`icon.ico` 比 `build.rs` 新就跳过」。曾因此让线上图标**陈旧好几天**
  （一直挂着旧靛蓝）。改完图标若 `icon.ico` 的 mtime 没动，就是被它吞了 —— §4.5 PART D 会查这条。
- 尺寸集合 `ICO_SIZES = [16,24,32,48,64,128]`（≈99 KB）。**刻意不含 256**：BMP 存一张 256 就是 256 KB，
  而 256 只在资源管理器"超大图标"里出现；要补它得给大尺寸走 PNG 编码（需把 png/miniz_oxide 加进
  build-dependencies，或手写 deflate）—— 本轮未采纳，留作明确的可选项。

---

## 6. 操作手册

### 部署（⚠️ 必须先停实例；用 .NET API 而非 Rename-Item）

**本轮实测推翻旧做法**：旧文档说「改名旧 exe，运行中的进程会继续用改名后的文件」——
在本机**不成立**：

1. 运行中的 `AIGate.exe` **独占**该文件 → `Rename-Item` / `Copy-Item` 覆盖均失败
   （分别报 `Access denied` / `being used by another process`）。
2. 即使停了进程，**`Rename-Item` 在本环境仍被拦**（沙箱点名该文件，`dangerouslyDisableSandbox` 也拦不住）；
   而 **`.NET [System.IO.File]::Move/Copy` 可行**。→ 一律改用 .NET API。

```powershell
# 0) 先停掉在跑的 8787 实例（否则文件被独占，换不了）
$c = Get-NetTCPConnection -LocalPort 8787 -State Listen
Stop-Process -Id $c.OwningProcess -Force

# 1) 备份（.NET Move；命名带时间戳，便于回滚）
$dst = 'D:\Office software\Development Project\AI中转'
$bak = Join-Path $dst ('AIGate.exe.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
[System.IO.File]::Move((Join-Path $dst 'AIGate.exe'), $bak)

# 2) 拷新（**必须来自重建** —— 且重建前确认 CHANGELOG.md 已定稿，见 §2.2）
[System.IO.File]::Copy(
  'D:\Office software\Development Project\AIGate\target\release\AIGate.exe',
  (Join-Path $dst 'AIGate.exe'), $true)

# 3) 重启（cwd 必须是部署目录 —— providers.json / data 都按 cwd 解析）
Start-Process -FilePath (Join-Path $dst 'AIGate.exe') -WorkingDirectory $dst
```

> `Start-Process` 必须在**非沙箱**下执行，否则命令结束后子进程会被回收（§4.2 同理）。
> 核对**不能只看 `git_commit` / `build_time`**（都是编译期盖章，见 §2.3）。
> 真正的判据是**行为实测** 或 **比对烤入资源哈希**：
> 例如 `/admin/api/changelog` 是否含你刚写的小节标题（注意 UTF-8 解码，§2.3）。

### 起一个隔离验证实例（不动线上）

1. 建目录，**把 exe 复制进去**（不只是放配置 —— `providers.json` 从 exe 所在目录加载）。
2. 该目录下 `providers.json`：`endpoint` 指向本机 mock 或 `http://127.0.0.1:9/...`，
   `api_key_default` 给 dummy（避免预检打真实上游）。
3. 启动：`PORT=8899 AIGATE_DATA_DIR=<隔离数据目录>`，**cwd 也指向隔离目录**。
4. 用完删掉。现成脚本见 §4.3（都自带隔离实例逻辑）。

### 推送（代理是关键）

```powershell
# 必须先确认 7890 在监听
Test-NetConnection 127.0.0.1 -Port 7890 -WarningAction SilentlyContinue
# http.proxy 必须清空, 只留 https.proxy —— 两个都设会 TLS 握手失败
git -c http.proxy= -c https.proxy=http://127.0.0.1:7890 push origin main
```

### 中文编辑的坑（PowerShell）

- **不要用行内 `python -c "..."`** 写含中文/引号的代码：PowerShell 会吃掉转义。
  写成 `.py` 文件再执行。
- `Get-Content -Raw` 默认按 ANSI 解码，中文乱码 → 用 `[System.IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)`。
- 多行 `git commit -m` 会被截断 → **写文件再 `git commit -F <file>`**。
- **不支持 heredoc**（`<<'X'` 会报「重定向运算符缺少文件规范」）。
- `cargo` 的 stderr 会让 PowerShell 报 exit=1，**编译实际可能成功** —— 以是否出现 `Finished` 为准。

---

## 7. 未完成 / 待办

| 项 | 说明 |
|---|---|
| ~~⚠️ 演示稿的改动只存在于「产物」里~~ | ✅ 已结（§2.1b）：已把 Apple 化 + 新标 + 装饰 + 文案**整体回灌** `gen_deck.py`，并用「重跑 + 与改前产物逐行对比」证明（只剩 5 行数据刷新）。**今起产物不许手改**（§2.1c） |
| ~~⚠️ **生成器不入库（这是回灌唯一的软肋）**~~ | ✅ 已解决（2026-09-30）：工具链搬进 `tools/` 并入库，**43 个文件** —— 37 个脚本（生成器 + 12 道门禁 + 取证与采集）+ 4 个口径快照 JSON + 门禁依赖的 `split_manifest.json` / `admin_orig.html`。搬完重跑验证：生成器产物与提交版**逐字节一致**、12 道门禁全绿、`e2e_proto_auto_any.py` PASS |
| ~~⚠️ 演示稿「没改」的口径分歧~~ | ✅ 已结（§2.1a）：用户 2026-09-30 确认「其实已经是新的了」 |
| ~~**提交本轮改动**~~ | ✅ 已完成（2026-09-30，用户明确要求后提交） |
| ~~重建 + 重部署 exe~~ | ✅ 本轮已完成（§2.1 部署状态）：哈希 `704FC5D8…`，当前实例 PID 50692 |
| **推送** | §2.4：本地已领先 **15 个**提交（`origin/main` 停在 `30d54f2`），代理 7890 未开 |
| 介绍视频 `aigate-intro.mp4` 已过期 | 帧是**换标前**拍的，要重编才有意义（deck 的 18 帧已按新样式重拍；本次因口径修正又重拍了第 9 / 11 / 13 页） |
| 面板截图重拍 | `assets/screenshots/` 两张仍是换标前拍的（README 正引用） |
| `icon.ico` 补 256 档 | §5.8：大尺寸需走 PNG 编码（加 `png`/`miniz_oxide` 到 build-dependencies 或手写 deflate），本轮未采纳 |
| ⚠️ `verify_logo.py` **PART D 现报 FAIL** | 是 **mtime 顺序**不是内容：10 个改动文件被批量重写（mtime 全 `03:04:42`）→ `build.rs` 反超 `icon.ico`。PART D 其余三项 + `check_exe_logo.py` 均判**新标**（见 §2.1 复测补记）。**下次 `cargo build` 即自愈**；别手动 touch `icon.ico` 去消警 |
| 拖拽排序 | cc-switch 的 `useDragSort`（排序即故障转移优先级）；本项目**完全没有排序能力** |
| 模型表虚拟滚动 | 抽屉里 652 个模型**全在 DOM**（`x-show` 过滤），应窗口化 |

### 已知会误导的点（务必先读）

- **exe 的 commit 号不可信** —— `build.rs` 编译期盖章，工作树可能先于提交就改好。
  判内容一律**实测**或**比对烤入资源哈希**（§2.3）。
- **`/admin/api/version` 的 `build_time` 也不可信** —— 取自 git 提交时间，不是真实构建时刻（§2.3）。
- **烤入资源会比代码旧** —— `CHANGELOG.md` 由 `include_str!` 进二进制；若它在构建后又改过，
  运行实例的「更新日志」页就是旧版（协议锁定轮正是如此，本轮已重建解决，§2.2）。**改完 CHANGELOG 记得重建。**
- **换 exe 必须先停实例，且用 `.NET File.Move/Copy`** —— `Rename-Item` 在本环境被拦（§6）。
- **PowerShell 校验中文要显式 UTF-8** —— `.Content` 走 Latin-1 会假阴性（§2.3）。
- **CHANGELOG 数字会涨** —— 引用小节/条目数前先数一遍（已从 21/113 涨到 27/138）。
- **省量数字会涨** —— 演示稿的数字来自 JSON，日志增长后**重跑采集脚本**，别手改。
- 文件名含中文（「AI中转」「参考源码」），脚本里不要用 shell 通配。

### 已明确「不要再动」

- 不要给模型配价来"让金额显示出来" —— 价格会变，**省量以 token 为准**。
- 不要把「省量」降级为并列功能之一 —— 它是产品主线。
- 不要在演示稿里重新引入金额/成本表述（用户明确要求产品介绍不谈钱）。
- 不要改回「无证据时兜底 openai」—— 那是 bug（§5.1）。
- 不要改回「默认剥带 tool_calls 的那一层」的说法 —— 记反了（§5.4）。

---

## 8. 各轮做了什么，便于判断现状

### 09-30 换标 + 演示稿 Apple 化 + 根目录清理轮（最新）

**三件事，全部只改前端/资源，未动网关逻辑**（`build.rs` 只改了图标生成）：

1. **品牌标重做 → 单一母版**：标改为「闸门通路」（圆角方块 + 右缘敞开的门框 + 穿过一枚箭头，
   单色实心 `#0a84ff`，不做渐变）。五处矢量落点 + `build.rs` 手写光栅全套同源，
   新增门禁 `verify_logo.py`（四部分全绿，见 §4.5）；细节与「别改回去」的三条比例理由见 §5.8。
2. **演示稿改 Apple 设计语言**：token 换成与 `src/admin/base.css` / `site/index.html` 同源的语义色，
   删掉鼠标光晕/聚光灯/渐变文字等装饰，数字改 `tabular-nums`。记录在 `docs/frontend.md §4.1b`。
3. **根目录清理**：三分法口径见 §5.7（`.gitignore` 新增 `_trash/` 与 `.zcodeignore`）。

**验收**：`cargo test --release` 253 绿 / 0 警告；`verify_logo.py` 四部分全绿；
exe 位图 vs 矢量标 3072 采样像素 均值差 1.48；按 §6 部署并按 §2.3 双验（哈希 + 行为）。
**留下两个坑**：① `docs/product-deck.html` 是 `gen_deck.py` 的产物，改动**未回灌生成器**（§7 首条）；
② 用户认为"演示稿没改"（§2.1a）。

### 09-29 重建部署轮

**只做重建 + 重部署，未改任何代码。** 目标：把协议锁定轮写定的 `CHANGELOG.md` 3 小节
烤进二进制并部署上线（上一版交接 §2.2 的遗留）。

- `cargo build --release` → `Finished in 59.86s` / 0 警告；新 exe 09-29 03:06:26 / 11,234,816 B。
- 校验：`cl_dump.py` 对构建份 3 小节全 True；`e2e_proto_auto_any.py` 四条路径 **PASS**。
- 部署：停旧实例（PID 26584）→ `.NET File.Move` 备份为 `AIGate.exe.bak_20260929_032612`
  → 拷入新 exe（哈希 `2D7D9146…`）→ 以部署目录为 cwd 重启（PID **39476**，03:26:19）。
- 闭环验证：线上 `/admin/api/changelog` 三小节全 True。

**本轮发现的 3 个环境事实（已写入 §2.3 / §6 / §7）**：

1. **换 exe 必须先停实例** —— 运行中的 `AIGate.exe` 独占文件，旧文档「改名即可」不成立。
2. **`Rename-Item` 在本环境被沙箱拦**（`dangerouslyDisableSandbox` 也无效），
   但 **`.NET [System.IO.File]::Move/Copy` 可行** —— 部署一律改用 .NET API。
3. **`build_time` 取自 git 提交时间**、非真实构建时刻；且 **PowerShell `.Content` 按 Latin-1
   解码会让中文校验假阴性** —— 判内容一律「行为实测 / 烤入资源哈希」+ 显式 UTF-8。

### 09-29 协议锁定轮

**1 处代码修复（未提交）**：`pick_api_format` 把「显式单值 `api_format`」提到最前当**硬约束**。
原先第 1 步「客户端协议命中可用集就直通」跑在显式配置之前 → 供应商探测到多协议时
（真实配置里 deepseek 就是 `openai,responses`），用户手动锁 `openai`，客户端打 `/v1/responses`
仍被放行 —— **手动设置等于没有**。前端 `effectiveApiFormat` 同步镜像；新增回归单测（252 条）。

**1 处镜像漂移修复（未提交）**：前端 `availableApiFormats` 漏了后端可用集的**静态层**
（URL 路径信号 + 178 条 host 目录），会导致「面板显示 OpenAI，实际发 /messages」。
改由后端随 `/admin/api/providers` 下发只读 `_static_formats`，不在 JS 里复刻目录（必然漂移）。

**用户报障的两个"显示骗人"**（都已修）：
- 协议下拉的**空值选项文案写作「OpenAI」**，而空值 = 自动 → 用户以为默认被固定成 chat；
  且他想手动锁 openai 时选中该项等于没设。现拆成「自动（跟随客户端）」+ 显式 `OpenAI`。
- FAQ 把「自动」说成「默认 OpenAI」，已改写。

**演示稿重做（16 → 18 页，未提交）**——用户两轮反馈：
1. **「最核心的推理链省 token 没讲清楚」** → 新增/重做 4 页，全部**实测**：
   - 「推理链是过程，不是结果」（为什么可以剥，依据代码注释）
   - 「推理链越滚越大」（`capture_multiturn.py` 跑 10 轮真实会话：不剥离涨到 11,510 B，
     剥离后压平在 3,210 B，末轮省 72%）——**替换掉原先那张示意柱图**
   - 「剥不剥，先看协议，再看层」（判定表，Anthropic 绝不剥优先于模型开关）
   - 「省掉的量级有多大」（单请求省量中位数 12.05 万、占输入 37.1%、前 20% 请求贡献 51%）
   + 重写「省量手段」页（原先与判定页重复，改为只讲各管哪一段及各自代价）
2. **「为什么还在说金额？我们是产品介绍」** → 删掉整页「为什么以 token 计不以金额计」，
   并清理全部金额/成本语境（封面「以最低成本送出去」→「不再重复传一遍」等）。
   正文金额类词**零命中**（校验方式：剥离 CSS/JS 注释与截图 base64 后检索）。

**发现并修掉演示稿讲错的两处机制**（核源码得出，非润色）：
- 旧稿写「剥掉**带** tool_calls 的推理链」——**恰好反了**，代码默认剥的是**不含**的那些。
- 旧稿称「改动集中在追加区之后所以前缀缓存不坏」——**代码里没有这种位置判断**，
  剥离从 index 0 遍历全历史；98.8% 命中率是实测聚合值，不是代码保证。该因果解释已删。

**新增工装**：`e2e_lock_vs_auto.py`、`capture_strip_diff.py`、`capture_multiturn.py`、
`savings_audit.py`（+ JSON）、`savings_shape.py`（+ JSON），见 §4.3。

### 09-29 复核轮

**更正一处误判**：上一版 §2.1 断言「部署 exe 不含协议修复」，据以认为线上报障是旧 exe 所致。
本轮**实测推翻**：部署份四条路径全 PASS，与 `target/release` 的烤入 changelog 逐字节同哈希。
根因是 `build.rs` 编译期用 `git rev-parse HEAD` 盖章，而修复源码**先于提交**存盘（06:36 vs 06:44），
exe 编的对、标签旧。**该误判已更正，并写入项目记忆。**

**新增取证工具**：`e2e_proto_auto_any.py` / `ver_probe.py` / `cl_dump.py`（见 §4.3）。

### 09-28 轮

**修复类（4 个真缺陷，其中 2 个此前完全不可见）**

1. `2c5cf8a` **协议匹配失效**：无能力证据时兜底 openai + 静态层未进可用集 →
   客户端 Responses 被静默改成 chat。修法：静态层纳入可用集；可用集为空时尊重客户端选择；
   转换侧修两处畸形（`user.content` 数组→字符串；`reasoning` 不再独立成条空 assistant）。
2. `0ce81be` **全部浮层落在 `x-data` 之外**：`footer.html` 提前关闭了根容器 →
   供应商抽屉/价格面板/更新亮点/拉取差异弹窗/**Toast 与确认弹窗**全不工作，且不报错。
3. `66d8412` **分析页未闭合 `g2 stagger`** 把后续所有页面吞进自己里面（关于页整体发暗）。
4. `482` / `c6f87e1` / `d1595f8`：记录页表头 `x-text` 与子 span 冲突（排序箭头从不显示，每次加载抛 7 个异常）；
   趋势图平滑函数 `smooth()` 定义了却从未被调用（图一直是折角）；迷你图线变粗（非等比拉伸 + 误加平滑）。

**前端全面重做**：骨架（拆左侧栏→顶栏胶囊）/ 视觉（玻璃卡+三级阴影）/ 尺寸（字阶上移一档）/
页面（Hero 指标带、设置行卡片化、抽屉整屏）/ 亮色主题 / 图表（对齐参考项目形态）。

**工装**：新增 `balance.py`、`theme_complete.py`、`theme_dark_diff.py`、`theme_contrast.py`、
`changelog_only_uses_parseable_markup` 单测、headless Edge 截图链路。

**文档**：`docs/frontend.md`（前端规约全表）、`docs/product-deck.html`（产品演示 17 页）、
CHANGELOG 拆出 0.7.0 发布节点。

---

## 9. 参考项目

`参考源码/cc-switch-main`（Tauri + React 的供应商切换器）。用户多次要求参考它做前端设计。
**注意**：它按「客户端维度」隔离供应商（每客户端独立列表），**不做同模型多协议**；
其协议写在预置目录的 URL 路径里。本项目在其之上多做了协议自动匹配与省量。
