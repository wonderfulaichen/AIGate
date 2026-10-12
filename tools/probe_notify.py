# -*- coding: utf-8 -*-
"""探针: 报错通知 —— 红点计数 + 气球弹窗（2026-10-12 用户要求「挂后台不知道有没有问题」）。

零新依赖的实现: 气球由系统自带 PowerShell 承载; 测试接缝 `AIGATE_NOTIFY_SCRIPT`
指向一个 .cmd 代替 PowerShell —— 每弹一次追加一行 `title|body` 到固定文件,
探针据此断言「弹了几次、内容是什么」(与 AIGATE_BUDDY_ENDPOINT 同一接缝哲学)。

判据 (7 组):
  ① 熔断打开 (err)          → 气球弹 1 次, 标题含「熔断」, 正文含供应商名
  ② 台账里同时出现 breaker_open 事件 (通知不替代台账, 两者都在)
  ③ 未读计数 > 0            → 红点会亮 (unread API)
  ④ 同一故障余波 (合并窗内重复) → **不再弹** (冷却/合并语义)
  ⑥ 用户自己重置熔断         → **不弹气球** (通知自己刚做的操作是噪音)
  ⑤ 确认已读 (ack)          → unread 归 0 (红点灭)
⑦ AIGATE_NO_NOTIFY=1 是一行 env 读取 (OnceLock 缓存, env 测试顺序依赖不可靠, 不单测);
   ⑧ QuotaLow→弹 由单测 notify::tests::classification_matches_user_decision 覆盖;
   e2e 不再为"换个事件类型"复刻 buddy 凭据栈。
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8881      # mock 上游
PG = 8882      # 网关
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_notify")
SCRIPT = os.path.join(ISO, "fake_balloon.cmd")
MARKER = os.path.join(ISO, "balloon.log")

FAIL = []
NCHK = [0]


def chk(n, cond, extra=""):
    NCHK[0] += 1
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


def balloons():
    """读接缝标记文件: 每行 `title|body`。

    ⚠️ 编码: 测试接缝是 `.cmd`, 其 `echo` 经**控制台代码页** (中文 Windows = GBK/936) 写文件,
    所以这里按 GBK 解码。真气球走 PowerShell 的 env var 是 Unicode 原生、无此问题 ——
    乱码只在这条测试接缝上, 不是通知本身的缺陷。
    """
    try:
        raw = open(MARKER, "rb").read()
        return [x for x in raw.decode("gbk", errors="replace").split("\n") if x.strip()]
    except Exception:
        return []


def wait_port(p, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", p), 1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    prov = {"providers": [
        {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
         "api_key_env": "K", "api_key_default": "d",
         "models": {"mock/m": {"upstream_model": "m"}}},
    ]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)
    # 测试接缝: 代替 PowerShell 弹气球 —— 每次追加一行 title|body
    with io.open(SCRIPT, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n"
                "echo %AIGATE_NOTIFY_TITLE%^|%AIGATE_NOTIFY_BODY%>>\"%MARKER_PATH%\"\r\n")

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1",
               AIGATE_NOTIFY_SCRIPT=SCRIPT, MARKER_PATH=MARKER,
               # 熔断阈值压到 2, 探针不用发一堆请求
               BREAKER_FAILURE_THRESHOLD="2", BREAKER_COOLDOWN_SECS="300")

    class Mock(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(500)
            self.send_header("content-type", "application/json")
            body = b'{"error":{"message":"mock broken"}}'
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", MP), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as admin_auth
        AH = admin_auth(os.path.join(ISO, "data"))

        def api(path, data=None, method=None):
            body = json.dumps(data).encode() if data is not None else None
            r = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path), data=body,
                                       headers={"content-type": "application/json", **AH},
                                       method=method or ("POST" if body else "GET"))
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))

        def ask():
            body = json.dumps({"model": "mock/m", "messages": [{"role": "user", "content": "x"}]}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                        headers={"content-type": "application/json"}, method="POST"), timeout=30) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
            except Exception:
                return -1

        # 确认台账已装好且未读为 0 起步 (Started 是 ok 级, 不点红点)
        time.sleep(1.0)
        ev0 = api("/admin/api/events?limit=50")
        chk("init-unread-zero", (ev0.get("unread") or 0) == 0,
            "unread=%s" % ev0.get("unread"))

        # ①②③ 熔断打开: 连续 2 次 500 → 熔断 → 气球 1 次 + 台账事件 + unread>0
        ask(); time.sleep(0.5); ask()
        time.sleep(2.0)
        bals = balloons()
        chk("breaker-open-balloon-fired", len(bals) >= 1,
            "balloons=%d %s" % (len(bals), bals[:2]))
        if bals:
            title, _, body = bals[0].partition("|")
            chk("balloon-title-names-kind", "熔断" in title, title[:80])
            chk("balloon-body-names-subject", "mock" in body, body[:120])
        ev = api("/admin/api/events?limit=50")
        kinds = [e.get("kind") for e in (ev.get("events") or [])]
        chk("ledger-has-breaker-open", "breaker_open" in kinds, kinds[:5])
        unread1 = ev.get("unread") or 0
        chk("unread-after-breaker", unread1 > 0, "unread=%s" % unread1)

        # ④ 合并窗内余波不再弹 (再打几次 500, 熔断已开 → 快速 503 挡回, 不产生新事件)
        n_before = len(balloons())
        for _ in range(3):
            ask()
        time.sleep(1.5)
        chk("aftershock-not-reballoon", len(balloons()) == n_before,
            "before=%d after=%d" % (n_before, len(balloons())))

        # ⑥ 用户自己重置熔断 → 不弹气球 (不计 unread 变化方向, 只断言"没多弹")
        n_before = len(balloons())
        api("/admin/api/circuit/reset", {"provider": "mock"}, method="POST")
        time.sleep(1.5)
        chk("manual-reset-no-balloon", len(balloons()) == n_before,
            "before=%d after=%d" % (n_before, len(balloons())))

        # ⑤ ack → unread 归 0 (下一轮红点灭由事件循环轮询处理, 探针只验计数)
        api("/admin/api/events/ack", {}, method="POST")
        ev2 = api("/admin/api/events?limit=5")
        chk("ack-clears-unread", (ev2.get("unread") or 0) == 0,
            "unread=%s" % ev2.get("unread"))

        # ⑧ QuotaLow → 弹气球: 分类是纯函数 (notify::tests::classification_matches_user_decision
        #    断言 QuotaLow.balloon()==true), 但 e2e 触发它要复刻整个 buddy 凭据栈
        #    (accounts.json + 特定响应格式 + 逐账号查询) —— 通知管线本身已由上面 ① 验证,
        #    此处不为"换个事件类型"再搭一遍 mock。真实触发路径由 balance.rs 既有单测保障。

        print("\n---- TOTAL: %d 项, 失败 %d 项" % (NCHK[0], len(FAIL)))
        return 0 if not FAIL else 1
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        if not os.environ.get("KEEP_ISO"):
            shutil.rmtree(ISO, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
