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
use crate::loop_guard::LoopDetector;
use crate::config::LoopGuardConfig;

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
    /// 模型死循环检测配置 (用于构造流式检测器, 默认开启).
    pub loop_guard: LoopGuardConfig,
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
                true, hit, miss, None,
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
                    false, hit, miss, None,
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
        state.loop_guard.clone(),
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
    loop_cfg: LoopGuardConfig,
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
        loop_guard: if loop_cfg.enabled {
            Some(LoopDetector::new(loop_cfg.window, loop_cfg.min_repeat, loop_cfg.max_buffer))
        } else {
            None
        },
        loop_aborted: false,
        error_closed: false,
        stream_errored: false,
        pending_error_sse: None,
        finish_reason: None,
        clean_finish: false,
        errored_msg: None,
        out_buf: Vec::new(),
        line_buf: Vec::new(),
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
    /// 上游 KV Cache 命中 token 数 (兼容 DeepSeek 扁平 / OpenAI 嵌套 schema).
    tokens_cache_hit: u32,
    /// 上游 KV Cache 未命中 token 数 (兼容 DeepSeek 扁平 / OpenAI 嵌套 schema).
    tokens_cache_miss: u32,
    /// 累计 SSE 响应 body 总字节数 (用于无 usage 时的估算).
    response_bytes: usize,
    /// 模型死循环检测器 (None 表示功能关闭, 不检测).
    loop_guard: Option<LoopDetector>,
    /// 已因死循环截断并发送 [DONE] 的标志, 防止重复触发.
    loop_aborted: bool,
    /// 已因上游错误/空闲超时而兜底结束 (已发 [DONE]); 防止下次 poll 重复发/继续 poll 上游.
    error_closed: bool,
    /// 流内检测到上游 SSE 错误事件 (HTTP 200 + error 事件), 等待改写成中文.
    stream_errored: bool,
    /// 改写后的中文 SSE 错误事件 JSON (不含 "data: " 前缀), 待转发一次.
    pending_error_sse: Option<String>,
    /// 上游在流式 chunk 中带的非正常 finish_reason (error/length/content_filter 等).
    /// 用于诊断"完成原因错误" (10004): AIGate 此前纯透传、无感知, 客户端据此报错.
    finish_reason: Option<String>,
    /// 流是否"干净结束": 上游发送过 finish_reason (任意值) 或 [DONE] 终止帧则为 true.
    /// 用于补全"请求记录是否报错"的判断: 若上游连接正常关闭 (None 分支) 却从未见
    /// finish_reason/[DONE], 说明响应被截断 (典型如 10014 的 mid-object split 丢尾帧),
    /// 此前会被记成 200 成功 → 记录页不显示报错. 见 poll_next None 分支.
    clean_finish: bool,
    /// 上游 SSE error 事件改写后的中文错误信息 (供完成时写错误日志).
    errored_msg: Option<String>,
    /// 归一化后的 SSE 输出缓冲: 把上游 payload (data: 前缀 / 裸 JSON / 裸 [DONE]) 统一封装为
    /// 标准 "data: {json}" 帧再转发客户端 (替代原始裸字节透传, 修复 zen 裸 JSON 被客户端拒收).
    out_buf: Vec<u8>,
    /// 跨 chunk 的半行缓冲: 上游裸 JSON 常被 TCP 切分在 chunk 边界 (mid-object split),
    /// 累积到完整行 (\n 结尾) 再解析, 否则半个 JSON 被当裸 JSON 解析失败 → 整段丢失 → 10014.
    line_buf: Vec<u8>,
    log_buffer: Option<TokenLogData>,
}

impl<S> TokenStream<S> {
    /// 解析一个上游 chunk: 累积到行缓冲后处理完整行 (供 poll_next 与测试调用).
    /// 跨 chunk 的半行由 line_buf 续拼, 修复 mid-object split 导致的 10014.
    fn parse_sse_chunk(&mut self, data: &[u8]) {
        self.response_bytes += data.len();
        self.line_buf.extend_from_slice(data);
        self.drain_lines();
    }

    /// 把上游 chunk 字节累积进 line_buf, 仅处理以 \n 结尾的"完整行"; 半行 (mid-object split) 留在
    /// 缓冲等下一 chunk 续拼. 这是修复 10014 ("响应数据无效") 的核心: 上游 zen 的裸 JSON 常被 TCP
    /// 切分在 chunk 边界, 旧版逐 chunk 行式解析把半个 JSON 当裸 JSON 解析失败 → 整段丢失 → 残缺响应.
    fn drain_lines(&mut self) {
        // 防御: 异常长的未终结行直接丢弃, 防止内存无限增长 (正常 SSE 每行远小于此).
        if self.line_buf.len() > 16 * 1024 * 1024 {
            warn!("proxy: SSE line buffer overflow (>16MiB without newline), dropping partial line");
            self.line_buf.clear();
        }
        while let Some(pos) = self.line_buf.iter().position(|&b| b == b'\n') {
            let raw: Vec<u8> = self.line_buf[..pos].to_vec();
            self.line_buf.drain(..=pos);
            let Ok(text) = std::str::from_utf8(&raw) else {
                // 非 UTF-8 的半行: 丢弃 (极罕见, 正常 SSE 均为 UTF-8).
                continue;
            };
            let line = text.trim();
            if line.is_empty() {
                continue;
            }
            self.process_line(line);
        }
    }

    /// 解析并归一化单行 SSE payload (data: 前缀 / 裸 JSON / 裸 [DONE]), 写入 out_buf 待转发.
    /// 调用前该行必须已是完整行 (由 drain_lines 保证 \n 结尾).
    fn process_line(&mut self, line: &str) {
        if line.is_empty() {
            return;
        }
        // 提取 SSE payload: 兼容三种上游格式
        //  1) 标准 "data: {…}" / "data:{…}" (带或不带空格)
        //  2) 裸 JSON chunk (NDJSON 风格): zen/opencode.ai 会把部分 chunk
        //     (含 content / finish_reason 终止帧) 直接以 {"id":…,"object":"chat.completion.chunk",…}
        //     发送, 不带 "data: " 前缀. 此前被当成 unparseable 整行丢弃 → 客户端收不到
        //     完整内容/完成原因 → "完成原因错误" (10004).
        //  3) 裸 "[DONE]" (无 data: 前缀的结束标记)
        let json_str: &str = if let Some(rest) = line.strip_prefix("data:") {
            rest.trim_start()
        } else if line == "[DONE]" {
            "[DONE]"
        } else if line.starts_with('{') {
            line
        } else {
            // 真正的 SSE 非数据行 (: 注释 / event: / id: / retry: 等) 忽略.
            return;
        };
        if json_str == "[DONE]" {
            // 上游显式发送终止帧: 标记流为干净结束 (否则 None 分支会误判为截断).
            self.clean_finish = true;
            // 归一化发射结束帧 (兼容裸 [DONE] 与 data: [DONE] 两种形式).
            self.out_buf.extend_from_slice(b"data: [DONE]\n\n");
            return;
        }
        let Ok(val) = serde_json::from_str::<serde_json::Value>(json_str) else {
            // 调试日志: 记录网关无法解析的 SSE 行 (客户端严格 JSON.parse 会因此报错).
            // 经跨 chunk 行缓冲后, 此处仅剩真正畸形的 JSON (不再是 mid-object 分片).
            let head: String = json_str.chars().take(200).collect();
            warn!(
                "proxy: unparseable SSE payload (len={}, head={head:?})",
                json_str.len()
            );
            return;
        };
        // 检测上游以 SSE 错误事件形式返回的错误 (HTTP 200 + error 事件):
        // 翻译为中文并改写, 否则正常 JSON 数据继续走下方 usage / delta 解析.
        if let Some((msg, etype)) = translate_sse_error(&val) {
            // 诊断: 上游以 SSE error 事件 (HTTP 200 + error) 返回错误. 此前静默透传且不写日志,
            // 请求会从 logs.jsonl 凭空消失 (正是 10004 在日志里查不到的根因之一).
            warn!(
                "proxy: upstream SSE error event (model={}, provider={}): {msg} [{etype}]",
                self.log_buffer.as_ref().map(|ld| ld.model.as_str()).unwrap_or("?"),
                self.log_buffer.as_ref().map(|ld| ld.provider.as_str()).unwrap_or("?"),
            );
            self.stream_errored = true;
            self.errored_msg = Some(format!("{msg} [{etype}]"));
            let ev = serde_json::json!({ "error": { "message": msg, "type": etype } });
            self.pending_error_sse = Some(serde_json::to_string(&ev).unwrap_or_default());
            return;
        }
        // 从 usage 事件提取精确 token 数
        if let Some(usage) = val.get("usage") {
            if let Some(pt) = usage.get("prompt_tokens").and_then(|v| v.as_u64()) {
                self.tokens_pt = pt as u32;
            }
            if let Some(ct) = usage.get("completion_tokens").and_then(|v| v.as_u64()) {
                self.tokens_ct = ct as u32;
            }
            // 上游 KV Cache 命中/未命中 (兼容 DeepSeek 扁平 schema 与 OpenAI 嵌套 schema)
            let (hit, miss) = usage_cache(usage);
            if hit > 0 { self.tokens_cache_hit = hit; }
            if miss > 0 { self.tokens_cache_miss = miss; }
        }
        // 部分供应商把 usage 放在 choices[0] 的内层; 同时取 delta 文本喂死循环检测器.
        if let Some(choices) = val.get("choices").and_then(|c| c.as_array()) {
            if let Some(choice) = choices.first() {
                if let Some(inner_usage) = choice.get("usage") {
                    if let Some(pt) = inner_usage.get("prompt_tokens").and_then(|v| v.as_u64()) {
                        self.tokens_pt = pt as u32;
                    }
                    if let Some(ct) = inner_usage.get("completion_tokens").and_then(|v| v.as_u64()) {
                        self.tokens_ct = ct as u32;
                    }
                    // 上游 KV Cache 命中/未命中 (兼容两种 schema)
                    let (hit, miss) = usage_cache(inner_usage);
                    if hit > 0 { self.tokens_cache_hit = hit; }
                    if miss > 0 { self.tokens_cache_miss = miss; }
                }
                // 取增量文本喂给死循环检测器 (content / reasoning_content / reasoning).
                if let Some(delta) = choice.get("delta") {
                    for key in ["content", "reasoning_content", "reasoning"] {
                        if let Some(s) = delta.get(key).and_then(|v| v.as_str()) {
                            if let Some(detector) = self.loop_guard.as_mut() {
                                detector.feed(s);
                            }
                        }
                    }
                }
                // 诊断: 捕获非正常的 finish_reason (error/length/content_filter 等).
                // 上游带 finish_reason="error" 时 AIGate 此前纯透传、无感知,
                // 客户端据此报 "完成原因错误" (10004). warn 到 aigate.log 以便定位.
                if let Some(fr) = choice.get("finish_reason").and_then(|v| v.as_str()) {
                    // 无论 finish_reason 取值, 只要上游发出了终止帧就标记干净结束.
                    self.clean_finish = true;
                    if !matches!(fr, "stop" | "tool_calls" | "function_call") {
                        warn!(
                            "proxy: upstream finish_reason={fr:?} (model={}, provider={}); client may report completion error (10004)",
                            self.log_buffer.as_ref().map(|ld| ld.model.as_str()).unwrap_or("?"),
                            self.log_buffer.as_ref().map(|ld| ld.provider.as_str()).unwrap_or("?"),
                        );
                        self.finish_reason = Some(fr.to_string());
                    }
                }
            }
        }
        // 归一化发射: 把解析到的 payload 以标准 "data: {json}" SSE 帧重新封装转发.
        // 每个成功解析的 payload 都转发 (无论是否含 choices), 保证客户端收到完整流;
        // 上游 (zen/opencode.ai) 的裸 JSON chunk 经此补上 data: 前缀, 客户端即可正常解析.
        self.out_buf.extend_from_slice(b"data: ");
        self.out_buf.extend_from_slice(json_str.as_bytes());
        self.out_buf.extend_from_slice(b"\n\n");
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
        // 已因上游错误/空闲超时兜底结束: 上次已发 [DONE], 本次直接收尾.
        if this.error_closed {
            return Poll::Ready(None);
        }
        loop {
            match Pin::new(&mut this.inner).poll_next(cx) {
            Poll::Ready(Some(Ok(chunk))) => {
                this.parse_sse_chunk(&chunk);
                // 流内错误事件 (HTTP 200 + SSE error): 改写成中文 error 事件并干净结束流.
                // 因 HTTP 头已发出 (200), 无法改为 JSON 错误响应, 只能以规范的 SSE
                // error 事件呈现, 客户端 (OpenAI 兼容) 会按规范展示中文错误.
                if this.stream_errored {
                    // 先 flush 错误事件之前的归一化帧, 避免丢失已解析的内容.
                    if !this.out_buf.is_empty() {
                        let out = std::mem::take(&mut this.out_buf);
                        return Poll::Ready(Some(Ok(Bytes::from(out))));
                    }
                    if let Some(translated) = this.pending_error_sse.take() {
                        return Poll::Ready(Some(Ok(Bytes::from(format!("data: {translated}\n\n")))));
                    }
                    // 翻译后的 error 事件已转发, 直接终止流 (丢弃上游剩余数据).
                    // Fix B2: 写一条带 error 标记的完成日志, 否则该请求会从请求日志中凭空消失
                    // (与旧 Err 分支的盲区一致; 正是 10004 在 logs.jsonl 看不到的原因).
                    if let Some(mut ld) = this.log_buffer.take() {
                        ld.response_body_len = this.response_bytes;
                        let err_msg = this.errored_msg.clone()
                            .or_else(|| this.finish_reason.clone().map(|f| format!("finish_reason={f}")));
                        tokio::spawn(async move {
                            crate::admin::record_request(
                                &ld.log_buffer, &ld.model, &ld.provider, &ld.endpoint,
                                200, ld.start, ld.response_body_len, err_msg,
                            ).await;
                        });
                    }
                    return Poll::Ready(None);
                }
                // 下游模型陷入死循环 → 立即截断流, 干净地发 [DONE] 结束.
                    // 不向客户端正文塞说明 (说明仅写运行日志, 满足"不污染正文").
                    if this.loop_guard.as_ref().map(|g| g.triggered()).unwrap_or(false) {
                        // 先 flush 已解析的归一化帧, 避免截断时丢失内容.
                        if !this.out_buf.is_empty() {
                            let out = std::mem::take(&mut this.out_buf);
                            return Poll::Ready(Some(Ok(Bytes::from(out))));
                        }
                        if !this.loop_aborted {
                            this.loop_aborted = true;
                            warn!(
                                "proxy: model loop detected, truncating stream (model={}, provider={}); loop note logged only",
                                this.log_buffer.as_ref().map(|ld| ld.model.as_str()).unwrap_or("?"),
                                this.log_buffer.as_ref().map(|ld| ld.provider.as_str()).unwrap_or("?"),
                            );
                            // 收尾帧必须带合法 finish_reason: 否则 OpenAI 兼容客户端 (IDE) 因末帧缺
                            // finish_reason 报 "完成原因错误" (10004). 用 "length" 语义=被截断的正常
                            // 完成, 客户端当作正常收尾不报错; 空 delta 不污染正文. 若某客户端仍 10004,
                            // 改 "stop" (最稳但把截断伪装成自然结束). 注意: 此处不置 clean_finish,
                            // 使网关记录仍按"截断"标 error, 与客户端正常收尾互不干扰.
                            let term = "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"length\"}]}\n\n\
                                        data: [DONE]\n\n";
                            return Poll::Ready(Some(Ok(Bytes::from(term.as_bytes().to_vec()))));
                        }
                        // 已发过 [DONE], 终止流 (收尾日志由下方 None 分支写入).
                        return Poll::Ready(None);
                    }
                    // 返回归一化后的 SSE 帧 (替代原始裸字节透传): 每条上游行 → 恰好一帧, 不会重复.
                    // 若本 chunk 无有效帧 (全是注释/空行), 继续取下一 chunk 避免发送空字节.
                    if this.out_buf.is_empty() {
                        continue;
                    }
                    let out = std::mem::take(&mut this.out_buf);
                    return Poll::Ready(Some(Ok(Bytes::from(out))));
                }
                Poll::Ready(Some(Err(e))) => {
                    // 上游错误或空闲超时:
                    //  - Fix A: 兜底补发终止帧让客户端优雅收尾, 避免卡在半截帧导致 "JSON 解析错误".
                    //    与 LoopGuard 同一哲学: 先发一帧带合法 finish_reason 的终止帧, 再 [DONE].
                    //    此处用 "stop" 而非 "length": 上游是意外断连/超时 (非我们主动截断),
                    //    "stop" 语义=正常结束, 所有 OpenAI 兼容客户端都当正常收尾、不会报 10004;
                    //    空 delta 不污染已转发的正文. 网关侧刻意不置 clean_finish → 记录页仍按
                    //    "连接意外结束" 标红 (Fix B), 与客户端正常收尾互不干扰.
                    //  - Fix B: 写一条带 error 标记的完成日志, 否则该请求会从请求日志中凭空消失,
                    //    无法对账 (正是"客户端报错但网关日志看不到该请求"的根因之一).
                    warn!("proxy: upstream stream ended (idle timeout or error): {e:?}");
                    this.done = true;
                    this.error_closed = true;
                    if let Some(mut ld) = this.log_buffer.take() {
                        ld.response_body_len = this.response_bytes;
                        // 先把错误信息格式化为 String (Send) 再进入 spawn, 否则 E 不 Send 会让 future 无法跨线程.
                        let err_msg = format!("{e:?}");
                        tokio::spawn(async move {
                            crate::admin::record_request(
                                &ld.log_buffer, &ld.model, &ld.provider, &ld.endpoint,
                                200, ld.start, ld.response_body_len, Some(err_msg),
                            ).await;
                        });
                    }
                    let term = "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n\
                                data: [DONE]\n\n";
                    return Poll::Ready(Some(Ok(Bytes::from(term.as_bytes().to_vec()))));
                }
                Poll::Ready(None) => {
                    // 流正常结束: 处理 line_buf 中可能残留的最后一个未以 \n 结尾的对象
                    // (mid-object split 的尾段被拆到最后一个 chunk). 否则该对象会丢失 → 10014.
                    if !this.line_buf.is_empty() {
                        let raw = std::mem::take(&mut this.line_buf);
                        if let Ok(text) = std::str::from_utf8(&raw) {
                            let line = text.trim();
                            if !line.is_empty() {
                                this.process_line(line);
                            }
                        }
                    }
                    // 若 flush 后又有归一化帧, 先发出去; 下次 None 再写收尾日志 (done 保证只写一次).
                    if !this.out_buf.is_empty() {
                        let out = std::mem::take(&mut this.out_buf);
                        return Poll::Ready(Some(Ok(Bytes::from(out))));
                    }
                    if !this.done {
                        this.done = true;
                        let req_body_len = this.log_buffer.as_ref().map(|ld| ld.req_body_len).unwrap_or(0);
                        let (pt, ct, hit, miss) = this.final_tokens(req_body_len);
                        // 完成日志的 error 判定:
                        //  - 流从未干净结束 (clean_finish==false): 上游连接关了却没发 finish_reason/[DONE],
                        //    响应被截断 (典型 10014 mid-object split 丢尾帧) → 记为 error, 否则记录页会显示 200 成功.
                        //  - 流干净结束但 finish_reason=="error": 记为 error (10004 类).
                        // 必须在 async 块外计算 (this 是 &mut, 不能跨线程), 仅捕获拥有的 String 进 spawn.
                        let err_for_log: Option<String> = if !this.clean_finish {
                            Some("stream ended without upstream finish_reason/[DONE] (response likely truncated)".to_string())
                        } else {
                            this.finish_reason.clone()
                                .filter(|f| f == "error")
                                .map(|f| format!("finish_reason={f}"))
                        };
                        if let Some(mut ld) = this.log_buffer.take() {
                            ld.response_body_len = this.response_bytes;
                            tokio::spawn(async move {
                                crate::admin::record_request_with_tokens(
                                    &ld.log_buffer, &ld.model, &ld.provider, &ld.endpoint,
                                    ld.start, pt, ct, ld.response_body_len, false,
                                    hit, miss, err_for_log,
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

/// 从 usage 对象提取上游 KV Cache 命中/未命中 token 数.
///
/// 兼容两种 OpenAI 兼容供应商的 schema:
///   1) DeepSeek / 多数国产供应商 (扁平): `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`;
///   2) OpenAI 原生 (嵌套): `usage.prompt_tokens_details.cached_tokens`
///      —— 未命中无法直接取得, 以 `prompt_tokens - cached_tokens` 推导 (prompt 中非缓存部分).
///   3) Anthropic 原生 (OpenCode GO / Claude 后端): `usage.cache_read_input_tokens` (命中)
///      + `usage.input_tokens` (非缓存输入, 含首次写入, 作为未命中).
/// 均未提供时返回 (0, 0).
fn usage_cache(usage: &serde_json::Value) -> (u32, u32) {
    // 1) 扁平 schema (DeepSeek 等)
    if let Some(hit) = usage.get("prompt_cache_hit_tokens").and_then(|v| v.as_u64()) {
        let hit = hit as u32;
        let miss = usage
            .get("prompt_cache_miss_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;
        return (hit, miss);
    }
    // 2) OpenAI 原生嵌套 schema
    if let Some(cached) = usage
        .get("prompt_tokens_details")
        .and_then(|d| d.get("cached_tokens"))
        .and_then(|v| v.as_u64())
    {
        let cached = cached as u32;
        let pt = usage.get("prompt_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
        let miss = pt.saturating_sub(cached);
        return (cached, miss);
    }
    // 3) Anthropic 原生 schema (OpenCode GO / Claude 后端):
    //    cache_read_input_tokens = 命中 (从缓存读取); input_tokens 已是"非缓存输入" (含首次写入), 直接作未命中.
    //    注意: Anthropic 的 input_tokens 不含 cache_read, 总量 = input_tokens + cache_read_input_tokens, 不可相减.
    if let Some(read) = usage.get("cache_read_input_tokens").and_then(|v| v.as_u64()) {
        let read = read as u32;
        let miss = if let Some(inp) = usage.get("input_tokens").and_then(|v| v.as_u64()) {
            inp as u32
        } else {
            // 部分转换层把总量放在 prompt_tokens, 此时未命中 = 总量 - 缓存命中
            (usage.get("prompt_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32)
                .saturating_sub(read)
        };
        return (read, miss);
    }
    (0, 0)
}

/// 从已完成 (非流式) 的响应 JSON 中提取 token 用量, 用于日志统计.
///
/// 返回 (prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens).
/// 后两者来自上游 `usage` 的 KV Cache 命中统计 (兼容 DeepSeek 扁平 / OpenAI 嵌套 schema),
/// 未提供时为 0.
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
        let (h, m) = usage_cache(u);
        hit = h;
        miss = m;
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

/// 构造单行格式化错误信息 (type 中文优先, 其次 code, 其次状态码中文).
fn format_error_line(status: u16, msg: &str, err_type: &str, code: &str, status_expl: &str) -> String {
    if !err_type.is_empty() && !crate::i18n::error_type(err_type).is_empty() {
        format!("{status} {} [{err_type}]: {msg}", crate::i18n::error_type(err_type))
    } else if !code.is_empty() {
        format!("{status} [{code}]: {msg}")
    } else if !err_type.is_empty() {
        format!("{status} [{err_type}]: {msg}")
    } else if !status_expl.is_empty() {
        format!("{status} {status_expl}: {msg}")
    } else {
        format!("{status}: {msg}")
    }
}

/// 格式化上游错误信息: 解析 JSON 错误响应，添加中文说明.
///
/// 优先级: error.type 中文 > HTTP 状态码中文 > 原始信息.
/// 这样即使上游返回纯文本 (如 `request timeout (HTTP Status: 408)`) 也能给中文说明.
///
/// 兼容性 (方案 B): 除标准 `{error:{message,type,code}}` 外, 也识别顶层
/// `{message:...}` / `{detail:...}` 这类非标准结构 (硅基流动 / FastAPI 网关),
/// 至少给出原始信息而非丢弃.
///
/// 输入: upstream_status (如 400/408), err_body (原始响应体).
/// 输出: "400 请求参数错误 [invalid_request_error]: Error from provider..."
///       或 "408 请求超时（上游处理超时...）: request timeout (HTTP Status: 408)"
fn format_upstream_error(status: u16, err_body: &str) -> String {
    // 兜底: 状态码中文说明 (即使纯文本响应体也生效)
    let status_expl = crate::i18n::http_status(status);

    // 尝试解析 JSON 错误响应
    if let Ok(err_json) = serde_json::from_str::<serde_json::Value>(err_body) {
        if let Some(error) = err_json.get("error") {
            let msg = error.get("message").and_then(|m| m.as_str()).unwrap_or("");
            let err_type = error.get("type").and_then(|t| t.as_str()).unwrap_or("");
            let code = error.get("code").and_then(|c| c.as_str()).unwrap_or("");
            format_error_line(status, msg, err_type, code, status_expl)
        } else if let Some(m) = err_json.get("message").and_then(|m| m.as_str()) {
            // 顶层 message 结构 → status 兜底 + 原文 (空串则回退原文)
            if m.is_empty() {
                format!("{status}: {err_body}")
            } else {
                format!("{status} {status_expl}: {m}")
            }
        } else if let Some(d) = err_json.get("detail").and_then(|d| d.as_str()) {
            // 顶层 detail 结构 (FastAPI/Django) → status 兜底 + 原文 (空串则回退)
            if d.is_empty() {
                format!("{status}: {err_body}")
            } else {
                format!("{status} {status_expl}: {d}")
            }
        } else if !status_expl.is_empty() {
            // JSON 但无 error/message/detail 字段, 用状态码中文兜底
            format!("{status} {status_expl}: {err_body}")
        } else {
            // JSON 但不含任何已知字段，直接返回原始信息
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

/// 解析并翻译单个上游 SSE 事件是否为错误, 返回 `(中文说明, 错误类型)`.
///
/// 用于流式透传中检测上游以 `data: {"error":{...}}` 事件形式返回的错误
/// (OpenAI 流式错误规范: HTTP 仍为 200, 错误在流内返回), 此时 HTTP 状态码
/// 分支 (`format_upstream_error`) 不会触发, 必须在此翻译.
///
/// 兼容三类结构:
///   1) 标准 `{"error":{message,type,code}}` (OpenAI / 硅基流动 / DeepSeek);
///   2) 顶层 `{"message":...}`;
///   3) 顶层 `{"detail":...}` (FastAPI / Django 网关).
/// 正常数据 (无 error/message/detail) 返回 `None`, 不影响透传.
fn translate_sse_error(val: &serde_json::Value) -> Option<(String, String)> {
    let (msg, err_type, code) = if let Some(e) = val.get("error").and_then(|e| e.as_object()) {
        (
            e.get("message").and_then(|m| m.as_str()).unwrap_or("").to_string(),
            e.get("type").and_then(|t| t.as_str()).unwrap_or("").to_string(),
            e.get("code").and_then(|c| c.as_str()).unwrap_or("").to_string(),
        )
    } else if let Some(m) = val.get("message").and_then(|m| m.as_str()) {
        (m.to_string(), String::new(), String::new())
    } else if let Some(d) = val.get("detail").and_then(|d| d.as_str()) {
        (d.to_string(), String::new(), String::new())
    } else {
        return None;
    };
    if msg.is_empty() && err_type.is_empty() && code.is_empty() {
        return None;
    }
    let type_expl = crate::i18n::error_type(&err_type);
    let translated = if !type_expl.is_empty() {
        format!("{type_expl} [{err_type}]: {msg}")
    } else if !code.is_empty() {
        format!("[{code}]: {msg}")
    } else if !err_type.is_empty() {
        format!("[{err_type}]: {msg}")
    } else {
        msg.clone()
    };
    let etype_out = if err_type.is_empty() {
        "stream_error".to_string()
    } else {
        err_type.clone()
    };
    Some((translated, etype_out))
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

        // 测试 5: 顶层 message 结构 (非标准) - 应提取 message 并附状态码中文
        let err_body = r#"{"message":"some error"}"#;
        let result = format_upstream_error(400, err_body);
        assert!(result.contains("400"));
        assert!(result.contains("some error"));
        // 注: 顶层 message 已被提取展示, 不再要求保留原始 JSON 结构

        // 测试 5b: 未知 JSON 结构 (无 error/message/detail) - 兜底保留原文
        let err_body = r#"{"foo":"bar"}"#;
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
            loop_guard: None,
            loop_aborted: false,
            error_closed: false,
            stream_errored: false,
            pending_error_sse: None,
            finish_reason: None,
            clean_finish: false,
            errored_msg: None,
            out_buf: Vec::new(),
            line_buf: Vec::new(),
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

        // OpenAI 原生嵌套 schema: prompt_tokens_details.cached_tokens (未命中 = prompt - cached)
        ts.parse_sse_chunk(
            b"data: {\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":2,\"prompt_tokens_details\":{\"cached_tokens\":80}}}\n\n",
        );
        assert_eq!(ts.tokens_cache_hit, 80);
        assert_eq!(ts.tokens_cache_miss, 20);

        // Anthropic 原生 schema: cache_read_input_tokens / input_tokens (OpenCode GO / Claude 后端)
        ts.parse_sse_chunk(
            b"data: {\"usage\":{\"input_tokens\":50,\"output_tokens\":3,\"cache_read_input_tokens\":200,\"cache_creation_input_tokens\":30}}\n\n",
        );
        assert_eq!(ts.tokens_cache_hit, 200);
        assert_eq!(ts.tokens_cache_miss, 50);
    }

    /// 流式错误事件翻译: 标准 error 事件 / 顶层 message / 顶层 detail / 正常数据返回 None.
    #[test]
    fn test_translate_sse_error() {
        // 标准 OpenAI 流式错误事件 → 按 type 翻译中文
        let v: serde_json::Value = serde_json::from_str(
            r#"{"error":{"message":"model not found","type":"invalid_request_error"}}"#,
        )
        .unwrap();
        let (msg, etype) = translate_sse_error(&v).unwrap();
        assert_eq!(etype, "invalid_request_error");
        assert!(msg.contains("请求参数错误"), "got: {msg}");

        // 顶层 message 结构 (非标准) → 返回原文 + 默认 stream_error 类型
        let v: serde_json::Value = serde_json::from_str(r#"{"message":"bad gateway"}"#).unwrap();
        let (msg, etype) = translate_sse_error(&v).unwrap();
        assert_eq!(etype, "stream_error");
        assert!(msg.contains("bad gateway"));

        // 顶层 detail 结构 (FastAPI) → 返回原文
        let v: serde_json::Value = serde_json::from_str(r#"{"detail":"Validation Error"}"#).unwrap();
        let (msg, _) = translate_sse_error(&v).unwrap();
        assert!(msg.contains("Validation Error"));

        // 正常数据 (无 error/message/detail) → 返回 None, 不影响透传
        let v: serde_json::Value =
            serde_json::from_str(r#"{"choices":[{"delta":{"content":"hi"}}]}"#).unwrap();
        assert!(translate_sse_error(&v).is_none());
    }

    /// 流式透传中检测到 SSE 错误事件 → 改写为中文 error 事件并干净结束流.
    #[test]
    fn test_stream_translates_sse_error() {
        use futures::stream;
        use futures::StreamExt;
        let mut ts = TokenStream {
            inner: stream::iter(vec![Ok::<_, std::io::Error>(bytes::Bytes::from(
                "data: {\"error\":{\"message\":\"model not found\",\"type\":\"invalid_request_error\"}}\n\n",
            ))]),
            done: false,
            tokens_pt: 0,
            tokens_ct: 0,
            tokens_cache_hit: 0,
            tokens_cache_miss: 0,
            response_bytes: 0,
            loop_guard: None,
            loop_aborted: false,
            error_closed: false,
            stream_errored: false,
            pending_error_sse: None,
            finish_reason: None,
            clean_finish: false,
            errored_msg: None,
            out_buf: Vec::new(),
            line_buf: Vec::new(),
            log_buffer: None,
        };
        // 第一次 poll: 收到翻译后的中文 SSE error 事件
        let first = futures::executor::block_on(ts.next()).unwrap().unwrap();
        let text = String::from_utf8(first.to_vec()).unwrap();
        assert!(text.starts_with("data: "), "应为 SSE 帧: {text}");
        assert!(text.contains("请求参数错误"), "应为中文翻译: {text}");
        assert!(text.contains("invalid_request_error"));
        // 第二次 poll: 流已干净终止
        let second = futures::executor::block_on(ts.next());
        assert!(second.is_none());
    }
}

