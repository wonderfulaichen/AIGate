# 一次性工装: 把 site/index.html 内联的 PNG data URI 重编码为 WebP, 压小自包含单文件的体积.
# 截图是不透明的 UI 画面, 用 RGB 有损 WebP (q88) —— 文字边缘仍清晰, 体积约为 PNG 的 1/3.
import os
import base64, io, pathlib, re

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
P = pathlib.Path(__file__).resolve().parents[1] / "site" / "index.html"
html = P.read_text(encoding="utf-8")

PAT = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")
cache = {}


def repl(m):
    raw = m.group(1)
    if raw in cache:
        return cache[raw]
    im = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=88, method=6)
    uri = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    cache[raw] = uri
    print(f"{im.size}  {len(raw) // 1024}KB -> {len(uri) // 1024}KB")
    return uri


out = PAT.sub(repl, html)
P.write_text(out, encoding="utf-8")
print("unique imgs:", len(cache))
print("html KB:", len(out.encode("utf-8")) // 1024)