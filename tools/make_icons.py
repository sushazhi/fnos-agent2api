#!/usr/bin/env python3
"""从一张主图生成飞牛应用要用的四个图标文件。

用法:
    python tools/make_icons.py <主图.png>
    python tools/make_icons.py                # 默认用 .local-build/source.png

产出（按中心方形裁剪 + LANCZOS 缩放）:
    ICON.PNG                      64x64    飞牛应用列表图标
    ICON_256.PNG                  256x256  飞牛应用详情图标
    app/ui/images/icon_64.png     64x64    面板内用
    app/ui/images/icon_256.png    256x256  面板内用

主图建议 1024x1024 正方形；非正方形会以中心裁剪成正方形，四周可能被切掉。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from PIL import Image
except ImportError:
    print("ERROR: 需要 Pillow。请先 `pip install Pillow`。", file=sys.stderr)
    sys.exit(1)

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OUTPUTS = (
    (os.path.join(PROJ, "ICON.PNG"), 64),
    (os.path.join(PROJ, "ICON_256.PNG"), 256),
    (os.path.join(PROJ, "app", "ui", "images", "icon_64.png"), 64),
    (os.path.join(PROJ, "app", "ui", "images", "icon_256.png"), 256),
)


def main():
    if len(sys.argv) > 1:
        src = os.path.abspath(sys.argv[1])
    else:
        src = os.path.join(PROJ, ".local-build", "source.png")

    if not os.path.isfile(src):
        print(f"ERROR: 找不到主图: {src}", file=sys.stderr)
        print("       用法: python tools/make_icons.py <主图.png>", file=sys.stderr)
        sys.exit(1)

    im = Image.open(src).convert("RGBA")
    print(f"主图: {src}")
    print(f"      {im.size[0]}x{im.size[1]} {im.mode}  {os.path.getsize(src):,} B")

    w, h = im.size
    side = min(w, h)
    if w != h:
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        print(f"      中心裁剪为 {im.size[0]}x{im.size[1]}（原图非正方形）")
    if side < 256:
        print(f"WARNING: 主图仅 {side}px，放大到 256 会糊。建议用 >= 256px 的图。")

    for path, size in OUTPUTS:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        im.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
        rel = os.path.relpath(path, PROJ).replace(os.sep, "/")
        print(f"  wrote {rel:32s} {size}x{size}  {os.path.getsize(path):,} B")


if __name__ == "__main__":
    main()
