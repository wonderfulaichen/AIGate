# -*- coding: utf-8 -*-
"""从 tools/narration.json 生成介绍片的**旁白文稿**（给人读 / 给 TTS 用）。

为什么要有这个脚本:
  narration.json 是配置(唯一事实源), 但它不是"文稿" —— 没有页码时间码、没有字数、
  看不出哪句会超时。本脚本把它摊成人能用的形式: 逐页时间码 + 字数 + 朗读时长预算 +
  该页画面对应的实测数字(核对用)。

纪律:
  · 文案只改 narration.json, 改完重跑本脚本 —— **不要手改产物**。
  · 每页时长(PAGE_SEC)只有一份定义, 在 capture_deck_video.js 里; 本脚本直接解析它,
    不复制一份, 否则改了一处就对不上。
  · 字数 = 不含标点(标点不占朗读时间), 语速是个假设, 会随音色/语速设置变 —— 只作预算用。

用法:
  python tools/make_narration_script.py          # 默认 4.5 字/秒
  python tools/make_narration_script.py 4.0      # 换一个语速假设

产物(本地草稿区, 不入库):
  .openbitfun/tmp/video/narration.md      读稿用, 含每页时间码与超时标记
  .openbitfun/tmp/video/tts/pNN.txt       逐页纯文本, 直接喂 TTS 逐段生成
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NARRATION = ROOT / "tools" / "narration.json"
CAPTURE = ROOT / "tools" / "capture_deck_video.js"
OUTDIR = ROOT / ".openbitfun" / "tmp" / "video"

FPS = 30
# 片尾淡出的起点(encode_video.py: fade=out:st=len-0.7:d=0.7) —— 最后一页的有效画面到这里为止
FADE_OUT = 0.7

# 标点与空白不占朗读时间, 计数时剔除
PUNCT = set("，。、；：！？…—·「」『』（）()《》〈〉“”‘’\"'%％,.;:!?~～ \t\r\n")

# 各页画面上出现的实测数字 —— 旁白必须与画面一致, 这几页专门列出来核对。
# 值全部来自快照 JSON(不手写), 键名即出处。
NUM_SRC = {
    2: [("输入 tokens", "savings_audit.json:prompt_tokens"),
        ("输出 tokens", "savings_audit.json:completion"),
        ("输入:输出", "savings_audit.json:in_out_ratio"),
        ("缓存命中率", "savings_audit.json:hit_rate_pct")],
    3: [("剥离省量", "savings_audit.json:strip"),
        ("占输入总量", "savings_audit.json:saved_share_of_input_pct"),
        ("受益请求占比", "savings_audit.json:req_with_savings_pct")],
    8: [("末轮上游收到(不剥)", "multiturn.json:final.recv_no_strip"),
        ("末轮上游收到(剥后)", "multiturn.json:final.recv_strip"),
        ("末轮省下", "multiturn.json:final.saved")],
    9: [("单请求省量中位数", "savings_shape.json:median_saved"),
        ("占该请求输入中位数", "savings_shape.json:median_strip_ratio"),
        ("输入超 10 万的请求占比", "savings_shape.json:big_input_pct"),
        ("集中在带 tool_calls 的占比", "savings_audit.json:toolcall_share_pct")],
    10: [("剥离前字节", "strip_diff.json:before.body_bytes"),
         ("剥离后字节", "strip_diff.json:after.body_bytes"),
         ("消息条数(前/后)", "strip_diff.json:before.messages | strip_diff.json:after.messages")],
}


def page_sec():
    """从 capture_deck_video.js 里解析每页时长 —— 唯一事实源, 不在这里复制一份。"""
    src = CAPTURE.read_text(encoding="utf-8")
    m = re.search(r"const PAGE_SEC = \[([^\]]*)\]", src, re.S)
    if not m:
        raise SystemExit("没在 %s 里找到 PAGE_SEC" % CAPTURE)
    vals = [float(x) for x in re.findall(r"[\d.]+", m.group(1))]
    if len(vals) != 18:
        raise SystemExit("PAGE_SEC 应含 18 项, 实得 %d" % len(vals))
    return vals


def tc(sec):
    return "%02d:%06.3f" % (int(sec // 60), sec % 60)


def nchar(text):
    return sum(1 for c in text if c not in PUNCT)


def fmt_num(v):
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


def load_snap():
    out = {}
    for name in ("savings_audit.json", "savings_shape.json", "multiturn.json", "strip_diff.json"):
        out[name] = json.loads((ROOT / "tools" / name).read_text(encoding="utf-8"))
    return out


def dig(snap, ref):
    """"a.json:b.c" -> 值。'|' 分隔的复合引用(分别取值拼起来, 便于把"前/后"放一行)。"""
    parts = [p.strip() for p in ref.split("|")]
    vals = []
    for p in parts:
        fname, path = p.split(":", 1)
        v = snap[fname]
        for k in path.split("."):
            v = v[int(k)] if isinstance(v, list) else v[k]
        vals.append(fmt_num(v))
    return " / ".join(vals)


def main():
    rate = float(sys.argv[1]) if len(sys.argv) > 1 else 4.5
    nj = json.loads(NARRATION.read_text(encoding="utf-8"))
    secs = page_sec()
    snap = load_snap()
    pages = nj["pages"]
    if len(pages) != len(secs):
        raise SystemExit("narration.json 有 %d 页, PAGE_SEC 有 %d 项" % (len(pages), len(secs)))

    total = sum(secs)
    rows, over, under = [], [], []
    t = 0.0
    for i, (pg, slot) in enumerate(zip(pages, secs)):
        text = pg["text"]
        n = nchar(text)
        est = n / rate
        start, end = t, t + slot
        t = end
        rows.append(dict(no=pg["page"], title=pg["title"], text=text, n=n, est=est,
                         start=start, end=end, slot=slot, delta=est - slot))
        if est > slot:
            over.append(rows[-1])
        elif est < slot * 0.6:
            under.append(rows[-1])

    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "tts").mkdir(exist_ok=True)

    L = []
    L.append("# AIGate 介绍片 · 旁白文稿\n")
    L.append("> 由 `tools/make_narration_script.py` 从 `tools/narration.json` 生成 —— "
             "改文案请改 JSON 再重跑，**不要手改本文件**。\n")
    L.append("> 画面 `docs/product-deck.html`（18 页）· 录制 `tools/capture_deck_video.js` · "
             "成片 `.openbitfun/tmp/video/aigate-intro.mp4`\n")
    L.append("> 音色：%s ｜ 语速假设：%.1f 字/秒 ｜ 字数不含标点\n" % (nj.get("voice", "?"), rate))
    L.append("> 页面时间码来自 `capture_deck_video.js::PAGE_SEC`（唯一事实源，本稿不另存一份）"
             "｜ 总长 **%.3fs** @ %dfps（%d 帧）｜ 片尾淡出起于 %.3fs\n"
             % (total, FPS, round(total * FPS), total - FADE_OUT))
    if nj.get("_note"):
        L.append("\n**%s**\n" % nj["_note"])
    if nj.get("_rules"):
        L.append("\n写作纪律（来自 narration.json）：\n")
        for r in nj["_rules"]:
            L.append("- %s\n" % r)

    L.append("\n## 总览\n\n")
    L.append("| 页 | 标题 | 画面时间 | 该页时长 | 旁白字数 | 预估朗读 | 余量 |\n")
    L.append("|---|---|---|---|---|---|---|\n")
    for r in rows:
        mark = "**超 %.1fs**" % r["delta"] if r["delta"] > 0 else ("空 %.1fs" % -r["delta"])
        L.append("| %d | %s | %s–%s | %.1fs | %d | %.1fs | %s |\n"
                 % (r["no"], r["title"], tc(r["start"]), tc(r["end"]), r["slot"],
                    r["n"], r["est"], mark))
    tn = sum(r["n"] for r in rows)
    te = sum(r["est"] for r in rows)
    L.append("| | **合计** | | **%.1fs** | **%d** | **%.1fs** | %s |\n"
             % (total, tn, te, "**超 %.1fs**" % (te - total) if te > total else "空 %.1fs" % (total - te)))

    if over:
        L.append("\n> ⚠️ **%d 页旁白超出画面时长**：%s\n" % (len(over), "、".join("第 %d 页" % r["no"] for r in over)))
        L.append("> 两条路：① 按 `PAGE_SEC` 延长画面（改一处，重录 %s）；"
                 "② 删字。按当前语速，全片旁白需 **%.1fs**，画面只有 **%.1fs**。\n"
                 % ("~13 分钟", te, total))
    if under:
        L.append("\n> 空档较多的页：%s（旁白偏短，画面会静默停留）\n"
                 % "、".join("第 %d 页(空 %.1fs)" % (r["no"], -r["delta"]) for r in under))

    L.append("\n---\n\n## 逐页文稿\n")
    for r in rows:
        L.append("\n### 第 %d 页 · %s\n\n" % (r["no"], r["title"]))
        L.append("`%s – %s`（%.1fs）｜旁白 %d 字 ｜ 预估 %.1fs ｜ %s\n\n"
                 % (tc(r["start"]), tc(r["end"]), r["slot"], r["n"], r["est"],
                    "**超 %.1fs**" % r["delta"] if r["delta"] > 0 else "余 %.1fs" % -r["delta"]))
        if r["no"] == rows[-1]["no"]:
            L.append("> 该页画面在 %.3fs 起淡出，最后 %.1fs 不要压文案。\n\n" % (total - FADE_OUT, FADE_OUT))
        L.append("> %s\n" % r["text"])
        if r["no"] in NUM_SRC:
            L.append("\n画面对应实测数字（核对旁白用，取自快照）：\n\n")
            for label, ref in NUM_SRC[r["no"]]:
                L.append("- %s = `%s`  ← %s\n" % (label, dig(snap, ref), ref))
        (OUTDIR / "tts" / ("p%02d.txt" % r["no"])).write_text(r["text"] + "\n", encoding="utf-8")

    md = OUTDIR / "narration.md"
    md.write_text("".join(L), encoding="utf-8")

    print("文稿: %s" % md)
    print("逐页纯文本: %s\\pNN.txt (%d 个)" % (OUTDIR / "tts", len(rows)))
    print("画面总长 %.3fs / 旁白 %d 字 / 预估朗读 %.1fs @ %.1f 字/秒" % (total, tn, te, rate))
    if over:
        print("超时页 %d 个: %s" % (len(over), ", ".join("p%d(+%.1fs)" % (r["no"], r["delta"]) for r in over)))
    if under:
        print("偏短页 %d 个: %s" % (len(under), ", ".join("p%d(%.1fs)" % (r["no"], -r["delta"]) for r in under)))


if __name__ == "__main__":
    main()
