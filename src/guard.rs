//! 隐私护栏 —— 出网前敏感信息扫描 (2026-10-13 方案 B, 对标 Astra-Gate).
//!
//! # 为什么需要
//! 网关看得到用户发给模型的**全部内容** (编程工具场景下就是整个代码库 + `.env` +
//! 数据库连接串 + 内网域名)。这些会原样发到第三方上游, 而用户往往不知道。
//!
//! # 与 Astra 的口径对齐 (`docs/content/docs/advanced/privacy.mdx`, MIT)
//! 1. **逐值匹配**, 不做全文正则扫整包 JSON —— system / messages / tools 的每个字符串值
//!    单独判。范围天然受限, 也不会把 JSON 结构本身当敏感内容。
//! 2. **原值只在请求内存上下文**, 不落库、不进日志 (本项目 recall 模块已有的隐私纪律)。
//! 3. 三档动作: **仅警告** (放行 + 记台账) / **拦截** (拒绝) / **脱敏** (发出占位符,
//!    响应回来还原)。默认**全类别仅警告** —— 用户 2026-10-13 拍板: 先观察误报,
//!    再逐类升级。
//! 4. `dry_run` = 只检测不处理 (记录命中并放行), 给上线前观察用。
//!
//! # 本项目特有的两条克制
//! - **默认不改请求体**: 脱敏会改变发往上游的 token 序列 → 破坏 KV 缓存前缀
//!   (我们主打 98.8% 命中率)。故脱敏类目必须用户逐个显式开启, 且开启文案写明缓存影响。
//! - **宁可漏报不可误报**: 高置信 (密钥/私钥块) 与低置信 (邮箱/手机号) 分开;
//!   所有正则可信度不足的类别默认仅警告。

use std::collections::HashMap;

/// 命中后的动作.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GuardAction {
    /// 放行, 只记台账.
    Warn,
    /// 拒绝请求 (未发往上游).
    Block,
    /// 脱敏: 请求里换成占位符, 响应回来还原.
    Redact,
}

impl Default for GuardAction {
    fn default() -> Self {
        Self::Warn
    }
}

/// 一条扫描规则: 一个名字 + 一个正则 + 当前动作.
#[derive(Debug, Clone)]
pub struct GuardRule {
    /// 稳定 id (落盘/面板用, 不要改).
    pub id: &'static str,
    /// 中文类别名 (面板展示).
    pub label_zh: &'static str,
    /// 英文类别名.
    pub label_en: &'static str,
    /// 正则 (Rust `regex` crate 语法; 本项目已在用, 无新依赖).
    pub pattern: regex::Regex,
    /// 该类别默认动作 (**全类别默认 Warn**, 见模块文档).
    pub default_action: GuardAction,
    /// 该类别是否**默认启用**。低置信类别默认关 (误报会淹没有用信号)。
    pub default_enabled: bool,
}

/// 一次命中 (不含原值, 只报类别与位置 —— 原值留在调用方内存里做还原映射).
#[derive(Debug, Clone, serde::Serialize)]
pub struct GuardHit {
    pub rule_id: String,
    pub action: GuardAction,
    /// 命中所在的消息下标 (messages[i]); system 顶层字段记 `None`.
    pub msg_index: Option<usize>,
}

/// 扫描结果 (含还原映射).
#[derive(Debug, Default)]
pub struct GuardScan {
    /// 全部命中 (含被 Warn 放行的).
    pub hits: Vec<GuardHit>,
    /// 占位符 → 原值 (仅 Redact 动作产生; 响应侧据此还原).
    ///
    /// ⚠️ **绝不出内存、绝不落盘**: 它活在一次请求的 handler 栈帧里,
    /// 响应结束即丢弃。若把这张表写进日志 = 把密钥抄到第二份存储 (本项目踩过的教训)。
    pub redactions: HashMap<String, String>,
    /// 是否需要拦截 (任一 Block 命中).
    pub blocked: Option<String>,
}

/// 内置规则表 (可增, id 不要改 —— 用户配置按 id 引用).
///
/// ⚠️ 正则可信度分级:
/// - **高置信** (密钥/私钥块/连接串): 默认启用 —— 这些出现在请求里几乎一定是泄漏。
/// - **低置信** (邮箱/手机号/身份证): 默认**关闭** —— 代码与文档里合法出现频率高,
///   误报会淹没有用信号 (本项目反复强调"预警变噪音就没人看了")。
pub fn builtin_rules() -> Vec<GuardRule> {
    let mut out = Vec::new();
    let mut add = |id: &'static str, zh: &'static str, en: &'static str, pat: &str, enabled: bool| {
        if let Ok(re) = regex::Regex::new(pat) {
            out.push(GuardRule {
                id,
                label_zh: zh,
                label_en: en,
                pattern: re,
                default_action: GuardAction::Warn,
                default_enabled: enabled,
            });
        }
    };
    // ── 高置信: 各类 API key 的已知前缀形态 ──
    // 只匹配"服务方公开的前缀 + 足够长度", 不试图匹配任意长随机串 (那会把
    // base64 图片/哈希值全算上, 属误报)。
    add("api_key", "API 密钥", "API key",
        r"\b(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|ghp_[0-9A-Za-z]{36}|gho_[0-9A-Za-z]{36}|xox[baprs]-[0-9A-Za-z\-]{10,})\b",
        true);
    // ── 高置信: PEM 私钥块 ──
    add("private_key", "私钥", "Private key",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----",
        true);
    // ── 高置信: 数据库/服务连接串 (含口令) ──
    add("conn_string", "连接串", "Connection string",
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)(?:://)[^\s:/@]+:[^\s/@]+@[^\s]+",
        true);
    // ── 高置信: JWT (eyJ 开头, 三段 base64url) ──
    add("jwt", "JWT 令牌", "JWT token",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        true);
    // ── 高置信: 内网地址 ──
    add("internal_host", "内网地址", "Internal host",
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|[a-z0-9\-]+\.(?:internal|local|corp|lan))\b",
        true);
    // ── 低置信: 默认关闭 ──
    add("email", "邮箱", "Email", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", false);
    add("phone_cn", "手机号", "Phone (CN)", r"\b1[3-9]\d{9}\b", false);
    add("id_card_cn", "身份证号", "ID card (CN)", r"\b\d{17}[\dXx]\b", false);
    out
}

/// 运行时配置: 每个类别 (启用?, 动作?) —— 未列出的类别用规则默认值.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct GuardConfig {
    /// 总开关 (false = 完全不扫描, 零开销).
    #[serde(default)]
    pub enabled: bool,
    /// 只检测不处理 (记录命中并放行).
    #[serde(default)]
    pub dry_run: bool,
    /// `rule_id -> (enabled, action)`. 缺省的类别用 `builtin_rules()` 的默认值.
    #[serde(default)]
    pub rules: HashMap<String, GuardRuleSetting>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct GuardRuleSetting {
    pub enabled: bool,
    pub action: GuardAction,
}

impl GuardConfig {
    /// 默认配置 (用户 2026-10-13 拍板): **总开关关** + dry_run 开。
    ///
    /// 为什么默认关而不是"默认扫+全 Warn": 扫描本身有 CPU 成本 (正则), 而大部分用户
    /// 并不需要它。**默认关 + 面板一键开** 比"默认开但只警告"更诚实:
    /// 后者会让用户以为"已在保护中", 而其实一条都没拦。
    pub fn default_off() -> Self {
        Self { enabled: false, dry_run: true, rules: HashMap::new() }
    }

    /// 某类别是否启用 (未配置 → 规则默认值).
    pub fn is_enabled(&self, rule: &GuardRule) -> bool {
        self.rules.get(rule.id).map(|s| s.enabled).unwrap_or(rule.default_enabled)
    }

    /// 某类别的动作 (未配置 → 规则默认值).
    pub fn action_of(&self, rule: &GuardRule) -> GuardAction {
        self.rules.get(rule.id).map(|s| s.action).unwrap_or(rule.default_action)
    }

    /// 生效的动作 (**dry_run 一律降级为 Warn** —— 它的全部意义就是"只检测不处理")。
    fn effective_action(&self, rule: &GuardRule) -> GuardAction {
        if self.dry_run {
            GuardAction::Warn
        } else {
            self.action_of(rule)
        }
    }
}

/// 逐值扫描一个请求体, 按需改写它.
///
/// 返回值 `GuardScan`; 改写直接发生在 `body` 上。未开启 / 无命中时 `body` 原样返回
/// (**逐字节不变** —— 这是"默认不影响转发"的硬约束, 有单测锁)。
///
/// ⚠️ 实现要点: 用 `std::mem::take` 把每个字符串值**换出**来扫, 扫完放回。
/// 直接对 `&mut String` 多重借用在同一父节点上会被借用检查器拒 (试过), 而克隆整棵
/// body 在热路径上不可接受 (7MB 请求体)。take + 放回零克隆、零 unsafe。
pub fn scan_and_redact(
    body: &mut serde_json::Value,
    cfg: &GuardConfig,
    rules: &[GuardRule],
) -> GuardScan {
    let mut scan = GuardScan::default();
    if !cfg.enabled {
        return scan;
    }
    // ① system 顶层字段 (chat 协议) —— None 表示无该字段。
    if let Some(serde_json::Value::String(s)) = body.get_mut("system") {
        scan_one(s, None, cfg, rules, &mut scan);
    }
    // ② messages[]: content (字符串 / parts[].text / parts[].content) + 消息级 tools 描述。
    let Some(serde_json::Value::Array(msgs)) = body.get_mut("messages") else {
        return scan;
    };
    for (i, m) in msgs.iter_mut().enumerate() {
        // content: 先取字符串形态, 否则取 parts 数组。两者互斥, 不存在多重借用。
        match m.get_mut("content") {
            Some(serde_json::Value::String(s)) => scan_one(s, Some(i), cfg, rules, &mut scan),
            Some(serde_json::Value::Array(parts)) => {
                for p in parts.iter_mut() {
                    if let Some(s) = p.get_mut("text").and_then(|t| match t {
                        serde_json::Value::String(s) => Some(s),
                        _ => None,
                    }) {
                        scan_one(s, Some(i), cfg, rules, &mut scan);
                    }
                    if let Some(s) = p.get_mut("content").and_then(|c| match c {
                        serde_json::Value::String(s) => Some(s),
                        _ => None,
                    }) {
                        scan_one(s, Some(i), cfg, rules, &mut scan);
                    }
                }
            }
            _ => {}
        }
        // 消息级 tools[] 的 description (自定义工具描述也可能带内网地址)。
        if let Some(serde_json::Value::Array(tools)) = m.get_mut("tools") {
            for t in tools.iter_mut() {
                if let Some(s) = t.get_mut("description").and_then(|d| match d {
                    serde_json::Value::String(s) => Some(s),
                    _ => None,
                }) {
                    scan_one(s, Some(i), cfg, rules, &mut scan);
                }
            }
        }
    }
    // ③ 顶层 tools[] (chat 请求形态) 的 description。
    if let Some(serde_json::Value::Array(tools)) = body.get_mut("tools") {
        for t in tools.iter_mut() {
            if let Some(s) = t.get_mut("description").and_then(|d| match d {
                serde_json::Value::String(s) => Some(s),
                _ => None,
            }) {
                scan_one(s, None, cfg, rules, &mut scan);
            }
        }
    }
    scan
}

/// 扫**一个**字符串值, 按动作处理 (命中记录 / 拦截 / 脱敏).
fn scan_one(
    slot: &mut String,
    msg_index: Option<usize>,
    cfg: &GuardConfig,
    rules: &[GuardRule],
    scan: &mut GuardScan,
) {
    for rule in rules {
        if !cfg.is_enabled(rule) || !rule.pattern.is_match(slot) {
            continue;
        }
        let action = cfg.effective_action(rule);
        scan.hits.push(GuardHit { rule_id: rule.id.to_string(), action, msg_index });
        match action {
            GuardAction::Warn => {}
            GuardAction::Block => {
                if scan.blocked.is_none() {
                    let label = if crate::i18n::current_lang() == crate::i18n::Lang::Zh {
                        rule.label_zh
                    } else {
                        rule.label_en
                    };
                    scan.blocked =
                        Some(format!("{}（{label}）", crate::i18n::guard_blocked_label()));
                }
            }
            GuardAction::Redact => redact_value(slot, rule, &mut scan.redactions),
        }
    }
}

/// 把一个字符串值里所有命中项替换成占位符 `[<RULEID>_<n>]`, 并把映射记进 `redactions`.
fn redact_value(slot: &mut String, rule: &GuardRule, redactions: &mut HashMap<String, String>) {
    let mut out = String::with_capacity(slot.len());
    let mut last = 0usize;
    for m in rule.pattern.find_iter(slot) {
        out.push_str(&slot[last..m.start()]);
        let placeholder = format!("[{}_{}]", rule.id.to_uppercase(), redactions.len() + 1);
        out.push_str(&placeholder);
        redactions.insert(placeholder.clone(), m.as_str().to_string());
        last = m.end();
    }
    out.push_str(&slot[last..]);
    *slot = out;
}

/// 把响应里的占位符还原为原值 (**尽力而为**).
///
/// 未还原的占位符**保留原样**并在返回值里计数 —— 静默丢弃会让用户以为还原成功了,
/// 而那是假象 (本项目"不可观测的成功就是失败"的纪律)。
pub fn restore_redactions(text: &str, redactions: &HashMap<String, String>) -> (String, usize) {
    if redactions.is_empty() {
        return (text.to_string(), 0);
    }
    let mut out = text.to_string();
    let mut restored = 0usize;
    for (placeholder, original) in redactions {
        if out.contains(placeholder.as_str()) {
            out = out.replace(placeholder.as_str(), original.as_str());
            restored += 1;
        }
    }
    (out, redactions.len().saturating_sub(restored))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg_on(dry_run: bool) -> GuardConfig {
        GuardConfig { enabled: true, dry_run, rules: HashMap::new() }
    }

    /// **默认关 = 逐字节不影响转发** (硬约束: 我们主打 KV 缓存命中率)。
    #[test]
    fn disabled_config_never_touches_body() {
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": "key is sk-abcdefghijklmnopqrstuvwxyz012345"}]
        });
        let before = serde_json::to_string(&body).unwrap();
        let scan = scan_and_redact(&mut body, &GuardConfig::default_off(), &builtin_rules());
        assert!(scan.hits.is_empty(), "关闭时不该有命中");
        assert_eq!(serde_json::to_string(&body).unwrap(), before, "关闭时 body 必须逐字节不变");
    }

    /// dry_run 只检测不改写 (这是它存在的全部意义)。
    #[test]
    fn dry_run_detects_but_leaves_body_alone() {
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": "token sk-abcdefghijklmnopqrstuvwxyz012345"}]
        });
        let before = serde_json::to_string(&body).unwrap();
        let scan = scan_and_redact(&mut body, &cfg_on(true), &builtin_rules());
        assert_eq!(scan.hits.len(), 1, "应检测到 1 处");
        assert_eq!(scan.hits[0].action, GuardAction::Warn, "dry_run 一律降级为 Warn");
        assert_eq!(scan.hits[0].rule_id, "api_key");
        assert!(scan.redactions.is_empty(), "dry_run 不得产生还原映射");
        assert_eq!(serde_json::to_string(&body).unwrap(), before, "dry_run 不得改写 body");
    }

    /// 脱敏: 换成占位符 + 建立映射; **原值不得出现在改写后的 body 里**。
    #[test]
    fn redact_replaces_secret_and_keeps_mapping() {
        let mut cfg = cfg_on(false);
        cfg.rules.insert("api_key".to_string(), GuardRuleSetting {
            enabled: true, action: GuardAction::Redact,
        });
        let secret = "sk-abcdefghijklmnopqrstuvwxyz012345";
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": format!("my key is {secret}, thanks")}]
        });
        let scan = scan_and_redact(&mut body, &cfg, &builtin_rules());
        assert_eq!(scan.hits.len(), 1);
        assert_eq!(scan.hits[0].action, GuardAction::Redact);
        let after = body["messages"][0]["content"].as_str().unwrap();
        assert!(!after.contains(secret), "改写后不得含原值: {after}");
        assert!(after.contains("[API_KEY_1]"), "应换成占位符: {after}");
        assert_eq!(scan.redactions.get("[API_KEY_1]").map(|s| s.as_str()), Some(secret));
        // 还原: 精确还原, 且无残留
        let (restored, missing) = restore_redactions(after, &scan.redactions);
        assert_eq!(restored, format!("my key is {secret}, thanks"), "还原必须精确");
        assert_eq!(missing, 0);
    }

    /// 还原"尽力而为": 模型把占位符改写/拆词时, 映射里那项**没还原上**必须在
    /// 返回值里报数 —— 静默丢弃会让用户以为还原成功了, 而那是假象。
    #[test]
    fn restore_reports_unrestored_placeholders() {
        let mut m = HashMap::new();
        m.insert("[EMAIL_1]".to_string(), "a@b.com".to_string());
        m.insert("[EMAIL_2]".to_string(), "c@d.com".to_string());
        // 模型把 [EMAIL_2] 改成了 "[EMAIL 2]" (拆词/改写) → 该项还原不上
        let (out, missing) = restore_redactions("contact [EMAIL_1] or [EMAIL 2]", &m);
        assert_eq!(out, "contact a@b.com or [EMAIL 2]", "能还原的还原, 不能的保留原样");
        assert_eq!(missing, 1, "必须报告有 1 处没还原上");
    }

    /// 拦截: 任一 Block 命中即 blocked (调用方据此拒绝请求)。
    #[test]
    fn block_action_sets_blocked_reason() {
        let mut cfg = cfg_on(false);
        cfg.rules.insert("private_key".to_string(), GuardRuleSetting {
            enabled: true, action: GuardAction::Block,
        });
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"}]
        });
        let scan = scan_and_redact(&mut body, &cfg, &builtin_rules());
        assert!(scan.blocked.is_some(), "私钥块应触发拦截");
        assert!(scan.blocked.unwrap().contains("私钥"));
    }

    /// 逐值范围: role / model / id 这些枚举字段**不扫** (扫它们纯属误报源)。
    #[test]
    fn only_user_content_is_scanned() {
        let mut cfg = cfg_on(false);
        cfg.rules.insert("internal_host".to_string(), GuardRuleSetting {
            enabled: true, action: GuardAction::Redact,
        });
        let mut body = serde_json::json!({
            "model": "db.internal",
            "messages": [{"role": "user", "content": "server at db.internal:5432"}],
        });
        let scan = scan_and_redact(&mut body, &cfg, &builtin_rules());
        assert_eq!(scan.hits.len(), 1, "只该命中 1 处 (正文), model 字段不扫");
        assert_eq!(scan.hits[0].msg_index, Some(0));
        assert_eq!(body["model"], "db.internal", "非内容字段不得被改写");
        assert!(body["messages"][0]["content"].as_str().unwrap().contains("[INTERNAL_HOST_1]"));
    }

    /// 低置信类别默认关 (误报会淹没有用信号)。
    #[test]
    fn low_confidence_categories_are_off_by_default() {
        let rules = builtin_rules();
        for id in ["email", "phone_cn", "id_card_cn"] {
            let r = rules.iter().find(|x| x.id == id).expect(id);
            assert!(!r.default_enabled, "{id} 应默认关闭");
        }
        for id in ["api_key", "private_key", "conn_string", "jwt", "internal_host"] {
            let r = rules.iter().find(|x| x.id == id).expect(id);
            assert!(r.default_enabled, "{id} 应默认启用 (高置信)");
        }
    }

    /// 多字节字符串（中文/emoji）下截串不得 panic，且脱敏占位符不破坏 UTF-8。
    #[test]
    fn utf8_secrets_do_not_panic() {
        let mut cfg = cfg_on(false);
        cfg.rules.insert("jwt".to_string(), GuardRuleSetting {
            enabled: true, action: GuardAction::Redact,
        });
        // 合法 jwt（纯 base64 段，正则能完整匹配），正文里另含中文/emoji ——
        // 验证"串里有多字节字符"时扫描与还原都不 panic、不切坏 UTF-8。
        let secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456xyzABC789";
        let content = format!("auth {secret} 这是用户的中文提问 🚀，请处理");
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": content}]
        });
        // 不 panic 即过；改写后仍是合法 JSON。
        let scan = scan_and_redact(&mut body, &cfg, &builtin_rules());
        assert_eq!(scan.hits.len(), 1);
        let out = serde_json::to_string(&body).unwrap();
        assert!(out.contains("[JWT_1]"));
        assert!(!out.contains(secret));
        assert!(out.contains("这是用户的中文提问"));
        // 还原成合法 UTF-8 字符串（防还原路径把多字节截断）。
        let (restored, _) = restore_redactions(&out, &scan.redactions);
        assert!(restored.contains(secret));
        assert!(restored.contains("这是用户的中文提问 🚀"));
    }

    /// 同一值命中多个规则时，优先级 = **先声明的规则**胜出（确定性，便于面板排序展示）。
    #[test]
    fn first_rule_wins_on_overlap() {
        let mut cfg = cfg_on(false);
        cfg.rules.insert("api_key".to_string(), GuardRuleSetting {
            enabled: true, action: GuardAction::Redact,
        });
        // jwt 也默认启用，但 api_key 必须在 builtin 里排在 jwt 前。
        let rules = builtin_rules();
        let api_idx = rules.iter().position(|r| r.id == "api_key").unwrap();
        let jwt_idx = rules.iter().position(|r| r.id == "jwt").unwrap();
        assert!(api_idx < jwt_idx, "api_key 必须排在 jwt 之前");
        // 一个形如 sk-xxx.eyJ... 的值：先被 api_key 吃掉（含 sk- 前缀），jwt 段不再二次命中。
        let mut body = serde_json::json!({
            "messages": [{"role": "user", "content": "sk-abcdefghijklmnopqrstuvwxyz012345.eyJhbGciOiJIUzI1NiJ9"}]
        });
        scan_and_redact(&mut body, &cfg, &rules);
        let redacted = body["messages"][0]["content"].as_str().unwrap();
        assert!(redacted.contains("[API_KEY_1]"));
    }
}
