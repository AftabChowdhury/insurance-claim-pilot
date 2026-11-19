"""Format-independent representation of an extracted document (PLAN.md §1.1).

Every extractor (PDF, DOCX, DOC, markdown) returns an ExtractedDocument whose blocks are in reading order.
Chunking, figure linking and indexing only work on these blocks.
"""
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

BlockKind = Literal["heading", "paragraph", "list", "table", "figure", "caption"]
# figure origin: embedded picture/drawing, SmartArt diagram (text taken from the file), or a whole scanned page
FigureSource = Literal["image", "smartart", "page_scan"]

CAPTION_RE = re.compile(r"^(figure|fig\.|abbildung|abb\.|diagram|chart|image|illustration)\s*[\dA-Z]", re.IGNORECASE)
NUMBERED_HEADING_RE = re.compile(r"^(?:§\s*)?(\d+(?:\.\d+)*|[A-Z]-?\d+(?:\.\d+)*)\.?\s+\S")


class ExtractionError(Exception):
    """The document cannot be extracted (encrypted, corrupt, missing converter, ...)."""


@dataclass
class Block:
    kind: BlockKind
    text: str = ""                    # markdown; for figures: rendered description (see render_figure)
    level: int = 0                    # headings: 0 = document title, 1 = section, 2 = clause, ...
    page: int | None = None           # 1-based page number (PDF); None for DOCX
    # --- figures only ---
    source: FigureSource | None = None
    image_path: str | None = None     # PNG under <work_dir>/figures/<sha256>.png
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None        # "Figure 3 – Claims process"
    hint_text: str | None = None      # text the extractor found inside the picture (PDF text layer, SmartArt)
    figure: dict | None = None        # vision result (figures.FigureDescription) once described
    decorative: bool = False          # logo / icon / repeated graphic: no chunk, no vision call


@dataclass
class ExtractedDocument:
    source: str                       # original file name
    file_type: str                    # pdf | docx | doc | md
    blocks: list[Block]
    title: str = ""
    page_count: int | None = None
    front_matter: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def figures(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "figure"]

    def to_markdown(self) -> str:
        """Readable rendering (preview / debugging). Decorative figures are left out."""
        out, page = [], None
        for b in self.blocks:
            if b.page and b.page != page:
                page = b.page
                out.append(f"<!-- page {page} -->")
            if b.kind == "heading":
                out.append("#" * max(b.level, 1) + " " + b.text)
            elif b.kind == "figure":
                if not b.decorative:
                    out.append(render_figure(b))
            elif b.text:
                out.append(b.text)
        return "\n\n".join(out) + "\n"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedDocument":
        return cls(**{**d, "blocks": [Block(**b) for b in d["blocks"]]})

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ExtractedDocument":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def render_figure(b: Block) -> str:
    """Text that stands in for a figure in chunks (PLAN.md §3.2). Works with or without a vision result."""
    f = b.figure or {}
    if b.source == "page_scan":
        return f"[Scanned page {b.page or ''} – not transcribed]"
    title = f.get("title") or b.caption or "Untitled figure"
    lines = [f"[Figure: {title}]"]
    if b.caption and f.get("title") and b.caption.lower() not in f["title"].lower():
        lines.append(f"Caption: {b.caption}")
    if f.get("description"):
        lines.append(f["description"])
    if f.get("steps"):
        lines.append("Steps:")
        lines += [f"{i}. {s}" for i, s in enumerate(f["steps"], 1)]
    text_in_figure = f.get("visible_text") or (b.hint_text if not f else "")
    if text_in_figure:
        lines.append("Text in figure:\n" + text_in_figure)
    return "\n".join(lines)


def numbered_heading_level(text: str) -> int | None:
    """'4.2 Water damage' -> 2, '4 Claims' -> 1, 'A-3 Theft' -> 1; None if the text is not a numbered heading."""
    m = NUMBERED_HEADING_RE.match(text.strip())
    if not m or len(text) > 120 or text.rstrip().endswith((".", ":", ";", ",")):
        return None
    return m.group(1).count(".") + 1


def attach_captions(blocks: list[Block]) -> list[Block]:
    """Move caption paragraphs into the neighbouring figure (previous first, then next)."""
    out: list[Block] = []
    for i, b in enumerate(blocks):
        is_caption = b.kind == "caption" or (b.kind == "paragraph" and len(b.text) < 200
                                              and CAPTION_RE.match(b.text.strip()))
        if is_caption:
            prev = out[-1] if out else None
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            target = (prev if prev and prev.kind == "figure" and not prev.caption and not prev.decorative
                      else nxt if nxt and nxt.kind == "figure" and not nxt.caption else None)
            if target is not None:
                target.caption = b.text.strip()
                continue
            b.kind = "paragraph"
        out.append(b)
    return out
