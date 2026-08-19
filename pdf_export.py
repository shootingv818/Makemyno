"""
pdf_export.py — turn a pile of photos into PDF albums, cheaply.
==============================================================

WHY THIS FILE IS SHAPED THIS WAY
--------------------------------
The feature delivers a CUMULATIVE PDF every N photos, so the customer sees
progress instead of waiting in silence. The obvious implementation rebuilds the
whole PDF from the raw photos each time — and that is quadratic:

    2000 photos, a PDF every 100  ->  20 builds
    work done = 100 + 200 + ... + 2000 = 21,000 image decodes
    instead of 2000

The base project did exactly that with a batch of 20, which meant 100 builds and
roughly 101,000 decodes for 2000 photos: about fifty times more CPU than needed,
plus 100 files pushed out.

The split below fixes it:

    prepare_image()        decode + downscale + re-encode ONCE per photo
    build_pdf_from_jpegs() lay out already-prepared JPEGs, no decoding at all

So the heavy work is O(photos) and each cumulative rebuild is a cheap layout
pass. Downscaling also keeps memory sane: a 3 MB phone photo becomes roughly
70 KB, which is the difference between ~600 MB and ~150 MB held for a 2000-photo
export.

Dependency-light on purpose: only reportlab + Pillow. Every image is validated
through Pillow before embedding, so one corrupt photo can never abort an export.
"""
from __future__ import annotations

import io

# A4 at 72 dpi, and the margin used on every page.
_PAGE = (595.2755905511812, 841.8897637795277)
_MARGIN = 24


def _page_geometry(width: int, height: int):
    """Scale an image to fit inside the page margins, centred."""
    page_w, page_h = _PAGE
    avail_w = page_w - 2 * _MARGIN
    avail_h = page_h - 2 * _MARGIN
    scale = min(avail_w / width, avail_h / height)
    draw_w, draw_h = width * scale, height * scale
    return (page_w - draw_w) / 2, (page_h - draw_h) / 2, draw_w, draw_h


def prepare_image(blob, quality: int = 45, max_size: int = 1000):
    """Decode ONE raw photo, downscale it, re-encode it to a light JPEG.

    Returns the JPEG bytes, or None when the blob cannot be decoded (in which
    case the caller simply skips that photo).

    Doing this exactly once per photo is what keeps cumulative delivery cheap:
    every later PDF just lays these out again without touching Pillow.
    """
    if not blob:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(blob))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max_size and max(im.size) > max_size:
            im.thumbnail((int(max_size), int(max_size)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=int(quality), optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def build_pdf_from_jpegs(jpegs: list, out_path: str) -> int:
    """Write already-prepared JPEGs into one PDF, one image per page.

    No decoding or re-encoding happens here, so rebuilding a growing cumulative
    PDF stays fast. Returns the page count; unreadable blobs are skipped.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from PIL import Image

    c = canvas.Canvas(out_path, pagesize=A4)
    added = 0
    for jpeg in jpegs:
        if not jpeg:
            continue
        try:
            width, height = Image.open(io.BytesIO(jpeg)).size
            if width <= 0 or height <= 0:
                continue
            x, y, draw_w, draw_h = _page_geometry(width, height)
            c.drawImage(ImageReader(io.BytesIO(jpeg)), x, y,
                        width=draw_w, height=draw_h,
                        preserveAspectRatio=True, anchor="c")
            c.showPage()
            added += 1
        except Exception:
            continue
    if added == 0:
        c.showPage()          # still produce a valid PDF so callers never crash
    c.save()
    return added


def build_pdf(images: list, out_path: str, quality: int = 45,
              max_size: int = 1000) -> int:
    """Convenience path for RAW photos: prepare then lay out.

    Prefer prepare_image() + build_pdf_from_jpegs() when the same photos will be
    written more than once, which is the whole point of the split.
    """
    prepared = []
    for blob in images:
        jpeg = prepare_image(blob, quality=quality, max_size=max_size)
        if jpeg:
            prepared.append(jpeg)
    return build_pdf_from_jpegs(prepared, out_path)


def estimate_size(jpegs: list) -> int:
    """Rough byte size of the PDF these JPEGs would produce."""
    return sum(len(j or b"") for j in jpegs) + 1024 * max(1, len(jpegs))
