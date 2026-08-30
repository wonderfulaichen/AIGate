//! 模型价格表 — 费用统计用.
//!
//! 背景: 国内大模型供应商（DeepSeek / 阿里百炼 / 智谱 / 月之暗面 / 硅基流动等）
//! **均不提供价格查询 API**; 价格仅在官方定价文档页公开, 且会变动
//! （促销 / 阶梯 / 高峰倍率 / 版本升级）. 因此无法程序化实时拉取.
//!
//! 做法: 本模块内置一份**官方公开价的默认值**（仅 DeepSeek, 抓取于 2026-08-13,
//! 更新于 2026-08-18 以反映分时段定价, 来源
//! <https://api-docs.deepseek.com/zh-cn/quick_start/pricing>）, 其余供应商
//! 请在 `providers.json` 的 model 条目配置 `price` 覆盖（优先级最高）.
//!
//! 计价单位: 元 / 百万 tokens. 部分供应商（DeepSeek）按**时段**翻倍计费:
//! 高峰（北京时间 09:00–12:00、14:00–18:00）价为 `*_per_m`, 其余时段为空闲价
//! `*_per_m_offpeak`（缺失时回退到高峰价, 使无分时段概念的供应商不受影响）.

use serde::{Deserialize, Serialize};

/// 单个模型的价格（元 / 百万 tokens）.
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
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

/// 内置默认价格表（按 `upstream_model` 匹配）.
///
/// ⚠️ 价格会变动! 以官方文档为准; 若与实际不符, 在 `providers.json` 覆盖.
/// 表格中 `*_per_m` 为**高峰价**, `*_per_m_offpeak` 为**空闲价**（DeepSeek 分时段,
/// 高峰 09:00–12:00 / 14:00–18:00 北京时间为高峰价的 2 倍）.
fn builtin_table() -> &'static [(&'static str, ModelPrice)] {
    &[
        (
            "deepseek-v4-flash",
            ModelPrice {
                // 高峰价（元/百万 token）: 输入 3.0 / 输出 9.0 / 缓存命中 0.10.
                input_per_m: 3.0,
                output_per_m: 9.0,
                cache_read_per_m: Some(0.10),
                cache_creation_per_m: None,
                // 空闲价（高峰 1/2）: 输入 1.5 / 输出 4.5 / 缓存命中 0.05.
                input_per_m_offpeak: 1.5,
                output_per_m_offpeak: 4.5,
                cache_read_per_m_offpeak: 0.05,
            },
        ),
        (
            "deepseek-v4-pro",
            ModelPrice {
                // 高峰价（元/百万 token）: 输入 9.0 / 输出 27.0 / 缓存命中 0.30.
                input_per_m: 9.0,
                output_per_m: 27.0,
                cache_read_per_m: Some(0.30),
                cache_creation_per_m: None,
                // 空闲价（高峰 1/2）: 输入 4.5 / 输出 13.5 / 缓存命中 0.15.
                input_per_m_offpeak: 4.5,
                output_per_m_offpeak: 13.5,
                cache_read_per_m_offpeak: 0.15,
            },
        ),
    ]
}

/// DeepSeek 高峰时段（北京时间）: 09:00–12:00 与 14:00–18:00.
/// 返回 `true` 表示给定时间戳（秒, Unix）落在高峰窗口内.
///
/// 仅依赖东八区偏移, 不引入 chrono 依赖; 周末/节假日不分时段, 一律按空闲计.
pub fn is_peak(ts: u64) -> bool {
    const TZ_OFFSET_SECS: i64 = 8 * 3600;
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let secs_of_day = (local % 86400) as i64;
    // 09:00:00 (32400) ≤ t < 12:00:00 (43200) 或 14:00:00 (50400) ≤ t < 18:00:00 (64800).
    (secs_of_day >= 32400 && secs_of_day < 43200) || (secs_of_day >= 50400 && secs_of_day < 64800)
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
