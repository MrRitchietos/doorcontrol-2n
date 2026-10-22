"""Logo upload and the app icons made from it (home screen icons for Android and iPhone)."""

import io
import os

from PIL import Image, ImageDraw, ImageOps

MAX_LOGO = 4 * 1024 * 1024
ICON_SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}


def hex_rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def save_logo(data_dir, raw):
    """Re-encode whatever came in as a clean PNG. Raises ValueError for anything that is not an image."""
    if len(raw) > MAX_LOGO:
        raise ValueError("logo too big (max 4 MB)")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise ValueError("not an image (use PNG, JPG or WEBP)")
    img = ImageOps.exif_transpose(img).convert("RGBA")
    img.thumbnail((1024, 1024))
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img.save(os.path.join(data_dir, "logo.png"), "PNG", optimize=True)


def remove_logo(data_dir):
    p = os.path.join(data_dir, "logo.png")
    if os.path.exists(p):
        os.remove(p)


def has_logo(data_dir):
    return os.path.exists(os.path.join(data_dir, "logo.png"))


def _door_glyph(size, fg):
    """Simple door drawing for when there is no logo."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = size * 0.36
    h = size * 0.52
    x0 = (size - w) / 2
    y0 = (size - h) / 2
    lw = max(2, int(size * 0.045))
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=size * 0.03, outline=fg, width=lw)
    r = size * 0.035
    cx = x0 + w * 0.75
    cy = y0 + h * 0.55
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
    d.line([x0 - size * 0.08, y0 + h, x0 + w + size * 0.08, y0 + h], fill=fg, width=lw)
    return img


def build_icons(data_dir, accent):
    """Icons are square with the accent color behind the logo, so a wide logo still looks right."""
    bg = hex_rgb(accent)
    logo = None
    if has_logo(data_dir):
        logo = Image.open(os.path.join(data_dir, "logo.png")).convert("RGBA")
    for name, size in ICON_SIZES.items():
        canvas = Image.new("RGBA", (size, size), bg + (255,))
        if logo is not None:
            # white rounded card so dark and colored logos both stay readable
            pad = int(size * 0.12)
            card = Image.new("RGBA", (size - 2 * pad, size - 2 * pad), (0, 0, 0, 0))
            ImageDraw.Draw(card).rounded_rectangle(
                [0, 0, card.width - 1, card.height - 1], radius=int(size * 0.12), fill=(255, 255, 255, 255)
            )
            canvas.alpha_composite(card, (pad, pad))
            inner = size - 2 * pad - int(size * 0.14)
            lg = logo.copy()
            lg.thumbnail((inner, inner), Image.LANCZOS)
            canvas.alpha_composite(lg, ((size - lg.width) // 2, (size - lg.height) // 2))
        else:
            canvas.alpha_composite(_door_glyph(size, (255, 255, 255, 255)))
        canvas.convert("RGB").save(os.path.join(data_dir, name), "PNG", optimize=True)
