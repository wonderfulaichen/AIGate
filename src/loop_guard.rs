//! 模型死循环（退化循环/重复输出）检测。
//!
//! 在代理层转发 SSE 流时，实时累积下游模型产出的增量文本，
//! 检测"最近窗口内某子串连续重复达到阈值"的退化循环模式，
//! 一旦命中立即截断流，避免无意义的无限生成拖垮客户端。

/// 模型死循环检测器。
///
/// 维护一个环形文本缓冲（上限 [`LoopDetector::max_buffer`] 字符），
/// 每次喂入增量文本后，在最近 [`LoopDetector::window`] 字符内检测
/// 是否存在长度为 L 的子串连续重复 >= [`LoopDetector::min_repeat`] 次。
pub struct LoopDetector {
    /// 累积的近期文本（超出上限按字符截断头部）。
    buf: String,
    /// 检测窗口字符数（仅在最近 N 个字符内检测重复）。
    window: usize,
    /// 连续重复最小次数（达到即判循环）。
    min_repeat: usize,
    /// 环形缓冲上限字符数。
    max_buffer: usize,
    /// 一旦判定循环即锁定，避免重复触发。
    triggered: bool,
}

impl LoopDetector {
    pub fn new(window: usize, min_repeat: usize, max_buffer: usize) -> Self {
        let max_buffer = max_buffer.max(window.saturating_mul(2));
        Self {
            buf: String::new(),
            window,
            min_repeat,
            max_buffer,
            triggered: false,
        }
    }

    /// 喂入一段增量文本（如 SSE `delta.content` / `delta.reasoning_content`）。
    /// 返回当前是否已判定为死循环。
    pub fn feed(&mut self, delta: &str) -> bool {
        if self.triggered || delta.is_empty() {
            return self.triggered;
        }
        self.buf.push_str(delta);
        // 环形截断：超出上限时按字符丢弃头部，避免切坏 UTF-8。
        if self.buf.len() > self.max_buffer {
            let excess = self.buf.len() - self.max_buffer;
            if let Some((idx, _)) = self.buf.char_indices().nth(excess) {
                self.buf.drain(..idx);
            }
        }
        if self.detect() {
            self.triggered = true;
        }
        self.triggered
    }

    /// 是否已判定为死循环（幂等）。
    pub fn triggered(&self) -> bool {
        self.triggered
    }

    /// 最近窗口内的文本样本 (诊断用): 返回缓冲尾部最多 `window` 个**字符**,
    /// 供截断日志附带输出, 用于事后区分「思考内容重复(误报)」与「正文重复(真循环)」.
    /// 按字符边界截取, 避免 UTF-8 多字节字符内部断点触发 panic
    /// (上游流中含中文/日文时, 按字节偏移切 window 极易切在字符中间).
    pub fn recent_text(&self) -> &str {
        // 找到「倒数第 window 个字符」的字节起点, 作为切片起点.
        let total = self.buf.chars().count();
        let take = self.window.min(total);
        if take == 0 {
            return "";
        }
        let skip = total - take;
        if let Some((idx, _)) = self.buf.char_indices().nth(skip) {
            &self.buf[idx..]
        } else {
            // 理论上 unreachable (skip < total == char_indices 数量), 兜底返回空串.
            ""
        }
    }

    /// 单元是否含"有意义字符"（字母/数字/汉字等），用于区分结构化噪声与真循环。
    ///
    /// 纯空白/标点/表格线（空格、换行、`|`、`-`、`#`、`。，；：` 等）在代码块、Markdown
    /// 表格、列表里天然高重复，若与正文用同一阈值会被误杀（实机 go 端点反复截断长技术文档）。
    /// 仅当重复单元含至少一个有意义字符时，才用 [`LoopGuardConfig::min_repeat`] 原阈值；
    /// 纯噪声单元需更高阈值（见 [`Self::noise_repeat`]）。
    fn has_meaningful(c: char) -> bool {
        c.is_alphanumeric() || c.is_alphabetic() // ASCII/Unicode 字母与数字 (含汉字)
    }

    /// 噪声单元（纯空白/标点/表格线）需要达到的重复次数：比 `min_repeat` 更宽松，
    /// 避免结构化输出被误杀。取 `min_repeat + 6` 与 12 的较大者。
    fn noise_repeat(&self) -> usize {
        (self.min_repeat + 6).max(12)
    }

    /// 在窗口尾部检测子串连续重复。
    fn detect(&self) -> bool {
        let chars: Vec<char> = self.buf.chars().collect();
        let n = chars.len();
        let win = self.window.min(n);
        if win == 0 {
            return false;
        }
        // 单字符极端重复（如 "。。。。。。。。") 单独用更高阈值保护，
        // 避免正常省略号（"...")被误判。
        if win >= 12 {
            let tail = &chars[n - 12..];
            if tail.iter().all(|&c| c == tail[0]) {
                return true;
            }
        }
        // 多字符片段连续重复：枚举重复单元长度 L（2..=max_l）。
        // 判据：仅当重复单元「含语义字符」(字母/数字/汉字) 时用 `min_repeat` 原阈值；
        // 纯噪声单元（短单元 l<=4 且全为空白/标点/表格线，如缩进空格、`|---|`、列表标记）
        // 用更宽松的 `noise_repeat()`，避免代码块/Markdown 表格的合法重复被误杀。
        let max_l = (win / self.min_repeat).max(1);
        for l in 2..=max_l {
            // 取尾部 l 字符作为候选重复单元（退化循环必在输出末尾）。
            if l > n {
                continue;
            }
            let unit = &chars[n - l..];
            // 纯噪声单元（全为空白/标点/表格线，不含字母/数字/汉字）用更宽松阈值，
            // 避免代码块/Markdown 表格的合法重复被误杀；含语义字符按原阈值判真循环。
            let is_noise = !unit.iter().any(|&c| Self::has_meaningful(c));
            let required = if is_noise {
                self.noise_repeat()
            } else {
                self.min_repeat
            };
            let need = l * required;
            if need > win {
                continue;
            }
            let tail_start = n - need;
            let mut ok = true;
            for k in 1..required {
                let seg = &chars[tail_start + k * l..tail_start + k * l + l];
                if seg != unit {
                    ok = false;
                    break;
                }
            }
            if ok {
                return true;
            }
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_multi_char_repeat() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let mut hit = false;
        for _ in 0..6 {
            hit = d.feed("循环输出");
        }
        assert!(hit, "连续6次重复片段应判为循环");
    }

    #[test]
    fn detects_single_char_run() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let mut hit = false;
        for _ in 0..15 {
            hit = d.feed("。");
        }
        assert!(hit, "15个连续相同单字应判为循环");
    }

    #[test]
    fn no_false_positive_on_normal_text() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let text = "这是一段正常的模型回复，包含若干不同的句子。我们应该相信用户提供的上下文，并据此给出合理的回答。代码生成的输出通常包含缩进与符号，但不应被判定为循环。";
        let hit = d.feed(text);
        assert!(!hit, "正常文本不应误判为循环");
    }

    #[test]
    fn ellipsis_not_false_positive() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let hit = d.feed("……");
        assert!(!hit, "正常省略号不应误判");
    }

    #[test]
    fn triggered_is_sticky() {
        let mut d = LoopDetector::new(384, 6, 4096);
        for _ in 0..6 {
            d.feed("ab");
        }
        assert!(d.triggered());
        assert!(d.feed("正常文本"), "触发后应保持触发态");
    }

    /// 回归: 上游流含中文时 recent_text 按字节偏移切 window 落在 UTF-8 字符内部,
    /// 触发 "start byte index N is not a char boundary" panic (实机错误位置 src\loop_guard.rs:66).
    /// 修复后应按字符边界安全截取最后 window 个字符, 不 panic 且返回合法 &str.
    #[test]
    fn recent_text_chinese_no_panic_on_byte_offset() {
        // LoopDetector::new(window, min_repeat, max_buffer).
        // window=100 字符, min_repeat=6 (循环判定的重复次数阈值), max_buffer=4096 字节.
        let mut d = LoopDetector::new(100, 6, 4096);
        let chunk = "这是一段中文输出，包含多字节字符。";
        for _ in 0..20 {
            d.feed(chunk);
        }
        // 关键调用: 修复前按字节切易 panic, 修复后必须安全返回.
        let sample = d.recent_text();
        // 修复后语义为「最后 window=100 个字符」: 中文每字 3 字节,
        // 上限字节 ≈ 100 * 3 = 300; ASCII 字数上限 = 100 字节.
        assert!(sample.len() <= 100 * 3, "窗口文本字节长度应不超过 window*max_utf8_bytes");
        assert!(std::str::from_utf8(sample.as_bytes()).is_ok(), "必须落在 UTF-8 字符边界");
        assert!(!sample.is_empty(), "窗口应至少返回部分文本");
        // 返回的应是最近写入的内容.
        assert!(sample.ends_with("字符。"), "应包含 chunk 末尾的中文");
    }

    /// 精确回归: 还原 panic 报告里的现象 (start byte index 落在 UTF-8 多字节字符内部).
    /// 实机原报告: window 取 1, buf 长度 466 字节, start=465 落在 '泻' (bytes 463..466) 中间.
    /// 修复后应按字符边界安全截取最后 window 个字符, 不 panic.
    #[test]
    fn recent_text_panic_exact_repro_465() {
        // window=1 字符, min_repeat=6, max_buffer=4096 字节.
        let mut d = LoopDetector::new(1, 6, 4096);
        // 构造 buf 长度 466 字节: 154 个 ASCII (154 字节) + 104 个 '中' (312 字节) = 466.
        // '中' UTF-8 占 3 字节, 字符起点为 154, 157, 160, ..., 463. 最后一个 '中' 起点 463, 字节 463..466.
        let payload: String = "a".repeat(154) + &"中".repeat(104);
        assert_eq!(payload.len(), 466, "前置条件: payload 长度 466 字节");
        assert_eq!(payload.chars().count(), 258, "前置条件: 258 字符");
        d.feed(&payload);
        // 旧实现: start = 466 - 1 = 465, 落在 '中' 字符最后一字节, 切片 panic.
        // 新实现: 取最后 1 个字符, 即末尾的 '中'.
        let sample = d.recent_text();
        assert_eq!(sample, "中", "应返回最后 1 个字符 (UTF-8 字符边界)");
    }

    /// 回归: 结构化文本里的合法重复不应被误杀 (实机 go 端点反复截断长技术文档的根因)。
    /// Markdown 表格分隔行 `|---|---|`、代码缩进、列表/标题标记在 384 字符窗口内
    /// 极易凑出短单元 (l<=4) 的 6 次重复, 旧实现会误判为死循环并截断。
    /// 修复后: 纯噪声短单元需达到 `noise_repeat()`(=min_repeat+6=12) 次才判。
    #[test]
    fn noise_unit_not_false_positive_at_six() {
        let mut d = LoopDetector::new(384, 6, 4096);
        // 模拟表格分隔行重复 6 次 (纯 `|`/`-` 噪声, 无语义字符)。
        let mut hit = false;
        for _ in 0..6 {
            hit = d.feed("|---|---|");
        }
        assert!(!hit, "纯噪声短单元重复 6 次不应误判 (需达 12 次)");
    }

    /// 回归: 纯噪声短单元重复达到 `noise_repeat()`(12) 次仍应判为循环
    /// (既放宽误杀, 又不丢失对极端噪声的保护)。
    #[test]
    fn noise_unit_triggers_at_twelve() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let mut hit = false;
        for _ in 0..12 {
            hit = d.feed("| ");
        }
        assert!(hit, "纯噪声短单元重复 12 次应判为循环");
    }

    /// 回归: 含语义字符的真循环 (代码标识符/中文片段) 仍按 `min_repeat`(6) 判定, 不受放宽影响。
    #[test]
    fn semantic_loop_still_detected() {
        let mut d = LoopDetector::new(384, 6, 4096);
        let mut hit = false;
        for _ in 0..6 {
            hit = d.feed("fn main() ");
        }
        assert!(hit, "含语义字符的重复片段应仍按原阈值判为循环");
    }
}
