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
        let max_l = (win / self.min_repeat).max(1);
        for l in 2..=max_l {
            let need = l * self.min_repeat;
            if need > win {
                continue;
            }
            let tail_start = n - need;
            let unit = &chars[tail_start..tail_start + l];
            let mut ok = true;
            for k in 1..self.min_repeat {
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
}
