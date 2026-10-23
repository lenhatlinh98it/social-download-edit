"""Generate assets/icon.ico and assets/icon.png (dev tool, needs Pillow)."""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets")
STOPS = [(0.0, (168, 85, 247)), (0.48, (99, 102, 241)), (1.0, (34, 211, 238))]  # same as the UI --grad


def lerp_color(t):
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def diagonal_gradient(size):
    # 135deg: top-left -> bottom-right, built from a 1px ramp then rotated
    ramp = Image.new("RGB", (size * 2, 1))
    for x in range(size * 2):
        ramp.putpixel((x, 0), lerp_color(x / (size * 2 - 1)))
    ramp = ramp.resize((size * 2, size * 2))
    return ramp.rotate(-45, resample=Image.BICUBIC).crop((size // 2, size // 2, size * 3 // 2, size * 3 // 2))


def rounded_mask(box, radius):
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def build():
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # squircle background with gradient
    pad = 40
    bg_mask = rounded_mask((pad, pad, S - pad, S - pad), radius=230)
    icon.paste(diagonal_gradient(S), (0, 0), bg_mask)

    # soft glossy highlight on the upper half
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).ellipse((-S * 0.2, -S * 0.75, S * 1.2, S * 0.48), fill=60)
    gloss = ImageChops.multiply(gloss.filter(ImageFilter.GaussianBlur(40)), bg_mask)
    icon = Image.alpha_composite(icon, Image.merge("RGBA", (*[Image.new("L", (S, S), 255)] * 3, gloss)))

    # 9:16 frame (a short / reel) in white, with a download arrow cut out of it
    fw, fh = 400, 640
    fx, fy = (S - fw) // 2, (S - fh) // 2 + 6
    frame = rounded_mask((fx, fy, fx + fw, fy + fh), radius=92)

    arrow = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(arrow)
    cx, stroke = S // 2, 64
    top, tip = fy + 140, fy + 420
    d.rounded_rectangle((cx - stroke // 2, top, cx + stroke // 2, tip - 20), radius=stroke // 2, fill=255)
    head = 150
    d.line([(cx - head, tip - head), (cx, tip), (cx + head, tip - head)], fill=255, width=stroke, joint="curve")
    for x, y in ((cx - head, tip - head), (cx + head, tip - head), (cx, tip)):
        d.ellipse((x - stroke // 2, y - stroke // 2, x + stroke // 2, y + stroke // 2), fill=255)
    base_y = fy + 510
    d.rounded_rectangle((cx - 130, base_y, cx + 130, base_y + stroke), radius=stroke // 2, fill=255)

    glyph = ImageChops.subtract(frame, arrow)

    # drop shadow under the white frame
    shadow = frame.filter(ImageFilter.GaussianBlur(28)).point(lambda v: int(v * 0.45))
    shadow = ImageChops.offset(shadow, 0, 22)
    shadow_layer = Image.merge("RGBA", (*[Image.new("L", (S, S), 30)] * 3, ImageChops.multiply(shadow, bg_mask)))
    icon = Image.alpha_composite(icon, shadow_layer)

    white = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    icon.paste(white, (0, 0), glyph)
    return icon


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    icon = build()
    icon.resize((512, 512), Image.LANCZOS).save(os.path.join(OUT_DIR, "icon.png"))
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # BMP entries: .NET's System.Drawing.Icon (used by pywebview on Windows) misreads PNG-compressed small sizes
    icon.save(os.path.join(OUT_DIR, "icon.ico"), sizes=[(s, s) for s in sizes], bitmap_format="bmp")
    print("written", OUT_DIR)


if __name__ == "__main__":
    main()
