# -*- coding: utf-8 -*-
"""Dump 完整 changelog JSON 到文件, 供逐字 diff。用法: cl_dump.py <exe> <out.json>"""
import json, os, shutil, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_cldump")
PORT = 8988
exe, out = sys.argv[1], sys.argv[2]

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
            with urllib.request.urlopen("http://127.0.0.1:%d/admin/api/changelog" % PORT,
                                        timeout=5) as r:
                raw = r.read().decode("utf-8", "replace")
            with open(out, "w", encoding="utf-8") as f:
                f.write(raw)
            print("wrote %s (%d bytes)" % (out, len(raw)))
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("未起来")
finally:
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
