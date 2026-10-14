//! 会话/轮次关联 —— 方案 F 第二阶段 (2026-10-13, 对标 Astra-Gate 的链路追踪).
//!
//! # 为什么需要这个模块
//! 记录 (`logs.jsonl`) 是**单请求视角**: 每条只说自己这次花了多久、用了多少 token。
//! 但用户排查时问的是"**这一轮** Agent 做了什么、时间花在哪、哪次调用慢/失败" ——
//! 那需要把多条请求串成一条链。本轮反复遇到的「跑一会就停」「上游断流」「余额 bug」
//! 全都靠手工翻日志拼接, 正是这个盲区的代价。
//!
//! # 核心设计: 只读请求本身, 不读也不存对话内容
//! (与 Astra 的 traces.mdx 同口径, 也是本项目隐私纪律的自然延伸)
//! 识别依据全部来自**请求头与请求体的结构**, 不含任何正文:
//!
//! | 识别依据 | 精确度 |
//! |---|---|
//! 会话 id: `x-claude-code-session-id` / `session-id` (Codex) / `prompt_cache_key` | **精确** (客户端自报) |
//! 同一轮的下一步: 上次响应产出的 tool_call id 出现在本次请求的 tool 结果里 | **精确** (id 配对) |
//! 兜底会话: system 提示词 + 首条用户消息的**指纹哈希** | 推断 (只存 8 字节哈希, 不存内容) |
//!
//! # 字段口径 (`SessionRef`)
//! - `session_key` 可能是真实 id, 也可能是 `sha256:xxxxxxxx` 形态的兜底指纹 ——
//!   面板不区分展示, 但**drawing 关联时必须同源** (真实 id 与指纹不可能相等).
//! - `turn` 单调递增; **新的一轮**判据 = 请求里最后一条消息是 user 角色 (Agent 发言结束).
//! - `parent_call` 只在"上次产出的 tool_call id 出现在本次请求"时填 —— 那是**实线**;
//!   推断出来的父子关系由调用方标**虚线**, 本模块不产生推断关系.

use std::sync::{Arc, Mutex};

/// 单次请求的会话归属 (随 `RequestLog` 落盘 → 面板读同一份数据, 无需另建存储).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct SessionRef {
    /// 会话键: 优先客户端自报的 session id; 都没有时是 `sha256:<16hex>` 兜底指纹。
    /// 空串 = 完全无法识别 (客户端无 session 概念且无消息可指纹) —— 此时不参与关联。
    #[serde(default)]
    pub session_key: String,
    /// 该会话内的**第 N 跳** (从 1 开始; 0 = 未识别)。
    ///
    /// ⚠️ 语义是"跳数"不是"轮数": 一轮「用户输入 → 工具调用 → 工具结果 → 模型再答」
    /// 是 **4 跳 / 1 轮**。工具结果那一跳既不是新轮、又确实是一次请求, 用"轮"装不下它。
    /// 前端若要按轮展示: 用 `answers_to.is_empty()` 分段即可 (空 = 新开一轮)。
    #[serde(default)]
    pub turn: u32,
    /// 本次请求**承接**的那次调用的 tool_call id 列表 (非空 = 上轮工具结果的回报)。
    /// 空 = 这是一轮的开头 (用户刚说话)。
    #[serde(default)]
    pub answers_to: Vec<String>,
    /// 本次会话键的来源: `"header"` (客户端自报, 精确) / `"fingerprint"` (兜底推断)。
    #[serde(default)]
    pub session_source: String,
}

/// 会话关联表: 会话键 → 最近状态 (用于给新请求编号).
///
/// 为什么用进程内表而不是每次扫日志: `logs.jsonl` 有 5000 行滚动上限, 扫全文件
/// 做编号在热路径上不可接受; 且旧会话的编号没有价值 (它不会再增长).
/// 重启后表清空 → 新会话从 turn 1 重新起, **不影响已有记录的关联**
/// (记录自己带着 session_key + turn, 面板按它们分组, 不依赖进程内状态).
#[derive(Clone, Default)]
pub struct SessionTracker {
    inner: Arc<Mutex<std::collections::HashMap<String, u32>>>,
}

impl SessionTracker {
    pub fn new() -> Self {
        Self::default()
    }

    /// 取得该会话的**下一轮**轮次号 (供新的一轮使用).
    pub fn next_turn(&self, key: &str) -> u32 {
        if key.is_empty() {
            return 0;
        }
        let mut g = self.inner.lock().expect("session tracker lock poisoned");
        let n = g.entry(key.to_string()).or_insert(0);
        *n += 1;
        *n
    }
}

/// 从**原始请求体**提取"本次请求回报了哪些 tool_call id"。
///
/// 兼容三种消息形态 (客户端各画各的):
/// - OpenAI chat: `messages[].content[]` 里 `{type:"tool", tool_call_id}`;
/// - Anthropic /messages: `messages[].content[]` 里 `{type:"tool_result", tool_use_id}`;
/// - Anthropic 顶层 `tools` 结果扁平化: 部分客户端把 tool_result 拍平成 `{tool_use_id}`.
///
/// ⚠️ 只取 id, **不取内容** (隐私纪律: 关联不需要知道工具返回了什么).
pub fn extract_answered_call_ids(body: &serde_json::Value) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let Some(msgs) = body.get("messages").and_then(|m| m.as_array()) else {
        return out;
    };
    for m in msgs {
        // content 可能是数组 (多模态/工具结果) 或字符串 (纯文本).
        let Some(items) = m.get("content").and_then(|c| c.as_array()) else { continue };
        for it in items {
            let ty = it.get("type").and_then(|t| t.as_str()).unwrap_or("");
            let id = if ty == "tool_result" {
                it.get("tool_use_id").and_then(|v| v.as_str())
            } else if ty == "tool" {
                it.get("tool_call_id").and_then(|v| v.as_str())
            } else {
                // 个别客户端不带 type, 直接拍平 id 字段 —— 只在 id 字段存在时收.
                if it.get("tool_use_id").is_some() {
                    it.get("tool_use_id").and_then(|v| v.as_str())
                } else {
                    it.get("tool_call_id").and_then(|v| v.as_str())
                }
            };
            if let Some(id) = id {
                if !id.is_empty() && !out.iter().any(|x| x == id) {
                    out.push(id.to_string());
                }
            }
        }
    }
    out
}

/// 判断本次请求是否是**新的一轮** (用户刚说话) 而非 Agent 的工具结果回报.
///
/// 判据: 最后一条消息 role == "user" **且**其 content 不是 tool_result/tool 数组。
///
/// ⚠️ 为什么必须排除 tool_result: Anthropic 协议里工具结果**也是 user 角色**
/// (`{role:"user", content:[{type:"tool_result",...}]}`)。只看 role 会把"工具结果回报"
/// 误判成"用户刚说话" → 轮次编号虚增, 同一轮被拆成两轮。
/// (调用处 `classify_session` 另有 `answered.is_empty()` 兜底, 但函数自身也该判对 ——
///  孤立可判的函数才不会在别的调用点被踩。)
pub fn is_new_turn(body: &serde_json::Value) -> bool {
    let Some(msgs) = body.get("messages").and_then(|m| m.as_array()) else {
        return false;
    };
    let Some(last) = msgs.last() else {
        return false;
    };
    if last.get("role").and_then(|r| r.as_str()) != Some("user") {
        return false;
    }
    // content 是数组且含 tool 结果 → 不是新轮 (那是工具结果的回报)。
    if let Some(items) = last.get("content").and_then(|c| c.as_array()) {
        let is_tool_result = items.iter().any(|it| {
            let ty = it.get("type").and_then(|t| t.as_str()).unwrap_or("");
            ty == "tool_result" || ty == "tool"
                || it.get("tool_use_id").is_some()
                || it.get("tool_call_id").is_some()
        });
        if is_tool_result {
            return false;
        }
    }
    true
}

/// 兜底会话指纹: system 提示词 + 首条用户消息的**内容哈希**.
///
/// ⚠️ **只存哈希, 不存内容**: 指纹的唯一用途是"把没有 session id 的客户端的连续请求
/// 归到一起"。存哈希就够, 存原文等于把对话写进第二份存储 (本项目 recall 已踩过
/// "留存含思考明文"的隐私代价)。
/// 取 system + 首条 user 而非全部消息: 一轮会话里这两者在整个会话中稳定,
/// 而中间的 assistant 回复每轮都变 (拿它做指纹等于每轮都是新会话)。
pub fn session_fingerprint(body: &serde_json::Value) -> Option<String> {
    let msgs = body.get("messages").and_then(|m| m.as_array())?;
    let mut material = String::new();
    // system: 可能是顶层字段 (chat) 或首条 system 消息.
    if let Some(s) = body.get("system").and_then(|v| v.as_str()) {
        material.push_str(s);
    } else if let Some(first) = msgs.first() {
        if first.get("role").and_then(|r| r.as_str()) == Some("system") {
            if let Some(s) = first.get("content").and_then(|c| c.as_str()) {
                material.push_str(s);
            }
        }
    }
    // 首条 user 消息的文本 (多模态取 text parts).
    for m in msgs {
        if m.get("role").and_then(|r| r.as_str()) != Some("user") {
            continue;
        }
        match m.get("content") {
            Some(serde_json::Value::String(s)) => material.push_str(s),
            Some(serde_json::Value::Array(parts)) => {
                for p in parts {
                    if let Some(t) = p.get("text").and_then(|v| v.as_str()) {
                        material.push_str(t);
                    }
                }
            }
            _ => {}
        }
        break; // 只取第一条
    }
    if material.trim().is_empty() {
        return None;
    }
    Some(format!("sha256:{:016x}", fnv1a64(material.as_bytes())))
}

/// FNV-1a 64 位哈希 (无第三方依赖; 本项目单 exe 零外部依赖的交付形态).
///
/// 为什么够用: 这里要的是"同一会话的多次请求落同值", 不是抗碰撞。
/// 16 hex = 64 bit, 对几十个会话规模碰撞概率可忽略。
fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

/// 主入口: 从请求头 + 请求体算出本次请求的会话归属.
///
/// `headers` 用于取客户端自报的 session id (精确); `body` 用于兜底指纹与轮次判定.
/// `tracker` 提供轮次编号 (进程内).
pub fn classify_session(
    headers: &axum::http::HeaderMap,
    body: &serde_json::Value,
    tracker: &SessionTracker,
) -> SessionRef {
    // ① 客户端自报的 session id (最高优先, 精确)
    let header_key = ["x-claude-code-session-id", "session-id", "x-session-id"]
        .iter()
        .find_map(|h| headers.get(*h).and_then(|v| v.to_str().ok()).map(|s| s.trim().to_string()))
        .filter(|s| !s.is_empty());
    // `prompt_cache_key` 次之: 它不是一个显式 session id, 但同一会话内稳定, 且由客户端
    // 自己算出来 —— 比内容指纹可靠 (不会因为"两条消息内容相同"而误并)。
    let cache_key = headers
        .get("prompt_cache_key")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    let (session_key, session_source) = if let Some(k) = header_key {
        (k, "header".to_string())
    } else if let Some(k) = cache_key {
        (k, "header".to_string())
    } else if let Some(fp) = session_fingerprint(body) {
        (fp, "fingerprint".to_string())
    } else {
        (String::new(), "none".to_string())
    };

    let answered = extract_answered_call_ids(body);
    // turn 语义 = **会话内第 N 跳** (不是"第 N 轮")。两者刻意不等价:
    //   · 一轮用户输入 → 模型出工具调用 → 工具结果回报 → 模型再答 = **4 跳 / 1 轮**;
    //   · 用"轮"会让工具结果那一跳无处安放 (它既不是新轮, 又确实是一次请求)。
    // 判据取"末条 user 且不是工具结果回报 **或** 有工具结果回报" —— 两者都推进跳数,
    // 只是前者开新轮、后者续当前轮 (前端若要显示"轮", 按 answers_to 是否空自分段即可)。
    let new_turn = answered.is_empty() && is_new_turn(body);
    let turn = if new_turn || !answered.is_empty() {
        tracker.next_turn(&session_key)
    } else {
        0
    };

    SessionRef {
        session_key,
        turn,
        answers_to: answered,
        session_source,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(msgs: &[serde_json::Value]) -> serde_json::Value {
        serde_json::json!({ "messages": msgs })
    }

    /// 精确关联的核心: 上次产出的 tool_call id 出现在本次请求 → `answers_to` 非空。
    #[test]
    fn answers_to_extracts_openai_and_anthropic_forms() {
        // OpenAI chat: {type:"tool", tool_call_id}
        let b = body(&[serde_json::json!({"role":"user","content":[
            {"type":"tool","tool_call_id":"call_A","content":"ok"}]})]);
        assert_eq!(extract_answered_call_ids(&b), vec!["call_A".to_string()]);

        // Anthropic: {type:"tool_result", tool_use_id}
        let b = body(&[serde_json::json!({"role":"user","content":[
            {"type":"tool_result","tool_use_id":"toolu_1","content":"ok"}]})]);
        assert_eq!(extract_answered_call_ids(&b), vec!["toolu_1".to_string()]);

        // 拍平形态 (无 type)
        let b = body(&[serde_json::json!({"role":"user","content":[
            {"tool_use_id":"toolu_9"}]})]);
        assert_eq!(extract_answered_call_ids(&b), vec!["toolu_9".to_string()]);

        // 同一条里多个结果 → 全部收到 (顺序保持, 去重)
        let b = body(&[serde_json::json!({"role":"user","content":[
            {"type":"tool","tool_call_id":"a"},{"type":"tool","tool_call_id":"b"},
            {"type":"tool","tool_call_id":"a"}]})]);
        assert_eq!(extract_answered_call_ids(&b), vec!["a".to_string(), "b".to_string()]);

        // 纯文本 user → 空 (没有回报任何调用)
        let b = body(&[serde_json::json!({"role":"user","content":"hi"})]);
        assert!(extract_answered_call_ids(&b).is_empty());
    }

    /// "新的一轮"判据: 末条 user **且**没有 tool 结果回报。
    /// 这两个条件必须同时成立 —— 只有 user 会漏 (Anthropic 的 tool_result 也是 user 角色)。
    #[test]
    fn new_turn_needs_both_conditions() {
        // 末条 user + 无回报 → 新轮
        assert!(is_new_turn(&body(&[serde_json::json!({"role":"user","content":"hi"})])));
        // 末条 user 但是 tool_result → **不是**新轮
        assert!(!is_new_turn(&body(&[serde_json::json!({"role":"user","content":[
            {"type":"tool_result","tool_use_id":"x"}]})])));
        // 末条 assistant → 不是新轮
        assert!(!is_new_turn(&body(&[serde_json::json!({"role":"assistant","content":"x"})])));
        // 空 messages → 不是新轮 (无法判定就不判)
        assert!(!is_new_turn(&body(&[])));
    }

    /// 兜底指纹: 同一会话的两次请求 (system+首条 user 相同) 必须落同值;
    /// 不同首条 user → 不同值。**且只存哈希, 不存内容**。
    #[test]
    fn fingerprint_is_stable_and_content_free() {
        let mk = |first: &str| serde_json::json!({
            "system": "SYSTEM PROMPT",
            "messages": [{"role":"user","content":first},{"role":"assistant","content":"a"},
                         {"role":"user","content":"later"}],
        });
        let f1 = session_fingerprint(&mk("first question")).expect("应有指纹");
        let f2 = session_fingerprint(&mk("first question")).expect("应有指纹");
        assert_eq!(f1, f2, "同会话两次请求必须同指纹");
        assert!(f1.starts_with("sha256:"), "形态应标明是哈希: {f1}");
        assert!(!f1.contains("first question"), "指纹不得含内容原文: {f1}");
        assert!(!f1.contains("SYSTEM"), "指纹不得含 system 原文: {f1}");
        assert_ne!(f1, session_fingerprint(&mk("other")).unwrap(), "不同会话应不同指纹");
        // 无 system 无 user → None (不硬造)
        assert!(session_fingerprint(&body(&[])).is_none());
    }

    /// 轮次编号: 同一会话递增, 不同会话互不干扰, 空 key 不编号。
    #[test]
    fn turn_numbers_increment_per_session() {
        let t = SessionTracker::new();
        assert_eq!(t.next_turn("s1"), 1);
        assert_eq!(t.next_turn("s2"), 1, "不同会话各自从 1 起");
        assert_eq!(t.next_turn("s1"), 2);
        assert_eq!(t.next_turn("s1"), 3);
        assert_eq!(t.next_turn(""), 0, "空 key 无法识别 → 不编号");
    }



    /// 端到端归类: header 优先于指纹 (客户端自报的最可靠)。
    #[test]
    fn classify_prefers_header_over_fingerprint() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("x-claude-code-session-id", "sess-1".parse().unwrap());
        let b = serde_json::json!({"system":"S","messages":[{"role":"user","content":"hi"}]});
        let tracker = SessionTracker::new();
        let r = classify_session(&headers, &b, &tracker);
        assert_eq!(r.session_key, "sess-1");
        assert_eq!(r.session_source, "header", "来自头的必须是 header 来源");
        assert_eq!(r.turn, 1, "首条 user → 第 1 轮");

        // 无 header 时回落指纹
        let empty = axum::http::HeaderMap::new();
        let r2 = classify_session(&empty, &b, &tracker);
        assert!(r2.session_key.starts_with("sha256:"), "无头应回落指纹: {}", r2.session_key);
        assert_eq!(r2.session_source, "fingerprint");
    }
}
