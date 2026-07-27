//! 响应缓存 — 对相同 (规范化请求体) 的非流式请求缓存上游响应, 省 token + 延迟.
//!
//! 设计要点:
//! - **默认关闭**, 由 `enabled` (AtomicBool) 控制, 可在管理面板作为"实验功能"开启.
//! - 仅缓存 `stream:false` 且 2xx 的响应; 流式请求不缓存 (命中率低且语义不同).
//! - 缓存键 = 去掉 `stream` 字段后的请求体做确定性序列化 (serde_json 默认用 BTreeMap,
//!   键已排序) 再哈希, 保证"同请求同键".
//! - 内存受 `max_entries` + TTL 双重约束, 不落盘 (本地工具, 隐私可控).

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

/// 单条缓存项.
struct Entry {
    body: String,
    expires_at: Instant,
}

/// 响应缓存运行时统计 (供面板展示).
#[derive(Serialize)]
pub struct CacheStats {
    pub enabled: bool,
    pub ttl_secs: u64,
    pub max_entries: usize,
    pub entries: usize,
    pub hits: u64,
    pub misses: u64,
}

/// 响应缓存: 受条目上限 + TTL 约束的内存缓存.
pub struct ResponseCache {
    enabled: AtomicBool,
    ttl: Duration,
    max_entries: usize,
    map: Mutex<std::collections::HashMap<String, Entry>>,
    hits: AtomicU64,
    misses: AtomicU64,
}

impl ResponseCache {
    pub fn new(enabled: bool, ttl: Duration, max_entries: usize) -> Self {
        Self {
            enabled: AtomicBool::new(enabled),
            ttl,
            max_entries: max_entries.max(1),
            map: Mutex::new(std::collections::HashMap::new()),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    /// 运行时开关 (面板"实验功能"切换).
    pub fn set_enabled(&self, on: bool) {
        self.enabled.store(on, Ordering::SeqCst);
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled.load(Ordering::SeqCst)
    }

    /// 清空所有缓存条目 (保留开关/TTL 等配置, 命中/未命中计数清零).
    pub fn clear(&self) {
        let mut map = self.map.lock().expect("cache lock poisoned");
        map.clear();
        self.hits.store(0, Ordering::SeqCst);
        self.misses.store(0, Ordering::SeqCst);
    }

    /// 由已注入参数的请求体计算缓存键; 流式请求返回 None (不缓存).
    pub fn make_key(body: &Value) -> Option<String> {
        // 仅缓存非流式请求.
        if body.get("stream").and_then(|v| v.as_bool()) == Some(true) {
            return None;
        }
        // 去掉 stream 字段后确定性序列化 (serde_json 默认 BTreeMap, 键已排序).
        let mut canonical = body.clone();
        if let Value::Object(ref mut obj) = canonical {
            obj.remove("stream");
        }
        let text = serde_json::to_string(&canonical).ok()?;
        let mut hasher = DefaultHasher::new();
        text.hash(&mut hasher);
        Some(format!("{:x}", hasher.finish()))
    }

    /// 查询缓存; 命中返回响应体字符串 (并计数 hits), 未命中/过期返回 None (计数 misses).
    pub fn get(&self, key: &str) -> Option<String> {
        if !self.is_enabled() {
            return None;
        }
        let mut map = self.map.lock().expect("cache lock poisoned");
        match map.get(key) {
            Some(e) if e.expires_at > Instant::now() => {
                self.hits.fetch_add(1, Ordering::SeqCst);
                Some(e.body.clone())
            }
            Some(_) => {
                map.remove(key); // 过期项顺手清理
                self.misses.fetch_add(1, Ordering::SeqCst);
                None
            }
            None => {
                self.misses.fetch_add(1, Ordering::SeqCst);
                None
            }
        }
    }

    /// 写入缓存; 超上限时先清过期项, 仍满则淘汰最早的一半.
    pub fn put(&self, key: &str, body: &str) {
        if !self.is_enabled() {
            return;
        }
        let mut map = self.map.lock().expect("cache lock poisoned");
        if map.len() >= self.max_entries {
            let now = Instant::now();
            map.retain(|_, e| e.expires_at > now);
            if map.len() >= self.max_entries {
                let drop = map.len() / 2;
                let keys: Vec<String> = map.keys().take(drop).cloned().collect();
                for k in keys {
                    map.remove(&k);
                }
            }
        }
        map.insert(
            key.to_string(),
            Entry {
                body: body.to_string(),
                expires_at: Instant::now() + self.ttl,
            },
        );
    }

    /// 当前统计快照.
    pub fn stats(&self) -> CacheStats {
        let map = self.map.lock().expect("cache lock poisoned");
        let now = Instant::now();
        let live = map.iter().filter(|(_, e)| e.expires_at > now).count();
        CacheStats {
            enabled: self.is_enabled(),
            ttl_secs: self.ttl.as_secs(),
            max_entries: self.max_entries,
            entries: live,
            hits: self.hits.load(Ordering::SeqCst),
            misses: self.misses.load(Ordering::SeqCst),
        }
    }
}
