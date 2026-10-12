# -*- coding: utf-8 -*-
"""探针: gen_speed 的分子是否被 reasoning token 污染 (2026-10-11).

问题来源: 读 dsh-our-free-model 的 README 得到启发 —— 它为一条 ~40 tok/s 的车道
报出 2941 tok/s, 根因是「分子与分母度量的不是同一段时间」: 一次调用计费 422 个
输出 token, 其中 291 个是 reasoning, 它们在第一个**可见** token 之前就已生成完毕,
而窗口起点正是那个 token.

我们同款风险: `proxy.rs` 的 `tokens_ct` 直接取 `usage.completion_tokens`
(**不含** 剥离 reasoning 的逻辑), 而
    gen_speed = Σcompletion_tokens / Σ(总耗时 − 首字延迟)
分子含思考、分母是首字之后的流式窗口 —— 两者不是同一段时间.

本探针用 mock 上游**精确构造**这个场景 (不依赖真实上游的随机思考量):
  · 上报 usage.completion_tokens = 1000, 其中 reasoning_tokens = 950
  · 但只流出约 50 字正文, 首字延迟 200ms, 总耗时约 1.2s
判据 (4 条):
  ① 网关记录的 completion_tokens 仍是 1000 (计费口径不该被改)
  ② 若 gen_speed 被算成 ~800 tok/s (≈1000/1s) → **分子含思考, 虚高**
  ③ 真实可见输出速度只有 ~12 tok/s (50 字 / 4 / 1s)
  ④ 上游**自己报了** reasoning_tokens, 我们没读 → 应记录并可剔除

不连网、不消耗额度: 全程本地 mock.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from adminauth import headers as admin_auth  # /admin/api/* 的 Bearer (2026-10-12 起)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_genspeed")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8965, 8966

# mock 的构造参数: 思考占 95%, 正文只有几十字
CT_TOTAL = 1000
CT_REASONING = 950
# 正文文案 (与 Mock 共用; 必须是**不重复**的自然文本, 否则触发循环检测 → 无 usage 帧)
BODY_TEXT = "递归是指函数在其定义中直接或间接地调用自身的一种编程技巧与数学概念"
CONTENT_CHARS = len(BODY_TEXT)
FIRST_DELAY_MS = 200
STREAM_MS = 1000

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("  —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def frame(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        time.sleep(FIRST_DELAY_MS / 1000.0)
        # 先流思考 (按我们实测, buddy 确实把 reasoning 以文本流出来).
        # ⚠️ 不能发重复串! 循环检测会把它判成死循环并截断 —— 那样本轮就没有 usage 帧,
        #    记录里 completion_tokens=0, 探针会误报"计费口径被改" (实测踩过).
        think = "让我分析一下这个问题的关键点在哪里以及应当如何回答"
        frame({"choices": [{"index": 0, "delta": {"reasoning_content": think}, "finish_reason": None}]})
        # 再流正文, 摊在 STREAM_MS 里. 同理: 用不重复的自然文本.
        body = BODY_TEXT
        per = max(1, len(body) // 5)
        for i in range(0, len(body), per):
            frame({"choices": [{"index": 0, "delta": {"content": body[i:i + per]}, "finish_reason": None}]})
            time.sleep(STREAM_MS / 1000.0 / 6)
        # 收尾: usage 明确区分总量与思考量
        frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 11, "completion_tokens": CT_TOTAL,
                         "completion_tokens_details": {"reasoning_tokens": CT_REASONING}}})
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
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d", "models": {"mock/m": {"upstream_model": "m"}}},
]}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("实例未就绪"); sys.exit(1)

    print("=== gen_speed 分子是否含 reasoning ===\n")
    print("mock 构造: completion_tokens=%d (其中 reasoning=%d), 实际只流 %d 字正文"
          % (CT_TOTAL, CT_REASONING, CONTENT_CHARS))
    print("           首字 %.0fms, 正文摊在 %dms\n" % (FIRST_DELAY_MS, STREAM_MS))

    body = {"model": "mock/m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    time.sleep(1.5)

    logs = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/logs?limit=5" % PG,
                               headers=admin_auth(os.path.join(ISO, "data"))), timeout=20
    ).read().decode("utf-8"))
    rows = logs.get("logs") if isinstance(logs, dict) else logs
    row = (rows or [None])[0]
    if not row:
        print("没有记录"); sys.exit(1)
    print("记录: status=%s completion_tokens=%s first_token_ms=%s latency_ms=%s"
          % (row.get("status"), row.get("completion_tokens"), row.get("first_token_ms"), row.get("latency_ms")))
    print("      记录里的 reasoning 字段: %s"
          % json.dumps({k: v for k, v in row.items() if "reason" in k.lower()}, ensure_ascii=False))
    print("")

    st = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/stats" % PG,
                               headers=admin_auth(os.path.join(ISO, "data"))), timeout=20
    ).read().decode("utf-8"))
    pm = (st.get("per_model") or [{}])[0]
    gen = pm.get("gen_speed") or 0

    ft = row.get("first_token_ms") or 0
    lat = row.get("latency_ms") or 0
    win = max(0.001, (lat - ft) / 1000.0)
    real_speed = (CONTENT_CHARS / 4.0) / win
    print("  窗口 = latency(%sms) − first_token(%sms) = %.3fs" % (lat, ft, win))
    print("")

    ck("① 计费口径不被改: 记录的 completion_tokens 仍是 %d" % CT_TOTAL,
       row.get("completion_tokens") == CT_TOTAL, "实得 %s" % row.get("completion_tokens"))
    # ② reasoning token 必须被记下来 (gen_speed 靠它剔除).
    ck("② 记录里带 reasoning token 明细", row.get("reasoning_tokens") == CT_REASONING,
       "实得 %s (期望 %d)" % (row.get("reasoning_tokens"), CT_REASONING))
    # ③ 核心判据: gen_speed 不得按**含思考**的 completion_tokens 去算.
    #    含思考会报 ≈1000/0.84s ≈ 1193 tok/s (修复前的实测值);
    #    剔除后分子只剩正文那点 (50 字/4 ≈ 12 token) → 量级掉到几十.
    #    阈值取 200: 远高于"已剔除"的量级, 又远低于"未剔除"的 1193 —— 两边都能判.
    naive = CT_TOTAL / win
    ck("③ gen_speed 已剔除思考 (未剔除会是 %.0f tok/s)" % naive,
       gen < 200,
       "网关报 %.1f tok/s; 若含思考则约 %.0f → %s"
       % (gen, naive, "OK 已剔除" if gen < 200 else "FAIL 仍含思考"))
    # ④ 与「真实可见正文速度」同量级 —— 不能差一个数量级.
    #    注: 不要求完全相等: mock 的思考是**真的流出来的**(占窗口开头一段),
    #    而窗口(首字→结束)覆盖了那段, 故网关值会略高于"纯正文摊满窗口"的理想值.
    #    合理区间放宽到 10 倍以内, 超出说明分子仍被污染.
    ck("④ 与真实可见速度同量级 (%.1f tok/s)" % real_speed,
       gen <= real_speed * 10,
       "比值 %.1f 倍 (阈值 10)" % (gen / max(real_speed, 0.001)))
    ck("⑤ 确实比「含思考」的算法低一个量级",
       gen < naive / 5,
       "网关 %.1f vs 含思考算法 %.0f → 降了 %.0f 倍" % (gen, naive, naive / max(gen, 0.01)))

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        print("  → gen_speed 分子仍含思考 token, 数字虚高")
        sys.exit(1)
    print("  PASS —— gen_speed 已剔除思考 token")
    print("     计费口径 (completion_tokens) 未受影响, 仅「吐字速度」的分子还原成正文.")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    srv.shutdown()
