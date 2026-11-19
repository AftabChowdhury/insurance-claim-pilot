"""Parse markdown (from pymupdf4llm, vision OCR or .md policy files) into Blocks."""
import re
from collections.abc import Callable

from .model import Block, numbered_heading_level

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\((.+)\)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
PIC_START, PIC_END = "<!-- Start of picture text -->", "<!-- End of picture text -->"
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def split_front_matter(text: str) -> tuple[dict, str]:
    """'---\\npolicy_type: home\\n---' at the top of a markdown file -> ({'policy_type': 'home'}, rest)."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, text[m.end():]


def _plain(text: str) -> str:
    return re.sub(r"\*\*|__", "", text).strip()


def parse_markdown(text: str, page: int | None = None,
                   on_image: Callable[[str], Block | None] | None = None) -> list[Block]:
    """Split markdown into heading / paragraph / list / table / figure blocks.

    on_image(path) turns an image reference into a figure Block (or None to drop it).
    pymupdf4llm's "picture text" (text found inside a picture) is attached to the preceding figure.
    """
    blocks: list[Block] = []
    buf: list[str] = []
    buf_kind: str | None = None  # paragraph | list | table | code

    def flush():
        nonlocal buf, buf_kind
        if buf:
            body = "\n".join(buf).strip()
            if body:
                kind = "paragraph" if buf_kind == "code" else buf_kind
                if kind == "paragraph" and (level := _bold_numbered_heading(body)):
                    blocks.append(Block("heading", _plain(body), level=level, page=page))
                else:
                    blocks.append(Block(kind, body, page=page))
        buf, buf_kind = [], None

    def start(kind: str):
        nonlocal buf_kind
        if buf_kind != kind:
            flush()
        buf_kind = kind

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if buf_kind == "code":
            buf.append(line)
            if stripped.startswith("```"):
                flush()
            i += 1
            continue
        if stripped.startswith("```"):
            start("code")
            buf.append(line)
            i += 1
            continue

        if stripped.startswith(PIC_START):
            # collect until the end marker (may be on the same line)
            chunk = stripped[len(PIC_START):]
            while PIC_END not in chunk and i + 1 < len(lines):
                i += 1
                chunk += "\n" + lines[i]
            pic_text = chunk.split(PIC_END)[0].replace("<br>", "\n").strip()
            flush()
            if blocks and blocks[-1].kind == "figure":
                prev = blocks[-1]
                prev.hint_text = "\n".join(filter(None, [prev.hint_text, pic_text]))
            elif pic_text:
                blocks.append(Block("paragraph", pic_text, page=page))
            i += 1
            continue

        if not stripped:
            if buf_kind != "list":  # pymupdf4llm puts blank lines between list items; keep the list open
                flush()
            i += 1
            continue

        if m := IMAGE_RE.match(stripped):
            flush()
            if on_image and (fig := on_image(m.group(1))):
                fig.page = fig.page or page
                blocks.append(fig)
            i += 1
            continue

        if m := HEADING_RE.match(stripped):
            flush()
            title = _plain(m.group(2))
            if title:
                blocks.append(Block("heading", title, level=len(m.group(1)), page=page))
            i += 1
            continue

        if stripped.startswith("|"):
            start("table")
            buf.append(stripped)
        elif LIST_RE.match(line):
            start("list")
            buf.append(line)
        elif buf_kind == "list" and line.startswith((" ", "\t")):
            buf.append(line)  # continuation of a list item
        else:
            start("paragraph")
            buf.append(stripped)
        i += 1
    flush()
    return blocks


def _bold_numbered_heading(text: str) -> int | None:
    """A paragraph that is only '**4.2 Water damage**' is a heading the layout engine did not recognise."""
    if "\n" in text or not (text.startswith("**") and text.endswith("**")):
        return None
    return numbered_heading_level(_plain(text))
