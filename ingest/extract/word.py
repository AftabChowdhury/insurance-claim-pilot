"""Word extraction (features/SAVE_IN_VECTOR_DB.md §1.3).

.docx: the document body is walked in order (paragraphs, tables, content controls), so images, SmartArt
diagrams and text boxes stay at the position where they appear. Headers/footers are skipped (logos).
.doc:  converted to .docx with LibreOffice (headless) first.
"""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document as open_docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from core.config import get_settings

from .images import store_image
from .model import Block, ExtractedDocument, ExtractionError, numbered_heading_level

log = logging.getLogger(__name__)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"

# elements whose children are runs (or more containers of runs)
RUN_CONTAINERS = {W + t for t in ("hyperlink", "ins", "smartTag", "fldSimple", "sdt", "sdtContent", "customXml")}
HEADING_STYLE_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


# ----------------------------------------------------------------------------------------------- .doc
def _soffice() -> str | None:
    configured = get_settings().libreoffice_path
    for candidate in (configured, "soffice", r"C:\Program Files\LibreOffice\program\soffice.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if candidate and (found := shutil.which(candidate) or (Path(candidate).is_file() and candidate)):
            return found
    return None


def convert_doc_to_docx(path: Path, out_dir: Path) -> Path:
    exe = _soffice()
    if not exe:
        raise ExtractionError("cannot read .doc files: LibreOffice not found (set LIBREOFFICE_PATH) "
                              "- or upload the document as .docx/.pdf")
    with tempfile.TemporaryDirectory(prefix="claimpilot_lo_") as profile:
        # separate profile so a running LibreOffice instance does not block the conversion
        cmd = [exe, f"-env:UserInstallation=file:///{Path(profile).as_posix()}", "--headless",
               "--convert-to", "docx", "--outdir", str(out_dir), str(path)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            raise ExtractionError(f"LibreOffice conversion failed: {e}") from e
    out = out_dir / (path.stem + ".docx")
    if not out.is_file():
        raise ExtractionError("LibreOffice conversion produced no .docx file")
    return out


# ----------------------------------------------------------------------------------------------- .docx
class _Segments:
    """Text and graphics of one paragraph, in order."""

    def __init__(self):
        self.items: list[tuple[str, object]] = []

    def text(self, s: str):
        if self.items and self.items[-1][0] == "text":
            self.items[-1] = ("text", self.items[-1][1] + s)
        else:
            self.items.append(("text", s))

    def add(self, kind: str, value):
        self.items.append((kind, value))


class _DocxWalker:
    def __init__(self, docx, figures_dir: Path):
        self.doc = docx
        self.rels = docx.part.related_parts
        self.figures_dir = figures_dir
        self.blocks: list[Block] = []
        self.warnings: list[str] = []
        self.list_counters: dict[tuple[str, int], int] = {}
        self._numfmt_cache: dict[tuple[str, int], str] = {}

    # --- body ----------------------------------------------------------------------------------
    def body(self, el):
        for child in el:
            if child.tag == W + "p":
                self.paragraph(child)
            elif child.tag == W + "tbl":
                self.table(child)
            elif child.tag in (W + "sdt", W + "sdtContent", W + "customXml"):
                self.body(child.find(W + "sdtContent") if child.tag == W + "sdt" else child)

    # --- paragraphs ----------------------------------------------------------------------------
    def paragraph(self, p):
        para = Paragraph(p, self.doc)
        style = para.style.name if para.style is not None else ""
        segs = _Segments()
        self._inline(p, segs)
        has_text = any(k == "text" and v.strip() for k, v in segs.items)

        kind, level, prefix = "paragraph", 0, ""
        num = self._num_pr(p, para)
        if style.lower() == "title":
            kind = "heading"
        elif m := HEADING_STYLE_RE.match(style):
            kind, level = "heading", int(m.group(1))
        elif (lvl := self._outline_level(p)) is not None:
            kind, level = "heading", lvl
        elif style.lower() == "caption":
            kind = "caption"
        elif num is not None and has_text:
            kind, prefix = "list", self._list_prefix(*num)
        elif has_text and self._all_bold(para):
            plain = "".join(v for k, v in segs.items if k == "text").strip()
            if (lvl := numbered_heading_level(plain)) is not None:
                kind, level = "heading", lvl

        for k, v in segs.items:
            if k == "text":
                text = re.sub(r"[ \t]+", " ", v).strip()
                if text:
                    self._add_text(kind, prefix + text, level)
                    prefix = prefix and "  "  # text after an inline image continues the same list item
            elif k == "image":
                self._add_image(v)
            elif k == "smartart":
                self._add_smartart(v)
            elif k == "textbox":
                self.body(v)

    def _add_text(self, kind: str, text: str, level: int):
        last = self.blocks[-1] if self.blocks else None
        if kind == "list" and last is not None and last.kind == "list":
            last.text += "\n" + text  # consecutive list items form one list block
        else:
            self.blocks.append(Block(kind, text, level=level))

    def _inline(self, el, segs: _Segments):
        for child in el:
            tag = child.tag
            if tag == W + "r":
                self._run(child, segs)
            elif tag in RUN_CONTAINERS:
                self._inline(child, segs)
            elif tag == MC + "AlternateContent" and (choice := child.find(MC + "Choice")) is not None:
                self._inline(choice, segs)

    def _run(self, r, segs: _Segments):
        for child in r:
            tag = child.tag
            if tag == W + "t":
                segs.text(child.text or "")
            elif tag in (W + "tab", W + "ptab"):
                segs.text(" ")
            elif tag in (W + "br", W + "cr"):
                segs.text("\n")
            elif tag in (W + "drawing", W + "pict", W + "object"):
                self._graphic(child, segs)
            elif tag == MC + "AlternateContent" and (choice := child.find(MC + "Choice")) is not None:
                self._run(choice, segs)  # Choice holds the modern drawing; Fallback is a duplicate

    def _graphic(self, el, segs: _Segments):
        for rel in el.iter(DGM + "relIds"):
            segs.add("smartart", rel.get(R + "dm"))
        for blip in el.iter(A + "blip"):
            if rid := blip.get(R + "embed"):
                segs.add("image", rid)
        for img in el.iter(V + "imagedata"):  # legacy VML pictures and OLE objects (e.g. Visio previews)
            if rid := img.get(R + "id"):
                segs.add("image", rid)
        for box in el.iter(W + "txbxContent"):
            segs.add("textbox", box)

    # --- tables ---------------------------------------------------------------------------------
    def table(self, tbl):
        try:
            rows = [[c.text for c in row.cells] for row in Table(tbl, self.doc).rows]
        except Exception:  # malformed merges: fall back to raw cells
            rows = [["".join(t.text or "" for t in tc.iter(W + "t")) for tc in tr.iter(W + "tc")]
                    for tr in tbl.iter(W + "tr")]
        rows = [[_cell(c) for c in r] for r in rows if any(c.strip() for c in r)]
        if rows:
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
            md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            self.blocks.append(Block("table", "\n".join(md)))
        for blip in tbl.iter(A + "blip"):  # pictures inside cells go right after the table
            if rid := blip.get(R + "embed"):
                self._add_image(rid)

    # --- figures --------------------------------------------------------------------------------
    def _add_image(self, rid: str):
        part = self.rels.get(rid)
        if part is None or not hasattr(part, "blob"):
            return
        info = store_image(part.blob, self.figures_dir)
        if info is None:
            self.warnings.append(f"image {part.partname} skipped: unsupported format")
            return
        self.blocks.append(Block("figure", source="image", **info))

    def _add_smartart(self, rid: str | None):
        part = self.rels.get(rid) if rid else None
        if part is None:
            return
        try:
            steps = smartart_steps(part.blob)
        except Exception as e:
            self.warnings.append(f"SmartArt diagram not readable: {e}")
            return
        if steps:
            text = "\n".join(steps)
            self.blocks.append(Block("figure", source="smartart", hint_text=text, figure={
                "kind": "workflow", "title": "", "description": "", "steps": steps,
                "visible_text": "", "referenced_clauses": []}))

    # --- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _outline_level(p) -> int | None:
        el = p.find(f"{W}pPr/{W}outlineLvl")
        if el is None:
            return None
        val = int(el.get(W + "val", "9"))
        return val + 1 if val < 9 else None

    @staticmethod
    def _all_bold(para: Paragraph) -> bool:
        runs = [r for r in para.runs if r.text.strip()]
        style_bold = bool(para.style is not None and para.style.font.bold)
        return bool(runs) and all(r.bold or (r.bold is None and style_bold) for r in runs)

    @staticmethod
    def _num_pr(p, para: Paragraph) -> tuple[str, int] | None:
        """(numId, ilvl) of a list paragraph, from the paragraph or its style chain."""
        candidates = [p]
        style = para.style
        while style is not None:
            candidates.append(style.element)
            style = style.base_style
        for el in candidates:
            num = el.find(f"{W}pPr/{W}numPr")
            if num is not None:
                num_id = num.find(W + "numId")
                ilvl = num.find(W + "ilvl")
                if num_id is not None and num_id.get(W + "val") != "0":
                    return num_id.get(W + "val"), int(ilvl.get(W + "val")) if ilvl is not None else 0
        if (para.style is not None and para.style.name.lower().startswith("list")):
            return "style:" + para.style.name, 0
        return None

    def _numfmt(self, num_id: str, ilvl: int) -> str:
        key = (num_id, ilvl)
        if key not in self._numfmt_cache:
            fmt = "bullet" if num_id.startswith("style:") and "number" not in num_id.lower() else "decimal"
            try:
                numbering = self.doc.part.numbering_part.element
                num = next(n for n in numbering.iter(W + "num") if n.get(W + "numId") == num_id)
                abstract_id = num.find(W + "abstractNumId").get(W + "val")
                abstract = next(a for a in numbering.iter(W + "abstractNum")
                                if a.get(W + "abstractNumId") == abstract_id)
                lvl = next(lv for lv in abstract.iter(W + "lvl") if lv.get(W + "ilvl") == str(ilvl))
                fmt = lvl.find(W + "numFmt").get(W + "val")
            except Exception:
                pass
            self._numfmt_cache[key] = fmt
        return self._numfmt_cache[key]

    def _list_prefix(self, num_id: str, ilvl: int) -> str:
        indent = "  " * ilvl
        if self._numfmt(num_id, ilvl) in ("bullet", "none"):
            return indent + "- "
        for k in [k for k in self.list_counters if k[0] == num_id and k[1] > ilvl]:
            del self.list_counters[k]  # a higher level restarts the numbering of deeper levels
        n = self.list_counters[(num_id, ilvl)] = self.list_counters.get((num_id, ilvl), 0) + 1
        return f"{indent}{n}. "


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def smartart_steps(data_xml: bytes) -> list[str]:
    """Ordered steps of a SmartArt data part (process diagrams, hierarchies, cycles).

    Each top-level node is one step; texts of its child nodes are appended ("Report claim – call hotline; ...").
    """
    root = etree.fromstring(data_xml)
    texts, doc_id = {}, None
    for pt in root.iter(DGM + "pt"):
        typ = pt.get("type", "node")
        if typ == "doc":
            doc_id = pt.get("modelId")
        elif typ == "node":
            text = " ".join(t.text for t in pt.iter(A + "t") if t.text).strip()
            texts[pt.get("modelId")] = text
    children: dict[str, list[tuple[int, str]]] = {}
    for cxn in root.iter(DGM + "cxn"):
        if cxn.get("type", "parOf") == "parOf":
            children.setdefault(cxn.get("srcId"), []).append((int(cxn.get("srcOrd", "0")), cxn.get("destId")))

    seen: set[str] = set()

    def ordered(node: str) -> list[str]:
        out = []
        for _, child in sorted(children.get(node, [])):
            if child not in seen:
                seen.add(child)
                out.append(child)
        return out

    def descendants(node: str) -> list[str]:
        result = []
        for child in ordered(node):
            if texts.get(child):
                result.append(texts[child])
            result += descendants(child)
        return result

    def top_level(node: str) -> list[str]:
        # skip text-less structural nodes until the first level that carries text
        nodes = ordered(node)
        if nodes and not any(texts.get(n) for n in nodes):
            return [s for n in nodes for s in top_level(n)]
        steps = []
        for n in nodes:
            details = descendants(n)
            text = texts.get(n, "")
            steps.append(f"{text} – {'; '.join(details)}" if text and details else text or "; ".join(details))
        return [s for s in steps if s]

    steps = top_level(doc_id) if doc_id else []
    return steps or [t for t in texts.values() if t]  # no hierarchy information: keep document order


def extract_docx(path: Path, figures_dir: Path, file_type: str = "docx") -> ExtractedDocument:
    try:
        docx = open_docx(str(path))
    except Exception as e:
        raise ExtractionError(f"cannot open Word document: {e}") from e
    walker = _DocxWalker(docx, figures_dir)
    walker.body(docx.element.body)
    title = docx.core_properties.title or ""
    return ExtractedDocument(source=path.name, file_type=file_type, blocks=walker.blocks, title=title,
                             warnings=walker.warnings)


def extract_doc(path: Path, figures_dir: Path) -> ExtractedDocument:
    with tempfile.TemporaryDirectory(prefix="claimpilot_doc_") as tmp:
        converted = convert_doc_to_docx(path, Path(tmp))
        result = extract_docx(converted, figures_dir, file_type="doc")
    result.source = path.name
    return result
