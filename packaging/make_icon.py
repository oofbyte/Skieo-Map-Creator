"""App icon: wooden tile, snow-capped peak, crimson map pin (drawn here, no game art)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
S = 1024


def icon():
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    wood = Image.open(HERE.parent / 'web' / 'img' / 'wood_h.png').convert('RGB').resize((S, S))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), 170, fill=255)
    im.paste(wood, (0, 0), mask)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((40, 40, S - 40, S - 40), 170, outline=(74, 42, 24, 255), width=34)
    # sky plaque
    d.rounded_rectangle((120, 150, S - 120, S - 150), 90, fill=(120, 170, 205, 255), outline=(74, 42, 24, 255), width=22)
    # mountains
    d.polygon([(140, 820), (430, 380), (560, 560), (690, 330), (900, 820)], fill=(52, 82, 104, 255))
    d.polygon([(690, 330), (770, 470), (735, 455), (700, 500), (660, 450), (620, 480)], fill=(246, 249, 252, 255))
    d.polygon([(430, 380), (495, 480), (462, 470), (430, 505), (398, 470), (372, 470)], fill=(246, 249, 252, 255))
    d.rectangle((130, 780, S - 130, 880), fill=(240, 245, 250, 255))
    # pin
    cx, cy, r = 700, 470, 120
    pin = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    p = ImageDraw.Draw(pin)
    p.ellipse((cx - r, cy - r - 80, cx + r, cy + r - 80), fill=(179, 38, 61, 255))
    p.polygon([(cx - r * 0.86, cy - 20), (cx + r * 0.86, cy - 20), (cx, cy + 190)], fill=(179, 38, 61, 255))
    p.ellipse((cx - 50, cy - 130, cx + 50, cy - 30), fill=(248, 240, 226, 255))
    shadow = pin.split()[3].filter(ImageFilter.GaussianBlur(14))
    sh = Image.new('RGBA', (S, S), (20, 10, 5, 140))
    im.paste(sh, (10, 18), shadow)
    im.alpha_composite(pin)
    return im


if __name__ == '__main__':
    im = icon()
    im.save(HERE / 'icon.png')
    im.save(HERE / 'icon.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print('icon written')
