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
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

/// 单条缓存项.
struct Entry {
    body: String,
    /// 写入时回填的精确 token 用量 (prompt_tokens, completion_tokens); 命中回放优先用此值
    /// 而非从缓存体重新解析 (非流式 JSON 自带 usage, 精确; 回放时若缺失则退回 extract_usage 兜底).
    /// (0, 0) 表示无精确值.
    usage: (u32, u32),
    expires_at: Instant,
}

/// 持久化结构: 落盘为 `{ "entries": [ {key, body, ttl_left, usage} ] }`.
/// `ttl_left` = 剩余有效期秒数 (重启后据此恢复 expires_at); 不含 `Instant` (不可序列化).
#[derive(Serialize, Deserialize)]
struct PersistEntry {
    key: String,
    body: String,
    ttl_left: u64,
    /// 录制时回填的精确 token 用量 (prompt, completion); 落盘以便重启后命中回放仍用精确值.
    #[serde(default)]
    usage: (u32, u32),
}

#[derive(Serialize, Deserialize)]
struct PersistPayload {
    entries: Vec<PersistEntry>,
    /// 开关状态一并落盘: 用户开启缓存后即使重启进程也不丢失 (旧格式文件缺此字段 → 默认 false, 由 serde default 兼容).
    #[serde(default)]
    enabled: bool,
}

/// 并发去重 (in-flight coalescing) 运行态.
///
/// 同一时刻字节级相同的非流式请求, 只打一次上游, 其余请求等待领导者完成后
/// 直接读响应缓存返回. 完全无损 —— 与响应缓存共用同一缓存键.
///
/// 实现: 以缓存键为索引, 记录正在进行的请求对应的 [`tokio::sync::Notify`]. 领导者注册,
/// 等待者克隆该 Notify 并 await; 领导者完成 (成功/失败/提前返回) 时由 [`InflightGuard`]
/// 的 Drop 移除条目并 `notify_waiters`, 唤醒所有等待者. 等待者被唤醒后重新查缓存,
/// 命中即得响应, 未命中 (领导者异常) 则降级为本请求独立上游调用.
pub struct InflightCache {
    map: Mutex<std::collections::HashMap<String, Arc<tokio::sync::Notify>>>,
}

impl InflightCache {
    pub fn new() -> Self {
        Self {
            map: Mutex::new(std::collections::HashMap::new()),
        }
    }

    /// 尝试加入相同键的"正在进行"请求.
    /// - 返回 `Some(notify)`: 本请求是等待者, 应 `notify.notified().await` 后查缓存.
    /// - 返回 `None`: 本请求是领导者, 需在完成后由 [`InflightGuard`] 释放.
    pub fn join_or_claim(&self, key: &str) -> Option<Arc<tokio::sync::Notify>> {
        let mut g = self.map.lock().expect("inflight lock poisoned");
        if let Some(n) = g.get(key) {
            return Some(n.clone());
        }
        g.insert(key.to_string(), Arc::new(tokio::sync::Notify::new()));
        None
    }

    /// 领导者完成: 移除条目并唤醒所有等待者. 幂等, 重复调用安全.
    pub fn release(&self, key: &str) {
        if let Ok(mut g) = self.map.lock() {
            if let Some(n) = g.remove(key) {
                n.notify_waiters();
            }
        }
    }
}

impl Default for InflightCache {
    fn default() -> Self {
        Self::new()
    }
}

/// 领导者析构守卫: 函数正常返回或任何提前返回/panic 时, 析构都会释放 in-flight 条目,
/// 确保等待者不会因领导者异常而永久挂起 (避免请求堆积/连接耗尽).
pub struct InflightGuard<'a> {
    cache: &'a InflightCache,
    key: String,
}

impl<'a> InflightGuard<'a> {
    pub fn new(cache: &'a InflightCache, key: String) -> Self {
        Self { cache, key }
    }
}

impl<'a> Drop for InflightGuard<'a> {
    fn drop(&mut self) {
        self.cache.release(&self.key);
    }
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
    /// TTL 受 Mutex 保护: 支持面板运行时调整 (set_ttl), 仅影响后续写入条目.
    ttl: Mutex<Duration>,
    /// 条目上限受 Mutex 保护: 支持面板运行时调整 (set_max_entries).
    max_entries: Mutex<usize>,
    /// 持久化落盘路径 (None = 不落盘). 配置后启动加载历史缓存, 正常退出时 flush_blocking.
    persist_path: Option<PathBuf>,
    /// 开关状态 flag 文件路径 (始终持久化, 与 persist_path 解耦):
    /// 用户面板开启缓存后即使进程重启也不应复位为关闭. 默认落在 data/cache_enabled.flag.
    enabled_flag: PathBuf,
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
    pub fn new(
        enabled: bool,
        ttl: Duration,
        max_entries: usize,
        persist_path: Option<PathBuf>,
    ) -> Self {
        // 开关状态 flag 文件路径: 优先复用 persist_path 的父目录, 否则落在 data/ 下.
        // 该 flag 始终持久化 (与 persist_path 解耦), 确保用户开启后重启不复位.
        let enabled_flag = match &persist_path {
            Some(p) => p
                .parent()
                .map(|d| d.join("cache_enabled.flag"))
                .unwrap_or_else(|| PathBuf::from("data/cache_enabled.flag")),
            None => PathBuf::from("data/cache_enabled.flag"),
        };
        // flag 文件若存在, 以其为准覆盖环境变量/默认值 (用户上次面板设置优先).
        let enabled = if enabled_flag.exists() {
            std::fs::read_to_string(&enabled_flag)
                .map(|s| s.trim() == "1")
                .unwrap_or(enabled)
        } else {
            enabled
        };
        let c = Self {
            enabled: AtomicBool::new(enabled),
            ttl: Mutex::new(ttl),
            max_entries: Mutex::new(max_entries.max(1)),
            persist_path: persist_path.clone(),
            enabled_flag,
            map: Mutex::new(std::collections::HashMap::new()),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
            enabled_hits: AtomicU64::new(0),
            enabled_misses: AtomicU64::new(0),
            saved_tokens: AtomicU64::new(0),
            enabled_saved_tokens: AtomicU64::new(0),
        };
        // 持久化: 配置了落盘路径则启动加载历史缓存 (过滤 TTL 过期) → 冷启动预热.
        if let Some(p) = persist_path {
            c.load_from(&p);
        }
        c
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
        // 开关状态持久化: 用户面板开启后即使进程重启也不复位.
        // 写失败静默忽略 (flag 非关键路径, 仅影响重启后是否自动恢复开启).
        let content = if on { "1" } else { "0" };
        if let Some(parent) = self.enabled_flag.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(&self.enabled_flag, content);
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled.load(Ordering::SeqCst)
    }

    /// 运行时调整 TTL (面板配置项). 仅影响后续写入条目的有效期, 已存在的条目不变.
    pub fn set_ttl(&self, ttl_secs: u64) {
        let mut g = self.ttl.lock().expect("ttl lock poisoned");
        *g = Duration::from_secs(ttl_secs.max(1));
    }

    /// 运行时调整条目上限 (面板配置项). 若新上限小于当前条目数, 下次写入/查询时自然淘汰.
    pub fn set_max_entries(&self, max_entries: usize) {
        let mut g = self.max_entries.lock().expect("max_entries lock poisoned");
        *g = max_entries.max(1);
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

    /// 由已注入参数的请求体计算缓存键.
    ///
    /// 仅对非流式请求调用 (proxy 层已限制: 流式编程流量滚动上下文命中率恒为 0, 不参与缓存).
    /// 规范化: 去掉不影响上游输出的字段, 让"实质相同请求"跨细微元数据差异也能命中缓存.
    /// IDE/中间件常透传随机 user/id/metadata/stream_options, 若不剔除会导致缓存 miss.
    /// 采用黑名单保留式: 仅移除已知无关字段, 新增的合理字段默认参与哈希, 避免误伤.
    pub fn make_key(body: &Value) -> Option<String> {
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

    /// 查询缓存; 命中返回 `(响应体字符串, 录制时精确 token 用量)` (并计数 hits),
    /// 未命中/过期返回 None (计数 misses). 本轮与"本次开启以来"两组计数同步累加.
    pub fn get(&self, key: &str) -> Option<(String, (u32, u32))> {
        if !self.is_enabled() {
            return None;
        }
        let mut map = self.map.lock().expect("cache lock poisoned");
        match map.get(key) {
            Some(e) if e.expires_at > Instant::now() => {
                self.hits.fetch_add(1, Ordering::SeqCst);
                self.enabled_hits.fetch_add(1, Ordering::SeqCst);
                Some((e.body.clone(), e.usage))
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

    /// 写入缓存 (JSON 字符串, 非流式命中用); 超上限时先清过期项, 仍满则淘汰最早的一半.
    /// `usage` = 响应体 usage 解析出的 (prompt, completion); 命中回放优先用此精确值.
    pub fn put(&self, key: &str, body: &str, usage: (u32, u32)) {
        if !self.is_enabled() {
            return;
        }
        self.insert(key.to_string(), body.to_string(), usage);
    }

    /// 内部写入: 执行上限/TTL 淘汰后插入条目. `put` 调用.
    fn insert(&self, key: String, body: String, usage: (u32, u32)) {
        let mut map = self.map.lock().expect("cache lock poisoned");
        let max_entries = *self.max_entries.lock().expect("max_entries lock poisoned");
        if map.len() >= max_entries {
            let now = Instant::now();
            map.retain(|_, e| e.expires_at > now);
            if map.len() >= max_entries {
                let drop = map.len() / 2;
                let keys: Vec<String> = map.keys().take(drop).cloned().collect();
                for k in keys {
                    map.remove(&k);
                }
            }
        }
        map.insert(
            key,
            Entry {
                body,
                usage,
                expires_at: Instant::now() + *self.ttl.lock().expect("ttl lock poisoned"),
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
            ttl_secs: self.ttl.lock().expect("ttl lock poisoned").as_secs(),
            max_entries: *self.max_entries.lock().expect("max_entries lock poisoned"),
            entries: live,
            hits: self.hits.load(Ordering::SeqCst),
            misses: self.misses.load(Ordering::SeqCst),
            enabled_hits: self.enabled_hits.load(Ordering::SeqCst),
            enabled_misses: self.enabled_misses.load(Ordering::SeqCst),
            saved_tokens: self.saved_tokens.load(Ordering::SeqCst),
            enabled_saved_tokens: self.enabled_saved_tokens.load(Ordering::SeqCst),
        }
    }

    /// 同步落盘当前缓存 (仅未过期条目) — 原子写: 先写 .tmp 再 rename, 避免半写文件.
    /// 正常退出时调用; 异常 kill 不落盘 (缓存非权威数据, 可接受). 未配置 persist_path 则空操作.
    pub fn flush_blocking(&self) {
        let Some(path) = &self.persist_path else {
            return;
        };
        let entries: Vec<PersistEntry> = {
            let map = self.map.lock().expect("cache lock poisoned");
            let now = Instant::now();
            map.iter()
                .filter(|(_, e)| e.expires_at > now)
                .map(|(k, e)| PersistEntry {
                    key: k.clone(),
                    body: e.body.clone(),
                    ttl_left: (e.expires_at - now).as_secs(),
                    usage: e.usage,
                })
                .collect()
        };
        if let Ok(s) = serde_json::to_string(&PersistPayload {
            entries,
            enabled: self.is_enabled(),
        }) {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let tmp = path.with_extension("tmp");
            if std::fs::write(&tmp, &s).is_ok() {
                let _ = std::fs::rename(&tmp, path);
            }
        }
    }

    /// 启动加载历史缓存: 读盘并过滤 TTL 过期项, 未过期者插入内存 → 冷启动预热.
    /// 读取/解析失败静默忽略 (缺失或旧格式文件不影响启动).
    fn load_from(&self, path: &Path) {
        let Ok(s) = std::fs::read_to_string(path) else {
            return;
        };
        let Ok(payload) = serde_json::from_str::<PersistPayload>(&s) else {
            return;
        };
        // 开关状态随缓存文件恢复: 用户开启缓存后即使进程重启也不应复位为关闭.
        // (仅当文件确实携带 enabled=true 时覆盖; 旧格式无此字段 → default=false 不强行开启.)
        if payload.enabled {
            self.set_enabled(true);
        }
        let now = Instant::now();
        let mut map = self.map.lock().expect("cache lock poisoned");
        for e in payload.entries {
            if e.ttl_left > 0 {
                map.insert(
                    e.key,
                    Entry {
                        body: e.body,
                        usage: e.usage,
                        expires_at: now + Duration::from_secs(e.ttl_left),
                    },
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

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
        let c = ResponseCache::new(true, Duration::from_secs(60), 100, None);
        // 命中 2 次, 未命中 1 次 → 本轮 66.7%, 开启后 66.7%
        let key = "k";
        c.put(key, "b1", (10, 20));
        assert_eq!(c.get(key), Some(("b1".to_string(), (10, 20))));
        c.put(key, "b2", (30, 40));
        assert_eq!(c.get(key), Some(("b2".to_string(), (30, 40))));
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
        let c = ResponseCache::new(true, Duration::from_secs(60), 100, None);
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
