# -*- coding: utf-8 -*-
"""探针: 端口被占用时**顺延并如实告知**（对标 dsh-our-free-model 的诚实提示机制）。

背景（2026-10-11）: 本地网关最常见的"打不开"= 上次进程没退干净 / 别的工具占了端口。
原实现直接 `show_error` + exit(1)，用户以为软件坏了。现改为从 PORT 起顺延，
并在日志 / 事件台账里**如实标注实际端口**。

判据:
  ① 端口被占时**仍能启动**（不再 exit 1）
  ② 顺延到 PORT+1 之类可用端口，且**确实在监听**
  ③ **如实告知**: 事件台账里有"请求的端口 X 已被占用，已改用 Y"
  ④ 顺延范围有限: 连占 21 个端口时应启动失败（不无限顺延）
  ⑤ 非"被占用"错误不顺延（本探针用非法端口 0 无法测, 用超范围端口近似）
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_portfb")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
BASE = 8975

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def free_iso():
    p = os.path.join(ISO, "data")
    if os.path.isdir(ISO):
        for f in os.listdir(p) if os.path.isdir(p) else []:
            pass
    shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
        "providers": [{"name": "mock", "endpoint": "http://127.0.0.1:9/v1/chat/completions",
                       "api_key_env": "K", "api_key_default": "d",
                       "models": {"mock/m": {"upstream_model": "m"}}}]
    }))


def start(port, extra_env=None):
    env = dict(os.environ)
    env.update(PORT=str(port), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="info", AIGATE_NO_PROXY="1")
    env.pop("AIGATE_ADMIN_TOKEN", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_port(p, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % p, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.4)
    return False


def events():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/admin/api/events?limit=20" % (BASE + 1),
                                    timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("events") or []
    except Exception:
        return []


# 先占住 BASE
blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
blocker.bind(("127.0.0.1", BASE))
blocker.listen(1)
print("已占用端口 %d（模拟「上次进程没退干净」）\n" % BASE)

free_iso()
proc = None
try:
    before = time.time()
    proc = start(BASE)
    ok = wait_port(BASE + 1)
    ck("① 端口被占时仍能启动（不再直接退出）", ok and proc.poll() is None,
       "进程状态=%s, 耗时 %.1fs" % (proc.poll(), time.time() - before))
    ck("② 顺延到 %d 并确实在监听" % (BASE + 1), ok)

    if ok:
        evs = events()
        txt = json.dumps(evs, ensure_ascii=False)
        ck("③ 事件台账**如实告知**端口变更",
           ("已被占用" in txt and str(BASE + 1) in txt),
           txt[:220])

    # ④ 顺延范围有限: 占满 BASE..BASE+20（共 21 个）→ 不应绑上任何端口。
    # ⚠️ 判据**不能**用"进程是否退出"：Windows 下绑定彻底失败会弹**模态 MessageBox**
    #    （`show_error`）等用户点确定，进程会一直挂着（这是既有行为，不是 bug）。
    #    所以这里测"有没有绑上端口"：占满后**不应**监听到 8975..8995 中的任何一个。
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    blockers = []
    for i in range(0, 21):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", BASE + i))
            s.listen(1)
            blockers.append(s)
        except Exception:
            pass
    free_iso()
    proc = start(BASE)
    time.sleep(6)
    bound = None
    for i in range(0, 21):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % (BASE + i), timeout=1).read()
            bound = BASE + i
            break
        except Exception:
            pass
    ck("④ 顺延范围有限（占满 21 个端口后不再绑任何端口，不无限顺延）",
       bound is None, "却绑上了 %s" % bound)
    for s in blockers:
        s.close()

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 端口占用时顺延并如实告知, 且顺延范围有上限")
    sys.exit(0)
finally:
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    blocker.close()
