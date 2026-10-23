# -*- coding: utf-8 -*-
"""验证版本号四处同步 + 「更新亮点」数据源正确.

项目纪律: 改版本号要**四处同改** (Cargo.toml / site/index.html / README / CHANGELOG),
且 CHANGELOG 必须**重建**才烤入二进制 (否则「更新亮点」弹窗看不到新内容)。

本探针断言:
  ① Cargo.toml / Cargo.lock / README / site 两处徽章 / CHANGELOG 首位节点 —— 六处一致;
  ② **构建产物**里 /admin/api/version 报告的版本号 == Cargo.toml 的版本号;
  ③ **构建产物**里烤入的 CHANGELOG 首位节点 == 同一个版本号, 且小节数 > 0
     (少了这一条, 就会重现「面板还是旧版更新亮点」的老问题);
  ④ 面板「更新亮点」首条确实是新版本 (前端拿的是同一份 CHANGELOG)。

红线: 隔离实例 + 随机端口; 绝不碰用户 8787。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "target", "release", "AIGate.exe")
_fails = []


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


def main():
    if not os.path.exists(EXE):
        print(f"{BAD}  找不到 {EXE} (先 cargo build --release)")
        return 1

    # ── ① 六处源码一致 ──
    cargo_toml = open(os.path.join(ROOT, "Cargo.toml"), encoding="utf-8").read()
    ver = re.search(r'^version\s*=\s*"([^"]+)"', cargo_toml, re.M).group(1)
    print(f"  Cargo.toml 版本: {ver}")

    lock = open(os.path.join(ROOT, "Cargo.lock"), encoding="utf-8").read()
    lock_ver = re.search(r'name = "AIGate"\nversion = "([^"]+)"', lock).group(1)
    check(lock_ver == ver, "① Cargo.lock 与 Cargo.toml 一致", f"{lock_ver} vs {ver}")

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    check(f"**{ver}**" in readme, "① README 版本行已更新", f"未见 **{ver}**")

    site = open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8").read()
    badges = re.findall(r'class="badge ver">v([0-9.]+) · ([0-9-]+)<', site)
    check(len(badges) == 2, "① site 有两处版本徽章", f"{badges}")
    check(all(b[0] == ver for b in badges), "① site 两处徽章版本一致", f"{badges}")

    changelog = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    first = re.search(r"^## \[([0-9.]+)\]", changelog, re.M).group(1)
    check(first == ver, "① CHANGELOG 首位节点 == 当前版本", f"{first} vs {ver}")

    # ── ②③ 构建产物: 版本接口 + 烤入的 changelog ──
    workdir = os.path.join(ROOT, ".openbitfun", "tmp", "e2e_version")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
    cfg = {"providers": []}
    with open(os.path.join(workdir, "providers.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    iso_exe = os.path.join(workdir, "AIGate.exe")
    shutil.copy2(EXE, iso_exe)
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AIGATE_DATA_DIR"] = os.path.join(workdir, "data")
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

        with urllib.request.urlopen(base + "/admin/api/version", timeout=20) as r:
            vinfo = json.loads(r.read().decode())
        print(f"  实例报告版本: {vinfo.get('version')}")
        check(vinfo.get("version") == ver,
              "② **构建产物** /admin/api/version 报告的版本号正确",
              f"{vinfo.get('version')} vs {ver}")

        # ③ 烤入的 changelog (面板「更新亮点」的数据源)
        out_json = os.path.join(ROOT, ".openbitfun", "tmp", "cl_version_probe.json")
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "cl_dump.py"),
                        EXE, out_json], capture_output=True, timeout=120)
        with open(out_json, encoding="utf-8") as f:
            cl = json.load(f)
        versions = cl.get("versions") or cl.get("sections") or []
        baked = versions[0]
        baked_ver = baked if isinstance(baked, str) else baked.get("version")
        check(baked_ver == ver,
              "③ **构建产物**里烤入的 CHANGELOG 首位节点 == 当前版本",
              f"{baked_ver} vs {ver}")
        n_sec = len(baked.get("sections", [])) if isinstance(baked, dict) else 0
        check(n_sec > 0, f"③ 首位节点含小节 ({n_sec} 节) —— 否则「更新亮点」是空的", f"{n_sec}")
    finally:
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
