//! 上游代理策略 — 环境变量判定 (单一事实来源).
//!
//! 启动时由 `main.rs::apply_proxy_env` 消费构建 reqwest Client,
//! 同时供管理面板 `/admin/api/proxy-config` 展示当前策略状态.

/// 代理策略状态 (供面板展示).
#[derive(Debug, Clone, serde::Serialize)]
pub struct ProxyStatus {
    /// "system"=继承系统代理 / "no_proxy"=禁用代理 / "custom"=自定义代理.
    pub mode: &'static str,
    /// 自定义代理 URL (mode=custom 时).
    pub url: Option<String>,
    /// AIGATE_NO_PROXY 环境变量原始值.
    pub no_proxy_env: Option<String>,
    /// AIGATE_PROXY 环境变量原始值.
    pub proxy_env: Option<String>,
}

/// 读取环境变量, 判定当前代理策略.
///
/// - `AIGATE_NO_PROXY` 为真 (1/true/yes/on, 不区分大小写) → 完全禁用代理, 绕过系统代理.
/// - `AIGATE_PROXY` 设了非空 URL → 显式使用该代理 (覆盖系统代理), 同时作用于 http/https.
/// - 两者都未设 → 走 reqwest 默认行为 (继承系统 HTTPS_PROXY/https_proxy).
///
/// 背景: reqwest 默认继承系统代理, 当系统代理无法与上游建立 TLS 隧道时会表现为
/// "unexpected EOF during handshake" + 502. 该开关用于绕过故障系统代理或指定可用代理.
pub fn proxy_status() -> ProxyStatus {
    let no_proxy_val = std::env::var("AIGATE_NO_PROXY").ok();
    let proxy_val = std::env::var("AIGATE_PROXY").ok();
    let no_proxy = no_proxy_val
        .as_deref()
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false);
    if no_proxy {
        ProxyStatus { mode: "no_proxy", url: None, no_proxy_env: no_proxy_val, proxy_env: proxy_val }
    } else if let Some(url) = proxy_val.as_deref().filter(|u| !u.is_empty()) {
        ProxyStatus { mode: "custom", url: Some(url.to_string()), no_proxy_env: no_proxy_val, proxy_env: proxy_val }
    } else {
        ProxyStatus { mode: "system", url: None, no_proxy_env: no_proxy_val, proxy_env: proxy_val }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // 环境变量是进程级全局, 并行测试会互相污染, 需串行化.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn clean_env() {
        std::env::remove_var("AIGATE_NO_PROXY");
        std::env::remove_var("AIGATE_PROXY");
    }

    #[test]
    fn no_proxy_wins_over_custom() {
        let _g = ENV_LOCK.lock().unwrap();
        clean_env();
        std::env::set_var("AIGATE_NO_PROXY", "true");
        std::env::set_var("AIGATE_PROXY", "http://127.0.0.1:7890");
        let s = proxy_status();
        assert_eq!(s.mode, "no_proxy");
        clean_env();
    }

    #[test]
    fn custom_proxy_detected() {
        let _g = ENV_LOCK.lock().unwrap();
        clean_env();
        std::env::set_var("AIGATE_PROXY", "http://127.0.0.1:7890");
        let s = proxy_status();
        assert_eq!(s.mode, "custom");
        assert_eq!(s.url.as_deref(), Some("http://127.0.0.1:7890"));
        clean_env();
    }

    #[test]
    fn empty_custom_falls_back_to_system() {
        let _g = ENV_LOCK.lock().unwrap();
        clean_env();
        std::env::set_var("AIGATE_PROXY", "");
        let s = proxy_status();
        assert_eq!(s.mode, "system");
        clean_env();
    }

    #[test]
    fn default_is_system() {
        let _g = ENV_LOCK.lock().unwrap();
        clean_env();
        let s = proxy_status();
        assert_eq!(s.mode, "system");
    }
}
