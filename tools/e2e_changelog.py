# -*- coding: utf-8 -*-
"""端到端: 启动隔离实例, 拉 /admin/api/changelog, 确认**最新的发布节点**排在首个且小节/条目非空。

为什么必须端到端: 解析器只认 ### 与 -, 写错标记不会报错、只是"内容消失" ——
单测只覆盖格式, 这里确认实际接口吐出的结构与内容都对。

⚠️ 断言**不写死版本号**: 首个版本块随发布前进 (0.7.0 → 0.7.2 → ...),
写死会让探针在下次发版时假失败 (2026-10-12 就撞上: 仍断言 0.7.0)。
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

    # /admin/api/* 自 2026-10-12 起要 Bearer; token 由隔离实例生成在 data/.admin_token
    tok = io.open(os.path.join(ISO, "data", ".admin_token"),
                  encoding="utf-8", errors="replace").read().strip()
    req = urllib.request.Request("http://127.0.0.1:%d/admin/api/changelog" % PORT)
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    vs = d.get("versions") or []
    print("版本节点数: %d" % len(vs))
    # 最新节点必须排在首个 (面板/TheAbout 页都按首个渲染)。
    # 版本号与 Cargo.toml 一致 —— 这是唯一需要"跟着版本走"的断言。
    cargo = io.open(os.path.join(ROOT, "Cargo.toml"), encoding="utf-8").read()
    import re as _re
    m = _re.search(r'^version\s*=\s*"([^"]+)"', cargo, _re.M)
    want_ver = m.group(1) if m else None
    first = vs[0]
    print("首个版本: %s  (%s)   期望: %s" % (first.get("version"), first.get("date"), want_ver))
    secs = first.get("sections") or []
    items = sum(len(s.get("items") or []) for s in secs)
    print("  小节 %d 个 / 条目 %d 条" % (len(secs), items))
    print()
    print("  小节标题与条目数:")
    for s in secs:
        print("    %-46s %d 条" % (s.get("title", "")[:46], len(s.get("items") or [])))

    ok = True
    if want_ver and first.get("version") != want_ver:
        print("\n[X] 首个版本 %s 不是 Cargo.toml 的 %s" % (first.get("version"), want_ver)); ok = False
    empty = [s.get("title") for s in secs if not (s.get("items") or [])]
    if empty:
        print("\n[X] 存在空小节: %s" % empty); ok = False
    # 抽查: 这些条目是**早期节点**里写的, 拿它们证明"内容真的在" (不是只解析出标题)。
    # 取全库 blob 而非只首个节点 —— 早期内容随节点下移, 仍应能被解析到。
    blob = "\n".join(i for v in vs for s in (v.get("sections") or []) for i in (s.get("items") or []))
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
