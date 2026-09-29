# -*- coding: utf-8 -*-
"""AIGate 项目介绍视频合成: PIL 逐帧生成 -> ffmpeg (stdin pipe) -> MP4。

为什么逐帧生成而不是用 ffmpeg 的 xfade 串联:
  11 个场景要串 10 次 xfade, filter graph 会难读且难调; 逐帧生成把转场/Ken Burns/字幕
  收在一处, 随时能抽单帧核对 (视频出问题最难的就是"哪一帧错了")。

为什么走 stdin pipe 而不是落 PNG 序列:
  90s x 30fps = 2700 帧 1080p, 落盘约 3GB 且 IO 慢; pipe 出去内存里转一圈就没了。

素材 (均为已核对过的产物):
  clip_frames/c%04d.png   开场概念镜头 (GenerateVideo, 转 30fps/1080p, 241 帧)
  deck_hd/pNN.png         演示稿页 (1920x1080, 已隐藏交互残留)
  panel/live_overview.png 真实面板概览 (在线实例截图)

用法: python make_video.py
"""
import io
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
VDIR = os.path.join(ROOT, ".openbitfun", "tmp", "video")
OUT = os.path.join(VDIR, "aigate-intro.mp4")

FPS = 30
W, H = 1920, 1080
XFADE = 0.6          # 交叉淡化时长(秒); 不额外增加片长 —— 它占用新场景的前 XFADE 秒
FADE_OUT = 0.7       # 全片末尾淡黑

# 字幕: 只用在开场概念镜头段 (那段没有文字载体)。
# 文案取自 deck 封面主张**原文**, 不新造句子 (项目红线: 新文案要先给用户过目)。
SUB_TEXT = "把重复的上下文，不再重复传一遍"
SUB_IN, SUB_FULL, SUB_OUT, SUB_END = 3.0, 3.6, 6.4, 7.0

# 时间轴: (时长, 类型, 素材名)。时长之和 = 片长。
SCENES = [
    (8, "clip", "clip"),
    (8, "img", "p01"),      # 封面
    (8, "img", "p02"),      # 痛点
    (8, "img", "p03"),      # 主张
    (8, "img", "p04"),      # 方案
    (8, "img", "p05"),      # 协议
    (7, "img", "p09"),      # 量级
    (9, "img", "p12"),      # 三手段
    (14, "img", "live"),    # 真实面板
    (8, "img", "p17"),      # 交付
    (4, "img", "p18"),      # 三步开始
]

KB = 1.04               # Ken Burns 最末放大倍数 (1.04 = 缓慢推近 4%)


def find_ffmpeg():
    """借用的 ffmpeg 优先, 其次 PATH。找不到就明确报错, 不做静默降级。"""
    for p in (r"C:\Program Files (x86)\Lenovo\LegionZone\2.0.30.9243\SEGamingAI\services\editor\ffmpeg.exe",
              shutil.which("ffmpeg")):
        if p and os.path.exists(p):
            return p
    raise SystemExit("找不到 ffmpeg —— 脚本依赖它编码; 安装 ffmpeg 或修正 find_ffmpeg() 里的路径")


def ensure_clip_frames(ff):
    """首次运行时把开场镜头提成 30fps/1080p 帧序列, 让脚本自洽 (不依赖手工前置步骤)。
    已存在则跳过 —— 241 帧重新提取要十几秒, 反复调参时没必要每次都付。"""
    d = os.path.join(VDIR, "clip_frames")
    if os.path.isdir(d) and [f for f in os.listdir(d) if f.startswith("c")]:
        return
    os.makedirs(d, exist_ok=True)
    print("提取开场镜头帧 (30fps / 1080p) ...")
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", os.path.join(VDIR, "clip_intro.mp4"),
                    "-vf", "fps=%d,scale=%d:%d:flags=lanczos" % (FPS, W, H),
                    "-an", "-f", "image2", os.path.join(d, "c%04d.png")], check=True)


def load_font(size):
    for p in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise SystemExit("找不到中文字体 (字幕需要), 请检查 C:\\Windows\\Fonts")


def load_stills():
    """预加载静图场景 (10 张 1080p 约 60MB, 直接常驻内存比反复解码划算)。
    统一放大到 KB 倍 —— 给 Ken Burns 留出裁剪余量, 否则推近时会被正中间裁掉字。"""
    imgs = {}
    for _, kind, name in SCENES:
        if kind != "img":
            continue
        path = os.path.join(VDIR, "panel", "live_overview.png") if name == "live" \
            else os.path.join(VDIR, "deck_hd", name + ".png")
        im = Image.open(path).convert("RGB")
        if im.size != (W, H):
            im = im.resize((W, H), Image.LANCZOS)
        imgs[name] = im.resize((int(W * KB), int(H * KB)), Image.LANCZOS)
    return imgs


def ken_burns(big, progress):
    """按进度从放大版里居中裁一块再缩回 1080p —— 即缓慢推近。
    裁剪窗口从整幅(第 0 帧)收到 1/KB(最后一帧), 全程连续无跳变。"""
    bw, bh = big.size
    s = 1.0 + (KB - 1.0) * progress
    cw, ch = int(bw / s), int(bh / s)
    left, top = (bw - cw) // 2, (bh - ch) // 2
    return big.crop((left, top, left + cw, top + ch)).resize((W, H), Image.LANCZOS)


def clip_frame(progress, total):
    """取开场镜头的第 k 帧。按需读盘 —— 241 帧全缓存要 340MB, 不值当。"""
    k = min(total, max(1, int(progress * total) + 1))
    return Image.open(os.path.join(VDIR, "clip_frames", "c%04d.png" % k)).convert("RGB")


def sub_alpha(t):
    """字幕不透明度曲线: 淡入 -> 保持 -> 淡出。"""
    if t < SUB_IN or t >= SUB_END:
        return 0.0
    if t < SUB_FULL:
        return (t - SUB_IN) / (SUB_FULL - SUB_IN)
    if t < SUB_OUT:
        return 1.0
    return max(0.0, (SUB_END - t) / (SUB_END - SUB_OUT))


def draw_subtitle(img, font, alpha):
    """把字幕烧进帧。用描边而不是底框 —— 深色画面上更干净, 也不挡住画面主体。"""
    if alpha <= 0:
        return img
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    tw, th = dr.textsize(SUB_TEXT, font=font)
    x, y = (W - tw) // 2, H - 168
    dr.text((x, y), SUB_TEXT, font=font, fill=(255, 255, 255, int(255 * alpha)),
            stroke_width=3, stroke_fill=(0, 0, 0, int(200 * alpha)))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def build_timeline():
    """算出每个场景的起始秒。返回 [(start, dur, kind, name)]。"""
    out, acc = [], 0.0
    for dur, kind, name in SCENES:
        out.append((acc, float(dur), kind, name))
        acc += dur
    return out, acc


def render_scene(tl, idx, progress, stills, nclip):
    start, dur, kind, name = tl[idx]
    if kind == "clip":
        return clip_frame(progress, nclip)
    return ken_burns(stills[name], progress)


def frame_at(t, tl, total, stills, nclip):
    """生成第 t 秒的画面。转场期: 新场景前 XFADE 秒与其前一个场景的**定格末态**混合。"""
    idx = len(tl) - 1
    for k, (start, dur, _, _) in enumerate(tl):
        if start <= t < start + dur:
            idx = k
            break
    start, dur = tl[idx][0], tl[idx][1]
    cur = render_scene(tl, idx, min(1.0, (t - start) / dur), stills, nclip)
    if idx > 0 and t < start + XFADE:
        prev = render_scene(tl, idx - 1, 1.0, stills, nclip)
        cur = Image.blend(prev, cur, (t - start) / XFADE)
    if t > total - FADE_OUT:                      # 全片末尾淡黑
        a = max(0.0, (total - t) / FADE_OUT)
        cur = Image.blend(Image.new("RGB", cur.size, (0, 0, 0)), cur, a)
    return cur


def main():
    ff = find_ffmpeg()
    ensure_clip_frames(ff)
    font = load_font(46)
    stills = load_stills()
    tl, total = build_timeline()
    nclip = len([f for f in os.listdir(os.path.join(VDIR, "clip_frames")) if f.startswith("c")])
    nframes = int(round(total * FPS))
    print("片长 %.1fs / %d 帧 / %d 场景 / ffmpeg=%s" % (total, nframes, len(tl), os.path.basename(ff)))

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-",
           "-an", "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fi in range(nframes):
        t = fi / FPS
        img = frame_at(t, tl, total, stills, nclip)
        img = draw_subtitle(img, font, sub_alpha(t))
        proc.stdin.write(img.tobytes())
        if fi % 150 == 0:
            print("  %5d/%d  (%.1fs)" % (fi, nframes, t))
    proc.stdin.close()
    rc = proc.wait()
    if rc != 0 or not os.path.exists(OUT):
        raise SystemExit("ffmpeg 编码失败 rc=%d" % rc)
    print("完成: %s  (%.1f MB)" % (OUT, os.path.getsize(OUT) / 1048576))


if __name__ == "__main__":
    main()