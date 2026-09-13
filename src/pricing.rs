//! 模型价格表 — 费用统计用.
//!
//! 价格完全由用户在 `providers.json` 的 model 条目配置 `price` 覆盖（面板可编辑）.
//! 未配置价格的模型费用记为 0（"未配置价格"）, 不再回退任何内置默认价.
//!
//! 计价单位: 元 / 百万 tokens. 部分供应商（DeepSeek）按**时段**翻倍计费:
//! 高峰时段价为 `*_per_m`, 其余时段为空闲价 `*_per_m_offpeak`
//! （缺失时回退到高峰价, 使无分时段概念的供应商不受影响）.
//! 高峰时段（周几 / 时间段 / 时区）由 `crate::peak` 配置, 可在面板「设置」中调整.

use serde::{Deserialize, Serialize};

/// 单个模型的价格（元 / 百万 tokens）.
///
/// 派生 `PartialEq`: 价格表需要判断同一 model_id 在不同供应商下是否同价
/// (同价才允许在供应商缺失时回退匹配).
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct ModelPrice {
    /// 输入 token 价格（元 / 百万 tokens）, 不含 KV Cache 命中部分. 同时作为**高峰价**.
    pub input_per_m: f64,
    /// 输出 token 价格（元 / 百万 tokens）. 同时作为**高峰价**.
    pub output_per_m: f64,
    /// 上游 KV Cache 命中 token 价格（元 / 百万 tokens）. 同时作为**高峰价**.
    /// 缺失时回退按 `input_per_m` 计（多数模型无独立 cache 价）.
    #[serde(default)]
    pub cache_read_per_m: Option<f64>,
    /// 上游 KV Cache 首次写入 (creation) token 价格（元 / 百万 tokens）.
    /// Anthropic 等写入缓存独立计费（常为输入的 1.25x）; 缺失时回退按 `input_per_m` 计.
    /// 多数供应商（DeepSeek 等）无独立 creation 价, 此项留空即等价旧行为.
    #[serde(default)]
    pub cache_creation_per_m: Option<f64>,
    /// 空闲时段输入价（元 / 百万 tokens）. 缺省回退 `input_per_m`（高峰价）.
    /// 仅 DeepSeek 等分时段计费供应商需配置.
    #[serde(default)]
    pub input_per_m_offpeak: f64,
    /// 空闲时段输出价（元 / 百万 tokens）. 缺省回退 `output_per_m`（高峰价）.
    #[serde(default)]
    pub output_per_m_offpeak: f64,
    /// 空闲时段 KV Cache 命中价（元 / 百万 tokens）. 缺省回退 `cache_read_per_m`（高峰价）.
    #[serde(default)]
    pub cache_read_per_m_offpeak: f64,
}

/// 给定时间戳（秒, Unix）是否落在高峰时段.
///
/// 规则由 `crate::peak` 的配置决定（周几 + 时间段 + 时区, 面板可改）;
/// 关闭分时段计费时恒为 `false`, 即全天走标准价.
pub fn is_peak(ts: u64) -> bool {
    crate::peak::is_peak(ts)
}

/// 按时间戳选择生效的价格（高峰用 `*_per_m`, 空闲用 `*_per_m_offpeak`, 缺失回退高峰）.
///
/// 返回三元组 `(input, output, cache_read)`, cache_read 已展开 `Option`（缺失回退 input）.
pub fn effective(p: ModelPrice, ts: u64) -> (f64, f64, f64) {
    let (peak, offpeak) = effective_parts(p);
    if is_peak(ts) { peak } else { offpeak }
}

/// 返回 (高峰, 空闲) 两组生效单价三元组 `(input, output, cache_read)`.
/// 供日级 rollup 按时段拆分存储的计费 token 在查询期重算费用使用.
pub fn effective_parts(p: ModelPrice) -> ((f64, f64, f64), (f64, f64, f64)) {
    let peak = (
        p.input_per_m,
        p.output_per_m,
        p.cache_read_per_m.unwrap_or(p.input_per_m),
    );
    let offpeak = (
        if p.input_per_m_offpeak > 0.0 { p.input_per_m_offpeak } else { p.input_per_m },
        if p.output_per_m_offpeak > 0.0 { p.output_per_m_offpeak } else { p.output_per_m },
        if p.cache_read_per_m_offpeak > 0.0 {
            p.cache_read_per_m_offpeak
        } else {
            p.cache_read_per_m.unwrap_or(p.input_per_m)
        },
    );
    (peak, offpeak)
}

/// 解析最终价格: 仅使用 `providers.json` 的 model.price（面板配置）.
///
/// 未配置时返回 `None`, 调用方按 0 处理（该模型费用不计入统计）.
pub fn resolve_price(override_price: Option<ModelPrice>) -> Option<ModelPrice> {
    override_price
}

/// 计算单条请求费用（元）.
///
/// 计费拆分:
/// - `prompt_tokens` 为输入总量（含 KV Cache 命中）;
/// - 命中部分 = `prompt_cache_hit_tokens`, 按空闲/高峰生效的 cache 读价计;
/// - 未命中部分 = `prompt_tokens - 命中`, 按生效的 input 价计;
/// - `completion_tokens` 按生效的 output 价计.
///
/// `ts` 为请求时间戳（秒, Unix）, 用于按供应商分时段规则选择高峰/空闲价
/// （DeepSeek 高峰价为空闲 2 倍）; 无分时段概念的供应商 offpeak 字段缺失, 自动回退高峰价.
///
/// 价格缺失（未配置）时返回 `None`, 调用方按 0 处理（不计入费用）.
pub fn compute_cost(
    price: Option<ModelPrice>,
    ts: u64,
    prompt_tokens: u32,
    completion_tokens: u32,
    prompt_cache_hit_tokens: u32,
) -> Option<f64> {
    let p = price?;
    let (input_price, output_price, cache_price) = effective(p, ts);
    let prompt = prompt_tokens as f64;
    let completion = completion_tokens as f64;
    let hit = (prompt_cache_hit_tokens as f64).min(prompt);
    let miss = (prompt - hit).max(0.0);
    let cost = hit / 1e6 * cache_price + miss / 1e6 * input_price + completion / 1e6 * output_price;
    Some(cost)
}
