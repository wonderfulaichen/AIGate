# -*- coding: utf-8 -*-
"""探针: 上游中途断流 —— 结束方式要能区分「上游断流」与「空闲超时」（2026-10-13 用户报障）。

用户报障截图: 502 / 首字 15.7s / 延迟 39.6s / 结束方式「空闲超时 / 上游连接错误」/
错误详情 `UnexpectedEof: unexpected EOF during chunk size line`。
根因判断: 空闲超时默认 120s, 而这条 39.6s 就结束 → **不可能是空闲超时**, 是上游断流。
但两者原先共用一个 error_closed 标志, end_reason 只能报含糊的 `idle`。

判据 (mock 上游按场景构造, 判据 = 记录里的 end_reason + 事件台账):
  ① 上游**中途断连**(分块传输未写完就 close) → end_reason=upstream_cut, 且记 502
  ② 事件台账出现 upstream_cut (供应商不稳要主动告诉用户; 红点亮、不弹气球由单测锁)
  ③ 上游**静默不发数据**超过空闲阈值 → end_reason=idle, 且**不**记 upstream_cut 事件
     (空闲超时可能是本机/上游慢, 不是供应商故障 —— 混报会让预警变噪音)
  ④ 对照组: 正常完整流 → end_reason=stop, 不产生任何 upstream_cut 事件
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
MP = 8878
PG = 8879
ISO = os.path.join(os.environ.get("TEMP", "."), "iso_probe_cut")
IDLE_SECS = 3   # 探针里把空闲超时压到 3s, 场景③ 不用等 120s

FAIL = []
NCHK = [0]
SCEN = {"mode": "cut"}


def chk(n, cond, extra=""):
    NCHK[0] += 1
    print("%s %s %s" % ("PASS" if cond else "FAIL", n, extra))
    if not cond:
        FAIL.append(n)


def make_handler(scen):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            body = {}
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                pass
            mode = ""
            for m in (body.get("messages") or []):
                c = m.get("content")
                if isinstance(c, str) and c.startswith("SCN:"):
                    mode = c[4:].strip()
            m = mode or scen["mode"]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            if m == "cut":
                # 先发一帧正常数据, 再**强行 RST 掉连接** —— 复刻用户实测的
                # `UnexpectedEof: unexpected EOF during chunk size line`。
                #
                # ⚠️ 关键: 必须是 RST 而不是优雅 FIN。优雅关闭时 reqwest 把流当作
                # "正常结束"（poll 返回 None），网关报的是 `eof` 而非断流错误 ——
                # 我第一版就是这样写的，探针如实报回 eof（说明探针没构造出真实故障）。
                # 用 SO_LINGER=0 + close 强制发 RST。
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n')
                self.wfile.flush()
                try:
                    import struct as _s
                    # l_onoff=1, l_linger=0 → close 时立即发 RST, 丢弃未发数据
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               _s.pack("ii", 1, 0))
                except Exception:
                    pass
                try:
                    self.connection.close()
                except Exception:
                    pass
                self.close_connection = True
            elif m == "silent":
                # 发一帧后**静默不发任何数据**(也不关连接) → 触发空闲超时
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n')
                self.wfile.flush()
                time.sleep(IDLE_SECS + 6)
            else:
                # 正常完整流
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

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
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"))
    shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
    prov = {"providers": [{
        "name": "mock",
        "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % MP,
        "api_key_env": "K", "api_key_default": "d",
        "models": {"mock/m": {"upstream_model": "m"}},
    }]}
    with io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f)

    env = dict(os.environ, PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1",
               STREAM_IDLE_TIMEOUT_SECS=str(IDLE_SECS),
               # 熔断阈值抬高, 避免探针的失败把供应商打进 Open 影响后续场景
               BREAKER_FAILURE_THRESHOLD="99")
    srv = ThreadingHTTPServer(("127.0.0.1", MP), make_handler(SCEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PG):
            print("实例未起来")
            return 2
        from adminauth import headers as admin_auth
        AH = admin_auth(os.path.join(ISO, "data"))

        def chat(tag):
            body = json.dumps({"model": "mock/m", "stream": True,
                               "messages": [{"role": "user", "content": "SCN:" + tag}]}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/v1/chat/completions" % PG, data=body,
                        headers={"content-type": "application/json"}, method="POST"), timeout=60) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.read().decode("utf-8", "replace")
            except Exception as e:
                return "ERR:" + type(e).__name__

        def last_record():
            p = os.path.join(ISO, "data", "logs.jsonl")
            for _ in range(30):
                try:
                    rows = [x for x in io.open(p, encoding="utf-8", errors="replace").read().split("\n") if x.strip()]
                except Exception:
                    rows = []
                if rows:
                    try:
                        return json.loads(rows[-1])
                    except Exception:
                        pass
                time.sleep(0.2)
            return {}

        def events():
            r = urllib.request.Request("http://127.0.0.1:%d/admin/api/events?limit=100" % PG, headers=AH)
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace")).get("events") or []

        # ① 上游中途断连
        chat("cut")
        time.sleep(1.5)
        rec = last_record()
        chk("cut-end-reason-is-upstream_cut", rec.get("end_reason") == "upstream_cut",
            "end_reason=%s" % rec.get("end_reason"))
        chk("cut-status-502", rec.get("status") == 502, "status=%s" % rec.get("status"))

        # ② 事件台账记了 upstream_cut (供应商不稳要主动告知)
        evs = events()
        kinds = [e.get("kind") for e in evs]
        chk("cut-recorded-in-ledger", "upstream_cut" in kinds, kinds[:6])

        # ③ 静默不发数据 → 空闲超时 (必须与上游断流区分, 且不记 upstream_cut)
        n_cut_before = kinds.count("upstream_cut")
        chat("silent")
        time.sleep(1.5)
        rec2 = last_record()
        chk("idle-end-reason-is-idle", rec2.get("end_reason") == "idle",
            "end_reason=%s" % rec2.get("end_reason"))
        kinds2 = [e.get("kind") for e in events()]
        chk("idle-does-not-record-upstream_cut",
            kinds2.count("upstream_cut") == n_cut_before,
            "upstream_cut 次数 %d -> %d（空闲超时不该记断流）" % (n_cut_before, kinds2.count("upstream_cut")))

        # ④ 对照组: 正常完整流不产生任何 upstream_cut
        n_before = kinds2.count("upstream_cut")
        out = chat("ok")
        time.sleep(1.2)
        rec3 = last_record()
        chk("ok-end-reason-is-stop", rec3.get("end_reason") == "stop",
            "end_reason=%s" % rec3.get("end_reason"))
        chk("ok-no-upstream_cut-event",
            [e.get("kind") for e in events()].count("upstream_cut") == n_before,
            "正常流不该记断流")
        chk("ok-stream-complete", "[DONE]" in out, out[:80])

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
