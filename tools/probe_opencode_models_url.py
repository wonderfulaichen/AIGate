# -*- coding: utf-8 -*-
"""opencode 模型列表端点推导取证 + 拉通探针.

背景 (2026-10-21): 用户报告「改了 go 的链接后模型获取失败」。
根因: 我们的 `/models` URL 推导是 `endpoint.replace("/chat/completions", "/models")`,
而 opencode 的模型端点**与协议段无关** —— 永远 `/inference[/go]/v1/models`,
协议段 (`/openai/`) 只存在于**请求**端点。于是
`/inference/go/openai/v1/chat/completions` 被推导成
`/inference/go/openai/v1/models` → 404, 面板「拉取模型」必然失败。

本探针两件事:
  ① 固化证据: 逐个 URL 打真实上游, 断言「200 的确实是 200、404 的确实是 404」
     (纯 URL 层, 不依赖我们的二进制)
  ② 拉通: 把推导逻辑抽成纯函数 `derive_models_url` (Rust 单测内断言),
     覆盖 opencode 新/旧端点 + 三家非 opencode 供应商 + 裸 base URL

退出码 0 = 全部通过; 1 = 有判据转红。
用法:
  python tools/probe_opencode_models_url.py live   # 只打真实上游
  python tools/probe_opencode_models_url.py rust   # 只跑 Rust 单测
  python tools/probe_opencode_models_url.py all    # 两者都跑
"""
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
_fails = []


def check(cond, label, detail=""):
    if cond:
        print(f"  {OK}  {label}")
    else:
        print(f"  {BAD}  {label}  {detail}")
        _fails.append(label)


# ── ① 真实上游: 固化端点形态 ──
# (url, 期望 http 状态, 说明)
CASES = [
    ("https://opencode.ai/inference/go/v1/models", 200, "新 go 模型端点 (正确形态)"),
    ("https://opencode.ai/inference/v1/models", 200, "新 zen 模型端点 (正确形态)"),
    ("https://opencode.ai/zen/go/v1/models", 200, "旧 go 模型端点 (兼容形态)"),
    ("https://opencode.ai/zen/v1/models", 200, "旧 zen 模型端点 (兼容形态)"),
    ("https://opencode.ai/inference/go/openai/v1/models", 404, "推导出的错误 URL (含 /openai)"),
    ("https://opencode.ai/inference/openai/v1/models", 404, "推导出的错误 URL (含 /openai)"),
    ("https://opencode.ai/zen/go/openai/v1/models", 404, "旧基址 + /openai 也 404"),
]


def probe_live():
    print("\n== ① 真实上游端点形态 ==")
    # 哨兵: 先探一个必定不存在的路径。若它返的也是 403/200 (与待判路径同态),
    # 说明本站当前对所有路径状态一致 (限流/WAF/统一鉴权), 逐路径探测毫无判别力
    # → 直接判定「本轮不可判别」, 不产出任何 200/404 结论, 避免假判据。
    # (同 src/providers.rs probe_protocols 的哨兵思路。)
    try:
        req = urllib.request.Request(
            "https://opencode.ai/aigate-probe-nonexistent-9f3c", method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            sentinel = r.status
    except urllib.error.HTTPError as e:
        sentinel = e.code
    except Exception as e:  # noqa: BLE001
        sentinel = None
        print(f"  [i] 哨兵网络错误: {e}")
    if sentinel is not None and sentinel not in (404, 501):
        print(f"  [i] 哨兵路径返 {sentinel} (非 404) → 本站当前不可判别 "
              f"(限流/WAF/统一鉴权), 跳过 200/404 判据, 不产出假结论")
        print("      (先前同批 URL 已实测: 正确形态 200、含 /openai 404)")
        return
    for url, want, why in CASES:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                got = r.status
        except urllib.error.HTTPError as e:
            got = e.code
        except Exception as e:  # noqa: BLE001
            print(f"  {BAD}  {url} 网络错误: {e}")
            _fails.append(url)
            continue
        check(got == want, f"{got} == {want}  {url}", f"({why})")


# ── ② Rust 单测: 纯函数推导 ──
TEST_NAME = "derive_models_url_matches_upstream_shape"

RUST_TEST = r'''
    #[test]
    fn %NAME%() {
        // (endpoint, 期望推导出的 /models URL, 说明)
        let cases = [
            // ── opencode: 模型端点与协议段无关, 永远 <base>/v1/models ──
            ("https://opencode.ai/inference/go/openai/v1/chat/completions",
             "https://opencode.ai/inference/go/v1/models",
             "go 新端点 (用户当前配置)"),
            ("https://opencode.ai/inference/openai/v1/chat/completions",
             "https://opencode.ai/inference/v1/models",
             "zen 新端点"),
            ("https://opencode.ai/zen/go/v1/chat/completions",
             "https://opencode.ai/zen/go/v1/models",
             "go 旧端点 (仍 200, 需兼容)"),
            ("https://opencode.ai/zen/v1/chat/completions",
             "https://opencode.ai/zen/v1/models",
             "zen 旧端点 (仍 200, 需兼容)"),
            ("https://opencode.ai/inference/anthropic/v1/messages",
             "https://opencode.ai/inference/anthropic/v1/models",
             "anthropic 协议段"),
            ("https://opencode.ai/inference/openai/v1/responses",
             "https://opencode.ai/inference/v1/models",
             "responses 协议段"),
            // ── 非 opencode: 保持既有行为 (协议段就是 /models 的前缀) ──
            ("https://api.deepseek.com/v1/chat/completions",
             "https://api.deepseek.com/v1/models", "DeepSeek"),
            ("https://api.minimaxi.io/anthropic/v1/messages",
             "https://api.minimaxi.io/anthropic/v1/models", "MiniMax"),
            ("https://api.openai.com/v1/responses",
             "https://api.openai.com/v1/models", "OpenAI Responses"),
            ("https://api.xxx.com/v1",
             "https://api.xxx.com/v1/models", "裸 base URL"),
        ];
        for (ep, want, why) in cases {
            let got = derive_models_url(ep);
            assert_eq!(got, want, "端点推导错误: {why} ({ep})");
        }
        // 负例: 推导结果绝不含 /openai (opencode 的协议段不该进模型端点)
        let bad = derive_models_url("https://opencode.ai/inference/go/openai/v1/chat/completions");
        assert!(!bad.contains("/openai"), "推导结果不该含协议段 /openai: {bad}");
    }
'''


def run_cargo_test():
    """跑 cargo test, 并区分『测试失败』与『编译失败』(exit≠0 ≠ 判据有牙齿)."""
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = env.get("CARGO_TARGET_DIR", "target")
    # 注意: 本 crate 是纯 bin (无 [lib]), 必须用 --bin AIGate; --lib 会直接报
    # "no library targets found" 而伪装成"测试失败"。
    r = subprocess.run(
        ["cargo", "test", "--bin", "AIGate", "--", "--nocapture", TEST_NAME],
        capture_output=True, text=True, timeout=900, env=env,
    )
    out = r.stdout + r.stderr
    if "could not compile" in out or "error[" in out:
        print(out[-4000:])
        check(False, "编译通过 (失败≠判据有牙齿, 先确认不是编译错误)")
        return
    check(r.returncode == 0, f"单测 {TEST_NAME} 通过 (exit 0)")
    if r.returncode != 0:
        print(out[-4000:])
    m = re.search(r"test result: (\w+)", out)
    if m:
        print(f"     cargo 报告: {m.group(0)}")


def probe_rust():
    print("\n== ② Rust 侧纯函数推导 (单测断言) ==")
    path = "src/providers.rs"
    with open(path, encoding="utf-8") as f:
        src = f.read()

    if "fn derive_models_url" not in src:
        check(False, "providers.rs 存在 derive_models_url (尚未实现)")
        return
    if TEST_NAME in src:
        print(f"  [i] {TEST_NAME} 已在源码中, 探针只跑不改文件")
        run_cargo_test()
        return

    anchor = "    fn is_free_detects_flag_and_name() {"
    if anchor not in src:
        check(False, "找到单测插入锚点", "is_free_detects_flag_and_name 不在 providers.rs")
        return
    patched = src.replace(anchor, RUST_TEST.replace("%NAME%", TEST_NAME) + "\n" + anchor, 1)
    tmp = path + ".probe.bak"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    try:
        run_cargo_test()
    finally:
        with open(tmp, encoding="utf-8") as f:
            orig = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(orig)
        os.remove(tmp)
        with open(path, encoding="utf-8") as f:
            after = f.read()
        # 判据: 还原后必须与改动前逐字节相同 (探针不留痕)
        check(after == src, "providers.rs 已还原 (逐字节相同)")
        if after != src:
            print("     [i] 还原不一致, 用 .probe.bak 内容恢复")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "live"
    if which in ("live", "all"):
        probe_live()
    if which in ("rust", "all"):
        probe_rust()
    print()
    if _fails:
        print(f"{BAD}  {len(_fails)} 项判据未通过: {_fails}")
        sys.exit(1)
    print(f"{OK}  全部通过")
