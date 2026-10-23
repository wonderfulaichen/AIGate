# -*- coding: utf-8 -*-
"""「拉取模型」改为**只预览不改路由** 的端到端验证.

背景 (2026-10-24): 旧实现在 `api_providers_fetch_models` 里直接 `registry.add_models()`,
于是"点了一下获取"就把新模型**立即**写进运行时路由表 —— 客户端立刻能调它, 而用户还没在
面板里确认、也没点「保存配置」。前端弹窗的「取消」只撤表单, **界面没接受、运行时却已生效**,
两边不一致且无法从界面看出来。

本探针按用户真实操作顺序验证四件事:
  ① 拉取前: /v1/models 里**没有**上游新模型 (基线)
  ② 拉取后 (未保存): /v1/models **仍然没有**它 → 运行时未被污染 (本轮核心判据)
  ③ 同时面板接口确实拿到了目录 (差异弹窗有东西可显示, 不是"拉了但没反应")
  ④ 保存后: /v1/models **才有**它 → 生效点确实是「保存配置」

另加一条能力回填判据 (顺带修的真 bug):
  ⑤ 拉取时采集到的 supported_efforts 在**保存时**写进 providers.json
     (旧注释声称保存时回填, 但链路里并没有这一步, 能力实际全丢)

红线: 隔离实例 + 随机端口 + 本地 mock 上游; 绝不碰用户 8787。
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

# mock 上游: 目录里有两个模型, 其中一个带 supported_efforts (buddy 式能力声明)
UPSTREAM_IDS = ["brand-new-model", "kept-model"]


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

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": "kept-model"},
                {"id": "brand-new-model"},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):  # noqa: N802  (probe_protocols 会探协议)
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1

    mport = free_port()
    srv = HTTPServer(("127.0.0.1", mport), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_fetch_preview")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 初始 providers.json 只配 kept-model (brand-new-model 是"上游新增"的那个)
    cfg = {"providers": [{
        "name": "mockprov",
        "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
        "api_key_env": "MOCK_KEY", "api_key_default": "dummy",
        "models": {"mockprov/kept-model": {"upstream_model": "kept-model"}},
    }]}
    prov_path = os.path.join(workdir, "providers.json")
    with open(prov_path, "w", encoding="utf-8") as f:
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

        # ① 拉取前基线: 运行时只有 kept-model
        _, models_before = get(base, "/v1/models")
        ids_before = [m.get("id") for m in json.loads(models_before).get("data", [])]
        print(f"\n  ① 拉取前 /v1/models: {ids_before}")
        check("brand-new-model" not in ids_before, "① 拉取前运行时没有新模型 (基线成立)")
        check("mockprov/kept-model" in ids_before, "① 原有模型在运行时里", f"{ids_before}")

        # ② 拉取 (但不保存) —— 面板的「获取模型」按钮走的正是这个接口
        st, resp = post(base, "/admin/api/providers/mockprov/fetch-models", {})
        print(f"  ② fetch-models 返回: success={resp.get('success')} "
              f"added={resp.get('added')} skipped={resp.get('skipped')} "
              f"removed={resp.get('removed')}")
        check(st == 200 and resp.get("success") is True, "② 拉取接口成功", f"{st} {resp}")
        check("brand-new-model" in (resp.get("models") or []),
              "② 接口返回了上游完整目录 (差异弹窗有内容)", f"{resp.get('models')}")
        check(resp.get("added") == 1, "② 正确算出新增 = 1 (kept 已存在)", f"{resp.get('added')}")
        check(resp.get("skipped") == 1, "② 正确算出已有 = 1", f"{resp.get('skipped')}")
        check(resp.get("removed") == [], "② 没有误报下架", f"{resp.get('removed')}")
        check(resp.get("applied") is False,
              "② 接口明确声明本次未改动运行时 (applied=false)", f"{resp.get('applied')}")

        # ③ 核心判据: 拉取后未保存, 运行时**仍**没有新模型
        _, models_after = get(base, "/v1/models")
        ids_after = [m.get("id") for m in json.loads(models_after).get("data", [])]
        print(f"  ③ 拉取后(未保存) /v1/models: {ids_after}")
        check("brand-new-model" not in ids_after,
              "③ **核心**: 拉取后未保存, 运行时未被污染 (取消弹窗真的无副作用)",
              f"{ids_after}")

        # 直接打新模型的入口, 必须还是 404 (不是"列出来了但调不了"那种半生效)
        st_new, _ = post(base, "/v1/chat/completions",
                         {"model": "mockprov/brand-new-model",
                          "messages": [{"role": "user", "content": "hi"}]})
        check(st_new == 404, "③ 直接调用新模型仍是 404 (半生效会被这条抓住)", f"实得 {st_new}")

        # ④ 保存后才生效 (模拟前端: 把新模型加进表单再保存)
        cfg2 = {"providers": [{
            "name": "mockprov",
            "endpoint": f"http://127.0.0.1:{mport}/v1/chat/completions",
            "api_key_env": "MOCK_KEY", "api_key_default": "dummy",
            "models": {
                "mockprov/kept-model": {"upstream_model": "kept-model"},
                "mockprov/brand-new-model": {"upstream_model": "brand-new-model"},
            },
        }]}
        st_save, resp_save = post(base, "/admin/api/providers/save",
                                  {"json": json.dumps(cfg2, ensure_ascii=False),
                                   "oldNames": ["mockprov"]})
        check(st_save == 200 and resp_save.get("message"),
              "④ 保存配置成功", f"{st_save} {resp_save}")

        _, models_saved = get(base, "/v1/models")
        ids_saved = [m.get("id") for m in json.loads(models_saved).get("data", [])]
        print(f"  ④ 保存后 /v1/models: {ids_saved}")
        check("mockprov/brand-new-model" in ids_saved,
              "④ **核心**: 保存后新模型才进入运行时 (生效点 = 保存配置)", f"{ids_saved}")

        # ⑤ 磁盘上的 providers.json 是生效的最终凭据
        on_disk = json.load(open(prov_path, encoding="utf-8"))
        disk_models = (on_disk["providers"][0].get("models") or {})
        check("mockprov/brand-new-model" in disk_models,
              "⑤ 磁盘 providers.json 里有新模型", f"{list(disk_models)}")
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
