//! OpenCode 一键登录 (OAuth 授权码流程, 对齐 opencode.ai/zen 网页登录).
//!
//! 参考 68hub: 其"一键登录"本质是开一个自己控制的嵌入式浏览器让用户登录, 登录后从浏览器
//! cookie jar 抠出 `auth` cookie. AIGate 无嵌入式浏览器, 故改用**本地 OAuth 回调**这一等价实现
//! (也正是 opencode CLI 登录所用机制):
//!
//! 1. 面板按钮 → 打开 `auth.opencode.ai/authorize` (redirect_uri 指向 AIGate 本地回调)
//! 2. 用户在自己的浏览器里登录 opencode
//! 3. opencode 把 `code` 跳回 AIGate 本地回调 `GET /admin/api/opencode-login/callback`
//! 4. 后端拿 `code` 去 token 端点换会话, 落盘到 opencode_accounts (无需手填 cookie)
//!
//! ⚠️ token 端点与"code 换回的令牌是否等同 `auth` cookie"均属 opencode 未公开实现, 此处采用
//! 标准 OIDC 约定 (`/oauth/token` + `access_token` 当作 `auth` cookie 值). 若实测不符,
//! 仅需调整下方 `TOKEN_ENDPOINT` 常量与 `exchange_code` 的字段映射, 不影响其余链路.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, UNIX_EPOCH};

/// 授权端点 (与用户提供的 opencode.ai/zen 登录地址一致).
const AUTH_AUTHORIZE: &str = "https://auth.opencode.ai/authorize";
/// 公共客户端 ID (opencode 网页登录使用, 无 client_secret).
const CLIENT_ID: &str = "app";
/// Token 端点 (实测 opencode 真实路径无 /oauth 前缀, 原 /oauth/token 返回 404).
const TOKEN_ENDPOINT: &str = "https://auth.opencode.ai/token";
/// state 有效期 (秒): 防止回调被重放/伪造.
const STATE_TTL_SECS: u64 = 300;

/// 生成不可预测的 state (CSRF 防护 + 关联本次登录流程).
pub fn gen_state() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let c = COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    format!("{nanos:032x}-{pid:08x}-{c:016x}")
}

/// 已发起登录流程的 state 存储 (短生命周期, 仅用于校验回调合法性).
#[derive(Clone)]
pub struct OAuthStateStore {
    map: Arc<Mutex<HashMap<String, Instant>>>,
}

impl OAuthStateStore {
    pub fn new() -> Self {
        Self {
            map: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// 登记本次登录流程的 state.
    pub fn insert(&self, state: &str) {
        let mut g = self.map.lock().unwrap();
        g.insert(state.to_string(), Instant::now());
        // 顺手清理过期项, 防止内存缓慢增长.
        let expired = Instant::now() - Duration::from_secs(STATE_TTL_SECS);
        g.retain(|_, t| *t > expired);
    }

    /// 消费并校验 state: 存在且未过期返回 true, 否则 false.
    pub fn consume(&self, state: &str) -> bool {
        let mut g = self.map.lock().unwrap();
        match g.remove(state) {
            Some(t) => t.elapsed() < Duration::from_secs(STATE_TTL_SECS),
            None => false,
        }
    }
}

/// 构建授权 URL (redirect_uri 由调用方传入, 必须为 AIGate 本地回调地址).
pub fn build_authorize_url(redirect_uri: &str, state: &str) -> String {
    format!(
        "{AUTH_AUTHORIZE}?client_id={CLIENT_ID}&redirect_uri={}&response_type=code&state={state}",
        percent_encode(redirect_uri)
    )
}

/// 用授权码兑换会话令牌. 返回 `access_token` (调用方拼成 `auth=<token>` 当作 cookie 值).
pub async fn exchange_code(code: &str, redirect_uri: &str) -> Result<String, String> {
    let client = reqwest::Client::new();
    let body = format!(
        "grant_type=authorization_code&code={}&redirect_uri={}&client_id={CLIENT_ID}",
        percent_encode(code),
        percent_encode(redirect_uri)
    );
    let resp = client
        .post(TOKEN_ENDPOINT)
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Accept", "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("token 请求失败: {e}"))?;

    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("token 端点返回 HTTP {status}: {text}"));
    }

    #[derive(serde::Deserialize)]
    struct TokenResp {
        access_token: Option<String>,
        #[serde(default)]
        error: Option<String>,
        #[serde(default)]
        error_description: Option<String>,
    }

    let tr: TokenResp = serde_json::from_str(&text)
        .map_err(|e| format!("解析 token 响应失败: {e} (原文: {text})"))?;
    tr.access_token.ok_or_else(|| {
        tr.error_description
            .or(tr.error)
            .unwrap_or_else(|| "token 响应缺少 access_token".to_string())
    })
}

/// 最小 Percent-Encode (满足 redirect_uri / code 的 URL 安全转义).
fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => {
                out.push('%');
                out.push_str(&format!("{b:02X}"));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_percent_encode() {
        assert_eq!(percent_encode("abcXYZ019-_.~"), "abcXYZ019-_.~");
        assert_eq!(percent_encode(":/"), "%3A%2F");
        assert_eq!(
            percent_encode("http://127.0.0.1:8787/x"),
            "http%3A%2F%2F127.0.0.1%3A8787%2Fx"
        );
    }

    #[test]
    fn test_state_store_consume() {
        let s = OAuthStateStore::new();
        let st = gen_state();
        assert!(!s.consume(&st), "未登记的 state 应校验失败");
        s.insert(&st);
        assert!(s.consume(&st), "刚登记的 state 应校验通过");
        assert!(!s.consume(&st), "消费后不应再次通过");
    }
}
