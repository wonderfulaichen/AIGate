//! opencode Console 一键登录：弹出窗口让用户完成 Google/GitHub 授权，
//! 再从 WebView2 的 CookieManager 取回 `__Host-console_session`
//! （HttpOnly，JS 的 document.cookie 读不到）。
//!
//! # 为什么要弹窗而不是服务端换 token
//!
//! opencode 是标准 OAuth：授权码只发给它自己的回调域名，本地服务端收不到 code
//! （对比 CodeBuddy 有 `auth/token?state=`，服务端能自己换）。所以只能让
//! **浏览器自己走完整条 OAuth**，我们再从浏览器里把 session 取出来。
//!
//! # 为什么轮询而不是"登完点确认"
//!
//! 用户在授权页点完按钮后，opencode 会跳回 `https://opencode.ai/console/wrk_xxx`
//! —— 这个 URL 里就带着组织 ID。所以只要轮询两样东西就够了：
//!   ① Cookie 里有没有 `__Host-console_session`
//!   ② 当前 URL 能不能提取出 `wrk_...`
//! 两者齐了就自动关窗，用户不需要再点任何东西。
//!
//! # 具体窗口实现
//!
//! 见 [`crate::oc_login_win`]（Win32 + WebView2 原生）。
//! ⚠️ 不要试图改回 wry + winit：winit 每次进程只允许一个 EventLoop，
//! 主窗口已占用 → 独立线程建窗必报 `EventLoop can't be recreated`。

use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

/// 登录阶段（面板轮询展示）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    Idle = 0,
    /// 窗口已弹出，等用户在浏览器里完成授权。
    Waiting = 1,
    /// 拿到凭据。
    Done = 2,
    /// 失败（见 `error()`）。
    Failed = 3,
    /// 用户关掉了窗口。
    Cancelled = 4,
}

impl Stage {
    fn from_u8(v: u8) -> Self {
        match v {
            1 => Stage::Waiting,
            2 => Stage::Done,
            3 => Stage::Failed,
            4 => Stage::Cancelled,
            _ => Stage::Idle,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Stage::Idle => "idle",
            Stage::Waiting => "waiting",
            Stage::Done => "done",
            Stage::Failed => "failed",
            Stage::Cancelled => "cancelled",
        }
    }
}

/// 登录换来的凭据。
#[derive(Debug, Clone)]
pub struct Credential {
    /// 可直接作为 Cookie 头用的完整串（`__Host-console_session=st_...`）。
    pub cookie: String,
    /// 组织 ID（`wrk_...`）。
    pub org_id: String,
}

/// 登录的共享状态（登录线程写，HTTP handler 读）。
pub struct LoginState {
    stage: AtomicU8,
    err: Mutex<String>,
    result: Mutex<Option<Credential>>,
    stop: std::sync::atomic::AtomicBool,
}

impl Default for LoginState {
    fn default() -> Self {
        Self::new()
    }
}

impl LoginState {
    pub fn new() -> Self {
        Self {
            stage: AtomicU8::new(Stage::Idle as u8),
            err: Mutex::new(String::new()),
            result: Mutex::new(None),
            stop: std::sync::atomic::AtomicBool::new(false),
        }
    }

    pub fn stage(&self) -> Stage {
        Stage::from_u8(self.stage.load(Ordering::Relaxed))
    }
    pub fn error(&self) -> String {
        self.err.lock().map(|s| s.clone()).unwrap_or_default()
    }
    pub fn take_result(&self) -> Option<Credential> {
        self.result.lock().ok().and_then(|mut r| r.take())
    }
    pub fn should_stop(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    pub fn begin(&self) {
        self.stop.store(false, Ordering::Relaxed);
        if let Ok(mut e) = self.err.lock() {
            e.clear();
        }
        if let Ok(mut r) = self.result.lock() {
            *r = None;
        }
        self.stage.store(Stage::Waiting as u8, Ordering::Relaxed);
    }

    pub fn finish_ok(&self, cred: Credential) {
        if let Ok(mut r) = self.result.lock() {
            *r = Some(cred);
        }
        self.stage.store(Stage::Done as u8, Ordering::Relaxed);
        self.stop.store(true, Ordering::Relaxed);
    }

    pub fn finish_err(&self, msg: impl Into<String>) {
        if let Ok(mut e) = self.err.lock() {
            *e = msg.into();
        }
        self.stage.store(Stage::Failed as u8, Ordering::Relaxed);
        self.stop.store(true, Ordering::Relaxed);
    }

    pub fn cancel(&self) {
        if self.stage() == Stage::Waiting {
            self.stage.store(Stage::Cancelled as u8, Ordering::Relaxed);
        }
        self.stop.store(true, Ordering::Relaxed);
    }
}

/// 从一段文本里提取组织 ID（`wrk_` + 字母数字）。
pub fn extract_org_id(text: &str) -> Option<String> {
    let b = text.as_bytes();
    let mut i = 0;
    while i + 4 <= b.len() {
        if &b[i..i + 4] == b"wrk_" {
            let mut j = i + 4;
            while j < b.len() && b[j].is_ascii_alphanumeric() {
                j += 1;
            }
            if j > i + 4 {
                return Some(text[i..j].to_string());
            }
        }
        i += 1;
    }
    None
}

/// 由 cookie 列表 + URL 里的工作区 ID 组装凭据；任一缺失返回 `None`（继续等）。
///
/// ⚠️ 这里**不过滤前缀**（2026-10-26 修）：早期要求 `starts_with("wrk_")`，
/// 但实测同一账号下 `org_` 前缀的工作区同样能查 Go 额度（且额度独立）。
/// 用户登录后如果落到 `org_` 工作区，旧逻辑会**永远等不到凭据**、
/// 表现为"窗口开着但一直不结束"。
///
/// 注意这只是**登录收口的判据**：认到工作区就说明登录完成了。
/// 真正要查额度时会用 Cookie 重新拉取**全部**工作区（见 `fetch_workspaces`），
/// 不依赖这里抓到的这一个。
pub fn credential_from_cookies(
    cookies: &[(String, String)],
    org_hint: Option<&str>,
) -> Option<Credential> {
    let session = cookies
        .iter()
        .find(|(n, _)| n == "__Host-console_session")
        .map(|(_, v)| v.trim())
        .filter(|v| !v.is_empty())?;
    // URL 里的工作区段：形如 /console/<id>[/go]。只要求非空，
    // 不限定前缀（wrk_ / org_ 都能查额度）。
    let org = org_hint.map(str::trim).filter(|s| !s.is_empty())?;
    Some(Credential {
        cookie: format!("__Host-console_session={session}"),
        org_id: org.to_string(),
    })
}

/// 弹窗登录并阻塞该新线程直到拿到凭据 / 用户取消 / 出错。
pub fn spawn_login(state: Arc<LoginState>) {
    state.begin();
    thread::spawn(move || {
        #[cfg(windows)]
        {
            if let Err(e) = crate::oc_login_win::run_login_window(Arc::clone(&state)) {
                // 只在"还没结果"时记失败：已 finish_ok 的不覆盖
                if state.stage() == Stage::Waiting {
                    state.finish_err(e);
                }
            }
        }
        #[cfg(not(windows))]
        {
            state.finish_err("一键登录仅支持 Windows");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_org_from_redirect_url() {
        assert_eq!(
            extract_org_id("https://opencode.ai/console/wrk_01KS5PNNQH9TAEBFMNJ5C8AF07"),
            Some("wrk_01KS5PNNQH9TAEBFMNJ5C8AF07".to_string())
        );
        assert_eq!(
            extract_org_id("https://opencode.ai/console/wrk_ABC123/go"),
            Some("wrk_ABC123".to_string())
        );
        // 未登录时停在登录页，没有 wrk_ → None（继续等，不当成失败）
        assert_eq!(extract_org_id("https://opencode.ai/console/login?next=%2Fconsole%2F"), None);
        assert_eq!(extract_org_id(""), None);
        assert_eq!(extract_org_id("wrk_"), None);
    }

    /// **必须能区分「还没登完」与「登完了」**：缺任一条件都返回 None 让轮询继续，
    /// 不能因为"cookie 有了但 URL 还没跳"就提前收口。
    #[test]
    fn credential_requires_both_cookie_and_org() {
        let cookies = vec![("__Host-console_session".to_string(), "st_abc".to_string())];
        // 只有 cookie → None（继续等 URL 跳转）
        assert!(credential_from_cookies(&cookies, None).is_none());
        // 只有 org → None
        assert!(credential_from_cookies(&[], Some("wrk_X")).is_none());
        // ⚠️ org_ 前缀**必须也认**（2026-10-26 修）：实测同账号下
        // org_ 工作区同样能查 Go 额度。旧逻辑要求 starts_with("wrk_")，
        // 用户落到 org_ 工作区时会**永远等不到凭据**（窗口开着不结束）。
        let c = credential_from_cookies(&cookies, Some("org_123"))
            .expect("org_ 前缀的工作区也要能收口");
        assert_eq!(c.org_id, "org_123");
        // 齐了 → Some
        let c = credential_from_cookies(&cookies, Some("wrk_X")).unwrap();
        assert_eq!(c.cookie, "__Host-console_session=st_abc");
        assert_eq!(c.org_id, "wrk_X");
        // cookie 值为空 → None
        let blank = vec![("__Host-console_session".to_string(), "  ".to_string())];
        assert!(credential_from_cookies(&blank, Some("wrk_X")).is_none());
        // 空白 org 也不能当凭据（避免 "  " 被当成工作区）
        assert!(credential_from_cookies(&cookies, Some("   ")).is_none());
        // 别的 cookie 混在一起时只认 session
        let mixed = vec![
            ("__stripe_mid".to_string(), "zzz".to_string()),
            ("__Host-console_session".to_string(), "st_right".to_string()),
        ];
        let c = credential_from_cookies(&mixed, Some("wrk_X")).unwrap();
        assert!(c.cookie.contains("st_right"));
        assert!(!c.cookie.contains("zzz"));
    }

    #[test]
    fn state_machine_transitions() {
        let s = LoginState::new();
        assert_eq!(s.stage(), Stage::Idle);
        assert!(!s.should_stop());

        s.begin();
        assert_eq!(s.stage(), Stage::Waiting);
        assert!(!s.should_stop(), "开始后不能立刻要求停止（否则窗口一闪就没）");

        s.finish_ok(Credential { cookie: "c".into(), org_id: "wrk_A".into() });
        assert_eq!(s.stage(), Stage::Done);
        assert!(s.should_stop());
        let cred = s.take_result().expect("应能取到凭据");
        assert_eq!(cred.org_id, "wrk_A");
        assert!(s.take_result().is_none(), "凭据只能取一次（避免重复写配置）");

        // 失败路径
        let s2 = LoginState::new();
        s2.begin();
        s2.finish_err("窗口创建失败");
        assert_eq!(s2.stage(), Stage::Failed);
        assert_eq!(s2.error(), "窗口创建失败");

        // 取消：只有 Waiting 才转 Cancelled（已 Done 的不能被改成 Cancelled）
        let s3 = LoginState::new();
        s3.begin();
        s3.cancel();
        assert_eq!(s3.stage(), Stage::Cancelled);
    }
}
