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


def diff_filings(current_accession, previous_accession, section=None) -> dict:
    from . import documents, filings

    try:
        cur = documents.get_sec_filing_text(current_accession, section)
        prev = documents.get_sec_filing_text(previous_accession, section)
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
