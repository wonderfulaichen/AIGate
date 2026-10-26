//! opencode Go 套餐额度：查询 Console API 拿到**官方真实额度**。
//!
//! # 为什么是 Console API 而不是 inference 端点
//!
//! 我最初（2026-10-24 早些时候）断言「opencode 没有额度查询接口，只能自己累加
//! `usage.opencode.allowance`」。**这个结论是错的**，已实测推翻：
//!
//! - inference 端点（`/inference/...`）确实没有额度接口；
//! - 但 **Console 网页 API** 有：
//!   `GET https://opencode.ai/console/api/go/status`
//!   需要 Cookie `__Host-console_session` + 请求头 `x-org-id: wrk_...`。
//!
//! 实测（2026-10-24，真实账号）返回 HTTP 200：
//! ```json
//! {"access":{"meters":{
//!   "fiveHour":{"limitMicroCents":"1200000000","usedMicroCents":"0","resetsAt":null},
//!   "week":{"limitMicroCents":"3000000000","usedMicroCents":"0","resetsAt":"..."},
//!   "month":{"limitMicroCents":"6000000000","usedMicroCents":"6000000000","resetsAt":"..."}
//! }}}
//! ```
//! `1200000000 / 6000000000 = 0.20`、`3000000000 / 6000000000 = 0.50` ——
//! 与官方文档「5 小时 = 月额度 20%、周 = 50%、月 = 100%」**精确吻合**，
//! 因此 `1e8` 是正确的单位换算（microCents → 美元）。
//!
//! # 与「累加 allowance」方案的关系
//!
//! 累加方案已**删除**。它有两个根本缺陷：
//! ① 只能统计**经过我们网关**的请求；直接从别的客户端打 opencode 的用量看不到；
//! ② 需要维护一张「模型 → 月额度」表，而那表是外部数据、会过期。
//! Console API 直接给官方真实值，零误差，无需维护。
//!
//! # 许可证说明
//!
//! 「存在这个 Console 端点」这一事实来自对开源项目 QuotaHub 的阅读，
//! 但**该仓库没有 LICENSE 文件**（GitHub metadata `license: null`），
//! 默认保留所有权利。因此这里**没有复用它的任何代码** ——
//! 端点形状与字段名是我方**独立实测**确认真值后自行实现的，
//! 数据模型（`QuotaWindow` 等）也按本项目风格重写。

use crate::providers::ProviderConfig;
use serde::Serialize;

/// Console 额度端点（事实来源：我方实测）。
const GO_STATUS_URL: &str = "https://opencode.ai/console/api/go/status";

/// 单位换算：Console 用 microCents 表示美元（1e8 microCents = 1 美元）。
///
/// 实测依据：`1200000000 / 6000000000 == 0.20`，与官方 5h=20% 精确吻合。
const MICRO_CENTS_PER_USD: f64 = 100_000_000.0;

/// 单个额度窗口（已换算成美元）。
#[derive(Debug, Clone, Serialize)]
pub struct QuotaWindow {
    /// 窗口标识：`fiveHour` / `week` / `month`（与 Console 的字段名一致）。
    pub key: String,
    /// 上限（美元）。`None` = Console 没给（如未激活的 5h 窗口）。
    pub limit_usd: Option<f64>,
    /// 已用（美元）。`None` = Console 没给。
    pub used_usd: Option<f64>,
    /// 重置时间（ISO8601），`None` = 无（未激活窗口）。
    pub resets_at: Option<String>,
    /// 已用百分比。
    ///
    /// ⚠️ 必须作为**字段**下发，不能只提供方法 ——
    /// `serde::Serialize` 不会序列化方法，前端拿不到 `w.used_pct` 就会
    /// 全部显示 "—"、进度条恒 0%（首次实测就是这么翻车的，被探针 ④ 抓到）。
    pub used_pct: Option<f64>,
}

impl QuotaWindow {
    /// 由金额算百分比并落到字段里。
    pub fn with_pct(mut self) -> Self {
        self.used_pct = self.compute_used_pct();
        self
    }

    /// 已用百分比。`None` 表示算不出（上限缺失或为 0）——
    /// 不返回 0，因为「没有上限」与「用了 0%」是两回事（未采集 ≠ 0）。
    pub fn compute_used_pct(&self) -> Option<f64> {
        let (Some(limit), Some(used)) = (self.limit_usd, self.used_usd) else {
            return None;
        };
        if limit <= 0.0 {
            return None;
        }
        // **不 clamp**：超额要如实反映（可能被额外计费或触发限流）。
        Some(used / limit * 100.0)
    }
}

/// 一次额度查询的结果。
#[derive(Debug, Clone, Serialize)]
pub struct QuotaSnapshot {
    /// 套餐产品标识（`go` / `go-plus`）。
    pub product: String,
    /// 套餐周期结束时间。
    pub ends_at: Option<String>,
    /// 是否已设为到期不续费。
    pub cancel_at_period_end: bool,
    /// 三个窗口（顺序：fiveHour / week / month，与官方语义一致）。
    pub windows: Vec<QuotaWindow>,
    /// 查询成功时间（Unix 秒）。
    pub fetched_at: u64,
}

/// 从 Console 的原始 JSON 解析额度。
///
/// 抽成纯函数便于单测：**不需要联网就能验证换算与字段映射**。
pub fn parse_go_status(v: &serde_json::Value) -> Result<QuotaSnapshot, String> {
    let access = v.get("access").ok_or("响应里没有 access 字段")?;
    let meters = access.get("meters").ok_or("access 里没有 meters")?;

    let micro = |w: &serde_json::Value, field: &str| -> Option<f64> {
        // Console 把数值发成**字符串**（"6000000000"），但也兼容数字。
        let raw = w.get(field)?;
        let n = raw
            .as_str()
            .and_then(|s| s.trim().parse::<f64>().ok())
            .or_else(|| raw.as_f64())?;
        Some(n / MICRO_CENTS_PER_USD)
    };

    let mut windows = Vec::new();
    for key in ["fiveHour", "week", "month"] {
        let Some(w) = meters.get(key) else { continue };
        let win = QuotaWindow {
            key: key.to_string(),
            limit_usd: micro(w, "limitMicroCents"),
            used_usd: micro(w, "usedMicroCents"),
            resets_at: w
                .get("resetsAt")
                .and_then(|x| x.as_str())
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string()),
            // 先占位，with_pct() 统一算好。
            used_pct: None,
        };
        windows.push(win.with_pct());
    }
    if windows.is_empty() {
        return Err("meters 里没有任何额度窗口".into());
    }

    Ok(QuotaSnapshot {
        product: v
            .get("product")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string(),
        ends_at: access
            .get("endsAt")
            .and_then(|x| x.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string()),
        cancel_at_period_end: v
            .get("cancelAtPeriodEnd")
            .and_then(|x| x.as_bool())
            .unwrap_or(false),
        windows,
        fetched_at: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    })
}

/// Console 组织/工作区列表端点（实测：返回 `[{id:"wrk_...",name:"Default",...}]`）。
///
/// 用途：用户粘了 Cookie 后，**不用再去地址栏抠 ID** —— 直接问它。
///
/// ⚠️ 同一账号下可以有**多个工作区，且前缀不止 `wrk_`** —— 实测该账号有两个：
/// `wrk_...`（name="Default"）与 `org_...`（name="aichen"），
/// **两者都能查 Go 额度且额度完全独立**（到期时间、已用量各不相同）。
/// 早期这里用 `starts_with("wrk_")` 过滤，把 `org_` 工作区整块丢掉，
/// 用户报「同账号多个工作区，其他区无法获得」就是这个 bug。
const ORGS_URL: &str = "https://opencode.ai/console/api/orgs";

/// 一个工作区（Console `/orgs` 返回项的可见字段，用于面板展示）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct WorkspaceRef {
    /// 工作区 ID（`wrk_` / `org_` 前缀都有，都可用）。
    pub id: String,
    /// 显示名（如 "Default" / "aichen"）。可能为空。
    pub name: String,
}

/// 用 Cookie 查该账号可用的**工作区**列表（含名字，供面板显示）。
///
/// **不过滤前缀**：`org_` 与 `wrk_` 都能查额度，过滤掉就会让用户看到
/// "少了一个区"且毫无提示。
pub async fn fetch_workspaces(
    client: &reqwest::Client,
    cookie: &str,
) -> Result<Vec<WorkspaceRef>, String> {
    let cookie = cookie.trim();
    if cookie.is_empty() {
        return Err("请先填 Cookie".into());
    }
    let resp = client
        .get(ORGS_URL)
        .header(reqwest::header::COOKIE, cookie)
        .header("Accept", "*/*")
        .header(
            reqwest::header::USER_AGENT,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
             (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("查询工作区失败: {e}"))?;
    let status = resp.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Err(format!("Cookie 无效或已过期 (HTTP {})", status.as_u16()));
    }
    if !status.is_success() {
        return Err(format!("Console 返回 HTTP {}", status.as_u16()));
    }
    let v: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("解析工作区列表失败: {e}"))?;
    let list: Vec<WorkspaceRef> = v
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|o| {
                    // id 与 name 必须从**同一个元素**取，不能分两趟按位置对齐
                    // （缺 id 的元素会让两趟错位，名字配到别的工作区上）。
                    let id = o.get("id").and_then(|x| x.as_str())?.trim();
                    if id.is_empty() {
                        return None;
                    }
                    Some(WorkspaceRef {
                        id: id.to_string(),
                        name: o
                            .get("name")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_string(),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    if list.is_empty() {
        return Err("该 Cookie 下没有可用工作区".into());
    }
    Ok(list)
}

/// 用 Cookie 查该账号可用的**工作区 ID**（简版，只给 ID）。
pub async fn fetch_workspace_ids(
    client: &reqwest::Client,
    cookie: &str,
) -> Result<Vec<String>, String> {
    Ok(fetch_workspaces(client, cookie)
        .await?
        .into_iter()
        .map(|w| w.id)
        .collect())
}

/// 用**传入的**凭据查额度（不看供应商配置）。
///
/// 用于"粘完立即验证"：用户还没保存时就能知道这对凭据能不能用 ——
/// 否则要先保存、再点查询、再回来看错误，走三步才知道填错了。
pub async fn fetch_go_status_with(
    client: &reqwest::Client,
    cookie: &str,
    org: &str,
) -> Result<QuotaSnapshot, String> {
    let cookie = cookie.trim();
    let org = org.trim();
    if cookie.is_empty() {
        return Err("请先填 Console Cookie".into());
    }
    if org.is_empty() {
        return Err("请先选工作区（可点「自动获取」）".into());
    }
    let probe = ProviderConfig {
        name: "probe".into(),
        endpoint: String::new(),
        api_key_env: String::new(),
        api_key_default: None,
        auth: None,
        preset: None,
        disabled: None,
        probed_protocols: None,
        balance_endpoint: None,
        headers: None,
        ua_by_model_prefix: None,
        require_system_first: None,
        api_format: None,
        endpoint_anthropic: None,
        endpoint_responses: None,
        prompt_cache: None,
        openai_cache_control: None,
        max_request_body_bytes: None,
        console_session: Some(cookie.to_string()),
        console_org_id: Some(org.to_string()),
        console_org_ids: None,
        models: std::collections::HashMap::new(),
    };
    fetch_go_status(client, &probe).await
}

/// 发一次额度查询。
///
/// 凭据来自供应商配置（`console_session` + 工作区 ID）。
/// 只读 GET，不消耗额度、不改账号状态。
pub async fn fetch_go_status(
    client: &reqwest::Client,
    provider: &ProviderConfig,
) -> Result<QuotaSnapshot, String> {
    let cookie = provider
        .console_session
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or("未配置 Console Cookie（供应商配置 → opencode 额度）")?;
    let workspaces = provider.console_workspaces();
    let org = workspaces
        .first()
        .ok_or("未配置工作区（可点「自动获取」拿到全部工作区）")?;
    fetch_go_status_for(client, cookie, org).await
}

/// 查**一个指定工作区**的额度（内部实现，不做前缀校验）。
async fn fetch_go_status_for(
    client: &reqwest::Client,
    cookie: &str,
    org: &str,
) -> Result<QuotaSnapshot, String> {
    let cookie = cookie.trim();
    let org = org.trim();
    if org.is_empty() {
        return Err("工作区 ID 为空".into());
    }

    let resp = client
        .get(GO_STATUS_URL)
        .header(reqwest::header::COOKIE, cookie)
        .header("x-org-id", org)
        .header("Accept", "*/*")
        .header("Referer", format!("https://opencode.ai/console/{org}/go"))
        // Console 是网页 API，缺 UA 时可能被拒。
        .header(
            reqwest::header::USER_AGENT,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
             (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("查询 Console 额度失败: {e}"))?;

    let status = resp.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Err(format!(
            "Console 认证失败 (HTTP {})，Cookie 可能已过期，请重新登录 opencode 后复制",
            status.as_u16()
        ));
    }
    if !status.is_success() {
        return Err(format!("Console 返回 HTTP {}", status.as_u16()));
    }
    let v: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("解析 Console 响应失败: {e}"))?;
    parse_go_status(&v)
}

/// 一个工作区的额度（或它的查询错误）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct WorkspaceQuota {
    /// 工作区 ID。
    pub workspace: String,
    /// 显示名（可能为空 —— 配置里只有 ID 时就没有名字）。
    pub name: String,
    /// 该工作区的额度；`None` 表示查询失败，原因在 `error`。
    pub quota: Option<QuotaSnapshot>,
    /// 查询失败原因（成功时为 `None`）。
    pub error: Option<String>,
}

/// 查**全部工作区**的额度（2026-10-26）。
///
/// # 为什么必须逐个查、不能只查一个
///
/// 一个 opencode 账号可以有多个工作区，每个工作区是**独立的 Go 订阅**
/// —— 实测同账号两个区：`wrk_` 的月额度已用满 100%、`org_` 的才 64.1%，
/// 到期时间也不同。只查第一个会让面板显示"用满了"，
/// 而实际上另一个区还有余量（用户报「其他区无法获得」）。
///
/// # 为什么单个失败不整体失败
///
/// 某个工作区可能被 `blocked`、或已退订（`/go/status` 返回 4xx）。
/// 那种情况下**其余工作区仍然要看得到** —— 整体报错会让用户
/// 因为一个区的问题看不到所有区。失败的区在 `error` 里说明原因。
pub async fn fetch_all_go_status(
    client: &reqwest::Client,
    provider: &ProviderConfig,
) -> Result<Vec<WorkspaceQuota>, String> {
    let cookie = provider
        .console_session
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or("未配置 Console Cookie（供应商配置 → opencode 额度）")?;
    let workspaces = provider.console_workspaces();
    if workspaces.is_empty() {
        return Err("未配置工作区（可点「自动获取」拿到全部工作区）".into());
    }

    let mut out = Vec::with_capacity(workspaces.len());
    for org in workspaces {
        let r = fetch_go_status_for(client, cookie, &org).await;
        out.push(match r {
            Ok(q) => WorkspaceQuota {
                workspace: org,
                name: String::new(),
                quota: Some(q),
                error: None,
            },
            Err(e) => WorkspaceQuota {
                workspace: org,
                name: String::new(),
                quota: None,
                error: Some(e),
            },
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// **真实响应结构**（2026-10-24 实测抓取，已脱敏，只保留额度相关字段）。
    ///
    /// 用真实响应做夹具比手捏 JSON 可靠：手捏容易漏掉「数值是字符串」这类细节。
    const REAL: &str = r#"{
      "subscriberUserId":"acc_x","product":"go","renewalProduct":"go",
      "renewalCurrency":"usd","useBalance":false,
      "cancelAtPeriodEnd":true,"renewalStopReason":"cancelled_by_user",
      "access":{
        "startsAt":"2026-09-29T07:17:34.000Z",
        "endsAt":"2026-10-29T07:17:34.000Z",
        "cancelAtPeriodEnd":true,
        "meters":{
          "fiveHour":{"startsAt":null,"resetsAt":null,
                      "limitMicroCents":"1200000000","usedMicroCents":"0"},
          "week":{"startsAt":"2026-10-19T00:00:00.000Z",
                  "resetsAt":"2026-10-26T00:00:00.000Z",
                  "limitMicroCents":"3000000000","usedMicroCents":"0"},
          "month":{"resetsAt":"2026-10-29T07:17:34.000Z",
                   "limitMicroCents":"6000000000","usedMicroCents":"6000000000"}
        }
      },
      "upgradePrice":{"amountMicroCents":"4000000000","currency":"usd"}
    }"#;

    /// 单位换算必须正确：`1e8` microCents = 1 美元。
    ///
    /// 这条钉住的是**整块功能正确性的地基** —— 换算错 100 倍会让"还剩 99%"
    /// 与"已用满"完全颠倒。判据取官方比例（5h/month == 0.20、week/month == 0.50），
    /// 因为这两个比例是从官方文档独立得知的，不依赖我对方程的猜测。
    #[test]
    fn micro_cents_conversion_matches_official_ratios() {
        let v: serde_json::Value = serde_json::from_str(REAL).unwrap();
        let s = parse_go_status(&v).expect("真实响应必须能解析");

        let get = |k: &str| s.windows.iter().find(|w| w.key == k).unwrap();
        let fh = get("fiveHour").limit_usd.expect("5h 有上限");
        let wk = get("week").limit_usd.expect("周有上限");
        let mo = get("month").limit_usd.expect("月有上限");

        assert!((fh - 12.0).abs() < 1e-9, "1200000000 microCents = $12，实际 {fh}");
        assert!((wk - 30.0).abs() < 1e-9, "3000000000 microCents = $30，实际 {wk}");
        assert!((mo - 60.0).abs() < 1e-9, "6000000000 microCents = $60，实际 {mo}");

        // 官方比例（独立已知的真值，最强的换算校验）
        assert!((fh / mo - 0.20).abs() < 1e-9, "5h 必须是月额度的 20%");
        assert!((wk / mo - 0.50).abs() < 1e-9, "周必须是月额度的 50%");
    }

    /// **百分比必须作为字段下发**，而不是只存在于方法里。
    ///
    /// 这是首次实测翻车的直接原因：`used_pct()` 是方法，serde 不序列化它，
    /// 前端拿到 undefined → 全部显示 "—"、进度条恒 0%。
    /// 探针 ④ 抓到了，这里用单测钉死。
    #[test]
    fn used_pct_is_serialized_as_field() {
        let v: serde_json::Value = serde_json::from_str(REAL).unwrap();
        let s = parse_go_status(&v).unwrap();
        let json = serde_json::to_value(&s).expect("必须能序列化");

        let month = json["windows"]
            .as_array()
            .unwrap()
            .iter()
            .find(|w| w["key"] == "month")
            .expect("month 窗口");
        assert_eq!(
            month["used_pct"].as_f64(),
            Some(100.0),
            "序列化后的 JSON 里必须有 used_pct 字段且为 100 —— \
             只提供方法会导致前端全显示 '—'"
        );
        // 同时确认不是靠方法名碰巧命中
        assert!(month.get("used_pct").is_some(), "used_pct 必须是字段");
    }

    /// 数值以**字符串**下发时必须能解析（Console 就是这么发的）。
    #[test]
    fn parses_string_numbers() {
        let v: serde_json::Value = serde_json::from_str(
            r#"{"access":{"meters":{"month":{
                 "limitMicroCents":"500000000","usedMicroCents":"250000000",
                 "resetsAt":"2026-11-01T00:00:00.000Z"}}}}"#,
        )
        .unwrap();
        let s = parse_go_status(&v).unwrap();
        let m = s.windows.iter().find(|w| w.key == "month").unwrap();
        assert_eq!(m.limit_usd, Some(5.0), "字符串 \"500000000\" = $5");
        assert_eq!(m.used_usd, Some(2.5), "\"250000000\" = $2.5");
        assert_eq!(m.compute_used_pct(), Some(50.0));
    }

    /// **已用满必须如实显示 100%**，未激活窗口（无上限）必须是 None 而不是 0%。
    ///
    /// 实测账号就是这个状态：月额度 $60/$60 已满、5h 窗口未激活。
    /// 若把"未激活"显示成 0%，用户会以为还有额度；若把满额显示成别的数，
    /// 用户会以为还能用 —— 两个方向都是误导。
    #[test]
    fn exhausted_and_inactive_windows_are_distinct() {
        let v: serde_json::Value = serde_json::from_str(REAL).unwrap();
        let s = parse_go_status(&v).unwrap();

        let month = s.windows.iter().find(|w| w.key == "month").unwrap();
        assert_eq!(month.compute_used_pct(), Some(100.0), "月额度 $60/$60 应为 100%");
        assert_eq!(month.used_usd, Some(60.0));

        // 5h 窗口：有上限但没有 resetsAt（未激活），已用 0 → 0%（真实为 0，可显示）
        let fh = s.windows.iter().find(|w| w.key == "fiveHour").unwrap();
        assert_eq!(fh.compute_used_pct(), Some(0.0), "已用 $0 / $12 = 0%");
        assert!(fh.resets_at.is_none(), "未激活窗口没有重置时间");

        // 真正"算不出"的情形：上限缺失 → None（不是 0）
        let no_limit = QuotaWindow {
            key: "month".into(),
            limit_usd: None,
            used_usd: Some(1.0),
            resets_at: None,
            used_pct: None,
        };
        assert_eq!(no_limit.compute_used_pct(), None, "没有上限 → None（未采集），不是 0%");
        let zero_limit = QuotaWindow {
            key: "month".into(),
            limit_usd: Some(0.0),
            used_usd: Some(1.0),
            resets_at: None,
            used_pct: None,
        };
        assert_eq!(zero_limit.compute_used_pct(), None, "上限为 0 → None（避免除零）");
    }

    /// **超额不 clamp**：如果 used > limit，必须如实给出 >100%。
    ///
    /// 为什么重要：超额是"该充值/该报障"的信号。夹到 100% 会让它看起来只是"刚好用完"。
    #[test]
    fn over_quota_is_not_clamped() {
        let v: serde_json::Value = serde_json::from_str(
            r#"{"access":{"meters":{"month":{
                 "limitMicroCents":"100000000","usedMicroCents":"150000000"}}}}"#,
        )
        .unwrap();
        let s = parse_go_status(&v).unwrap();
        let m = s.windows.iter().find(|w| w.key == "month").unwrap();
        assert_eq!(m.compute_used_pct(), Some(150.0), "$1.5 / $1 = 150%，不得夹到 100%");
        assert_eq!(m.used_pct, Some(150.0), "字段值同样不得 clamp");
    }

    /// 响应缺 `access` / `meters` 时必须报错，不能静默返回空额度。
    #[test]
    fn malformed_payload_errors_out() {
        for bad in [r#"{}"#, r#"{"access":{}}"#, r#"{"access":{"meters":{}}}"#] {
            let v: serde_json::Value = serde_json::from_str(bad).unwrap();
            assert!(parse_go_status(&v).is_err(), "缺字段应报错: {bad}");
        }
    }

    /// 套餐元信息要取到（产品名、到期时间、是否已取消续费）。
    #[test]
    fn extracts_plan_metadata() {
        let v: serde_json::Value = serde_json::from_str(REAL).unwrap();
        let s = parse_go_status(&v).unwrap();
        assert_eq!(s.product, "go");
        assert_eq!(s.ends_at.as_deref(), Some("2026-10-29T07:17:34.000Z"));
        assert!(s.cancel_at_period_end, "该账号已设为到期不续费");
        assert_eq!(s.windows.len(), 3, "三个窗口都要在");
        assert_eq!(s.windows[0].key, "fiveHour");
        assert_eq!(s.windows[1].key, "week");
        assert_eq!(s.windows[2].key, "month");
    }
}
