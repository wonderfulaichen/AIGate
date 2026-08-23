//! OpenAI Responses API (`/v1/responses`) ↔ OpenAI `chat/completions` 转换层.
//!
//! AIGate 对外保持 OpenAI 兼容; 对配置了 `api_format: "responses"` 的模型
//! (如 OpenCode Go 网关的 grok / gpt-5.6-luna / muse-spark 系列)
//! 在转发边界做双向协议转换:
//! - 请求: `chat/completions` (messages) → `responses` (input)
//! - 响应: `responses` (output) → `chat/completions` (choices)
//!
//! Responses API 核心差异:
//! - 入参: `messages` → `input`, `max_tokens` → `max_output_tokens`, system→`instructions`
//! - 流式: `response.output_text.delta` → `choices[0].delta.content`
//! - 结束: `response.completed` → `finish_reason` + usage + `[DONE]`
//! - 非流: `output` 数组 → `choices` 数组

use serde_json::{json, Map, Value};
use std::collections::HashMap;

// ─── 请求转换: OpenAI chat/completions → Responses API ───

/// 把 OpenAI chat/completions 请求体转换为 Responses API 请求体.
///
/// 入参 `body` 应已由 [`crate::proxy::inject_model_params`] 处理 (model 已替换为
/// 上游真实名、reasoning_effort 已注入), 因此本函数只做协议格式转换.
pub fn openai_to_responses(body: &Value) -> Value {
    let mut out = Map::new();

    // model: 保留 (inject_model_params 已做 upstream_model 替换)
    if let Some(m) = body.get("model") {
        out.insert("model".to_string(), m.clone());
    }

    // system → instructions: 从 messages 中提取 role=system, 放到顶层 instructions.
    let mut instructions_parts: Vec<String> = Vec::new();
    let mut input: Vec<Value> = Vec::new();

    if let Some(msgs) = body.get("messages").and_then(|m| m.as_array()) {
        for msg in msgs {
            let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
            match role {
                "system" => {
                    // 提取 system 消息内容到 instructions
                    let text = extract_text_content(msg);
                    if !text.is_empty() {
                        instructions_parts.push(text);
                    }
                }
                "assistant" if msg.get("tool_calls").is_some() => {
                    // assistant + tool_calls → Responses API function_call items (每个 tool_call 一个)
                    if let Some(calls) = msg.get("tool_calls").and_then(|c| c.as_array()) {
                        for (idx, call) in calls.iter().enumerate() {
                            let mut call_id = call.get("id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                            let name = call.get("function").and_then(|f| f.get("name")).and_then(|n| n.as_str()).unwrap_or("").trim().to_string();
                            if name.is_empty() {
                                continue;
                            }
                            if call_id.is_empty() {
                                // 上游要求 call_id 非空, 合成一个 (与 tool 侧需一致, 但 tool 侧空值会跳过, 此处合成可保成对)
                                call_id = format!("call_{}_{}", name, idx);
                            }
                            let args_val = call.get("function").and_then(|f| f.get("arguments"));
                            let args = match args_val {
                                Some(Value::String(s)) => s.clone(),
                                Some(v) if v.is_object() || v.is_array() => serde_json::to_string(v).unwrap_or_default(),
                                _ => String::new(),
                            };
                            input.push(json!({
                                "type": "function_call",
                                "id": call_id,
                                "call_id": call_id,
                                "name": name,
                                "arguments": args,
                            }));
                        }
                    }
                }
                "assistant" => {
                    // assistant + content → message item with output_text content type
                    // 空内容跳过, 避免 input[2] type 不匹配 (空 output_text 非法)
                    let content_parts = convert_output_content_types(msg);
                    let is_empty = match &content_parts {
                        Value::Array(arr) => arr.iter().all(|p| p.get("text").and_then(|v| v.as_str()).map(|s| s.trim().is_empty()).unwrap_or(true)),
                        _ => true,
                    };
                    if !is_empty {
                        input.push(json!({
                            "type": "message",
                            "role": "assistant",
                            "content": content_parts,
                        }));
                    }
                }
                "tool" => {
                    // tool result → function_call_output item (call_id 非空才合法)
                    let mut call_id = msg.get("tool_call_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                    if call_id.is_empty() {
                        call_id = msg.get("id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                    }
                    if call_id.is_empty() {
                        // 空 call_id 无法配对 function_call, 跳过以避免 400
                        continue;
                    }
                    // 兼容多种 content 形态: string / array[text] / object
                    let output = match msg.get("content") {
                        Some(Value::String(s)) => s.clone(),
                        Some(Value::Array(parts)) => {
                            let mut out = String::new();
                            for p in parts {
                                if let Some(t) = p.get("text").and_then(|v| v.as_str()) {
                                    if !out.is_empty() { out.push('\n'); }
                                    out.push_str(t);
                                } else if let Some(s) = p.as_str() {
                                    if !out.is_empty() { out.push('\n'); }
                                    out.push_str(s);
                                } else {
                                    let s = serde_json::to_string(p).unwrap_or_default();
                                    if !out.is_empty() { out.push('\n'); }
                                    out.push_str(&s);
                                }
                            }
                            out
                        }
                        Some(v) if v.is_object() => serde_json::to_string(v).unwrap_or_default(),
                        _ => extract_text_content(msg),
                    };
                    input.push(json!({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    }));
                }
                "user" | "developer" => {
                    // user/developer → message item with input_text content type
                    let content_parts = extract_and_convert_content(msg, "input_text");
                    input.push(json!({
                        "type": "message",
                        "role": role,
                        "content": content_parts,
                    }));
                }
                _ => {
                    // 其他角色 → 保留原始结构, 做 content type 映射
                    let mut converted = msg.clone();
                    convert_input_content_types(&mut converted);
                    input.push(converted);
                }
            }
        }
    }

    if !instructions_parts.is_empty() {
        out.insert("instructions".to_string(), json!(instructions_parts.join("\n\n")));
    }

    if !input.is_empty() {
        out.insert("input".to_string(), Value::Array(input));
    }

    // max_tokens → max_output_tokens (Responses 要求 >=16)
    let max_tokens = body
        .get("max_tokens")
        .or_else(|| body.get("max_completion_tokens"));
    if let Some(mt) = max_tokens {
        if let Some(n) = mt.as_u64() {
            if n >= 16 {
                out.insert("max_output_tokens".to_string(), mt.clone());
            } else if n > 0 {
                // CodeBuddy 等发小值 4/5 会触发 upstream 400, 钳制到 16
                out.insert("max_output_tokens".to_string(), json!(16));
            }
        } else {
            out.insert("max_output_tokens".to_string(), mt.clone());
        }
    }

    // tools: chat/completions 格式嵌套在 function 里, Responses API 要求平铺到顶层:
    //   chat/completions: {"type":"function", "function":{"name":"x", "description":"...", "parameters":{...}}}
    //   Responses API:    {"type":"function", "name":"x", "description":"...", "parameters":{...}}
    // 兼容无 type 的隐式 function 与已扁平态, 空数组不落盘
    if let Some(tools) = body.get("tools").and_then(|t| t.as_array()) {
        let converted: Vec<Value> = tools
            .iter()
            .filter_map(|tool| {
                // 已扁平态: 直接含 name 且无 function 嵌套
                if tool.get("function").is_none() && tool.get("name").is_some() {
                    let mut t = Map::new();
                    t.insert("type".to_string(), json!("function"));
                    if let Some(name) = tool.get("name") {
                        t.insert("name".to_string(), name.clone());
                    }
                    if let Some(desc) = tool.get("description") {
                        t.insert("description".to_string(), desc.clone());
                    }
                    if let Some(params) = tool.get("parameters") {
                        t.insert("parameters".to_string(), sanitize_parameters(params));
                    } else {
                        t.insert("parameters".to_string(), json!({"type":"object","properties":{}}));
                    }
                    if let Some(strict) = tool.get("strict") {
                        t.insert("strict".to_string(), strict.clone());
                    } else {
                        t.insert("strict".to_string(), json!(false));
                    }
                    return Some(Value::Object(t));
                }
                // 标准嵌套态: {type:"function", function:{name,...}}
                // 兼容缺 type 的隐式 function: 只要含 function 字段即按 function 处理
                if let Some(func) = tool.get("function") {
                    // 缺 name 的非法 tool 直接丢弃, 避免 tools[0] missing name
                    let name = func.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
                    if name.is_empty() {
                        return None;
                    }
                    let mut t = Map::new();
                    t.insert("type".to_string(), json!("function"));
                    t.insert("name".to_string(), json!(name));
                    if let Some(desc) = func.get("description") {
                        t.insert("description".to_string(), desc.clone());
                    }
                    if let Some(params) = func.get("parameters") {
                        t.insert("parameters".to_string(), sanitize_parameters(params));
                    } else {
                        t.insert("parameters".to_string(), json!({"type":"object","properties":{}}));
                    }
                    if let Some(strict) = func.get("strict") {
                        t.insert("strict".to_string(), strict.clone());
                    } else if let Some(strict) = tool.get("strict") {
                        t.insert("strict".to_string(), strict.clone());
                    } else {
                        t.insert("strict".to_string(), json!(false));
                    }
                    return Some(Value::Object(t));
                }
                // 无法识别的 tool 项直接丢弃
                None
            })
            .collect();
        if !converted.is_empty() {
            out.insert("tools".to_string(), Value::Array(converted));
        }
    }

    // tool_choice: 归一化为 Responses 枚举, 空 tools 时不透传
    // go 网关仅支持 "auto" (required/none/指定函数均 400), 统一钳制为 auto
    let has_tools = out.get("tools").and_then(|v| v.as_array()).map(|a| !a.is_empty()).unwrap_or(false);
    if let Some(tc) = body.get("tool_choice") {
        if !has_tools {
            // 无工具时 tool_choice 无意义, 丢弃以避免 Expected 'function' type
        } else if let Some(s) = tc.as_str() {
            // 字符串枚举: 仅 auto 透传, required/none 钳制为 auto
            if s == "auto" || s == "required" || s == "none" {
                out.insert("tool_choice".to_string(), json!("auto"));
            }
        } else if tc.get("function").and_then(|f| f.get("name")).and_then(|v| v.as_str()).is_some()
            || tc.get("type").and_then(|v| v.as_str()) == Some("function")
            || tc.get("type").and_then(|v| v.as_str()).map(|t| ["auto","required","none"].contains(&t)).unwrap_or(false)
        {
            // 指定函数或 type 枚举: 统一钳制为 auto (下游不支持 required/none/指定函数)
            out.insert("tool_choice".to_string(), json!("auto"));
        }
    }

    // 透传可选参数
    for key in ["temperature", "top_p", "stream", "metadata"] {
        if let Some(v) = body.get(key) {
            out.insert(key.to_string(), v.clone());
        }
    }

    // stop → stop (Responses API 直接用 stop)
    if let Some(stop) = body.get("stop") {
        out.insert("stop".to_string(), stop.clone());
    }

    // reasoning: Responses API 要求 reasoning: {effort, summary}, 顶层 reasoning_effort 会被 go 网关判 unknown parameter
    // 统一将 reasoning_effort / reasoningEffort / reasoning.effort 映射为 reasoning 对象，保留可配置档位
    let model_id = body.get("model").and_then(|m| m.as_str()).unwrap_or("").to_ascii_lowercase();
    let is_muse = model_id.contains("muse-spark");
    let mut mapped_reasoning: Option<Value> = None;
    if let Some(r) = body.get("reasoning") {
        if r.is_object() {
            mapped_reasoning = Some(r.clone());
        } else if let Some(s) = r.as_str() {
            let lower = s.to_ascii_lowercase();
            let eff = match lower.as_str() {
                "minimal" | "low" => "low".to_string(),
                "medium" => "medium".to_string(),
                "high" | "xhigh" => "high".to_string(),
                "max" | "maximum" => "max".to_string(),
                other => other.to_string(),
            };
            let eff = if is_muse && eff == "max" { "high".to_string() } else { eff };
            mapped_reasoning = Some(json!({"effort": eff, "summary": "auto"}));
        }
    }
    if mapped_reasoning.is_none() {
        if let Some(eff_val) = body.get("reasoning_effort").or_else(|| body.get("reasoningEffort")) {
            if let Some(s) = eff_val.as_str() {
                let lower = s.to_ascii_lowercase();
                let eff = match lower.as_str() {
                    "minimal" | "low" => "low".to_string(),
                    "medium" => "medium".to_string(),
                    "high" | "xhigh" => "high".to_string(),
                    "max" | "maximum" => "max".to_string(),
                    other => other.to_string(),
                };
                let eff = if is_muse && eff == "max" { "high".to_string() } else { eff };
                mapped_reasoning = Some(json!({"effort": eff, "summary": "auto"}));
            }
        }
    }
    if let Some(r) = mapped_reasoning {
        out.insert("reasoning".to_string(), r);
    }

    Value::Object(out)
}

/// 清理 JSON Schema 的 parameters, 移除 $schema / additionalProperties 等严格校验下非法字段.
fn sanitize_parameters(v: &Value) -> Value {
    if let Some(obj) = v.as_object() {
        let mut out = Map::new();
        // 仅保留 Responses 兼容的 JSON Schema 核心字段
        if let Some(t) = obj.get("type") {
            out.insert("type".to_string(), t.clone());
        } else {
            out.insert("type".to_string(), json!("object"));
        }
        if let Some(props) = obj.get("properties") {
            out.insert("properties".to_string(), props.clone());
        } else {
            out.insert("properties".to_string(), json!({}));
        }
        if let Some(req) = obj.get("required") {
            out.insert("required".to_string(), req.clone());
        }
        if let Some(desc) = obj.get("description") {
            out.insert("description".to_string(), desc.clone());
        }
        // 非空才返回, 否则回落空对象
        if out.is_empty() {
            json!({"type":"object","properties":{}})
        } else {
            Value::Object(out)
        }
    } else {
        json!({"type":"object","properties":{}})
    }
}

/// 从消息中提取文本内容 (兼容 string/array 格式).
fn extract_text_content(msg: &Value) -> String {
    match msg.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|p| {
                if p.get("type").and_then(|t| t.as_str()) == Some("text") {
                    p.get("text").and_then(|t| t.as_str()).map(|s| s.to_string())
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// 将 chat/completions content type 映射为 Responses API content type.
/// - `"text"` → `"input_text"` (user/developer 消息中的文本)
/// - `"image_url"` → `"input_image"` (用户消息中的图片)
/// 注意: assistant 消息中的 `"output_text"` / function_call 等不在 input 中出现,
///       此函数仅处理客户端发出的 input 侧 content.
fn convert_input_content_types(msg: &mut Value) {
    if let Some(Value::Array(parts)) = msg.get_mut("content") {
        for part in parts.iter_mut() {
            if let Some(t) = part.get_mut("type") {
                if t.as_str() == Some("text") {
                    *t = Value::String("input_text".to_string());
                } else if t.as_str() == Some("image_url") {
                    *t = Value::String("input_image".to_string());
                }
            }
        }
    }
}

/// 从消息中提取 content 并转换 type 字段, 返回 Responses API content 数组.
/// - target_type: 替换后的 type 值 (如 `"input_text"` / `"output_text"`)
fn extract_and_convert_content(msg: &Value, target_type: &str) -> Value {
    match msg.get("content") {
        Some(Value::String(s)) => json!([{"type": target_type, "text": s}]),
        Some(Value::Array(parts)) => {
            let converted: Vec<Value> = parts
                .iter()
                .filter_map(|p| {
                    let text = match p.get("type").and_then(|t| t.as_str()) {
                        Some("text") | Some("input_text") | Some("output_text") => {
                            p.get("text").and_then(|t| t.as_str()).unwrap_or("")
                        }
                        _ => return None,
                    };
                    if text.is_empty() {
                        None
                    } else {
                        Some(json!({"type": target_type, "text": text}))
                    }
                })
                .collect();
            if converted.is_empty() {
                json!([{"type": target_type, "text": ""}])
            } else {
                Value::Array(converted)
            }
        }
        _ => json!([{"type": target_type, "text": ""}]),
    }
}

/// 将 assistant 消息的 content type 映射为 Responses API output_text.
/// - `"text"` → `"output_text"` (assistant 消息中的文本)
fn convert_output_content_types(msg: &Value) -> Value {
    extract_and_convert_content(msg, "output_text")
}

// ─── 流式响应转换: Responses API → OpenAI chat/completions ───

/// Responses API SSE 流事件 → OpenAI SSE 事件的有状态转换器.
///
/// Responses API SSE 每个事件为单行 `data: {json}`, 不使用 `event:` 行;
/// `feed_line` 逐行喂入, 返回 0..N 条 OpenAI 格式 data payload (不含 "data: " 前缀).
pub struct ResponsesStreamConv {
    /// 累积的文本增量, 用于 role 首帧检测.
    has_started: bool,
    /// tool_use 累积: index → (id, name, 已累积 input JSON).
    tools: HashMap<usize, (String, String, String)>,
    /// 上游返回的错误消息 (非 None 时表示请求/生成失败).
    pub last_error: Option<String>,
}

impl ResponsesStreamConv {
    pub fn new() -> Self {
        Self {
            has_started: false,
            tools: HashMap::new(),
            last_error: None,
        }
    }

    /// 输入一行 SSE (不含尾随换行), 返回要转发的 OpenAI data payload 列表.
    pub fn feed_line(&mut self, line: &str) -> Vec<String> {
        let line = line.trim_end_matches('\r');
        let data = if let Some(d) = line.strip_prefix("data:") {
            d.trim()
        } else if line.starts_with('{') {
            line
        } else {
            return vec![]; // 注释 / 空行等忽略
        };
        if data.is_empty() || data == "[DONE]" {
            return if data == "[DONE]" {
                vec!["[DONE]".to_string()]
            } else {
                vec![]
            };
        }
        let Ok(val) = serde_json::from_str::<Value>(data) else {
            return vec![];
        };
        self.handle_event(&val)
    }

    fn handle_event(&mut self, val: &Value) -> Vec<String> {
        let ev_type = val.get("type").and_then(|t| t.as_str()).unwrap_or("");
        match ev_type {
            // ── 流开始 ──
            "response.created" | "response.in_progress" => vec![],

            // ── 文本增量 ──
            "response.output_text.delta" => {
                let mut out = vec![];
                // 首帧: 补 role 帧
                if !self.has_started {
                    self.has_started = true;
                    out.push(
                        json!({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]})
                            .to_string(),
                    );
                }
                let text = val.get("delta").and_then(|d| d.as_str()).unwrap_or("");
                if !text.is_empty() {
                    out.push(
                        json!({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": null}]})
                            .to_string(),
                    );
                }
                out
            }

            // ── thinking 增量 ──
            "response.reasoning_summary.delta" | "response.thinking.delta" => {
                let text = val
                    .get("delta")
                    .and_then(|d| d.as_str())
                    .unwrap_or("");
                if text.is_empty() {
                    return vec![];
                }
                let mut out = vec![];
                if !self.has_started {
                    self.has_started = true;
                    out.push(
                        json!({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]})
                            .to_string(),
                    );
                }
                out.push(
                    json!({"choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": null}]})
                        .to_string(),
                );
                out
            }

            // ── tool call 增量 ──
            "response.function_call_arguments.delta" => {
                let idx = val
                    .get("output_index")
                    .and_then(|i| i.as_u64())
                    .unwrap_or(0) as usize;
                let partial = val.get("delta").and_then(|d| d.as_str()).unwrap_or("");

                // 累积器: 若已存在则复用, 且用非空的 call_id/name 回填空值
                if let Some((id, name, acc)) = self.tools.get_mut(&idx) {
                    if let Some(cid) = val.get("call_id").or_else(|| val.get("id")).and_then(|v| v.as_str()) {
                        if !cid.trim().is_empty() && id.is_empty() {
                            *id = cid.to_string();
                        }
                    }
                    if let Some(n) = val.get("name").and_then(|v| v.as_str()) {
                        if !n.trim().is_empty() && name.is_empty() {
                            *name = n.to_string();
                        }
                    }
                    acc.push_str(partial);
                    let (id_c, name_c, args) = (id.clone(), name.clone(), acc.clone());
                    vec![json!({
                        "choices": [{"index": 0, "delta": {
                            "tool_calls": [{"index": idx, "id": id_c, "type": "function", "function": {"name": name_c, "arguments": args}}]
                        }, "finish_reason": null}]
                    })
                    .to_string()]
                } else {
                    let call_id = val
                        .get("call_id")
                        .or_else(|| val.get("id"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let name = val
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let mut acc = String::new();
                    acc.push_str(partial);
                    let (id_c, name_c, args) = (call_id.clone(), name.clone(), acc.clone());
                    self.tools.insert(idx, (call_id, name, acc));
                    vec![json!({
                        "choices": [{"index": 0, "delta": {
                            "tool_calls": [{"index": idx, "id": id_c, "type": "function", "function": {"name": name_c, "arguments": args}}]
                        }, "finish_reason": null}]
                    })
                    .to_string()]
                }
            }

            // ── output item 添加 (text 或 function_call) ──
            "response.output_item.added" => {
                // 如果是 function_call 类型, 提前发出 role 帧 + tool_calls 初始 delta
                // 并落盘到累积器, 避免后续 delta 首次插入时 name/call_id 为空
                if let Some(item) = val.get("item") {
                    if item.get("type").and_then(|t| t.as_str()) == Some("function_call") {
                        let mut out = vec![];
                        if !self.has_started {
                            self.has_started = true;
                            out.push(json!({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}).to_string());
                        }
                        let call_id = item.get("call_id").or_else(|| item.get("id")).and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let name = item.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let idx = val.get("output_index").and_then(|i| i.as_u64()).unwrap_or(0) as usize;
                        // 落盘累积器 (若已存在则用非空值回填)
                        if let Some((eid, ename, _)) = self.tools.get_mut(&idx) {
                            if !call_id.is_empty() && eid.is_empty() { *eid = call_id.clone(); }
                            if !name.is_empty() && ename.is_empty() { *ename = name.clone(); }
                        } else if !call_id.is_empty() || !name.is_empty() {
                            self.tools.insert(idx, (call_id.clone(), name.clone(), String::new()));
                        }
                        if !call_id.is_empty() || !name.is_empty() {
                            out.push(json!({
                                "choices": [{"index": 0, "delta": {
                                    "tool_calls": [{"index": idx, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]
                                }, "finish_reason": null}]
                            }).to_string());
                        }
                        out
                    } else {
                        vec![]
                    }
                } else {
                    vec![]
                }
            }

            // ── 流结束: response.completed ──
            "response.completed" => {
                let mut out = vec![];
                if let Some(usage) = val.get("response").and_then(|r| r.get("usage")) {
                    out.push(openai_usage_from_responses(usage));
                }
                let status = val
                    .get("response")
                    .and_then(|r| r.get("status"))
                    .and_then(|s| s.as_str())
                    .unwrap_or("");
                // 若本轮产生了 tool_calls,则 finish_reason 应为 tool_calls 而非 stop
                let has_tools = !self.tools.is_empty();
                let finish_reason = if has_tools && status == "completed" {
                    "tool_calls"
                } else {
                    match status {
                        "completed" => "stop",
                        "incomplete" => "length",
                        "failed" => "stop",
                        other => other,
                    }
                };
                out.push(
                    json!({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
                        .to_string(),
                );
                out.push("[DONE]".to_string());
                out
            }
            // Responses 流的中间完成标记: 部分网关在 completed 前还会发
            // response.output_item.done / response.content_part.done / response.function_call_arguments.done
            // 这些事件本身不需转发, 但需忽略而非当 error
            "response.output_item.done"
            | "response.content_part.done"
            | "response.content_part.added"
            | "response.output_text.done"
            | "response.function_call_arguments.done"
            | "response.reasoning_summary.done"
            | "response.reasoning_summary_part.done"
            | "response.reasoning_summary_part.added"
            | "response.reasoning.done" => vec![],

            // ── 错误: 记录到 last_error, 返回空 (流结束后 poll_next 会用 last_error 记录日志) ──
            "error" | "response.failed" | "response.incomplete" => {
                let err_msg = val
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .or_else(|| val.get("error").and_then(|e| e.as_str()))
                    .unwrap_or("upstream error");
                self.last_error = Some(err_msg.to_string());
                vec![]
            }

            _ => vec![], // 其他事件忽略
        }
    }
}

impl Default for ResponsesStreamConv {
    fn default() -> Self {
        Self::new()
    }
}

// ─── 非流式响应转换: Responses API → OpenAI chat/completions ───

/// 把 Responses API 非流式响应体转换为 OpenAI chat/completions 响应体.
pub fn responses_to_openai_nonstream(body: &Value) -> Value {
    let mut content_text = String::new();
    let mut tool_calls: Vec<Value> = vec![];

    if let Some(output) = body.get("output").and_then(|o| o.as_array()) {
        for item in output {
            match item.get("type").and_then(|t| t.as_str()) {
                Some("message") => {
                    // 消息类型的 output item: 提取 content 数组中的文本
                    if let Some(content) = item.get("content").and_then(|c| c.as_array()) {
                        for part in content {
                            match part.get("type").and_then(|t| t.as_str()) {
                                Some("output_text") => {
                                    if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
                                        if !content_text.is_empty() {
                                            content_text.push('\n');
                                        }
                                        content_text.push_str(text);
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                }
                Some("function_call") => {
                    // function_call 类型的 output item
                    let id = item.get("id").and_then(|v| v.as_str())
                        .or_else(|| item.get("call_id").and_then(|v| v.as_str()))
                        .unwrap_or("");
                    let name = item.get("name").and_then(|v| v.as_str()).unwrap_or("");
                    let args = item.get("arguments").and_then(|v| v.as_str()).unwrap_or("");
                    tool_calls.push(json!({
                        "id": id,
                        "type": "function",
                        "function": {"name": name, "arguments": args}
                    }));
                }
                _ => {}
            }
        }
    }

    // 构造 OpenAI 响应
    let status = body.get("status").and_then(|s| s.as_str()).unwrap_or("");
    let finish_reason = match status {
        "completed" => "stop",
        "incomplete" => "length",
        _ => "stop",
    };

    let usage = body.get("usage").cloned().unwrap_or_else(|| json!({}));

    let finish_reason = if !tool_calls.is_empty() && finish_reason == "stop" {
        "tool_calls".to_string()
    } else {
        finish_reason.to_string()
    };

    let mut message = json!({
        "role": "assistant",
        "content": if content_text.is_empty() && !tool_calls.is_empty() { Value::Null } else { json!(content_text) },
    });
    if !tool_calls.is_empty() {
        message["tool_calls"] = Value::Array(tool_calls);
    }
    let choice = json!({
        "index": 0,
        "message": message,
        "finish_reason": finish_reason,
    });

    json!({
        "id": body.get("id").cloned().unwrap_or_else(|| json!("")),
        "object": "chat.completion",
        "choices": [choice],
        "usage": usage,
    })
}

// ─── 错误转换: Responses API → OpenAI ───

/// 把 Responses API 错误响应体转换为 OpenAI 错误格式.
pub fn responses_error_to_openai(body: &Value) -> Value {
    // Responses API 错误: {"error": {"message": "...", "type": "...", "code": "..."}}
    // 与 OpenAI 格式一致, 直接透传
    json!({
        "error": body
            .get("error")
            .cloned()
            .unwrap_or_else(|| json!({"message": body.to_string()}))
    })
}

// ─── 辅助函数 ───

/// Responses API usage → OpenAI usage payload.
fn openai_usage_from_responses(u: &Value) -> String {
    let input = u
        .get("input_tokens")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let output = u
        .get("output_tokens")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let cached = u
        .get("input_tokens_details")
        .and_then(|d| d.get("cached_tokens"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let reasoning = u
        .get("output_tokens_details")
        .and_then(|d| d.get("reasoning_tokens"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);

    let mut usage = json!({
        "prompt_tokens": input,
        "completion_tokens": output,
    });
    let mut details = Map::new();
    if cached > 0 {
        details.insert("cached_tokens".to_string(), json!(cached));
    }
    if reasoning > 0 {
        details.insert("reasoning_tokens".to_string(), json!(reasoning));
    }
    if !details.is_empty() {
        if cached > 0 {
            usage["prompt_tokens_details"] = Value::Object(details.clone());
        }
        if reasoning > 0 {
            usage["completion_tokens_details"] = Value::Object(details);
        }
    }
    json!({ "choices": [], "usage": usage }).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 转换无损性: 每类消息的大体积内容都必须原样出现在转换产物中.
    ///
    /// 背景: 排查「模型失忆反复读文件/无法完成编辑」——若历史 tool 输出、
    /// 助手正文或工具参数在 messages→input 转换时被丢弃, upstream 收到的
    /// 上下文残缺, 模型表现为重复读取已读文件、忘记自己改过什么.
    #[test]
    fn conversion_preserves_all_message_content() {
        let big_sys = "S".repeat(15_000);
        let big_user = "U".repeat(10_000);
        let big_asst = "A".repeat(20_000);
        let big_args = format!("{{\"path\":\"a.rs\",\"blob\":\"{}\"}}", "Y".repeat(30_000));
        let big_out = "X".repeat(50_000);

        let body = json!({
            "model": "m",
            "messages": [
                {"role": "system", "content": big_sys},
                {"role": "user", "content": big_user},
                {"role": "assistant", "content": big_asst},
                {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                    "function": {"name": "edit_file", "arguments": big_args}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": big_out},
            ],
        });
        let conv = openai_to_responses(&body);

        // system → instructions (完整保留)
        assert_eq!(conv["instructions"].as_str(), Some(big_sys.as_str()), "system 内容丢失");
        let input = conv["input"].as_array().unwrap();
        // user message
        let u = &input[0];
        assert_eq!(u["content"][0]["text"].as_str(), Some(big_user.as_str()), "user 内容丢失");
        // assistant 正文
        let a = &input[1];
        assert_eq!(a["content"][0]["text"].as_str(), Some(big_asst.as_str()), "assistant 正文丢失");
        // function_call arguments 完整
        let fc = &input[2];
        assert_eq!(fc["type"], "function_call");
        assert_eq!(fc["arguments"].as_str(), Some(big_args.as_str()), "工具参数丢失");
        // tool 输出完整
        let out = &input[3];
        assert_eq!(out["type"], "function_call_output");
        assert_eq!(out["output"].as_str(), Some(big_out.as_str()), "tool 输出丢失");
    }

    /// 顺序保持: 多轮 function_call/output 对的相对顺序必须与原始一致,
    /// 错序会让模型把 A 工具的结果当成 B 的, 直接破坏编辑类工具链.
    #[test]
    fn conversion_preserves_tool_pair_order() {
        let body = json!({
            "model": "m",
            "messages": [
                {"role": "user", "content": "go"},
                {"role": "assistant", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"p\":\"1\"}"}},
                    {"id": "c2", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "result-1"},
                {"role": "tool", "tool_call_id": "c2", "content": "result-2"},
                {"role": "assistant", "content": "done"},
            ],
        });
        let conv = openai_to_responses(&body);
        let input = conv["input"].as_array().unwrap();
        let kinds: Vec<String> = input.iter().map(|it| it["type"].as_str().unwrap_or("?").to_string()).collect();
        assert_eq!(kinds, vec!["message", "function_call", "function_call", "function_call_output", "function_call_output", "message"]);
        assert_eq!(input[3]["call_id"], "c1");
        assert_eq!(input[4]["call_id"], "c2");
    }
}
