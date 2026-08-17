//! i18n — 用户可见文案集中管理 (中/英双语).
//!
//! 所有由服务端动态生成、最终展示给用户的字符串 (错误翻译 / HTTP 状态码说明 /
//! 熔断与健康状况 / 启动错误 / 托盘菜单 / API 返回 message) 都集中在此, 便于统一维护.
//!
//! 设计取舍:
//! - 以普通函数暴露, 而非巨型 key→value map, 兼顾类型安全、零运行时开销与可发现性.
//! - 当前语言由 [`current_lang`] 读取, 进程内可通过 [`set_current_lang`] 运行时切换
//!   (供管理面板设置即时生效; 同时 [`init_lang`] 在启动时从环境变量/持久化文件加载).
//! - [`pick`] 按当前语言返回中文或英文静态串, 调用方无需感知语言.
//! - 静态 HTML 面板的可见中文标签在 `admin.html` 内通过 `data-i18n` 属性 + 前端 `t()`
//!   实现双语; 本模块的职能是收敛 Rust 侧生成的所有动态文案.

use std::sync::atomic::{AtomicBool, Ordering};

/// 界面语言.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Zh,
    En,
}

/// 当前语言运行态 (true = 英文). 用原子量以支持运行时无缝切换且线程安全.
static IS_EN: AtomicBool = AtomicBool::new(false);

/// 进程启动时调用一次: 优先级 环境变量 `AIGATE_LANG` > `lang.json` > 默认中文.
///
/// 必须在进入任何可能展示文案的逻辑之前 (且 `std::env::set_current_dir` 之后,
/// 以保证 `lang.json` 相对路径正确) 调用.
pub fn init_lang() {
    let lang = std::env::var("AIGATE_LANG")
        .ok()
        .and_then(|v| parse_lang(&v))
        .or_else(|| crate::lang::load_lang())
        .unwrap_or(Lang::Zh);
    set_current_lang(lang);
}

/// 读取当前语言.
pub fn current_lang() -> Lang {
    if IS_EN.load(Ordering::SeqCst) {
        Lang::En
    } else {
        Lang::Zh
    }
}

/// 运行时切换语言 (供管理面板持久化后即时生效).
pub fn set_current_lang(lang: Lang) {
    IS_EN.store(lang == Lang::En, Ordering::SeqCst);
}

/// 解析语言字符串 (用于环境变量 / 持久化文件). 兼容多种写法, 无法识别返回 `None`.
pub fn parse_lang(s: &str) -> Option<Lang> {
    match s.trim().to_ascii_lowercase().as_str() {
        "zh" | "zh-cn" | "zh_cn" | "chinese" | "中文" => Some(Lang::Zh),
        "en" | "en-us" | "en_us" | "english" | "英文" => Some(Lang::En),
        _ => None,
    }
}

/// 当前语言代码 (供前端注入 `window.AIGATE_LANG`).
pub fn lang_code() -> &'static str {
    match current_lang() {
        Lang::Zh => "zh-CN",
        Lang::En => "en",
    }
}

/// 按当前语言挑选文案. 中文用 `zh`, 英文用 `en`. 调用方无需感知语言.
#[inline]
pub fn pick(zh: &'static str, en: &'static str) -> &'static str {
    match current_lang() {
        Lang::Zh => zh,
        Lang::En => en,
    }
}

/// 规范化键: 转小写并去除所有非字母数字字符.
///
/// 使 `rate_limit_error` / `RateLimitError` / `rate-limit-error` / `rate limit error`
/// 等供应商五花八门的写法都能命中同一映射, 避免漏翻.
fn norm_key(s: &str) -> String {
    s.chars()
        .filter_map(|c| {
            if c.is_ascii_alphanumeric() {
                Some(c.to_ascii_lowercase())
            } else {
                None
            }
        })
        .collect()
}

/// 根据 HTTP 状态码返回说明 (覆盖纯文本响应体场景, 如上游 408 超时).
///
/// 错误 `type` 映射 ([`error_type`]) 更具体, 优先于此处; 此处作为兜底,
/// 让任何状态码 (即使上游返回纯文本而非 JSON) 都能给出可读的提示.
pub fn http_status(status: u16) -> &'static str {
    match status {
        400 => pick(
            "请求参数错误（模型不支持、参数缺失或格式错误）",
            "Bad request (unsupported model, missing or invalid params)",
        ),
        401 => pick(
            "认证失败（API Key 无效或已过期）",
            "Unauthorized (invalid or expired API key)",
        ),
        402 => pick(
            "需要付费（余额不足或需订阅）",
            "Payment required (insufficient balance or no subscription)",
        ),
        403 => pick(
            "权限不足（无权访问该模型或功能）",
            "Forbidden (no access to this model or feature)",
        ),
        404 => pick(
            "资源不存在（端点或模型名错误）",
            "Not found (wrong endpoint or model name)",
        ),
        408 => pick(
            "请求超时（上游处理超时，可能是模型思考时间长或供应商繁忙）",
            "Request timeout (upstream slow; model may be thinking or provider busy)",
        ),
        409 => pick(
            "请求冲突（并发或状态不一致）",
            "Conflict (concurrency or inconsistent state)",
        ),
        413 => pick(
            "请求体过大（上下文或文件超出上限）",
            "Payload too large (context or file exceeds limit)",
        ),
        422 => pick(
            "请求参数无法处理（字段校验失败）",
            "Unprocessable (field validation failed)",
        ),
        425 => pick(
            "请求过于提前（重试时序冲突）",
            "Too early (retry timing conflict)",
        ),
        429 => pick("请求频率超限（请稍后重试）", "Rate limited (please retry later)"),
        500 => pick(
            "服务器内部错误（供应商服务异常）",
            "Internal server error (provider fault)",
        ),
        502 => pick(
            "网关错误（上游返回无效响应）",
            "Bad gateway (upstream returned invalid response)",
        ),
        503 => pick(
            "服务不可用（供应商过载或维护中）",
            "Service unavailable (provider overloaded or in maintenance)",
        ),
        504 => pick(
            "网关超时（上游处理超时未响应）",
            "Gateway timeout (upstream did not respond)",
        ),
        _ => "",
    }
}

/// 上游错误 `type` 字段 → 说明.
///
/// 覆盖 OpenAI / 硅基流动 / DeepSeek 等主流供应商的标准 `error.type`,
/// 并兼容驼峰 / 连字符 / 下划线 / 空格等多种写法 (见 [`norm_key`]).
/// 同时供 HTTP 错误响应与流式 SSE 错误事件复用, 保证两套翻译路径一致.
pub fn error_type(t: &str) -> &'static str {
    match norm_key(t).as_str() {
        "invalidrequesterror" | "invalidrequest" => pick(
            "请求参数错误（模型不支持、参数缺失或格式错误）",
            "Invalid request (unsupported model, missing or invalid params)",
        ),
        "authenticationerror" | "authentication" | "auth" => pick(
            "认证失败（API Key 无效或已过期）",
            "Authentication failed (invalid or expired API key)",
        ),
        "invalidapikey" | "invalidkey" | "invalidauthentication" | "apikeyerror"
        | "apikeyinvalid" => pick("认证失败（API Key 无效或已过期）", "Invalid API key"),
        "apikeyexpired" | "keyexpired" => pick("认证失败（API Key 已过期）", "API key expired"),
        "ratelimiterror" | "ratelimited" | "ratelimit" | "toomanyrequests" => {
            pick("请求频率超限（请稍后重试）", "Rate limited (please retry later)")
        }
        "permissiondenied" | "permission" | "forbidden" => pick(
            "权限不足（无权访问该模型或功能）",
            "Permission denied (no access to this model or feature)",
        ),
        "contextlength exceeded" | "contextlengthexceeded" | "contextlength" | "tokenlimit"
        | "maxtokensexceeded" | "maxtokens" => {
            pick("上下文长度超限（请求内容过长）", "Context length exceeded (request too long)")
        }
        "insufficientquota" | "quotaexceeded" | "quota" => {
            pick("配额不足（账户余额耗尽）", "Insufficient quota (balance exhausted)")
        }
        "freeusagelimiterror" | "freeusagelimit" | "freeusagelimitexceeded"
        | "freelimitexceeded" | "usagelimitexceeded" | "usagelimit" => pick(
            "免费额度超限（免费档已用尽，请稍后再试或升级套餐）",
            "Free usage limit exceeded (free tier exhausted; retry later or upgrade)",
        ),
        "servererror" | "internalservererror" | "internalerror" | "serviceerror" => pick(
            "服务器内部错误（供应商服务异常）",
            "Internal server error (provider fault)",
        ),
        "serviceunavailable" | "unavailable" => pick(
            "服务不可用（供应商过载或维护中）",
            "Service unavailable (provider overloaded or in maintenance)",
        ),
        "timeout" | "requesttimeout" | "gatewaytimeout" => {
            pick("请求超时（上游处理超时）", "Request timeout (upstream slow)")
        }
        "contentfilter" | "contentpolicy" | "moderation" | "safety" => pick(
            "内容被过滤（触发内容安全策略）",
            "Content filtered (safety policy triggered)",
        ),
        "modelnotfound" | "modelerror" | "unknownmodel" => {
            pick("模型不存在或不可用", "Model not found or unavailable")
        }
        _ => "",
    }
}

/// 翻译上游英文错误正文中的常见短语为中文 (保守替换, 未知内容原样保留).
///
/// 仅替换已知的固定英文短语 (如 `Rate limit exceeded`、`Please try again later`、
/// `Error from provider (X):`), 其余 (含动态供应商名与其他原文) 保持不变, 避免误译.
/// 用于让中文用户看到的错误正文也是中文, 同时保留英文原文用于排障.
pub fn translate_upstream_message(msg: &str) -> String {
    let mut s = msg.to_string();
    // `Error from provider (X):` → `来自供应商 (X) 的错误：`
    // 仅替换前缀, 保留括号内动态供应商名.
    if let Some(rest) = s.strip_prefix("Error from provider (") {
        s = format!("来自供应商 ({rest}");
    }
    // 已知短语保守替换 (顺序无关, 互不重叠).
    s = s
        .replace("Rate limit exceeded", "请求频率超限")
        .replace("Rate limited", "请求频率受限")
        .replace("Too many requests", "请求过多")
        .replace("Please try again later", "请稍后再试")
        .replace("Please retry later", "请稍后再试")
        .replace("Please try again", "请稍后再试");
    s
}

/// 熔断状态原始值 (closed / open / half-open) → 文案 (供面板展示).
pub fn circuit_state_cn(s: &str) -> &'static str {
    match s {
        "open" => pick("熔断", "Open (tripped)"),
        "half-open" => pick("半开", "Half-open"),
        _ => pick("运行正常", "Closed"), // closed 及未知取值
    }
}

/// 健康检查状态文案 (供面板展示).
///
/// 逻辑与 [`health_level`] 保持一致: open 视为熔断断开, half-open 视为恢复中,
/// 其余按连通性 (`reachable`) 区分正常 / 不可达.
pub fn health_status_text(circuit: &str, reachable: bool) -> String {
    match circuit {
        "open" => format!(
            "{}（{}）",
            pick("熔断断开", "Open (tripped)"),
            pick(
                if reachable { "可达" } else { "不可达" },
                if reachable { "reachable" } else { "unreachable" }
            )
        ),
        "half-open" => pick("恢复中", "Recovering").to_string(),
        _ => {
            if reachable {
                pick("正常", "OK").to_string()
            } else {
                pick("不可达", "Unreachable").to_string()
            }
        }
    }
}

/// 健康检查等级 (供面板 CSS 配色): 返回 `"ok"` 或 `"error"`.
///
/// 这是给 CSS 用的技术性 code, 不随语言变化.
pub fn health_level(circuit: &str, reachable: bool) -> &'static str {
    match circuit {
        "open" | "half-open" => "error",
        _ => {
            if reachable {
                "ok"
            } else {
                "error"
            }
        }
    }
}

/// 启动期系统错误文案 (供托盘弹窗展示, 已统一中文/英文).
///
/// 返回前缀; 动态系统错误细节 `{e}` 由调用方 `format!` 拼接.
/// 集中管理可使所有用户可见的启动错误文案都来自同一处 (单一文案源).
pub fn startup_error(kind: &str) -> &'static str {
    match kind {
        "config" => pick("加载配置失败", "Failed to load config"),
        "client" => pick("创建 HTTP 客户端失败", "Failed to create HTTP client"),
        "bind" => pick("端口绑定失败", "Failed to bind port"),
        "serve" => pick("服务器运行错误", "Server runtime error"),
        "panic" => pick("意外错误，程序即将退出", "Unexpected error, shutting down"),
        _ => pick("错误", "Error"),
    }
}

/// 端口绑定失败时的附加排查提示 (与 [`startup_error`] 的 `bind` 配合).
pub fn startup_bind_hint() -> &'static str {
    pick(
        "请检查端口是否被占用或更换 PORT 环境变量。",
        "Check if the port is in use, or set a different PORT env var.",
    )
}

/// 托盘右键菜单文案 (供系统托盘菜单, 已统一双语).
pub fn tray_menu(item: &str) -> &'static str {
    match item {
        "show" => pick("打开主窗口", "Open window"),
        "console" => pick("隐藏控制台", "Hide console"),
        "quit" => pick("退出", "Quit"),
        _ => "",
    }
}

/// ─── 管理面板 API 反馈文案 (message / error 字段) ───
///
/// 这些字符串由 `/admin/api/*` 接口返回给面板, 作为 toast / 提示展示给用户,
/// 按当前语言双语化. 带动态参数的用 [`fmt_msg!`] 按语言选择字面量模板; 底层透传的
/// 系统错误 `e` 不翻译, 仅翻译中文前缀.

/// 按当前语言 format: 解决 `format!` 首参必须是字面量的限制 (无法传 `pick` 结果).
macro_rules! fmt_msg {
    ($zh:literal, $en:literal $(, $arg:expr)* $(,)?) => {{
        if current_lang() == Lang::En {
            format!($en $(, $arg)*)
        } else {
            format!($zh $(, $arg)*)
        }
    }};
}

/// 清空日志反馈.
pub fn msg_logs_cleared(count: usize) -> String {
    fmt_msg!("已清空 {} 条记录", "Cleared {} log entries", count)
}

/// 保存 providers.json 时缺少 `json` 字段.
pub fn msg_missing_json_field() -> &'static str {
    pick("缺少 json 字段", "Missing 'json' field")
}

/// 写入文件失败 (透传底层系统错误 `e`, 不翻译).
pub fn msg_write_file_failed(e: &dyn std::fmt::Display) -> String {
    fmt_msg!("写入文件失败: {}", "Failed to write file: {}", e)
}

/// providers.json 保存并热重载成功.
pub fn msg_config_saved() -> &'static str {
    pick("配置已保存并重载", "Config saved and reloaded")
}

/// providers.json 从磁盘重载成功.
pub fn msg_config_reloaded() -> &'static str {
    pick("配置已重载", "Config reloaded")
}

/// 清除某 API Key.
pub fn msg_key_cleared(env: &str) -> String {
    fmt_msg!("已清除 {}", "Cleared {}", env)
}

/// 更新某 API Key.
pub fn msg_key_updated(env: &str) -> String {
    fmt_msg!("已更新 {}", "Updated {}", env)
}

/// 未找到指定供应商.
pub fn msg_provider_not_found(name: &str) -> String {
    fmt_msg!("未找到供应商: {}", "Provider not found: {}", name)
}

/// 从上游拉取模型后的汇总反馈.
pub fn msg_models_fetched(ids: usize, added: usize, skipped: usize) -> String {
    fmt_msg!(
        "已从上游拉取 {} 个模型, 新增 {} 个, 跳过 {} 个已存在",
        "Fetched {} models from upstream, added {}, skipped {} existing",
        ids, added, skipped
    )
}

/// 生成模拟日志反馈.
pub fn msg_mock_generated(count: usize) -> String {
    fmt_msg!("已生成 {} 条模拟请求日志", "Generated {} mock request logs", count)
}

/// 手动重置某供应商熔断.
pub fn msg_circuit_reset(p: &str) -> String {
    fmt_msg!("已重置 {} 的熔断", "Reset circuit breaker for {}", p)
}

/// 某供应商无熔断记录.
pub fn msg_circuit_none(p: &str) -> String {
    fmt_msg!("{} 暂无熔断记录", "No circuit record for {}", p)
}

/// 语言持久化失败.
pub fn msg_lang_save_failed(e: &dyn std::fmt::Display) -> String {
    fmt_msg!("语言保存失败: {}", "Failed to save language: {}", e)
}

/// 提交了不支持的语言代码.
pub fn msg_lang_unsupported(lang: &str) -> String {
    fmt_msg!("不支持的语言: {}", "Unsupported language: {}", lang)
}

/// 上游流在上游返回 finish_reason/[DONE] 之前结束, 响应可能被截断.
pub fn msg_stream_truncated() -> &'static str {
    pick(
        "stream 在上游返回 finish_reason/[DONE] 之前结束（响应可能被截断）",
        "Stream ended without upstream finish_reason/[DONE] (response likely truncated)",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// 所有 i18n 测试共享全局语言状态, 需串行执行以避免互相改写 `current_lang`
    /// 导致断言交错失败. 用进程级 Mutex 串行化 (cargo test 默认多线程并行).
    static I18N_TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn error_type_normalizes_variants() {
        let _g = I18N_TEST_LOCK.lock().unwrap();
        set_current_lang(Lang::Zh);
        // 同一语义的多种写法应命中同一中文说明
        assert_eq!(error_type("rate_limit_error"), error_type("RateLimitError"));
        assert_eq!(error_type("rate-limit-error"), error_type("ratelimiterror"));
        assert!(!error_type("RateLimitError").is_empty());
        assert!(error_type("unknown_type_xyz").is_empty());
    }

    #[test]
    fn circuit_and_health_mapping() {
        let _g = I18N_TEST_LOCK.lock().unwrap();
        set_current_lang(Lang::Zh);
        assert_eq!(circuit_state_cn("open"), "熔断");
        assert_eq!(circuit_state_cn("half-open"), "半开");
        assert_eq!(circuit_state_cn("closed"), "运行正常");
        assert_eq!(health_level("open", true), "error");
        assert_eq!(health_level("closed", true), "ok");
        assert_eq!(health_level("closed", false), "error");
        assert!(health_status_text("closed", true).contains("正常"));
        assert!(health_status_text("open", false).contains("熔断断开"));
    }

    #[test]
    fn startup_and_tray_text() {
        let _g = I18N_TEST_LOCK.lock().unwrap();
        set_current_lang(Lang::Zh);
        assert_eq!(startup_error("config"), "加载配置失败");
        assert_eq!(startup_error("bind"), "端口绑定失败");
        assert_eq!(startup_error("panic"), "意外错误，程序即将退出");
        assert!(startup_bind_hint().contains("PORT"));
        assert_eq!(tray_menu("show"), "打开主窗口");
        assert_eq!(tray_menu("quit"), "退出");
        assert_eq!(tray_menu("unknown"), "");
    }

    #[test]
    fn bilingual_switch() {
        let _g = I18N_TEST_LOCK.lock().unwrap();
        set_current_lang(Lang::En);
        assert_eq!(
            error_type("rate_limit_error"),
            "Rate limited (please retry later)"
        );
        assert_eq!(circuit_state_cn("open"), "Open (tripped)");
        assert_eq!(health_level("open", true), "error");
        assert!(health_status_text("closed", true).contains("OK"));
        assert!(health_status_text("open", true).contains("reachable"));
        assert_eq!(startup_error("bind"), "Failed to bind port");
        assert_eq!(tray_menu("quit"), "Quit");
        // 还原默认, 避免影响后续 (串行) 测试
        set_current_lang(Lang::Zh);
        assert_eq!(error_type("rate_limit_error"), "请求频率超限（请稍后重试）");
    }

    #[test]
    fn stream_truncated_i18n() {
        let _g = I18N_TEST_LOCK.lock().unwrap();
        set_current_lang(Lang::Zh);
        assert_eq!(
            msg_stream_truncated(),
            "stream 在上游返回 finish_reason/[DONE] 之前结束（响应可能被截断）"
        );
        set_current_lang(Lang::En);
        assert!(msg_stream_truncated()
            .contains("Stream ended without upstream finish_reason/[DONE]"));
        set_current_lang(Lang::Zh);
    }
}
