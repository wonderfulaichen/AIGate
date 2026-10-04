//! 供应商余额查询模块 — 支持多种供应商的余额查询 API.
//!
//! 设计原则:
//! 1. 统一接口: `BalanceManager` 管理查询 + 手动余额 + 缓存
//! 2. 配置驱动: 通过 `providers.json` 的 `balance_endpoint` 字段配置查询地址
//! 3. 混合来源: 有 API 的供应商自动查询, 无 API 的 (如 opencode zen/go) 手动维护
//! 4. 缓存机制: API 查询结果缓存 1 小时, 手动余额即时生效
//! 5. 错误容错: 查询失败时显示错误, 不影响其他功能

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn};

/// 余额信息 (统一对外结构).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BalanceInfo {
    /// 供应商名称.
    pub provider: String,
    /// 余额 (元).
    pub balance: Option<f64>,
    /// 货币单位 (CNY/USD/等).
    pub currency: String,
    /// 最后更新时间戳 (秒).
    pub last_updated: u64,
    /// 错误信息 (查询失败时).
    pub error: Option<String>,
    /// 余额来源: "api"=自动查询, "manual"=手动设置.
    pub source: String,
    /// 是否配置了余额查询 API.
    pub has_api: bool,
    /// 已消耗量 (积分制供应商的服务端权威统计; 无消耗概念的供应商为 None).
    #[serde(default)]
    pub consumed: Option<f64>,
}

impl BalanceInfo {
    /// 创建 API 查询结果的余额信息.
    fn from_api(provider: &str, balance: Option<f64>, currency: String, now: u64, error: Option<String>) -> Self {
        Self {
            provider: provider.to_string(),
            balance,
            currency,
            last_updated: now,
            error,
            source: "api".to_string(),
            has_api: true,
            consumed: None,
        }
    }

    /// 创建手动设置的余额信息.
    fn from_manual(provider: &str, balance: f64, currency: &str, now: u64) -> Self {
        Self {
            provider: provider.to_string(),
            balance: Some(balance),
            currency: currency.to_string(),
            last_updated: now,
            error: None,
            source: "manual".to_string(),
            has_api: false,
            consumed: None,
        }
    }

    /// 创建积分制 (buddy) API 结果: 剩余 + 已消耗双值.
    fn from_credits(provider: &str, balance: f64, consumed: f64, now: u64) -> Self {
        Self {
            provider: provider.to_string(),
            balance: Some(balance),
            currency: "credits".to_string(),
            last_updated: now,
            error: None,
            source: "api".to_string(),
            has_api: true,
            consumed: Some(consumed),
        }
    }
}

/// 余额查询管理器.
#[derive(Clone)]
pub struct BalanceManager {
    /// API 查询结果缓存.
    api_cache: Arc<RwLock<HashMap<String, BalanceInfo>>>,
    /// 缓存过期时间 (秒).
    cache_ttl: u64,
    /// 手动余额存储文件路径.
    manual_file: PathBuf,
}

/// 手动余额存储内容: provider → 余额.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
struct ManualBalanceFile {
    balances: HashMap<String, f64>,
}

impl BalanceManager {
    /// 创建余额查询管理器.
    pub fn new(manual_file: PathBuf) -> Self {
        Self {
            api_cache: Arc::new(RwLock::new(HashMap::new())),
            cache_ttl: 3600, // 1小时
            manual_file,
        }
    }

    /// 设置手动余额 (持久化到文件).
    pub async fn set_manual_balance(&self, provider: &str, balance: f64) -> Result<(), String> {
        let mut file = Self::load_manual_file(&self.manual_file);
        file.balances.insert(provider.to_string(), balance);
        let json = serde_json::to_string_pretty(&file)
            .map_err(|e| format!("序列化失败: {e}"))?;
        std::fs::write(&self.manual_file, json).map_err(|e| format!("写入失败: {e}"))
    }

    /// 清除手动余额.
    pub async fn clear_manual_balance(&self, provider: &str) -> Result<(), String> {
        let mut file = Self::load_manual_file(&self.manual_file);
        file.balances.remove(provider);
        let json = serde_json::to_string_pretty(&file)
            .map_err(|e| format!("序列化失败: {e}"))?;
        std::fs::write(&self.manual_file, json).map_err(|e| format!("写入失败: {e}"))
    }

    /// 清除某供应商的 API 余额缓存.
    ///
    /// 手动设置/清除余额后调用, 使该供应商回退到手动值或在下次刷新时重新查询上游,
    /// 避免残留的「成功」缓存掩盖手动覆盖.
    pub async fn clear_cache(&self, provider: &str) {
        let mut cache = self.api_cache.write().await;
        cache.remove(provider);
    }

    /// 读取手动余额文件.
    fn load_manual_file(path: &PathBuf) -> ManualBalanceFile {
        std::fs::read_to_string(path)
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default()
    }

    /// 查询所有供应商的余额 (API 查询 + 手动余额合并).
    ///
    /// 设计要点 (修复「刷新时交替失败」):
    /// 1. 成功结果写入 `api_cache` 并跨请求复用; 缓存随 `BalanceManager` 常驻于 `AppState`,
    ///    不再每次请求重建 (旧实现每次 `new`, 缓存等于没用, 每次刷新都重新打上游).
    /// 2. **失败结果绝不写入缓存**, 保证下次刷新必定重试; 已成功的供应商则一直命中缓存.
    /// 3. 按 `(balance_endpoint, api_key)` 去重: 多个供应商若共用同一上游账号/密钥,
    ///    本次刷新只打一次上游, 避免「第二个请求被限流」导致交替失败.
    pub async fn query_all_balances(
        &self,
        client: &reqwest::Client,
        provider_configs: &HashMap<String, ProviderBalanceConfig>,
        accounts: &crate::accounts::AccountStore,
    ) -> Vec<BalanceInfo> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let manual = Self::load_manual_file(&self.manual_file);
        let mut results: Vec<BalanceInfo> = Vec::new();

        // 待查询队列: 排除手动覆盖与「成功」缓存命中的供应商.
        struct ToQuery {
            provider: String,
            endpoint: String,
            api_key: String,
        }
        let mut to_query: Vec<ToQuery> = Vec::new();

        for (provider_name, config) in provider_configs {
            // 手动余额优先 (用户手动设置的覆盖 API 查询结果)
            if let Some(manual_balance) = manual.balances.get(provider_name) {
                results.push(BalanceInfo::from_manual(
                    provider_name,
                    *manual_balance,
                    "CNY",
                    now,
                ));
                continue;
            }

            // 仅命中「成功」缓存才复用; 失败不缓存, 故此处不会出现陈旧失败.
            let cache = self.api_cache.read().await;
            if let Some(cached) = cache.get(provider_name) {
                if cached.error.is_none() && now - cached.last_updated < self.cache_ttl {
                    results.push(cached.clone());
                    continue;
                }
            }
            drop(cache);

            match &config.balance_endpoint {
                Some(ep) => to_query.push(ToQuery {
                    provider: provider_name.clone(),
                    endpoint: ep.clone(),
                    api_key: config.api_key.clone(),
                }),
                None if config.preset.as_deref() == Some("buddy") => {
                    // 内置 buddy 预设: 走 CodeBuddy 积分端点 (POST + 账号 token),
                    // 无需 balance_endpoint 配置 —— 预设自带账号形式.
                    to_query.push(ToQuery {
                        provider: provider_name.clone(),
                        endpoint: BUDDY_CREDITS_SCHEME.to_string(),
                        api_key: String::new(), // 凭据来自 accounts.json, 不参与去重键
                    })
                }
                None => results.push(BalanceInfo::from_api(
                    provider_name,
                    None,
                    "CNY".to_string(),
                    now,
                    Some("未配置余额查询 API".to_string()),
                )),
            }
        }

        // 按 (endpoint, api_key) 去重, 同一上游只查询一次.
        // ⚠️ buddy 积分**不参与去重**: 两个预设条目可绑不同账号 (accounts 按 name 索引),
        // 合并共享结果会把 A 的积分显示到 B 头上 (e2e 实测撞出).
        let mut grouped: std::collections::BTreeMap<(String, String), Vec<String>> =
            std::collections::BTreeMap::new();
        let mut buddy_only: Vec<String> = Vec::new();
        for q in &to_query {
            if q.endpoint == BUDDY_CREDITS_SCHEME {
                buddy_only.push(q.provider.clone());
            } else {
                grouped
                    .entry((q.endpoint.clone(), q.api_key.clone()))
                    .or_default()
                    .push(q.provider.clone());
            }
        }

        // buddy: 逐供应商查询 (各自账号) + 独立缓存.
        for p in buddy_only {
            let b = Self::query_buddy_credits(client, accounts, &p).await;
            if b.error.is_none() {
                let mut cache = self.api_cache.write().await;
                cache.insert(p.clone(), b.clone());
            }
            results.push(b);
        }

        for ((endpoint, api_key), providers) in grouped {
            // 通用 GET + Bearer (buddy 已在上面单独处理, 不会进入本分支).
            let cfg = ProviderBalanceConfig {
                balance_endpoint: Some(endpoint),
                api_key,
                preset: None,
            };
            let balance = self.query_provider_balance(client, &providers[0], &cfg).await;
            // 仅以首个供应商名打日志; 结果会复制到同组所有供应商.
            for p in &providers {
                let mut b = balance.clone();
                b.provider = p.clone();
                // 仅缓存成功结果; 失败留待下次刷新重试.
                if b.error.is_none() {
                    let mut cache = self.api_cache.write().await;
                    cache.insert(p.clone(), b.clone());
                }
                results.push(b);
            }
        }

        // 处理只有手动余额的供应商 (未配置 API 但手动设置了)
        for (provider_name, manual_balance) in &manual.balances {
            if !provider_configs.contains_key(provider_name) {
                results.push(BalanceInfo::from_manual(
                    provider_name,
                    *manual_balance,
                    "CNY",
                    now,
                ));
            }
        }

        results
    }

    /// 查询单个供应商的余额.
    async fn query_provider_balance(
        &self,
        client: &reqwest::Client,
        provider_name: &str,
        config: &ProviderBalanceConfig,
    ) -> BalanceInfo {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        if let Some(endpoint) = &config.balance_endpoint {
            info!(target: "balance", "查询余额: provider='{}' endpoint={}", provider_name, endpoint);
            match client
                .get(endpoint)
                .header("Authorization", format!("Bearer {}", config.api_key))
                // DeepSeek 等供应商余额接口要求 Accept: application/json (官方示例明确携带),
                // 缺省 reqwest 仅发 Accept: */*, 可能被拒 (406/非 JSON).
                .header("Accept", "application/json")
                .timeout(std::time::Duration::from_secs(10))
                .send()
                .await
            {
                Ok(resp) => {
                    let status = resp.status();
                    if status.is_success() {
                        match resp.json::<serde_json::Value>().await {
                            Ok(json) => {
                                // 从响应中提取余额和货币
                                let (balance, currency) = extract_balance_from_json(&json);
                                if balance.is_none() {
                                    warn!(target: "balance", "provider='{}' 响应成功但无余额字段: {}", provider_name, json);
                                }
                                BalanceInfo::from_api(
                                    provider_name,
                                    balance,
                                    currency,
                                    now,
                                    if balance.is_none() {
                                        Some("响应中未找到余额字段".to_string())
                                    } else {
                                        None
                                    },
                                )
                            }
                            Err(e) => {
                                warn!(target: "balance", "provider='{}' 解析响应失败: {}", provider_name, e);
                                BalanceInfo::from_api(
                                    provider_name,
                                    None,
                                    "CNY".to_string(),
                                    now,
                                    Some(format!("解析响应失败: {e}")),
                                )
                            }
                        }
                    } else {
                        // 非成功状态码: 读响应体用于诊断 (截断避免刷屏; 必须按字符边界,
                        // 中文错误体在字节位截断会 panic —— 见 i18n::snip 的实机事故注释).
                        let body = resp.text().await.unwrap_or_default();
                        let truncated = crate::i18n::snip(&body, 300);
                        warn!(target: "balance", "provider='{}' 余额查询 HTTP {}: {}", provider_name, status, truncated);
                        // 将技术性错误转换为用户友好的提示
                        let user_friendly_msg = match status.as_u16() {
                            401 => "API Key 无效或未配置".to_string(),
                            403 => "API Key 无余额查询权限".to_string(),
                            404 => "余额查询端点不存在".to_string(),
                            429 => "请求过于频繁".to_string(),
                            500..=599 => "服务端错误".to_string(),
                            _ => format!("HTTP {}", status),
                        };
                        BalanceInfo::from_api(
                            provider_name,
                            None,
                            "CNY".to_string(),
                            now,
                            Some(user_friendly_msg),
                        )
                    }
                }
                Err(e) => {
                    warn!(target: "balance", "provider='{}' 请求失败: {}", provider_name, e);
                    // 将网络错误转换为用户友好的提示
                    let user_friendly_msg = if e.is_timeout() {
                        "请求超时".to_string()
                    } else if e.is_connect() {
                        "连接失败".to_string()
                    } else {
                        format!("网络错误: {e}")
                    };
                    BalanceInfo::from_api(
                        provider_name,
                        None,
                        "CNY".to_string(),
                        now,
                        Some(user_friendly_msg),
                    )
                }
            }
        } else {
            BalanceInfo::from_api(
                provider_name,
                None,
                "CNY".to_string(),
                now,
                Some("未配置余额查询 API".to_string()),
            )
        }
    }
}

/// 供应商余额配置.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct ProviderBalanceConfig {
    /// 余额查询 API 端点.
    pub balance_endpoint: Option<String>,
    /// API Key.
    pub api_key: String,
    /// 内置预设标记 (如 "buddy") — 无 balance_endpoint 也可走预设专属余额实现.
    #[serde(default)]
    pub preset: Option<String>,
}

/// buddy 预设的积分查询在查询队列里的伪端点 (分派到 [`BalanceManager::query_buddy_credits`]).
const BUDDY_CREDITS_SCHEME: &str = "buddy://credits";

impl BalanceManager {
    /// CodeBuddy 积分余额 (harness credits.ts 同款端点与头):
    ///   POST /v2/billing/meter/get-user-resource  body {}
    /// 响应 `data.Response.Data.Accounts[]` 每个资源包带
    /// `CapacityRemain`(剩余) / `CapacityUsed`(已消耗) —— 服务端权威统计, 不做估算.
    /// 凭据 = accounts.json 里该供应商的账号 token (登录/续期链见 accounts.rs).
    async fn query_buddy_credits(
        client: &reqwest::Client,
        accounts: &crate::accounts::AccountStore,
        provider: &str,
    ) -> BalanceInfo {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let Some(token) = accounts.get(provider).await.map(|e| e.access_token) else {
            return BalanceInfo::from_api(
                provider,
                None,
                "credits".to_string(),
                now,
                Some("未登录账号（供应商卡片点「登录」）".to_string()),
            );
        };
        let resp = client
            .post("https://copilot.tencent.com/v2/billing/meter/get-user-resource")
            .header("Authorization", format!("Bearer {token}"))
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .header("X-Domain", "copilot.tencent.com")
            .header("X-Product", "CodeBuddy")
            .header("X-Product-Code", "codebuddy")
            .header("User-Agent", "CodeBuddyIDE/1.106.1")
            .body("{}")
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await;
        match resp {
            Ok(r) => {
                let status = r.status();
                let body = r.text().await.unwrap_or_default();
                if !status.is_success() {
                    let friendly = match status.as_u16() {
                        401 => "账号未登录或凭据失效（重新登录）".to_string(),
                        429 => "请求过于频繁".to_string(),
                        500..=599 => "服务端错误".to_string(),
                        _ => format!("HTTP {status}"),
                    };
                    warn!(target: "balance", "buddy 积分查询 HTTP {status}: {}", crate::i18n::snip(&body, 200));
                    return BalanceInfo::from_api(
                        provider,
                        None,
                        "credits".to_string(),
                        now,
                        Some(friendly),
                    );
                }
                let v: serde_json::Value = match serde_json::from_str(&body) {
                    Ok(v) => v,
                    Err(e) => {
                        return BalanceInfo::from_api(
                            provider,
                            None,
                            "credits".to_string(),
                            now,
                            Some(format!("解析响应失败: {e}")),
                        )
                    }
                };
                // data.Response.Data.Accounts[] (兼容 data 下直接是结构的变体)
                let accounts_arr = v
                    .pointer("/data/Response/Data/Accounts")
                    .or_else(|| v.pointer("/data/Accounts"))
                    .and_then(|a| a.as_array());
                let Some(arr) = accounts_arr else {
                    return BalanceInfo::from_api(
                        provider,
                        None,
                        "credits".to_string(),
                        now,
                        Some("响应中未找到积分包字段".to_string()),
                    );
                };
                let (mut remain, mut used) = (0f64, 0f64);
                for pkg in arr {
                    remain += pkg
                        .get("CapacityRemain")
                        .and_then(|x| x.as_f64())
                        .unwrap_or(0.0);
                    used += pkg
                        .get("CapacityUsed")
                        .and_then(|x| x.as_f64())
                        .unwrap_or(0.0);
                }
                info!(target: "balance", "buddy 积分: 剩余 {remain} / 已消耗 {used} ({} 个资源包)", arr.len());
                BalanceInfo::from_credits(provider, remain, used, now)
            }
            Err(e) => {
                warn!(target: "balance", "buddy 积分查询网络错误: {e}");
                BalanceInfo::from_api(
                    provider,
                    None,
                    "credits".to_string(),
                    now,
                    Some(if e.is_timeout() {
                        "请求超时".to_string()
                    } else {
                        format!("网络错误: {e}")
                    }),
                )
            }
        }
    }
}

/// 从 JSON 响应中提取 (余额, 货币). 支持多种格式:
/// 1. DeepSeek: `balance_infos: [{currency, total_balance}]`
/// 2. 常见扁平字段: balance/amount/credits/remaining/quota
/// 3. 嵌套: data.balance 等
fn extract_balance_from_json(json: &serde_json::Value) -> (Option<f64>, String) {
    // 格式 1: DeepSeek balance_infos
    if let Some(infos) = json.get("balance_infos").and_then(|v| v.as_array()) {
        if let Some(first) = infos.first() {
            let currency = normalize_currency(
                first.get("currency").and_then(|c| c.as_str()).unwrap_or("CNY"),
            );
            let balance = first
                .get("total_balance")
                .and_then(|b| b.as_str())
                .and_then(|s| s.parse::<f64>().ok())
                .or_else(|| first.get("total_balance").and_then(|b| b.as_f64()));
            if balance.is_some() {
                return (balance, currency);
            }
        }
    }

    // 格式 2: 常见扁平字段. 同时尝试从响应里提取币种 (如 OpenAI/ofox: {"currency":"usd","balance":6}).
    let fields = ["balance", "amount", "credits", "remaining", "quota", "remaining_quota"];
    for field in &fields {
        if let Some(value) = json.get(*field) {
            let num = value.as_f64().or_else(|| value.as_str().and_then(|s| s.parse::<f64>().ok()));
            if let Some(num) = num {
                return (Some(num), extract_currency_from_json(json));
            }
        }
    }

    // 格式 3: 嵌套 data 对象
    if let Some(data) = json.get("data") {
        let (balance, currency) = extract_balance_from_json(data);
        if balance.is_some() {
            return (balance, currency);
        }
    }

    (None, "CNY".to_string())
}

/// 从响应 JSON 中提取币种并归一化 (大写 ISO 码). 支持常见字段名与书写形式.
fn extract_currency_from_json(json: &serde_json::Value) -> String {
    let keys = ["currency", "unit", "cash_unit", "currency_type", "cash_currency"];
    for key in &keys {
        if let Some(v) = json.get(*key).and_then(|c| c.as_str()) {
            let norm = normalize_currency(v);
            if !norm.is_empty() {
                return norm;
            }
        }
    }
    "CNY".to_string()
}

/// 币种归一化: 接受 "usd"/"USD"/"$" / "cny"/"¥" 等, 返回大写的 ISO 码; 无法识别返回空串.
fn normalize_currency(raw: &str) -> String {
    let s = raw.trim().to_uppercase();
    if s.is_empty() {
        return String::new();
    }
    // 直接匹配 ISO 码 (允许前后缀, 如 "USD"/"usd")
    if s == "USD" || s == "CNY" || s == "EUR" || s == "JPY" || s == "GBP" || s == "HKD" {
        return s;
    }
    // 符号或别名
    match s.as_str() {
        "$" | "DOLLAR" | "US$" => "USD".to_string(),
        "¥" | "RMB" | "元" => "CNY".to_string(),
        "€" => "EUR".to_string(),
        "£" => "GBP".to_string(),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_deepseek_balance_infos() {
        let json = serde_json::json!({
            "is_available": true,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00"
                }
            ]
        });
        let (balance, currency) = extract_balance_from_json(&json);
        assert_eq!(balance, Some(110.0));
        assert_eq!(currency, "CNY");
    }

    #[test]
    fn test_flat_balance() {
        let json = serde_json::json!({
            "balance": 100.5,
            "currency": "CNY"
        });
        let (balance, _) = extract_balance_from_json(&json);
        assert_eq!(balance, Some(100.5));
    }

    #[test]
    fn test_balance_string() {
        let json = serde_json::json!({
            "balance": "50.0"
        });
        let (balance, _) = extract_balance_from_json(&json);
        assert_eq!(balance, Some(50.0));
    }

    #[test]
    fn test_nested_data() {
        let json = serde_json::json!({
            "data": {
                "balance": 88.8
            }
        });
        let (balance, _) = extract_balance_from_json(&json);
        assert_eq!(balance, Some(88.8));
    }

    #[test]
    fn test_manual_file_roundtrip() {
        let dir = std::env::temp_dir();
        let path = dir.join("aigate_test_balance.json");
        let manager = BalanceManager::new(path.clone());
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            manager.set_manual_balance("zen", 12.5).await.unwrap();
            manager.set_manual_balance("go", 88.0).await.unwrap();
        });
        let loaded = BalanceManager::load_manual_file(&path);
        assert_eq!(loaded.balances.get("zen"), Some(&12.5));
        assert_eq!(loaded.balances.get("go"), Some(&88.0));
        let _ = std::fs::remove_file(&path);
    }
}
