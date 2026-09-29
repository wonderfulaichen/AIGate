# -*- coding: utf-8 -*-
"""真实多轮会话的「请求体增长 vs 剥离后」实测曲线。

为什么不用示意: 原先那页柱状图是**按轮次建模**画出来的, 标注了"示意"但说服力弱。
这里真跑一遍: 造一个典型的 agent 多轮会话 (提问 → 推理+工具调用 → 工具结果 → …),
逐轮把**上游实际收到的请求体**抓下来, 并且在「开剥离 / 关剥离」两种设置下各跑一遍,
得到真实的两条曲线。

结论用途: 演示「重复上下文越滚越大 → 剥离把它压平」这件事有实据, 而非编辑创作。
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
TOOLS = os.path.dirname(os.path.abspath(__file__))                   # 口径快照 JSON 与脚本同目录
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_multiturn")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
OUT = os.path.join(TOOLS, "multiturn.json")

CAP = {}


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        CAP['raw'] = self.rfile.read(n)
        body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run_session(port_gw, port_mock, strip, label):
    """跑一遍多轮会话, 返回逐轮的请求体体积。strip=是否开启剥离。"""
    srv = ThreadingHTTPServer(("127.0.0.1", port_mock), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    d = os.path.join(ISO, label)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    shutil.copy(EXE, os.path.join(d, "AIGate.exe"))
    io.open(os.path.join(d, "providers.json"), "w", encoding="utf-8").write(json.dumps({
        "providers": [{"name": "gw", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % port_mock,
                       "api_key_env": "K", "api_key_default": "d",
                       "models": {"gw/m": {"upstream_model": "m"}}}]}, ensure_ascii=False))

    env = dict(os.environ)
    env.update(PORT=str(port_gw), AIGATE_DATA_DIR=os.path.join(d, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1",
               STRIP_HISTORY_REASONING="1" if strip else "0",
               STRIP_TOOLCALL_ON_CHAT="1" if strip else "0")
    proc = subprocess.Popen([os.path.join(d, "AIGate.exe")], cwd=d, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    curve = []
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % port_gw, timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)

        # 逐轮构造会话: 每轮 = 用户提问 + 助手(带推理链, 其中一轮带工具调用) + 工具结果
        REASON = ("我需要先确认这个函数在哪些地方被调用, 再决定怎么改。调用 search 工具, "
                  "关键词取函数名; 如果命中太多就加路径过滤。") * 5
        TOOLOUT = ('{"hits":[{"file":"src/a.rs","line":42},{"file":"src/b.rs","line":7}],'
                   '"note":"' + ("x" * 300) + '"}')
        msgs = [{"role": "system", "content": "你是一个严谨的编程助手。"}]

        for turn in range(1, 11):
            msgs.append({"role": "user", "content": "第 %d 轮: 继续处理这个重构任务。" % turn})
            use_tool = (turn % 3 == 0)
            a = {"role": "assistant", "content": None if use_tool else "好的, 这一步我这样处理。",
                 "reasoning_content": REASON}
            if use_tool:
                a["tool_calls"] = [{"id": "c%d" % turn, "type": "function",
                                    "function": {"name": "search", "arguments": "{\"q\":\"f\"}"}}]
            msgs.append(a)
            if use_tool:
                msgs.append({"role": "tool", "tool_call_id": "c%d" % turn, "content": TOOLOUT})

            payload = json.dumps({"model": "gw/m", "stream": False, "messages": msgs},
                                 ensure_ascii=False).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port_gw,
                                        data=payload, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            time.sleep(0.25)

            got = json.loads(CAP.get('raw') or b'{}')
            sent_bytes = len(payload)
            recv_bytes = len(json.dumps(got, ensure_ascii=False).encode())
            n_reason = sum(1 for m in (got.get("messages") or [])
                           if any(k in m for k in ("reasoning_content", "reasoning", "reasoning_details")))
            curve.append({"turn": turn, "sent_bytes": sent_bytes, "recv_bytes": recv_bytes,
                          "messages": len(msgs), "with_reasoning": n_reason})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        srv.shutdown()
    return curve


print("=== 真实多轮会话请求体增长实测 ===")
with_strip = run_session(8995, 8996, True, "strip_on")
without = run_session(8997, 8998, False, "strip_off")

print("\n轮次 |  不剥离(上游实收) |  剥离后(上游实收) |  差额")
for a, b in zip(without, with_strip):
    print("  %2d |  %12s B  |  %12s B  |  −%s B"
          % (a["turn"], format(a["recv_bytes"], ","), format(b["recv_bytes"], ","),
             format(a["recv_bytes"] - b["recv_bytes"], ",")))

res = {
    "without": without,
    "with_strip": with_strip,
    "final": {
        "turn": without[-1]["turn"],
        "sent": without[-1]["sent_bytes"],
        "recv_no_strip": without[-1]["recv_bytes"],
        "recv_strip": with_strip[-1]["recv_bytes"],
        "saved": without[-1]["recv_bytes"] - with_strip[-1]["recv_bytes"],
    },
}
io.open(OUT, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=2))
print("\n已写出 %s" % OUT)
print("末轮: 客户端发出 %s B → 不剥离上游收 %s B / 剥离后 %s B (省 %s B)"
      % (format(res['final']['sent'], ","), format(res['final']['recv_no_strip'], ","),
         format(res['final']['recv_strip'], ","), format(res['final']['saved'], ",")))
