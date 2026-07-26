//! 思考参数整流器 (OpenAI 兼容).
//!
//! 不同客户端 / 上游对"推理强度"的表达不一致:
//! - 部分客户端发 `thinking: true/false` (Claude / OpenAI beta 风格), 而 DeepSeek / Qwen 等
//!   OpenAI 兼容端点只认 `reasoning_effort`, 收到 `thinking` 会返回 400.
//! - `reasoning_effort` 取值别名 (minimal/low/medium/high/maximum) 需统一为上游接受的
//!   low / medium / high / max.
//!
//! 本模块在转发前把客户端思考参数规范为上游可读形式.
//!
//! 说明: cc-switch 的 `thinking_rectifier.rs` / `thinking_budget_rectifier.rs` 是
//! Anthropic/Claude 签名与 budget_tokens 专用 (32000/64000 等 Claude 魔法数字), 不适用
//! 于本项目的 OpenAI 兼容场景. 此处仅借鉴其"请求前规范化思考参数"的思路, 自行实现
//! OpenAI 兼容版本 (GPL-3.0 项目间思路借鉴合法).

use serde_json::Value;

/// 把 `reasoning_effort` 取值别名归一化为上游接受的 low/medium/high/max.
fn normalize_effort(v: &str) -> String {
    match v.to_ascii_lowercase().as_str() {
        "minimal" | "low" => "low".to_string(),
        "medium" => "medium".to_string(),
        "high" => "high".to_string(),
        "max" | "maximum" | "maximize" => "max".to_string(),
        other => other.to_string(),
    }
}

/// 规范化请求体中的思考参数. 原地修改 `body`.
///
/// - `thinking: true`  → 置 `reasoning_effort` (优先用模型配置值, 否则 "high") 并移除 `thinking`.
/// - `thinking: false` → 仅移除 `thinking` (是否注入 reasoning_effort 由 `inject_model_params` 按模型配置决定).
/// - `thinking` 为对象 → 取其中 `effort` 映射到 `reasoning_effort`, 否则用模型配置 / 默认 "high".
/// - `reasoning_effort` → 别名归一化为 low/medium/high/max.
pub fn normalize_thinking(body: &mut Value, model: &crate::providers::ModelConfig) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    // 1. 处理客户端发来的 thinking 字段
    if let Some(thinking) = obj.remove("thinking") {
        match thinking {
            Value::Bool(true) => {
                let effort = model
                    .reasoning_effort
                    .clone()
                    .unwrap_or_else(|| "high".to_string());
                obj.entry("reasoning_effort".to_string())
                    .or_insert_with(|| Value::String(effort));
            }
            Value::Bool(false) => {
                // 不注入, 交由 inject_model_params 按模型配置处理
            }
            Value::Object(map) => {
                let effort = map
                    .get("effort")
                    .and_then(|v| v.as_str())
                    .map(|s| normalize_effort(s))
                    .or_else(|| model.reasoning_effort.clone())
                    .unwrap_or_else(|| "high".to_string());
                obj.entry("reasoning_effort".to_string())
                    .or_insert_with(|| Value::String(effort));
            }
            _ => {}
        }
    }

    // 2. 归一化 reasoning_effort 别名 (避免借用冲突: 先算出结果再写回)
    let need_fix = match obj.get("reasoning_effort") {
        Some(Value::String(s)) => {
            let n = normalize_effort(s);
            if n != *s {
                Some(n)
            } else {
                None
            }
        }
        _ => None,
    };
    if let Some(n) = need_fix {
        obj.insert("reasoning_effort".to_string(), Value::String(n));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::ModelConfig;
    use serde_json::json;

    fn model_with_effort(effort: &str) -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: Some(effort.to_string()),
            extra_body: None,
        }
    }

    fn no_effort_model() -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: None,
            extra_body: None,
        }
    }

    #[test]
    fn thinking_true_maps_to_model_effort() {
        let mut body = json!({ "model": "x", "thinking": true });
        normalize_thinking(&mut body, &model_with_effort("medium"));
        assert!(body.get("thinking").is_none());
        assert_eq!(body["reasoning_effort"], "medium");
    }

    #[test]
    fn thinking_true_defaults_to_high() {
        let mut body = json!({ "model": "x", "thinking": true });
        normalize_thinking(&mut body, &no_effort_model());
        assert_eq!(body["reasoning_effort"], "high");
    }

    #[test]
    fn thinking_false_is_removed() {
        let mut body = json!({ "model": "x", "thinking": false });
        normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("thinking").is_none());
        assert!(body.get("reasoning_effort").is_none());
    }

    #[test]
    fn thinking_object_effort_mapped() {
        let mut body = json!({ "model": "x", "thinking": { "effort": "max" } });
        normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("thinking").is_none());
        assert_eq!(body["reasoning_effort"], "max");
    }

    #[test]
    fn reasoning_effort_alias_normalized() {
        let mut body = json!({ "model": "x", "reasoning_effort": "maximum" });
        normalize_thinking(&mut body, &no_effort_model());
        assert_eq!(body["reasoning_effort"], "max");

        let mut body2 = json!({ "model": "x", "reasoning_effort": "minimal" });
        normalize_thinking(&mut body2, &no_effort_model());
        assert_eq!(body2["reasoning_effort"], "low");
    }

    #[test]
    fn valid_reasoning_effort_unchanged() {
        let mut body = json!({ "model": "x", "reasoning_effort": "high" });
        normalize_thinking(&mut body, &no_effort_model());
        assert_eq!(body["reasoning_effort"], "high");
    }
}
