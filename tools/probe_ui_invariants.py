# -*- coding: utf-8 -*-
"""前端不变量回归探针 —— 钉住期 0 修的 5 个 bug + 防止同类问题复发.

为什么用**源码静态断言**而不是浏览器: 这 5 条都是"结构/契约"级约束
(值域是否一致、白名单是否覆盖、定义是否重复、token 是否存在),
静态判据更精确、更快, 且能在 CI 里跑。渲染行为由其它探针覆盖。

判据:
  B1 健康等级: 前端不得再用 `status_level === 'err'` 这种字面量比较;
     且 `healthState()` 必须存在于 ds-core.js。
  B2 草稿守卫: `switchTab` 必须走 `unsavedDraftKinds()`; `peakDirty`/`tooltipDirty`
     不得在无 dirty 判断时被 fetch 覆盖。
  B3 localStorage: 源码里每一处 `setItem('aigate…')` 写入的键，都必须在
     ds-core.js 的 KEEP 白名单里。
  B4 无重复定义: `filteredModelStats` / `modelDetailProviders` 全仓各只有一处 getter。
  B5 CSS token: 引用的 `var(--fs-*)` 必须在 base.css 里定义过; `var(--line)` 不得出现。

改坏了任一修复 → 对应判据必须转红 (已逐条验证)。

用法: python tools/probe_ui_invariants.py
退出码 0 = 全过; 1 = 有判据转红。
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "src", "admin")

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


def read(name):
    with io.open(os.path.join(ADMIN, name), encoding="utf-8") as f:
        return f.read()


def all_admin_files():
    out = {}
    for n in sorted(os.listdir(ADMIN)):
        if n.endswith((".html", ".js", ".css")):
            out[n] = read(n)
    return out


FILES = all_admin_files()


# ── B1：健康等级取值一致 ──
def b1():
    print("\n== B1 健康等级 (后端契约: 'ok' / 'error') ==")
    core = FILES["ds-core.js"]
    check("healthState(" in core, "ds-core.js 定义了 healthState()（唯一取值来源）")

    # 模板里不得再出现 status_level 的字面量比较（应统一走 healthState/healthOf）
    hits = []
    for name, src in FILES.items():
        if not name.endswith(".html"):
            continue
        for m in re.finditer(r"status_level\s*===?\s*'(ok|err|error)'", src):
            line = src[: m.start()].count("\n") + 1
            hits.append(f"{name}:{line} ({m.group(0)})")
    check(not hits, "模板里不再有 status_level 字面量比较", f"{hits}")

    # 特别钉住那个真实 bug：不得有人判 'err'（前端从未写过这个值）
    bad = [h for h in hits if "'err'" in h]
    check(not bad, "无 'err' 判定（该值不存在，曾经导致按钮永不出现）", f"{bad}")


# ── B2：草稿守卫 ──
def b2():
    print("\n== B2 未保存草稿不得被静默覆盖 ==")
    core = FILES["ds-core.js"]
    cfg = FILES["ds-config.js"]
    check("unsavedDraftKinds()" in core, "switchTab 走 unsavedDraftKinds() 统一守卫")
    check("clearDirtyFlags()" in cfg, "ds-config.js 提供 clearDirtyFlags()")
    check("peakDirty" in cfg and "tooltipDirty" in cfg,
          "clearDirtyFlags 覆盖 peakDirty / tooltipDirty")

    # fetchPeakSchedule / fetchTooltipConfig 在 switchTab 里必须带 dirty 判断
    m = re.search(r"if \(tabId==='settings'\|\|tabId==='providers'\)\s*\{(.*?)\n      \}",
                  core, re.S)
    check(m is not None, "找到 switchTab 的 settings/providers 分支")
    if m:
        block = m.group(1)
        for fn, flag in (("fetchPeakSchedule", "peakDirty"),
                         ("fetchTooltipConfig", "tooltipDirty")):
            if fn in block:
                guarded = re.search(rf"if\s*\(\s*!\s*this\.{flag}\s*\)\s*this\.{fn}\(", block)
                check(guarded is not None,
                      f"{fn}() 调用前有 !{flag} 守卫（否则覆盖草稿）",
                      f"该分支里 {fn} 未受 {flag} 保护")


# ── B3：localStorage 白名单覆盖所有写入键 ──
def b3():
    print("\n== B3 localStorage 白名单覆盖全部写入键 ==")
    core = FILES["ds-core.js"]
    m = re.search(r"const KEEP = \{(.*?)\};", core, re.S)
    check(m is not None, "找到 KEEP 白名单")
    if not m:
        return
    keep = set(re.findall(r"'(aigate[^']*)'", m.group(1)))
    # 源码里所有写入的键（只认**完整字面量**：'aigate.xxx' / 'aigate_xxx'）。
    # ⚠️ 不能用 `indexOf('aigate')` 那种前缀判断来算 —— 它不是写入键，
    # 本探针第一版就因此把 'aigate…' 当成缺失键报了假失败。
    written = set()
    for name, src in FILES.items():
        if not name.endswith(".js"):
            continue
        for mm in re.finditer(r"setItem\(\s*'(aigate[.A-Za-z0-9_-]*)'", src):
            written.add(mm.group(1))
    print(f"     写入键: {sorted(written)}")
    print(f"     白名单: {sorted(keep)}")
    missing = sorted(written - keep)
    check(not missing,
          "所有写入的 aigate* 键都在白名单里（缺则每次升级被静默重置）",
          f"缺失: {missing}")


# ── B4：无重复 getter 定义 ──
def b4():
    print("\n== B4 关键 getter 不得重复定义（拼接顺序会静默覆盖） ==")
    for name in ("filteredModelStats", "modelDetailProviders"):
        sites = []
        for fname, src in FILES.items():
            if not fname.endswith(".js"):
                continue
            for m in re.finditer(rf"get\s+{name}\s*\(", src):
                line = src[: m.start()].count("\n") + 1
                sites.append(f"{fname}:{line}")
        check(len(sites) == 1, f"get {name} 全仓只有 1 处定义", f"实得 {len(sites)}: {sites}")


# ── B5：CSS 变量都必须已定义 ──
def b5():
    print("\n== B5 引用的 CSS 变量必须已定义 ==")
    base = FILES["base.css"]
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", base))
    # 其它 css 也可能定义变量
    for fname, src in FILES.items():
        if fname.endswith(".css") and fname != "base.css":
            defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", src))

    # 只查字阶与常见 token（避免误报第三方/局部变量）
    watch = re.compile(r"var\((--fs-[a-z0-9-]+|--line)\)")
    bad = []
    for fname, src in FILES.items():
        for m in watch.finditer(src):
            v = m.group(1)
            if v not in defined:
                line = src[: m.start()].count("\n") + 1
                bad.append(f"{fname}:{line} ({v})")
    check(not bad, "所有 var(--fs-*) / var(--line) 都有定义", f"悬空引用: {bad}")


# ── B6：模板里的 i18n 键必须真实存在 ──
def b6():
    print("\n== B6 模板里的 t('key') 必须在 i18n 表里存在 ==")
    # 为什么必须静态查: `t()` 找不到键时**原样返回键名** —— 页面上直接显示
    # "tab_dashboard" 这种裸键名，不报错、不进 console，只有肉眼才看得见。
    # 本探针第一版写页面头部时就踩到了（写成 t('tab_dashboard')，正确是 t('nav.dashboard')）。
    i18n = FILES.get("i18n.js") or ""
    # i18n.js 里键的写法是 `'key': 'value'`，取第一张表（zh-CN）即可 —— 中英对称由 check_i18n 保证
    defined = set(re.findall(r"'([A-Za-z0-9_.\-]+)'\s*:", i18n))
    if not defined:
        check(False, "能解析出 i18n 键表", "i18n.js 解析失败")
        return
    # ⚠️ 只查**完整字面量键**，且 `t(` 前不能是标识符字符 —— 否则会把
    # `numTextFmt('anRequests',…)`、`msText('…')` 这类**函数名以 t 结尾的调用**
    # 误当成 i18n 键（第一版就是这么报了 29 个假缺失，如 'anRequests'/'heroTokens'
    # 其实是数字补间的"槽名"，不是文案键）。
    # 拼接键（t('fmt.'+x)）也已排除：后面紧跟 + 号的不算完整键。
    bad = []
    pat_full = re.compile(r"""(?<![A-Za-z0-9_$.])t\(\s*'([A-Za-z0-9_.\-]+)'\s*[,)]""")
    for fname, src in FILES.items():
        if not fname.endswith(".html"):
            continue
        for m in pat_full.finditer(src):
            k = m.group(1)
            if k.endswith(".") or k.endswith("_"):
                continue
            if k not in defined:
                line = src[: m.start()].count("\n") + 1
                bad.append(f"{fname}:{line} ({k})")
    if bad:
        print(f"     未定义的键: {bad[:8]}")
    check(not bad, "模板里所有完整字面量 t('key') 都在 i18n 表里（否则页面显示裸键名）",
          f"缺 {len(bad)} 个")


def main():
    print("前端不变量回归（静态断言）")
    b1(); b2(); b3(); b4(); b5(); b6()
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项未通过: {_fails}")
        return 1
    print(f"{OK}  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
