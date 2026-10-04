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
            "buddy" => Some(AuthConfig {
                mode: AuthMode::Account,
                refresh: Some(RefreshConfig {
                    endpoint: "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
                        .to_string(),
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
    },
    PresetProvider {
        id: "deepseek",
        display_name: "DeepSeek",
        desc: "DeepSeek 官方（OpenAI 兼容）：填写 API Key 即可，网关自动处理缓存与省量。",
        endpoint: "https://api.deepseek.com/v1/chat/completions",
        api_key_env: "DEEPSEEK_API_KEY",
        account: false,
    },
];

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
                })
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 目录只收**实测跑通**的预设。两个已下发: buddy (账号型) / deepseek (静态 Key 型).
    /// 曾评估过 TRAE (字节) 但**未收录** —— 它不是「换端点」能通的: 需 Cloud-IDE-JWT +
    /// 十余个身份头、请求体 OpenAI→SOLO 转换、响应 SOLO 事件→OpenAI chunk、ExchangeToken
    /// 续期 (refresh_token 轮换) + 设备指纹持久化、模型按通道分目录 (发错通道→流内 4001);
    /// 参考项目自身也标注请求体加密未解 / PKCE 未支持。**内置跑不通的 = 内置坏按钮**
    /// (同「不收 free tier 被 403 的 opencode」的口径, 见 HANDOFF §8)。
    #[test]
    fn publishes_only_proven_presets() {
        let arr = presets_json().as_array().expect("presets json array").clone();
        let ids: Vec<&str> = arr
            .iter()
            .filter_map(|v| v.get("id").and_then(|s| s.as_str()))
            .collect();
        assert_eq!(ids, vec!["buddy", "deepseek"], "只下发两个已跑通的预设");
        assert_eq!(PRESETS.len(), 2, "目录不得残留未跑通条目");
        // 静态 key 型不得带 auth 段 (面板据 isAccountProv 分流登录/填 key)
        let ds = arr.iter().find(|v| v.get("id").and_then(|s| s.as_str()) == Some("deepseek")).unwrap();
        assert_eq!(ds.get("account").and_then(|v| v.as_bool()), Some(false));
        assert!(ds.get("auth").map(|v| v.is_null()).unwrap_or(true), "静态型不应带 auth 段");
    }
}
