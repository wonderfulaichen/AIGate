//! 日级 rollup 持久化统计 — 解决日志 5000 条滚动窗口封顶导致月级统计失真.
//!
//! 机制: 每条请求日志写入时按「本地日 × 供应商 × 上游模型」实时累加到内存聚合表,
//! 节流/跨天/关机时整体落盘到 `data/daily_stats.jsonl` (一行一天). 文件体量极小
//! (一年约数百 KB), 整体重写即可, 无需追加+压缩.
//!
//! 查询侧 (见 `admin::api_stats`): 日志窗口覆盖不到、且在今天之前的天, 从 rollup
//! 读取并与日志统计合并 — 两个来源按天严格互斥, 不会重复计数.
//!
//! 费用语义: 计费相关 token 按「高峰/空闲」时段拆分存储, 查询期用当前费率重算
//! 费用 — 与日志口径一致 (providers.json 改价可追溯历史), 不预存金额.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::admin::{bucket_start, RequestLog};

/// 单个「供应商 × 上游模型」单日聚合条目.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct RollupEntry {
    #[serde(default)]
    pub provider: String,
    /// 上游模型 (缺失时回退中转 ID, 与统计聚合口径一致).
    #[serde(default)]
    pub upstream: String,
    /// 映射到该上游模型的中转 ID 集合 (去重, 查询期用于价格覆盖匹配与免费判定).
    #[serde(default)]
    pub aliases: Vec<String>,
    // ── 全部请求 (含缓存命中/错误) ──
    #[serde(default)]
    pub requests: u64,
    #[serde(default)]
    pub errors: u64,
    #[serde(default)]
    pub latency_sum_ms: u64,
    #[serde(default)]
    pub body_bytes: u64,
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub completion_tokens: u64,
    #[serde(default)]
    pub cache_hit_tokens: u64,
    #[serde(default)]
    pub cache_miss_tokens: u64,
    /// 纯生成速度样本: Σ输出 token 与 Σ(总耗时−首token延迟).
    #[serde(default)]
    pub gen_output_tokens: u64,
    #[serde(default)]
    pub gen_time_sum_ms: u64,
    /// 参与生成速度计算的请求数 (样本量展示用).
    #[serde(default)]
    pub gen_samples: u64,
    // ── 优化省量 (全部请求) ──
    #[serde(default)]
    pub strip_saved_tokens: u64,
    #[serde(default)]
    pub trim_saved_tokens: u64,
    #[serde(default)]
    pub resp_cache_saved_tokens: u64,
    /// 省量中落在高峰时段的部分 (费用折算用; 空闲 = 三者之和 − 此值).
    #[serde(default)]
    pub saved_peak_tokens: u64,
    // ── 计费拆分 (仅非缓存请求; 高峰/空闲分开累计, 查询期按最新费率重算) ──
    #[serde(default)]
    pub bill_prompt_peak: u64,
    #[serde(default)]
    pub bill_completion_peak: u64,
    #[serde(default)]
    pub bill_hit_peak: u64,
    /// KV 缓存首次写入 token (净省计算用, 仅非缓存请求).
    #[serde(default)]
    pub bill_creation_peak: u64,
    #[serde(default)]
    pub bill_prompt_offpeak: u64,
    #[serde(default)]
    pub bill_completion_offpeak: u64,
    #[serde(default)]
    pub bill_hit_offpeak: u64,
    #[serde(default)]
    pub bill_creation_offpeak: u64,
}

impl RollupEntry {
    /// 累加单条请求日志 (字段口径与 compute_stats 一一对应).
    fn add_log(&mut self, log: &RequestLog) {
        self.requests += 1;
        if log.status >= 400 || log.error.is_some() {
            self.errors += 1;
        }
        self.latency_sum_ms += log.latency_ms;
        self.body_bytes += log.body_len as u64;
        self.prompt_tokens += log.prompt_tokens as u64;
        self.completion_tokens += log.completion_tokens as u64;
        self.cache_hit_tokens += log.prompt_cache_hit_tokens as u64;
        self.cache_miss_tokens += log.prompt_cache_miss_tokens as u64;
        if let Some(ft) = log.first_token_ms {
            if ft < log.latency_ms && log.completion_tokens > 0 {
                self.gen_output_tokens += log.completion_tokens as u64;
                self.gen_time_sum_ms += log.latency_ms - ft;
                self.gen_samples += 1;
            }
        }
        let peak = crate::pricing::is_peak(log.timestamp);
        let saved = log.strip_saved_tokens as u64
            + log.trim_saved_tokens as u64
            + log.resp_cache_saved_tokens as u64;
        self.strip_saved_tokens += log.strip_saved_tokens as u64;
        self.trim_saved_tokens += log.trim_saved_tokens as u64;
        self.resp_cache_saved_tokens += log.resp_cache_saved_tokens as u64;
        if peak {
            self.saved_peak_tokens += saved;
        }
        // 计费拆分: 命中本地缓存的请求未真实消费上游 token, 不计费.
        if !log.cached {
            let prompt = log.prompt_tokens as u64;
            let hit = (log.prompt_cache_hit_tokens as u64).min(prompt);
            let creation = (log.prompt_cache_creation_tokens as u64).min(prompt.saturating_sub(hit));
            if peak {
                self.bill_prompt_peak += prompt;
                self.bill_completion_peak += log.completion_tokens as u64;
                self.bill_hit_peak += hit;
                self.bill_creation_peak += creation;
            } else {
                self.bill_prompt_offpeak += prompt;
                self.bill_completion_offpeak += log.completion_tokens as u64;
                self.bill_hit_offpeak += hit;
                self.bill_creation_offpeak += creation;
            }
        }
        if !self.aliases.contains(&log.model) {
            self.aliases.push(log.model.clone());
        }
    }
}

/// 单日聚合.
#[derive(Debug, Clone, Default)]
pub struct DailyRollup {
    /// 本地日 0 点时间戳 (秒).
    pub day_start: u64,
    pub entries: Vec<RollupEntry>,
}

impl DailyRollup {
    /// 累加一条日志到对应条目 (每日条目数有限, 线性查找即可).
    fn record(&mut self, log: &RequestLog, upstream: &str) {
        if let Some(i) = self
            .entries
            .iter()
            .position(|e| e.provider == log.provider && e.upstream == upstream)
        {
            self.entries[i].add_log(log);
        } else {
            let mut e = RollupEntry {
                provider: log.provider.clone(),
                upstream: upstream.to_string(),
                ..Default::default()
            };
            e.add_log(log);
            self.entries.push(e);
        }
    }
}

/// std::sync::Mutex 毒化容错: 一次 panic 后锁被标记 poisoned, 直接 unwrap 会连环 panic
/// 把整个统计接口打挂; 吞掉毒化状态取内值, 保证后续请求可恢复.
fn lock_days<'a>(m: &'a Mutex<BTreeMap<u64, DailyRollup>>) -> std::sync::MutexGuard<'a, BTreeMap<u64, DailyRollup>> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 落盘行格式 (一行一天).
#[derive(serde::Serialize, serde::Deserialize)]
struct RollupDayLine {
    d: u64,
    e: Vec<RollupEntry>,
}

/// 日级 rollup 账本 — 线程共享句柄, push 路径 O(条目数) 累加, 整体落盘.
pub struct RollupBook {
    days: Mutex<BTreeMap<u64, DailyRollup>>,
    dirty: AtomicBool,
    /// 上次落盘的 Unix 毫秒, 用于节流 (频繁重写浪费 IO).
    last_flush_ms: AtomicU64,
    file_path: PathBuf,
}

/// 落盘最小间隔 (毫秒): push 路径仅在距上次落盘超过该值时触发异步重写.
const FLUSH_THROTTLE_MS: u64 = 15_000;

impl RollupBook {
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        Self {
            days: Mutex::new(BTreeMap::new()),
            dirty: AtomicBool::new(false),
            last_flush_ms: AtomicU64::new(0),
            file_path: dir.join("daily_stats.jsonl"),
        }
    }

    /// 启动时从文件加载 (只调用一次).
    pub fn load_from_file(&self) {
        let Ok(content) = std::fs::read_to_string(&self.file_path) else {
            return;
        };
        let mut days = self.days.lock().unwrap_or_else(|e| e.into_inner());
        for line in content.lines() {
            if let Ok(l) = serde_json::from_str::<RollupDayLine>(line) {
                days.insert(l.d, DailyRollup {
                    day_start: l.d,
                    entries: l.e,
                });
            }
        }
    }

    /// 累加一条请求日志 (push 路径调用, 持锁极短).
    pub fn record(&self, log: &RequestLog) {
        let day = bucket_start(log.timestamp, "day");
        let upstream = log
            .upstream_model
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| log.model.clone());
        let mut days = self.days.lock().unwrap();
        let entry = days.entry(day).or_default();
        // 关键: or_default() 产生的 DailyRollup.day_start 是 0, 必须回填为 map 的 key,
        // 否则 serialize() 写出的 `d` 恒为 0 —— 落盘后所有历史天塌缩成 1970-01-01,
        // 重启加载后按天检索/长跨度统计全部失真 (实测部署实例的 daily_stats.jsonl
        // 三行 d 全为 0 即此因).
        entry.day_start = day;
        entry.record(log, &upstream);
        self.dirty.store(true, Ordering::Release);
    }

    /// 节流判定: 距上次落盘超过阈值则占用本次配额并返回 true (调用方负责实际落盘).
    pub fn should_flush(&self) -> bool {
        if !self.dirty.load(Ordering::Acquire) {
            return false;
        }
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        let last = self.last_flush_ms.load(Ordering::Acquire);
        if now_ms.saturating_sub(last) > FLUSH_THROTTLE_MS
            && self
                .last_flush_ms
                .compare_exchange(last, now_ms, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
        {
            return true;
        }
        false
    }

    /// 启动回填: 日志缓冲区完整覆盖的天 (> 边界天) 以日志为准重建, 修正崩溃丢失的增量;
    /// 边界天 (最早日志所在天, 缓冲区只有其尾部) 仅在账本为空 (首次启用) 时尽力回填,
    /// 否则保留账本中的完整数据. 只在启动时调用一次, 之后日志与 rollup 按天天然互斥.
    pub fn backfill_from_logs(&self, logs: &[RequestLog]) {
        let Some(cover_ts) = logs.iter().map(|l| l.timestamp).min() else {
            return;
        };
        let cover_day = bucket_start(cover_ts, "day");
        let book_was_empty = self.is_empty();
        let mut rebuilt: BTreeMap<u64, DailyRollup> = BTreeMap::new();
        for log in logs {
            let day = bucket_start(log.timestamp, "day");
            if day == cover_day && !book_was_empty {
                continue;
            }
            let upstream = log
                .upstream_model
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| log.model.clone());
            rebuilt.entry(day).or_default().record(log, &upstream);
        }
        let mut days = self.days.lock().unwrap_or_else(|e| e.into_inner());
        for (day, rollup) in rebuilt {
            days.insert(day, rollup);
        }
        drop(days);
        self.dirty.store(true, Ordering::Release);
    }

    /// 取 [start, end] 范围内 (按 day_start) 的天, 按时间升序.
    /// 空范围 (start > end, 如查询范围起点晚于账本最早天) 返回空, 绝不 panic.
    pub fn days_between(&self, start: u64, end: u64) -> Vec<DailyRollup> {
        if start > end {
            return Vec::new();
        }
        let days = lock_days(&self.days);
        days.range(start..=end).map(|(_, d)| d.clone()).collect()
    }

    pub fn is_empty(&self) -> bool {
        self.days.lock().unwrap_or_else(|e| e.into_inner()).is_empty()
    }

    fn serialize(&self) -> String {
        let days = self.days.lock().unwrap_or_else(|e| e.into_inner());
        let mut out = String::with_capacity(4096);
        // 用 BTreeMap 的 key 作为权威日期, 而非结构体字段 —— 字段可能因历史 bug 为 0.
        for (day_key, d) in days.iter() {
            let effective_day = if d.day_start == 0 { *day_key } else { d.day_start };
            let line = RollupDayLine {
                d: effective_day,
                e: d.entries.clone(),
            };
            if let Ok(s) = serde_json::to_string(&line) {
                out.push_str(&s);
                out.push('\n');
            }
        }
        out
    }

    /// 整体重写落盘文件 (异步). 先写临时文件再 rename, 防止写盘中断损坏已有数据.
    pub async fn flush(&self) {
        let out = self.serialize();
        let tmp = self.file_path.with_extension("jsonl.tmp");
        if tokio::fs::write(&tmp, out.as_bytes()).await.is_ok() {
            let _ = tokio::fs::rename(&tmp, &self.file_path).await;
        }
        self.dirty.store(false, Ordering::Release);
    }

    /// 同步落盘 (关机等非 async 上下文).
    pub fn flush_blocking(&self) {
        let out = self.serialize();
        let tmp = self.file_path.with_extension("jsonl.tmp");
        if std::fs::write(&tmp, out.as_bytes()).is_ok() {
            let _ = std::fs::rename(&tmp, &self.file_path);
        }
        self.dirty.store(false, Ordering::Release);
    }

    /// 清空 (与清空日志联动, 保持统计口径一致).
    pub async fn clear(&self) {
        self.days.lock().unwrap_or_else(|e| e.into_inner()).clear();
        self.dirty.store(false, Ordering::Release);
        let _ = tokio::fs::remove_file(&self.file_path).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造一条最小可用的请求日志 (仅 record 路径需要的字段有意义).
    fn mk_log(ts: u64, provider: &str, upstream: &str) -> crate::admin::RequestLog {
        crate::admin::RequestLog {
            timestamp: ts,
            model: upstream.to_string(),
            provider: provider.to_string(),
            endpoint: String::new(),
            status: 200,
            latency_ms: 0,
            body_len: 0,
            error: None,
            prompt_tokens: 100,
            completion_tokens: 10,
            cached: false,
            prompt_cache_hit_tokens: 0,
            prompt_cache_miss_tokens: 0,
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            strip_toolcall_saved_tokens: 0,
            audit_exempt_reasoning_tokens: 0,
            audit_dup_block_tokens: 0,
            audit_dup_block_count: 0,
            audit_observed: false,
            first_token_ms: None,
            upstream_model: Some(upstream.to_string()),
        }
    }

    /// 关键回归: 落盘的 `d` (day_start) 必须是真实日界, 不得为 0.
    ///
    /// 历史 bug: `record()` 用 `entry(day).or_default()` 建 DailyRollup, 而
    /// `day_start` 字段保持 Default 的 0, 且从未回填 —— serialize 写出的 `d` 恒为 0,
    /// 落盘后所有历史天塌缩成 1970-01-01, 重启加载后按天检索与长跨度统计全部失真
    /// (实测部署实例 daily_stats.jsonl 三行 d 全为 0). 只有"写入→落盘→加载"全程才暴露.
    #[test]
    fn persisted_day_start_is_real_day_boundary() {
        let dir = "data-test-rollup-day";
        let _ = std::fs::remove_dir_all(dir);
        let book = RollupBook::new(dir);
        let rt = tokio::runtime::Runtime::new().unwrap();

        // 两个不同天的日志
        let day1 = crate::admin::bucket_start(1_789_318_171, "day");
        let day2 = crate::admin::bucket_start(1_789_405_171, "day");
        assert_ne!(day1, day2, "测试前提: 两个时间戳应属不同天");
        book.record(&mk_log(1_789_318_171, "p", "m"));
        book.record(&mk_log(1_789_405_171, "p", "m"));
        rt.block_on(book.flush());

        // 落盘内容: 两行, d 必须是真实日界而非 0
        let content = std::fs::read_to_string(std::path::Path::new(dir).join("daily_stats.jsonl")).unwrap();
        let days: Vec<u64> = content
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str::<RollupDayLine>(l).unwrap().d)
            .collect();
        assert_eq!(days.len(), 2, "应落盘两天");
        assert!(days.contains(&day1) && days.contains(&day2), "d 应为真实日界, 实得 {days:?}");
        assert!(!days.contains(&0), "d 不得为 0 (历史 bug 特征)");

        // 重新加载后按天仍可检索 (而非塌缩成一天)
        let book2 = RollupBook::new(dir);
        book2.load_from_file();
        assert!(book2.days_between(day1, day1).len() == 1, "day1 应可单独检索");
        assert!(book2.days_between(day2, day2).len() == 1, "day2 应可单独检索");
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 回归: 查询范围起点晚于账本最早天 (start > end) 时必须返回空而不是 BTreeMap panic.
    #[test]
    fn days_between_empty_range_returns_empty() {
        let book = RollupBook::new("data-test-rollup");
        // start > end 的空范围
        assert!(book.days_between(2000, 1000).is_empty());
        // 空账本 + 任意合法范围
        assert!(book.days_between(1000, 2000).is_empty());
        // 干净清理, 不留文件
        let _ = std::fs::remove_dir_all("data-test-rollup");
    }
}
