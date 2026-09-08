"""Document-level retrieval off a filing accession."""

from collections.abc import Iterable
from typing import TYPE_CHECKING
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from dataclasses import replace

if TYPE_CHECKING:
    # Provider SDK filing type at the boundary only; never constructed here.
    from edgar import Filing as EdgarFiling
from .models import Filing, FilingDocument, pit_of
from .normalization import document_from_attachment, filing_from_edgar

_MAX_CHARS = 32_000

def get_by_accession_number(accession_no: str) -> EdgarFiling:
    """Seam for tests: monkeypatch this name, never `edgar` itself."""
    from .client import ensure_identity
    from edgar import get_by_accession_number as _get

    ensure_identity()
    return _get(accession_no)


def _filing(accession_no: str) -> EdgarFiling:
    try:
        filing = get_by_accession_number(accession_no)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid accession number: {accession_no!r}") from exc
    if filing is None:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    return filing


def _meta(accession_no: str) -> Filing:
    """Hydrate filing metadata first so exact lookups cannot bypass PIT."""
    return filing_from_edgar(_filing(accession_no))


def _require_known(accession_no: str, as_of: str | None) -> Filing:
    """Validate as_of and reject accessions unknown at that date."""
    from .filings import _check_as_of

    as_of = _check_as_of(as_of)
    if as_of is None:
        return _meta(accession_no)
    meta = _meta(accession_no)
    value, _basis = pit_of(meta)
    if value is None or value[:10] > as_of:
        raise ValueError(
            f"filing {accession_no!r} not known as of {as_of!r}")
    return meta


def list_sec_documents(accession_no: str, as_of: str | None = None) -> list[FilingDocument]:
    meta = _require_known(accession_no, as_of)
    filing = _filing(accession_no)
    out: list[FilingDocument] = []
    for a in filing.attachments:
        doc = document_from_attachment(accession_no, a)
        out.append(replace(
            doc,
            filed_at=meta.filed_at or None,
            accepted_at=meta.accepted_at,
            known_at=meta.known_at or None,
            source_url=meta.source or None,
            is_primary=(doc.document_name is not None
                        and doc.document_name == meta.primary_document),
        ))
    return out


def _resolve_in(filing: EdgarFiling, accession_no: str, document_name: str | None = None) -> object:
    if document_name is None:
        try:
            attachment = filing.document
        except Exception as exc:
            raise ValueError(
                f"no primary document for accession: {accession_no!r}"
            ) from exc
        if attachment is None:
            raise ValueError(f"no primary document for accession: {accession_no!r}")
        return attachment
    try:
        attachments = filing.attachments
    except Exception as exc:
        raise ValueError(f"no documents for accession: {accession_no!r}") from exc
    for attachment in attachments:
        try:
            name = getattr(attachment, "document")
        except Exception:
            continue
        if name == document_name:
            return attachment
    raise ValueError(f"document not found: {document_name!r}")


def _resolve(accession_no: str, document_name: str | None = None) -> object:
    return _resolve_in(_filing(accession_no), accession_no, document_name)


def _text_of(attachment: object) -> str:
    for attr in ("content", "text"):
        try:
            value = getattr(attachment, attr)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, str):
            return value
    return ""


def _raw_root_for(data_root: Path | str | None) -> Path | None:
    """Raw archive root under a data root (tolerates a parquet-root input)."""
    if data_root is None:
        return None
    base = Path(data_root)
    return base / "raw" if base.name != "parquet" else base.parent / "raw"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_window(offset: int | str | float, max_chars: int | str | float | None) -> tuple[int, int | None]:
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        raise ValueError(f"offset must be an integer, got {offset!r}") from None
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if max_chars is None:
        return offset, None
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        raise ValueError(f"max_chars must be an integer, got {max_chars!r}") from None
    if max_chars < 1 or max_chars > _MAX_CHARS:
        raise ValueError(f"max_chars must be 1..{_MAX_CHARS}, got {max_chars}")
    return offset, max_chars


def _bounded_response(*, accession_no: str, document_name: object,
                      description: object, url: object, full_text: str,
                      content_hash: object, source_content_hash: object,
                      source_representation: object, raw_archive_path: object,
                      source_url: object, filed_at: object, known_at: object,
                      retrieved_at: object, offset: int, max_chars: int | None,
                      cache_hit: bool, cache_type: str,
                      warnings: Iterable[str] | None = None) -> dict[str, object]:
    total = len(full_text)
    if offset > total:
        raise ValueError(f"offset {offset} beyond document length {total}")
    end = total if max_chars is None else min(offset + max_chars, total)
    out = {
        "accession_no": accession_no,
        "document_name": document_name,
        "description": description,
        "url": url,
        "text": full_text[offset:end],
        "content_hash": content_hash,
        "source_content_hash": source_content_hash,
        "source_representation": source_representation,
        "raw_archive_path": raw_archive_path,
        "offset": offset,
        "end_offset": end,
        "total_chars": total,
        "more_available": end < total,
        "source_url": source_url,
        "filed_at": filed_at,
        "known_at": known_at,
        "retrieved_at": retrieved_at,
        "cache_hit": cache_hit,
        "cache_type": cache_type,
    }
    if warnings:
        out["warnings"] = list(warnings)
    return out


def _source_bytes_of(attachment: object) -> tuple[bytes | None, str | None]:
    """Exact source bytes when EdgarTools exposes them.

    ``download()`` bytes win, then byte-valued ``content``/``text``.
    Returns (None, None) when only transformed string text is available.
    """
    download = getattr(attachment, "download", None)
    if callable(download):
        try:
            result = download()
        except Exception:
            result = None
        if isinstance(result, bytes):
            return result, "source_bytes"
    for attr in ("content", "text"):
        try:
            value = getattr(attachment, attr)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, bytes):
            return value, "source_bytes"
    return None, None


def _stored_candidates(accession_no: str, document_name: str | None, as_of: str | None, data_root: Path | str | None) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Local archive rows for one accession/document; returns (filing, rows).

    Filing lookup resolves the primary name when document_name is None;
    without a filing row and without a name, an unambiguous single-document
    accession still resolves. Storage failures read as a local miss.
    """
    from . import store as _store

    filings: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    try:
        filings = _store.query_filings(
            accession=accession_no, as_of=as_of, limit=5, root=data_root)
    except Exception:
        filings = []
    filing = filings[0] if filings else None
    filing_name = filing.get("primary_document") if filing else None
    if filing_name is not None and not isinstance(filing_name, str):
        filing_name = str(filing_name)
    effective = document_name or filing_name
    try:
        if effective is not None:
            rows = _store.query_document_text(
                accession=accession_no, document_name=effective,
                as_of=as_of, limit=50, root=data_root)
        elif document_name is None:
            rows = _store.query_document_text(
                accession=accession_no, as_of=as_of, limit=50, root=data_root)
            if len({r.get("document_name") for r in rows}) > 1:
                return filing, []
        else:
            rows = []
    except Exception:
        return filing, []
    return filing, rows


def _pick_revision(rows: list[dict[str, object]], *, as_of: str | None, accession_no: str, document_name: object) -> tuple[dict[str, object] | None, list[str] | None, dict[str, object] | None]:
    """Deterministic revision choice; returns (row, warnings) or an error dict.

    Rows arrive ordered known_at DESC, retrieved_at DESC, content_hash DESC.
    Conflicting hashes at the winning known_at warn on latest reads but are
    a PIT conflict on historical reads: retrieval time must not decide what
    was knowable.
    """
    top_known = rows[0].get("known_at")
    winners = [r for r in rows if r.get("known_at") == top_known]
    if len({r.get("content_hash") for r in winners}) > 1:
        if as_of is not None:
            return None, None, {
                "error": f"conflicting revisions for {accession_no}/{document_name} "
                         f"known at {top_known}; cannot resolve as of {as_of}",
                "error_type": "pit_revision_conflict",
                "accession_no": accession_no,
                "document_name": document_name,
                "known_at": top_known,
                "as_of": as_of,
            }
        note = (f"conflicting revisions for {accession_no}/{document_name} "
                f"known at {top_known}; chose latest retrieved_at/content_hash, "
                f"{len(winners) - 1} revision(s) retained")
        return winners[0], [note], None
    return rows[0], None, None


def get_sec_document(accession_no: str, document_name: str | None = None, as_of: str | None = None, *,
                     offset: int = 0, max_chars: int | None = None,
                     data_root: Path | str | None = None) -> dict[str, object]:
    """Exact document retrieval; EFTS callers pass the matched document name.
    Primary-document fallback applies only when document_name is None.

    Archive-first: stored revisions under the selected root win with
    ``known_at <= as_of``; a local miss fetches through EdgarTools once and
    writes the archive through. Omitted ``max_chars`` returns the full
    normalized text for internal callers; model callers pass a bound.
    """
    from .filings import _check_as_of

    offset, max_chars = _check_window(offset, max_chars)
    as_of = _check_as_of(as_of)
    accession_no = accession_no if isinstance(accession_no, str) else str(accession_no)
    filing_row, rows = _stored_candidates(accession_no, document_name, as_of, data_root)
    if rows:
        display_name = document_name or rows[0].get("document_name")
        row, rev_warnings, conflict = _pick_revision(
            rows, as_of=as_of, accession_no=accession_no,
            document_name=display_name)
        if conflict is not None:
            return conflict
        if row is None:  # Unreachable: a winning revision always yields a row.
            raise ValueError(
                f"no document revision for {accession_no}/{display_name}")
        raw_text = row.get("text")
        full = raw_text if isinstance(raw_text, str) else ""
        source_url = row.get("source_url")
        return _bounded_response(
            accession_no=accession_no,
            document_name=row.get("document_name") or document_name,
            description=None,
            url=source_url or "",
            full_text=full,
            content_hash=row.get("content_hash"),
            source_content_hash=row.get("source_content_hash"),
            source_representation=row.get("source_representation"),
            raw_archive_path=row.get("raw_archive_path"),
            source_url=source_url,
            filed_at=row.get("filed_at"),
            known_at=row.get("known_at"),
            retrieved_at=row.get("retrieved_at"),
            offset=offset, max_chars=max_chars,
            cache_hit=True, cache_type="stockbot_archive",
            warnings=rev_warnings,
        )
    filing = _filing(accession_no)
    meta = filing_from_edgar(filing)
    if as_of is not None:
        value, _basis = pit_of(meta)
        if value is None or value[:10] > as_of:
            raise ValueError(
                f"filing {accession_no!r} not known as of {as_of!r}")
    attachment = _resolve_in(filing, accession_no, document_name)
    if isinstance(attachment, str):
        return {
            "accession_no": accession_no,
            "document_name": attachment,
            "description": None,
            "url": "",
            "text": attachment,
        }
    try:
        name = getattr(attachment, "document")
    except Exception:
        name = None
    try:
        description = getattr(attachment, "description")
    except Exception:
        description = None
    try:
        url = getattr(attachment, "url", None) or ""
    except Exception:
        url = ""
    doc_name = (name if isinstance(name, str) else None) or document_name or \
        meta.primary_document or "primary"
    source_bytes, representation = _source_bytes_of(attachment)
    if source_bytes is None:
        normalized = _text_of(attachment)
        source_bytes = normalized.encode("utf-8")
        representation = "normalized_text"
    else:
        normalized = source_bytes.decode("utf-8", "replace")
    from ..domain.market.ids import sec_doc_id
    from . import archive as _archive
    from . import store as _store

    source_content_hash = hashlib.sha256(source_bytes).hexdigest()
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    doc_id = sec_doc_id("filing-document", f"{accession_no}/{doc_name}", content_hash)
    known_at = meta.known_at or meta.filed_at
    source_url = url or meta.source or None
    warnings: list[str] = []
    raw_path: str | None = None
    retrieved_at: str | None = None
    try:
        record = _archive.archive_sec_document(
            accession_no, doc_name, source_bytes, url=source_url or "",
            metadata={"form": meta.form, "representation": representation},
            root=_raw_root_for(data_root))
        raw_path = str(record.payload_path)
        retrieved_at = record.retrieved_at
        _store.store_document_text(
            doc_id, normalized, accession=accession_no, document_name=doc_name,
            source_url=source_url, raw_archive_path=raw_path,
            source_content_hash=source_content_hash,
            source_representation=representation,
            filed_at=meta.filed_at, known_at=known_at,
            retrieved_at=retrieved_at, root=data_root)
    except Exception as exc:
        warnings.append(f"persistence failed, returning live evidence: {exc}")
        retrieved_at = retrieved_at or _utcnow()
    return _bounded_response(
        accession_no=accession_no,
        document_name=doc_name,
        description=description,
        url=url,
        full_text=normalized,
        content_hash=content_hash,
        source_content_hash=source_content_hash,
        source_representation=representation,
        raw_archive_path=raw_path,
        source_url=source_url,
        filed_at=meta.filed_at,
        known_at=known_at,
        retrieved_at=retrieved_at,
        offset=offset, max_chars=max_chars,
        cache_hit=False, cache_type="live_or_edgartools_http",
        warnings=warnings or None,
    )


def get_sec_filing_text(accession_no: str, document_name: str | None = None, as_of: str | None = None) -> str:
    text = get_sec_document(accession_no, document_name, as_of=as_of)["text"]
    if not isinstance(text, str):
        raise ValueError(f"no text for accession: {accession_no!r}")
    return text



def _exhibit_dict(accession_no: str, attachment: object) -> dict[str, object]:
    def _get(name: str) -> object:
        try:
            return getattr(attachment, name)
        except Exception:
            return None

    url = _get("url") or ""
    return {
        "accession_no": accession_no,
        "exhibit": _get("document_type"),
        "description": _get("description"),
        "document": _get("document"),
        "url": url if isinstance(url, str) else "",
    }


def get_filing_exhibits(accession_no: str) -> list[dict[str, object]]:
    filing = _filing(accession_no)
    exhibits = getattr(filing, "exhibits", None)
    if exhibits is None:
        exhibits = filing.attachments
    return [_exhibit_dict(accession_no, a) for a in exhibits]


def get_filing_exhibit(accession_no: str, exhibit: str) -> dict[str, object]:
    want = exhibit.upper()
    for row in get_filing_exhibits(accession_no):
        if str(row.get("exhibit") or "").upper() == want:
            return row
    raise ValueError(f"exhibit not found: {exhibit!r}")
