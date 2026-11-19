"""Small helpers shared by claim and policy uploads."""
import re
from pathlib import Path


def safe_name(name: str) -> str:
    """Strip path components and unusual characters from an uploaded file name."""
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "file"
