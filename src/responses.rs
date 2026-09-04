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
/// - 输入侧 (`input_text`) 额外保留 `image_url` 图片部件 → `input_image`
///   (chat 格式 image_url 字段为 {url} 对象或裸字符串两种形态);
///   输出侧 (`output_text`) 仍只保留文本 — Responses API 无输出图片类型.
fn extract_and_convert_content(msg: &Value, target_type: &str) -> Value {
    match msg.get("content") {
        Some(Value::String(s)) => json!([{"type": target_type, "text": s}]),
        Some(Value::Array(parts)) => {
            let converted: Vec<Value> = parts
                .iter()
                .filter_map(|p| {
                    let ptype = p.get("type").and_then(|t| t.as_str());
                    if target_type == "input_text" && ptype == Some("image_url") {
                        let url = match p.get("image_url") {
                            Some(Value::String(s)) => Some(s.clone()),
                            Some(Value::Object(o)) => o.get("url").and_then(|u| u.as_str()).map(|s| s.to_string()),
                            _ => None,
                        };
                        return url.map(|u| json!({"type": "input_image", "image_url": u}));
                    }
                    let text = match ptype {
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

// ─── 请求转换: Responses API → OpenAI chat/completions (/v1/responses 入口) ───

/// 把客户端 Responses API 请求体转换为 OpenAI chat/completions 请求体.
///
/// `/v1/responses` 入口在上游为 OpenAI / Anthropic 协议时, 先把客户端请求
/// 规范化为 chat 请求, 复用 [`crate::proxy::chat_completions`] 整条管线
/// (含 anthropic/responses 上游转换、熔断、缓存、自动续写), 出口再译回 Responses.
///
/// 保真约束:
/// - 有状态引用 (`previous_response_id` / `item_reference`) 依赖服务端会话存储,
///   中转网关无状态, 必须**显式报错**而非静默忽略 — 否则客户端误以为历史生效.
/// - 无法表达的 input 部件保留为 JSON 文本, 不静默丢弃.
pub fn responses_to_openai_request(body: &Value) -> Result<Value, String> {
    if let Some(prev) = body.get("previous_response_id").and_then(|v| v.as_str()) {
        if !prev.trim().is_empty() {
            return Err(format!(
                "previous_response_id '{prev}' requires server-side conversation state, which this relay does not provide. Send the full conversation history in `input` instead."
            ));
        }
    }

    let mut out = Map::new();
    if let Some(m) = body.get("model") {
        out.insert("model".to_string(), m.clone());
    }

    let mut messages: Vec<Value> = Vec::new();

    // instructions → 顶部 system 消息
    if let Some(instr) = body.get("instructions").and_then(|i| i.as_str()) {
        if !instr.trim().is_empty() {
            messages.push(json!({"role": "system", "content": instr}));
        }
    }

    match body.get("input") {
        // 字符串形态 → 单条 user 消息
        Some(Value::String(s)) => {
            messages.push(json!({"role": "user", "content": s}));
        }
        Some(Value::Array(items)) => {
            for item in items {
                convert_input_item(item, &mut messages)?;
            }
        }
        _ => {}
    }
    out.insert("messages".to_string(), Value::Array(messages));

    // max_output_tokens → max_tokens
    if let Some(mt) = body.get("max_output_tokens") {
        if !mt.is_null() {
            out.insert("max_tokens".to_string(), mt.clone());
        }
    }

    // tools: Responses 平铺 {type:"function", name, description, parameters, strict}
    // → chat 嵌套 {type:"function", function:{name, ...}}
    if let Some(tools) = body.get("tools").and_then(|t| t.as_array()) {
        let converted: Vec<Value> = tools
            .iter()
            .filter_map(|tool| {
                let name = tool.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
                if name.is_empty() {
                    return None;
                }
                let mut func = Map::new();
                func.insert("name".to_string(), json!(name));
                if let Some(d) = tool.get("description") {
                    func.insert("description".to_string(), d.clone());
                }
                if let Some(p) = tool.get("parameters") {
                    func.insert("parameters".to_string(), p.clone());
                }
                let mut t = Map::new();
                t.insert("type".to_string(), json!("function"));
                t.insert("function".to_string(), Value::Object(func));
                Some(Value::Object(t))
            })
            .collect();
        if !converted.is_empty() {
            out.insert("tools".to_string(), Value::Array(converted));
        }
    }

    // tool_choice: 字符串枚举透传; 指定函数形态 {type:"function","name":x} → chat 嵌套
    if let Some(tc) = body.get("tool_choice") {
        match tc {
            Value::String(_) => {
                out.insert("tool_choice".to_string(), tc.clone());
            }
            Value::Object(o) => {
                if o.get("type").and_then(|t| t.as_str()) == Some("function") {
                    if let Some(name) = o.get("name").and_then(|n| n.as_str()) {
                        out.insert(
                            "tool_choice".to_string(),
                            json!({"type": "function", "function": {"name": name}}),
                        );
                    }
                }
            }
            _ => {}
        }
    }

    // reasoning.effort → reasoning_effort (客户端档位优先; inject_model_params 只补缺不覆盖)
    if let Some(effort) = body
        .get("reasoning")
        .and_then(|r| r.get("effort"))
        .and_then(|e| e.as_str())
    {
        if !effort.is_empty() {
            out.insert("reasoning_effort".to_string(), json!(effort));
        }
    }

    for key in ["temperature", "top_p", "stream", "stop", "user", "parallel_tool_calls"] {
        if let Some(v) = body.get(key) {
            if !v.is_null() {
                out.insert(key.to_string(), v.clone());
            }
        }
    }

    Ok(Value::Object(out))
}

/// 转换单个 Responses input item → chat messages (可产生 0..1 条).
fn convert_input_item(item: &Value, messages: &mut Vec<Value>) -> Result<(), String> {
    let itype = item.get("type").and_then(|t| t.as_str()).unwrap_or("");
    match itype {
        // 旧版客户端 input item 可能不带 type (仅 {role, content})
        "message" | "" => {
            let role = item.get("role").and_then(|r| r.as_str()).unwrap_or("user");
            // developer → system (chat 规范以 system 最为通用)
            let role = if role == "developer" { "system" } else { role };
            match role {
                "system" | "user" | "assistant" => {
                    let content = convert_content_parts(item.get("content"), role);
                    messages.push(json!({"role": role, "content": content}));
                }
                other => {
                    return Err(format!("unsupported input item role '{other}'"));
                }
            }
        }
        "function_call" => {
            let name = item
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if name.is_empty() {
                return Err("function_call item missing tool name".to_string());
            }
            let call_id = item
                .get("call_id")
                .or_else(|| item.get("id"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            let args = match item.get("arguments") {
                Some(Value::String(s)) => s.clone(),
                Some(v) if !v.is_null() => serde_json::to_string(v).unwrap_or_default(),
                _ => String::new(),
            };
            messages.push(json!({
                "role": "assistant",
                "content": Value::Null,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}],
            }));
        }
        "function_call_output" => {
            let call_id = item
                .get("call_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if call_id.is_empty() {
                return Err("function_call_output item missing call_id".to_string());
            }
            let output = match item.get("output") {
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
                _ => String::new(),
            };
            messages.push(json!({"role": "tool", "tool_call_id": call_id, "content": output}));
        }
        "reasoning" => {
            // 历史 reasoning item → assistant reasoning_content (思考链),
            // 是否转发给上游由 strip_history_reasoning 配置统一控制, 不在此静默丢弃.
            let mut text = String::new();
            for key in ["summary", "content"] {
                if let Some(Value::Array(parts)) = item.get(key) {
                    for p in parts {
                        if let Some(t) = p.get("text").and_then(|v| v.as_str()) {
                            if !text.is_empty() {
                                text.push('\n');
                            }
                            text.push_str(t);
                        }
                    }
                }
            }
            if text.is_empty() {
                return Ok(());
            }
            messages.push(json!({"role": "assistant", "content": Value::Null, "reasoning_content": text}));
        }
        "item_reference" => {
            let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("");
            return Err(format!(
                "item_reference '{id}' requires server-side conversation state, which this relay does not provide. Send the full conversation history in `input` instead."
            ));
        }
        other => {
            return Err(format!(
                "unsupported input item type '{other}' (relay only supports message / function_call / function_call_output / reasoning)"
            ));
        }
    }
    Ok(())
}

/// Responses message content → chat content.
///
/// string 原样; 数组逐部件映射: input_text/output_text → text,
/// input_image → image_url {url}, refusal → 文本;
/// 未知部件保留为 JSON 文本 (不静默丢弃).
/// assistant 角色输出纯字符串 (chat 规范下兼容性最佳, 部分上游拒绝 assistant 数组);
/// 其余角色输出部件数组 (user 消息需保留图片等多部件).
fn convert_content_parts(content: Option<&Value>, role: &str) -> Value {
    let as_string = role == "assistant";
    match content {
        Some(Value::String(s)) => json!(s),
        Some(Value::Array(parts)) => {
            let mut out: Vec<Value> = Vec::new();
            let mut texts: Vec<String> = Vec::new();
            for p in parts {
                let ptype = p.get("type").and_then(|t| t.as_str()).unwrap_or("");
                match ptype {
                    "input_text" | "output_text" | "text" => {
                        let t = p.get("text").and_then(|v| v.as_str()).unwrap_or("");
                        if !t.is_empty() {
                            if as_string {
                                texts.push(t.to_string());
                            } else {
                                out.push(json!({"type": "text", "text": t}));
                            }
                        }
                    }
                    "input_image" => {
                        if as_string {
                            continue; // assistant 无图片部件
                        }
                        let url = match p.get("image_url") {
                            Some(Value::String(s)) => Some(s.clone()),
                            Some(Value::Object(o)) => o.get("url").and_then(|v| v.as_str()).map(|s| s.to_string()),
                            _ => None,
                        };
                        if let Some(u) = url {
                            out.push(json!({"type": "image_url", "image_url": {"url": u}}));
                        }
                    }
                    "refusal" => {
                        let t = p.get("refusal").and_then(|v| v.as_str()).unwrap_or("");
                        if !t.is_empty() {
                            if as_string {
                                texts.push(t.to_string());
                            } else {
                                out.push(json!({"type": "text", "text": t}));
                            }
                        }
                    }
                    _ => {
                        if !p.is_null() {
                            let s = serde_json::to_string(p).unwrap_or_default();
                            if !s.is_empty() {
                                if as_string {
                                    texts.push(s);
                                } else {
                                    out.push(json!({"type": "text", "text": s}));
                                }
                            }
                        }
                    }
                }
            }
            if as_string {
                if texts.is_empty() {
                    Value::Null
                } else {
                    json!(texts.join("\n"))
                }
            } else if out.is_empty() {
                Value::Null
            } else {
                Value::Array(out)
            }
        }
        _ => Value::Null,
    }
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

            // ── 错误: 提取详情记入 last_error (流结束后记录日志用), 并向客户端
            //    转发 OpenAI 风格错误帧 — 否则客户端只看到静默的空响应/length 收尾,
            //    完全无法感知失败原因 (实测 muse 复杂回合 response.failed 后
            //    CodeBuddy 表现为"永远不改文件"的空转).
            //    注意错误位置因事件类型而异: error 事件在顶层 error.{message,code},
            //    response.failed 嵌在 response.error.{message,code}.
            "error" | "response.failed" => {
                let err_msg = extract_upstream_error_detail(val).unwrap_or_else(|| "upstream error".to_string());
                let status = val.get("response").and_then(|r| r.get("status")).and_then(|s| s.as_str());
                let full = match status {
                    Some(st) => format!("{err_msg} [status={st}]"),
                    None => err_msg,
                };
                self.last_error = Some(full.clone());
                vec![json!({"error": {"message": full, "type": "upstream_error"}}).to_string()]
            }
            // response.incomplete 是"正常截断"而非失败 (如 max_output_tokens 用尽),
            // 不向客户端发错误帧 — 但它是流的【终止事件】, 必须补发 finish_reason=length
            // + usage + [DONE], 否则客户端收不到终止帧 (表现为 finish_reason:null / 卡死等待),
            // 且 proxy 会误判为"非干净断流"触发自动续写 (对 max_tokens 截断续写是错的).
            "response.incomplete" => {
                let reason = extract_upstream_error_detail(val)
                    .unwrap_or_else(|| "max_output_tokens".to_string());
                self.last_error = Some(reason);
                let mut out = vec![];
                if let Some(usage) = val.get("response").and_then(|r| r.get("usage")) {
                    out.push(openai_usage_from_responses(usage));
                }
                out.push(
                    json!({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
                        .to_string(),
                );
                out.push("[DONE]".to_string());
                out
            }
            // response.cancelled: 请求被取消, 同样作为终止事件补发收尾帧 (finish_reason=stop),
            // 避免客户端等待不存在的 completed.
            "response.cancelled" => {
                let mut out = vec![];
                out.push(
                    json!({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                        .to_string(),
                );
                out.push("[DONE]".to_string());
                out
            }

            _ => vec![], // 其他事件忽略
        }
    }
}

/// 从 Responses 错误类事件中提取人类可读的错误详情.
/// 覆盖三种形态:
/// - error 事件: 顶层 `error.message` 或 `error` 本身是字符串
/// - response.failed: 嵌套 `response.error.message` / `response.error.code`
/// - response.incomplete: `response.incomplete_details.reason`
fn extract_upstream_error_detail(val: &Value) -> Option<String> {
    let resp = val.get("response");
    val.get("error")
        .and_then(|e| e.get("message"))
        .and_then(|m| m.as_str())
        .or_else(|| val.get("error").and_then(|e| e.as_str()))
        .or_else(|| {
            resp.and_then(|r| r.get("error"))
                .and_then(|e| e.get("message"))
                .and_then(|m| m.as_str())
        })
        .or_else(|| {
            resp.and_then(|r| r.get("error"))
                .and_then(|e| e.get("code"))
                .and_then(|c| c.as_str())
        })
        .or_else(|| {
            resp.and_then(|r| r.get("incomplete_details"))
                .and_then(|d| d.get("reason"))
                .and_then(|s| s.as_str())
        })
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty())
}

impl Default for ResponsesStreamConv {
    fn default() -> Self {
        Self::new()
    }
}

// ─── 流式响应转换: OpenAI chat/completions → Responses API (/v1/responses 出口) ───

/// OpenAI chat/completions SSE → Responses API SSE 的有状态转换器.
///
/// `/v1/responses` 入口在上游为 OpenAI / Anthropic 协议时, chat_completions
/// 管线产出的 OpenAI SSE 需译回 Responses 事件流. `feed` 输入一行 chat data
/// payload (不含 "data: " 前缀), 返回 0..N 条 `(事件名, data JSON)` 对;
/// 事件名为空串表示仅输出 data 行 (如 `[DONE]` 哨兵, 不带 `event:` 行).
///
/// 事件序列对齐 OpenAI 官方 Responses 事件流:
/// `response.created → output_item.added → (delta…) → output_item.done → response.completed`.
/// 终止事件延迟到 usage 帧之后 ([DONE] / [`Self::finalize`]) 统一收尾,
/// 保证 `response.completed.response.usage` 携带 token 计数.
pub struct ChatToResponsesStreamConv {
    resp_id: String,
    model: String,
    created_at: i64,
    started: bool,
    done_emitted: bool,
    /// finish_reason 已到但终止事件延迟到 usage 帧后统一发.
    pending_finish: Option<String>,
    usage: Option<Value>,
    /// reasoning item: 是否已添加 + 累积文本 + output_index.
    reasoning_open: bool,
    reasoning_text: String,
    reasoning_index: Option<u64>,
    /// message item: 是否已添加 + 累积文本 + output_index.
    msg_open: bool,
    msg_text: String,
    msg_index: Option<u64>,
    /// chat tool_calls index → 状态.
    tools: HashMap<usize, ToolState>,
    next_output_index: u64,
}

struct ToolState {
    output_index: u64,
    call_id: String,
    name: String,
    args: String,
    added: bool,
}

impl ChatToResponsesStreamConv {
    pub fn new() -> Self {
        Self {
            resp_id: "resp_0".to_string(),
            model: String::new(),
            created_at: 0,
            started: false,
            done_emitted: false,
            pending_finish: None,
            usage: None,
            reasoning_open: false,
            reasoning_text: String::new(),
            reasoning_index: None,
            msg_open: false,
            msg_text: String::new(),
            msg_index: None,
            tools: HashMap::new(),
            next_output_index: 0,
        }
    }

    /// 输入一行 chat SSE data payload, 返回要写出的 (事件名, data JSON) 列表.
    pub fn feed(&mut self, payload: &str) -> Vec<(String, String)> {
        if self.done_emitted {
            return vec![];
        }
        let payload = payload.trim();
        if payload == "[DONE]" {
            return self.emit_closure();
        }
        let Ok(v) = serde_json::from_str::<Value>(payload) else {
            return vec![];
        };

        // 错误帧: {"error": {...}} → response.failed (客户端可感知失败原因, 不静默)
        if v.get("error").map(|e| e.is_object() || e.is_string()).unwrap_or(false) {
            self.done_emitted = true;
            let msg = v["error"]
                .get("message")
                .and_then(|m| m.as_str())
                .map(|s| s.to_string())
                .unwrap_or_default();
            let msg = if msg.is_empty() {
                serde_json::to_string(&v["error"]).unwrap_or_default()
            } else {
                msg
            };
            let resp = json!({
                "id": self.resp_id, "object": "response", "created_at": self.created_at,
                "status": "failed", "error": {"code": "upstream_error", "message": msg},
                "model": self.model, "output": [],
            });
            return vec![("response.failed".to_string(), json!({"response": resp}).to_string())];
        }

        // usage 帧 (include_usage 末帧 choices 为空 / deepseek 末帧随 finish_reason 携带)
        if let Some(u) = v.get("usage") {
            if u.is_object() {
                self.usage = Some(u.clone());
            }
        }

        let Some(choice) = v.get("choices").and_then(|c| c.as_array()).and_then(|a| a.first()) else {
            return vec![];
        };
        let mut out: Vec<(String, String)> = Vec::new();

        // 首帧: response.created + response.in_progress (此时 output 尚未产生, 为空数组)
        if !self.started {
            self.started = true;
            if let Some(id) = v.get("id").and_then(|x| x.as_str()) {
                if !id.is_empty() {
                    self.resp_id = format!("resp_{id}");
                }
            }
            if let Some(m) = v.get("model").and_then(|x| x.as_str()) {
                if !m.is_empty() {
                    self.model = m.to_string();
                }
            }
            if let Some(c) = v.get("created").and_then(|x| x.as_i64()) {
                self.created_at = c;
            }
            let sk = json!({"response": self.skeleton_created()});
            out.push(("response.created".to_string(), sk.to_string()));
            out.push(("response.in_progress".to_string(), sk.to_string()));
        }

        let delta = choice.get("delta").cloned().unwrap_or(Value::Null);

        // reasoning_content 增量 → reasoning item + reasoning_summary_text.delta
        if let Some(rc) = delta.get("reasoning_content").and_then(|x| x.as_str()) {
            if !rc.is_empty() {
                if !self.reasoning_open {
                    self.reasoning_open = true;
                    self.reasoning_index = Some(self.alloc_output_index());
                    let oi = self.reasoning_index.unwrap();
                    out.push((
                        "response.output_item.added".to_string(),
                        json!({
                            "output_index": oi,
                            "item": {"type": "reasoning", "id": format!("rs_{oi}"), "summary": [], "status": "in_progress"},
                        }).to_string(),
                    ));
                    out.push((
                        "response.reasoning_summary_part.added".to_string(),
                        json!({
                            "item_id": format!("rs_{oi}"), "summary_index": 0,
                            "part": {"type": "summary_text", "text": ""},
                        }).to_string(),
                    ));
                }
                let oi = self.reasoning_index.unwrap_or(0);
                self.reasoning_text.push_str(rc);
                out.push((
                    "response.reasoning_summary_text.delta".to_string(),
                    json!({"item_id": format!("rs_{oi}"), "summary_index": 0, "delta": rc}).to_string(),
                ));
            }
        }

        // content 增量 → message item + output_text.delta
        if let Some(c) = delta.get("content").and_then(|x| x.as_str()) {
            if !c.is_empty() {
                if !self.msg_open {
                    self.msg_open = true;
                    self.msg_index = Some(self.alloc_output_index());
                    let oi = self.msg_index.unwrap();
                    out.push((
                        "response.output_item.added".to_string(),
                        json!({
                            "output_index": oi,
                            "item": {"type": "message", "id": format!("msg_{oi}"), "role": "assistant", "status": "in_progress", "content": []},
                        }).to_string(),
                    ));
                    out.push((
                        "response.content_part.added".to_string(),
                        json!({
                            "item_id": format!("msg_{oi}"), "output_index": oi, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        }).to_string(),
                    ));
                }
                let oi = self.msg_index.unwrap_or(0);
                self.msg_text.push_str(c);
                out.push((
                    "response.output_text.delta".to_string(),
                    json!({"item_id": format!("msg_{oi}"), "output_index": oi, "content_index": 0, "delta": c}).to_string(),
                ));
            }
        }

        // tool_calls 增量 → function_call item + function_call_arguments.delta
        if let Some(calls) = delta.get("tool_calls").and_then(|x| x.as_array()) {
            for call in calls {
                let idx = call.get("index").and_then(|i| i.as_u64()).unwrap_or(0) as usize;
                let args_delta = call
                    .get("function")
                    .and_then(|f| f.get("arguments"))
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();
                if !self.tools.contains_key(&idx) {
                    let st = ToolState {
                        output_index: self.alloc_output_index(),
                        call_id: call.get("id").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        name: call
                            .get("function")
                            .and_then(|f| f.get("name"))
                            .and_then(|n| n.as_str())
                            .unwrap_or("")
                            .to_string(),
                        args: String::new(),
                        added: false,
                    };
                    self.tools.insert(idx, st);
                }
                let st = self.tools.get_mut(&idx).unwrap();
                if let Some(cid) = call.get("id").and_then(|x| x.as_str()) {
                    if !cid.is_empty() && st.call_id.is_empty() {
                        st.call_id = cid.to_string();
                    }
                }
                if let Some(n) = call.get("function").and_then(|f| f.get("name")).and_then(|x| x.as_str()) {
                    if !n.is_empty() && st.name.is_empty() {
                        st.name = n.to_string();
                    }
                }
                st.args.push_str(&args_delta);
                let oi = st.output_index;
                let item_id = format!("fc_{oi}");
                if !st.added {
                    st.added = true;
                    out.push((
                        "response.output_item.added".to_string(),
                        json!({
                            "output_index": oi,
                            "item": {"type": "function_call", "id": item_id, "call_id": st.call_id.clone(),
                                     "name": st.name.clone(), "arguments": "", "status": "in_progress"},
                        }).to_string(),
                    ));
                }
                if !args_delta.is_empty() {
                    out.push((
                        "response.function_call_arguments.delta".to_string(),
                        json!({"item_id": item_id, "output_index": oi, "delta": args_delta}).to_string(),
                    ));
                }
            }
        }

        // finish_reason: 记下, 收尾统一延迟 (usage 帧可能紧随其后)
        if let Some(fr) = choice.get("finish_reason").and_then(|x| x.as_str()) {
            if !fr.is_empty() {
                self.pending_finish = Some(fr.to_string());
            }
        }
        out
    }

    /// 流结束时兜底收尾: 即使上游未发 [DONE], 也要让客户端收到终止事件.
    pub fn finalize(&mut self) -> Vec<(String, String)> {
        self.emit_closure()
    }

    fn alloc_output_index(&mut self) -> u64 {
        let i = self.next_output_index;
        self.next_output_index += 1;
        i
    }

    /// 终止序列: 各 item 的 done 事件 (按 output_index 排序) + 终止事件 + [DONE].
    fn emit_closure(&mut self) -> Vec<(String, String)> {
        if self.done_emitted {
            return vec![];
        }
        self.done_emitted = true;
        let finish = self.pending_finish.take().unwrap_or_else(|| "stop".to_string());
        let usage = self.usage.take();

        // 先克隆 tool 状态 (终止骨架与 done 事件共用), 再按 output_index 排序.
        let mut fc_items: Vec<(u64, String, String, String)> = self
            .tools
            .values()
            .map(|st| (st.output_index, st.call_id.clone(), st.name.clone(), st.args.clone()))
            .collect();
        fc_items.sort_by_key(|(oi, _, _, _)| *oi);

        let mut seq: Vec<(u64, String, String)> = Vec::new();
        if self.reasoning_open {
            let oi = self.reasoning_index.unwrap_or(0);
            let item_id = format!("rs_{oi}");
            let text = self.reasoning_text.clone();
            seq.push((oi, "response.reasoning_summary_text.done".to_string(), json!({
                "item_id": item_id, "summary_index": 0, "text": text,
            }).to_string()));
            seq.push((oi, "response.reasoning_summary_part.done".to_string(), json!({
                "item_id": item_id, "summary_index": 0, "part": {"type": "summary_text", "text": text},
            }).to_string()));
            seq.push((oi, "response.output_item.done".to_string(), json!({
                "output_index": oi,
                "item": {"type": "reasoning", "id": item_id, "summary": [{"type": "summary_text", "text": text}], "status": "completed"},
            }).to_string()));
        }
        if self.msg_open {
            let oi = self.msg_index.unwrap_or(0);
            let item_id = format!("msg_{oi}");
            let text = self.msg_text.clone();
            seq.push((oi, "response.output_text.done".to_string(), json!({
                "item_id": item_id.clone(), "output_index": oi, "content_index": 0, "text": text,
            }).to_string()));
            seq.push((oi, "response.content_part.done".to_string(), json!({
                "item_id": item_id.clone(), "output_index": oi, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }).to_string()));
            seq.push((oi, "response.output_item.done".to_string(), json!({
                "output_index": oi,
                "item": {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": text, "annotations": []}]},
            }).to_string()));
        }
        for (oi, call_id, name, args) in &fc_items {
            let item_id = format!("fc_{oi}");
            seq.push((*oi, "response.function_call_arguments.done".to_string(), json!({
                "item_id": item_id.clone(), "output_index": oi, "arguments": args,
            }).to_string()));
            seq.push((*oi, "response.output_item.done".to_string(), json!({
                "output_index": oi,
                "item": {"type": "function_call", "id": item_id, "call_id": call_id, "name": name,
                         "arguments": args, "status": "completed"},
            }).to_string()));
        }
        seq.sort_by_key(|(oi, _, _)| *oi);
        let mut out: Vec<(String, String)> = seq.into_iter().map(|(_, ev, data)| (ev, data)).collect();

        // 终止事件: length → response.incomplete (官方截断语义), 其余 → response.completed
        let (ev_name, status, incomplete) = if finish == "length" {
            ("response.incomplete", "incomplete", Some(json!({"reason": "max_output_tokens"})))
        } else {
            ("response.completed", "completed", None)
        };
        // 终止骨架的 usage 需逆映射为 Responses 形态 (input_tokens/output_tokens + details)
        let usage_mapped = usage.map(|u| openai_usage_to_responses(&u)).unwrap_or(Value::Null);
        out.push((
            ev_name.to_string(),
            json!({"response": self.skeleton_final(status, usage_mapped, incomplete, fc_items)}).to_string(),
        ));
        // 哨兵: 仅 data 行, 不带 event: (Responses 生态流以 [DONE] 收尾)
        out.push((String::new(), "[DONE]".to_string()));
        out
    }

    /// 流开始时的 response 骨架 (output 尚未产生, 为空数组 — 与官方一致).
    fn skeleton_created(&self) -> Value {
        json!({
            "id": self.resp_id, "object": "response", "created_at": self.created_at,
            "status": "in_progress", "error": Value::Null, "incomplete_details": Value::Null,
            "model": self.model, "output": [],
        })
    }

    /// 终止时的 response 骨架 (output 含全部 item, usage 携带计数).
    fn skeleton_final(
        &self,
        status: &str,
        usage: Value,
        incomplete: Option<Value>,
        fc_items: Vec<(u64, String, String, String)>,
    ) -> Value {
        let mut output: Vec<Value> = Vec::new();
        if self.reasoning_open {
            output.push(json!({
                "type": "reasoning", "id": format!("rs_{}", self.reasoning_index.unwrap_or(0)),
                "summary": [{"type": "summary_text", "text": self.reasoning_text}], "status": "completed",
            }));
        }
        if self.msg_open {
            output.push(json!({
                "type": "message", "id": format!("msg_{}", self.msg_index.unwrap_or(0)),
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": self.msg_text, "annotations": []}],
            }));
        }
        for (oi, call_id, name, args) in &fc_items {
            output.push(json!({
                "type": "function_call", "id": format!("fc_{oi}"),
                "call_id": call_id, "name": name, "arguments": args, "status": "completed",
            }));
        }
        json!({
            "id": self.resp_id, "object": "response", "created_at": self.created_at,
            "status": status, "error": Value::Null,
            "incomplete_details": incomplete.unwrap_or(Value::Null),
            "model": self.model, "output": output,
            "usage": usage,
        })
    }
}

impl Default for ChatToResponsesStreamConv {
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

    let usage = map_responses_usage_to_openai(body.get("usage").unwrap_or(&Value::Null));

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

// ─── 非流式响应转换: OpenAI chat/completions → Responses API (/v1/responses 出口) ───

/// 把 OpenAI chat/completions 非流式响应体转换为 Responses API response 对象.
///
/// 与 [`responses_to_openai_nonstream`] 互为逆向: `/v1/responses` 入口在上游为
/// OpenAI / Anthropic 协议时, chat_completions 管线产物需译回 Responses 形态.
pub fn openai_to_responses_nonstream(body: &Value) -> Value {
    let mut output: Vec<Value> = Vec::new();
    let mut status = "completed";
    let mut incomplete_details: Option<Value> = None;
    let mut output_index = 0u64;

    if let Some(choice) = body.get("choices").and_then(|c| c.as_array()).and_then(|a| a.first()) {
        let msg = choice.get("message").cloned().unwrap_or(Value::Null);
        // reasoning_content → reasoning item (summary_text 形态)
        if let Some(rc) = msg.get("reasoning_content").and_then(|v| v.as_str()) {
            if !rc.is_empty() {
                output.push(json!({
                    "type": "reasoning", "id": format!("rs_{output_index}"),
                    "summary": [{"type": "summary_text", "text": rc}],
                    "status": "completed",
                }));
                output_index += 1;
            }
        }
        // content → message item (output_text)
        let text = msg.get("content").and_then(|c| c.as_str()).unwrap_or("");
        if !text.is_empty() {
            output.push(json!({
                "type": "message", "id": format!("msg_{output_index}"),
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }));
            output_index += 1;
        }
        // tool_calls → function_call items
        if let Some(calls) = msg.get("tool_calls").and_then(|t| t.as_array()) {
            for call in calls {
                let id = call.get("id").and_then(|v| v.as_str()).unwrap_or("");
                let name = call
                    .get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let args = call
                    .get("function")
                    .and_then(|f| f.get("arguments"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                output.push(json!({
                    "type": "function_call", "id": format!("fc_{output_index}_{id}"),
                    "call_id": id, "name": name, "arguments": args, "status": "completed",
                }));
                output_index += 1;
            }
        }
        if choice.get("finish_reason").and_then(|v| v.as_str()) == Some("length") {
            status = "incomplete";
            incomplete_details = Some(json!({"reason": "max_output_tokens"}));
        }
    }

    let mut out = json!({
        "id": format!("resp_{}", body.get("id").and_then(|v| v.as_str()).unwrap_or("0")),
        "object": "response",
        "created_at": body.get("created").and_then(|v| v.as_i64()).unwrap_or(0),
        "status": status,
        "error": Value::Null,
        "incomplete_details": incomplete_details.clone().unwrap_or(Value::Null),
        "model": body.get("model").cloned().unwrap_or(Value::Null),
        "output": output,
    });
    if let Some(u) = body.get("usage") {
        if u.is_object() && !u.as_object().map(|o| o.is_empty()).unwrap_or(true) {
            out["usage"] = openai_usage_to_responses(u);
        }
    }
    out
}

/// OpenAI usage → Responses usage (逆向映射).
///
/// prompt_tokens/completion_tokens (+ details) → input_tokens/output_tokens
/// (+ input_tokens_details.cached_tokens / output_tokens_details.reasoning_tokens).
fn openai_usage_to_responses(u: &Value) -> Value {
    let prompt = u.get("prompt_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let completion = u.get("completion_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let mut out = json!({
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt + completion,
    });
    if let Some(cached) = u
        .get("prompt_tokens_details")
        .and_then(|d| d.get("cached_tokens"))
        .and_then(|v| v.as_u64())
    {
        out["input_tokens_details"]["cached_tokens"] = json!(cached);
    }
    if let Some(reasoning) = u
        .get("completion_tokens_details")
        .and_then(|d| d.get("reasoning_tokens"))
        .and_then(|v| v.as_u64())
    {
        out["output_tokens_details"]["reasoning_tokens"] = json!(reasoning);
    }
    out
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

/// Responses API usage → OpenAI usage 对象 (非流式响应体用).
///
/// 上游按 Responses 规范用 `input_tokens` / `output_tokens` (+ `input_tokens_details.cached_tokens`
/// / `output_tokens_details.reasoning_tokens`), 而 OpenAI 客户端与本地日志按
/// `prompt_tokens` / `completion_tokens` 读取 — 不映射会导致非流式响应 token 计数为 0.
/// 若上游已返回 OpenAI 风格字段 (含 prompt_tokens), 原样透传.
fn map_responses_usage_to_openai(u: &Value) -> Value {
    let obj = match u.as_object() {
        Some(o) if !o.is_empty() => o,
        _ => return json!({}),
    };
    // 已是 OpenAI 风格 → 透传.
    if obj.contains_key("prompt_tokens") || obj.contains_key("completion_tokens") {
        return u.clone();
    }
    let input = obj.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let output = obj.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let mut out = json!({
        "prompt_tokens": input,
        "completion_tokens": output,
        "total_tokens": input + output,
    });
    if let Some(cached) = obj
        .get("input_tokens_details")
        .and_then(|d| d.get("cached_tokens"))
        .and_then(|v| v.as_u64())
    {
        if cached > 0 {
            out["prompt_tokens_details"] = json!({"cached_tokens": cached});
        }
    }
    if let Some(reasoning) = obj
        .get("output_tokens_details")
        .and_then(|d| d.get("reasoning_tokens"))
        .and_then(|v| v.as_u64())
    {
        if reasoning > 0 {
            out["completion_tokens_details"] = json!({"reasoning_tokens": reasoning});
        }
    }
    out
}

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
    // 分别构造 prompt/completion 的 details, 避免同一对象交叉污染两个字段.
    if cached > 0 {
        usage["prompt_tokens_details"] = json!({"cached_tokens": cached});
    }
    if reasoning > 0 {
        usage["completion_tokens_details"] = json!({"reasoning_tokens": reasoning});
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

    /// 用户消息中的图片部件必须保留: chat `image_url` → Responses `input_image`.
    ///
    /// 背景: 排查「Responses 模型无法用 tool 改文件」时发现转换落差为恒定
    /// 冻结块 — 若会话历史含图片, 旧版在 extract_and_convert_content 中把
    /// 非 text 部件整体丢弃, 带图请求模型全程"失明".
    #[test]
    fn conversion_preserves_user_image_parts() {
        let data_uri = format!("data:image/png;base64,{}", "Z".repeat(100_000));
        let body = json!({
            "model": "m",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "照这张图改 UI"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "image_url", "image_url": "data:image/jpeg;base64,QkFTRTY0"},
                ]},
            ],
        });
        let conv = openai_to_responses(&body);
        let content = conv["input"][0]["content"].as_array().unwrap();
        assert_eq!(content.len(), 3, "图片部件被丢弃");
        assert_eq!(content[0]["type"], "input_text");
        assert_eq!(content[1]["type"], "input_image");
        // 对象形态: 大 base64 data URI 必须逐字节保留
        assert_eq!(content[1]["image_url"].as_str(), Some(data_uri.as_str()), "对象形态 image_url 丢失");
        // 裸字符串形态
        assert_eq!(content[2]["type"], "input_image");
        assert_eq!(content[2]["image_url"], "data:image/jpeg;base64,QkFTRTY0");
    }

    /// assistant 输出侧不产出 input_image (Responses API 无输出图片类型),
    /// 非 text 部件仍按原行为跳过, 不影响纯文本正文.
    #[test]
    fn assistant_output_side_drops_non_text_parts() {
        let msg = json!({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "正文"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        });
        let out = convert_output_content_types(&msg);
        let arr = out.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["type"], "output_text");
        assert_eq!(arr[0]["text"], "正文");
    }

    /// response.failed 的错误详情嵌在 response.error 下 (非顶层), 必须被提取,
    /// 且客户端要收到 OpenAI 风格错误帧 — 否则表现为静默空响应.
    /// 背景: muse 复杂回合连续 4 次生成 ~38.7K tok 后 response.failed,
    /// 旧版只记 "upstream error" 且不转发, CodeBuddy 端表现为"永远不改文件".
    #[test]
    fn response_failed_surfaces_detail_and_emits_error_frame() {
        let mut conv = ResponsesStreamConv::new();
        let out = conv.feed_line(
            r#"data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"generation limit exceeded"}}}"#,
        );
        assert_eq!(out.len(), 1, "必须向客户端转发错误帧");
        let frame: Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(frame["error"]["type"], "upstream_error");
        assert!(
            frame["error"]["message"].as_str().unwrap().contains("generation limit exceeded"),
            "错误帧缺少上游详情: {}", out[0]
        );
        let last = conv.last_error.clone().unwrap();
        assert!(last.contains("generation limit exceeded") && last.contains("status=failed"), "last_error={last}");
    }

    /// response.incomplete 是正常截断 (如 max_output_tokens 用尽): 不发错误帧,
    /// 但必须作为终止事件补发 finish_reason=length + [DONE] (否则客户端收不到收尾帧),
    /// 并把 reason 记入 last_error 供日志定位.
    #[test]
    fn response_incomplete_emits_length_finish_and_done() {
        let mut conv = ResponsesStreamConv::new();
        let out = conv.feed_line(
            r#"data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"usage":{"input_tokens":100,"output_tokens":50}}}"#,
        );
        // 无错误帧, 但必须有 length 收尾帧 + [DONE]
        assert!(!out.iter().any(|s| s.contains("\"error\"")), "截断不应发错误帧: {out:?}");
        assert!(out.iter().any(|s| s.contains("\"finish_reason\":\"length\"")), "缺 length 收尾帧: {out:?}");
        assert_eq!(out.last().map(|s| s.as_str()), Some("[DONE]"), "缺 [DONE] 终止帧");
        assert!(out.iter().any(|s| s.contains("\"prompt_tokens\":100")), "缺 usage 帧: {out:?}");
        assert_eq!(conv.last_error.as_deref(), Some("max_output_tokens"));
    }

    /// response.cancelled 也作为终止事件补发 stop 收尾帧 + [DONE].
    #[test]
    fn response_cancelled_emits_stop_finish() {
        let mut conv = ResponsesStreamConv::new();
        let out = conv.feed_line(r#"data: {"type":"response.cancelled","response":{"status":"cancelled"}}"#);
        assert!(out.iter().any(|s| s.contains("\"finish_reason\":\"stop\"")), "缺 stop 收尾: {out:?}");
        assert_eq!(out.last().map(|s| s.as_str()), Some("[DONE]"));
    }

    /// 非流式 usage 字段名映射: Responses 的 input_tokens/output_tokens 必须转成
    /// OpenAI 的 prompt_tokens/completion_tokens, 否则客户端与本地日志计数为 0.
    #[test]
    fn nonstream_usage_field_names_mapped() {
        let body = json!({
            "id": "resp_1", "status": "completed",
            "output": [{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hi"}]}],
            "usage": {"input_tokens": 50, "output_tokens": 10,
                      "input_tokens_details": {"cached_tokens": 8},
                      "output_tokens_details": {"reasoning_tokens": 4}}
        });
        let out = responses_to_openai_nonstream(&body);
        assert_eq!(out["usage"]["prompt_tokens"], 50);
        assert_eq!(out["usage"]["completion_tokens"], 10);
        assert_eq!(out["usage"]["total_tokens"], 60);
        assert_eq!(out["usage"]["prompt_tokens_details"]["cached_tokens"], 8);
        assert_eq!(out["usage"]["completion_tokens_details"]["reasoning_tokens"], 4);
    }

    /// 已是 OpenAI 风格 usage 时原样透传, 不重复映射.
    #[test]
    fn nonstream_usage_openai_style_passthrough() {
        let u = json!({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7});
        let mapped = map_responses_usage_to_openai(&u);
        assert_eq!(mapped, u);
    }

    /// 流式 usage details 不交叉污染: cached 只进 prompt_tokens_details,
    /// reasoning 只进 completion_tokens_details.
    #[test]
    fn stream_usage_details_not_cross_contaminated() {
        let u = json!({"input_tokens": 100, "output_tokens": 20,
                       "input_tokens_details": {"cached_tokens": 30},
                       "output_tokens_details": {"reasoning_tokens": 8}});
        let s = openai_usage_from_responses(&u);
        let v: Value = serde_json::from_str(&s).unwrap();
        // prompt_tokens_details 只含 cached_tokens
        assert_eq!(v["usage"]["prompt_tokens_details"]["cached_tokens"], 30);
        assert!(v["usage"]["prompt_tokens_details"].get("reasoning_tokens").is_none(),
            "reasoning 不应出现在 prompt_tokens_details");
        // completion_tokens_details 只含 reasoning_tokens
        assert_eq!(v["usage"]["completion_tokens_details"]["reasoning_tokens"], 8);
        assert!(v["usage"]["completion_tokens_details"].get("cached_tokens").is_none(),
            "cached 不应出现在 completion_tokens_details");
    }

    /// 顶层 error 事件的 message 直接提取.
    #[test]
    fn top_level_error_event_extracts_message() {
        let mut conv = ResponsesStreamConv::new();
        let out = conv.feed_line(r#"data: {"type":"error","error":{"message":"context too long"}}"#);
        assert_eq!(out.len(), 1);
        assert!(conv.last_error.as_deref() == Some("context too long"));
    }

    // ── /v1/responses 入口: Responses → chat 请求转换 ──

    /// 请求转换无损性: instructions / 多形态 input item / 大体积内容必须完整保留.
    #[test]
    fn responses_request_to_chat_preserves_content() {
        let big_instr = "I".repeat(15_000);
        let big_args = format!("{{\"path\":\"a.rs\",\"blob\":\"{}\"}}", "Y".repeat(30_000));
        let big_out = "X".repeat(50_000);
        let big_user = "U".repeat(10_000);
        let data_uri = format!("data:image/png;base64,{}", "Z".repeat(100_000));

        let body = json!({
            "model": "m",
            "instructions": big_instr,
            "input": [
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": big_user},
                    {"type": "input_image", "image_url": data_uri},
                ]},
                {"type": "function_call", "call_id": "c1", "name": "edit_file", "arguments": big_args},
                {"type": "function_call_output", "call_id": "c1", "output": big_out},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
            ],
            "tools": [{"type": "function", "name": "edit_file", "description": "edit", "parameters": {"type": "object", "properties": {}}}],
            "reasoning": {"effort": "high", "summary": "auto"},
            "max_output_tokens": 2048,
            "stream": true,
        });
        let conv = responses_to_openai_request(&body).unwrap();

        // instructions → 首条 system 消息
        assert_eq!(conv["messages"][0]["role"], "system");
        assert_eq!(conv["messages"][0]["content"].as_str(), Some(big_instr.as_str()), "instructions 丢失");
        // user 消息: 文本 + 图片都保留
        assert_eq!(conv["messages"][1]["role"], "user");
        let parts = conv["messages"][1]["content"].as_array().unwrap();
        assert_eq!(parts[0]["type"], "text");
        assert_eq!(parts[0]["text"].as_str(), Some(big_user.as_str()), "user 文本丢失");
        assert_eq!(parts[1]["type"], "image_url");
        assert_eq!(parts[1]["image_url"]["url"].as_str(), Some(data_uri.as_str()), "图片丢失");
        // function_call → assistant tool_calls (参数完整)
        assert_eq!(conv["messages"][2]["tool_calls"][0]["function"]["name"], "edit_file");
        assert_eq!(
            conv["messages"][2]["tool_calls"][0]["function"]["arguments"].as_str(),
            Some(big_args.as_str()),
            "工具参数丢失"
        );
        // function_call_output → tool 消息 (输出完整)
        assert_eq!(conv["messages"][3]["role"], "tool");
        assert_eq!(conv["messages"][3]["tool_call_id"], "c1");
        assert_eq!(conv["messages"][3]["content"].as_str(), Some(big_out.as_str()), "tool 输出丢失");
        // reasoning → assistant reasoning_content (交由 strip 配置统一处理)
        assert_eq!(conv["messages"][4]["reasoning_content"], "thinking...");
        // assistant 正文
        assert_eq!(conv["messages"][5]["content"], "done");
        // 参数映射
        assert_eq!(conv["max_tokens"], 2048);
        assert_eq!(conv["reasoning_effort"], "high");
        assert_eq!(conv["stream"], true);
        assert_eq!(conv["tools"][0]["function"]["name"], "edit_file");
    }

    /// 有状态引用必须显式拒绝 (中转网关无会话存储, 静默忽略会让客户端误以为历史生效).
    #[test]
    fn responses_request_rejects_stateful_refs() {
        let body = json!({"model": "m", "input": [], "previous_response_id": "resp_abc"});
        let err = responses_to_openai_request(&body).unwrap_err();
        assert!(err.contains("previous_response_id"), "应报 previous_response_id 错: {err}");

        let body2 = json!({"model": "m", "input": [{"type": "item_reference", "id": "it_1"}]});
        let err2 = responses_to_openai_request(&body2).unwrap_err();
        assert!(err2.contains("item_reference"), "应报 item_reference 错: {err2}");
    }

    // ── /v1/responses 出口: chat → Responses 响应转换 ──

    /// 非流式转换: reasoning/tool_calls/usage 逆映射 + length 截断语义.
    #[test]
    fn chat_response_to_responses_nonstream() {
        let body = json!({
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1700000000, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": null, "reasoning_content": "pondering",
                "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "run", "arguments": "{\"x\":1}"}}],
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60,
                      "prompt_tokens_details": {"cached_tokens": 8},
                      "completion_tokens_details": {"reasoning_tokens": 4}},
        });
        let out = openai_to_responses_nonstream(&body);
        assert_eq!(out["object"], "response");
        assert_eq!(out["status"], "completed");
        assert_eq!(out["created_at"], 1700000000);
        let output = out["output"].as_array().unwrap();
        assert_eq!(output[0]["type"], "reasoning");
        assert_eq!(output[0]["summary"][0]["text"], "pondering");
        assert_eq!(output[1]["type"], "function_call");
        assert_eq!(output[1]["call_id"], "call_9");
        assert_eq!(output[1]["arguments"], "{\"x\":1}");
        // usage 逆映射
        assert_eq!(out["usage"]["input_tokens"], 50);
        assert_eq!(out["usage"]["output_tokens"], 10);
        assert_eq!(out["usage"]["input_tokens_details"]["cached_tokens"], 8);
        assert_eq!(out["usage"]["output_tokens_details"]["reasoning_tokens"], 4);

        // length 截断 → incomplete + incomplete_details
        let mut truncated = body.clone();
        truncated["choices"][0]["finish_reason"] = json!("length");
        truncated["choices"][0]["message"]["tool_calls"] = json!(null);
        let out2 = openai_to_responses_nonstream(&truncated);
        assert_eq!(out2["status"], "incomplete");
        assert_eq!(out2["incomplete_details"]["reason"], "max_output_tokens");
    }

    /// 流式转换: 完整事件序列 (created → item.added → delta → done → completed + usage + [DONE]).
    #[test]
    fn chat_stream_to_responses_events() {
        let mut conv = ChatToResponsesStreamConv::new();
        let mut events: Vec<(String, String)> = Vec::new();
        let frames = [
            r#"{"id":"ccpl-1","object":"chat.completion.chunk","created":1700000000,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}"#,
            r#"{"choices":[{"index":0,"delta":{"reasoning_content":"think"},"finish_reason":null}]}"#,
            r#"{"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}"#,
            r#"{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"run","arguments":"{\"a\""}}]},"finish_reason":null}]}"#,
            r#"{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":1}"}}]},"finish_reason":null}]}"#,
            r#"{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}"#,
            "[DONE]",
        ];
        for f in frames {
            events.extend(conv.feed(f));
        }
        assert!(conv.finalize().is_empty(), "已收尾后 finalize 不应重复输出");

        let names: Vec<&str> = events.iter().map(|(e, _)| e.as_str()).collect();
        let find = |prefix: &str| names.iter().position(|n| n.starts_with(prefix));
        // 事件齐全
        assert!(find("response.created") == Some(0), "首事件必须是 created: {names:?}");
        assert!(names.contains(&"response.in_progress"));
        assert!(names.contains(&"response.output_item.added"));
        assert!(names.contains(&"response.reasoning_summary_text.delta"));
        assert!(names.contains(&"response.output_text.delta"));
        assert!(names.contains(&"response.function_call_arguments.delta"));
        assert!(names.contains(&"response.function_call_arguments.done"));
        assert!(names.contains(&"response.output_item.done"));
        assert!(names.contains(&"response.completed"));
        // delta 内容透传
        let text_delta = events.iter().find(|(e, _)| e == "response.output_text.delta").unwrap();
        assert!(text_delta.1.contains("\"delta\":\"hello\""), "{text_delta:?}");
        let args_delta = events.iter().find(|(e, _)| e == "response.function_call_arguments.delta").unwrap();
        assert!(args_delta.1.contains("\"delta\":\"{\\\"a\\\"\""), "{args_delta:?}");
        // 终止序列顺序: output_item.done 全部在 completed 之前, [DONE] 收尾
        let done_pos = names.iter().position(|n| n.is_empty()).unwrap();
        assert_eq!(events[done_pos].1, "[DONE]");
        let completed_pos = find("response.completed").unwrap();
        assert!(completed_pos < done_pos);
        let item_done_positions: Vec<usize> = names.iter().enumerate()
            .filter(|(_, n)| **n == "response.output_item.done").map(|(i, _)| i).collect();
        assert!(!item_done_positions.is_empty());
        assert!(item_done_positions.iter().all(|p| *p < completed_pos), "item.done 必须先于 completed");
        // completed 携带完整 output + usage (tool_calls 的 finish → function_call item)
        let completed_data: Value = serde_json::from_str(&events[completed_pos].1).unwrap();
        let output = completed_data["response"]["output"].as_array().unwrap();
        assert!(output.iter().any(|it| it["type"] == "message" && it["content"][0]["text"] == "hello"));
        assert!(output.iter().any(|it| it["type"] == "function_call" && it["arguments"] == "{\"a\":1}"));
        assert_eq!(completed_data["response"]["usage"]["input_tokens"], 10);
        assert_eq!(completed_data["response"]["usage"]["output_tokens"], 5);
    }

    /// 流式 length 截断 → response.incomplete (官方截断语义).
    #[test]
    fn chat_stream_length_finish_emits_incomplete() {
        let mut conv = ChatToResponsesStreamConv::new();
        let mut events = conv.feed(
            r#"{"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"abc"},"finish_reason":null}]}"#,
        );
        events.extend(conv.feed(
            r#"{"choices":[{"index":0,"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}"#,
        ));
        events.extend(conv.feed("[DONE]"));
        let names: Vec<&str> = events.iter().map(|(e, _)| e.as_str()).collect();
        assert!(names.contains(&"response.incomplete"), "length 截断应发 response.incomplete: {names:?}");
        assert!(!names.contains(&"response.completed"));
        let inc = events.iter().find(|(e, _)| e == "response.incomplete").unwrap();
        let v: Value = serde_json::from_str(&inc.1).unwrap();
        assert_eq!(v["response"]["status"], "incomplete");
        assert_eq!(v["response"]["incomplete_details"]["reason"], "max_output_tokens");
        assert_eq!(v["response"]["usage"]["input_tokens"], 1);
    }

    /// 流中错误帧 → response.failed (客户端可感知失败原因, 不静默).
    #[test]
    fn chat_stream_error_frame_emits_failed() {
        let mut conv = ChatToResponsesStreamConv::new();
        let events = conv.feed(r#"{"error":{"message":"quota exceeded","type":"insufficient_quota"}}"#);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].0, "response.failed");
        assert!(events[0].1.contains("quota exceeded"), "{:?}", events[0].1);
        // 失败后不再产出后续帧
        assert!(conv.feed("[DONE]").is_empty());
        assert!(conv.finalize().is_empty());
    }
}
