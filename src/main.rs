//! 入口 — 启动 axum server, 创建原生桌面窗口 + 系统托盘.

// Windows 上隐藏控制台窗口 (双击 exe 时无黑框, 从 cmd 运行时仍可见)
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::{Arc, Mutex, atomic::{AtomicBool, AtomicUsize}};
use std::time::{Duration, Instant};

use reqwest::dns::{Name, Resolve, Resolving};

use axum::extract::State;
use axum::http::{Request, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::body::Body;
use axum::{routing::{get, post}, Router};
use tokio::sync::RwLock;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

use winit::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoop},
    window::WindowBuilder,
};
use wry::WebViewBuilder;

use tray_icon::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};

mod admin;
mod anthropic;
mod responses;
mod cache;
mod i18n;
mod lang;
mod circuit_breaker;
mod config;
mod thinking;
mod keys;
mod accounts;
mod providers;
mod pricing;
mod peak;
mod proxy;
mod loop_guard;
mod store;
mod rollup;
mod version;
mod tooltip;
mod balance;
mod opencode_quota;
mod oc_login;
#[cfg(windows)]
mod oc_login_win;
mod currency;
mod proxy_cfg;
mod seen_version;
mod model_meta;
mod recall;
mod events;
mod notify;
mod traces;
mod client_id;
mod context_breakdown;
mod guard;
mod provider_catalog;
mod presets;

use admin::compute_realtime_stats_sync;

use config::Config;
use proxy::AppState;
use admin::LogBuffer;

/// 诊断日志双写器: 同时写 stdout (开发/命令行可见) 与 exe 同目录的 aigate.log.
/// 窗口隐藏版 (windows_subsystem="windows") 无控制台, stdout 被丢弃, 必须落盘才能取诊断日志.
struct TeeWriter {
    stdout: std::io::Stdout,
    file: Option<std::sync::Arc<std::sync::Mutex<std::fs::File>>>,
}

impl std::io::Write for TeeWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let _ = self.stdout.write_all(buf);
        if let Some(f) = &self.file {
            let mut g = f.lock().unwrap();
            g.write_all(buf)?;
        }
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        let _ = self.stdout.flush();
        if let Some(f) = &self.file {
            f.lock().unwrap().flush()?;
        }
        Ok(())
    }
}

/// tracing-subscriber 的 MakeWriter 实现: 每次写入新建一个 TeeWriter (持有 stdout + 共享文件句柄).
#[derive(Clone)]
struct TeeMakeWriter {
    file: Option<std::sync::Arc<std::sync::Mutex<std::fs::File>>>,
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for TeeMakeWriter {
    type Writer = TeeWriter;
    fn make_writer(&'a self) -> TeeWriter {
        TeeWriter {
            stdout: std::io::stdout(),
            file: self.file.clone(),
        }
    }
}

// ─── DNS ───

/// DNS 解析器 — 只解析 IPv4 地址, 避免 IPv6 导致的连接失败.
struct Ipv4Resolver;

impl Ipv4Resolver {
    fn new() -> Self {
        Self
    }
}

impl Resolve for Ipv4Resolver {
    fn resolve(&self, name: Name) -> Resolving {
        let host = name.as_str().to_owned();
        Box::pin(async move {
            let addrs = tokio::task::spawn_blocking(move || {
                format!("{host}:443")
                    .to_socket_addrs()
                    .map(|iter| iter.filter(|a| matches!(a, SocketAddr::V4(_))).collect::<Vec<_>>())
            })
            .await
            .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)?
            .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)?;
            Ok(Box::new(addrs.into_iter()) as Box<dyn Iterator<Item = SocketAddr> + Send>)
        })
    }
}

// ─── Panic Hook ───

fn set_panic_hook() {
    std::panic::set_hook(Box::new(|info| {
        let msg = match info.payload().downcast_ref::<&str>() {
            Some(s) => s.to_string(),
            None => match info.payload().downcast_ref::<String>() {
                Some(s) => s.clone(),
                None => format!("{info:?}"),
            },
        };
        let location = info
            .location()
            .map(|l| format!("\n  位置: {}:{}", l.file(), l.line()))
            .unwrap_or_default();
        eprintln!("AIGate panic: {msg}{location}");
        show_error(&format!("{}:\n\n{msg}{location}", crate::i18n::startup_error("panic")));
        std::process::exit(1);
    }));
}

// ─── 工作目录 / 配置 ───

fn exe_dir() -> std::path::PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
}

fn ensure_default_configs(base: &std::path::Path) {
    let providers_path = base.join("providers.json");

    // 只要 providers.json 不存在就重建默认配置。
    // 取消「还要求 .aigate_initialized 不存在」的旧门槛: 该门槛本意是防止在
    // 缺少真实配置的新目录里静默播种空配置遮蔽用户数据, 但其防遮蔽作用已被
    // 「已存在即不覆盖」完全覆盖; 反而制造了「用户主动删配置后无法自动恢复、
    // 程序直接因读不到配置而崩溃」的坑。现改为: 删 providers.json 直接重建默认,
    // 真有备份的用户把原文件放回即可, 不会被覆盖。
    let needs_seed = !providers_path.exists();
    if needs_seed {
        // 默认配置预设常用供应商 (真实公开 endpoint, 不写任何密钥):
        // - endpoint 均为真实可达地址, 不再用 api.example.com 占位符 (避免误触熔断);
        // - api_key 仅通过 api_key_env 指向环境变量, 由用户自行填写, 绝不硬编码密钥;
        // - 协议: 缺省 OpenAI chat/completions; 同一供应商下个别模型若官方规定走
        //   /messages (Anthropic 格式, 如 opencode go 的 MiniMax/Qwen3-*-plus·max、
        //   zen 的 Claude 系列), 直接在模型条目加 "api_format":"anthropic" 标注,
        //   代理会自动将该模型请求的端点由 /chat/completions 改写为 /messages,
        //   客户端侧始终是 OpenAI 兼容. 不再需要为同供应商拆分多个配置块.
        // - 每个供应商预填各自端点的常用模型 (upstream_model 为上游真实 ID),
        //   其余模型可在面板「拉取模型」从上游 /v1/models 自动获取后补充.
        let default = r#"{
  "_说明": "providers 为供应商列表, 已预设常用供应商(真实 endpoint, key 走环境变量)。字段: name 名称; endpoint 上游地址(必须真实可达, 缺省 /chat/completions); api_format 协议格式(缺省 openai, 可逐模型覆盖为 anthropic / /messages); api_key_env 读取 Key 的环境变量名; balance_endpoint 余额查询地址(可选); models 模型表。models 的键是客户端使用的【模型中转ID】(可自取), upstream_model 才是上游真实模型 ID。同一供应商下不同模型可能走不同协议(如 opencode go 网关 glm/kimi→/chat/completions, minimax/qwen3-*-plus·max→/messages), 无需拆分供应商, 直接在模型条目加 \"api_format\":\"anthropic\" 即可, 代理会自动改写端点路径。",
  "providers": [
    {
      "name": "zen",
      "endpoint": "https://opencode.ai/zen/v1/chat/completions",
      "api_key_env": "OPENCODE_ZEN_KEY",
      "api_key_default": "public",
      "models": {
        "deepseek-v4-flash": { "upstream_model": "deepseek-v4-flash" },
        "glm-5.2": { "upstream_model": "glm-5.2" },
        "kimi-k3": { "upstream_model": "kimi-k3" },
        "claude-sonnet-4-6": { "upstream_model": "claude-sonnet-4-6", "api_format": "anthropic" }
      }
    },
    {
      "name": "go",
      "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
      "api_key_env": "OPENCODE_GO_KEY",
      "models": {
        "deepseek-v4-flash-GO": { "upstream_model": "deepseek-v4-flash" },
        "glm-5.2-GO": { "upstream_model": "glm-5.2" },
        "kimi-k3-GO": { "upstream_model": "kimi-k3" },
        "mimo-v2.5": { "upstream_model": "mimo-v2.5" },
        "minimax-m2.5": { "upstream_model": "minimax-m2.5", "api_format": "anthropic" },
        "minimax-m2.7": { "upstream_model": "minimax-m2.7", "api_format": "anthropic" },
        "qwen3.8-max": { "upstream_model": "qwen3.8-max", "api_format": "anthropic" },
        "qwen3.7-max": { "upstream_model": "qwen3.7-max", "api_format": "anthropic" }
      }
    },
    {
      "name": "deepseek",
      "endpoint": "https://api.deepseek.com/v1/chat/completions",
      "api_key_env": "DEEPSEEK_API_KEY",
      "balance_endpoint": "https://api.deepseek.com/user/balance",
      "models": {
        "deepseek-v4-flash-DS": { "upstream_model": "deepseek-v4-flash" },
        "deepseek-v4-pro-DS": { "upstream_model": "deepseek-v4-pro" }
      }
    }
  ]
}"#;
        let _ = std::fs::write(&providers_path, default);

        // 醒目警告: tracing 此时尚未初始化 (在 main 中更晚建立), 故直接 eprintln
        // 并追加到 exe 同目录 aigate.log, 确保即便窗口隐藏版也能在诊断日志中看到。
        let warn = format!(
            "[AIGate] 警告: 在 {p} 未找到 providers.json, 已自动创建【默认配置】(常用供应商+预填模型, key 需自行填入环境变量)。\n\
若你已有真实配置, 请将原 providers.json 复制到该目录并重启 AIGate, 否则面板看到的是默认配置。",
            p = providers_path.display()
        );
        eprintln!("{warn}");
        append_diag_log(base, &warn);
    }

    let env_path = base.join(".env");
    if !env_path.exists() {
        let default = "# AIGate 环境变量\n\
# 为 providers.json 中各供应商的 api_key_env 填入对应 Key (也可通过系统环境变量设置)\n\n\
# 示例: 为名为 deepseek 的供应商配置 Key\n\
# DEEPSEEK_API_KEY=sk-your-key-here\n";
        let _ = std::fs::write(&env_path, default);
    }

    let data_dir = base.join("data");
    let _ = std::fs::create_dir_all(&data_dir);
}

/// 在 tracing 尚未初始化时, 把诊断警告直接追加到 exe 同目录的 aigate.log,
/// 保证窗口隐藏版 (无控制台) 也能在诊断日志中看到关键提示.
fn append_diag_log(base: &std::path::Path, msg: &str) {
    use std::io::Write;
    let log_path = base.join("aigate.log");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
        let _ = writeln!(f, "{msg}");
    }
}

// ─── 管理面板 API 鉴权中间件 ───

/// 管理 API 鉴权。
///
/// **2026-10-12 改默认值（对标 dsh-our-free-model 的安全口径）**：原先"配置
/// `AIGATE_ADMIN_TOKEN` 才校验，未配置则**完全放行**。实测那样意味着：
/// 任何能访问 `127.0.0.1:8787` 的进程/页面，都不带凭据就能读到账号昵称、余额、
/// 以及 `/admin/api/providers` 里**原样回吐的真实 JWT**（该接口本身另有脱敏缺口
/// 见 HANDOFF §7）。而本机浏览器里任意一个页面发个 fetch 就能拿到。
///
/// dsh 的做法（README「安全与隐私」）：本机 Key **由 crypto 运行时生成**、`timingSafeEqual`
/// 比对、存 0600 文件；/"与 "/health 为存活探针，先于 Key 检查应答，**但只回答"是否在线"**,
/// 拿模型清单仍需 Key；把监听改到可路由接口被直接拒绝。
/// 本项目的取舍（**用户 2026-10-12 选定「默认要 token」**）：
///   ① `AIGATE_ADMIN_TOKEN` 未设置时**自动生成一个随机 token**，写进
///      `data/.admin_token`（0600），面板由 `admin_page` 注入同一个值 ——
///      原窗口（WebView）无感，外部无凭据访问一律 401。要显式关掉可设
///      `AIGATE_OPEN_ADMIN=1`（此时回到旧行为，风险自负）。
///   ② `/health` **不鉴权**，但只回答固定 `ok` 两个字符（存活探针用途），
///      不泄露版本/配置（与 dsh 同口径：先应答"是否在线"，其余全要 Key）。
///   ③ 显式配了 `AIGATE_ADMIN_TOKEN` 时沿用配置值。
async fn require_admin_token(
    State(state): State<AppState>,
    req: Request<Body>,
    next: Next,
) -> Response {
    if let Some(token) = &state.admin_token {
        let authorized = req
            .headers()
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .map(|v| v == format!("Bearer {token}"))
            .unwrap_or(false);
        if !authorized {
            return (
                StatusCode::UNAUTHORIZED,
                "admin api requires a valid bearer token",
            )
                .into_response();
        }
    }
    next.run(req).await
}

// ─── 品牌标几何 (与 icon.svg / build.rs **同一套 256 空间数字**) ───
//
// ⚠️ 2026-10-10 修的漏改: 09-30 换标时改了母版 + 5 处矢量落点 + `build.rs` 的 exe 光栅,
//    **唯独漏了这里** —— 于是托盘与窗口图标一直在画**旧标**(紫色渐变 + 中心白环 + 四节点),
//    而所有门禁都报绿 (verify_logo.py 的 PART C 只扫 build.rs, 不覆盖 main.rs).
//    症状: 窗口标题栏/任务栏显示旧图标, 面板与 exe 资源却是新标, 用户报"图标怎么不更新".
//
//    这里是与 build.rs `logo_sample` 逐行对应的**同源还原**(不是又一份拷贝的画法):
//    同样的常量、同样的 SDF、同样的 3×3 超采样, 只是输入尺寸由 ICO 的 6 档变成固定 32。
//    改品牌标必须**同时**改三处: icon.svg / build.rs / 这里 (verify_logo.py PART E 会扫).
/// 端口被占用时最多顺延尝试的次数（含原端口本身）。
///
/// 20 是"够用且不掩盖问题"的折中：本地同时开几个实例最多也就占几个端口；
/// 而无限顺延会让"PORT 配错了"变成"怎么每次端口都不一样"。
const PORT_FALLBACK_TRIES: u16 = 20;

const LOGO_BLUE: (u8, u8, u8) = (10, 132, 255); // #0a84ff = Apple systemBlue
const LOGO_FG: (u8, u8, u8) = (255, 255, 255);
const LOGO_BOX: (f32, f32, f32, f32) = (8.0, 8.0, 240.0, 240.0); // x y w h
const LOGO_RX: f32 = 56.0; // 圆角半径 (占 23%)
const GATE_STROKE: f32 = 20.0; // 门框线宽 (箭头 32 更粗, 主次靠线宽拉开)
/// 门框折线: 上臂 → 左柱 → 下臂, 右缘敞开
const GATE_PTS: [(f32, f32); 4] = [(148.0, 56.0), (52.0, 56.0), (52.0, 200.0), (148.0, 200.0)];
/// 箭头填充多边形: 杆 y112–144 由 x104 贯到 176, 头 apex (220,128) 两翼落 y104/152
const ARROW_PTS: [(f32, f32); 7] = [
    (104.0, 112.0),
    (176.0, 112.0),
    (176.0, 104.0),
    (220.0, 128.0),
    (176.0, 152.0),
    (176.0, 144.0),
    (104.0, 144.0),
];

/// 采样一个**子像素**的颜色 (256 空间), 返回 (r, g, b, a) —— a 取 0.0/1.0.
/// 与 build.rs `logo_sample` 逐行对应.
fn logo_sample(sx: f32, sy: f32) -> (f32, f32, f32, f32) {
    // 圆角矩形 SDF: >0 在圆角外 → 透明 (旧版整块不透明, 圆角等于没画)
    let half_w = LOGO_BOX.2 * 0.5;
    let half_h = LOGO_BOX.3 * 0.5;
    let dx = (sx - (LOGO_BOX.0 + half_w)).abs() - (half_w - LOGO_RX);
    let dy = (sy - (LOGO_BOX.1 + half_h)).abs() - (half_h - LOGO_RX);
    let outside = (dx.max(0.0).powi(2) + dy.max(0.0).powi(2)).sqrt();
    if outside + dx.max(dy).min(0.0) - LOGO_RX > 0.0 {
        return (0.0, 0.0, 0.0, 0.0);
    }
    // 门框: 到折线的距离 <= 半线宽即落在笔画上 (线段距离天然就是 SVG 的 round 线帽)
    if dist_to_polyline(sx, sy, &GATE_PTS) <= GATE_STROKE * 0.5 || point_in_poly(sx, sy, &ARROW_PTS) {
        return (LOGO_FG.0 as f32, LOGO_FG.1 as f32, LOGO_FG.2 as f32, 1.0);
    }
    (LOGO_BLUE.0 as f32, LOGO_BLUE.1 as f32, LOGO_BLUE.2 as f32, 1.0)
}

/// 点到折线的最短距离 (与 build.rs 对应).
fn dist_to_polyline(px: f32, py: f32, pts: &[(f32, f32)]) -> f32 {
    let mut best = f32::MAX;
    for w in pts.windows(2) {
        let (x1, y1) = w[0];
        let (vx, vy) = (w[1].0 - x1, w[1].1 - y1);
        let len2 = vx * vx + vy * vy;
        let t = if len2 > 0.0 {
            (((px - x1) * vx + (py - y1) * vy) / len2).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let d = ((px - (x1 + t * vx)).powi(2) + (py - (y1 + t * vy)).powi(2)).sqrt();
        if d < best {
            best = d;
        }
    }
    best
}

/// 射线法判点是否落在填充多边形内 (与 build.rs 对应).
fn point_in_poly(px: f32, py: f32, pts: &[(f32, f32)]) -> bool {
    let mut inside = false;
    let mut j = pts.len() - 1;
    for i in 0..pts.len() {
        let (xi, yi) = pts[i];
        let (xj, yj) = pts[j];
        if (yi > py) != (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// 托盘/窗口图标: 32×32 RGBA, 图案 = 品牌标「闸门通路」.
///
/// 与 build.rs 的 `bgra_bitmap` 同源 (3×3 超采样 + 直通 alpha), 差别只在:
/// build.rs 输出 BGRA 行序自下而上 (BMP 系), 这里输出 RGBA 自上而下 (winit/tray_icon).
///
/// ⚠️ 必须超采样: 圆角与箭头斜边不抗锯齿会明显拉毛 (build.rs 里有同样的注释).
fn make_icon_rgba() -> Vec<u8> {
    let w = 32u32;
    let h = 32u32;
    let mut pixels = Vec::with_capacity((w * h * 4) as usize);

    for y in 0..h {
        for x in 0..w {
            const SS: u32 = 3;
            let kx = 256.0 / w as f32;
            let ky = 256.0 / h as f32;
            let mut acc = [0.0f32; 3];
            let mut cov = 0.0f32;
            for sy in 0..SS {
                for sx in 0..SS {
                    let px = (x as f32 + (sx as f32 + 0.5) / SS as f32) * kx;
                    let py = (y as f32 + (sy as f32 + 0.5) / SS as f32) * ky;
                    let s = logo_sample(px, py);
                    if s.3 > 0.0 {
                        acc[0] += s.0;
                        acc[1] += s.1;
                        acc[2] += s.2;
                        cov += 1.0;
                    }
                }
            }
            let (r, g, b, a) = if cov == 0.0 {
                (0, 0, 0, 0)
            } else {
                let n = (SS * SS) as f32;
                (
                    (acc[0] / cov).round() as u8,
                    (acc[1] / cov).round() as u8,
                    (acc[2] / cov).round() as u8,
                    (cov / n * 255.0).round() as u8,
                )
            };
            pixels.push(r);
            pixels.push(g);
            pixels.push(b);
            pixels.push(a);
        }
    }
    pixels
}

/// 带**红点**的托盘图标（有未读告警时用）：在品牌标右上角叠一个红点 + 白描边。
///
/// 白描边是刻意的：托盘背景在深色/浅色任务栏下都可能出现，纯红点在深色下会糊掉，
/// 白描边保证两种主题下都醒目。位置取右上角 (25.5, 6.5) —— 品牌标的门框在中偏左，
/// 右上角是空的，不会盖住图案本身（那会让"有告警"和"图标换了"混淆）。
fn make_icon_rgba_alert() -> Vec<u8> {
    let mut pixels = make_icon_rgba();
    let (cx, cy) = (25.5f32, 6.5f32);
    let (r_red, r_white) = (5.0f32, 7.0f32);
    for y in 0..32u32 {
        for x in 0..32u32 {
            let dx = x as f32 + 0.5 - cx;
            let dy = y as f32 + 0.5 - cy;
            let d = (dx * dx + dy * dy).sqrt();
            let idx = ((y * 32 + x) * 4) as usize;
            if d <= r_red {
                // 红点本体 (不透明)
                pixels[idx] = 230;
                pixels[idx + 1] = 57;
                pixels[idx + 2] = 70;
                pixels[idx + 3] = 255;
            } else if d <= r_white {
                // 白描边 (不透明)
                pixels[idx] = 255;
                pixels[idx + 1] = 255;
                pixels[idx + 2] = 255;
                pixels[idx + 3] = 255;
            }
        }
    }
    pixels
}

// ─── 系统托盘 ───

/// 托盘相关句柄.
struct TrayHandles {
    #[allow(dead_code)]
    _tray: tray_icon::TrayIcon,
    _event_rx: tray_icon::TrayIconEventReceiver,
    menu_rx: tray_icon::menu::MenuEventReceiver,
    show_item: MenuItem,
    console_item: CheckMenuItem,
    quit_item: MenuItem,
    /// 常态图标 (无未读告警).
    icon_normal: tray_icon::Icon,
    /// 带红点的图标 (有未读告警).
    icon_alert: tray_icon::Icon,
}

/// 创建系统托盘及右键菜单.
/// 从 WebView2 读 cookie（**能读 HttpOnly**，这是 JS `document.cookie` 做不到的）。
///
/// 为什么必须走原生 API：opencode 的 `__Host-console_session` 实测 `httpOnly: true`
/// （也符合 `__Host-` 前缀规范），`document.cookie` 读不到它。
/// wry 0.38 的 `WebView` 没有 cookie API（`cookies_for_url` / `cookies` 都不存在），
/// 但它暴露了 `WebViewExtWindows::controller()`，由此 QI 到 `ICoreWebView2_2`
/// 即可拿 `CookieManager`。
///
/// 返回 `(name, value)` 列表；读不到时返回空列表（调用方按"还没登录完"处理，
/// 不当成错误 —— 轮询期间未登录是正常状态）。
///
/// 失败全部静默降级为空列表：这是登录流的辅助路径，不该因为读 cookie 失败
/// 就把用户的登录尝试判死；上层会继续轮询。
fn setup_tray() -> TrayHandles {    let icon_normal =
        tray_icon::Icon::from_rgba(make_icon_rgba(), 32, 32).expect("创建托盘图标失败");
    let icon_alert =
        tray_icon::Icon::from_rgba(make_icon_rgba_alert(), 32, 32).expect("创建托盘告警图标失败");

    let menu = Menu::new();

    let show_item = MenuItem::new(crate::i18n::tray_menu("show"), true, None);
    let console_item = CheckMenuItem::new(crate::i18n::tray_menu("console"), true, true, None);
    let quit_item = MenuItem::new(crate::i18n::tray_menu("quit"), true, None);

    menu.append(&show_item).ok();
    menu.append(&PredefinedMenuItem::separator()).ok();
    menu.append(&console_item).ok();
    menu.append(&PredefinedMenuItem::separator()).ok();
    menu.append(&quit_item).ok();

    let tray = tray_icon::TrayIconBuilder::new()
        .with_icon(icon_normal.clone())
        .with_menu(Box::new(menu))
        .with_tooltip(&format!("AIGate {}", crate::version::VERSION))
        .build()
        .expect("创建系统托盘失败");

    let event_rx = tray_icon::TrayIconEvent::receiver().clone();
    let menu_rx = tray_icon::menu::MenuEvent::receiver().clone();

    TrayHandles {
        _tray: tray,
        _event_rx: event_rx,
        menu_rx,
        show_item,
        console_item,
        quit_item,
        icon_normal,
        icon_alert,
    }
}

// ─── 控制台窗口 (Windows) ───

/// 显示或隐藏控制台窗口.
#[cfg(windows)]
fn toggle_console(show: bool) {
    type HWND = *mut std::ffi::c_void;

    #[link(name = "kernel32")]
    extern "system" {
        fn GetConsoleWindow() -> HWND;
    }

    #[link(name = "user32")]
    extern "system" {
        fn ShowWindow(hWnd: HWND, nCmdShow: i32) -> bool;
    }

    const SW_HIDE: i32 = 0;
    const SW_SHOW: i32 = 5;

    unsafe {
        let hwnd = GetConsoleWindow();
        if !hwnd.is_null() {
            ShowWindow(hwnd, if show { SW_SHOW } else { SW_HIDE });
        }
    }
}

#[cfg(not(windows))]
fn toggle_console(_show: bool) {}

/// 加载隐私护栏配置 (`config/guard.json`, 方案 B)。
///
/// 与 `tooltip::load_config` 同一模式: 文件不存在/损坏 → 默认**关**。
/// 默认关的理由 (用户 2026-10-13 拍板 + `guard::GuardConfig::default_off` 文档):
/// 扫描有 CPU 成本且多数用户不需要; 默认开但只警告会让用户误以为"已在保护中"。
fn load_guard_config() -> Option<crate::guard::GuardConfig> {
    let path = std::path::Path::new("config").join("guard.json");
    match std::fs::read_to_string(&path) {
        Ok(text) => match serde_json::from_str::<crate::guard::GuardConfig>(&text) {
            Ok(cfg) => {
                info!(
                    "guard: config loaded (enabled={}, dry_run={})",
                    cfg.enabled, cfg.dry_run
                );
                Some(cfg)
            }
            Err(e) => {
                warn!("guard: {} 解析失败 ({e}), 使用默认(关闭)", path.display());
                None
            }
        },
        Err(_) => Some(crate::guard::GuardConfig::default_off()),
    }
}

/// 根据环境变量配置上游请求的代理策略 (诊断 opencode.ai 等 TLS 握手 EOF 用).
fn apply_proxy_env(builder: reqwest::ClientBuilder) -> reqwest::ClientBuilder {
    let status = crate::proxy_cfg::proxy_status();
    match status.mode {
        "no_proxy" => {
            info!("proxy: AIGATE_NO_PROXY set -> system proxy disabled");
            builder.no_proxy()
        }
        "custom" => {
            let url = status.url.unwrap_or_default();
            match reqwest::Proxy::all(&url) {
                Ok(p) => {
                    info!("proxy: AIGATE_PROXY set -> using explicit proxy {url}");
                    builder.proxy(p)
                }
                Err(e) => {
                    warn!("proxy: invalid AIGATE_PROXY '{url}': {e}, falling back to system proxy");
                    builder
                }
            }
        }
        _ => {
            info!("proxy: using system proxy (HTTPS_PROXY/https_proxy) by default");
            builder
        }
    }
}

// ─── 主入口 ───

fn main() {
    set_panic_hook();

    let base = exe_dir();
    ensure_default_configs(&base);
    std::env::set_current_dir(&base).unwrap_or(());
    crate::i18n::init_lang();

    // ── 后台 tokio runtime ──
    let rt = tokio::runtime::Runtime::new().expect("创建运行时失败");

    // 在 runtime 上下文中初始化 tracing
    let _guard = rt.enter();

    // 日志同时写 stdout 与 exe 同目录的 aigate.log.
    // 窗口隐藏版 (windows_subsystem="windows") 无控制台, stdout 被丢弃, 必须落盘才能取诊断日志.
    let log_path = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("aigate.log")))
        .unwrap_or_else(|| std::path::PathBuf::from("aigate.log"));
    // 简单滚动: 超过 5MB 移为 aigate.log.old, 避免无限增大.
    if let Ok(meta) = std::fs::metadata(&log_path) {
        if meta.len() > 5 * 1024 * 1024 {
            let _ = std::fs::rename(&log_path, log_path.with_extension("old"));
        }
    }
    let file_writer: Option<std::sync::Arc<std::sync::Mutex<std::fs::File>>> =
        match std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
            Ok(f) => Some(std::sync::Arc::new(std::sync::Mutex::new(f))),
            Err(e) => {
                eprintln!("aigate: 无法打开日志文件 {:?}: {e}, 仅输出到 stdout", log_path);
                None
            }
        };
    let tee = TeeMakeWriter { file: file_writer };
    tracing_subscriber::fmt()
        .with_writer(tee)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let config = Config::from_env();
    let port = config.port;

    let registry = match providers::ProviderRegistry::load("providers.json")
        .or_else(|_| providers::ProviderRegistry::load("D:\\Office software\\Development Project\\AI中转\\providers.json"))
    {
        Ok(r) => r,
        Err(e) => {
            show_error(&format!("{}: {e}", crate::i18n::startup_error("config")));
            std::process::exit(1);
        }
    };

    // 上游 HTTP 客户端: AIGATE_FORCE_HTTP1=1 时强制 HTTP/1.1 (禁用 h2 多路复用).
    // 用途: 经系统代理的长流式连接在 h2 下偶发被中间层掐断
    // ("unexpected EOF during chunk size line"), 强制 h1 可作为诊断与缓解开关.
    let force_http1 = std::env::var("AIGATE_FORCE_HTTP1")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    let mut client_builder = apply_proxy_env(reqwest::Client::builder());
    if force_http1 {
        client_builder = client_builder.http1_only();
        info!("proxy: AIGATE_FORCE_HTTP1 set -> upstream forced to HTTP/1.1 (h2 disabled)");
    }
    let client = match client_builder
        .dns_resolver(Arc::new(Ipv4Resolver::new()))
        .connect_timeout(std::time::Duration::from_secs(config.connect_timeout_secs))
        .timeout(std::time::Duration::from_secs(config.request_timeout_secs))
        // HTTP/2 多路复用: 由 TLS ALPN 自动协商, 上游不支持时回退 HTTP/1.1, 零风险.
        // 配合默认开启的 keep-alive + 连接池, 减少连发请求时的建连/TLS 握手开销.
        .http2_adaptive_window(true)
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            show_error(&format!("{}: {e}", crate::i18n::startup_error("client")));
            std::process::exit(1);
        }
    };

    // 熔断表预填充: 每个供应商一个熔断实例, 使用可配置阈值.
    let breaker_cfg = config.breaker.clone();
    let mut breaker_map: HashMap<String, circuit_breaker::CircuitBreaker> = HashMap::new();
    for p in registry.providers() {
        breaker_map.insert(p.name.clone(), circuit_breaker::CircuitBreaker::with_config(breaker_cfg.clone()));
    }
    let breakers = Arc::new(Mutex::new(breaker_map));

    // 启动预检: 对网络不可达的供应商预置熔断 (Open), 使其快速失败而非干等超时.
    let precheck_timeout = Duration::from_secs(config.connect_timeout_secs.min(5));
    let precheck_snapshot = registry.providers();
    // 克隆一份 client 供预检使用 (reqwest::Client 内部是 Arc, 克隆开销极低),
    // 避免预检 future 借用 client 与其后 move 进 AppState 冲突.
    let precheck_client = client.clone();
    // 捕获引用 (Copy) 而非移动 client 本身, 避免每个 future 都试图 move precheck_client.
    let precheck_client_ref = &precheck_client;
    let mut prechecks = Vec::new();
    for p in &precheck_snapshot {
        let ep = p.endpoint.clone();
        prechecks.push(async move {
            (p.name.clone(), proxy::precheck_provider(precheck_client_ref, &ep, precheck_timeout).await)
        });
    }
    rt.block_on(async {
        for (name, reachable) in futures::future::join_all(prechecks).await {
            if reachable {
                info!("main: precheck ok for provider={name}");
            } else {
                // 启动瞬时探测失败仅作告警, 不再 force_open 永久钉死熔断.
                //
                // 根因: 预检只在启动那一刹那探测一次, 误判率高
                //   (服务先于网络/代理启动、5s 超时过短、代理未就绪等).
                // 一旦 force_open, 熔断进入 Open, 即便上游随后恢复,
                // 也只能靠运行期自愈的 60s 节奏缓慢探测; 若某次 HalfOpen
                // 探测请求因 handler 中途退出而漏调 report_breaker,
                // probe_in_flight 会永久为 true → 该供应商死锁在 503, 无法自愈.
                //
                // 改为: 启动探测失败只告警, 运行期熔断自愈机制全权负责.
                // 第一次真实请求仍以 Closed 状态正常放行,
                // 若上游真不可用, 连续失败会由运行期熔断接管 (Open→HalfOpen→恢复).
                warn!("main: precheck FAILED for provider={name} (startup snapshot only; runtime breaker handles recovery)");
            }
        }
    });

    // KeyStore 初始化需要 providers 列表做旧版 (按 env_var 索引) → 按 provider 索引的无损迁移.
    let providers_snapshot = registry.providers();
    // 数据目录 (日志 / 日级 rollup / keys.json): 默认 exe 同级的 data/ (启动时 cwd 已切到 exe 目录).
    //
    // 需要从别处复用历史统计时, 用 AIGATE_DATA_DIR 显式指定, 例如构建目录跑二进制:
    //   AIGATE_DATA_DIR="D:\...\AI中转\data" ./AIGate.exe
    //
    // 这里曾有一段"自动回落到硬编码部署目录"的逻辑 (本地 logs.jsonl 比部署目录小 1KB 就改用它).
    // 已移除, 原因: 路径写死不可移植; 触发条件过宽 (本地 data 为空即命中);
    // 且会静默改写部署实例的数据 —— 本地跑一次测试就可能污染线上统计.
    let resolved_data_dir: String = std::env::var("AIGATE_DATA_DIR")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "data".to_string());
    info!("main: data dir = {resolved_data_dir}");
    // 本地无历史时给出可发现的提示 (而不是自动改目录): 从构建目录跑二进制想看历史统计,
    // 用 AIGATE_DATA_DIR 指向部署目录即可.
    let local_log_len = std::fs::metadata(std::path::Path::new(&resolved_data_dir).join("logs.jsonl"))
        .map(|m| m.len())
        .unwrap_or(0);
    if local_log_len == 0 {
        info!("main: no log history in '{resolved_data_dir}'; set AIGATE_DATA_DIR to reuse another instance's data dir");
    }
    // 事件台账 (2026-10-11): 必须在任何转发路径之前安装 —— 否则启动早期的事件
    // (如首次健康探测发现的异常) 会静默丢弃。安装成功与否不影响启动。
    let _ = events::install(Arc::new(events::EventLog::new(&resolved_data_dir)));
    // 路由表句柄提前取出: AppState 与日志缓冲区 (价格快照) 都要引用它.
    let registry = Arc::new(RwLock::new(registry));
    // ── opencode 一键登录（2026-10-24）──
    //
    // ── opencode Console 一键登录（2026-10-24）──
    //
    // 为什么走独立线程的 WebView：opencode 是 Google/GitHub OAuth，回调落在
    // opencode 域名，本地服务端收不到。所以让浏览器自己走完整条 OAuth，
    // 再从 WebView2 的 CookieManager 读 `__Host-console_session`
    // （HttpOnly，JS 的 document.cookie 读不到，但原生 API 能读）。
    //
    // ⚠️ 为什么不在主事件循环回调里建窗：实测那样 WebView2 不会被真正初始化
    // （`build` 返回 Ok、窗口在列表里、尺寸正确，但 URL 停在 about:blank、
    //   客户区位图纯白）。
    //   而"独立线程 + wry"也不行：winit 每次进程**只允许一个 EventLoop**，
    //   主窗口已占用 → 必报 `EventLoop can't be recreated`。
    //   最终走 Win32 + WebView2 原生（不依赖 winit），见 `src/oc_login_win.rs`。
    //
    // HTTP handler 直接调 `oc_login::spawn_login` 起线程（不经过主循环），
    // 线程轮询 cookie 后写回共享的 LoginState。
    let oc_login_state: Arc<crate::oc_login::LoginState> =
        Arc::new(crate::oc_login::LoginState::new());
    let state = AppState {
        client,
        registry: Arc::clone(&registry),
        oc_login: Arc::clone(&oc_login_state),
        admin_token: config.admin_token.clone(),
        breaker: config.breaker.clone(),
        cache: Arc::new(crate::cache::ResponseCache::new(
            config.cache_enabled,
            Duration::from_secs(config.cache_ttl_secs),
            config.cache_max_entries,
            config.cache_persist_path.clone(),
        )),
        inflight: Arc::new(crate::cache::InflightCache::new()),
        stream_idle_timeout_secs: Arc::new(std::sync::atomic::AtomicU64::new(config.stream_idle_timeout_secs)),
        retry_max: Arc::new(std::sync::atomic::AtomicU32::new(config.retry_max)),
        retry_backoff_ms: Arc::new(std::sync::atomic::AtomicU64::new(config.retry_backoff_ms)),
        key_store: keys::KeyStore::new(&resolved_data_dir, &providers_snapshot),
        accounts: crate::accounts::AccountStore::new(&resolved_data_dir),
        login_sessions: Default::default(),
        sessions: crate::traces::SessionTracker::new(),
        // 隐私护栏 (方案 B): 默认关 + dry_run; 配置文件 `config/guard.json`.
        guard_config: std::sync::Arc::new(tokio::sync::RwLock::new(load_guard_config())),
        guard_rules: std::sync::Arc::new(crate::guard::builtin_rules()),
        balance_manager: crate::balance::BalanceManager::new(
            std::path::PathBuf::from("config").join("balance.json"),
        ),
        log_buffer: LogBuffer::new()
            .with_store(store::LogStore::new(&resolved_data_dir))
            .with_rollup(rollup::RollupBook::new(&resolved_data_dir))
            .with_registry(&registry),
        stats_cache: Arc::new(tokio::sync::Mutex::new(None)),
        loop_guard: config.loop_guard.clone(),
        strip_history_reasoning: Arc::new(AtomicBool::new(config.strip_history_reasoning)),
        max_history_turns: Arc::new(AtomicUsize::new(config.max_history_turns)),
        auto_continue: Arc::new(AtomicUsize::new(config.auto_continue)),
        strip_toolcall_on_chat: Arc::new(AtomicBool::new(config.strip_toolcall_on_chat)),
        strip_toolcall_on_responses: Arc::new(AtomicBool::new(config.strip_toolcall_on_responses)),
        recall: Arc::new(recall::RecallStore::new(
            &resolved_data_dir,
            recall::RecallConfig {
                enabled: config.recall_enabled,
                max_entry_bytes: config.recall_max_entry_bytes,
                max_entries: config.recall_max_entries,
                ..Default::default()
            },
        )),
        tool_output_max_bytes: Arc::new(AtomicUsize::new(config.tool_output_max_bytes)),
        thinking_purify: Arc::new(tokio::sync::RwLock::new(config.thinking_purify.clone())),
        purify_min_chars: Arc::new(AtomicUsize::new(config.thinking_purify_min_chars)),
        purify_max_nodes: Arc::new(AtomicUsize::new(config.thinking_purify_max_nodes)),
        model_meta: Arc::new(model_meta::MetaCache::new()),
        // 进程级稳定 session id: 启动时生成一次, 用于 OpenCode Go 等
        // 要求 x-opencode-session 头的上游做兜底注入. 不覆盖客户端自传.
        // 用 PID + 启动时间戳 + 进程内单调计数器即可 — 这是路由标识不是
        // 安全令牌, 上游只用来「同一来源走同一后端」, 碰撞概率可忽略.
        instance_session: Arc::new(format!(
            "aigate-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        )),
        breakers,
    };

    // 管理面板 API 路由: 配置了 AIGATE_ADMIN_TOKEN 时整体启用 Bearer 鉴权.
    let mut admin_api = Router::new()
        .route("/admin/api/logs", get(admin::api_logs).delete(admin::api_logs_delete))
        .route("/admin/api/errors", get(admin::api_errors))
        .route("/admin/api/routes", get(admin::api_routes))
        .route("/admin/api/providers", get(admin::api_providers_get))
        .route("/admin/api/providers/save", post(admin::api_providers_save))
        .route("/admin/api/providers/reload", post(admin::api_providers_reload))
        .route("/admin/api/providers/test", post(admin::api_providers_test))
        .route("/admin/api/providers/:name/fetch-models", post(admin::api_providers_fetch_models))
        .route("/admin/api/keys", get(admin::api_keys_get).put(admin::api_keys_put))
        .route("/admin/api/accounts", get(admin::api_accounts_get))
        .route("/admin/api/accounts/refresh", post(admin::api_accounts_refresh))
        .route("/admin/api/accounts/login", post(admin::api_accounts_login))
        .route("/admin/api/accounts/login/start", post(admin::api_accounts_login_start))
        .route("/admin/api/accounts/login/status", post(admin::api_accounts_login_status))
        .route("/admin/api/accounts/logout", post(admin::api_accounts_logout))
        .route("/admin/api/accounts/reorder", post(admin::api_accounts_reorder))
        .route("/admin/api/accounts/picking", get(admin::api_accounts_picking_get).post(admin::api_accounts_picking_set))
        .route("/admin/api/presets", get(admin::api_presets_get))
        .route("/admin/api/health", get(admin::api_health))
        .route("/admin/api/circuit/reset", post(admin::api_circuit_reset))
        .route("/admin/api/cache", get(admin::api_cache_get).post(admin::api_cache_set))
        .route("/admin/api/cache/clear", post(admin::api_cache_clear))
        .route("/admin/api/strip-reasoning", get(admin::api_strip_reasoning_get).post(admin::api_strip_reasoning_set))
        .route("/admin/api/thinking-purify", get(admin::api_thinking_purify_get).post(admin::api_thinking_purify_set))
        .route("/admin/api/strip-toolcall-protocols", get(admin::api_strip_toolcall_protocols_get).post(admin::api_strip_toolcall_protocols_set))
        .route("/admin/api/max-history-turns", get(admin::api_max_history_turns_get).post(admin::api_max_history_turns_set))
        // 降级可回取: 配置开关 / 留存列表 / 取回原文 / 统计与清空.
        .route("/admin/api/recall", get(admin::api_recall_get).post(admin::api_recall_set))
        .route("/admin/api/recall/list", get(admin::api_recall_list))
        .route("/admin/api/recall/entry/:id", get(admin::api_recall_entry))
        .route("/admin/api/recall/clear", post(admin::api_recall_clear))
        // 降级埋点: 静默降级计数 (解析失败导致优化/翻译未生效).
        .route("/admin/api/degradations", get(admin::api_degradations))
        // 事件台账 (2026-10-11): 关键事件的时间线 + 清空.
        .route("/admin/api/events", get(admin::api_events))
        .route("/admin/api/events/ack", post(admin::api_events_ack))
        .route("/admin/api/events/clear", post(admin::api_events_clear))
        // 用量热力图 (2026-10-11): 逐日 token 总量, 数据源是日级 rollup (跨日志滚动窗口).
        .route("/admin/api/heatmap", get(admin::api_heatmap))
        .route("/admin/api/tool-output-limit", get(admin::api_tool_output_limit_get).post(admin::api_tool_output_limit_set))
        .route("/admin/api/auto-continue", get(admin::api_auto_continue_get).post(admin::api_auto_continue_set))
        .route("/admin/api/stream-timeout", get(admin::api_stream_timeout_get).post(admin::api_stream_timeout_set))
        .route("/admin/api/retry", get(admin::api_retry_get).post(admin::api_retry_set))
        .route("/admin/api/model-meta", post(admin::api_model_meta))
        .route("/admin/api/stats", get(admin::api_stats))
        .route("/admin/api/mock", post(admin::api_mock))
        .route("/admin/api/lang", get(admin::api_lang).post(admin::api_lang_set))
        .route("/admin/api/version", get(admin::api_version))
        .route("/admin/api/proxy-config", get(admin::api_proxy_config))
        .route("/admin/api/changelog", get(admin::api_changelog))
    .route("/admin/api/seen-version", get(admin::api_seen_version_get).post(admin::api_seen_version_post))
        .route("/admin/api/realtime", get(admin::api_realtime))
        .route("/admin/api/tooltip-config", get(admin::api_tooltip_config_get).post(admin::api_tooltip_config_set))
        .route("/admin/api/currency", get(admin::api_currency_get).post(admin::api_currency_set))
        .route("/admin/api/peak-schedule", get(admin::api_peak_schedule_get).post(admin::api_peak_schedule_set))
        .route("/admin/api/balance", get(admin::api_balance))
        .route("/admin/api/opencode/quota", get(admin::api_opencode_quota))
        .route("/admin/api/opencode/quota-all", get(admin::api_opencode_quota_all))
        .route("/admin/api/opencode/login/start", post(admin::api_oc_login_start))
        .route("/admin/api/opencode/login/status", post(admin::api_oc_login_status))
        .route("/admin/api/opencode/login/cancel", post(admin::api_oc_login_cancel))
        .route("/admin/api/opencode/orgs", post(admin::api_oc_orgs))
        .route("/admin/api/opencode/verify", post(admin::api_oc_verify))
        .route("/admin/api/balance/manual", post(admin::api_balance_manual_set));
    // `admin_token` 现在默认一定有值（未配置时自动生成并落盘 data/.admin_token），
    // 所以这个分支几乎总是进；保留条件判断是为了 `AIGATE_OPEN_ADMIN=1`（显式不鉴权）能生效。
    if config.admin_token.is_some() {
        admin_api = admin_api.layer(middleware::from_fn_with_state(state.clone(), require_admin_token));
    }

    let app = Router::new()
        .route("/v1/chat/completions", post(proxy::chat_completions))
        .route("/v1/messages", post(proxy::messages_completions))
        // Anthropic 的输入估算端点：Claude Code 等客户端发送前会调它估算上下文占用。
        // 纯本地估算、不打上游、不消耗额度（见 proxy::count_tokens 文档）。
        .route("/v1/messages/count_tokens", post(proxy::count_tokens))
        .route("/v1/responses", post(proxy::responses_completions))
        .route("/v1/models", get(handle_models))
        // 存活探针：**故意不鉴权**（探活需要），但只回答固定 `ok`
        // —— 不泄露版本/配置（dsh 同口径：先应答"是否在线"，其余全要 Key）。
        .route("/health", get(|| async { "ok" }))
        .route("/admin", get(admin::admin_page))
        .route("/admin/static/:file", get(admin::admin_static))
        .merge(admin_api)
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone());

    // ── 账号静默续期调度 (2026-10-01 账号化): 启动 30s 后首查, 之后每 10 分钟 ──
    // 只处理 auth.mode=account 且配置了 refresh 的供应商; 临期 (margin 内) 才真打续期.
    // 请求路径 (usable_token) 也会按需续期, 两者经 AccountStore.refresh_gate 串行防重复 POST.
    {
        let sched = state.clone();
        rt.spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
            loop {
                let providers = sched.registry.read().await.providers();
                for p in providers {
                    let Some(auth) = p.auth.as_ref() else { continue };
                    if auth.mode != crate::providers::AuthMode::Account { continue }
                    let Some(rc) = auth.refresh.as_ref() else { continue };
                    // 多账号: 逐个账号续期 (任一失效不影响其他号; 临期才真打续期).
                    for acct in sched.accounts.entries(&p.name).await {
                        match crate::accounts::refresh_account(
                            &sched.client, rc, &p.name, &acct.id, &sched.accounts, auth.expiry_margin_secs,
                        ).await {
                            Ok(_) => { /* 未到临期时为无操作; 续成则已写盘 */ }
                            Err(e) => tracing::warn!("accounts: 后台续期 '{}' 账号 {} 失败: {e}", p.name, acct.id),
                        }
                    }
                }
                tokio::time::sleep(std::time::Duration::from_secs(600)).await;
            }
        });
    }

    // 退出时用于同步刷新日志到磁盘的 state 克隆 (事件循环闭包捕获).
    let state_for_shutdown = state;

    // ── 绑定端口并启动 Axum ──
    //
    // 端口被占时**从 PORT 起顺延试几个**, 而不是直接退出（2026-10-11 对标 dsh-our-free-model）：
    // 本地网关最常见的"打不开"就是上次的进程还没退干净 / 另一个工具占了 8787，
    // 此时"启动失败并退出"会让用户以为软件坏了；顺延到可用端口并在日志与面板里
    // **如实标注实际端口**，则一切照常可用。
    //
    // ⚠️ 三条纪律：
    // ① **只在"端口被占"时顺延**（`AddrInUse`）—— 地址非法、权限不足等错误直接报错退出，
    //    顺延只会把真正的问题掩盖成"端口变了"。
    // ② **顺延范围有限**（最多 20 个）——无限顺延会掩盖"端口配置错了"。
    // ③ **必须如实告知**：日志、启动事件、`/admin/api/version` 都要带实际端口，
    //    否则用户按 8787 连会连不上而不知道为什么（这正是要避免的"静默改行为"）。
    let (listener, actual_port) = {
        let mut chosen = None;
        let mut last_err = None;
        for offset in 0..=PORT_FALLBACK_TRIES {
            let p = port.saturating_add(offset);
            let a = SocketAddr::from(([127, 0, 0, 1], p));
            match rt.block_on(async { tokio::net::TcpListener::bind(a).await }) {
                Ok(l) => {
                    chosen = Some(l);
                    if offset > 0 {
                        warn!(
                            "main: port {port} unavailable (in use), falling back to {p} \
                             (requested PORT={port}, actually listening on {p})"
                        );
                    }
                    break;
                }
                Err(e) => {
                    let in_use = e.kind() == std::io::ErrorKind::AddrInUse;
                    if offset == 0 && !in_use {
                        // 非"被占用"的错误（地址非法/权限不足）→ 立即报错，不顺延。
                        show_error(&format!(
                            "{}: {e}\n\n{}",
                            crate::i18n::startup_error("bind"),
                            crate::i18n::startup_bind_hint()
                        ));
                        std::process::exit(1);
                    }
                    last_err = Some(e);
                }
            }
        }
        match chosen {
            // 先取实际端口再交出 listener（`l` 一旦 move 就走不了 local_addr）。
            Some(l) => {
                let p = l.local_addr().map(|a| a.port()).unwrap_or(port);
                (l, p)
            }
            None => {
                show_error(&format!(
                    "{}: {}\n\n{}",
                    crate::i18n::startup_error("bind"),
                    last_err.map(|e| e.to_string()).unwrap_or_default(),
                    crate::i18n::startup_bind_hint()
                ));
                std::process::exit(1);
            }
        }
    };
    if actual_port != port {
        // 落一条**事件台账**：用户排查"为什么面板地址变了"时，这里就是答案
        // （与"出问题得自己找"相对的：软件主动说清楚它做了什么）。
        events::note(
            events::EventKind::PortFallback,
            "",
            &format!(
                "请求的端口 {port} 已被占用，已改用 {actual_port}（面板地址 http://127.0.0.1:{actual_port}/admin）"
            ),
        );
    }
    // 台账里留一条"进程启动": 它既是分界线 (哪些事件属于哪一次运行), 也让用户
    // 打开面板时台账必定**不是空的** —— 空的台账会让"这个功能是不是没工作"没法判断。
    // ⚠️ 报告**实际**监听端口（可能与 PORT 不同, 见上方顺延逻辑）。
    events::note(
        events::EventKind::Started,
        "",
        &format!(
            "{} 已启动，监听 127.0.0.1:{}",
            crate::version::build_info(),
            actual_port
        ),
    );
    if actual_port != port {
        info!(
            "{} — listening on http://127.0.0.1:{} (PORT={port} was in use; \
             set PORT to pin a specific port)",
            crate::version::build_info(),
            actual_port
        );
    } else {
        info!(
            "{} — listening on http://127.0.0.1:{}",
            crate::version::build_info(),
            actual_port
        );
    }

    rt.spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            show_error(&format!("{}: {e}", crate::i18n::startup_error("serve")));
            std::process::exit(1);
        }
    });

    // 等待服务器就绪
    std::thread::sleep(std::time::Duration::from_millis(200));

    // ── 创建原生窗口 ──
    let event_loop = EventLoop::new().expect("创建事件循环失败");

    // 窗口图标 (使用 RGBA 字节创建 winit Icon)
    let window_icon = {
        let rgba = make_icon_rgba();
        winit::window::Icon::from_rgba(rgba, 32, 32).ok()
    };

    let window = Arc::new(
        WindowBuilder::new()
            .with_title("AIGate")
            .with_window_icon(window_icon)
            .with_inner_size(LogicalSize::new(1200.0, 800.0))
            .with_min_inner_size(LogicalSize::new(800.0, 600.0))
            .build(&event_loop)
            .expect("创建窗口失败"),
    );

    // ── WebView (wry 0.38: new/with_url 均不返回 Result) ──
    // URL 携带版本号查询串作为缓存破除: WebView2 默认持久 HTTP 缓存会命中
    // 旧版 /admin 页面, 导致新构建仍显示旧版本号; 版本变化后查询串随之变化,
    // 缓存键不同, 强制拉取新页面.
    let admin_url = format!(
        "http://127.0.0.1:{port}/admin?v={}",
        crate::version::VERSION
    );
    let webview = WebViewBuilder::new(window.as_ref())
        .with_url(&admin_url)
        .build()
        .expect("构建 WebView 失败");

    // ── 系统托盘 ──
    let handles = setup_tray();

    // 隐藏控制台
    toggle_console(false);

    // ── 事件循环 ──
    let mut last_tooltip_update = Instant::now();
    let mut tooltip_version = String::new();
    let mut tray_alert_active = false;
    let _ = event_loop.run(move |event, elwt| {
        // 用 WaitUntil 替代 Wait: Wait 在没有窗口/托盘事件时永久阻塞, 事件循环体不执行,
        // 导致下方 tooltip 定时更新代码永不运行 (用户最小化到托盘后悬停看到的永远是初始文本).
        // WaitUntil 保证即使无事件, 也会按 tooltip 更新周期按时唤醒执行.
        let tooltip_cfg = tooltip::load_config();
        let wake_interval = Duration::from_secs(tooltip_cfg.update_interval_secs.clamp(1, 10) as u64);
        elwt.set_control_flow(ControlFlow::WaitUntil(Instant::now() + wake_interval));

        // 处理托盘菜单事件
        while let Ok(menu_event) = handles.menu_rx.try_recv() {
            let id = menu_event.id.0;
            if id == handles.show_item.id().0 {
                // 打开主窗口
                window.set_visible(true);
                let _ = window.focus_window();
                // 用户来面板了 → 红点确认已读 (未读语义 = "没在面板窗口里看过")
                crate::notify::ack();
                let _ = handles._tray.set_icon(Some(handles.icon_normal.clone()));
            } else if id == handles.console_item.id().0 {
                // 切换控制台显示/隐藏
                let hidden = handles.console_item.is_checked();
                toggle_console(!hidden);
                handles.console_item.set_checked(!hidden);
            } else if id == handles.quit_item.id().0 {
                // 退出程序: 先同步刷新日志/缓存/日级 rollup 到磁盘, 避免异步尾写 / 缓存丢失.
                state_for_shutdown.log_buffer.flush_blocking();
                state_for_shutdown.log_buffer.rollup_flush_blocking();
                state_for_shutdown.cache.flush_blocking();
                elwt.exit();
            }
        }

        while let Ok(tray_event) = handles._event_rx.try_recv() {
            use tray_icon::ClickType;
            match tray_event.click_type {
                ClickType::Left | ClickType::Double => {
                    window.set_visible(true);
                    let _ = window.focus_window();
                    // 用户来面板了 → 红点确认已读
                    crate::notify::ack();
                    let _ = handles._tray.set_icon(Some(handles.icon_normal.clone()));
                }
                _ => {}
            }
        }

        // ── 托盘红点轮询 (2026-10-12: 报错通知) ──
        // 与 tooltip 同一个唤醒周期 (1-10s): 有未读告警 → 带红点图标, 否则常态图标。
        // 只在状态**变化**时 set_icon, 不每轮重设 (无谓的 IPC)。
        // 气球弹窗由 events::note → notify::on_event 即时触发, 不走这条轮询路径 ——
        // 红点最多晚 10s 出现, 气球是即时的, 两者冗余设计 (杀软拦了气球还有红点)。
        //
        // ⚠️ **不在这里按"窗口可见"自动确认** —— 那样面板开着时红点永不亮 (用户把面板
        // 留在另一个显示器、事件就被静默确认了)。确认只发生在**用户显式动作**:
        // 托盘点击 / 菜单「显示面板」/ 窗口重获焦点 (见下方 match 分支)。
        {
            let want_alert = crate::notify::unread() > 0;
            if want_alert != tray_alert_active {
                tray_alert_active = want_alert;
                let icon = if want_alert { &handles.icon_alert } else { &handles.icon_normal };
                if let Err(e) = handles._tray.set_icon(Some(icon.clone())) {
                    // 红点画不上去不能影响主流程: 气球已弹过, 记一笔即可。
                    tracing::warn!("notify: failed to set tray alert icon: {e}");
                }
            }
        }

        // 更新任务栏 tooltip (根据配置)
        let config = tooltip_cfg; // 复用循环开头读取的配置 (与 WaitUntil 唤醒周期一致)
        let interval = config.update_interval_secs.clamp(1, 10) as u64;
        if last_tooltip_update.elapsed() >= Duration::from_secs(interval) {
            last_tooltip_update = Instant::now();
            if config.enabled {
                let stats = compute_realtime_stats_sync(&state_for_shutdown.log_buffer);
                let mut lines = vec![format!("AIGate v{}", crate::version::VERSION), "━━━━━━━━━━━━━".to_string()];
                // 未读告警数置顶显示 —— 悬停托盘即可看到"有几个问题", 不必点开面板。
                let unread = crate::notify::unread();
                if unread > 0 {
                    lines.push(format!("⚠ 未读告警: {unread} 个 (打开面板查看)"));
                }
                if config.metrics.requests_per_second {
                    lines.push(format!("请求速度: {:.1} req/s", stats.requests_per_second));
                }
                if config.metrics.avg_latency_ms {
                    lines.push(format!("平均延迟: {:.0} ms", stats.avg_latency_ms));
                }
                if config.metrics.cache_hit_rate {
                    lines.push(format!("缓存命中: {:.1}%", stats.cache_hit_rate * 100.0));
                }
                if config.metrics.gen_speed {
                    lines.push(format!("生成速度: {:.1} tok/s", stats.gen_speed));
                }
                if config.metrics.today_requests {
                    lines.push(format!("今日请求: {}", stats.today_requests));
                }
                let new_tooltip = lines.join("\n");
                if new_tooltip != tooltip_version {
                    tooltip_version = new_tooltip;
                    let _ = handles._tray.set_tooltip(Some(&tooltip_version));
                }
            } else {
                // tooltip 禁用，显示基础信息
                let basic = format!("AIGate v{} (已禁用)", crate::version::VERSION);
                if basic != tooltip_version {
                    tooltip_version = basic;
                    let _ = handles._tray.set_tooltip(Some(&tooltip_version));
                }
            }
        }

        match event {
            Event::WindowEvent { event, .. } => match event {
                WindowEvent::CloseRequested => {
                    // 关闭窗口 → 最小化到托盘, 不退出
                    window.set_visible(false);
                }
                WindowEvent::Focused(true) => {
                    // 窗口重获焦点 = 用户回来看面板 → 红点确认已读。
                    // (打开路径在托盘点击/菜单处已 ack; 这里覆盖"窗口一直开着, 用户切回来"的情况)
                    if crate::notify::unread() > 0 {
                        crate::notify::ack();
                        let _ = handles._tray.set_icon(Some(handles.icon_normal.clone()));
                        tray_alert_active = false;
                    }
                }
                WindowEvent::Resized(size) => {
                    // 修复: winit 0.29 + wry 0.38 在 Windows 上, 双击标题栏从最大化恢复时
                    // WebView 不会自动跟随窗口大小重绘. 显式调用 set_bounds 强制 WebView 重新适配.
                    let _ = webview.set_bounds(wry::Rect { x: 0, y: 0, width: size.width, height: size.height });
                }
                _ => {}
            },
            Event::LoopExiting => {
                // 兜底: 事件循环退出前再同步刷新一次日志/缓存/日级 rollup (覆盖非菜单退出路径).
                state_for_shutdown.log_buffer.flush_blocking();
                state_for_shutdown.log_buffer.rollup_flush_blocking();
                state_for_shutdown.cache.flush_blocking();
                toggle_console(true); // 退出前显示控制台, 让用户看到最后日志
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 取 RGBA 缓冲中某个像素 (r,g,b,a).
    fn px(buf: &[u8], x: u32, y: u32) -> (u8, u8, u8, u8) {
        let i = ((y * 32 + x) * 4) as usize;
        (buf[i], buf[i + 1], buf[i + 2], buf[i + 3])
    }

    /// **回归 (2026-10-10, 用户报"图标怎么不更新")**: 窗口/托盘图标必须画**新标**,
    /// 不得回退成 09-30 换标前那版 (紫色渐变底 + 中心白环 + 四节点).
    ///
    /// 这个 bug 的真实教训: 换标改了母版 + 5 处矢量落点 + build.rs 光栅, 唯独漏了
    /// main.rs —— 而所有门禁都报绿 (verify_logo.py PART C 只扫 build.rs).
    /// 故这里用**三种旧标特征**同时盯: 旧靛蓝渐变、全不透明四角、中心白环.
    #[test]
    fn tray_and_window_icon_is_the_new_brand_mark() {
        let buf = make_icon_rgba();
        assert_eq!(buf.len(), 32 * 32 * 4, "必须是 32×32 RGBA");

        // ① 四个角落必须**透明** (新标是圆角方块, 圆角外透明).
        //    旧标整块填紫色渐变, 四角 alpha=255 —— 这是最直接的判据.
        for &(x, y) in &[(0, 0), (31, 0), (0, 31), (31, 31)] {
            let (_, _, _, a) = px(&buf, x, y);
            assert_eq!(a, 0, "({x},{y}) 圆角外必须透明, 旧标整块不透明会被这里抓住");
        }

        // ② 中心附近必须是**新标蓝** #0a84ff (10,132,255), 不是旧靛蓝 #6366f1.
        //    取左上区域偏蓝底的像素 (避开门框白笔画): x=12,y=30 落在方块内、笔画外.
        let (r, g, b, a) = px(&buf, 12, 30);
        assert!(a > 0, "方块内不应透明");
        assert!(
            (r as i32 - 10).abs() <= 6 && (g as i32 - 132).abs() <= 6 && (b as i32 - 255).abs() <= 6,
            "底色必须是 #0a84ff, 实得 rgb({r},{g},{b}) —— 旧靛蓝 #6366f1 会被这里抓住"
        );

        // ③ 必须有**白色**像素 (门框/箭头的负形), 且数量在合理区间.
        //    旧标也有白 (中心环+节点), 所以这条单独不够, 但与 ①② 组合足以定性.
        let white = buf.chunks(4).filter(|p| p[0] > 240 && p[1] > 240 && p[2] > 240 && p[3] > 200).count();
        assert!(white > 40, "新标的白笔画应占一定比例, 实得 {white} px");

        // ④ 右侧必须**敞开** (右缘无门框): x=29 一列不应出现成片的白.
        //    旧标右侧有两个白节点 (25,8)/(25,24) —— 若回退旧画法这里会命中.
        let right_white = (0..32).filter(|&y| {
            let (r, g, b, a) = px(&buf, 29, y);
            r > 240 && g > 240 && b > 240 && a > 200
        }).count();
        assert!(
            right_white <= 12,
            "右缘应基本敞开 (旧标右侧有两个白节点), 实得 {right_white} px 白色"
        );
    }

    /// 图标几何必须与母版/ build.rs **同源**: 采样点落在门框折线上时必须出白色.
    /// 用 256 空间的已知坐标反推 (LOGO_BOX 左上 ~(8,8), 左柱 x=52 且 y=128 处是笔画).
    #[test]
    fn icon_geometry_matches_svg_brand_mark() {
        // (52,128) 在左柱中点上 → 整块应是白 (笔画内)
        let s = logo_sample(52.0, 128.0);
        assert_eq!((s.0, s.1, s.2), (255.0, 255.0, 255.0), "左柱中点应为白色");
        // (128,30) 在方块内、笔画外 → 蓝
        let s2 = logo_sample(128.0, 30.0);
        assert_eq!((s2.0, s2.1, s2.2), (10.0, 132.0, 255.0), "方块内应为品牌蓝");
        // (128,128) 在箭头杆上 → 白
        let s3 = logo_sample(128.0, 128.0);
        assert_eq!((s3.0, s3.1, s3.2), (255.0, 255.0, 255.0), "箭头杆应为白色");
        // 圆角外 (1,1) → 透明
        let s4 = logo_sample(1.0, 1.0);
        assert_eq!(s4.3, 0.0, "圆角外必须透明");
    }

    /// dump 32px 图标成二进制 PPM (P6), 供人工核对 (`AIGATE_DUMP_ICON=<目录>`).
    ///
    /// 为什么需要: 上面的断言只抽查了几个点, 而"图标到底长什么样"是**整体**判断.
    /// 默认关闭 (单测不该写文件), 显式开:
    ///     AIGATE_DUMP_ICON=<目录> cargo test --release dump_runtime_icon -- --nocapture
    ///
    /// 输出 PPM 而非 PNG: P6 格式就一个头 + 原始 RGB, 十几行搞定; 手写 PNG 编码器
    /// (zlib + CRC32) 要上百行 —— 为一次性调试在正式代码里塞那么多不值得,
    /// 而任何看图工具都开得了 PPM.
    #[test]
    fn dump_runtime_icon_on_demand() {
        let Ok(dir) = std::env::var("AIGATE_DUMP_ICON") else { return };
        let buf = make_icon_rgba();

        // 放大 4 倍: 32px 直接看是糊的, 4 倍才好辨认形状与边缘
        const SCALE: u32 = 4;
        let (w, h) = (32 * SCALE, 32 * SCALE);
        let mut raw = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                let i = (((y / SCALE) * 32 + (x / SCALE)) * 4) as usize;
                // alpha 合成到白底: winit/tray_icon 在显示时就是这么做的,
                // 直接写原值会让半透明边缘在图片里看着像脏点.
                let (r, g, b, a) = (buf[i] as u16, buf[i + 1] as u16, buf[i + 2] as u16, buf[i + 3] as u16);
                let na = 255 - a;
                raw.push((r + na * 255 / 255) as u8);
                raw.push((g + na * 255 / 255) as u8);
                raw.push((b + na * 255 / 255) as u8);
            }
        }
        let path = std::path::Path::new(&dir).join("runtime_icon_32.ppm");
        std::fs::write(&path, format!("P6\n{w} {h}\n255\n").into_bytes()).unwrap();
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
            f.write_all(&raw).unwrap();
        }
        println!("dump 到 {} ({}×{}, {}× 放大)", path.display(), w, h, SCALE);
    }
}

async fn handle_models(
    State(state): State<AppState>,
) -> axum::Json<serde_json::Value> {
    // 能力表有两个来源，**按优先级合并**（2026-10-11 接上第二个来源）：
    //  ① `FETCHED_META`（拉取模型时从**上游自己的目录**采集，如 buddy 的 `/v3/config`）——
    //     最权威（上游自己声明的），但**只在点过「拉取模型」后才有**；
    //  ② **models.dev**（公开库，按模型名归一化匹配）——覆盖面广（实测 4/4 命中），
    //     随启动预热，不需要用户做任何事。
    //
    // ⚠️ 为什么必须补 ②：原先只查 ①，而 ① 在真实配置下**恒为空**（680 个模型里
    // 带 `context_window` 的是 **0 个**）—— 也就是这段下发代码**从来没生效过**。
    // 客户端（若它读这些字段）看到的是"所有模型都没有上下文限制"，比不给更糟。
    //
    // 缺失一律**不下发该字段**，不做猜测 —— 客户端据此渲染档位选择器时，
    // 缺失会退化成"自行输入"，而错报会让客户端渲染出上游会拒绝的值。
    let fetched = crate::providers::fetched_meta_snapshot().await;
    // 一次读锁内把两样东西都取成 owned 值，随后即可放锁（避免持锁跨 await 去查 models.dev）。
    let (ids, upstream_names) = {
        let registry = state.registry.read().await;
        let ids: Vec<String> = registry.model_ids().into_iter().map(String::from).collect();
        let names: Vec<String> = ids
            .iter()
            .map(|id| {
                registry
                    .lookup(id)
                    .and_then(|e| e.model.upstream_model.clone())
                    .unwrap_or_else(|| id.to_string())
            })
            .collect();
        (ids, names)
    };
    let dev = state
        .model_meta
        .resolve_many(&state.client, &upstream_names)
        .await;

    let registry = state.registry.read().await;
    let data: Vec<serde_json::Value> = ids
        .iter()
        .map(|id| {
            let entry = registry.lookup(id);
            let upstream = entry
                .as_ref()
                .and_then(|e| e.model.upstream_model.clone())
                .unwrap_or_else(|| id.to_string());
            let m = fetched.get(&upstream);
            let dev_m = dev.get(&upstream).and_then(|x| x.as_ref());
            let explicit = entry.as_ref().and_then(|e| e.model.supported_efforts.clone());
            let mut obj = serde_json::json!({
                "id": id,
                "object": "model",
                "owned_by": "AIGate",
            });
            if let Some(o) = obj.as_object_mut() {
                // 档位: 模型级显式声明 > 上游目录采集值。都没有则不下发。
                // ⚠️ models.dev **不提供**档位信息（它不是上游能力声明），故不参与此项。
                if let Some(efforts) = explicit.or_else(|| m.and_then(|x| x.supported_efforts.clone()))
                {
                    o.insert("supported_efforts".into(), serde_json::json!(efforts));
                }
                // 上下文/输出: 上游目录优先（上游自己说的），缺失回落 models.dev。
                if let Some(v) = m
                    .and_then(|x| x.context_window)
                    .or_else(|| dev_m.and_then(|x| x.context))
                {
                    o.insert("context_window".into(), serde_json::json!(v));
                }
                if let Some(v) = m
                    .and_then(|x| x.max_output_tokens)
                    .or_else(|| dev_m.and_then(|x| x.output))
                {
                    o.insert("max_output_tokens".into(), serde_json::json!(v));
                }
                if let Some(v) = m.and_then(|x| x.can_disable_thinking) {
                    o.insert("can_disable_thinking".into(), serde_json::json!(v));
                }
                // 下面两项**只有 models.dev 有**（上游目录不声明模态能力）。
                // 用途: 客户端据此判断能不能发图 / 该不该开推理。
                if let Some(d) = dev_m {
                    if d.vision {
                        o.insert("supports_vision".into(), serde_json::json!(true));
                    }
                    if d.reasoning {
                        o.insert("supports_reasoning".into(), serde_json::json!(true));
                    }
                    if d.tool_call {
                        o.insert("supports_tool_call".into(), serde_json::json!(true));
                    }
                }
            }
            obj
        })
        .collect();
    drop(registry);
    axum::Json(serde_json::json!({ "object": "list", "data": data }))
}

// ─── 错误弹窗 ───

#[cfg(windows)]
fn show_error(msg: &str) {
    use std::ffi::OsStr;
    use std::iter::once;
    use std::os::windows::ffi::OsStrExt;
    let wide: Vec<u16> = OsStr::new(msg)
        .encode_wide()
        .chain(once(0))
        .collect();
    let title: Vec<u16> = OsStr::new("AIGate")
        .encode_wide()
        .chain(once(0))
        .collect();
    unsafe {
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(
                hWnd: *mut std::ffi::c_void,
                lpText: *const u16,
                lpCaption: *const u16,
                uType: u32,
            ) -> i32;
        }
        MessageBoxW(std::ptr::null_mut(), wide.as_ptr(), title.as_ptr(), 0x10);
    }
}

#[cfg(not(windows))]
fn show_error(msg: &str) {
    eprintln!("{msg}");
}
