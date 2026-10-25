# -*- coding: utf-8 -*-
"""opencode 一键登录：端点契约 + 真实弹窗验证。

为什么需要这个探针：

「一键登录」有三层，任一层坏了用户都只看到"没反应"：
  ① HTTP 端点契约（未知供应商要拒、未登录时 stage 必须是 idle 不是 done）；
  ② 窗口真的弹出来（winit 只允许一个 EventLoop —— 曾经的实现必报
     `EventLoop can't be recreated`，端点却仍返回 waiting，看起来"正常"）；
  ③ 窗口里真的渲染了页面（WebView2 同进程同 user data folder 会返回
     `0x8007139F`，端点仍返回 waiting）。

②③ 是"静默失败"的典型：端点全绿、用户看不到窗口。所以本探针**必须**验证
真实窗口存在且非纯色，不能只打 API。

用法（默认跑全部；--api-only 跳过窗口层，用于 CI）：
    python tools/probe_oc_login.py
    python tools/probe_oc_login.py --api-only
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from ctypes import (
    WINFUNCTYPE, byref, c_bool, c_long, c_ubyte, c_uint, c_ushort, c_void_p,
    create_unicode_buffer, sizeof, windll,
)
from ctypes import Structure

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
DATA = os.path.join(ROOT, ".openbitfun", "tmp", "probe_oc_login_data")
PORT = 18801
TOKEN = "probe-oc-login-token"
SHOT = os.path.join(ROOT, ".openbitfun", "tmp", "probe_oc_login.png")

user32 = windll.user32
gdi32 = windll.gdi32


class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


class BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", c_uint), ("biWidth", c_long), ("biHeight", c_long),
        ("biPlanes", c_ushort), ("biBitCount", c_ushort), ("biCompression", c_uint),
        ("biSizeImage", c_uint), ("biXPelsPerMeter", c_long), ("biYPelsPerMeter", c_long),
        ("biClrUsed", c_uint), ("biClrImportant", c_uint),
    ]


class Probe:
    def __init__(self):
        self.ok = 0
        self.fail = 0

    def check(self, cond, msg, detail=""):
        if cond:
            print("[PASS] %s" % msg)
            self.ok += 1
        else:
            print("[FAIL] %s%s" % (msg, ("  << " + detail) if detail else ""))
            self.fail += 1


def post(path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def windows_of(pid):
    """该进程所有顶层窗口的句柄集合（ctypes 直读，避免 PowerShell 编码问题）。"""
    handles = []
    proto = WINFUNCTYPE(c_bool, c_void_p, c_void_p)

    def cb(h, _l):
        p = c_uint()
        user32.GetWindowThreadProcessId(h, byref(p))
        if p.value == pid:
            handles.append(h)
        return True

    user32.EnumWindows(proto(cb), 0)
    return set(handles)


def find_login_hwnd(pid):
    """按**窗口类名**找登录窗口（类名是我们自己注册的 ASCII 常量）。"""
    proto = WINFUNCTYPE(c_bool, c_void_p, c_void_p)
    found = []

    def cb(h, _l):
        p = c_uint()
        user32.GetWindowThreadProcessId(h, byref(p))
        if p.value == pid:
            cls = create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            if cls.value == "AIGateOcLogin":
                found.append(h)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


def window_size(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, byref(r))
    return r.right - r.left, r.bottom - r.top


def capture(hwnd, path):
    """PrintWindow(PW_RENDERFULLCONTENT) → DIB → PNG（不依赖窗口在最前）。"""
    from PIL import Image

    w, h = window_size(hwnd)
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)

    bi = BITMAPINFOHEADER()
    bi.biSize = sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h  # 负数 = 自上而下
    bi.biPlanes = 1
    bi.biBitCount = 32

    buf = (c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
    img.convert("RGB").save(path)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return w, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-only", action="store_true", help="只验端点契约，不验真实窗口")
    args = ap.parse_args()

    if not os.path.exists(EXE):
        print("找不到 %s，先 cargo build --release" % EXE)
        return 2
    if os.path.isdir(DATA):
        shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)

    # ⚠️ providers.json 的基准是 **exe 所在目录**（main.rs::exe_dir），不是 cwd，
    # 也不是 AIGATE_DATA_DIR —— 必须把 exe 复制进隔离目录，否则它会去
    # target/release/ 找配置，找不到就播种默认配置（provA/provB）。
    exe = os.path.join(DATA, "AIGate.exe")
    shutil.copy(EXE, exe)
    real_providers = os.path.join(ROOT, "providers.json")
    if os.path.exists(real_providers):
        shutil.copy(real_providers, os.path.join(DATA, "providers.json"))

    env = dict(os.environ, PORT=str(PORT), AIGATE_DATA_DIR=DATA, AIGATE_ADMIN_TOKEN=TOKEN)
    log = open(os.path.join(DATA, "run.log"), "wb")
    p = subprocess.Popen([exe], cwd=DATA, env=env, stdout=log, stderr=subprocess.STDOUT)

    pr = Probe()
    try:
        for _ in range(120):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3).read()
                break
            except Exception:
                if p.poll() is not None:
                    print("实例启动失败：")
                    print(open(os.path.join(DATA, "run.log"), encoding="utf-8",
                               errors="replace").read()[-2000:])
                    return 1
        else:
            print("实例未就绪")
            return 1

        # ── ① 端点契约 ──
        j = post("/admin/api/opencode/login/start", {"provider": "绝不能存在的供应商xyz"})
        pr.check("error" in j, "未知供应商被拒", str(j))

        j = post("/admin/api/opencode/login/start", {})
        pr.check("error" in j, "缺 provider 被拒", str(j))

        j = post("/admin/api/opencode/login/status", {"provider": "go"})
        pr.check(j.get("stage") == "idle",
                 "未登录时 stage=idle（不能是 done）", "得到 %r" % j.get("stage"))

        post("/admin/api/opencode/login/cancel", {})
        j = post("/admin/api/opencode/login/status", {"provider": "go"})
        pr.check(j.get("stage") != "done", "cancel 后不是 done", "得到 %r" % j.get("stage"))

        if args.api_only:
            print("\n%d 通过 / %d 失败（--api-only，未验真实窗口）" % (pr.ok, pr.fail))
            return 0 if pr.fail == 0 else 1

        # ── ② 真实窗口 ──
        #
        # ⚠️ 必须先确认**主窗口已经存在**再测登录窗口。
        # 主窗口的 wry WebView 会占用 WebView2 的默认 user data folder；
        # 如果主窗口还没建好就去测，登录窗口即使与它共用默认目录也不会冲突，
        # 于是"独立目录"这个修法看起来可删 —— 假通过。
        before = set()
        for _ in range(40):
            before = windows_of(p.pid)
            if before:
                break
            time.sleep(0.5)
        print("（诊断）启动前该进程窗口数: %d" % len(before))
        pr.check(len(before) > 0,
                 "主窗口已存在（否则测不出 user data folder 冲突）",
                 "窗口数 %d" % len(before))
        j = post("/admin/api/opencode/login/start", {"provider": "go"})
        pr.check(j.get("stage") == "waiting", "start 返回 waiting", str(j))

        hwnd = None
        for _ in range(40):
            time.sleep(1)
            hwnd = find_login_hwnd(p.pid)
            if hwnd:
                break
        pr.check(hwnd is not None,
                 "登录窗口真的弹出来了（类名 AIGateOcLogin）",
                 "窗口集合 %d -> %d" % (len(before), len(windows_of(p.pid))))
        if not hwnd:
            print("\n%d 通过 / %d 失败" % (pr.ok, pr.fail))
            return 1

        w, h = window_size(hwnd)
        pr.check(w > 100 and h > 100, "窗口有真实尺寸（不是 0x0 白窗）", "%dx%d" % (w, h))

        j = post("/admin/api/opencode/login/status", {"provider": "go"})
        pr.check(j.get("stage") == "waiting",
                 "用户未操作时 stage 保持 waiting", "得到 %r / %r" % (j.get("stage"), j.get("error")))

        # ── ③ 窗口里真的渲染了页面 ──
        time.sleep(15)  # WebView2 首次启动 + 加载 opencode 登录页
        try:
            capture(hwnd, SHOT)
            from PIL import Image
            img = Image.open(SHOT).convert("RGB")
            colors = img.getcolors(maxcolors=200000)
            n = len(colors) if colors else 200000
            pr.check(n > 20,
                     "窗口里有真实渲染内容（不是纯色 → WebView2 真的在跑）",
                     "不同颜色数 %d，截图见 %s" % (n, SHOT))
        except ImportError:
            print("[SKIP] 没有 Pillow，跳过渲染判据")
        except Exception as e:
            pr.check(False, "窗口渲染检查", str(e))

        # ── ④ 取消真的关窗 ──
        hwnd_before = find_login_hwnd(p.pid)
        post("/admin/api/opencode/login/cancel", {})
        gone = False
        for _ in range(25):
            time.sleep(1)
            if find_login_hwnd(p.pid) is None:
                gone = True
                break
        pr.check(gone, "取消后登录窗口真的关闭了",
                 "hwnd %s 仍在" % hwnd_before)
        j = post("/admin/api/opencode/login/status", {"provider": "go"})
        pr.check(j.get("stage") == "cancelled", "取消后 stage=cancelled",
                 "得到 %r" % j.get("stage"))

        return 0 if pr.fail == 0 else 1
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        log.close()
        print("\n%d 通过 / %d 失败" % (pr.ok, pr.fail))


if __name__ == "__main__":
    sys.exit(main())
