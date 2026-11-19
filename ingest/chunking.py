"""Structure-aware chunking (PLAN.md §4, §3.2).

One clause = one chunk, unless the clause is longer than CHUNK_MAX_TOKENS: then it is split on block,
paragraph and sentence boundaries into parts of ~CHUNK_TARGET_TOKENS with CHUNK_OVERLAP_TOKENS overlap
(overlap only between parts of the same clause). Text of different clauses is never mixed.
Tables and figures are atomic; an oversized table is split by rows with the header repeated.
Large figures (workflows, image tables, ...) get their own chunk with the clause's clause_id.

Chunks have the index shape (id, clause_id, title, section, content, parent_content, source, policy_type,
doc_id, chunk_type, chunk_index, page, image_refs, chunker_version) plus embed_text (contextual header +
content, used for the embedding) and tokens (for previews); the indexer drops the last two.

    uv run python -m ingest.chunking path\\to\\policy.pdf [--policy-type home] [--no-vision] [--show]
    uv run python -m ingest.chunking --doc-id 3f2a9c1b7e44
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from core.config import ROOT, get_settings
from ingest.extract.model import Block, ExtractedDocument
from ingest.structure import Clause, Structure, block_markdown, detect_policy_type, detect_structure

CHUNKER_VERSION = "1"
PARENT_MAX_CHARS = 6000
FIGURE_OWN_CHUNK_KINDS = {"workflow", "table", "chart", "form"}
SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+(?=[\"'(§\[]?[A-Z0-9])")
FIGURE_REF_RE = re.compile(r"\b(?:figure|fig\.|abbildung|abb\.|diagram|chart)\s*([A-Z]?\d+(?:\.\d+)*)", re.IGNORECASE)
KEY_RE = re.compile(r"[^A-Za-z0-9_\-=]")


# ----------------------------------------------------------------------------------------------- tokens
@lru_cache
def _encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")  # tokenizer of gpt-4.1 / text-embedding-3
    except Exception:  # offline without cached encoding: fall back to an estimate
        return None


def count_tokens(text: str) -> int:
    enc = _encoder()
    return len(enc.encode(text, disallowed_special=())) if enc else max(1, len(text) // 4)


@dataclass(frozen=True)
class ChunkSettings:
    target: int
    max: int
    overlap: int
    min: int

    @classmethod
    def from_settings(cls) -> "ChunkSettings":
        s = get_settings()
        return cls(s.chunk_target_tokens, s.chunk_max_tokens, s.chunk_overlap_tokens, s.chunk_min_tokens)

    @property
    def version(self) -> str:
        """Stored in every chunk: changes when the chunker or its settings change -> re-index needed."""
        return f"{CHUNKER_VERSION}-t{self.target}-m{self.max}-o{self.overlap}-n{self.min}"


# ----------------------------------------------------------------------------------------------- pieces
@dataclass
class Piece:
    """An atomic unit of a clause: a paragraph/list/table/small figure (with a preceding subheading)."""
    text: str
    kind: str                       # text | table | figure
    page: int | None = None
    image_refs: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


def _split_sentences(text: str, limit: int) -> list[str]:
    """Split text into parts of <= limit tokens on paragraph, line, sentence and finally word boundaries."""
    if count_tokens(text) <= limit:
        return [text]
    for sep, splitter in (("\n\n", lambda t: t.split("\n\n")), ("\n", lambda t: t.split("\n")),
                          (" ", SENTENCE_RE.split), (" ", lambda t: t.split(" "))):
        units = [u for u in splitter(text) if u.strip()]
        if len(units) > 1:
            return _pack([u for unit in units for u in _split_sentences(unit, limit)], limit, sep)
    return [text]  # a single unsplittable token run


def _pack(units: list[str], limit: int, sep: str) -> list[str]:
    parts, current = [], ""
    for u in units:
        candidate = f"{current}{sep}{u}" if current else u
        if current and count_tokens(candidate) > limit:
            parts.append(current)
            current = u
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _split_table(text: str, limit: int) -> list[str]:
    lines = text.splitlines()
    if len(lines) < 3:
        return _split_sentences(text, limit)
    header, rows = "\n".join(lines[:2]), lines[2:]
    budget = max(limit - count_tokens(header), 20)
    return [f"{header}\n{part}" for part in _pack(rows, budget, "\n")]


def _overlap_tail(text: str, overlap: int) -> str:
    """Last sentences of text, up to `overlap` tokens."""
    if overlap <= 0:
        return ""
    tail: list[str] = []
    for sentence in reversed(SENTENCE_RE.split(text.replace("\n", " "))):
        if count_tokens(" ".join([sentence, *tail])) > overlap:
            break
        tail.insert(0, sentence)
    return " ".join(tail)


# ----------------------------------------------------------------------------------------------- chunker
def _is_own_figure(b: Block, cfg: ChunkSettings) -> bool:
    if b.source == "smartart" or (b.figure or {}).get("kind") in FIGURE_OWN_CHUNK_KINDS:
        return True
    return count_tokens(b.text) >= 2 * cfg.min


def _clause_pieces(clause: Clause, cfg: ChunkSettings) -> tuple[list[Piece], list[Block]]:
    """Pieces for the text chunk(s) and the figures that get their own chunk."""
    pieces: list[Piece] = []
    own_figures: list[Block] = []
    pending_heading = ""
    for b in clause.blocks:
        if b.kind == "heading":
            pending_heading = f"{pending_heading}{block_markdown(b)}\n"
            continue
        if b.kind == "figure":
            if b.decorative or not b.text:
                continue
            if _is_own_figure(b, cfg):
                own_figures.append(b)
                continue
        kind = "table" if b.kind == "table" else "figure" if b.kind == "figure" else "text"
        refs = [b.image_path] if b.kind == "figure" and b.image_path else []
        pieces.append(Piece(pending_heading + block_markdown(b), kind, b.page, refs))
        pending_heading = ""
    if pending_heading:
        pieces.append(Piece(pending_heading.strip(), "text"))
    return pieces, own_figures


def _split_piece(p: Piece, limit: int) -> list[Piece]:
    if p.tokens <= limit:
        return [p]
    parts = _split_table(p.text, limit) if p.kind == "table" else _split_sentences(p.text, limit)
    return [Piece(t, p.kind, p.page, p.image_refs) for t in parts]


def _parts(pieces: list[Piece], heading: str, cfg: ChunkSettings) -> list[list[Piece]]:
    """Group pieces into chunk parts (lists of pieces); overlap is added as a leading text piece."""
    head_tokens = count_tokens(heading)
    if sum(p.tokens for p in pieces) + head_tokens <= cfg.max:
        return [pieces]
    budget = max(cfg.target - head_tokens, 50)
    # leave room for the overlap that is prepended to every part after the first
    units = [u for p in pieces for u in _split_piece(p, max(budget - cfg.overlap, 30))]

    parts: list[list[Piece]] = []
    current: list[Piece] = []
    used = 0
    for u in units:
        if current and used + u.tokens > budget:
            parts.append(current)
            last = current[-1]
            tail = _overlap_tail(last.text, cfg.overlap) if last.kind == "text" else ""
            current = [Piece(f"[…] {tail}", "text", last.page)] if tail else []
            used = sum(p.tokens for p in current)
        current.append(u)
        used += u.tokens
    if current:
        parts.append(current)
    # merge a too-small last part into the previous one
    if len(parts) > 1 and sum(p.tokens for p in parts[-1]) < cfg.min:
        tail = [p for p in parts.pop() if not p.text.startswith("[…]")]
        parts[-1].extend(tail)
    return parts


def _rel(path: str) -> str:
    """Image paths are stored relative to the project root (portable between machines / Docker)."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _key(*parts: str) -> str:
    return KEY_RE.sub("_", "-".join(parts).replace(".", "_"))


def chunk_document(doc: ExtractedDocument, doc_id: str, policy_type: str | None = None,
                   source: str | None = None, cfg: ChunkSettings | None = None) -> list[dict]:
    cfg = cfg or ChunkSettings.from_settings()
    source = source or doc.source
    policy_type = detect_policy_type(doc, policy_type, source)
    structure = detect_structure(doc)
    chunks: list[dict] = []

    figure_targets = _figure_reference_targets(structure)

    def add(clause: Clause, content: str, chunk_type: str, page, image_refs, suffix: str):
        parent = structure.section_texts.get(clause.section_no, "")[:PARENT_MAX_CHARS]
        header = " | ".join(x for x in (f"{policy_type} policy", structure.title, clause.section, clause.title) if x)
        chunks.append({
            "id": _key(doc_id, clause.clause_id, suffix),
            "clause_id": clause.clause_id,
            "title": clause.title,
            "section": clause.section,
            "content": content,
            "parent_content": parent,
            "source": source,
            "policy_type": policy_type,
            "doc_id": doc_id,
            "chunk_type": chunk_type,
            "chunk_index": len(chunks),
            "page": page,
            "image_refs": [_rel(p) for p in image_refs],
            "chunker_version": cfg.version,
            "embed_text": f"{header}\n\n{content}",
            "tokens": count_tokens(content),
        })

    for clause in structure.clauses:
        heading = f"### {clause.title}"
        pieces, own_figures = _clause_pieces(clause, cfg)
        parts = _parts(pieces, heading, cfg) if pieces else []
        for n, part in enumerate(parts, 1):
            head = heading if n == 1 else f"{heading} (continued {n}/{len(parts)})"
            body = "\n\n".join(p.text for p in part)
            kinds = {p.kind for p in part if not p.text.startswith("[…]")}
            chunk_type = "table" if kinds == {"table"} else "figure" if kinds == {"figure"} else "text"
            page = next((p.page for p in part if p.page), None)
            refs = list(dict.fromkeys(r for p in part for r in p.image_refs))
            add(clause, f"{head}\n{body}", chunk_type, page, refs, str(n))

        for f_no, fig in enumerate(own_figures, 1):
            target = clause
            if clause.synthetic:  # figure outside a real clause: link it to the clause that refers to it
                target = figure_targets.get(id(fig)) or clause
            texts = _split_sentences(fig.text, cfg.max - count_tokens(heading))
            for part_no, text in enumerate(texts, 1):
                suffix = f"f{f_no}" + (f"_{part_no}" if len(texts) > 1 else "")
                add(target, f"### {target.title}\n{text}", "figure", fig.page,
                    [fig.image_path] if fig.image_path else [], suffix)
    _dedupe_keys(chunks)
    return chunks


def _figure_reference_targets(structure: Structure) -> dict[int, Clause]:
    """Figures in intro/appendix text -> the clause that refers to them ('see Figure 3') or that the vision
    model found in the figure (referenced_clauses). PLAN.md §3.2 point 3."""
    by_number: dict[str, Clause] = {}
    by_clause_id = {c.clause_id: c for c in structure.clauses if not c.synthetic}
    for c in structure.clauses:
        if c.synthetic:
            continue
        for b in c.blocks:
            if b.kind in ("paragraph", "list", "table"):
                for m in FIGURE_REF_RE.finditer(b.text):
                    by_number.setdefault(m.group(1).upper(), c)
    targets: dict[int, Clause] = {}
    for c in structure.clauses:
        if not c.synthetic:
            continue
        for b in c.blocks:
            if b.kind != "figure":
                continue
            m = FIGURE_REF_RE.match(b.caption or "")
            if m and m.group(1).upper() in by_number:
                targets[id(b)] = by_number[m.group(1).upper()]
            elif refs := [r for r in (b.figure or {}).get("referenced_clauses", []) if r in by_clause_id]:
                targets[id(b)] = by_clause_id[refs[0]]
    return targets


def _dedupe_keys(chunks: list[dict]) -> None:
    seen: set[str] = set()
    for c in chunks:
        key, n = c["id"], 1
        while c["id"] in seen:
            n += 1
            c["id"] = f"{key}_{n}"
        seen.add(c["id"])


# ----------------------------------------------------------------------------------------------- CLI
def main() -> None:
    import argparse
    import asyncio
    import json
    import sys

    from core import db
    from ingest.extract import ExtractionError, extract, extract_document
    from ingest.structure import source_name

    ap = argparse.ArgumentParser(prog="python -m ingest.chunking")
    ap.add_argument("file", nargs="?", type=Path)
    ap.add_argument("--doc-id", help="id of an admin-uploaded policy document")
    ap.add_argument("--policy-type", help="home | auto | general (default: front matter / file name)")
    ap.add_argument("--no-vision", action="store_true", help="extract without vision model")
    ap.add_argument("--reextract", action="store_true", help="ignore an existing extracted.json")
    ap.add_argument("--show", action="store_true", help="print chunk contents")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    s = get_settings()

    if args.doc_id:
        record = db.get_policy_doc(args.doc_id)
        if not record:
            sys.exit(f"Unknown doc_id {args.doc_id}")
        path, work_dir, doc_id = db.policy_doc_path(record), s.policy_upload_dir / args.doc_id, args.doc_id
        source = record["filename"]
    elif args.file:
        path, work_dir = args.file, s.policy_upload_dir / "_preview" / args.file.name.replace(".", "_")
        doc_id, source = "preview-" + args.file.stem, source_name(args.file)
    else:
        ap.error("give a file or --doc-id")

    extracted = work_dir / "extracted.json"
    try:
        if extracted.is_file() and not args.reextract:
            doc = ExtractedDocument.load(extracted)
        elif args.no_vision:
            work_dir.mkdir(parents=True, exist_ok=True)
            doc = extract(path, work_dir)
            doc.save(extracted)
        else:
            doc = asyncio.run(extract_document(path, work_dir))
    except ExtractionError as e:
        sys.exit(f"Extraction failed: {e}")

    chunks = chunk_document(doc, doc_id, args.policy_type, source)
    (work_dir / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False, indent=1), encoding="utf-8")
    for c in chunks:
        print(f"{c['chunk_index']:3} {c['clause_id']:8} {c['chunk_type']:6} {c['tokens']:4} tok  "
              f"p{c['page'] or '-':<3} {c['id']}  | {c['title'][:60]}")
        if args.show:
            print("    " + c["content"].replace("\n", "\n    ") + "\n")
    tokens = [c["tokens"] for c in chunks] or [0]
    print(f"\n--- {len(chunks)} chunks, policy_type={chunks[0]['policy_type'] if chunks else '-'}, "
          f"tokens min/avg/max = {min(tokens)}/{sum(tokens) // len(tokens)}/{max(tokens)}, "
          f"version {ChunkSettings.from_settings().version}")
    print(f"Saved {work_dir / 'chunks.json'}")


if __name__ == "__main__":
    main()
