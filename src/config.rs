//! 配置 — 从环境变量读取端口等基本参数.
//!
//! 启动时自动加载同目录下的 .env 文件 (不覆盖已有环境变量).
//! API key 由 providers.json 中的 api_key_env 字段指定, 从对应环境变量读取.

use std::env;
use std::time::Duration;

use crate::circuit_breaker::CircuitBreakerConfig;

/// 中转程序运行配置.
#[derive(Debug, Clone)]
pub struct Config {
    /// 监听端口.
    pub port: u16,
    /// 连接超时 (秒) — 仅限制 TCP/TLS 建连阶段, 不限制整体请求时长.
    pub connect_timeout_secs: u64,
    /// 整体请求超时 (秒) — 含等待上游与读取流式响应的总时长.
    pub request_timeout_secs: u64,
    /// 流式响应空闲超时 (秒) — 上游超过此时长未吐出下一块则断开, 防假死.
    pub stream_idle_timeout_secs: u64,
    /// 瞬态失败最大重试次数 (仅对连接/超时错误与流式开始前返回的 5xx 重试, 不含 429). 默认 1.
    pub retry_max: u32,
    /// 管理面板 API 鉴权令牌 (env `AIGATE_ADMIN_TOKEN`). 为空则不鉴权.
    pub admin_token: Option<String>,
    /// 熔断阈值配置 (可经环境变量覆盖).
    pub breaker: CircuitBreakerConfig,
    /// 响应缓存是否开启 (实验功能, 默认关闭, 可在面板开启).
    pub cache_enabled: bool,
    /// 缓存条目 TTL (秒).
    pub cache_ttl_secs: u64,
    /// 缓存最大条目数.
    pub cache_max_entries: usize,
}

impl Config {
    /// 从环境变量构造配置.
    ///
    /// 优先级: 已有环境变量 > .env 文件 > 默认值.
    /// - `PORT`: 监听端口, 默认 8787.
    /// - `CONNECT_TIMEOUT`: 连接超时秒数, 默认 10.
    /// - `AIGATE_ADMIN_TOKEN`: 管理面板 API 鉴权令牌, 默认不鉴权.
    /// - `BREAKER_*`: 熔断阈值, 默认沿用 cc-switch 实战值.
    /// - `RETRY_MAX`: 瞬态失败最大重试次数, 默认 1.
    /// - `CACHE_ENABLED`: 响应缓存开关 (实验功能), 默认 0 (关).
    /// - `CACHE_TTL_SECS`: 缓存条目 TTL 秒数, 默认 300.
    /// - `CACHE_MAX_ENTRIES`: 缓存最大条目数, 默认 1024.
    pub fn from_env() -> Self {
        load_dotenv();
        Self {
            port: env_u16("PORT", 8787),
            connect_timeout_secs: env_u64("CONNECT_TIMEOUT", 10),
            request_timeout_secs: env_u64("REQUEST_TIMEOUT_SECS", 660),
            stream_idle_timeout_secs: env_u64("STREAM_IDLE_TIMEOUT_SECS", 120),
            retry_max: env_u32("RETRY_MAX", 1),
            admin_token: env::var("AIGATE_ADMIN_TOKEN")
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
            breaker: CircuitBreakerConfig {
                failure_threshold: env_u32("BREAKER_FAILURE_THRESHOLD", 4),
                success_threshold: env_u32("BREAKER_SUCCESS_THRESHOLD", 2),
                timeout: Duration::from_secs(env_u64("BREAKER_TIMEOUT_SECS", 60)),
                error_rate_threshold: env_f64("BREAKER_ERROR_RATE", 0.6),
                min_requests: env_u32("BREAKER_MIN_REQUESTS", 10),
            },
            cache_enabled: env_bool("CACHE_ENABLED", false),
            cache_ttl_secs: env_u64("CACHE_TTL_SECS", 300),
            cache_max_entries: env_u64("CACHE_MAX_ENTRIES", 1024) as usize,
        }
    }
}

/// 读取 u16 环境变量, 失败或缺失时回退默认值.
fn env_u16(key: &str, default: u16) -> u16 {
    env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

/// 读取 u64 环境变量, 失败或缺失时回退默认值.
fn env_u64(key: &str, default: u64) -> u64 {
    env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

/// 读取 u32 环境变量, 失败或缺失时回退默认值.
fn env_u32(key: &str, default: u32) -> u32 {
    env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

/// 读取 f64 环境变量, 失败或缺失时回退默认值.
fn env_f64(key: &str, default: f64) -> f64 {
    env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

/// 读取 bool 环境变量, 失败或缺失时回退默认值.
///
/// 真值: `1` / `true` / `yes` (大小写不敏感); 假值: `0` / `false` / `no`; 其余回退默认.
fn env_bool(key: &str, default: bool) -> bool {
    match env::var(key).ok().as_deref() {
        Some("1") | Some("true") | Some("TRUE") | Some("yes") | Some("YES") => true,
        Some("0") | Some("false") | Some("FALSE") | Some("no") | Some("NO") => false,
        _ => default,
    }
}

/// 加载当前目录下的 .env 文件.
///
/// 简单解析: KEY=VALUE, 忽略注释 (#) 和空行, 去除值两端引号.
/// 不覆盖已有环境变量 (让命令行 / 系统环境变量优先).
fn load_dotenv() {
    let Ok(content) = std::fs::read_to_string(".env") else {
        return; // .env 不存在也没关系, 用环境变量或默认值
    };
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            let k = k.trim();
            let v = v.trim().trim_matches('"').trim_matches('\'');
            // 只在环境变量不存在时才设置 (不覆盖)
            if env::var(k).is_err() {
                env::set_var(k, v);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_port_is_8787() {
        env::remove_var("PORT");
        let config = Config::from_env();
        assert_eq!(config.port, 8787);
    }
}
