//! 日志持久化 — 以 JSON Lines 格式存储请求日志到文件.
//!
//! 启动时自动加载已有日志到内存缓冲区, 写操作异步追加到文件.
//! 文件路径: data/logs.jsonl, 自动创建 data/ 目录.

use std::path::PathBuf;

use tokio::io::AsyncWriteExt;

use crate::admin::RequestLog;

/// 日志文件超过此字节数后触发滚动.
const ROTATE_BYTES: u64 = 2 * 1024 * 1024;
/// 滚动时保留的最近日志条数.
const MAX_LINES: usize = 5000;

/// JSON Lines 日志存储.
#[derive(Clone)]
pub struct LogStore {
    file_path: PathBuf,
}

impl LogStore {
    /// 创建存储, 自动创建 data/ 目录.
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        Self {
            file_path: dir.join("logs.jsonl"),
        }
    }

    /// 启动时从文件加载已有日志, 返回最多 `max` 条 (最新的).
    pub fn load(&self, max: usize) -> Vec<RequestLog> {
        let content = match std::fs::read_to_string(&self.file_path) {
            Ok(c) => c,
            Err(_) => return Vec::new(),
        };
        let mut logs: Vec<RequestLog> = content
            .lines()
            .filter_map(|line| serde_json::from_str::<RequestLog>(line).ok())
            .collect();
        // 只保留最新的 max 条
        if logs.len() > max {
            logs = logs.split_off(logs.len() - max);
        }
        logs
    }

    /// 异步追加一条日志到文件.
    pub async fn append(&self, log: &RequestLog) {
        let line = match serde_json::to_string(log) {
            Ok(s) => s + "\n",
            Err(_) => return,
        };
        if let Ok(mut file) = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.file_path)
            .await
        {
            let _ = file.write_all(line.as_bytes()).await;
        }
        // 超过阈值则滚动, 仅保留最近 MAX_LINES 条, 避免文件无限增长.
        if let Ok(meta) = tokio::fs::metadata(&self.file_path).await {
            if meta.len() > ROTATE_BYTES {
                self.rotate().await;
            }
        }
    }

    /// 滚动: 读取全部, 仅保留最近 MAX_LINES 条并重写文件.
    async fn rotate(&self) {
        let content = match tokio::fs::read_to_string(&self.file_path).await {
            Ok(c) => c,
            Err(_) => return,
        };
        let lines: Vec<&str> = content.lines().collect();
        let kept: Vec<&str> = if lines.len() > MAX_LINES {
            lines[lines.len() - MAX_LINES..].to_vec()
        } else {
            lines
        };
        let mut out = String::with_capacity(kept.len() * 64);
        for l in kept {
            out.push_str(l);
            out.push('\n');
        }
        if let Ok(mut file) = tokio::fs::File::create(&self.file_path).await {
            let _ = file.write_all(out.as_bytes()).await;
        }
    }

    /// 异步用完整列表重写文件 (clear 后调用).
    pub async fn rewrite(&self, logs: &[RequestLog]) {
        let content: String = logs
            .iter()
            .filter_map(|l| serde_json::to_string(l).ok())
            .map(|s| s + "\n")
            .collect();
        if let Ok(mut file) = tokio::fs::File::create(&self.file_path).await {
            let _ = file.write_all(content.as_bytes()).await;
        }
    }
}
