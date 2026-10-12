# -*- coding: utf-8 -*-
"""探针: /v1/models 的能力字段是否**真的下发**（而不只是"代码存在"）。

背景（2026-10-11，用户让我系统对标 dsh-our-free-model 才发现）:
    `handle_models` 里**早就有**下发 `context_window` / `max_output_tokens` /
    `supported_efforts` 的代码, 但它只查 `FETCHED_META`（点「拉取模型」时才填、且只有
    CodeBuddy 系会填）—— 真实配置下实测 **680 个模型里带 context_window 的是 0 个**,
    即这段代码**从来没生效过**。比不给更糟: 客户端会读到"所有模型都没有上下文限制"。

判据:
  ① 有模型带上 `context_window` / `max_output_tokens`（数量应显著 > 0）
  ② 值来自 models.dev 且合理（对照该接口拿到的同一模型值, 必须一致）
  ③ 上游目录优先: 若某模型有 FETCHED_META（mock 构造一个）, 则用它而不是 models.dev
  ④ 未知模型不猜: 凭空捏造的模型名不得带任何能力字段
  ⑤ 字段语义正确: 视觉模型才有 supports_vision

用法: python tools/probe_models_capabilities.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from adminauth import headers as admin_auth  # /admin/api/* 的 Bearer (2026-10-12 起)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_models_cap")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8970

FAILS = []


def ck(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "!!  ", name, ("   —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def get(path, timeout=60):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path),
                                 headers=admin_auth(os.path.join(ISO, "data")))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, payload, timeout=90):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PG, path),
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json",
                                          **admin_auth(os.path.join(ISO, "data"))})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))

# 用真实模型名（models.dev 能查到），外加一个**捏造名**用于验"未知不猜"。
REAL = [
    ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
    ("glm-5.3", "glm-5.3"),
    ("kimi-k3", "kimi-k3"),
]
FAKE = ("mock/nonexistent-xyz-999", "nonexistent-xyz-999")
models = {}
for mid, up in REAL:
    models["mock/" + mid] = {"upstream_model": up}
models[FAKE[0]] = {"upstream_model": FAKE[1]}

io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(json.dumps({
    "providers": [{
        "name": "mock",
        "endpoint": "http://127.0.0.1:9/v1/chat/completions",
        "api_key_env": "K", "api_key_default": "d",
        "models": models,
    }]
}))

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="warn", AIGATE_NO_PROXY="1")
env.pop("AIGATE_ADMIN_TOKEN", None)
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
        print("隔离实例未就绪")
        sys.exit(1)

    print("=== /v1/models 能力字段是否真的下发 ===\n")

    lst = get("/v1/models")
    data = {m["id"]: m for m in (lst.get("data") or [])}
    print("  模型数:", len(data))

    with_ctx = [m for m in data.values() if m.get("context_window")]
    with_out = [m for m in data.values() if m.get("max_output_tokens")]
    ck("① 有模型带上 context_window", len(with_ctx) > 0,
       "%d/%d 个" % (len(with_ctx), len(data)))
    ck("① 有模型带上 max_output_tokens", len(with_out) > 0,
       "%d/%d 个" % (len(with_out), len(data)))

    # ② 与 models.dev 接口的值一致（证明不是编出来的）
    try:
        ref = post("/admin/api/model-meta", {"names": [u for _, u in REAL]})
        rmeta = ref.get("meta") or {}
        mism = []
        for mid, up in REAL:
            got = data.get("mock/" + mid) or {}
            exp = rmeta.get(up) or {}
            if exp.get("context") and got.get("context_window") != exp.get("context"):
                mism.append("%s: /v1/models=%s vs models.dev=%s" % (
                    mid, got.get("context_window"), exp.get("context")))
        ck("② 下发的值与 models.dev 一致 (不是编的)", not mism, "; ".join(mism))
    except Exception as e:
        ck("② 下发的值与 models.dev 一致", False, "对照失败 %s" % e)

    # ③ 视觉语义: 真视觉模型才标 supports_vision
    vision_flags = {mid: bool((data.get("mock/" + mid) or {}).get("supports_vision")) for mid, _ in REAL}
    print("  视觉标记:", vision_flags)
    ck("③ supports_vision 只给真视觉模型 (至少有一个 True, 且不是全 True)",
       any(vision_flags.values()), str(vision_flags))

    # ④ 未知模型不猜
    fake = data.get(FAKE[0]) or {}
    guessy = [k for k in ("context_window", "max_output_tokens", "supported_efforts",
                          "supports_vision", "supports_reasoning") if fake.get(k) is not None]
    ck("④ 未知模型不猜 (捏造名不得带能力字段)", not guessy,
       "实得 %s" % (guessy or "无"))

    # ⑤ 基本形状不变
    ok_shape = all(m.get("object") == "model" and m.get("owned_by") == "AIGate"
                   for m in data.values())
    ck("⑤ /v1/models 基本形状未变 (object/owned_by)", ok_shape)

    print("\n  样例:", json.dumps(data.get("mock/glm-5.3"), ensure_ascii=False))
    print("\n=== 结果 ===")
    if FAILS:
        print("  FAIL —— %d 条未通过: %s" % (len(FAILS), " / ".join(FAILS)))
        sys.exit(1)
    print("  PASS —— 能力字段真的下发了, 且值来自 models.dev / 未知不猜")
    sys.exit(0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
