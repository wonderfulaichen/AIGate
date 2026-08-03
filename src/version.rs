//! 版本信息中枢 — 单一真相源为 `Cargo.toml` 的 `package.version`.
//!
//! 构建时间 / Git commit 由 `build.rs` 在编译期经 `cargo:rustc-env` 注入
//! (离线 / 无 git 时为空串, 对应片段自动省略). 调用方应走本模块,
//! 不要在别处硬编码版本号.
//!
//! 注入的 env 由 `env!` 在编译期读取:
//! - `CARGO_PKG_VERSION`  语义版本 (Cargo 默认注入)
//! - `AIGATE_BUILD_TIME`  编译时间 RFC3339 (build.rs 注入)
//! - `AIGATE_GIT_COMMIT`  短 commit hash (build.rs 注入)

/// 语义版本号 (如 `"0.2.0"`), 来自 `Cargo.toml`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// 编译时间 (RFC3339, 如 `"2026-08-04T12:00:00Z"`), 由 `build.rs` 注入.
/// 离线构建时为空串.
pub const BUILD_TIME: &str = env!("AIGATE_BUILD_TIME");

/// 短 commit hash (如 `"a1b2c3d"`), 由 `build.rs` 注入. 离线构建时为空串.
pub const GIT_COMMIT: &str = env!("AIGATE_GIT_COMMIT");

/// 完整展示串, 供日志 / 托盘 / 关于页使用.
///
/// 例: `v0.2.0 · commit a1b2c3d · built 2026-08-04T12:00:00Z`
/// 缺 commit / build_time 时自动省略对应片段.
pub fn build_info() -> String {
    let mut s = format!("v{VERSION}");
    if !GIT_COMMIT.is_empty() {
        s.push_str(&format!(" · commit {GIT_COMMIT}"));
    }
    if !BUILD_TIME.is_empty() {
        s.push_str(&format!(" · built {BUILD_TIME}"));
    }
    s
}

/// 结构化版本信息, 供 `/admin/api/version` 与前端 `window.AIGATE_VERSION` 使用.
pub fn to_json() -> serde_json::Value {
    serde_json::json!({
        "version": VERSION,
        "build_time": BUILD_TIME,
        "git_commit": GIT_COMMIT,
        "build_info": build_info(),
    })
}
