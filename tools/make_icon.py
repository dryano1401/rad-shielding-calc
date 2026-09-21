"""Build the application icon from the Affinity atom mark.

The mark is three elliptical orbitals, a nucleus and one electron, with the
brand's navy and cyan.  Drawn rather than downsampled, because stroke weight
has to grow as the icon shrinks or the orbitals disappear: below about 32 px
three overlapping thin ellipses turn to mush, so the small entries reduce to
a single ring around the nucleus, which is the same idea at a size that can
carry it.
"""
import struct
from io import BytesIO
from PIL import Image, ImageDraw

INK   = (0x10, 0x22, 0x3E, 255)   # Affinity navy, sampled from the logo
CYAN  = (0x00, 0xB0, 0xCA, 255)   # Affinity cyan
WHITE = (0xFF, 0xFF, 0xFF, 255)
BRASS = (0xB8, 0x86, 0x3B, 255)

SS = 8                            # supersample factor
FULL_MARK_MIN = 32                # below this the mark reduces to a ring


def _orbital(px, angle, rx, ry, width, colour):
    layer = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    c = px / 2
    ImageDraw.Draw(layer).ellipse(
        [c - rx, c - ry, c + rx, c + ry], outline=colour, width=max(1, int(round(width)))
    )
    return layer.rotate(angle, resample=Image.BICUBIC, center=(c, c))


def render(size, *, barrier=False):
    px = size * SS
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=px * 0.22, fill=INK)

    m = px * 0.085
    span = px - 2 * m
    c = px / 2

    if barrier:
        # A shielding barrier down the right-hand third, in the brand's brass.
        bx = px * 0.775
        d.rectangle([bx, px * 0.16, bx + span * 0.085, px * 0.84], fill=BRASS)

    if size >= FULL_MARK_MIN:
        rx, ry = span * 0.46, span * 0.185
        w = span * (0.055 if size <= 32 else 0.042)
        for angle, colour in ((0, WHITE), (120, WHITE), (60, CYAN)):
            img.alpha_composite(_orbital(px, angle, rx, ry, w, colour))
        nr = span * 0.095
        d.ellipse([c - nr, c - nr, c + nr, c + nr], fill=WHITE)
        if size >= 48:
            dot = span * 0.045
            dx, dy = c - span * 0.175, c - span * 0.395
            d.ellipse([dx - dot, dy - dot, dx + dot, dy + dot], fill=CYAN)
    else:
        r = span * 0.40
        d.ellipse([c - r, c - r, c + r, c + r], outline=CYAN,
                  width=max(1, int(round(span * 0.105))))
        nr = span * 0.15
        d.ellipse([c - nr, c - nr, c + nr, c + nr], fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path, images):
    """Write a multi-resolution .ico with PNG-compressed entries.

    Pillow's own ICO writer resizes one source image to every requested size,
    which would throw away the per-size drawing above, so the container is
    assembled here instead.
    """
    blobs = []
    for im in images:
        buf = BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())

    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(images)))     # reserved, type=icon, count
    offset = 6 + 16 * len(images)
    for im, blob in zip(images, blobs):
        w = 0 if im.width >= 256 else im.width           # 0 means 256 in ICO
        h = 0 if im.height >= 256 else im.height
        out.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    for blob in blobs:
        out.write(blob)
    with open(path, "wb") as fh:
        fh.write(out.getvalue())


SIZES = [16, 20, 24, 32, 48, 64, 128, 256]

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "radshield.ico"
    write_ico(target, [render(s) for s in SIZES])
    print(f"wrote {target}")
