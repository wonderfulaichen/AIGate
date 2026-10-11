//! 内置供应商预设 — 面板「新增 → 内置」的数据源 (2026-10-01 起, 2026-10-03 扩到主流原厂).
//!
//! 设计口径:
//! - **收谁**: 只收**实测穿网关可用**的服务, 分两类凭据形态 (面板按 `account` 分流 UI):
//!   账号型 (buddy/CodeBuddy, 出「浏览器登录」) 与静态 Key 型 (deepseek, 出「填 Key 保存」).
//!   不收: OpenCode (free tier 有客户端来源闸门, 403 FreeTierError, 登录也必失败),
//!   TRAE (需 SOLO 协议族 + 设备指纹, 未跑通) —— 内置跑不通的等于内置坏按钮.
//! - **预设只是模板**: 「添加」把模板写入 providers.json 一条普通记录并带 `preset` 标记,
//!   之后由面板层实施受保护语义 (不可删 / 可停用 / 已修改徽章 / 恢复默认, 见 ProviderConfig::preset).
//! - **端点等字段的唯一事实源在这里**, providers.json 里那份是模板的实例 (可被"恢复默认"重置).

use crate::providers::{AuthConfig, AuthMode, RefreshConfig};

/// 一个内置预设 (下发给面板的 JSON 视图与其同构)。
pub struct PresetProvider {
    /// 条目 id, 同时用作 `ProviderConfig::preset` 标记值与默认 `name`
    /// (账号按 name 索引, 故 name 一旦落盘不应再改)。
    pub id: &'static str,
    /// 面板显示名。
    pub display_name: &'static str,
    /// 中文说明 (一句话, 显示在预设卡片上)。
    pub desc: &'static str,
    /// OpenAI 兼容端点。
    pub endpoint: &'static str,
    /// key 环境变量名 (账号型仍声明: 账号不可用时回落密钥链的逃生舱)。
    pub api_key_env: &'static str,
    /// 是否账号形式 (true → 面板出「登录」入口而非 key 输入)。
    pub account: bool,
    /// 浏览器登录流的 `platform` 查询参数与身份头 (账号型才有)。
    ///
    /// 为什么要单独一层而不是复用 `id`: 同一套登录/续期协议被**多个产品**共用,
    /// 而 `platform` / 归属名 / 版本号各产品不同 —— `id` 只是我们在面板里的索引键。
    pub login: Option<LoginProfile>,
    /// 该上游是否**硬性要求首条消息是 system**(缺失时网关补一条最小的)。
    /// 见 `ProviderConfig::require_system_first` —— 实测 WorkBuddy 国际版
    /// 对首条非 system 的请求返回 `400 code=11128`。
    pub require_system_first: bool,
}

/// 一个产品的登录与身份配置 (CodeBuddy 系共用同一套协议, 仅这些取值不同)。
pub struct LoginProfile {
    /// 认证基址 (`/v2/plugin/auth/*` 与 `/v3/config` 的宿主)。
    pub endpoint: &'static str,
    /// `auth/state?platform=` 的取值。**这是登录流的关键差异**:
    /// 国内版 `ide`、国际版 `workbuddy-ai` —— 传错会拿到另一区域的凭据。
    pub platform: &'static str,
    /// `X-Domain` 头 (不带协议)。
    pub api_domain: &'static str,
    /// `X-Product-Code` 头。
    pub product_code: &'static str,
    /// 归属名 (`X-IDE-Name` / `X-IDE-Type` / `X-Product` 三头共用)。
    pub attribution_name: &'static str,
    /// `X-IDE-Version` 头 (客户端形态版本号)。
    pub client_version: &'static str,
    /// 默认 User-Agent (无按模型分档命中时使用)。
    pub user_agent: &'static str,
    /// 按模型 id 前缀覆盖 UA 的规则 (先命中先返回)。空表示只用 `user_agent`。
    pub ua_by_model_prefix: &'static [(&'static str, &'static str)],
}

impl PresetProvider {
    /// 组装该预设的 `auth` 段 (账号型才有)。
    pub fn auth(&self) -> Option<AuthConfig> {
        if !self.account {
            return None;
        }
        match self.id {
            // buddy 实测 (2026-10-01): POST + X-Refresh-Token 头, {code:0} 业务码,
            // data.{accessToken,refreshToken,expiresIn(秒)}, 55 天有效期。
            // WorkBuddy 国际版共用同一套契约 (2026-10-11 实测 auth/state 返回官方 authUrl)。
            "buddy" | "workbuddy" => Some(AuthConfig {
                mode: AuthMode::Account,
                refresh: Some(RefreshConfig {
                    endpoint: format!(
                        "{}/v2/plugin/auth/token/refresh",
                        self.login.as_ref().map(|l| l.endpoint).unwrap_or("https://copilot.tencent.com")
                    ),
                    method: "POST".to_string(),
                    headers: [(
                        "X-Refresh-Token".to_string(),
                        "{refresh_token}".to_string(),
                    )]
                    .into_iter()
                    .collect(),
                    access_token_path: "data.accessToken".to_string(),
                    refresh_token_path: "data.refreshToken".to_string(),
                    expires_in_path: "data.expiresIn".to_string(),
                }),
                expiry_margin_secs: 3600,
            }),
            _ => None,
        }
    }
}

/// 内置预设目录。**面板「新增」的数据源; 修改须同步登录/续期实测。**
pub const PRESETS: &[PresetProvider] = &[
    PresetProvider {
        id: "buddy",
        display_name: "CodeBuddy",
        desc: "腾讯 CodeBuddy 账号形式：登录一次，到期前自动续期，无需管理 API key。",
        endpoint: "https://copilot.tencent.com/v2/chat/completions",
        api_key_env: "BUDDY_ACCESS_TOKEN",
        account: true,
        login: Some(LoginProfile {
            endpoint: "https://copilot.tencent.com",
            platform: "ide",
            api_domain: "copilot.tencent.com",
            product_code: "codebuddy",
            attribution_name: "CodeBuddy",
            client_version: "1.106.1",
            user_agent: "CodeBuddyIDE/1.106.1",
            ua_by_model_prefix: &[],
        }),
        require_system_first: false,
    },
    // WorkBuddy 国际版 (腾讯): 与 CodeBuddy 共用同一套登录/续期/对话协议,
    // 仅 endpoint、platform、归属头与 UA 分档不同 (实测 2026-10-11)。
    // ⚠️ 端点/头/UA 分档规则取自 `deepseek-harness-codearts` (MIT) 的 product.ts —— 见
    //    THIRD-PARTY-NOTICES.md; 那是公开可观测的接口事实, 未搬运代码。
    PresetProvider {
        id: "workbuddy",
        display_name: "WorkBuddy (国际版)",
        desc: "腾讯 WorkBuddy 国际版账号形式：与 CodeBuddy 同一套协议，模型池为国际区（含 GPT / Gemini / Claude 系）。",
        endpoint: "https://www.workbuddy.ai/v2/chat/completions",
        api_key_env: "WORKBUDDY_ACCESS_TOKEN",
        account: true,
        login: Some(LoginProfile {
            endpoint: "https://www.workbuddy.ai",
            platform: "workbuddy-ai",
            api_domain: "www.workbuddy.ai",
            product_code: "workbuddy",
            attribution_name: "WorkBuddy",
            client_version: "5.5.2",
            user_agent: "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2",
            // 国际系模型 (gpt/gemini/claude) 用国际形态 UA; 国内系 (glm/hy/kimi/minimax)
            // 仍用国内客户端形态 —— 上游按 UA 归因「使用端」, 混用会让其中一类归因错误。
            ua_by_model_prefix: &[
                ("gpt-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"),
                ("gemini-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"),
                ("claude-", "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"),
                ("glm-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"),
                ("hy", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"),
                ("kimi-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"),
                ("minimax-", "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"),
            ],
        }),
        // 实测: 对 messages[0] 非 system 的请求返回 400 code=11128
        // "first message is not system prompt"; 补一条 system 后立刻 200。
        require_system_first: true,
    },
    PresetProvider {
        id: "deepseek",
        display_name: "DeepSeek",
        desc: "DeepSeek 官方（OpenAI 兼容）：填写 API Key 即可，网关自动处理缓存与省量。",
        endpoint: "https://api.deepseek.com/v1/chat/completions",
        api_key_env: "DEEPSEEK_API_KEY",
        account: false,
        login: None,
        require_system_first: false,
    },
];

/// 取某预设的**认证基址** (登录/续期/`/v3/config` 的宿主)。
///
/// 供"拉取模型"等处按预设决定打哪个域名 —— 国内版与国际版是同一套路径不同域名,
/// 写死一个会让另一个产品永远拉错站。
pub fn login_endpoint_of(id: &str) -> Option<String> {
    PRESETS
        .iter()
        .find(|p| p.id == id)
        .and_then(|p| p.login.as_ref())
        .map(|l| l.endpoint.to_string())
}

/// 该预设是否属 **CodeBuddy 系**（buddy / workbuddy）—— 即"用 /v3/config 当模型目录"的那一族。
///
/// 判据是**声明了 login 配置**（账号型产品都走这套协议），而不是枚举 id ——
/// 否则每新增一个同类产品都要回来改这里，漏改的表现是"能登录但拉不到模型"。
pub fn is_codebuddy_family(id: Option<&str>) -> bool {
    match id {
        Some(id) => PRESETS
            .iter()
            .find(|p| p.id == id)
            .map(|p| p.login.is_some())
            .unwrap_or(false),
        None => false,
    }
}

/// 取某预设 id 的登录流取值 (供 `admin::login_profile_for` 转成运行时结构).
pub fn login_profile_of(id: &str) -> Option<crate::accounts::LoginProfile> {
    let p = PRESETS.iter().find(|p| p.id == id)?;
    let l = p.login.as_ref()?;
    Some(crate::accounts::LoginProfile {
        endpoint: l.endpoint.to_string(),
        platform: l.platform.to_string(),
        api_domain: l.api_domain.to_string(),
        product_code: l.product_code.to_string(),
        attribution_name: l.attribution_name.to_string(),
        client_version: l.client_version.to_string(),
        user_agent: l.user_agent.to_string(),
    })
}

/// 下发给面板的 JSON 数组 (字段与前端组装 provider 条目时所需一一对应).
pub fn presets_json() -> serde_json::Value {
    serde_json::Value::Array(
        PRESETS
            .iter()
            .map(|p| {
                serde_json::json!({
                    "id": p.id,
                    "name": p.id,            // 默认 name = id (账号索引键)
                    "display_name": p.display_name,
                    "desc": p.desc,
                    "endpoint": p.endpoint,
                    "api_key_env": p.api_key_env,
                    "account": p.account,
                    "auth": p.auth(),
                    // 登录流的 platform 与身份头取值 (前端"浏览器登录"按钮要用) —
                    // 不下发则前端只能猜 (国内/国际版 platform 不同, 猜错就登错区)。
                    "login": p.login.as_ref().map(|l| serde_json::json!({
                        "endpoint": l.endpoint,
                        "platform": l.platform,
                        "api_domain": l.api_domain,
                        "product_code": l.product_code,
                        "attribution_name": l.attribution_name,
                        "client_version": l.client_version,
                        "user_agent": l.user_agent,
                    })),
                    "ua_by_model_prefix": p.login.as_ref()
                        .map(|l| l.ua_by_model_prefix.iter().map(|(k, v)| serde_json::json!([k, v])).collect::<Vec<_>>())
                        .unwrap_or_default(),
                    // 让前端一键添加时把该开关写进 providers.json —— 漏了它内建预设
                    // 对 WorkBuddy 就是"能登录但一发请求 400"。
                    "require_system_first": p.require_system_first,
                })
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 目录只收**实测跑通**的预设。三个已下发: buddy (CodeBuddy 国内) / workbuddy (国际版) /
    /// deepseek (静态 Key 型)。**内置跑不通的 = 内置坏按钮**
    /// (同「不收 free tier 被 403 的 opencode」的口径, 见 HANDOFF §8)。
    ///
    /// 曾评估过 TRAE (字节) 但**未收录** —— 它不是「换端点」能通的: 需 Cloud-IDE-JWT +
    /// 十余个身份头、请求体 OpenAI→SOLO 转换、响应 SOLO 事件→OpenAI chunk、ExchangeToken
    /// 续期 (refresh_token 轮换) + 设备指纹持久化、模型按通道分目录 (发错通道→流内 4001);
    /// 参考项目自身也标注请求体加密未解 / PKCE 未支持。
    #[test]
    fn publishes_only_proven_presets() {
        let arr = presets_json().as_array().expect("presets json array").clone();
        let ids: Vec<&str> = arr
            .iter()
            .filter_map(|v| v.get("id").and_then(|s| s.as_str()))
            .collect();
        assert_eq!(ids, vec!["buddy", "workbuddy", "deepseek"], "只下发已跑通的预设");
        assert_eq!(PRESETS.len(), 3, "目录不得残留未跑通条目");
        // 静态 key 型不得带 auth 段 (面板据 isAccountProv 分流登录/填 key)
        let ds = arr.iter().find(|v| v.get("id").and_then(|s| s.as_str()) == Some("deepseek")).unwrap();
        assert_eq!(ds.get("account").and_then(|v| v.as_bool()), Some(false));
        assert!(ds.get("auth").map(|v| v.is_null()).unwrap_or(true), "静态型不应带 auth 段");
    }

    /// 账号型预设必须各带**自己的**登录 platform（国内 `ide` / 国际 `workbuddy-ai`）。
    ///
    /// 为什么单独钉这条: `platform` 决定登录流拿哪个区域的凭据, 而两个产品共用同一套
    /// 协议 —— 若误用同一个值, 表现是"登录成功但凭据属于另一个区"(静默、极难查),
    /// 而不是报错。故用单测锁死。
    #[test]
    fn account_presets_carry_their_own_platform() {
        let arr = presets_json().as_array().unwrap().clone();
        let get = |id: &str| arr.iter().find(|v| v.get("id").and_then(|s| s.as_str()) == Some(id)).unwrap().clone();
        let b = get("buddy");
        assert_eq!(b.pointer("/login/platform").and_then(|v| v.as_str()), Some("ide"));
        assert_eq!(b.pointer("/login/endpoint").and_then(|v| v.as_str()), Some("https://copilot.tencent.com"));
        let w = get("workbuddy");
        assert_eq!(w.pointer("/login/platform").and_then(|v| v.as_str()), Some("workbuddy-ai"));
        assert_eq!(w.pointer("/login/endpoint").and_then(|v| v.as_str()), Some("https://www.workbuddy.ai"));
        assert_eq!(w.pointer("/login/attribution_name").and_then(|v| v.as_str()), Some("WorkBuddy"));
        // 续期端点必须跟着产品走 (国际版不能打到国内域名)
        assert_eq!(
            w.pointer("/auth/refresh/endpoint").and_then(|v| v.as_str()),
            Some("https://www.workbuddy.ai/v2/plugin/auth/token/refresh")
        );
        // UA 分档: 国际系与前缀规则必须下发 (缺了会让上游把用量归因到错误的产品)
        let rules = w.get("ua_by_model_prefix").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        assert!(rules.iter().any(|r| r[0] == "gpt-"), "gpt-* 应有国际形态 UA 规则");
        assert!(rules.iter().any(|r| r[0] == "glm-"), "glm-* 应有国内形态 UA 规则");
    }
}
