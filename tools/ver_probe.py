# -*- coding: utf-8 -*-
"""启动指定 exe 的隔离实例, 打印 /admin/api/version —— 只读, 不碰线上。"""
import io, json, os, shutil, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_ver")
PORT = 8986


def probe(exe, tag):
    if os.path.isdir(ISO):
        shutil.rmtree(ISO, ignore_errors=True)
    os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
    shutil.copy(exe, os.path.join(ISO, "AIGate.exe"))
    with open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"providers": []}))
    env = dict(os.environ)
    env.update(PORT=str(PORT), AIGATE_DATA_DIR=os.path.join(ISO, "data"),
               RUST_LOG="warn", AIGATE_NO_PROXY="1")
    p = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                # /admin/api/* 自 2026-10-12 起要 Bearer; token 由隔离实例生成在 data/.admin_token
                tok = io.open(os.path.join(ISO, "data", ".admin_token"),
                              encoding="utf-8", errors="replace").read().strip()
                req = urllib.request.Request("http://127.0.0.1:%d/admin/api/version" % PORT)
                if tok:
                    req.add_header("Authorization", "Bearer " + tok)
                with urllib.request.urlopen(req, timeout=2) as r:
                    print("%-14s %s" % (tag, r.read().decode("utf-8", "replace")))
                    return
            except Exception:
                time.sleep(0.5)
        print("%-14s 未起来" % tag)
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
        time.sleep(1)


probe(sys.argv[1], sys.argv[2])
