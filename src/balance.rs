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
    /// **最近会过期**的那批积分的到期时刻 (Unix 秒; 仅积分制供应商).
    ///
    /// **最近会重置的配额窗口**的结束时刻 (Unix 秒; 仅积分制供应商).
    ///
    /// 取「窗口内仍有剩余 (`CycleCapacityRemain > 0`) 的包里 `CycleEndTime` 最早者」——
    /// 已耗尽的窗口不会造成损失, 不该占据这个位置.
    /// ⚠️ **不是 `DeductionEndTime`** (那是扣费有效期, 实测 2034/2035 年, 与配额重置无关).
    #[serde(default)]
    pub expiring_at: Option<u64>,
    /// 与 [`Self::expiring_at`] 同一窗口内**还剩多少**额度 (到窗口结束即作废这么多).
    #[serde(default)]
    pub expiring_amount: Option<f64>,
    /// **本窗口额度进度** (仅积分制, 且仅当上游给了窗口字段时才有值).
    ///
    /// 现状补的缺口: 面板只知道配额窗口**何时结束** (`expiring_at`), 不知道
    /// **本窗口已经用了多少 %** —— 于是"快用完了"和"几乎没用"看起来一样,
    /// 用户只能等到报错才发现没额度 (§7 待办②).
    ///
    /// ⚠️ **口径: 只统计「仍有剩余」的包** (`CycleCapacityRemain > 0`), 不是全部包.
    ///     实测 (2026-10-10, 34 个包): 若把已耗尽的旧包也算进去, ΣCycleCapacitySize
    ///     = 4463 / ΣCycleCapacityUsed = 3682 → 会报「已用 82.5%」, 而那些包是过去
    ///     一个月陆续赠送的**体验包/活动包**, 早已用完 —— 它们不会造成损失,
    ///     不该拉高进度. 只看还有额度的 5 个包: 807 / 26 → **3.2%**, 才是事实.
    #[serde(default)]
    pub quota_progress: Option<QuotaProgress>,
    /// **当前使用账号**的显示名 (多账号; 昵称/登录名).
    ///
    /// 为什么余额行要带账号名与"每个号各自多少分": `balance`/`consumed` 取的是**当前账号**
    /// 自己的数值 (不是合计) —— 否则切换账号后数字不变, 用户会以为切换是假的
    /// (2026-10-07 反馈). 合计另放 `total_*`.
    #[serde(default)]
    pub account_label: Option<String>,
    /// 该供应商已登录的账号数 (>1 时面板才显示合计).
    #[serde(default)]
    pub account_count: Option<usize>,
    /// 全部账号的合计剩余 / 合计已消耗 (仅多账号时有意义).
    #[serde(default)]
    pub total_balance: Option<f64>,
    #[serde(default)]
    pub total_consumed: Option<f64>,
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
            expiring_at: None,
            expiring_amount: None,
            quota_progress: None,
            account_label: None,
            account_count: None,
            total_balance: None,
            total_consumed: None,
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
            expiring_at: None,
            expiring_amount: None,
            quota_progress: None,
            account_label: None,
            account_count: None,
            total_balance: None,
            total_consumed: None,
        }
    }

    /// 创建积分制 (buddy) API 结果: 剩余 + 已消耗 + (可选) 最近过期/最近重置与余量/本窗口进度.
    fn from_credits(
        provider: &str,
        balance: f64,
        consumed: f64,
        expiring: Option<(u64, f64)>,
        progress: Option<QuotaProgress>,
        now: u64,
    ) -> Self {
        Self {
            provider: provider.to_string(),
            balance: Some(balance),
            currency: "credits".to_string(),
            last_updated: now,
            error: None,
            source: "api".to_string(),
            has_api: true,
            consumed: Some(consumed),
            expiring_at: expiring.map(|(t, _)| t),
            expiring_amount: expiring.map(|(_, a)| a),
            quota_progress: progress,
            account_label: None,
            account_count: None,
            total_balance: None,
            total_consumed: None,
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

/// 本窗口额度进度 (由积分包聚合而来).
///
/// 只描述「还有额度的那些包」的合计 —— 已耗尽的包不参与, 见 [`BalanceInfo::quota_progress`.
#[derive(Debug, Clone, Copy, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct QuotaProgress {
    /// 合计窗口总量 (Σ`CycleCapacitySize`).
    pub window_size: f64,
    /// 合计已用 (Σ`CycleCapacityUsed`).
    pub window_used: f64,
    /// 合计剩余 (Σ`CycleCapacityRemain`).
    pub window_remain: f64,
    /// 已用百分比 (0..100). `window_size == 0` 时为 `None` —— **没有窗口信息**,
    /// 与"窗口里有 0 额度"是两回事, 不能显示成 0%.
    pub used_pct: Option<f64>,
    /// 参与统计的资源包数 (样本量展示用; 只有一个包时前端不该暗示"多家供应商").
    pub packages: usize,
}

/// 额度进度达到此百分比即记一条「额度将尽」台账.
///
/// 80% 的取舍: 留出**可行动的余量** —— 报到 95% 才说, 用户基本已经用完了;
/// 报到 50% 又太吵 (半个窗口就喊, 而大多数窗口本来就会用到一半以上)。
pub const QUOTA_LOW_PCT: f64 = 80.0;

/// 聚合 buddy 的积分资源包.
///
/// 返回 `(总剩余, 总已消耗, 最近会重置的窗口 (时刻, 该窗口剩余), 还有额度的包的进度)`.
///
/// ⚠️ **"最近会重置"取 `CycleEndTime`, 不是 `DeductionEndTime`** (2026-10-08 实测更正):
/// 后者是"扣费有效期"(实测 2034/2035 年), 与配额何时重置无关, 取它会得出
/// "十几年后到期"这种无用且误导的数字. `CycleEndTime` 才是窗口结束时刻
/// (`CapacityType=4` 为自然月末 "2026-10-31 23:59:59"; `CapacityType=1` 为各自周期末).
///
/// ⚠️ **进度只算「仍有剩余」的包**: 见 [`QuotaProgress` 的口径说明. 2026-10-10 实测
/// 34 个包横跨 34 个窗口, 全加会算出 82.5% (把已耗尽的体验包/活动包算进去).
pub fn aggregate_credits(arr: &[serde_json::Value]) -> (f64, f64, Option<(u64, f64)>, Option<QuotaProgress>) {
    let (mut remain, mut used) = (0f64, 0f64);
    let mut expiring: Option<(u64, f64)> = None;
    // 只统计"仍有剩余"的包 (与 expiring 的筛选口径一致).
    let mut win_size = 0f64;
    let mut win_used = 0f64;
    let mut win_remain = 0f64;
    let mut win_pkgs = 0usize;
    for pkg in arr {
        let rem = pkg
            .get("CapacityRemain")
            .and_then(|x| x.as_f64())
            .unwrap_or(0.0);
        remain += rem;
        used += pkg
            .get("CapacityUsed")
            .and_then(|x| x.as_f64())
            .unwrap_or(0.0);
        // 窗口内剩余: 优先 Cycle* (CapacityRemain 是跨周期累计, 不能判断"这窗口还剩多少").
        let cycle_rem = pkg
            .get("CycleCapacityRemain")
            .or_else(|| pkg.get("CapacityRemain"))
            .and_then(|x| x.as_f64())
            .unwrap_or(0.0);
        if cycle_rem > 0.0 {
            // CycleEndTime 是字符串 "YYYY-MM-DD HH:MM:SS" (不是时间戳).
            if let Some(secs) = pkg
                .get("CycleEndTime")
                .and_then(|x| x.as_str())
                .and_then(parse_datetime)
            {
                if expiring.map(|(t, _)| secs < t).unwrap_or(true) {
                    expiring = Some((secs, cycle_rem));
                }
            }
            // 本窗口进度: 同一个包里累计 (与 expiring 同一个 if, 不会漏计).
            win_remain += cycle_rem;
            win_size += pkg
                .get("CycleCapacitySize")
                .or_else(|| pkg.get("CapacitySize"))
                .and_then(|x| x.as_f64())
                .unwrap_or(0.0);
            win_used += pkg
                .get("CycleCapacityUsed")
                .or_else(|| pkg.get("CapacityUsed"))
                .and_then(|x| x.as_f64())
                .unwrap_or(0.0);
            win_pkgs += 1;
        }
    }
    let progress = if win_pkgs > 0 && win_size > 0.0 {
        Some(QuotaProgress {
            window_size: win_size,
            window_used: win_used,
            window_remain: win_remain,
            // size 已知才谈得上百分比; size 缺失 (老包只有 Capacity*) 时给 None.
            used_pct: Some((win_used / win_size * 100.0).clamp(0.0, 100.0)),
            packages: win_pkgs,
        })
    } else {
        None
    };
    (remain, used, expiring, progress)
}

/// 解析上游的 `"YYYY-MM-DD HH:MM:SS"` (本机时区, 与面板显示口径一致).
///
/// 上游给的是**字符串**且是本地时间 —— 直接按本机时区解析。失败返回 None
/// (该包不参与"最近重置"判定, 不影响总额)。
pub fn parse_datetime(s: &str) -> Option<u64> {
    let s = s.trim();
    let (date, time) = s.split_once(' ')?;
    if !time.contains(':') {
        return None;
    }
    let mut dp = date.split('-');
    let y = dp.next()?.parse::<i32>().ok()?;
    let m = dp.next()?.parse::<u32>().ok()?;
    let d = dp.next()?.parse::<u32>().ok()?;
    let mut tp = time.split(':');
    let hh = tp.next()?.parse::<u32>().ok()?;
    let mm = tp.next()?.parse::<u32>().ok()?;
    let ss = tp.next()?.parse::<u32>().ok()?;
    if hh > 23 || mm > 59 || ss > 59 || m < 1 || m > 12 || d < 1 || d > 31 {
        return None;
    }
    // chrono 旧版无 with_ymd_and_hms, 走 NaiveDate/NaiveTime + 本地时区.
    use chrono::TimeZone;
    let nd = chrono::NaiveDate::from_ymd_opt(y, m, d)?;
    let nt = chrono::NaiveTime::from_hms_opt(hh, mm, ss)?;
    let local = chrono::Local
        .from_local_datetime(&chrono::NaiveDateTime::new(nd, nt))
        .single()?;
    Some(local.timestamp().max(0) as u64)
}

/// 一个账号的配额快照 (选号决策用; 由余额查询写入, 选号读取).
#[derive(Debug, Clone)]
pub struct QuotaSnapshot {
    /// 账号 id (多账号下唯一的匹配键).
    pub account_id: String,
    /// token 前缀 (id 缺失时的兜底匹配; 不存完整 token).
    pub token_prefix: String,
    /// 该账号的剩余总额度.
    pub remain: f64,
    /// 最近会重置的窗口结束时刻.
    pub cycle_end: Option<u64>,
    pub updated_at: u64,
}

static QUOTA_SNAPSHOTS: std::sync::OnceLock<std::sync::Mutex<Vec<QuotaSnapshot>>> =
    std::sync::OnceLock::new();

fn quota_slots() -> &'static std::sync::Mutex<Vec<QuotaSnapshot>> {
    QUOTA_SNAPSHOTS.get_or_init(|| std::sync::Mutex::new(Vec::new()))
}

/// 记录某账号的配额 (余额查询成功后调用).
pub fn record_quota(account_id: &str, token: &str, remain: f64, cycle_end: Option<u64>, now: u64) {
    let prefix: String = token.chars().take(24).collect();
    let mut v = quota_slots().lock().unwrap_or_else(|e| e.into_inner());
    match v
        .iter_mut()
        .find(|q| !account_id.is_empty() && q.account_id == account_id)
    {
        Some(q) => {
            q.remain = remain;
            q.cycle_end = cycle_end;
            q.updated_at = now;
            q.token_prefix = prefix;
        }
        None => v.push(QuotaSnapshot {
            account_id: account_id.to_string(),
            token_prefix: prefix,
            remain,
            cycle_end,
            updated_at: now,
        }),
    }
}

/// 选号用: 读取全部账号的配额快照 (不打上游 —— 选号在请求关键路径上).
pub fn quota_cache_snapshot() -> Vec<QuotaSnapshot> {
    quota_slots().lock().map(|v| v.clone()).unwrap_or_default()
}

/// **选号用**: 读某账号的配额视图 `(剩余总额, 最近窗口重置时刻)`.
///
/// 只走快照, 不打上游。数据缺失或超过 1 小时 → None (调用方回落 `order` 策略)。
pub fn cached_quota_view(token: &str, account_id: &str, now: u64) -> Option<(f64, u64)> {
    let snap = quota_cache_snapshot();
    let hit = snap
        .iter()
        .find(|q| !account_id.is_empty() && q.account_id == account_id)
        .or_else(|| {
            snap.iter()
                .find(|q| q.token_prefix.len() >= 12 && token.starts_with(&q.token_prefix))
        })?;
    if now.saturating_sub(hit.updated_at) > 3600 {
        return None;
    }
    hit.cycle_end.map(|end| (hit.remain, end))
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
        // **账号型**供应商 (配了 balance_endpoint 但在 accounts.json 里有账号) 也要逐账号查:
        // 按 (endpoint, api_key) 去重时它们共用同一把静态 key, 只会被查一次 ——
        // 于是配额快照只记下一个账号, 选号策略 (pace) 因数据不齐静默回落 order
        // (2026-10-08 实测撞出). 逐账号查才能给每个候选都记上额度与重置窗口.
        let account_based: std::collections::HashSet<String> = accounts
            .providers_with_accounts()
            .await
            .into_iter()
            .collect();
        let mut account_query: Vec<String> = Vec::new();
        for q in &to_query {
            if q.endpoint == BUDDY_CREDITS_SCHEME {
                buddy_only.push(q.provider.clone());
            } else if account_based.contains(&q.provider) {
                account_query.push(q.provider.clone());
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

        // 账号型 + 通用余额端点: 逐账号用各自的 token 查询 (记配额快照, 供选号策略).
        for p in account_query {
            let Some(cfg) = provider_configs.get(&p) else { continue };
            let Some(ep) = cfg.balance_endpoint.clone() else { continue };
            let auth = cfg.auth.clone();
            let b = self
                .query_account_quotas(client, accounts, &p, &ep, auth.as_ref())
                .await;
            if b.error.is_none() {
                let mut cache = self.api_cache.write().await;
                cache.insert(p.clone(), b.clone());
            }
            results.push(b);
        }

        for ((endpoint, api_key), providers) in grouped {
            // 通用 GET + 鉴权头 (buddy 已在上面单独处理, 不会进入本分支).
            let cfg = ProviderBalanceConfig {
                balance_endpoint: Some(endpoint),
                api_key,
                preset: None,
                // 分组键只带 (endpoint, api_key), 但同组供应商共用一个上游 → auth 取首个即可.
                auth: provider_configs
                    .get(&providers[0])
                    .and_then(|c| c.auth.clone()),
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
            // 鉴权头按 auth 声明 (缺省 Authorization: Bearer) —— 与转发路径同源
            let req = crate::providers::with_auth_header(
                client.get(endpoint),
                config.auth.as_ref(),
                &config.api_key,
            )
            // DeepSeek 等供应商余额接口要求 Accept: application/json (官方示例明确携带),
            // 缺省 reqwest 仅发 Accept: */*, 可能被拒 (406/非 JSON).
            .header("Accept", "application/json")
            .timeout(std::time::Duration::from_secs(10));
            match req.send().await {
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
    /// 鉴权头声明 (缺省 `Authorization: Bearer`) —— 余额查询与转发用**同一份**
    /// `auth` 段, 否则非标准鉴权头的供应商会出现"对话通、余额查不了"的半通状态.
    #[serde(default)]
    pub auth: Option<crate::providers::AuthConfig>,
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
        // 多账号 (2026-10-07): **逐号查询后累加** —— 每个账号的积分包独立,
        // 面板的"剩余/已用"应是全部账号之和; 最近过期取所有账号里最早的那个批次.
        let list = accounts.entries(provider).await;
        if list.is_empty() {
            return BalanceInfo::from_api(
                provider,
                None,
                "credits".to_string(),
                now,
                Some("未登录账号（供应商卡片点「登录」）".to_string()),
            );
        }
        let (mut remain_total, mut used_total) = (0f64, 0f64);
        let mut packages = 0usize;
        let mut expiring: Option<(u64, f64)> = None;
        let mut first_err: Option<String> = None;
        let mut ok_accounts = 0usize;
        // 每个账号各自的 (剩余, 已消耗, 本窗口进度) —— 主值取"当前使用账号"的那一份.
        // 进度也必须按账号分: 多账号的额度窗口互不相干 (实测两个号各有一批资源包),
        // 混算会得出一个谁都不是的百分比.
        let mut per_account: HashMap<String, (f64, f64, Option<QuotaProgress>)> = HashMap::new();
        for acct in &list {
            match Self::query_one_buddy_credits(client, &acct.access_token).await {
                Ok((remain, used, exp, prog, n)) => {
                    ok_accounts += 1;
                    remain_total += remain;
                    used_total += used;
                    packages += n;
                    per_account.insert(acct.id.clone(), (remain, used, prog));
                    // 供选号策略 (pace) 使用: 记下该账号的额度与最近重置窗口.
                    record_quota(&acct.id, &acct.access_token, remain, exp.map(|(t, _)| t), now);
                    if let Some((t, a)) = exp {
                        if expiring.map_or(true, |(et, _)| t < et) {
                            expiring = Some((t, a));
                        }
                    }
                }
                Err(e) => {
                    warn!(target: "balance", "buddy 积分查询失败 (账号 {}): {e}", crate::i18n::snip(&acct.id, 12));
                    if first_err.is_none() {
                        first_err = Some(e);
                    }
                }
            }
        }
        if ok_accounts == 0 {
            // 全部账号都失败 → 报第一个错误 (与单账号时代行为一致).
            return BalanceInfo::from_api(
                provider,
                None,
                "credits".to_string(),
                now,
                first_err.or_else(|| Some("积分查询失败".to_string())),
            );
        }
        // **主值取"当前使用账号"自己的数值** (不是合计): 切换账号后余额行必须跟着变,
        // 否则用户会以为切换是假的 (2026-10-07 反馈). 合计另放 total_*.
        let active_id = list
            .iter()
            .find(|e| e.is_available(now))
            .map(|e| e.id.clone())
            .or_else(|| list.first().map(|e| e.id.clone()));
        info!(
            target: "balance",
            "buddy 积分: 合计剩余 {remain_total} / 已消耗 {used_total} ({ok_accounts}/{} 个账号共 {packages} 个资源包), 最近过期={:?}",
            list.len(), expiring
        );
        let mut info = BalanceInfo::from_credits(
            provider,
            remain_total,
            used_total,
            expiring,
            // 进度: 单账号时直接用它; 多账号时下面按"当前使用账号"覆盖.
            // 取第一个成功的 (反正马上会被覆盖, 只为不让字段空着).
            per_account.values().find(|v| v.2.is_some()).and_then(|v| v.2),
            now,
        );
        info.account_count = Some(list.len());
        info.total_balance = Some(remain_total);
        info.total_consumed = Some(used_total);
        if let Some(acct) = list.iter().find(|e| Some(&e.id) == active_id.as_ref()) {
            if let Some((r, u, prog)) = per_account.get(&acct.id) {
                info.balance = Some(*r);
                info.consumed = Some(*u);
                info.quota_progress = *prog;
            }
            info.account_label = crate::accounts::account_display_name(&acct.access_token)
                .or_else(|| acct.label.clone());
        }
        // 部分账号失败时保留提示 (总额仍可用, 但用户该知道有号没算进来).
        if let Some(e) = first_err {
            info.error = Some(format!(
                "{}: {e}",
                crate::i18n::pick("部分账号查询失败", "some accounts failed")
            ));
        }
        // 额度将尽 → 记一条台账 (2026-10-11, 用户要的"异常通知").
        //
        // 判据用**窗口进度**而非剩余绝对值: "还剩 200 积分"是否算少, 取决于窗口多大
        // (剩 200/300 是少, 剩 200/100000 还很多)。这与面板进度条同源, 不会出现
        // "面板说还健康、台账说快没了"这种自相矛盾。
        // ⚠️ 只在 `used_pct` 有值 (= 上游确实给了窗口字段) 时判, 缺数据不猜 (口径红线)。
        if let Some(p) = info.quota_progress.and_then(|p| p.used_pct) {
            if p >= QUOTA_LOW_PCT {
                crate::admin::note_event(
                    crate::events::EventKind::QuotaLow,
                    provider,
                    &format!(
                        "{}使用了 {:.0}%（剩余 {} / 共 {}）",
                        crate::i18n::pick("本窗口额度", "window quota"),
                        p,
                        info.quota_progress.map(|q| q.window_remain).unwrap_or(0.0).round(),
                        info.quota_progress.map(|q| q.window_size).unwrap_or(0.0).round(),
                    ),
                );
            }
        }
        info
    }

    /// **账号型 + 通用余额端点**: 逐账号用各自 token 查询, 并记配额快照.
    ///
    /// 为什么必须逐账号查: 去重逻辑按 (endpoint, 静态 key) 合并, 而多账号共用同一把
    /// 静态 key → 只查一次 → 配额快照只覆盖一个账号 → 选号策略 (pace) 因数据不齐
    /// 静默回落 order (2026-10-08 实测撞出).
    ///
    /// 主值取**当前使用账号**的数值 (切换后必须跟着变), 合计另放 total_*.
    async fn query_account_quotas(
        &self,
        client: &reqwest::Client,
        accounts: &crate::accounts::AccountStore,
        provider: &str,
        endpoint: &str,
        auth: Option<&crate::providers::AuthConfig>,
    ) -> BalanceInfo {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let list = accounts.entries(provider).await;
        if list.is_empty() {
            return BalanceInfo::from_api(
                provider,
                None,
                "CNY".to_string(),
                now,
                Some("未登录账号（供应商卡片点「登录」）".to_string()),
            );
        }
        let mut remain_total = 0f64;
        // 每个账号各自的 (剩余, 本窗口进度) — 主值取"当前使用账号"的那一份.
        // 进度按账号分: 各账号的额度窗口互不相干, 混算会得出谁都不是的百分比.
        let mut per: std::collections::HashMap<String, (f64, Option<QuotaProgress>)> =
            std::collections::HashMap::new();
        let mut expiring: Option<(u64, f64)> = None;
        let mut first_err: Option<String> = None;
        let mut ok = 0usize;
        for acct in &list {
            // 用该账号自己的 token 查询 (这才是"这个号还剩多少"的正确凭据).
            match self
                .query_generic_balance_with_token(client, endpoint, &acct.access_token, auth)
                .await
            {
                Ok((remain, exp, prog)) => {
                    ok += 1;
                    remain_total += remain;
                    per.insert(acct.id.clone(), (remain, prog));
                    record_quota(&acct.id, &acct.access_token, remain, exp.map(|(t, _)| t), now);
                    if let Some((t, a)) = exp {
                        if expiring.map_or(true, |(et, _)| t < et) {
                            expiring = Some((t, a));
                        }
                    }
                }
                Err(e) => {
                    if first_err.is_none() {
                        first_err = Some(e);
                    }
                }
            }
        }
        if ok == 0 {
            return BalanceInfo::from_api(
                provider,
                None,
                "CNY".to_string(),
                now,
                first_err.or_else(|| Some("余额查询失败".to_string())),
            );
        }
        // 当前账号 = 第一个可用账号 (与选号口径一致).
        let active = list.iter().find(|e| e.is_available(now)).or_else(|| list.first());
        let mut info =
            BalanceInfo::from_api(provider, Some(remain_total), "CNY".to_string(), now, None);
        info.account_count = Some(list.len());
        info.total_balance = Some(remain_total);
        if let Some(a) = active {
            if let Some((r, prog)) = per.get(&a.id) {
                info.balance = Some(*r);
                info.quota_progress = *prog;
            }
            info.account_label = crate::accounts::account_display_name(&a.access_token)
                .or_else(|| a.label.clone());
        }
        info.expiring_at = expiring.map(|(t, _)| t);
        info.expiring_amount = expiring.map(|(_, a)| a);
        if let Some(e) = first_err {
            info.error = Some(e);
        }
        info
    }

    /// 通用余额端点查询 (GET + 鉴权头, 缺省 Bearer), 返回 `(剩余额度, 最近重置窗口, 本窗口进度)`.
    ///
    /// `auth` = 供应商的鉴权头声明 (逐账号查询时与转发同一份), 缺省 `Authorization: Bearer`.
    async fn query_generic_balance_with_token(
        &self,
        client: &reqwest::Client,
        endpoint: &str,
        token: &str,
        auth: Option<&crate::providers::AuthConfig>,
    ) -> Result<(f64, Option<(u64, f64)>, Option<QuotaProgress>), String> {
        let resp = crate::providers::with_auth_header(client.get(endpoint), auth, token)
            .header("Accept", "application/json")
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    "请求超时".to_string()
                } else {
                    format!("网络错误: {e}")
                }
            })?;
        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status().as_u16()));
        }
        let json = resp
            .json::<serde_json::Value>()
            .await
            .map_err(|e| format!("响应非 JSON: {e}"))?;
        // 优先按资源包聚合 (能拿到配额窗口); 退化到扁平余额字段时没有窗口信息.
        let accs = json
            .pointer("/data/Response/Data/Accounts")
            .or_else(|| json.pointer("/data/Accounts"))
            .and_then(|a| a.as_array());
        if let Some(arr) = accs {
            let (remain, _used, exp, prog) = aggregate_credits(arr);
            return Ok((remain, exp, prog));
        }
        let (balance, _) = extract_balance_from_json(&json);
        match balance {
            Some(b) => Ok((b, None, None)),
            None => Err("响应中未找到余额字段".to_string()),
        }
    }

    /// 单个账号的积分查询: 返回 `(剩余, 已消耗, 最近重置窗口, 本窗口进度, 资源包数)`.
    async fn query_one_buddy_credits(
        client: &reqwest::Client,
        token: &str,
    ) -> Result<(f64, f64, Option<(u64, f64)>, Option<QuotaProgress>, usize), String> {
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
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    "请求超时".to_string()
                } else {
                    format!("网络错误: {e}")
                }
            })?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            let friendly = match status.as_u16() {
                401 => "账号未登录或凭据失效（重新登录）".to_string(),
                429 => "请求过于频繁".to_string(),
                500..=599 => "服务端错误".to_string(),
                _ => format!("HTTP {status}"),
            };
            warn!(target: "balance", "buddy 积分查询 HTTP {status}: {}", crate::i18n::snip(&body, 200));
            return Err(friendly);
        }
        let v: serde_json::Value =
            serde_json::from_str(&body).map_err(|e| format!("解析响应失败: {e}"))?;
        // data.Response.Data.Accounts[] (兼容 data 下直接是结构的变体)
        let arr = v
            .pointer("/data/Response/Data/Accounts")
            .or_else(|| v.pointer("/data/Accounts"))
            .and_then(|a| a.as_array())
            .ok_or_else(|| "响应中未找到积分包字段".to_string())?;
        // 聚合: 剩余总量 / 已消耗 / 最近重置窗口 / 本窗口进度 (纯函数, 见 aggregate_credits).
        let (remain, used, expiring, progress) = aggregate_credits(arr);
        Ok((remain, used, expiring, progress, arr.len()))
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

    /// buddy 积分包聚合: 只认**窗口内仍有剩余**的包的最早重置时刻 (窗口已耗尽的
    /// 包不该占据"最近会重置"位 —— 它不会造成损失). 响应形态取自 2026-10-08 实测.
    #[test]
    fn credits_aggregate_picks_soonest_cycle_end_with_remainder() {
        let arr = serde_json::json!([
            // 窗口已耗尽 + 最早重置 → **不该**被选中
            {"CycleCapacityRemain": 0, "CapacityRemain": 0, "CapacityUsed": 500,
             "CycleEndTime": "2026-10-16 20:09:24"},
            // 有剩余, 月末重置
            {"CycleCapacityRemain": 300, "CapacityRemain": 300, "CapacityUsed": 200,
             "CycleEndTime": "2026-10-31 23:59:59"},
            // 有剩余且更早 → 选中这个
            {"CycleCapacityRemain": 120, "CapacityRemain": 120, "CapacityUsed": 30,
             "CycleEndTime": "2026-10-20 07:00:18"},
        ]);
        let (remain, used, expiring, _prog) = aggregate_credits(arr.as_array().unwrap());
        assert_eq!(remain, 420.0);
        assert_eq!(used, 730.0);
        let (at, amt) = expiring.expect("应有最近重置的窗口");
        let expect = parse_datetime("2026-10-20 07:00:18").unwrap();
        assert_eq!(at, expect);
        assert_eq!(amt, 120.0);
    }

    /// 全部包都无窗口信息 → expiring 为 None, 前端不显示该行.
    #[test]
    fn credits_aggregate_without_cycle_returns_none() {
        let arr = serde_json::json!([
            {"CapacityRemain": 100, "CapacityUsed": 0},
            {"CapacityRemain": 50, "CapacityUsed": 0, "CycleEndTime": ""},
        ]);
        let (remain, used, expiring, _prog) = aggregate_credits(arr.as_array().unwrap());
        assert_eq!(remain, 150.0);
        assert_eq!(used, 0.0);
        assert!(expiring.is_none(), "空/缺失的窗口时间不得当作有效期限");
    }

    /// 回归: **配额窗口取 `CycleEndTime`, 不是 `DeductionEndTime`** (2026-10-08 实测更正).
    ///
    /// 老实现取 DeductionEndTime (实测 2034/2035 年, 是"扣费有效期") 当"最近过期",
    /// 于是面板会显示"十几年后到期"这种无用且误导的数字.
    #[test]
    fn credits_window_uses_cycle_end_time_not_deduction() {
        // 真实形态 (脱敏): 自然月包 + 30 天滚动包; DeductionEndTime 都是遥远未来.
        let arr = serde_json::json!([
            {"CapacityType": 4, "CapacityRemain": 500, "CapacityUsed": 0,
             "CycleCapacityRemain": 500, "CycleStartTime": "2026-10-01 00:00:00",
             "CycleEndTime": "2026-10-31 23:59:59", "DeductionEndTime": 2049883762000i64},
            {"CapacityType": 1, "CapacityRemain": 120, "CapacityUsed": 30,
             "CycleCapacityRemain": 120, "CycleStartTime": "2026-09-16 20:09:25",
             "CycleEndTime": "2026-10-16 20:09:24", "DeductionEndTime": 1792152564000i64},
        ]);
        let (remain, used, expiring, _prog) = aggregate_credits(arr.as_array().unwrap());
        assert_eq!(remain, 620.0);
        assert_eq!(used, 30.0);
        let (at, amt) = expiring.expect("应有最近重置的窗口");
        // 最近的是 10-16 那个滚动包 (早于 10-31 月末)
        let expect = parse_datetime("2026-10-16 20:09:24").unwrap();
        assert_eq!(at, expect, "取 CycleEndTime, 不得取 DeductionEndTime");
        assert_eq!(amt, 120.0, "窗口内剩余应看 CycleCapacityRemain");
    }

    /// **本窗口进度只算「仍有剩余」的包** (§7 待办②, 2026-10-10 定案).
    ///
    /// 形态取自真实响应 (34 个包): 已耗尽的旧包 (`CycleCapacityRemain=0`) 占了绝大部分
    /// 容量. 若把它们也算进分母: ΣCycleCapacitySize=4463 / ΣUsed=3682 → **82.5%**,
    /// 而那些包是过去一个月陆续赠送的体验包/活动包, 早已用完、**不会造成损失**.
    /// 只算还有额度的 5 个包: 807 / 26 → **3.2%** —— 这才是"当前还剩多少"的事实.
    #[test]
    fn quota_progress_counts_only_packages_with_remainder() {
        let arr = serde_json::json!([
            // 已耗尽 (不该进分母): 30 天滚动包, 9 月中旬开始、此刻已用完
            {"CapacityRemain": 0, "CapacityUsed": 1500, "CycleCapacityRemain": 0,
             "CycleCapacitySize": 1500, "CycleCapacityUsed": 1500,
             "CycleEndTime": "2026-10-16 20:09:24"},
            {"CapacityRemain": 0, "CapacityUsed": 100, "CycleCapacityRemain": 0,
             "CycleCapacitySize": 100, "CycleCapacityUsed": 100,
             "CycleEndTime": "2026-10-16 20:10:28"},
            // 仍有剩余 (该进): 月末自然月包, 用了 26/500
            {"CapacityRemain": 473, "CapacityUsed": 500, "CycleCapacityRemain": 473,
             "CycleCapacitySize": 500, "CycleCapacityUsed": 26,
             "CycleEndTime": "2026-10-31 23:59:59"},
            // 仍有剩余且尚未动用 (该进: 它是"还没用"的事实, 不是"没有")
            {"CapacityRemain": 100, "CapacityUsed": 0, "CycleCapacityRemain": 100,
             "CycleCapacitySize": 100, "CycleCapacityUsed": 0,
             "CycleEndTime": "2026-11-01 00:49:45"},
        ]);
        let (remain, used, expiring, prog) = aggregate_credits(arr.as_array().unwrap());
        // 总量口径不变: 仍统计**全部**包 (那是"账户还剩多少积分", 与进度是两回事).
        assert_eq!(remain, 573.0, "总剩余 = 473 + 100 (已耗尽的包贡献 0)");
        assert_eq!(used, 2100.0);
        let (_at, amt) = expiring.expect("应有最近重置窗口");
        assert_eq!(amt, 473.0);

        let p = prog.expect("应有进度");
        assert_eq!(p.window_size, 600.0, "只算 2 个有剩余的包 (500+100), 不含已耗尽的 1600");
        assert_eq!(p.window_used, 26.0);
        assert_eq!(p.window_remain, 573.0);
        assert_eq!(p.packages, 2, "样本量 = 有剩余的包数");
        // 26/600 = 4.33% —— 而不是全算进来的 3682/4463 = 82.5%
        assert!((p.used_pct.unwrap() - 4.333).abs() < 0.01, "实得 {}", p.used_pct.unwrap());
    }

    /// 所有包都耗尽 → 进度为 None (**不是 0%**) —— "没有额度了"也不该显示成"0% 已用",
    /// 那会被读成"完全没用过". 调用方据此显示"额度已耗尽".
    #[test]
    fn quota_progress_none_when_all_exhausted() {
        let arr = serde_json::json!([
            {"CapacityRemain": 0, "CapacityUsed": 500, "CycleCapacityRemain": 0,
             "CycleCapacitySize": 500, "CycleCapacityUsed": 500},
        ]);
        let (_, _, _, prog) = aggregate_credits(arr.as_array().unwrap());
        assert!(prog.is_none(), "全部耗尽时不得给 0% 进度");
    }

    /// 无窗口字段的老式积分包 → 进度为 None. 不得把 `Capacity*`(累计口径) 当窗口用:
    /// 累计口径跨多个周期, 拿它算"本窗口已用"是错的.
    #[test]
    fn quota_progress_none_without_cycle_fields() {
        let arr = serde_json::json!([
            {"CapacityRemain": 100, "CapacityUsed": 40},
            {"CapacityRemain": 50, "CapacityUsed": 10},
        ]);
        let (_, _, _, prog) = aggregate_credits(arr.as_array().unwrap());
        assert!(prog.is_none(), "没有 Cycle* 字段就没有窗口口径, 不得拿累计口径凑");
    }

    /// 上游给了包、有剩余, 但**没给窗口总量** → 整个进度为 None (不编造百分比).
    ///
    /// 这是刻意的: `window_size == 0` 时"已用百分之几"这个问题本身不成立,
    /// 返回一个 `used_pct: None` 的半截结构只会让每个消费方都要多判一次.
    /// 面板据此只显示"还剩多少", 不显示进度条.
    #[test]
    fn quota_progress_none_when_window_size_missing() {
        let arr = serde_json::json!([
            // 有剩余、有 CycleCapacityUsed, 唯独缺 CycleCapacitySize/CapacitySize
            {"CapacityRemain": 30, "CapacityUsed": 0, "CycleCapacityRemain": 30,
             "CycleCapacityUsed": 0},
        ]);
        let (_, _, _, prog) = aggregate_credits(arr.as_array().unwrap());
        assert!(prog.is_none(), "分母缺失时不给半截进度, 由前端只显示剩余");
    }

    /// 上游数值异常 (已用 > 总量) 时钳到 100 —— 进度条不能超出容器, 也不能显示 >100%.
    #[test]
    fn quota_progress_clamps_over_100_pct() {
        let arr = serde_json::json!([
            {"CapacityRemain": 0, "CapacityUsed": 0, "CycleCapacityRemain": 5,
             "CycleCapacitySize": 50, "CycleCapacityUsed": 120,
             "CycleEndTime": "2026-10-31 23:59:59"},
        ]);
        let (_, _, _, prog) = aggregate_credits(arr.as_array().unwrap());
        let p = prog.expect("仍有 5 剩余, 该有进度");
        assert_eq!(p.used_pct, Some(100.0), "异常值必须钳到 100, 不得显示 240%");
    }

    /// `BalanceInfo` 序列化必须带 `quota_progress` 字段 (前端按现有 snake_case 读,
    /// 与 `expiring_at` / `expiring_amount` 同一套 —— 该结构体**没有** `rename_all`,
    /// 是刻意保持与既有字段一致的).
    /// 老消费方读到 None → 显示「—」, 不会崩.
    #[test]
    fn balance_info_serializes_quota_progress() {
        let info = BalanceInfo::from_credits(
            "buddy", 473.0, 26.0, Some((1_700_000_000, 473.0)),
            Some(QuotaProgress {
                window_size: 500.0,
                window_used: 26.0,
                window_remain: 473.0,
                used_pct: Some(5.2),
                packages: 1,
            }),
            1_700_000_000,
        );
        let v = serde_json::to_value(&info).unwrap();
        let prog = v.get("quota_progress").expect("应序列化为 quota_progress (snake_case)");
        assert_eq!(prog["window_size"], 500.0);
        assert_eq!(prog["used_pct"], 5.2);
        assert_eq!(prog["packages"], 1);
        // 旧消费方 (老前端) 读不到也不报错 —— 字段缺失时按 None 处理.
        assert!(v.get("nonexistent_field").is_none());
    }

    /// parse_datetime 的边界: 非法/缺字段返回 None (不参与判定, 不影响总额).
    #[test]
    fn parse_datetime_rejects_malformed() {
        assert!(parse_datetime("2026-10-31 23:59:59").is_some());
        assert!(parse_datetime("2026-10-31").is_none(), "缺时间部分");
        assert!(parse_datetime("not a date").is_none());
        assert!(parse_datetime("2026-13-01 00:00:00").is_none(), "月份越界");
        assert!(parse_datetime("2026-10-31 25:00:00").is_none(), "小时越界");
    }

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
