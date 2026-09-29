# -*- coding: utf-8 -*-
"""端到端: 启动隔离实例, 拉 /admin/api/changelog, 确认 0.7.0 排在首个且小节/条目非空。

为什么必须端到端: 解析器只认 ### 与 -, 写错标记不会报错、只是"内容消失" ——
单测只覆盖格式, 这里确认实际接口吐出的结构与内容都对。
"""
import io, json, os, shutil, subprocess, sys, threading, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
ISO = os.path.join(ROOT, ".openbitfun", "tmp", "iso_chlog")
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
PORT = 8941

if os.path.isdir(ISO):
    shutil.rmtree(ISO, ignore_errors=True)
os.makedirs(os.path.join(ISO, "data"), exist_ok=True)
shutil.copy(EXE, os.path.join(ISO, "AIGate.exe"))
io.open(os.path.join(ISO, "providers.json"), "w", encoding="utf-8").write(
    '{"providers":[{"name":"mock","endpoint":"http://127.0.0.1:9/v1/chat/completions",'
    '"api_key_env":"MK","api_key_default":"d","models":{"mock/a":{"upstream_model":"a"}}}]}')

env = dict(os.environ)
env.update(PORT=str(PORT), AIGATE_DATA_DIR=os.path.join(ISO, "data"), RUST_LOG="warn", AIGATE_NO_PROXY="1")
proc = subprocess.Popen([os.path.join(ISO, "AIGate.exe")], cwd=ISO, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)

    with urllib.request.urlopen("http://127.0.0.1:%d/admin/api/changelog" % PORT, timeout=20) as r:
        d = json.load(r)
    vs = d.get("versions") or []
    print("版本节点数: %d" % len(vs))
    first = vs[0]
    print("首个版本: %s  (%s)" % (first.get("version"), first.get("date")))
    secs = first.get("sections") or []
    items = sum(len(s.get("items") or []) for s in secs)
    print("  小节 %d 个 / 条目 %d 条" % (len(secs), items))
    print()
    print("  小节标题与条目数:")
    for s in secs:
        print("    %-46s %d 条" % (s.get("title", "")[:46], len(s.get("items") or [])))

    ok = True
    if first.get("version") != "0.7.0":
        print("\n[X] 首个版本不是 0.7.0"); ok = False
    empty = [s.get("title") for s in secs if not (s.get("items") or [])]
    if empty:
        print("\n[X] 存在空小节: %s" % empty); ok = False
    # 抽查: 缺陷小节的内容真的在
    blob = "\n".join(i for s in secs for i in (s.get("items") or []))
    for k in ("平滑曲线", "Alpine 作用域", "g2 stagger", "非等比"):
        present = k in blob
        print("  含「%s」: %s" % (k, "是" if present else "**否**"))
        if not present:
            ok = False
    print("\n=== 结果: %s ===" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
