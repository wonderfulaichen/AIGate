//! 反向代理核心 — 读取请求, 按模型路由, 注入 key, SSE 流式透传.
//!
//! 路由表由 providers.json 定义, 支持多个供应商. 详见 providers.rs.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use futures::Stream;
use reqwest::Client;
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::admin::LogBuffer;
use crate::keys::KeyStore;
use crate::providers::ProviderRegistry;
use crate::circuit_breaker::{CircuitBreaker, CircuitBreakerConfig};
use crate::cache::ResponseCache;

/// 共享状态: HTTP client + 供应商路由表 + 请求日志缓冲区 + Key 存储.
#[derive(Clone)]
pub struct AppState {
    pub client: Client,
    /// 路由表, RwLock 支持热重载.
    pub registry: Arc<RwLock<ProviderRegistry>>,
    /// 内存请求日志缓冲区.
    pub log_buffer: LogBuffer,
    /// API Key 存储.
    pub key_store: KeyStore,
    /// 管理面板 API 鉴权令牌 (None 表示不鉴权).
    pub admin_token: Option<String>,
    /// 熔断阈值配置 (用于热重载时按配置补齐新供应商的熔断器).
    pub breaker: CircuitBreakerConfig,
    /// 响应缓存 (实验功能, 默认关闭, 面板可开启).
    pub cache: std::sync::Arc<ResponseCache>,
    /// 流式响应空闲超时.
    pub stream_idle_timeout: Duration,
    /// 瞬态失败最大重试次数 (仅对连接/超时错误与流式前 5xx 重试).
    pub retry_max: u32,
    /// 重试退避基数 (毫秒): 每次重试前等待 base*attempt + 抖动.
    pub retry_backoff: Duration,
    /// 熔断器表: provider name -> 该供应商的熔断器 (std Mutex, 临界区极短).
    pub breakers: BreakerMap,
}

/// 熔断器表类型别名.
pub type BreakerMap = Arc<Mutex<HashMap<String, CircuitBreaker>>>;

/// 查询某供应商是否允许发送请求 (会消费 HalfOpen 的探测名额).
///
/// 返回 false 表示熔断处于 Open, 或 HalfOpen 下探测名额已占用,
/// 调用方应快速失败 (503) 而非打向上游.
pub fn check_breaker(breakers: &BreakerMap, provider: &str) -> bool {
    let mut g = breakers.lock().expect("breaker lock poisoned");
    g.entry(provider.to_string())
        .or_insert_with(CircuitBreaker::new)
        .allow_request()
}

/// 上报一次上游请求结果. success=true 记成功, false 记失败.
pub fn report_breaker(breakers: &BreakerMap, provider: &str, success: bool) {
    let mut g = breakers.lock().expect("breaker lock poisoned");
    let cb = g.entry(provider.to_string()).or_insert_with(CircuitBreaker::new);
    if success {
        cb.record_success();
    } else {
        cb.record_failure();
    }
}

/// 供应商配置热重载后同步熔断表.
///
/// - 新增的供应商: 按 `cfg` 阈值创建熔断器 (避免退回默认阈值).
/// - 已删除的供应商: 从表中移除.
/// - 已存在的供应商: 保留其当前熔断状态 (不重置在跑的熔断).
pub fn sync_breakers(breakers: &BreakerMap, provider_names: &[String], cfg: &CircuitBreakerConfig) {
    let mut g = breakers.lock().expect("breaker lock poisoned");
    g.retain(|name, _| provider_names.iter().any(|p| p == name));
    for name in provider_names {
        g.entry(name.clone())
            .or_insert_with(|| CircuitBreaker::with_config(cfg.clone()));
    }
}

/// 启动/健康检查用的连通性探测: 复用 reqwest 客户端发真实 HTTP 请求.
///
/// 复用客户端的 IPv4 解析器 + 系统代理 + connect_timeout, 与真实转发请求走**同一条**
/// 网络路径, 因此对"只有走客户端路径才可达"的供应商 (如依赖代理出网、或 IPv6 被屏蔽)
/// 也能正确判定, 不会出现裸 TCP 误判不可达的问题.
///
/// 对供应商的 `/models` 端点发 GET: 任何 HTTP 响应 (含 401/404/TLS 错误) 都说明已建连,
/// 视为**可达**; 仅当连接层错误 (DNS 失败 / 连接被拒 / 超时) 才视为**不可达**.
/// `/models` 仅返回模型列表元数据, 不消耗生成 token, 成本可忽略.
pub async fn precheck_provider(client: &Client, endpoint: &str, timeout: Duration) -> bool {
    let models_url = endpoint.replace("/chat/completions", "/models");
    match client.get(&models_url).timeout(timeout).send().await {
        // 任何 HTTP 响应 = TCP/TLS 已建连 = 可达.
        Ok(_) => true,
        // 区分连接层错误与其他错误: 只有连接/超时失败才判不可达.
        Err(e) => !e.is_connect() && !e.is_timeout(),
    }
}

/// 处理 POST /v1/chat/completions.
///
/// 流程: 读 body -> 解析 model -> 查路由表 -> 注入 key + 模型参数 -> 转发 -> 流式回传.
pub async fn chat_completions(
    State(state): State<AppState>,
    headers: HeaderMap,
    req: Request<Body>,
) -> Result<Response, Response> {
    let start = std::time::Instant::now();

    // 1. 读取请求 body
    let (_, body) = req.into_parts();
    let bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => {
            crate::admin::record_request(
                &state.log_buffer, "-", "-", "-", 400, start, 0, Some(e.to_string()),
            ).await;
            return Err(error_response(StatusCode::BAD_REQUEST, &e.to_string()));
        }
    };

    // 2. 解析 model 字段
    let model = parse_model(&bytes).ok_or_else(|| {
        error_response(StatusCode::BAD_REQUEST, "missing or invalid model field")
    })?;

    // 3. 查路由表 (RwLock 读锁)
    let route = match state.registry.read().await.lookup(&model) {
        Some(r) => r,
        None => {
            crate::admin::record_request(
                &state.log_buffer, &model, "-", "-", 404, start, bytes.len(), None,
            ).await;
            return Err(error_response(
                StatusCode::NOT_FOUND,
                &format!("unknown model '{model}' - not in providers.json"),
            ));
        }
    };
    let provider_name = route.provider.name.clone();
    let endpoint = route.provider.endpoint.clone();
    let model_cfg = route.model.clone();
    let provider = route.provider.clone(); // 保存 provider 用于后续 headers 和 api_key
    drop(route); // 释放读锁

    // 2.5 熔断检查: 供应商处于 Open / 探测名额占用时快速失败 (503),
    //     避免把请求打向已挂的上游, 也避免苦等 660s 超时.
    if !check_breaker(&state.breakers, &provider_name) {
        warn!("proxy: circuit open for provider={provider_name}, fast-fail 503");
        crate::admin::record_request(
            &state.log_buffer, &model, &provider_name, &endpoint, 503, start, bytes.len(),
            Some("circuit breaker open".to_string()),
        ).await;
        return Err(error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            &format!("provider '{provider_name}' circuit open (recovering, will retry shortly)"),
        ));
    }

    info!(
        "proxy: model={model}, provider={provider_name}, endpoint={endpoint}, body_len={}",
        bytes.len()
    );

    // 4. 获取 API key (通过 KeyStore)
    let key = match state.registry.read().await.api_key(&provider, &state.key_store).await {
        Ok(k) => k,
        Err(e) => {
            crate::admin::record_request(
                &state.log_buffer, &model, &provider_name, &endpoint, 500, start, bytes.len(), Some(e.clone()),
            ).await;
            return Err(error_response(StatusCode::INTERNAL_SERVER_ERROR, &e));
        }
    };

    // 5. 注入模型级参数 (reasoning_effort / extra_body): 固定按 providers.json 配置档注入,
    //    不做自适应探测/降级 —— 思考强度完全由配置档决定 (客户端显式关闭/自带档位时尊重客户端).
    let (bytes, _injected) = inject_model_params(bytes, &model_cfg);
    let body_len_val = bytes.len(); // 在 bytes 被 move 前保存

    // 5.5 响应缓存查询: 缓存开启且为非流式请求时, 命中直接返回 (省 token + 延迟).
    let cache_key: Option<String> = if state.cache.is_enabled() {
        serde_json::from_slice::<serde_json::Value>(&bytes)
            .ok()
            .and_then(|v| ResponseCache::make_key(&v))
    } else {
        None
    };
    if let Some(key) = &cache_key {
        if let Some(cached) = state.cache.get(key) {
            let (pt, ct, hit, miss) = extract_usage(&cached);
            crate::admin::record_request_with_tokens(
                &state.log_buffer, &model, &provider_name, &endpoint, start, pt, ct, cached.len(),
                true, hit, miss,
            ).await;
            return Ok(axum::Json(
                serde_json::from_str::<serde_json::Value>(&cached).unwrap_or(serde_json::Value::Null),
            ).into_response());
        }
    }

    // 6. 构造转发请求 — 过滤客户端 headers, 只保留标准 headers,
    //    避免非标准 headers (如 x-ide-type) 导致 upstream 拒绝服务.
    let mut req_headers = filter_headers(&headers);
    req_headers.insert(
        HeaderName::from_static("authorization"),
        HeaderValue::from_str(&format!("Bearer {key}"))
            .map_err(|e| error_response(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()))?,
    );
    // 注入供应商配置的额外 headers
    if let Some(extra) = &provider.headers {
        for (k, v) in extra {
            if let (Ok(name), Ok(val)) = (
                HeaderName::from_bytes(k.as_bytes()),
                HeaderValue::from_str(v),
            ) {
                req_headers.insert(name, val);
            }
        }
    }

    // 8. 发送请求 (带瞬态重试) — 仅对连接/超时错误与流式开始前返回的 5xx 重试,
    //    不含 429 (视为终态). 熔断上报统一在"最终 attempt"结果上执行一次,
    //    避免重试放大失败计数误开熔断.
    let ep_clone = endpoint.clone();
    let total_attempts = state.retry_max.saturating_add(1);
    let mut upstream: Option<reqwest::Response> = None;
    let mut send_err: Option<String> = None;
    let mut attempt = 0u32;
    loop {
        attempt += 1;
        // 每次重试前重新确认熔断状态, 若已 Open 则快速失败 (503).
        if !check_breaker(&state.breakers, &provider_name) {
            warn!("proxy: circuit open for provider={provider_name}, fast-fail 503");
            crate::admin::record_request(
                &state.log_buffer, &model, &provider_name, &endpoint, 503, start,
                body_len_val, Some("circuit breaker open".to_string()),
            ).await;
            return Err(error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                &format!("provider '{provider_name}' circuit open (recovering, will retry shortly)"),
            ));
        }
        match state
            .client
            .request(Method::POST, &endpoint)
            .headers(req_headers.clone())
            .header("content-type", "application/json")
            .header("accept", "text/event-stream")
            .body(bytes.clone())
            .send()
            .await
        {
            Ok(resp) => {
                let status = resp.status().as_u16();
                if status >= 500 {
                    // 5xx 可重试 (服务端瞬态故障); 不重试 4xx (含 429).
                    if attempt < total_attempts {
                        warn!("proxy: upstream {status} (attempt {attempt}/{total_attempts}), retrying");
                        retry_backoff(attempt, state.retry_backoff).await;
                        continue;
                    }
                    upstream = Some(resp);
                    break;
                }
                // 2xx 成功或 4xx 终态, 不重试.
                upstream = Some(resp);
                break;
            }
            Err(e) => {
                let chain = format_error_chain(&e);
                // 仅连接层 / 超时错误可重试; 其余 (如请求构造错误) 直接失败.
                let retryable = e.is_connect() || e.is_timeout();
                if retryable && attempt < total_attempts {
                    warn!("proxy: send() failed (attempt {attempt}/{total_attempts}): {chain}, retrying");
                    retry_backoff(attempt, state.retry_backoff).await;
                    continue;
                }
                send_err = Some(chain);
                break;
            }
        }
    }

    // 连接层失败 (重试耗尽) → 502.
    if let Some(chain) = send_err {
        report_breaker(&state.breakers, &provider_name, false);
        warn!("proxy: send() failed for {ep_clone}: {chain}");
        let model2 = model.clone();
        let p2 = provider_name.clone();
        let lb = state.log_buffer.clone();
        let s = start;
        let chain_clone = chain.clone();
        tokio::spawn(async move {
            crate::admin::record_request(&lb, &model2, &p2, &ep_clone, 502, s, body_len_val, Some(chain_clone)).await;
        });
        return Err(error_response(StatusCode::BAD_GATEWAY, &format!("upstream error: {chain}")));
    }

    // 9. 检查 upstream 响应
    let upstream = upstream.expect("upstream response must be present after send loop");
    let upstream_status = upstream.status().as_u16();

    if upstream_status >= 400 {
        let err_body = upstream.text().await.unwrap_or_default();
        warn!(
            "proxy: upstream {upstream_status} for {endpoint}: {err}",
            err = &err_body[..err_body.len().min(500)]
        );
        // 解析错误信息，添加中文说明
        let err_msg = format_upstream_error(upstream_status, &err_body);
        // 5xx 视为供应商故障 → 记失败 (可能触发熔断); 4xx (含 429) 视为
        // 客户端/配置问题, 供应商仍健康 → 记成功, 不熔断.
        report_breaker(&state.breakers, &provider_name, upstream_status < 500);
        let err_clone = err_msg.clone();
        let model2 = model.clone();
        let p2 = provider_name.clone();
        let ep2 = endpoint.clone();
        let lb = state.log_buffer.clone();
        tokio::spawn(async move {
            crate::admin::record_request(&lb, &model2, &p2, &ep2, upstream_status, start, body_len_val, Some(err_clone)).await;
        });
        return Err(error_response(
            StatusCode::from_u16(upstream_status).unwrap_or(StatusCode::BAD_GATEWAY),
            &err_msg,
        ));
    }

    // 10. 流式透传 — 通过 TokenTracker 记录 token 用量
    // 上游返回 2xx → 供应商健康, 记成功 (可能关闭熔断).
    report_breaker(&state.breakers, &provider_name, true);
    // 非流式且缓存开启 → 读全量响应体存入缓存后直接返回 (响应 JSON 自带 usage, 精确记 token).
    // 注意: text() 按值消费 upstream, 故此分支必须 return, 不可再进入下方流式透传.
    if let Some(key) = &cache_key {
        match upstream.text().await {
            Ok(body_text) => {
                state.cache.put(key, &body_text);
                let (pt, ct, hit, miss) = extract_usage(&body_text);
                crate::admin::record_request_with_tokens(
                    &state.log_buffer, &model, &provider_name, &endpoint, start, pt, ct, body_text.len(),
                    false, hit, miss,
                ).await;
                return Ok(axum::Json(
                    serde_json::from_str::<serde_json::Value>(&body_text).unwrap_or(serde_json::Value::Null),
                ).into_response());
            }
            Err(e) => {
                return Err(error_response(
                    StatusCode::BAD_GATEWAY,
                    &format!("failed to read upstream response body: {e}"),
                ));
            }
        }
    }
    let resp = stream_response_with_tokens(
        upstream,
        state.log_buffer.clone(),
        model,
        provider_name,
        endpoint,
        start,
        body_len_val,
        state.stream_idle_timeout,
    );
    Ok(resp)
}

/// 上游字节流的空闲超时包装.
///
/// 每收到一块即重置计时器; 若超过 `idle` 仍无下一块 (上游假死), 流以
/// `IdleError::Idle` 结束, 使代理及时释放连接, 而不是挂等整个请求超时.
struct IdleTimeoutStream<S> {
    inner: S,
    idle: Duration,
    sleep: Pin<Box<tokio::time::Sleep>>,
}

impl<S> IdleTimeoutStream<S> {
    /// 构造空闲超时包装流.
    pub fn new(inner: S, idle: Duration) -> Self {
        Self {
            inner,
            idle,
            sleep: Box::pin(tokio::time::sleep(idle)),
        }
    }
}

/// 空闲超时流的结束原因.
#[derive(Debug)]
enum IdleError<E> {
    /// 上游自身返回错误.
    Inner(E),
    /// 超过空闲阈值未收到数据.
    Idle,
}

impl<E: std::fmt::Display> std::fmt::Display for IdleError<E> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            IdleError::Inner(e) => write!(f, "upstream error: {e}"),
            IdleError::Idle => write!(f, "stream idle timeout (no data)"),
        }
    }
}

impl<E: std::error::Error + 'static> std::error::Error for IdleError<E> {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            IdleError::Inner(e) => Some(e),
            IdleError::Idle => None,
        }
    }
}

impl<S, E> Stream for IdleTimeoutStream<S>
where
    S: Stream<Item = Result<Bytes, E>> + Unpin,
    E: std::fmt::Debug + 'static,
{
    type Item = Result<Bytes, IdleError<E>>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_next(cx) {
            Poll::Ready(Some(Ok(b))) => {
                // 收到数据, 重置空闲计时器
                this.sleep
                    .as_mut()
                    .reset(tokio::time::Instant::now() + this.idle);
                Poll::Ready(Some(Ok(b)))
            }
            Poll::Ready(Some(Err(e))) => Poll::Ready(Some(Err(IdleError::Inner(e)))),
            Poll::Ready(None) => Poll::Ready(None),
            Poll::Pending => {
                if this.sleep.as_mut().poll(cx).is_ready() {
                    Poll::Ready(Some(Err(IdleError::Idle)))
                } else {
                    Poll::Pending
                }
            }
        }
    }
}

/// 对上游 SSE 流进行透传, 同时解析 usage 事件记录 token 数.
fn stream_response_with_tokens(
    upstream: reqwest::Response,
    log_buffer: LogBuffer,
    model: String,
    provider: String,
    endpoint: String,
    start: std::time::Instant,
    req_body_len: usize,
    idle: Duration,
) -> Response {
    let status = StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::OK);
    let headers = upstream.headers().clone();
    // 用空闲超时包装上游字节流, 防止上游假死长期占用连接.
    let inner_stream = IdleTimeoutStream::new(upstream.bytes_stream(), idle);

    let tracked = TokenStream {
        inner: inner_stream,
        done: false,
        tokens_pt: 0,
        tokens_ct: 0,
        tokens_cache_hit: 0,
        tokens_cache_miss: 0,
        response_bytes: 0,
        log_buffer: Some(TokenLogData {
            log_buffer,
            model,
            provider,
            endpoint,
            start,
            req_body_len,
            response_body_len: 0,
        }),
    };

    let body = Body::from_stream(tracked);
    let mut resp = Response::builder().status(status).body(body).unwrap();
    for (name, value) in headers.iter() {
        let name_str = name.as_str();
        if name_str != "content-length" && name_str != "transfer-encoding" {
            resp.headers_mut().append(name.clone(), value.clone());
        }
    }
    resp
}

/// 用于记录 token 用量的日志数据.
struct TokenLogData {
    log_buffer: LogBuffer,
    model: String,
    provider: String,
    endpoint: String,
    start: std::time::Instant,
    req_body_len: usize,
    response_body_len: usize,
}

/// 包装上游 SSE 字节流, 解析 `data: {...usage...}` 事件提取 token 数,
/// 流结束时自动写入请求日志.
///
/// 如果上游未发送 usage 事件 (常见于免费/开源模型), 则从响应 body
/// 字节数估算 completion_tokens (body_bytes / 4 作为近似值).
struct TokenStream<S> {
    inner: S,
    done: bool,
    tokens_pt: u32,
    tokens_ct: u32,
    /// 上游 KV Cache 命中 token 数 (usage.prompt_cache_hit_tokens).
    tokens_cache_hit: u32,
    /// 上游 KV Cache 未命中 token 数 (usage.prompt_cache_miss_tokens).
    tokens_cache_miss: u32,
    /// 累计 SSE 响应 body 总字节数 (用于无 usage 时的估算).
    response_bytes: usize,
    log_buffer: Option<TokenLogData>,
}

impl<S> TokenStream<S> {
    /// 解析单个 SSE chunk 中的 usage 事件.
    fn parse_sse_chunk(&mut self, data: &[u8]) {
        self.response_bytes += data.len();
        let Ok(text) = std::str::from_utf8(data) else { return };
        for line in text.lines() {
            let line = line.trim();
            let Some(json_str) = line.strip_prefix("data: ") else { continue };
            if json_str == "[DONE]" { continue; }
            let Ok(val) = serde_json::from_str::<serde_json::Value>(json_str) else { continue };
            // 从 usage 事件提取精确 token 数
            if let Some(usage) = val.get("usage") {
                if let Some(pt) = usage.get("prompt_tokens").and_then(|v| v.as_u64()) {
                    self.tokens_pt = pt as u32;
                }
                if let Some(ct) = usage.get("completion_tokens").and_then(|v| v.as_u64()) {
                    self.tokens_ct = ct as u32;
                }
                // 上游 KV Cache 命中/未命中统计 (DeepSeek 等)
                if let Some(hit) = usage.get("prompt_cache_hit_tokens").and_then(|v| v.as_u64()) {
                    self.tokens_cache_hit = hit as u32;
                }
                if let Some(miss) = usage.get("prompt_cache_miss_tokens").and_then(|v| v.as_u64()) {
                    self.tokens_cache_miss = miss as u32;
                }
            }
            // 部分供应商把 usage 放在 choices[0] 的内层
            if let Some(choices) = val.get("choices").and_then(|c| c.as_array()) {
                if let Some(choice) = choices.first() {
                    if let Some(inner_usage) = choice.get("usage") {
                        if let Some(pt) = inner_usage.get("prompt_tokens").and_then(|v| v.as_u64()) {
                            self.tokens_pt = pt as u32;
                        }
                        if let Some(ct) = inner_usage.get("completion_tokens").and_then(|v| v.as_u64()) {
                            self.tokens_ct = ct as u32;
                        }
                        if let Some(hit) = inner_usage.get("prompt_cache_hit_tokens").and_then(|v| v.as_u64()) {
                            self.tokens_cache_hit = hit as u32;
                        }
                        if let Some(miss) = inner_usage.get("prompt_cache_miss_tokens").and_then(|v| v.as_u64()) {
                            self.tokens_cache_miss = miss as u32;
                        }
                    }
                }
            }
        }
    }

    /// 流结束时计算最终 token 数: 优先使用上游返回的精确值, 否则估算.
    /// 返回 (prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens).
    fn final_tokens(&self, req_body_len: usize) -> (u32, u32, u32, u32) {
        let pt = if self.tokens_pt > 0 {
            self.tokens_pt
        } else {
            // 估算: 请求 body 字节 / 4 ≈ prompt token 数
            std::cmp::max(1, (req_body_len / 4) as u32)
        };
        let ct = if self.tokens_ct > 0 {
            self.tokens_ct
        } else {
            // 估算: 响应 body 字节 / 4 ≈ completion token 数
            std::cmp::max(1, (self.response_bytes / 4) as u32)
        };
        // 缓存命中/未命中: 上游未返回 usage 时无法估算, 保持解析到的精确值 (默认 0).
        (pt, ct, self.tokens_cache_hit, self.tokens_cache_miss)
    }
}

impl<S, E> Stream for TokenStream<S>
where
    S: Stream<Item = Result<Bytes, E>> + Unpin,
    E: std::fmt::Debug,
{
    type Item = Result<Bytes, E>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = &mut *self;
        loop {
            match Pin::new(&mut this.inner).poll_next(cx) {
                Poll::Ready(Some(Ok(chunk))) => {
                    this.parse_sse_chunk(&chunk);
                    return Poll::Ready(Some(Ok(chunk)));
                }
                Poll::Ready(Some(Err(e))) => {
                    // 上游错误或空闲超时: 结束流, 让客户端看到正常结束.
                    warn!("proxy: upstream stream ended (idle timeout or error): {e:?}");
                    this.done = true;
                    return Poll::Ready(None);
                }
                Poll::Ready(None) => {
                    if !this.done {
                        this.done = true;
                        let req_body_len = this.log_buffer.as_ref().map(|ld| ld.req_body_len).unwrap_or(0);
                        let (pt, ct, hit, miss) = this.final_tokens(req_body_len);
                        if let Some(mut ld) = this.log_buffer.take() {
                            ld.response_body_len = this.response_bytes;
                            tokio::spawn(async move {
                                crate::admin::record_request_with_tokens(
                                    &ld.log_buffer, &ld.model, &ld.provider, &ld.endpoint,
                                    ld.start, pt, ct, ld.response_body_len, false,
                                    hit, miss,
                                ).await;
                            });
                        }
                    }
                    return Poll::Ready(None);
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

/// 从请求 body 解析 model 字段.
fn parse_model(body: &[u8]) -> Option<String> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    v.get("model")?.as_str().map(|s| s.to_string())
}

/// 重试退避: 等待 `base * attempt` 毫秒 + 伪随机抖动 (0 ~ base/2).
///
/// 线性增长避免重试风暴, 抖动打散多客户端在同一瞬态故障时的齐发重试.
async fn retry_backoff(attempt: u32, base: Duration) {
    if base.is_zero() {
        return;
    }
    let base_ms = base.as_millis() as u64;
    let jitter = ((attempt.wrapping_mul(0x9E37_79B1) % 100) as u64) * base_ms / 200; // 0 ~ base/2
    let wait = base_ms.saturating_mul(attempt as u64) + jitter;
    tokio::time::sleep(Duration::from_millis(wait)).await;
}

/// 从已完成 (非流式) 的响应 JSON 中提取 token 用量, 用于日志统计.
///
/// 返回 (prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens).
/// 后两者来自上游 `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
/// (DeepSeek 等上游的 KV Cache / 上下文硬盘缓存命中统计), 未提供时为 0.
fn extract_usage(text: &str) -> (u32, u32, u32, u32) {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(text) else {
        return (0, 0, 0, 0);
    };
    let mut pt = 0u32;
    let mut ct = 0u32;
    let mut hit = 0u32;
    let mut miss = 0u32;
    if let Some(u) = v.get("usage") {
        pt = u.get("prompt_tokens").and_then(|x| x.as_u64()).unwrap_or(0) as u32;
        ct = u
            .get("completion_tokens")
            .and_then(|x| x.as_u64())
            .unwrap_or(0) as u32;
        hit = u
            .get("prompt_cache_hit_tokens")
            .and_then(|x| x.as_u64())
            .unwrap_or(0) as u32;
        miss = u
            .get("prompt_cache_miss_tokens")
            .and_then(|x| x.as_u64())
            .unwrap_or(0) as u32;
    }
    (pt, ct, hit, miss)
}

/// 注入模型级参数到请求 body.
///
/// - upstream_model: 替换 body 中的 model 字段为上游真实模型名.
/// - reasoning_effort: 配置档仅作"客户端无指示时的默认". 客户端显式关闭思考
///   (`thinking:false`) 或已自带档位时不注入/不覆盖, 客户端档位优先.
/// - extra_body: 逐字段注入, 不覆盖已有字段.
fn inject_model_params(
    bytes: bytes::Bytes,
    model_cfg: &crate::providers::ModelConfig,
) -> (bytes::Bytes, bool) {
    let Ok(mut v) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return (bytes, false); // 解析失败, 原样返回
    };
    let had_effort = v.get("reasoning_effort").is_some();

    // 思考参数规范化: 客户端 thinking:true → reasoning_effort 等 (见 thinking.rs).
    // 返回客户端是否显式关闭思考 (thinking:false), 用于跳过配置档兜底注入.
    let explicitly_disabled = crate::thinking::normalize_thinking(&mut v, model_cfg);

    // 没有任何需要注入的参数 → 已含规范化结果, 序列化返回.
    let has_remap = model_cfg.upstream_model.is_some();
    let has_effort = model_cfg.reasoning_effort.is_some();
    let has_extra = model_cfg
        .extra_body
        .as_ref()
        .map(|v| v.is_object())
        .unwrap_or(false);
    if !has_remap && !has_effort && !has_extra {
        return (serde_json::to_vec(&v).unwrap_or_else(|_| bytes.to_vec()).into(), false);
    };

    // 替换 model 为上游真实模型名
    // 注意: 需先将 client_model 拥有化 (to_string), 否则 v.get 的不可变借用
    // 会与下面 v["model"] 的可变借用冲突.
    if let Some(real) = &model_cfg.upstream_model {
        if let Some(client_model) = v
            .get("model")
            .and_then(|m| m.as_str())
            .map(|s| s.to_string())
        {
            if client_model != *real {
                v["model"] = serde_json::Value::String(real.clone());
                info!("proxy: remapped model '{client_model}' -> '{real}'");
            }
        }
    }

    // 注入 reasoning_effort: 配置档仅作"客户端无指示时的默认", 客户端档位优先.
    // 客户端显式关闭思考 (thinking:false) 时不注入, 尊重其关思考意图.
    if let Some(effort) = &model_cfg.reasoning_effort {
        if !explicitly_disabled && v.get("reasoning_effort").is_none() {
            v["reasoning_effort"] = serde_json::Value::String(effort.clone());
            info!("proxy: injected reasoning_effort={effort}");
        }
    }

    // 注入 extra_body (逐字段, 不覆盖)
    if let Some(serde_json::Value::Object(extra)) = &model_cfg.extra_body {
        let obj = v.as_object_mut().expect("body must be a JSON object");
        for (k, val) in extra {
            if !obj.contains_key(k) {
                obj.insert(k.clone(), val.clone());
            }
        }
    }

    let injected = v.get("reasoning_effort").is_some() && !had_effort;
    (serde_json::to_vec(&v).unwrap_or_else(|_| bytes.to_vec()).into(), injected)
}

/// 过滤客户端 headers: 白名单模式, 只转发安全的标准 headers.
///
/// 客户端 (如 CodeBuddy) 会发送大量非标准 headers (x-ide-type, x-domain 等),
/// 这些 headers 会导致 upstream 识别客户端来源并拒绝服务 (500).
/// 因此只放行 client 不传的 headers, 由 bridge 自行设置 authorization / content-type / accept.
fn filter_headers(headers: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (name, value) in headers.iter() {
        let name_str = name.as_str();
        // 只放行 user-agent (upstream 可能用于日志统计)
        match name_str {
            "user-agent" => {}
            _ => continue,
        }
        out.append(name.clone(), value.clone());
    }
    out
}

/// 构造 JSON 错误响应 (OpenAI 兼容格式).
fn error_response(status: StatusCode, message: &str) -> Response {
    let body = serde_json::json!({
        "error": { "message": message, "type": "bridge_error" }
    });
    (status, axum::Json(body)).into_response()
}

/// 格式化完整错误链 — 遍历 std::error::Error::source(), 打印每一层原因.
///
/// reqwest 的 Display 只显示顶层消息 (如 "error sending request for url (...)"),
/// 底层原因 (DNS 失败 / TCP 重置 / TLS 超时等) 藏在 source chain 里, 需手动遍历.
fn format_error_chain(e: &dyn std::error::Error) -> String {
    let mut msg = format!("{e}");
    let mut current = e.source();
    while let Some(cause) = current {
        msg.push_str(&format!(" -> {cause}"));
        current = cause.source();
    }
    msg
}

/// 根据 HTTP 状态码返回中文说明 (覆盖纯文本响应体场景, 如上游 408 超时).
///
/// type 映射 (见 `format_upstream_error`) 更具体, 优先于此处; 此处作为兜底,
/// 让任何状态码 (即使上游返回纯文本而非 JSON) 都能给出可读的中文提示.
fn status_explanation(status: u16) -> &'static str {
    match status {
        400 => "请求参数错误（模型不支持、参数缺失或格式错误）",
        401 => "认证失败（API Key 无效或已过期）",
        402 => "账户需付费（余额不足或需订阅）",
        403 => "权限不足（无权访问该模型或功能）",
        404 => "资源不存在（端点或模型名错误）",
        408 => "请求超时（上游处理超时，可能是模型思考时间长或供应商繁忙）",
        409 => "请求冲突（并发或状态不一致）",
        413 => "请求体过大（上下文或文件超出上限）",
        429 => "请求频率超限（请稍后重试）",
        500 => "服务器内部错误（供应商服务异常）",
        502 => "网关错误（上游网关不可用）",
        503 => "服务不可用（供应商过载或维护中）",
        504 => "网关超时（上游处理超时未响应）",
        _ => "",
    }
}

/// 格式化上游错误信息: 解析 JSON 错误响应，添加中文说明.
///
/// 优先级: error.type 中文 > HTTP 状态码中文 > 原始信息.
/// 这样即使上游返回纯文本 (如 `request timeout (HTTP Status: 408)`) 也能给中文说明.
///
/// 输入: upstream_status (如 400/408), err_body (原始响应体).
/// 输出: "400 请求参数错误 [invalid_request_error]: Error from provider..."
///       或 "408 请求超时（上游处理超时...）: request timeout (HTTP Status: 408)"
fn format_upstream_error(status: u16, err_body: &str) -> String {
    // 兜底: 状态码中文说明 (即使纯文本响应体也生效)
    let status_expl = status_explanation(status);

    // 尝试解析 JSON 错误响应
    if let Ok(err_json) = serde_json::from_str::<serde_json::Value>(err_body) {
        if let Some(error) = err_json.get("error") {
            let msg = error.get("message").and_then(|m| m.as_str()).unwrap_or("");
            let err_type = error.get("type").and_then(|t| t.as_str()).unwrap_or("");
            let code = error.get("code").and_then(|c| c.as_str()).unwrap_or("");

            // 根据错误类型添加中文说明 (比状态码更具体, 优先)
            let type_expl = match err_type {
                "invalid_request_error" => "请求参数错误（可能是模型不支持、参数缺失或格式错误）",
                "authentication_error" => "认证失败（API Key 无效或已过期）",
                "rate_limit_error" => "请求频率超限（请稍后重试）",
                "server_error" => "服务器内部错误（供应商服务异常）",
                "context_length_exceeded" => "上下文长度超限（请求内容过长）",
                "insufficient_quota" => "配额不足（账户余额耗尽）",
                "permission_denied" => "权限不足（无权访问该模型或功能）",
                _ => "",
            };

            // 构造格式化错误信息: type 中文优先, 其次 code, 其次状态码中文
            if !type_expl.is_empty() {
                format!("{status} {type_expl} [{err_type}]: {msg}")
            } else if !code.is_empty() {
                format!("{status} [{code}]: {msg}")
            } else if !err_type.is_empty() {
                format!("{status} [{err_type}]: {msg}")
            } else if !status_expl.is_empty() {
                format!("{status} {status_expl}: {msg}")
            } else {
                format!("{status}: {msg}")
            }
        } else if !status_expl.is_empty() {
            // JSON 但没有 error 字段, 用状态码中文兜底
            format!("{status} {status_expl}: {err_body}")
        } else {
            // JSON 但没有 error 字段，直接返回原始信息
            format!("{status}: {err_body}")
        }
    } else if !status_expl.is_empty() {
        // 非 JSON 格式, 但有状态码中文 → 给出中文说明 + 原文
        format!("{status} {status_expl}: {err_body}")
    } else {
        // 非 JSON 格式且无状态码说明, 直接返回原始信息
        format!("{status}: {err_body}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::ModelConfig;

    fn cfg_with_effort(effort: &str) -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: Some(effort.to_string()),
            extra_body: None,
        }
    }

    fn cfg_no_effort() -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: None,
            extra_body: None,
        }
    }

    fn parse(out: bytes::Bytes) -> serde_json::Value {
        serde_json::from_slice(&out).unwrap()
    }

    /// 方向 B: 客户端显式 thinking:false → 即使配置档有 max, 也不注入 reasoning_effort.
    #[test]
    fn thinking_false_suppresses_config_effort() {
        let cfg = cfg_with_effort("max");
        let body = serde_json::json!({ "model": "x", "thinking": false }).to_string();
        let (out, _) = inject_model_params(bytes::Bytes::from(body), &cfg);
        let v = parse(out);
        assert!(v.get("thinking").is_none());
        assert!(v.get("reasoning_effort").is_none());
    }

    /// 客户端未提思考 → 注入配置档默认 (max).
    #[test]
    fn no_thinking_injects_config_default() {
        let cfg = cfg_with_effort("max");
        let body = serde_json::json!({ "model": "x" }).to_string();
        let (out, _) = inject_model_params(bytes::Bytes::from(body), &cfg);
        let v = parse(out);
        assert_eq!(v["reasoning_effort"], "max");
    }

    /// 客户端自带 reasoning_effort → 优先, 不被配置档覆盖.
    #[test]
    fn client_effort_wins_over_config() {
        let cfg = cfg_with_effort("max");
        let body = serde_json::json!({ "model": "x", "reasoning_effort": "low" }).to_string();
        let (out, _) = inject_model_params(bytes::Bytes::from(body), &cfg);
        let v = parse(out);
        assert_eq!(v["reasoning_effort"], "low");
    }

    /// 配置档无 effort 且客户端无指示 → 透传, 不注入 (上游用自己的默认 high).
    #[test]
    fn no_config_no_client_stays_clean() {
        let cfg = cfg_no_effort();
        let body = serde_json::json!({ "model": "x" }).to_string();
        let (out, _) = inject_model_params(bytes::Bytes::from(body), &cfg);
        let v = parse(out);
        assert!(v.get("reasoning_effort").is_none());
    }

    /// 测试 format_upstream_error 函数.
    #[test]
    fn test_format_upstream_error() {
        // 测试 1: invalid_request_error - 应包含中文说明
        let err_body = r#"{"error":{"message":"Error from provider (Console): Upstream request failed","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}"#;
        let result = format_upstream_error(400, err_body);
        assert!(result.contains("400"));
        assert!(result.contains("请求参数错误"));
        assert!(result.contains("invalid_request_error"));
        assert!(result.contains("Error from provider"));

        // 测试 2: authentication_error - 应包含认证失败说明
        let err_body = r#"{"error":{"message":"Invalid API key","type":"authentication_error"}}"#;
        let result = format_upstream_error(401, err_body);
        assert!(result.contains("401"));
        assert!(result.contains("认证失败"));
        assert!(result.contains("Invalid API key"));

        // 测试 3: rate_limit_error - 应包含频率限制说明
        let err_body = r#"{"error":{"message":"Rate limit exceeded","type":"rate_limit_error"}}"#;
        let result = format_upstream_error(429, err_body);
        assert!(result.contains("429"));
        assert!(result.contains("请求频率超限"));
        assert!(result.contains("Rate limit exceeded"));

        // 测试 4: 非 JSON 格式 + 状态码有说明 - 应基于状态码给中文说明
        let err_body = "plain text error";
        let result = format_upstream_error(500, err_body);
        assert_eq!(result, "500 服务器内部错误（供应商服务异常）: plain text error");

        // 测试 5: JSON 但没有 error 字段 - 应返回原始信息
        let err_body = r#"{"message":"some error"}"#;
        let result = format_upstream_error(400, err_body);
        assert!(result.contains("400"));
        assert!(result.contains(err_body));

        // 测试 6: 408 纯文本响应体 (上游网关超时) - 应基于状态码给中文说明
        let err_body = "request timeout (HTTP Status: 408)";
        let result = format_upstream_error(408, err_body);
        assert!(result.contains("408"));
        assert!(result.contains("请求超时"));
        assert!(result.contains(err_body));

        // 测试 7: 503 纯文本 - 应基于状态码给中文说明
        let err_body = "Service Temporarily Unavailable";
        let result = format_upstream_error(503, err_body);
        assert!(result.contains("503"));
        assert!(result.contains("服务不可用"));
        assert!(result.contains(err_body));

        // 测试 8: type 映射优先于 status 映射 (400 + invalid_request_error 仍用 type 中文)
        let err_body = r#"{"error":{"message":"bad","type":"invalid_request_error"}}"#;
        let result = format_upstream_error(400, err_body);
        assert!(result.contains("请求参数错误"));
        assert!(!result.contains("请求参数错误（模型不支持、参数缺失或格式错误）: 请求参数错误")); // 不重复
    }

    /// 非流式: extract_usage 提取上游 KV Cache 命中/未命中 token.
    #[test]
    fn test_extract_usage_cache_fields() {
        // 含 cache 字段 → 完整提取 4 元组
        let body = r#"{"usage":{"prompt_tokens":10,"completion_tokens":20,"prompt_cache_hit_tokens":100,"prompt_cache_miss_tokens":5}}"#;
        assert_eq!(extract_usage(body), (10, 20, 100, 5));

        // 无 cache 字段 → 命中/未命中记 0
        let body = r#"{"usage":{"prompt_tokens":7,"completion_tokens":3}}"#;
        assert_eq!(extract_usage(body), (7, 3, 0, 0));

        // 非 JSON → 全部为 0
        assert_eq!(extract_usage("not json"), (0, 0, 0, 0));
    }

    /// 流式: parse_sse_chunk 解析 SSE 事件中的 cache 命中/未命中 (外层 usage 与 choices[0].usage 内层).
    #[test]
    fn test_parse_sse_chunk_cache_fields() {
        use futures::stream;
        let mut ts = TokenStream {
            inner: stream::empty::<Result<Bytes, std::io::Error>>(),
            done: false,
            tokens_pt: 0,
            tokens_ct: 0,
            tokens_cache_hit: 0,
            tokens_cache_miss: 0,
            response_bytes: 0,
            log_buffer: None,
        };
        // SSE 外层 usage
        ts.parse_sse_chunk(
            b"data: {\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":2,\"prompt_cache_hit_tokens\":50,\"prompt_cache_miss_tokens\":3}}\n\n",
        );
        assert_eq!(ts.tokens_pt, 1);
        assert_eq!(ts.tokens_ct, 2);
        assert_eq!(ts.tokens_cache_hit, 50);
        assert_eq!(ts.tokens_cache_miss, 3);

        // choices[0].usage 内层 (部分供应商把 usage 放在此处)
        ts.parse_sse_chunk(
            b"data: {\"choices\":[{\"usage\":{\"prompt_cache_hit_tokens\":7,\"prompt_cache_miss_tokens\":1}}]}\n\n",
        );
        assert_eq!(ts.tokens_cache_hit, 7);
        assert_eq!(ts.tokens_cache_miss, 1);
    }
}

