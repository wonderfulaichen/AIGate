# -*- coding: utf-8 -*-
"""把数据快照 + 真实面板截图注入 pv_scene.tpl.html → 生成 PV 场景 scene.html。

数据纪律 (与 gen_deck.py 同源): 数字与文案全部读 tools/*.json 或在此拼装, 模板里不写死。
截图: .openbitfun/tmp/shots/pv_s_dashboard.png (1920x1080) —— shoot.py 拍。

用法: python tools/build_pv_scene.py
产物: .openbitfun/tmp/video/pv/scene.html (自包含单文件, 截图 base64 内嵌)
"""
import base64
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(ROOT, ".openbitfun", "tmp", "shots")
OUT_DIR = os.path.join(ROOT, ".openbitfun", "tmp", "video", "pv")
OUT = os.path.join(OUT_DIR, "scene.html")


def load(name):
    with io.open(os.path.join(TOOLS, name), encoding="utf-8") as f:
        return json.load(f)


def img_b64(name):
    p = os.path.join(SHOTS, name)
    if not os.path.exists(p):
        raise SystemExit("缺截图 %s —— 先 build_preview.py + mkwrap.py + shoot.py --w=1920 --h=1080" % p)
    with open(p, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def main():
    audit = load("savings_audit.json")
    diff = load("strip_diff.json")
    shape = load("savings_shape.json")
    mt = load("multiturn.json")

    tpl_path = os.path.join(TOOLS, "pv_scene.tpl.html")
    with io.open(tpl_path, encoding="utf-8") as f:
        html = f.read()

    # 对外只谈 token, 不谈金额 (HANDOFF §1)
    data = {
        "dash": img_b64("pv_s_dashboard.png"),
        "foot": "每次请求 · 全量重发",
        "repost": "每一轮，原样重发",
        # 闸门字幕: 上一轮用户指正"没讲原理" —— 这里必须回答**为什么能剥** (过程≠结果)。
        # 括号里"在此剥离"的旧行文删掉: 剥离线/站B 已经标了动作, 2.6s 窗口只够讲一个道理。
        "gate_label": "推理链是过程，不是结果 —— 答案已产出，它即可剥离",
        "dim_a": "输入 %.2f 亿 tokens" % (audit["prompt_tokens"] / 1e8),
        "dim_b": "剥离 %.2f 亿 · 占输入 %.1f%%" % (audit["strip"] / 1e8, audit["saved_share_of_input_pct"]),
        # 两行分工声明 (走 innerHTML): 用户指正"剥离的不能叫重复" ——
        # 重复仍在、由 KV 缓存接住; 剥离减的是**无效上下文与冗余**。别写回单行统计。
        "cache": ("重复的，KV 缓存接住（命中 %.1f%%）—— 剥离减的是无效上下文与冗余<br>"
                  "<span class=\"sm\">单请求省量中位数 %s tokens   ·   输入:输出 %d:1</span>") % (
            audit["hit_rate_pct"], "{:,}".format(shape["median_saved"]), round(audit["in_out_ratio"])),
        # 站 B 双行字幕 (走 innerHTML): ① 字节对照 (含品牌蓝箭头) ② 必留清单 ——
        # 与闸门字幕的"为什么能剥"配对成 留/剥 两面。曾试过独立 a-stay 元素,
        # 被排版门禁打回 (站B 微漂 z→1.14 时下缘越界), 合并进已验证的 y=884 位。
        "body_line": ("同一请求 body %d B <span style=\"color:#0a84ff;font-weight:800\">→</span> %d B  "
                      "<span class=\"sm\">消息 %d 条不变</span>"
                      "<br><span style=\"font-size:.88em;color:rgba(168,212,255,.85)\">"
                      "必须留下的：system · 提问 · 回答 · 工具结果</span>") % (
            diff["before"]["body_bytes"], diff["after"]["body_bytes"], diff["before"]["messages"]),
        # 站 E 四大数字 (计数动画的目标值): [剥离亿, 占输入%, 受益%, 中位数]
        "ev": [round(audit["strip"] / 1e8, 2),
               round(audit["saved_share_of_input_pct"], 1),
               round(audit["req_with_savings_pct"], 1),
               shape["median_saved"]],
        # 站 C 游标读数: 原始逐轮字节 (前端插值)
        "mtOff": [r["recv_bytes"] for r in mt["without"]],
        "mtOn": [r["recv_bytes"] for r in mt["with_strip"]],
        # 站 E 实时条: 模型省量 (取 top_models)
        "ticker": shape.get("top_models", []),
        # 面板驱动: 真实省量 (今日 = strip 单日估算, 本月/累计 = strip 总量)
        "panelToday": audit["strip"] // 500,     # 5000 条日志按日均摊
        "panelMonth": audit["strip"],
    }

    if "{{DATA}}" not in html:
        raise SystemExit("模板缺少 {{DATA}} 占位符")
    html = html.replace("{{DATA}}", json.dumps(data, ensure_ascii=False))

    # ── 站 C 折线: 点列从 multiturn.json 生成 (y: 0B→480, 12000B→60; x: 第1轮90 → 第10轮1291) ──
    def pts(key):
        rows = mt[key]
        out = []
        for i, r in enumerate(rows):
            x = 90 + i * (1200 / 9)
            y = 480 - r["recv_bytes"] / 12000.0 * 420
            out.append("%.0f,%.0f" % (x, y))
        return " ".join(out)

    y_off = 480 - mt["without"][-1]["recv_bytes"] / 12000.0 * 420
    y_on = 480 - mt["with_strip"][-1]["recv_bytes"] / 12000.0 * 420
    subs = {
        "STB_BYTES": "%d → %d B" % (diff["before"]["body_bytes"], diff["after"]["body_bytes"]),
        "P_OFF": pts("without"),
        "P_ON": pts("with_strip"),
        "DOT_OFF_Y": "%.0f" % y_off,
        "DOT_ON_Y": "%.0f" % y_on,
        "LAB_OFF_Y": "%.0f" % (y_off - 18),
        "LAB_ON_Y": "%.0f" % (y_on + 40),
        "LAB_OFF_T": "{:,} B".format(mt["without"][-1]["recv_bytes"]),
        "LAB_ON_T": "{:,} B".format(mt["with_strip"][-1]["recv_bytes"]),
    }
    for k, v in subs.items():
        token = "{{%s}}" % k
        if token not in html:
            raise SystemExit("模板缺少占位符 %s" % token)
        html = html.replace(token, v)

    left = re.findall(r"\{\{\w+\}\}", html)
    if left:
        raise SystemExit("未替换的占位符: %s" % left)

    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    # ── 真实面板: 把 build_preview 的预览页复制到场景旁边, 供 iframe 加载 ──
    # (capture_pv.js 用路由把 /admin/static/* 接到真文件; 父页访问 iframe DOM 需
    #  --allow-file-access-from-files, 同样在该脚本里)
    src_panel = os.path.join(ROOT, ".openbitfun", "tmp", "preview", "admin.html")
    dst_panel = os.path.join(OUT_DIR, "panel.html")
    if not os.path.exists(src_panel):
        raise SystemExit("缺面板预览 %s —— 先跑 python tools/build_preview.py" % src_panel)
    import shutil
    shutil.copyfile(src_panel, dst_panel)
    print("panel -> %s (%.0f KB)" % (dst_panel, os.path.getsize(dst_panel) / 1024))

    print("scene -> %s (%.0f KB)" % (OUT, len(html.encode("utf-8")) / 1024))
    print("  输入 %s" % data["dim_a"])
    print("  %s" % data["dim_b"])
    print("  %s" % data["cache"])
    print("  站B: %s" % subs["STB_BYTES"])
    print("  站C: 末轮 %s vs %s" % (subs["LAB_OFF_T"], subs["LAB_ON_T"]))
    print("  站E: 亿%.2f / %.1f%% / %.1f%% / %s" % tuple(data["ev"]))


if __name__ == "__main__":
    main()
