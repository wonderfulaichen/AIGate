//! Anthropic `/messages` 协议 ↔ OpenAI `chat/completions` 转换层.
//!
//! AIGate 对外保持 OpenAI 兼容; 仅对配置了 `api_format: "anthropic"` 的供应商
//! (如 OpenCode Go 网关的 MiniMax/Qwen 系列, 官方 go.mdx 规定它们走 `/messages`)
//! 在转发边界做双向协议转换: 请求 OpenAI→Anthropic, 响应 Anthropic→OpenAI.
//!
//! 字段映射参考 opencode 源码 `packages/llm/src/protocols/anthropic-messages.ts`:
//! - messages: system 提取到顶层 system 数组; tool 消息折叠进前一条 user 的 tool_result 块
//! - tools: `{name, description, input_schema}` (input_schema 展平为单层 object)
//! - tool_choice: auto→auto / required→any / 指定→`{type:"tool",name}` / none→移除
//! - max_tokens: Anthropic 必填, 缺省 4096 (同 opencode 默认)
//! - stop: 数组 → stop_sequences
//! - reasoning_effort → `thinking: {type:"enabled", budget_tokens}`
//! - usage: `input_tokens = input + cache_read + cache_creation` 合并 (opencode 同款)
//! - stop_reason: end_turn→stop / max_tokens→length / tool_use→tool_calls / refusal→content_filter

use serde_json::{json, Map, Value};
use std::collections::HashMap;

// ─── 请求转换: OpenAI → Anthropic ───

/// 把 OpenAI chat/completions 请求体转换为 Anthropic /messages 请求体.
///
/// 入参 `body` 应已由 [`crate::proxy::inject_model_params`] 处理 (model 已替换为
/// 上游真实名、reasoning_effort 已注入), 因此本函数只做协议格式转换.
///
/// `prompt_cache`: 是否注入 `cache_control` 断点 (仅对走 /messages 的供应商生效).
/// 开启后在 system 末块 + 最后一条 user 消息末块各放一个 ephemeral 断点, 使上游
/// 第二轮起命中 prompt cache (input 按 0.1x 计 + 一次性写入费). 个别网关不支持
/// 时会改写/丢弃 client cache_control 并报错, 此时由调用方传 false 关闭.
pub fn openai_to_anthropic(body: &Value, prompt_cache: bool) -> Value {
    let mut out = Map::new();

    // model: 保留 (inject_model_params 已做 upstream_model 替换)
    if let Some(m) = body.get("model") {
        out.insert("model".to_string(), m.clone());
    }

    // messages + system
    lower_messages(body, &mut out, prompt_cache);

    // tools: OpenAI function 声明 → Anthropic {name, description, input_schema}
    if let Some(tools) = body.get("tools").and_then(|t| t.as_array()) {
        let lowered: Vec<Value> = tools
            .iter()
            .filter_map(|t| {
                let f = t.get("function").unwrap_or(t);
                let name = f.get("name").and_then(|v| v.as_str())?.to_string();
                let mut o = Map::new();
                o.insert("name".to_string(), json!(name));
                if let Some(desc) = f.get("description").and_then(|v| v.as_str()) {
                    if !desc.is_empty() {
                        o.insert("description".to_string(), json!(desc));
                    }
                }
                // input_schema: 展平为单层 object (去掉 $schema 等 openai 专属键)
                let schema = f
                    .get("parameters")
                    .and_then(|p| p.as_object())
                    .map(|p| {
                        let mut s = p.clone();
                        s.remove("$schema");
                        Value::Object(s)
                    })
                    .unwrap_or_else(|| json!({"type": "object"}));
                o.insert("input_schema".to_string(), schema);
                Some(Value::Object(o))
            })
            .collect();
        if !lowered.is_empty() {
            out.insert("tools".to_string(), Value::Array(lowered));
        }
    }

    // tool_choice 映射
    if let Some(tc) = body.get("tool_choice") {
        let mapped = match tc {
            Value::String(s) if s == "auto" => Some(json!({"type": "auto"})),
            Value::String(s) if s == "required" => Some(json!({"type": "any"})),
            Value::String(s) if s == "none" => None, // Anthropic 无 "none", 缺省即 auto
            Value::Object(o) => {
                let name = o
                    .get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(|v| v.as_str())
                    .or_else(|| o.get("name").and_then(|v| v.as_str()));
                match name {
                    Some(n) => Some(json!({"type": "tool", "name": n})),
                    None => None,
                }
            }
            _ => None,
        };
        if let Some(m) = mapped {
            out.insert("tool_choice".to_string(), m);
        }
    }

    // max_tokens: Anthropic 必填, 缺省 4096 (兼容 max_completion_tokens 别名)
    let max_tokens = body
        .get("max_tokens")
        .or_else(|| body.get("max_completion_tokens"))
        .cloned()
        .unwrap_or_else(|| json!(4096));
    out.insert("max_tokens".to_string(), max_tokens);

    // 透传可选参数
    for key in ["temperature", "top_p", "stream"] {
        if let Some(v) = body.get(key) {
            out.insert(key.to_string(), v.clone());
        }
    }
    // stop: 数组 → stop_sequences
    if let Some(stop) = body.get("stop").and_then(|s| s.as_array()) {
        if !stop.is_empty() {
            out.insert("stop_sequences".to_string(), Value::Array(stop.clone()));
        }
    }

    // reasoning_effort → thinking (Anthropic 的思考开关 + budget)
    if let Some(effort) = body.get("reasoning_effort").and_then(|v| v.as_str()) {
        let budget = match effort.to_ascii_lowercase().as_str() {
            "low" => 1024,
            "medium" => 2048,
            "high" => 4096,
            "max" | "maximum" => 8000,
            _ => 4096,
        };
        out.insert(
            "thinking".to_string(),
            json!({"type": "enabled", "budget_tokens": budget}),
        );
    }

    Value::Object(out)
}

/// 消息转换: OpenAI messages → Anthropic messages + 顶层 system.
///
/// `prompt_cache`: 开启时在 system 数组末块 + 最后一条 user 消息末块注入
/// `cache_control: {type:"ephemeral"}` 断点, 让上游第二轮起命中 prompt cache.
fn lower_messages(body: &Value, out: &mut Map<String, Value>, prompt_cache: bool) {
    let Some(msgs) = body.get("messages").and_then(|m| m.as_array()) else {
        return;
    };
    let mut system: Vec<Value> = vec![];
    let mut anthropic: Vec<Value> = vec![];

    for msg in msgs {
        let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
        match role {
            "system" => {
                if let Some(t) = msg.get("content").and_then(|c| c.as_str()) {
                    if !t.is_empty() {
                        system.push(json!({"type": "text", "text": t}));
                    }
                }
            }
            "user" => {
                let content = lower_user_content(msg);
                if !content.is_empty() {
                    anthropic.push(json!({"role": "user", "content": content}));
                }
            }
            "assistant" => {
                let blocks = lower_assistant_content(msg);
                if !blocks.is_empty() {
                    anthropic.push(json!({"role": "assistant", "content": blocks}));
                }
            }
            "tool" => {
                // Anthropic 无独立 tool 消息: 折叠进前一条 user 消息的 tool_result 块
                let tr = tool_result_block(msg);
                match anthropic.last_mut() {
                    Some(last) if last.get("role").and_then(|r| r.as_str()) == Some("user") => {
                        if let Some(arr) = last.get_mut("content").and_then(|c| c.as_array_mut()) {
                            arr.push(tr);
                        }
                    }
                    _ => anthropic.push(json!({"role": "user", "content": [tr]})),
                }
            }
            _ => {}
        }
    }

    if !system.is_empty() {
        // prompt cache 断点 1/2: system 数组末块 (覆盖全局稳定前缀, 第二轮起命中缓存)
        if prompt_cache {
            if let Some(last) = system.last_mut() {
                if let Some(o) = last.as_object_mut() {
                    o.insert("cache_control".to_string(), json!({"type": "ephemeral"}));
                }
            }
        }
        out.insert("system".to_string(), Value::Array(system));
    }

    if !anthropic.is_empty() {
        // prompt cache 断点 2/2: 最后一条 user 消息末块 (覆盖会话尾部可变前缀前的稳定部分)
        if prompt_cache {
            if let Some(last_user) = anthropic
                .iter_mut()
                .rev()
                .find(|m| m.get("role").and_then(|r| r.as_str()) == Some("user"))
            {
                if let Some(arr) = last_user.get_mut("content").and_then(|c| c.as_array_mut()) {
                    if let Some(last_block) = arr.last_mut() {
                        if let Some(o) = last_block.as_object_mut() {
                            o.insert("cache_control".to_string(), json!({"type": "ephemeral"}));
                        }
                    }
                }
            }
        }
        out.insert("messages".to_string(), Value::Array(anthropic));
    }
}

/// user 消息 content → Anthropic blocks (text / image).
fn lower_user_content(msg: &Value) -> Vec<Value> {
    match msg.get("content") {
        Some(Value::String(s)) => vec![json!({"type": "text", "text": s})],
        Some(Value::Array(parts)) => {
            let mut out = vec![];
            for p in parts {
                match p.get("type").and_then(|t| t.as_str()) {
                    Some("text") => {
                        if let Some(t) = p.get("text").and_then(|x| x.as_str()) {
                            if !t.is_empty() {
                                out.push(json!({"type": "text", "text": t}));
                            }
                        }
                    }
                    Some("image_url") => {
                        // OpenAI image_url → Anthropic image block (url / base64 data URI)
                        let url = p
                            .pointer("/image_url/url")
                            .and_then(|u| u.as_str())
                            .unwrap_or("");
                        if let Some(data_uri) = url.strip_prefix("data:") {
                            if let Some((mime_part, b64)) = data_uri.split_once(',') {
                                if !b64.is_empty() {
                                    let mime = mime_part
                                        .split(';')
                                        .next()
                                        .unwrap_or("image/png")
                                        .to_string();
                                    out.push(json!({
                                        "type": "image",
                                        "source": {"type": "base64", "media_type": mime, "data": b64}
                                    }));
                                }
                            }
                        } else if !url.is_empty() {
                            out.push(json!({
                                "type": "image",
                                "source": {"type": "url", "url": url}
                            }));
                        }
                    }
                    _ => {}
                }
            }
            out
        }
        _ => vec![],
    }
}

/// assistant 消息 content → Anthropic blocks (text + tool_use).
fn lower_assistant_content(msg: &Value) -> Vec<Value> {
    let mut blocks: Vec<Value> = vec![];
    if let Some(t) = msg.get("content").and_then(|c| c.as_str()) {
        if !t.is_empty() {
            blocks.push(json!({"type": "text", "text": t}));
        }
    } else if let Some(parts) = msg.get("content").and_then(|c| c.as_array()) {
        for p in parts {
            if p.get("type").and_then(|t| t.as_str()) == Some("text") {
                if let Some(t) = p.get("text").and_then(|x| x.as_str()) {
                    if !t.is_empty() {
                        blocks.push(json!({"type": "text", "text": t}));
                    }
                }
            }
        }
    }
    // 顶层 tool_calls (OpenAI 非流式 assistant 消息格式)
    if let Some(tcs) = msg.get("tool_calls").and_then(|c| c.as_array()) {
        for tc in tcs {
            let id = tc.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let f = tc.get("function").cloned().unwrap_or_else(|| json!({}));
            let name = f.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let input = f
                .get("arguments")
                .and_then(|v| v.as_str())
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .unwrap_or_else(|| json!({}));
            blocks.push(json!({"type": "tool_use", "id": id, "name": name, "input": input}));
        }
    }
    blocks
}

/// tool 消息 → Anthropic tool_result block.
fn tool_result_block(msg: &Value) -> Value {
    let id = msg
        .get("tool_call_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    // content 字符串或数组 → 字符串
    let content = match msg.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(parts)) => {
            let mut buf = String::new();
            for p in parts {
                if let Some(t) = p.get("text").and_then(|x| x.as_str()) {
                    buf.push_str(t);
                }
            }
            buf
        }
        _ => String::new(),
    };
    let is_error = msg
        .get("is_error")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    json!({
        "type": "tool_result",
        "tool_use_id": id,
        "content": content,
        "is_error": is_error,
    })
}

// ─── 流式响应转换: Anthropic SSE → OpenAI SSE ───

/// Anthropic SSE 流事件 → OpenAI SSE 事件的有状态转换器.
///
/// Anthropic SSE 每个事件由 `event: <name>` + `data: {json}` 两行组成;
/// `feed_line` 逐行喂入, 返回 0..N 条 OpenAI 格式 data payload (不含 "data: " 前缀).
/// tool_use 的 partial JSON 增量在此累积, 以 OpenAI tool_calls 累积式 arguments 帧发出.
pub struct AnthropicStreamConv {
    /// 待配对的 event 名 (上一条 `event:` 行).
    pending_event: Option<String>,
    /// tool_use 累积: content_block index → (id, name, 已累积 input JSON).
    tools: HashMap<usize, (String, String, String)>,
}

impl AnthropicStreamConv {
    pub fn new() -> Self {
        Self {
            pending_event: None,
            tools: HashMap::new(),
        }
    }

    /// 输入一行 SSE (不含尾随换行), 返回要转发的 OpenAI data payload 列表.
    pub fn feed_line(&mut self, line: &str) -> Vec<String> {
        let line = line.trim_end_matches('\r');
        if let Some(ev) = line.strip_prefix("event:") {
            self.pending_event = Some(ev.trim().to_string());
            return vec![];
        }
        let data = if let Some(d) = line.strip_prefix("data:") {
            d.trim()
        } else if line.starts_with('{') {
            line
        } else {
            return vec![]; // 注释 / 空行等忽略
        };
        if data.is_empty() {
            return vec![];
        }
        let ev = self.pending_event.take().unwrap_or_default();
        let Ok(val) = serde_json::from_str::<Value>(data) else {
            return vec![];
        };
        self.handle_event(&ev, &val)
    }

    fn handle_event(&mut self, ev: &str, val: &Value) -> Vec<String> {
        match ev {
            "message_start" => {
                // role 帧 (OpenAI 流需要首帧带 role) + usage(input) 帧
                let mut out = vec![];
                if let Some(u) = val.get("message").and_then(|m| m.get("usage")) {
                    out.push(usage_openai(u));
                }
                out.push(
                    json!({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]})
                        .to_string(),
                );
                out
            }
            "content_block_start" => {
                let idx = val
                    .get("index")
                    .and_then(|i| i.as_u64())
                    .unwrap_or(0) as usize;
                let is_tool_use = val
                    .get("content_block")
                    .and_then(|c| c.get("type"))
                    .and_then(|t| t.as_str())
                    == Some("tool_use");
                if is_tool_use {
                    // 某些网关 (MiniMax/Qwen) 的 content_block_start 只带 type: "tool_use",
                    // id/name 可能为空; 兜底从后续 input_json_delta 无 index 时取 0.
                    let cb = val.get("content_block").cloned().unwrap_or_else(|| json!({}));
                    let id = cb.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let name = cb.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    self.tools.insert(idx, (id, name, String::new()));
                }
                vec![]
            }
            "content_block_delta" => {
                let idx = val
                    .get("index")
                    .and_then(|i| i.as_u64())
                    .unwrap_or(0) as usize;
                let Some(delta) = val.get("delta") else {
                    return vec![];
                };
                match delta.get("type").and_then(|t| t.as_str()) {
                    Some("text_delta") => {
                        let text = delta.get("text").and_then(|v| v.as_str()).unwrap_or("");
                        vec![json!({
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": null}]
                        })
                        .to_string()]
                    }
                    Some("thinking_delta") => {
                        // 思考增量 → OpenAI reasoning_content (兼容 IDE 思考展示)
                        let text = delta.get("thinking").and_then(|v| v.as_str()).unwrap_or("");
                        vec![json!({
                            "choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": null}]
                        })
                        .to_string()]
                    }
                    Some("input_json_delta") => {
                        // 累积 tool 参数 partial JSON → OpenAI tool_calls 累积式 arguments
                        let partial = delta.get("partial_json").and_then(|v| v.as_str()).unwrap_or("");
                        if let Some((id, name, acc)) = self.tools.get_mut(&idx) {
                            acc.push_str(partial);
                            let (id_c, name_c, args) = (id.clone(), name.clone(), acc.clone());
                            vec![json!({
                                "choices": [{"index": 0, "delta": {
                                    "tool_calls": [{"index": idx, "id": id_c, "function": {"name": name_c, "arguments": args}}]
                                }, "finish_reason": null}]
                            })
                            .to_string()]
                        } else {
                            vec![]
                        }
                    }
                    _ => vec![],
                }
            }
            "content_block_stop" => vec![],
            "message_delta" => {
                // finish_reason 映射帧 + usage(output) 帧
                let mut out = vec![];
                let stop = val
                    .get("delta")
                    .and_then(|d| d.get("stop_reason"))
                    .and_then(|s| s.as_str())
                    .unwrap_or("");
                if !stop.is_empty() {
                    out.push(
                        json!({"choices": [{"index": 0, "delta": {}, "finish_reason": map_stop_reason(stop)}]})
                            .to_string(),
                    );
                }
                if let Some(u) = val.get("usage") {
                    out.push(usage_openai(u));
                }
                out
            }
            "message_stop" => vec!["[DONE]".to_string()],
            "error" => {
                // Anthropic error 事件 → OpenAI error 事件
                let err = val
                    .get("error")
                    .cloned()
                    .unwrap_or_else(|| val.clone());
                vec![json!({"error": err}).to_string()]
            }
            _ => vec![], // ping 等忽略
        }
    }
}

impl Default for AnthropicStreamConv {
    fn default() -> Self {
        Self::new()
    }
}

/// Anthropic usage → OpenAI usage payload.
///
/// 保留 KV cache 细分 (cache_read / cache_creation) 到 `prompt_tokens_details`, 不合并丢弃
/// (opencode 亦如此, 合并即丢信息). `prompt_tokens` 维持三者之和以兼容 IDE 输入计数.
///
/// **仅在确有缓存计数时才输出 `prompt_tokens_details`**: `message_start` 的 usage 只含
/// `input_tokens` (cached=0), 若无条件输出 `cached_tokens:0`, 下游 `usage_cache` 第 2 分支会
/// 误算 `miss = prompt_tokens - 0 = input_tokens` (详见该函数), 污染"未使用缓存"请求的命中率统计.
/// 缺失/为零的 cache 字段则依靠分支 1/3 判 (0,0), 不污染.
fn usage_openai(u: &Value) -> String {
    let input = u.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let cache_read = u.get("cache_read_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let cache_creation = u.get("cache_creation_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let prompt = input + cache_read + cache_creation;
    let output = u.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let mut usage = json!({
        "prompt_tokens": prompt,
        "completion_tokens": output,
    });
    if cache_read > 0 || cache_creation > 0 {
        usage["prompt_tokens_details"] = json!({
            "cached_tokens": cache_read,
            "cache_creation_tokens": cache_creation
        });
    }
    json!({ "choices": [], "usage": usage }).to_string()
}

/// Anthropic stop_reason → OpenAI finish_reason.
fn map_stop_reason(r: &str) -> String {
    match r {
        "end_turn" | "stop_sequence" | "pause_turn" => "stop".to_string(),
        "max_tokens" => "length".to_string(),
        "tool_use" => "tool_calls".to_string(),
        "refusal" => "content_filter".to_string(),
        other => other.to_string(),
    }
}

// ─── 非流式响应转换: Anthropic → OpenAI ───

/// 把 Anthropic /messages 非流式响应体转换为 OpenAI chat/completions 响应体.
pub fn anthropic_to_openai_nonstream(body: &Value) -> Value {
    let mut content_parts: Vec<String> = vec![];
    let mut tool_calls: Vec<Value> = vec![];
    if let Some(blocks) = body.get("content").and_then(|c| c.as_array()) {
        for b in blocks {
            match b.get("type").and_then(|t| t.as_str()) {
                Some("text") => {
                    if let Some(t) = b.get("text").and_then(|x| x.as_str()) {
                        content_parts.push(t.to_string());
                    }
                }
                Some("tool_use") => {
                    tool_calls.push(json!({
                        "id": b.get("id").cloned().unwrap_or_else(|| json!("")),
                        "type": "function",
                        "function": {
                            "name": b.get("name").cloned().unwrap_or_else(|| json!("")),
                            "arguments": b.get("input").cloned().unwrap_or_else(|| json!({})).to_string(),
                        }
                    }));
                }
                _ => {}
            }
        }
    }
    let finish = body
        .get("stop_reason")
        .and_then(|s| s.as_str())
        .map(map_stop_reason)
        .unwrap_or_else(|| "stop".to_string());
    // 拆解 input / cache_read / cache_creation 三项, 既保留总和到 prompt_tokens, 又平行输出
    // prompt_tokens_details 供下游 usage_cache 统计命中率 (合并会丢信息).
    let (prompt, cached, creation, ct) = match body.get("usage") {
        Some(u) => {
            let input = u.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
            let cache_read = u.get("cache_read_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
            let cache_creation =
                u.get("cache_creation_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
            (
                input + cache_read + cache_creation,
                cache_read,
                cache_creation,
                u.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0),
            )
        }
        None => (0, 0, 0, 0),
    };
    let mut message = serde_json::Map::new();
    message.insert("role".to_string(), json!("assistant"));
    message.insert("content".to_string(), json!(content_parts.join("")));
    if !tool_calls.is_empty() {
        message.insert("tool_calls".to_string(), Value::Array(tool_calls));
    }
    json!({
        "id": body.get("id").cloned().unwrap_or_else(|| json!("chatcmpl-anthropic")),
        "object": "chat.completion",
        "model": body.get("model").cloned().unwrap_or_else(|| json!("")),
        "choices": [{"index": 0, "message": Value::Object(message), "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": ct,
            "total_tokens": prompt + ct,
            "prompt_tokens_details": {
                "cached_tokens": cached,
                "cache_creation_tokens": creation
            }
        },
    })
}

/// 把 Anthropic 错误响应体转换为 OpenAI 错误格式.
///
/// Anthropic: `{"type":"error","error":{"type":"...","message":"..."}}`
/// OpenAI:    `{"error":{"message":"...","type":"..."}}`
pub fn anthropic_error_to_openai(body: &Value) -> Value {
    json!({
        "error": body
            .get("error")
            .cloned()
            .unwrap_or_else(|| json!({"message": body.to_string()}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_basic_text() {
        let body = json!({
            "model": "minimax-m2.5",
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
                {"role": "user", "content": "再见"}
            ],
            "stream": true
        });
        let out = openai_to_anthropic(&body, false);
        assert_eq!(out["model"], "minimax-m2.5");
        assert_eq!(out["max_tokens"], 4096); // 缺省
        assert_eq!(out["stream"], true);
        // system 提取到顶层
        assert_eq!(out["system"][0]["text"], "你是助手");
        // 消息数: user/assistant/user = 3
        let msgs = out["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[0]["role"], "user");
        assert_eq!(msgs[0]["content"][0]["type"], "text");
        assert_eq!(msgs[1]["content"][0]["text"], "你好！");
    }

    #[test]
    fn request_tool_message_folded_into_user() {
        let body = json!({
            "model": "m",
            "messages": [
                {"role": "user", "content": "查天气"},
                {"role": "assistant", "content": null, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"bj\"}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "晴"}
            ]
        });
        let out = openai_to_anthropic(&body, false);
        let msgs = out["messages"].as_array().unwrap();
        // assistant: content 含 tool_use
        assert_eq!(msgs[1]["content"][0]["type"], "tool_use");
        assert_eq!(msgs[1]["content"][0]["name"], "get_weather");
        assert_eq!(msgs[1]["content"][0]["input"]["city"], "bj");
        // tool → 折叠进下一条 user 的 tool_result
        assert_eq!(msgs[2]["role"], "user");
        assert_eq!(msgs[2]["content"][0]["type"], "tool_result");
        assert_eq!(msgs[2]["content"][0]["tool_use_id"], "c1");
        assert_eq!(msgs[2]["content"][0]["content"], "晴");
    }

    #[test]
    fn request_tools_and_choice() {
        let body = json!({
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "$schema": "x", "properties": {}}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
            "reasoning_effort": "max"
        });
        let out = openai_to_anthropic(&body, false);
        assert_eq!(out["tools"][0]["name"], "f");
        assert!(out["tools"][0]["input_schema"].get("$schema").is_none()); // 展平
        assert_eq!(out["tool_choice"], json!({"type": "tool", "name": "f"}));
        // reasoning_effort → thinking
        assert_eq!(out["thinking"]["type"], "enabled");
        assert_eq!(out["thinking"]["budget_tokens"], 8000);
    }

    #[test]
    fn request_image_url() {
        let body = json!({
            "model": "m",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
            ]}]
        });
        let out = openai_to_anthropic(&body, false);
        let content = out["messages"][0]["content"].as_array().unwrap();
        assert_eq!(content[1]["type"], "image");
        assert_eq!(content[1]["source"]["type"], "url");
    }

    /// T1: prompt_cache=true 时, system 末块 + 最后一条 user 末块注入 cache_control.
    #[test]
    fn prompt_cache_injects_breakpoints() {
        let body = json!({
            "model": "m",
            "messages": [
                {"role": "system", "content": "s1"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}
            ]
        });
        let out = openai_to_anthropic(&body, true);
        // system 末块注入
        let sys = out["system"].as_array().unwrap();
        assert_eq!(sys.last().unwrap()["cache_control"], json!({"type": "ephemeral"}));
        // 最后一条 user 消息 (u2) 的 content 末块注入
        let msgs = out["messages"].as_array().unwrap();
        let last = msgs.last().unwrap();
        assert_eq!(last["role"], "user");
        let content = last["content"].as_array().unwrap();
        assert_eq!(content.last().unwrap()["cache_control"], json!({"type": "ephemeral"}));
        // 中间 user (u1) 不注入
        assert!(msgs[0]["content"][0].get("cache_control").is_none());
    }

    /// T1: prompt_cache=false 时完全不注入.
    #[test]
    fn prompt_cache_off_injects_nothing() {
        let body = json!({
            "model": "m",
            "messages": [
                {"role": "system", "content": "s1"},
                {"role": "user", "content": "u1"}
            ]
        });
        let out = openai_to_anthropic(&body, false);
        let sys = out["system"].as_array().unwrap();
        assert!(sys.last().unwrap().get("cache_control").is_none());
        let msgs = out["messages"].as_array().unwrap();
        assert!(msgs.last().unwrap()["content"][0].get("cache_control").is_none());
    }

    #[test]
    fn stream_full_flow() {
        let mut conv = AnthropicStreamConv::new();
        let mut all: Vec<String> = vec![];
        let lines = [
            "event: message_start",
            r#"data: {"type":"message_start","message":{"id":"m1","usage":{"input_tokens":10,"output_tokens":0}}}"#,
            "event: content_block_start",
            r#"data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}"#,
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"世界"}}"#,
            "event: content_block_stop",
            r#"data: {"type":"content_block_stop","index":0}"#,
            "event: message_delta",
            r#"data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}"#,
            "event: message_stop",
            r#"data: {"type":"message_stop"}"#,
        ];
        for l in lines {
            all.extend(conv.feed_line(l));
        }
        let joined = all.join("\n");
        // 首帧 role
        assert!(joined.contains("\"role\":\"assistant\""));
        // 文本增量
        assert!(joined.contains("你好"));
        assert!(joined.contains("世界"));
        // usage: prompt=10, completion=7
        assert!(joined.contains("\"prompt_tokens\":10"));
        assert!(joined.contains("\"completion_tokens\":7"));
        // finish_reason: end_turn→stop
        assert!(joined.contains("\"finish_reason\":\"stop\""));
        // 结束帧
        assert_eq!(all.last().map(|s| s.as_str()), Some("[DONE]"));
    }

    #[test]
    fn stream_tool_use_accumulates() {
        let mut conv = AnthropicStreamConv::new();
        let mut all: Vec<String> = vec![];
        let lines = [
            "event: content_block_start",
            r#"data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"f","input":{}}}"#,
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"a\":"}}"#,
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"1}"}}"#,
            "event: content_block_stop",
            r#"data: {"type":"content_block_stop","index":0}"#,
            "event: message_delta",
            r#"data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}"#,
        ];
        for l in lines {
            all.extend(conv.feed_line(l));
        }
        let joined = all.join("\n");
        // 累积式 arguments (JSON 字符串内为转义形式 {\"a\":1})
        assert!(joined.contains("{\\\"a\\\":1}"));
        // tool_use → finish_reason=tool_calls
        assert!(joined.contains("\"finish_reason\":\"tool_calls\""));
    }

    #[test]
    fn usage_preserves_cache_details() {
        // 回归测试: Anthropic usage 转换必须保留 KV cache 细分, 否则下游命中率恒为 0.
        let u = json!({
            "input_tokens": 10,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 3,
            "output_tokens": 5
        });
        let s = usage_openai(&u);
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["usage"]["prompt_tokens"], 15); // 三项之和
        assert_eq!(v["usage"]["prompt_tokens_details"]["cached_tokens"], 2);
        assert_eq!(v["usage"]["prompt_tokens_details"]["cache_creation_tokens"], 3);

        // 非流式同口径
        let body = json!({
            "id": "m1", "model": "claude", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3, "output_tokens": 5}
        });
        let out = anthropic_to_openai_nonstream(&body);
        assert_eq!(out["usage"]["prompt_tokens"], 15);
        assert_eq!(out["usage"]["prompt_tokens_details"]["cached_tokens"], 2);
        assert_eq!(out["usage"]["prompt_tokens_details"]["cache_creation_tokens"], 3);
    }

    #[test]
    fn usage_no_cache_no_details() {
        // 回归测试: 仅含 input_tokens (无缓存, 对应 message_start) 时绝不能输出
        // prompt_tokens_details, 否则 usage_cache 第2分支会误算 miss=input_tokens, 污染
        // "未使用缓存"请求的命中率统计.
        let u = json!({"input_tokens": 1000, "output_tokens": 0});
        let s = usage_openai(&u);
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert!(v["usage"].get("prompt_tokens_details").is_none(),
            "no-cache usage must not emit prompt_tokens_details, got: {}", s);
        assert_eq!(v["usage"]["prompt_tokens"], 1000);
        // 下游 usage_cache 应判为 (0,0)
        let (hit, miss, creation) = crate::proxy::usage_cache(&v["usage"]);
        assert_eq!((hit, miss, creation), (0, 0, 0));
    }

    #[test]
    fn nonstream_conversion() {
        let body = json!({
            "id": "m1",
            "model": "minimax-m2.5",
            "content": [
                {"type": "text", "text": "第一段"},
                {"type": "text", "text": "第二段"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {"x": 1}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 3, "cache_read_input_tokens": 2}
        });
        let out = anthropic_to_openai_nonstream(&body);
        assert_eq!(out["object"], "chat.completion");
        assert_eq!(out["choices"][0]["message"]["content"], "第一段第二段");
        assert_eq!(out["choices"][0]["finish_reason"], "tool_calls");
        assert_eq!(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "f");
        // input 合并 cache: 5+2=7
        assert_eq!(out["usage"]["prompt_tokens"], 7);
        assert_eq!(out["usage"]["completion_tokens"], 3);
    }

    #[test]
    fn error_conversion() {
        let body = json!({"type": "error", "error": {"type": "overloaded_error", "message": "服务过载"}});
        let out = anthropic_error_to_openai(&body);
        assert_eq!(out["error"]["message"], "服务过载");
        assert_eq!(out["error"]["type"], "overloaded_error");
    }
}
