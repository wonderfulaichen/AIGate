# -*- coding: utf-8 -*-
"""抓取真实的「剥离前 / 剥离后」对照素材 —— 供产品演示的原理页使用。

不要手写示意 JSON: 让网关真跑一遍, 把**客户端发来的原文**与**上游实收的正文**
都抓下来做逐字段对照。这样演示里的内容是可复现的实测结果, 而非编辑创作。

场景: 一个典型的多轮 agent 会话 —— 历史里累积了模型自己的推理链,
其中有的 assistant 消息带 tool_calls、有的不带 (两层剥离条件不同)。
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
TOOLS = os.path.dirname(os.path.abspath(__file__))                   # 口径快照 JSON 与脚本同目录
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_diff")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG, PM = 8993, 8994
OUT = os.path.join(TOOLS, "strip_diff.json")

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


srv = ThreadingHTTPServer(("127.0.0.1", PM), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({"providers": [
    {"name": "chatgw", "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % PM,
     "api_key_env": "K", "api_key_default": "d",
     "models": {"chatgw/m": {"upstream_model": "m"}}},
]}, ensure_ascii=False))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── 造一个真实形态的多轮会话: 推理链随轮次累积 ──
REASON_PLAIN = ("用户想让我重构这个函数。先看它做了什么: 它把输入按分隔符切开, 再逐个校验, "
                "最后拼回去。边界情况有几个要留意 —— 空串、单个分隔符、连续分隔符。"
                "我倾向于先抽出一个纯函数, 这样好测。") * 6
REASON_TC = ("我需要读一下配置文件确认端口。调用 read_file 工具, 路径取上一步得到的那个。"
             "如果返回里没有 ports 字段, 那就说明配置在别处, 我再搜一遍。") * 8
TOOL_OUT = "{\"ports\":[8787,8899],\"retries\":3,\"timeout_ms\":60000,\"note\":\"" + ("x" * 400) + "\"}"


def assistant_plain(text):
    return {"role": "assistant", "content": text,
            "reasoning_content": REASON_PLAIN, "reasoning": "短摘要"}


def assistant_tc(call_id):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "read_file", "arguments": "{\"path\":\"conf.json\"}"}}],
            "reasoning_content": REASON_TC}


BODY = {
    "model": "chatgw/m",
    "stream": False,
    "messages": [
        {"role": "system", "content": "你是一个严谨的编程助手。"},
        {"role": "user", "content": "帮我重构这个函数。"},
        assistant_plain("好的, 我先看一遍。"),
        {"role": "user", "content": "顺便确认一下服务端口。"},
        assistant_tc("call_1"),
        {"role": "tool", "tool_call_id": "call_1", "content": TOOL_OUT},
        assistant_plain("端口是 8787 与 8899, 重试 3 次。"),
        {"role": "user", "content": "那就按这个改吧。"},
    ],
}


def reason_chars(m):
    n = 0
    for k in ("reasoning_content", "reasoning", "reasoning_details"):
        if k in m:
            n += len(json.dumps(m[k], ensure_ascii=False))
    return n


def profile(body):
    msgs = body.get("messages") or []
    plain = sum(1 for m in msgs if m.get("role") == "assistant" and not m.get("tool_calls"))
    tc = sum(1 for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    with_reason = sum(1 for m in msgs if reason_chars(m) > 0)
    reason_bytes = sum(reason_chars(m) for m in msgs)
    return {
        "messages": len(msgs),
        "assistant_plain": plain,
        "assistant_toolcall": tc,
        "messages_with_reasoning": with_reason,
        "reasoning_bytes": reason_bytes,
        "body_bytes": len(json.dumps(body, ensure_ascii=False).encode()),
    }


try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PG, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)

    orig_bytes = json.dumps(BODY, ensure_ascii=False).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PG,
                                data=orig_bytes, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    time.sleep(0.6)

    got = json.loads(CAP.get('raw') or b'{}')
    before, after = profile(BODY), profile(got)

    # 逐消息对照 (供页面展示"哪几条被剥")
    detail = []
    for i, (a, b) in enumerate(zip(BODY["messages"], got.get("messages") or [])):
        role = a.get("role")
        if role != "assistant":
            continue
        detail.append({
            "index": i,
            "role": role,
            "has_tool_calls": bool(a.get("tool_calls")),
            "reasoning_before": reason_chars(a),
            "reasoning_after": reason_chars(b),
        })

    result = {"before": before, "after": after, "detail": detail,
              "saved_bytes": before["body_bytes"] - after["body_bytes"],
              "strip_saved_tokens_est": (before["body_bytes"] - after["body_bytes"]) // 4}

    # 上游实收 messages 的字段名单 (证明 reasoning 字段真没了)
    result["upstream_fields"] = [
        sorted(k for k in m.keys()) for m in (got.get("messages") or [])
    ]

    io.open(OUT, "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    recs = [json.loads(l) for l in io.open(os.path.join(ISO, "data", "logs.jsonl"), encoding="utf-8") if l.strip()]
    if recs:
        print("\n日志落库: strip_saved_tokens=%s strip_toolcall_saved_tokens=%s"
              % (recs[-1].get("strip_saved_tokens"), recs[-1].get("strip_toolcall_saved_tokens")))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    srv.shutdown()
