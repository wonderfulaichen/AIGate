//! 上下文构成分析 (2026-10-24) —— 回答"输入大在哪里".
//!
//! # 为什么需要
//! 现在面板能说"省了多少", 但回答不了**省下来的和剩下的分别是什么**:
//! 是工具 schema 太大? 是工具结果反复携带? 还是历史助手消息堆着?
//! 用户想进一步优化时没有抓手。(借鉴 WaLiAPI 的 prompt 分类可观测性;
//! 它参考了 magpie 的分类口径。)
//!
//! # 口径纪律 (最重要的一条)
//! ⚠️ **这是估算, 不是实测, 且不做"等比校准"。**
//!
//! WaLiAPI 的做法是: 本地估算各部分 → 用上游真实 prompt tokens 按比例缩放 →
//! 缩放后**取消估算标记**当实测展示。我们**刻意不那样做**, 理由:
//! 总和对得上, 不代表各部分都对 —— 等比缩放只是把误差均摊到每一类,
//! 让一个原本诚实的估算**看起来**像实测。本项目对外表述的底线是
//! 「估算值必须标 ≈」(见 `usage_estimated` 那一整套机制)。
//!
//! 所以: 各部分都是估算, 且**与上游 prompt_tokens 分开呈现**。
//! 用户看到的是"构成比例"(相对形状可信) 而非"精确 token 数"。
//!
//! # 分类 (六类, 与前端徽标一一对应)
//! `system` / `tools` / `injected` / `assistant` / `tool_result` / `chat`
//!
//! 其中 `injected` 是重点: agent 框架 (Claude Code / Codex / OpenBitFun 等) 会把
//! `<system-reminder>` 之类内容塞进 **user 消息**, 它们不是用户真正说的话 ——
//! 混进 `chat` 会让人误以为"用户输入很大"。

use serde_json::Value;

/// 分类键 (与前端展示口径一一对应, 改动需同步 i18n)。
pub const KIND_SYSTEM: &str = "system";
pub const KIND_TOOLS: &str = "tools";
pub const KIND_INJECTED: &str = "injected";
pub const KIND_ASSISTANT: &str = "assistant";
pub const KIND_TOOL_RESULT: &str = "tool_result";
pub const KIND_CHAT: &str = "chat";

/// 全部分类 (固定顺序, 供展示时补零行)。
pub const ALL_KINDS: [&str; 6] = [
    KIND_SYSTEM,
    KIND_TOOLS,
    KIND_INJECTED,
    KIND_ASSISTANT,
    KIND_TOOL_RESULT,
    KIND_CHAT,
];

/// agent 框架注入内容的特征前缀/片段。
///
/// 判据是"出现在文本**前 300 字符内**"而不是"整个文本相等":
/// 注入块通常贴在 user 消息开头或末尾, 且常与真实发言混在同一条消息里。
/// 覆盖 Claude Code / Codex / OpenBitFun / CodeBuddy 等常见下游 (实测取样)。
const INJECTED_MARKERS: [&str; 14] = [
    "<system-reminder>",
    "<environment_context>",
    "<environment-context>",
    "<task-notification>",
    "<system_warning>",
    "<user_instructions>",
    "<permissions",
    "<turn-aborted>",
    "## My request:",
    "The TodoWrite tool hasn't been used recently",
    "The user opened the file",
    "[System Instructions]",
    "<EXTREMELY_IMPORTANT>",
    "<command-name>",
];

/// 单类聚合结果。
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct KindStat {
    /// 该类估算 token 数 (始终是**估算**, 见模块文档)。
    pub tokens: u64,
    /// 该类包含的条目数 (消息条 / 工具个数)。0 表示该类不适用 (如无 tools)。
    pub items: u64,
}

/// 一次请求的上下文构成。
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ContextBreakdown {
    /// 六类分项, 键取 [`ALL_KINDS`] 之一。缺失的类不出现 (前端按固定顺序补零)。
    pub kinds: Vec<(String, KindStat)>,
    /// 估算合计 (各类之和)。**不是**上游 prompt_tokens —— 两者分开呈现。
    pub total_estimated: u64,
    /// 本次是否检测到图片 (图片按像素折算 token, 不在文本估算范围内)。
    #[serde(default)]
    pub has_images: bool,
}

impl ContextBreakdown {
    fn add(&mut self, kind: &'static str, tokens: u64, items: u64) {
        if let Some(e) = self.kinds.iter_mut().find(|(k, _)| k == kind) {
            e.1.tokens += tokens;
            e.1.items += items;
        } else {
            self.kinds.push((kind.to_string(), KindStat { tokens, items }));
        }
        self.total_estimated += tokens;
    }

    /// 按固定顺序补齐六类, 供前端稳定渲染 (不会因为某类为 0 而少一行)。
    pub fn normalized(mut self) -> Self {
        let mut out: Vec<(String, KindStat)> = Vec::with_capacity(ALL_KINDS.len());
        for k in ALL_KINDS {
            let st = self
                .kinds
                .iter()
                .find(|(kk, _)| kk == k)
                .map(|(_, v)| v.clone())
                .unwrap_or_default();
            out.push((k.to_string(), st));
        }
        self.kinds = out;
        self
    }
}

/// 取一条消息 content 的文本 (string 或 parts 数组里的文本部分)。
///
/// 只取**文本**: 图片/文件等二进制内容不计入 (见 `has_images`)。
fn content_text(content: &Value) -> (String, bool) {
    let mut text = String::new();
    let mut has_image = false;
    match content {
        Value::String(s) => text.push_str(s),
        Value::Array(parts) => {
            for p in parts {
                if let Some(t) = p.get("text").and_then(Value::as_str) {
                    text.push_str(t);
                    text.push('\n');
                }
                // 图片/文件: 标记存在, 但不把 base64 当文本算 token (会严重高估)
                let ty = p.get("type").and_then(Value::as_str).unwrap_or("");
                if ty == "image" || ty == "image_url" || ty == "input_image"
                    || ty == "document" || ty == "file"
                    || p.get("image_url").is_some()
                {
                    has_image = true;
                }
            }
        }
        _ => {}
    }
    (text, has_image)
}

/// 判断一段 user 文本是否为 agent 框架注入内容。
///
/// 按字符边界取前 300 字符 (CJK 多字节直接按字节切片会 panic —— 本项目踩过)。
fn is_injected(text: &str) -> bool {
    let head: String = text
        .char_indices()
        .take_while(|(idx, _)| *idx < 300)
        .map(|(_, ch)| ch)
        .collect();
    INJECTED_MARKERS.iter().any(|m| head.contains(m))
}

/// 对请求体做分类估算。覆盖三种下游协议形态:
/// - Chat Completions / Anthropic Messages: `messages[]` + `tools[]`
/// - Responses: `input` (string 或数组) + `instructions` + `tools[]`
/// - 兜底: 整个 JSON body 计入 `chat`
pub fn classify(body: &Value) -> ContextBreakdown {
    let mut b = ContextBreakdown::default();
    let est = crate::anthropic::estimate_tokens;

    // ① 工具定义: 序列化后计数 (含 schema 体积 —— 工具 schema 往往是隐形大头)。
    if let Some(tools) = body.get("tools").and_then(Value::as_array) {
        if !tools.is_empty() {
            let text = serde_json::to_string(tools).unwrap_or_default();
            // +4/个: 工具在真实提示里还有名字与分隔符开销 (经验值, 属估算的一部分)
            b.add(KIND_TOOLS, est(&text) + (tools.len() as u64) * 4, tools.len() as u64);
        }
    }

    // ② Responses 的 instructions 等价于 system。
    if let Some(ins) = body.get("instructions").and_then(Value::as_str) {
        if !ins.is_empty() {
            b.add(KIND_SYSTEM, est(ins) + 4, 1);
        }
    }

    // ③ messages 数组 (Chat / Anthropic)。
    if let Some(msgs) = body.get("messages").and_then(Value::as_array) {
        for m in msgs {
            let role = m.get("role").and_then(Value::as_str).unwrap_or("");
            let (text, img) = content_text(m.get("content").unwrap_or(&Value::Null));
            if img {
                b.has_images = true;
            }
            // +4/条: 每条消息的角色与分隔符开销 (经验值)
            let tokens = est(&text) + 4;
            match role {
                "system" | "developer" => b.add(KIND_SYSTEM, tokens, 1),
                "assistant" => b.add(KIND_ASSISTANT, tokens, 1),
                "tool" | "function" => b.add(KIND_TOOL_RESULT, tokens, 1),
                "user" => {
                    if is_injected(&text) {
                        b.add(KIND_INJECTED, tokens, 1);
                    } else {
                        b.add(KIND_CHAT, tokens, 1);
                    }
                }
                _ => b.add(KIND_CHAT, tokens, 1),
            }
        }
    }

    // ④ Responses 的 input (messages 缺失或为空时才走这里)。
    let has_messages = body
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|m| !m.is_empty());
    if !has_messages {
        if let Some(input) = body.get("input") {
            match input {
                Value::String(s) => {
                    let tokens = est(s) + 4;
                    if is_injected(s) {
                        b.add(KIND_INJECTED, tokens, 1);
                    } else {
                        b.add(KIND_CHAT, tokens, 1);
                    }
                }
                Value::Array(items) => {
                    for item in items {
                        let role = item.get("role").and_then(Value::as_str).unwrap_or("");
                        let (mut text, img) = content_text(item);
                        if img {
                            b.has_images = true;
                        }
                        if let Some(out) = item.get("output").and_then(Value::as_str) {
                            text.push_str(out);
                        }
                        let tokens = est(&text) + 4;
                        // type 优先于 role: Responses 用 type 区分 function_call_output 等
                        match item.get("type").and_then(Value::as_str).unwrap_or(role) {
                            "function_call_output" | "tool_result" => {
                                b.add(KIND_TOOL_RESULT, tokens, 1)
                            }
                            t if t.contains("reasoning") => b.add(KIND_ASSISTANT, tokens, 1),
                            _ => match role {
                                "assistant" => b.add(KIND_ASSISTANT, tokens, 1),
                                "system" | "developer" => b.add(KIND_SYSTEM, tokens, 1),
                                _ => {
                                    if is_injected(&text) {
                                        b.add(KIND_INJECTED, tokens, 1);
                                    } else {
                                        b.add(KIND_CHAT, tokens, 1);
                                    }
                                }
                            },
                        }
                    }
                }
                _ => {}
            }
        }
    }

    // ⑤ 全空兜底: 整个 body 计入 chat (与 estimate_usage 的兜底口径一致)。
    if b.total_estimated == 0 {
        let s = serde_json::to_string(body).unwrap_or_default();
        b.add(KIND_CHAT, est(&s), 1);
    }

    b.normalized()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn get(b: &ContextBreakdown, kind: &str) -> KindStat {
        b.kinds
            .iter()
            .find(|(k, _)| k == kind)
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    }

    /// 六类必须始终齐备 (前端按固定顺序渲染, 少一类会让徽标错位)。
    #[test]
    fn always_returns_all_six_kinds_in_order() {
        let b = classify(&json!({"messages": [{"role": "user", "content": "hi"}]}));
        let keys: Vec<&str> = b.kinds.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ALL_KINDS.to_vec(), "六类顺序必须固定");
    }

    /// 基本分类: system / assistant / tool 结果 / user 各归各位。
    #[test]
    fn classifies_basic_roles() {
        let b = classify(&json!({
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "tool", "content": "tool output here"}
            ]
        }));
        assert!(get(&b, KIND_SYSTEM).tokens > 0, "system 应被计入");
        assert!(get(&b, KIND_CHAT).tokens > 0, "user 应被计入 chat");
        assert!(get(&b, KIND_ASSISTANT).tokens > 0, "assistant 应被计入");
        assert!(get(&b, KIND_TOOL_RESULT).tokens > 0, "tool 结果应被计入");
        assert_eq!(get(&b, KIND_SYSTEM).items, 1);
        assert_eq!(get(&b, KIND_TOOL_RESULT).items, 1);
    }

    /// **重点判据**: agent 注入内容不得混进「用户消息」。
    ///
    /// 这是本模块的核心价值 —— 否则用户会误以为"我自己的输入很大",
    /// 而真相是框架塞了 `<system-reminder>` 之类。
    #[test]
    fn injected_content_is_not_counted_as_user_chat() {
        let b = classify(&json!({
            "messages": [
                {"role": "user", "content": "<system-reminder>\nYou are an agent.\n</system-reminder>"},
                {"role": "user", "content": "真正的问题"}
            ]
        }));
        assert!(get(&b, KIND_INJECTED).tokens > 0, "注入内容应单独计入 injected");
        assert_eq!(get(&b, KIND_INJECTED).items, 1);
        // 真实发言仍在 chat
        assert_eq!(get(&b, KIND_CHAT).items, 1, "只有真实发言算 chat");
        assert!(
            get(&b, KIND_INJECTED).tokens > get(&b, KIND_CHAT).tokens,
            "本用例里注入块明显更长"
        );
    }

    /// 工具 schema 要计入 `tools` —— 它常是隐形大头。
    #[test]
    fn tool_schemas_counted_separately() {
        let b = classify(&json!({
            "tools": [
                {"type": "function", "function": {"name": "read",
                 "description": "Read a file from disk and return its contents verbatim",
                 "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
            ],
            "messages": [{"role": "user", "content": "hi"}]
        }));
        let t = get(&b, KIND_TOOLS);
        assert!(t.tokens > 0, "工具 schema 应计入 tools");
        assert_eq!(t.items, 1, "条目数 = 工具个数");
    }

    /// Responses 协议的 `input` 数组也要分类 (含 function_call_output)。
    #[test]
    fn responses_input_array_is_classified() {
        let b = classify(&json!({
            "instructions": "you are helpful",
            "input": [
                {"type": "message", "role": "user", "content": "hello"},
                {"type": "function_call_output", "output": "tool said this"}
            ]
        }));
        assert!(get(&b, KIND_SYSTEM).tokens > 0, "instructions 算 system");
        assert!(get(&b, KIND_TOOL_RESULT).tokens > 0, "function_call_output 算工具结果");
    }

    /// 图片只标记存在, **不得**把 base64 当文本算 (会严重高估)。
    #[test]
    fn images_marked_but_not_counted_as_text() {
        let big_b64 = "A".repeat(100_000); // 100KB 的伪 base64
        let b = classify(&json!({
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": format!("data:image/png;base64,{}", big_b64)}}
            ]}]
        }));
        assert!(b.has_images, "应标记含图片");
        assert!(
            b.total_estimated < 1000,
            "图片 base64 不得计入文本 token (实际 {})",
            b.total_estimated
        );
    }

    /// 空 body 兜底: 不得给出 0 (否则前端会显示"没有输入", 那是假的)。
    #[test]
    fn empty_body_falls_back_to_whole_json() {
        let b = classify(&json!({"foo": "bar"}));
        assert!(b.total_estimated > 0, "兜底应计入整个 body");
        assert_eq!(get(&b, KIND_CHAT).items, 1);
    }

    /// 合计 == 各类之和 (内部自洽; 注意这**不代表**等于上游 prompt_tokens)。
    #[test]
    fn total_equals_sum_of_kinds() {
        let b = classify(&json!({
            "tools": [{"type": "function", "function": {"name": "x", "description": "dd"}}],
            "messages": [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
                {"role": "tool", "content": "t"}
            ]
        }));
        let sum: u64 = b.kinds.iter().map(|(_, v)| v.tokens).sum();
        assert_eq!(b.total_estimated, sum, "合计必须等于各类之和");
    }

    /// CJK 文本不应被按字节高估 (复用 estimate_tokens 的 CJK 口径)。
    #[test]
    fn cjk_text_not_overestimated() {
        let zh = classify(&json!({"messages": [{"role": "user", "content": "这是一个中文测试句子"}]}));
        let en = classify(&json!({"messages": [{"role": "user", "content": "this is a test"}]}));
        // 10 个汉字 ≈ 10 token; 若按字节/4 会得 30+。这里只断言量级合理。
        let zh_tokens = get(&zh, KIND_CHAT).tokens;
        assert!(zh_tokens <= 20, "10 个汉字不该超过 20 token (实际 {zh_tokens})");
        assert!(zh_tokens > 0);
        assert!(get(&en, KIND_CHAT).tokens > 0);
    }
}
