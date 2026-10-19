# -*- coding: utf-8 -*-
"""构建自包含预览: src/admin/ 切片按 split_manifest 拼接 + fetch 桩 (场景 ?sc=empty|partial|done).
输出 .openbitfun/tmp/preview/admin.html — 供本地 http.server + ControlHub iframe 实测.
P0 拆分后 src/admin.html 已删除; 拼接顺序与 admin.rs 的 concat! 一致 (见 split_manifest.json)."""
import io, os, re, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
TOOLS = os.path.dirname(os.path.abspath(__file__))                   # 本脚本目录 (split_manifest.json 与脚本同目录)
SRC = os.path.join(ROOT, "src", "admin.html")  # 保留名仅为兼容旧引用; 实际读取见 read_panel()
OUT_DIR = os.path.join(ROOT, ".openbitfun", "tmp", "preview")
OUT = os.path.join(OUT_DIR, "admin.html")

def read_panel():
    man = json.load(io.open(os.path.join(TOOLS, "split_manifest.json"), encoding="utf-8"))
    return "".join(io.open(os.path.join(ROOT, "src", "admin", e["file"]), encoding="utf-8", newline="").read()
                   for e in man)

STUB = r"""<script>
/* preview fetch stub — scenarios: empty(0/3) | partial(1/3) | done(3/3) */
(function () {
  var sc = 'empty';
  try { sc = new URLSearchParams(location.search).get('sc') || 'empty'; } catch (e) {}
  var AUDIT = { output: { output_tokens: 1200, requests: 30, max_tokens: 480, long_requests: 0, loop_truncated: 0 },
    // input_tokens = 实发输入量, 与 applied_saved_tokens 一起才能算出"≈ 不省时是现在的几倍".
    // 刻意给一个可观的比例 (实发 46.2M / 省 8.9M), 与模型明细那行同源, 便于交叉核对。
    input_tokens: 46200000,
    applied_saved_tokens: 8390000, applied_strip_tokens: 8390000, applied_trim_tokens: 0, applied_resp_cache_tokens: 0,
    cache_miss_tokens: 0, cache_read_tokens: 0, exempt_reasoning_tokens: 0, dup_block_tokens: 0, dup_block_count: 0 };
  // 多点图表数据: 趋势图/环图/排名条需要多桶多模型才能体现设计 (否则永远是空态).
  // 固定基线日期而非 Date.now(), 保证每次截图一致.
  var T0 = 1758900000; // 固定起点 (秒)
  var MODELS = ['provA/up-a', 'provA/up-b', 'provB/up-c', 'provB/up-d', 'provC/up-e'];
  function mkTrends(n) {
    var a = [];
    for (var i = 0; i < 24; i++) {
      var ts = T0 + i * 3600;
      var d = new Date(ts * 1000);
      var req = n ? Math.round(2 + 6 * Math.abs(Math.sin(i / 3.1)) + (i % 5)) : 0;
      a.push({ ts: ts, date: (d.getMonth() + 1) + '/' + d.getDate() + ' ' + d.getHours() + 'h',
               requests: req, total_cost: req * 0.03,
               total_prompt_tokens: req * 820, total_completion_tokens: req * 260,
               total_cache_hit_tokens: req * 300 });
    }
    return a;
  }
  function mkModelTrends(n) {
    var a = [];
    if (!n) return a;
    for (var i = 0; i < 24; i++) {
      var ts = T0 + i * 3600;
      for (var m = 0; m < MODELS.length; m++) {
        var p = MODELS[m].split('/');
        var w = Math.max(0, Math.sin((i + m * 3) / 3.4)) * (6 - m);
        if (w <= 0.15) continue;
        a.push({ ts: ts, date: '', provider: p[0], upstream_model: p[1],
                 prompt_tokens: Math.round(w * 900), completion_tokens: Math.round(w * 280) });
      }
    }
    return a;
  }
  function stats(n) {
    var TR = mkTrends(n);
    return { total_requests: n, total_cost: n ? 1.23 : 0, total_prompt_tokens: n * 800, total_completion_tokens: n * 260,
      // 概览三处修复的验证场景: 配了价但本窗口调用的模型都没配价 (真实部署即此情形)
      has_price_config: true, has_priced_request: false,
      today_requests: n ? 12 : 0, today_strip_saved_tokens: n ? 315 : 0, today_trim_saved_tokens: 0,
      today_resp_cache_saved_tokens: 0, month_opt_saved_tokens: n ? 400524507 : 0,
      total_opt_saved_tokens: n ? 400524507 : 0, window_minutes: 6756.6,
      model_trends: mkModelTrends(n),
      trends: TR,
      per_model: n ? MODELS.map(function (k, i) {
        var p = k.split('/');
        return { provider: p[0], upstream_model: p[1], model: k, requests: Math.max(1, 14 - i * 2), errors: i === 3 ? 1 : 0,
                 avg_latency_ms: 620 + i * 130, total_prompt_tokens: (14 - i * 2) * 820, total_completion_tokens: (14 - i * 2) * 260,
                 total_cost: (14 - i * 2) * 0.03, cache_hit: 0.42 - i * 0.05, gen_speed: 54 - i * 3,
                 // 优化省量 (2026-10-16 新增列): 逐模型各不相同, 一眼能看出"不是共用的一个数";
                 // 第 4 个模型刻意设为**不计费** (billing=false) 且金额为 0 —— 用来核对
                 // 「已省」列显示 "—" 而不是 ¥0.00 (免费模型没有费用基数)。
                 optimization_saved_tokens: i === 4 ? 0 : (14 - i * 2) * 4100,
                 today_optimization_saved_tokens: (14 - i * 2) * 210,
                 optimization_saved_fee: i === 4 ? 0 : (14 - i * 2) * 0.11,
                 billing: i !== 4, free: i === 4,
                 // 「输入 Tokens（原始 → 实发）」列 (2026-10-16): 原始 = 实发 + 省量。
                 // 第 4 个模型刻意让省量为 0 → 原始 == 实发 (验证"确实没省"也能如实显示,
                 // 而不是显示 "—" 让人误以为没采集)。
                 total_prompt_tokens_before: (14 - i * 2) * 820 + (i === 4 ? 0 : (14 - i * 2) * 4100),
                 req_bytes_before: (14 - i * 2) * 3300,
                 req_bytes_after: (14 - i * 2) * 2600,
                 // 逐节点阶段耗时 (2026-10-16): 每行**各不相同** —— 用来验证时间轴切节点后
                 // 数字真的变 (若各节点都显示全局值, 就是"全部混在一起"换个地方重演)。
                 // 刻意让 up-a 快、up-d 慢 (等首字 220 vs 940), 一眼能看出区分。
                 stage_summary: { waiting_samples: 10 - i, waiting_sum_ms: (10 - i) * (220 + i * 180),
                                  reasoning_samples: 10 - i, reasoning_sum_ms: (10 - i) * (30 + i * 25),
                                  output_samples: 10 - i, output_sum_ms: (10 - i) * (300 + i * 90),
                                  tool_call_samples: i, tool_call_sum_ms: i * 40 },
                 gateway_stages: {
                   route: { samples: 10 - i, avg_ms: 2 + i, p50_ms: 2, p95_ms: 6 },
                   guard: { samples: 10 - i, avg_ms: 0.4, p50_ms: 0, p95_ms: 3 },
                   convert: { samples: i, avg_ms: 1, p50_ms: 1, p95_ms: 4 },
                   upstream_ttfb: { samples: 10 - i, avg_ms: 300 + i * 140, p50_ms: 280, p95_ms: 900 + i * 60 },
                   overhead: { samples: 10 - i, avg_ms: 4 + i * 2, p50_ms: 4, p95_ms: 15 },
                 },
                 ttft_avg_ms: 280 + i * 90, ttft_samples: Math.max(1, 14 - i * 2) };
      }) : [],
      per_provider: n ? [{ provider: 'provA', requests: 24, errors: 0, rate_limited: 0, avg_latency_ms: 700, total_prompt_tokens: 24 * 800, total_completion_tokens: 24 * 260, total_cost: 1.23,
                           stage_summary: { waiting_samples: 20, waiting_sum_ms: 20 * 310, reasoning_samples: 20, reasoning_sum_ms: 20 * 55, output_samples: 20, output_sum_ms: 20 * 390, tool_call_samples: 2, tool_call_sum_ms: 80 },
                           gateway_stages: { route: { samples: 20, avg_ms: 3, p50_ms: 3, p95_ms: 7 }, guard: { samples: 20, avg_ms: 0.4, p50_ms: 0, p95_ms: 3 }, convert: { samples: 2, avg_ms: 1, p50_ms: 1, p95_ms: 3 }, upstream_ttfb: { samples: 20, avg_ms: 400, p50_ms: 380, p95_ms: 950 }, overhead: { samples: 20, avg_ms: 6, p50_ms: 5, p95_ms: 18 } } },
                         { provider: 'provB', requests: 12, errors: 1, rate_limited: 2, avg_latency_ms: 940, total_prompt_tokens: 12 * 800, total_completion_tokens: 12 * 260, total_cost: 0.61,
                           stage_summary: { waiting_samples: 10, waiting_sum_ms: 10 * 720, reasoning_samples: 10, reasoning_sum_ms: 10 * 140, output_samples: 10, output_sum_ms: 10 * 520, tool_call_samples: 1, tool_call_sum_ms: 30 },
                           gateway_stages: { route: { samples: 10, avg_ms: 5, p50_ms: 5, p95_ms: 12 }, guard: { samples: 10, avg_ms: 0.4, p50_ms: 0, p95_ms: 3 }, convert: { samples: 1, avg_ms: 2, p50_ms: 2, p95_ms: 2 }, upstream_ttfb: { samples: 10, avg_ms: 880, p50_ms: 820, p95_ms: 1600 }, overhead: { samples: 10, avg_ms: 12, p50_ms: 11, p95_ms: 30 } } },
                         { provider: 'provC', requests: 6, errors: 0, rate_limited: 0, avg_latency_ms: 1180, total_prompt_tokens: 6 * 800, total_completion_tokens: 6 * 260, total_cost: 0.28,
                           stage_summary: { waiting_samples: 5, waiting_sum_ms: 5 * 1100, reasoning_samples: 5, reasoning_sum_ms: 5 * 210, output_samples: 5, output_sum_ms: 5 * 640, tool_call_samples: 0, tool_call_sum_ms: 0 },
                           gateway_stages: { route: { samples: 5, avg_ms: 7, p50_ms: 7, p95_ms: 14 }, guard: { samples: 5, avg_ms: 0.4, p50_ms: 0, p95_ms: 3 }, convert: { samples: 0, avg_ms: null, p50_ms: null, p95_ms: null }, upstream_ttfb: { samples: 5, avg_ms: 1350, p50_ms: 1300, p95_ms: 2400 }, overhead: { samples: 5, avg_ms: 19, p50_ms: 18, p95_ms: 42 } } }] : [],
      error_count: n ? 3 : 0,
      // 网关侧阶段 (2026-10-15): 面板「网关自身开销」+ 链路拓扑节点的数据源。
      // 各段都带 samples/avg/p50/p95 —— 缺 samples 时面板会判为"无样本"而不显示。
      // 全局阶段汇总 (供时间轴"全部（混合）"档): 刻意让等首字均值落在各节点之间
      // (220 与 940 的中间 ≈ 500), 用来说明"全局会把慢的稀释掉"。
      stage_summary: n ? {
        waiting_samples: 35, waiting_sum_ms: 35 * 500,
        reasoning_samples: 35, reasoning_sum_ms: 35 * 95,
        output_samples: 35, output_sum_ms: 35 * 430,
        tool_call_samples: 3, tool_call_sum_ms: 110 } : {},
      gateway_stages: n ? {
        route: { samples: 42, avg_ms: 2.4, p50_ms: 2, p95_ms: 6 },
        // guard 均值刻意给 0.4ms (Math.round → 0): 用来验证"实测 0 显示 0ms"而不是 "—"。
        // 护栏本来就是亚毫秒级的活, 0 是常见实测值, 不是"没测到"。
        guard: { samples: 42, avg_ms: 0.4, p50_ms: 0, p95_ms: 3 },
        convert: { samples: 18, avg_ms: 1.2, p50_ms: 1, p95_ms: 4 },
        upstream_ttfb: { samples: 42, avg_ms: 412.5, p50_ms: 380, p95_ms: 980 },
        overhead: { samples: 42, avg_ms: 4.8, p50_ms: 4, p95_ms: 15 }
      } : {},
      // 全局首字延迟汇总 (融合卡底部的分布条需要它; 缺了 ttftHasData 为假 → 那一块不显示)
      ttft: n ? { samples: 35, avg_ms: 412.5, p50_ms: 380, p90_ms: 980, slowest_bucket: 2,
                  hist: [3, 8, 12, 6, 4, 1, 1, 0, 0, 0] } : { samples: 0, hist: [] },
      top_models: n ? [{ model: 'provA/up-a', requests: 14, errors: 0 }, { model: 'provA/up-b', requests: 12, errors: 0 },
                       { model: 'provB/up-c', requests: 10, errors: 1 }, { model: 'provB/up-d', requests: 8, errors: 1 },
                       { model: 'provC/up-e', requests: 6, errors: 0 }] : [],
      avg_latency_ms: 700, gen_speed: 52.1, gen_samples: 30, has_price_config: true,
      // 今日计数器: 后端在写入路径独立累加, 不受日志文件 5000 行滚动上限影响 —— 概览 Hero 的口径来源。
      // 刻意让 today.request 明显小于窗口 total_requests, 一眼能看出"主数字不再是窗口累计"。
      session: { requests: n ? 96 : 0, success: n ? 94 : 0, prompt_tokens: n ? 76800 : 0,
                 completion_tokens: n ? 24960 : 0, cache_hit_tokens: n ? 26400 : 0,
                 today: { requests: n ? 12 : 0, success: n ? 12 : 0,
                          prompt_tokens: n ? 9600 : 0, completion_tokens: n ? 3120 : 0,
                          cache_hit_tokens: n ? 3300 : 0 } },
      source_window: '预览窗口', audit: AUDIT, today_strip_saved_tokens: 0, today_trim_saved_tokens: 0,
      today_resp_cache_saved_tokens: 0, today_opt_saved_fee: 0, month_opt_saved_tokens: 0, month_opt_saved_fee: 0,
      total_opt_saved_tokens: 0, total_opt_saved_fee: 0, opt_saved_since: '2026-01-01' };
  }
  // ?saved=1: 注入有省量的场景 (验证「今日 vs 本月」口径披露与「本月==累计」处理).
  // 默认不注入, 保持其他场景的既有数值不变.
  (function () {
    var on = false;
    try { on = new URLSearchParams(location.search).get('saved') === '1'; } catch (e) {}
    if (!on) return;
    var origStats = stats;
    stats = function (n) {
      var s = origStats(n);
      if (n > 0) {
        s.today_requests = 1022;
        s.today_strip_saved_tokens = 315;
        s.month_opt_saved_tokens = 400524507;
        s.total_opt_saved_tokens = 400524507;
        s.window_minutes = 6756.6;
        s.has_price_config = true;
        s.has_priced_request = false;
      }
      return s;
    };
  })();
  function prov(withModel) {
    var models = {};
    if (withModel) models['provA/up-a'] = { upstream_model: 'up-a', api_format: '', price: { input_per_m: 2, output_per_m: 8 }, origin: 'fetched' };
    return { parsed: { providers: [{ name: 'provA', endpoint: 'http://127.0.0.1:9/v1', balance_endpoint: '', models: models }] } };
  }
  var LOG = { timestamp: Date.now() / 1000, status: 200, model: 'provA/up-a', upstream_model: 'up-a', provider: 'provA',
    endpoint: '/v1/chat/completions', prompt_tokens: 800, completion_tokens: 260, latency_ms: 720, first_token_ms: 310,
    cached: false, cost: 0.41, body_len: 1024, error: '',
    // 阶段耗时: 记录详情的「时间去哪了」+「网关去程」两条都要有数据才看得出瀑布是否对齐;
    // convert_ms 留 null 表示"直通"(未发生协议转换) —— 正是要核对的措辞分支。
    stages: { waiting_first_ms: 310, reasoning_ms: 40, output_ms: 370, tool_call_ms: null,
              route_ms: 2, guard_ms: 1, convert_ms: null, upstream_ttfb_ms: 300, overhead_ms: 5 } };
  // ?logs=N: 造 N 条日志 (验证分页/排序/列显隐需要多页数据; 轮询会覆盖注入值, 故由桩提供)
  var manyLogs = (function () {
    var n = 0;
    try { n = parseInt(new URLSearchParams(location.search).get('logs') || '0', 10) || 0; } catch (e) {}
    var a = [];
    for (var i = 0; i < n; i++) {
      a.push(Object.assign({}, LOG, {
        timestamp: Math.floor(Date.now() / 1000) - i * 60,
        status: i % 7 === 0 ? 502 : 200,
        latency_ms: 200 + i * 3,
        prompt_tokens: 800 + i,
        completion_tokens: 260 + (i % 50),
        error: i % 7 === 0 ? 'upstream connect error (ECONNREFUSED 127.0.0.1:9)' : ''
      }));
    }
    return a;
  })();
  var FIX = {
    empty:    { stats: stats(0),  prov: { parsed: { providers: [] } }, logs: [] },
    partial:  { stats: stats(0),  prov: prov(false), logs: [] },
    done:     { stats: stats(42), prov: prov(true),  logs: [LOG, Object.assign({}, LOG, { status: 502, error: 'upstream connect error' })] }
  };
  var cur = FIX[sc] || FIX.empty;
  // 预览不经过 Rust 的 marker 替换, 手工提供版本号 —— 版本键清理 (ds-core.init) 与头部版本显示需要它
  window.AIGATE_VERSION = window.AIGATE_VERSION || { version: 'PREVIEW' };
  // 桩的**有状态**存储: 这些 setter 会把后端回值写回前端状态 (以服务端为准, 避免
  // "界面显示开了但实际没开")。桩若对 POST 也返回固定值, 点击就不会翻转 ——
  // 看起来像"开关坏了", 实际是桩没模拟服务端行为。故这里记住提交值并回显。
  var STUB_STATE = {
    stripReasoning: true, thinkingPurify: 'rule', stripTcChat: true, stripTcResp: false,
    maxHistoryTurns: 20, autoContinue: 2, streamTimeoutSecs: 120, retryMax: 2, retryBackoffMs: 200,
    toolOutputLimit: 0, cacheEnabled: true, recallEnabled: true,
    cache: { enabled: true, entries: 42, hits: 300, misses: 60, bytes: 524288, evicted: 3,
             max_entries: 200, max_entry_bytes: 262144, saved_tokens: 120000,
             enabled_saved_tokens: 98000, ttl_secs: 600, recalls: 2 },
    recall: { enabled: true, entries: 7, bytes: 4096, evicted: 0, recalls: 2,
              max_entries: 200, max_entry_bytes: 262144 }
  };
  var orig = window.fetch;
  window.fetch = function (url, opts) {
    var u = String(url);
    // POST 时解析提交体, 写进 STUB_STATE 再回显 —— 模拟真实后端的"照实返回"。
    var sent = null;
    try { if (opts && opts.method === 'POST' && opts.body) sent = JSON.parse(opts.body); } catch (e) {}
    if (u.indexOf('/admin/api/') >= 0) {
      var body = {};
      if (u.indexOf('/fetch-models') >= 0) {
        body = { success: true, message: 'Fetched 5 models from upstream',
          models: ['deepseek-chat', 'deepseek-reasoner', 'gpt-4o-mini', 'gpt-4o', 'qwen-max'],
          removed: ['old-gone-model'] };
      }
      else if (u.indexOf('/providers/test') >= 0) {
        body = sc === 'done' ? { success: true, status: 'ok', latency_ms: 231 }
          : { success: false, status: 'unreachable: connection refused (connect ECONNREFUSED 127.0.0.1:9)', latency_ms: 8123 };
      }
      else if (u.indexOf('/stats') >= 0) body = cur.stats;
      else if (u.indexOf('/providers') >= 0) body = cur.prov;
      else if (u.indexOf('/logs') >= 0) body = manyLogs.length ? manyLogs : cur.logs;
      else if (u.indexOf('/balance') >= 0) body = [];
      else if (u.indexOf('/health') >= 0) body = [];
      else if (u.indexOf('/changelog') >= 0) body = { versions: [] };
      else if (u.indexOf('/keys') >= 0) body = { providers: [{ provider: 'provA', configured: true, hint: 'sk-***' }] };
      // 其余后端接口的桩: 缺了它们会返回空对象, 而设置页对 tooltipConfig.metrics.* 做 x-model 绑定,
      // 于是首屏就抛一串 "Cannot read properties of undefined" —— 异常会中断 Alpine 后续子树的渲染
      // (实测导致抽屉永远不出现). 故这里必须给出结构完整的默认值.
      else if (u.indexOf('/tooltip-config') >= 0) body = { enabled: false, update_interval_secs: 3,
        metrics: { requests_per_second: true, avg_latency_ms: true, cache_hit_rate: false, gen_speed: false, today_requests: false } };
      else if (u.indexOf('/proxy-config') >= 0) body = { enabled: false };
      else if (u.indexOf('/peak-schedule') >= 0) body = { enabled: true, tz_offset_hours: 8, weekdays: [1,2,3,4,5,6,7],
        windows: [{start:'09:00',end:'12:00'},{start:'14:00',end:'18:00'}] };
      else if (u.indexOf('/currency') >= 0) body = { currency: 'CNY', rates: { CNY: 1, USD: 7.2 } };
      else if (u.indexOf('/seen-version') >= 0) body = { version: 'PREVIEW' };
      // 设置页这批开关的桩: 字段必须与**后端真实返回结构**逐个对齐 (见 admin.rs 的 json! 键),
      // 否则前端读到 undefined —— 表现是"开关永远是关的 / 数字永远是 0", 而且**不报错**。
      // (这里曾一律返回 {}, 于是 9 个开关在预览里点了毫无反应, 是桩不合格而非产品 bug。)
      else if (u.indexOf('/strip-reasoning') >= 0) {
        if (sent && 'enabled' in sent) STUB_STATE.stripReasoning = !!sent.enabled;
        body = { enabled: STUB_STATE.stripReasoning };
      }
      else if (u.indexOf('/thinking-purify') >= 0) {
        if (sent && sent.mode) STUB_STATE.thinkingPurify = sent.mode;
        // effective = 配置开 且 recall 开 (与后端 purify_params 同判据), 桩必须一致,
        // 否则预览里"提纯生效"的显示与真实行为不符。
        body = { mode: STUB_STATE.thinkingPurify, recall_on: STUB_STATE.recallEnabled,
                 effective: STUB_STATE.thinkingPurify === 'rule' && STUB_STATE.recallEnabled,
                 min_chars: 400, max_nodes: 6 };
      }
      else if (u.indexOf('/strip-toolcall-protocols') >= 0) {
        if (sent) { if ('chat' in sent) STUB_STATE.stripTcChat = !!sent.chat;
                    if ('responses' in sent) STUB_STATE.stripTcResp = !!sent.responses; }
        body = { chat: STUB_STATE.stripTcChat, responses: STUB_STATE.stripTcResp };
      }
      else if (u.indexOf('/max-history-turns') >= 0) {
        if (sent && 'turns' in sent) STUB_STATE.maxHistoryTurns = Math.max(0, sent.turns | 0);
        body = { turns: STUB_STATE.maxHistoryTurns };
      }
      else if (u.indexOf('/auto-continue') >= 0) {
        if (sent && 'count' in sent) STUB_STATE.autoContinue = Math.max(0, sent.count | 0);
        body = { count: STUB_STATE.autoContinue };
      }
      else if (u.indexOf('/stream-timeout') >= 0) {
        if (sent && 'secs' in sent) STUB_STATE.streamTimeoutSecs = Math.max(30, sent.secs | 0);
        body = { secs: STUB_STATE.streamTimeoutSecs };
      }
      else if (u.indexOf('/retry') >= 0) {
        if (sent) { if ('max' in sent) STUB_STATE.retryMax = Math.max(0, sent.max | 0);
                    if ('backoff_ms' in sent) STUB_STATE.retryBackoffMs = Math.max(0, sent.backoff_ms | 0); }
        body = { max: STUB_STATE.retryMax, backoff_ms: STUB_STATE.retryBackoffMs };
      }
      else if (u.indexOf('/tool-output-limit') >= 0) {
        if (sent && 'bytes' in sent) STUB_STATE.toolOutputLimit = Math.max(0, sent.bytes | 0);
        body = { bytes: STUB_STATE.toolOutputLimit };
      }
      // 缓存/recall: 前端直接 `this.cache = await r.json()` (整体替换), 故字段必须齐 ——
      // 缺一个就是 undefined, 且**不报错**。
      else if (u.indexOf('/cache') >= 0) {
        if (sent) {
          if ('enabled' in sent) STUB_STATE.cache.enabled = !!sent.enabled;
          if ('ttl_secs' in sent) STUB_STATE.cache.ttl_secs = Math.max(1, sent.ttl_secs | 0);
          if ('max_entries' in sent) STUB_STATE.cache.max_entries = Math.max(1, sent.max_entries | 0);
        }
        body = STUB_STATE.cache;
      }
      else if (u.indexOf('/recall') >= 0) {
        if (sent) {
          if ('enabled' in sent) { STUB_STATE.recall.enabled = !!sent.enabled; STUB_STATE.recallEnabled = !!sent.enabled; }
        }
        body = STUB_STATE.recall;
      }
      else if (u.indexOf('/degradations') >= 0) body = [];
      var resp = new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
      // ?delay=N: 桩响应延迟 N ms — 用于观察首次加载骨架三态 (P1 p1-load-states)
      var delay = 0;
      try { delay = parseInt(new URLSearchParams(location.search).get('delay') || '0', 10) || 0; } catch (e) {}
      if (delay > 0) return new Promise(function (res) { setTimeout(function () { res(resp); }, delay); });
      return Promise.resolve(resp);
    }
    return orig.apply(window, arguments);
  };
})();
</script>
"""

html = read_panel()
assert "</head>" in html, "no head"
# 错误收集器: 必须**早于 Alpine** 安装 —— Alpine 在 DOMContentLoaded 就初始化,
# 而外层 wrapper 的 window.onerror 挂在 iframe load 之后, 抓不到初始化期的异常
# (实测: 抽屉渲染失败但外层看不到任何错误). 这里把收集结果留给 diag 读取.
ERRS = r"""<script>
window.__ERRS = [];
window.onerror = function (m, s, l, c, e) { window.__ERRS.push(String(m) + ' @line' + l); };
window.addEventListener('unhandledrejection', function (e) { window.__ERRS.push('REJECT ' + e.reason); });
</script>
"""
html = html.replace("</head>", ERRS + STUB + "</head>", 1)
os.makedirs(OUT_DIR, exist_ok=True)
io.open(OUT, "w", encoding="utf-8").write(html)
print("preview built:", OUT, len(html), "chars")

# ?tab=<name>: 让页面**以指定页签启动** —— 把 init 里的 switchTab('dashboard') 换成目标页签。
#
# 为什么必须改源码而不是载入后点侧栏: headless 下 iframe 不可见时响应式刷新被节流,
# Alpine 处理了状态变更但 DOM 不更新 (实测: $data 里 activeTab 已变, 面板仍显示概览),
# 网页截图因此拍不到其它页签。改初始状态则不存在这个时序问题。
TABS = ["dashboard", "analytics", "records", "providers", "settings", "about"]
for t in TABS:
    if t == "dashboard":
        continue  # 概览即默认, 无需变体
    # 注意: 不能只把 switchTab 的目标换掉 —— 原 init 里 switchTab('dashboard') 还负责首次
    # fetchStats(); 直接换目标会跳过它, 首载完成逻辑 (_endFirstLoad) 不跑, loading 恒为真,
    # 页面就永远停在骨架 (实测记录页如此)。故保留一次 fetchStats 再切目标。
    v = html.replace("await this.switchTab('dashboard');",
                     "await this.fetchStats();\n      await this.switchTab('%s');" % t, 1)
    if v == html:
        raise SystemExit("未能替换初始 switchTab —— ds-core.js 的 init 改了? 需同步本脚本")
    p = os.path.join(OUT_DIR, "admin_%s.html" % t)
    io.open(p, "w", encoding="utf-8").write(v)
    print("variant:", p)

# 抽屉变体: 供应商页 + 直接打开第 0 个供应商的抽屉 —— 用于核对最密集的模型表.
# 同样必须先 fetchStats (见上), 否则 loading 恒为真、抽屉里没有数据.
# 抽屉变体: 把 drawerProv 直接写进**初始状态** —— headless 下运行时响应式不刷新
# (实测: 事后改 activeTab/drawerProv, Alpine 数据变了但 DOM 不更新), 只有"首次渲染即带上"
# 才能截到. 故这里替换 ds-state.js 的 drawerProv: null 初值.
MOCK_MODELS = []
for i in range(16):
    # 前 3 条给显式 api_format, 用于核对「自动」与「锁定」在下拉里可区分:
    #   i=0 → 显式 openai (应显示 OpenAI, 实线边框)
    #   i=1 → 显式 responses
    #   i=2 → 空 (自动, 应显示「自动（跟随客户端）」, 虚线边框)
    explicit = {0: 'openai', 1: 'responses', 2: ''}.get(i, '')
    MOCK_MODELS.append(
        '{"model_id":"provA/model-%02d","upstream_model":"up-model-%02d","reasoning_effort":"",'
        '"api_format":"%s","api_formats":null,"free":false,"strip_toolcall_reasoning":false,'
        '"origin":"fetched","_isNew":%s,"_removed":false,"price":%s}'
        % (i, i, explicit, "true" if i < 3 else "false",
           '{"input_per_m":1.0,"output_per_m":2.0}' if i % 3 == 0 else "null"))
MOCK_DRAWER = ('{name:"provA", endpoint:"https://api.example.com/v1/chat/completions", '
               'api_key_env:"K", api_key_default:"d", api_key:"", '
               'probed_protocols:["openai","responses"], headers:null, '
               'api_format:"", endpoint_anthropic:"", endpoint_responses:"", '
               'balance_endpoint:"", prompt_cache:true, openai_cache_control:false, '
               'max_request_body_bytes:0, models:[' + ",".join(MOCK_MODELS) + ']}')

v = html.replace("await this.switchTab('dashboard');",
                 "await this.fetchStats();\n      await this.switchTab('providers');", 1)
assert v != html, "抽屉变体: switchTab 替换失败"
v2 = v.replace("provViewMode: 'card', drawerProv: null,",
               "provViewMode: 'table', drawerProv: %s," % MOCK_DRAWER, 1)
assert v2 != v, "抽屉变体: drawerProv 初值替换失败 —— ds-state.js 改了? 需同步本脚本"
p = os.path.join(OUT_DIR, "admin_drawer.html")
io.open(p, "w", encoding="utf-8").write(v2)
print("variant:", p)

# 记录详情变体: 记录页 + 直接打开第一条记录的详情 —— 用于核对「时间去哪了」与
# 「网关去程」两条瀑布的措辞/配色是否一致 (未采集 → "—", 直通 → "直通")。
# 同抽屉变体: 必须写进**初始状态**, headless 下运行时改 selectedLog 不会重渲染。
v = html.replace("await this.switchTab('dashboard');",
                 "await this.fetchStats();\n      await this.switchTab('records');", 1)
assert v != html, "记录详情变体: switchTab 替换失败"
# 日志对象直接内联 (桩里的 LOG 在 IIFE 内, Alpine 的 data 取不到)。
MOCK_REC = ('{timestamp:1758900000,status:200,model:"provA/up-a",upstream_model:"up-a",provider:"provA",'
            'endpoint:"/v1/chat/completions",prompt_tokens:800,completion_tokens:260,latency_ms:720,'
            'first_token_ms:310,cached:false,cost:0.41,body_len:1024,error:"",end_reason:"stop",'
            # 实测请求体字节 (2026-10-16): 原始 → 实发, 供用户核对"省量是不是真的"。
            # 刻意给一个明显的缩减 (2.2MB → 1.2MB), 与省量列同源可交叉验算。
            'req_bytes:1183238,req_bytes_orig:2275753,'
            'stages:{waiting_first_ms:310,reasoning_ms:40,output_ms:370,tool_call_ms:null,'
            'route_ms:2,guard_ms:1,convert_ms:null,upstream_ttfb_ms:300,overhead_ms:5}}')
v2 = v.replace("logs: [], selectedLog: null,",
               "logs: [], selectedLog: %s," % MOCK_REC)
assert v2 != v, "记录详情变体: selectedLog 初值替换失败 —— ds-state.js 改了? 需同步本脚本"
p = os.path.join(OUT_DIR, "admin_recorddetail.html")
io.open(p, "w", encoding="utf-8").write(v2)
print("variant:", p)
