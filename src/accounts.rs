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
}

/// 账号凭据存储. `data/accounts.json`, 按 `provider.name` 索引.
#[derive(Clone)]
pub struct AccountStore {
    path: PathBuf,
    map: Arc<RwLock<HashMap<String, AccountEntry>>>,
    /// 续期串行闸: 后台调度与请求路径可能同时判定"临期", 串行化并复查,
    /// 避免对上游并发 POST 同一个 refresh_token (若上游严格轮换, 后到者 4xx 会把
    /// 刚续好的健康账号误标 dead). 刷新是低频操作, 全局一把锁足够.
    refresh_gate: Arc<tokio::sync::Mutex<()>>,
}

impl AccountStore {
    /// 从 data 目录加载 (文件不存在 = 空账号池, 不是错误).
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("accounts.json");
        let map: HashMap<String, AccountEntry> = std::fs::read_to_string(&path)
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or_default();
        Self {
            path,
            map: Arc::new(RwLock::new(map)),
            refresh_gate: Arc::new(tokio::sync::Mutex::new(())),
        }
    }

    pub async fn get(&self, provider: &str) -> Option<AccountEntry> {
        self.map.read().await.get(provider).cloned()
    }

    /// 写入单条 (并原子落盘).
    pub async fn set(&self, provider: &str, entry: AccountEntry) {
        let mut m = self.map.write().await;
        m.insert(provider.to_string(), entry);
        let snapshot = m.clone();
        drop(m);
        Self::write_atomic(&self.path, &snapshot);
    }

    /// 删除单条 (登出: 清凭据, 并原子落盘). 不存在则无事.
    pub async fn remove(&self, provider: &str) {
        let mut m = self.map.write().await;
        if m.remove(provider).is_some() {
            let snapshot = m.clone();
            drop(m);
            Self::write_atomic(&self.path, &snapshot);
        }
    }

    /// 面板状态视图: 到期剩余秒数 / 是否作废 (永不返回 token 原文).
    pub async fn status_map(&self) -> HashMap<String, serde_json::Value> {
        let now = now_secs();
        self.map
            .read()
            .await
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    serde_json::json!({
                        "expires_at": v.expires_at,
                        "expires_in_secs": v.expires_at.map(|t| t.saturating_sub(now) as i64),
                        "dead": v.dead,
                        "has_refresh_token": v.refresh_token.is_some(),
                        "updated_at": v.updated_at,
                    }),
                )
            })
            .collect()
    }

    fn write_atomic(path: &std::path::Path, map: &HashMap<String, AccountEntry>) {
        let json = serde_json::to_string_pretty(map).unwrap_or_default();
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, &json).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }
}

/// 取可用 token: 账号缺失/已作废/已过期且无法续期 → None (调用方回落 api_key 链).
///
/// 续期策略: 临期(margin 内) 尝试续期、失败仍用旧值 (还没过期);
/// 硬过期则必须续期成功, 否则 None —— 拿过期 token 打上游只会换来必败的 401.
pub async fn usable_token(
    store: &AccountStore,
    provider: &str,
    margin_secs: u64,
    refresh: Option<&RefreshConfig>,
    client: &reqwest::Client,
) -> Option<String> {
    let mut entry = store.get(provider).await?;
    if entry.dead {
        return None;
    }
    let now = now_secs();
    if entry.is_expired(now) {
        let rc = refresh?;
        match refresh_account(client, rc, provider, store, margin_secs).await {
            Ok(new) => entry = new,
            Err(e) => {
                tracing::warn!("accounts: '{provider}' 过期续期失败: {e} -> 回落 api_key 链");
                return None;
            }
        }
    } else if entry.is_expiring(now, margin_secs) {
        if let Some(rc) = refresh {
            // 临期续期失败不致命: 旧 token 尚未过期, 继续使用, 下周期重试.
            if let Ok(new) = refresh_account(client, rc, provider, store, margin_secs).await {
                entry = new;
            }
        }
    }
    Some(entry.access_token)
}

/// 执行一次续期.
///
/// 判定 (实测 + 插件文档):
/// - HTTP 非 2xx 或业务码非 0 (腾讯系 `{code:0}` 才是成功) → 失败;
/// - 400/401/403 = 凭据作废 → 置 `dead`, 停止自动续期 (待重新登录);
/// - 其余 (网络/5xx/瞬态) 不置 dead, 下周期重试.
pub async fn refresh_account(
    client: &reqwest::Client,
    rc: &RefreshConfig,
    provider: &str,
    store: &AccountStore,
    margin_secs: u64,
) -> Result<AccountEntry, String> {
    // 串行化 + 拿锁后复查: 另一个任务可能刚续完 → 此时既未过期也不在 margin 内 → 直接返回,
    // 不重复 POST (与调用方判断"需要续期"的口径保持一致: 过期 或 临期 margin 内).
    let _gate = store.refresh_gate.lock().await;
    let entry = store
        .get(provider)
        .await
        .ok_or_else(|| format!("账号不存在: {provider}"))?;
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
            let mut e = entry;
            e.dead = true;
            e.updated_at = Some(now_secs());
            store.set(provider, e).await;
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
    let entry = AccountEntry {
        access_token,
        refresh_token: new_rt.or(entry.refresh_token),
        expires_at: expires_in.map(|s| now + s),
        dead: false,
        updated_at: Some(now),
    };
    store.set(provider, entry.clone()).await;
    Ok(entry)
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

const BUDDY_ENDPOINT: &str = "https://copilot.tencent.com";
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
    let url = format!("{BUDDY_ENDPOINT}{AUTH_STATE_PATH}?platform=ide");
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
    let url = format!("{BUDDY_ENDPOINT}{AUTH_TOKEN_PATH}?state={}", urlencode(&state));
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
                        store.set(&provider, entry).await;
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
fn parse_login_token(data: &serde_json::Value) -> Option<AccountEntry> {
    let access = data
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
        access_token: access.to_string(),
        refresh_token: refresh,
        expires_at,
        dead: false,
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
            access_token: "t".into(),
            refresh_token: Some("r".into()),
            expires_at: Some(1_000_000),
            dead: false,
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
    }

    #[tokio::test]
    async fn store_roundtrip_and_status_masking() {
        let dir = std::env::temp_dir().join(format!("aigate_acct_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = AccountStore::new(dir.to_str().unwrap());
        store
            .set(
                "buddy",
                AccountEntry {
                    access_token: "SECRET_AT".into(),
                    refresh_token: Some("SECRET_RT".into()),
                    expires_at: Some(1_700_000_000),
                    dead: false,
                    updated_at: Some(1),
                },
            )
            .await;
        // 落盘 + 重载一致
        let raw = std::fs::read_to_string(dir.join("accounts.json")).unwrap();
        assert!(raw.contains("SECRET_AT"), "文件应含凭据 (本地数据目录)");
        let store2 = AccountStore::new(dir.to_str().unwrap());
        let e = store2.get("buddy").await.unwrap();
        assert_eq!(e.access_token, "SECRET_AT");
        // 状态视图绝不能泄 token
        let st = store2.status_map().await;
        let s = serde_json::to_string(&st).unwrap();
        assert!(!s.contains("SECRET"), "状态 API 视图不得包含 token 原文: {s}");
        assert_eq!(st["buddy"]["dead"], false);
        assert_eq!(st["buddy"]["expires_at"], 1_700_000_000);
        let _ = std::fs::remove_dir_all(&dir);
    }
}