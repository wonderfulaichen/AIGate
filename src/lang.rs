//! 界面语言持久化: 读写工作目录下的 `lang.json` ({"lang":"zh-CN"}).
//!
//! 语言运行态由 [`crate::i18n`] 的原子全局持有; 本模块只负责磁盘持久化,
//! 与运行时状态解耦, 职责单一. 解析逻辑统一复用 [`crate::i18n::parse_lang`].

use crate::i18n::{self, Lang};

const LANG_FILE: &str = "lang.json";

/// 从 `lang.json` 读取语言. 文件不存在 / 损坏 / 字段缺失时返回 `None`,
/// 交由 [`crate::i18n::init_lang`] 回退到环境变量或默认中文.
pub fn load_lang() -> Option<Lang> {
    let text = std::fs::read_to_string(LANG_FILE).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    let code = v.get("lang")?.as_str()?;
    i18n::parse_lang(code)
}

/// 持久化语言选择: 写入 `lang.json` 并同步更新运行态. 失败返回 IO/序列化错误.
pub fn save_lang(lang: Lang) -> std::io::Result<()> {
    let code = match lang {
        Lang::Zh => "zh-CN",
        Lang::En => "en",
    };
    let json = serde_json::json!({ "lang": code });
    let text = serde_json::to_string_pretty(&json)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(LANG_FILE, text)?;
    i18n::set_current_lang(lang);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_roundtrip_via_i18n() {
        // 验证 parse_lang 对持久化文件可能写入的两种 code 都能识别
        assert_eq!(i18n::parse_lang("zh-CN"), Some(Lang::Zh));
        assert_eq!(i18n::parse_lang("en"), Some(Lang::En));
        assert_eq!(i18n::parse_lang("garbage"), None);
    }
}
