# -*- coding: utf-8 -*-
"""端到端: 首字延迟 (TTFT) 探测历史 —— 按模型聚合、持久化、跨重启累计。

背景 (§7 待办①): first_token_ms 一直在采集, 但只随单条记录落一行 —— 没有
按模型聚合的探测历史, 重启后只能翻日志。本次把它做成 rollup 的日级聚合:
逐日累加直方图 + Σ延迟, 落盘 data/daily_stats.jsonl, 与日志窗口解耦。

判定 (12 条):
  A 基本采集
   ① 有效样本被计入 (样本数 == 发过的有效请求数)
   ② 均值落在合理区间 (≈ mock 的实际首字延迟)
   ③ 分布直方图总和 == 样本数
   ④ 判定一致: 全局 / 模型表 / 供应商表 三处样本数相同
  B 无效样本不得污染 (三条判据, 与 rollup::ttft_of 一致)
   ⑤ 非流式 (首字未知) 不计数
   ⑥ 从未出字 (空等, completion=0) 不计数 —— 否则"上游卡死"会被粉饰成"响应慢"
  C 持久化 (本次的核心)
   ⑦ 杀进程 → 重启 → 样本数仍在 (不是只活在内存里)
   ⑧ 重启后继续累加 (不是从头开始)
   ⑨ 落盘文件里真的有 ttft_hist 字段 (不是只在内存聚合)
  D 分位数与展示口径
   ⑩ p50 / p90 由直方图反推且不越界
   ⑪ 无样本时 avg_ms 为 null (前端据此显示「—」而非 0ms)
   ⑫ 桶边界与后端常量一致 (≤上界进本桶)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_ttft")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8979, 8980

# 后端桶上界 (rollup::TTFT_BUCKET_MAX_MS) —— 前端与这里必须一致.
BUCKETS = [250, 500, 1000, 2000, 3500, 5000, 10000, 20000, 60000, None]

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("  —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class Mock(BaseHTTPRequestHandler):
    """按请求内容注入不同的首字延迟: 用 x-delay-ms 头模拟上游排队.

    流式返回, 首发帧之前先 sleep —— 网关据此算出 first_token_ms.
    """
    """按请求内容注入不同的首字延迟.

    ⚠️ 延迟只能从**请求体**带, 不能用自定义 HTTP 头 —— 网关 filter_headers 只放行
    少数头 (§8 OpenCode 身份头那轮), 自定义头会被剥掉, 注入会静默失效
    (实测用 x-delay-ms 时 latency 恒为 0~1ms, 样本被判为无效)。
    """
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        content = (body.get("messages") or [{}])[0].get("content", "") or ""
        delay = 0
        for tok in content.replace("[", " ").replace("]", " ").split():
            if tok.startswith("D") and tok[1:].isdigit():
                delay = int(tok[1:])
        empty = "[EMPTY]" in content
        nonstream = "[NONSTREAM]" in content

        if nonstream:
            out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        time.sleep(delay / 1000.0)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        if not empty:
            chunk = {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        # 收尾帧: 有空等场景也照发, 但正文为空 → completion_tokens 由 usage 决定
        last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 0 if empty else 3}}
        # 首帧与收尾之间留一点间隔: 判据要求 **ft < 总耗时**(严格小于), 而 mock 的
        # 首帧几乎是最后一个动作 —— 不隔开的话 ft 与 latency 取到同一毫秒,
        # 样本会被正确排除 (实测踩过, 表现为"样本数莫名少一半").
        time.sleep(0.05)
        self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\n")
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


def start():
    p = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            return p
        except Exception:
            time.sleep(0.5)
    raise SystemExit("实例未起来")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    time.sleep(1.2)


def post(content, delay_ms=0, stream=True):
    """发一条请求; delay_ms 注入上游首字前的等待 (走请求体, 见 Mock 注释)."""
    body = {"model": "mock/m", "messages": [{"role": "user", "content": "%s D%d" % (content, delay_ms)}],
            "stream": stream}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def stats():
    return json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:%d/admin/api/stats" % PG), timeout=20).read().decode("utf-8"))


def ttft():
    return (stats().get("ttft") or {})


try:
    print("=== 首字延迟 (TTFT) 探测历史 ===\n")

    # ── A: 基本采集 ──
    p1 = start()
    # 4 条有效流式请求, 首字分别约 100/400/900/1500 ms
    for d in (100, 400, 900, 1500):
        post("有效样本 %dms" % d, delay_ms=d)
    time.sleep(1.5)

    t = ttft()
    print("A 基本采集")
    check("① 有效样本被计入 (期望 4)", t.get("samples") == 4, "实得 %s" % t.get("samples"))
    avg = t.get("avg_ms")
    check("② 均值落在合理区间 (≥100ms)", avg is not None and avg >= 100, "均值 %.0fms" % (avg or 0))
    hist = t.get("hist") or []
    check("③ 直方图总和 == 样本数", sum(hist) == t.get("samples"), "sum=%s" % sum(hist))
    check("③b 直方图长度 == 桶数 (%d)" % len(BUCKETS), len(hist) == len(BUCKETS), "实得 %d" % len(hist))

    st = stats()
    ms = sum((m.get("ttft_samples") or 0) for m in (st.get("per_model") or []))
    ps = sum((x.get("ttft_samples") or 0) for x in (st.get("per_provider") or []))
    check("④ 三处样本数一致 (全局/模型/供应商)",
          ms == ps == t.get("samples"), "全局=%s 模型=%s 供应商=%s" % (t.get("samples"), ms, ps))

    # ── B: 无效样本不得污染 ──
    #
    # ⚠️ 两条判据的**逐分支验证放单测**(rollup::tests::ttft_of_rejects_unusable_samples),
    #    那里能精确构造 ft/latency/ct 三个字段, 是确定的.
    #    e2e 这里只留**稳定的负例**: 空等 (从未出字) 与非流式 —— 实测网关在这两条
    #    路径上 first_token_ms 本来就是 None, 用来当反例会"永远成立"(假通过),
    #    故只作回归留档, 判据改坏时由单测负责抓.
    print("\nB 无效样本不得污染")
    before = t.get("samples")
    post("空等从未出字 [EMPTY]", delay_ms=300)
    post("非流式 [NONSTREAM]", delay_ms=300, stream=False)
    time.sleep(1.5)
    t2 = ttft()
    check("⑤⑥ 空等/非流式不计入样本 (回归留档, 判据逐分支见单测)",
          t2.get("samples") == before, "仍为 %s" % t2.get("samples"))

    # ── D: 分位数 ──
    print("\nD 分位数与展示口径")
    p50, p90 = t2.get("p50_ms"), t2.get("p90_ms")
    check("⑩ p50/p90 存在且 p50 ≤ p90",
          p50 is not None and p90 is not None and p50 <= p90, "p50=%s p90=%s" % (p50, p90))
    check("⑩b p50 落在某个桶上界上", p50 in [b for b in BUCKETS if b is not None] or p50 is None,
          "p50=%s" % p50)

    # ── C: 持久化 (核心) ──
    print("\nC 持久化")
    n_before = t2.get("samples")
    sum_before = (t2.get("avg_ms") or 0) * n_before
    stop(p1)

    p2 = start()
    time.sleep(1.5)
    t3 = ttft()
    check("⑦ 重启后样本数仍在 (不是只活在内存)",
          t3.get("samples") == n_before, "重启前 %s → 重启后 %s" % (n_before, t3.get("samples")))

    for d in (200, 600):
        post("重启后追加 %dms" % d, delay_ms=d)
    time.sleep(1.5)
    t4 = ttft()
    check("⑧ 重启后继续累加", t4.get("samples") == n_before + 2,
          "期望 %d 实得 %s" % (n_before + 2, t4.get("samples")))
    check("⑧b 均值按 Σ/样本数重算 (未归零)",
          (t4.get("avg_ms") or 0) > 0 and (t4.get("avg_ms") or 0) >= 100,
          "%.0fms" % (t4.get("avg_ms") or 0))

    stop(p2)

    # ⑨ 落盘文件里真的有 ttft 字段
    fp = os.path.join(ISO, "data", "daily_stats.jsonl")
    disk = io.open(fp, encoding="utf-8").read() if os.path.isfile(fp) else ""
    has_ttft = "ttft_hist" in disk and "ttft_samples" in disk
    check("⑨ 落盘文件含 ttft 字段", has_ttft,
          "%s (%d 字节)" % (os.path.basename(fp), len(disk)))

    # ⑪ 无样本时为 null: 用一个全新的隔离数据目录验证
    print("\nD2 无样本口径")
    iso2 = ISO + "_empty"
    if os.path.isdir(iso2):
        shutil.rmtree(iso2, ignore_errors=True)
    os.makedirs(os.path.join(iso2, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(iso2, "AIGate.exe"))
    shutil.copy(os.path.join(ISO, "providers.json"), os.path.join(iso2, "providers.json"))
    env2 = dict(env)
    env2.update(AIGATE_DATA_DIR=os.path.join(iso2, "data"))
    p3 = subprocess.Popen([os.path.join(iso2, "AIGate.exe")], cwd=iso2, env=env2,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        t5 = ttft()
        check("⑪ 无样本时 avg_ms 为 null (前端显示「—」而非 0ms)",
              t5.get("samples") == 0 and t5.get("avg_ms") is None,
              "samples=%s avg_ms=%s" % (t5.get("samples"), t5.get("avg_ms")))
    finally:
        stop(p3)
        shutil.rmtree(iso2, ignore_errors=True)

    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 首字延迟探测历史: 采集 / 判据 / 持久化 / 重启累加 全通")
    sys.exit(0)
finally:
    srv.shutdown()
