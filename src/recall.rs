//! 降级可回取 — 留存「转发优化」被剔离/截断的原文, 供事后核查.
//!
//! ## 为什么需要这个模块
//!
//! AIGate 的转发优化是**有损**的: 剥离历史推理链 (`remove_reasoning_fields` 直接
//! `m.remove(k)`)、截断超长 tool 输出、裁剪历史轮次 —— 内容被丢进上游请求前就
//! **永久消失**了. 面板只留下"省了多少 token"的计数, 却**无法回答"这次到底删了什么"**.
//! 用户若怀疑模型变笨源于剥离, 只能关开关 (对后续请求生效), 当次请求已无从复查.
//!
//! 本模块把这个缺口补上: 每次优化把**被移除的内容**留存一份, 并在写请求日志时记下
//! 可回取的引用, 面板「请求详情」里据此查看/导出原文. 参照 RTK 的 recall store 设计
//! (`[full output: rtk recall <hash>]` + `rtk gain --recalls` 统计回取频率), 但按本
//! 项目的约束做了简化与取舍.
//!
//! ## 关键取舍 (与 RTK 的差异, 都是刻意的)
//!
//! 1. **默认关闭**. RTK 的 recall 是无条件留存; 但本项目落盘含**对话明文**(见
//!    `Config::cache_persist_path` 的同类警示), 留存推理链等于留存模型思考内容.
//!    故默认关, 由用户在设置页显式开启并接受落盘风险.
//! 2. **有界留存**. 内容寻址 + 单项上限 + 条目数上限 + FIFO 淘汰, 避免无限增长.
//!    同一内容重复出现时天然去重 (内容寻址的副作用, 与 RTK 一致).
//! 3. **失败绝不影响转发**. 任何 IO/序列化错误只记 warn 并放弃留存 —— 这是转发路径,
//!    留存是旁路, 不能因为它让请求失败 (与 RTK "never-block-the-user" 同哲学).
//!
//! ## 存储布局
//!
//! ```text
//! data/recall/
//!   index.jsonl     每行一条元数据 (id/时间/来源/字节数/摘要), 便于面板列举与统计
//!   <id>.txt        被移除的原文 (按内容哈希命名, 同内容自动复用)
//! ```
//!
//! `<id>` = 内容字节的 FNV-1a 64 位哈希的十六进制 (16 字符). 不用加密哈希是刻意的:
//! 这里只需要"同内容同 id"的去重语义与稳定命名, 不承担安全职责, 且零依赖.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

/// 留存内容的来源 —— 面板据此说明"这份原文是被哪个优化移除的".
///
/// 序列化为 snake_case, 前端拼 `recall_kind_<kind>` 取 i18n 文案
/// (故新增种类时必须同步补中英文键, 否则界面显示裸键名).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecallKind {
    /// 剥离的历史推理链 (普通轮次).
    ReasoningPlain,
    /// 剥离的历史推理链 (带 tool_calls 的轮次).
    ReasoningToolcall,
    /// 截断的超长 tool 输出.
    ToolOutputTruncated,
    /// 长会话历史裁剪丢弃的轮次.
    HistoryTrimmed,
    /// 响应缓存命中而**未**发往上游的请求体 (回放时留档).
    RespCacheSkipped,
}

/// 一条留存记录的元数据 (写入 index.jsonl, 也是面板列举的内容).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallEntry {
    /// 内容哈希 (16 位十六进制), 即文件名的主体.
    pub id: String,
    /// 写入时间 (unix 秒).
    pub ts: u64,
    /// 来源优化类型.
    pub kind: RecallKind,
    /// 被移除内容的原始字节数 (注意: 是内容本身, 不是估计的 token).
    pub bytes: usize,
    /// 单行摘要 (前 120 字符, 已折叠换行) —— 让面板不必读全文就能辨认.
    pub preview: String,
    /// 关联的请求 (模型名 / 上游模型), 便于在请求详情里对上号.
    #[serde(default)]
    pub model: Option<String>,
}

/// 留存统计 (面板展示"留存了多少 / 是否被查看过").
#[derive(Debug, Clone, Default, Serialize)]
pub struct RecallStats {
    /// 已留存条目数.
    pub entries: u64,
    /// 已留存内容总字节数.
    pub bytes: u64,
    /// 累计回取 (被查看) 次数 —— 用于回答"留了到底有没有用".
    pub recalls: u64,
    /// 因超上限而被丢弃的条目数.
    pub evicted: u64,
    /// 留存是否启用.
    pub enabled: bool,
}

/// 留存配置 (与 `config.rs` 的环境变量对应, 运行时可切).
#[derive(Debug, Clone)]
pub struct RecallConfig {
    pub enabled: bool,
    /// 单条内容留存上限 (字节). 超过则只留头部 + 标记, 避免单条撑爆磁盘.
    pub max_entry_bytes: usize,
    /// 条目数上限 (超出按 FIFO 淘汰最旧).
    pub max_entries: usize,
    /// 正文预览截断长度.
    pub preview_chars: usize,
}

impl Default for RecallConfig {
    fn default() -> Self {
        Self {
            // 默认关: 落盘含模型思考明文, 需用户显式接受.
            enabled: false,
            max_entry_bytes: 256 * 1024,
            max_entries: 200,
            preview_chars: 120,
        }
    }
}

/// 内容寻址留存库.
pub struct RecallStore {
    dir: PathBuf,
    index_path: PathBuf,
    /// 运行时配置 (AtomicBool 语义用 Mutex 包一层即可, 写入极低频).
    cfg: Mutex<RecallConfig>,
    /// 累计回取次数 (进程内计数; 持久化的总量在 index 文件里推导).
    recalls: AtomicU64,
    /// 累计淘汰数.
    evicted: AtomicU64,
}

impl RecallStore {
    /// 创建留存库 (自动建目录).
    pub fn new(data_dir: &str, cfg: RecallConfig) -> Self {
        let dir = Path::new(data_dir).join("recall");
        let _ = std::fs::create_dir_all(&dir);
        Self {
            index_path: dir.join("index.jsonl"),
            dir,
            cfg: Mutex::new(cfg),
            recalls: AtomicU64::new(0),
            evicted: AtomicU64::new(0),
        }
    }

    /// 当前是否启用留存.
    pub fn is_enabled(&self) -> bool {
        self.cfg.lock().map(|c| c.enabled).unwrap_or(false)
    }

    /// 运行时切换开关 (面板).
    pub fn set_enabled(&self, on: bool) {
        if let Ok(mut c) = self.cfg.lock() {
            c.enabled = on;
        }
    }

    /// 读取当前配置的副本 (面板展示上限值).
    pub fn config(&self) -> RecallConfig {
        self.cfg.lock().map(|c| c.clone()).unwrap_or_default()
    }

    /// 内容哈希 → 文件名主体. FNV-1a 风格 (用 std 默认哈希器, 零依赖).
    fn content_id(content: &[u8]) -> String {
        let mut h = DefaultHasher::new();
        content.hash(&mut h);
        format!("{:016x}", h.finish())
    }

    /// 单行摘要: 折叠空白 + 截断.
    fn make_preview(content: &str, max_chars: usize) -> String {
        let flat: String = content
            .chars()
            .map(|c| if c == '\n' || c == '\r' || c == '\t' { ' ' } else { c })
            .collect();
        let collapsed = flat.split_whitespace().collect::<Vec<_>>().join(" ");
        if collapsed.chars().count() <= max_chars {
            collapsed
        } else {
            let mut s: String = collapsed.chars().take(max_chars).collect();
            s.push('…');
            s
        }
    }

    /// 留存一份被移除的内容. 返回其 id (供写进请求日志); 未启用或失败时返回 None.
    ///
    /// **绝不返回 Err**: 留存是旁路, 失败只记 warn, 不能影响转发.
    pub fn store(
        &self,
        kind: RecallKind,
        content: &str,
        model: Option<&str>,
    ) -> Option<String> {
        if !self.is_enabled() || content.is_empty() {
            return None;
        }
        let cfg = self.config();
        let id = Self::content_id(content.as_bytes());

        // 内容寻址去重: 同内容已存过就不再写文件 (但元数据仍记一条, 以保留"这次也命中了").
        let file = self.dir.join(format!("{id}.txt"));
        if !file.exists() {
            // 单项上限: 超长只留头部并显式标注, 避免单条撑爆磁盘.
            let body = if content.len() > cfg.max_entry_bytes {
                let cut = floor_char_boundary(content, cfg.max_entry_bytes);
                format!(
                    "{}\n\n...[recall 留存截断: 原 {} 字节, 仅保留前 {} 字节]...",
                    &content[..cut],
                    content.len(),
                    cut
                )
            } else {
                content.to_string()
            };
            if std::fs::write(&file, body.as_bytes()).is_err() {
                tracing::warn!("recall: failed to persist entry {id}");
                return None;
            }
        }

        let entry = RecallEntry {
            id: id.clone(),
            ts: now_secs(),
            kind,
            bytes: content.len(),
            preview: Self::make_preview(content, cfg.preview_chars),
            model: model.map(|s| s.to_string()),
        };
        if let Ok(line) = serde_json::to_string(&entry) {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.index_path)
            {
                let _ = writeln!(f, "{line}");
            } else {
                tracing::warn!("recall: failed to append index");
                return None;
            }
        } else {
            return None;
        }

        self.enforce_limits(&cfg);
        Some(id)
    }

    /// 条目数超上限时按 FIFO 淘汰 (删最旧的元数据行与无引用正文).
    fn enforce_limits(&self, cfg: &RecallConfig) {
        let entries = match self.read_index() {
            Some(e) => e,
            None => return,
        };
        if entries.len() <= cfg.max_entries {
            return;
        }
        let drop_n = entries.len() - cfg.max_entries;
        let (drop, keep) = entries.split_at(drop_n);
        // 被淘汰条目若其内容不再被任何保留条目引用, 则删正文文件.
        let kept_ids: std::collections::HashSet<&str> =
            keep.iter().map(|e| e.id.as_str()).collect();
        for e in drop {
            if !kept_ids.contains(e.id.as_str()) {
                let _ = std::fs::remove_file(self.dir.join(format!("{}.txt", e.id)));
            }
        }
        let mut out = String::with_capacity(keep.len() * 96);
        for e in keep {
            if let Ok(l) = serde_json::to_string(e) {
                out.push_str(&l);
                out.push('\n');
            }
        }
        if std::fs::write(&self.index_path, out.as_bytes()).is_ok() {
            self.evicted.fetch_add(drop_n as u64, Ordering::Relaxed);
        }
    }

    /// 读取全部索引 (按写入顺序, 最旧在前).
    fn read_index(&self) -> Option<Vec<RecallEntry>> {
        let content = std::fs::read_to_string(&self.index_path).ok()?;
        Some(
            content
                .lines()
                .filter(|l| !l.trim().is_empty())
                .filter_map(|l| serde_json::from_str::<RecallEntry>(l).ok())
                .collect(),
        )
    }

    /// 列出最近 `limit` 条留存记录 (最新在前) —— 面板用.
    pub fn list(&self, limit: usize) -> Vec<RecallEntry> {
        let mut v = self.read_index().unwrap_or_default();
        v.reverse();
        v.truncate(limit);
        v
    }

    /// 取回某条被移除的原文 (并计入回取次数).
    pub fn get(&self, id: &str) -> Option<String> {
        // 防御: id 只应为十六进制, 拒绝任何路径分隔符 (否则可穿越目录).
        if id.is_empty()
            || id.len() > 64
            || !id.chars().all(|c| c.is_ascii_hexdigit())
        {
            return None;
        }
        let content = std::fs::read_to_string(self.dir.join(format!("{id}.txt"))).ok()?;
        self.recalls.fetch_add(1, Ordering::Relaxed);
        Some(content)
    }

    /// 统计 (不含回取次数 —— 那是进程内计数, 与历史日志口径不同, 面板分别展示).
    pub fn stats(&self) -> RecallStats {
        let entries = self.read_index().unwrap_or_default();
        RecallStats {
            entries: entries.len() as u64,
            bytes: entries.iter().map(|e| e.bytes as u64).sum(),
            recalls: self.recalls.load(Ordering::Relaxed),
            evicted: self.evicted.load(Ordering::Relaxed),
            enabled: self.is_enabled(),
        }
    }

    /// 清空全部留存 (面板"清空"按钮).
    pub fn clear(&self) -> usize {
        let n = self.read_index().map(|v| v.len()).unwrap_or(0);
        let _ = std::fs::remove_file(&self.index_path);
        if let Ok(rd) = std::fs::read_dir(&self.dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().and_then(|s| s.to_str()) == Some("txt") {
                    let _ = std::fs::remove_file(p);
                }
            }
        }
        n
    }
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 向下取整到最近的 char 边界 (避免切断多字节字符造成 panic).
fn floor_char_boundary(s: &str, mut idx: usize) -> usize {
    if idx >= s.len() {
        return s.len();
    }
    while idx > 0 && !s.is_char_boundary(idx) {
        idx -= 1;
    }
    idx
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_store(name: &str, enabled: bool) -> RecallStore {
        let dir = std::env::temp_dir().join(format!("aigate_recall_test_{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        RecallStore::new(dir.to_str().unwrap(), RecallConfig {
            enabled,
            ..Default::default()
        })
    }

    /// 默认关闭时不得写任何东西 —— 落盘含明文, 必须显式开启.
    #[test]
    fn disabled_by_default_writes_nothing() {
        let s = tmp_store("disabled", false);
        assert!(!s.is_enabled());
        assert_eq!(s.store(RecallKind::ReasoningPlain, "secret reasoning", None), None);
        assert_eq!(s.stats().entries, 0);
    }

    /// 开启后可留存并原样取回 (字节一致是核心保证: 这是"回看被删了什么"的唯一依据).
    #[test]
    fn store_then_get_roundtrip() {
        let s = tmp_store("roundtrip", true);
        let body = "推理链内容\nline2\t tab";
        let id = s.store(RecallKind::ReasoningToolcall, body, Some("m1")).expect("应留存");
        assert_eq!(s.get(&id).as_deref(), Some(body));
        let list = s.list(10);
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].kind, RecallKind::ReasoningToolcall);
        assert_eq!(list[0].bytes, body.len());
        assert_eq!(list[0].model.as_deref(), Some("m1"));
        assert_eq!(s.stats().recalls, 1, "get 应计入回取次数");
    }

    /// 内容寻址: 同内容重复留存只占一份正文, 但元数据各记一条 (保留"也命中过"的事实).
    #[test]
    fn same_content_dedupes_file_but_keeps_metadata() {
        let s = tmp_store("dedupe", true);
        let a = s.store(RecallKind::ReasoningPlain, "same-body", None).unwrap();
        let b = s.store(RecallKind::ReasoningToolcall, "same-body", None).unwrap();
        assert_eq!(a, b, "同内容应得到同一 id");
        assert_eq!(s.stats().entries, 2, "元数据各记一条");
        // 正文文件只有一个. 注意 new() 会在传入目录下再建 `recall/` 子目录.
        let txt = std::fs::read_dir(
            std::env::temp_dir().join("aigate_recall_test_dedupe").join("recall"),
        )
        .unwrap()
        .flatten()
        .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("txt"))
        .count();
        assert_eq!(txt, 1, "同内容只应有一个正文文件");
    }

    /// 超单项上限时截断并显式标注 (不能静默截断, 否则回取内容看似完整实则缺失).
    #[test]
    fn oversized_entry_is_capped_and_marked() {
        let dir = std::env::temp_dir().join("aigate_recall_test_cap");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = RecallStore::new(dir.to_str().unwrap(), RecallConfig {
            enabled: true,
            max_entry_bytes: 100,
            ..Default::default()
        });
        let big = "x".repeat(500);
        let id = s.store(RecallKind::ToolOutputTruncated, &big, None).unwrap();
        let got = s.get(&id).unwrap();
        assert!(got.len() < big.len());
        assert!(got.contains("recall 留存截断"), "截断必须有显式标注");
        assert!(got.contains("原 500 字节"));
    }

    /// 多字节安全: 上限切在 UTF-8 中间时不得 panic.
    #[test]
    fn cap_respects_utf8_boundaries() {
        let dir = std::env::temp_dir().join("aigate_recall_test_utf8");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = RecallStore::new(dir.to_str().unwrap(), RecallConfig {
            enabled: true,
            max_entry_bytes: 7, // "中文中文" 每字 3 字节 -> 7 落在字符中间
            ..Default::default()
        });
        let id = s.store(RecallKind::ReasoningPlain, "中文中文中文", None).unwrap();
        let got = s.get(&id).unwrap();
        assert!(got.contains("中文"), "应保留完整字符: {got:?}");
    }

    /// 路径穿越防御: id 只接受十六进制, 拒绝任何可疑输入.
    #[test]
    fn get_rejects_path_traversal() {
        let s = tmp_store("traversal", true);
        assert_eq!(s.get("../../secret"), None);
        assert_eq!(s.get("abc/def"), None);
        assert_eq!(s.get(""), None);
        assert_eq!(s.get(&"z".repeat(80)), None);
    }

    /// 条目数超上限时 FIFO 淘汰最旧, 且不影响剩余条目的可回取性.
    #[test]
    fn evicts_oldest_over_limit() {
        let dir = std::env::temp_dir().join("aigate_recall_test_evict");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = RecallStore::new(dir.to_str().unwrap(), RecallConfig {
            enabled: true,
            max_entries: 3,
            ..Default::default()
        });
        let ids: Vec<String> = (0..5)
            .map(|i| s.store(RecallKind::ReasoningPlain, &format!("body-{i}"), None).unwrap())
            .collect();
        let list = s.list(10);
        assert_eq!(list.len(), 3, "应保留最近 3 条");
        // 最新的可回取
        assert!(s.get(&ids[4]).is_some());
        // 最旧的正文已被清理
        assert!(s.get(&ids[0]).is_none(), "被淘汰条目的正文应删除");
        assert!(s.stats().evicted >= 2);
    }

    /// clear 应移除索引与全部正文.
    #[test]
    fn clear_removes_everything() {
        let s = tmp_store("clear", true);
        let id = s.store(RecallKind::ReasoningPlain, "body", None).unwrap();
        assert_eq!(s.clear(), 1);
        assert_eq!(s.stats().entries, 0);
        assert!(s.get(&id).is_none());
    }

    /// preview 折叠换行并截断 —— 面板不必读全文即可辨认.
    #[test]
    fn preview_is_single_line_and_truncated() {
        let dir = std::env::temp_dir().join("aigate_recall_test_preview");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = RecallStore::new(dir.to_str().unwrap(), RecallConfig {
            enabled: true,
            preview_chars: 10,
            ..Default::default()
        });
        s.store(RecallKind::ReasoningPlain, "a\nb\tc   d e f g h i j k", None).unwrap();
        let e = &s.list(1)[0];
        assert!(!e.preview.contains('\n'), "preview 不得含换行");
        assert!(e.preview.chars().count() <= 11, "preview 应被截断: {:?}", e.preview);
    }
}
