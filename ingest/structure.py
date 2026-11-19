"""Structure detection (features/SAVE_IN_VECTOR_DB.md §2.1): extracted blocks -> sections -> clauses with clause_id.

- The document title (level-0 heading, or a single top-level heading before everything else) is not a section.
- The highest remaining heading level = section, the next level = clause. Deeper headings stay inside the clause.
- A document with one heading level: every heading is a clause (and its own section).
- clause_id comes from the heading number ("4.2", "E-2", "§ 12", "Clause 4.2"); unnumbered headings get a
  synthetic id "S<section>.<n>". Text before the first clause of a section becomes an intro clause.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from ingest.extract.model import Block, ExtractedDocument

CLAUSE_NO_RE = re.compile(r"^(?:§\s*|(?:clause|article|art\.|section|part|chapter)\s+)?((?:[A-Z]-?)?\d+(?:\.\d+)*)\b",
                          re.IGNORECASE)
POLICY_TYPES = ("home", "auto")


@dataclass
class Clause:
    clause_id: str
    title: str                     # "4.2 Sudden and accidental discharge of water"
    section: str                   # "Section 4 – Covered Perils"
    section_no: int                # ordinal of the section in the document (for parent_content lookup)
    blocks: list[Block] = field(default_factory=list)
    synthetic: bool = False        # no own numbered clause heading (intro text, unnumbered heading)


@dataclass
class Structure:
    title: str
    clauses: list[Clause]
    section_texts: dict[int, str]  # section_no -> full markdown of the section (parent_content)


def heading_number(text: str) -> str | None:
    m = CLAUSE_NO_RE.match(text.strip())
    return m.group(1).upper() if m else None


def detect_policy_type(doc: ExtractedDocument, override: str | None = None, filename: str | None = None) -> str:
    """Admin's explicit choice -> front matter -> filename prefix -> 'general'."""
    if override:
        return override.lower()
    if pt := doc.front_matter.get("policy_type"):
        return pt.lower()
    name = (filename or doc.source).lower()
    return next((t for t in POLICY_TYPES if name.startswith(t)), "general")


def block_markdown(b: Block, heading_depth: int = 4) -> str:
    """Markdown of one block; headings are written with heading_depth '#' (## section, ### clause, #### deeper)."""
    if b.kind == "heading":
        return "#" * heading_depth + " " + b.text
    if b.kind == "figure":
        return "" if b.decorative else b.text
    return b.text


def detect_structure(doc: ExtractedDocument) -> Structure:
    blocks = [b for b in doc.blocks if not (b.kind == "figure" and b.decorative)]
    title = doc.front_matter.get("title") or doc.title

    headings = [b for b in blocks if b.kind == "heading" and b.level > 0]
    title_blocks = {id(b) for b in blocks if b.kind == "heading" and b.level == 0}
    levels = sorted({h.level for h in headings})
    if len(levels) > 1 and headings[0].level == levels[0] and sum(h.level == levels[0] for h in headings) == 1:
        title_blocks.add(id(headings[0]))  # single top heading = document title
        title = title or headings[0].text
        levels = levels[1:]
    section_level = levels[0] if levels else None
    clause_level = levels[1] if len(levels) > 1 else None

    clauses: list[Clause] = []
    section_parts: dict[int, list[str]] = {}
    section, section_no, section_id = "", 0, "S0"
    synthetic_count: dict[int, int] = {}
    current: Clause | None = None

    def new_clause(heading: Block | None) -> Clause:
        if heading is not None and (num := heading_number(heading.text)):
            c = Clause(num, heading.text, section, section_no)
        elif heading is not None:  # unnumbered heading
            n = synthetic_count[section_no] = synthetic_count.get(section_no, 0) + 1
            c = Clause(f"S{section_id}.{n}", heading.text, section, section_no, synthetic=True)
        elif section_no:           # text between a section heading and its first clause
            c = Clause(section_id, f"{section} (introduction)", section, section_no, synthetic=True)
        else:                      # text before the first section
            c = Clause("S0", "Introduction", "", 0, synthetic=True)
        clauses.append(c)
        return c

    for b in blocks:
        if id(b) in title_blocks:
            continue
        is_heading = b.kind == "heading" and b.level > 0
        if is_heading and b.level == section_level:
            section_no += 1
            section = b.text
            section_id = heading_number(b.text) or str(section_no)
            section_parts[section_no] = [block_markdown(b, 2)]
            # single-level document: the section heading is also the clause heading
            current = new_clause(b) if clause_level is None else None
            continue
        is_clause = is_heading and b.level == clause_level
        section_parts.setdefault(section_no, []).append(block_markdown(b, 3 if is_clause else 4))
        if is_clause:
            current = new_clause(b)
            continue
        if current is None:
            current = new_clause(None)
        current.blocks.append(b)

    clauses = [c for c in clauses if c.blocks]
    _dedupe_ids(clauses)
    section_texts = {no: "\n\n".join(p for p in parts if p).strip() for no, parts in section_parts.items()}
    return Structure(title=title, clauses=clauses, section_texts=section_texts)


def _dedupe_ids(clauses: list[Clause]) -> None:
    seen: dict[str, int] = {}
    for c in clauses:
        if c.clause_id in seen:
            seen[c.clause_id] += 1
            c.clause_id = f"{c.clause_id}-{seen[c.clause_id]}"
        else:
            seen[c.clause_id] = 1


def source_name(stored_path: str | Path) -> str:
    """'3f2a9c1b7e44_home_policy.pdf' (upload storage name) -> 'home_policy.pdf'."""
    name = Path(stored_path).name
    prefix, _, rest = name.partition("_")
    return rest if len(prefix) == 12 and rest and all(ch in "0123456789abcdef" for ch in prefix) else name
