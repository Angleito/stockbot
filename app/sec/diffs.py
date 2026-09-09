"""Deterministic filing diffs (stdlib difflib only; never an LLM)."""

from __future__ import annotations

import difflib

_MAX_LINES = 500


def _specialization(forms: list[str]) -> str:
    f = {x.upper().replace("-", "").replace(" ", "") for x in forms}
    if f <= {"10K", "10Q", "10KA", "10QA"}:
        return "10-K/10-Q"
    blob = " ".join(f)
    if "13D" in blob:
        return "13D/A"
    if "13G" in blob:
        return "13G/A"
    if "S1" in blob:
        return "S-1/A"
    if "S3" in blob:
        return "S-3/A"
    if any(x in blob for x in ("14A", "14C", "PX14A")):
        return "proxy"
    if any(x in blob for x in ("SCTO", "14D9", "13E3", "S4")):
        return "tender/merger"
    return "generic"


def _resolve_section(accession_no: str, section: str | None) -> str | None:
    """Map the caller's ``section`` to a real document name (or None).

    None/''/'full' diff the primary document. Any other value fuzzy-matches
    the filing's document names (case-insensitive exact, then substring);
    on a miss the error names real documents so the caller can recover via
    list_sec_documents instead of guessing again. Filing-text headings
    (``risk_factors``, ``Item 1A``) are never documents: the error says so.
    """
    if section is None or not str(section).strip() or str(section).strip().lower() == "full":
        return None
    from . import documents

    want = str(section).strip()
    try:
        names = [d.document_name for d in documents.list_sec_documents(accession_no)
                 if d.document_name]
    except Exception as exc:
        raise ValueError(f"cannot list documents for {accession_no!r}: {exc}") from exc
    lowered = want.lower()
    for name in names:
        if name.lower() == lowered:
            return name
    hits = [n for n in names if lowered in n.lower() or n.lower() in lowered]
    if len(hits) == 1:
        return hits[0]
    sample = ", ".join(names[:12])
    raise ValueError(
        f"document not found: {want!r} for accession {accession_no!r}; "
        f"available documents: {sample or 'none'}. Omit 'section' to diff "
        f"the primary document, or pick one of the listed names. Filing-text "
        f"headings such as 'risk_factors' or 'Item 1A' are sections inside "
        f"the primary document, not documents: page it with get_sec_document.")

def diff_filings(
    current_accession: str,
    previous_accession: str,
    section: str | None = None,
) -> dict[str, object]:
    from . import documents, filings

    try:
        cur_name = _resolve_section(current_accession, section)
        prev_name = _resolve_section(previous_accession, section)
        cur = documents.get_sec_filing_text(current_accession, cur_name)
        prev = documents.get_sec_filing_text(previous_accession, prev_name)
        forms = [
            filings.get_sec_filing(a).form
            for a in (current_accession, previous_accession)
        ]
    except Exception as exc:
        return {"error": str(exc)}
    lines = list(
        difflib.unified_diff(
            prev.splitlines(), cur.splitlines(), lineterm="", n=3
        )
    )
    truncated = len(lines) > _MAX_LINES
    body = lines[:_MAX_LINES]
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "current_accession": current_accession,
        "previous_accession": previous_accession,
        "section": section,
        "specialization": _specialization(forms),
        "diff_lines": body,
        "added": added,
        "removed": removed,
        "truncated": truncated,
    }
