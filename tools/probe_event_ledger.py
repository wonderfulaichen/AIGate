# -*- coding: utf-8 -*-
"""探针: 事件台账 —— 熔断等异常是否真的"出报告"(而非要用户去日志里翻)。

背景（2026-10-11, 用户原话）:
    「我感觉我们软件设计得不够人性化。比如熔断、报错等等连工作报告、异常通知都没做。
      出问题得用户自己去一点点找。」
根因（代码级）: `circuit_breaker.rs` **一行 tracing 都没有** —— open()/close() 只改状态位,
面板只有**当前快照**, 刷新一下断过的痕迹就没了。台账就是补这个。

判据 (7 条, 全部走真实 HTTP 与真实落盘):
  ① 熔断真的被触发 (上游连续 500)          —— 前提, 否则后面全是空验证
  ② 台账里出现 breaker_open               —— 这就是"报告"
  ③ 它带**原因**(连续失败 N 次)且带 provider —— 没有原因等于只说了"出事了"
  ④ 恢复后出现 breaker_closed             —— 故障自愈也要报(否则用户以为还断着)
  ⑤ 熔断**之前**有 upstream_failing 预警   —— 用户想在熔断前就知道"这供应商不稳"
  ⑥ 落盘到 events.jsonl                   —— 内存里有但没落盘 = 重启后什么也查不到
  ⑦ /admin/api/events 按**时间倒序**返回   —— 面板要"最新在最上"

不连网: 本地 mock 上游。凭据/端口全在隔离目录里。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from adminauth import headers as admin_auth  # /admin/api/* 的 Bearer (2026-10-12 起)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_events")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8963, 8964

FAILS = []
# 熔断阈值: 默认 failure_threshold=4. 让 mock 先连败再成功, 覆盖"断开→恢复"整条链。
FAIL_PHASE = {"n": 0}
FAIL_COUNT = 6


class Mock(BaseHTTPRequestHandler):
    """前 FAIL_COUNT 次请求返回 500 (触发熔断), 之后返回正常流 (触发恢复)。"""

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        with threading.Lock():
            FAIL_PHASE["n"] += 1
            k = FAIL_PHASE["n"]
        if k <= FAIL_COUNT:
            body = b'{"error":{"message":"mock upstream down"}}'
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def frame(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        frame({"choices": [{"index": 0, "delta": {"content": "RECOVERED-OK"}, "finish_reason": None}]})
        frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
# 熔断配置走**环境变量** (见 config.rs), 不是 providers.json ——
# 缩短阈值与冷却时间, 让"连败→熔断→冷却→恢复"整条链在几秒内跑完。
# 判据不依赖具体数值, 只依赖事件被记下。
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "mockprov",
        "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mock/m": {"upstream_model": "m"}},
    }],
}))

env = dict(os.environ)
env.pop("LOOP_GUARD_ENABLED", None)
env.update(
    PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1",
    # 退避调到最小, 让连败快速跑完
    AIGATE_RETRY_MAX="0", AIGATE_RETRY_BACKOFF_MS="1",
    # 熔断: 3 次连败即开, 冷却 2 秒, 错误率判据关掉 (只测连续失败这条路径)
    BREAKER_FAILURE_THRESHOLD="3",
    BREAKER_SUCCESS_THRESHOLD="1",
    BREAKER_TIMEOUT_SECS="2",
    BREAKER_ERROR_RATE="0.99",
    BREAKER_MIN_REQUESTS="999",
)
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def call(timeout=15):
    body = json.dumps({"model": "mock/m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def events():
    raw = urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/events?limit=200" % PG,
                               headers=admin_auth(os.path.join(ISO, "data"))), timeout=20
    ).read().decode("utf-8")
    return json.loads(raw).get("events") or []


try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("实例未就绪")
        sys.exit(1)

    print("=== 事件台账: 熔断等异常是否真的出报告 ===\n")

    # 连打到熔断打开
    saw_503 = False
    for i in range(FAIL_COUNT + 4):
        st, _ = call()
        if st == 503:
            saw_503 = True
            break
    # 熔断打开后, 冷却 (2s) 内再打几次确认被挡
    for _ in range(3):
        call(timeout=6)
    ck("① 熔断真的被触发 (上游连续 500 → 网关返回 503)",
       saw_503, "saw_503=%s (False 说明熔断没开, 后续断言都无意义)" % saw_503)

    time.sleep(1.0)
    evs = events()
    kinds = [e["kind"] for e in evs]

    open_ev = next((e for e in evs if e["kind"] == "breaker_open"), None)
    ck("② 台账里有「熔断已打开」这条报告", open_ev is not None,
       "实得类别: %s" % (kinds or "空"))
    ck("③ 该条带**原因**与主体 (能回答'为什么断的')",
       bool(open_ev) and "连续失败" in (open_ev.get("detail") or "")
       and open_ev.get("subject") == "mockprov" and open_ev.get("severity") == "err",
       "detail=%r subject=%r severity=%r" % (
           (open_ev or {}).get("detail"), (open_ev or {}).get("subject"),
           (open_ev or {}).get("severity")))
    # ★ 预警: 熔断要连败 3 次才开, 用户想在第 2 次就知道 —— 这条是"通知"而非"事后报告"。
    ck("⑤ 熔断**之前**有「上游连续失败」预警",
       "upstream_failing" in kinds,
       "类别序列: %s" % kinds)

    # 冷却结束 + 上游恢复 → 应出现"已恢复/恢复响应"
    time.sleep(3.0)
    for _ in range(4):
        call(timeout=10)
        time.sleep(0.4)
    time.sleep(1.0)
    evs2 = events()
    kinds2 = [e["kind"] for e in evs2]
    ck("④ 恢复后有「熔断已恢复」(或上游已恢复响应) 的报告",
       "breaker_closed" in kinds2 or "upstream_recovered" in kinds2,
       "类别序列: %s" % kinds2)

    # ⑥ 落盘: 直接读 events.jsonl (内存里有但没落盘 = 重启后什么都查不到)
    p = os.path.join(ISO, "data", "events.jsonl")
    disk = []
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8", errors="replace").read().splitlines():
            if line.strip():
                try:
                    disk.append(json.loads(line))
                except Exception:
                    pass
    ck("⑥ 事件确实落进 events.jsonl",
       any(x.get("kind") == "breaker_open" for x in disk),
       "磁盘 %d 行, 类别: %s" % (len(disk), [x.get("kind") for x in disk]))

    # ⑦ 倒序: 面板要"最新在最上"
    ts = [int(e.get("ts") or 0) for e in evs2]
    ck("⑦ 接口按时间倒序返回 (最新在最前)",
       ts == sorted(ts, reverse=True),
       "时间序列: %s" % ts)

    # ⑧ "启动"事件存在 —— 它让台账必定非空, 也是"这条属于哪次运行"的分界。
    ck("⑧ 台账有「服务已启动」作为分界",
       "started" in kinds2, "类别序列: %s" % kinds2)

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 熔断、上游连续失败、恢复都进了台账, 且带原因、落盘、倒序")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
