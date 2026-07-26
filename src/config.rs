//! 配置 — 从环境变量读取端口等基本参数.
//!
//! 启动时自动加载同目录下的 .env 文件 (不覆盖已有环境变量).
//! API key 由 providers.json 中的 api_key_env 字段指定, 从对应环境变量读取.

use std::env;

/// 中转程序运行配置.
#[derive(Debug, Clone)]
pub struct Config {
    /// 监听端口.
    pub port: u16,
}

impl Config {
    /// 从环境变量构造配置.
    ///
    /// 优先级: 已有环境变量 > .env 文件 > 默认值.
    /// - `PORT`: 监听端口, 默认 8787.
    pub fn from_env() -> Self {
        load_dotenv();
        Self {
            port: env::var("PORT")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(8787),
        }
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
