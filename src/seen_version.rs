//! 已见版本持久化：记录用户已看过「更新亮点」的版本号，驱动首次更新弹窗。
//! 配置文件位于 exe 同目录 `config/seen_version.json`，遵循 lang/tooltip 的持久化约定。

use std::fs;
use std::path::PathBuf;

const FILE: &str = "seen_version.json";

/// 返回 exe 同目录下的 config 目录（与 lang.rs/tooltip.rs 保持一致）。
fn config_dir() -> PathBuf {
    if let Ok(p) = std::env::current_exe() {
        if let Some(dir) = p.parent() {
            return dir.join("config");
        }
    }
    PathBuf::from("config")
}

/// 读取已见版本号，未记录则返回空字符串。
pub fn load_seen() -> String {
    let path = config_dir().join(FILE);
    match fs::read_to_string(&path) {
        Ok(s) => match serde_json::from_str::<serde_json::Value>(&s) {
            Ok(v) => v
                .get("version")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            Err(_) => String::new(),
        },
        Err(_) => String::new(),
    }
}

/// 保存已见版本号（如为空则不写入，视为未读）。
pub fn save_seen(version: &str) {
    if version.is_empty() {
        return;
    }
    let dir = config_dir();
    let _ = fs::create_dir_all(&dir);
    let path = dir.join(FILE);
    let body = serde_json::json!({ "version": version }).to_string();
    let _ = fs::write(&path, body);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // std::env::current_exe 进程级，串行避免并发写同一配置文件互相干扰。
    static LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn seen_roundtrip() {
        let _g = LOCK.lock().unwrap();
        let v = "9.9.9";
        save_seen(v);
        assert_eq!(load_seen(), v);
        // 空版本不写入，保持上次记录。
        save_seen("");
        assert_eq!(load_seen(), v);
    }

    #[test]
    fn seen_default_empty() {
        let _g = LOCK.lock().unwrap();
        let dir = config_dir();
        let _ = fs::remove_file(dir.join(FILE));
        assert_eq!(load_seen(), "");
    }
}
