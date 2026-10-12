# -*- coding: utf-8 -*-
"""复现: 上游持续发「非内容帧」时, 网关空等到整体超时 (用户 2026-10-08 报障).

现场证据 (真实日志 26 条, 全部 go/mimo-v2.6-flash, 输入 ~430K):
    status=502 latency=365400ms first_token_ms=None completion_tokens=0
    error: 上游在生成中途断流（…客户端已收到 length 终止帧…）[末帧: : keep-alive ⏎ …]

三个疑点, 逐条用行为验证 (不读代码):
  ① **空闲超时为何没生效**: 设 120s, 实测却等了 365s. 怀疑上游在"持续发帧"
     (每帧间隔 < 120s) 从而不断重置计时器, 但那些帧**不带任何正文** → 网关陪着空等.
  ② **首字延迟与输出均为空**: 说明从未收到 delta.content.
  ③ **报错文案误导**: "客户端已收到 length 终止帧" —— 那个 length 帧是**网关自己补发**的,
     不是上游发的; 而真正的诊断信息 (上游最后发了什么) 被网关自己注入的 `: keepalive` 挤掉.

本探针: mock 上游持续发送**空 delta 帧**(带 role / 空 choices), 间隔远小于空闲超时,
但不含任何 content; 观察网关多久才放弃、以及记录的 error 文案说了什么.
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_idle_noise")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8983, 8984
NOISE_SECS = 8          # mock 持续发噪声帧的时长
NOISE_GAP = 0.5         # 帧间隔 (远小于空闲超时 120s → 计时器不断被重置)
IDLE_SECS = 3           # 隔离实例的空闲超时设小, 便于快速观察

class _ClientGone(Exception):
    """客户端 (网关) 已断开 —— 空闲超时生效时的预期结果."""

class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        t0 = time.time()
        # 真实形态 (2026-10-08 从 go 上游实测的 [末帧: : keep-alive …] 反推):
        # 上游**持续下发 SSE 注释帧保活**, 但**不产出任何正文** —— 本次请求就是这么空等的.
        def w(s):
            # 网关在空闲超时后主动断开是预期行为 (本探针就是在验证它); 写失败直接退出循环.
            try:
                self.wfile.write(s.encode()); self.wfile.flush()
            except OSError:
                raise _ClientGone()
        try:
            w('data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n')
            while time.time() - t0 < NOISE_SECS:
                w(': keepalive\n\n')        # ← 关键: 注释帧, 无任何负载
                time.sleep(NOISE_GAP)
            # 然后静默 (不发终止帧, 也不关闭连接) —— 空闲超时应当接管
            time.sleep(60)
        except _ClientGone:
            pass  # 网关按空闲超时断开 —— 正是本探针要验证的行为
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n); print(("PASS " if ok else "FAIL ") + n + ((" | " + str(d)) if d else ""))

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{"name": "noisy",
                          "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
                          "api_key_env": "K", "api_key_default": "d",
                          "models": {"noisy/m": {"upstream_model": "m"}}}]},
          open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"))
env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1",
           STREAM_IDLE_TIMEOUT_SECS=str(IDLE_SECS),
           REQUEST_TIMEOUT_SECS="40")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def api(path):
    try:
        # /admin/api/* 的 Bearer (2026-10-12 起); token 由隔离实例生成在 data/.admin_token
        try:
            tok = io.open(os.path.join(ISO, "data", ".admin_token"),
                          encoding="utf-8", errors="replace").read().strip()
        except Exception:
            tok = ""
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path))
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        return {"_err": str(e)}

try:
    for _ in range(60):
        if "_err" not in api("/admin/api/version"):
            break
        time.sleep(0.5)
    else:
        raise SystemExit("实例未就绪")

    body = json.dumps({"model": "noisy/m", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                                 headers={"content-type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            got = r.read().decode("utf-8", "replace")
    except Exception as e:
        got = str(e)
    elapsed = time.time() - t0
    print(f"  客户端耗时: {elapsed:.1f}s (mock 噪声 {NOISE_SECS}s 后静默, 空闲超时={IDLE_SECS}s)")
    print(f"  客户端收到 {len(got)} 字节")

    # 等记录落盘
    time.sleep(1.5)
    recs = api("/admin/api/logs?limit=5")
    rows = recs if isinstance(recs, list) else (recs.get("logs") or recs.get("records") or [])
    last = rows[0] if rows else {}
    print(f"  记录: status={last.get('status')} latency={last.get('latency_ms')}ms "
          f"first={last.get('first_token_ms')} out={last.get('completion_tokens')}")
    print(f"  error: {(last.get('error') or '')[:220]}")

    # ① 上游持续发噪声帧 (间隔 < 空闲超时) → 空闲超时不该被无限推迟;
    #    噪声停止后应在一个空闲超时内放弃. mock 总时长 8s + 空闲 3s ≈ 11s.
    ck("① 噪声结束后在一个空闲超时内放弃 (不空等到整体超时)",
       elapsed < 25, f"实际 {elapsed:.1f}s")

    # ② 关键回归: **保活帧不得重置空闲计时器** —— 耗时必须 ≈ 空闲超时, 而非噪声时长.
    ck("② 保活帧未重置空闲计时器 (耗时不被保活撑长)",
       elapsed < IDLE_SECS + 4, f"实际 {elapsed:.1f}s (空闲超时={IDLE_SECS}s)")

    # ③ 报错文案不得把网关自己补发的终止帧说成"客户端已收到"
    err = last.get("error") or ""
    ck("③ 文案不与事实矛盾 (不称 '客户端已收到 length 终止帧' 归属上游)",
       "客户端已收到 length 终止帧" not in err, err[:120])

    # ④ 诊断信息不该被网关自己注入的 keepalive 挤掉
    ck("④ 末帧诊断不含网关自己注入的 ': keep-alive'",
       "keep-alive" not in err, err[-90:])
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    srv.shutdown()

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL)); sys.exit(1)