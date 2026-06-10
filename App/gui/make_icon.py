#!/usr/bin/env python3
"""Generate an INKERS-branded app icon (PNG).

Output: App/Images/icon.png  (512×512)  +  icon_256.png  +  icon_128.png

The app currently uses App/Images/2739025.png as its launcher icon
(the tripod-scanner illustration). This script generates a fallback
INKERS-branded icon — keep it around in case the bundled icon needs
to be regenerated or restyled.
"""
import os
from PIL import Image, ImageDraw, ImageFont

# Output into the App/Images folder (sibling of this gui/ folder)
_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR  = os.path.join(_GUI_DIR, '..', 'Images')
OUT_DIR  = os.path.abspath(OUT_DIR)

SIZE     = 512
RADIUS   = 92
PRIMARY        = (1,   89,  196)   # #0159C4
PRIMARY_DARK   = (1,   58,  138)   # #013a8a
WHITE          = (255, 255, 255)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    return mask


def _vertical_gradient(size: int, top, bottom) -> Image.Image:
    base = Image.new('RGB', (1, size))
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        base.putpixel((0, y), (r, g, b))
    return base.resize((size, size))


def _scan_arcs(canvas: Image.Image):
    d = ImageDraw.Draw(canvas, 'RGBA')
    cx, cy = SIZE * 0.30, SIZE * 0.62
    arc = (255, 255, 255, 230)
    for radius, width in [(110, 8), (170, 8), (230, 8)]:
        bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        d.arc(bbox, start=290, end=355, fill=arc, width=width)
    r = 18
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)


def _wordmark(canvas: Image.Image, text: str = 'INKERS'):
    font = None
    for path in [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraBold.ttf',
        '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
    ]:
        if os.path.exists(path):
            font = ImageFont.truetype(path, 54)
            break
    if font is None:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(canvas, 'RGBA')
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (SIZE - tw) // 2 - bbox[0]
    y = int(SIZE * 0.80) - bbox[1]
    d.text((x, y), text, fill=(255, 255, 255, 235), font=font)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    grad = _vertical_gradient(SIZE, PRIMARY, PRIMARY_DARK).convert('RGBA')
    _scan_arcs(grad)
    _wordmark(grad)

    out = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(grad, (0, 0), _rounded_mask(SIZE, RADIUS))

    for px, name in [(512, 'icon.png'),
                     (256, 'icon_256.png'),
                     (128, 'icon_128.png')]:
        img = out if px == SIZE else out.resize((px, px), Image.LANCZOS)
        path = os.path.join(OUT_DIR, name)
        img.save(path, 'PNG', optimize=True)
        print(f'wrote {path}')


if __name__ == '__main__':
    main()
