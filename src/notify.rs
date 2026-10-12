//! 报错通知 —— 红点（托盘图标）+ 气球弹窗（2026-10-12，用户要求「软件挂后台都不知道有没有问题」）。
//!
//! <h3>与事件台账的分工</h3>
//! 台账（`events.rs`）解决「查得到」：关键事件落盘、面板倒序列出。
//! 本模块解决「主动推出来」：事件发生时托盘点红点 + 弹一次气球。
//! 数据源就是台账 —— `events::note()` 是唯一漏斗，非合并的新事件才触发通知。
//!
//! <h3>零新依赖的取舍</h3>
//! `tray-icon 0.13` 没有通知 API（查过源码）。此前用户也明确过「不加新依赖」——
//! 故气球由**系统自带的 PowerShell**（WinForms NotifyIcon.ShowBalloonTip）承载：
//! 每次弹一个短命进程（约 1 秒启动开销，存活 ~12s 让气球显示完）。
//! 已知限制（如实记录）：
//!   · 个别杀软会拦短命 PowerShell → 气球没了，但**红点仍在**（两层是冗余设计）；
//!   · 气球不进通知中心（那是 WinRT Toast 的能力，需加 `windows` crate，用户未选）。
//! 红点本身是托盘图标程序化重绘（`make_icon_rgba_with(alert=true)`），零外部进程。
//!
//! <h3>分级（用户 2026-10-12 拍板）</h3>
//!   · **气球**：错误级（熔断打开/账号作废）+ 额度将尽 —— 「不然我无法及时切换模型」；
//!   · **红点**：任何未读的错误/警告（含上游连续失败预警、端口顺延）；
//!   · 用户自己触发的（重置熔断/保存配置）与恢复类事件**不通知** —— 通知自己刚做的操作是噪音。
//!
//! <h3>未读语义</h3>
//! 红点 = 「有你没在面板窗口里看过的问题」。**确认 = 打开面板窗口**（托盘点击/菜单「显示面板」）。
//! 刻意**不**用「面板 API 被轮询」当确认：WebView 隐藏到托盘后仍在轮询，那会把红点在
//! 用户没看的情况下清掉（正是要解决的「挂后台不知道」）。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::events::EventKind;

/// 未读告警计数（红点的唯一数据源）。只增不减，`ack()` 清零。
static UNREAD: AtomicU64 = AtomicU64::new(0);
/// 上次弹气球的时刻（全局冷却，防不同事件连环刷屏）。
static LAST_BALLOON: Mutex<Option<Instant>> = Mutex::new(None);
/// 气球冷却时长。台账自身有 60s 合并窗（同 kind+subject），这里防的是**不同事件**连弹。
const BALLOON_COOLDOWN: Duration = Duration::from_secs(30);
/// 环境开关（缓存一次）：`AIGATE_NO_NOTIFY=1` 关闭全部通知（红点 + 气球）。
static DISABLED: OnceLock<bool> = OnceLock::new();

/// 通知总开关（`AIGATE_NO_NOTIFY=1` 关闭）。
pub fn disabled() -> bool {
    *DISABLED.get_or_init(|| {
        matches!(std::env::var("AIGATE_NO_NOTIFY"), Ok(v) if v == "1" || v.eq_ignore_ascii_case("true"))
    })
}

/// 台账漏斗的唯一出口：非合并的新事件进来，决定点红点 / 弹气球。
///
/// `EventLog::record` 返回是否被合并 —— 合并的重复（同故障余波）**不通知**，
/// 否则一个持续故障会在冷却窗外连环弹。
pub fn on_event(kind: EventKind, subject: &str, detail: &str) {
    if disabled() {
        return;
    }
    if !kind.dot() && !kind.balloon() {
        return;
    }
    UNREAD.fetch_add(1, Ordering::Relaxed);
    if kind.balloon() {
        try_balloon(kind, subject, detail);
    }
}

/// 当前未读告警数（0 = 无红点）。
pub fn unread() -> u64 {
    UNREAD.load(Ordering::Relaxed)
}

/// 确认已读（打开面板窗口时调用）。
pub fn ack() {
    UNREAD.store(0, Ordering::Relaxed);
}

/// 冷却检查 + 触发弹窗（分离出来便于单测里直接验证冷却语义）。
fn try_balloon(kind: EventKind, subject: &str, detail: &str) {
    {
        let mut g = match LAST_BALLOON.lock() {
            Ok(g) => g,
            Err(_) => return, // 锁中毒不值得 panic: 通知失败绝不能影响转发
        };
        if let Some(t) = *g {
            if t.elapsed() < BALLOON_COOLDOWN {
                return;
            }
        }
        *g = Some(Instant::now());
    }
    let title = format!("AIGate · {}", crate::i18n::notify_kind_name(kind));
    let mut body = if subject.is_empty() {
        detail.to_string()
    } else if detail.is_empty() {
        subject.to_string()
    } else {
        format!("{subject} — {detail}")
    };
    // 气球正文上限：PowerShell 环境变量传递无长度问题，但气球 UI 本身会截断，
    // 这里按字符边界安全截断（i18n::snip，别再手写字节切片 —— 见 panic 二次复发教训）。
    body = crate::i18n::snip(&body, 400).to_string();
    spawn_balloon(title, body);
}

/// 弹一次气球。
///
/// 测试接缝 `AIGATE_NOTIFY_SCRIPT`：指向一个可执行脚本（如 .cmd）代替 PowerShell ——
/// e2e 探针据此断言「弹了几次、内容是什么」，而不用真去截气球（与
/// `AIGATE_BUDDY_ENDPOINT` 同一设计哲学：没有接缝就测不到的路径必须留接缝）。
#[cfg(windows)]
fn spawn_balloon(title: String, body: String) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    // 固定脚本，**不向命令行插任何用户数据**（标题/正文走环境变量，零引号地狱）。
    const PS: &str = "Add-Type -AssemblyName System.Windows.Forms; \
Add-Type -AssemblyName System.Drawing; \
$n = New-Object System.Windows.Forms.NotifyIcon; \
$n.Icon = [System.Drawing.SystemIcons]::Warning; \
$n.Visible = $true; \
$n.ShowBalloonTip(10000, $env:AIGATE_NOTIFY_TITLE, $env:AIGATE_NOTIFY_BODY, [System.Windows.Forms.ToolTipIcon]::Error); \
Start-Sleep -Seconds 12; \
$n.Dispose()";

    let mut cmd = if let Ok(script) = std::env::var("AIGATE_NOTIFY_SCRIPT") {
        // 测试接缝: 直接跑指定脚本 (不弹真气球, 探针断言用)
        std::process::Command::new(script)
    } else {
        let ps = format!(
            "{}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".to_string())
        );
        let mut c = std::process::Command::new(ps);
        c.args(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", PS]);
        c
    };
    cmd.env("AIGATE_NOTIFY_TITLE", &title)
        .env("AIGATE_NOTIFY_BODY", &body)
        .creation_flags(CREATE_NO_WINDOW);
    // 弹窗进程自己活着显示气球 (~12s), 我们不等待 —— spawn 即返回。
    // 失败静默降级: 红点仍在, 通知失败绝不能影响转发 (与埋点同一纪律)。
    match cmd.spawn() {
        Ok(_) => tracing::info!("notify: balloon spawned ({title})"),
        Err(e) => tracing::warn!("notify: balloon spawn failed: {e} (红点仍然有效)"),
    }
}

#[cfg(not(windows))]
fn spawn_balloon(_title: String, _body: String) {
    // 非 Windows: 无 PowerShell 承载, 红点照常 (本项目实际只在 Windows 交付).
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 分级表是用户拍板的口径, 钉住它 —— 改分级前必须改这里并说服自己。
    #[test]
    fn classification_matches_user_decision() {
        // 气球: 错误级(系统成因) + 额度将尽
        assert!(EventKind::BreakerOpen.balloon());
        assert!(EventKind::AccountDead.balloon());
        assert!(EventKind::QuotaLow.balloon());
        // 红点: 任何未读错误/警告 (含预警与端口顺延)
        assert!(EventKind::UpstreamFailing.dot());
        assert!(EventKind::PortFallback.dot());
        // 用户自己触发的: 两样都不给 (通知自己刚做的操作是噪音)
        assert!(!EventKind::BreakerReset.dot());
        assert!(!EventKind::BreakerReset.balloon());
        assert!(!EventKind::ProvidersReloaded.dot());
        assert!(!EventKind::ProvidersReloaded.balloon());
        // 恢复类/启动/中间态: 不通知
        for k in [
            EventKind::Started,
            EventKind::BreakerClosed,
            EventKind::UpstreamRecovered,
            EventKind::BreakerHalfOpen,
        ] {
            assert!(!k.dot() && !k.balloon(), "{k:?} 不该通知");
        }
    }

    /// 未读计数与确认: 告警类事件递增, ack 清零; 非告警类不递增。
    #[test]
    fn unread_counter_and_ack() {
        let before = unread();
        on_event(EventKind::BreakerOpen, "go", "连续失败 5 次");
        assert!(unread() > before, "熔断打开应点亮红点");
        ack();
        assert_eq!(unread(), 0, "ack 应清零");
        on_event(EventKind::ProvidersReloaded, "x", "");
        assert_eq!(unread(), 0, "用户自己的操作不该点亮红点");
        on_event(EventKind::UpstreamFailing, "go", "连续失败 3 次");
        assert!(unread() > 0, "预警级也该点亮红点");
        ack();
    }

    /// 冷却: 首次通过, 冷却窗内第二次被吞 (防不同事件连环弹)。
    /// 注意: spawn_balloon 在 cfg(test) 下不真弹 (见 spawn_balloon), 本测试只验冷却判定。
    #[test]
    fn balloon_cooldown_throttles_bursts() {
        // 重置冷却时钟 (测试可重复执行)
        if let Ok(mut g) = LAST_BALLOON.lock() {
            *g = None;
        }
        try_balloon(EventKind::BreakerOpen, "a", "x");
        assert!(LAST_BALLOON.lock().unwrap().is_some(), "首次应放行并记时");
        // 冷却窗内: 再调不改变时钟语义 (第二次直接 return, 不更新时刻)
        let t1 = *LAST_BALLOON.lock().unwrap();
        try_balloon(EventKind::AccountDead, "b", "y");
        assert_eq!(*LAST_BALLOON.lock().unwrap(), t1, "冷却窗内应被吞掉, 不刷新时刻");
    }
}
