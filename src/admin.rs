//! 管理面板 — 请求日志环形缓冲区 + 可视化 Web 面板.
//!
//! 访问 http://127.0.0.1:8787/admin 打开面板.
//! 功能: 实时请求日志 / 使用统计 / 路由配置查看 / 健康检查.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::response::Html;
use axum::Json;
use futures::future::join_all;
use serde::Serialize;
use tokio::sync::Mutex;

use crate::store::LogStore;

const LOG_CAPACITY: usize = 100;

/// 单条请求日志.
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct RequestLog {
    pub timestamp: u64,
    pub model: String,
    pub provider: String,
    pub endpoint: String,
    pub status: u16,
    pub latency_ms: u64,
    pub body_len: usize,
    pub error: Option<String>,
    /// 提示 token 数 (从上游响应 usage 中提取).
    #[serde(default)]
    pub prompt_tokens: u32,
    /// 补全 token 数 (从上游响应 usage 中提取).
    #[serde(default)]
    pub completion_tokens: u32,
    /// 是否命中本地响应缓存 (命中则未真实请求上游, 省 token + 延迟).
    #[serde(default)]
    pub cached: bool,
}

/// 内存环形缓冲区 — 存储最近 N 条请求日志, 可选持久化到文件.
#[derive(Clone)]
pub struct LogBuffer {
    inner: Arc<Mutex<VecDeque<RequestLog>>>,
    store: Option<LogStore>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(LOG_CAPACITY))),
            store: None,
        }
    }

    /// 附加持久化存储 (启动时调用).
    pub fn with_store(mut self, store: LogStore) -> Self {
        // 启动时从文件加载已有日志
        let logs = store.load(LOG_CAPACITY);
        if !logs.is_empty() {
            if let Ok(mut buf) = self.inner.try_lock() {
                for log in logs {
                    if buf.len() >= LOG_CAPACITY {
                        buf.pop_front();
                    }
                    buf.push_back(log);
                }
            }
        }
        self.store = Some(store);
        self
    }

    pub async fn push(&self, log: RequestLog) {
        let mut buf = self.inner.lock().await;
        if buf.len() >= LOG_CAPACITY {
            buf.pop_front();
        }
        buf.push_back(log.clone());
        // 异步持久化
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.append(&log).await;
            });
        }
    }

    pub async fn drain(&self) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().rev().cloned().collect()
    }

    /// 清空缓冲区并重写持久化文件.
    pub async fn clear(&self) {
        let mut buf = self.inner.lock().await;
        buf.clear();
        if let Some(store) = &self.store {
            let store = store.clone();
            let logs = Vec::new();
            tokio::spawn(async move {
                store.rewrite(&logs).await;
            });
        }
    }
}

/// 获取当前 Unix 时间戳 (秒).
pub fn now_ts() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

/// 记录一次请求结果到日志缓冲区 (供 proxy.rs 调用).
pub async fn record_request(
    log_buffer: &LogBuffer,
    model: &str,
    provider: &str,
    endpoint: &str,
    status: u16,
    start: Instant,
    body_len: usize,
    error: Option<String>,
) {
    let log = RequestLog {
        timestamp: now_ts(),
        model: model.to_string(),
        provider: provider.to_string(),
        endpoint: endpoint.to_string(),
        status,
        latency_ms: start.elapsed().as_millis() as u64,
        body_len,
        error,
        prompt_tokens: 0,
        completion_tokens: 0,
        cached: false,
    };
    log_buffer.push(log).await;
}

/// 带 token 统计的日志记录 (成功响应用).
///
/// `cached` 标记该响应是否来自本地缓存命中 (命中时未真实请求上游).
pub async fn record_request_with_tokens(
    log_buffer: &LogBuffer,
    model: &str,
    provider: &str,
    endpoint: &str,
    start: Instant,
    prompt_tokens: u32,
    completion_tokens: u32,
    response_body_len: usize,
    cached: bool,
) {
    let log = RequestLog {
        timestamp: now_ts(),
        model: model.to_string(),
        provider: provider.to_string(),
        endpoint: endpoint.to_string(),
        status: 200,
        latency_ms: start.elapsed().as_millis() as u64,
        body_len: response_body_len,
        error: None,
        prompt_tokens,
        completion_tokens,
        cached,
    };
    log_buffer.push(log).await;
}

// ─── API 路由 ───

/// GET /admin — 返回管理面板 HTML.
///
/// 将 API 鉴权令牌注入页面 (仅同源 WebView 可见), 使本地面板能带令牌调用受保护的
/// /admin/api/* 接口; 未配置 AIGATE_ADMIN_TOKEN 时注入 `null`, 不鉴权.
pub async fn admin_page(State(state): State<super::proxy::AppState>) -> Html<String> {
    let token_json = serde_json::to_string(&state.admin_token).unwrap_or_else(|_| "null".to_string());
    let html = ADMIN_HTML.replace("/*__AIGATE_TOKEN__*/", &format!("window.AIGATE_TOKEN = {token_json};"));
    Html(html)
}

/// 管理面板前端页面 (编译时嵌入).
const ADMIN_HTML: &str = include_str!("admin.html");

/// GET /admin/api/logs — 返回最近 100 条请求日志.
pub async fn api_logs(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<RequestLog>> {
    Json(state.log_buffer.drain().await)
}

/// DELETE /admin/api/logs — 清空日志缓冲区.
pub async fn api_logs_delete(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let count = {
        let buf = state.log_buffer.inner.lock().await;
        buf.len()
    };
    state.log_buffer.clear().await;
    Json(serde_json::json!({ "message": format!("已清空 {count} 条记录") }))
}

/// 路由配置的脱敏视图.
#[derive(Serialize)]
pub struct RouteInfo {
    model_id: String,
    provider: String,
    endpoint: String,
    upstream_model: Option<String>,
    reasoning_effort: Option<String>,
}

/// GET /admin/api/routes — 返回当前路由配置.
pub async fn api_routes(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let provider_names: Vec<String> = registry
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();
    let routes: Vec<RouteInfo> = registry
        .model_ids()
        .into_iter()
        .filter_map(|id| {
            let entry = registry.lookup(id)?;
            Some(RouteInfo {
                model_id: id.to_string(),
                provider: entry.provider.name.clone(),
                endpoint: entry.provider.endpoint.clone(),
                upstream_model: entry.model.upstream_model.clone(),
                reasoning_effort: entry.model.reasoning_effort.clone(),
            })
        })
        .collect();
    drop(registry);
    Json(serde_json::json!({
        "providers": provider_names,
        "model_count": routes.len(),
        "routes": routes,
    }))
}

/// GET /admin/api/providers — 返回 providers.json 的原始 JSON (用于编辑器).
pub async fn api_providers_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    match registry.to_json() {
        Ok(json_str) => {
            let parsed: serde_json::Value =
                serde_json::from_str(&json_str).unwrap_or(serde_json::Value::Null);
            Json(serde_json::json!({ "json": json_str, "parsed": parsed }))
        }
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// POST /admin/api/providers/save — 保存 providers.json 并热重载.
pub async fn api_providers_save(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let json_str = match payload.get("json").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => return Json(serde_json::json!({ "error": "缺少 json 字段" })),
    };
    // 写入文件
    if let Err(e) = std::fs::write("providers.json", json_str) {
        return Json(serde_json::json!({ "error": format!("写入文件失败: {e}") }));
    }
    // 热重载
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;
    Json(serde_json::json!({ "message": "配置已保存并重载" }))
}

/// 配置热重载后, 按当前熔断阈值同步熔断表 (新增供应商补齐, 删除的清理).
async fn sync_breakers_for(state: &super::proxy::AppState) {
    let names: Vec<String> = state
        .registry
        .read()
        .await
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();
    super::proxy::sync_breakers(&state.breakers, &names, &state.breaker);
}

/// POST /admin/api/providers/reload — 从磁盘重读 providers.json.
pub async fn api_providers_reload(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;
    Json(serde_json::json!({ "message": "配置已重载" }))
}

/// GET /admin/api/keys — 返回所有 API Key 的脱敏视图.
pub async fn api_keys_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let env_vars: Vec<String> = registry
        .providers()
        .iter()
        .map(|p| p.api_key_env.clone())
        .collect();
    drop(registry);
    let entries = state.key_store.masked_view(&env_vars).await;
    Json(serde_json::json!({ "keys": entries }))
}

/// PUT /admin/api/keys — 更新 API Key.
#[derive(serde::Deserialize)]
pub struct KeyUpdate {
    pub env_var: String,
    pub value: String,
}

pub async fn api_keys_put(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<KeyUpdate>,
) -> Json<serde_json::Value> {
    match state.key_store.set(&payload.env_var, &payload.value).await {
        Ok(()) => {
            if payload.value.is_empty() {
                Json(serde_json::json!({ "message": format!("已清除 {}", payload.env_var) }))
            } else {
                Json(serde_json::json!({ "message": format!("已更新 {}", payload.env_var) }))
            }
        }
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// 健康检查结果.
#[derive(Serialize)]
pub struct HealthEntry {
    provider: String,
    endpoint: String,
    status: String,
    latency_ms: u64,
    /// 熔断状态: closed / open / half-open.
    circuit: String,
}

/// POST /admin/api/providers/test — 测试单个供应商连通性.
#[derive(serde::Deserialize)]
pub struct TestProviderReq {
    endpoint: String,
}

pub async fn api_providers_test(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<TestProviderReq>,
) -> Json<serde_json::Value> {
    let start = std::time::Instant::now();
    let base_url = payload.endpoint.replace("/chat/completions", "/models");
    let status = match state
        .client
        .get(&base_url)
        .header("Authorization", "Bearer probe")
        .timeout(Duration::from_secs(8))
        .send()
        .await
    {
        Ok(resp) => {
            let code = resp.status().as_u16();
            if code == 401 || code == 403 {
                "ok".to_string()
            } else if code < 500 {
                "ok".to_string()
            } else {
                format!("error {code}")
            }
        }
        Err(e) => format!("unreachable: {e}"),
    };
    let latency_ms = start.elapsed().as_millis() as u64;
    Json(serde_json::json!({
        "success": status == "ok",
        "status": status,
        "latency_ms": latency_ms,
    }))
}

/// 模拟测试数据 — 生成假请求日志用于前端调试, 不消耗上游 token.
pub async fn api_mock(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    #[derive(Clone)]
    struct MockRng(u64);
    impl MockRng {
        fn next_u64(&mut self) -> u64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            self.0
        }
        fn gen_range(&mut self, lo: u64, hi: u64) -> u64 {
            lo + (self.next_u64() % (hi - lo))
        }
    }
    let mut rng = MockRng(12345);

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let models = [
        ("big-pickle-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("deepseek-v4-flash-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("mimo-v2.5-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("north-mini-code-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("deepseek-v4-pro-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("kimi-k2.7-code-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("glm-5.2-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("deepseek-v4-flash-DS", "deepseek", "https://api.deepseek.com/v1/chat/completions"),
    ];

    // 生成 80 条成功 + 20 条错误 = 100 条, 分布在最近 7 天
    let mut logs = Vec::with_capacity(100);
    for i in 0..80 {
        let (model, prov, ep) = models[i % models.len()];
        let seconds_ago = rng.gen_range(0, 604800);
        logs.push(RequestLog {
            timestamp: now - seconds_ago,
            model: model.to_string(),
            provider: prov.to_string(),
            endpoint: ep.to_string(),
            status: 200,
            latency_ms: rng.gen_range(300, 6000),
            body_len: rng.gen_range(50, 2000) as usize,
            error: None,
            prompt_tokens: rng.gen_range(50, 500) as u32,
            completion_tokens: rng.gen_range(100, 2000) as u32,
            cached: false,
        });
    }

    let err_msgs = [
        "upstream returned 429: Too Many Requests",
        "upstream returned 500: Internal server error",
        "connection reset by peer",
        "upstream timeout after 30s",
    ];
    for i in 0..20 {
        let (model, prov, ep) = models[(i + 3) % models.len()];
        let seconds_ago = rng.gen_range(0, 604800);
        logs.push(RequestLog {
            timestamp: now - seconds_ago,
            model: model.to_string(),
            provider: prov.to_string(),
            endpoint: ep.to_string(),
            status: if i % 2 == 0 { 429 } else { 502 },
            latency_ms: rng.gen_range(100, 3000),
            body_len: rng.gen_range(50, 2000) as usize,
            error: Some(err_msgs[i % err_msgs.len()].to_string()),
            prompt_tokens: 0,
            completion_tokens: 0,
            cached: false,
        });
    }

    // 写入缓冲区
    for log in &logs {
        state.log_buffer.push(log.clone()).await;
    }

    let count = logs.len();
    Json(serde_json::json!({
        "message": format!("已生成 {count} 条模拟请求日志"),
        "success_count": 80,
        "error_count": 20,
        "model_count": 4,
        "provider_count": 3,
    }))
}

/// GET /admin/api/health — 复用熔断状态 + TCP 连通性预检 (不再打真实 /models, 省 token).
pub async fn api_health(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<HealthEntry>> {
    let providers = state.registry.read().await.providers();
    let precheck_timeout = Duration::from_secs(5);

    // 并发: 读取熔断状态并做 HTTP 连通性预检 (复用 reqwest 客户端, 与真实请求同路径).
    let client = &state.client;
    let mut futs = Vec::new();
    for p in &providers {
        let ep = p.endpoint.clone();
        let cb_state = {
            let g = state.breakers.lock().unwrap();
            g.get(&p.name)
                .map(|cb| cb.peek_state().as_str().to_string())
                .unwrap_or_else(|| "closed".to_string())
        };
        futs.push(async move {
            let start = Instant::now();
            let reachable = super::proxy::precheck_provider(client, &ep, precheck_timeout).await;
            (p.name.clone(), cb_state, reachable, start.elapsed())
        });
    }

    let results = join_all(futs).await;
    let entries = results
        .into_iter()
        .map(|(name, cb, reachable, elapsed)| {
            let endpoint = providers
                .iter()
                .find(|p| p.name == name)
                .map(|p| p.endpoint.clone())
                .unwrap_or_default();
            let latency_ms = elapsed.as_millis() as u64;
            let status = match cb.as_str() {
                "open" => format!(
                    "error: 熔断断开 ({})",
                    if reachable { "TCP 可达" } else { "不可达" }
                ),
                "half-open" => "recovering".to_string(),
                _ => {
                    if reachable {
                        "ok".to_string()
                    } else {
                        "unreachable".to_string()
                    }
                }
            };
            HealthEntry {
                provider: name,
                endpoint,
                status,
                latency_ms,
                circuit: cb,
            }
        })
        .collect();
    Json(entries)
}

/// POST /admin/api/circuit/reset — 手动强制关闭某供应商的熔断 (运维用).
#[derive(serde::Deserialize)]
pub struct CircuitResetReq {
    provider: String,
}

pub async fn api_circuit_reset(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<CircuitResetReq>,
) -> Json<serde_json::Value> {
    let mut g = state.breakers.lock().unwrap();
    match g.get_mut(&payload.provider) {
        Some(cb) => {
            cb.force_close();
            Json(serde_json::json!({ "message": format!("已重置 {} 的熔断", payload.provider) }))
        }
        None => Json(serde_json::json!({ "message": format!("{} 暂无熔断记录", payload.provider) })),
    }
}

// ─── 响应缓存 (实验功能) ───

/// 切换缓存开关的请求体.
#[derive(serde::Deserialize)]
pub struct CacheToggleReq {
    pub enabled: bool,
}

/// GET /admin/api/cache — 返回缓存当前状态与统计 (命中/未命中/条目数).
pub async fn api_cache_get(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    Json(state.cache.stats())
}

/// POST /admin/api/cache — 运行时切换缓存开关 (面板"实验功能"开关).
pub async fn api_cache_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<CacheToggleReq>,
) -> Json<crate::cache::CacheStats> {
    state.cache.set_enabled(payload.enabled);
    Json(state.cache.stats())
}

/// POST /admin/api/cache/clear — 手动清空全部缓存条目 (实验功能调试用).
pub async fn api_cache_clear(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    state.cache.clear();
    Json(state.cache.stats())
}

// ─── 使用统计 ───

/// 单个模型的聚合统计.
#[derive(Debug, Clone, Serialize)]
pub struct ModelStats {
    pub model: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency_ms: f64,
    pub total_body_bytes: usize,
    pub total_prompt_tokens: u32,
    pub total_completion_tokens: u32,
}

/// 单个供应商的聚合统计.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderStats {
    pub provider: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency_ms: f64,
    pub total_body_bytes: usize,
    pub total_prompt_tokens: u32,
    pub total_completion_tokens: u32,
}

/// 日趋势 (通用: 按小时/天/月聚合均使用此结构).
#[derive(Debug, Clone, Serialize)]
pub struct DailyTrend {
    pub date: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency: f64,
}

/// 使用统计聚合结果.
#[derive(Debug, Clone, Serialize)]
pub struct UsageStats {
    pub total_requests: usize,
    pub success_count: usize,
    pub error_count: usize,
    pub total_body_bytes: usize,
    pub avg_latency_ms: f64,
    pub total_prompt_tokens: u32,
    pub total_completion_tokens: u32,
    pub per_model: Vec<ModelStats>,
    pub per_provider: Vec<ProviderStats>,
    pub trends: Vec<DailyTrend>,
    pub top_models: Vec<ModelStats>,
}

/// GET /admin/api/stats?granularity={hour|day|month} — 返回使用统计.
pub async fn api_stats(
    State(state): State<super::proxy::AppState>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<UsageStats> {
    let logs = state.log_buffer.drain().await;
    let granularity = params.get("granularity").map(|s| s.as_str()).unwrap_or("day");
    let mut stats = compute_stats(&logs);
    stats.trends = compute_trends(&logs, granularity);
    Json(stats)
}

/// 将 Unix 时间戳转成 MM/DD 日期字符串 (避免 chrono 依赖).
fn ts_to_date(ts: u64) -> String {
    const SECS_PER_DAY: u64 = 86400;
    let days = ts / SECS_PER_DAY;

    let mut y = 1970i64;
    let mut d = days as i64;

    loop {
        let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
        let dim = if leap { 366 } else { 365 };
        if d < dim {
            break;
        }
        d -= dim;
        y += 1;
    }

    let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    let mdays: [i64; 12] = if leap {
        [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };

    let mut m = 0usize;
    for (i, &md) in mdays.iter().enumerate() {
        if d < md {
            m = i + 1;
            break;
        }
        d -= md;
    }
    if m == 0 {
        m = 12;
    }

    format!("{:02}/{:02}", m, d + 1)
}

fn ts_to_month(ts: u64) -> String {
    let date = ts_to_date(ts);
    let parts: Vec<&str> = date.split('/').collect();
    if parts.len() == 2 {
        let y_days = ts / 86400;
        let mut y = 1970i64;
        let mut remaining = y_days as i64;
        loop {
            let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
            let dim = if leap { 366 } else { 365 };
            if remaining < dim { break; }
            remaining -= dim;
            y += 1;
        }
        let month_num = parts[0].parse::<u32>().unwrap_or(1);
        format!("{y}/{month_num:02}")
    } else {
        date
    }
}

fn ts_to_hour(ts: u64) -> String {
    let date = ts_to_date(ts);
    let hour = (ts % 86400) / 3600;
    format!("{date} {hour:02}:00")
}

/// 从日志列表计算聚合统计.
fn compute_stats(logs: &[RequestLog]) -> UsageStats {
    let total = logs.len();
    let success_count = logs.iter().filter(|l| l.status < 400).count();
    let error_count = total - success_count;
    let total_body_bytes: usize = logs.iter().map(|l| l.body_len).sum();
    let avg_latency_ms = if total > 0 {
        logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / total as f64
    } else {
        0.0
    };
    let total_prompt_tokens: u32 = logs.iter().map(|l| l.prompt_tokens).sum();
    let total_completion_tokens: u32 = logs.iter().map(|l| l.completion_tokens).sum();

    // 按模型分组
    let mut model_map: HashMap<&str, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        model_map.entry(&log.model).or_default().push(log);
    }
    let mut per_model: Vec<ModelStats> = model_map
        .into_iter()
        .map(|(model, logs)| {
            let reqs = logs.len();
            let errs = logs.iter().filter(|l| l.status >= 400).count();
            let bytes: usize = logs.iter().map(|l| l.body_len).sum();
            let avg = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
            let pt: u32 = logs.iter().map(|l| l.prompt_tokens).sum();
            let ct: u32 = logs.iter().map(|l| l.completion_tokens).sum();
            ModelStats {
                model: model.to_string(),
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
            }
        })
        .collect();
    per_model.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按供应商分组
    let mut prov_map: HashMap<&str, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        prov_map.entry(&log.provider).or_default().push(log);
    }
    let mut per_provider: Vec<ProviderStats> = prov_map
        .into_iter()
        .map(|(provider, logs)| {
            let reqs = logs.len();
            let errs = logs.iter().filter(|l| l.status >= 400).count();
            let bytes: usize = logs.iter().map(|l| l.body_len).sum();
            let avg = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
            let pt: u32 = logs.iter().map(|l| l.prompt_tokens).sum();
            let ct: u32 = logs.iter().map(|l| l.completion_tokens).sum();
            ProviderStats {
                provider: provider.to_string(),
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
            }
        })
        .collect();
    per_provider.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按天分组趋势 (默认)
    let trends = compute_trends(logs, "day");

    // Top 5 模型
    let top_models: Vec<ModelStats> = per_model.iter().take(5).cloned().collect();

    UsageStats {
        total_requests: total,
        success_count,
        error_count,
        total_body_bytes,
        avg_latency_ms,
        total_prompt_tokens,
        total_completion_tokens,
        per_model,
        per_provider,
        trends,
        top_models,
    }
}

/// 按指定粒度聚合趋势数据.
fn compute_trends(logs: &[RequestLog], granularity: &str) -> Vec<DailyTrend> {
    let key_fn: fn(u64) -> String = match granularity {
        "hour" => ts_to_hour,
        "month" => ts_to_month,
        _ => ts_to_date,
    };

    let mut map: HashMap<String, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        let key = key_fn(log.timestamp);
        map.entry(key).or_default().push(log);
    }
    let mut trends: Vec<DailyTrend> = map
        .into_iter()
        .map(|(date, logs)| {
            let reqs = logs.len();
            let errs = logs.iter().filter(|l| l.status >= 400).count();
            let avg_lat = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
            DailyTrend {
                date,
                requests: reqs,
                errors: errs,
                avg_latency: avg_lat,
            }
        })
        .collect();
    trends.sort_by(|a, b| a.date.cmp(&b.date));
    trends
}
