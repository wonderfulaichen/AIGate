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
    pub fn allow_request(&mut self) -> bool {
        match self.state() {
            CircuitState::Closed => true,
            CircuitState::Open => false,
            CircuitState::HalfOpen => {
                if self.probe_in_flight {
                    false
                } else {
                    self.probe_in_flight = true;
                    true
                }
            }
        }
    }

    /// 记录一次成功.
    pub fn record_success(&mut self) {
        self.push(true);
        self.consecutive_successes += 1;
        self.consecutive_failures = 0;
        if self.state == CircuitState::HalfOpen {
            self.probe_in_flight = false;
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
    }

    fn close(&mut self) {
        self.state = CircuitState::Closed;
        self.opened_at = None;
        self.probe_in_flight = false;
        self.window.clear();
        self.consecutive_failures = 0;
        self.consecutive_successes = 0;
    }

    /// 手动强制关闭熔断 (运维用, 如面板"重置熔断"按钮).
    pub fn force_close(&mut self) {
        self.close();
    }

    /// 手动强制打开熔断 (启动预检发现供应商不可达时使用).
    ///
    /// 进入 Open 后按 timeout 超时推进到 HalfOpen, 由探测请求决定是否恢复,
    /// 期间所有请求快速失败 (503), 避免苦等上游超时.
    pub fn force_open(&mut self) {
        self.open();
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
}
