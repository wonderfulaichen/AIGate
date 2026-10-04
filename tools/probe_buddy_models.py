# -*- coding: utf-8 -*-
"""诊断: buddy 模型列表为何拉不到新模型 —— 三方对照 + 网关「拉取模型」实际命中数.

对照:
  A. 网关转发 GET /v1/models (客户端/面板实际走的) —— 看返回个数与是否含新模型
  B. CodeBuddy 直连  /v2/models 与 /v1/models —— 上游到底发不发
  C. /v3/config (权威模型配置) —— 新模型以哪个为准
  D. 通过网关 POST /admin/api/providers/<name>/fetch-models —— 面板「拉取」实测命中
"""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_models")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PG = 8988
cred = json.load(open(os.path.join(tempfile.gettempdir(), "cb_plain.json"), encoding="utf-8"))
tok = cred["auth"]["accessToken"]

HEADERS = {
    "X-Domain": "copilot.tencent.com", "X-Product-Code": "codebuddy",
    "X-Product": "CodeBuddy", "X-Agent-Purpose": "conversation",
    "X-IDE-Name": "CodeBuddy", "X-IDE-Type": "CodeBuddy",
    "X-IDE-Version": "1.106.1", "user-agent": "CodeBuddyIDE/1.106.1",
}

def get(url, extra=None, timeout=25):
    h = dict(extra or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"

def ids_of(txt):
    try:
        j = json.loads(txt)
    except Exception:
        return None, txt[:200]
    arr = j.get("data") if isinstance(j, dict) else j
    if isinstance(arr, list):
        return sorted({str(x.get("id") or x.get("model_id") or x) for x in arr if not isinstance(x, str)} |
                      {str(x) for x in arr if isinstance(x, str)}), None
    return None, txt[:200]

# ── B. 直连上游 ──
print("═══ B. CodeBuddy 直连 ═══")
for path in ("/v2/models", "/v1/models", "/v2/chat/models"):
    st, txt = get("https://copilot.tencent.com" + path,
                  dict(HEADERS, **{"authorization": "Bearer " + tok,
                                   "accept": "application/json"}))
    ids, err = ids_of(txt)
    print(f"  [{path}] HTTP {st} 模型数={len(ids) if ids else '-'} {err or ''}")
    if ids:
        print("     ", ids[:30])

# ── C. /v3/config 权威 ──
print("\n═══ C. /v3/config 权威模型配置 ═══")
st, txt = get("https://copilot.tencent.com/v3/config",
              dict(HEADERS, **{"authorization": "Bearer " + tok}))
cfg_ids = []
if st == 200:
    j = json.loads(txt)
    models = None
    for kp in (("data", "models"), ("models",)):
        cur = j
        for k in kp:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None: break
        if isinstance(cur, list):
            models = cur; break
    if models:
        cfg_ids = sorted({str(m.get("id")) for m in models if isinstance(m, dict)})
        print(f"  模型数={len(cfg_ids)}")
        print("   ", cfg_ids[:30])
else:
    print(f"  HTTP {st}")

# ── A + D. 经网关 ──
print("\n═══ A/D. 经网关 (隔离实例, 空模型表) ═══")
if os.path.isdir(ISO):
    import shutil; shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
import shutil; shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
json.dump({"providers": [{
    "name": "buddy",
    "endpoint": "https://copilot.tencent.com/v2/chat/completions",
    "api_key_env": "BUDDY_ACCESS_TOKEN", "api_key_default": tok,
    "headers": HEADERS, "preset": "buddy", "models": {},
}]}, open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8"),
    ensure_ascii=False, indent=1)

env = dict(os.environ)
env.update(PORT=str(PG), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
           RUST_LOG="info", AIGATE_NO_PROXY="1")
logf = open(os.path.join(ISO, "run.log"), "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=logf, stderr=logf)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PG}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    st, txt = get(f"http://127.0.0.1:{PG}/v1/models",
                  {"authorization": "Bearer d"})
    gids, err = ids_of(txt)
    print(f"  [网关 GET /v1/models] HTTP {st} 模型数={len(gids) if gids else '-'} {err or ''}")
    if gids: print("   ", gids[:30])
    # D. 面板「拉取模型」
    req = urllib.request.Request(
        f"http://127.0.0.1:{PG}/admin/api/providers/buddy/fetch-models",
        data=b"{}", method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        def n(x):
            return len(x) if isinstance(x, (list, dict)) else 0
        print(f"  [fetch-models] HTTP {r.status} added={n(d.get('added'))} "
              f"existing={n(d.get('existing'))} removed={n(d.get('removed'))}")
        print("   added:", (d.get("added") or [])[:30] if isinstance(d.get("added"), list) else d.get("added"))
        print("   msg:", d.get("message") or d.get("error"))
    except Exception as e:
        print("  [fetch-models] 失败:", type(e).__name__, e)
        try:
            print("   body:", e.read().decode("utf-8", "replace")[:300])
        except Exception:
            pass
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    logf.close()
    tail = open(os.path.join(ISO, "run.log"), encoding="utf-8", errors="replace").read()
    for line in tail.splitlines():
        if "models" in line.lower() or "model" in line.lower():
            print("   LOG:", line[:220])
