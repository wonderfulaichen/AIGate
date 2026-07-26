//! 入口 — 启动 axum server, 创建原生桌面窗口 + 系统托盘.

// Windows 上隐藏控制台窗口 (双击 exe 时无黑框, 从 cmd 运行时仍可见)
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::Arc;

use reqwest::dns::{Name, Resolve, Resolving};

use axum::extract::State;
use axum::{routing::post, Router};
use tokio::sync::RwLock;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::info;

use winit::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoop},
    window::WindowBuilder,
};
use wry::WebViewBuilder;

use tray_icon::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};

mod admin;
mod config;
mod keys;
mod providers;
mod proxy;
mod store;

use config::Config;
use proxy::AppState;
use admin::LogBuffer;

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
        show_error(&format!("意外错误，程序即将退出:\n\n{msg}{location}"));
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
        let default = r#"{
  "providers": [
    {
      "name": "opencode-zen",
      "endpoint": "https://opencode.ai/zen/v1/chat/completions",
      "api_key_env": "OPENCODE_ZEN_KEY",
      "api_key_default": "public",
      "models": {
        "big-pickle-ZEN": {"upstream_model": "big-pickle"},
        "deepseek-v4-flash-free-ZEN": {"upstream_model": "deepseek-v4-flash-free", "reasoning_effort": "max"},
        "mimo-v2.5-free-ZEN": {"upstream_model": "mimo-v2.5-free", "reasoning_effort": "high"},
        "north-mini-code-free-ZEN": {"upstream_model": "north-mini-code-free"}
      }
    },
    {
      "name": "opencode-go",
      "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
      "api_key_env": "OPENCODE_GO_KEY",
      "models": {
        "kimi-k2.7-code-GO": {"upstream_model": "kimi-k2.7-code", "reasoning_effort": "high"},
        "deepseek-v4-pro-GO": {"upstream_model": "deepseek-v4-pro", "reasoning_effort": "max"},
        "deepseek-v4-flash-GO": {"upstream_model": "deepseek-v4-flash", "reasoning_effort": "max"},
        "glm-5.2-GO": {"upstream_model": "glm-5.2", "reasoning_effort": "high"}
      }
    },
    {
      "name": "deepseek",
      "endpoint": "https://api.deepseek.com/v1/chat/completions",
      "api_key_env": "DEEPSEEK_API_KEY",
      "models": {
        "deepseek-v4-flash-DS": {"upstream_model": "deepseek-v4-flash", "reasoning_effort": "max"},
        "deepseek-v4-pro-DS": {"upstream_model": "deepseek-v4-pro", "reasoning_effort": "max"}
      }
    }
  ]
}
"#;
        let _ = std::fs::write(&providers_path, default);
    }

    let env_path = base.join(".env");
    if !env_path.exists() {
        let default = "# AIGate 环境变量\n\
                        # 在下方填入你的 API Key, 或通过系统环境变量设置\n\n\
                        # OpenCode Zen (免费模型, 无需修改)\n\
                        OPENCODE_ZEN_KEY=public\n\n\
                        # OpenCode Go 套餐 (订阅后填入)\n\
                        # OPENCODE_GO_KEY=sk-your-key-here\n\n\
                        # DeepSeek 官方 (填入你的 Key)\n\
                        # DEEPSEEK_API_KEY=sk-your-key-here\n";
        let _ = std::fs::write(&env_path, default);
    }

    let data_dir = base.join("data");
    let _ = std::fs::create_dir_all(&data_dir);
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

    let show_item = MenuItem::new("打开主窗口", true, None);
    let console_item = CheckMenuItem::new("隐藏控制台", true, true, None);
    let quit_item = MenuItem::new("退出", true, None);

    menu.append(&show_item).ok();
    menu.append(&PredefinedMenuItem::separator()).ok();
    menu.append(&console_item).ok();
    menu.append(&PredefinedMenuItem::separator()).ok();
    menu.append(&quit_item).ok();

    let tray = tray_icon::TrayIconBuilder::new()
        .with_icon(icon)
        .with_menu(Box::new(menu))
        .with_tooltip("AIGate")
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

    // ── 后台 tokio runtime ──
    let rt = tokio::runtime::Runtime::new().expect("创建运行时失败");

    // 在 runtime 上下文中初始化 tracing
    let _guard = rt.enter();

    tracing_subscriber::fmt()
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
            show_error(&format!("加载配置失败: {e}"));
            std::process::exit(1);
        }
    };

    let client = match reqwest::Client::builder()
        .dns_resolver(Arc::new(Ipv4Resolver::new()))
        .timeout(std::time::Duration::from_secs(660))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            show_error(&format!("创建 HTTP 客户端失败: {e}"));
            std::process::exit(1);
        }
    };

    let state = AppState {
        client,
        registry: Arc::new(RwLock::new(registry)),
        log_store: store::LogStore::new("data"),
        key_store: keys::KeyStore::new("data"),
        log_buffer: LogBuffer::new().with_store(store::LogStore::new("data")),
    };

    let app = Router::new()
        .route("/v1/chat/completions", post(proxy::chat_completions))
        .route("/v1/models", axum::routing::get(handle_models))
        .route("/health", axum::routing::get(|| async { "ok" }))
        .route("/admin", axum::routing::get(admin::admin_page))
        .route("/admin/api/logs", axum::routing::get(admin::api_logs).delete(admin::api_logs_delete))
        .route("/admin/api/routes", axum::routing::get(admin::api_routes))
        .route("/admin/api/providers", axum::routing::get(admin::api_providers_get))
        .route("/admin/api/providers/save", axum::routing::post(admin::api_providers_save))
        .route("/admin/api/providers/reload", axum::routing::post(admin::api_providers_reload))
        .route("/admin/api/providers/test", axum::routing::post(admin::api_providers_test))
        .route("/admin/api/keys", axum::routing::get(admin::api_keys_get).put(admin::api_keys_put))
        .route("/admin/api/health", axum::routing::get(admin::api_health))
        .route("/admin/api/stats", axum::routing::get(admin::api_stats))
        .route("/admin/api/mock", axum::routing::post(admin::api_mock))
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    info!("AIGate listening on http://{addr}");

    // ── 绑定端口并启动 Axum ──
    let listener = rt.block_on(async {
        tokio::net::TcpListener::bind(addr).await
    }).unwrap_or_else(|e| {
        show_error(&format!("端口 {port} 绑定失败: {e}\n\n请检查端口是否被占用或更换 PORT 环境变量。"));
        std::process::exit(1);
    });

    rt.spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            show_error(&format!("服务器运行错误: {e}"));
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
                // 退出程序
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
