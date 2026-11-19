"""Normalise extracted images to PNG and store them by content hash."""
import hashlib
import io
import logging
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)


def to_png(data: bytes) -> tuple[bytes, int, int] | None:
    """Convert image bytes (png/jpg/gif/bmp/tiff, and emf/wmf on Windows via Pillow) to RGB PNG."""
    try:
        pix = pymupdf.Pixmap(data)
        if pix.colorspace and pix.colorspace.n not in (1, 3):  # CMYK etc.
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("png"), pix.width, pix.height
    except Exception:
        pass
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.format in ("WMF", "EMF"):
            img.load(dpi=150)
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height
    except Exception as e:  # unsupported vector format, corrupt image
        log.info("Cannot convert image to PNG: %s", e)
        return None


def store_image(data: bytes, figures_dir: Path) -> dict | None:
    """Save as figures_dir/<sha256>.png. Returns image_path, sha256, width, height (or None if unreadable)."""
    converted = to_png(data)
    if converted is None:
        return None
    png, width, height = converted
    sha = hashlib.sha256(png).hexdigest()
    figures_dir.mkdir(parents=True, exist_ok=True)
    path = figures_dir / f"{sha}.png"
    if not path.exists():
        path.write_bytes(png)
    return {"image_path": str(path), "sha256": sha, "width": width, "height": height}
