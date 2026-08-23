//! 管理面板 — 请求日志环形缓冲区 + 可视化 Web 面板.
//!
//! 访问 http://127.0.0.1:8787/admin 打开面板.
//! 功能: 实时请求日志 / 使用统计 / 路由配置查看 / 健康检查.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse};
use axum::body::Bytes;
use axum::Json;
use futures::future::join_all;
use serde::Serialize;
use tokio::sync::Mutex;

use crate::store::LogStore;
use crate::tooltip::{self, TooltipConfig};
use crate::balance::ProviderBalanceConfig;
use crate::pricing::{self, ModelPrice};

/// 面板「最近请求」实时展示保留的条数 (仅前端展示, 不影响全量统计/持久化).
/// 默认 1000, 可覆盖日均 ~500 请求约 2 天的窗口.
const LOG_CAPACITY: usize = 1000;

/// 面板「错误」独立保留条数 (按"错误"维度, 与最近请求窗口完全分离).
/// 正常请求再多也不会挤掉错误展示——错误从全量 inner 按错误维度过滤返回.
const ERROR_CAPACITY: usize = 100;

/// 实时统计 (供任务栏 tooltip 展示), 基于最近 N 秒日志聚合.
#[derive(Debug, Clone, Serialize)]
pub struct RealtimeStats {
    /// 最近 10 秒请求速度 (req/s).
    pub requests_per_second: f64,
    /// 最近 10 秒平均延迟 (ms).
    pub avg_latency_ms: f64,
    /// 最近 10 秒缓存命中率 (0.0~1.0).
    pub cache_hit_rate: f64,
    /// 最近 10 秒生成速度 (tok/s).
    pub gen_speed: f64,
    /// 今日总请求数.
    pub today_requests: usize,
    /// 当前时间戳 (秒).
    pub timestamp: u64,
}

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
    /// 输入 token 数 (从上游响应 usage 中提取).
    #[serde(default)]
    pub prompt_tokens: u32,
    /// 输出 token 数 (从上游响应 usage 中提取).
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
    /// 上游 KV Cache 首次写入 (creation) token 数 (Anthropic `cache_creation_input_tokens`
    /// / OpenAI `prompt_tokens_details.cache_creation_tokens`). 多数供应商 (DeepSeek 等)
    /// 无独立 creation 口径, 记 0, 此时 creation 计入未命中按 input 价计.
    #[serde(default)]
    pub prompt_cache_creation_tokens: u32,
    /// 首 token 延迟 (ms): 从请求开始到上游吐出第一个增量内容的耗时.
    /// 用于计算"纯生成吐字速度" = 输出 token / (总耗时 - 首 token 延迟), 排除排队与 TTFT.
    /// 非流式/命中本地缓存的请求此项为 None.
    #[serde(default)]
    pub first_token_ms: Option<u64>,
    /// 上游真实模型 ID (由 providers.json 的 upstream_model 透传). 用于按"供应商/上游模型"聚合统计, 避免按中转 ID 统计造成的混乱.
    /// 路由未查到/解析失败等未知场景为 None, 统计时回退到中转 model.
    #[serde(default)]
    pub upstream_model: Option<String>,
    /// 转发优化省下的输入 token 数: 剥离历史推理链 (字符数/4 估算).
    /// 这些 token 在发往上游客前已被移除, 不计入 prompt_tokens; 单独记账供"优化省量"展示.
    #[serde(default)]
    pub strip_saved_tokens: u32,
    /// 转发优化省下的输入 token 数: 长会话历史裁剪 (字符数/4 估算).
    #[serde(default)]
    pub trim_saved_tokens: u32,
    /// 本地响应缓存命中省下的 token 数 (命中时本应消耗/计费的 prompt+completion).
    /// 仅 cached=true 时有值; 命中时 prompt_tokens 已是缓存体, 不与计费基数重复.
    #[serde(default)]
    pub resp_cache_saved_tokens: u32,
}

/// 内存日志缓冲区 — 内存仅保留最近 `LOG_CAPACITY` 条用于面板实时展示;
/// 文件 `logs.jsonl` 为全量权威数据源 (受 `store::MAX_LINES` 上限约束).
/// 统计/聚合一律基于 [`LogBuffer::drain_all`] 从文件加载的全量数据, 避免跨天/跨月数据被内存容量截断.
#[derive(Clone)]
pub struct LogBuffer {
    inner: Arc<Mutex<VecDeque<RequestLog>>>,
    /// 写入序列号: 每次 push/clear 递增, 供统计缓存做失效判断 (读端纳秒级, 无需克隆全量).
    seq: Arc<AtomicU64>,
    store: Option<LogStore>,
    // ─── 本轮 (进程启动以来) 累计计数, 在 push 时原子累加, 不受 5000 条滚动窗口封顶影响 ───
    /// 本轮请求总数 (含错误/缓存命中).
    session_requests: Arc<AtomicU64>,
    /// 本轮成功请求数.
    session_success: Arc<AtomicU64>,
    /// 本轮输入 token 累计.
    session_prompt_tokens: Arc<AtomicU64>,
    /// 本轮输出 token 累计.
    session_completion_tokens: Arc<AtomicU64>,
    /// 本轮上游 KV Cache 命中 token 累计.
    session_cache_hit_tokens: Arc<AtomicU64>,
    /// 本轮上游 KV Cache 未命中 token 累计.
    session_cache_miss_tokens: Arc<AtomicU64>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(crate::store::MAX_LINES))),
            seq: Arc::new(AtomicU64::new(0)),
            store: None,
            session_requests: Arc::new(AtomicU64::new(0)),
            session_success: Arc::new(AtomicU64::new(0)),
            session_prompt_tokens: Arc::new(AtomicU64::new(0)),
            session_completion_tokens: Arc::new(AtomicU64::new(0)),
            session_cache_hit_tokens: Arc::new(AtomicU64::new(0)),
            session_cache_miss_tokens: Arc::new(AtomicU64::new(0)),
        }
    }

    /// 当前写入序列号 (单调增). 统计缓存以 (seq, granularity, providers_mtime) 判断是否失效.
    pub fn seq(&self) -> u64 {
        self.seq.load(Ordering::Acquire)
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
        // 写入序列号递增 (缓存失效信号).
        self.seq.fetch_add(1, Ordering::Release);
        // 本轮 (进程级) 累计计数: 独立于滚动窗口, 重启清零.
        self.session_requests.fetch_add(1, Ordering::Relaxed);
        if log.status < 400 {
            self.session_success.fetch_add(1, Ordering::Relaxed);
        }
        self.session_prompt_tokens
            .fetch_add(log.prompt_tokens as u64, Ordering::Relaxed);
        self.session_completion_tokens
            .fetch_add(log.completion_tokens as u64, Ordering::Relaxed);
        self.session_cache_hit_tokens
            .fetch_add(log.prompt_cache_hit_tokens as u64, Ordering::Relaxed);
        self.session_cache_miss_tokens
            .fetch_add(log.prompt_cache_miss_tokens as u64, Ordering::Relaxed);
        // 异步持久化 (文件为全量权威源, 不受内存容量限制).
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.append(&log).await;
            });
        }
    }

    /// 本轮 (进程启动以来) 累计统计快照, 不受日志滚动窗口封顶影响.
    pub fn session_stats(&self) -> SessionStats {
        SessionStats {
            requests: self.session_requests.load(Ordering::Relaxed),
            success: self.session_success.load(Ordering::Relaxed),
            prompt_tokens: self.session_prompt_tokens.load(Ordering::Relaxed),
            completion_tokens: self.session_completion_tokens.load(Ordering::Relaxed),
            cache_hit_tokens: self.session_cache_hit_tokens.load(Ordering::Relaxed),
            cache_miss_tokens: self.session_cache_miss_tokens.load(Ordering::Relaxed),
        }
    }

    /// 返回全量日志 (用于统计/聚合), 基于内存中加载的全量数据, 不消费.
    pub async fn drain_all(&self) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().cloned().collect()
    }

    /// 只读遍历 (不克隆全量): 持锁期间对每个日志调用 `f`, 用于实时聚合等只需流式消费的场景.
    /// 相较 `drain_all` 省去一次 5000 条深拷贝 (含多个 String 字段) 的堆分配,
    /// 且锁持有时间仅覆盖遍历本身, 不覆盖后续聚合, 降低对写入路径 (proxy 响应链路) 的阻塞.
    pub fn for_each_recent<F>(&self, start_ts: u64, mut f: F)
    where
        F: FnMut(&RequestLog),
    {
        if let Ok(buf) = self.inner.try_lock() {
            for log in buf.iter() {
                if log.timestamp >= start_ts {
                    f(log);
                }
            }
        }
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

    /// 取最近 N 条**错误**日志 (按"错误"维度独立留存, 不受 `recent` 请求窗口冲刷).
    /// 错误判定: status>=400 或 error 字段非空. 内存 inner 保留全量(MAX_LINES),
    /// 故错误数量与"最近 N 条请求"完全解耦——正常请求再多也不会挤掉错误展示.
    pub async fn recent_errors(&self, n: usize) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter()
            .rev()
            .filter(|l| l.status >= 400 || l.error.is_some())
            .take(n)
            .cloned()
            .collect()
    }

    /// 当前内存中日志条数.
    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    /// 清空缓冲区并重写持久化文件.
    pub async fn clear(&self) {
        let mut buf = self.inner.lock().await;
        buf.clear();
        // 清空也是数据变更, 递增序列号使统计缓存失效.
        self.seq.fetch_add(1, Ordering::Release);
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
    upstream_model: Option<&str>,
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
        prompt_cache_creation_tokens: 0,
        strip_saved_tokens: 0,
        trim_saved_tokens: 0,
        resp_cache_saved_tokens: 0,
        first_token_ms: None,
        upstream_model: upstream_model.map(|s| s.to_string()),
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
    upstream_model: Option<&str>,
    start: Instant,
    prompt_tokens: u32,
    completion_tokens: u32,
    response_body_len: usize,
    cached: bool,
    cache_hit_tokens: u32,
    cache_miss_tokens: u32,
    cache_creation_tokens: u32,
    strip_saved_tokens: u32,
    trim_saved_tokens: u32,
    resp_cache_saved_tokens: u32,
    first_token_ms: Option<u64>,
    error: Option<String>,
) {
    let log = RequestLog {
        timestamp: now_ts(),
        model: model.to_string(),
        provider: provider.to_string(),
        endpoint: endpoint.to_string(),
        status: 200,
        latency_ms: start.elapsed().as_millis() as u64,
        body_len: response_body_len,
        error,
        prompt_tokens,
        completion_tokens,
        cached,
        prompt_cache_hit_tokens: cache_hit_tokens,
        prompt_cache_miss_tokens: cache_miss_tokens,
        prompt_cache_creation_tokens: cache_creation_tokens,
        strip_saved_tokens,
        trim_saved_tokens,
        resp_cache_saved_tokens,
        first_token_ms,
        upstream_model: upstream_model.map(|s| s.to_string()),
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
    let html = ADMIN_HTML
        .replace("/*__AIGATE_TOKEN__*/", &format!("window.AIGATE_TOKEN = {token_json};"))
        .replace(
            "/*__AIGATE_LANG__*/",
            &format!("window.AIGATE_LANG = \"{}\";", crate::i18n::lang_code()),
        )
        .replace(
            "/*__AIGATE_VERSION__*/",
            &format!("window.AIGATE_VERSION = {};", crate::version::to_json()),
        );
    Html(html)
}

/// GET /admin/static/:file — 返回随包内联的前端依赖 (Alpine.js / Tailwind),
/// 走本地服务而非境外 CDN, 解决国内打不开 jsdelivr/tailwindcss 导致的面板黑屏.
/// `:file` 仅允许白名单文件名, 杜绝路径穿越.
pub async fn admin_static(Path(file): Path<String>) -> impl IntoResponse {
    let body: &'static str = match file.as_str() {
        "alpine.min.js" => ALPINE_JS,
        "tailwind.js" => TAILWIND_JS,
        _ => {
            return (
                StatusCode::NOT_FOUND,
                [(axum::http::header::CONTENT_TYPE, "text/plain; charset=utf-8")],
                "not found",
            )
                .into_response();
        }
    };
    (
        [(axum::http::header::CONTENT_TYPE, "application/javascript; charset=utf-8")],
        Bytes::from_static(body.as_bytes()),
    )
        .into_response()
}

/// 管理面板前端页面 (编译时嵌入).
const ADMIN_HTML: &str = include_str!("admin.html");
/// 更新日志 (Keep a Changelog 风格), 烤入二进制供关于页展示.
const CHANGELOG: &str = include_str!("../CHANGELOG.md");
/// Alpine.js (随包内联, 替代 cdn.jsdelivr.net) — 面板渲染引擎, 缺失会导致整页不渲染(黑屏).
const ALPINE_JS: &str = include_str!("admin_static/alpine.min.js");
/// Tailwind 运行时 JIT (随包内联, 替代 cdn.tailwindcss.com) — 负责工具类样式生成.
const TAILWIND_JS: &str = include_str!("admin_static/tailwind.js");

/// 给展示用日志附加 `free` 标记 (免费模型), 判定与路由配置页完全一致 (注册表 is_free),
/// 不污染磁盘持久化的 RequestLog 结构, 仅在 API 响应层注入.
fn request_logs_with_free(
    logs: Vec<RequestLog>,
    free_ids: &std::collections::HashSet<String>,
) -> Vec<serde_json::Value> {
    logs.into_iter()
        .map(|l| {
            let mut v = serde_json::to_value(&l).unwrap_or(serde_json::Value::Null);
            if let Some(obj) = v.as_object_mut() {
                obj.insert("free".into(), serde_json::Value::Bool(free_ids.contains(&l.model)));
            }
            v
        })
        .collect()
}

/// GET /admin/api/logs — 返回最近 100 条请求日志 (展示用, 内存限长), 附带免费标记.
pub async fn api_logs(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<serde_json::Value>> {
    let logs = state.log_buffer.recent(LOG_CAPACITY).await;
    let registry = state.registry.read().await;
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(&model_id) {
                free_ids.insert(model_id.to_string());
            }
        }
    }
    drop(registry);
    Json(request_logs_with_free(logs, &free_ids))
}

/// DELETE /admin/api/logs — 清空日志缓冲区.
pub async fn api_logs_delete(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let count = state.log_buffer.len().await;
    // 清空内存展示缓冲与持久化文件. 删除前先同步落盘已有数据, 避免异步尾写丢失.
    state.log_buffer.flush().await;
    state.log_buffer.clear().await;
    Json(serde_json::json!({ "message": crate::i18n::msg_logs_cleared(count) }))
}

/// GET /admin/api/errors — 返回最近 N 条错误日志 (独立维度, 与请求窗口分离, 不被冲刷), 附带免费标记.
pub async fn api_errors(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<serde_json::Value>> {
    let logs = state.log_buffer.recent_errors(ERROR_CAPACITY).await;
    let registry = state.registry.read().await;
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(&model_id) {
                free_ids.insert(model_id.to_string());
            }
        }
    }
    drop(registry);
    Json(request_logs_with_free(logs, &free_ids))
}

/// 路由配置的脱敏视图.
#[derive(Serialize)]
pub struct RouteInfo {
    model_id: String,
    provider: String,
    endpoint: String,
    /// 该模型实际转发的端点路径: 与 proxy.rs 运行时一致,
    /// 模型级 api_format=anthropic 且供应商端点为 /chat/completions 时改写为 /messages.
    api_format: String,
    upstream_model: Option<String>,
    reasoning_effort: Option<String>,
    /// 是否免费模型 (显式 free 标记或 upstream_model 含 free/免费).
    free: bool,
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
            // 复刻 proxy.rs 运行时的端点改写逻辑: 模型级 api_format=anthropic
            // 且供应商端点仍是 /chat/completions 时, 改写为 /messages 或 /responses.
            let mut endpoint = entry.provider.endpoint.clone();
            let anthropic_mode = entry.model.is_anthropic(&entry.provider);
            let responses_mode = entry.model.is_responses(&entry.provider);
            if anthropic_mode {
                if let Some(ep) = &entry.provider.endpoint_anthropic {
                    if !ep.trim().is_empty() {
                        endpoint = ep.clone();
                    }
                } else if endpoint.ends_with("/chat/completions") {
                    endpoint = endpoint.replace("/chat/completions", "/messages");
                }
            } else if responses_mode {
                if let Some(ep) = &entry.provider.endpoint_responses {
                    if !ep.trim().is_empty() {
                        endpoint = ep.clone();
                    }
                } else if endpoint.ends_with("/chat/completions") {
                    endpoint = endpoint.replace("/chat/completions", "/responses");
                }
            }
            let api_format = if anthropic_mode { "anthropic" } else if responses_mode { "responses" } else { "openai" }.to_string();
            Some(RouteInfo {
                model_id: id.to_string(),
                provider: entry.provider.name.clone(),
                endpoint,
                api_format,
                upstream_model: entry.model.upstream_model.clone(),
                reasoning_effort: entry.model.reasoning_effort.clone(),
                free: entry.model.is_free(id),
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
        None => return Json(serde_json::json!({ "error": crate::i18n::msg_missing_json_field() })),
    };

    // 1) 写前校验: 解析 + 结构化语义校验, 防止坏配置覆盖落盘 (包含 name 唯一/非空, endpoint 非空).
    let new_names = match validate_providers_json(json_str) {
        Ok(names) => names,
        Err(e) => return Json(serde_json::json!({ "error": e })),
    };

    // 2) 备份当前文件 (覆盖写盘前保留上一版, 便于回滚).
    let _ = std::fs::copy("providers.json", "providers.json.bak");

    // 3) 取旧供应商名 (用于级联清理孤儿 key).
    let old_names: Vec<String> = state
        .registry
        .read()
        .await
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();

    // 4) 原子写盘: 临时文件 + rename, 避免写入中断留下半截文件.
    let tmp = "providers.json.tmp";
    if let Err(e) = std::fs::write(tmp, json_str) {
        return Json(serde_json::json!({ "error": crate::i18n::msg_write_file_failed(&e) }));
    }
    if let Err(e) = std::fs::rename(tmp, "providers.json") {
        return Json(serde_json::json!({ "error": crate::i18n::msg_write_file_failed(&e) }));
    }

    // 5) 热重载
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;

    // 6) 级联清理: 删除已从配置中移除的供应商的密钥 (包含关系: key 随 provider 消失).
    let removed: Vec<String> = old_names
        .iter()
        .filter(|n| !new_names.contains(n))
        .cloned()
        .collect();
    if !removed.is_empty() {
        let _ = state.key_store.remove_many(&removed).await;
    }

    Json(serde_json::json!({ "message": crate::i18n::msg_config_saved() }))
}

/// 校验 providers.json 文本: 解析为 {providers:[...]}, 每个供应商 name 非空且唯一, endpoint 非空.
/// 返回供应商名列表 (供调用方做孤儿 key 清理).
fn validate_providers_json(json_str: &str) -> Result<Vec<String>, String> {
    let v: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| format!("JSON 解析失败: {e}"))?;
    let arr = v
        .get("providers")
        .and_then(|p| p.as_array())
        .ok_or_else(|| "providers.json 缺少顶层 providers 数组".to_string())?;
    let mut names: Vec<String> = Vec::with_capacity(arr.len());
    for (i, p) in arr.iter().enumerate() {
        let name = p
            .get("name")
            .and_then(|n| n.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("第 {i} 个供应商 name 为空 (必填)"))?;
        if names.iter().any(|n| n == name) {
            return Err(format!("供应商 name 重复: '{name}'"));
        }
        let endpoint = p
            .get("endpoint")
            .and_then(|e| e.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("供应商 '{name}' 的 endpoint 为空 (必填)"))?;
        let _ = endpoint;
        names.push(name.to_string());
    }
    Ok(names)
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
    Json(serde_json::json!({ "message": crate::i18n::msg_config_reloaded() }))
}

/// GET /admin/api/keys — 返回所有供应商的密钥脱敏视图 (包含关系: 每行对应一个 provider).
pub async fn api_keys_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let providers = registry.providers();
    let views = state.key_store.masked_view_for_providers(&providers).await;
    drop(registry);
    Json(serde_json::json!({ "providers": views }))
}

/// PUT /admin/api/keys — 更新某供应商的密钥 (Key 是 provider 的子资源).
#[derive(serde::Deserialize)]
pub struct KeyUpdate {
    /// 归属的供应商名.
    pub provider: String,
    pub value: String,
}

pub async fn api_keys_put(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<KeyUpdate>,
) -> Json<serde_json::Value> {
    match state
        .key_store
        .set_for_provider(&payload.provider, &payload.value)
        .await
    {
        Ok(()) => {
            if payload.value.is_empty() {
                Json(serde_json::json!({ "message": crate::i18n::msg_key_cleared(&payload.provider) }))
            } else {
                Json(serde_json::json!({ "message": crate::i18n::msg_key_updated(&payload.provider) }))
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
    /// 健康等级: ok / error (供面板 CSS 配色).
    status_level: String,
    /// 健康状态中文文案 (供面板展示).
    status_text: String,
    latency_ms: u64,
    /// 熔断状态原始值: closed / open / half-open (供面板 CSS 配色).
    circuit: String,
    /// 熔断状态中文文案 (供面板展示).
    circuit_text: String,
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
            None => return Json(serde_json::json!({ "error": crate::i18n::msg_provider_not_found(&name) })),
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

    // 3) 合并进内存注册表 (新增未存在的, 跳过已有) + 计算已下架.
    //    下架判定必须基于【上游模型ID】(upstream_model, 缺省回落中转ID):
    //    中转ID 是用户自取别名可随时改名, 不能作为与上游清单比对的依据 ——
    //    否则一改中转ID 就会被误标"已下架".
    let (added, skipped, removed) = {
        let mut registry = state.registry.write().await;
        let mut existing: Vec<String> = registry
            .providers()
            .iter()
            .find(|p| p.name == name)
            .map(|p| {
                let mut v: Vec<String> = p
                    .models
                    .iter()
                    .map(|(k, m)| m.upstream_model.clone().unwrap_or_else(|| k.clone()))
                    .collect();
                v.sort();
                v.dedup(); // 多个中转ID 可别名到同一上游模型, 去重避免重复计数
                v
            })
            .unwrap_or_default();
        let removed: Vec<String> = existing.drain(..).filter(|id| !ids.contains(id)).collect();
        let (added, skipped) = registry.add_models(&name, &ids);
        (added, skipped, removed)
    };

    // 注: 不再直接写盘/重载. 拉取的模型仅写入内存注册表 (运行期即时生效),
    // 并由前端合并进供应商表单, 待用户点 "保存配置" 才持久化.
    // 这样不会冲掉用户在表单里尚未保存的其它改动 (包含关系: 保存由用户主导).

    Json(serde_json::json!({
        "success": true,
        "provider": name,
        "models": ids,
        "added": added,
        "skipped": skipped,
        "removed": removed.clone(),
        "removed_count": removed.len(),
        "message": crate::i18n::msg_models_fetched(ids.len(), added, skipped),
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
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            first_token_ms: None,
            upstream_model: None,
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
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            first_token_ms: None,
            upstream_model: None,
        });
    }

    // 写入缓冲区
    for log in &logs {
        state.log_buffer.push(log.clone()).await;
    }

    let count = logs.len();
    Json(serde_json::json!({
        "message": crate::i18n::msg_mock_generated(count),
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
            HealthEntry {
                provider: name,
                endpoint,
                status_level: crate::i18n::health_level(&cb, reachable).to_string(),
                status_text: crate::i18n::health_status_text(&cb, reachable),
                latency_ms,
                circuit_text: crate::i18n::circuit_state_cn(&cb).to_string(),
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
            Json(serde_json::json!({ "message": crate::i18n::msg_circuit_reset(&payload.provider) }))
        }
        None => Json(serde_json::json!({ "message": crate::i18n::msg_circuit_none(&payload.provider) })),
    }
}

/// GET /admin/api/lang — 返回当前界面语言 (供前端初始化下拉).
pub async fn api_lang() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "lang": crate::i18n::lang_code() }))
}

/// POST /admin/api/lang — 切换并持久化界面语言 (写入 lang.json + 更新运行态).
#[derive(serde::Deserialize)]
pub struct LangReq {
    lang: String,
}

pub async fn api_lang_set(Json(payload): Json<LangReq>) -> Json<serde_json::Value> {
    match crate::i18n::parse_lang(&payload.lang) {
        Some(lang) => match crate::lang::save_lang(lang) {
            Ok(()) => Json(serde_json::json!({ "success": true, "lang": crate::i18n::lang_code() })),
            Err(e) => Json(serde_json::json!({ "error": crate::i18n::msg_lang_save_failed(&e) })),
        },
        None => Json(serde_json::json!({ "error": crate::i18n::msg_lang_unsupported(&payload.lang) })),
    }
}

/// GET /admin/api/version — 返回结构化版本信息 (供前端关于页/页脚展示).
pub async fn api_version() -> Json<serde_json::Value> {
    Json(crate::version::to_json())
}

/// GET /admin/api/proxy-config — 返回当前代理策略状态 (启动时由环境变量决定).
pub async fn api_proxy_config() -> Json<serde_json::Value> {
    Json(serde_json::json!(crate::proxy_cfg::proxy_status()))
}

/// GET /admin/api/changelog — 返回解析后的更新日志 (供关于页展示).
///
/// 解析烤入的 `CHANGELOG.md` (Keep a Changelog 风格): 按 `## [` 切分版本块,
/// 提取版本号/日期, 再按 `### ` 切分小节, 收集 `- ` 条目.
pub async fn api_changelog() -> Json<serde_json::Value> {
    Json(parse_changelog())
}

/// 读取用户已看过「更新亮点」的版本号（空字符串表示从未记录）。
pub async fn api_seen_version_get() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "version": crate::seen_version::load_seen() }))
}

/// 写入已见版本号，标记当前版本更新日志已读。
pub async fn api_seen_version_post(Json(body): Json<serde_json::Value>) -> Json<serde_json::Value> {
    let v = body
        .get("version")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    crate::seen_version::save_seen(&v);
    Json(serde_json::json!({ "ok": true, "version": v }))
}

/// 解析 `CHANGELOG.md` 为结构化 JSON: { versions: [{ version, date, sections:[{title,items}] }] }.
fn parse_changelog() -> serde_json::Value {
    let mut versions = Vec::new();
    for raw in CHANGELOG.split("\n## [") {
        let raw = raw.trim();
        if raw.is_empty() {
            continue;
        }
        // 首行形如 "[0.3.0] - 2026-08-08"; 注意 split("\n## [") 已吞掉 "## [",
        // 因此段首可能无 '[' 前缀 (如 "0.3.0] - 2026-08-08"), 统一用 find(']') 解析.
        let nl = raw.find('\n').unwrap_or(raw.len());
        let header = raw[..nl].strip_prefix('[').unwrap_or(&raw[..nl]);
        let mut version = String::new();
        let mut date = String::new();
        if let Some(close) = header.find(']') {
            version = header[..close].trim().to_string();
            let after = &header[close + 1..];
            if let Some(d) = after.strip_prefix(" - ") {
                date = d.trim().to_string();
            }
        }
        if version.is_empty() {
            continue; // 跳过文件头等非版本块
        }
        let body = if nl < raw.len() { &raw[nl + 1..] } else { "" };
        let mut sections = Vec::new();
        let mut cur_title = String::new();
        let mut cur_items: Vec<String> = Vec::new();
        for line in body.lines() {
            let line = line.trim_end();
            if let Some(title) = line.strip_prefix("### ") {
                if !cur_title.is_empty() || !cur_items.is_empty() {
                    sections.push(serde_json::json!({ "title": cur_title, "items": cur_items }));
                    cur_items = Vec::new();
                }
                cur_title = title.trim().to_string();
            } else if let Some(item) = line.strip_prefix("- ") {
                cur_items.push(item.trim().to_string());
            }
        }
        if !cur_title.is_empty() || !cur_items.is_empty() {
            sections.push(serde_json::json!({ "title": cur_title, "items": cur_items }));
        }
        versions.push(serde_json::json!({
            "version": version,
            "date": date,
            "sections": sections
        }));
    }
    serde_json::json!({ "versions": versions })
}

#[cfg(test)]
mod changelog_tests {
    use super::*;

    /// 回归测试: 烤入的 CHANGELOG.md 必须能解析出全部版本块.
    /// 曾因 split("\n## [") 吞掉 "## [" 前缀导致版本块全部被跳过 (versions 恒为空).
    #[test]
    fn parse_changelog_returns_all_versions() {
        let json = parse_changelog();
        let versions = json.get("versions").and_then(|v| v.as_array()).unwrap();
        assert!(!versions.is_empty(), "versions 不应为空");
        // 校验版本号格式与字段完整性
        for v in versions {
            let version = v.get("version").and_then(|x| x.as_str()).unwrap_or("");
            assert!(version.starts_with('v') || version.chars().next().is_some_and(|c| c.is_ascii_digit()),
                "版本号格式异常: {version}");
            assert!(v.get("date").is_some(), "缺少 date");
            assert!(v.get("sections").and_then(|s| s.as_array()).is_some(), "缺少 sections");
        }
        // 首个版本应为最新版 = 当前 Cargo 版本 (动态断言, 版本号升级不再破坏测试).
        assert_eq!(
            versions[0].get("version").and_then(|x| x.as_str()),
            Some(env!("CARGO_PKG_VERSION"))
        );
    }
}

/// GET /admin/api/tooltip-config — 返回当前 tooltip 配置.
pub async fn api_tooltip_config_get() -> Json<serde_json::Value> {
    let config = tooltip::load_config();
    Json(serde_json::to_value(config).unwrap_or_default())
}

/// POST /admin/api/tooltip-config — 保存 tooltip 配置.
#[derive(serde::Deserialize)]
pub struct TooltipConfigReq {
    pub config: TooltipConfig,
}

pub async fn api_tooltip_config_set(
    Json(payload): Json<TooltipConfigReq>,
) -> Json<serde_json::Value> {
    match tooltip::save_config(&payload.config) {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": format!("保存失败: {}", e) })),
    }
}

/// GET /admin/api/currency — 返回当前费用显示币种配置.
pub async fn api_currency_get() -> Json<serde_json::Value> {
    let config = crate::currency::load_config();
    Json(serde_json::to_value(config).unwrap_or_default())
}

/// POST /admin/api/currency — 保存费用显示币种配置.
#[derive(serde::Deserialize)]
pub struct CurrencyConfigReq {
    pub config: crate::currency::CurrencyConfig,
}

pub async fn api_currency_set(
    Json(payload): Json<CurrencyConfigReq>,
) -> Json<serde_json::Value> {
    match crate::currency::save_config(&payload.config) {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": format!("保存失败: {}", e) })),
    }
}

/// GET /admin/api/balance — 返回各供应商余额信息 (API 查询 + 手动余额合并).
pub async fn api_balance(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    // 余额管理器为 AppState 常驻实例 (缓存跨请求复用), 不再每次重建.
    let balance_manager = &state.balance_manager;

    let registry = state.registry.read().await;
    let providers = registry.providers();
    
    // 构建供应商余额配置
    let mut provider_configs = std::collections::HashMap::new();
    for config in providers {
        if let Some(balance_endpoint) = &config.balance_endpoint {
            // 获取 API key (面板值 > 环境变量 > 默认值)
            let api_key = state.key_store.get_for_provider(&config.name).await
                .or_else(|| std::env::var(&config.api_key_env).ok().filter(|s| !s.is_empty()))
                .or_else(|| config.api_key_default.clone())
                .unwrap_or_default();
            
            provider_configs.insert(
                config.name.clone(),
                ProviderBalanceConfig {
                    balance_endpoint: Some(balance_endpoint.clone()),
                    api_key,
                },
            );
        }
    }
    
    // 查询余额
    let balances = balance_manager.query_all_balances(&state.client, &provider_configs).await;
    
    Json(serde_json::json!({
        "balances": balances,
    }))
}

/// POST /admin/api/balance/manual — 设置或清除某供应商的手动余额.
#[derive(serde::Deserialize)]
pub struct ManualBalanceReq {
    /// 供应商名称.
    pub provider: String,
    /// 余额 (元). 传 None 表示清除手动余额, 回退到 API 查询.
    pub balance: Option<f64>,
}

pub async fn api_balance_manual_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<ManualBalanceReq>,
) -> Json<serde_json::Value> {
    let balance_manager = &state.balance_manager;

    let result = if let Some(balance) = payload.balance {
        balance_manager.set_manual_balance(&payload.provider, balance).await
    } else {
        balance_manager.clear_manual_balance(&payload.provider).await
    };
    // 手动设置/清除后, 清掉该供应商的 API 缓存, 使其回退或下次重查.
    let _ = balance_manager.clear_cache(&payload.provider).await;

    match result {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// GET /admin/api/realtime — 返回最近 10 秒的实时指标 (供任务栏 tooltip 展示).
pub async fn api_realtime(
    State(state): State<super::proxy::AppState>,
) -> Json<RealtimeStats> {
    // 就地聚合: 不克隆全量日志, 仅持锁遍历一次 (省去每秒一次 5000 条深拷贝).
    Json(state.log_buffer.realtime_stats())
}

/// 计算实时统计 (同步版本, 供事件循环 tooltip 更新使用).
pub fn compute_realtime_stats_sync(log_buffer: &LogBuffer) -> RealtimeStats {
    log_buffer.realtime_stats()
}

/// 实时统计: 在 LogBuffer 内部持锁遍历一次完成聚合, 避免调用方先 drain_all 全量克隆.
impl LogBuffer {
    /// 就地聚合最近 N 秒 + 今日窗口指标, 单次遍历, 不分配全量 Vec.
    pub fn realtime_stats(&self) -> RealtimeStats {
        let now = now_ts();
        let window = 10u64; // 最近 10 秒
        let start = now.saturating_sub(window);
        let today_start = ((now as i64 + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;

        let mut r_count: u64 = 0;
        let mut r_latency_sum: u64 = 0;
        let mut r_hit: u64 = 0;
        let mut r_miss: u64 = 0;
        let mut r_gen_ct: u64 = 0;
        let mut r_gen_ms: u64 = 0;
        let mut today_count: u64 = 0;

        self.for_each_recent(0, |l| {
            if l.timestamp >= today_start {
                today_count += 1;
            }
            if l.timestamp < start {
                return;
            }
            r_count += 1;
            r_latency_sum += l.latency_ms;
            r_hit += l.prompt_cache_hit_tokens as u64;
            r_miss += l.prompt_cache_miss_tokens as u64;
            if let Some(ft) = l.first_token_ms {
                if ft < l.latency_ms && l.completion_tokens > 0 {
                    r_gen_ct += l.completion_tokens as u64;
                    r_gen_ms += l.latency_ms - ft;
                }
            }
        });

        let count = r_count as f64;
        let requests_per_second = if window > 0 { count / window as f64 } else { 0.0 };
        let avg_latency_ms = if count > 0.0 {
            r_latency_sum as f64 / count
        } else {
            0.0
        };
        let cache_hit_rate = if r_hit + r_miss > 0 {
            r_hit as f64 / (r_hit + r_miss) as f64
        } else {
            0.0
        };
        let gen_speed = if r_gen_ms > 0 {
            r_gen_ct as f64 / r_gen_ms as f64 * 1000.0
        } else {
            0.0
        };
        RealtimeStats {
            requests_per_second,
            avg_latency_ms,
            cache_hit_rate,
            gen_speed,
            today_requests: today_count as usize,
            timestamp: now,
        }
    }
}

// ─── 响应缓存 (实验功能) ───

/// 缓存配置请求体 (运行时可调): 开关 / TTL / 条目上限. 字段均可选, 仅更新提供项.
#[derive(serde::Deserialize)]
pub struct CacheConfigReq {
    pub enabled: Option<bool>,
    pub ttl_secs: Option<u64>,
    pub max_entries: Option<usize>,
}

/// GET /admin/api/cache — 返回缓存当前状态与统计 (命中/未命中/条目数).
pub async fn api_cache_get(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    Json(state.cache.stats())
}

/// POST /admin/api/cache — 运行时更新缓存配置 (面板"性能与优化"设置页).
/// 开关 / TTL / 条目上限独立可选; 仅更新请求中提供的字段, 其余保持原值.
pub async fn api_cache_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<CacheConfigReq>,
) -> Json<crate::cache::CacheStats> {
    if let Some(enabled) = payload.enabled {
        state.cache.set_enabled(enabled);
    }
    if let Some(ttl_secs) = payload.ttl_secs {
        state.cache.set_ttl(ttl_secs);
    }
    if let Some(max_entries) = payload.max_entries {
        state.cache.set_max_entries(max_entries);
    }
    Json(state.cache.stats())
}

/// POST /admin/api/cache/clear — 手动清空全部缓存条目 (实验功能调试用).
pub async fn api_cache_clear(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    state.cache.clear();
    Json(state.cache.stats())
}

// ─── 历史推理链瘦身开关 ───

/// GET /admin/api/strip-reasoning — 返回当前是否剥离历史推理链.
pub async fn api_strip_reasoning_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "enabled": state.strip_history_reasoning.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/strip-reasoning — 运行时切换历史推理链剥离开关.
#[derive(serde::Deserialize)]
pub struct StripReasoningReq {
    pub enabled: bool,
}

pub async fn api_strip_reasoning_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<StripReasoningReq>,
) -> Json<serde_json::Value> {
    state.strip_history_reasoning.store(payload.enabled, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "enabled": payload.enabled }))
}

// ─── 长会话历史裁剪开关 ───

/// GET /admin/api/max-history-turns — 返回当前保留的最近 user 轮数 (0 = 不裁剪).
pub async fn api_max_history_turns_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "turns": state.max_history_turns.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/max-history-turns — 运行时设置保留轮数 (0 = 关闭裁剪, 推荐 10~30).
#[derive(serde::Deserialize)]
pub struct MaxHistoryTurnsReq {
    pub turns: usize,
}

pub async fn api_max_history_turns_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<MaxHistoryTurnsReq>,
) -> Json<serde_json::Value> {
    let turns = payload.turns;
    state.max_history_turns.store(turns, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "turns": turns }))
}

// ─── 流截断自动续写 ───

/// GET /admin/api/auto-continue — 返回当前流截断自动续写次数上限 (0 = 关闭).
pub async fn api_auto_continue_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "count": state.auto_continue.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/auto-continue — 运行时设置自动续写上限 (0 = 关闭, 推荐 1~3).
/// 上游断流且无 finish_reason 时, 网关自动带已输出正文重发"继续"请求并拼接新响应.
#[derive(serde::Deserialize)]
pub struct AutoContinueReq {
    pub count: usize,
}

pub async fn api_auto_continue_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<AutoContinueReq>,
) -> Json<serde_json::Value> {
    // 上限保护: 续写链过长会成倍放大延迟与 token 消耗.
    let count = payload.count.min(5);
    state.auto_continue.store(count, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "count": count }))
}

// ─── 流空闲超时 / 重试参数 (运行时可调) ───

/// GET /admin/api/stream-timeout — 流式响应空闲超时秒数.
pub async fn api_stream_timeout_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "secs": state.stream_idle_timeout_secs.load(std::sync::atomic::Ordering::Relaxed) }))
}

#[derive(serde::Deserialize)]
pub struct StreamTimeoutReq {
    pub secs: u64,
}

pub async fn api_stream_timeout_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<StreamTimeoutReq>,
) -> Json<serde_json::Value> {
    // 合理区间: 太小误伤长思考静默期, 太大失去假死保护.
    let secs = payload.secs.clamp(30, 600);
    state.stream_idle_timeout_secs.store(secs, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "secs": secs }))
}

/// GET /admin/api/retry — 瞬态失败重试参数 (次数 + 退避基数毫秒).
pub async fn api_retry_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "max": state.retry_max.load(std::sync::atomic::Ordering::Relaxed),
        "backoff_ms": state.retry_backoff_ms.load(std::sync::atomic::Ordering::Relaxed),
    }))
}

#[derive(serde::Deserialize)]
pub struct RetryReq {
    pub max: u32,
    pub backoff_ms: u64,
}

pub async fn api_retry_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<RetryReq>,
) -> Json<serde_json::Value> {
    let mx = payload.max.min(5);
    let backoff = payload.backoff_ms.min(10_000);
    state.retry_max.store(mx, std::sync::atomic::Ordering::Relaxed);
    state.retry_backoff_ms.store(backoff, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "max": mx, "backoff_ms": backoff }))
}

// ─── 模型元信息 (models.dev) ───

/// POST /admin/api/model-meta — 批量解析模型元信息 (上下文/输出限制/视觉等).
/// 请求体 `{names: ["kimi-k3-free", ...]}`; 响应 `{meta: {名称: ModelMeta|null}}`,
/// null = 未收录 (前端隐藏标签). 首次调用触发 models.dev 拉取 (24h 缓存, 走系统代理),
/// 拉取失败返回全 null 不阻塞面板.
#[derive(serde::Deserialize)]
pub struct ModelMetaReq {
    pub names: Vec<String>,
}

pub async fn api_model_meta(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<ModelMetaReq>,
) -> Json<serde_json::Value> {
    // 上限保护: 名单异常大时截断 (正常配置 <500 个模型).
    let names: Vec<String> = payload.names.into_iter().take(2000).collect();
    let map = state.model_meta.resolve_many(&state.client, &names).await;
    Json(serde_json::json!({ "meta": map }))
}

// ─── 使用统计 ───

/// 本轮 (进程启动以来) 累计统计 — 在 push 时原子累加, 不受日志 5000 条滚动窗口封顶影响.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SessionStats {
    /// 本轮请求总数 (含错误/缓存命中).
    pub requests: u64,
    /// 本轮成功请求数 (status < 400).
    pub success: u64,
    /// 本轮输入 token 累计.
    pub prompt_tokens: u64,
    /// 本轮输出 token 累计.
    pub completion_tokens: u64,
    /// 本轮上游 KV Cache 命中 token 累计.
    pub cache_hit_tokens: u64,
    /// 本轮上游 KV Cache 未命中 token 累计.
    pub cache_miss_tokens: u64,
}

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
    /// 纯生成吐字速度 (tok/s): 仅统计首 token 延迟已知的流式请求,
    /// = Σ输出token / Σ(总耗时-首token延迟) × 1000. 排除排队与 TTFT, 比 avg_latency 反推更准.
    pub gen_speed: f64,
    /// 映射到本聚合项的中转 ID 集合 (去重). 因统计按"供应商/上游模型"聚合,
    /// 多个中转 ID 可映射到同一上游模型, 此字段便于对照溯源.
    pub aliases: Vec<String>,
    /// 该模型累计费用 (元), 见 `crate::pricing`.
    pub total_cost: f64,
    /// 应用于本聚合项的单价（元/百万tokens）. 来自 providers.json 覆盖或内置表;
    /// 取组内首条日志解析的结果作代表（同组内多中转 ID 映射到同一上游, 单价通常一致）.
    /// `None` 表示未配置价格（费用记 0）.
    pub price: Option<ModelPrice>,
    /// 是否免费模型 (显式 free 标记或上游/中转模型名含 free/免费). 与路由配置页判定完全一致,
    /// 由调用方从注册表预计算 free_ids 传入, 组内任一中转 ID 命中即标记.
    pub free: bool,
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
    /// 纯生成吐字速度 (tok/s), 同 ModelStats.gen_speed.
    pub gen_speed: f64,
    /// 该供应商累计费用 (元), 见 `crate::pricing`.
    pub total_cost: f64,
    /// 该供应商今日窗口费用 (元, 东八区日界). 前端据 total_cost/today_cost 是否 >0 决定
    /// 是否展示该供应商费用卡片 (方案 A: 只显示有实际费用的供应商).
    pub today_cost: f64,
    /// 该供应商 KV 缓存命中节省的金额 (元). 仅计费供应商有意义; 免费/月套餐等
    /// 不按量计费模型在聚合层已被 `free_ids` 排除 (见 `compute_stats` 分组循环).
    /// 全局合计无意义 (会混入不计费供应商), 故前端仅按供应商各自展示.
    pub cache_saved: f64,
    /// 该供应商是否按量计费 (组内任一中转 ID 不在 free_ids 即视为计费). 仅计费供应商
    /// 在前端展示"已省"列; 免费/月套餐等不按量计费供应商 (如 opencode) 无费用基数,
    /// "已省"无实际意义, 前端以 "—" 占位而非误显示金额.
    pub billing: bool,
    /// 今日窗口 (东八区日界): 转发优化省下的输入 token 总数 (剥离推理链 + 历史裁剪 + 响应缓存命中).
    pub today_opt_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅剥离推理链省下的输入 token.
    pub today_strip_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅历史裁剪省下的输入 token.
    pub today_trim_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅响应缓存命中省下的 token.
    pub today_resp_cache_saved_tokens: u64,
}

/// 日趋势 (通用: 按小时/天/月聚合均使用此结构).
#[derive(Debug, Clone, Serialize)]
pub struct DailyTrend {
    pub date: String,
    /// 该时间桶的起始时间戳(秒), 用于按真实时间排序(避免 MM/DD 字符串比较跨月错序).
    pub ts: u64,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency: f64,
    /// 输入 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_prompt_tokens: u64,
    /// 输出 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_completion_tokens: u64,
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 该时间桶费用 (元).
    pub total_cost: f64,
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
    /// 命中率 = 命中 token / 总输入 token.
    pub cache_hit_rate: f64,
    /// 累计 KV 缓存**净**节省金额 (元): Σ(命中折扣 − 写入溢价, 见 `log_cache_saved`).
    /// 仅概览参考口径 (含不计费供应商, 语义有限); 供应商表「KV缓存省」列才是按供应商计费的权威口径.
    pub total_cache_saved: f64,
    /// 累计转发优化省下的输入 token 数: 剥离推理链 + 历史裁剪 + 响应缓存命中省下的 token (已持久化进日志, 跨重启累计).
    pub total_opt_saved_tokens: u64,
    /// 累计转发优化省下的费用 (元): 省下 token × 对应模型 input 单价. 仅计费供应商计入, 与 total_cost 同口径.
    pub total_opt_saved_fee: f64,
    /// 累计优化省量明细 (均来自日志, 跨重启累计): 仅剥离推理链省下的输入 token.
    pub total_strip_saved_tokens: u64,
    /// 累计优化省量明细: 仅历史裁剪省下的输入 token.
    pub total_trim_saved_tokens: u64,
    /// 累计优化省量明细: 仅响应缓存命中省下的 token.
    pub total_resp_cache_saved_tokens: u64,
    /// 本月窗口 (东八区月首0点起) 转发优化省下的输入 token 数 (剥离推理链 + 历史裁剪 + 响应缓存命中).
    pub month_opt_saved_tokens: u64,
    /// 近 30 天每日优化省量序列 (tokens, 按本地日界聚合, 末位=今日), 用于头条卡片 sparkline.
    pub opt_saved_series: Vec<u64>,
    /// 累计费用 (元), 价格缺失的模型按 0 计.
    pub total_cost: f64,
    // ─── 今日窗口统计 (东八区日界, 不受日志 5000 条滚动窗口封顶影响) ───
    pub today_requests: usize,
    pub today_success: usize,
    pub today_errors: usize,
    pub today_avg_latency_ms: f64,
    pub today_total_prompt_tokens: u32,
    pub today_total_completion_tokens: u32,
    pub today_total_cache_hit_tokens: u64,
    pub today_total_cache_miss_tokens: u64,
    /// 今日窗口费用 (元, 东八区日界).
    pub today_total_cost: f64,
    /// 今日窗口: 转发优化省下的输入 token 数 (剥离推理链 + 历史裁剪 + 响应缓存命中). 向东八区日界对齐, 与今日 6 卡片同口径.
    pub today_opt_saved_tokens: u64,
    /// 今日窗口: 转发优化省下的费用 (元), 与 today_total_cost 同口径 (仅计费供应商计入).
    pub today_opt_saved_fee: f64,
    /// 今日窗口优化省量明细: 仅剥离推理链省下的输入 token.
    pub today_strip_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅历史裁剪省下的输入 token.
    pub today_trim_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅响应缓存命中省下的 token.
    pub today_resp_cache_saved_tokens: u64,
    /// 累计优化省量的最早记录日期 (MM/DD), 给"累计"标注时间起算点, 避免无意义地 forever 累计; 无优化记录时为空串.
    pub opt_saved_since: String,
    /// 是否有任意模型配置了价格 (providers.json 的 model.price).
    /// 前端据此决定是否显示费用相关卡片/列, 避免无价格时显示 ¥0.00 误导.
    pub has_price_config: bool,
    /// 本轮 (进程启动以来) 累计统计 (原子计数, 不受日志滚动窗口封顶影响).
    pub session: SessionStats,
}

/// GET /admin/api/stats?granularity={hour|day|month} — 返回使用统计.
pub async fn api_stats(
    State(state): State<super::proxy::AppState>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<UsageStats> {
    let granularity = params.get("granularity").map(|s| s.as_str()).unwrap_or("day");
    // 缓存失效信号: 日志写入序列号 + providers.json 修改时间 (价格配置变化时失效).
    let seq = state.log_buffer.seq();
    let prov_mtime = std::fs::metadata("providers.json")
        .and_then(|m| m.modified())
        .map(|t| t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0))
        .unwrap_or(0);

    // 命中缓存且数据未变更 → 直接返回, 避免每次刷新/轮询全量克隆 + 聚合 (5000 条).
    {
        let guard = state.stats_cache.lock().await;
        if let Some(cached) = guard.as_ref() {
            if cached.0 == seq && cached.1 == granularity && cached.2 == prov_mtime {
                return Json(cached.3.clone());
            }
        }
    }

    let logs = state.log_buffer.drain_all().await;
    // 价格覆盖表: 中转 model id → 价格 (来自 providers.json 的 model.price, 优先级最高).
    let registry = state.registry.read().await;
    let mut price_overrides: HashMap<String, ModelPrice> = HashMap::new();
    // has_price_config 必须与实际计费口径一致: 不仅看 providers.json 显式 price 覆盖,
    // 还要包含 resolve_price 回退到的内置 DeepSeek 表 / 官方 endpoint 自动套价
    // (否则官方 DS 供应商靠内置表计费时, 会误判"未配置价格"而隐藏费用卡片).
    let mut has_price_config = false;
    // 免费中转 ID 集合: 与路由配置页 is_free 判定完全一致 (显式 free 优先, 否则回退命名识别).
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(model_id.as_str()) {
                free_ids.insert(model_id.clone());
            }
            if let Some(price) = mcfg.price {
                price_overrides.insert(model_id, price);
                has_price_config = true;
            } else if pricing::resolve_price(
                None,
                mcfg.upstream_model.as_deref(),
            )
            .is_some()
            {
                has_price_config = true;
            }
        }
    }
    drop(registry);
    let mut stats = compute_stats(&logs, &price_overrides, &free_ids);
    stats.has_price_config = has_price_config;
    stats.trends = compute_trends(&logs, granularity, &price_overrides);
    // 本轮 (进程启动以来) 累计统计: 原子计数快照, 不依赖滚动窗口.
    stats.session = state.log_buffer.session_stats();

    // 写回缓存 (仅当数据确实变更).
    *state.stats_cache.lock().await = Some((seq, granularity.to_string(), prov_mtime, stats.clone()));
    Json(stats)
}

/// 本地时区偏移 (秒). 默认按东八区 (UTC+8) 切分日/月界, 使趋势符合用户日历.
/// 若需跟随系统时区可改为读取本地 UTC 偏移, 但固定东八区对国内用户更可预期.
const TZ_OFFSET_SECS: i64 = 8 * 3600;

/// 自 1970-01-01 起的累计天数 -> (年, 月, 日). 与 days_from_civil 互逆, 无 chrono 依赖.
fn days_to_ymd(mut d: i64) -> (i64, i64, i64) {
    if d < 0 {
        d = 0;
    }
    let mut y = 1970i64;
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
    (y, m as i64, d + 1)
}

/// (年, 月, 日) -> 自 1970-01-01 起的累计天数 (Howard Hinnant 算法, 避免 chrono 依赖).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// 将 ts 对齐到指定粒度的桶起始秒 (本地时区). hour=整点, day=本地0点, month=本地月首0点.
fn bucket_start(ts: u64, granularity: &str) -> u64 {
    let local = (ts as i64) + TZ_OFFSET_SECS;
    match granularity {
        "hour" => {
            let base = (local / 3600) * 3600;
            (base - TZ_OFFSET_SECS) as u64
        }
        "month" => {
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            (days_from_civil(y, m, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => {
            let base = (local / 86400) * 86400;
            (base - TZ_OFFSET_SECS) as u64
        }
    }
}

/// 桶起始 ts 步进到下一个桶 (本地时区, 对齐粒度).
fn next_bucket(ts: u64, granularity: &str) -> u64 {
    match granularity {
        "hour" => ts + 3600,
        "month" => {
            let local = (ts as i64) + TZ_OFFSET_SECS;
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
            (days_from_civil(ny, nm, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => ts + 86400,
    }
}

/// 将 Unix 时间戳转成 MM/DD 日期字符串 (按东八区切日界, 避免 chrono 依赖).
fn ts_to_date(ts: u64) -> String {
    const SECS_PER_DAY: u64 = 86400;
    // 先按本地时区偏移归到本地当天 0 点, 再取天数.
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let days = if local < 0 { 0 } else { local / SECS_PER_DAY as i64 };
    let (_y, m, d) = days_to_ymd(days);
    format!("{:02}/{:02}", m, d)
}

fn ts_to_month(ts: u64) -> String {
    let date = ts_to_date(ts);
    let parts: Vec<&str> = date.split('/').collect();
    if parts.len() == 2 {
        // 年份须按本地时区天数推算 (与 ts_to_date 的本地切日一致),
        // 否则跨年边界会出现 "2025/01" 实为 2026 年 1 月 的串年 bug.
        let local = (ts as i64) + TZ_OFFSET_SECS;
        let y_days = if local < 0 { 0 } else { local / 86400 };
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

/// 价格解析结果记忆化键: (中转 model, endpoint). upstream_model 已并入 resolve_price 内部逻辑,
/// 但同一 (model, endpoint) 组合的解析结果稳定, 可安全复用 (详见 `PriceMemo`).
type PriceMemoKey = (String, String);

/// 请求级价格解析记忆化表: 避免 5000 条日志各自重复调用 `resolve_price` (内含内置表/回退查找).
/// 仅在单次 `compute_stats`/`compute_trends` 调用生命周期内有效, 不入全局.
struct PriceMemo<'a> {
    overrides: &'a HashMap<String, ModelPrice>,
    cache: std::cell::RefCell<HashMap<PriceMemoKey, Option<ModelPrice>>>,
}

impl<'a> PriceMemo<'a> {
    fn new(overrides: &'a HashMap<String, ModelPrice>) -> Self {
        Self {
            overrides,
            cache: std::cell::RefCell::new(HashMap::new()),
        }
    }

    /// 解析单条日志的价格 (记忆化): 命中则直接返回缓存的 `Option<ModelPrice>`.
    fn resolve(&self, log: &RequestLog) -> Option<ModelPrice> {
        let key = (log.model.clone(), log.endpoint.clone());
        if let Some(v) = self.cache.borrow().get(&key).copied() {
            return v;
        }
        let p = pricing::resolve_price(
            self.overrides.get(&log.model).copied(),
            log.upstream_model.as_deref(),
        );
        self.cache.borrow_mut().insert(key, p);
        p
    }
}

/// 单条请求费用（元）. 价格缺失时按 0 计（不计入费用）.
fn log_cost(memo: &PriceMemo, log: &RequestLog) -> f64 {
    // 命中本地响应缓存的请求未真实消费上游 token, 不计费,
    // 否则会与原请求重复计费, 导致费用虚高（高缓存命中率场景偏差极大）.
    if log.cached {
        return 0.0;
    }
    let p = memo.resolve(log);
    pricing::compute_cost(
        p,
        log.timestamp,
        log.prompt_tokens,
        log.completion_tokens,
        log.prompt_cache_hit_tokens,
    )
    .unwrap_or(0.0)
}

/// 单条请求因 KV 缓存**净**节省的金额（元）.
///
/// 净省 = 命中折扣 − 写入溢价:
///   - 命中折扣 = 命中 token × (input 价 − cache 读价)（读价缺失/无优惠则让差为 0）；
///   - 写入溢价 = 首次写入 token × (cache_creation 价 − input 价)（写入价通常高于 input 价,
///     如 DeepSeek 写入 1.25×）。该笔额外支出须从命中折扣扣除才是真实净省, 否则当 T2 历史
///     裁剪平移 user 前缀 / 系统提示词改动 / 切模型导致缓存反复重建时, 「已省」会被高估.
/// 输入价为 0 (未配置/月套餐) 或价格缺失 → 无计费基数, 净省记 0. 与 `log_cost` 复用同一价格
/// 解析与钳制口径 (读价/写入价缺失时回退 input 价, 对应项让差为 0), 防上游脏数据越界.
fn log_cache_saved(memo: &PriceMemo, log: &RequestLog) -> f64 {
    if log.cached {
        return 0.0;
    }
    let p = memo.resolve(log);
    let Some(p) = p else { return 0.0; };
    // 按请求时段选择生效价（高峰/空闲）; 与 compute_cost 同口径.
    let (input_price, _, cache_read) = pricing::effective(p, log.timestamp);
    // 输入价为 0 (价格未配置/不按量计费, 如月套餐) → 无计费基数, 净省记 0 (兜底防误计).
    if input_price == 0.0 {
        return 0.0;
    }
    let prompt = log.prompt_tokens as f64;
    // 与 compute_cost 一致拆分 hit / creation, 防上游脏数据导致负 fresh 或越界.
    let hit = (log.prompt_cache_hit_tokens as f64).min(prompt);
    let creation = (log.prompt_cache_creation_tokens as f64).min((prompt - hit).max(0.0));

    // 命中折扣: 命中 token 按 (input − 读价) 省; 读价缺失回退 input → 让差为 0.
    let read_saved = (input_price - cache_read).max(0.0);
    // 写入溢价: 首次写入按 cache_creation 价计 (通常高于 input); 缺失回退 input → 让差为 0.
    let creation_price = p.cache_creation_per_m.unwrap_or(input_price);
    let creation_penalty = (creation_price - input_price).max(0.0);

    // 净省不钳制到 0: 单条纯写入请求可能为负贡献, 聚合后自然抵消, 方能反映真实成本.
    hit / 1e6 * read_saved - creation / 1e6 * creation_penalty
}

/// 从日志列表计算聚合统计.
///
/// `price_overrides`: 中转 model id → 价格, 来自 providers.json 的 model.price（优先级最高）,
/// 缺失时回退内置默认价格表（见 `crate::pricing`）.
/// `free_ids`: 免费中转 ID 集合（来自注册表 is_free 判定）, 组内任一中转 ID 命中即标记免费.
fn compute_stats(
    logs: &[RequestLog],
    price_overrides: &HashMap<String, ModelPrice>,
    free_ids: &std::collections::HashSet<String>,
) -> UsageStats {
    // 价格解析记忆化: 本次聚合生命周期内复用 resolve_price 结果 (避免 5000 条重复解析).
    let memo = PriceMemo::new(price_overrides);
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
    let total_cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
    let total_cache_saved: f64 = logs.iter().map(|l| log_cache_saved(&memo, l)).sum();
    // 转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中): 累计 token 与折算费用 (按各模型 input 单价, 与 total_cost 同口径).
    let total_strip_saved_tokens: u64 = logs.iter().map(|l| l.strip_saved_tokens as u64).sum();
    let total_trim_saved_tokens: u64 = logs.iter().map(|l| l.trim_saved_tokens as u64).sum();
    let total_resp_cache_saved_tokens: u64 = logs.iter().map(|l| l.resp_cache_saved_tokens as u64).sum();
    // 累计优化省量 = 剥离推理链 + 历史裁剪 + 响应缓存命中 (三者均来自日志, 跨重启累计).
    let total_opt_saved_tokens: u64 = total_strip_saved_tokens + total_trim_saved_tokens + total_resp_cache_saved_tokens;
    let total_opt_saved_fee: f64 = logs
        .iter()
        .map(|l| {
            let opt = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as f64;
            if opt <= 0.0 {
                return 0.0;
            }
            if let Some(p) = memo.resolve(l) {
                // 按请求时段选择生效 input 价（高峰/空闲）, 与 log_cost 同口径.
                let (input_price, _, _) = pricing::effective(p, l.timestamp);
                if input_price > 0.0 {
                    return opt / 1e6 * input_price;
                }
            }
            0.0
        })
        .sum();
    // 命中率口径: 命中 / 总输入 token (与 opencode-visual-cache 一致: 缓存读 / prompt_tokens).
    // 分母用总输入而非 hit+miss: creation(首次写入) 已从 miss 拆出, hit+miss = prompt - creation 会偏小.
    let cache_hit_rate = if total_prompt_tokens > 0 {
        total_cache_hit_tokens as f64 / total_prompt_tokens as f64
    } else {
        0.0
    };

    // ─── 概览窗口聚合 (随 range 切换: today=当日 / hour=近24h / day=近30天 / month=近12月) ───
    // ─── 今日窗口聚合 (东八区 0 点起) ───
    // 日志文件有 5000 条滚动上限, "累计"口径会封顶失真; 今日口径不受影响, 用于概览小卡片.
    let now = now_ts() as i64;
    let today_start = ((now + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;
    let today_logs: Vec<&RequestLog> = logs.iter().filter(|l| l.timestamp >= today_start).collect();
    let today_requests = today_logs.len();
    let today_success = today_logs.iter().filter(|l| l.status < 400).count();
    let today_errors = today_requests - today_success;
    let today_avg_latency_ms = if today_requests > 0 {
        today_logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / today_requests as f64
    } else {
        0.0
    };
    let today_total_prompt_tokens: u32 = today_logs.iter().map(|l| l.prompt_tokens).sum();
    let today_total_completion_tokens: u32 = today_logs.iter().map(|l| l.completion_tokens).sum();
    let today_total_cache_hit_tokens: u64 = today_logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
    let today_total_cache_miss_tokens: u64 = today_logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
    let today_total_cost: f64 = today_logs.iter().map(|l| log_cost(&memo, l)).sum();
    // 今日窗口的转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中): token 与折算费用,
    // 与 today_total_cost 同口径 (东八区日界), 供概览头条卡片展示, 不再永远累计.
    let today_strip_saved_tokens: u64 = today_logs.iter().map(|l| l.strip_saved_tokens as u64).sum();
    let today_trim_saved_tokens: u64 = today_logs.iter().map(|l| l.trim_saved_tokens as u64).sum();
    let today_resp_cache_saved_tokens: u64 = today_logs.iter().map(|l| l.resp_cache_saved_tokens as u64).sum();
    // 今日优化省量 = 三者今日窗口之和 (与今日 6 卡片同口径, 东八区日界).
    let today_opt_saved_tokens: u64 = today_strip_saved_tokens + today_trim_saved_tokens + today_resp_cache_saved_tokens;
    let today_opt_saved_fee: f64 = today_logs
        .iter()
        .map(|l| {
            let opt = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as f64;
            if opt <= 0.0 {
                return 0.0;
            }
            if let Some(p) = memo.resolve(l) {
                // 按请求时段选择生效 input 价（高峰/空闲）, 与 log_cost 同口径.
                let (input_price, _, _) = pricing::effective(p, l.timestamp);
                if input_price > 0.0 {
                    return opt / 1e6 * input_price;
                }
            }
            0.0
        })
        .sum();
    // 累计优化省量的最早记录日期 (取有优化省量日志的最早时间戳), 用于给"累计"标注起算点.
    let opt_saved_since = logs
        .iter()
        .filter(|l| (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) > 0)
        .map(|l| l.timestamp)
        .min()
        .map(ts_to_date)
        .unwrap_or_default();

    // 本月窗口聚合 (东八区月首0点起): 月优化省量 = 三者本月之和.
    let month_start = bucket_start(now_ts(), "month");
    let month_logs: Vec<&RequestLog> = logs.iter().filter(|l| l.timestamp >= month_start).collect();
    let month_opt_saved_tokens: u64 = month_logs
        .iter()
        .map(|l| (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64)
        .sum();

    // 近 30 天每日优化省量序列 (末位=今日), 用于头条卡片 sparkline.
    // 按本地日界分桶: 桶 key = 当日0点 ts; 遍历所有日志累加当日省量, 再按日界补齐最近30天空桶.
    let mut day_buckets: BTreeMap<u64, u64> = BTreeMap::new();
    for l in logs.iter() {
        let day_start = bucket_start(l.timestamp, "day");
        let saved = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64;
        *day_buckets.entry(day_start).or_insert(0) += saved;
    }
    let mut opt_saved_series: Vec<u64> = Vec::with_capacity(30);
    let mut ts = bucket_start(now_ts(), "day");
    for _ in 0..30 {
        opt_saved_series.push(*day_buckets.get(&ts).unwrap_or(&0));
        // 向前推一天 (day 粒度)
        ts = ts.saturating_sub(86400);
    }
    opt_saved_series.reverse(); // 末位=今日, 首位=30天前

    // 按"供应商/上游模型"组合分组: 上游模型缺失时回退到中转 model, 避免按中转 ID 统计造成的混乱.
    // 过滤 provider 为空或 "-" 的占位日志 (模型未找到/解析失败时的 404 占位), 避免模型明细出现 "-/xxx" 幽灵条目.
    let mut model_map: HashMap<String, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        if log.provider.is_empty() || log.provider == "-" {
            continue;
        }
        let effective = log
            .upstream_model
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| log.model.clone());
        let key = format!("{}/{}", log.provider, effective);
        model_map.entry(key).or_default().push(log);
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
            let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
            // 纯生成吐字速度: 仅累计首 token 延迟已知的流式请求 (ft<总耗时 且 有输出 token),
            // gen_ms = 总耗时 - 首 token 延迟, 排除排队与 TTFT.
            let (gen_ct, gen_ms): (u64, u64) = logs
                .iter()
                .filter_map(|l| {
                    l.first_token_ms
                        .filter(|&ft| ft < l.latency_ms && l.completion_tokens > 0)
                        .map(|ft| (l.completion_tokens as u64, l.latency_ms - ft))
                })
                .fold((0u64, 0u64), |(ac, am), (c, m)| (ac + c, am + m));
            let gen_speed = if gen_ms > 0 {
                gen_ct as f64 / gen_ms as f64 * 1000.0
            } else {
                0.0
            };
            // 中转 ID 集合 (去重, 排序) — 便于对照"该上游模型由哪些中转 ID 映射而来".
            let mut aliases: Vec<String> = logs
                .iter()
                .map(|l| l.model.clone())
                .collect::<std::collections::HashSet<_>>()
                .into_iter()
                .collect();
            aliases.sort();

            // 单价取组内首条日志解析的结果（覆盖优先, 仅官方 DS 供应商回退内置表）,
            // 供前端单价列展示.
            let price = pricing::resolve_price(
                price_overrides.get(&logs[0].model).copied(),
                logs[0].upstream_model.as_deref(),
            );

            ModelStats {
                model,
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
                gen_speed,
                aliases,
                total_cost: cost,
                price,
                free: logs.iter().any(|l| free_ids.contains(&l.model)),
            }
        })
        .collect();
    per_model.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按供应商分组 (过滤空值和 "-" 占位符: 请求体解析失败/模型路由未找到时 provider 硬编码为 "-").
    let mut prov_map: HashMap<&str, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        let provider = if log.provider.is_empty() || log.provider == "-" {
            continue;
        } else {
            &log.provider
        };
        prov_map.entry(provider).or_default().push(log);
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
            let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
            // KV 缓存命中节省金额: 仅计费 (非免费/月套餐) 供应商计入, 排除不按量计费的模型.
            let cache_saved: f64 = logs
                .iter()
                .filter(|l| !free_ids.contains(&l.model))
                .map(|l| log_cache_saved(&memo, l))
                .sum();
            // 是否按量计费: 组内任一中转 ID 不在 free_ids (免费/月套餐) 即视为计费供应商.
            let billing = logs.iter().any(|l| !free_ids.contains(&l.model));
            let today_cost: f64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| log_cost(&memo, l))
                .sum();
            // 今日窗口转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中), 与今日卡片同口径 (东八区日界).
            let today_strip_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.strip_saved_tokens as u64)
                .sum();
            let today_trim_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.trim_saved_tokens as u64)
                .sum();
            let today_resp_cache_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.resp_cache_saved_tokens as u64)
                .sum();
            let today_opt_saved_tokens =
                today_strip_saved_tokens + today_trim_saved_tokens + today_resp_cache_saved_tokens;
            // 纯生成吐字速度, 同 per_model 口径.
            let (gen_ct, gen_ms): (u64, u64) = logs
                .iter()
                .filter_map(|l| {
                    l.first_token_ms
                        .filter(|&ft| ft < l.latency_ms && l.completion_tokens > 0)
                        .map(|ft| (l.completion_tokens as u64, l.latency_ms - ft))
                })
                .fold((0u64, 0u64), |(ac, am), (c, m)| (ac + c, am + m));
            let gen_speed = if gen_ms > 0 {
                gen_ct as f64 / gen_ms as f64 * 1000.0
            } else {
                0.0
            };
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
                gen_speed,
                total_cost: cost,
                today_cost,
                cache_saved,
                billing,
                today_opt_saved_tokens,
                today_strip_saved_tokens,
                today_trim_saved_tokens,
                today_resp_cache_saved_tokens,
            }
        })
        .collect();
    per_provider.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按天分组趋势 (默认)
    let trends = compute_trends(logs, "day", price_overrides);

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
        today_requests,
        today_success,
        today_errors,
        today_avg_latency_ms,
        today_total_prompt_tokens,
        today_total_completion_tokens,
        today_total_cache_hit_tokens,
        today_total_cache_miss_tokens,
        total_cost,
        total_cache_saved,
        total_opt_saved_tokens,
        total_opt_saved_fee,
        total_strip_saved_tokens,
        total_trim_saved_tokens,
        total_resp_cache_saved_tokens,
        month_opt_saved_tokens,
        opt_saved_series,
        today_total_cost,
        today_opt_saved_tokens,
        today_opt_saved_fee,
        today_strip_saved_tokens,
        today_trim_saved_tokens,
        today_resp_cache_saved_tokens,
        opt_saved_since,
        // has_price_config 在 api_stats 中按 price_overrides 是否非空注入.
        has_price_config: false,
        // 本轮 (进程级) 统计由 LogBuffer 原子计数提供, compute_stats 内无来源 → 默认空,
        // api_stats 返回前会覆盖为 log_buffer.session_stats().
        session: SessionStats::default(),
    }
}

/// 按指定粒度聚合趋势数据.
///
/// 关键: 聚合后按粒度补齐**固定窗口**的空桶 (hour=24 整点 / day=最近30天 / month=最近12个月),
/// 使时间轴严格对齐且递增 —— 最新桶=当前粒度边界, 最旧桶=窗口起点 (含无请求空段),
/// 避免"只显示有数据的桶导致段数不定、时间轴不连续/不对应"的问题.
fn compute_trends(
    logs: &[RequestLog],
    granularity: &str,
    price_overrides: &HashMap<String, ModelPrice>,
) -> Vec<DailyTrend> {
    // 趋势桶内每条日志仍走 log_cost, 复用记忆化避免重复解析价格.
    let memo = PriceMemo::new(price_overrides);
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
    // 用 BTreeMap<桶起始ts, 趋势> 聚合 (按 ts 有序, 且桶起始保证同桶唯一).
    let mut buckets: BTreeMap<u64, DailyTrend> = BTreeMap::new();
    for (date, logs) in map {
        let reqs = logs.len();
        // 桶起始 ts: 取桶内首条日志对齐到粒度边界 (同一桶所有日志对齐后相同).
        let ts = bucket_start(logs.first().map(|l| l.timestamp).unwrap_or(0), granularity);
        let errs = logs.iter().filter(|l| l.status >= 400).count();
        let avg_lat = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
        let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
        let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
        let pt: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
        let ct: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
        let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
        buckets.insert(
            ts,
            DailyTrend {
                date,
                ts,
                requests: reqs,
                errors: errs,
                avg_latency: avg_lat,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
                total_cost: cost,
            },
        );
    }

    // 补齐固定窗口空桶 (含无请求段), 保证时间轴对齐且递增.
    let window: u64 = match granularity {
        "hour" => 24,
        "month" => 12,
        _ => 30,
    };
    let now = now_ts();
    let end = bucket_start(now, granularity);
    // 窗口起点: 优先取「最近 N 个粒度」对齐边界; 但若真实数据更早, 则延伸到真实最早桶,
    // 避免丢弃窗口外的历史数据 (同时保证正常情况即固定滑动窗口).
    let mut now_start = end;
    for _ in 1..window {
        now_start = prev_bucket(now_start, granularity);
    }
    let real_min = buckets.keys().next().copied().unwrap_or(now_start);
    let start = now_start.min(real_min);
    let mut ts = start;
    loop {
        buckets.entry(ts).or_insert_with(|| DailyTrend {
            date: key_fn(ts),
            ts,
            requests: 0,
            errors: 0,
            avg_latency: 0.0,
            total_prompt_tokens: 0,
            total_completion_tokens: 0,
            total_cache_hit_tokens: 0,
            total_cache_miss_tokens: 0,
            total_cost: 0.0,
        });
        if ts == end {
            break;
        }
        ts = next_bucket(ts, granularity);
        if ts > end {
            // 安全护栏: 步进越过 end (理论上不会发生) 则停止.
            break;
        }
    }

    buckets.into_values().collect()
}

/// 桶起始 ts 步进到上一个桶 (本地时区, 对齐粒度). 与 next_bucket 对称.
fn prev_bucket(ts: u64, granularity: &str) -> u64 {
    match granularity {
        "hour" => ts.saturating_sub(3600),
        "month" => {
            let local = (ts as i64) + TZ_OFFSET_SECS;
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            let (py, pm) = if m == 1 { (y - 1, 12) } else { (y, m - 1) };
            (days_from_civil(py, pm, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => ts.saturating_sub(86400),
    }
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
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            first_token_ms: None,
            upstream_model: None,
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
        let s = compute_stats(&logs, &HashMap::new(), &std::collections::HashSet::new());
        // 全局: 命中 80+60+0=140, 未命中 20+40+100=160
        assert_eq!(s.total_cache_hit_tokens, 140);
        assert_eq!(s.total_cache_miss_tokens, 160);
        assert!((s.cache_hit_rate - 140.0 / 300.0).abs() < 1e-9);
        // 模型分组: 按"供应商/上游模型"组合 (upstream 缺失回退 model) => "ds/deepseek"
        let ds = s.per_model.iter().find(|m| m.model == "ds/deepseek").unwrap();
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
        let s = compute_stats(&logs, &HashMap::new(), &std::collections::HashSet::new());
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
        let trends = compute_trends(&logs, "day", &HashMap::new());
        // 补齐窗口后至少 30 个日桶 (含空桶); 两条日志都应归入东八区 08/02 同一桶.
        assert!(trends.len() >= 30, "日粒度应补齐最近 30 天窗口");
        let bucket = trends.iter().find(|d| d.date == "08/02").expect("应有 08/02 桶");
        assert_eq!(bucket.requests, 2);
        // 时间轴严格递增 (按 ts).
        let mut prev = 0u64;
        for d in &trends {
            assert!(d.ts >= prev, "日桶必须按时间递增");
            prev = d.ts;
        }
    }

    /// 趋势: 跨多日/多月日志能正确聚合成多个桶 (验证统计基于全量而非内存截断).
    #[test]
    fn test_trend_full_data_multi_bucket() {
        // 2026-07-15T00:00:00Z 起, 间隔 5 天, 全部落在 07 月.
        let base = 1_784_073_600;
        let logs: Vec<RequestLog> = (0..3)
            .map(|i| mk_ts("m", "p", base + i * 5 * 86400))
            .collect();
        let trends = compute_trends(&logs, "day", &HashMap::new());
        assert!(trends.len() >= 30, "日粒度至少补齐 30 天窗口");
        // 非空桶应有 3 个 (跨 07-15 / 07-20 / 07-25), 其余为补齐的空桶.
        let non_empty = trends.iter().filter(|d| d.requests > 0).count();
        assert_eq!(non_empty, 3, "跨多日应生成 3 个非空趋势桶");
        // 按月聚合: 全部落在同一个月 (07 月), 仅 1 个非空桶; 窗口补齐 12 个月.
        let month = compute_trends(&logs, "month", &HashMap::new());
        assert_eq!(month.len(), 12, "月粒度补齐 12 个月窗口");
        let m_non_empty = month.iter().filter(|d| d.requests > 0).count();
        assert_eq!(m_non_empty, 1);
        assert!(month.iter().any(|d| d.date == "2026/07" && d.requests == 3));
    }

    /// 趋势: 小时粒度补齐最近 24 个整点桶 (最新=当前小时, 最旧=当前小时-23h),
    /// 且时间轴严格递增 (回应用户反馈: 小时应显示 24 段、时间对应).
    #[test]
    fn test_trend_hour_window_24_segments() {
        let now = now_ts();
        // 往当前小时内塞一条日志, 验证它归入「当前小时」桶.
        let logs = vec![mk_ts("m", "p", now)];
        let trends = compute_trends(&logs, "hour", &HashMap::new());
        assert_eq!(trends.len(), 24, "小时粒度应补齐最近 24 个整点");
        // 时间轴严格递增.
        let mut prev = 0u64;
        for d in &trends {
            assert!(d.ts >= prev, "小时桶必须按时间递增");
            prev = d.ts;
        }
        // 最新桶 = 当前小时整点, 且其内有 1 条请求.
        let last = trends.last().expect("应有最新桶");
        assert_eq!(last.requests, 1, "当前小时桶应包含刚插入的日志");
        // 最新桶时间 = 当前时间对齐到整点 (<= now).
        assert!(last.ts <= now);
        assert_eq!(last.ts % 3600, 0, "桶起始须为整点");
        // 最旧桶 = 最新桶 - 23 小时.
        let first = trends.first().expect("应有最旧桶");
        assert_eq!(last.ts - first.ts, 23 * 3600);
    }

    /// 构造带指定时间戳的日志 (复用 mk 的其余默认值).
    fn mk_ts(model: &str, provider: &str, ts: u64) -> RequestLog {
        let mut log = mk(model, provider, 0, 0);
        log.timestamp = ts;
        log
    }

    /// 回归: 跨年边界月桶年份必须正确 (ts_to_month 曾用 UTC 天数推年,
    /// 导致本地 2026-01-01 被错标为 "2025/01"). 同时校验月桶序列严格按时间递增.
    #[test]
    fn test_trend_month_year_boundary() {
        // 2026-01-01T00:30:00Z → 东八区 2026-01-01 08:30, 属 2026/01 (非 2025/01).
        let ts = 1_768_185_000;
        let logs = vec![mk_ts("m", "p", ts)];
        let month = compute_trends(&logs, "month", &HashMap::new());
        assert_eq!(month.len(), 12, "月粒度补齐 12 个月窗口");
        // 必须存在 "2026/01" 桶 (而非 "2025/01"), 且含 1 条请求.
        let jan = month
            .iter()
            .find(|d| d.date == "2026/01")
            .expect("跨年边界应正确标记为 2026/01");
        assert_eq!(jan.requests, 1);
        assert!(!month.iter().any(|d| d.date == "2025/01"), "不得出现串年的 2025/01");
        // 月桶序列按时间严格递增 (date 字符串不再因串年错序).
        let mut prev = String::new();
        for d in &month {
            assert!(d.date >= prev, "月桶必须按时间递增: {} < {}", d.date, prev);
            prev = d.date.clone();
        }
    }

    /// 本轮 (进程级) 累计: push 时原子累加, 请求/成功/输入/输出/KV 命中均正确.
    #[test]
    fn session_stats_accumulates_on_push() {
        let buf = LogBuffer::new();
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let mut log = mk("m", "p", 80, 20);
            buf.push(log.clone()).await; // 成功, hit=80 miss=20
            log.prompt_cache_hit_tokens = 60;
            log.prompt_cache_miss_tokens = 40;
            buf.push(log.clone()).await; // 成功
            log.status = 500;
            log.prompt_cache_hit_tokens = 0;
            log.prompt_cache_miss_tokens = 0;
            buf.push(log).await; // 失败
        });
        let s = buf.session_stats();
        assert_eq!(s.requests, 3);
        assert_eq!(s.success, 2, "500 不算成功");
        assert_eq!(s.prompt_tokens, 300, "3 条 × 100");
        assert_eq!(s.completion_tokens, 150, "3 条 × 50");
        assert_eq!(s.cache_hit_tokens, 140);
        assert_eq!(s.cache_miss_tokens, 60);
    }

    /// 「已省」改为净省口径: 命中折扣 − 写入溢价, 与 `compute_cost` 输入拆分一致.
    /// 锁定缓存被反复重建 (T2 裁剪平移前缀 / 改系统提示词 / 切模型) 时不再高估省钱.
    #[test]
    fn test_cache_saved_net_of_creation_premium() {
        // DeepSeek 类价格 (元/百万 token): 输入 2, 读 0.2, 写入 2.5.
        let price = ModelPrice {
            input_per_m: 2.0,
            output_per_m: 8.0,
            cache_read_per_m: Some(0.2),
            cache_creation_per_m: Some(2.5),
            input_per_m_offpeak: 0.0,
            output_per_m_offpeak: 0.0,
            cache_read_per_m_offpeak: 0.0,
        };
        let mut ov = HashMap::new();
        ov.insert("ds".to_string(), price);
        let memo = PriceMemo::new(&ov);

        // 1) 纯命中 1M token, 无写入 → 净省 = 1 × (2 − 0.2) = 1.8.
        let mut log = mk("ds", "ds", 1_000_000, 0);
        log.prompt_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log) - 1.8).abs() < 1e-9);

        // 2) 纯写入 1M token (无命中) → 净省 = −1 × (2.5 − 2) = −0.5 (写入溢价抵消, 不钳到 0).
        let mut log2 = mk("ds", "ds", 0, 0);
        log2.prompt_tokens = 1_000_000;
        log2.prompt_cache_creation_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log2) - (-0.5)).abs() < 1e-9);

        // 3) 命中 1M + 写入 1M → 净省 = 1.8 − 0.5 = 1.3.
        let mut log3 = mk("ds", "ds", 1_000_000, 0);
        log3.prompt_tokens = 2_000_000;
        log3.prompt_cache_creation_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log3) - 1.3).abs() < 1e-9);

        // 4) 命中本地响应缓存 (log.cached) → 未真实消费上游, 省额记 0.
        let mut log4 = mk("ds", "ds", 1_000_000, 0);
        log4.cached = true;
        assert_eq!(log_cache_saved(&memo, &log4), 0.0);

        // 5) 输入价为 0 (月套餐/未配置) → 无计费基数, 净省记 0.
        let free = ModelPrice { input_per_m: 0.0, output_per_m: 0.0, cache_read_per_m: None, cache_creation_per_m: None, input_per_m_offpeak: 0.0, output_per_m_offpeak: 0.0, cache_read_per_m_offpeak: 0.0 };
        let mut ov2 = HashMap::new();
        ov2.insert("opencode".to_string(), free);
        let memo2 = PriceMemo::new(&ov2);
        let mut log5 = mk("opencode", "oc", 1_000_000, 0);
        log5.prompt_tokens = 1_000_000;
        assert_eq!(log_cache_saved(&memo2, &log5), 0.0);
    }
}
