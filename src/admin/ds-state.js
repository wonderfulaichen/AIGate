function dashboard() {
  return {
    activeTab: 'dashboard',
    sbCollapsed: false,
    loading: true,
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
    // 记录页错误单元格的展开态 {timestamp: bool} —— 按日志时间戳存, 轮询重取 logs 不丢展开.
    logErrOpen: {},

    // Donut chart
    donutColors: CHART_PALETTE,

    // Polling
    pollTimer: null,