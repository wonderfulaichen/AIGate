//! 任务栏 Tooltip 配置持久化: 读写工作目录下的 `tooltip.json`.
//!
//! 配置结构:
//! ```json
//! {
//!   "enabled": true,
//!   "metrics": {
//!     "requests_per_second": true,
//!     "avg_latency_ms": true,
//!     "cache_hit_rate": true,
//!     "gen_speed": true,
//!     "today_requests": true
//!   },
//!   "update_interval_secs": 1
//! }
//! ```

use serde::{Deserialize, Serialize};

const TOOLTIP_FILE: &str = "tooltip.json";

/// Tooltip 配置.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TooltipConfig {
    /// 是否启用 tooltip 显示.
    pub enabled: bool,
    /// 要显示的指标.
    pub metrics: TooltipMetrics,
    /// 更新间隔 (秒), 范围 1-10.
    #[serde(default = "default_update_interval")]
    pub update_interval_secs: u8,
}

/// 要显示的指标.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TooltipMetrics {
    /// 请求速度 (req/s).
    pub requests_per_second: bool,
    /// 平均延迟 (ms).
    pub avg_latency_ms: bool,
    /// 缓存命中率 (%).
    pub cache_hit_rate: bool,
    /// 生成速度 (tok/s).
    pub gen_speed: bool,
    /// 今日请求数.
    pub today_requests: bool,
}

fn default_update_interval() -> u8 {
    1
}

impl Default for TooltipConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            metrics: TooltipMetrics::default(),
            update_interval_secs: default_update_interval(),
        }
    }
}

impl Default for TooltipMetrics {
    fn default() -> Self {
        Self {
            requests_per_second: true,
            avg_latency_ms: true,
            cache_hit_rate: true,
            gen_speed: true,
            today_requests: true,
        }
    }
}

/// 从 `tooltip.json` 读取配置. 文件不存在/损坏时返回默认配置.
pub fn load_config() -> TooltipConfig {
    std::fs::read_to_string(TOOLTIP_FILE)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// 持久化配置: 写入 `tooltip.json`.
pub fn save_config(config: &TooltipConfig) -> std::io::Result<()> {
    let text = serde_json::to_string_pretty(config)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(TOOLTIP_FILE, text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_valid() {
        let config = TooltipConfig::default();
        assert!(config.enabled);
        assert_eq!(config.update_interval_secs, 1);
        assert!(config.metrics.requests_per_second);
    }
}
