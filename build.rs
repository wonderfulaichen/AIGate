//! 构建脚本 — 生成 .ico 图标 + 版本资源, 并嵌入 exe.
//!
//! 用 embed-resource 编译 `resources.rc` (含图标引用 + VS_VERSION_INFO 文件属性版本),
//! 无需外部工具. 另外注入 `AIGATE_BUILD_TIME` / `AIGATE_GIT_COMMIT` 供 `src/version.rs`
//! 在编译期读取 (离线时为空串).

use chrono::Utc;
use std::process::Command;

fn main() {
    // i18n 构建门禁: src/admin/i18n.js 中英双表 重复键 / 缺键 检查 (失败即编译报错).
    // 由 check_i18n.cjs 的字符串字面量感知扫描移植而来 — 手工脚本升级为不可绕过的门禁.
    check_i18n_tables();

    // 生成 icon.ico
    generate_ico("icon.ico");

    // 注入构建元数据 (供 src/version.rs 的 env! 读取)
    let build_time = Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();
    println!("cargo:rustc-env=AIGATE_BUILD_TIME={build_time}");
    println!("cargo:rustc-env=AIGATE_GIT_COMMIT={}", git_commit());
    // 显式声明重跑条件: 一旦声明任何 rerun-if-changed, cargo 默认的
    // "包内任一文件变更即重跑" 就会失效, 故需把原有隐式依赖补全.
    // 追踪 .git/logs/HEAD (reflog) 而非 .git/HEAD — commit 只追加 reflog,
    // 不会重写 HEAD 文件 (其 mtime 停留在上次分支切换), 追踪后者等于没追:
    // 实际踩过: commit 后首次构建复用缓存输出, 部署出去的横幅是旧 commit 号.
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=Cargo.toml");
    println!("cargo:rerun-if-changed=.git/logs/HEAD");

    // 生成并编译版本资源 (含图标 + 文件属性版本号).
    // 版本号来自 Cargo.toml 单一真相源; 不含 build_time/commit, 避免每次重链.
    write_version_rc("resources.rc", env!("CARGO_PKG_VERSION"));
    embed_resource::compile("resources.rc", embed_resource::NONE);
}

/// 取当前 git 短 commit hash; 离线 / 无 git 时返回空串.
fn git_commit() -> String {
    Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// i18n 构建门禁: 解析 `src/admin/i18n.js` 的 `I18N` 双表, 校验
/// ① 语言表必须恰好存在 zh-CN / en-US 且都在
/// ② 表内无重复键 (后定义覆盖前定义会静默产生死文案)
/// ③ 中英键集必须一致 (缺键会让该语言回退显示另一种语言)
///
/// 扫描器为字符串/注释感知 —— 中英表大量同行内联多键 (`'a': 'x', 'b': 'y',`),
/// 按行正则抓键会漏掉近 2/3 (HANDOFF 7.5 的实测教训), 故此处用状态机逐字符解析.
/// 失败即 panic, 使编译失败 —— 手工脚本升级为不可绕过的门禁.
fn check_i18n_tables() {
    use std::collections::{HashMap, HashSet};

    const PATH: &str = "src/admin/i18n.js";
    let src = std::fs::read_to_string(PATH)
        .unwrap_or_else(|e| panic!("i18n 门禁: 读取 {PATH} 失败: {e}"));
    let anchor = "const I18N = {";
    let start = src
        .find(anchor)
        .unwrap_or_else(|| panic!("i18n 门禁: {PATH} 中找不到 `{anchor}`"));

    // 从 anchor 起按字符扫描 (行号定位用)
    let base_line = 1 + src[..start].chars().filter(|&c| c == '\n').count();
    let chars: Vec<char> = src[start..].chars().collect();
    let line_of = |i: usize| -> usize {
        base_line + chars[..i].iter().filter(|&&c| c == '\n').count()
    };

    // 字符串/模板字面量在 Normal 态一次性原子跳过 (含键位探测), 故无需 Sq/Dq/Tpl 持续态;
    // 只有注释需要跨字符续扫.
    enum St {
        Normal,
        Line,
        Block,
    }
    let mut st = St::Normal;
    let mut depth: usize = 0;
    let mut lang: Option<String> = None;
    let mut tables: HashMap<String, HashSet<String>> = HashMap::new();
    let mut errors: Vec<String> = Vec::new();

    // 定位 I18N 对象的起始 `{`
    let mut i = chars.iter().position(|&c| c == '{').expect("no '{'");
    let mut done = false;
    while i < chars.len() && !done {
        let c = chars[i];
        let next = chars.get(i + 1).copied().unwrap_or('\0');
        match st {
            St::Line => {
                if c == '\n' {
                    st = St::Normal;
                }
                i += 1;
                continue;
            }
            St::Block => {
                if c == '*' && next == '/' {
                    st = St::Normal;
                    i += 2;
                } else {
                    i += 1;
                }
                continue;
            }
            St::Normal => {}
        }
        // ---- Normal ----
        if c == '/' && next == '/' {
            st = St::Line;
            i += 2;
            continue;
        }
        if c == '/' && next == '*' {
            st = St::Block;
            i += 2;
            continue;
        }
        // 模板字面量: 非键, 原子跳过 (不解析 ${} 插值 —— I18N 值域为纯文本)
        if c == '`' {
            let mut k = i + 1;
            while k < chars.len() {
                if chars[k] == '\\' {
                    k += 2;
                    continue;
                }
                if chars[k] == '`' {
                    break;
                }
                k += 1;
            }
            i = (k + 1).min(chars.len());
            continue;
        }
        // try key: 字符串字面量 或 标识符, 后跟 `:` 即键
        let mut lit: Option<String> = None;
        let mut j0 = i;
        if c == '\'' || c == '"' {
            let q = c;
            let mut k = i + 1;
            let mut s = String::new();
            while k < chars.len() {
                let ch = chars[k];
                if ch == '\\' {
                    if k + 1 < chars.len() {
                        s.push(chars[k + 1]);
                    }
                    k += 2;
                    continue;
                }
                if ch == q {
                    break;
                }
                s.push(ch);
                k += 1;
            }
            if k < chars.len() {
                lit = Some(s);
                j0 = k + 1; // 闭引号之后
            }
        } else if c.is_ascii_alphabetic() || c == '_' || c == '$' {
            let mut k = i;
            while k < chars.len()
                && (chars[k].is_ascii_alphanumeric() || chars[k] == '_' || chars[k] == '$')
            {
                k += 1;
            }
            lit = Some(chars[i..k].iter().collect());
            j0 = k;
        }
        match lit {
            None => {
                if c == '{' {
                    depth += 1;
                } else if c == '}' {
                    depth = depth.saturating_sub(1);
                    if depth == 1 {
                        lang = None; // 语言表闭合
                    }
                    if depth == 0 {
                        done = true; // I18N 闭合
                    }
                }
                i += 1;
            }
            Some(key) => {
                // 跳过空白找 `:`
                let mut j = j0;
                while j < chars.len() && chars[j].is_whitespace() {
                    j += 1;
                }
                if j < chars.len() && chars[j] == ':' {
                    // 找值起点
                    let mut v = j + 1;
                    while v < chars.len() && chars[v].is_whitespace() {
                        v += 1;
                    }
                    if v < chars.len() && chars[v] == '{' {
                        // 对象值: 深度 1 处即语言表
                        if depth == 1 {
                            lang = Some(key);
                            tables.entry(lang.clone().unwrap()).or_default();
                        }
                        depth += 1;
                        i = v + 1;
                        continue;
                    }
                    // 标量值: 在语言表内 (深度 2) 则记录该键
                    if depth == 2 {
                        if let Some(l) = &lang {
                            let t = tables.entry(l.clone()).or_default();
                            if !t.insert(key.clone()) {
                                errors.push(format!(
                                    "{PATH} 第 {} 行: 表 `{l}` 重复键 '{key}' (后定义覆盖前定义, 前者成死文案)",
                                    line_of(i)
                                ));
                            }
                        }
                    }
                    i = j0; // 继续扫值本身
                    continue;
                }
                // 不是键 (是值), 整体跳过该字面量
                i = j0;
            }
        }
    }

    // ① 双表存在
    for required in ["zh-CN", "en-US"] {
        if !tables.contains_key(required) {
            errors.push(format!("{PATH}: 缺少语言表 `{required}`"));
        }
    }
    // ③ 键集一致 (以 zh-CN 为基准双向对比)
    if let (Some(zh), Some(en)) = (tables.get("zh-CN"), tables.get("en-US")) {
        let mut miss_en: Vec<&String> = zh.difference(en).collect();
        let mut miss_zh: Vec<&String> = en.difference(zh).collect();
        miss_en.sort();
        miss_zh.sort();
        let fmt = |v: &[&String]| -> String {
            v.iter()
                .take(20)
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        };
        if !miss_en.is_empty() {
            errors.push(format!(
                "{PATH}: zh-CN 有而 en-US 缺 {} 键: {}{}",
                miss_en.len(),
                fmt(&miss_en),
                if miss_en.len() > 20 { ", …" } else { "" }
            ));
        }
        if !miss_zh.is_empty() {
            errors.push(format!(
                "{PATH}: en-US 有而 zh-CN 缺 {} 键: {}{}",
                miss_zh.len(),
                fmt(&miss_zh),
                if miss_zh.len() > 20 { ", …" } else { "" }
            ));
        }
    }

    if !errors.is_empty() {
        panic!(
            "i18n 构建门禁失败 ({} 处):\n  {}\n修复后才能编译 —— 中英双表必须同步 (重复键/缺键都会让面板显示错文案/原文).",
            errors.len(),
            errors.join("\n  ")
        );
    }
    let (nz, ne) = (
        tables.get("zh-CN").map_or(0, |s| s.len()),
        tables.get("en-US").map_or(0, |s| s.len()),
    );
    println!("cargo:rerun-if-changed={PATH}");
    eprintln!("i18n 门禁通过: zh-CN {nz} 键 / en-US {ne} 键, 无重复无缺键");
}

/// 写出 `resources.rc`: 引用已生成的 icon.ico, 并声明文件属性版本信息.
fn write_version_rc(path: &str, version: &str) {
    let parts: Vec<u16> = version
        .split('.')
        .map(|p| p.parse::<u16>().unwrap_or(0))
        .collect();
    let v0 = parts.first().copied().unwrap_or(0);
    let v1 = parts.get(1).copied().unwrap_or(0);
    let v2 = parts.get(2).copied().unwrap_or(0);

    let rc = format!(
        "1 ICON \"icon.ico\"\n\n\
         1 VERSIONINFO\n\
         FILEVERSION {v0},{v1},{v2},0\n\
         PRODUCTVERSION {v0},{v1},{v2},0\n\
         FILEOS 0x40004\n\
         FILETYPE 0x1\n\
         BEGIN\n\
             BLOCK \"StringFileInfo\"\n\
             BEGIN\n\
                 BLOCK \"040904B0\"\n\
                 BEGIN\n\
                     VALUE \"FileDescription\", \"AIGate Local AI Gateway\"\n\
                     VALUE \"FileVersion\", \"{version}\"\n\
                     VALUE \"ProductVersion\", \"{version}\"\n\
                     VALUE \"ProductName\", \"AIGate\"\n\
                     VALUE \"LegalCopyright\", \"GPL-3.0\"\n\
                 END\n\
             END\n\
             BLOCK \"VarFileInfo\"\n\
             BEGIN\n\
                 VALUE \"Translation\", 0x409, 1200\n\
             END\n\
         END\n"
    );
    let _ = std::fs::write(path, rc);
}

/// ICO 里带的尺寸 —— 对齐 Windows 实际会取用的档位:
/// 资源管理器小图标 16/24、任务栏与中图标 32/48、Alt+Tab 与大图标 64/128。
/// ⚠️ **刻意不含 256**: BMP 存一张 256 就是 256 KB, 整份 ICO 会从 ~99 KB 涨到 ~350 KB, 而 256
/// 只在资源管理器"超大图标"里出现一次 (128 放大一档观感可接受)。要补它就得给大尺寸走 PNG,
/// 那需要在 build-dependencies 里加 png/miniz_oxide(Cargo.lock 里已有, 属传递依赖) 或手写
/// deflate —— 本轮不引入, 留作明确的可选项。
const ICO_SIZES: [u32; 6] = [16, 24, 32, 48, 64, 128];

/// 生成 ICO 图标: 每档尺寸各一张 32bpp BGRA 位图, 布局 = 文件头 + N 个目录项 + N 段位图。
/// 图案 = 品牌标「闸门通路」, 几何见下方品牌标几何常量 (与 icon.svg 同源)。
/// 圆角外为**透明**(不是填色) —— 否则任务栏里显示的是个方块。
fn generate_ico(path: &str) {
    // 如果已存在且比脚本新则跳过
    if let (Ok(meta), Ok(src_meta)) = (
        std::fs::metadata(path),
        std::fs::metadata("build.rs"),
    ) {
        if meta.modified().ok() >= src_meta.modified().ok() {
            return;
        }
    }

    let images: Vec<(u32, u32, Vec<u8>)> = ICO_SIZES
        .iter()
        .map(|&s| (s, s, bgra_bitmap(s, s)))
        .collect();

    // 文件头: reserved / type=1(icon) / count
    let mut ico = Vec::new();
    ico.extend(&0u16.to_le_bytes());
    ico.extend(&1u16.to_le_bytes());
    ico.extend(&(images.len() as u16).to_le_bytes());

    // 目录项 (16 字节/档): 尺寸、色数、位深、数据长度与偏移
    let mut offset = 6 + 16 * images.len() as u32;
    for (w, h, pixels) in &images {
        let blob = 40 + pixels.len() as u32;
        ico.push(if *w >= 256 { 0 } else { *w as u8 }); // 0 表示 256
        ico.push(if *h >= 256 { 0 } else { *h as u8 });
        ico.push(0);                                    // 调色板色数 (32bpp 不用)
        ico.push(0);                                    // reserved
        ico.extend(&1u16.to_le_bytes());                // planes
        ico.extend(&32u16.to_le_bytes());               // bpp
        ico.extend(&blob.to_le_bytes());
        ico.extend(&offset.to_le_bytes());
        offset += blob;
    }

    // 位图数据: BITMAPINFOHEADER + 像素
    for (w, h, pixels) in &images {
        ico.extend(dib_header(*w, *h, pixels.len() as u32));
        ico.extend(pixels);
    }

    let _ = std::fs::write(path, &ico);
}

/// BITMAPINFOHEADER (40 字节)。biHeight 要 ×2 —— ICO 约定: DIB 高度含下半部的 AND 掩码区,
/// 32bpp 已带 alpha 故不写掩码数据, 但高度仍按约定翻倍。
fn dib_header(w: u32, h: u32, pixel_size: u32) -> Vec<u8> {
    let mut bih = Vec::with_capacity(40);
    bih.extend(&40u32.to_le_bytes());       // biSize
    bih.extend(&w.to_le_bytes());           // biWidth
    bih.extend(&(h * 2).to_le_bytes());     // biHeight (×2 for ICO)
    bih.extend(&1u16.to_le_bytes());        // biPlanes
    bih.extend(&32u16.to_le_bytes());       // biBitCount
    bih.extend(&0u32.to_le_bytes());        // biCompression (BI_RGB)
    bih.extend(&pixel_size.to_le_bytes());  // biSizeImage
    bih.extend(&0u32.to_le_bytes());        // biXPelsPerMeter
    bih.extend(&0u32.to_le_bytes());        // biYPelsPerMeter
    bih.extend(&0u32.to_le_bytes());        // biClrUsed
    bih.extend(&0u32.to_le_bytes());        // biClrImportant
    bih
}

/// 一档尺寸的 BGRA 位图。⚠️ DIB 的行序是**自下而上**(BMP 系惯例), 故这里倒着写行 ——
/// 本标上下对称、写反了也看不出来, 但那是踩坑的温床, 一律按规范写。
fn bgra_bitmap(w: u32, h: u32) -> Vec<u8> {
    let mut pixels = vec![0u8; (w * h * 4) as usize];
    for y in 0..h {
        let src_y = h - 1 - y;
        for x in 0..w {
            let idx = ((y * w + x) * 4) as usize;
            let (r, g, b, a) = pixel_color(x, src_y, w, h);
            pixels[idx] = b;
            pixels[idx + 1] = g;
            pixels[idx + 2] = r;
            pixels[idx + 3] = a;
        }
    }
    pixels
}

// ── 品牌标几何: 与 icon.svg **同一套 256 空间数字** ──
// 母版 icon.svg / 面板 core.js / site / deck 共用同一套形状, 这里是它在 exe 图标上的
// **同源还原**(不是"另一个版本"): 坐标系与 SVG 完全一致, 采样时除以缩放即可
// (32px 图标 = 256/8)。校验:
//     python .openbitfun/tmp/verify_logo.py     # 逐点比对本地常量与母版 path
const LOGO_BLUE: (u8, u8, u8) = (10, 132, 255);                  // #0a84ff = Apple systemBlue
const LOGO_FG: (u8, u8, u8) = (255, 255, 255);
const LOGO_BOX: (f32, f32, f32, f32) = (8.0, 8.0, 240.0, 240.0); // x y w h
const LOGO_RX: f32 = 56.0;                                       // 圆角半径 (占 23%)
const GATE_STROKE: f32 = 20.0;                                   // 门框线宽 (箭头 32 更粗, 主次靠线宽拉开)
/// 门框折线: 上臂 → 左柱 → 下臂, 右缘敞开
const GATE_PTS: [(f32, f32); 4] = [(148.0, 56.0), (52.0, 56.0), (52.0, 200.0), (148.0, 200.0)];
/// 箭头填充多边形: 杆 y112–144 由 x104 贯到 176, 头 apex (220,128) 两翼落 y104/152
const ARROW_PTS: [(f32, f32); 7] = [
    (104.0, 112.0),
    (176.0, 112.0),
    (176.0, 104.0),
    (220.0, 128.0),
    (176.0, 152.0),
    (176.0, 144.0),
    (104.0, 144.0),
];

/// 采样一个**子像素**的颜色 (256 空间)。返回 (r, g, b, a), a 只取 0/1。
fn logo_sample(sx: f32, sy: f32) -> (f32, f32, f32, f32) {
    // 圆角矩形 SDF: >0 在圆角外 → 透明 (旧版整块不透明, 圆角等于没画)
    let half_w = LOGO_BOX.2 * 0.5;
    let half_h = LOGO_BOX.3 * 0.5;
    let dx = (sx - (LOGO_BOX.0 + half_w)).abs() - (half_w - LOGO_RX);
    let dy = (sy - (LOGO_BOX.1 + half_h)).abs() - (half_h - LOGO_RX);
    let outside = (dx.max(0.0).powi(2) + dy.max(0.0).powi(2)).sqrt();
    if outside + dx.max(dy).min(0.0) - LOGO_RX > 0.0 {
        return (0.0, 0.0, 0.0, 0.0);
    }
    // 门框: 到折线的距离 <= 半线宽即落在笔画上 (线段距离天然就是 SVG 的 round 线帽)
    if dist_to_polyline(sx, sy, &GATE_PTS) <= GATE_STROKE * 0.5 || point_in_poly(sx, sy, &ARROW_PTS) {
        return (LOGO_FG.0 as f32, LOGO_FG.1 as f32, LOGO_FG.2 as f32, 1.0);
    }
    (LOGO_BLUE.0 as f32, LOGO_BLUE.1 as f32, LOGO_BLUE.2 as f32, 1.0)
}

/// 点到折线的最短距离
fn dist_to_polyline(px: f32, py: f32, pts: &[(f32, f32)]) -> f32 {
    let mut best = f32::MAX;
    for w in pts.windows(2) {
        let (x1, y1) = w[0];
        let (vx, vy) = (w[1].0 - x1, w[1].1 - y1);
        let len2 = vx * vx + vy * vy;
        let t = if len2 > 0.0 {
            (((px - x1) * vx + (py - y1) * vy) / len2).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let d = ((px - (x1 + t * vx)).powi(2) + (py - (y1 + t * vy)).powi(2)).sqrt();
        if d < best {
            best = d;
        }
    }
    best
}

/// 射线法判点是否落在填充多边形内
fn point_in_poly(px: f32, py: f32, pts: &[(f32, f32)]) -> bool {
    let mut inside = false;
    let mut j = pts.len() - 1;
    for i in 0..pts.len() {
        let (xi, yi) = pts[i];
        let (xj, yj) = pts[j];
        if (yi > py) != (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// 一个像素的颜色。**3×3 超采样**按覆盖率算 alpha —— 旧版画的是圆环(曲率小, 锯齿不显眼),
/// 换成圆角 + 箭头斜边后不抗锯齿会明显拉毛, 故这里必须超采样。
/// 返回直通 alpha (ICO 32bpp 用直通, 不是预乘)。
fn pixel_color(x: u32, y: u32, w: u32, h: u32) -> (u8, u8, u8, u8) {
    const SS: u32 = 3;
    let kx = 256.0 / w as f32;
    let ky = 256.0 / h as f32;
    let mut acc = [0.0f32; 3];
    let mut cov = 0.0f32;
    for sy in 0..SS {
        for sx in 0..SS {
            let px = (x as f32 + (sx as f32 + 0.5) / SS as f32) * kx;
            let py = (y as f32 + (sy as f32 + 0.5) / SS as f32) * ky;
            let s = logo_sample(px, py);
            if s.3 > 0.0 {
                acc[0] += s.0;
                acc[1] += s.1;
                acc[2] += s.2;
                cov += 1.0;
            }
        }
    }
    if cov == 0.0 {
        return (0, 0, 0, 0);
    }
    let n = (SS * SS) as f32;
    (
        (acc[0] / cov).round() as u8,
        (acc[1] / cov).round() as u8,
        (acc[2] / cov).round() as u8,
        (cov / n * 255.0).round() as u8,
    )
}
