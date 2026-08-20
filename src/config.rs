//! 配置 — 从环境变量读取端口等基本参数.
//!
//! 启动时自动加载同目录下的 .env 文件 (不覆盖已有环境变量).
//! API key 由 providers.json 中的 api_key_env 字段指定, 从对应环境变量读取.

use std::env;
use std::path::PathBuf;
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
    /// 重试退避基数 (毫秒): 每次重试前等待 base*attempt + 抖动, 避免瞬时抖动放大对上游压力. 默认 200.
    pub retry_backoff_ms: u64,
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
    /// 响应缓存持久化路径 (实验功能, 默认 None = 不落盘, 纯内存).
    /// 非空则缓存落盘, 正常退出→重启自动加载 (冷启动预热).
    /// 设 `1`/`true`/`yes` → 默认 `data/response_cache.json`; 设具体路径 → 使用该路径.
    /// ⚠️ 隐私: 落盘体含对话 messages 明文. 启用即接受本地落盘风险.
    pub cache_persist_path: Option<PathBuf>,
    /// 模型死循环检测配置 (可经环境变量覆盖, 默认开启).
    pub loop_guard: LoopGuardConfig,
    /// 转发上游前是否剥离历史 assistant 消息中的推理链 (reasoning_content/reasoning).
    /// 默认开启: 多轮对话中上游回传的推理链会随历史累积, 既浪费输入 token 又无推理价值
    /// (推理链不应被"喂回"模型), 还会干扰 KV 缓存命中. 带 tool_calls 的 assistant 消息保留
    /// (部分客户端规范要求推理链与工具调用并存).
    pub strip_history_reasoning: bool,
    /// 长会话历史裁剪: 仅保留最近 N 条 user 轮, 更早的历史整体丢弃 (降低每轮 input token).
    /// 默认 0 = 不裁剪 (保持现状). 设为正整数即开启 (推荐 10~30). 这是"以质量换成本"的
    /// 显式开关, 默认关闭以免悄悄丢失早期上下文依赖. system 消息始终保留, tool 链随所属
    /// user 轮一并保留/丢弃.
    pub max_history_turns: usize,
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
    /// - `RETRY_BACKOFF_MS`: 重试退避基数毫秒, 默认 200.
    /// - `CACHE_ENABLED`: 响应缓存开关 (实验功能), 默认 0 (关).
    /// - `CACHE_TTL_SECS`: 缓存条目 TTL 秒数, 默认 300.
    /// - `CACHE_MAX_ENTRIES`: 缓存最大条目数, 默认 1024.
    /// - `CACHE_PERSIST`: 响应缓存持久化路径 (实验功能, 默认不落盘).
    ///   设 `1`/`true` → 默认 `data/response_cache.json`; 设具体路径 → 使用该路径; 默认/空/`0` 不落盘.
    ///   启用后正常退出自动加载历史缓存 (冷启动预热). 注意: 落盘含对话明文, 接受本地落盘风险再启用.
    pub fn from_env() -> Self {
        load_dotenv();
        Self {
            port: env_u16("PORT", 8787),
            connect_timeout_secs: env_u64("CONNECT_TIMEOUT", 10),
            request_timeout_secs: env_u64("REQUEST_TIMEOUT_SECS", 660),
            stream_idle_timeout_secs: env_u64("STREAM_IDLE_TIMEOUT_SECS", 120),
            retry_max: env_u32("RETRY_MAX", 1),
            retry_backoff_ms: env_u64("RETRY_BACKOFF_MS", 200),
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
            cache_persist_path: parse_cache_persist_path(),
            loop_guard: LoopGuardConfig {
                enabled: env_bool("LOOP_GUARD_ENABLED", true),
                window: env_usize("LOOP_GUARD_WINDOW", 384),
                min_repeat: env_usize("LOOP_GUARD_MIN_REPEAT", 6),
                max_buffer: env_usize("LOOP_GUARD_MAX_BUFFER", 4096),
            },
            strip_history_reasoning: env_bool("STRIP_HISTORY_REASONING", true), // 默认开启
            max_history_turns: env_usize("MAX_HISTORY_TURNS", 0), // 默认 0 = 不裁剪 (保持上下文完整性, 旧版行为); 超限时由 proxy 紧急瘦身兜底
        }
    }
}

/// 模型死循环检测配置.
#[derive(Debug, Clone)]
pub struct LoopGuardConfig {
    /// 是否启用循环检测 (默认开启).
    pub enabled: bool,
    /// 检测窗口字符数 (仅在最近 N 个字符内检测重复).
    pub window: usize,
    /// 连续重复最小次数 (达到即判为死循环).
    pub min_repeat: usize,
    /// 环形缓冲上限字符数.
    pub max_buffer: usize,
}

/// 读取 u16 环境变量, 失败或缺失时回退默认值.
fn env_u16(key: &str, default: u16) -> u16 {
    env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

/// 读取 usize 环境变量, 失败或缺失时回退默认值.
fn env_usize(key: &str, default: usize) -> usize {
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

/// 解析 `CACHE_PERSIST` 环境变量为落盘路径.
///
/// - `1`/`true`/`yes` → 默认 `data/response_cache.json`;
/// - 其他非空字符串 → 当作具体路径;
/// - 缺失/空/`0`/`false`/`no` → `None` (不落盘).
fn parse_cache_persist_path() -> Option<PathBuf> {
    let raw = env::var("CACHE_PERSIST").ok()?.trim().to_string();
    if raw.is_empty() {
        return None;
    }
    match raw.to_ascii_lowercase().as_str() {
        "0" | "false" | "no" | "none" => None,
        "1" | "true" | "yes" => Some(PathBuf::from("data/response_cache.json")),
        _ => Some(PathBuf::from(raw)),
    }
}

/// 加载当前目录下的 .env 文件.
///
/// 简单解析: KEY=VALUE, 忽略注释 (#) 和空行, 去除值两端引号.
/// 不覆盖已有环境变量 (让命令行 / 系统环境变量优先).
/// 兼容多部署目录: 优先 exe 同目录, 其次尝试常见部署目录 AI中转.
fn load_dotenv() {
    let content = std::fs::read_to_string(".env")
        .or_else(|_| {
            // 回落: AI中转 部署目录 (与源码同级)
            let fallback = std::path::Path::new("D:\\Office software\\Development Project\\AI中转\\.env");
            std::fs::read_to_string(fallback)
        })
        .or_else(|_| {
            // 再回落: exe 上一级的 AI中转
            if let Ok(exe) = std::env::current_exe() {
                if let Some(parent) = exe.parent().and_then(|p| p.parent()) {
                    let p2 = parent.join("AI中转").join(".env");
                    if let Ok(c) = std::fs::read_to_string(&p2) {
                        return Ok(c);
                    }
                }
            }
            Err(std::io::Error::new(std::io::ErrorKind::NotFound, "no .env"))
        });
    let Ok(content) = content else {
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
