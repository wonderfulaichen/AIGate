//! 高峰 / 空闲时段配置: 读写工作目录下的 `peak_schedule.json`.
//!
//! 背景: 部分供应商（如 DeepSeek）按时段计费 —— 高峰时段用标准价, 其余时段用空闲价.
//! 各家的时段规则不同, 且会调整, 因此**不在代码里写死**, 改由本模块持久化配置,
//! 用户可在面板「设置 → 分时段计费」直接改周几与时间段.
//!
//! 语义: 仅当 `enabled` 为 true 且当天星期命中 `weekdays`、当天时刻落在某个
//! `windows` 区间内时, 判为高峰; 其余情况一律空闲. 未配置空闲价的模型
//! （`*_per_m_offpeak` 为 0）在空闲时段自动回退标准价, 故不受本配置影响.
//!
//! 运行时通过进程内全局 [`PeakSchedule`] 生效: 启动时加载一次, 面板保存后热更新,
//! 无需重启. 保持 [`is_peak`] 签名不变, 使 `pricing` / `rollup` 无需感知配置来源.

use std::sync::{OnceLock, RwLock};

use serde::{Deserialize, Serialize};

const PEAK_FILE: &str = "peak_schedule.json";

/// 单条高峰时间窗（`HH:MM` 本地时刻, 左闭右开）.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeakWindow {
    #[serde(default)]
    pub start: String,
    #[serde(default)]
    pub end: String,
}

impl PeakWindow {
    /// 解析为「自 00:00 起的分钟」区间, 格式非法或区间为空时返回 `None`.
    pub fn as_minutes(&self) -> Option<(u32, u32)> {
        let (s, e) = (parse_hm(&self.start)?, parse_hm(&self.end)?);
        if s < e { Some((s, e)) } else { None }
    }
}

/// 高峰时段配置.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeakSchedule {
    /// 是否启用分时段计费. false = 全天按标准价（等价于关闭高峰判定）.
    #[serde(default = "default_enabled")]
    pub enabled: bool,
    /// 判定所用时区相对 UTC 的偏移小时数. 默认 +8（北京时间）.
    #[serde(default = "default_tz")]
    pub tz_offset_hours: i32,
    /// 哪些星期算高峰. 1=周一 … 7=周日. 未列出的星期全天按空闲计.
    #[serde(default = "default_weekdays")]
    pub weekdays: Vec<u8>,
    /// 高峰时间窗列表, 任一命中即高峰.
    #[serde(default = "default_windows")]
    pub windows: Vec<PeakWindow>,
}

fn default_enabled() -> bool {
    true
}
fn default_tz() -> i32 {
    8
}
/// 默认每天都是高峰日 —— 与历史硬编码行为一致, 避免升级后费用口径突变.
fn default_weekdays() -> Vec<u8> {
    vec![1, 2, 3, 4, 5, 6, 7]
}
fn default_windows() -> Vec<PeakWindow> {
    vec![
        PeakWindow { start: "09:00".into(), end: "12:00".into() },
        PeakWindow { start: "14:00".into(), end: "18:00".into() },
    ]
}

impl Default for PeakSchedule {
    fn default() -> Self {
        Self {
            enabled: default_enabled(),
            tz_offset_hours: default_tz(),
            weekdays: default_weekdays(),
            windows: default_windows(),
        }
    }
}

/// 解析 `HH:MM` 为自 00:00 起的分钟数. 越界或格式错误返回 `None`.
fn parse_hm(s: &str) -> Option<u32> {
    let (h, m) = s.trim().split_once(':')?;
    let (h, m): (u32, u32) = (h.trim().parse().ok()?, m.trim().parse().ok()?);
    if h > 23 || m > 59 {
        return None;
    }
    Some(h * 60 + m)
}

/// 给定时间戳（秒, Unix）在该配置下是否为高峰.
///
/// 时区偏移仅做整数小时平移（不引入 chrono）; 星期由 Unix 纪元日推得
/// （1970-01-01 为周四）.
pub fn is_peak_with(ts: u64, sched: &PeakSchedule) -> bool {
    if !sched.enabled {
        return false;
    }
    let local = (ts as i64) + (sched.tz_offset_hours as i64) * 3600;
    let days = local.div_euclid(86400);
    let secs_of_day = local.rem_euclid(86400);
    // 1970-01-01 = 周四 = 4（ISO: 1=周一 … 7=周日）
    let dow = ((days + 3).rem_euclid(7) + 1) as u8;
    if !sched.weekdays.contains(&dow) {
        return false;
    }
    let mins = (secs_of_day / 60) as u32;
    sched
        .windows
        .iter()
        .any(|w| w.as_minutes().is_some_and(|(s, e)| mins >= s && mins < e))
}

/// 当前生效的配置（进程内全局, 启动时加载, 面板保存后热更新）.
fn cell() -> &'static RwLock<PeakSchedule> {
    static SCHEDULE: OnceLock<RwLock<PeakSchedule>> = OnceLock::new();
    SCHEDULE.get_or_init(|| RwLock::new(load_config()))
}

/// 判定给定时间戳是否为高峰（按当前生效配置）.
pub fn is_peak(ts: u64) -> bool {
    let sched = cell().read().unwrap_or_else(|e| e.into_inner());
    is_peak_with(ts, &sched)
}

/// 当前配置快照.
pub fn current() -> PeakSchedule {
    cell().read().unwrap_or_else(|e| e.into_inner()).clone()
}

/// 热更新生效配置（调用方负责落盘）.
pub fn set_current(sched: PeakSchedule) {
    *cell().write().unwrap_or_else(|e| e.into_inner()) = sched;
}

/// 从 `peak_schedule.json` 读取配置. 文件不存在/损坏/字段缺失时回退默认.
pub fn load_config() -> PeakSchedule {
    std::fs::read_to_string(PEAK_FILE)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// 持久化配置并热更新生效.
pub fn save_config(sched: &PeakSchedule) -> std::io::Result<()> {
    let text = serde_json::to_string_pretty(sched)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(PEAK_FILE, text)?;
    set_current(sched.clone());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Unix 第 `day` 天 的 UTC `hour` 时 → 时间戳.
    /// 北京时刻 = UTC + 8h.
    fn utc(day: i64, hour: u32) -> u64 {
        ((day * 86400) + (hour as i64) * 3600) as u64
    }

    fn sched(weekdays: Vec<u8>, windows: Vec<(&str, &str)>) -> PeakSchedule {
        PeakSchedule {
            enabled: true,
            tz_offset_hours: 8,
            weekdays,
            windows: windows
                .into_iter()
                .map(|(s, e)| PeakWindow { start: s.into(), end: e.into() })
                .collect(),
        }
    }

    #[test]
    fn default_matches_legacy_windows() {
        let s = PeakSchedule::default();
        assert!(s.enabled);
        assert_eq!(s.weekdays.len(), 7);
        assert_eq!(s.windows.len(), 2);
        assert_eq!(s.windows[0].as_minutes(), Some((540, 720)));
        assert_eq!(s.windows[1].as_minutes(), Some((840, 1080)));
    }

    #[test]
    fn weekday_computed_from_unix_epoch() {
        // 1970-01-01 = 周四 = 4
        let s = sched(vec![4], vec![("09:00", "12:00")]);
        // day0 UTC 01:00 = 北京 09:00 → 周四且窗口内 → 高峰
        assert!(is_peak_with(utc(0, 1), &s));
        // day0 UTC 09:00 = 北京 17:00 → 不在窗口
        assert!(!is_peak_with(utc(0, 9), &s));
        // day1 = 周五 → 不在 weekdays → 空闲（即使时刻相同）
        assert!(!is_peak_with(utc(1, 1), &s));
    }

    #[test]
    fn disabled_never_peak() {
        let mut s = sched(vec![1, 2, 3, 4, 5, 6, 7], vec![("00:00", "23:59")]);
        s.enabled = false;
        assert!(!is_peak_with(utc(0, 1), &s));
    }

    #[test]
    fn window_boundaries_are_half_open() {
        let s = sched(vec![4], vec![("09:00", "12:00")]);
        // 北京 09:00 → 命中
        assert!(is_peak_with(utc(0, 1), &s));
        // 北京 11:59 → 命中
        assert!(is_peak_with(utc(0, 3) + 59 * 60, &s));
        // 北京 12:00 → 右开, 不命中
        assert!(!is_peak_with(utc(0, 4), &s));
    }

    #[test]
    fn invalid_window_ignored() {
        let s = sched(vec![4], vec![("12:00", "09:00")]);
        assert!(!is_peak_with(utc(0, 1), &s));
        assert_eq!(PeakWindow { start: "25:00".into(), end: "26:00".into() }.as_minutes(), None);
        assert_eq!(PeakWindow { start: "bad".into(), end: "10:00".into() }.as_minutes(), None);
    }

    #[test]
    fn multiple_windows_any_match() {
        let s = sched(vec![4], vec![("09:00", "12:00"), ("14:00", "18:00")]);
        assert!(is_peak_with(utc(0, 2), &s)); // 北京 10:00
        assert!(!is_peak_with(utc(0, 5), &s)); // 北京 13:00
        assert!(is_peak_with(utc(0, 7), &s)); // 北京 15:00
    }

    #[test]
    fn custom_timezone_shift() {
        let mut s = sched(vec![4], vec![("09:00", "12:00")]);
        s.tz_offset_hours = 0;
        assert!(is_peak_with(utc(0, 9), &s)); // UTC 09:00 命中
        assert!(!is_peak_with(utc(0, 1), &s)); // UTC 01:00 不命中
    }

    #[test]
    fn missing_fields_fall_back_to_default() {
        let s: PeakSchedule = serde_json::from_str("{}").unwrap();
        assert_eq!(s, PeakSchedule::default());
    }
}
