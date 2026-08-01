//! 管理面板 — 请求日志环形缓冲区 + 可视化 Web 面板.
//!
//! 访问 http://127.0.0.1:8787/admin 打开面板.
//! 功能: 实时请求日志 / 使用统计 / 路由配置查看 / 健康检查.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State};
use axum::response::Html;
use axum::Json;
use futures::future::join_all;
use serde::Serialize;
use tokio::sync::Mutex;

use crate::store::LogStore;

/// 面板「最近请求」实时展示保留的条数 (仅前端展示, 不影响全量统计/持久化).
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
    /// 上游 KV Cache 命中 token 数 (usage.prompt_cache_hit_tokens, DeepSeek 等).
    #[serde(default)]
    pub prompt_cache_hit_tokens: u32,
    /// 上游 KV Cache 未命中 token 数 (usage.prompt_cache_miss_tokens).
    #[serde(default)]
    pub prompt_cache_miss_tokens: u32,
}

/// 内存日志缓冲区 — 内存仅保留最近 `LOG_CAPACITY` 条用于面板实时展示;
/// 文件 `logs.jsonl` 为全量权威数据源 (受 `store::MAX_LINES` 上限约束).
/// 统计/聚合一律基于 [`LogBuffer::drain_all`] 从文件加载的全量数据, 避免跨天/跨月数据被内存容量截断.
#[derive(Clone)]
pub struct LogBuffer {
    inner: Arc<Mutex<VecDeque<RequestLog>>>,
    store: Option<LogStore>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(crate::store::MAX_LINES))),
            store: None,
        }
    }

    /// 附加持久化存储 (启动时调用), 并从文件加载全量日志到内存.
    pub fn with_store(mut self, store: LogStore) -> Self {
        // 启动加载全量日志 (统计基于全量, 不受展示容量限制).
        let logs = store.load(usize::MAX);
        if !logs.is_empty() {
            if let Ok(mut buf) = self.inner.try_lock() {
                for log in logs {
                    if buf.len() >= crate::store::MAX_LINES {
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
        if buf.len() >= crate::store::MAX_LINES {
            buf.pop_front();
        }
        buf.push_back(log.clone());
        // 异步持久化 (文件为全量权威源, 不受内存容量限制).
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.append(&log).await;
            });
        }
    }

    /// 返回全量日志 (用于统计/聚合), 基于内存中加载的全量数据, 不消费.
    pub async fn drain_all(&self) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().cloned().collect()
    }

    /// 将当前内存全量日志同步重写到文件 (退出/清空前调用, 确保尾写不丢).
    pub async fn flush(&self) {
        if let Some(store) = &self.store {
            let store = store.clone();
            let logs = self.drain_all().await;
            store.rewrite(&logs).await;
        }
    }

    /// 同步刷新全量日志到磁盘 (事件循环/退出等非 async 上下文使用, 阻塞当前线程).
    pub fn flush_blocking(&self) {
        if let Some(store) = &self.store {
            // 同步取出全量 (Mutex 在同步上下文锁定).
            let logs = {
                match self.inner.try_lock() {
                    Ok(buf) => buf.iter().cloned().collect::<Vec<_>>(),
                    Err(_) => return,
                }
            };
            store.rewrite_blocking(&logs);
        }
    }

    /// 取最近 N 条 (用于前端展示, 不消费).
    pub async fn recent(&self, n: usize) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().rev().take(n).cloned().collect()
    }

    /// 当前内存中日志条数.
    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    /// 清空缓冲区并重写持久化文件.
    pub async fn clear(&self) {
        let mut buf = self.inner.lock().await;
        buf.clear();
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.rewrite(&[]).await;
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
        prompt_cache_hit_tokens: 0,
        prompt_cache_miss_tokens: 0,
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
    cache_hit_tokens: u32,
    cache_miss_tokens: u32,
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
        prompt_cache_hit_tokens: cache_hit_tokens,
        prompt_cache_miss_tokens: cache_miss_tokens,
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

/// GET /admin/api/logs — 返回最近 100 条请求日志 (展示用, 内存限长).
pub async fn api_logs(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<RequestLog>> {
    Json(state.log_buffer.recent(LOG_CAPACITY).await)
}

/// DELETE /admin/api/logs — 清空日志缓冲区.
pub async fn api_logs_delete(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let count = state.log_buffer.len().await;
    // 清空内存展示缓冲与持久化文件. 删除前先同步落盘已有数据, 避免异步尾写丢失.
    state.log_buffer.flush().await;
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

/// POST /admin/api/providers/:name/fetch-models
/// 从上游 `/v1/models` 拉取模型列表, 合并进 provider 并持久化到 providers.json.
///
/// 拉取到的模型 `reasoning_effort` 留空, 由客户端 (opencode / CodeBuddy 等) 自行调节思考档位.
/// 已存在的模型 ID 不会被覆盖 (保留用户自定义的 upstream_model / reasoning_effort).
pub async fn api_providers_fetch_models(
    State(state): State<super::proxy::AppState>,
    Path(name): Path<String>,
) -> Json<serde_json::Value> {
    // 1) 取 provider 配置 + 真实 key (只读锁, 取完即释放, 不跨 await 持有)
    let (provider, key) = {
        let registry = state.registry.read().await;
        let provider = match registry.providers().into_iter().find(|p| p.name == name) {
            Some(p) => p,
            None => return Json(serde_json::json!({ "error": format!("未找到供应商: {name}") })),
        };
        let key = match registry.api_key(&provider, &state.key_store).await {
            Ok(k) => k,
            Err(e) => return Json(serde_json::json!({ "error": e })),
        };
        (provider, key)
    };

    // 2) 向上游拉取模型 (用真实 key 鉴权)
    let ids = match crate::providers::fetch_models_from_upstream(&state.client, &provider, &key).await
    {
        Ok(ids) => ids,
        Err(e) => return Json(serde_json::json!({ "error": e })),
    };

    // 3) 合并进内存注册表 (新增未存在的, 跳过已有)
    let (added, skipped) = {
        let mut registry = state.registry.write().await;
        registry.add_models(&name, &ids)
    };

    // 4) 持久化到 providers.json (含新增模型)
    {
        let registry = state.registry.read().await;
        let json_str = match registry.to_json() {
            Ok(s) => s,
            Err(e) => return Json(serde_json::json!({ "error": e })),
        };
        drop(registry);
        if let Err(e) = std::fs::write("providers.json", &json_str) {
            return Json(serde_json::json!({ "error": format!("写入文件失败: {e}") }));
        }
    }

    // 5) 热重载 (重建路由表, 使 /v1/models 立即包含新模型) + 同步熔断表
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;

    Json(serde_json::json!({
        "success": true,
        "provider": name,
        "fetched": ids.len(),
        "added": added,
        "skipped": skipped,
        "message": format!(
            "已从上游拉取 {} 个模型, 新增 {} 个, 跳过 {} 个已存在",
            ids.len(), added, skipped
        ),
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
            prompt_cache_hit_tokens: rng.gen_range(0, 400) as u32,
            prompt_cache_miss_tokens: rng.gen_range(0, 100) as u32,
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
            prompt_cache_hit_tokens: 0,
            prompt_cache_miss_tokens: 0,
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
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
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
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
}

/// 日趋势 (通用: 按小时/天/月聚合均使用此结构).
#[derive(Debug, Clone, Serialize)]
pub struct DailyTrend {
    pub date: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency: f64,
    /// 提示 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_prompt_tokens: u64,
    /// 补全 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_completion_tokens: u64,
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
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
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 命中率 = 命中 token / (命中 + 未命中) token.
    pub cache_hit_rate: f64,
}

/// GET /admin/api/stats?granularity={hour|day|month} — 返回使用统计.
pub async fn api_stats(
    State(state): State<super::proxy::AppState>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<UsageStats> {
    let logs = state.log_buffer.drain_all().await;
    let granularity = params.get("granularity").map(|s| s.as_str()).unwrap_or("day");
    let mut stats = compute_stats(&logs);
    stats.trends = compute_trends(&logs, granularity);
    Json(stats)
}

/// 本地时区偏移 (秒). 默认按东八区 (UTC+8) 切分日/月界, 使趋势符合用户日历.
/// 若需跟随系统时区可改为读取本地 UTC 偏移, 但固定东八区对国内用户更可预期.
const TZ_OFFSET_SECS: i64 = 8 * 3600;

/// 将 Unix 时间戳转成 MM/DD 日期字符串 (按东八区切日界, 避免 chrono 依赖).
fn ts_to_date(ts: u64) -> String {
    const SECS_PER_DAY: u64 = 86400;
    // 先按本地时区偏移归到本地当天 0 点, 再取天数.
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let local = if local < 0 { 0 } else { local as u64 };
    let days = local / SECS_PER_DAY;

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
    // 小时也按本地时区归位.
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let hour = (((local as u64) % 86400) / 3600) as u32;
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
    let total_cache_hit_tokens: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
    let total_cache_miss_tokens: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
    let cache_hit_rate = if total_cache_hit_tokens + total_cache_miss_tokens > 0 {
        total_cache_hit_tokens as f64 / (total_cache_hit_tokens + total_cache_miss_tokens) as f64
    } else {
        0.0
    };

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
            let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
            let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
            ModelStats {
                model: model.to_string(),
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
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
            let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
            let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
            ProviderStats {
                provider: provider.to_string(),
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
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
        total_cache_hit_tokens,
        total_cache_miss_tokens,
        cache_hit_rate,
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
            let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
            let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
            let pt: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
            let ct: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
            DailyTrend {
                date,
                requests: reqs,
                errors: errs,
                avg_latency: avg_lat,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
            }
        })
        .collect();
    trends.sort_by(|a, b| a.date.cmp(&b.date));
    trends
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造一条带 KV Cache 统计的请求日志.
    fn mk(model: &str, provider: &str, hit: u32, miss: u32) -> RequestLog {
        RequestLog {
            timestamp: 0,
            model: model.to_string(),
            provider: provider.to_string(),
            endpoint: "/v1/chat/completions".to_string(),
            status: 200,
            latency_ms: 10,
            body_len: 100,
            error: None,
            prompt_tokens: 100,
            completion_tokens: 50,
            cached: false,
            prompt_cache_hit_tokens: hit,
            prompt_cache_miss_tokens: miss,
        }
    }

    /// 聚合: 全局缓存命中/未命中 token 与命中率正确; 模型与供应商分组正确累加.
    #[test]
    fn test_cache_hit_rate_aggregation() {
        let logs = vec![
            mk("deepseek", "ds", 80, 20),
            mk("deepseek", "ds", 60, 40),
            mk("gpt", "oa", 0, 100),
        ];
        let s = compute_stats(&logs);
        // 全局: 命中 80+60+0=140, 未命中 20+40+100=160
        assert_eq!(s.total_cache_hit_tokens, 140);
        assert_eq!(s.total_cache_miss_tokens, 160);
        assert!((s.cache_hit_rate - 140.0 / 300.0).abs() < 1e-9);
        // 模型分组: deepseek 命中 140, 未命中 60
        let ds = s.per_model.iter().find(|m| m.model == "deepseek").unwrap();
        assert_eq!(ds.total_cache_hit_tokens, 140);
        assert_eq!(ds.total_cache_miss_tokens, 60);
        // 供应商分组: ds 命中 140, 未命中 60
        let p = s.per_provider.iter().find(|p| p.provider == "ds").unwrap();
        assert_eq!(p.total_cache_hit_tokens, 140);
        assert_eq!(p.total_cache_miss_tokens, 60);
    }

    /// 聚合: 无 cache 数据时命中率为 0 (不除零).
    #[test]
    fn test_cache_hit_rate_zero() {
        let logs = vec![mk("m", "p", 0, 0)];
        let s = compute_stats(&logs);
        assert_eq!(s.total_cache_hit_tokens, 0);
        assert_eq!(s.total_cache_miss_tokens, 0);
        assert_eq!(s.cache_hit_rate, 0.0);
    }

    /// 趋势: 日界按东八区切分 (UTC 23:30 属「当天」, UTC 01:00 属「次日」).
    #[test]
    fn test_trend_local_timezone_day_boundary() {
        // 2026-08-01T23:30:00Z → 东八区 08-02 07:30, 属 08/02
        let t_same_day = 1_785_627_000;
        // 2026-08-02T01:00:00Z → 东八区 08-02 09:00, 仍属 08/02
        let t_next = 1_785_632_400;
        let logs = vec![mk_ts("m", "p", t_same_day), mk_ts("m", "p", t_next)];
        let trends = compute_trends(&logs, "day");
        // 两条都属于东八区 08/02, 应合并为一个桶.
        assert_eq!(trends.len(), 1);
        assert_eq!(trends[0].requests, 2);
        assert_eq!(trends[0].date, "08/02");
    }

    /// 趋势: 跨多日/多月日志能正确聚合成多个桶 (验证统计基于全量而非内存截断).
    #[test]
    fn test_trend_full_data_multi_bucket() {
        // 2026-07-15T00:00:00Z 起, 间隔 5 天, 全部落在 07 月.
        let base = 1_784_073_600;
        let logs: Vec<RequestLog> = (0..3)
            .map(|i| mk_ts("m", "p", base + i * 5 * 86400))
            .collect();
        let trends = compute_trends(&logs, "day");
        assert_eq!(trends.len(), 3, "跨多日应生成多个趋势桶");
        // 按月聚合: 全部落在同一个月 (07 月), 仅 1 个桶.
        let month = compute_trends(&logs, "month");
        assert_eq!(month.len(), 1);
        assert_eq!(month[0].date, "2026/07");
    }

    /// 构造带指定时间戳的日志 (复用 mk 的其余默认值).
    fn mk_ts(model: &str, provider: &str, ts: u64) -> RequestLog {
        let mut log = mk(model, provider, 0, 0);
        log.timestamp = ts;
        log
    }
}
