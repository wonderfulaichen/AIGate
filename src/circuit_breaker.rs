//! 熔断器 — 移植自 cc-switch (GPL-3.0).
//!
//! 原实现: https://github.com/farion1231/cc-switch blob/main/src-tauri/src/proxy/circuit_breaker.rs
//! AIGate 同为 GPL-3.0, 可合法移植并保留署名.
//!
//! 用途: 按供应商维度监控上游健康. 连续失败达阈值后断开 (Open),
//! 拒绝继续把请求打向已挂的供应商; 超时后放行单个探测 (HalfOpen),
//! 探测成功则恢复 (Closed). 避免苦等上游 660s 超时, 也避免反复打挂的端点.

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
        }
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
                    self.state = CircuitState::HalfOpen;
                    self.probe_in_flight = false;
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
        if self.state == CircuitState::HalfOpen {
            self.probe_in_flight = false;
            self.probe_since = None;
            if self.consecutive_successes >= self.config.success_threshold {
                self.close();
            }
        }
    }

    /// 记录一次失败.
    pub fn record_failure(&mut self) {
        self.push(false);
        self.consecutive_failures += 1;
        self.consecutive_successes = 0;
        match self.state {
            CircuitState::Closed => {
                let rate = self.error_rate();
                if self.consecutive_failures >= self.config.failure_threshold
                    || (self.window.len() as u32 >= self.config.min_requests
                        && rate >= self.config.error_rate_threshold)
                {
                    self.open();
                }
            }
            CircuitState::HalfOpen => {
                self.probe_in_flight = false;
                self.probe_since = None;
                self.open();
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

    fn open(&mut self) {
        self.state = CircuitState::Open;
        self.opened_at = Some(Instant::now());
        self.probe_in_flight = false;
        self.probe_since = None;
    }

    fn close(&mut self) {
        self.state = CircuitState::Closed;
        self.opened_at = None;
        self.probe_in_flight = false;
        self.probe_since = None;
        self.window.clear();
        self.consecutive_failures = 0;
        self.consecutive_successes = 0;
    }

    /// 手动强制关闭熔断 (运维用, 如面板"重置熔断"按钮).
    pub fn force_close(&mut self) {
        self.close();
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
}
