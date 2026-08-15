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
    /// 本轮 (进程启动以来) 累计命中次数.
    pub hits: u64,
    /// 本轮 (进程启动以来) 累计未命中次数.
    pub misses: u64,
    /// 本次开启以来累计命中次数 (每次 set_enabled(true) 清零).
    pub enabled_hits: u64,
    /// 本次开启以来累计未命中次数 (每次 set_enabled(true) 清零).
    pub enabled_misses: u64,
    /// 本轮 (进程启动以来) 缓存命中累计省下的 token 数.
    pub saved_tokens: u64,
    /// 本次开启以来缓存命中累计省下的 token 数 (每次 set_enabled(true) 清零).
    pub enabled_saved_tokens: u64,
}

/// 响应缓存: 受条目上限 + TTL 约束的内存缓存.
pub struct ResponseCache {
    enabled: AtomicBool,
    ttl: Duration,
    max_entries: usize,
    map: Mutex<std::collections::HashMap<String, Entry>>,
    /// 本轮 (进程启动以来) 命中计数, 重启清零.
    hits: AtomicU64,
    /// 本轮 (进程启动以来) 未命中计数, 重启清零.
    misses: AtomicU64,
    /// 本次开启以来命中计数, 每次 set_enabled(true) 清零.
    enabled_hits: AtomicU64,
    /// 本次开启以来未命中计数, 每次 set_enabled(true) 清零.
    enabled_misses: AtomicU64,
    /// 本轮 (进程启动以来) 命中省下 token, 重启清零.
    saved_tokens: AtomicU64,
    /// 本次开启以来命中省下 token, 每次 set_enabled(true) 清零.
    enabled_saved_tokens: AtomicU64,
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
            enabled_hits: AtomicU64::new(0),
            enabled_misses: AtomicU64::new(0),
            saved_tokens: AtomicU64::new(0),
            enabled_saved_tokens: AtomicU64::new(0),
        }
    }

    /// 运行时开关 (面板"实验功能"切换).
    /// 每次开启时清零"本次开启以来"计数, 使 enabled 口径 = 本次开启后的实际命中率.
    pub fn set_enabled(&self, on: bool) {
        if on {
            self.enabled_hits.store(0, Ordering::SeqCst);
            self.enabled_misses.store(0, Ordering::SeqCst);
            self.enabled_saved_tokens.store(0, Ordering::SeqCst);
        }
        self.enabled.store(on, Ordering::SeqCst);
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled.load(Ordering::SeqCst)
    }

    /// 记录一次命中省下的 token 量 (调用方由命中响应体 usage 的 prompt+completion 算出,
    /// 即本次若打上游本应消耗/计费的 token, 因命中缓存而省下).
    pub fn record_hit_saved(&self, tokens: u64) {
        self.saved_tokens.fetch_add(tokens, Ordering::SeqCst);
        self.enabled_saved_tokens.fetch_add(tokens, Ordering::SeqCst);
    }

    /// 清空所有缓存条目 (保留开关/TTL 等配置, 本轮与开启后计数均清零).
    pub fn clear(&self) {
        let mut map = self.map.lock().expect("cache lock poisoned");
        map.clear();
        self.hits.store(0, Ordering::SeqCst);
        self.misses.store(0, Ordering::SeqCst);
        self.enabled_hits.store(0, Ordering::SeqCst);
        self.enabled_misses.store(0, Ordering::SeqCst);
        self.saved_tokens.store(0, Ordering::SeqCst);
        self.enabled_saved_tokens.store(0, Ordering::SeqCst);
    }

    /// 由已注入参数的请求体计算缓存键; 流式请求返回 None (不缓存).
    pub fn make_key(body: &Value) -> Option<String> {
        // 仅缓存非流式请求.
        if body.get("stream").and_then(|v| v.as_bool()) == Some(true) {
            return None;
        }
        // 规范化: 去掉不影响上游输出的字段, 让"实质相同请求"跨细微元数据差异也能命中缓存.
        // IDE/中间件常透传随机 user/id/metadata/stream_options, 若不剔除会导致缓存 miss.
        // 采用黑名单保留式: 仅移除已知无关字段, 新增的合理字段默认参与哈希, 避免误伤.
        let mut canonical = body.clone();
        if let Value::Object(ref mut obj) = canonical {
            obj.remove("stream");
            obj.remove("user");
            obj.remove("metadata");
            obj.remove("id");
            obj.remove("stream_options");
            // ⚠️ seed 必须保留: OpenAI 的 seed 直接控制采样确定性 (确定性采样),
            // 不同 seed 的请求输出可能不同 —— 剔除会导致错误命中 (返回其他 seed 的输出).
            // 仅当 seed 为 null 时移除 (null 与缺省语义等价, 不影响输出).
            if obj.get("seed").is_some_and(|v| v.is_null()) {
                obj.remove("seed");
            }
            // n==1 是默认值, 不区分输出; 仅当 n>1 时才有实质差异, 故抹平默认值.
            if obj.get("n").and_then(|v| v.as_u64()) == Some(1) {
                obj.remove("n");
            }
        }
        let text = serde_json::to_string(&canonical).ok()?;
        let mut hasher = DefaultHasher::new();
        text.hash(&mut hasher);
        Some(format!("{:x}", hasher.finish()))
    }

    /// 查询缓存; 命中返回响应体字符串 (并计数 hits), 未命中/过期返回 None (计数 misses).
    /// 本轮与"本次开启以来"两组计数同步累加 (后者供面板展示开启后的实际命中率).
    pub fn get(&self, key: &str) -> Option<String> {
        if !self.is_enabled() {
            return None;
        }
        let mut map = self.map.lock().expect("cache lock poisoned");
        match map.get(key) {
            Some(e) if e.expires_at > Instant::now() => {
                self.hits.fetch_add(1, Ordering::SeqCst);
                self.enabled_hits.fetch_add(1, Ordering::SeqCst);
                Some(e.body.clone())
            }
            Some(_) => {
                map.remove(key); // 过期项顺手清理
                self.misses.fetch_add(1, Ordering::SeqCst);
                self.enabled_misses.fetch_add(1, Ordering::SeqCst);
                None
            }
            None => {
                self.misses.fetch_add(1, Ordering::SeqCst);
                self.enabled_misses.fetch_add(1, Ordering::SeqCst);
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
            enabled_hits: self.enabled_hits.load(Ordering::SeqCst),
            enabled_misses: self.enabled_misses.load(Ordering::SeqCst),
            saved_tokens: self.saved_tokens.load(Ordering::SeqCst),
            enabled_saved_tokens: self.enabled_saved_tokens.load(Ordering::SeqCst),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn make_key_ignores_stream() {
        let a = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"stream":true});
        let b = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"stream":false});
        // stream=true 一律不缓存
        assert!(ResponseCache::make_key(&a).is_none());
        // 去掉 stream 后两者同键
        let c = json!({"model":"x","messages":[{"role":"user","content":"hi"}]});
        assert_eq!(ResponseCache::make_key(&b), ResponseCache::make_key(&c));
    }

    #[test]
    fn make_key_normalizes_noise_fields() {
        let base = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"temperature":0.7});
        let with_noise = json!({
            "model":"x","messages":[{"role":"user","content":"hi"}],"temperature":0.7,
            "user":"random-123","id":"req-abc","metadata":{"trace":"zzz"},
            "seed":null,"stream_options":{"include_usage":true},"n":1
        });
        assert_eq!(
            ResponseCache::make_key(&base),
            ResponseCache::make_key(&with_noise),
            "细微元数据差异不应导致缓存 miss"
        );
    }

    #[test]
    fn make_key_distinguishes_substantive_fields() {
        let a = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"temperature":0.7});
        let b = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"temperature":0.9});
        assert_ne!(ResponseCache::make_key(&a), ResponseCache::make_key(&b));
    }

    /// 双口径命中率: 本轮累计 vs 本次开启以来 (重新开启清零 enabled 口径).
    #[test]
    fn session_vs_enabled_hit_rates() {
        let c = ResponseCache::new(true, Duration::from_secs(60), 100);
        // 命中 2 次, 未命中 1 次 → 本轮 66.7%, 开启后 66.7%
        let key = "k";
        c.put(key, "b1");
        assert_eq!(c.get(key), Some("b1".to_string()));
        c.put(key, "b2");
        assert_eq!(c.get(key), Some("b2".to_string()));
        assert!(c.get("missing").is_none());
        let s = c.stats();
        assert_eq!(s.hits, 2);
        assert_eq!(s.misses, 1);
        assert_eq!(s.enabled_hits, 2);
        assert_eq!(s.enabled_misses, 1);

        // 关闭再开启 → enabled 口径清零; 关闭期间 get() 不计数 (未启用不算缓存请求)
        c.set_enabled(false);
        assert!(c.get("missing").is_none()); // 关闭时不计
        c.set_enabled(true);
        assert!(c.get("missing").is_none()); // 开启后计入 enabled 口径
        let s = c.stats();
        assert_eq!(s.hits, 2, "本轮命中累计应保留");
        assert_eq!(s.misses, 2, "本轮未命中累计应保留 (开启后 1 次)");
        assert_eq!(s.enabled_hits, 0, "开启后命中口径应清零");
        assert_eq!(s.enabled_misses, 1, "开启后未命中从本次开启起算");
    }

    /// 省量统计: record_hit_saved 双口径累计, 重新开启清零 enabled 口径.
    #[test]
    fn saved_tokens_dual_tracking() {
        let c = ResponseCache::new(true, Duration::from_secs(60), 100);
        c.record_hit_saved(500);
        c.record_hit_saved(1500);
        let s = c.stats();
        assert_eq!(s.saved_tokens, 2000);
        assert_eq!(s.enabled_saved_tokens, 2000);

        c.set_enabled(false);
        c.set_enabled(true); // 重新开启 → enabled 口径清零, 本轮保留
        let s = c.stats();
        assert_eq!(s.saved_tokens, 2000, "本轮省量应保留");
        assert_eq!(s.enabled_saved_tokens, 0, "开启后省量应清零");

        c.record_hit_saved(300);
        let s = c.stats();
        assert_eq!(s.saved_tokens, 2300);
        assert_eq!(s.enabled_saved_tokens, 300);

        c.clear(); // 清空全部清零
        let s = c.stats();
        assert_eq!(s.saved_tokens, 0);
        assert_eq!(s.enabled_saved_tokens, 0);
    }

    #[test]
    fn make_key_seed_must_participate_in_hash() {
        // seed 直接控制采样确定性: 不同 seed 的请求可能产生不同输出,
        // 必须各自独立缓存, 绝不能跨 seed 命中.
        let a = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"seed":1});
        let b = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"seed":2});
        let no_seed = json!({"model":"x","messages":[{"role":"user","content":"hi"}]});
        let null_seed = json!({"model":"x","messages":[{"role":"user","content":"hi"}],"seed":null});
        assert_ne!(ResponseCache::make_key(&a), ResponseCache::make_key(&b), "seed 不同必须区分");
        assert_ne!(ResponseCache::make_key(&a), ResponseCache::make_key(&no_seed), "seed=1 与无 seed 必须区分");
        // null 与缺省语义等价
        assert_eq!(ResponseCache::make_key(&no_seed), ResponseCache::make_key(&null_seed));
    }
}
