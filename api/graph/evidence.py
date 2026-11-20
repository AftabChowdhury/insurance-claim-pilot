"""verify_evidence node (first node of the claim graph): checks the uploaded images and OCRs scanned PDFs.

Per image (validated + fingerprinted at upload, api/evidence.py):
1. Reuse: same or near-identical photo (sha256 / dHash) in another claim.
2. Metadata: photo taken before the loss / long after it, editing or generator software, AI markers, no EXIF.
3. Vision model (sanitised copy only): image type, damage, matches the declared loss, visible text, invoice data,
   manipulation hints, text addressed to an AI (-> prompt injection, handled by guard_input).
4. Consistency: invoice total vs claimed amount, document date vs loss date.
5. Verdict: verified | suspicious | unusable | unverified (vision unavailable).

Signals carry a fraud weight (used by fraud_agent) and a strength; any strong signal or >= 2 weak signals make the
image suspicious, which sends the claim to the admin (guard_output). The customer never sees these results.
"""
import asyncio
import base64
import logging
import re
from datetime import date, datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from core import db
from core.config import get_settings
from core.domain import label_for, money
from core.llm import chat

from . import prompts
from .schemas import ImageEvidence
from .state import ClaimState, event

log = logging.getLogger(__name__)

EDITOR_RE = re.compile(r"photoshop|lightroom|gimp|affinity|pixelmator|snapseed|picsart|canva|facetune|"
                       r"paint\.net|fotor|luminar|remini|faceapp", re.IGNORECASE)
GENERATOR_RE = re.compile(r"dall[-·\s]?e|midjourney|stable\s*diffusion|firefly|imagen|flux|comfyui|"
                          r"automatic1111|invokeai|novelai|leonardo|ideogram|gpt-?4o|sora", re.IGNORECASE)

# code -> (fraud weight, strong). Strong signals alone make an image suspicious.
SIGNALS = {
    "duplicate_other_customer": (0.4, True),
    "duplicate_same_customer": (0.25, True),
    "ai_generated": (0.2, True),
    "embedded_instructions": (0.2, True),
    "taken_before_loss": (0.2, True),
    "does_not_match_loss": (0.15, True),
    "edited_software": (0.2, False),
    "taken_long_after_loss": (0.05, False),
    "amount_mismatch": (0.15, False),
    "document_date_before_loss": (0.1, False),
    "manipulation_hint": (0.1, False),
    "screenshot": (0.05, False),
    "no_exif": (0.05, False),
}
MAX_EVIDENCE_TEXT = 4000


def _signal(code: str, message: str, file: str, excerpt: str = "") -> dict:
    """message goes into agent prompts and must never quote customer text; excerpt is shown to the admin only."""
    weight, strong = SIGNALS[code]
    return {"code": code, "message": message, "file": file, "weight": weight, "strong": strong, "excerpt": excerpt}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip()[:19], fmt).date()
        except ValueError:
            continue
    return None


def _image_message(text: str, path: str) -> HumanMessage:
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "high"}},
    ])


async def _vision(state: ClaimState, doc: dict, sem: asyncio.Semaphore) -> ImageEvidence | None:
    system = prompts.IMAGE_EVIDENCE.format(loss_type=label_for(state["loss_type"]),
                                           loss_date=state.get("loss_date") or "unknown", name=doc["name"][:80])
    try:
        async with sem:
            return await chat().with_structured_output(ImageEvidence).ainvoke(
                [SystemMessage(system), _image_message("Examine this evidence image.", doc["image"]["sanitised_path"])])
    except Exception as e:  # noqa: BLE001 - the claim continues; the image stays "unverified"
        log.warning("Vision analysis failed for %s/%s: %s", state["claim_id"], doc["name"], e)
        return None


def _metadata_signals(state: ClaimState, doc: dict, loss: date | None) -> list[dict]:
    name, exif = doc["name"], doc["image"].get("exif", {})
    out = []
    software = exif.get("software") or ""
    if exif.get("ai_markers") or GENERATOR_RE.search(software):
        out.append(_signal("ai_generated", f"AI-generation marker in metadata: "
                           f"{', '.join(exif.get('ai_markers') or []) or software}", name))
    elif EDITOR_RE.search(software):
        out.append(_signal("edited_software", f"Edited with {software}", name))
    taken = _parse_date(exif.get("date_taken"))
    if taken and loss:
        if taken < loss:
            out.append(_signal("taken_before_loss", f"Photo taken {taken} – before the loss date {loss}", name))
        elif (taken - loss).days > 60:
            out.append(_signal("taken_long_after_loss", f"Photo taken {taken} – {(taken - loss).days} days after "
                               f"the loss", name))
    if not exif.get("has_exif"):
        out.append(_signal("no_exif", "No camera metadata (screenshot, download or stripped by a messenger)", name))
    return out


def _duplicate_signals(state: ClaimState, doc: dict) -> list[dict]:
    img = doc["image"]
    out = []
    matches = db.find_similar_images(img["sha256"], img["dhash"], state["claim_id"],
                                     get_settings().image_duplicate_distance)
    for m in matches[:3]:
        same = m.get("customer_id") and m["customer_id"] == state.get("customer_id")
        how = "identical file" if m["exact"] else f"near-identical photo (distance {m['distance']})"
        code = "duplicate_same_customer" if same else "duplicate_other_customer"
        who = "the same customer" if same else "another customer"
        out.append(_signal(code, f"{how} already used in claim {m['claim_id']} ({m['name']}) by {who}", doc["name"]))
    return out


def _vision_signals(state: ClaimState, doc: dict, v: ImageEvidence, loss: date | None) -> list[dict]:
    name = doc["name"]
    out = []
    if v.embedded_instructions.strip():
        out.append(_signal("embedded_instructions", "Text addressed to an AI inside the image (withheld)", name,
                           excerpt=v.embedded_instructions[:300]))
    if v.matches_loss_type == "no" or v.image_type == "unrelated":
        out.append(_signal("does_not_match_loss", f"Image does not show a {label_for(state['loss_type'])} loss "
                           f"({v.image_type}, damage: {v.damage_type})", name))
    if v.manipulation_indicators:
        out.append(_signal("manipulation_hint", "Possible manipulation: " + "; ".join(v.manipulation_indicators[:3]),
                           name))
    if v.image_type == "screenshot":
        out.append(_signal("screenshot", "Screenshot instead of an original photo / document", name))
    claimed = float(state.get("amount") or 0)
    if v.image_type == "invoice_receipt" and v.total_amount and v.currency.upper() in ("", "EUR", "€"):
        if claimed > v.total_amount * 1.10:
            out.append(_signal("amount_mismatch", f"Claimed {money(claimed)} but the invoice total is "
                               f"{money(v.total_amount)}", name))
    doc_date = _parse_date(v.document_date)
    if doc_date and loss and doc_date < loss and v.image_type in ("invoice_receipt", "police_report"):
        out.append(_signal("document_date_before_loss", f"{v.image_type.replace('_', ' ')} dated {doc_date}, before "
                           f"the loss date {loss}", name))
    return out


def _verdict(signals: list[dict], v: ImageEvidence | None) -> str:
    weak = [s for s in signals if not s["strong"] and s["code"] != "no_exif"]  # missing EXIF alone is common
    if any(s["strong"] for s in signals) or len(weak) >= 2:
        return "suspicious"
    if v is None:
        return "unverified"
    if v.image_type == "unusable" or v.quality in ("blurry", "too_dark"):
        return "unusable"
    return "verified"


def _evidence_text(e: dict) -> str:
    """Text of an image for the agents (and for guard_input's injection scan)."""
    v = e.get("vision")
    if not v:
        return f"[Image {e['name']}: not analysed]"
    parts = [f"[Image evidence: {v['image_type']}] {v['description']}"]
    if v["damaged_items"]:
        parts.append("Damaged items: " + ", ".join(v["damaged_items"]))
    if v["vendor"] or v["total_amount"]:
        parts.append(f"Invoice: {v['vendor'] or '?'} · date {v['document_date'] or '?'} · total "
                     f"{v['total_amount'] or '?'} {v['currency']}")
    if v["visible_text"]:
        parts.append("Visible text:\n" + v["visible_text"])
    return "\n".join(parts)[:MAX_EVIDENCE_TEXT]


async def _ocr_pdf(doc: dict, sem: asyncio.Semaphore) -> str | None:
    """OCR the first pages of a scanned PDF with the vision model."""
    import pymupdf

    s = get_settings()
    out_dir = Path(doc["path"]).parent / "_sanitised"
    out_dir.mkdir(exist_ok=True)
    pages = []
    try:
        with pymupdf.open(doc["path"]) as pdf:
            for i, page in enumerate(pdf):
                if i >= s.claim_pdf_ocr_max_pages:
                    break
                png = out_dir / f"{Path(doc['name']).stem}_p{i + 1}.jpg"
                page.get_pixmap(dpi=150).save(str(png), jpg_quality=85)
                pages.append(png)

        async def one(p: Path) -> str:
            async with sem:
                msg = await chat().ainvoke([SystemMessage(prompts.PDF_OCR), _image_message("Transcribe this page.",
                                                                                         str(p))])
            return msg.content if isinstance(msg.content, str) else str(msg.content)

        texts = await asyncio.gather(*[one(p) for p in pages])
        return "\n\n".join(t.strip() for t in texts if t.strip()) or None
    except Exception as e:  # noqa: BLE001
        log.warning("OCR failed for %s: %s", doc["name"], e)
        return None


async def verify_evidence(state: ClaimState) -> dict:
    s = get_settings()
    loss = _parse_date(state.get("loss_date"))
    sem = asyncio.Semaphore(s.vision_concurrency)
    docs = [dict(d) for d in state.get("documents", [])]
    images = [d for d in docs if d.get("extraction") == "image" and d.get("image")]
    scanned = [d for d in docs if d.get("extraction") == "ocr_pending" and d["name"].lower().endswith(".pdf")]

    analysed = images[:s.claim_images_max] if s.claim_vision else []
    readings = await asyncio.gather(*[_vision(state, d, sem) for d in analysed])
    vision = {d["name"]: r for d, r in zip(analysed, readings)}
    if s.claim_vision and scanned:
        texts = await asyncio.gather(*[_ocr_pdf(d, sem) for d in scanned])
        for d, text in zip(scanned, texts):
            if text:
                d["text"], d["extraction"] = text, "pdf_ocr"

    evidence, injections = [], []
    for d in images:
        v = vision.get(d["name"])
        signals = _duplicate_signals(state, d) + _metadata_signals(state, d, loss)
        if v:
            signals += _vision_signals(state, d, v, loss)
            if v.embedded_instructions.strip():
                injections.append({"source": d["name"], "excerpt": v.embedded_instructions[:300]})
        exif = d["image"].get("exif", {})
        e = {"name": d["name"], "kind": "image", "verdict": _verdict(signals, v), "signals": signals,
             "vision": v.model_dump() if v else None,
             "exif": {k: exif.get(k) for k in ("date_taken", "make", "model", "software", "gps", "ai_markers",
                                                "has_exif")},
             "width": d["image"]["width"], "height": d["image"]["height"]}
        d["text"] = _evidence_text(e)  # scanned by guard_input, read by the intake agent
        evidence.append(e)
    for d in scanned:
        evidence.append({"name": d["name"], "kind": "scanned_pdf", "signals": [],
                         "verdict": "verified" if d.get("text") else "unusable"})

    counts = {k: sum(e["verdict"] == k for e in evidence) for k in ("verified", "suspicious", "unusable", "unverified")}
    invoice_totals = [e["vision"]["total_amount"] for e in evidence
                      if e.get("vision") and e["vision"]["image_type"] == "invoice_receipt"
                      and e["vision"]["total_amount"] and e["vision"]["currency"].upper() in ("", "EUR", "€")]
    has_text_docs = any(d.get("text") and d.get("extraction") not in ("image",) for d in docs)
    usable_images = any(e["verdict"] != "unusable" for e in evidence if e["kind"] == "image")
    all_signals = [sig for e in evidence for sig in e["signals"]]
    summary = {**counts, "images": len(images), "scanned_pdfs": len(scanned),
               "documented_total": round(sum(invoice_totals), 2) if invoice_totals else None,
               "has_usable_evidence": has_text_docs or usable_images,
               "signals": [f"{sig['file']}: {sig['message']}" for sig in all_signals],
               "signal_codes": sorted({sig["code"] for sig in all_signals}),
               "vision": s.claim_vision}

    if not evidence:
        msg = "No images or scanned documents to verify"
    else:
        msg = (f"{len(images)} image(s), {len(scanned)} scanned PDF(s): {counts['verified']} verified · "
               f"{counts['suspicious']} suspicious · {counts['unusable']} unusable"
               + (f" · {counts['unverified']} unverified" if counts["unverified"] else ""))
    status = "warn" if counts["suspicious"] or injections or counts["unverified"] else "done"
    return {"documents": docs, "evidence": evidence, "evidence_summary": summary, "evidence_injection": injections,
            **event("verify_evidence", msg, status, summary=summary)}


def evidence_for_prompt(state: ClaimState) -> str:
    """Compact, system-generated evidence summary for the coverage and fraud agents."""
    s = state.get("evidence_summary") or {}
    if not s or not (s.get("images") or s.get("scanned_pdfs")):
        return "No images uploaded."
    lines = [f"Images: {s.get('images', 0)} ({s.get('verified', 0)} verified, {s.get('suspicious', 0)} suspicious, "
             f"{s.get('unusable', 0)} unusable, {s.get('unverified', 0)} not analysed)"]
    if s.get("documented_total"):
        lines.append(f"Documented invoice total: {s['documented_total']:.2f} EUR")
    lines += [f"- {x}" for x in s.get("signals", [])[:10]]
    return "\n".join(lines)
