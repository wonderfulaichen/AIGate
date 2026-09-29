# -*- coding: utf-8 -*-
"""对比两份 exe 烤入的 CHANGELOG 条目数 —— 判断部署版是否缺最新修复的记录。"""
import json, os, shutil, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_cl")
PORT = 8987


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
                with urllib.request.urlopen("http://127.0.0.1:%d/admin/api/changelog" % PORT,
                                            timeout=3) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                    vs = d.get("versions", d if isinstance(d, list) else [])
                    if vs:
                        v0 = vs[0]
                        items = sum(len(s.get("items", [])) for s in v0.get("sections", []))
                        print("%-10s 版本块=%d  首块=%s  小节=%d  条目=%d"
                              % (tag, len(vs), v0.get("version"),
                                 len(v0.get("sections", [])), items))
                        has = any("协议" in i or "responses" in i.lower()
                                  for s in v0.get("sections", []) for i in s.get("items", []))
                        print("           含协议修复条目: %s" % ("是" if has else "**否**"))
                    else:
                        print("%-10s 无版本块: %s" % (tag, str(d)[:200]))
                    return
            except Exception:
                time.sleep(0.5)
        print("%-10s 未起来" % tag)
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
        time.sleep(1)


probe(sys.argv[1], sys.argv[2])
