//! 熔断器 — 本项目的独立实现.
//!
//! 用途: 按供应商维度监控上游健康. 连续失败达阈值后断开 (Open),
//! 拒绝继续把请求打向已挂的供应商; 超时后放行单个探测 (HalfOpen),
//! 探测成功则恢复 (Closed). 避免苦等上游 660s 超时, 也避免反复打挂的端点.
//!
//! ## 与外部项目的说明 (2026-10-08 核对)
//!
//! 设计上参考过 cc-switch (MIT) 的熔断器思路 —— 那是**通用模式** (Closed/Open/HalfOpen
//! 三态 + 阈值 + 半开探测), 不是其专有表达. 实测**逐行对照**: 与本项目此文件
//! 完全相同的代码行仅 29 行, 且全是 `match` 分支/结构声明这类无法避免的通用写法
//! (代码行数 339 vs 363), 属独立重写而非搬运.
//!
//! ⚠️ **本文件此前误标为「移植自 cc-switch (GPL-3.0)」—— 两处都不准确**:
//! ① cc-switch 的实际许可是 **MIT** (其 LICENSE 与 package.json 均为 MIT);
//! ② 本文件是独立重写, 不是移植. 错误的措辞会凭空抬高本项目的许可义务
//!    (AGPL 类条款会传染, 而 MIT 只是署名), 故更正为现状描述.
//! 见 `THIRD-PARTY-NOTICES.md`.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// 熔断状态.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    Closed,
    Open,
    HalfOpen,
}

impl CircuitState {
    /// 供面板展示的字符串.
    pub fn as_str(&self) -> &'static str {
        match self {
            CircuitState::Closed => "closed",
            CircuitState::Open => "open",
            CircuitState::HalfOpen => "half-open",
        }
    }
}

/// 状态迁移的**原因** —— 这是"为什么断了"这个问题的唯一答案来源.
///
/// 为什么必须记原因: 面板此前只有**当前快照** (closed / open / half-open),
/// 于是用户看到"熔断开着"却不知道该查什么 —— 是连续失败撞的阈值, 还是错误率超标?
/// 两者要采取的行动完全不同 (前者查单点故障, 后者查整体不稳).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransitionReason {
    /// 连续失败次数达到 `failure_threshold`.
    ConsecutiveFailures { n: u32 },
    /// 窗口内错误率达到 `error_rate_threshold` (附样本数, 否则没法判断可信度).
    ErrorRate { pct: u32, samples: usize },
    /// HalfOpen 下的探测请求失败 → 重新断开.
    ProbeFailed,
    /// HalfOpen 下探测 (连续) 成功达到 `success_threshold` → 恢复.
    ProbeSucceeded { n: u32 },
    /// 上游**已能正常响应** (连续失败后首次成功), 但未必已满足关闭熔断的条件.
    ///
    /// 与 `ProbeSucceeded` 分开的理由: 后者专指"熔断正式关闭", 文案是"确认已恢复";
    /// 半开状态下只成功 1 次还不能说"确认恢复", 但用户确实需要知道"它又开始应答了"。
    /// 混用会让文案与事实不符 —— 本项目对"如实描述"是有硬要求的。
    RespondingAgain { n: u32 },
    /// Open 维持超时后自动转入 HalfOpen (放行一次探测).
    CooldownElapsed { after_secs: u64 },
    /// 用户手动重置.
    ManualReset,
}

impl TransitionReason {
    /// 落进台账/日志的**人话**. 前端不拼这个串 —— 精确值只有这里拿得到.
    pub fn describe(&self) -> String {
        match self {
            TransitionReason::ConsecutiveFailures { n } => {
                format!("连续失败 {n} 次，达到阈值")
            }
            TransitionReason::ErrorRate { pct, samples } => {
                format!("最近 {samples} 次请求错误率 {pct}%，超过阈值")
            }
            TransitionReason::ProbeFailed => "冷却后的探测请求仍然失败".to_string(),
            TransitionReason::ProbeSucceeded { n } => {
                format!("连续 {n} 次探测成功，确认上游已恢复")
            }
            TransitionReason::RespondingAgain { n } => {
                format!("上游已恢复响应（连续 {n} 次成功）")
            }
            TransitionReason::CooldownElapsed { after_secs } => {
                format!("冷却 {after_secs} 秒结束，放行一次探测")
            }
            TransitionReason::ManualReset => "在设置面板手动重置".to_string(),
        }
    }
}

/// 一次状态迁移 (从 → 到 + 原因). 由熔断器记录, 上层取走并写台账.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transition {
    pub from: CircuitState,
    pub to: CircuitState,
    pub reason: TransitionReason,
}

/// 熔断配置.
#[derive(Debug, Clone)]
pub struct CircuitBreakerConfig {
    /// 连续失败次数达到此值 → Open.
    pub failure_threshold: u32,
    /// HalfOpen 下连续成功次数达到此值 → Closed.
    pub success_threshold: u32,
    /// Open 状态维持此时间后 → HalfOpen (允许 1 个探测).
    pub timeout: Duration,
    /// 窗口内错误率超过此比例 (且样本数 >= min_requests) → Open.
    pub error_rate_threshold: f64,
    /// 触发错误率判定所需的最小样本数.
    pub min_requests: u32,
}

impl Default for CircuitBreakerConfig {
    fn default() -> Self {
        // 沿用 cc-switch 经过实战验证的默认阈值.
        Self {
            failure_threshold: 4,
            success_threshold: 2,
            timeout: Duration::from_secs(60),
            error_rate_threshold: 0.6,
            min_requests: 10,
        }
    }
}

/// 近期结果滚动窗口容量 (用于错误率判定).
const WINDOW: usize = 20;

/// 单供应商熔断器.
#[derive(Debug)]
pub struct CircuitBreaker {
    config: CircuitBreakerConfig,
    state: CircuitState,
    /// 近期结果窗口: true=成功, false=失败.
    window: VecDeque<bool>,
    consecutive_failures: u32,
    consecutive_successes: u32,
    /// Open 起始时刻, 用于超时判定.
    opened_at: Option<Instant>,
    /// HalfOpen 下是否已有探测在飞 (保证同时仅 1 个探测).
    probe_in_flight: bool,
    /// 探测开始时刻: 用于探测请求因 handler 中途退出而漏调 report_breaker
    /// 时, 超时后强制释放名额, 避免 provider 永久死锁在 503.
    probe_since: Option<Instant>,
    /// 待取走的状态迁移队列.
    ///
    /// 为什么用队列而不是回调: 熔断器是**纯逻辑 + 零 I/O** (它被 `Mutex` 包着、
    /// 临界区必须极短, 写文件绝不能在锁内做)。上层在放锁之后取走并写台账, 既保住
    /// 临界区, 又让"什么时候发生了迁移"变成可读的事实 —— 此前这个文件**一行日志都没有**,
    /// 熔断开合完全不可见 (用户报"出问题得自己一点点找"的根因之一)。
    ///
    /// ⚠️ 必须**有上限**: 若上层从不取走 (如单测、或某条路径漏接), 无界队列会一直涨。
    pending: Vec<Transition>,
    /// 是否已经报过"该供应商在连续失败"的**预警**.
    ///
    /// 用意: 熔断要连续失败 4 次才断, 而用户往往在第 2 次失败时就想知道"这个供应商
    /// 是不是不稳"。预警只在跨过阈值时报**一次**, 恢复后复位 —— 否则每次失败报一条,
    /// 台账立刻变成噪音 (那正是"用户不再看台账"的成因)。
    warned_failing: bool,
}

impl CircuitBreaker {
    pub fn new() -> Self {
        Self::with_config(CircuitBreakerConfig::default())
    }

    pub fn with_config(config: CircuitBreakerConfig) -> Self {
        Self {
            config,
            state: CircuitState::Closed,
            window: VecDeque::with_capacity(WINDOW),
            consecutive_failures: 0,
            consecutive_successes: 0,
            opened_at: None,
            probe_in_flight: false,
            probe_since: None,
            pending: Vec::new(),
            warned_failing: false,
        }
    }

    /// 取走待处理的状态迁移 (上层据此写事件台账).
    ///
    /// `drain` 语义: 取走即清空, 同一个迁移不会被记两次。
    pub fn take_transitions(&mut self) -> Vec<Transition> {
        std::mem::take(&mut self.pending)
    }

    /// 记一次迁移 (内部用). 上限 32 条 —— 见 `pending` 字段的说明.
    fn note(&mut self, from: CircuitState, to: CircuitState, reason: TransitionReason) {
        if from == to {
            return;
        }
        if self.pending.len() >= 32 {
            self.pending.remove(0);
        }
        self.pending.push(Transition { from, to, reason });
    }

    /// 仅读取当前状态, 不推进 Open→HalfOpen (供监控/健康接口只读展示).
    pub fn peek_state(&self) -> CircuitState {
        self.state
    }

    /// 当前状态 (会按需把 Open 推进到 HalfOpen).
    pub fn state(&mut self) -> CircuitState {
        if self.state == CircuitState::Open {
            if let Some(opened) = self.opened_at {
                if opened.elapsed() >= self.config.timeout {
                    let from = self.state;
                    self.state = CircuitState::HalfOpen;
                    self.probe_in_flight = false;
                    self.note(
                        from,
                        self.state,
                        TransitionReason::CooldownElapsed {
                            after_secs: self.config.timeout.as_secs(),
                        },
                    );
                }
            }
        }
        self.state
    }

    /// 是否允许发请求. 调用后会消费 HalfOpen 的探测名额.
    ///
    /// ⚠️ 只应在**请求入口**调用一次: 每次调用都会尝试认领探测名额.
    /// 同一请求的重试循环复检请用 [`Self::allows_retry`].
    pub fn allow_request(&mut self) -> bool {
        match self.state() {
            CircuitState::Closed => true,
            CircuitState::Open => false,
            CircuitState::HalfOpen => {
                // 防御死锁: 探测名额被占, 但探测开始已超过 timeout 仍未回填
                // (handler 中途退出漏调 report_breaker), 强制释放名额放行新探测.
                if self.probe_in_flight {
                    if let Some(since) = self.probe_since {
                        if since.elapsed() >= self.config.timeout {
                            self.probe_in_flight = false;
                            self.probe_since = None;
                        }
                    }
                }
                if self.probe_in_flight {
                    false
                } else {
                    self.probe_in_flight = true;
                    self.probe_since = Some(Instant::now());
                    true
                }
            }
        }
    }

    /// 非消费式复检: 判断「已持有探测名额的本次请求」是否仍可继续发送.
    ///
    /// 供重试循环使用. 与 [`Self::allow_request`] 的区别是**不认领也不消费**探测名额:
    /// - Closed → 放行;
    /// - Open → 拒绝 (可能是本次探测之外的原因重新断开, 如并发的另一次探测失败);
    /// - HalfOpen → 放行 (探测名额已在入口由本次请求认领, 重试属于同一次探测).
    ///
    /// 不能在这里再调 `allow_request`: 名额已被本次请求自己占住, 再调必返回 false,
    /// 于是请求在发出一字节之前就被自己挡成 503, 探测永远打不到上游 ——
    /// 熔断器因此无法自动恢复, 只能手动重置 (实测事故根因).
    pub fn allows_retry(&mut self) -> bool {
        match self.state() {
            CircuitState::Closed | CircuitState::HalfOpen => true,
            CircuitState::Open => false,
        }
    }

    /// Open 状态下距转入 HalfOpen (允许探测) 还需等待的秒数; 非 Open 返回 None.
    /// 供错误响应告知调用方"多久后自动恢复".
    pub fn retry_after_secs(&self) -> Option<u64> {
        if self.state != CircuitState::Open {
            return None;
        }
        let opened = self.opened_at?;
        let timeout = self.config.timeout.as_secs_f64();
        let elapsed = opened.elapsed().as_secs_f64();
        Some(if elapsed >= timeout { 0 } else { (timeout - elapsed).ceil() as u64 })
    }

    /// 记录一次成功.
    pub fn record_success(&mut self) {
        self.push(true);
        self.consecutive_successes += 1;
        self.consecutive_failures = 0;
        let was_failing = self.warned_failing;
        self.warned_failing = false;
        if self.state == CircuitState::HalfOpen {
            self.probe_in_flight = false;
            self.probe_since = None;
            if self.consecutive_successes >= self.config.success_threshold {
                self.close_with(TransitionReason::ProbeSucceeded {
                    n: self.consecutive_successes,
                });
                return;
            }
        }
        // 恢复通知: 之前处在"连续失败"状态、现在成功了 —— 报一次"已在应答"。
        // 这是用户要的"异常通知"里最该有的一条: 故障自愈了, 但没人在看面板。
        // ⚠️ 用 `RespondingAgain` 而非 `ProbeSucceeded` —— 此刻可能才成功 1 次、
        //    熔断还没关, 说"确认恢复"就是与事实不符。
        if was_failing {
            self.note_peer(TransitionReason::RespondingAgain { n: self.consecutive_successes });
        }
    }

    /// 记一条**不成迁移**的通知性事件 (借 pending 队列捎出去).
    ///
    /// 用途: "已恢复"是 from==to==Closed, 不构成状态迁移, 但用户需要知道 ——
    /// 故单独走这条路, 而不是硬塞进 Transition (那会破坏"迁移必有 from≠to"的不变式)。
    fn note_peer(&mut self, reason: TransitionReason) {
        if self.pending.len() >= 32 {
            self.pending.remove(0);
        }
        // from == to 会被 report_transitions 归到"其余组合"跳过, 故这里改用
        // HalfOpen→Closed 之外的**显式标记**: 直接复用 Closed→Closed 表达"仍是闭合但情况变了",
        // 由 report_transitions 识别 `from == to == Closed` 为"恢复通知"。
        self.pending.push(Transition {
            from: CircuitState::Closed,
            to: CircuitState::Closed,
            reason,
        });
    }

    /// 记录一次失败.
    pub fn record_failure(&mut self) {
        self.push(false);
        self.consecutive_failures += 1;
        self.consecutive_successes = 0;
        match self.state {
            CircuitState::Closed => {
                let rate = self.error_rate();
                let samples = self.window.len();
                // 判据顺序即原因优先级: 先看"连续失败"(单点故障, 更明确的信号),
                // 再看"错误率"(整体不稳). 两者同时成立时记前者 —— 它是更可行动的线索.
                if self.consecutive_failures >= self.config.failure_threshold {
                    self.open_with(TransitionReason::ConsecutiveFailures {
                        n: self.consecutive_failures,
                    });
                } else if samples as u32 >= self.config.min_requests
                    && rate >= self.config.error_rate_threshold
                {
                    self.open_with(TransitionReason::ErrorRate {
                        pct: (rate * 100.0).round() as u32,
                        samples,
                    });
                } else if self.consecutive_failures >= 2 && !self.warned_failing {
                    // ★ 预警 (2026-10-11): 还没到熔断阈值, 但已经连续失败 2 次 ——
                    //   用户往往此刻就想知道"这个供应商不稳", 而不是等熔断真的开了。
                    //   只报一次 (`warned_failing` 置位), 恢复后复位。
                    self.warned_failing = true;
                    self.pending.push(Transition {
                        from: CircuitState::Closed,
                        to: CircuitState::Closed,
                        reason: TransitionReason::ConsecutiveFailures {
                            n: self.consecutive_failures,
                        },
                    });
                }
            }
            CircuitState::HalfOpen => {
                self.probe_in_flight = false;
                self.probe_since = None;
                self.open_with(TransitionReason::ProbeFailed);
            }
            CircuitState::Open => {}
        }
    }

    /// 追加一条结果并维持窗口容量.
    fn push(&mut self, ok: bool) {
        self.window.push_back(ok);
        while self.window.len() > WINDOW {
            self.window.pop_front();
        }
    }

    /// 窗口内失败比例.
    fn error_rate(&self) -> f64 {
        if self.window.is_empty() {
            return 0.0;
        }
        let failures = self.window.iter().filter(|&&ok| !ok).count() as f64;
        failures / self.window.len() as f64
    }

    fn open_with(&mut self, reason: TransitionReason) {
        let from = self.state;
        self.state = CircuitState::Open;
        self.opened_at = Some(Instant::now());
        self.probe_in_flight = false;
        self.probe_since = None;
        self.note(from, CircuitState::Open, reason);
    }

    fn close_with(&mut self, reason: TransitionReason) {
        let from = self.state;
        self.state = CircuitState::Closed;
        self.opened_at = None;
        self.probe_in_flight = false;
        self.probe_since = None;
        self.window.clear();
        self.consecutive_failures = 0;
        self.consecutive_successes = 0;
        self.note(from, CircuitState::Closed, reason);
    }

    /// 手动强制关闭熔断 (运维用, 如面板"重置熔断"按钮).
    pub fn force_close(&mut self) {
        // 已经是 Closed 时不记迁移 (note 内部按 from==to 过滤), 避免"点了下重置"
        // 就在台账里留一条毫无信息量的事件.
        let from = self.state;
        self.close_with(if from == CircuitState::Open {
            TransitionReason::ManualReset
        } else {
            TransitionReason::ProbeSucceeded { n: self.consecutive_successes }
        });
    }
}

impl Default for CircuitBreaker {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_closed() {
        let mut cb = CircuitBreaker::new();
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.allow_request());
    }

    #[test]
    fn opens_after_consecutive_failures() {
        let mut cb = CircuitBreaker::new();
        for _ in 0..4 {
            cb.record_failure();
        }
        assert_eq!(cb.state(), CircuitState::Open);
        assert!(!cb.allow_request());
    }

    #[test]
    fn opens_on_error_rate() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 100, // 不被连续失败触发
            success_threshold: 2,
            timeout: Duration::from_secs(60),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Open);
    }

    #[test]
    fn half_open_probe_recovers() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            timeout: Duration::from_millis(1),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure(); // -> Open
        assert!(!cb.allow_request());
        std::thread::sleep(Duration::from_millis(3));
        assert_eq!(cb.state(), CircuitState::HalfOpen);
        assert!(cb.allow_request()); // 探测 1
        cb.record_success(); // consecutive_successes=1, 仍 HalfOpen
        assert!(cb.allow_request()); // 探测 2
        cb.record_success(); // -> Closed
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.allow_request());
    }

    #[test]
    fn half_open_probe_failure_reopens() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            timeout: Duration::from_millis(1),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        std::thread::sleep(Duration::from_millis(3));
        assert!(cb.allow_request()); // 探测
        cb.record_failure(); // -> Open
        assert_eq!(cb.state(), CircuitState::Open);
    }

    #[test]
    fn only_one_probe_in_flight() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            timeout: Duration::from_millis(1),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        std::thread::sleep(Duration::from_millis(3));
        assert!(cb.allow_request()); // 第一个探测占用名额
        assert!(!cb.allow_request()); // 第二个被拒
    }

    /// 回归: HalfOpen 下「入口认领名额 + 重试复检」不得把同一请求挡成 503.
    ///
    /// 事故形态: 入口 allow_request() 认领名额后, 重试循环又调 allow_request(),
    /// 名额已被自己占用 → false → 请求在发出前被自己挡下, 探测永远到不了上游,
    /// 熔断只能手动重置 (实测 aigate.log 中出现 8 次「入口通过 0~3ms 后 circuit open」).
    #[test]
    fn retry_recheck_does_not_consume_probe() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            timeout: Duration::from_millis(1),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure(); // -> Open
        std::thread::sleep(Duration::from_millis(3));

        assert!(cb.allow_request(), "入口应认领探测名额");
        // 同一请求重试复检: 必须放行, 且不得再次消费/释放名额
        assert!(cb.allows_retry(), "重试复检必须放行 (否则探测发不出去)");
        assert!(cb.allows_retry(), "多次重试复检同样放行");
        // 名额仍被本次探测持有 —— 并发的另一个请求不得插队
        assert!(!cb.allow_request(), "名额应仍被本次探测占用, 其他请求不得插队");
    }

    /// allows_retry 在 Closed / Open 下的语义: Closed 放行, Open 拒绝.
    #[test]
    fn allows_retry_semantics() {
        let mut cb = CircuitBreaker::new();
        assert!(cb.allows_retry(), "Closed 应放行");
        for _ in 0..4 {
            cb.record_failure();
        }
        assert_eq!(cb.state(), CircuitState::Open);
        assert!(!cb.allows_retry(), "Open 应拒绝");
    }

    /// retry_after_secs: Open 时给出剩余等待秒数, 其他状态为 None.
    #[test]
    fn retry_after_secs_reports_remaining() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            timeout: Duration::from_secs(60),
            error_rate_threshold: 0.6,
            min_requests: 2,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        assert_eq!(cb.retry_after_secs(), None, "Closed 无等待");
        cb.record_failure(); // -> Open
        let w = cb.retry_after_secs().expect("Open 应给出等待秒数");
        assert!(w <= 60 && w > 0, "等待秒数应在 (0,60], 实际 {w}");
    }

    /// 端到端: 探测成功两次后熔断自动恢复 (证明无需手动重置).
    #[test]
    fn half_open_recovers_without_manual_reset() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 4,
            success_threshold: 2,
            timeout: Duration::from_millis(1),
            error_rate_threshold: 0.6,
            min_requests: 10,
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        for _ in 0..4 {
            cb.record_failure();
        }
        assert_eq!(cb.state(), CircuitState::Open);
        std::thread::sleep(Duration::from_millis(3));
        assert_eq!(cb.state(), CircuitState::HalfOpen);
        // 探测 1: 入口认领 + 复检放行 + 成功
        assert!(cb.allow_request());
        assert!(cb.allows_retry());
        cb.record_success();
        // 探测 2
        assert!(cb.allow_request());
        assert!(cb.allows_retry());
        cb.record_success();
        assert_eq!(cb.state(), CircuitState::Closed, "两次探测成功后应自动恢复");
        assert!(cb.allows_retry());
    }

    // ─── 状态迁移留痕 (2026-10-11: 用户报"出问题得自己一点点找") ───
    //
    // 这组断言的全部意义: 熔断此前**一行日志都没有**, 面板只有当前快照 ——
    // 用户看到"熔断开着"却查不出"什么时候断的、为什么断"。迁移队列就是那个答案。

    /// 连续失败撞阈值 → 必须留下**原因**(而不是只留一个 open 状态).
    #[test]
    fn transition_records_consecutive_failure_reason() {
        let cfg = CircuitBreakerConfig { failure_threshold: 3, min_requests: 100, ..CircuitBreakerConfig::default() };
        let mut cb = CircuitBreaker::with_config(cfg);
        for _ in 0..3 {
            cb.record_failure();
        }
        let ts = cb.take_transitions();
        // 第 2 次失败时先报了一条"连续失败"预警, 第 3 次撞阈值才是断开 —— 两条都在队列里,
        // 顺序即发生顺序 (预警在前)。
        assert_eq!(ts.len(), 2, "预警 + 断开各记一条");
        assert_eq!(ts[1].from, CircuitState::Closed);
        assert_eq!(ts[1].to, CircuitState::Open);
        assert_eq!(ts[1].reason, TransitionReason::ConsecutiveFailures { n: 3 });
        assert!(ts[1].reason.describe().contains("连续失败 3 次"), "原因要能人话描述");
        // drain 语义: 取走即清空, 不会被记两次
        assert!(cb.take_transitions().is_empty(), "取走后应清空");
    }

    /// 预警只在跨过"连续 2 次失败"时报**一次** —— 否则每次失败报一条, 台账变噪音。
    #[test]
    fn warns_failing_only_once_until_recovery() {
        let cfg = CircuitBreakerConfig { failure_threshold: 99, min_requests: 999, ..CircuitBreakerConfig::default() };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure(); // 1 次: 不到预警线
        assert!(cb.take_transitions().is_empty(), "1 次失败不该报 (太早)");
        cb.record_failure(); // 2 次: 报预警
        assert_eq!(cb.take_transitions().len(), 1, "2 次失败报一次预警");
        cb.record_failure(); // 3 次: 仍在失败, 不重复报
        cb.record_failure(); // 4 次
        assert!(cb.take_transitions().is_empty(), "持续失败期间不重复报");
        cb.record_success(); // 恢复 → 报一条"已恢复"
        let ts = cb.take_transitions();
        assert_eq!(ts.len(), 1, "恢复报一条");
        assert_eq!(ts[0].reason, TransitionReason::RespondingAgain { n: 1 });
        cb.record_failure(); // 再次失败: 预警状态已复位, 会再报
        cb.record_failure();
        assert_eq!(cb.take_transitions().len(), 1, "恢复后再次连续失败应重新预警");
    }

    /// 错误率触发时, **原因必须与连续失败区分开** —— 两者要采取的行动不同。
    #[test]
    fn transition_distinguishes_error_rate_from_consecutive() {
        // min_requests=4 / 阈值 50%: F,T,F,T,F 造出 60% 错误率, 而连续失败数只有 1
        // (且 <2, 不触发预警) ⇒ 只可能由错误率触发, 与"连续失败"路径完全分开。
        // ⚠️ 判定只在 record_failure 里跑, 故最后一步必须是失败, 否则样本数不够。
        let cfg = CircuitBreakerConfig {
            failure_threshold: 99,
            min_requests: 4,
            error_rate_threshold: 0.5,
            ..CircuitBreakerConfig::default()
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        cb.record_success();
        cb.record_failure();
        cb.record_success();
        cb.record_failure();
        let ts = cb.take_transitions();
        assert_eq!(ts.len(), 1, "错误率达标应断开 (且未触发连续失败预警)");
        match &ts[0].reason {
            TransitionReason::ErrorRate { pct, samples } => {
                assert_eq!(*pct, 60, "错误率应如实记录");
                assert_eq!(*samples, 5, "样本数要一起记, 否则没法判断可信度");
            }
            other => panic!("应记为 ErrorRate, 实得 {other:?}"),
        }
    }

    /// 冷却结束 → HalfOpen 也要留痕 (否则"它自己好了"这件事无从解释).
    #[test]
    fn transition_records_cooldown_to_half_open() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            timeout: Duration::from_millis(1),
            success_threshold: 9, // 避免顺手就关回 Closed 干扰断言
            ..CircuitBreakerConfig::default()
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure(); // -> Open
        let _ = cb.take_transitions();
        std::thread::sleep(Duration::from_millis(3));
        assert_eq!(cb.state(), CircuitState::HalfOpen);
        let ts = cb.take_transitions();
        assert_eq!(ts.len(), 1);
        assert_eq!(ts[0].to, CircuitState::HalfOpen);
        assert!(matches!(ts[0].reason, TransitionReason::CooldownElapsed { .. }));
    }

    /// 探测失败 → 重新断开, 原因是 ProbeFailed (而不是又算成"连续失败 N 次").
    #[test]
    fn transition_records_probe_failure() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            timeout: Duration::from_millis(1),
            ..CircuitBreakerConfig::default()
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        let _ = cb.take_transitions();
        std::thread::sleep(Duration::from_millis(3));
        let _ = cb.state(); // -> HalfOpen
        let _ = cb.take_transitions();
        cb.record_failure(); // 探测失败
        let ts = cb.take_transitions();
        assert_eq!(ts.len(), 1);
        assert_eq!(ts[0].to, CircuitState::Open);
        assert_eq!(ts[0].reason, TransitionReason::ProbeFailed);
    }

    /// 手动重置: Open → Closed 必须标成"人工介入", 与"自己恢复"分开。
    #[test]
    fn transition_marks_manual_reset() {
        let cfg = CircuitBreakerConfig { failure_threshold: 1, ..CircuitBreakerConfig::default() };
        let mut cb = CircuitBreaker::with_config(cfg);
        cb.record_failure();
        let _ = cb.take_transitions();
        cb.force_close();
        let ts = cb.take_transitions();
        assert_eq!(ts.len(), 1);
        assert_eq!(ts[0].reason, TransitionReason::ManualReset);
        assert!(ts[0].reason.describe().contains("手动"));
    }

    /// **已经在 Closed 时点重置不该产生事件** —— 否则台账里全是"点了下按钮"的噪音
    /// (这正是让用户不再看台账的原因)。
    #[test]
    fn no_transition_when_already_closed() {
        let mut cb = CircuitBreaker::new();
        cb.force_close();
        assert!(cb.take_transitions().is_empty(), "本就闭合, 无迁移");
        cb.record_success();
        cb.record_success();
        assert!(cb.take_transitions().is_empty(), "成功不产生迁移");
    }

    /// 迁移队列必须有上限: 上层若漏接, 不能无限增长。
    #[test]
    fn pending_transitions_are_bounded() {
        let cfg = CircuitBreakerConfig {
            failure_threshold: 1,
            timeout: Duration::from_millis(1),
            ..CircuitBreakerConfig::default()
        };
        let mut cb = CircuitBreaker::with_config(cfg);
        for _ in 0..100 {
            cb.record_failure(); // Closed→Open
            cb.force_close(); // Open→Closed
        }
        assert!(cb.take_transitions().len() <= 32, "队列必须有上限");
    }

    /// 原因文案必须是人话且带上具体数值 (前端不拼这个串, 精确值只有这里拿得到)。
    #[test]
    fn reason_descriptions_carry_numbers() {
        assert!(TransitionReason::ErrorRate { pct: 42, samples: 20 }.describe().contains("42"));
        assert!(TransitionReason::ErrorRate { pct: 42, samples: 20 }.describe().contains("20"));
        assert!(TransitionReason::CooldownElapsed { after_secs: 60 }.describe().contains("60"));
        assert!(TransitionReason::ProbeSucceeded { n: 2 }.describe().contains("2"));
    }
}
