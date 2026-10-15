# -*- coding: utf-8 -*-
"""探针: 网关侧阶段计时 + 链路拓扑计数（2026-10-15, 对标 Astra-Gate）。

判据（mock 上游 + 隔离实例; 判据 = 记录里的 stages 网关侧字段 + /admin/api/stats）：
  ① OpenAI 直通: route/guard/overhead/ttfb 均已采集, 且**结构次序**成立
     (route ≤ overhead ≤ latency, ttfb ≤ latency —— 三者都从"进门"起算, 次序是硬约束)
  ② 直通请求的 convert_ms 必须是 **null**（未发生协议转换）, 不是 0
  ③ 走 /v1/chat/completions 但上游是 Anthropic 协议时: convert_ms **有值**（真发生了转换）
  ④ 原生直通 /v1/messages: 网关侧照常采集, 而**模型侧四段全 null** —— 直通不解析字节,
     如实留白（给 0 就是假数）
  ⑤ 护栏开启 + 大请求体: guard_ms > 0（确实扫了, 且密钥吊在正文末尾 —— 命中项放在开头会被
     最左匹配短路, 那样测出来恒为 0ms, 断言就是假的）, 且仍 ≤ overhead_ms
  ⑥ 上游 429: per_provider.rate_limited 计数 +1（拓扑节点用）; 未限流的供应商保持 0
  ⑦ 窗口级 gateway_stages: 各段 samples 只数**采集到**的请求; convert 只数真转换的
     （样本数 == 1, 直通那几条不计入 —— 否则转换均值被 0 稀释）
  ⑧ p50 ≤ p95, 且无样本的段为 null（未采集 ≠ 0）
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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8892
PG = 8893
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_gwstages")

FAIL = []
NCHK = [0]


def chk(n, cond, extra=""):
    NCHK[0] += 1
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


def scene_of(body):
    for m in (body.get("messages") or []):
        c = m.get("content")
        if isinstance(c, str) and c.startswith("SCN:"):
            return c[4:].strip()
        # Anthropic 允许 content 为块数组
        if isinstance(c, list):
            for b in c:
                t = b.get("text") if isinstance(b, dict) else None
                if isinstance(t, str) and t.startswith("SCN:"):
                    return t[4:].strip()
    return ""


def openai_sse(delta, finish=None):
    return ("data: " + json.dumps({
        "id": "c-" + uuid.uuid4().hex[:6], "object": "chat.completion.chunk", "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n").encode("utf-8")


def anthropic_sse(ev, data):
    return ("event: %s\ndata: %s\n\n" % (ev, json.dumps(data))).encode("utf-8")


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            scene = scene_of(body)
            path = self.path.split("?")[0]

            # 限流场景: 无论哪条协议都回 429（判据只看记录里的 status / 计数）
            if scene == "429":
                out = json.dumps({"error": {"message": "rate limited", "type": "rate_limit_error"}}).encode()
                self.send_response(429)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            stream = bool(body.get("stream"))
            anthropic = path.endswith("/v1/messages")

            if anthropic:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def wa(b):
                    self.wfile.write(b)
                    self.wfile.flush()

                wa(anthropic_sse("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "role": "assistant", "content": [],
                    "usage": {"input_tokens": 5, "output_tokens": 0}}}))
                wa(anthropic_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                         "delta": {"type": "text_delta", "text": "ok"}}))
                wa(anthropic_sse("message_delta", {"type": "message_delta",
                                                   "delta": {"stop_reason": "end_turn"},
                                                   "usage": {"output_tokens": 3}}))
                wa(anthropic_sse("message_stop", {"type": "message_stop"}))
                return

            if not stream:
                out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                               "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def w(b):
                self.wfile.write(b)
                self.wfile.flush()

            w(openai_sse({"content": "ok"}))
            time.sleep(0.05)
            w(openai_sse({}, "stop"))
            w(b"data: [DONE]\n\n")

    return H


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
    if not os.path.exists(EXE):
        print("缺少构建产物: %s (先 cargo build --release)" % EXE)
        return 2
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    os.makedirs(os.path.join(ISO, "config"), exist_ok=True)
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    # 两个供应商: 一个 OpenAI 协议 (chat 直通), 一个 Anthropic 协议 (chat 需转换 + /v1/messages 原生直通)
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": [
            # ⚠️ `api_key_env` 是**必填**字段 (String, 不可省): 漏了会让整份 providers.json
            #    解析失败, 网关静默回落到备用路径的真实配置 —— 探针会看到"未知模型 404"
            #    而不是配置错误, 极难定位 (本轮真踩过)。
            {"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
             "api_key_env": "MOCK_KEY", "api_key_default": "d",
             "models": {"mock/m": {"upstream_model": "m", "api_format": "openai"}}},
            {"name": "mockant", "endpoint": "http://127.0.0.1:%d/v1/messages" % MP,
             "api_key_env": "MOCK_KEY", "api_key_default": "d",
             "models": {"mockant/ma": {"upstream_model": "ma", "api_format": "anthropic"}}},
            # 第三条**单独的中转 ID**: 原生直通与"chat 转换"这两条记录的**上游端点**是
            # 同一个 (/v1/messages), 靠端点区分不开 —— 用独立 model 名区分才是确定性的。
            {"name": "mocknative", "endpoint": "http://127.0.0.1:%d/v1/messages" % MP,
             "api_key_env": "MOCK_KEY", "api_key_default": "d",
             "models": {"mocknative/mn": {"upstream_model": "mn", "api_format": "anthropic"}}},
        ]}, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1", MOCK_KEY="d")
    srv = ThreadingHTTPServer(("127.0.0.1", MP), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as admin_auth, get_json, post_json
        AH = admin_auth(os.path.join(ISO, "data"))
        base = "http://127.0.0.1:%d" % PG

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception as e:
                return 0, "ERR:" + type(e).__name__

        def records():
            p = os.path.join(ISO, "data", "logs.jsonl")
            try:
                rows = [x for x in io.open(p, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
            except Exception:
                rows = []
            out = []
            for r in rows:
                try:
                    out.append(json.loads(r))
                except Exception:
                    pass
            return out

        def find(model, after=0, want_last=True):
            """按 model 取记录; 等到**总条数超过 after** 再取。

            ⚠️ 只等"该 model 有记录"是不够的 (2026-10-15 真踩): 日志写入是异步的, 上一条
            同 model 的记录早已落地, 于是 find() 立刻返回**旧记录**, 断言读的是上一次请求的
            阶段字段 —— 表现为"guard_ms 恒为 0"这种假失败 (窗口级聚合里同一次请求明明是 43ms),
            排查方向会被彻底带偏。故必须按条数增量来等下一条。"""
            for _ in range(40):
                recs = records()
                hit = [r for r in recs if r.get("model") == model]
                if len(recs) > after and hit:
                    return hit[-1] if want_last else hit
                time.sleep(0.2)
            return {} if want_last else []

        def stages(rec):
            return rec.get("stages") or {}

        # ① ② OpenAI 直通
        n0 = len(records())
        post("/v1/chat/completions", {"model": "mock/m", "stream": True,
                                      "messages": [{"role": "user", "content": "SCN:plain"}]})
        r1 = find("mock/m", n0)
        s1 = stages(r1)
        chk("direct-route-present", s1.get("route_ms") is not None, s1)
        chk("direct-guard-present", s1.get("guard_ms") is not None, s1)
        chk("direct-overhead-present", s1.get("overhead_ms") is not None, s1)
        chk("direct-ttfb-present", s1.get("upstream_ttfb_ms") is not None, s1)
        chk("direct-convert-is-null", s1.get("convert_ms") is None,
            "上游同为 OpenAI 协议 → 未发生转换, convert_ms 必须是 null 而不是 0: %s" % s1.get("convert_ms"))
        lat1 = r1.get("latency_ms") or 0
        chk("direct-route-le-overhead",
            (s1.get("route_ms") or 0) <= (s1.get("overhead_ms") or 0),
            "route=%s overhead=%s (两者都从进门起算, 路由必须先于'首次发往上游')"
            % (s1.get("route_ms"), s1.get("overhead_ms")))
        chk("direct-guard-le-overhead",
            (s1.get("guard_ms") or 0) <= (s1.get("overhead_ms") or 0),
            "guard=%s overhead=%s" % (s1.get("guard_ms"), s1.get("overhead_ms")))
        chk("direct-overhead-le-latency", (s1.get("overhead_ms") or 0) <= lat1,
            "overhead=%s latency=%s" % (s1.get("overhead_ms"), lat1))
        chk("direct-ttfb-le-latency", (s1.get("upstream_ttfb_ms") or 0) <= lat1,
            "ttfb=%s latency=%s" % (s1.get("upstream_ttfb_ms"), lat1))

        # ③ chat 入口 + Anthropic 上游 → 真转换
        n0 = len(records())
        post("/v1/chat/completions", {"model": "mockant/ma", "stream": True,
                                      "messages": [{"role": "user", "content": "SCN:plain"}]})
        r2 = find("mockant/ma", n0)
        s2 = stages(r2)
        chk("convert-present-when-converting", s2.get("convert_ms") is not None,
            "OpenAI 入口 + Anthropic 上游必然转换 → convert_ms 应有值: %s" % s2.get("convert_ms"))
        chk("convert-route-overhead-present",
            all(s2.get(k) is not None for k in ("route_ms", "overhead_ms", "upstream_ttfb_ms")), s2)

        # ④ 原生直通 /v1/messages: 网关侧有, 模型侧必须留白
        n0 = len(records())
        code, _ = post("/v1/messages", {"model": "mocknative/mn", "max_tokens": 16, "stream": True,
                                        "messages": [{"role": "user", "content": "SCN:plain"}]})
        chk("native-http-200", code == 200, "status=%s" % code)
        r3 = find("mocknative/mn", n0)
        s3 = stages(r3)
        chk("native-gateway-stages-present",
            all(s3.get(k) is not None for k in ("route_ms", "guard_ms", "overhead_ms", "upstream_ttfb_ms")),
            "原生直通也要有网关侧阶段: %s" % s3)
        chk("native-convert-is-null", s3.get("convert_ms") is None,
            "字节原样转发 = 未转换: %s" % s3.get("convert_ms"))
        chk("native-model-stages-null",
            all(s3.get(k) is None for k in ("waiting_first_ms", "reasoning_ms", "output_ms", "tool_call_ms")),
            "直通不解析模型阶段 → 四段必须留白(显示 —), 不是 0: %s" % s3)

        # ⑤ 护栏开启 + 大请求体 → guard_ms 有实际耗时
        # ⚠️ 两个必须同时满足, 否则断言是**假**的 (2026-10-15 各踩一次):
        #   (a) 体量要足够大。Rust regex 吞吐很高, 200KB / 1.4MB 的扫描常落在 0~1ms,
        #       整数毫秒截断后读成 0 —— 计时本身没坏, 是判据太松。
        #   (b) 更隐蔽的一条: 命中项**不能放在字符串开头**。`is_match` 是最左匹配,
        #       首个字节就命中就立刻返回, 后面 10MB 压根不扫 —— 实测 8.5MB 载荷照样 0ms。
        #       故把非命中正文放前面, 密钥吊在**末尾**: 扫描必须真的走完全程才有 >0ms。
        big = ("lorem ipsum dolor sit amet " * 400000) + " sk-abcdefghijklmnopqrstuvwxyz012345"
        with io.open(os.path.join(ISO, "config", "guard.json"), "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "dry_run": True, "rules": {}}, f)
        post_json(base + "/admin/api/providers/reload", {}, AH)
        time.sleep(0.3)
        n0 = len(records())
        post("/v1/chat/completions", {"model": "mock/m", "stream": True,
                                      "messages": [{"role": "user", "content": big}]})
        r4 = find("mock/m", n0)
        s4 = stages(r4)
        # ⑤a 扫描确实跑了 (不依赖计时的不变量): dry_run 会写 guard_hit 台账
        ev = get_json(base + "/admin/api/events", AH)
        chk("guard-on-guard-hit-event", any(e.get("kind") == "guard_hit" for e in (ev.get("events") or [])),
            "护栏开启 + 请求体含密钥 → 应有 guard_hit 台账事件")
        # ⑤b 计时确实接上了: 密钥吊在 10MB 正文末尾, 扫描必须走完全程
        chk("guard-on-guard-ms-positive", (s4.get("guard_ms") or 0) > 0,
            "护栏开启且正文 ~10MB 后才命中: guard_ms=%s (应 >0 —— 否则计时没接上)" % s4.get("guard_ms"))
        chk("guard-on-guard-le-overhead",
            (s4.get("guard_ms") or 0) <= (s4.get("overhead_ms") or 0),
            "guard=%s overhead=%s (护栏扫描是 overhead 的子集)" % (s4.get("guard_ms"), s4.get("overhead_ms")))

        # ⑥ 上游 429 → 供应商限流计数
        code, _ = post("/v1/chat/completions", {"model": "mock/m", "stream": True,
                                                "messages": [{"role": "user", "content": "SCN:429"}]})
        chk("upstream-429-passthrough", code == 429, "客户端应看到 429, 实得 %s" % code)
        time.sleep(0.6)

        # ⑦ ⑧ 窗口级汇总
        st = get_json(base + "/admin/api/stats", AH)
        g = st.get("gateway_stages") or {}
        overhead = g.get("overhead") or {}
        route = g.get("route") or {}
        conv = g.get("convert") or {}
        ttfb = g.get("upstream_ttfb") or {}
        chk("summary-overhead-samples", (overhead.get("samples") or 0) >= 4,
            "gateway_stages.overhead=%s (直通+转换+原生+护栏大请求 = 4 条采集到)" % overhead)
        chk("summary-route-samples-match", route.get("samples") == overhead.get("samples"),
            "route=%s overhead=%s (同一次请求两段都采集, 样本数应一致)"
            % (route.get("samples"), overhead.get("samples")))
        chk("summary-convert-only-converted", conv.get("samples") == 1,
            "convert.samples=%s —— 只有 chat+Anthropic 那一条真转换; 直通不进转换样本(否则被 0 稀释)"
            % conv.get("samples"))
        chk("summary-ttfb-samples", (ttfb.get("samples") or 0) >= 4, "ttfb=%s" % ttfb)
        chk("summary-p50-le-p95",
            all((g.get(k) or {}).get("p50_ms") is not None
                and (g.get(k) or {}).get("p50_ms") <= (g.get(k) or {}).get("p95_ms")
                for k in ("route", "guard", "upstream_ttfb", "overhead")),
            "各段 p50 ≤ p95: %s" % {k: (g.get(k) or {}).get("p95_ms") for k in
                                    ("route", "guard", "upstream_ttfb", "overhead")})
        chk("summary-avgs-present",
            all((g.get(k) or {}).get("avg_ms") is not None for k in ("route", "guard", "upstream_ttfb", "overhead")),
            "有样本的段必须有均值: %s" % g)

        # ⑥b 提供商限流计数 (拓扑节点数据源)
        pp = {p.get("provider"): p for p in (st.get("per_provider") or [])}
        chk("provider-rate-limited-counted", (pp.get("mock", {}).get("rate_limited") or 0) >= 1,
            "mock 收到 1 次 429 → rate_limited 应 ≥1: %s" % pp.get("mock"))
        chk("provider-rate-limited-untouched", (pp.get("mockant", {}).get("rate_limited") or 0) == 0,
            "mockant 无 429 → 必须为 0: %s" % pp.get("mockant"))

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
