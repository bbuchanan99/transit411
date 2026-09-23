#!/usr/bin/env python3
"""
Branded house graphics - one per pillar, in the Dispatch look.

These are the safe fallback wherever a post has no usable picture: the article page, section lists
and The Wire's lead slot. They are ours, so there is no copyright question, and they keep the site
looking deliberate rather than patchy.

Run once (in a container that has a TrueType font) and commit the PNGs:
  docker compose run --rm --entrypoint sh command-center -c \
    "apt-get update -qq && apt-get install -y -qq fonts-dejavu-core && python tools/make_house_images.py /out"
"""
import os
import sys

W, H = 1200, 630
INK = (23, 20, 15)
PAPER = (247, 244, 237)
RED = (192, 52, 31)
FLAME = (238, 106, 84)
MUTED = (106, 100, 88)
LINE = (216, 210, 196)

# pillar -> (label, motif) where the motif is drawn as a simple transit-ish diagram
PILLARS = {
    "funding": "Funding",
    "procurement": "Procurement",
    "people": "People",
    "policy": "Policy",
    "data": "Data",
    "news": "News",
}
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font(size):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def motif(d, key):
    """A light geometric mark per pillar - route lines, a grid, a rising bar, a stop pattern."""
    y0 = 300
    if key == "funding":
        for i, h in enumerate((70, 120, 170, 230)):
            d.rectangle([760 + i * 90, y0 + 240 - h, 760 + i * 90 + 54, y0 + 240], fill=FLAME if i == 3 else LINE)
    elif key == "procurement":
        for i in range(4):
            for j in range(3):
                d.rectangle([760 + i * 90, y0 + j * 80, 760 + i * 90 + 54, y0 + j * 80 + 54],
                            outline=LINE, width=6, fill=FLAME if (i + j) == 4 else None)
    elif key == "people":
        for i in range(3):
            cx = 800 + i * 120
            d.ellipse([cx - 34, y0 + 10, cx + 34, y0 + 78], fill=FLAME if i == 1 else LINE)
            d.rectangle([cx - 52, y0 + 96, cx + 52, y0 + 210], fill=FLAME if i == 1 else LINE)
    elif key == "policy":
        d.rectangle([760, y0 + 30, 1120, y0 + 46], fill=LINE)
        d.rectangle([760, y0 + 100, 1120, y0 + 116], fill=FLAME)
        d.rectangle([760, y0 + 170, 980, y0 + 186], fill=LINE)
    elif key == "data":
        pts = [(770, y0 + 210), (850, y0 + 140), (930, y0 + 170), (1010, y0 + 70), (1110, y0 + 30)]
        d.line(pts, fill=FLAME, width=10, joint="curve")
        for x, y in pts:
            d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=INK)
    else:
        d.line([(760, y0 + 200), (1120, y0 + 60)], fill=LINE, width=10)
        for x in (760, 880, 1000, 1120):
            y = y0 + 200 - int((x - 760) * 140 / 360)
            d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=FLAME)


def build(key, label, out_dir):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 104], fill=INK)                      # masthead band
    d.text((60, 30), "TRANSIT", font=font(46), fill=PAPER)
    w = d.textlength("TRANSIT", font=font(46))
    d.text((60 + w, 30), "411", font=font(46), fill=FLAME)
    d.rectangle([0, 104, W, 112], fill=RED)                    # accent rule
    d.text((60, 210), label.upper(), font=font(96), fill=INK)  # the pillar
    d.text((64, 340), "Transit news, data & moves", font=font(34), fill=MUTED)
    motif(d, key)
    d.rectangle([0, H - 12, W, H], fill=INK)
    path = os.path.join(out_dir, key + ".png")
    img.save(path, "PNG", optimize=True)
    return path, os.path.getsize(path)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    for key, label in PILLARS.items():
        path, size = build(key, label, out_dir)
        print("%-12s %s (%.0f KB)" % (key, path, size / 1024))


if __name__ == "__main__":
    main()
