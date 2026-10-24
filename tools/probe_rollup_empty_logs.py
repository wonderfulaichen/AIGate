# -*- coding: utf-8 -*-
"""空日志缓冲区时 rollup 历史必须可见（2026-10-24 修的既有缺陷）。

缺陷原样：`stats` 的 rollup 合并写的是
    if let Some(cover_ts) = state.log_buffer.oldest_ts().await { ...合并... }
而 `oldest_ts()` 在缓冲区**为空**时返回 `None` → 整段合并被静默跳过。
后果：日志被清空（或全部轮转走）后，即使 `daily_stats.jsonl` 里有完整历史，
`per_provider` / `per_model` 也完全看不到历史数据。

为什么用真实实例而不是单测：`rollup_merge_bounds` 是纯函数，单测只能证明
"给了 now 就能算出正确边界"；证明不了**调用方是否会去调它** —— 而缺陷正好
出在调用方（`if let Some` 把 None 吞掉）。必须端到端跑一次。

判据：
  ① 缓冲区为空时，range=29d 仍能看到 rollup 里的供应商（核心）
  ② 失败原因随 rollup 一并透出（上一轮的字段在空缓冲区下也不丢）
  ③ 今天的数据不得被合并进来（今天的 rollup 仍在累加，不完整）
  ④ 默认 range（不合并 rollup）仍不包含历史（防止把合并无条件打开）

用法: python tools/probe_rollup_empty_logs.py
退出码 0 = 全过; 1 = 有判据转红。
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
DEPLOY_ROOT = r"D:\Office software\Development Project\AI中转"
WD = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_rollupempty")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        _fails.append(label)
        print(f"  {BAD}  {label}  {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE}（先 cargo build --release）")
        return 1
    shutil.rmtree(WD, ignore_errors=True)
    data = os.path.join(WD, "data")
    os.makedirs(data, exist_ok=True)
    src_pv = os.path.join(DEPLOY_ROOT, "providers.json")
    if not os.path.isfile(src_pv):
        print(f"{BAD}  找不到 {src_pv}")
        return 1
    shutil.copy2(src_pv, os.path.join(WD, "providers.json"))

    now = int(time.time())
    TZ = 8 * 3600
    today = (now + TZ) // 86400 * 86400 - TZ
    yesterday = today - 86400

    # 昨天: 有 zen（带失败原因）
    # 今天: 也有 zen（用来验证"今天不被合并"，因为今天的 rollup 仍在累加）
    def entry(day_label, reqs):
        return {
            "provider": "zen", "upstream": f"m-{day_label}",
            "aliases": [f"zen/m-{day_label}"],
            "requests": reqs, "errors": reqs,
            "latency_sum_ms": reqs * 500, "body_bytes": 1000,
            "prompt_tokens": 100, "completion_tokens": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 100,
            "last_error": f"404 <html><body>{day_label} Not Found</body></html>",
            "last_error_status": 404,
            "last_error_ts": yesterday + 3600 if day_label == "y" else now - 60,
        }

    with io.open(os.path.join(data, "daily_stats.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"d": yesterday, "e": [entry("y", 7)]}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"d": today, "e": [entry("t", 3)]}, ensure_ascii=False) + "\n")
    # 关键：logs 完全为空 —— 这正是缺陷场景
    io.open(os.path.join(data, "logs.jsonl"), "w", encoding="utf-8").write("")

    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = data
    env["AIGATE_OPEN_ADMIN"] = "1"
    iso = os.path.join(WD, "AIGate.exe")
    shutil.copy2(EXE, iso)
    proc = subprocess.Popen([iso], env=env, cwd=WD,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"{BAD}  实例未就绪")
            return 1
        d29 = json.loads(urllib.request.urlopen(
            base + "/admin/api/stats?range=29d", timeout=30).read().decode())
        dwin = json.loads(urllib.request.urlopen(
            base + "/admin/api/stats", timeout=30).read().decode())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()

    p29 = {p["provider"]: p for p in (d29.get("per_provider") or [])}
    pwin = {p["provider"]: p for p in (dwin.get("per_provider") or [])}
    print("空日志缓冲区 + rollup 有历史")
    print(f"  [现场] range=29d per_provider={sorted(p29)}；默认 range={sorted(pwin)}")

    # ① 核心：空缓冲区仍能看到历史
    check("zen" in p29,
          "① 缓冲区为空时 range=29d 仍能看到 rollup 里的供应商",
          "per_provider 为空 —— 说明 rollup 合并又被跳过了")

    if "zen" in p29:
        zen = p29["zen"]
        # 只应有昨天的 7 次（今天的 3 次不得进来）
        check(zen.get("requests") == 7,
              "③ 今天的数据未被合并（今天的 rollup 仍在累加，不完整）",
              f"requests={zen.get('requests')}（期望 7 = 只有昨天）")
        # ② 失败原因随 rollup 透出
        check(zen.get("last_error_status") == 404
              and "y Not Found" in (zen.get("last_error") or ""),
              "② 失败原因随 rollup 一并透出（空缓冲区下也不丢）",
              f"last_error={zen.get('last_error')!r} status={zen.get('last_error_status')}")

    # ④ 默认 range 不得包含历史（合并只对显式 range 生效）
    check("zen" not in pwin,
          "④ 默认 range（不合并 rollup）仍不含历史",
          f"per_provider={sorted(pwin)} —— 合并被无条件打开了")

    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过:")
        for f in _fails:
            print(f"     - {f}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
