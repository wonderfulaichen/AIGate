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

/// 解析最终价格: `providers.json` 的 model.price 覆盖优先, 否则用内置表.
pub fn resolve_price(
    override_price: Option<ModelPrice>,
    upstream: Option<&str>,
) -> Option<ModelPrice> {
    if let Some(p) = override_price {
        return Some(p);
    }
    upstream.and_then(builtin_price)
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
) -> Option<f64> {
    let p = price?;
    let prompt = prompt_tokens as f64;
    let completion = completion_tokens as f64;
    let hit = (prompt_cache_hit_tokens as f64).min(prompt);
    let miss = (prompt - hit).max(0.0);
    let cache_price = p.cache_read_per_m.unwrap_or(p.input_per_m);
    let cost = hit / 1e6 * cache_price
        + miss / 1e6 * p.input_per_m
        + completion / 1e6 * p.output_per_m;
    Some(cost)
}
