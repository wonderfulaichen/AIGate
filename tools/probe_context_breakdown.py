# -*- coding: utf-8 -*-
"""上下文构成分析的端到端验证.

背景 (2026-10-24): 面板能说"省了多少", 但回答不了"输入大在哪里" ——
是工具 schema 太大? 工具结果反复携带? 还是历史助手消息堆着?
(借鉴 WaLiAPI/magpie 的分类可观测性。)

本探针用一条**结构已知**的请求, 验证分类真的落到日志与聚合里:
  ① 请求进网关后, logs.jsonl 里该条记录**带 context 字段**;
  ② 六类齐备且顺序固定;
  ③ 分类正确: 工具 schema → tools; `<system-reminder>` → injected(不是 chat);
     工具结果 → tool_result; assistant → assistant;
  ④ 聚合接口 (`/admin/api/stats`) 的 `audit.context_kinds` 累加到该类;
  ⑤ **口径**: 图片 base64 不计入文本估算 (否则会高估几十倍)。

⚠️ 关键: 本模块的核心价值是「注入内容不混进用户消息」——
故 ③ 里注入块**必须**落在 injected, chat 只留真实发言。

红线: 隔离实例 + 随机端口 + mock 上游; 绝不碰用户 8787。
"""
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
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        b = json.dumps({
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5000, "completion_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def post(base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1
    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_ctx")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)
    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mockprov/m1": {"upstream_model": "m1"}},
    }]}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = data_dir
    env["AIGATE_OPEN_ADMIN"] = "1"
    proc = subprocess.Popen([iso_exe], env=env, cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  隔离实例未就绪")
            return 1

        # 构造一条**结构已知**的请求: 每类都有明显特征
        body = {
            "model": "mockprov/m1",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the contents of a file from disk and return them as text",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"}},
                                   "required": ["path"]},
                },
            }],
            "messages": [
                {"role": "system", "content": "You are a careful coding agent."},
                # 注入内容: 必须落到 injected, 不能算 chat
                {"role": "user", "content": "<system-reminder>\nAgent framework context here.\n</system-reminder>"},
                {"role": "user", "content": "请解释这段代码"},
                {"role": "assistant", "content": "这段代码做了三件事: 读取、解析、写回。"},
                {"role": "tool", "content": "file contents: alpha beta gamma delta"},
            ],
            "stream": False,
        }
        st, _ = post(base, "/v1/chat/completions", body)
        check(st == 200, "请求成功返回", f"{st}")
        time.sleep(1.5)

        # ① 读磁盘日志, 确认 context 字段存在
        lp = os.path.join(data_dir, "logs.jsonl")
        rows = []
        with open(lp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
        logged = next((r for r in rows if r.get("context")), None)
        check(logged is not None, "① 日志里带 context 字段", f"共 {len(rows)} 条")
        if not logged:
            return 1

        ctx = logged["context"]
        kinds = ctx.get("kinds") or []
        kv = {k: v for k, v in kinds}
        print(f"  构成分类 ({len(kinds)} 类): " +
              ", ".join(f"{k}={v['tokens']}" for k, v in kinds))

        # ② 六类齐备且顺序固定
        expect_order = ["system", "tools", "injected", "assistant", "tool_result", "chat"]
        check([k for k, _ in kinds] == expect_order,
              "② 六类齐备且顺序固定", f"{[k for k, _ in kinds]}")

        # ③ 分类正确
        check(kv.get("tools", {}).get("tokens", 0) > 0, "③ 工具 schema → tools")
        check(kv.get("system", {}).get("tokens", 0) > 0, "③ system 消息 → system")
        check(kv.get("assistant", {}).get("tokens", 0) > 0, "③ assistant → assistant")
        check(kv.get("tool_result", {}).get("tokens", 0) > 0, "③ tool 消息 → tool_result")
        # **核心**: 注入块落在 injected, chat 只有真实发言
        check(kv.get("injected", {}).get("tokens", 0) > 0,
              "③ **核心**: <system-reminder> → injected (不混进 chat)")
        check(kv.get("injected", {}).get("items", 0) == 1,
              "③ 注入块计 1 条", f"{kv.get('injected')}")
        check(kv.get("chat", {}).get("items", 0) == 1,
              "③ chat 只有 1 条真实发言 (注入的不算)", f"{kv.get('chat')}")
        check(not ctx.get("has_images"), "③ 本条无图片")

        # ④ 聚合接口累加
        stats = get(base, "/admin/api/stats")
        audit = stats.get("audit") or {}
        agg = dict(audit.get("context_kinds") or [])
        print(f"  聚合: " + ", ".join(f"{k}={v}" for k, v in agg.items()))
        check(int(agg.get("tools", 0)) > 0, "④ 聚合里 tools 已累加", f"{agg}")
        check(int(agg.get("injected", 0)) > 0, "④ 聚合里 injected 已累加", f"{agg}")
        check(int(audit.get("sampled_context_requests", 0)) >= 1,
              "④ 采样计数 ≥1", f"{audit.get('sampled_context_requests')}")

        # ⑤ 图片不计入文本估算
        big = "A" * 200_000
        body2 = {
            "model": "mockprov/m1",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + big}},
            ]}],
            "stream": False,
        }
        post(base, "/v1/chat/completions", body2)
        time.sleep(1.5)
        rows2 = []
        with open(lp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows2.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
        withimg = next((r for r in rows2 if (r.get("context") or {}).get("has_images")), None)
        check(withimg is not None, "⑤ 含图片的请求被标记 has_images")
        if withimg:
            total = withimg["context"].get("total_estimated", 0)
            print(f"  含图请求的文本估算合计: {total}")
            check(total < 2000,
                  "⑤ **关键**: 200KB base64 未被当文本计入 (否则会高估几十倍)",
                  f"实得 {total}")
    finally:
        srv.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项判据未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
