//! 模型价格表 — 费用统计用.
//!
//! 背景: 国内大模型供应商（DeepSeek / 阿里百炼 / 智谱 / 月之暗面 / 硅基流动等）
//! **均不提供价格查询 API**; 价格仅在官方定价文档页公开, 且会变动
//! （促销 / 阶梯 / 高峰倍率 / 版本升级）. 因此无法程序化实时拉取.
//!
//! 做法: 本模块内置一份**官方公开价的默认值**（仅 DeepSeek, 抓取于 2026-08-13,
//! 来源 <https://api-docs.deepseek.com/zh-cn/quick_start/pricing>）, 其余供应商
//! 请在 `providers.json` 的 model 条目配置 `price` 覆盖（优先级最高）.
//!
//! 计价单位: 元 / 百万 tokens.

use serde::{Deserialize, Serialize};

/// 单个模型的价格（元 / 百万 tokens）.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct ModelPrice {
    /// 输入 token 价格（元 / 百万 tokens）, 不含 KV Cache 命中部分.
    pub input_per_m: f64,
    /// 输出 token 价格（元 / 百万 tokens）.
    pub output_per_m: f64,
    /// 上游 KV Cache 命中 token 价格（元 / 百万 tokens）.
    /// 缺失时回退按 `input_per_m` 计（多数模型无独立 cache 价）.
    #[serde(default)]
    pub cache_read_per_m: Option<f64>,
    /// 上游 KV Cache 首次写入 (creation) token 价格（元 / 百万 tokens）.
    /// Anthropic 等写入缓存独立计费（常为输入的 1.25x）; 缺失时回退按 `input_per_m` 计.
    /// 多数供应商（DeepSeek 等）无独立 creation 价, 此项留空即等价旧行为.
    #[serde(default)]
    pub cache_creation_per_m: Option<f64>,
}

/// 内置默认价格表（按 `upstream_model` 匹配）.
///
/// ⚠️ 价格会变动! 以官方文档为准; 若与实际不符, 在 `providers.json` 覆盖.
fn builtin_table() -> &'static [(&'static str, ModelPrice)] {
    &[
        (
            "deepseek-v4-flash",
            ModelPrice {
                input_per_m: 1.0,
                output_per_m: 2.0,
                cache_read_per_m: Some(0.02),
                cache_creation_per_m: None,
            },
        ),
        (
            "deepseek-v4-pro",
            ModelPrice {
                input_per_m: 3.0,
                output_per_m: 6.0,
                cache_read_per_m: Some(0.025),
                cache_creation_per_m: None,
            },
        ),
    ]
}

/// 去除常见免费 / 试用后缀（如 `-free`）, 用于更宽松地匹配内置表.
fn normalize_upstream(s: &str) -> String {
    let s = s.trim();
    for suf in ["-free", "-Free", "-FREE", "-trial"] {
        if let Some(stripped) = s.strip_suffix(suf) {
            return stripped.to_string();
        }
    }
    s.to_string()
}

/// 按 `upstream_model` 查内置表. 先精确匹配, 再尝试去除免费 / 试用后缀匹配.
pub fn builtin_price(upstream: &str) -> Option<ModelPrice> {
    let table = builtin_table();
    if let Some((_, p)) = table.iter().find(|(k, _)| *k == upstream) {
        return Some(*p);
    }
    let norm = normalize_upstream(upstream);
    table.iter().find(|(k, _)| *k == norm).map(|(_, p)| *p)
}

/// 判断供应商 endpoint 是否为 DeepSeek 官方 API (host 含 `api.deepseek.com`).
///
/// 内置 DeepSeek 价**仅**对官方供应商自动套用; opencode / zen / go 等网关
/// 不自动套用（否则会按模型 id 跨供应商串价——网关中转的 `deepseek-v4-flash`
/// 也会被套成 DS 官方价, 但网关实际计费口径未必相同）. 网关需在 `providers.json`
/// 显式配 `price`, 否则记"未配置"（费用按 0 计）.
///
/// 注意: endpoint 是用户自有配置, host 形式固定, 用 `contains` 足矣.
pub fn is_official_deepseek(endpoint: Option<&str>) -> bool {
    endpoint.map(|e| e.contains("api.deepseek.com")).unwrap_or(false)
}

/// 解析最终价格: `providers.json` 的 model.price 覆盖优先, 否则（仅官方 DeepSeek
/// 供应商）用内置表.
pub fn resolve_price(
    override_price: Option<ModelPrice>,
    upstream: Option<&str>,
    endpoint: Option<&str>,
) -> Option<ModelPrice> {
    if let Some(p) = override_price {
        return Some(p);
    }
    // 内置 DeepSeek 价仅限官方供应商; 网关不自动套, 避免跨供应商串价.
    if is_official_deepseek(endpoint) {
        return upstream.and_then(builtin_price);
    }
    None
}

/// 计算单条请求费用（元）.
///
/// 计费拆分:
/// - `prompt_tokens` 为输入总量（含 KV Cache 命中）;
/// - 命中部分 = `prompt_cache_hit_tokens`, 按 `cache_read_per_m`（缺失则 `input_per_m`）计;
/// - 未命中部分 = `prompt_tokens - 命中`, 按 `input_per_m` 计;
/// - `completion_tokens` 按 `output_per_m` 计.
///
/// 价格缺失（未配置）时返回 `None`, 调用方按 0 处理（不计入费用）.
pub fn compute_cost(
    price: Option<ModelPrice>,
    prompt_tokens: u32,
    completion_tokens: u32,
    prompt_cache_hit_tokens: u32,
    prompt_cache_creation_tokens: u32,
) -> Option<f64> {
    let p = price?;
    let prompt = prompt_tokens as f64;
    let completion = completion_tokens as f64;
    // 输入拆为三部分: 命中(从缓存读) / 首次写入(creation) / 全新输入(fresh).
    // 三者之和 = prompt_tokens; 任一超出剩余量则钳制, 防上游脏数据导致负 fresh.
    let hit = (prompt_cache_hit_tokens as f64).min(prompt);
    let creation = (prompt_cache_creation_tokens as f64).min((prompt - hit).max(0.0));
    let fresh = (prompt - hit - creation).max(0.0);
    let cache_read_price = p.cache_read_per_m.unwrap_or(p.input_per_m);
    let cache_creation_price = p.cache_creation_per_m.unwrap_or(p.input_per_m);
    let cost = hit / 1e6 * cache_read_price
        + creation / 1e6 * cache_creation_price
        + fresh / 1e6 * p.input_per_m
        + completion / 1e6 * p.output_per_m;
    Some(cost)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    const FLASH: ModelPrice = ModelPrice {
        input_per_m: 1.0,
        output_per_m: 2.0,
        cache_read_per_m: Some(0.02),
        cache_creation_per_m: Some(0.1),
    };

    /// 输入拆分口径: hit / creation / fresh 三者之和 = prompt_tokens, 各自独立计价.
    #[test]
    fn compute_cost_with_cache_price() {
        // 100 万输入 token, 其中 60 万命中缓存, 无写入; 50 万输出.
        // 命中: 0.6 * 0.02 = 0.012; fresh: 0.4 * 1.0 = 0.4; 输出: 0.5 * 2.0 = 1.0 → 1.412
        let c = compute_cost(Some(FLASH), 1_000_000, 500_000, 600_000, 0).unwrap();
        assert!((c - 1.412).abs() < 1e-9);
    }

    #[test]
    fn compute_cost_without_cache_price_falls_back_to_input() {
        // 无 cache_read_per_m / cache_creation_per_m 时, 命中与写入均按 input 价计.
        let p = ModelPrice { input_per_m: 1.0, output_per_m: 2.0, cache_read_per_m: None, cache_creation_per_m: None };
        let c = compute_cost(Some(p), 1_000_000, 500_000, 600_000, 0).unwrap();
        // 全部输入按 1.0: 1.0 + 输出 1.0 = 2.0
        assert!((c - 2.0).abs() < 1e-9);
    }

    #[test]
    fn compute_cost_creation_uses_separate_price() {
        // 100 万输入: hit=40万(读价0.02), creation=30万(写价0.1), fresh=30万(input=1.0); 输出50万(2.0).
        // hit: 0.4*0.02=0.008; creation: 0.3*0.1=0.03; fresh: 0.3*1.0=0.3; 输出: 0.5*2=1.0 → 1.338
        let c = compute_cost(Some(FLASH), 1_000_000, 500_000, 400_000, 300_000).unwrap();
        assert!((c - 1.338).abs() < 1e-9);
    }

    #[test]
    fn compute_cost_creation_falls_back_to_input() {
        // cache_creation_per_m 缺失时, creation 按 input 价计 (等价旧行为).
        let p = ModelPrice { input_per_m: 1.0, output_per_m: 2.0, cache_read_per_m: None, cache_creation_per_m: None };
        // hit=40万(1.0), creation=30万(1.0), fresh=30万(1.0) → 输入 1.0; 输出 1.0 → 2.0
        let c = compute_cost(Some(p), 1_000_000, 500_000, 400_000, 300_000).unwrap();
        assert!((c - 2.0).abs() < 1e-9);
    }

    #[test]
    fn compute_cost_none_price_is_none() {
        assert!(compute_cost(None, 100, 100, 0, 0).is_none());
    }

    #[test]
    fn compute_cost_clamps_hit_to_prompt() {
        // 命中数大于输入总数时钳制到输入总数（防上游脏数据导致负 fresh）.
        let c = compute_cost(Some(FLASH), 100, 100, 999, 0).unwrap();
        // hit=100(按 cache 0.02), fresh=0, 输出 100/1e6*2 → 0.000002 + 0.0002 = 0.000202
        assert!((c - 0.000202).abs() < 1e-12);
    }

    #[test]
    fn resolve_price_override_beats_builtin() {
        let mut ov: HashMap<String, ModelPrice> = HashMap::new();
        ov.insert("ds-flash".into(), ModelPrice { input_per_m: 9.0, output_per_m: 9.0, cache_read_per_m: None, cache_creation_per_m: None });
        // override 优先, 无论 endpoint 是否为官方 DS.
        let p = resolve_price(ov.get("ds-flash").copied(), Some("deepseek-chat"), None).unwrap();
        assert_eq!(p.input_per_m, 9.0);
    }

    #[test]
    fn resolve_price_falls_back_to_builtin() {
        let ov: HashMap<String, ModelPrice> = HashMap::new();
        // 官方 DeepSeek 供应商 endpoint, 无 override 时回退内置表.
        let p = resolve_price(
            ov.get("ds-flash").copied(),
            Some("deepseek-v4-flash"),
            Some("https://api.deepseek.com/v1/chat/completions"),
        )
        .unwrap();
        assert_eq!(p.input_per_m, FLASH.input_per_m);
    }

    #[test]
    fn resolve_price_normalizes_free_suffix() {
        // 官方 endpoint 下, 免费/试用后缀归一化后仍命中内置表.
        assert!(resolve_price(None, Some("deepseek-v4-flash-free"), Some("https://api.deepseek.com/v1/chat/completions")).is_some());
        assert!(resolve_price(None, Some("deepseek-v4-flash-trial"), Some("https://api.deepseek.com/v1/chat/completions")).is_some());
    }

    #[test]
    fn resolve_price_unknown_is_none() {
        assert!(resolve_price(None, Some("no-such-model"), Some("https://api.deepseek.com/v1/chat/completions")).is_none());
    }

    #[test]
    fn resolve_price_gateway_no_builtin() {
        // 关键回归: 网关 (opencode/zen/go) 中转 deepseek-v4-flash, 但 endpoint 非官方 DS,
        // 不应自动套 DeepSeek 官方价 —— 必须显式配 price, 否则记"未配置".
        assert!(
            resolve_price(
                None,
                Some("deepseek-v4-flash"),
                Some("https://api.example.com/go/v1/chat/completions"),
            )
            .is_none()
        );
    }
}
