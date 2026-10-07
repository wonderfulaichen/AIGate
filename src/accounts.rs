//! 账号形式供应商的运行时凭据与静默续期 — `data/accounts.json`, 按供应商名索引.
//!
//! ## 与 keys.json 的分工 (2026-10-01 账号化)
//! - `keys.json`: 静态密钥 (api_key 模式的面板编辑值), 值是纯字符串.
//! - `accounts.json` (本模块): 账号凭据 `{access_token, refresh_token, expires_at}` +
//!   到期前静默续期. 与 keys.json 平级、同样按 `provider.name` 索引.
//!
//! ## 设计依据 (gitee iJetLi/deepseek-harness-codearts 插件, 实测核对)
//! - 续期时机: 到期前 1 小时 (`expiry_margin_secs`, 与 CodeArts getFirstRefreshTime 同语义).
//! - **只按到期与否判定** —— 插件记的真实缺陷: 曾按"启用"过滤, 停用账号的 refresh_token
//!   一路放到失效, 重新启用后只能重登. 本实现无"启用"概念, 天然免疫.
//! - buddy 实测 (2026-10-01): `POST /v2/plugin/auth/token/refresh` + `X-Refresh-Token` 头,
//!   HTTP 200 `{code:0, data:{accessToken, refreshToken, expiresIn(秒)}}`, 55 天有效期.
//!
//! ## 状态机
//! `active → (到期/临期) → refresh → active | dead`
//! - `dead=true`: 4xx 级续期失败 (refresh_token 作废), 停止自动续期, 面板提示重登;
//! - 网络错误/5xx: 不置 dead, 下个周期重试.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

use crate::providers::RefreshConfig;

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 一条账号凭据.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct AccountEntry {
    /// 稳定标识 (本地生成, 与 token 无关) —— 面板按它登出/切换/排序单个账号.
    /// 旧文件 (单账号、无 id) 加载时自动补, 不影响凭据本身.
    #[serde(default)]
    pub id: String,
    /// 展示名 (可选, 用户自定义; 空则由面板按序号显示).
    #[serde(default)]
    pub label: Option<String>,
    /// **账号身份指纹** (从凭据里推出, 如 buddy JWT 的 `sub` 前 8 位).
    ///
    /// 用途有二:
    /// 1. **去重**: 同一个账号再次登录会拿到**新的 token 但相同的身份** —— 按身份判同一账号,
    ///    才不会把同一个号在列表里堆成两条 (若按 token 字符串比, 换了 token 就会被当新账号);
    /// 2. **可辨识**: 面板里两个 buddy 账号看起来一模一样, 有指纹用户才能确认"加进去的是不是
    ///    另一个号" (2026-10-07 实测: 用户在浏览器里没退出当前账号, 登录页直接把同一个号的
    ///    token 又发回来了, 界面因此"看起来没变化", 无法分辨).
    #[serde(default)]
    pub identity: Option<String>,
    pub access_token: String,
    /// 续期令牌 (可能被上游轮换; 响应里没有新值则沿用).
    #[serde(default)]
    pub refresh_token: Option<String>,
    /// 到期时刻 (Unix **秒**). None = 不可知 (此时不主动续期, 用到上游拒绝为止).
    #[serde(default)]
    pub expires_at: Option<u64>,
    /// 续期被 4xx 级拒绝 → 凭据作废, 停止自动续期, 待重新登录.
    #[serde(default)]
    pub dead: bool,
    /// 上游拒绝 (401/403, 如策略拦截/额度用尽) 后的**冷却截止** (Unix 秒).
    /// 冷却期内不参与选号, 到期自动恢复 —— 与 `dead` 的区别: dead 是凭据本身作废
    /// (只能重新登录), cooldown 是"暂时别用这个号" (会自动恢复).
    #[serde(default)]
    pub cooldown_until: Option<u64>,
    #[serde(default)]
    pub updated_at: Option<u64>,
}

impl AccountEntry {
    /// 已硬过期 (现在 >= 到期时刻).
    pub fn is_expired(&self, now: u64) -> bool {
        self.expires_at.is_some_and(|t| now >= t)
    }

    /// 临期 (margin 秒内到期).
    pub fn is_expiring(&self, now: u64, margin_secs: u64) -> bool {
        self.expires_at.is_some_and(|t| now + margin_secs >= t)
    }

    /// 当前是否可用于选号 (未作废、不在冷却期).
    pub fn is_available(&self, now: u64) -> bool {
        !self.dead && self.cooldown_until.map_or(true, |t| now >= t)
    }
}

/// 极简 base64url 解码 (只为读 JWT 载荷, 不做严格 padding/字符集校验).
fn b64url_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    let (mut buf, mut bits) = (0u32, 0u8);
    for c in s.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            b'=' => break,
            _ => return None,
        } as u32;
        buf = (buf << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    Some(out)
}

/// 从访问令牌推出**稳定的账号身份** (面板展示 + 同账号去重).
///
/// buddy 的 token 是 JWT, 载荷里的 `sub` 就是账号 id —— 同一个号重新登录, 即便 token 变了,
/// `sub` 不变, 故它是"是不是同一个账号"的正确判据. 非 JWT / 解析不出 `sub` → `None`
/// (调用方退回按 token 字符串比较, 行为与旧版一致).
pub fn account_identity(access_token: &str) -> Option<String> {
    let v = jwt_claims(access_token)?;
    let sub = v.get("sub")?.as_str()?;
    if sub.is_empty() {
        return None;
    }
    // 只留短指纹: 面板够区分即可, 不必把完整标识放进状态接口/文件.
    Some(sub.chars().take(8).collect())
}

/// 从访问令牌里取**人可读的账号名** (面板显示用, 不可用于去重).
///
/// 优先级: `nickname` (用户昵称) → `preferred_username` (登录名/手机号) → `name` / `email`.
/// 为什么必须显示名字: 光看 id/指纹用户认不出这是哪个号 (2026-10-07 反馈:
/// "只显示 id 号分不清, 一般都是看账号名称的"). 这些声明都在凭据里, 无需额外请求.
pub fn account_display_name(access_token: &str) -> Option<String> {
    let v = jwt_claims(access_token)?;
    for k in ["nickname", "preferred_username", "name", "email"] {
        if let Some(s) = v.get(k).and_then(|x| x.as_str()) {
            let s = s.trim();
            if !s.is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// 登录名 / 手机号等次要标识 (面板 tooltip 用, 便于区分同名账号).
pub fn account_username(access_token: &str) -> Option<String> {
    let v = jwt_claims(access_token)?;
    for k in ["preferred_username", "email"] {
        if let Some(s) = v.get(k).and_then(|x| x.as_str()) {
            let s = s.trim();
            if !s.is_empty() && Some(s.to_string()) != account_display_name(access_token) {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// 解出 JWT 载荷 (非 JWT / 解析失败 → None).
fn jwt_claims(access_token: &str) -> Option<serde_json::Value> {
    let payload = access_token.split('.').nth(1)?;
    let bytes = b64url_decode(payload)?;
    serde_json::from_slice(&bytes).ok()
}

/// 生成账号 id (本地唯一即可, 与凭据无关).
///
/// 不用 uuid 依赖: 纳秒时间戳 + 进程内自增计数, 单机唯一性足够.
fn new_account_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("a{:x}{:x}", nanos, SEQ.fetch_add(1, Ordering::Relaxed))
}

/// 账号凭据存储. `data/accounts.json`, 按 `provider.name` 索引, **每个供应商可存多个账号**.
///
/// 数组顺序 = **选号优先级** (面板可调整): 优先用靠前的健康账号, 失效自动跳到下一个.
/// 为什么按优先级而不是轮询均摊: 上游 KV 缓存按账号隔离, 轮询会让同一会话的缓存命中
/// 分裂到两个账号上; 优先级序既保住缓存亲和, 又能在号失效时自动接管.
///
/// 兼容旧文件: 旧版是 `{"buddy": {单账号对象}}`, 加载时自动升级成单元素数组 (并补 id).
#[derive(Clone)]
pub struct AccountStore {
    path: PathBuf,
    map: Arc<RwLock<HashMap<String, Vec<AccountEntry>>>>,
    /// 续期串行闸: 后台调度与请求路径可能同时判定"临期", 串行化并复查,
    /// 避免对上游并发 POST 同一个 refresh_token (若上游严格轮换, 后到者 4xx 会把
    /// 刚续好的健康账号误标 dead). 刷新是低频操作, 全局一把锁足够.
    refresh_gate: Arc<tokio::sync::Mutex<()>>,
}

impl AccountStore {
    /// 从 data 目录加载 (文件不存在 = 空账号池, 不是错误).
    /// 旧格式 (单账号对象) 自动升级为数组并补 id, 升级结果立即落盘.
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("accounts.json");
        let raw: HashMap<String, serde_json::Value> = std::fs::read_to_string(&path)
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or_default();
        let mut map: HashMap<String, Vec<AccountEntry>> = HashMap::with_capacity(raw.len());
        let mut upgraded = false;
        for (prov, v) in raw {
            let mut list: Vec<AccountEntry> = match v {
                // 新格式: 账号数组.
                serde_json::Value::Array(_) => serde_json::from_value(v).unwrap_or_default(),
                // 旧格式: 单个账号对象 → 升级为一元素数组.
                other => match serde_json::from_value::<AccountEntry>(other) {
                    Ok(e) => {
                        upgraded = true;
                        vec![e]
                    }
                    Err(_) => Vec::new(),
                },
            };
            for e in list.iter_mut() {
                if e.id.is_empty() {
                    e.id = new_account_id();
                    upgraded = true;
                }
            }
            if !list.is_empty() {
                map.insert(prov, list);
            }
        }
        if upgraded {
            Self::write_atomic(&path, &map);
        }
        Self {
            path,
            map: Arc::new(RwLock::new(map)),
            refresh_gate: Arc::new(tokio::sync::Mutex::new(())),
        }
    }

    /// 该供应商的全部账号 (副本, 按优先级顺序).
    pub async fn entries(&self, provider: &str) -> Vec<AccountEntry> {
        self.map.read().await.get(provider).cloned().unwrap_or_default()
    }

    /// 按 id 取单个账号.
    pub async fn get_by_id(&self, provider: &str, id: &str) -> Option<AccountEntry> {
        self.map
            .read()
            .await
            .get(provider)?
            .iter()
            .find(|e| e.id == id)
            .cloned()
    }

    /// 新增一个账号 (登录成功后调用). 返回新建/命中的账号 id.
    ///
    /// 去重口径: **同一个账号视为同一条** —— 优先按身份指纹 (`identity`) 比, 缺指纹时退回
    /// 按 access_token 比. 命中则原地更新 (重新登录同一个号不该在列表里堆两条), 否则追加到末尾
    /// (优先级最低, 前面用坏了才轮到它).
    pub async fn add(&self, provider: &str, mut entry: AccountEntry) -> String {
        if entry.id.is_empty() {
            entry.id = new_account_id();
        }
        if entry.identity.is_none() {
            entry.identity = account_identity(&entry.access_token);
        }
        let mut m = self.map.write().await;
        let list = m.entry(provider.to_string()).or_default();
        let same_acc = |e: &AccountEntry| match (&entry.identity, &e.identity) {
            (Some(a), Some(b)) => a == b,
            _ => e.access_token == entry.access_token,
        };
        let hit = list.iter().position(same_acc);
        let fp = entry.identity.clone().unwrap_or_else(|| "无指纹".into());
        let (effective_id, is_new) = match hit {
            Some(i) => {
                let keep_id = list[i].id.clone();
                list[i] = entry;
                list[i].id = keep_id.clone(); // 同一账号保持原 id (面板选中态不跳)
                (keep_id, false)
            }
            None => {
                let id = entry.id.clone();
                list.push(entry);
                (id, true)
            }
        };
        let n = list.len();
        let snapshot = m.clone();
        drop(m);
        Self::write_atomic(&self.path, &snapshot);
        // 记账可查: 用户报障"再登录一个号还是 1 个"时, 这条日志直接说明是新增还是命中同一账号.
        if is_new {
            tracing::info!("accounts: '{provider}' 新增账号 {effective_id} (身份 {fp}), 现有 {n} 个");
        } else {
            tracing::info!(
                "accounts: '{provider}' 账号 {effective_id} 已存在 (身份 {fp} 是同一账号, 仅更新凭据), \
                 现有 {n} 个; 若本想添加**另一个**账号, 请先在浏览器退出当前账号再登录"
            );
        }
        effective_id
    }

    /// **切换当前使用的账号 / 调整优先级**: 按 `ids` 给出的顺序重排 (数组顺序即优先级).
    ///
    /// 面板的「设为当前账号」与拖拽排序共用这一个原语 (前者就是"把该 id 放到最前")。
    /// 校验: `ids` 必须**恰好覆盖**现有账号集合 (不重不漏) —— 不然一次失手的前端调用就会
    /// 悄悄丢账号或漏排某个号 (宁可整个操作失败, 也不做部分应用)。返回 Err(说明) 时不改动任何数据。
    pub async fn reorder(&self, provider: &str, ids: &[String]) -> Result<(), String> {
        let mut m = self.map.write().await;
        let Some(list) = m.get_mut(provider) else {
            return Err(format!("供应商 {provider} 没有账号"));
        };
        if ids.len() != list.len() {
            return Err(format!("账号数量不匹配 (收到 {} 个, 实际 {} 个)", ids.len(), list.len()));
        }
        let mut map: HashMap<&str, &AccountEntry> = list.iter().map(|e| (e.id.as_str(), e)).collect();
        let mut reordered: Vec<AccountEntry> = Vec::with_capacity(list.len());
        for id in ids {
            match map.remove(id.as_str()) {
                Some(e) => reordered.push(e.clone()),
                None => return Err(format!("未知或重复的账号 id: {id}")),
            }
        }
        if !map.is_empty() {
            return Err("存在未出现在排序里的账号".to_string());
        }
        // 第一个可用账号 = 新选中的"当前账号"; 记日志便于事后核对切换是否生效.
        let now = now_secs();
        if let Some(head) = reordered.iter().find(|e| e.is_available(now)) {
            tracing::info!(
                "accounts: '{provider}' 切换当前账号 → {} (身份 {}), 顺序 {}",
                head.id,
                head.identity.clone().unwrap_or_else(|| "无指纹".into()),
                ids.len()
            );
        }
        *list = reordered;
        let snapshot = m.clone();
        drop(m);
        Self::write_atomic(&self.path, &snapshot);
        Ok(())
    }

    /// 列出**有账号**的供应商名 (余额侧据此决定"是否按供应商单独查询")。
    pub async fn providers_with_accounts(&self) -> Vec<String> {
        self.map
            .read()
            .await
            .iter()
            .filter(|(_, list)| !list.is_empty())
            .map(|(k, _)| k.clone())
            .collect()
    }

    /// 按 id 原地更新一个账号 (续期/冷却标记).
    pub async fn update_by_id<F: FnOnce(&mut AccountEntry)>(&self, provider: &str, id: &str, f: F) {
        let mut m = self.map.write().await;
        let Some(list) = m.get_mut(provider) else { return };
        let Some(e) = list.iter_mut().find(|e| e.id == id) else { return };
        f(e);
        let snapshot = m.clone();
        drop(m);
        Self::write_atomic(&self.path, &snapshot);
    }

    /// 删除单个账号 (按 id). 返回是否删掉.
    pub async fn remove_by_id(&self, provider: &str, id: &str) -> bool {
        let mut m = self.map.write().await;
        let Some(list) = m.get_mut(provider) else { return false };
        let before = list.len();
        list.retain(|e| e.id != id);
        let removed = list.len() != before;
        if list.is_empty() {
            m.remove(provider);
        }
        if removed {
            let snapshot = m.clone();
            drop(m);
            Self::write_atomic(&self.path, &snapshot);
        }
        removed
    }

    /// 删除某供应商的全部账号 (退出登录/清空).
    pub async fn remove(&self, provider: &str) {
        let mut m = self.map.write().await;
        if m.remove(provider).is_some() {
            let snapshot = m.clone();
            drop(m);
            Self::write_atomic(&self.path, &snapshot);
        }
    }

    /// 标记某账号冷却 N 秒 (上游 401/403 拒了它): 冷却期内不参与选号.
    pub async fn cooldown(&self, provider: &str, id: &str, secs: u64) {
        let until = now_secs() + secs;
        self.update_by_id(provider, id, |e| e.cooldown_until = Some(until)).await;
    }

    /// 面板状态视图: 每个供应商一组账号状态 (永不返回 token 原文).
    ///
    /// 值为**数组**, 顺序即优先级; 每项含 `active` = 当前会被选中的那个
    /// (第一个可用账号), 面板据此高亮.
    pub async fn status_map(&self) -> HashMap<String, serde_json::Value> {
        let now = now_secs();
        self.map
            .read()
            .await
            .iter()
            .map(|(k, list)| {
                let active_id = list
                    .iter()
                    .find(|e| e.is_available(now))
                    .map(|e| e.id.clone());
                let arr: Vec<serde_json::Value> = list
                    .iter()
                    .map(|v| {
                        serde_json::json!({
                            "id": v.id,
                            "label": v.label,
                            "identity": v.identity,
                            // 人可读账号名 (昵称/登录名) —— 列表主标签, 取自凭据自身, 无需额外请求.
                            "name": account_display_name(&v.access_token),
                            "username": account_username(&v.access_token),
                            "expires_at": v.expires_at,
                            "expires_in_secs": v.expires_at.map(|t| t.saturating_sub(now) as i64),
                            "dead": v.dead,
                            "cooldown_until": v.cooldown_until,
                            "cooling": v.cooldown_until.map_or(false, |t| now < t),
                            "has_refresh_token": v.refresh_token.is_some(),
                            "updated_at": v.updated_at,
                            "active": active_id.as_deref() == Some(v.id.as_str()),
                        })
                    })
                    .collect();
                (k.clone(), serde_json::Value::Array(arr))
            })
            .collect()
    }

    fn write_atomic(path: &std::path::Path, map: &HashMap<String, Vec<AccountEntry>>) {
        let json = serde_json::to_string_pretty(map).unwrap_or_default();
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, &json).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }
}

// ── 选号策略 (2026-10-08) ──
//
// 依据: 每个账号的额度有**独立的重置窗口** (buddy 实测: CapacityType=4 是自然月,
// CapacityType=1 多为 30 天滚动)。用哪个号会直接影响月底总量 —— 若总用额度还剩
// 最多的那个号, 而另一个号窗口快到了却没用, 重置时那部分就白丢了。
// 参考 magpie 的 smart/pace 思路, 按我们已有的数据实现:
//
// - `order` (默认/既有行为): 严格按列表顺序, 第一个可用即可 —— 保住 KV 缓存亲和,
//   语义最可预测 ("我设了谁就用谁"), 面板显示与实际也一致。
// - `pace`: 只在**多个账号都能用**且配额窗口信息可用时, 优先用「剩余额度 ÷ 距窗口
//   重置的时间」比值最小的号 —— 即"最该被消耗的那个":
//   剩得多且快重置 = 不用就浪费 (比值大, 该先用); 剩得少 = 该留着 (比值小, 后用)。
//
// ⚠️ 只在配额数据齐备时生效: 拿不到窗口信息时静默回落到 order —— 宁可不优化,
//   也不要在未知数据上做出用户无法预测的切换 (那会破坏"我设了谁就用谁"的信任)。

/// 选号策略.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AccountPicking {
    /// 按列表顺序, 第一个可用即可 (默认; 保住缓存亲和, 行为可预测).
    #[default]
    Order,
    /// 优先消耗"快重置且剩余多"的账号, 减少窗口重置时的浪费.
    Pace,
}

/// 一个账号的配额快照 (选号决策用; 数据缺失则为 None).
#[derive(Debug, Clone, Copy, Default)]
pub struct QuotaView {
    /// 全部资源包的总剩余额度.
    pub remain: Option<f64>,
    /// **最近会重置**的窗口结束时刻 (Unix 秒).
    pub cycle_end: Option<u64>,
}

/// `pace` 打分: 越小越该被优先消耗.
///
/// 算式: `剩余额度 / 距窗口重置的剩余天数` —— 单位是"每天有多少额度会作废"。
/// 该值**大** = 剩余多且快到期 = 不用就浪费 → 应该**先**用 (故取负数, 让大者排前)。
/// 分母下限 0.5 天, 避免窗口将至时比值爆炸把单个号锁死。
fn pace_score(q: QuotaView, now: u64) -> Option<f64> {
    let remain = q.remain?;
    let end = q.cycle_end?;
    if remain <= 0.0 || end <= now {
        // 窗口已过或额度为 0: 无"浪费"风险, 不该因此被优先选中.
        return Some(0.0);
    }
    let days = ((end - now) as f64 / 86400.0).max(0.5);
    Some(-(remain / days))
}

/// 选号模式: 进程级 **可写** (默认 `order`; 环境变量 `AIGATE_ACCOUNT_PICKING=pace` 可改,
/// 将来面板切换也走这里)。用 RwLock 而非 OnceLock —— 后者只能设一次, 测试与运行时
/// 都无法再切换 (实测撞出: 单测里第二次设定静默无效)。
static PICKING: std::sync::OnceLock<std::sync::RwLock<AccountPicking>> = std::sync::OnceLock::new();

fn picking_slot() -> &'static std::sync::RwLock<AccountPicking> {
    PICKING.get_or_init(|| {
        let init = match std::env::var("AIGATE_ACCOUNT_PICKING").ok().as_deref() {
            Some("pace") => AccountPicking::Pace,
            _ => AccountPicking::Order,
        };
        std::sync::RwLock::new(init)
    })
}

pub fn account_picking() -> AccountPicking {
    *picking_slot().read().unwrap_or_else(|e| e.into_inner())
}

/// 设定选号策略 (测试用; 将来面板切换也走这个函数).
pub fn set_account_picking(m: AccountPicking) {
    *picking_slot().write().unwrap_or_else(|e| e.into_inner()) = m;
}

/// 取可用 token (含账号 id): 账号缺失/全部作废/全部冷却 → None (调用方回落 api_key 链).
///
/// 选号规则 (**按优先级顺序**, 面板里的顺序即优先级):
/// 1. 从前往后找第一个"可用"账号 (`is_available`: 未作废、不在冷却期);
/// 2. 该号过期 → 必须先续期成功才用 (拿过期 token 打上游只会换来必败的 401);
///    临期 → 尝试续期, 失败仍用旧值 (还没过期);
/// 3. 该号在续期时被判作废 (dead) → **自动跳到下一个**可用账号.
///
/// 上游 KV 缓存按账号隔离, 故不做轮询均摊 (轮询会把同一会话的缓存命中分裂到多个账号);
/// 优先级序既能保住缓存亲和, 又能在号失效时自动接管.
pub async fn usable_token(
    store: &AccountStore,
    provider: &str,
    margin_secs: u64,
    refresh: Option<&RefreshConfig>,
    client: &reqwest::Client,
) -> Option<(String, String)> {
    pick_account(store, provider, margin_secs, refresh, client, &[]).await
}

/// 选出一个可用账号, 排除 `exclude` 里的 id (换号重试用).
///
/// 返回 `(access_token, account_id)`.
///
/// **顺序**: `order` 模式按列表顺序; `pace` 模式按"最该被消耗的号"重排 ——
/// 但仅在**首个可用账号之后仍有候选**且配额数据齐备时才调整 (见 [`AccountPicking`])。
pub async fn pick_account(
    store: &AccountStore,
    provider: &str,
    margin_secs: u64,
    refresh: Option<&RefreshConfig>,
    client: &reqwest::Client,
    exclude: &[String],
) -> Option<(String, String)> {
    let list = store.entries(provider).await;
    let now = now_secs();

    // 候选 = 可用且未排除的账号 (保持列表顺序).
    let mut cands: Vec<&AccountEntry> = list
        .iter()
        .filter(|e| !exclude.contains(&e.id) && e.is_available(now))
        .collect();
    if cands.is_empty() {
        return None;
    }

    if account_picking() == AccountPicking::Pace && cands.len() > 1 {
        // 只在配额信息齐备时重排; 有任一候选拿不到数据就整体回落 order
        // (避免在未知数据上做出用户无法预测的切换).
        let quotas: Vec<Option<QuotaView>> = cands
            .iter()
            .map(|e| {
                crate::balance::cached_quota_view(&e.access_token, &e.id, now)
                    .map(|(remain, cycle_end)| QuotaView {
                        remain: Some(remain),
                        cycle_end: Some(cycle_end),
                    })
            })
            .collect();
        if quotas.iter().all(|q| q.is_some()) {
            let scores: Vec<f64> = quotas
                .iter()
                .map(|q| pace_score(q.unwrap(), now).unwrap_or(0.0))
                .collect();
            // 稳定排序: 分数相同(=都无浪费风险)时保持面板里的顺序.
            let mut order: Vec<usize> = (0..cands.len()).collect();
            order.sort_by(|&a, &b| {
                scores[a]
                    .partial_cmp(&scores[b])
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            let old_head = cands[0];
            cands = order.into_iter().map(|i| cands[i]).collect();
            if cands[0].id != old_head.id {
                tracing::info!(
                    "accounts: '{provider}' pace 选号 → {} (额度 {} / 窗口重置还剩 {} 天), 优先于面板首位的 {}",
                    cands[0].id,
                    quotas[0].map(|q| q.remain.unwrap_or(0.0)).unwrap_or(0.0),
                    quotas[0]
                        .and_then(|q| q.cycle_end)
                        .map(|end| (end.saturating_sub(now)) / 86400)
                        .unwrap_or(0),
                    old_head.id
                );
            }
        }
    }

    for cand in cands {
        let mut entry = cand.clone();
        if entry.is_expired(now) {
            let rc = refresh?;
            match refresh_account(client, rc, provider, &entry.id, store, margin_secs).await {
                Ok(new) => entry = new,
                Err(e) => {
                    // 硬过期又续不成 → 该号不可用, 试下一个 (换号接管).
                    tracing::warn!(
                        "accounts: '{provider}' 账号 {} 过期续期失败: {e} -> 试下一个账号/回落 api_key 链",
                        entry.id
                    );
                    continue;
                }
            }
        } else if entry.is_expiring(now, margin_secs) {
            if let Some(rc) = refresh {
                // 临期续期失败不致命: 旧 token 尚未过期, 继续使用, 下周期重试.
                if let Ok(new) = refresh_account(client, rc, provider, &entry.id, store, margin_secs).await {
                    entry = new;
                }
            }
        }
        return Some((entry.access_token, entry.id));
    }
    None
}

/// 标记某账号被上游拒绝 (401/403) → 冷却, 并在还有别的健康号时取出一个顶上.
///
/// 为什么换号重试是安全的: 401/403 发生在上游**尚未生成内容**时, 重发不重复计费、
/// 不会产生重复输出; 而客户端对这类错误通常不重试 (Auth/Permission 属终态) ——
/// 网关内换号是唯一能把它变成"无感"的地方.
///
/// ⚠️ **没有可换的号时不冷却**: 单账号供应商被判 401 多半就是凭据/配置真的不对,
/// 冷却它只会让后续请求悄悄回落到密钥链、把真实错误盖掉 (用户看到的是另一个错)。
/// 此时原样把 401 交给客户端才诚实。
pub async fn failover_account(
    store: &AccountStore,
    provider: &str,
    failed_id: &str,
    cooldown_secs: u64,
    margin_secs: u64,
    refresh: Option<&RefreshConfig>,
    client: &reqwest::Client,
    tried: &[String],
) -> Option<(String, String)> {
    // 先找接班人: 找不到就不冷却 (见上).
    let next = pick_account(store, provider, margin_secs, refresh, client, tried).await;
    if next.is_some() {
        store.cooldown(provider, failed_id, cooldown_secs).await;
    }
    next
}

/// 执行一次续期 (按账号 id).
///
/// 判定 (实测 + 插件文档):
/// - HTTP 非 2xx 或业务码非 0 (腾讯系 `{code:0}` 才是成功) → 失败;
/// - 400/401/403 = 凭据作废 → 置 `dead`, 停止自动续期 (待重新登录);
/// - 其余 (网络/5xx/瞬态) 不置 dead, 下周期重试.
pub async fn refresh_account(
    client: &reqwest::Client,
    rc: &RefreshConfig,
    provider: &str,
    account_id: &str,
    store: &AccountStore,
    margin_secs: u64,
) -> Result<AccountEntry, String> {
    // 串行化 + 拿锁后复查: 另一个任务可能刚续完 → 此时既未过期也不在 margin 内 → 直接返回,
    // 不重复 POST (与调用方判断"需要续期"的口径保持一致: 过期 或 临期 margin 内).
    let _gate = store.refresh_gate.lock().await;
    let entry = store
        .get_by_id(provider, account_id)
        .await
        .ok_or_else(|| format!("账号不存在: {provider}/{account_id}"))?;
    if entry.dead {
        return Err("凭据已失效 (dead), 需重新登录".to_string());
    }
    let now = now_secs();
    if !entry.is_expired(now) && !entry.is_expiring(now, margin_secs) {
        // 已被并发任务续期: 无事可做, 返回当前条目.
        return Ok(entry);
    }
    let rt = entry
        .refresh_token
        .as_deref()
        .filter(|s| !s.is_empty())
        .ok_or("无 refresh_token, 需通过登录流程重新获取")?;

    let method =
        reqwest::Method::from_bytes(rc.method.as_bytes()).unwrap_or(reqwest::Method::POST);
    let mut req = client.request(method, &rc.endpoint);
    for (k, v) in &rc.headers {
        req = req.header(k.as_str(), v.replace("{refresh_token}", rt));
    }
    let resp = req.send().await.map_err(|e| format!("网络错误: {e}"))?;
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    let v: serde_json::Value =
        serde_json::from_str(&text).map_err(|_| format!("非 JSON 响应 (HTTP {status})"))?;
    // 业务码 (存在 code 字段时): 0 才算成功; 否则按 HTTP 状态判定.
    let code_ok = match v.get("code").and_then(|c| c.as_i64()) {
        Some(c) => c == 0,
        None => (200..300).contains(&status),
    };
    if !code_ok {
        let dead = matches!(status, 400 | 401 | 403);
        if dead {
            store
                .update_by_id(provider, account_id, |e| {
                    e.dead = true;
                    e.updated_at = Some(now_secs());
                })
                .await;
        }
        return Err(format!(
            "续期被拒 HTTP {status}{}: {}",
            if dead { " (凭据作废, 已标记需重新登录)" } else { "" },
            crate::i18n::snip(&text, 200)
        ));
    }

    let pick = |path: &str| -> Option<String> {
        let mut cur = &v;
        for seg in path.split('.') {
            cur = cur.get(seg)?;
        }
        // 字符串取原文; **数字也取** (buddy 实测 expiresIn 是数字 4752000, 旧写法只 as_str()
        // 会让 expires_at 恒为 None -> 续期后到期时间丢失 -> 后续"临期"判定短路).
        match cur {
            serde_json::Value::String(s) if !s.is_empty() => Some(s.clone()),
            serde_json::Value::Number(n) => Some(n.to_string()),
            _ => None,
        }
    };
    let access_token = pick(&rc.access_token_path)
        .ok_or_else(|| format!("响应缺少 access_token (路径 {})", rc.access_token_path))?;
    let new_rt = pick(&rc.refresh_token_path); // 未轮换 = 沿用旧值
    let expires_in = pick(&rc.expires_in_path).and_then(|s| s.parse::<u64>().ok());
    let now = now_secs();
    let updated = AccountEntry {
        // id / label / identity 保持不变 (续期只换凭据, 不换身份).
        id: entry.id.clone(),
        label: entry.label.clone(),
        identity: entry.identity.clone(),
        access_token,
        refresh_token: new_rt.or(entry.refresh_token),
        expires_at: expires_in.map(|s| now + s),
        dead: false,
        cooldown_until: None, // 续期成功 = 凭据是好的, 顺手解冷却
        updated_at: Some(now),
    };
    store
        .update_by_id(provider, &entry.id, |e| *e = updated.clone())
        .await;
    Ok(updated)
}

// ── 浏览器一键登录 (2026-10-01, harness buddy-oauth.ts 同款流程) ──
//
// 为什么需要它: 手动粘贴 Access Token 只有开发者会用 —— 普通用户拿不到
// (token 加密存在本机 CodeBuddy IDE 里). 官方登录流是**轮询式**的, 不需要本地
// 回调服务器:
//   1. POST /v2/plugin/auth/state?platform=ide → {state, authUrl} (免认证, X-No-* 头)
//   2. 浏览器打开 authUrl, 用户在官方页登录 (CodeBuddy 产品无需装饰 URL)
//   3. GET  /v2/plugin/auth/token?state=... 轮询 (code 11217=未就绪继续, 5 分钟超时)
//      → accessToken / refreshToken / expiresIn(相对秒) → 写 accounts.json

/// 浏览器一键登录的官方端点.
///
/// 可用 `AIGATE_BUDDY_ENDPOINT` 覆盖 —— 这**不是给用户用的配置**, 而是给端到端测试留的接缝:
/// 该路径原先端点写死, 导致「浏览器登录第二个账号会不会覆盖第一个」这类回归**无法被测到**
/// (2026-10-07 正是如此漏掉一个真 bug: 再登录一个号后列表仍是 1 个). 有了它, 探针可以把
/// state/token 指向 mock, 完整跑一遍 登录→轮询→落盘→账号数, 并用上游实收请求做判据.
fn buddy_endpoint() -> String {
    std::env::var("AIGATE_BUDDY_ENDPOINT")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://copilot.tencent.com".to_string())
}

const BUDDY_UA: &str = "CodeBuddyIDE/1.106.1";
const AUTH_STATE_PATH: &str = "/v2/plugin/auth/state";
const AUTH_TOKEN_PATH: &str = "/v2/plugin/auth/token";
/// token 尚未就绪 (用户还没在浏览器完成登录) → 继续轮询.
const CODE_TOKEN_NOT_READY: i64 = 11217;
/// 登录总超时 (与 harness LOGIN_TIMEOUT_MS 一致).
const LOGIN_TIMEOUT_SECS: u64 = 300;
const LOGIN_POLL_INTERVAL_SECS: u64 = 2;

/// 一次浏览器登录会话 (按供应商名索引; `state` 不下发前端).
#[derive(Debug, Clone, Serialize)]
pub struct LoginSession {
    pub auth_url: String,
    /// "pending" | "ok" | "failed"
    pub status: String,
    pub error: Option<String>,
    pub started_at: u64,
    #[serde(skip)]
    pub state: String,
}

/// 会话表: 供应商名 → 登录会话 (AppState 持有).
pub type LoginSessions = Arc<RwLock<HashMap<String, LoginSession>>>;

fn now_secs_pub() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 第 1 步: 取登录 state + authUrl, 记入会话表 (pending), 返回 authUrl 供前端打开.
pub async fn buddy_login_start(
    client: &reqwest::Client,
    sessions: &LoginSessions,
    provider: &str,
) -> Result<String, String> {
    let url = format!("{}{AUTH_STATE_PATH}?platform=ide", buddy_endpoint());
    let resp = client
        .post(&url)
        .header("X-Domain", "copilot.tencent.com")
        .header("X-No-Authorization", "true")
        .header("X-No-User-Id", "true")
        .header("X-No-Enterprise-Id", "true")
        .header("X-No-Department-Info", "true")
        .header("User-Agent", BUDDY_UA)
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| format!("请求登录 state 失败: {e}"))?;
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("auth/state HTTP {status}: {}", crate::i18n::snip(&body, 200)));
    }
    let v: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("auth/state 响应非 JSON: {e}"))?;
    let state = v.pointer("/data/state").and_then(|s| s.as_str()).unwrap_or("");
    let auth_url = v.pointer("/data/authUrl").and_then(|s| s.as_str()).unwrap_or("");
    if state.is_empty() || auth_url.is_empty() {
        return Err(format!("auth/state 响应缺字段: {}", crate::i18n::snip(&body, 200)));
    }
    sessions.write().await.insert(
        provider.to_string(),
        LoginSession {
            auth_url: auth_url.to_string(),
            status: "pending".to_string(),
            error: None,
            started_at: now_secs_pub(),
            state: state.to_string(),
        },
    );
    Ok(auth_url.to_string())
}

/// 第 2/3 步: 后台轮询 token, 拿到即写 accounts.json 并把会话标 ok (由调用方 spawn).
pub async fn buddy_login_poll(
    client: reqwest::Client,
    sessions: LoginSessions,
    store: AccountStore,
    provider: String,
) {
    let state = match sessions.read().await.get(&provider) {
        Some(s) => s.state.clone(),
        None => return,
    };
    let url = format!("{}{AUTH_TOKEN_PATH}?state={}", buddy_endpoint(), urlencode(&state));
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(LOGIN_TIMEOUT_SECS);
    loop {
        if std::time::Instant::now() >= deadline {
            set_session_fail(&sessions, &provider, "登录超时（5 分钟）".to_string()).await;
            return;
        }
        tokio::time::sleep(std::time::Duration::from_secs(LOGIN_POLL_INTERVAL_SECS)).await;
        // 网络错误同样继续轮询 (与 harness 一致, 不中断登录流程).
        let resp = client
            .get(&url)
            .header("X-No-Authorization", "true")
            .header("User-Agent", BUDDY_UA)
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await;
        let Ok(resp) = resp else { continue };
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        let v: serde_json::Value = match serde_json::from_str(&body) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if status.is_success() {
            if let Some(data) = v.get("data") {
                if data.is_null() {
                    continue; // data:null = 仍未就绪
                }
                match parse_login_token(data) {
                    Some(entry) => {
                        // ⚠️ 必须用 add (追加), 不能用覆盖式写入 —— 多账号下"再登录一个号"
                        // 会静默把已有账号冲掉 (2026-10-07 实测 bug: 浏览器登录第二个号后
                        // 面板仍只显示 1 个). 覆盖式 set() 已整个删除, 就是为了让这类错误
                        // 无法再编译通过.
                        store.add(&provider, entry).await;
                        if let Some(s) = sessions.write().await.get_mut(&provider) {
                            s.status = "ok".to_string();
                            s.error = None;
                        }
                        return;
                    }
                    None => {
                        set_session_fail(
                            &sessions,
                            &provider,
                            format!("token 响应字段异常: {}", crate::i18n::snip(&body, 200)),
                        )
                        .await;
                        return;
                    }
                }
            }
            continue;
        }
        let code = v.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
        if code == CODE_TOKEN_NOT_READY {
            continue;
        }
        let msg = v
            .get("msg")
            .and_then(|m| m.as_str())
            .unwrap_or(&crate::i18n::snip(&body, 200));
        set_session_fail(&sessions, &provider, format!("HTTP {status} code={code}: {msg}")).await;
        return;
    }
}

/// 从 `auth/token` 的 data 解析凭据 (harness parseTokenData 同口径):
/// `expiresAt` 绝对值优先 (兼容毫秒/秒), 否则 `expiresIn` 相对秒;
/// 都拿不到 → expires_at=None (不主动续期, 到期回落 key 链; refresh_token 仍可续).
fn parse_login_token(data: &serde_json::Value) -> Option<AccountEntry> {    let access = data
        .get("accessToken")
        .and_then(|t| t.as_str())
        .filter(|s| !s.is_empty())?;
    let refresh = data
        .get("refreshToken")
        .and_then(|t| t.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    let now = now_secs_pub();
    let expires_at = match data.get("expiresAt").and_then(|e| e.as_i64()) {
        Some(v) if v > 1_000_000_000_000 => Some((v / 1000) as u64), // 毫秒时间戳
        Some(v) if v > 1_000_000_000 => Some(v as u64),               // 秒时间戳
        _ => data
            .get("expiresIn")
            .and_then(|e| e.as_u64())
            .map(|secs| now + secs),
    };
    Some(AccountEntry {
        id: new_account_id(),
        label: None,
        identity: account_identity(access),
        access_token: access.to_string(),
        refresh_token: refresh,
        expires_at,
        dead: false,
        cooldown_until: None,
        updated_at: Some(now),
    })
}

async fn set_session_fail(sessions: &LoginSessions, provider: &str, msg: String) {
    if let Some(s) = sessions.write().await.get_mut(provider) {
        s.status = "failed".to_string();
        s.error = Some(msg);
    }
}

/// application/x-www-form-urlencoded 编码 (state 是服务端下发的随机串, 保守编码).
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::{AuthMode, ProviderConfig, RefreshConfig};

    fn rc() -> RefreshConfig {
        // buddy 实测形态 (2026-10-01): data.accessToken / data.refreshToken / data.expiresIn
        serde_json::from_value(serde_json::json!({
            "endpoint": "https://copilot.tencent.com/v2/plugin/auth/token/refresh",
            "method": "POST",
            "headers": {"X-Refresh-Token": "{refresh_token}"}
        }))
        .unwrap()
    }

    /// pace 打分: "每天会作废多少额度" —— 剩得多且快重置 = 不用就浪费, 应先用.
    #[test]
    fn pace_score_prefers_soon_reset_with_many_credits() {
        let now = 1_700_000_000u64;
        let day = 86400u64;
        // A: 1000 额度, 1 天后重置 → 每天作废 1000 (最该先用)
        let a = pace_score(
            QuotaView { remain: Some(1000.0), cycle_end: Some(now + day) },
            now,
        )
        .unwrap();
        // B: 同额度, 30 天后重置 → 每天作废约 33 (不急)
        let b = pace_score(
            QuotaView { remain: Some(1000.0), cycle_end: Some(now + 30 * day) },
            now,
        )
        .unwrap();
        // C: 10 额度, 1 天后重置 → 每天作废 10 (剩得少, 后用)
        let c = pace_score(
            QuotaView { remain: Some(10.0), cycle_end: Some(now + day) },
            now,
        )
        .unwrap();
        assert!(a < b, "同额度下快重置的应更优先 (分数更小): {a} vs {b}");
        assert!(a < c, "同窗口下剩得多的应更优先: {a} vs {c}");

        // 窗口已过 / 额度为 0 → 无浪费风险, 不该因此被优先 (0 分).
        let past = pace_score(
            QuotaView { remain: Some(500.0), cycle_end: Some(now - day) },
            now,
        )
        .unwrap();
        let empty = pace_score(QuotaView { remain: Some(0.0), cycle_end: Some(now + day) }, now).unwrap();
        assert_eq!(past, 0.0);
        assert_eq!(empty, 0.0);

        // 数据缺失 → None (调用方据此回落 order).
        assert!(pace_score(QuotaView { remain: None, cycle_end: Some(now + day) }, now).is_none());
        assert!(pace_score(QuotaView { remain: Some(100.0), cycle_end: None }, now).is_none());
    }

    /// 默认策略是 order (行为可预测, 保住缓存亲和) —— 未经显式配置不得改变.
    #[test]
    fn default_picking_is_order() {
        assert_eq!(AccountPicking::default(), AccountPicking::Order);
        set_account_picking(AccountPicking::Order);
        assert_eq!(account_picking(), AccountPicking::Order);
        set_account_picking(AccountPicking::Pace);
        assert_eq!(account_picking(), AccountPicking::Pace);
        set_account_picking(AccountPicking::Order); // 还原, 避免影响其它用例
    }

    #[test]
    fn serde_defaults_match_buddy_shape() {
        let r = rc();
        assert_eq!(r.method, "POST");
        assert_eq!(r.access_token_path, "data.accessToken");
        assert_eq!(r.refresh_token_path, "data.refreshToken");
        assert_eq!(r.expires_in_path, "data.expiresIn");
        assert_eq!(r.headers.get("X-Refresh-Token").unwrap(), "{refresh_token}");
        // 旧 providers.json (无 auth 字段) 解析: None = 行为不变
        let p: ProviderConfig = serde_json::from_value(serde_json::json!({
            "name": "old", "endpoint": "https://x/v1/chat/completions", "api_key_env": "K",
            "models": {}
        }))
        .unwrap();
        assert!(p.auth.is_none());
        // account 声明段 roundtrip
        let p2: ProviderConfig = serde_json::from_value(serde_json::json!({
            "name": "b", "endpoint": "https://x/v2/chat/completions", "api_key_env": "K",
            "auth": {"mode": "account", "refresh": {"endpoint": "https://x/refresh"}},
            "models": {}
        }))
        .unwrap();
        let a = p2.auth.expect("auth 应解析");
        assert_eq!(a.mode, AuthMode::Account);
        assert_eq!(a.expiry_margin_secs, 3600, "缺省 margin = 1 小时");
        assert!(a.refresh.is_some());
    }

    #[test]
    fn expiry_semantics() {
        let e = AccountEntry {
            id: "a1".into(),
            label: None,
            identity: None,
            access_token: "t".into(),
            refresh_token: Some("r".into()),
            expires_at: Some(1_000_000),
            dead: false,
            cooldown_until: None,
            updated_at: None,
        };
        assert!(!e.is_expired(999_999));
        assert!(e.is_expired(1_000_000), "到期时刻本身算过期");
        assert!(!e.is_expiring(999_999 - 3601, 3600), "还差 3601 秒不算临期");
        assert!(e.is_expiring(999_999 - 3599, 3600), "剩 3599 秒算临期");
        let e2 = AccountEntry {
            expires_at: None,
            ..e.clone()
        };
        assert!(!e2.is_expired(2_000_000), "无到期信息不主动判过期");
        // 可用性: 作废 / 冷却期内都不可选, 冷却到期自动恢复.
        assert!(e.is_available(0), "正常账号可选");
        let dead = AccountEntry { dead: true, ..e.clone() };
        assert!(!dead.is_available(0), "作废账号不可选");
        let cooling = AccountEntry { cooldown_until: Some(500), ..e.clone() };
        assert!(!cooling.is_available(499), "冷却期内不可选");
        assert!(cooling.is_available(500), "冷却到期自动恢复");
    }

    /// 多账号: 同一供应商可存多个号, 顺序即优先级; 同 token 视为同一个号 (原地更新).
    #[tokio::test]
    async fn store_holds_multiple_accounts_in_priority_order() {
        let dir = std::env::temp_dir().join(format!("aigate_multi_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = AccountStore::new(dir.to_str().unwrap());
        let mk = |tok: &str, exp: Option<u64>| AccountEntry {
            id: String::new(),
            label: None,
            identity: None,
            access_token: tok.into(),
            refresh_token: None,
            expires_at: exp,
            dead: false,
            cooldown_until: None,
            updated_at: None,
        };
        let id1 = store.add("buddy", mk("T1", None)).await;
        let id2 = store.add("buddy", mk("T2", None)).await;
        assert_ne!(id1, id2, "两个号应有不同 id");
        let list = store.entries("buddy").await;
        assert_eq!(list.len(), 2, "应存下两个账号");
        assert_eq!(list[0].access_token, "T1", "顺序 = 优先级 (先加的在前)");
        // 同 token 再登录 → 原地更新, 不新增条目
        let id1b = store.add("buddy", mk("T1", Some(123))).await;
        assert_eq!(id1b, id1, "同凭据应保持原 id");
        let list = store.entries("buddy").await;
        assert_eq!(list.len(), 2, "同 token 不得堆出第三条");
        assert_eq!(list[0].expires_at, Some(123), "同 token 应是更新而非新增");
        // 重载 (落盘 → 读回) 保持多账号
        let store2 = AccountStore::new(dir.to_str().unwrap());
        assert_eq!(store2.entries("buddy").await.len(), 2);
        // 按 id 删除只影响那一个
        assert!(store2.remove_by_id("buddy", &id2).await);
        assert_eq!(store2.entries("buddy").await.len(), 1);
        assert!(!store2.remove_by_id("buddy", "nonexistent").await);
        // 状态视图: 每供应商一组, active 指第一个可用号; 且绝不泄 token
        let st = store2.status_map().await;
        let arr = st["buddy"].as_array().expect("状态应为数组");
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["active"], true);
        let s = serde_json::to_string(&st).unwrap();
        assert!(!s.contains("\"T1\""), "状态 API 不得含 token 原文: {s}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 旧文件 (单账号对象) 必须能升级: 加载成单元素数组并补 id, 且回写后仍是数组.
    #[tokio::test]
    async fn legacy_single_account_file_is_upgraded() {
        let dir = std::env::temp_dir().join(format!("aigate_legacy_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // 旧格式: 值是单个对象, 无 id 字段 (0.7.1 及以前)
        std::fs::write(
            dir.join("accounts.json"),
            r#"{"buddy":{"access_token":"OLD_AT","refresh_token":"OLD_RT","expires_at":1700000000,"dead":false}}"#,
        )
        .unwrap();
        let store = AccountStore::new(dir.to_str().unwrap());
        let list = store.entries("buddy").await;
        assert_eq!(list.len(), 1, "旧单账号文件应升级为一元素数组");
        assert_eq!(list[0].access_token, "OLD_AT", "凭据不得丢");
        assert_eq!(list[0].refresh_token.as_deref(), Some("OLD_RT"));
        assert!(!list[0].id.is_empty(), "旧条目应自动补 id");
        // 回写后文件应是数组形态 (后续 add 才能正常追加)
        let raw = std::fs::read_to_string(dir.join("accounts.json")).unwrap();
        assert!(raw.trim_start().starts_with('{'), "仍是按供应商索引的对象");
        assert!(raw.contains('['), "值应已升级为数组: {raw}");
        let store2 = AccountStore::new(dir.to_str().unwrap());
        let id2 = store2
            .add("buddy", AccountEntry {
                id: String::new(), label: None, identity: None, access_token: "NEW_AT".into(),
                refresh_token: None, expires_at: None, dead: false,
                cooldown_until: None, updated_at: None,
            })
            .await;
        assert!(!id2.is_empty());
        assert_eq!(store2.entries("buddy").await.len(), 2, "升级后应能继续追加账号");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn store_roundtrip_and_status_masking() {
        let dir = std::env::temp_dir().join(format!("aigate_acct_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = AccountStore::new(dir.to_str().unwrap());
        store
            .add(
                "buddy",
                AccountEntry {
                    id: String::new(),
                    label: None,
                    identity: None,
                    access_token: "SECRET_AT".into(),
                    refresh_token: Some("SECRET_RT".into()),
                    expires_at: Some(1_700_000_000),
                    dead: false,
                    cooldown_until: None,
                    updated_at: Some(1),
                },
            )
            .await;
        // 落盘 + 重载一致
        let raw = std::fs::read_to_string(dir.join("accounts.json")).unwrap();
        assert!(raw.contains("SECRET_AT"), "文件应含凭据 (本地数据目录)");
        let store2 = AccountStore::new(dir.to_str().unwrap());
        let list2 = store2.entries("buddy").await;
        assert_eq!(list2.len(), 1);
        let e = list2.first().unwrap();
        assert_eq!(e.access_token, "SECRET_AT");
        // 状态视图绝不能泄 token (多账号后值为**数组**, 顺序即优先级)
        let st = store2.status_map().await;
        let s = serde_json::to_string(&st).unwrap();
        assert!(!s.contains("SECRET"), "状态 API 视图不得包含 token 原文: {s}");
        let arr = st["buddy"].as_array().expect("每个供应商的状态是一组账号");
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["dead"], false);
        assert_eq!(arr[0]["expires_at"], 1_700_000_000);
        assert_eq!(arr[0]["active"], true, "唯一的可用号即当前选中的");
        let _ = std::fs::remove_dir_all(&dir);
    }
}