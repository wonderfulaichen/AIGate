//! 费用显示币种配置: 读写工作目录下的 `currency.json`.
//!
//! 内部所有费用均以人民币(CNY)为基准计价. 本模块仅负责「显示币种」选择
//! 与静态汇率表, 前端据此把 CNY 金额换算到目标币种展示. 零外部依赖、零风险.
//!
//! 汇率语义: `rates[code]` = 1 单位该币种等于多少 CNY.
//!   例如 `"USD": 7.2` 表示 1 USD = 7.2 CNY, 故 CNY 金额换算到 USD 为 `amount / 7.2`.
//! 用户可在 `currency.json` 中手动调整任意币种的汇率.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

const CURRENCY_FILE: &str = "currency.json";

/// 费用显示币种配置.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CurrencyConfig {
    /// 当前选中的显示币种代码 (默认 "CNY").
    #[serde(default = "default_currency")]
    pub currency: String,
    /// 静态汇率表: code -> 1 单位该币种 = 多少 CNY. 至少包含 CNY=1.0.
    #[serde(default = "default_rates")]
    pub rates: HashMap<String, f64>,
}

fn default_currency() -> String {
    "CNY".to_string()
}

/// 默认静态汇率表 (CNY 基准). 仅作初始值, 用户可随时在 `currency.json` 调整.
fn default_rates() -> HashMap<String, f64> {
    let mut m = HashMap::new();
    m.insert("CNY".to_string(), 1.0);
    m.insert("USD".to_string(), 7.2);
    m.insert("EUR".to_string(), 7.8);
    m.insert("JPY".to_string(), 0.048);
    m.insert("GBP".to_string(), 9.1);
    m.insert("HKD".to_string(), 0.92);
    m
}

impl Default for CurrencyConfig {
    fn default() -> Self {
        Self {
            currency: default_currency(),
            rates: default_rates(),
        }
    }
}

/// 从 `currency.json` 读取配置. 文件不存在/损坏/字段缺失时回退默认.
pub fn load_config() -> CurrencyConfig {
    std::fs::read_to_string(CURRENCY_FILE)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// 持久化配置: 写入 `currency.json`.
pub fn save_config(config: &CurrencyConfig) -> std::io::Result<()> {
    let text = serde_json::to_string_pretty(config)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(CURRENCY_FILE, text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_has_cny_base_and_common_rates() {
        let c = CurrencyConfig::default();
        assert_eq!(c.currency, "CNY");
        assert_eq!(c.rates.get("CNY").copied(), Some(1.0));
        assert!(c.rates.get("USD").copied().unwrap() > 1.0);
        assert!(c.rates.get("JPY").copied().unwrap() < 1.0);
    }

    #[test]
    fn missing_fields_fall_back_to_default() {
        let c: CurrencyConfig = serde_json::from_str("{}").unwrap();
        assert_eq!(c.currency, "CNY");
        assert!(c.rates.contains_key("USD"));
    }

    #[test]
    fn partial_fields_merge_with_defaults() {
        let c: CurrencyConfig =
            serde_json::from_str(r#"{"currency":"USD"}"#).unwrap();
        assert_eq!(c.currency, "USD");
        assert_eq!(c.rates.get("CNY").copied(), Some(1.0));
    }
}
