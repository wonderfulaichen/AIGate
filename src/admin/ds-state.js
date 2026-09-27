function dashboard() {
  return {
    activeTab: 'dashboard',
    sbCollapsed: false,
    loading: true,
    // ── 首次加载三态外壳 (P1): skeleton(骨架) / empty(空态) / normal(内容) ──
    // skelReady: 骨架宽限期 (250ms, ds-core.init 定时点亮) —— 首载快于宽限期则骨架从不出现, 不闪跳.
    // _skelShownAt: 骨架实际显示时刻; 一旦显示, loading 至少再保持 1s (_endFirstLoad), 避免骨架↔内容来回跳.
    // 后台轮询不碰 loading (仅首次加载管理), 内容永不被刷新清空 —— 无 3s 频闪.
    skelReady: false,
    _skelShownAt: 0,
    // 手动刷新指示: 仅用户点击触发 (后台轮询不置位), 供刷新按钮转圈.
    _refreshing: false,
    // 供应商表单独立加载态 (loadProvidersForm): 不与 stats 的 loading 耦合 —
    // 首次为 null 时才置位, 骨架同样遵守宽限期与最少驻留 1s (_provSkelAt).
    provLoading: false,
    _provSkelAt: 0,
    // 记录页首载: logsFetched 首次置位前不显示统计卡/表格 —— stats 与 logs 是两个独立请求,
    // stats 先到时不能让「logs 还没回来」显示成 0 条 (无数据 ≠ 0); 失败也置位 (退回既有空态行为).
    logsFetched: false,
    // 统计拉取失败标记: 失败时必须复位 loading, 否则界面永远停在「加载中」
    // (原实现只在 r.ok 分支置 loading=false, 失败被 catch 吞掉 → 假"加载中"永不结束).
    statsError: false,
    // 清空记录进行中: 期间禁用按钮并显示「保存中…」.
    // 清理会重写日志文件 + 删除账本, 不是瞬时操作; 无此标记时用户可能连点两次
    // (第二次请求已无数据可清), 或因界面毫无反馈而以为没生效.
    clearingLogs: false,
    version: (window.AIGATE_VERSION && window.AIGATE_VERSION.version) ? window.AIGATE_VERSION : {version:'?', git_commit:'', build_time:''},
    changelog: null,

    // 中转 Base URL: 由当前页面 origin 推导 (admin 与代理同端口), 自动适配实际端口
    baseUrl: (window.location.origin ? window.location.origin : 'http://127.0.0.1:8787') + '/v1',
    copiedField: '',
    copyTimer: null,
    // 第一次请求示例的当前语言页签: 'curl' | 'python' | 'node'
    firstReqTab: 'curl',
    // 概览页 FAQ 手风琴: -1 = 全部收起
    faqOpen: -1,
    // P3: 接入引导 3/3 完成后清单默认收起; 页头「引导完成」徽章可点击重新展开核对.
    onboardShowDone: false,

    // Data
    logs: [], selectedLog: null, health: [], stats: null, cache: null,
    healthLoading: false,
    balanceData: null, balanceLoading: false,
    // Analytics
    chartMode: 'line', // 'line' | 'bar'
    analyticsRange: '29d',
    analyticsSpan: 'day', // 'hour' | 'day' | 'month' — 统一时间范围与粒度 (24h/30d/12m)
    modelDetailFilter: '',
    modelProviderFilter: '',
    modelTrendData: {keys:[],dates:[],legend:''},
    modelTrendSvgHtml: '',
    trendHover: null,
    // 趋势图是否做过自适应裁剪 (首尾空桶已去掉) —— 供上下文标签说明当前展示区间.
    modelTrendTrimmed: false,
    hiddenModels: {},
    // Proxy policy status
    proxyStatus: null,
    // 历史推理链剥离开关 (运行时, 重启回到环境变量默认)
    stripReasoning: false,
    // 「带 tool_calls 的历史推理链」按协议白名单剥离 (运行时, 重启回默认).
    // 注意是协议维度: 供应商只换 endpoint, 协议才决定剥离是否安全.
    // anthropic 无开关 —— 该协议下剥离必然被上游拒绝.
    stripTcChat: true,
    stripTcResp: false,
    // 长会话历史裁剪: 保留最近 user 轮数 (0 = 关闭), 运行时可切换
    maxHistoryTurns: 0,
    // 断流自动续写次数上限 (0 = 关闭), 运行时可调
    autoContinue: 2,
    // 模型元信息 (models.dev): {模型名: {context,output,vision,reasoning,tool_call}|null}
    modelMeta: {},
    // 参考价: {供应商: {上游模型名: {input,output,cache_read,cache_write,provider}}} (USD/1M).
    // 按供应商严格匹配 models.dev —— 同名模型各家中转价差异极大, 不可跨家回退.
    modelCosts: {},
    modelMetaLoading: false,
    _metaSig: '',
    // 降级可回取: 留存开关/统计, 以及查看弹窗 (列表与详情两态)
    recall: { enabled: false, entries: 0, bytes: 0, recalls: 0, evicted: 0 },
    showRecall: false,
    recallEntries: [],
    recallDetail: null,
    recallCopied: false,
    // 常态截断超长 tool 输出的阈值 (字节, 0 = 关闭)
    toolOutputLimit: 0,
    // 静默降级计数 {body_parse_failed, upstream_error_parse_failed}
    degradations: {},
    // 连接参数 (运行时可调): 流空闲超时秒 / 重试次数 / 重试退避基数毫秒
    streamTimeoutSecs: 120,
    retryMax: 1,
    retryBackoffMs: 200,
    // 转发优化省量统计: 推理链剥离 + 历史裁剪累计省下的字符数

    // 更新亮点弹窗：当前版本与已见版本不一致（含首次运行）时弹出。
    showWhatsNew: false,
    seenVersion: '',

    // Settings
    keysByProvider: {}, keyMsg: '',
    providersFormData: null, providersFormMsg: '', providersFormOk: false, isDirty: false, _saving: false,
    provSearch: '', provViewMode: 'table', drawerProv: null,
    // 抽屉「高级端点」折叠状态 (偏好存 localStorage, 跨次打开保留)
    drawerAdvOpen: false,
    // 拉取模型结果弹窗: 新增/已存在/下架 三 Tab 差异视图 + 前缀分组勾选 (参照 new-api fetch-models-dialog)
    showFetchDiff: false,
    fetchDiff: null,    // {pi, added:[{model_id,upstream_model}], existing:[id], removed:[id], message}
    fetchDiffTab: 'new',
    fetchDiffSel: {},   // {model_id: bool} 新增模型的导入勾选
    pricePanelModel: null, pricePanel: {},
    // 价格剪贴板: 复制后可在其他模型面板"粘贴", 或批量应用到勾选的模型.
    priceClipboard: null, priceClipboardSet: false, priceClipMsg: '',
    showAddProvider: false, addProvName: '', addProvEndpoint: '', addProvAnthropicEndpoint: '', addProvResponsesEndpoint: '', addProvBalanceEndpoint: '',
    tooltipConfig: null, tooltipMsg: '',

    // 费用显示币种: 内部费用以 CNY 为基准, 仅展示层换算.
    currencyConfig: { currency: 'CNY', rates: { CNY: 1, USD: 7.2, EUR: 7.8, JPY: 0.048, GBP: 9.1, HKD: 0.92 } },
    currencyRateEdit: 7.2, currencyMsg: '',
    // 分时段计费: 高峰日 / 高峰时段 / 时区 (后端 peak_schedule.json).
    peakSchedule: { enabled: true, tz_offset_hours: 8, weekdays: [1,2,3,4,5,6,7], windows: [{start:'09:00',end:'12:00'},{start:'14:00',end:'18:00'}] },
    peakMsg: '', peakMsgOk: false,

    trendGranularity: 'day',

    // Logs
    logSearch: '', logProviderFilter: '', logStatusFilter: '', logTimeFilter: '', logPage: 0, pageSize: 20,
    // P3: 每页条数选项 (与 core.js PAGE_SIZE_OPTIONS 一致; 白名单校验在 savePageSize).
    pageSizeOptions: [20, 50, 100, 200],
    // 记录页搜索 IME 保护 (P1): logSearchDraft = 输入框实时值 (x-model), logSearch = 实际过滤值.
    // 拼音未上屏 (composing) 期间只改草稿不过滤; 上屏 (compositionend) 立即提交, 普通输入防抖 200ms.
    // 原实现 @input 直写 logSearch —— 中文每敲一个拼音字就过滤一次, 结果闪跳且易误筛.
    logSearchDraft: '', logSearchComposing: false, _logSearchT: null,
    // 记录页错误单元格的展开态 {timestamp: bool} —— 按日志时间戳存, 轮询重取 logs 不丢展开.
    logErrOpen: {},
    // P3 记录表: 排序 (点表头) 与列显隐 (存 localStorage).
    logSortKey: '', logSortDir: 'desc',
    logColsShown: { status: true, error: true, model: true, reasoning: true, provider: true, tokens: true, cache: true, latency: true },
    // 列显隐下拉开合 (点击外部关闭)
    logColsOpen: false,
    // 全局 aria-live 播报文本 (P1): 分页/筛选等状态变化写入, 由 body-shell 的 sr-only 区播报.
    // 只在用户操作 (resetLogView/gotoLogPage) 时更新, 后台轮询不写 —— 无播报轰炸.
    liveMsg: '',
    // 焦点归还: 打开浮层时记录触发元素, 关闭后把焦点还回去 (键盘用户不迷路).
    _lastFocus: null,

    // Donut chart
    donutColors: CHART_PALETTE,

    // Polling
    pollTimer: null,