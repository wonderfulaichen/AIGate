# -*- coding: utf-8 -*-
"""品牌标同源门禁 —— 所有矢量落点必须与 icon.svg **母版**逐属性一致。

为什么要有这道门禁 (2026-09-30 换标时实测):
    同一个标在仓库里同时存在三种画法 —— icon.svg/core.js 是新几何, site 与 deck 还是
    旧靛蓝 (#6366f1 一族), build.rs 里又是另一份手写光栅。根因不是"改错了", 而是
    **改了 A 忘改 B**, 且没有任何机制会报错。所以把 icon.svg 当唯一事实源, 逐属性比对。

  母版        icon.svg
  矢量落点    src/admin/core.js::brandLogoSvg()      (面板顶栏 + 关于页)
              site/index.html                        (落地页顶栏品牌)
              docs/product-deck.html                 (封面 logo + 流向 hub, 共 2 处)
  光栅落点    build.rs::pixel_color()                (exe 图标的 32×32, 手写逐像素)

用法:   python tools/verify_logo.py
退出码: 0 = 全部一致; 1 = 有不一致 / 旧标残留
分工:   PART A/B 查矢量落点, PART C 逐控制点比对 build.rs 的光栅几何, PART D 验 icon.ico 实体
        (尺寸集合 / 采样像素 / 构建缓存有没有把改动吞掉)
"""
import io
import os
import re
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级

MASTER = "icon.svg"
VECTOR_SITES = [
    ("core.js", "src/admin/core.js", 1),
    ("site", "site/index.html", 1),
    ("deck", "docs/product-deck.html", 2),
]
RASTER = ("build.rs", "build.rs")

# 旧标的指纹 —— 出现任意一条就说明还有一份没换干净
OLD_MARK_PATTERNS = [
    (r"#6366f1|#4f46e5", "旧靛蓝"),
    (r'circle cx="72" cy="80"', "旧四节点"),
    (r'rx="48"', "旧圆角 48"),
    (r"url\(#ag\)", "旧渐变引用"),
]

# 旧 exe 光栅的指纹 —— build.rs::pixel_color 用浮点系数而非 #hex, 故单列
OLD_RASTER_PATTERNS = [
    (r"// 背景: 紫色渐变", "旧光栅注释"),
    (r"99\.0 \+", "旧光栅紫底系数"),
    (r"dist_center", "旧光栅圆环/节点几何"),
]

MARK_RE = re.compile(r'<svg[^>]*viewBox="0 0 256 256"[^>]*>(.*?)</svg>', re.S)
PATH_RE = re.compile(r"<path\b([^>]*?)/>")
RECT_RE = re.compile(r"<rect\b([^>]*?)/>")
ATTR_RE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def norm_color(v):
    v = v.strip().lower()
    if v.startswith("#") and len(v) == 4:                      # #fff -> #ffffff
        v = "#" + "".join(c * 2 for c in v[1:])
    return v


def norm_d(v):
    """路径数据只比几何, 不比排版: 去掉全部空白并转小写。"""
    return re.sub(r"\s+", "", v).lower()


def attrs_of(blob):
    return {k.lower(): v.strip() for k, v in ATTR_RE.findall(blob)}


def signature_of(body):
    """从一段标的内容抽出可比对的属性组 —— 只认结构, 不认缩进/换行。"""
    sig = {}
    rect = RECT_RE.search(body)
    if not rect:
        return None
    ra = attrs_of(rect.group(1))
    sig["rect"] = tuple(ra.get(k, "") for k in ("x", "y", "width", "height", "rx"))
    sig["rect.fill"] = norm_color(ra.get("fill", ""))
    paths = []
    for blob in PATH_RE.findall(body):
        pa = attrs_of(blob)
        paths.append((
            norm_d(pa.get("d", "")),
            pa.get("fill", "none").strip().lower(),
            norm_color(pa.get("stroke", "")),
            pa.get("stroke-width", ""),
            pa.get("stroke-linecap", ""),
            pa.get("stroke-linejoin", ""),
        ))
    sig["paths"] = tuple(paths)
    return sig


def collect(rel):
    """返回 (归一化后的 svg 文本列表, 签名, 命中段数) —— 多段时取全部。"""
    text = read(rel)
    if rel.endswith(".js"):
        # JS 里 svg 是字符串拼接出来的: 截出函数体, 去掉拼接符与引号, 再把空白收成单空格。
        # ⚠️ 不能把空白全删 —— 拼接处的那个空格正是属性之间的分隔符
        #    ('…stroke-width="24"' + ' stroke-linecap="round"'), 删掉会粘成
        #    stroke-width="24"stroke-linecap=… 从而解析不出属性。
        m = re.search(r"function brandLogoSvg\(\)\s*\{(.*?)\n\}", text, re.S)
        if not m:
            return None, None, 0
        body = m.group(1).replace("+", "").replace("'", "")
        body = re.sub(r"\s+", " ", body)
        marks = MARK_RE.findall(body)
    else:
        marks = MARK_RE.findall(text)
    return marks, None, len(marks)


def fail(msg):
    print("  ✗ " + msg)
    return 1


# ── PART C 用: 把母版 path 展开成点列, 再与 build.rs 里的常量逐个比对 ──
NUM = r"-?\d+(?:\.\d+)?"


def expand_path(d):
    """母版 path → 绝对坐标点列。只支持本标用到的 M/V/H/L/Z(够用且不会静默比错):
    遇到别的命令直接抛错 —— 万一以后给标加了曲线, 这里必须跟着改, 不能悄悄跳过。"""
    toks = re.findall(r"[MVHLZmvhlz]" + "|" + NUM, d)
    pts, cmd, i = [], None, 0
    x = y = 0.0
    while i < len(toks):
        t = toks[i]
        if t.upper() in ("M", "V", "H", "L", "Z"):
            cmd = t.upper()
            i += 1
            continue
        if cmd in ("M", "L"):
            x, y = float(t), float(toks[i + 1])
            i += 2
        elif cmd == "H":
            x = float(t)
            i += 1
        elif cmd == "V":
            y = float(t)
            i += 1
        else:
            raise ValueError("expand_path 遇到不支持的 path 命令: %s" % cmd)
        pts.append((x, y))
        cmd = "L" if cmd == "M" else cmd
    return pts


def rust_pts(src, name):
    m = re.search(name + r"\s*:\s*\[\(f32, f32\);\s*\d+\]\s*=\s*\[(.*?)\];", src, re.S)
    if not m:
        return None
    return [(float(a), float(b)) for a, b in
            re.findall(r"\(\s*(%s)\s*,\s*(%s)\s*\)" % (NUM, NUM), m.group(1))]


def rust_nums(src, name):
    m = re.search(name + r"\s*:\s*\(f32, f32, f32, f32\)\s*=\s*\(([^)]*)\)", src)
    return None if not m else tuple(float(v) for v in re.findall(NUM, m.group(1)))


def rust_scalar(src, name):
    m = re.search(name + r"\s*:\s*f32\s*=\s*(%s)" % NUM, src)
    return None if not m else float(m.group(1))


def rust_rgb(src, name):
    m = re.search(name + r"\s*:\s*\(u8, u8, u8\)\s*=\s*\(([^)]*)\)", src)
    return None if not m else tuple(int(v) for v in re.findall(r"\d+", m.group(1)))


def main():
    bad = 0
    marks = MARK_RE.findall(read(MASTER))
    master_sig = signature_of(marks[0]) if marks else None
    if not master_sig or len(marks) != 1 or len(master_sig["paths"]) != 2:
        return fail("母版 icon.svg 解析失败（应为 1 个 <svg> + 1 个 rect + 2 个 path）")

    print("母版 icon.svg")
    print("  rect  " + " ".join(master_sig["rect"]) + "  fill=" + master_sig["rect.fill"])
    for i, p in enumerate(master_sig["paths"]):
        print("  path%d %s" % (i + 1, p[0]))
    print("")

    print("PART A · 矢量落点逐属性比对")
    for label, rel, expect in VECTOR_SITES:
        found, _, count = collect(rel)
        if found is None:
            bad += fail("%s: 解析失败" % label)
            continue
        if count != expect:
            bad += fail("%s: 命中 %d 处标, 期望 %d 处" % (label, count, expect))
            continue
        for idx, body in enumerate(found):
            sig = signature_of(body)
            where = "%s[%d]" % (label, idx + 1)
            if sig is None:
                bad += fail("%s: 抽不出 rect/path 结构" % where)
            elif sig != master_sig:
                diffs = [k for k in master_sig if sig.get(k) != master_sig[k]]
                bad += fail("%s: 与母版不一致 -> %s" % (where, ", ".join(diffs)))
            else:
                print("  ✓ %s 与母版一致（%d 个 path）" % (where, len(sig["paths"])))
    print("")

    print("PART B · 旧标残留扫描")
    # 只扫矢量落点; exe 光栅单独由 PART C 负责(build.rs 里是浮点几何, 与矢量指纹不同源)
    scan = [("icon.svg", MASTER)] + [(l, r) for l, r, _ in VECTOR_SITES]
    for label, rel in scan:
        text = read(rel)
        hits = []
        for pat, what in OLD_MARK_PATTERNS:
            for m in re.finditer(pat, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append("L%d %s(%s)" % (line, what, m.group(0)))
        if hits:
            bad += fail("%s: 残留 %s" % (label, "; ".join(hits[:4])))
        else:
            print("  ✓ %s 无旧标残留" % label)
    print("")

    print("PART C · exe 光栅几何 == 矢量标（逐控制点比对）")
    rs = read(RASTER[1])
    m_rect = attrs_of(RECT_RE.search(marks[0]).group(1))
    m_paths = [attrs_of(b) for b in PATH_RE.findall(marks[0])]
    pairs = [
        ("颜色 LOGO_BLUE", (10, 132, 255), rust_rgb(rs, "LOGO_BLUE")),
        ("颜色 LOGO_FG", (255, 255, 255), rust_rgb(rs, "LOGO_FG")),
        ("矩形 LOGO_BOX vs rect", tuple(float(m_rect[k]) for k in ("x", "y", "width", "height")),
         rust_nums(rs, "LOGO_BOX")),
        ("圆角 LOGO_RX", (float(m_rect["rx"]),), (rust_scalar(rs, "LOGO_RX"),)),
        ("门框线宽 GATE_STROKE", (float(m_paths[0]["stroke-width"]),), (rust_scalar(rs, "GATE_STROKE"),)),
        ("门框折线 GATE_PTS", expand_path(m_paths[0]["d"]), rust_pts(rs, "GATE_PTS")),
        ("箭头多边形 ARROW_PTS", expand_path(m_paths[1]["d"]), rust_pts(rs, "ARROW_PTS")),
    ]
    for label, want_v, got_v in pairs:
        if got_v is None:
            bad += fail("%s: build.rs 里找不到对应常量" % label)
        elif want_v != got_v:
            bad += fail("%s 与母版不一致 | 母版 %s | build.rs %s" % (label, want_v, got_v))
        else:
            print("  ✓ %s 与母版一致" % label)
    for pat, what in OLD_RASTER_PATTERNS:
        if re.search(pat, rs):
            bad += fail("build.rs: 仍是旧光栅 -> %s" % what)
    print("")

    print("PART D · icon.ico 实体检查（尺寸集合 / 采样像素 / 缓存是否吞了改动）")
    ico_path = os.path.join(ROOT, "icon.ico")
    if not os.path.exists(ico_path):
        bad += fail("icon.ico 不存在 —— 跑一次 cargo build 会生成")
    else:
        # build.rs 里那句"icon.ico 比脚本新就跳过"曾让线上图标陈旧好几天(旧靛蓝一直没换掉),
        # 所以这条必须查: 图标比脚本旧 = 改动被缓存吞了。
        if os.path.getmtime(ico_path) < os.path.getmtime(os.path.join(ROOT, RASTER[1])):
            bad += fail("icon.ico 比 build.rs 旧 —— 被『已存在且更新则跳过』吞了, 重跑 cargo build")
        blob = open(ico_path, "rb").read()
        n = struct.unpack_from("<H", blob, 4)[0]
        entries, off = [], 6
        for _ in range(n):
            w, h, _c, _res, _p, bpp, size, data_off = struct.unpack_from("<BBBBHHII", blob, off)
            off += 16
            entries.append((w or 256, h or 256, bpp, size, data_off))
        got_sizes = sorted({e[0] for e in entries})
        m_sizes = re.search(r"ICO_SIZES:\s*\[u32;\s*\d+\]\s*=\s*\[([^\]]*)\]", rs)
        want_sizes = sorted(int(v) for v in re.findall(r"\d+", m_sizes.group(1))) if m_sizes else None
        if want_sizes is None:
            bad += fail("build.rs 里找不到 ICO_SIZES")
        elif got_sizes != want_sizes:
            bad += fail("icon.ico 尺寸集合 %s 与 ICO_SIZES %s 不一致" % (got_sizes, want_sizes))
        else:
            print("  ✓ 含 %d 档尺寸 %s, 与 ICO_SIZES 一致（%d B）" % (len(got_sizes), got_sizes, len(blob)))
        # 抽 32px 那档解几个像素: 证明"exe 图标 == 矢量标"不只几何相同, 画出来也对
        blue = tuple(int(m_rect["fill"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        white = tuple(int(m_paths[0]["stroke"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        e32 = next((e for e in entries if e[0] == 32), None)
        if e32 is None:
            bad += fail("icon.ico 里没有 32×32 档")
        else:
            w, h, _bpp, _size, data_off = e32
            base = data_off + 40          # 跳过 BITMAPINFOHEADER

            def px(x, y):
                i = base + ((h - 1 - y) * w + x) * 4       # DIB 自下而上, BGRA
                return (blob[i + 2], blob[i + 1], blob[i], blob[i + 3])

            for label, xy, want_px in [
                ("圆角外透明", (0, 0), (0, 0, 0, 0)),
                ("底色 = 母版 rect fill", (4, 16), blue + (255,)),
                ("箭杆 = 母版描边色", (16, 16), white + (255,)),
            ]:
                got_px = px(*xy)
                if got_px != want_px:
                    bad += fail("32px %s: 实测 %s, 期望 %s" % (label, got_px, want_px))
                else:
                    print("  ✓ 32px %s" % label)
    print("")

    if bad:
        print("[FAIL] 品牌标同源: %d 项不一致" % bad)
        return 1
    print("[OK] 品牌标同源: 母版 + 5 处矢量落点全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())