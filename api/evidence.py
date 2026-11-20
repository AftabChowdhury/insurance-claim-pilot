"""Claim image evidence, step A (at upload): is it a real image, fingerprints, metadata and a sanitised copy.

- Real image check: Pillow must decode it, the format (magic bytes) must match the extension, no decompression
  bombs, minimum size.
- Fingerprints: sha256 (exact copy) and a 64-bit difference hash (dHash; survives resizing / re-compression),
  stored in the claim_evidence table so later claims can be compared (reuse detection, step B).
- Metadata: EXIF date taken, camera, software, GPS present, plus AI / provenance markers (C2PA, IPTC
  "trainedAlgorithmicMedia", generator parameters in PNG text chunks). Read from the original before sanitising.
- Sanitised copy: auto-oriented, metadata-free JPEG, longest side <= CLAIM_IMAGE_MAX_SIDE. Only this copy is ever
  sent to the vision model; the original is kept for the admin.

Step B (vision analysis, duplicate / consistency checks) runs in the graph: api/graph/evidence.py.
"""
import hashlib
import re
from pathlib import Path

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from core.config import get_settings

IMAGE_EXTENSIONS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
SANITISED_DIR = "_sanitised"

# Byte markers of provenance / AI-generation metadata (XMP, C2PA manifests, generator text chunks).
AI_MARKERS = {
    b"trainedAlgorithmicMedia": "IPTC digital source type: AI-generated",
    b"compositeWithTrainedAlgorithmicMedia": "IPTC digital source type: AI-edited",
    b"c2pa": "C2PA content-credentials manifest",
}
PNG_GENERATOR_KEYS = ("parameters", "prompt", "workflow", "invokeai_metadata", "sd-metadata", "dream")


class EvidenceError(ValueError):
    """The upload is not an acceptable image (message is shown to the customer)."""


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def dhash(img: Image.Image, size: int = 8) -> str:
    """Difference hash: compare neighbouring pixels of a 9x8 greyscale thumbnail -> 64-bit hex string."""
    small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left, right = px[row * (size + 1) + col], px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:016x}"


def _exif(img: Image.Image, raw: bytes) -> dict:
    out: dict = {}
    exif = img.getexif()
    if exif:
        base = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items()}
        sub = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.get_ifd(ExifTags.IFD.Exif).items()}
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        for key, src in (("date_taken", sub.get("DateTimeOriginal") or sub.get("DateTimeDigitized")),
                         ("date_modified", base.get("DateTime")), ("make", base.get("Make")),
                         ("model", base.get("Model")), ("software", base.get("Software"))):
            if src:
                out[key] = str(src).strip("\x00 ").strip()
        out["gps"] = bool(gps)
    # PNG / other text chunks (editors and image generators write their name or prompt here)
    info_software = img.info.get("Software") or img.info.get("software")
    if info_software and "software" not in out:
        out["software"] = str(info_software)[:200]
    generator = [k for k in PNG_GENERATOR_KEYS if k in img.info]
    markers = [label for marker, label in AI_MARKERS.items() if marker in raw]
    if generator:
        markers.append(f"Image-generator metadata ({', '.join(generator)})")
    out["ai_markers"] = markers
    out["has_exif"] = bool(exif)
    return out


def inspect_image(path: Path, data: bytes) -> dict:
    """Validate an uploaded image and return its evidence record. Raises EvidenceError if not acceptable."""
    s = get_settings()
    expected = IMAGE_EXTENSIONS[path.suffix.lower()]
    Image.MAX_IMAGE_PIXELS = s.claim_image_max_mpx * 1_000_000
    try:
        with Image.open(path) as probe:
            probe.verify()  # structural check (truncated / corrupt files)
        img = Image.open(path)
        img.load()
    except Image.DecompressionBombError as e:
        raise EvidenceError(f"{path.name}: image is too large (> {s.claim_image_max_mpx} megapixels)") from e
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise EvidenceError(f"{path.name}: not a valid image file") from e
    if img.format != expected:
        raise EvidenceError(f"{path.name}: file content is {img.format or 'unknown'}, not {expected} "
                            f"as the extension says")
    if min(img.size) < s.claim_image_min_px:
        raise EvidenceError(f"{path.name}: image is too small ({img.width}x{img.height} px, minimum "
                            f"{s.claim_image_min_px} px) – please upload the original photo")

    meta = _exif(img, data)
    upright = ImageOps.exif_transpose(img).convert("RGB")
    upright.thumbnail((s.claim_image_max_side, s.claim_image_max_side))
    out_dir = path.parent / SANITISED_DIR
    out_dir.mkdir(exist_ok=True)
    sanitised = out_dir / (re.sub(r"[^\w.-]", "_", path.stem) + ".jpg")
    upright.save(sanitised, "JPEG", quality=90)  # no exif / xmp / text chunks are written

    return {"sha256": hashlib.sha256(data).hexdigest(), "dhash": dhash(upright), "width": img.width,
            "height": img.height, "format": img.format, "exif": meta, "sanitised_path": str(sanitised)}
