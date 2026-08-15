# 更新日志 (Changelog)

所有重要改动记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.2] - Unreleased

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
