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

use tracing::warn;

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
                // 日界恒为真实本地 0 点 (>= 2020 年), 不可能是 0. `d:0` 是历史 bug 写出的
                // 化石行 (day_start 字段未回填): 它的真实日期已不可考, 若照单全收会以 key=0
                // 长期驻留 —— 既无法被 record() 更新, 又会让「1970-01-01」出现在按天查询里,
                // 并在每次落盘时原样复写. 直接丢弃并告警, 让它随下次落盘彻底消失.
                if l.d == 0 {
                    warn!("rollup: drop malformed day line (d=0, {} entries)", l.e.len());
                    continue;
                }
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
    /// 边界天 (最早日志所在天, 缓冲区只有其尾部) 仅在账本已持有该天时保留账本数据,
    /// 否则同样尽力回填. 只在启动时调用一次, 之后日志与 rollup 按天天然互斥.
    ///
    /// 保留账本的前提是「账本真的有那一天」: 若账本缺该天 (历史 bug / 首次启用 / 被清空),
    /// 跳过就等于整天数据在两侧同时缺席 (日志侧被 merge_end 排除, rollup 侧无条目) ——
    /// 实测部署实例因此整日丢失 09-12 的统计. 缺天时宁可用「只有尾部」的日志补上部分数据.
    pub fn backfill_from_logs(&self, logs: &[RequestLog]) {
        let Some(cover_ts) = logs.iter().map(|l| l.timestamp).min() else {
            return;
        };
        let cover_day = bucket_start(cover_ts, "day");
        let has_cover_day = self
            .days
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(&cover_day);
        let mut rebuilt: BTreeMap<u64, DailyRollup> = BTreeMap::new();
        for log in logs {
            let day = bucket_start(log.timestamp, "day");
            if day == cover_day && has_cover_day {
                continue;
            }
            let upstream = log
                .upstream_model
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| log.model.clone());
            let entry = rebuilt.entry(day).or_default();
            // 与 record() 同理: or_default() 的 day_start 是 0, 必须回填为 map 的 key.
            entry.day_start = day;
            entry.record(log, &upstream);
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

    /// 回归: 载入时必须丢弃 `d:0` 化石行, 且下次落盘不再复写它.
    ///
    /// 历史 bug 写出的 `d:0` 行以 key=0 驻留账本: 既无法被 record() 更新 (真实日界不会等于 0),
    /// 又会让 1970-01-01 出现在按天查询里, 并在每次 flush 时原样复写 —— 永久污染.
    #[test]
    fn load_drops_fossil_day_zero_line() {
        let dir = "data-test-rollup-fossil";
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir).unwrap();
        let day = crate::admin::bucket_start(1_789_318_171, "day");
        let fossil = serde_json::to_string(&RollupDayLine {
            d: 0,
            e: vec![RollupEntry { provider: "p".into(), upstream: "m".into(), requests: 7, ..Default::default() }],
        })
        .unwrap();
        let real = serde_json::to_string(&RollupDayLine {
            d: day,
            e: vec![RollupEntry { provider: "p".into(), upstream: "m".into(), requests: 3, ..Default::default() }],
        })
        .unwrap();
        std::fs::write(std::path::Path::new(dir).join("daily_stats.jsonl"), format!("{fossil}\n{real}\n")).unwrap();

        let book = RollupBook::new(dir);
        book.load_from_file();
        assert!(book.days_between(0, 0).is_empty(), "d=0 化石行必须被丢弃");
        assert_eq!(book.days_between(day, day).len(), 1, "真实天必须保留");

        // 落盘后化石行彻底消失 (不再被复写)
        book.flush_blocking();
        let content = std::fs::read_to_string(std::path::Path::new(dir).join("daily_stats.jsonl")).unwrap();
        let days: Vec<u64> = content
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str::<RollupDayLine>(l).unwrap().d)
            .collect();
        assert_eq!(days, vec![day], "落盘只应剩真实天");
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 回归: 空账本启动时, 边界天 (最早日志所在天) 也必须由 backfill 重建.
    ///
    /// 部署实例曾整日丢失 09-12 的统计 (165 请求 / 2900 万输入 token): 该天恰是日志窗口的
    /// 最早天, backfill 在「账本非空」时跳过它 —— 而账本里又没有它, 于是它既不进日志侧
    /// (被 merge_end 排除) 也不进 rollup 侧, 在统计里彻底隐形. 账本为空 (首次启用/重建)
    /// 时必须把它一并回填.
    #[test]
    fn backfill_on_empty_book_rebuilds_cover_day() {
        let dir = "data-test-rollup-cover";
        let _ = std::fs::remove_dir_all(dir);
        let day1 = crate::admin::bucket_start(1_789_318_171, "day");
        let day2 = crate::admin::bucket_start(1_789_405_171, "day");
        assert_ne!(day1, day2);
        let logs = vec![
            mk_log(1_789_318_171, "p", "m"),
            mk_log(1_789_318_172, "p", "m"),
            mk_log(1_789_405_171, "p", "m"),
        ];

        let book = RollupBook::new(dir);
        assert!(book.days_between(day1, day2).is_empty(), "测试前提: 账本初始为空");
        book.backfill_from_logs(&logs);
        book.flush_blocking();

        let book2 = RollupBook::new(dir);
        book2.load_from_file();
        let d1 = book2.days_between(day1, day1);
        assert_eq!(d1.len(), 1, "边界天 day1 必须被重建");
        assert_eq!(d1[0].day_start, day1, "重建的 day_start 必须是真实日界");
        assert_eq!(d1[0].entries.iter().map(|e| e.requests).sum::<u64>(), 2, "day1 应含 2 条请求");
        assert_eq!(book2.days_between(day2, day2).len(), 1, "day2 亦应重建");
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 回归: 账本非空但**缺**边界天时, 该天仍须由日志回填 (不得静默丢数据).
    ///
    /// 「边界天只有尾部日志, 账本更完整」这一保留前提, 仅在账本确实持有该天时成立.
    /// 账本缺该天时跳过它 = 整天在日志侧 (被 merge_end 排除) 与 rollup 侧同时缺席,
    /// 正是部署实例 09-12 整天消失的成因.
    #[test]
    fn backfill_fills_cover_day_missing_from_book() {
        let dir = "data-test-rollup-cover-missing";
        let _ = std::fs::remove_dir_all(dir);
        let day1 = crate::admin::bucket_start(1_789_318_171, "day");
        let day2 = crate::admin::bucket_start(1_789_405_171, "day");
        assert_ne!(day1, day2);
        let logs = vec![
            mk_log(1_789_318_171, "p", "m"),
            mk_log(1_789_405_171, "p", "m"),
        ];

        // 账本已有「更晚的天」(非空), 但没有边界天 day1
        let book = RollupBook::new(dir);
        book.record(&mk_log(1_789_405_171, "p", "m"));
        book.flush_blocking();
        assert!(book.days_between(day1, day1).is_empty(), "测试前提: 账本缺 day1");

        book.backfill_from_logs(&logs);
        assert_eq!(book.days_between(day1, day1).len(), 1, "缺的边界天必须由日志回填");
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 回归: 账本已持有边界天时必须保留其数据 (不被「只有尾部」的日志覆盖).
    ///
    /// 边界天的日志窗口只含该天尾部, 账本才是全天累计 —— 这是保留规则的本意, 不能被
    /// 「缺天则回填」的新逻辑破坏.
    #[test]
    fn backfill_preserves_cover_day_when_book_has_it() {
        let dir = "data-test-rollup-cover-keep";
        let _ = std::fs::remove_dir_all(dir);
        let day1 = crate::admin::bucket_start(1_789_318_171, "day");
        let book = RollupBook::new(dir);
        // 账本里 day1 有 5 条 (全天累计), 日志里 day1 只剩 1 条 (窗口尾部)
        for i in 0..5 {
            book.record(&mk_log(1_789_318_171 + i, "p", "m"));
        }
        book.flush_blocking();

        book.backfill_from_logs(&[mk_log(1_789_318_171, "p", "m")]);
        let d1 = book.days_between(day1, day1);
        assert_eq!(d1.len(), 1);
        assert_eq!(
            d1[0].entries.iter().map(|e| e.requests).sum::<u64>(),
            5,
            "账本持有的边界天不得被日志尾部覆盖"
        );
        let _ = std::fs::remove_dir_all(dir);
    }
}
