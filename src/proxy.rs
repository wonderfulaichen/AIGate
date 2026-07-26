//! 反向代理核心 — 读取请求, 按模型路由, 注入 key, SSE 流式透传.
//!
//! 路由表由 providers.json 定义, 支持多个供应商. 详见 providers.rs.

use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

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

/// 共享状态: HTTP client + 供应商路由表 + 请求日志缓冲区 + Key 存储.
#[derive(Clone)]
pub struct AppState {
    pub client: Client,
    /// 路由表, RwLock 支持热重载.
    pub registry: Arc<RwLock<ProviderRegistry>>,
    /// 内存请求日志缓冲区.
    pub log_buffer: LogBuffer,
    /// 持久化日志存储.
    pub log_store: crate::store::LogStore,
    /// API Key 存储.
    pub key_store: KeyStore,
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

    // 5. 注入模型级参数 (reasoning_effort / extra_body)
    let bytes = inject_model_params(bytes, &model_cfg);
    let body_len_val = bytes.len(); // 在 bytes 被 move 前保存

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

    // 7. 发送请求, 获取流式响应
    let ep_clone = endpoint.clone(); // 用于闭包
    let upstream = state
        .client
        .request(Method::POST, &endpoint)
        .headers(req_headers)
        .header("content-type", "application/json")
        .header("accept", "text/event-stream")
        .body(bytes)
        .send()
        .await
        .map_err(|e| {
            let chain = format_error_chain(&e);
            warn!("proxy: send() failed for {ep_clone}: {chain}");
            let e_clone = chain.clone();
            let model2 = model.clone();
            let p2 = provider_name.clone();
            let ep2 = ep_clone.clone();
            let lb = state.log_buffer.clone();
            let s = start;
            tokio::spawn(async move {
                crate::admin::record_request(
                    &lb, &model2, &p2, &ep2, 502, s, body_len_val, Some(e_clone),
                ).await;
            });
            error_response(StatusCode::BAD_GATEWAY, &format!("upstream error: {chain}"))
        })?;

    // 8. 检查 upstream 响应
    let upstream_status = upstream.status().as_u16();
    if upstream_status >= 400 {
        let err_body = upstream.text().await.unwrap_or_default();
        warn!(
            "proxy: upstream {upstream_status} for {endpoint}: {err}",
            err = &err_body[..err_body.len().min(500)]
        );
        let err_msg = format!("upstream returned {upstream_status}: {err_body}");
        let err_clone = err_msg.clone();
        let model2 = model.clone();
        let p2 = provider_name.clone();
        let ep2 = endpoint.clone();
        let lb = state.log_buffer.clone();
        tokio::spawn(async move {
            crate::admin::record_request(
                &lb, &model2, &p2, &ep2, upstream_status, start, body_len_val, Some(err_clone),
            ).await;
        });
        return Err(error_response(
            StatusCode::from_u16(upstream_status).unwrap_or(StatusCode::BAD_GATEWAY),
            &err_msg,
        ));
    }

    // 9. 流式透传 — 通过 TokenTracker 记录 token 用量
    let resp = stream_response_with_tokens(
        upstream,
        state.log_buffer.clone(),
        model,
        provider_name,
        endpoint,
        start,
        body_len_val,
    );
    Ok(resp)
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
) -> Response {
    let status = StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::OK);
    let headers = upstream.headers().clone();
    let inner_stream = upstream.bytes_stream();

    let tracked = TokenStream {
        inner: inner_stream,
        done: false,
        tokens_pt: 0,
        tokens_ct: 0,
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
                    }
                }
            }
        }
    }

    /// 流结束时计算最终 token 数: 优先使用上游返回的精确值, 否则估算.
    fn final_tokens(&self, req_body_len: usize) -> (u32, u32) {
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
        (pt, ct)
    }
}

impl<S, E> Stream for TokenStream<S>
where
    S: Stream<Item = Result<Bytes, E>> + Unpin,
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
                Poll::Ready(None) => {
                    if !this.done {
                        this.done = true;
                        let req_body_len = this.log_buffer.as_ref().map(|ld| ld.req_body_len).unwrap_or(0);
                        let (pt, ct) = this.final_tokens(req_body_len);
                        if let Some(mut ld) = this.log_buffer.take() {
                            ld.response_body_len = this.response_bytes;
                            tokio::spawn(async move {
                                crate::admin::record_request_with_tokens(
                                    &ld.log_buffer, &ld.model, &ld.provider, &ld.endpoint,
                                    ld.start, pt, ct, ld.response_body_len,
                                ).await;
                            });
                        }
                    }
                    return Poll::Ready(None);
                }
                other => return other,
            }
        }
    }
}

/// 从请求 body 解析 model 字段.
fn parse_model(body: &[u8]) -> Option<String> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    v.get("model")?.as_str().map(|s| s.to_string())
}

/// 注入模型级参数到请求 body.
///
/// - upstream_model: 替换 body 中的 model 字段为上游真实模型名.
/// - reasoning_effort: 如果 body 没有该字段且模型配置了, 则注入.
/// - extra_body: 逐字段注入, 不覆盖已有字段.
fn inject_model_params(
    bytes: bytes::Bytes,
    model_cfg: &crate::providers::ModelConfig,
) -> bytes::Bytes {
    // 没有任何需要注入的参数 → 原样返回
    let has_remap = model_cfg.upstream_model.is_some();
    let has_effort = model_cfg.reasoning_effort.is_some();
    let has_extra = model_cfg
        .extra_body
        .as_ref()
        .map(|v| v.is_object())
        .unwrap_or(false);
    if !has_remap && !has_effort && !has_extra {
        return bytes;
    }

    let Ok(mut v) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return bytes; // 解析失败, 原样返回
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

    // 注入 reasoning_effort (不覆盖客户端已有值)
    if let Some(effort) = &model_cfg.reasoning_effort {
        if v.get("reasoning_effort").is_none() {
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

    serde_json::to_vec(&v).unwrap_or_else(|_| bytes.to_vec()).into()
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
