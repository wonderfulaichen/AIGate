//! 调用来源与请求关联键识别 (2026-10-21, 借鉴 WaLiAPI 的可观测性设计).
//!
//! # 为什么需要
//! 用户报障「我明明没调用这个供应商的模型，结果却突然显示有调用」。当时面板只能
//! 给出「某供应商某模型有 N 条记录」，而**是谁发的、是不是同一次请求的重试、能不能
//! 拿去和上游对账**全都无法回答 —— 只能靠翻 `aigate.log` 猜。本模块补的正是这三样。
//!
//! # 两条纪律
//! 1. **来源名是推断，不是身份**。UA 与自定义头都能被调用方任意伪造，故本模块的结果
//!    **只用于展示与归因**，不参与任何路由、鉴权或计费决策。无法识别时给 `None`
//!    （前端显示 `—`），不硬造"未知客户端"之类的假值。
//! 2. **关联键必须过滤后才回显**。直接把客户端传来的 `X-Request-Id` 塞进响应头会
//!    引入头注入风险（CRLF / 控制字符），故只接受可见 ASCII 且限长，非法值视为未携带。
//!
//! # 与 `traces.rs` 的分工
//! `traces::SessionRef` 回答「这是同一个会话的第几跳」（会话连续性）；
//! 本模块回答「这条请求是谁发的、它的对账号是多少」（单条请求的来源与身份）。
//! 两者互补：排查"多出来的调用"看来源，排查"这一轮做了什么"看会话链。

use axum::http::HeaderMap;

/// 客户端可显式声明自己名字的头（比 UA 可靠：UA 常是库的默认串）。
pub const CLIENT_NAME_HEADER: &str = "x-aigate-client";

/// 关联键长度上限（对齐 nginx `X-Request-Id` 的 128 惯例）。
const MAX_REQUEST_ID_LEN: usize = 128;

/// 校验候选关联键：非空、限长、全部为可见 ASCII (0x21..=0x7E)。
///
/// 拒绝空格与控制字符是**防头注入**（CRLF 折行能伪造响应头）；
/// 拒绝非 ASCII 避免编码歧义（回显时按字节写入，非 ASCII 会产生不一致的形态）。
/// 非法值一律当作「未携带」，由调用方生成新值。
pub fn sanitize_request_id(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > MAX_REQUEST_ID_LEN {
        return None;
    }
    if !trimmed.bytes().all(|b| (0x21..=0x7E).contains(&b)) {
        return None;
    }
    Some(trimmed.to_string())
}

/// 从请求头解析关联键：`x-request-id` → `x-aigate-trace-id` → 生成新值。
///
/// 两个头都非法时**生成新值**而不是把非法值透传下去 —— 回显一个带换行的值
/// 比丢弃它危险得多。
pub fn resolve_request_id(headers: &HeaderMap) -> String {
    for name in ["x-request-id", "x-aigate-trace-id"] {
        if let Some(v) = headers.get(name).and_then(|h| h.to_str().ok()) {
            if let Some(valid) = sanitize_request_id(v) {
                return valid;
            }
        }
    }
    new_request_id()
}

/// 生成一个本地关联键。
///
/// 为什么不用 UUID：本项目是**单 exe 零外部依赖**的交付形态，不为一个日志字段引入
/// `uuid` crate。这里要的是"同一进程内不重复、可按时间排序"，故用
/// `req_<毫秒时间戳36进制><原子计数器36进制>` 即可，且天然按时间递增、便于肉眼排序。
pub fn new_request_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("req_{ms:x}{n:04x}")
}

/// 从请求头识别调用来源的友好名。
///
/// 优先级：显式声明头（最可信）→ User-Agent 特征匹配 → UA 首段产品名 → `None`。
///
/// ⚠️ 全部是**推断**。UA 可被任意伪造，故结果只用于展示与归因，
/// 不参与路由/鉴权/计费（见模块文档纪律 1）。
pub fn detect_client_name(headers: &HeaderMap) -> Option<String> {
    if let Some(explicit) = headers
        .get(CLIENT_NAME_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        return Some(truncate(explicit));
    }
    let ua = headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())?;
    if let Some(name) = known_client(ua) {
        return Some(name.to_string());
    }
    // 兜底：取 UA 首段产品名（到第一个空格或 `/` 为止）。
    let head = ua.split([' ', '/']).next().unwrap_or(ua);
    if head.is_empty() {
        None
    } else {
        Some(truncate(head))
    }
}

/// 已知接入软件的 UA 特征 → 规范显示名。
///
/// 顺序即特异性：先匹配更具体的（`claude-code` 先于裸 `claude`），
/// 否则会被短特征抢先命中而报错名字。
fn known_client(ua: &str) -> Option<&'static str> {
    let ua = ua.to_ascii_lowercase();
    const RULES: &[(&str, &str)] = &[
        ("claude-code", "Claude Code"),
        ("claudecode", "Claude Code"),
        ("codex", "Codex"),
        ("workbuddy", "WorkBuddy"),
        ("codebuddy", "CodeBuddy"),
        ("cursor", "Cursor"),
        ("windsurf", "Windsurf"),
        ("trae", "Trae"),
        ("vscode", "VS Code"),
        ("visual-studio-code", "VS Code"),
        ("jetbrains", "JetBrains"),
        ("intellij", "JetBrains"),
        ("goose", "Goose"),
        ("cline", "Cline"),
        ("roo-code", "Roo Code"),
        ("roocode", "Roo Code"),
        ("continue", "Continue"),
        ("opencode", "OpenCode"),
        ("gemini-cli", "Gemini CLI"),
        ("openbitfun", "OpenBitFun"),
        ("claude", "Claude"),
        ("curl", "curl"),
        ("python-requests", "Python Requests"),
        ("python-httpx", "Python httpx"),
        ("okhttp", "OkHttp"),
        ("axios", "Axios"),
        ("node-fetch", "Node Fetch"),
        ("undici", "Undici"),
        ("apache-httpclient", "Apache HttpClient"),
    ];
    RULES
        .iter()
        .find(|(needle, _)| ua.contains(needle))
        .map(|(_, name)| *name)
}

/// 按字符数截断到 60，避免异常长的 UA 撑爆表格列宽。
fn truncate(name: &str) -> String {
    name.chars().take(60).collect()
}

/// 从上游响应头里取上游自己的请求号，供与上游对账。
///
/// 供应商各报各的名字（`x-request-id` / `request-id` / `x-trace-id`），
/// 这里只取第一个命中的非空值。**不做 sanitize 之外的处理**：它是上游原文，
/// 只落日志不回显给客户端，故保留原样更利于对账。
pub fn upstream_request_id(headers: &HeaderMap) -> Option<String> {
    const NAMES: &[&str] = &[
        "x-request-id",
        "request-id",
        "x-trace-id",
        "x-amzn-requestid",
        "cf-ray",
    ];
    for name in NAMES {
        if let Some(v) = headers.get(*name).and_then(|h| h.to_str().ok()) {
            let v = v.trim();
            if !v.is_empty() {
                return Some(truncate(v));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{HeaderName, HeaderValue};

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.insert(
                HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        map
    }

    /// 关联键的过滤必须挡住头注入与编码歧义 —— 这是安全判据，不是格式偏好。
    #[test]
    fn sanitize_rejects_injection_shapes() {
        assert_eq!(sanitize_request_id("abc-123_XYZ.09"), Some("abc-123_XYZ.09".into()));
        // 空 / 超长
        assert_eq!(sanitize_request_id(""), None);
        assert_eq!(sanitize_request_id("   "), None);
        assert_eq!(sanitize_request_id(&"a".repeat(129)), None);
        assert_eq!(sanitize_request_id(&"a".repeat(128)).map(|s| s.len()), Some(128));
        // 空格 / 制表符 / 换行 (CRLF 折行能伪造响应头)
        assert_eq!(sanitize_request_id("abc def"), None);
        assert_eq!(sanitize_request_id("abc\tdef"), None);
        assert_eq!(sanitize_request_id("abc\r\ndef"), None);
        // 非 ASCII
        assert_eq!(sanitize_request_id("请求id"), None);
    }

    #[test]
    fn resolve_prefers_standard_header_and_never_echoes_invalid() {
        // 标准头优先于自定义兼容头
        let m = headers(&[("x-request-id", "std-1"), ("x-aigate-trace-id", "legacy-1")]);
        assert_eq!(resolve_request_id(&m), "std-1");
        // 只有兼容头时用它
        let m = headers(&[("x-aigate-trace-id", "legacy-2")]);
        assert_eq!(resolve_request_id(&m), "legacy-2");
        // 非法标准头 → 回落到兼容头（而不是把非法值透传）
        let m = headers(&[("x-request-id", "bad value"), ("x-aigate-trace-id", "ok-1")]);
        assert_eq!(resolve_request_id(&m), "ok-1");
        // 全非法 → 生成新值，且新值本身必须能通过 sanitize
        let m = headers(&[("x-request-id", "bad value")]);
        let got = resolve_request_id(&m);
        assert_ne!(got, "bad value");
        assert_eq!(sanitize_request_id(&got), Some(got.clone()));
    }

    #[test]
    fn new_request_id_is_unique_and_sanitizable() {
        let a = new_request_id();
        let b = new_request_id();
        assert_ne!(a, b, "同一进程内两次生成不得相同");
        assert_eq!(sanitize_request_id(&a), Some(a.clone()));
    }

    /// 来源识别：显式头 > UA 特征 > UA 首段 > None。
    #[test]
    fn detect_client_name_priority_chain() {
        // 显式声明最可信
        assert_eq!(
            detect_client_name(&headers(&[
                ("x-aigate-client", "MyAgent"),
                ("user-agent", "curl/8.0"),
            ])),
            Some("MyAgent".to_string())
        );
        // UA 命中已知特征
        assert_eq!(
            detect_client_name(&headers(&[("user-agent", "claude-code/1.2.3 (darwin)")])),
            Some("Claude Code".to_string())
        );
        // 未收录的 UA → 取首段产品名（不是硬造规范名）
        assert_eq!(
            detect_client_name(&headers(&[("user-agent", "OpenAI/1.0 Python")])),
            Some("OpenAI".to_string()),
            "未收录的 UA 走首段兜底，不得硬造规范名"
        );
        // 未知 UA → 取首段产品名
        assert_eq!(
            detect_client_name(&headers(&[("user-agent", "some-tool/2.0 (compatible)")])),
            Some("some-tool".to_string())
        );
        // 无 UA 无显式头 → None（前端显示 —，不硬造）
        assert_eq!(detect_client_name(&HeaderMap::new()), None);
        assert_eq!(detect_client_name(&headers(&[("user-agent", "   ")])), None);
    }

    /// 兜底名过长要截断，否则一条异常 UA 能把表格列撑爆。
    #[test]
    fn long_client_name_is_truncated() {
        let long = "x".repeat(200);
        let got = detect_client_name(&headers(&[("user-agent", &long)])).unwrap();
        assert_eq!(got.chars().count(), 60);
    }

    #[test]
    fn upstream_request_id_picks_first_known_header() {
        assert_eq!(
            upstream_request_id(&headers(&[("x-request-id", "up-1")])),
            Some("up-1".to_string())
        );
        assert_eq!(
            upstream_request_id(&headers(&[("cf-ray", "ray-9"), ("x-request-id", "up-2")])),
            Some("up-2".to_string()),
            "多个头同时存在时按固定优先级取，不随头顺序抖动"
        );
        assert_eq!(upstream_request_id(&HeaderMap::new()), None);
        assert_eq!(upstream_request_id(&headers(&[("x-request-id", "  ")])), None);
    }
}
