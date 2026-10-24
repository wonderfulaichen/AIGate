//! opencode Go / Go Plus 套餐额度表与剩余量计算。
//!
//! # 为什么需要这个模块
//!
//! opencode 是**套餐制**（Go $10/月、Go Plus $40/月），额度以**美元**计，
//! 且每个模型有**独立的月额度**（GLM-5.3 是 $15，GLM-5.3-Flash 是 $60）。
//! 三档限额：**5 小时 = 月额度 20%、周 = 50%、月 = 100%**。
//!
//! # 为什么没有"查额度"的接口
//!
//! 官方**不提供**额度查询端点。实际机制是：每条响应在
//! `usage.opencode.allowance` 里报告「本请求计了多少额度（美元）」，
//! 用户自己去 Console 页面看总账。因此本项目的做法是
//! **在本地累加 allowance**，再对照本模块的月额度表算剩余。
//!
//! 这与参考项目（WALiAPI）的选择一致：它对 6 个 provider 逐个声明 `supports_quota`，
//! 只有 Codex / Gemini 为 true（它们有 `GET /wham/usage` 这类专用端点），
//! WorkBuddy / Kimi / Grok / Qoder 一律 false。
//!
//! # ⚠️ 这张表是外部数据，会变
//!
//! 来源：<https://opencode.ai/v2/docs/console/go#usage-limits>
//! 抄录日期：2026-10-24。
//!
//! 官方明确写「Usage limits may change as we learn from early usage and feedback」。
//! 调价/调额度后本表会过期，表现为面板上的百分比与实际控制台不一致。
//! **更新方式：改下面这张表，同步改 `TABLE_DATE`。**
//! 面板会把 `TABLE_DATE` 显示出来，让用户知道这份数据的时效。

/// 本模块额度表的抄录日期。面板会显示它，便于判断是否过期。
pub const TABLE_DATE: &str = "2026-10-24";

/// 额度表来源 URL（面板 title 里给出，便于用户核对）。
pub const TABLE_SOURCE: &str = "https://opencode.ai/v2/docs/console/go#usage-limits";

/// 单个模型的月度额度（美元）。
///
/// 只列官方文档给出的数值。**文档里没有的模型返回 `None`** ——
/// 不能猜一个默认值：猜高了用户以为还能用，猜低了会误报额度耗尽。
pub struct ModelQuota {
    /// 上游模型 ID（与 `usage` 里 `model` 字段、我们的 `upstream_model` 一致）。
    pub model: &'static str,
    /// 月额度（美元）。
    pub monthly_usd: f64,
}

/// 官方文档的模型额度表（按 `docs/console/go#usage-limits` 的表格抄录）。
///
/// 顺序与官方一致，便于逐行核对。免费/不限量的模型不入表（见 [`monthly_limit`]）。
const QUOTAS: &[ModelQuota] = &[
    ModelQuota { model: "glm-5.3-flash", monthly_usd: 60.0 },
    ModelQuota { model: "glm-5.3", monthly_usd: 15.0 },
    ModelQuota { model: "glm-5.2", monthly_usd: 60.0 },
    ModelQuota { model: "kimi-k3.1-pro", monthly_usd: 15.0 },
    ModelQuota { model: "kimi-k3", monthly_usd: 15.0 },
    ModelQuota { model: "kimi-k2.7-code", monthly_usd: 60.0 },
    ModelQuota { model: "longcat-2.0", monthly_usd: 60.0 },
    ModelQuota { model: "step-5-preview", monthly_usd: 15.0 },
    ModelQuota { model: "mimo-v2.6-flash", monthly_usd: 60.0 },
    ModelQuota { model: "mimo-v2.6-pro", monthly_usd: 15.0 },
    ModelQuota { model: "mimo-v2.5", monthly_usd: 60.0 },
    ModelQuota { model: "mimo-v2.5-pro", monthly_usd: 15.0 },
    ModelQuota { model: "minimax-m3.1-flash", monthly_usd: 20.0 },
    ModelQuota { model: "minimax-m3", monthly_usd: 60.0 },
    ModelQuota { model: "muse-spark-1.3-contributor", monthly_usd: 60.0 },
    ModelQuota { model: "muse-spark-1.2-contributor", monthly_usd: 60.0 },
    ModelQuota { model: "qwen3.8-max", monthly_usd: 15.0 },
    ModelQuota { model: "qwen3.8-flash", monthly_usd: 30.0 },
    ModelQuota { model: "qwen3.7-plus", monthly_usd: 60.0 },
    ModelQuota { model: "deepseek-v4.1-flash", monthly_usd: 80.0 },
    ModelQuota { model: "deepseek-v4-pro", monthly_usd: 15.0 },
    ModelQuota { model: "hy4-preview", monthly_usd: 30.0 },
    ModelQuota { model: "hy3", monthly_usd: 60.0 },
    ModelQuota { model: "grok-4.7", monthly_usd: 15.0 },
    ModelQuota { model: "gpt-6-luna", monthly_usd: 15.0 },
    ModelQuota { model: "gpt-5.6-luna", monthly_usd: 15.0 },
    ModelQuota { model: "claude-haiku-5-5", monthly_usd: 15.0 },
    ModelQuota { model: "gemini-4-argon", monthly_usd: 30.0 },
];

/// 明确**不限量**的模型（官方标 `Unlimited`，限时免费）。
///
/// 单列出来而不是"查不到就当不限量"：查不到与不限量是两件事，
/// 前者是「我们不知道」，后者是「官方说不限」。混用会让新模型被误标成不限量。
const UNLIMITED: &[&str] = &["longcat-2.5-preview-free", "beam-free"];

/// 查某模型的月额度（美元）。
///
/// 返回：
/// - `Some(Some(usd))` —— 有限额度，值为月额度；
/// - `Some(None)` —— 官方标**不限量**；
/// - `None` —— **表里没有这个模型**（未采集，不是 0）。
///
/// 用嵌套 `Option` 而不是 `0.0` 表示不限量：`0.0` 会被算成"一点额度都没有"。
pub fn monthly_limit(model: &str) -> Option<Option<f64>> {
    if UNLIMITED.contains(&model) {
        return Some(None);
    }
    QUOTAS
        .iter()
        .find(|q| q.model == model)
        .map(|q| Some(q.monthly_usd))
}

/// 三档窗口的额度上限（美元）。
///
/// 官方口径：5 小时 = 月额度 20%、周 = 50%、月 = 100%。
/// 返回 `None` 表示该模型不限量或不在表里，调用方据此不画进度条。
pub struct WindowLimits {
    pub five_hour: f64,
    pub weekly: f64,
    pub monthly: f64,
}

/// 按官方比例算三档上限。模型不在表里 → `None`。
pub fn window_limits(model: &str) -> Option<WindowLimits> {
    match monthly_limit(model)? {
        Some(monthly) => Some(WindowLimits {
            five_hour: monthly * 0.20,
            weekly: monthly * 0.50,
            monthly,
        }),
        // 不限量：没有上限可算。
        None => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 额度表必须与官方文档一致 —— 抽查几条有代表性的（含最深/最浅额度的模型）。
    ///
    /// 为什么抽查而不是全量断言：全量断言等于把表抄两遍，改表时要改两处，
    /// 反而容易漂移。抽查覆盖"额度最小/最大/中间"三档即可发现整表被改成同一个值。
    #[test]
    fn quota_table_matches_official_values() {
        assert_eq!(monthly_limit("deepseek-v4.1-flash"), Some(Some(80.0)),
                   "官方表里额度最高的模型（$80）");
        assert_eq!(monthly_limit("deepseek-v4-pro"), Some(Some(15.0)),
                   "官方表里额度最低的一档（$15）");
        assert_eq!(monthly_limit("glm-5.3-flash"), Some(Some(60.0)));
        assert_eq!(monthly_limit("glm-5.3"), Some(Some(15.0)),
                   "GLM-5.3 与 GLM-5.3-Flash 额度差 4 倍，不能混");
        assert_eq!(monthly_limit("minimax-m3.1-flash"), Some(Some(20.0)),
                   "非整十档（$20）要单独确认");
        assert_eq!(monthly_limit("qwen3.8-flash"), Some(Some(30.0)));
    }

    /// **表里没有的模型必须是 `None`，不能猜。**
    ///
    /// 猜默认值的两个方向都会害人：猜高了用户以为还能用，猜低了会误报额度耗尽。
    #[test]
    fn unknown_model_is_none_not_guessed() {
        assert_eq!(monthly_limit("some-brand-new-model"), None,
                   "表里没有 → None（未采集），不得猜一个默认额度");
        assert_eq!(window_limits("some-brand-new-model").is_none(), true);
    }

    /// 不限量模型（官方标 Unlimited）要与"查不到"区分开。
    #[test]
    fn unlimited_is_distinct_from_unknown() {
        assert_eq!(monthly_limit("beam-free"), Some(None),
                   "不限量 = Some(None)，是'已采集且确认不限'");
        assert_eq!(monthly_limit("not-in-table"), None,
                   "查不到 = None，是'未采集'");
        assert_ne!(monthly_limit("beam-free"), monthly_limit("not-in-table"),
                   "两者必须能区分（混用会让新模型被误标成不限量）");
        // 不限量没有上限可算 → 不画进度条
        assert!(window_limits("beam-free").is_none());
    }

    /// 三档比例必须是 20% / 50% / 100%（官方口径）。
    ///
    /// 这条钉住的是**比例**而不是绝对值 —— 官方若改了比例，这里会红。
    #[test]
    fn window_limits_follow_official_ratios() {
        let l = window_limits("glm-5.3").expect("GLM-5.3 应在表里");
        assert_eq!(l.monthly, 15.0);
        assert_eq!(l.five_hour, 3.0, "5 小时 = 月额度 20%");
        assert_eq!(l.weekly, 7.5, "周 = 月额度 50%");
        // 三档必须递增且都以月额度为上限
        assert!(l.five_hour < l.weekly && l.weekly < l.monthly);
        assert_eq!(l.monthly, 15.0, "月档就是月额度本身");
    }
}
