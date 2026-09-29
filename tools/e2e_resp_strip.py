# -*- coding: utf-8 -*-
"""端到端: Responses 直通路径的剥离开关 (默认关 / 开启后生效), 并核对省量落库."""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_rs")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")

CAP = {}


def run_case(port_gw, port_mock, strip_env, label):
    class Mock(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('content-length') or 0)
            CAP['body'] = self.rfile.read(n)
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            for e in [
                {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
                {"type": "response.output_text.delta", "output_index": 0, "delta": "ok"},
                {"type": "response.completed", "response": {"id": "r1", "status": "completed",
                 "usage": {"input_tokens": 10, "output_tokens": 2}, "output": []}},
            ]:
                self.wfile.write(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", port_mock), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    d = os.path.join(ISO, label)
    if os.path.isdir(d): shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(d, "AIGate.exe"))
    prov = {"providers": [{"name": "mock", "endpoint": "http://127.0.0.1:%d/v1/responses" % port_mock,
                           "api_key_env": "MK", "api_key_default": "d",
                           "models": {"mock/m1": {"upstream_model": "m1", "api_format": "responses"}}}]}
    io.open(os.path.join(d, "providers.json"), "w", encoding="utf-8").write(json.dumps(prov))

    env = dict(os.environ)
    env.update(PORT=str(port_gw), AIGATE_DATA_DIR=os.path.join(d, "data"), RUST_LOG="info", AIGATE_NO_PROXY="1")
    if strip_env is not None:
        env["STRIP_TOOLCALL_ON_RESPONSES"] = strip_env
    proc = subprocess.Popen([os.path.join(d, "AIGate.exe")], cwd=d, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % port_gw, timeout=2).read(); break
            except Exception: time.sleep(0.5)

        LONG = "这是一段很长的历史推理链内容，" * 200
        body = {"model": "mock/m1", "stream": True, "max_output_tokens": 512,
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q1"}]},
                    {"type": "reasoning", "id": "rs_1",
                     "summary": [{"type": "summary_text", "text": LONG}],
                     "content": [{"type": "reasoning_text", "text": LONG}]},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a1"}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q2"}]}]}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/responses" % port_gw,
                                    data=json.dumps(body, ensure_ascii=False).encode(),
                                    headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r: r.read()
        time.sleep(0.9)

        got = json.loads(CAP.get('body') or b'{}')
        n_reason = sum(1 for it in (got.get("input") or []) if it.get("type") == "reasoning")
        recs = [json.loads(l) for l in io.open(os.path.join(d, "data", "logs.jsonl"), encoding="utf-8") if l.strip()]
        st = int(recs[-1].get("strip_saved_tokens") or 0) if recs else -1
        print("  [%s] 上游收到的 reasoning item: %d | 日志 strip_saved_tokens: %d"
              % (label, n_reason, st))
        return n_reason, st
    finally:
        proc.terminate()
        try: proc.wait(timeout=8)
        except Exception: proc.kill()
        srv.shutdown()


print("=== Responses 直通剥离开关端到端 ===")
n_off, st_off = run_case(8909, 8910, None, "off")
n_on, st_on = run_case(8911, 8912, "1", "on")

print("\n=== 判定 ===")
ok = True
if n_off != 1 or st_off != 0:
    print("  [X] 默认关时不应剥离 (期望 reasoning=1, strip=0, 实得 %d / %d)" % (n_off, st_off)); ok = False
else:
    print("  OK  默认关: reasoning item 原样转发, strip=0 (保持字节保真)")
if n_on != 0 or st_on <= 0:
    print("  [X] 开启后应剥离 (期望 reasoning=0, strip>0, 实得 %d / %d)" % (n_on, st_on)); ok = False
else:
    print("  OK  开启后: reasoning item 被剥离, strip=%d tok 落库" % st_on)
print("\n=== 结果: %s ===" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
