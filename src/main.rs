//! 入口 — 启动 axum server, 创建原生桌面窗口 + 系统托盘.

// Windows 上隐藏控制台窗口 (双击 exe 时无黑框, 从 cmd 运行时仍可见)
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::Duration;

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
mod cache;
mod i18n;
mod lang;
mod circuit_breaker;
mod config;
mod thinking;
mod keys;
mod providers;
mod proxy;
mod loop_guard;
mod store;
mod version;

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
    if !providers_path.exists() {
        // 默认配置预设常用供应商 (真实公开 endpoint, 不写任何密钥):
        // - endpoint 均为真实可达地址, 不再用 api.example.com 占位符 (避免误触熔断);
        // - api_key 仅通过 api_key_env 指向环境变量, 由用户自行填写, 绝不硬编码密钥;
        // - models 留空, 由面板「拉取模型」从上游 /v1/models 自动获取, 避免模型 ID 过时。
        let default = r#"{
  "_说明": "providers 为供应商列表, 已预设常用供应商(真实 endpoint, key 走环境变量)。字段: name 名称; endpoint 上游 /v1/chat/completions 地址(必须真实可达); api_key_env 读取 Key 的环境变量名; models 模型表(留空, 在面板点「拉取模型」自动获取)。models 的键是客户端使用的【模型中转ID】(可自取), upstream_model 才是上游真实模型 ID。",
  "providers": [
    {
      "name": "zen",
      "endpoint": "https://opencode.ai/zen/v1/chat/completions",
      "api_key_env": "OPENCODE_ZEN_KEY",
      "api_key_default": "public",
      "models": {}
    },
    {
      "name": "go",
      "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
      "api_key_env": "OPENCODE_GO_KEY",
      "models": {}
    },
    {
      "name": "deepseek",
      "endpoint": "https://api.deepseek.com/v1/chat/completions",
      "api_key_env": "DEEPSEEK_API_KEY",
      "models": {}
    }
  ]
}"#;
        let _ = std::fs::write(&providers_path, default);
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

// ─── 管理面板 API 鉴权中间件 ───

/// 管理 API 鉴权: 配置 AIGATE_ADMIN_TOKEN 后, 要求请求头携带
/// `Authorization: Bearer <token>`, 否则返回 401; 未配置则直接放行.
///
/// 本地桌面窗口 (WebView) 由 admin_page 注入同一令牌, 面板功能不受影响.
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

// ─── 托盘图标生成 ───

/// 创建 32×32 RGBA 图标字节, 复用 build.rs 的像素逻辑.
fn make_icon_rgba() -> Vec<u8> {
    let w = 32u32;
    let h = 32u32;
    let mut pixels = Vec::with_capacity((w * h * 4) as usize);

    for y in 0..h {
        for x in 0..w {
            let cx = w as f32 / 2.0;
            let cy = h as f32 / 2.0;

            // 背景紫色渐变
            let bg_r = (99.0 + (y as f32 / h as f32) * (79.0 - 99.0)) as u8;
            let bg_g = (102.0 + (y as f32 / h as f32) * (70.0 - 102.0)) as u8;
            let bg_b = (241.0 + (y as f32 / h as f32) * (229.0 - 241.0)) as u8;

            let dist_center = ((x as f32 - cx).powi(2) + (y as f32 - cy).powi(2)).sqrt();

            let (r, g, b, a) = if dist_center >= 5.5 && dist_center <= 7.5 {
                (255, 255, 255, 255) // 中心白色环
            } else if dist_center <= 4.0 {
                (bg_r, bg_g, bg_b, 255) // 中心紫色圆点
            } else {
                // 4 个连接节点
                let nodes = [(7, 8), (25, 8), (7, 24), (25, 24)];
                let mut found = false;
                for &(nx, ny) in &nodes {
                    let nd = (((x as i32 - nx).pow(2) + (y as i32 - ny).pow(2)) as f32).sqrt();
                    if nd <= 3.0 {
                        found = true;
                        break;
                    }
                }
                if found {
                    (255, 255, 255, 255)
                } else {
                    (bg_r, bg_g, bg_b, 255)
                }
            };
            pixels.push(r);
            pixels.push(g);
            pixels.push(b);
            pixels.push(a);
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
}

/// 创建系统托盘及右键菜单.
fn setup_tray() -> TrayHandles {
    let rgba = make_icon_rgba();
    let icon = tray_icon::Icon::from_rgba(rgba, 32, 32).expect("创建托盘图标失败");

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
        .with_icon(icon)
        .with_menu(Box::new(menu))
        .with_tooltip(&format!("AIGate {}", crate::version::VERSION))
        .build()
        .expect("创建系统托盘失败");

    let event_rx = tray_icon::TrayIconEvent::receiver().clone();
    let menu_rx = tray_icon::menu::MenuEvent::receiver().clone();

    TrayHandles { _tray: tray, _event_rx: event_rx, menu_rx, show_item, console_item, quit_item }
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

    let registry = match providers::ProviderRegistry::load("providers.json") {
        Ok(r) => r,
        Err(e) => {
            show_error(&format!("{}: {e}", crate::i18n::startup_error("config")));
            std::process::exit(1);
        }
    };

    let client = match reqwest::Client::builder()
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
                warn!("main: precheck FAILED for provider={name}, preset circuit OPEN");
                if let Some(cb) = breakers.lock().unwrap().get_mut(&name) {
                    cb.force_open();
                }
            }
        }
    });

    let state = AppState {
        client,
        registry: Arc::new(RwLock::new(registry)),
        admin_token: config.admin_token.clone(),
        breaker: config.breaker.clone(),
        cache: Arc::new(crate::cache::ResponseCache::new(
            config.cache_enabled,
            Duration::from_secs(config.cache_ttl_secs),
            config.cache_max_entries,
        )),
        stream_idle_timeout: Duration::from_secs(config.stream_idle_timeout_secs),
        retry_max: config.retry_max,
        retry_backoff: Duration::from_millis(config.retry_backoff_ms),
        key_store: keys::KeyStore::new("data"),
        log_buffer: LogBuffer::new().with_store(store::LogStore::new("data")),
        loop_guard: config.loop_guard.clone(),
        breakers,
    };

    // 管理面板 API 路由: 配置了 AIGATE_ADMIN_TOKEN 时整体启用 Bearer 鉴权.
    let mut admin_api = Router::new()
        .route("/admin/api/logs", get(admin::api_logs).delete(admin::api_logs_delete))
        .route("/admin/api/routes", get(admin::api_routes))
        .route("/admin/api/providers", get(admin::api_providers_get))
        .route("/admin/api/providers/save", post(admin::api_providers_save))
        .route("/admin/api/providers/reload", post(admin::api_providers_reload))
        .route("/admin/api/providers/test", post(admin::api_providers_test))
        .route("/admin/api/providers/:name/fetch-models", post(admin::api_providers_fetch_models))
        .route("/admin/api/keys", get(admin::api_keys_get).put(admin::api_keys_put))
        .route("/admin/api/health", get(admin::api_health))
        .route("/admin/api/circuit/reset", post(admin::api_circuit_reset))
        .route("/admin/api/cache", get(admin::api_cache_get).post(admin::api_cache_set))
        .route("/admin/api/cache/clear", post(admin::api_cache_clear))
        .route("/admin/api/stats", get(admin::api_stats))
        .route("/admin/api/mock", post(admin::api_mock))
        .route("/admin/api/lang", get(admin::api_lang).post(admin::api_lang_set))
        .route("/admin/api/version", get(admin::api_version));
    if config.admin_token.is_some() {
        admin_api = admin_api.layer(middleware::from_fn_with_state(state.clone(), require_admin_token));
    }

    let app = Router::new()
        .route("/v1/chat/completions", post(proxy::chat_completions))
        .route("/v1/models", get(handle_models))
        .route("/health", get(|| async { "ok" }))
        .route("/admin", get(admin::admin_page))
        .merge(admin_api)
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone());

    // 退出时用于同步刷新日志到磁盘的 state 克隆 (事件循环闭包捕获).
    let state_for_shutdown = state;

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    info!("{} — listening on http://{}", crate::version::build_info(), addr);

    // ── 绑定端口并启动 Axum ──
    let listener = rt.block_on(async {
        tokio::net::TcpListener::bind(addr).await
    }).unwrap_or_else(|e| {
        show_error(&format!("{}: {e}\n\n{}", crate::i18n::startup_error("bind"), crate::i18n::startup_bind_hint()));
        std::process::exit(1);
    });

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
            .build(&event_loop)
            .expect("创建窗口失败"),
    );

    // ── WebView (wry 0.38: new/with_url 均不返回 Result) ──
    let admin_url = format!("http://127.0.0.1:{port}/admin");
    let _webview = WebViewBuilder::new(window.as_ref())
        .with_url(&admin_url)
        .build()
        .expect("构建 WebView 失败");

    // ── 系统托盘 ──
    let handles = setup_tray();

    // 隐藏控制台
    toggle_console(false);

    // ── 事件循环 ──
    let _ = event_loop.run(move |event, elwt| {
        elwt.set_control_flow(ControlFlow::Wait);

        // 处理托盘菜单事件
        while let Ok(menu_event) = handles.menu_rx.try_recv() {
            let id = menu_event.id.0;
            if id == handles.show_item.id().0 {
                // 打开主窗口
                window.set_visible(true);
                let _ = window.focus_window();
            } else if id == handles.console_item.id().0 {
                // 切换控制台显示/隐藏
                let hidden = handles.console_item.is_checked();
                toggle_console(!hidden);
                handles.console_item.set_checked(!hidden);
            } else if id == handles.quit_item.id().0 {
                // 退出程序: 先同步刷新日志到磁盘, 避免异步尾写丢失.
                state_for_shutdown.log_buffer.flush_blocking();
                elwt.exit();
            }
        }

        // 处理托盘点击事件 — 只响应左键/双击, 忽略右键(用于弹出菜单)
        while let Ok(tray_event) = handles._event_rx.try_recv() {
            use tray_icon::ClickType;
            match tray_event.click_type {
                ClickType::Left | ClickType::Double => {
                    window.set_visible(true);
                    let _ = window.focus_window();
                }
                _ => {}
            }
        }

        match event {
            Event::WindowEvent { event, .. } => match event {
                WindowEvent::CloseRequested => {
                    // 关闭窗口 → 最小化到托盘, 不退出
                    window.set_visible(false);
                }
                WindowEvent::Resized(_size) => {
                    // webview 会自动跟随窗口大小
                }
                _ => {}
            },
            Event::LoopExiting => {
                // 兜底: 事件循环退出前再同步刷新一次日志 (覆盖非菜单退出路径).
                state_for_shutdown.log_buffer.flush_blocking();
                toggle_console(true); // 退出前显示控制台, 让用户看到最后日志
            }
            _ => {}
        }
    });
}

// ─── 模型列表 ───

async fn handle_models(
    State(state): State<AppState>,
) -> axum::Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let data: Vec<serde_json::Value> = registry
        .model_ids()
        .into_iter()
        .map(|id| {
            serde_json::json!({
                "id": id,
                "object": "model",
                "owned_by": "AIGate",
            })
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
