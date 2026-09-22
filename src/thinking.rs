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
pub(crate) fn normalize_effort(v: &str) -> String {
    match v.to_ascii_lowercase().as_str() {
        "minimal" | "low" => "low".to_string(),
        "medium" => "medium".to_string(),
        "high" | "xhigh" => "high".to_string(),
        "max" | "maximum" | "maximize" => "max".to_string(),
        other => other.to_string(),
    }
}

/// 思考档位 → Anthropic `budget_tokens` (单一换算表, 与 [`budget_tokens_to_effort`] 互逆).
///
/// 逆向换算曾各有一张表且互不自洽 (high→4096 但 4096 被换回 low, 一来一回丢两档),
/// 现统一在此处; round-trip 由单测固化.
pub(crate) fn effort_to_budget_tokens(effort: &str) -> u64 {
    match normalize_effort(effort).as_str() {
        "low" => 1024,
        "medium" => 2048,
        "high" => 4096,
        "max" => 8000,
        // 未知档位给中间值 (与历史行为 `_ => 4096` 一致).
        _ => 4096,
    }
}

/// Anthropic `budget_tokens` → 思考档位 (单一换算表, 与 [`effort_to_budget_tokens`] 互逆).
///
/// 口径: <1200 → low, <2500 → medium, <6000 → high, ≥6000 → max —— 恰好把
/// 1024/2048/4096/8000 四个正向档位映射回原档 (round-trip 有单测).
pub(crate) fn budget_tokens_to_effort(n: u64) -> &'static str {
    if n < 1200 {
        "low"
    } else if n < 2500 {
        "medium"
    } else if n < 6000 {
        "high"
    } else {
        "max"
    }
}

/// 配置档思考强度: `None` / 空串(面板「无」经手工编辑 providers.json 的等价形态) 都视为
/// 「未配置」; 出参已归一化. 优先级链 (客户端 > 配置档 > 不注入) 的第二级统一走这里判空.
pub(crate) fn config_effort(model: &crate::providers::ModelConfig) -> Option<String> {
    model
        .reasoning_effort
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(normalize_effort)
}

/// 为 Anthropic 协议计算可注入的 `budget_tokens`.
///
/// 约束 (缺一条就会制造必然失败/空正文的请求):
/// - 预算须 ≥ 1024 (Anthropic 下限) 且 **< max_tokens**;
/// - 且为可见输出预留 ≥ 1024 —— 思考 token 计入 max_tokens, 预算贴满上限会把正文饿成空
///   字符串 (实测案例: max_tokens 64 时思考占满、正文 `''`, 见 HANDOFF §G).
///
/// 无法同时满足时返回 `None` = 本次**不注入** thinking: 宁可降级为不思考, 也不制造
/// 必然 400 / 空回复. 这使配置档注入成为"尽力而为"的第二级而非破坏性覆盖.
pub(crate) fn anthropic_thinking_budget(effort: &str, max_tokens: u64) -> Option<u64> {
    const MIN_BUDGET: u64 = 1024;
    const OUTPUT_RESERVE: u64 = 1024;
    let cap = max_tokens.checked_sub(OUTPUT_RESERVE)?;
    if cap < MIN_BUDGET {
        return None;
    }
    Some(effort_to_budget_tokens(effort).min(cap))
}

/// 对 `muse-spark` 等仅支持 low/medium/high 的模型钳制档位 (max/xhigh→high).
pub(crate) fn clamp_effort_for_model(effort: String, model: &crate::providers::ModelConfig) -> String {
    let id = model
        .upstream_model
        .as_deref()
        .unwrap_or("")
        .to_ascii_lowercase();
    let is_muse = id.contains("muse-spark");
    if is_muse && effort == "max" {
        return "high".to_string();
    }
    effort
}

/// 规范化请求体中的思考参数. 原地修改 `body`, 返回客户端是否**显式关闭**了思考.
///
/// 返回值语义 (供 `inject_model_params` 决定是否用配置档兜底):
/// - `true`  = 客户端发了 `thinking: false`, 明确不要思考 → 代理不得注入 reasoning_effort.
/// - `false` = 其余情况 (未提 / 开启 / 指定档位) → 由调用方按既有规则处理.
///
/// 字段处理:
/// - `thinking: true`  → 置 `reasoning_effort` (优先用模型配置值, 否则 "high") 并移除 `thinking`.
/// - `thinking: false` → 移除 `thinking`, 返回 `true` (客户端档位优先, 不注入配置档).
/// - `thinking` 为对象 → 取其中 `effort` 映射到 `reasoning_effort`; `type:"disabled"` 视同
///   `thinking: false` (返回 `true`); `budgetTokens`/`budget_tokens` 按统一换算表近似档位;
///   都缺省时用模型配置 / 默认 "high".
/// - `reasoning_effort` → 别名归一化为 low/medium/high/max.
///
/// 设计原则 (优先级链): **客户端(编程工具)档位 > 面板配置档 > 不注入**. 配置档
/// (`providers.json` 的 reasoning_effort) 仅作"客户端无指示时的默认", 客户端发的档位
/// (含 thinking 布尔 / 对象 / reasoning_effort, 以及显式关闭) 一律优先. 代理只做
/// `thinking → reasoning_effort` 的协议翻译, 不强制拉满.
pub fn normalize_thinking(body: &mut Value, model: &crate::providers::ModelConfig) -> bool {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return false,
    };

    let mut explicitly_disabled = false;

    // 0. 部分 SDK/客户端把 thinking 塞进 extra_body (而非顶层), 先提到顶层统一处理.
    //    仅当顶层尚无 thinking 时才搬, 避免覆盖客户端显式表达.
    if !obj.contains_key("thinking") {
        if let Some(eb) = obj.get_mut("extra_body").and_then(|e| e.as_object_mut()) {
            if let Some(t) = eb.remove("thinking") {
                obj.insert("thinking".to_string(), t);
            }
        }
    }

    // 1. 处理客户端发来的 thinking 字段
    if let Some(thinking) = obj.remove("thinking") {
        match thinking {
            Value::Bool(true) => {
                let effort = config_effort(model).unwrap_or_else(|| "high".to_string());
                let effort = clamp_effort_for_model(effort, model);
                obj.entry("reasoning_effort".to_string())
                    .or_insert_with(|| Value::String(effort));
            }
            Value::Bool(false) => {
                // 客户端显式关闭思考: 标记, 交由 inject_model_params 跳过配置档注入
                explicitly_disabled = true;
            }
            Value::Object(map) => {
                // Anthropic 风格 type 字段: "disabled" = 客户端明确不要思考 (与 thinking:false 同权),
                // 不得被配置档强行打开; "enabled" 或缺省则继续按档位链解析.
                let type_disabled = map.get("type").and_then(|t| t.as_str()) == Some("disabled");
                if type_disabled {
                    explicitly_disabled = true;
                } else {
                    let effort = map
                        .get("effort")
                        .and_then(|v| v.as_str())
                        .map(|s| normalize_effort(s))
                        .or_else(|| {
                            // Anthropic 风格 budgetTokens/budget_tokens → 档位近似 (统一换算表)
                            map.get("budgetTokens")
                                .or_else(|| map.get("budget_tokens"))
                                .and_then(|v| v.as_u64())
                                .map(|n| budget_tokens_to_effort(n).to_string())
                        })
                        .or_else(|| config_effort(model))
                        .unwrap_or_else(|| "high".to_string());
                    let effort = clamp_effort_for_model(effort, model);
                    obj.entry("reasoning_effort".to_string())
                        .or_insert_with(|| Value::String(effort));
                }
            }
            _ => {}
        }
    }

    // 1b. 处理 reasoningEffort 驼峰与 reasoning 对象 (opencode/Responses 风格)
    if let Some(v) = obj.remove("reasoningEffort") {
        let s = match &v {
            Value::String(s) => normalize_effort(s),
            _ => v.as_str().map(|s| normalize_effort(s)).unwrap_or_else(|| "high".to_string()),
        };
        let s = clamp_effort_for_model(s, model);
        obj.entry("reasoning_effort".to_string())
            .or_insert_with(|| Value::String(s));
    }
    if let Some(Value::Object(r)) = obj.get("reasoning").cloned() {
        if let Some(eff) = r.get("effort").and_then(|v| v.as_str()).map(|s| normalize_effort(s)) {
            let eff = clamp_effort_for_model(eff, model);
            obj.entry("reasoning_effort".to_string())
                .or_insert_with(|| Value::String(eff));
        }
    }

    // 2. 归一化 reasoning_effort 别名并按模型钳制 (避免借用冲突: 先算出结果再写回)
    let need_fix = match obj.get("reasoning_effort") {
        Some(Value::String(s)) => {
            let n = clamp_effort_for_model(normalize_effort(s), model);
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

    // 3. 推理激活时剥离采样参数: 部分上游 (DeepSeek-reasoner / Qwen-thinking 等) 固定
    //    temperature、top_p, 请求携带会直接 400. 推理激活即 reasoning_effort 存在
    //    (thinking:true / 对象 / 配置档注入, 且未被显式关闭). 普通模型不受影响.
    if obj.contains_key("reasoning_effort") {
        obj.remove("temperature");
        obj.remove("top_p");
    }

    explicitly_disabled
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
            free: None,
            extra_body: None,
            api_format: None,
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
        }
    }

    fn no_effort_model() -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: None,
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
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
        let disabled = normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("thinking").is_none());
        assert!(body.get("reasoning_effort").is_none());
        // 标记客户端显式关闭思考, 供代理跳过配置档注入
        assert!(disabled);
    }

    #[test]
    fn thinking_true_not_marked_disabled() {
        let mut body = json!({ "model": "x", "thinking": true });
        let disabled = normalize_thinking(&mut body, &no_effort_model());
        assert!(!disabled);
        assert_eq!(body["reasoning_effort"], "high");
    }

    #[test]
    fn no_thinking_field_not_disabled() {
        let mut body = json!({ "model": "x" });
        let disabled = normalize_thinking(&mut body, &no_effort_model());
        assert!(!disabled);
    }

    #[test]
    fn thinking_object_effort_mapped() {
        let mut body = json!({ "model": "x", "thinking": { "effort": "max" } });
        normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("thinking").is_none());
        assert_eq!(body["reasoning_effort"], "max");
    }

    /// Anthropic 风格 `type:"disabled"` = 客户端明确关思考 → 不得被配置档打开.
    #[test]
    fn thinking_object_type_disabled_respected() {
        let mut body = json!({ "model": "x", "thinking": { "type": "disabled" } });
        let disabled = normalize_thinking(&mut body, &model_with_effort("max"));
        assert!(disabled, "type:disabled 应与 thinking:false 同权");
        assert!(body.get("thinking").is_none());
        assert!(body.get("reasoning_effort").is_none());
    }

    /// Anthropic 风格 `type:"enabled"` + budget_tokens → 按统一换算表映射档位.
    #[test]
    fn thinking_object_enabled_budget_mapped() {
        let mut body = json!({ "model": "x", "thinking": { "type": "enabled", "budget_tokens": 8000 } });
        let disabled = normalize_thinking(&mut body, &no_effort_model());
        assert!(!disabled);
        assert_eq!(body["reasoning_effort"], "max");
    }

    /// 换算表 round-trip: 四个正向档位经 effort→budget→effort 必须回原档
    /// (历史 bug: 两张表不自洽, high→4096→low 一来一回丢两档).
    #[test]
    fn budget_effort_round_trip() {
        for effort in ["low", "medium", "high", "max"] {
            let budget = effort_to_budget_tokens(effort);
            assert_eq!(budget_tokens_to_effort(budget), effort, "{effort} round-trip 丢档");
        }
    }

    /// 配置档空串(面板「无」的手编等价形态)视为未配置, 不注入.
    #[test]
    fn empty_config_effort_treated_as_none() {
        assert!(config_effort(&model_with_effort("")).is_none());
        assert!(config_effort(&model_with_effort("  ")).is_none());
        assert_eq!(config_effort(&model_with_effort(" maximum ")).as_deref(), Some("max"));
    }

    /// Anthropic budget 注入护栏: 须留可见输出空间且不超 max_tokens; 放不下则拒绝注入.
    #[test]
    fn anthropic_thinking_budget_guard() {
        // 充足: 预算不被钳制
        assert_eq!(anthropic_thinking_budget("max", 65536), Some(8000));
        // 略紧: 钳到 max_tokens - 1024 (为可见输出预留), 不直接放弃
        assert_eq!(anthropic_thinking_budget("high", 5000), Some(3976));
        assert_eq!(anthropic_thinking_budget("max", 4096), Some(3072));
        // max_tokens 太小 → 连最小预算 1024 都放不下 → None (不注入, 不制造 400/空正文)
        assert_eq!(anthropic_thinking_budget("high", 2000), None);
        assert_eq!(anthropic_thinking_budget("low", 2047), None);
        // 低预算不被放大
        assert_eq!(anthropic_thinking_budget("low", 4096), Some(1024));
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

    #[test]
    fn extra_body_thinking_extracted() {
        let mut body = json!({ "model": "x", "extra_body": { "thinking": true } });
        normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("extra_body").is_none() || body["extra_body"].get("thinking").is_none());
        assert_eq!(body["reasoning_effort"], "high");
    }

    #[test]
    fn xhigh_alias_normalized() {
        let mut body = json!({ "model": "x", "reasoning_effort": "xhigh" });
        normalize_thinking(&mut body, &no_effort_model());
        assert_eq!(body["reasoning_effort"], "high");
    }

    #[test]
    fn top_level_thinking_beats_extra_body() {
        let mut body = json!({ "model": "x", "thinking": false, "extra_body": { "thinking": true } });
        let disabled = normalize_thinking(&mut body, &no_effort_model());
        // 顶层 thinking:false 优先, 不被 extra_body 覆盖
        assert!(disabled);
        assert!(body.get("reasoning_effort").is_none());
    }

    #[test]
    fn sampling_params_stripped_when_reasoning_active() {
        let mut body = json!({ "model": "x", "thinking": true, "temperature": 0.0, "top_p": 0.9 });
        normalize_thinking(&mut body, &no_effort_model());
        assert!(body.get("temperature").is_none());
        assert!(body.get("top_p").is_none());
        assert_eq!(body["reasoning_effort"], "high");
    }

    #[test]
    fn sampling_params_kept_when_reasoning_off() {
        let mut body = json!({ "model": "x", "temperature": 0.5, "top_p": 0.8 });
        normalize_thinking(&mut body, &no_effort_model());
        assert_eq!(body["temperature"], 0.5);
        assert_eq!(body["top_p"], 0.8);
    }
}
