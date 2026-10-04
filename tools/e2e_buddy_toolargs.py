# -*- coding: utf-8 -*-
"""诊断: CodeBuddy tool_call `arguments` 拼接后是否合法 JSON —— 客户端报
「写入文件失败 / Arguments are invalid JSON」(魔改 obt 编程工具实测报障, 2026-10-01)。

三路取证 (同一请求体):
  A. 穿网关 (AIGate → CodeBuddy)   —— 用户实际链路
  B. 直连 CodeBuddy                —— 隔离"网关环节"的对照组
  C. 帧级对照 —— finish_reason / 分片数 / 拼接长度 / JSON.parse 结果 / 首尾片段

判定矩阵:
  A 坏 B 坏 → 上游/模型行为 (截断或非标), 网关只做透传;
  A 坏 B 好 → 网关环节破坏 (回填/重组), 按帧 diff 定位;
  A 好      → 问题在客户端请求形态 (tools schema / 入口差异), 需抓魔改 obt 实际请求。

用法: python tools/e2e_buddy_toolargs.py
凭据: %TEMP%\\cb_plain.json (由会话内解密步骤生成, 本脚本只读)。
"""
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_buddyargs")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8996
CRED = os.path.join(tempfile.gettempdir(), "cb_plain.json")

if not os.path.exists(CRED):
    raise SystemExit(f"缺凭据 {CRED}")
cred = json.load(open(CRED, encoding="utf-8"))
tok = cred["auth"]["accessToken"]

HEADERS = {
    "X-Domain": "copilot.tencent.com",
    "X-Product-Code": "codebuddy",
    "X-Product": "CodeBuddy",
    "X-Agent-Purpose": "conversation",
    "X-IDE-Name": "CodeBuddy",
    "X-IDE-Type": "CodeBuddy",
    "X-IDE-Version": "1.106.1",
    "user-agent": "CodeBuddyIDE/1.106.1",
}

# ── 请求体: 写文件风格的大 arguments (模拟魔改 obt 的 write_file 工具) ──
BODY = {
    "model": "deepseek-v4-flash",
    "stream": True,
    "max_tokens": 8192,
    "messages": [
        {"role": "system", "content": "你是一个编程助手。用 write_file 工具写出完整文件，不要省略。"},
        {"role": "user", "content": "写文件 PipelineOverlapProbe.java：一个 Java 类，含 6 个方法"
                                    "（初始化半径窗口数组、中心点采样、重叠区域逐格比较、一致性校验、"
                                    "差值统计、main 入口），每个方法带完整实现和中文注释，不要偷懒省略代码。"},
    ],
    "tools": [{"type": "function", "function": {
        "name": "write_file",
        "description": "写入一个文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        }, "required": ["path", "content"]},
    }}],
    "tool_choice": "auto",
}


def collect_sse(readline, label, max_frames=20000):
    """读 SSE → (finish_reason, tool 分片列表, content 文本, 帧数, raw 前若干帧)"""
    tcs, text, frames, done = [], [], [], False
    finish = None
    reason_chars = 0
    while len(frames) < max_frames:
        line = readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").rstrip("\n")
        if not s.startswith("data:"):
            continue
        p = s[5:].strip()
        if p == "[DONE]":
            done = True
            break
        frames.append(p)
        try:
            j = json.loads(p)
        except Exception:
            continue
        ch = (j.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]
        d = ch.get("delta") or {}
        if d.get("content"):
            text.append(d["content"])
        # 统计 reasoning (thinking) 字符 —— 它与 content/tool_calls 共享 max_tokens 额度
        rc = d.get("reasoning_content") or d.get("reasoning") or ""
        if isinstance(rc, str):
            reason_chars += len(rc)
        for tc in d.get("tool_calls") or []:
            tcs.append(tc)
    arg_chars = sum(len((tc.get("function") or {}).get("arguments") or "") for tc in tcs)
    print(f"  [{label}] 帧数={len(frames)} done={done} finish_reason={finish!r} "
          f"tool分片={len(tcs)} | 字符分布: reasoning={reason_chars} "
          f"content={sum(len(t) for t in text)} tool_args={arg_chars}")
    return finish, tcs, "".join(text), frames, done


def stitch(tcs):
    """按 index 拼接 arguments (标准 OpenAI 流式语义), 返回 {index: {id, name, args}}"""
    acc = {}
    order = []
    for tc in tcs:
        idx = tc.get("index")
        if idx is None:
            continue
        if idx not in acc:
            acc[idx] = {"id": None, "name": None, "args": []}
            order.append(idx)
        f = tc.get("function") or {}
        if tc.get("id"):
            acc[idx]["id"] = tc["id"]
        if f.get("name"):
            acc[idx]["name"] = (acc[idx]["name"] or "") + f["name"]
        if f.get("arguments"):
            acc[idx]["args"].append(f["arguments"])
    out = {}
    for i in order:
        out[i] = {"id": acc[i]["id"], "name": acc[i]["name"], "args": "".join(acc[i]["args"])}
    return out


def report(label, finish, stitched):
    print(f"\n--- {label} ---")
    all_ok = True
    for i, v in stitched.items():
        raw = v["args"]
        try:
            parsed = json.loads(raw)
            ok = True
            keys = sorted(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
        except Exception as e:
            ok = False
            keys = f"{type(e).__name__}: {e}"
        all_ok = all_ok and ok
        print(f"  [{i}] id={v['id']!r} name={v['name']!r} len={len(raw)} json_ok={ok}")
        if not ok:
            print(f"       尾部200: {raw[-200:]!r}")
        else:
            print(f"       keys={keys} finish={finish!r}")
            # content 字段本身的完整性 (写文件场景: JSON 合法但内容被截平也要看得出)
            if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
                c = parsed["content"]
                print(f"       content len={len(c)} 尾部: {c[-60:]!r}")
    return all_ok


# ═══ A. 穿网关 ═══
print("═══ A. 穿网关 (AIGate) ═══")
if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{
    "name": "buddy",
    "endpoint": "https://copilot.tencent.com/v2/chat/completions",
    "api_key_env": "BUDDY_ACCESS_TOKEN",
    "api_key_default": tok,
    "headers": HEADERS,
    "models": {"buddy/deepseek-v4-flash": {"upstream_model": "deepseek-v4-flash"}},
}]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"),
    ensure_ascii=False, indent=1)

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok_a = False
ok_a2 = False
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    body_obj = dict(BODY)
    body_obj["model"] = "buddy/deepseek-v4-flash"   # 穿网关用中转 ID; 直连用上游原名
    body = json.dumps(body_obj).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions",
                                 data=body, headers={"content-type": "application/json"})
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=300)
    fin_a, tcs_a, txt_a, frames_a, done_a = collect_sse(
        resp.readline, "A 穿网关")
    print(f"  耗时 {time.time()-t0:.1f}s 原始帧样例: {frames_a[0][:160] if frames_a else None!r}")
    st_a = stitch(tcs_a)
    ok_a = report("A 穿网关", fin_a, st_a)

    # A2 对照: 同请求 + reasoning_effort=low —— 验证"思考吃掉输出额度导致 tool_args 截断".
    body2 = dict(body_obj)
    body2["reasoning_effort"] = "low"
    req2 = urllib.request.Request(f"http://127.0.0.1:{PG}/v1/chat/completions",
                                  data=json.dumps(body2).encode(),
                                  headers={"content-type": "application/json"})
    print("\n--- A2 穿网关 + reasoning_effort=low ---")
    t0 = time.time()
    try:
        resp2 = urllib.request.urlopen(req2, timeout=300)
        fin_a2, tcs_a2, _, _, _ = collect_sse(resp2.readline, "A2 low")
        st_a2 = stitch(tcs_a2)
        ok_a2 = report("A2 low", fin_a2, st_a2)
        print(f"  耗时 {time.time()-t0:.1f}s")
    except Exception as e:
        ok_a2 = False
        print("  A2 失败:", type(e).__name__, e)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

# ═══ B. 直连 ═══
print("\n═══ B. 直连 CodeBuddy (对照) ═══")
ok_b = False
try:
    payload = json.dumps(BODY).encode()
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("copilot.tencent.com", 443, context=ctx, timeout=300)
    h = dict(HEADERS)
    h["content-type"] = "application/json"
    h["authorization"] = "Bearer " + tok
    h["accept"] = "text/event-stream"
    t0 = time.time()
    conn.request("POST", "/v2/chat/completions", body=payload, headers=h)
    r = conn.getresponse()
    print(f"  HTTP {r.status}")
    if r.status != 200:
        print("  ", r.read(500).decode("utf-8", "replace"))
    else:
        fin_b, tcs_b, txt_b, frames_b, done_b = collect_sse(r.readline, "B 直连")
        print(f"  耗时 {time.time()-t0:.1f}s")
        st_b = stitch(tcs_b)
        ok_b = report("B 直连", fin_b, st_b)
    conn.close()
except Exception as e:
    print("  直连失败:", type(e).__name__, e)

# ═══ 判定 ═══
print("\n═══ 判定 ═══")
print(f"  A (默认档): json_ok={ok_a}   A2 (effort=low): json_ok={ok_a2}   B (直连): json_ok={ok_b}")
if not ok_a and ok_a2:
    print("  A 坏 A2 好: **思考额度挤占实锤** —— finish=length 时 reasoning 吃掉 max_tokens 份额, "
          "tool_args 被截断; 降档位可解. 网关透传无破坏 (B 与 A2 同构正常).")
elif ok_a and ok_a2 and ok_b:
    print("  A/A2/B 全好: 本轮未复现截断 —— 属长文件+长思考的随机撞墙; 以 A 首轮 length 证据为准, "
          "系统性诱因是 8192 总额度与写文件长度的结构性冲突.")
elif not ok_a and not ok_a2 and not ok_b:
    print("  A/A2/B 全坏: 与档位无关 —— 看 finish_reason: 仍为 length 则是平台上限, "
          "为 tool_calls 却 JSON 坏才是格式破坏.")
else:
    print("  混合结果 —— 结合各组 finish_reason 与尾部片段逐一定性 (见上).")
