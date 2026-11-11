"""GitHub social preview (1280x640) + README hero banner for Auditarr.
Background: the app's own screenshots (uibot/screens/*.png or a folder given as argv[1]) tilted and dimmed, Homarr-style; falls back to a drawn mosaic.
Foreground: logo mark, wordmark, tagline, feature strip. Output: docs/social-preview.png (1280x640) and docs/banner.png (1600x500).
Usage: python docs/make_banner.py [screenshot dir]"""
import glob, math, os, sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "uibot", "screens")
TEAL = (61, 214, 180); INK = (14, 20, 18); PANEL = (21, 29, 26); LINE = (36, 48, 41); FG = (225, 233, 228); MUT = (143, 163, 154)
def font(size, weight="Bold"):
    for p in (f"/usr/share/fonts/noto/NotoSans-{weight}.ttf", f"/usr/share/fonts/truetype/noto/NotoSans-{weight}.ttf", "/usr/share/fonts/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(p): return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def mosaic(w, h):
    """Drawn stand-in for screenshots: rows of panels with chips and bars, like the Findings page."""
    img = Image.new("RGB", (w, h), INK); d = ImageDraw.Draw(img); y = -40
    chips = [(255, 111, 111), (255, 169, 77), (255, 209, 102), (143, 209, 143)]
    import random; rnd = random.Random(7)
    while y < h + 60:
        x = -60 + rnd.randint(0, 80); rowh = rnd.choice([46, 58, 70])
        while x < w + 60:
            pw = rnd.randint(180, 420); d.rounded_rectangle((x, y, x + pw, y + rowh), 8, fill=PANEL, outline=LINE)
            c = rnd.choice(chips); d.rounded_rectangle((x + 12, y + rowh // 2 - 7, x + 58, y + rowh // 2 + 7), 7, fill=c)
            d.rounded_rectangle((x + 70, y + rowh // 2 - 4, x + 70 + rnd.randint(50, pw - 90), y + rowh // 2 + 4), 4, fill=LINE)
            if rnd.random() < 0.4: d.rounded_rectangle((x + pw - 70, y + rowh // 2 - 9, x + pw - 12, y + rowh // 2 + 9), 5, outline=TEAL)
            x += pw + 14
        y += rowh + 14
    return img

def background(w, h):
    def pick(kind):   # prefer the dark variant of the fake-stack ("live") page: moody, and the data on it is synthetic
        for pat in (f"ui-{kind}-live-dark.png", f"ui-{kind}-dark.png", f"ui-{kind}-live.png", f"ui-{kind}.png"):
            p = os.path.join(SHOTS, pat)
            if os.path.exists(p): return [p]
        return []
    shots = pick("desktop") + pick("phone")
    if shots:
        desk = Image.open(shots[0]).convert("RGB"); big = Image.new("RGB", (int(w * 1.6), int(h * 1.6)), INK)
        tile = desk.resize((int(big.width * 0.62), int(big.width * 0.62 * desk.height / desk.width)))
        big.paste(tile, (0, 0)); big.paste(tile, (tile.width + 30, 120)); big.paste(tile, (-200, tile.height + 30)); big.paste(tile, (tile.width - 170, tile.height + 150))
        ph = [p for p in shots if "phone" in p]
        if ph:
            pimg = Image.open(ph[0]).convert("RGB"); pimg = pimg.resize((int(big.width * 0.17), int(big.width * 0.17 * pimg.height / pimg.width)))
            big.paste(pimg, (int(big.width * 0.66), int(big.height * 0.05)))
        bg = big
    else: bg = mosaic(int(w * 1.6), int(h * 1.6))
    bg = bg.rotate(-8, resample=Image.BICUBIC, expand=False)
    cx, cy = bg.width // 2, bg.height // 2; bg = bg.crop((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2))
    bg = bg.filter(ImageFilter.GaussianBlur(1.2))
    # dim towards the centre so the text sits on a calm area, keep the edges lively
    overlay = Image.new("L", (w, h), 0); od = ImageDraw.Draw(overlay)
    for i in range(60, 0, -1):
        od.ellipse((w // 2 - i * w // 90, h // 2 - i * h // 70, w // 2 + i * w // 90, h // 2 + i * h // 70), fill=int(235 - i * 2.4))
    dark = Image.new("RGB", (w, h), INK); return Image.composite(dark, bg, overlay).convert("RGBA")

def logo(d, x, y, s):
    r = s * 0.28; cx, cy = x + s * 0.44, y + s * 0.44; lw = max(4, int(s * 0.085))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=TEAL, width=lw)
    d.line((cx + r * 0.72, cy + r * 0.72, x + s * 0.92, y + s * 0.92), fill=TEAL, width=lw + 3)
    d.line((cx - r * 0.45, cy + r * 0.02, cx - r * 0.08, cy + r * 0.4), fill=FG, width=max(3, int(s * 0.06)))
    d.line((cx - r * 0.1, cy + r * 0.42, cx + r * 0.5, cy - r * 0.42), fill=FG, width=max(3, int(s * 0.06)))

def compose(w, h, out, tagline, strip, scale=1.0):
    img = background(w, h); d = ImageDraw.Draw(img)
    S = int(96 * scale); logo(d, w // 2 - S // 2, int(h * 0.17), S)
    f1 = font(int(118 * scale), "Black"); name = "Auditarr"; tw = d.textlength(name, font=f1); x = (w - tw) / 2; y = int(h * 0.17) + S + int(6 * scale)
    d.text((x + 3, y + 4), name, font=f1, fill=(0, 0, 0)); d.text((x, y), "Audit", font=f1, fill=FG); d.text((x + d.textlength("Audit", font=f1), y), "arr", font=f1, fill=TEAL)
    f2 = font(int(34 * scale), "Medium"); tw2 = d.textlength(tagline, font=f2); d.text(((w - tw2) / 2 + 2, y + int(150 * scale) + 2), tagline, font=f2, fill=(0, 0, 0)); d.text(((w - tw2) / 2, y + int(150 * scale)), tagline, font=f2, fill=FG)
    f3 = font(int(17 * scale), "Bold"); sp = "   ·   ".join(strip); tw3 = d.textlength(sp, font=f3) * 1.12
    sx = (w - tw3) / 2; yy = y + int(210 * scale)
    parts = sp.split("   ·   ")
    for i, part in enumerate(parts):
        for ch in part:   # letter-spaced small caps strip
            d.text((sx, yy), ch, font=f3, fill=MUT); sx += d.textlength(ch, font=f3) + 2.2 * scale
        if i < len(parts) - 1: d.text((sx + 8 * scale, yy - 2), "·", font=f3, fill=TEAL); sx += d.textlength("   ·   ", font=f3) * 1.0
    img.convert("RGB").save(out, optimize=True); print("wrote", out, img.size)

compose(1280, 640, os.path.join(HERE, "social-preview.png"), "Quality control for your media library.", ["SONARR", "RADARR", "BAZARR", "PLEX", "ONE-CLICK FIXES", "AI SUBTITLES", "OPEN SOURCE"])
compose(1600, 500, os.path.join(HERE, "banner.png"), "Quality control for your media library.", ["SONARR", "RADARR", "BAZARR", "PLEX", "ONE-CLICK FIXES", "AI SUBTITLES", "OPEN SOURCE"], scale=0.85)
