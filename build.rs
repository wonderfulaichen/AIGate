//! 构建脚本 — 生成 .ico 图标 + 版本资源, 并嵌入 exe.
//!
//! 用 embed-resource 编译 `resources.rc` (含图标引用 + VS_VERSION_INFO 文件属性版本),
//! 无需外部工具. 另外注入 `AIGATE_BUILD_TIME` / `AIGATE_GIT_COMMIT` 供 `src/version.rs`
//! 在编译期读取 (离线时为空串).

use chrono::Utc;
use std::process::Command;

fn main() {
    // 生成 icon.ico
    generate_ico("icon.ico");

    // 注入构建元数据 (供 src/version.rs 的 env! 读取)
    let build_time = Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();
    println!("cargo:rustc-env=AIGATE_BUILD_TIME={build_time}");
    println!("cargo:rustc-env=AIGATE_GIT_COMMIT={}", git_commit());

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

/// 生成一个 32×32 的 ICO 图标 (BGRA 无压缩格式).
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

    let w: u32 = 32;
    let h: u32 = 32;

    // 1. 像素数据 (BGRA, 32bpp)
    let mut pixels = vec![0u8; (w * h * 4) as usize];

    for y in 0..h {
        for x in 0..w {
            let idx = ((y * w + x) * 4) as usize;
            let (r, g, b, a) = pixel_color(x, y, w, h);
            pixels[idx + 0] = b; // B
            pixels[idx + 1] = g; // G
            pixels[idx + 2] = r; // R
            pixels[idx + 3] = a; // A
        }
    }

    // 2. BITMAPINFOHEADER (40 bytes)
    let bih_len: u32 = 40;
    let pixel_size = pixels.len() as u32;

    let mut bih = Vec::new();
    bih.extend(&bih_len.to_le_bytes());       // biSize
    bih.extend(&w.to_le_bytes());              // biWidth
    bih.extend(&(h * 2).to_le_bytes());        // biHeight (×2 for ICO)
    bih.extend(&1u16.to_le_bytes());           // biPlanes
    bih.extend(&32u16.to_le_bytes());          // biBitCount
    bih.extend(&0u32.to_le_bytes());           // biCompression (BI_RGB)
    bih.extend(&pixel_size.to_le_bytes());     // biSizeImage
    bih.extend(&0u32.to_le_bytes());           // biXPelsPerMeter
    bih.extend(&0u32.to_le_bytes());           // biYPelsPerMeter
    bih.extend(&0u32.to_le_bytes());           // biClrUsed
    bih.extend(&0u32.to_le_bytes());           // biClrImportant

    // 3. ICO 文件
    let data_offset: u32 = 6 + 16; // header + 1 directory entry
    let _file_size = data_offset + bih_len + pixel_size;

    let mut ico = Vec::new();

    // ICO header
    ico.extend(&0u16.to_le_bytes());            // reserved
    ico.extend(&1u16.to_le_bytes());            // type (1=icon)
    ico.extend(&1u16.to_le_bytes());            // count

    // Directory entry
    ico.push(if w >= 256 { 0 } else { w as u8 }); // width
    ico.push(if h >= 256 { 0 } else { h as u8 }); // height
    ico.push(0);                                   // colors
    ico.push(0);                                   // reserved
    ico.extend(&1u16.to_le_bytes());               // planes
    ico.extend(&32u16.to_le_bytes());              // bpp
    ico.extend(&(bih_len + pixel_size).to_le_bytes()); // size
    ico.extend(&data_offset.to_le_bytes());            // offset

    // BITMAPINFOHEADER + pixel data
    ico.extend(&bih);
    ico.extend(&pixels);

    let _ = std::fs::write(path, &ico);
}

/// 为 (x, y) 坐标返回 (R, G, B, A) 颜色值.
fn pixel_color(x: u32, y: u32, w: u32, h: u32) -> (u8, u8, u8, u8) {
    let cx = w as f32 / 2.0;
    let cy = h as f32 / 2.0;

    // 背景: 紫色渐变 (近似)
    let bg_r = (99.0 + (y as f32 / h as f32) * (79.0 - 99.0)) as u8;
    let bg_g = (102.0 + (y as f32 / h as f32) * (70.0 - 102.0)) as u8;
    let bg_b = (241.0 + (y as f32 / h as f32) * (229.0 - 241.0)) as u8;

    let dist_center = ((x as f32 - cx).powi(2) + (y as f32 - cy).powi(2)).sqrt();

    // 中心白色环: r=6~8
    if dist_center >= 5.5 && dist_center <= 7.5 {
        return (255, 255, 255, 255);
    }
    // 中心紫色圆点: r<4
    if dist_center <= 4.0 {
        return (bg_r, bg_g, bg_b, 255);
    }

    // 4 个连接节点
    let nodes = [(7, 8), (25, 8), (7, 24), (25, 24)];
    for &(nx, ny) in &nodes {
        let nd = (((x as i32 - nx).pow(2) + (y as i32 - ny).pow(2)) as f32).sqrt();
        if nd <= 3.0 {
            return (255, 255, 255, 255);
        }
    }

    // 背景
    (bg_r, bg_g, bg_b, 255)
}
