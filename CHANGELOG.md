# 更新日志 (Changelog)

所有重要改动记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.5.5] - 2026-09-13

### 新增
- **省量审计面板**：分析页新增「省量审计」，量化"还能从哪儿省 token"——已生效省量按来源拆分（剥离推理链 / 其中 tool_calls 轮次 / 历史裁剪 / 响应缓存）、KV 缓存命中率与未命中量、潜在可省量（豁免推理链、重复内容块）及其占已采样输入的比例，另有输出侧汇总（输出总量、输出/输入比、长输出请求、死循环截断次数）。**只读统计，不改变任何转发内容**，各项随请求日志持久化、跨重启累计。
- **「剥离带工具调用的推理链」开关（逐模型）**：agent 循环中带 `tool_calls` 的历史消息占输入 token 比例可达 38.5%（实测），此前该部分被保留。现可在模型表格按模型勾选剥离——因约束因上游而异（Anthropic 的 thinking 块须与 tool_use 并存、DeepSeek reasoner 亦要求 `reasoning_content` 与 `tool_calls` 同在，剥掉会 400），故按模型而非供应商灰度。
- **模型单价可编辑（按量计价）**：模型表格新增「单价」列，点击展开价目表面板，布局与供应商官方定价页一致（输入-缓存命中 / 输入-缓存未命中 / 输出 × 空闲时段 / 高峰时段，另含 Anthropic 缓存写入价）。支持复制/粘贴价格与「批量应用」到勾选模型。
- **高峰时段可配置**：原先硬编码"北京时间 09:00–12:00、14:00–18:00"，现可在设置页配置时区偏移、高峰日（周一至周日多选）与时间段（可增删多条），落盘 `peak_schedule.json` 并热更新、无需重启。
- **供应商名称可编辑**：抽屉标题栏的名称改为可编辑输入，重命名时自动迁移该供应商的 API Key。
- **上游请求体诊断**：上游返回 400 且错误体点名 `messages.N.content` 时，自动打印该条消息的结构形态（如 `role=tool content=array(len=1)[image_url] +tool_call_id`），定位非法 content 结构而无需 dump 请求体；不含用户正文。
- **`AIGATE_DATA_DIR` 环境变量**：显式指定数据目录（日志 / 日级 rollup / keys）。默认仍为 exe 同级 `data/`。
- **请求详情补费用与省量拆分**：记录页弹窗新增「本次费用」「KV 缓存净省」，并把「优化省量」由单一总数拆为四类来源（剥离推理链 / 其中工具轮次 / 历史裁剪 / 响应缓存）且各带折算费用；费用由后端下发以保证与统计页同口径。
- **省量审计面板补费用**：已生效省量的四行明细各带 `≈ 费用`，KV 卡片补「缓存净省」金额。
- **上游请求体诊断**：上游 400 点名 `messages.N.content` 时打印该消息的结构形态（不含正文），用于定位非法 content 结构。
- **i18n 补漏**：修正 `Prompt / Completion`、`hit` / `miss`、`Body 大小` 等中英混排或未本地化的字段；新增费用 / 省量相关键的中英文。
- **Token 趋势图可读性重做**：原图只有原生 `<title>` 悬停提示（有延迟、一次只显示一条），无法一眼看出每个点用了多少。现改为**悬停十字线 + 数值浮层**：一次列出该时段各模型用量、总计，以及请求数 / 错误数 / 输入 / 输出 / 费用，浮层在图表左右边缘自动翻转对齐；图例补上各模型**总量与占比**（原先只有名字）；空桶明确标注「无请求」。

### 修复
- **Token 趋势图被拉伸变形**：图表用 `preserveAspectRatio="none"` 强制撑满容器，导致圆点被压成椭圆、文字被横向拉伸。现改为等比缩放（`width:100%;height:auto`），实测渲染宽高比 3.214 与 viewBox 的 900:280 完全一致。
- **纯思考段断流时输出 token 被记 0（上一次修复矫枉过正）**：为消除「按响应字节估算」造成的 50 万 token 虚高，估算回退被改成只看可见正文 —— 但推理模型的思考 token 同样是真实产出，于是全程只有思考的断流被记成 0，请求看起来免费（实测末帧 `reasoning` 仍在流，却记「已输出 0 tok」）。现只累加**文本本身**：可见正文 + 推理文本，且 `reasoning` 与 `reasoning_details` 是同内容的两份表示、只取其一（两者都算正是旧版虚高的成因之一）。既不虚高也不记 0；实测流 6000 字节推理文本后断连 → 记 1500 token。注：4 字节/token 对中文偏保守，估算值可能略低 —— 宁可少记也不重复旧版的十余倍虚高。
- **估算值与实测值长得一模一样**：承接上条 —— 估算出的 token 与上游上报的 token 在面板上完全无法区分，等于把猜测当实测展示（这正是连续两次修反的深层原因：只能在"虚高"和"归零"之间二选一，因为没有第三种表达）。现引入第三态：`RequestLog.usage_estimated` 标记「上游未上报 usage」，判据是**是否解析到 usage 对象**而非「token 是否非零」（上报了 usage 但输出确为 0 是合法情况，不能误标）。记录列表在估算行前显示 `≈`（悬停给出说明）、请求详情显示「用量为估算值（上游未返回 usage，非实测）」、省量审计面板的窗口标签追加「N 次用量为估算」。非流式路径同样覆盖：上游不给 usage 时记的 0 会被标记为未上报，不再读成「确实没消耗」。
- **同一个「缓存命中率」有两套分母**：实时统计（托盘窗口）用 `hit / (hit + miss)`，而统计页用 `hit / total_prompt_tokens`（含缓存首次写入）。因 creation 已从 miss 中拆出，前者分母偏小、命中率被系统性高估；又因该模型下 creation 恰为 0，两者数值相等，长期未被发现。现四处（实时统计 / 统计页 / rollup 合并后 / 前端两个辅助函数）统一为 `hit / total_prompt_tokens`。
- **记录页长模型名/供应商名无截断**：模型与供应商单元格既无 `title` 也无宽度上限，名称一长就换行、撑乱行高。现加 `.cell-clip`（单行省略 + `title` 保留完整值）。
- **非满值显示成 100%、非零显示成 0.0%**：`toFixed(1)` 把 99.95%+ 抬成 `100.0%`，`Math.round` 把 99.5%+ 抬成 `100%` —— 舍入把"有没有"这个事实改掉了。实测记录页 **231 条缓存命中率显示 100.0%，而真 100% 的一条都没有**（例 603264/603517 = 99.958%）；成功率同样会出现「成功率 100%」与「错误请求 1」并存的矛盾。现统一走 `fmtPct`：非满值 → `>99.9%`，非零 → `<0.1%`，**无数据 → `—`（既不显示 0 也不显示 100%）**。覆盖图例占比 / 调用分布 / 模型表 / 供应商表 / 单条命中率 / 审计命中率 / 两处成功率共 9 处。
- **概览 hero 在「今日无请求」时整块显示 0**：每天开始（或停用一段时间后）打开面板，页面最显眼的位置六个数全是 0，而卡片里的迷你折线画的是**整个窗口**的曲线（有峰）—— 数字与图形口径不一致，看起来自相矛盾。现今日有请求就显示今日，否则整块回落到窗口累计，并在标题标明当前口径（悬停说明原因）。依据参考项目「同一面板内数字与图形强制同口径」的做法（`Insights.tsx` 的汇总与图表共用同一个 `validRows`/`days`）。
- **趋势图被固定窗口摊平**：29 天窗口里只有最近几天有数据，3/4 画布是贴着 0 的直线，信息全挤在一角。现自动裁掉首尾**没有数据**的桶（两端各留一桶作为边界），并在粒度标签后标出实际展示区间（`天 粒度 · UTC+8 · 09/11 → 09/16`），避免让人误以为展示的就是整个窗口。实测 30 桶 → 6 桶。
- **平均 RPM/TPM 分母含大量空闲**：按整个查询窗口（29 天）当分母，算出「平均 RPM 0.06」读不出任何信息。现按**首个到最后一个有数据的桶**计算活跃跨度（并钳到窗口以内 —— 桶跨度按整天向外取整，不钳会算出比整窗口还小的 RPM），实测 0.06 → 0.42，标签同时注明「活跃期间」。
- **记录页时间列无日期**：日志窗口本身跨天（09/13 → 09/16），只给 `HH:MM:SS` 时同一条 `22:45:42` 无法判断是哪一天（详情弹窗里有完整日期）。现列表时间列改为 `MM/DD HH:MM:SS`。
- **标签中英混排**：记录页的 `tok` / 表头 `TOK` 改为 `Token` / `Tokens`。
- **概览 hero 的「数字与图形同口径」只做到一半，注释与实现不符**：主数字是**今日**口径（`heroToday ? today_* : total_*`），而卡片里的迷你折线**始终**画整个窗口的 `stats.trends`。于是今日有请求时（日常使用的主要场景），会出现「今日 22 请求」旁边配一条峰值上百的窗口曲线 —— 同一张卡片里两个尺度；而代码注释写着「现在数字与图形同口径（都跟 stats 的窗口）」，实际没做到。现统一为**窗口累计**口径（与折线严格同口径），今日用量不再抢主位、改以文字附在提示末尾。**附**：`性能与健康` 面板也有同类混用 —— 成功率用 `today_*` 而旁边的平均延迟、生成速度用窗口，同一张卡片里同样是两个尺度；现成功率一并改为窗口口径（`(total_requests - error_count) / total_requests`）。改动顺带清掉了因此失去引用的 2 个 i18n 键（`errors_only`、`hero_scope_today_hint`，中英各 2 条），复查死键回到 0、中英表仍对齐（各 438 条）。
- **记录页翻到后面的页后，数据收缩会留下一个空白表格**：日志缓冲区会滚动淘汰旧记录，而面板每 3 秒轮询刷新一次 —— 用户停在第 5 页时若列表缩到 2 页，`logPage` 无人夹取，`paginatedLogs` 便切出一个空数组，渲染成**只有表头的空白表格**：底部的「共 N 条」与页码显示看起来都正常，唯独一行数据都没有，极易被误读成"筛选把结果筛没了"。清空全部记录后同理（`logs` 变空但 `logPage` 仍停在原页）。现补 `clampLogPage()` 并在 `fetchLogs`（轮询路径）与 `clearAllLogs` 后调用，把页码夹回有效范围；同时复位清空后的页码。实测：120 条停在第 5 页、收缩到 30 条时，原实现渲染 0 行，修复后自动回到第 2 页并正常显示 10 行。
- **概览 hero 的请求提示把两个口径的数字拼在一句话里**：今日有请求时，提示渲染为「错误 {今日错误} · 总计 {窗口总请求}」——即"错误"取自今日、而"总计"取自整个窗口，配上主数字「今日 22 请求」，同屏出现三个互不相干的数（例：主数字 22、提示「错误 12 · 总计 2598」，其中 2598 既不是 22 也不对应任何可见口径）。现改为两者同为今日口径，提示与主数字可互相验算（错误 + 成功 = 今日请求）。**附**：核对线上实测数据（窗口 2619 请求 / 70 错误，成功率 97.33%）后确认，此前截图上「成功 99.5% + 错误 12 + 本期 2598」三者本身是**自洽**的（同属窗口口径，2586/2598 = 99.5%）—— 真正的问题只在 hero 提示这一处的跨口径拼接，不在成功率本身。
- **统计拉取失败时，界面永远卡在假的「加载中」**：`fetchStats` 的 `loading` 标志**只在 `r.ok` 分支才被置回 `false`**，而失败走的是空的 `catch(e){}` —— 于是 `loading` 停留在初始值 `true`，界面按 `x-if="loading && !stats"` 无限显示加载条。无论令牌失效、后端异常还是网络断开，用户看到的都是「正在加载」，**永远不会变成"失败了"**，也没有任何重试入口（全站 0 个重试按钮）。这与项目「界面不得说假话」的原则直接冲突：它把一个已确定的失败伪装成进行中。现补齐失败路径：非 2xx 与异常两条分支都复位 `loading` 并置位新增的 `statsError`；概览与分析页据此给出「拉取失败」+「刷新」按钮（复用现成的 `fetch_failed` / `refresh` 键，**未新增 i18n**）。同时把概览原有的「等待首个请求」引导页加上 `!statsError` 前置条件 —— 否则失败时会与错误提示同时出现，且内容是错的（失败并不是"等待首个请求"）。
- **分析页「供应商性能」表没有空状态，无数据时只剩一个空表头**：该表的 `<tbody>` 只有 `x-for`，没有任何兜底 —— 窗口内没有请求时，用户看到的是一个只有表头、没有任何说明的空表格，无法区分「确实没有数据」与「正在加载/加载失败」。同页的「模型明细」与「记录」两表都有空状态（且还区分了「无匹配」与「无数据」），这一处属遗漏。现补 `x-if="!(stats.per_provider||[]).length"` + 「暂无数据」空行（复用现成的 `no_data` 键，未新增 i18n）。**注**：扫描还报出若干「缺空状态」，经逐一核对多为合理的既有机制（比例条用 `x-show="xxxSegs.length"` 自身兜底、`provProtocols` 是固定枚举、设置页的时段/品牌列表本身不可为空），均未改动。
- **三个模态框只能用鼠标关闭，键盘用户按 Esc 无反应**：请求详情、更新亮点、添加供应商三个弹窗都只绑了 `@click.self`（点遮罩关闭），**没有任何键盘路径**，也没有 `role="dialog"` / `aria-modal`，关闭按钮是个光秃秃的 `✕`（无 `aria-label`）。现为三者统一补上 `@keydown.escape.window`（各自绑到对应的关闭动作：`selectedLog=null` / `dismissWhatsNew()` / `showAddProvider=false`）、`role="dialog"`、`aria-modal="true"`，并给 `✕` 加上 `aria-label`。**注**：写这处时我一度用了并不存在的 `t('close')` 键，经键存在性检查拦下后改用现成的 `t('cancel')`。
- **同一个「成功率」在概览与记录页配色不一致，记录页把它写死成绿色**：概览那处按阈值变色（95%+ 绿 / 80%+ 橙 / 以下红，无数据灰），而记录页写死 `style="color:var(--ok)"` —— 于是记录里全是错误、成功率为 0% 时，它仍显示一个**绿色的 `0%`**，而同一排的「错误请求」却是红色，同屏自相矛盾。现抽出共用的 `rateColor(pct, text)`，两处统一走同一套阈值（避免以后再次分叉）。配色规则：无数据 → 灰，≥95% → 绿，≥80% → 橙，其余 → 红。**注**：实现时测试发现 `Number(null)` 会等于 `0`，导致「无数据」被误判成「成功率 0%」的红色，故对 `null`/`undefined`/空串单独做了防护。
- **4 处百分比显示绕过 `fmtPct`，把「非满值」舍入成 `100%`、把「无数据」显示成 `0.0%`**：先前引入 `fmtPct` 时统一了 9 处，但漏了这几处 —— 它们直接用 `Math.round(...)+'%'` 或 `.toFixed(1)+'%'`，等于把已经修好的问题又留了几个洞。实测其后果：`9999/10000` 会显示成 `100.0%`（与真正的 100% 无法区分），`1/100000` 会显示成 `0.0%`（与"确实为 0"无法区分）。涉及：概览「模型流量 Top」的成功率（裸 `Math.round`，即截图上「成功 99.5%」旁边那列）、设置页「响应缓存 → 运行命中率」（无数据时硬编码 `'0.0'+'%'`）、审计面板「已生效省量占比」（无数据与真零省量都显示 `0.0%`）、审计面板「长输出占比」。现全部改走 `fmtPct`：非满值给 `>99.9%`、非零给 `<0.1%`、**无数据给 `—`**。其中「已生效省量占比」还区分了「无数据 (`—`)」与「确实没省量 (`0%`)」。**注**：`outInRatio`（输出/输入比）是**比值**不是占比、可以 >100%，有意保持 `.toFixed(2)` 不改。
- **8 个改动型操作静默吞错，「拨了开关没反应」时用户毫不知情**：设置页的运行时可调开关（`setStripReasoning` / `setCacheConfig` / `setMaxHistoryTurns` / `setAutoContinue` / `setStreamTimeout` / `setRetry`）以及 `clearCache` / `resetCircuit`，原先一律写成 `catch(e){}` —— 请求失败时既不抛出、也不回写状态、**零条用户可见提示**。这些开关的状态由 `:class` 绑定（非双向绑定），所以失败时会静默弹回原位：用户看到开关自己跳回去，却不知道是令牌失效、后端异常还是网络断了。实测（用真实函数体 + mock fetch 在 Node 中跑）：失败时提示数 = 0。现按项目既有的 `toast()` 机制补上失败提示（复用现成的 `save_failed` / `network_error` 两个键，**未新增任何 i18n 键**）：`r.ok` 为假时报「保存失败」，抛异常时报「网络错误」。仅补失败分支，未改成功路径、未改任何设置项的行为。**有意未纳入**：`setLang` / `fetchModelMeta`（读取类）、`dismissWhatsNew`（仅关弹窗，失败无害）。
- **「清空所有记录」此前无二次确认，误点即永久销毁全部历史**：该按钮调用 `DELETE /admin/api/logs`，服务端会 `flush()` 后 `clear()` 并 `rollup_clear()` —— 按 `LogBuffer::clear` 与 `rollup_clear` 的实现，**内存与磁盘（日志文件、日级账本）一并重写为空**，没有备份、无法撤销。实测线上这一动作会抹掉 3703 条记录 / 6 天跨度 / 952.4M 输入 token 的统计，其中含 **2161 条历史价格快照**（正是"改价不改写历史账单"的载体，删后永久失去）。对比之下删供应商、删模型都有确认且写明影响范围，唯独破坏力最大的这个操作毫无防护；记录页顶部那个按钮的文案还只是含糊的「清除」，紧挨「刷新 / 导出 JSON」，极易误点。现补 `confirm()`，措辞如实写明「同时删除内存与磁盘上的全部请求日志、日级账本与历史价格快照，且无法恢复；如需留档请先导出 JSON」，并把该按钮文案统一为「清空所有记录」（与设置页一致）。**注**：`t('clear')` 为清缓存与清密钥共用，未改动其取值。
- **省量审计补「构成比例条」**：原先只有一排数字，占比要自己心算。现「已生效省量」「KV 缓存命中率」「潜在可省」三处各加一条分段比例条（纯 CSS `flex-grow` 表达占比，不必先算百分比；段间 2px 缝隙由 `gap` + `overflow:hidden` 形成），配一行图例。效果：剥离省量里「工具轮次」占绝对多数、缓存命中 97.2% 那根橙色细线、潜在省量两项各半 —— 都是一眼可见，而不是读数字后才明白。段数 ≤1 时不渲染（单段没有"构成"可言）。
- **移除「Token 活动」面板**：该面板（含其 JS / CSS / i18n 与逐时取数）整体删除。它先后被误改两次 —— 原实现是「日历热力图」且代码注释里明确写着**与所选粒度解耦**（固定按天聚合），于是选「小时(24)」只有 1 列、选「天(30)」只有 5 列，一小块方格浮在 1149px 宽的面板左侧像没收好的残片，**上方的时间范围选择器等于成了摆设**；随后我把它改成按桶的柱状图（那不是热力图），再改成时段 × 星期热力图，仍未获认可。**根因**：既要它「永远满格」又要它「按日期展开」，在 30 天范围下是互相冲突的（行是「周」、列是「日」就必然是 5 列）。与其继续在冲突里打转，不如整块撤掉 —— 逐日 / 逐时用量已由下方 **Token 趋势图** 覆盖（它铺满宽度、跟随范围、带悬停数值浮层）。时间范围选择器保留，继续驱动趋势图。删除是干净的：已确认 `tokenActivity*` / `fetchHourlyTrends` / `.ta-*` / `.hm-*` / `token_activity_*` 全部清零，且 `fetchStats`、`analysisWindowMinutes`、`.trend-tip`、`.prop-bar` 等相邻代码与样式完好（实测 平均 RPM/TPM 仍为 0.42 / 110.7K，未退化为 NaN）。
- **清理死代码：39 个面板成员 + 133 个 i18n 键**：上一条移除「Token 活动」后，其下属的取数与渲染链条并未随面板一起消失 —— 本次把它们连同其他历史重做遗留一并清掉。删除项（均经引用扫描确认「只有定义、无任何调用」）：热力图残链（`heatmapSvg` / `heatmapHours` / `heatmapTotalRequests` / `hourlyStats` / `fetchHourlyStats` / `tokenActivity*`）、旧趋势图实现（`trendSvg` / `trendSummaryHtml` / `trendMetricLabel` / `trendColor` / `dailyMode`，已被「按模型拆分」的新图 `modelTrendSvgHtml` 取代）、旧侧边栏数据源（`navItems`，导航早已改为硬编码 `<a>`）、旧手动余额 UI（`saveManualBalance` / `clearManualBalance` / `balanceEditProvider` / `balanceEditValue` / `fmtResetIn`）、旧版成功率与占比计算（`analysisSuccessRate` / `modelSuccessRate` / `auditPct`）、已下线筛选（`provNameFilter` / `provProtocolFilter` / `provEffortFilter` / `provFreeOnly` / `provNoKeyOnly` / `clearProvFilters` / `provHasFree` 等）、以及 `estTok` / `circuitClass` / `clearLogs` / `avgGenSpeed` / `maxModelReqs` / `maxDailyReqs` 等。i18n 侧删除 133 个无引用键（中英各 133 条，共 266 条）。验证：JS 语法检查通过、`cargo test --release` 189 项全绿、隔离实例实测页面正常渲染（29 个已删符号确认消失、13 个存活符号确认在位）。
- **`audit_kv_creation` 未定义导致图例显示原始键名**：省量审计的 KV 缓存构成图例调用了 `t('audit_kv_creation')`，但该键在中英表里都没有定义 —— 图例上直接显示出 `audit_kv_creation` 这串原始键名。现补上中文「缓存写入」/ 英文 `cache write`。
- **两个键只有中文、缺英文**：`tbl.strip_reasoning` 与 `strip_toolcall_reasoning_hint` 仅定义了中文，英文界面下会回退显示中文。现补齐英文（含逐模型剥离推理链的完整说明），中英表键集已完全对齐（各 439 条）。
- **记录详情把「响应体大小」标成了「请求体大小」**：弹窗里那一格读的是 `RequestLog.body_len`，而该字段存的是**响应体**字节数（落库处 `body_len: response_body_len`，自 v0.1.0 起即如此；`response_body_len = response_bytes` 是 9/12 「修复统计/记录链路 6 处数据错误」时有意引入的）。实测佐证：该字段与 `completion_tokens` 相关性 **r=+0.965**，与 `prompt_tokens` 仅 **−0.108**；若当作请求体，中位数会低至 **0.49 字节/token**，对 JSON 请求体物理上不可能。现标签改为「响应体大小」/ `Response size`，与数据语义一致（**只改标签，未动数据与口径** —— 该字段的聚合值 `total_body_bytes` 也未在任何面板上展示，故无连带影响）。
- **省量审计补「口径披露」**：面板上的省量 token 一直是**估算值**（被剥离内容的序列化字符数 ÷ 4），但界面上从未说明，容易被读成实测值 —— 这正是本项目反复强调要避免的「看着精确的假数」。现页脚补上估算口径（并说明中文实际约 3 字节/token、真实省量只会更高、费用按该请求自身 KV 命中比例在「未命中价 / 缓存读价」间加权）。**仅补充说明文案，未改任何算法或数字。**
- **分析页五张 KPI 卡的数值基线不齐**：只有「平均 TPM」那张多一行提示，于是同排五个数字高低错落。现每张卡都留出提示行高度（`.an-stat-hint{min-height:14px}`），实测五个数值的顶端坐标已一致。
- **模型明细表空状态不区分**：「筛选后为空」与「确实没有数据」原先都显示「没有匹配的模型」。现按 `per_model` 是否为空分别给出「没有匹配」与「暂无数据」。
- **上游错误文案被截断成中英夹杂**：`translate_upstream_message` 用朴素子串替换短句，而 `Please try again` 是 `Please try again in a moment.` 的前缀 —— 替换后留下残余，实测产出 `... is temporarily unavailable. 请稍后再试 in a moment.` 这种半中半英的残句。现短句**仅在句末替换**（其后只剩标点/空白），并补上两条实际遇到的上游整句（`Upstream model provider is temporarily unavailable`、`All available accounts are currently rate-limited`）的翻译；未收录的长句**原样保留英文**，比替换出残句更清楚。
- **省量审计「其中 tool_calls 轮次」比父项还大**：四项（剥离推理链 / 其中 tool_calls 轮次 / 历史裁剪 / 响应缓存）实为**互斥相加**关系（相加恰等于「已生效省量」，实测 206,574,733 分毫不差），但文案写成「其中」暗示从属关系，于是出现「子项 182.0M > 父项 24.6M」的读数自相矛盾。现两项均标明为「剥离推理链（普通轮次）」与「剥离推理链（工具轮次）」，措辞与算术一致；请求详情里同一拆分一并改正。**数值口径未变** —— 只是因为请求详情的合计是把四项相加，此处不可改成父子层级，否则会重复计数。
- **Anthropic 上游流式请求输入 token 被清零**：`usage_openai` 为缺失字段补 0，导致收尾的 `message_delta` 帧（仅含 `output_tokens`）覆盖掉 `message_start` 已解析的正确输入量，最终退化为"请求字节 ÷ 4"的估算值——输入 token 与按输入计费的费用同时算错。现缺失即不输出，并在累加器侧加防御。
- **缓存命中回放的 token 被重复计入总量**：命中本地响应缓存的请求本未调用上游，却按原始用量写入日志，导致内存汇总、session 计数与日级 rollup 全部虚高；并发去重场景下 10 个相同请求只打 1 次上游，token 总量却近 10 倍。现统一在日志入口清零（省量仍由 `resp_cache_saved_tokens` 单独记账）。
- **长跨度统计退化为只有今天**：rollup 合并判据原为「缓冲区最老日志是否早于今天」，一旦单日请求数超过日志滚动窗口（缓冲区只含今天）就完全不合并，使 29d/365d 查询只剩今天的数据。现改为基于查询起点，并避开仍在累加中的今天 rollup。
- **模型趋势图缺历史**：合并 rollup 时只能往已存在的桶累加，而历史天的桶在日志侧已被剔除，导致其数据被静默丢弃。现支持为历史天新建桶。
- **OpenAI 上游 400「Invalid input」**：工具返回图片时（截图/读图类工具），客户端把 `image_url` 放进 `role: "tool"` 消息的 content 数组，而 OpenAI 协议不支持——严格校验的上游拒绝。现已把图片提升为紧随其后的 user 消息（等整串 tool 消息结束再插入，不破坏 `tool_calls → tool` 配对），图片不丢失。
- **今日 token 合计溢出**：`today_total_*_tokens` 由 u32 改为 u64（5000 条大上下文请求可超出 4.29e9 而静默回绕）。
- **上游 2xx 但读取 body 失败时请求从日志消失**：两条路径补记日志；流中途失败/截断改记 502 并保留已解析的 token（原记 200 且清零，日志表格按 status<400 显示为成功配色）。
- **费用配置面板打不开 / 价格不持久化**：修复弹窗 z-index 低于抽屉、`x-if` 多根元素只渲染遮罩、以及保存与加载两处漏写 `price` 字段（导致保存后文件无价格、刷新后显示"未配置"）。
- **供应商名称不可编辑 / 裸 base URL 无法获取模型**：endpoint 不含 `/chat/completions` 或 `/messages` 时自动追加 `/models`，修复 `https://api.commandcode.ai/provider/v1` 这类裸地址获取模型失败。
- **手动探测后熔断状态被覆盖**：`testProvider` 硬编码 `circuit: 'closed'`，使实际仍处 Open 的供应商隐藏了"重置熔断"按钮；现改为探测后重新同步真实状态。
- **数据目录自动回落到硬编码部署路径**：原逻辑在本地 `data/logs.jsonl` 小于部署目录时自动改用后者，路径写死且会静默改写部署实例的数据。现改为显式 `AIGATE_DATA_DIR`。
- **cache 测试并行串扰**：两个测试共用 `data/cache_enabled.flag`，并行时 `set_enabled` 写盘互相覆盖，约 1/9 概率随机失败。改用每测试独立临时目录。
- **KV 缓存首次写入溢价从未计入费用**：`compute_cost` 的签名里没有 creation 参数，`cache_creation_per_m` 配置完全失效——写入缓存的 token 被并入"未命中输入"按 input 价计。Anthropic 类上游写入常为 input 的 1.25x，属系统性少计。现按三档拆分计价（命中 / 首次写入 / 其余），写入档用 `cache_creation_per_m`（未配置时回退 input 价），rollup 路径同步。
- **价格表同名串价**：价格表原以 model_id 为唯一键，同名模型出现在多个供应商下时后写入者覆盖前者，可能"按 A 家价格给 B 家请求记账"。改为 `(供应商, model_id)` 复合键；且已知供应商但该模型未配价时必须记 0，不得回退到别家同名模型的价格（原来会回退，违反"未配置价格 = 费用 0"）。rollup 侧同步改为按条目自带供应商精确匹配。
- **优化省量卡片补费用**：今日 / 本月 / 累计三个省量维度均显示折算费用（`≈ ¥x`），此前只有 token 数，看不出究竟省了多少钱。新增 `month_opt_saved_fee` 字段（今日与累计本就有费用字段但未展示）。
- **上游未给 usage 时输出 token 被按响应字节放大**：估算回退用的是 `response_bytes / 4`，而 SSE 帧含大量协议开销、且部分上游（opencode 系）同时下发 `reasoning` 与 `reasoning_details`（同内容双份），纯思考场景单条响应体可达 2MB。实测一条请求被记为 **501580** completion token（超过输入的 303766），输出费用虚高十余倍（该条 2.31 元占窗口总费用 14%）。现改为按**可见正文**（`accumulated_content`）估算，全程无正文时记 0 —— 宁可少记也不虚高。
- **日级 rollup 落盘日期恒为 0（数据塌缩）**：`RollupBook::record` 用 `entry(day).or_default()` 建 `DailyRollup`，而 `day_start` 字段保持 `Default` 的 0 且**从未回填** —— 内存里按正确的天聚合，落盘时 `d` 却全写成 0。实测部署实例 `daily_stats.jsonl` 三行 `d` 全为 0，意味着重启加载后所有历史天塌缩成 1970-01-01，按天检索与长跨度统计全部失真。现回填 `day_start = day`，并让 `serialize` 以 BTreeMap 的 key 为权威日期做兜底。
- **`d:0` 化石行在账本里永久驻留**：承接上条，已写出的 `d:0` 行会被载入为 key=0 —— 真实日界永不等于 0，故 `record()` 永远更新不到它；它却会现身按天查询（1970-01-01），并在每次落盘时被原样复写。更糟的是多行 `d:0` 载入时互相覆盖（后写者胜），把其中真正的天彻底抹掉，**整天统计就此消失**。现载入即丢弃并告警，下次落盘不再复写。
- **边界天整天统计消失（日志侧与 rollup 侧同时缺席）**：「边界天（最早日志所在天）保留账本数据」的规则原以「账本非空」为前提，但账本可能压根没有该天（历史 bug / 首次启用 / 被清空）——此时跳过它等于该天在日志侧（被 `merge_end` 排除）与 rollup 侧同时缺席，在统计里彻底隐形。实测部署实例整天丢失 09-12（165 请求 / 2900 万输入 token）。现改为**仅在账本确实持有该天时**保留，否则用日志回填（只有尾部也强于没有）。
- **请求记录与日级 rollup 不冻结价格（历史账单被改写 / 被抹除）**：`RequestLog` 原先只存 token，费用在查询期用**当前** `providers.json` 现算 —— 实测同一批 1451 条历史日志，仅改价就会改写历史金额，**删掉模型配置更会让历史费用整片归零**。现每条请求在写入时结算并随记录落盘当时生效的单价：改价 / 删模型都不再影响已有历史，新请求仍按新价计费（同一批数据里可以新旧价并存，各自结算）。rollup 条目同步带快照，并按价格分列条目 —— 同日改价时前后两段口径不同，合并成一条必然算错一半。旧记录（升级前写入、无快照）回退按当前配置现算，随日志滚出滚动窗口自然退出。（快照取价用非阻塞 `try_read`，避免请求路径已持 registry 读锁时因写者排队而自锁。）
- **省量折算按未命中输入价放大 50 倍**：省下的 token 原先一律按「未命中输入价」折算，而实测 agent 工作流缓存命中率高达 98%（commandcodeAI 98.3% / ginka 93.3%），被省掉的推理链与历史轮次绝大多数本就落在缓存命中区 —— 而 `cache_read` 价与 `input` 价相差约 50x，于是面板把省量价值整体放大约 50 倍（实测「其中 tool_calls 轮次」显示 ¥63.75，按命中比例加权后实为 ¥2.38）。现按**每条请求自身的命中/未命中结构**加权折算，并把散在四处的内联实现（审计明细 / 今日 / 本月 / 累计）统一到同一函数，消除各卡片口径打架。

## [0.5.4] - 2026-09-04

### 新增
- **对外 Responses `/v1/responses` 入口（含原生直通）**：客户端可直接以 Responses API 格式接入（Codex 等）。上游同为 Responses 协议时**原生直通**——请求体仅换模型名/合 extra_body 原样转发，响应 SSE 字节级透传，reasoning/多模态/工具结构零转换损耗；上游为 OpenAI/Anthropic 时转换为 chat 规范复用现有管线（含熔断/缓存/自动续写），响应译回 Responses（官方事件序列：`response.created → output_item.added → delta → done → response.completed`，含 reasoning 与工具调用事件；length 截断发 `response.incomplete`）。usage 按 Responses 口径记账（input_tokens 含缓存读，cached/miss 拆分）。
- **原生直通泛化**：`/v1/messages` 与 `/v1/responses` 的同协议直通统一为一个中继（`relay_native_passthrough`），按上游协议自动解析端点（独立协议端点优先，否则改写 /chat/completions）、错误体风格与记账口径。
- **有状态引用显式拒绝**：`/v1/responses` 的 `previous_response_id` / `item_reference` 依赖服务端会话存储，中转网关无状态——显式返回 400 说明，不静默忽略（避免客户端误以为历史生效）。
- **面板 API 信息更新**：概览页 API 信息与关于页端点列表补齐 `/v1/messages`、`/v1/responses` 入口（含中英文描述）。

### 修复
- （本轮无独立修复项；冒烟覆盖：直通保真比对、双协议转换、错误路径、`/v1/chat/completions` 回归，零 panic）

## [0.5.3] - 2026-08-31

### 新增
- **日级统计持久化 (rollup)**：按「日 × 供应商 × 上游模型」实时聚合请求/tokens/费用/优化省量并落盘 `data/daily_stats.jsonl`，日志 5000 条滚动窗口不再封顶月级统计——分析页「月 (12)/天 (30)」视图可回看日志窗口之外的全量历史，费用按高峰/空闲拆分存储、查询期按最新费率重算（改价可追溯）。
- **对外 Anthropic `/v1/messages` 入口（含原生直通）**：Claude Code、opencode（Anthropic 模式）等客户端可直接指向网关。上游同为 Anthropic 协议时**原生直通**——请求体仅换模型名/合 extra_body 原样转发，响应 SSE 字节级透传，thinking 块 signature、多模态与工具结构零转换损耗；上游为其他协议时译为 OpenAI 规范复用现有路由/熔断/重试/统计管线，响应译回 Anthropic（含流式 SSE、思考块、工具调用）。
- **概览页优化省量卡片**：新增「优化省量」面板，展示今日/本月省 Tokens 与累计起算点；API 信息面板移至右侧栏。
- **记录页详细信息增强**：记录表新增「思考强度」「缓存命中率」列；详情弹窗补缓存命中率/思考强度/生成速度/优化省量。
- **中转 ID 自动生成**：拉取/导入模型时自动按「供应商/模型ID」命名，免手动填写，不满意可改；模型行新增一键复制中转 ID。
- **更新日志面板可滚动**：修复 `.pnl` 的 `overflow:hidden` 覆盖滚动条的问题。

### 修复
- **统计接口全线挂起 (panic)**：`days_between` 在查询范围起点晚于账本最早天时对 `BTreeMap` 传 `start>end` 触发 panic，且 panic 毒化互斥锁导致所有含 rollup 合并的请求连环挂起。已加空范围守卫并将锁获取改为毒化容错，小时档切换不再报错。
- **`/v1/messages` 复制按钮 Alpine 清理报错**：`template x-if` 并列节点在 x-for 清理阶段重复求值触发 `_x_dataStack` 告警，改为单个三元 `x-text`。
- **页面标题与侧栏不一致**：概览页标题「控制台」与侧栏「概览」对齐；移除侧栏底部重复的版本号显示。

## [0.5.2] - 2026-08-30

### 新增
- **统一分析页时间控件**：原先「时间范围」(1d/7d/14d/29d) 与「趋势粒度」(时/天/月) 两套重叠控件合并为单一选择器「小时 (24) / 天 (30) / 月 (12)」——每个选项同时决定窗口与桶大小（24h/30d/12m），消除粒度与范围不一致的混淆。后端新增 `365d` 范围支持月视图 12 个月数据。
- **Token 活动日历横向布局**：日历改为 GitHub 贡献图风格（月份左右走、星期上下走），CSS Grid 列数动态按周数排布，修复原先竖条排列的问题。
- **供应商配置新模型提示**：点击「获取模型」后，供应商列表对应行显示绿色 `新 +N` 徽标，提示有未保存的新模型，保存配置后自动清除。
- **按钮加载反馈**：测试连接、获取模型、保存配置三类操作按钮在执行中显示旋转动画（CSS `.spin`）并禁用，避免重复点击；替代原先静态 ⏳ emoji。
- **供应商模型标签恢复**：模型行重新显示来源标签——绿色 `NEW`（刚获取）、天蓝色 `获取`（已保存的拉取来源）、琥珀色 `手动`（手动添加）、红色 `下架`（已标记移除），`NEW` 标签8秒后自动消失。

### 修复
- **分析页条形图月视图堆叠截断**：条形图 Y 轴 `max` 原按单模型最大值计算，堆叠后总高度超出 SVG 顶部导致柱子被截断。现条形图模式下 `max` 改为取各时间点堆叠总和最大值，折线图仍用单模型最大值保持可比性。
- **后端 `model_trends` 粒度写死 `day`**：`api_stats` 的 `model_trends` 聚合原硬编码为 `"day"`，导致前端切换到小时/月粒度时趋势图数据与 X 轴不匹配。现改为按实际请求的 `granularity` 聚合。
- **Token 活动日历跨粒度渲染错误**：日历算法固定按天步进，但数据来自 `stats.trends`（受粒度影响），小时/月粒度下桶被误当天渲染。现日历始终通过专用 `fetchDailyTrends` 获取日级数据，与趋势粒度解耦。
- **Alpine `defaultApiFormat` 表达式报错**：模型 ID 输入框的 `@input` 处理器中误用 `this.defaultApiFormat(...)`——Alpine 模板表达式中 `this` 不指向组件。改为裸名调用 `defaultApiFormat(...)`。
- **Token 趋势图日期轴与 `model_trends` 不一致**：趋势图 X 轴原仅从 `model_trends`（只含活跃月份）取值，可能与 `stats.trends` 缺失桶错位。改为从 `stats.trends`（权威桶列表）取日期轴，模型数据按需填充零值。
- **分析页标题**：「每日 Token 趋势」更名为「Token 趋势」，匹配统一时间控件（粒度不再固定为日）。

## [0.5.1] - 2026-08-23

### 新增
- **模型元信息悬停与视觉标签**：设置页模型表格行悬停显示「上下文窗口 / 最大输出 / 输入模态 / 推理·工具调用」，支持图像输入的模型在厂商徽章旁显示「👁 视觉」标签；概览页模型用量行同步悬停。数据来自 models.dev 公开库（免费无 key，走系统代理，24h 缓存，失败静默降级），网关别名（`kimi-k3-free` 等）经归一化匹配：剥噪声后缀 + 日期尾段 → 前缀族唯一最佳模糊匹配，私有未收录模型不显示任何信息。

## [0.5.0] - 2026-08-22

### 新增
- **断流自动续写 (P2)**：上游在生成中途掐断连接（无 `finish_reason`/`[DONE]`）时，网关自动以「原始上下文 + 已输出正文 + 继续指令」重新请求，新响应无缝拼进当前 SSE 流，客户端无感知。`TokenStream` 去泛型化（inner 装箱可换段），新增续写状态机（等待 keepalive、换段重置 line_buf/转换器/死循环检测器、usage 跨段累加 ct / prompt 取 max）。安全阀：最多 2 次（面板/环境变量 `AIGATE_AUTO_CONTINUE` 可调 0~5），出现 tool_calls 的流不续写（半截 JSON 拼不回），正文为空不续写。续写失败时收尾日志带原因。
- **设置页运行时可调参数**：断流自动续写次数开关；补齐此前仅环境变量控制的「流空闲超时（30~600s）」「瞬态失败重试次数」「重试退避基数」，全部即时生效，重启回到 `.env` 默认值。

### 修复
- **流截断文案统一**：移除 16K 输出量分级阈值（mimo-v2.5 在 12493 tok 也复现过截断，固定阈值会漏），所有无终止帧断流统一展示「上游在生成中途断流（非网关截断）+ token 数 + 末帧内容」。

## [0.4.9] - 2026-08-21

### 修复
- **流截断根因确认与分级文案**：大样本日志证实「可能被截断」**全部**发生在输出 >1.6 万 token 时（18K~80K），≤8K 的请求从未复现——上游网关对超长生成直接断连而不发 `finish_reason=length`（日志中伴随 `unexpected EOF during chunk size line`）。错误文案按输出量分级：≥16K 标注「上游在长输出中途主动断流（非网关截断）」，否则维持原通用文案。网关侧已兜底补发 `length` 终止帧 + `[DONE]`，客户端可正常收尾并续问。
- **新增 `AIGATE_FORCE_HTTP1=1` 环境变量**：强制上游走 HTTP/1.1（禁用 h2 多路复用）。经系统代理的长流式连接在 h2 下偶发被中间层掐断，此开关用于诊断与缓解（写入 `.env` 重启生效）。
- **流截断诊断增强（"可能被截断"定位）**：`TokenStream` 新增最近 8 条原始 SSE 行环形缓冲，流无终止帧被关闭时，错误日志附带 `已输出/输入 token 数 + 末帧内容`（如 `[末帧: data: {"choices":…} ⏎ …]`），可一眼分辨「上游长输出触顶掐断」vs「系统代理/h2 GOAWAY 断连」vs「终止帧漏解析」。
- **工具大输出中文截断闪退 `is_char_boundary`（`src/proxy.rs:1720`）**：`truncate_tool_outputs` 按字节截断在 UTF-8 多字节字符中间触发断言，`389KB` 中文 `tool` 结果直接 panic。新增 `floor_char_boundary` 按字符边界回退，两处截断均取边界安全点，非 `MAX_PER_TOOL` 刘海。
- **死循环误报（`muse-spark` 1.2M 2.9K 场景）**：`loop_guard` 全局阈值 `384/6/4096 → 512/8/8192` 降低误击，`providers.rs` 新增模型级 `loop_guard` 开关，`proxy.rs` 对 `muse-spark` 默认关闭守卫（可通过 `providers.json: loop_guard: true` 显式开启），截图 `可能被截断` 的 `muse-spark:go 19:53/19:45` 将不再误判为循环。

## [0.4.8] - 2026-08-20

### 修复
- **思考强度全模型可配置（`unknown parameter reasoning_effort`）**：`muse-spark-1.2-contributor/go/responses` 报 400，`thinking.rs` 支持 `reasoningEffort`/`reasoning.effort`/`budgetTokens` 归一与 `muse-spark max→high` 钳制，`responses.rs` 将顶层 `reasoning_effort` 映射为 `reasoning: {effort, summary:"auto"}` 并剔除原字段，`anthropic` 保持 `thinking.budgetTokens`，`chat/completions` 保持透传，`muse-spark` 三档 `low/medium/high` 与 `Go` 全模型可配验证通过。
- **模型用量明细幽灵 `-/` 与历史丢失**：`admin.rs:2020` 同步过滤 `provider="-"`，`main.rs` 启动回落到 `AI中转/data` 恢复 5000 条历史，卡片自适应 `max-width 1200→1600/1800/92%` 修复宽屏留白。

## [0.4.7] - 2026-08-20

### 修复
- **模型用量明细出现 `-/` 幽灵条目**：模型未找到/请求体解析失败时日志 `provider` 记为 `"-"`，`admin.rs:2093` 供应商统计已过滤该占位但 `per_model` 未过滤，导致明细表出现 `-/muse-spark-1.2-contributor`、`-/go-muse` 等 404 占位行（截图红框）。`compute_stats` 的 `model_map` 同步过滤 `provider.is_empty() || provider=="-"`，与供应商统计口径一致；存量 `logs.jsonl` 中的历史占位条目不再计入明细（总量仍保留以反映真实错误率）。

## [0.4.6] - 2026-08-20

### 修复
- **CodeBuddy `muse-spark-1.2-contributor` 工具调用 `10014 Expected 'function' type.`**：Responses 流式 `tool_calls` 增量缺 `type:"function"` 导致 IDE 严格校验失败。`responses.rs` 流式转换补 `type`，`response.output_item.added` 预发 `role` + `tool_calls` 首帧并落盘累积器，后续 `function_call_arguments.delta` 非空回填 `call_id/name` 并累积 `arguments`，`response.completed` 含工具时 `finish_reason` 矫正为 `tool_calls`。
- **非流式空响应**：`proxy.rs` 仅缓存开启时走 `upstream.text()`，缓存关闭的非流式被误入 `stream_response_with_tokens` 按 SSE 解析导致空体。改为 `!is_stream` 双分支（带缓存仅 `put`、不带缓存直接转换返回），Responses/Anthropic 非流 `output`→`message` 均修正。
- **上游 `tool_choice: required/none` 400**：Go 网关仅支持 `auto`，`required/none/指定函数` 均报 `only "auto" is supported`。`openai_to_responses` 统一钳制有工具时 `tool_choice` 为 `auto`，规避 400。
- **历史回放“执行历史命令时遇到格式问题”循环**：`assistant.tool_calls.arguments`/`tool` 结果 `output` 兼容 `string/object/array` 三态，`call_id` 追加 `id` 回落、`output` 多部件拼接，避免 `function_call` 与 `function_call_output` 不成对导致模型重试 `git log --oneline -8`。
- **部署目录双写不一致**：二进制以 `exe_dir` 为 `base`，但真实 `providers.json`/`​.env` 在 `AI中转/`。`config.rs` 的 `load_dotenv` 与 `main.rs` 的 `ProviderRegistry::load` 增加对 `AI中转/` 的回落，启动双向同步 `providers.json`/`​.env`/`AIGate.exe` 到 `target/release/`，Eliminate `OPENCODE_GO_KEY` 缺失导致的 500。

## [0.4.5] - 2026-08-19

### 新增
- 支持 OpenAI Responses API (`/v1/responses`) 协议：新增 `src/responses.rs` 双向转换层（请求 `messages`→`input`、`max_tokens`→`max_output_tokens`、`system`→`instructions`；流式 `response.output_text.delta`→`choices[0].delta.content`；非流 `output` 数组→`choices` 数组）。`providers.rs` 新增 `is_responses()` 方法，`default_api_format` 自动为 `grok-*` / `gpt-5.6-luna` / `muse-spark-*` 标注 `responses`；`proxy.rs` 新增 `responses_mode` 分支（端点改写 + 请求/响应体转换 + `TokenStream::responses_conv`），对客户端完全透明（始终为 OpenAI 兼容）。

## [0.4.4] - 2026-08-19

### 优化
- 分时段精确计费：`ModelPrice` 支持按时间段（峰/谷）切换 input/output/cache 价格。`pricing.rs` 新增 `is_peak(ts)` 与 `effective(price, ts)`，转发计费与概览费用统计按请求时间戳取对应时段单价；管理面板供应商总览卡、供应商统计表、模型用量明细表的费用展示同步走分时段口径，空闲档（无价模型）自然显示无价状态。

### 修复
- 循环守卫（loop_guard）对结构化文本误杀导致流式输出被截断：原 `detect()` 对所有重复单元统一用 `min_repeat=6` 阈值，Markdown 表格分隔行 `|---|`、代码缩进、列表/标题标记等合法重复在 384 字符窗口内极易凑出短重复，被误判为死循环并主动截断（日志中模型自述"刚才输出断了，我接着说完"即被误杀后重试的证据）。修复：`detect()` 新增 `has_meaningful()` 判定——**纯噪声单元（不含字母/数字/汉字的空白/标点/表格线）重复阈值放宽到 `min_repeat+6`（=12）次**，含语义字符的真循环仍按 6 次判定；新增 3 个回归测试锁定语义。
- 供应商统计显示空 "-" 供应商：请求体解析失败 / 模型路由未找到时日志 `provider` 硬编码为 `"-"`，导致概览供应商统计出现无意义的 "-" 条目。修复：`admin.rs` 分组时过滤空值与 `"-"` 占位符，`admin.html` 前端同步过滤双保险。
- 上游请求体过大（413）预检：新增供应商级 `max_request_body_bytes`（默认不限制）。`providers.rs` 的 `ProviderConfig` 加该字段，`proxy.rs` 在转发前预检 `bytes.len()`，超限直接返回中文 413 提示。该字段为可选 opt-in，仅在供应商有明确 body 上限且需提前拦截时按需配置（如某些网关有严格上传大小限制），**不影响默认行为**。go 供应商上游限制约 1.06MB 且非精确值，不宜设置此字段——超限由上游返回自身 413 即可。
- i18n 测试断言对齐：补修预存在的 `circuit_state_cn("closed")` 测试断言（旧断言 `"正常"` 未随代码文案改为 `"运行正常"` 同步，属测试滞后而非代码 bug），全量测试恢复全绿。

## [0.4.3] - 2026-08-18

### 优化
- 响应缓存恢复「仅非流式」语义：本地响应缓存命中分支只服务于非流式请求，流式请求继续走上游实时流式转发（原误把流式也走命中缓存、返回冻结的首块而非实时流）。`cache.rs` / `proxy.rs` 调整命中判定与回退逻辑；启用说明与配置提示文案同步更新为「非流式」口径（中英文）。
- 概览「优化省量」卡片移除全局加总的「今日省费用」数字，仅保留「今日省 Tokens」（不依赖价格、永远准确）；费用改由各供应商总览卡、供应商统计表、模型用量明细表各自展示（ds 等有价模型出钱、mimo 等无价模型自然显示无价状态），避免无价模型被静默归零成 ¥0.00 误导。对应 i18n 死字符串清理。
- 供应商总览卡顺序稳定：主序改为有请求记录的全部供应商（`per_provider`，按请求数降序），余额数据仅作字段 merge；消除并发余额查询返回时序不定导致刷新时供应商卡片位置随机跳动的问题，并使无 `balance_endpoint` 的供应商（如 zen）也能出现并展示优化省量（余额显示「—」）。
- 供应商总览卡新增「今日优化省量」信息：当某供应商今日有转发优化省量时，卡片内显示今日优化省量总 token 及剥离推理链 / 历史裁剪省量明细。后端 `ProviderStats` 新增 `today_opt_saved_tokens` / `today_strip_saved_tokens` / `today_trim_saved_tokens` / `today_resp_cache_saved_tokens` 四个字段，`compute_stats` 供应商分组循环按今日窗口（东八区日界，与 `today_cost` 同口径）求和。
- 概览「优化省量」卡片右侧填充累计节省 Tokens（来自 `stats.total_opt_saved_tokens`），消除卡片右侧空白。

### 修复
- 供应商总览卡优化省量不显示：原 `today_opt_saved_tokens` 等字段仅存在于全局 `UsageStats`，`ProviderStats` 缺字段导致前端读取恒为 `undefined`、区块永不渲染；后端补齐供应商维度字段后解决。
- 余额币种解析错误（美元供应商显示成人民币）：OpenAI/ofox 等兼容余额接口返回扁平字段（如 `{"currency":"usd","balance":6}`），但 `extract_balance_from_json` 的扁平路径只取余额数字、忽略 `currency` 字段且写死返回 `"CNY"`，导致 6 美元误显示为 6 元。修复：扁平路径改为调用 `extract_currency_from_json` 从响应提取币种；新增 `normalize_currency` 归一化 `usd`/`$`→`USD`、`cny`/`¥`→`CNY` 等常见写法，无法识别才回退 CNY；DeepSeek 路径币种同样走归一化。前端余额卡使用后端 `row.currency`，无需改动。
- 供应商总览卡空供应商回归：`providerRows` 换 `per_provider` 主序时混入 `provider` 为空的脏记录（日志 provider 缺失生成空名条目），恢复以 `balanceData`（配置了 `balance_endpoint` 的供应商）为主序并加空名过滤；同时回退误将费用列 `x-show` 改为 `row.billing`（导致已正常显示的今日/累积费用被隐藏），恢复全局 `stats.has_price_config` 口径，并回退后端 `billing` 判定到 `free_ids` 原逻辑，避免波及其他功能。
- 优化省量头条卡片布局：原「今日省 Tokens / 总优化省量」两栏 + 底部剥离/裁剪明细行改为「今日省 Tokens / 本月省 Tokens / 近30天 sparkline」三栏紧凑布局（本月窗口为东八区月首0点至今，替换累计口径；右侧30天柱状图由后端 `opt_saved_series` 按本地日界聚合近30天省量驱动，纯 div 实现无第三方库），今日/本月顶部对齐、消除中间留白。后端 `UsageStats` 新增 `month_opt_saved_tokens` 与 `opt_saved_series: Vec<u64>`。
- 供应商总览卡布局收敛：余额 / 费用（今日+累计）/ 优化省量三块由 `grid-cols-[auto_1fr_auto]` 散开式改为 `flex` 紧凑分组——余额与费用作为左组紧挨、优化省量 `ml-auto` 贴右，消除卡片内横向留白。
- 流式 token 在编程客户端（Cursor/Claude Code 等）不显示：mimo 等模型流式响应不把 usage 放在 `finish_reason:"stop"` 帧，而是发成 `choices:[]` 的独立 usage 帧，标准客户端在收到 `finish_reason:"stop"` 后只读一次 usage、忽略后续独立帧，导致面板能看到 token 而编程工具显示空白。修复：在 `[DONE]` 前**总是补发**一帧带 `usage` 的 `finish_reason:"stop"`（token 取自 AIGate 精确计算值 `final_tokens`，prompt/completion/total 齐全），确保客户端在 `finish_reason:"stop"` 帧即读到消耗。`TokenStream` 新增 `saw_usage` 标记流内是否出现过 usage，便于后续按需精确补发。
- 健康检查页重复显示两个「正常」标签：熔断状态文案原返回 `"正常"`，与供应商「存活正常」语义重叠。修复：`circuit_state_cn` 的 closed 分支文案改为「运行正常」（与「正常」区分，前者指熔断器闭合=流量放行、后者指探测存活），消除概念重复。

## [0.4.2] - 2026-08-15

### 新增
- 缓存首次写入（cache\_creation）独立计费维度：Anthropic 等供应商写入缓存独立计费（常为输入的 1.25x）。`ModelPrice` 新增 `cache_creation_per_m: Option<f64>`（缺失回退输入价，兼容既有配置）；`usage_cache` / 流式解析 / `extract_usage` 全链路从 Anthropic `cache_creation_input_tokens` 与 OpenAI `prompt_tokens_details.cache_creation_tokens` 拆分 creation，落库 `RequestLog.prompt_cache_creation_tokens`；`compute_cost` 按 hit / creation / fresh 三段独立计价，对无 creation 口径的供应商（DeepSeek 等）结果与旧实现完全一致。受 opencode-visual-cache 插件启发补齐。
- 概览「已省」金额指标：累计因 KV 缓存命中节省的费用（命中 token × (input 价 − cache 读价)），直观体现缓存成本价值；无缓存优惠或价格未配置时记 0。
- 费用显示币种切换：设置页新增「显示币种」卡片，内部费用仍以人民币（CNY）为基准计价，前端按静态汇率表换算展示（默认含 USD/EUR/JPY/GBP/HKD），汇率可在面板内直接调整并持久化到 `config/currency.json`，零外部依赖。受 opencode-visual-cache 多币种展示启发补齐。
- 多轮历史推理链自动瘦身：转发上游前移除不含 `tool_calls` 的 assistant 消息中的 `reasoning_content` / `reasoning`（带工具调用的消息保留），默认开启。设置页新增「转发优化」开关可即时切换（`STRIP_HISTORY_REASONING=0` 设定重启后的默认值），避免上游回传的推理链随历史累积浪费输入 token、并干扰 KV 缓存命中。
- 长会话历史裁剪（opt-in）：新增环境变量 `MAX_HISTORY_TURNS`（默认 0 = 不裁剪）。设为正整数后，转发上游前仅保留最近 N 条 user 轮，更早历史整体丢弃（system 始终保留、tool 链随所属轮一并保留/丢弃），降低长会话每轮 input token。默认关闭以免悄悄丢失早期上下文依赖，需主动开启（推荐 10~30）。管理面板「设置 → 转发优化」新增可视化开关 + 数值输入，运行时即时切换（重启回到环境变量默认值）。

### 优化
- 命中率口径统一为 `命中 / 总输入 token`（与 opencode-visual-cache 一致：缓存读 / prompt_tokens）。此前分母为 `命中 + 未命中`，在拆分出 creation 后会偏小；旧日志与无缓存请求等价不变。前端概览卡片、日志徽章 / 详情、模型 / 供应商明细表同步修正。
- **修复：启动预检失败永久钉死熔断导致 503 不退（go 等供应商）**：原 `precheck` 在启动那刹那探测一次，失败即 `force_open` 把熔断永久置为 Open；若此时网络/代理尚未就绪（服务先于网络启动、5s 超时过短等），会误杀健康供应商，且一旦某次 HalfOpen 探测请求因 handler 中途退出漏调 `report_breaker`，`probe_in_flight` 永久为 true → 该供应商死锁在 503 无法自愈。修复：① `main.rs` 启动预检失败**只 WARN 不再 `force_open`**，运行期熔断自愈机制全权负责（首次真实请求仍 Closed 放行，真不可达由运行期熔断接管）；② `circuit_breaker.rs` 新增 `probe_since` 字段，`allow_request` 在 HalfOpen 探测名额被占且超过 `timeout` 仍未回填时**强制释放名额**放行新探测，彻底消除死锁。删除已无引用的 `force_open`。
- **Token 优化① — Anthropic prompt caching 断点注入**：`providers.json` 新增供应商级 `prompt_cache`（默认 true）。走 `/messages` 协议的模型，在 `system` 数组末块 + 最后一条 `user` 消息末块注入 `cache_control: {type:"ephemeral"}`，使上游第二轮起命中 prompt cache（input 按 0.1x 计 + 一次性写入费 1.25x）。联网确认 go 网关的 MiniMax/Qwen 支持该标记，故默认开启；个别网关改写/不支持时报错可设 `"prompt_cache": false` 关闭。纯 OpenAI 协议不注入。
- **Token 优化② — 缓存命中率提升**：`cache.rs::make_key` 规范化时剔除与输出无关的透传字段（`user`/`metadata`/`id`/`stream_options` 及默认 `n:1`），IDE/中间件常带随机值导致缓存 miss 的问题消除——相同实质请求跨细微元数据差异也能命中响应缓存（省重复生成 + 延迟）。保留 `messages`/`model`/`temperature`/`max_tokens`/`tools` 等实质字段，加单测验证。
- **审查修正（2026-08-15）**：`seed` 字段**不剔除**——OpenAI 的 `seed` 直接控制采样确定性（确定性采样），不同 seed 的请求输出可能不同，剔除会导致不同 seed 错误命中同一缓存（返回其他 seed 的输出）。已改为仅 `seed:null`（与缺省等价）时移除，有值必参与哈希；新增 `make_key_seed_must_participate_in_hash` 单测锁定该行为。
- **修复：任务栏 tooltip 悬停提示从不刷新**（根因）：`main.rs` 事件循环用 `ControlFlow::Wait`——没有窗口/托盘事件时事件循环永久阻塞，循环体内的 tooltip 定时更新代码永不执行，悬停看到的永远是启动时初始文本。改为 `ControlFlow::WaitUntil`，按 tooltip 更新周期（配置 1-10s）定时唤醒执行更新；同时复用循环开头读取的配置，避免每次循环重复读 `tooltip.json`。
- **修复：响应缓存 anthropic 模式格式 bug**：非流式缓存写入时存的是上游原始响应体（anthropic 模式为 Anthropic 结构），但命中分支直接把缓存体返回给 OpenAI 客户端 → 格式错误。已改为先做 `anthropic_to_openai_nonstream` 转换再入缓存，命中返回与 OpenAI 客户端格式一致。
- **Token 优化统计闭环（概览页「本轮信息」卡片组）**：管理面板概览页新增「本轮信息」区块（进程启动以来累计，重启清零，不受日志 5000 条滚动窗口封顶影响）——本轮请求数 / 本轮输入 / 本轮输出 / KV 缓存命中率+命中 token / 总优化省量（响应缓存精确 token + 转发优化按 4 字节≈1 token 估算，统一 K/M 制式），下方省量明细行按功能开关拆分（已剥离推理链 / 已裁剪历史 / 响应缓存 N 次·X tok），未开启的优化项明确显示「未开启」而非误导性 0。后端：`LogBuffer` 新增 `session_*` 进程级原子计数（push 时累加），`UsageStats.session` 带出；`cache.rs` 新增 `enabled_hits/enabled_misses` 双口径命中率 + `saved_tokens/enabled_saved_tokens` 省量计数；`AppState` 新增 `strip_saved_chars`/`trim_saved_chars`（`strip_history_reasoning_messages`/`trim_history_turns` 返回被移除内容字节数）经 `GET /admin/api/forward-savings` 供面板展示。至此 KV 缓存（上游）、响应缓存（本地）、转发优化（输入瘦身）三类 token 优化均有实际统计，统计全部归概览页、设置页只留开关。
- **管理面板布局重构**：① 「模型用量明细」表从概览页移入分析页（与热力图/趋势/供应商统计同页，概览只留环形分布）；② 「路由配置」与「健康检查」两页合并为一页（健康检查在上、状态图例居中、路由配置在下），侧边栏由 7 项收敛为 6 项；③ 概览页统计卡复用「今日」口径文案改为「本轮」专属文案，杜绝口径混淆。
- 思考参数整流增强：客户端的 `extra_body.thinking` 提取到顶层统一处理；`reasoning_effort` 新增 `xhigh`→`high` 别名；思考激活（存在 `reasoning_effort`）时自动剥离 `temperature` / `top_p`，避免 DeepSeek-reasoner / Qwen-thinking 等上游因固定采样参数返回 400。
- 概览页新增「累计缓存节省」金额展示（基于既有 `stats.total_cache_saved`），直观呈现 KV 缓存的成本价值。
- 免费标签扩展到概览页：原先「免费」徽章仅在路由配置页显示，现概览页「用量明细表」「模型使用排行」「展开全部模型」「成本分布图例」等所有按模型维度展示处也显示绿色免费徽章。后端在 `api_stats` 持注册表锁时预计算免费中转 ID 集合（`is_free` 判定，与路由页完全一致，含 `free:false` 显式覆盖），`compute_stats` 按组内任一中转 ID 命中标记 `ModelStats.free`，前端统一渲染；不再只依赖路由接口。
- 免费标签扩展到「请求记录」与「错误记录」页：`GET /admin/api/logs` 与 `GET /admin/api/errors` 响应层新增 `free` 标记（不污染磁盘持久化的 `RequestLog` 结构，仅在 API 响应注入），两者复用 `request_logs_with_free` 辅助函数，免费判定同样来自注册表 `is_free`（`free_ids` 在持注册表读锁时构建）。前端记录列表行、展开详情、错误列表中模型名后均显示绿色免费徽章。
- 管理面板设置页重组：原先 7 张卡片平铺无分组、且「响应缓存」开关散落在 health 页。现按逻辑分区加分组标题，并把响应缓存卡片从 health 页移入设置页「性能与优化」组（与转发优化并列，所有运行开关集中）。设置页分区：性能与优化（转发优化+响应缓存）、个性化（界面语言+显示货币）、连接（代理信息）、凭据（API Keys）、监控（任务栏 Tooltip）、供应商（供应商管理表单）；health 页仅留健康检查。切换设置页时新增 `fetchCache()` 拉取缓存数据。i18n 新增 `section.*` 六个分组标题键（中英）。
- 管理面板美术优化（维持深色+紫调、叠加克制星空/银河氛围）：① 侧边栏顶部新增 AIGate 渐变品牌标识，激活导航项加左侧 3px 渐变高亮条+紫调光晕+填充底，hover 平滑过渡；② 概览 6 张统计卡加品类 inline-SVG 图标徽标（请求/成功率/延迟/输入/输出/缓存，各自强调色），数字与副文层次更清晰；③ 设置页 6 个分组标题加左侧强调竖条（`.section-title`）；④ 收敛散落硬编码色到 CSS 变量（`--c-muted`/`--c-border`/`--c-accent2`/`--c-ink` 等），统一配色 token；⑤ 趋势折线图下方加 `<linearGradient>` 面积渐变填充，提升图表质感。纯 CSS 星空/星云（`body::before` 星点 + `body::after` 星云辉光，无动画、零 GPU 负担）。
- 管理面板美术二次微调：① 移除侧边栏顶部渐变品牌标识（用户认为不需要显示）；② 健康页探测列表下方新增「状态图例」说明卡片（健康/异常、熔断 closed/half-open/open 三态、延迟含义及熔断保护提示），填充页面空白并解释各徽章语义；i18n 新增 `health_legend_title`/`legend_*`/`circuit_*`/`legend_hint`（中英）。健康页保持现有「进入时仅在首次自动探测」行为，未加重探测频率。
- 管理面板美术三次收敛（用户：侧边栏美化过了、不够简约；inline-SVG 图标徽标不要底色）：① 侧边栏激活态去除光晕与描边环、渐变高亮条改为 2px 单色细条、底色降至 `rgba(99,102,241,.08)`，整体更克制；② 概览 6 张统计卡的 inline-SVG 图标去除彩色圆角底徽标，仅保留强调色描边（质感来自线性 icon 本身）。
- 管理面板质感与一致性收尾：① 趋势折线/条形图数据点新增原生 `<title>` hover 提示（零 JS、零常驻视觉噪音，鼠标悬停才浮出日期+数值，按当前模式格式化：次数/费用/Token/延迟）；折线圆点放大至 r=3 便于命中。② 配色 token 收尾：散落的 indigo 内联硬编码色（`color:#818cf8`，约 15 处：统计卡图标、路由表 model_id、探测/刷新/重置熔断按钮、链接、about/baseUrl 等）统一收敛到 CSS 变量 `var(--c-indigo)`；供应商统计表成功/错误列、趋势粒度切换按钮同步改用 `--c-ok`/`--c-err`/`--accent`/`--c-muted` 变量，消除风格漂移（`:root` 变量定义保持不变）。
- **修复：趋势图不显示（hover 提示引入的回归）**：`trendSvg` getter 内 `const t=this.stats?.trends` 把局部变量 `t` 遮蔽了全局翻译函数 `t(key)`；新增的 `tipText` 误用 `this.t(...)`（组件实例无此方法→抛错）且在 tokens 之外分支误用 `t(...)`（解析成 trends 数组→不可调用→抛错），导致 getter 抛 `TypeError`、图表空白。修复：tooltip 翻译统一改用 `window.t(...)` 显式调用全局函数，避开局部遮蔽。Node 复刻验证 8 种（折线/条形 × requests/tokens/latency/cost）模式均正常产出 SVG，无抛错。
- **修复：趋势图时间轴不递增（跨月/跨年乱序）**：`compute_trends` 原按 `DailyTrend.date` 字符串排序（`a.date.cmp(&b.date)`），而日期格式为 `MM/DD`（月/日）、月份桶为 `YYYY/MM`。字符串比较在跨月/跨年时失效——如 `"01/05"` 字典序小于 `"12/30"`，导致 1 月桶排到 12 月之前，时间轴倒挂。修复：`DailyTrend` 新增 `ts: u64`（桶起始时间戳），聚合时取桶内首条日志时间戳，排序改为 `a.ts.cmp(&b.ts)`（按真实时间秒数排序），彻底消除跨粒度错序。前端无需改动（只读 `date`）。
- **修复：趋势图时间不对应（未按固定窗口对齐，缺段/段数不定）**：原 `compute_trends` 只对**有请求的桶**出点，导致小时视图段数不固定（<24）、最旧/最新取决于实际数据，与"最近24整点"不符；日/月同理。修复：聚合后按粒度补齐**固定窗口空桶**——hour=最近24整点、day=最近30天、month=最近12个月，含无请求空段，桶起始 ts 对齐到粒度边界（整点/本地0点/本地月首），用 `BTreeMap<ts,桶>` 保证严格递增。窗口起点取 `min(最近N粒度边界, 真实最早桶)`，既保证固定滑动窗口又不丢弃窗口外历史数据。新增 `days_to_ymd`/`days_from_civil`/`bucket_start`/`next_bucket`/`prev_bucket` 本地日历 helper（无 chrono 依赖），并把 `ts_to_date` 重构为复用 `days_to_ymd`（单一真相源）。新增 `test_trend_hour_window_24_segments` 验证小时窗口24段、最新=当前小时、最旧=当前小时-23h、时间轴递增。
- **修复：月趋势图数据变零/串年（图表数据变零的根因）**：`ts_to_month` 年份用 **UTC 天数**（`ts/86400`）推算，而月份用 `ts_to_date`（已按东八区本地切日）。跨年边界——如 UTC 2026-01-01 00:30 在东八区已是 2026-01-01 08:30——月份判为 1 月、年份却算成 2025，生成 `"2025/01"`；该桶 ts 按本地对齐到 2026-01 月首，排序后 date 串到 2025 年末之后，且 `BTreeMap` 去重使 2026/01 真实桶被覆盖/错位 → 月视图整段数值错乱（表现为"变零"）。修复：`ts_to_month` 年份改用**本地时区天数**（`(ts+TZ)/86400`）推算，与 `ts_to_date` 口径一致；并复用 `days_to_ymd` 单一真相源。新增 `test_trend_month_year_boundary` 锁定跨年边界（2026-01-01 必须标 `2026/01` 而非 `2025/01`，且月序列严格递增）。

### 移除
- 整组移除 OpenCode Go 套餐用量 / 一键登录 / 账号管理系统（`go_quota.rs` / `opencode_accounts.rs` / `opencode_oauth.rs` 三模块 + 管理面板卡片/设置区 + `providers.json` 的 `auth_cookie_env` / `workspace_id` 字段）。根因：opencode.ai 把"网站登录 session"与"OAuth API 令牌"当两套独立体系，非官方 scraping 端点（`_server` / dashboard HTML）只认网站 `auth` cookie、不认 OAuth token，且 `/api/account`、`/api/usage` 等公开用量 API 不存在（404），该功能此前实测已完全失效。代理获取 opencode 凭据走 `providers.json` 的 `api_key_env`，与此系统无关，移除不影响代理主功能。用户决策：不再维护脆弱的非官方集成，保持精简。

## [0.4.1] - 2026-08-13

### 新增
- 模型级 `api_format` 覆盖：在模型条目加 `"api_format": "anthropic"` 即可让单个模型走 Anthropic `/messages`，无需为同一供应商拆分多个配置块。解决 OpenCode Go/Zen 网关「同供应商不同模型走不同端点」的问题（如 go 网关 glm/kimi → `/chat/completions`，minimax / qwen3-*-plus·max → `/messages`）。未设置时回落供应商级 `api_format`。
- 「获取模型」拉取上游时，按官方 `go.mdx` / `zen.mdx` 清单（`default_api_format`）自动为 go 的 minimax* / qwen3*-plus·max、zen 的 claude* / qwen3*-plus·max 标注 `api_format: "anthropic"`，其余回落 OpenAI；端点路径在转发时按需由 `/chat/completions` 改写为 `/messages`。

### 新增
- 模型编辑表单新增「协议」列与 `api_format` 下拉（OpenAI / Anthropic），可直接在管理面板为单个模型设置协议格式，无需手改 JSON；保存时仅显式选择 Anthropic 才写字段，空值回落默认。

### 优化
- 模型死循环检测器只分析输出正文（`content`），不再把思考内容（`reasoning_content`/`reasoning`）计入——修复 `reasoning_effort=max` 思考档模型（如 deepseek-v4-flash-GO）因思考高频复述被误判死循环、7% 请求被截断的问题（实机 454/6228 次）。
- 死循环截断日志附带最近检测窗口文本样本（`sample=`），便于事后区分误报与真循环；记录页错误文案区分「死循环截断」与「上游流截断」。
- 窗口双击标题栏从最大化恢复时，WebView 显式重绘（`set_bounds`），修复窗口变成 1px 白边黑屏问题；窗口增加最小尺寸 800×600。
- 启动配置播种守卫放宽：只要 `providers.json` 不存在就自动重建最新默认模板（取消原先「还要求目录缺 `.aigate_initialized` 隐藏标记」的门槛）。修复了「用户删配置后无法自动恢复、程序因读不到文件直接崩溃」的坑；已有真实配置的用户放回原文件即可，不会被覆盖。硬编码默认模板同步更新为模型级 `api_format` 方案（删除过时的 `go-anthropic` 拆分供应商）。

### 修复
- 路由表「端点」列显示与真实转发一致：原先固定显示供应商级基准端点（如 go 供应商下所有模型都显示 `/chat/completions`），现改为按模型级 `api_format` 计算实际转发端点（`proxy.rs` 运行时改写逻辑同步到 `RouteInfo`）——minimax / qwen3 等走 Anthropic 的模型显示 `/messages`，glm / kimi / deepseek 仍显示 `/chat/completions`，并加粉色 `A` 协议徽章，消除「显示 ≠ 真实转发」的歧义。
- 缓存命中率统计修复：Anthropic 通道（go 网关的 minimax / qwen3、zen 的 claude 等走 `/messages`）的 `cache_read_input_tokens` / `cache_creation_input_tokens` 在 Anthropic→OpenAI 转换时被合并进 `prompt_tokens` 丢失，导致 KV 缓存命中/未命中 token 统计恒为 0（命中率恒显 0%）。现转换时平行保留 `prompt_tokens_details.{cached_tokens, cache_creation_tokens}`，下游 `usage_cache` 可正确识别；并修复流式分块 usage 互相覆盖（`if hit>0 / if miss>0` 分写）导致数值错乱的隐患（同事件内 hit 或 miss 任一非零则整体覆盖）。
- 上游流截断错误国际化 + 错误独立留存：管理面板的 stream 截断错误（`stream ended without upstream finish_reason/[DONE] (response likely truncated)`）原硬编码英文且存进日志后不受 i18n 控制，现接入 `i18n::msg_stream_truncated()` 按当前语言生成（中英双语）。**错误展示与"最近 100 条请求"窗口彻底分离**：新增 `GET /admin/api/errors`（`LogBuffer::recent_errors` 按 `status>=400 || error 非空` 维度从全量日志过滤、倒序取最近 100 条错误），前端「请求记录」页顶部独立错误区块（可滚动、显示时间/状态/模型/错误全文）数据来自该独立接口——正常请求再多也不会把错误挤掉（旧版仅在混合 100 条窗口内显示错误摘要，错误仍会被正常流量冲刷掉）。i18n 键 `errors_title`（中英）。
- 上游错误正文静态 i18n 补全：错误类型映射 `i18n::error_type` 新增 `freeusagelimiterror`（FreeUsageLimitError，控制台免费档限额）等免费额度超限键（中英）；新增 `i18n::translate_upstream_message` 保守翻译上游英文错误正文中的常见短语（`Rate limit exceeded`→请求频率超限、`Please try again later`→请稍后再试、`Error from provider (X):`→来自供应商 (X)：等），未知内容（含动态供应商名）原样保留，避免误译。该翻译接入 `format_upstream_error`（HTTP 错误体）与 `translate_sse_error`（流内错误事件）两条路径，使中文用户看到的错误详情也是纯中文（英文原文仍保留便于排障）。单测 `test_format_upstream_error` 新增 FreeUsageLimitError 覆盖并修正既有断言。
- 缓存命中率修复的回归补丁：上一轮缓存修复中，`usage_openai`（流式）对 `message_start` 事件（此时 Anthropic usage 仅含 `input_tokens`、`cached_tokens=0`）也输出了 `prompt_tokens_details`，导致下游 `usage_cache` 第 2 分支误算 `miss = prompt_tokens - 0 = input_tokens`——**未使用缓存的请求**被记成 `miss=input`（0% 命中率但 miss 被填满，属误导）。现仅在确有缓存计数（`cache_read>0 || cache_creation>0`）时才输出该字段；无缓存请求落库回到 (0,0)，与修复前一致。新增单测 `usage_no_cache_no_details` 锁回归。

### 修复
- 费用统计修复：命中本地响应缓存的请求（`cached=true`）此前仍按缓存响应里的原始 token 数计费，与原请求重复计费，导致费用虚高（高缓存命中率场景偏差极大）。`log_cost` 现跳过 `cached` 请求（计 0），`compute_stats`/`compute_trends` 共用该函数，一处改全生效。

### 修复
- 计费口径修复：内置 DeepSeek 官方价此前仅按 `upstream_model`（模型 id）全局套用，导致 opencode/zen/go 等网关中转 `deepseek-v4-flash` 也被自动套成 DeepSeek 官方价（跨供应商串价）。现 `resolve_price` 增加供应商 endpoint 判断，内置价**仅限官方 DeepSeek 供应商**（endpoint host 含 `api.deepseek.com`）生效；网关未手动配 `price` 的请求记「未配置」、费用按 0 计。新增单测 `resolve_price_gateway_no_builtin` 锁回归。

### 优化
- 模型用量明细表新增「单价(元/1M)」列：展示输入价/输出价·缓存价（未配置显示「未配置」），计费口径透明可核对（`ModelStats.price` 取组内首条日志的 `resolve_price` 解析结果，覆盖优先、回退内置表）。
- 概览页费用卡片新增「未配置价格」提示：当存在请求但费用全为 0 时，引导在 `providers.json` 模型条目加 `price`（内置表仅覆盖官方在售的 `deepseek-v4-flash` / `deepseek-v4-pro`，其他供应商需手动配价）。

### 新增
- `pricing.rs` 单元测试（8 例）：覆盖 `compute_cost` 的缓存价 / 无缓存价回退输入价 / 价格未配置 / hit>prompt 钳制，以及 `resolve_price` 的 override 优先 / 内置回退 / `-free`·`-trial` 后缀归一化 / 未知模型返回 None，加固计费正确性。补 8 处测试用 `ModelConfig` 字面量漏加的 `price` 字段。

## [0.4.0] - 2026-08-08

### 新增
- 管理面板页面拆分：概览页（6 张统计卡片 + 模型排行/占比环 + 模型用量明细表）与分析页（使用热力图、趋势图折线/条形切换、供应商表、余额）分离，信息层次更清晰。
- 分析页使用热力图（按星期 × 日期展示调用/Tokens 热度）。
- 趋势图支持折线图与条形图一键切换。
- 设置页新增「代理服务」卡片，可视化显示当前代理模式（系统 / 禁用 / 自定义）及地址。
- 新增「更新亮点」首次启动弹窗：版本升级后首次打开自动展示最新更新日志，标记已读后不再弹出。

### 优化
- 侧边栏重新排序（关于置底），图标垂直居中并补全导航项。
- 全局 UI 美化：玻璃拟态卡片、Inter / JetBrains Mono 字体、统一配色 CSS 变量与渐变按钮。
- 图表渲染修复：改用 JS 生成 SVG 字符串经 `x-html` 注入，解决折线未对齐、条形图空白与图表过大问题。

## [0.3.0] - 2026-08-08

### 新增
- 代理控制开关：支持 `AIGATE_NO_PROXY`（绕过系统代理）与 `AIGATE_PROXY=<url>`（显式指定代理），便于排查上游 TLS 握手失败（如 opencode.ai 间歇性 EOF）。

### 优化
- 上游请求重试条件放宽：在连接/超时错误之外，新增对「请求发送阶段断连」（`is_request`，如 `connection closed before message completed`）的自动重试，应对上游瞬态掉线自愈。
- 概览页 6 张小卡片改为「今日」口径（东八区日界聚合），不再受日志 5000 条滚动上限导致的「总请求」封顶失真影响。

### 补记
- 下列能力此前已随版本发布但缺少更新日志，此处统一补记：模型死循环检测 LoopGuard；缓存命中率兼容 DeepSeek / OpenAI / Anthropic 三套 usage schema；API Key 实时编辑修复；流式 SSE 跨 chunk 行缓冲与归一化转发（修复 IDE 报 `10004` / `10014` 残缺响应）；健康检查 HTTP 探测（修复裸 TCP 误判）；日志轮转；熔断阈值可配置；管理面板 API 鉴权。

## [0.2.0] - 2026-08-03

### 摘要
- 模型死循环检测 LoopGuard（子串连续重复自动截断，不污染客户端正文）。
- 缓存命中率解析兼容 DeepSeek / OpenAI / Anthropic 三套 usage schema。
- API Key 实时编辑修复（keys.json 优先级高于环境变量）。
- 流式 SSE 跨 chunk 行缓冲与归一化转发，修复 IDE 报 `10004` / `10014` 残缺响应。
- i18n 中英双语框架、版本号中枢与构建信息注入。

## [0.1.0] - 2026-07-27

### 摘要
- 初版发布：本地 OpenAI 兼容反向代理。
- 多供应商路由、模型名映射、思考强度注入。
- 熔断（CircuitBreaker）、管理面板、请求日志与统计。
