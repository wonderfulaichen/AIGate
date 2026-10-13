//! 事件台账 —— 把「出问题得用户自己一点点找」变成「一眼看到发生了什么」(2026-10-11).
//!
//! <h3>为什么要有这个模块</h3>
//!
//! 用户原话:「我感觉我们软件设计得不够人性化。比如熔断、报错等等连工作报告、异常通知都没做。
//! 出问题得用户自己去一点点找。」
//!
//! 这句话的根因是**观测模型只有 pull、没有 push**:
//! - 面板有的是**当前快照** —— 供应商页显示熔断处于 `closed/open/half-open`,
//!   但没有任何地方记着「它**什么时候**断过、**断了几次**、**为什么**断」;
//! - 更糟的是**熔断开合本身完全不留痕** —— `circuit_breaker.rs` 全文零 `tracing` 宏,
//!   `open()` / `close()` 只改状态位。刷新一下面板, 断过的痕迹就没了;
//! - 于是排查只能靠"用户自己去日志里翻", 而他并不知道该翻什么关键词。
//!
//! 本模块提供的是**带时间的事件台账**: 关键事件发生时追加一条, 落盘、跨重启保留,
//! 面板按时间倒序列出。这与请求日志的分工是:
//! - `logs.jsonl` = **每一次请求**的结果(高频、逐条、有 token 明细);
//! - `events.jsonl` = **系统状态的变化**(低频、有原因、有人话)。
//!
//! <h3>为什么只记关键事件, 不记每条错误</h3>
//!
//! 用户把这个选择明确成「只报关键事件」。技术上的理由同样充分: 编程工具的正常取消
//! (499) 每天几百条, 逐条记会让台账变成噪音, 真正的故障反而被埋掉 ——
//! 这正是"记录页有错误筛选但没人去看"的原因。故只收**状态变化类**事件。

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

/// 事件类别. `as_str` 的取值会落进 `events.jsonl` 并出现在面板 i18n key 里
/// (`event_<kind>`), **改动等于改数据格式** —— 只增不改.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    /// 熔断打开: 该供应商的请求开始被快速挡下 (附触发判据).
    BreakerOpen,
    /// 熔断恢复: 探测成功、重新放行 (附断了多久).
    BreakerClosed,
    /// 熔断进入半开: 冷却结束, 放行一次探测.
    BreakerHalfOpen,
    /// 熔断被**手动**重置 (面板按钮).
    BreakerReset,
    /// 上游连续失败达阈值 —— **熔断打开之前**的预警, 让用户先知道"这个供应商不稳".
    UpstreamFailing,
    /// 上游恢复正常 (连续失败后首次成功).
    UpstreamRecovered,
    /// 额度即将耗尽 (积分/余额低于阈值).
    QuotaLow,
    /// 账号失效 (401/403 被判 dead, 停止续期).
    AccountDead,
    /// 配置热重载 (用户改了 providers.json).
    ProvidersReloaded,
    /// **上游在生成中途断开连接**（2026-10-13）。
    ///
    /// 为什么单列一类: 它与 `UpstreamFailing`（连续失败→熔断路径）不同 ——
    /// 那是"打不通/一直失败"，这是"**打通过、正在出字，然后被中途掐断**"
    /// （实测形态：`UnexpectedEof: unexpected EOF during chunk size line`，
    /// 首字已出但流被截断）。前者熔断能兜住，后者熔断兜不住（它算一次 502 失败，
    /// 但单次不成阈值）—— 用户侧看到的是"回答答一半就没了"，而面板此前只有一条 502。
    /// 只对**上游断流**报，不对"真空闲超时"报（后者常是本机/上游慢，不是供应商故障）。
    UpstreamCut,
    /// 进程启动 —— 台账的"分界线", 便于分辨"这条是重启前还是重启后".
    Started,
    /// **端口被占用, 已顺延到别的端口**（2026-10-11）。
    ///
    /// 为什么单独一类而不是复用 `Started`: 事件台账会**合并**同 `(kind, subject)`
    /// 60 秒内的重复。端口变更与"服务已启动"都是 `subject=""`, 合并后只会留下最后一条
    /// （实测把变更详情吞掉了）。语义上也确实不同：那条说"起来了", 这条说"**换了地址**"。
    PortFallback,
}

impl EventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EventKind::BreakerOpen => "breaker_open",
            EventKind::BreakerClosed => "breaker_closed",
            EventKind::BreakerHalfOpen => "breaker_half_open",
            EventKind::BreakerReset => "breaker_reset",
            EventKind::UpstreamFailing => "upstream_failing",
            EventKind::UpstreamRecovered => "upstream_recovered",
            EventKind::UpstreamCut => "upstream_cut",
            EventKind::QuotaLow => "quota_low",
            EventKind::AccountDead => "account_dead",
            EventKind::ProvidersReloaded => "providers_reloaded",
            EventKind::Started => "started",
            EventKind::PortFallback => "port_fallback",
        }
    }

    /// 严重程度 —— 决定面板上的配色。**刻意只有三档**, 避免"什么都是红的"
    /// (那等于没有分级)。
    ///
    /// - `ok`: 恢复正常类;
    /// - `warn`: 需要注意 (预警、额度低);
    /// - `err`: 已经不可用 (熔断、账号作废).
    pub fn severity(self) -> &'static str {
        match self {
            EventKind::BreakerClosed | EventKind::UpstreamRecovered | EventKind::Started => "ok",
            EventKind::BreakerHalfOpen
            | EventKind::UpstreamFailing
            | EventKind::UpstreamCut
            | EventKind::QuotaLow
            | EventKind::ProvidersReloaded
            // 端口顺延是"提醒"级: 一切可用, 只是地址变了 —— 用户该知道, 但不必紧张。
            | EventKind::PortFallback => "warn",
            EventKind::BreakerOpen | EventKind::BreakerReset | EventKind::AccountDead => "err",
        }
    }

    /// 是否点亮**托盘红点** (通知模块, 2026-10-12): 任何未读的错误/警告。
    ///
    /// 与 `severity` 的差异是刻意的:
    ///   · `BreakerReset` / `ProvidersReloaded` 是**用户自己刚做的操作** ——
    ///     通知自己刚做的事是噪音, 不点红点;
    ///   · `BreakerHalfOpen` 是恢复过程的中间态, 随后必有 Closed(恢复) 或重新 Open ——
    ///     单独亮红点会制造"到底好了没"的困惑。
    pub fn dot(self) -> bool {
        matches!(
            self,
            EventKind::BreakerOpen
                | EventKind::AccountDead
                | EventKind::UpstreamFailing
                // 上游中途断流 = 供应商不稳的直接证据, 该点亮红点让用户看见。
                | EventKind::UpstreamCut
                | EventKind::QuotaLow
                | EventKind::PortFallback
        )
    }

    /// 是否**弹气球** (用户 2026-10-12 拍板): 错误级(系统成因) + 额度将尽。
    ///
    /// 额度将尽虽是 warn 级, 但用户明确要求弹 ——「不然我无法及时切换模型」。
    /// `UpstreamFailing` (预警) 不弹: 它随后大概率升级成 BreakerOpen(弹),
    /// 预警就弹会把"可能要坏"当"已经坏了"报。
    pub fn balloon(self) -> bool {
        matches!(
            self,
            EventKind::BreakerOpen | EventKind::AccountDead | EventKind::QuotaLow
        )
    }
}

/// 一条事件.
///
/// ⚠️ **每个字段都要 `#[serde(default)]` 或本身可缺省**: `events.jsonl` 是**追加式**的,
/// 升级后新旧行混在同一文件里, 少一个 default 会让历史行整行解析失败 ——
/// 表现是"事件凭空消失"(本项目在 rollup / RequestLog 加字段时都踩过)。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    /// 事件时刻 (Unix 秒). 显示层按本地时区格式化.
    pub ts: u64,
    pub kind: EventKind,
    /// 主体: 供应商名 / 账号指纹 / 模型名 —— 面板据此聚合与筛选. 无主体时为空串.
    #[serde(default)]
    pub subject: String,
    /// **人话**说明: 为什么发生、后果是什么、该怎么办.
    ///
    /// 为什么不让前端按 kind 拼: 同一类事件的原因不同 (熔断可能是"连续失败 5 次"
    /// 也可能是"错误率 40%"), 只在后端能拿到精确值。前端拼就只能写笼统文案。
    #[serde(default)]
    pub detail: String,
    /// 同一 `(kind, subject)` 的**重复次数**. 相同事件在时间窗内合并成一 条,
    /// 只把次数加上去 —— 否则一个持续故障的供应商会把整个台账刷满
    /// (实测该类刷屏正是"用户不再看台账"的直接原因)。
    #[serde(default = "one")]
    pub count: u64,
}

fn one() -> u64 {
    1
}

/// 相同事件合并的时间窗 (秒). 窗内同 `(kind, subject)` 只累加 `count`.
///
/// 60 秒是刻意的: 短于此的重复是**同一次故障**的余波(重试、并发请求),
/// 分开记只会让用户以为"又出了一次事"。
const MERGE_WINDOW_SECS: u64 = 60;
/// 台账容量上限 (条). 超出即从最旧开始丢弃 —— 这是**诊断信号**不是业务数据,
/// 不需要无限保留; 500 条足够覆盖"最近出过什么事"。
pub const MAX_EVENTS: usize = 500;

/// 事件台账: 内存环形缓冲 + 追加式落盘.
///
/// 与 `LogBuffer` 的取舍一致: 内存只保留最近 N 条供面板展示, 文件是权威副本
/// (启动时回填)。区别是这里**不做滚动** —— 上限就是 `MAX_EVENTS`, 写满即从头丢,
/// 因为事件是低频的诊断信号, 无需像请求日志那样支撑跨月统计。
pub struct EventLog {
    inner: Arc<Mutex<VecDeque<Event>>>,
    file_path: PathBuf,
}

impl EventLog {
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        let file_path = dir.join("events.jsonl");
        // 启动回填: 让"重启前出过什么事"在面板上仍然可见 —— 这是与进程内计数器
        // (DEGRADATIONS / session_stats 重启即清零) 的关键区别。
        let mut dq: VecDeque<Event> = std::fs::read_to_string(&file_path)
            .map(|c| {
                c.lines()
                    .filter_map(|l| serde_json::from_str::<Event>(l).ok())
                    .collect()
            })
            .unwrap_or_default();
        while dq.len() > MAX_EVENTS {
            dq.pop_front();
        }
        Self { inner: Arc::new(Mutex::new(dq)), file_path }
    }

    /// 记一条事件 (合并同窗重复 + 落盘)。
    ///
    /// 返回 `true` = 记了一条**新**事件; `false` = 与上一条合并（同故障余波）。
    /// 通知模块据此只对新事件响铃 —— 合并的重复若也弹，一个持续故障会在冷却窗外连环弹。
    ///
    /// 失败静默: **埋点自身绝不能影响转发** —— 与 `note_degradation` 同一原则。
    pub fn record(&self, kind: EventKind, subject: &str, detail: &str) -> bool {
        let ev = Event { ts: crate::admin::now_ts(), kind, subject: subject.to_string(), detail: detail.to_string(), count: 1 };
        let line = serde_json::to_string(&ev).ok();
        let mut merged = false;
        if let Ok(mut g) = self.inner.lock() {
            if let Some(last) = g.back_mut() {
                // 只与**紧邻的上一条**比较: 合并的前提是"中间没有别的事件",
                // 否则会把时间上分隔很远的两次故障误算成一次。
                if last.kind == kind
                    && last.subject == subject
                    && ev.ts.saturating_sub(last.ts) <= MERGE_WINDOW_SECS
                {
                    last.count += 1;
                    last.ts = ev.ts;
                    last.detail = ev.detail.clone();
                    merged = true;
                }
            }
            if !merged {
                g.push_back(ev);
                while g.len() > MAX_EVENTS {
                    g.pop_front();
                }
            }
        }
        // 合并时**不重复落盘**: 否则文件里会出现 N 行只差 count 的记录,
        // 而回填时它们会被当成 N 条独立事件 (与内存里的合并结果不一致)。
        if !merged {
            if let Some(line) = line {
                use std::io::Write;
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&self.file_path)
                {
                    let _ = f.write_all(format!("{line}\n").as_bytes());
                }
            }
        }
        !merged
    }

    /// 最近 `n` 条 (时间正序, 最新的在最后) —— 面板拿到后自行倒序展示.
    pub fn recent(&self, n: usize) -> Vec<Event> {
        let g = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return Vec::new(),
        };
        let skip = g.len().saturating_sub(n);
        g.iter().skip(skip).cloned().collect()
    }

    /// 清空 (面板"清空台账"). 同时重写文件, 否则重启后旧事件又回来了。
    pub fn clear(&self) {
        if let Ok(mut g) = self.inner.lock() {
            g.clear();
        }
        let _ = std::fs::write(&self.file_path, b"");
    }
}

/// 进程级台账句柄.
///
/// 为什么用全局而不是塞进 `AppState`: 事件产生点很散 —— 熔断器 (无状态的自由函数
/// `report_breaker`)、余额查询、账号续期、配置热重载等, 其中一些根本拿不到 `AppState`。
/// 为它们逐个穿参会把签名污染一大片, 而台账**本来就是进程级的单例**
/// (与 `DEGRADATIONS` 同一取舍)。
static LEDGER: std::sync::OnceLock<Arc<EventLog>> = std::sync::OnceLock::new();

/// 启动时安装台账 (只生效一次; 重复调用返回 false).
pub fn install(log: Arc<EventLog>) -> bool {
    LEDGER.set(log).is_ok()
}

/// 记一条事件.
///
/// 未安装时**静默丢弃** —— 埋点不能因为"台账还没起来"就让调用方处理错误;
/// 单测与工具路径 (不起完整进程) 就走这条路。
pub fn note(kind: EventKind, subject: &str, detail: &str) {
    // 通知只对**新事件**触发 (record 返回 false = 与上一条合并的同故障余波).
    let fresh = match LEDGER.get() {
        Some(l) => l.record(kind, subject, detail),
        None => false,
    };
    // 同时落一条日志: 计数只说"有", 日志说"何时" —— 与 note_degradation 同一口径.
    match kind.severity() {
        "err" => tracing::warn!("event: {} [{}] {}", kind.as_str(), subject, detail),
        _ => tracing::info!("event: {} [{}] {}", kind.as_str(), subject, detail),
    }
    if fresh {
        crate::notify::on_event(kind, subject, detail);
    }
}

/// 读取台账 (未安装时返回空).
pub fn ledger() -> Option<&'static Arc<EventLog>> {
    LEDGER.get()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> String {
        let d = std::env::temp_dir().join(format!("aigate_evt_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d.to_string_lossy().to_string()
    }

    #[test]
    fn records_and_reads_back_newest_last() {
        let d = tmp_dir("basic");
        let log = EventLog::new(&d);
        log.record(EventKind::BreakerOpen, "go", "连续失败 5 次");
        // 不同 subject ⇒ 不合并
        log.record(EventKind::BreakerOpen, "buddy", "错误率 40%");
        let evs = log.recent(10);
        assert_eq!(evs.len(), 2);
        assert_eq!(evs[0].subject, "go", "正序: 最早的在前");
        assert_eq!(evs[1].subject, "buddy");
        assert_eq!(evs[1].kind, EventKind::BreakerOpen);
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 同窗重复必须合并成一条 —— 否则持续故障会把台账刷满, 用户就不看了。
    #[test]
    fn merges_repeats_within_window() {
        let d = tmp_dir("merge");
        let log = EventLog::new(&d);
        for _ in 0..7 {
            log.record(EventKind::UpstreamFailing, "go", "连续失败 3 次");
        }
        let evs = log.recent(10);
        assert_eq!(evs.len(), 1, "同 (kind, subject) 同窗应合并");
        assert_eq!(evs[0].count, 7, "次数累加");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 中间夹了别的事件就不再合并 —— 那是两次独立故障, 合并会让用户少看到一次。
    #[test]
    fn does_not_merge_across_other_events() {
        let d = tmp_dir("nomerge");
        let log = EventLog::new(&d);
        log.record(EventKind::BreakerOpen, "go", "a");
        log.record(EventKind::BreakerClosed, "go", "b");
        log.record(EventKind::BreakerOpen, "go", "c");
        let evs = log.recent(10);
        assert_eq!(evs.len(), 3, "夹了恢复事件 ⇒ 两次断开是两件事");
        assert!(evs.iter().all(|e| e.count == 1));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn caps_at_max_events_dropping_oldest() {
        let d = tmp_dir("cap");
        let log = EventLog::new(&d);
        for i in 0..(MAX_EVENTS + 20) {
            // 交替 subject 避免被合并
            log.record(EventKind::Started, &format!("s{i}"), "");
        }
        let evs = log.recent(MAX_EVENTS + 100);
        assert_eq!(evs.len(), MAX_EVENTS, "超上限即丢最旧的");
        assert_eq!(evs[0].subject, "s20", "丢掉的是最早那批");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 跨重启保留 —— 这是与"进程内计数器"的关键区别 (用户要看的是"最近出过什么事",
    /// 而不是"本次启动以来出过什么事")。
    #[test]
    fn survives_restart() {
        let d = tmp_dir("restart");
        {
            let log = EventLog::new(&d);
            log.record(EventKind::BreakerOpen, "go", "连续失败 5 次");
        }
        let log2 = EventLog::new(&d);
        let evs = log2.recent(10);
        assert_eq!(evs.len(), 1, "新实例应回填到旧事件");
        assert_eq!(evs[0].kind, EventKind::BreakerOpen);
        assert_eq!(evs[0].subject, "go");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 合并过的行**不重复落盘** —— 否则回填会得到 N 条独立事件, 与内存不一致。
    #[test]
    fn merged_repeats_are_not_written_again() {
        let d = tmp_dir("mergefile");
        {
            let log = EventLog::new(&d);
            for _ in 0..5 {
                log.record(EventKind::QuotaLow, "buddy", "剩余 3%");
            }
        }
        let raw = std::fs::read_to_string(std::path::Path::new(&d).join("events.jsonl")).unwrap();
        assert_eq!(raw.lines().count(), 1, "5 次合并只应落 1 行");
        let log2 = EventLog::new(&d);
        assert_eq!(log2.recent(10).len(), 1, "回填后仍是 1 条");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn clear_empties_memory_and_disk() {
        let d = tmp_dir("clear");
        let log = EventLog::new(&d);
        log.record(EventKind::BreakerOpen, "go", "x");
        assert_eq!(log.recent(10).len(), 1);
        log.clear();
        assert_eq!(log.recent(10).len(), 0);
        let log2 = EventLog::new(&d);
        assert_eq!(log2.recent(10).len(), 0, "清空后重启不该又冒出来");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 旧行缺 `count` 必须仍能解析 (追加式文件里新旧混存)。
    #[test]
    fn legacy_line_without_count_still_loads() {
        let d = tmp_dir("legacy");
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(
            std::path::Path::new(&d).join("events.jsonl"),
            "{\"ts\":1,\"kind\":\"breaker_open\",\"subject\":\"go\",\"detail\":\"x\"}\n",
        )
        .unwrap();
        let log = EventLog::new(&d);
        let evs = log.recent(10);
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].count, 1, "缺 count 默认 1, 不是 0(0 会被读成'没发生过')");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// 严重程度只有三档, 且恢复正常类必须是 ok —— 避免"什么都是红的"。
    #[test]
    fn severity_is_three_tier() {
        assert_eq!(EventKind::BreakerOpen.severity(), "err");
        assert_eq!(EventKind::BreakerReset.severity(), "err");
        assert_eq!(EventKind::AccountDead.severity(), "err");
        assert_eq!(EventKind::QuotaLow.severity(), "warn");
        assert_eq!(EventKind::BreakerClosed.severity(), "ok");
        assert_eq!(EventKind::UpstreamRecovered.severity(), "ok");
    }

    /// kind 的落盘字面量是数据格式 —— 钉住它, 避免改名让历史台账读不出来。
    #[test]
    fn kind_serialization_is_stable() {
        assert_eq!(EventKind::BreakerOpen.as_str(), "breaker_open");
        assert_eq!(EventKind::UpstreamRecovered.as_str(), "upstream_recovered");
        assert_eq!(
            serde_json::to_string(&EventKind::BreakerHalfOpen).unwrap(),
            "\"breaker_half_open\""
        );
    }
}
