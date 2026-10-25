//! opencode 一键登录的 **Win32 + WebView2 原生实现**（不经过 wry/winit）。
//!
//! # 为什么不用 wry
//!
//! 第一版是把 wry WebView 放在独立线程里建窗。实测报
//! `EventLoop can't be recreated` —— winit 只允许每次进程创建**一个** EventLoop，
//! 主窗口已经用掉了那个名额（`main.rs` 的 winit 事件循环）。所以"独立线程再建
//! 一个 wry 窗口"这条路在 wry 0.38 上根本走不通，不是参数问题。
//!
//! 改用 `webview2-com` 直接建窗：`CreateWindowExW` 出原生窗口，
//! `CreateCoreWebView2Environment` + `CreateCoreWebView2Controller` 挂 WebView2，
//! 自己跑 `GetMessageW` 消息循环。这条路径不依赖 winit，可以在任意线程反复建。
//!
//! # 消息泵与 COM 回调
//!
//! WebView2 的完成回调（包括 `GetCookies`）靠**消息泵**投递。所以：
//! ① 建立环境/控制器用 `wait_for_async_operation`（它内部会泵消息）；
//! ② 轮询 cookie 不能 `recv()` 阻塞等 —— 那会堵住泵，回调永远到不了。
//!    改为 `PeekMessage` + `SetTimer` 驱动的非阻塞轮询，见 [`run`]。

use std::sync::{Arc, Mutex};

use webview2_com::Microsoft::Web::WebView2::Win32::*;
use webview2_com::{
    CreateCoreWebView2ControllerCompletedHandler, CreateCoreWebView2EnvironmentCompletedHandler,
};
use windows::core::{Interface, HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::{E_POINTER, HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect, GetMessageW,
    KillTimer, PostQuitMessage, RegisterClassW, SetTimer, SetWindowTextW, ShowWindow,
    TranslateMessage, CW_USEDEFAULT, MSG, SW_SHOW, WM_CLOSE, WM_DESTROY, WM_TIMER, WNDCLASSW,
    WS_OVERLAPPEDWINDOW,
};

use crate::oc_login::{credential_from_cookies, extract_org_id, LoginState, Stage};

/// 轮询 cookie 的定时器周期（毫秒）。
///
/// 为什么 1s：用户在授权页要点按钮、过 2FA，快于此没有意义；而每次轮询都要
/// 走一趟 COM 跨进程调用，太密只是白烧 CPU。
const POLL_MS: u32 = 1000;

/// 窗口类的名称（RegisterClassW 用；同一进程重复注册无害）。
const WCLASS: &str = "AIGateOcLogin";

/// 窗口标题。
///
/// 含 ASCII "opencode" 是为了**可被外部工具识别**（探针按标题找窗口），
/// 中文部分是给人看的。
const WINDOW_TITLE: &str = "登录 opencode（完成后自动关闭）";

/// 定时器 ID。
const TIMER_ID: usize = 1;

/// 窗口/WebView 的上下文，挂在 `GWLP_USERDATA` 上。
struct Ctx {
    state: Arc<LoginState>,
    controller: ICoreWebView2Controller,
    webview: ICoreWebView2,
    /// 最近一次 `GetCookies` 的结果（回调线程写入，UI 线程取走）。
    cookies: Arc<Mutex<Option<Vec<(String, String)>>>>,
}

/// 在主线程外的**新线程**里弹窗并阻塞到拿到凭据 / 用户关闭 / 出错。
///
/// 返回 `Err` 仅表示"没拿到结果且非用户取消"（例如 WebView2 运行时缺失）。
pub fn run_login_window(state: Arc<LoginState>) -> Result<(), String> {
    unsafe {
        // WebView2 要求 STA。失败（已是别的套间模型）时继续尝试 —— 多数情况能work。
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }

    let hwnd = create_window()?;
    let (controller, webview) = create_webview(hwnd)?;

    let cookies: Arc<Mutex<Option<Vec<(String, String)>>>> = Arc::new(Mutex::new(None));

    // 设置初始尺寸（不给就是 0×0 → 纯白）
    let size = client_size(hwnd);
    unsafe {
        let _ = controller.SetBounds(RECT {
            left: 0,
            top: 0,
            right: size.0,
            bottom: size.1,
        });
        let _ = controller.SetIsVisible(true);
        // 打开登录页
        let url = HSTRING::from("https://opencode.ai/console/login");
        let _ = webview.Navigate(&url);
        let _ = ShowWindow(hwnd, SW_SHOW);
    }

    // 上下文交给窗口过程（用 Box::into_raw 保住生命周期，WM_DESTROY 时收回）
    let ctx = Box::new(Ctx {
        state: Arc::clone(&state),
        controller,
        webview,
        cookies: Arc::clone(&cookies),
    });
    unsafe {
        windows::Win32::UI::WindowsAndMessaging::SetWindowLongPtrW(
            hwnd,
            windows::Win32::UI::WindowsAndMessaging::GWLP_USERDATA,
            Box::into_raw(ctx) as isize,
        );
        // 启动轮询定时器
        SetTimer(hwnd, TIMER_ID, POLL_MS, None);
    }

    // 自己跑消息循环（不依赖 winit）
    let mut msg = MSG::default();
    loop {
        let r = unsafe { GetMessageW(&mut msg, None, 0, 0) }.0;
        if r == -1 {
            return Err("登录窗口消息循环异常".to_string());
        }
        if r == 0 {
            break; // WM_QUIT
        }
        unsafe {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }

    // 收口：拿状态说话
    match state.stage() {
        Stage::Done => Ok(()),
        Stage::Waiting => {
            // 消息循环退出但还没结果 —— 视为用户关闭窗口
            state.cancel();
            Ok(())
        }
        _ => Ok(()),
    }
}

/// 建原生窗口（不可见；由调用方 ShowWindow）。
fn create_window() -> Result<HWND, String> {
    let class = HSTRING::from(WCLASS);
    let wc = WNDCLASSW {
        lpfnWndProc: Some(wndproc),
        hInstance: unsafe { GetModuleHandleW(None) }
            .map(|h| HINSTANCE(h.0))
            .unwrap_or_default(),
        lpszClassName: PCWSTR(class.as_ptr()),
        ..Default::default()
    };
    unsafe {
        RegisterClassW(&wc); // 重复注册返回 0，无害
        let title = HSTRING::from(WINDOW_TITLE);
        // windows 0.54：CreateWindowExW 直接返回 HWND（失败为空句柄），不是 Result
        let hwnd = CreateWindowExW(
            Default::default(),
            PCWSTR(class.as_ptr()),
            &title,
            WS_OVERLAPPEDWINDOW,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            560,
            760,
            None,
            None,
            wc.hInstance,
            None,
        );
        if hwnd.0 == 0 {
            return Err("创建登录窗口失败：句柄为空".to_string());
        }
        Ok(hwnd)
    }
}

/// 建 WebView2 环境 + 控制器（异步 → 用 `wait_for_async_operation` 泵消息等）。
fn create_webview(hwnd: HWND) -> Result<(ICoreWebView2Controller, ICoreWebView2), String> {
    use std::sync::mpsc;
    use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2EnvironmentOptions;

    // ⚠️ **必须给独立的 user data folder**。
    //
    // 主窗口那份 wry WebView 已经在进程内用默认目录建过一个环境。同一进程第二次
    // 用**同一目录**建环境时，`CreateCoreWebView2Controller` 会返回
    // `0x8007139F ERROR_INVALID_STATE`（"组或资源的状态不是执行请求操作的正确状态"）
    // —— 这不是权限或运行时缺失，而是 WebView2 不允许同一 user data folder 有
    // 两个环境实例。给登录窗口一个独立目录即可（顺带把登录 Cookie 与主面板隔离，
    // 本来就是两套会话，隔离更干净）。
    let mut opts: Option<ICoreWebView2EnvironmentOptions> = None;
    let data_dir = crate::config::oc_login_data_dir();
    let data_dir_wide = HSTRING::from(data_dir.as_str());

    let env = {
        let (tx, rx) = mpsc::channel();
        CreateCoreWebView2EnvironmentCompletedHandler::wait_for_async_operation(
            Box::new(move |handler| unsafe {
                CreateCoreWebView2EnvironmentWithOptions(
                    None,
                    PCWSTR(data_dir_wide.as_ptr()),
                    opts.take().as_ref(),
                    &handler,
                )
                .map_err(webview2_com::Error::WindowsError)
            }),
            Box::new(move |code, env| {
                code?;
                let env = env.ok_or_else(|| windows::core::Error::from(E_POINTER))?;
                let _ = tx.send(env);
                Ok(())
            }),
        )
        .map_err(|e| format!("创建 WebView2 环境失败: {e}"))?;
        rx.recv()
            .map_err(|_| "创建 WebView2 环境失败：回调通道断开".to_string())?
    };

    let controller = {
        let (tx, rx) = mpsc::channel();
        CreateCoreWebView2ControllerCompletedHandler::wait_for_async_operation(
            Box::new(move |handler| unsafe {
                env.CreateCoreWebView2Controller(hwnd, &handler)
                    .map_err(webview2_com::Error::WindowsError)
            }),
            Box::new(move |code, ctrl| {
                code?;
                let ctrl = ctrl.ok_or_else(|| windows::core::Error::from(E_POINTER))?;
                let _ = tx.send(ctrl);
                Ok(())
            }),
        )
        .map_err(|e| format!("创建 WebView2 控制器失败: {e}"))?;
        rx.recv()
            .map_err(|_| "创建 WebView2 控制器失败：回调通道断开".to_string())?
    };

    let webview = unsafe { controller.CoreWebView2() }
        .map_err(|e| format!("取 CoreWebView2 失败: {e}"))?;
    Ok((controller, webview))
}

fn client_size(hwnd: HWND) -> (i32, i32) {
    let mut r = RECT::default();
    unsafe {
        let _ = GetClientRect(hwnd, &mut r);
    }
    ((r.right - r.left).max(500), (r.bottom - r.top).max(700))
}

/// 发起一次异步 cookie 读取，结果写进 `sink`（**不阻塞**，回调靠消息泵投递）。
fn request_cookies(webview: &ICoreWebView2, sink: &Arc<Mutex<Option<Vec<(String, String)>>>>) {
    let Ok(core2) = webview.cast::<ICoreWebView2_2>() else {
        return;
    };
    let Ok(cm) = (unsafe { core2.CookieManager() }) else {
        return;
    };
    let sink = Arc::clone(sink);
    let handler = webview2_com::GetCookiesCompletedHandler::create(Box::new(
        move |_hr, list: Option<ICoreWebView2CookieList>| {
            let mut out = Vec::new();
            if let Some(list) = list {
                let mut count = 0u32;
                if unsafe { list.Count(&mut count) }.is_ok() {
                    for i in 0..count {
                        let Ok(cookie) = (unsafe { list.GetValueAtIndex(i) }) else {
                            continue;
                        };
                        let mut name = PWSTR::null();
                        let mut value = PWSTR::null();
                        if unsafe { cookie.Name(&mut name) }.is_err() {
                            continue;
                        }
                        if unsafe { cookie.Value(&mut value) }.is_err() {
                            continue;
                        }
                        let n = unsafe { name.to_string() }.unwrap_or_default();
                        let v = unsafe { value.to_string() }.unwrap_or_default();
                        if !n.is_empty() {
                            out.push((n, v));
                        }
                    }
                }
            }
            if let Ok(mut g) = sink.lock() {
                *g = Some(out);
            }
            Ok(())
        },
    ));
    // 读失败不当成登录失败：下一轮定时器还会再试
    let _ = unsafe { cm.GetCookies(PCWSTR::null(), &handler) };
}

/// 取当前 URL（用于提取组织 ID）。
fn current_url(webview: &ICoreWebView2) -> String {
    unsafe {
        let mut p = PWSTR::null();
        if webview.Source(&mut p).is_err() {
            return String::new();
        }
        p.to_string().unwrap_or_default()
    }
}

extern "system" fn wndproc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    unsafe {
        let ptr = windows::Win32::UI::WindowsAndMessaging::GetWindowLongPtrW(
            hwnd,
            windows::Win32::UI::WindowsAndMessaging::GWLP_USERDATA,
        ) as *mut Ctx;

        match msg {
            WM_TIMER => {
                if ptr.is_null() {
                    return LRESULT(0);
                }
                let ctx = &*ptr;
                // ① 停止请求（外部点了取消）→ 关窗
                if ctx.state.should_stop() {
                    let _ = KillTimer(hwnd, TIMER_ID);
                    let _ = DestroyWindow(hwnd);
                    return LRESULT(0);
                }
                // ② 先看上一轮 cookie 到没到
                let got = ctx.cookies.lock().ok().and_then(|mut g| g.take());
                if let Some(cs) = got {
                    let url = current_url(&ctx.webview);
                    let org = extract_org_id(&url);
                    if let Some(cred) = credential_from_cookies(&cs, org.as_deref()) {
                        ctx.state.finish_ok(cred);
                        let _ = KillTimer(hwnd, TIMER_ID);
                        let _ = DestroyWindow(hwnd);
                        return LRESULT(0);
                    }
                }
                // ③ 发起下一轮读
                request_cookies(&ctx.webview, &ctx.cookies);
                // 顺带刷新标题提示（用户能看到"正在等待授权"）
                let _ = SetWindowTextW(hwnd, &HSTRING::from(WINDOW_TITLE));
                LRESULT(0)
            }
            WM_CLOSE => {
                if !ptr.is_null() {
                    let ctx = &*ptr;
                    // 已拿到凭据时不要覆盖成 Cancelled
                    if ctx.state.stage() == Stage::Waiting {
                        ctx.state.cancel();
                    }
                }
                let _ = KillTimer(hwnd, TIMER_ID);
                let _ = DestroyWindow(hwnd);
                LRESULT(0)
            }
            WM_DESTROY => {
                if !ptr.is_null() {
                    // 收回 Box，释放 WebView2 控制器（顺序：先关控制器再丢上下文）
                    let ctx = Box::from_raw(ptr);
                    let _ = ctx.controller.Close();
                    drop(ctx);
                    windows::Win32::UI::WindowsAndMessaging::SetWindowLongPtrW(
                        hwnd,
                        windows::Win32::UI::WindowsAndMessaging::GWLP_USERDATA,
                        0,
                    );
                }
                PostQuitMessage(0);
                LRESULT(0)
            }
            _ => DefWindowProcW(hwnd, msg, wp, lp),
        }
    }
}

// ─────────────────────────── 测试 ───────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// 窗口类名不能变：`RegisterClassW` 用的是它，改了会让旧窗口过程失配。
    #[test]
    fn window_class_name_is_stable() {
        assert_eq!(WCLASS, "AIGateOcLogin");
    }

    /// 轮询周期必须是正数（0 会让 SetTimer 变成"只触发一次的毫秒数"语义）。
    #[test]
    fn poll_interval_is_positive() {
        assert!(POLL_MS >= 100, "轮询太密会白烧 CPU: {POLL_MS}ms");
        assert!(POLL_MS <= 5000, "轮询太疏用户会觉得卡: {POLL_MS}ms");
    }
}
