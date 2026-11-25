"""Select a small, representative set of reviews as traceable evidence.

Only an id + short snippet is kept (never the bulk text), so summaries stay
small and the ``reviews`` collection remains the system of record. Evidence is
deduped by ``review_id``; each ref records the section it was chosen for.
"""

from __future__ import annotations

from app.reporting.schemas.report_summary import EvidenceRef

_SNIPPET = 220


def _snippet(text: str | None) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return t[:_SNIPPET] + ("…" if len(t) > _SNIPPET else "")


def _ref(r: dict, section: str) -> EvidenceRef:
    return EvidenceRef(
        review_id=str(r.get("review_id")),
        source_record_id=r.get("source_record_id"),
        platform=r.get("platform"),
        product_id=r.get("product_id"),
        brand=r.get("brand"),
        rating=r.get("rating"),
        sentiment=r.get("sentiment"),
        snippet=_snippet(r.get("text")),
        section=section,
    )


def _rank(r: dict):
    # Most helpful first, then most recent.
    return (-(r.get("helpful_votes") or 0), r.get("date") is None, str(r.get("date") or ""))


def select_evidence(reviews: list[dict], per_section: int) -> list[EvidenceRef]:
    neg = sorted((r for r in reviews if r.get("sentiment") in ("negative", "mixed")), key=_rank)
    pos = sorted((r for r in reviews if r.get("sentiment") == "positive"), key=_rank)
    complaints = sorted((r for r in reviews if r.get("complaints")), key=_rank)

    chosen: dict[str, EvidenceRef] = {}

    def add(subset: list[dict], section: str) -> None:
        taken = 0
        for r in subset:
            rid = str(r.get("review_id"))
            if rid in chosen:
                continue
            chosen[rid] = _ref(r, section)
            taken += 1
            if taken >= per_section:
                break

    add(complaints, "complaint_analysis")
    add(neg, "sentiment_analysis")
    add(pos, "sentiment_analysis")

    # A couple per leading brand for brand comparison.
    by_brand: dict[str, list[dict]] = {}
    for r in reviews:
        if r.get("brand"):
            by_brand.setdefault(r["brand"], []).append(r)
    for brand, revs in sorted(by_brand.items(), key=lambda kv: len(kv[1]), reverse=True)[:4]:
        add(sorted(revs, key=_rank), "brand_comparison")

    return list(chosen.values())
