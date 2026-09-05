"""Exhaustive SEC discovery orchestration over the existing SEC surface.

CIK stays an SEC identifier mapped to the provider-independent Stockbot
``entity_id`` (``app/domain/market/ids.py``); there is no ``SECCompany``
class and no second entity store. Name comparison is deterministic stdlib
only (``unicodedata`` + ``re`` + ``difflib``); ``difflib`` ranks fuzzy
candidates but ties or fuzzy-only evidence stay ``ambiguous`` — never
first-result wins. Person expansion is exact plus honorific/middle-initial
stripping only: no nicknames are ever generated.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

from ...domain.market.ids import sec_entity_id
from ..filings import _check_as_of
from ..models import (
    EntityCandidate,
    FilingParty,
    SECSearchRequest,
    SECSearchResult,
    SearchAttempt,
    SearchCoverage,
    pit_of,
)

PARSER_VERSION = "1"
SOURCE = "sec-submissions"

# Trailing legal-form tokens stripped for the second comparison key only;
# the displayed source name is always preserved.
_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "company", "co",
    "ltd", "limited", "llc", "pllc", "llp", "lp", "lllp", "plp", "plc",
    "pa", "pc", "sa", "ag", "gmbh", "nv", "bv", "spa", "ab", "asa", "as",
    "aps", "oy", "oyj", "sarl", "sas", "srl", "sl", "pty", "pte", "pvt",
    "bhd", "sdn", "ltda",
})

# Rank order: exact ticker > exact name > normalized > historical > fuzzy.
_TIER_RANK = {
    "exact_cik": 0,
    "exact_ticker": 0,
    "exact_name": 1,
    "normalized": 2,
    "historical": 3,
    "fuzzy": 4,
}

_FUZZY_FLOOR = 0.6

_ACCESSION_RE = re.compile(r"^(\d{10})-?(\d{2})-?(\d{6})$")


def normalize_name(value: object) -> str:
    """Deterministic name key: NFKD, casefold, punctuation/whitespace collapse."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def stripped_name_key(value: object) -> str:
    """Second comparison key with trailing legal suffixes removed."""
    parts = normalize_name(value).split(" ")
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(part for part in parts if part)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_accession_no(value: object) -> str:
    """Canonical dashed accession; accepts dashed or undashed input once."""
    if not isinstance(value, str):
        raise ValueError(f"invalid accession number: {value!r}")
    match = _ACCESSION_RE.match(value.strip())
    if not match:
        raise ValueError(f"invalid accession number: {value!r}")
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _former_valid_at(entry: dict, as_of: str | None) -> bool:
    """Half-open [from, to) validity; null bounds are unbounded."""
    if as_of is None:
        return True
    start = entry.get("from") or None
    end = entry.get("to") or None
    return (start is None or str(start)[:10] <= as_of) and (
        end is None or as_of < str(end)[:10])


def _classify_name(query: str, current: object,
                   former_names: list | None,
                   as_of: str | None) -> tuple:
    """Query vs current/former names -> (match_type|None, score, warnings).

    ``None`` match_type means no evidence (or only PIT-excluded historical
    evidence, which is reported via warnings and never resolves).
    """
    warnings: list[str] = []
    want, want_stripped = normalize_name(query), stripped_name_key(query)
    if not want:
        return None, 0.0, tuple(warnings)
    have, have_stripped = normalize_name(current), stripped_name_key(current)
    if want == have:
        return "exact_name", 1.0, tuple(warnings)
    if want_stripped and want_stripped == have_stripped:
        return "normalized", 0.9, tuple(warnings)
    former = former_names or []
    if any(not (entry.get("from") or entry.get("to"))
           for entry in former if isinstance(entry, dict)):
        warnings.append(
            "former name(s) lack effective dates; historical alias "
            "coverage is source-limited")
    best: tuple = (None, 0.0)
    for entry in former:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("name") or ""
        cand, cand_stripped = normalize_name(raw), stripped_name_key(raw)
        if (want == cand) or (want_stripped and want_stripped == cand_stripped):
            if _former_valid_at(entry, as_of):
                return "historical", 0.8, tuple(warnings)
            warnings.append(
                f"former name {str(raw)!r} outside PIT interval "
                f"[{entry.get('from') or '?'}..{entry.get('to') or '?'}) "
                f"for as_of {as_of}")
            continue
        if cand:
            score = _ratio(want, cand)
            if score > best[1]:
                best = ("fuzzy", score)
    if best[0] is not None and best[1] >= _FUZZY_FLOOR:
        return best[0], best[1], tuple(warnings)
    if have:
        score = _ratio(want, have)
        if score >= _FUZZY_FLOOR:
            return "fuzzy", score, tuple(warnings)
    return None, 0.0, tuple(warnings)


def _candidate(cik: int | None, name: str, tickers: tuple,
               match_source: str, match_type: str, score: float,
               status: str, entity_id: str | None = None) -> EntityCandidate:
    return EntityCandidate(
        cik=cik, name=name, tickers=tuple(tickers), exchange=None,
        match_source=match_source, match_score=score, match_type=match_type,
        verification_status=status, entity_id=entity_id,
    )


def _verify_against(meta: dict, *, expected_name=None,
                    expected_ticker=None, as_of=None) -> tuple:
    """Shared strict verifier -> (EntityCandidate, warnings).

    Either expectation contradicting authoritative metadata is ``conflict``;
    fuzzy-only name evidence is ``ambiguous`` (may not map to an entity);
    otherwise ``verified`` with the mechanical ``sec:cik:`` entity id.
    """
    cik = meta.get("cik")
    current = meta.get("name") or ""
    tickers = tuple(meta.get("tickers") or [])
    warnings: list[str] = []
    if expected_name is None and expected_ticker is None:
        return _candidate(cik, str(current), tickers, SOURCE,
                          "exact_cik", 1.0, "verified",
                          sec_entity_id(cik)), tuple(warnings)
    ticker_ok: bool | None = None
    if expected_ticker is not None:
        want = str(expected_ticker).strip().upper()
        ticker_ok = bool(want) and want in [str(t).strip().upper() for t in tickers]
    match_type: str | None = None
    score = 0.0
    if expected_name is not None:
        match_type, score, name_warnings = _classify_name(
            str(expected_name), current, meta.get("former_names"), as_of)
        warnings.extend(name_warnings)
    if (ticker_ok is False) or (expected_name is not None and match_type is None):
        return _candidate(cik, str(current), tickers, SOURCE,
                          match_type or ("exact_ticker" if ticker_ok else ""),
                          score, "conflict"), tuple(warnings)
    if match_type == "fuzzy":
        return _candidate(cik, str(current), tickers, SOURCE,
                          "fuzzy", score, "ambiguous"), tuple(warnings)
    if ticker_ok:
        return _candidate(cik, str(current), tickers, SOURCE,
                          match_type or "exact_ticker",
                          score or 1.0, "verified",
                          sec_entity_id(cik)), tuple(warnings)
    assert match_type is not None  # guarded by the conflict branch above
    return _candidate(cik, str(current), tickers, SOURCE,
                      match_type, score, "verified",
                      sec_entity_id(cik)), tuple(warnings)


def verify_sec_entity(cik: int | str, *, expected_name=None,
                      expected_ticker=None, as_of=None) -> EntityCandidate:
    """Verify one CIK against submissions metadata.

    Missing CIK -> ``not_found``; contradicting expectations -> ``conflict``;
    fuzzy-only evidence -> ``ambiguous`` with no entity id. Only ``verified``
    candidates carry the canonical ``entity_id``.
    """
    from ..client import get_submissions_metadata

    as_of = _check_as_of(as_of)
    try:
        cik_int: int | None = int(str(cik).strip())
    except (TypeError, ValueError, AttributeError):
        cik_int = None
    if cik_int is None:
        return _candidate(None, str(expected_name or ""), (), SOURCE,
                          "", 0.0, "not_found")
    meta = get_submissions_metadata(cik_int)
    if meta is None:
        return _candidate(cik_int, str(expected_name or ""), (), SOURCE,
                          "", 0.0, "not_found")
    candidate, _warnings = _verify_against(
        meta, expected_name=expected_name,
        expected_ticker=expected_ticker, as_of=as_of)
    return candidate


def _persist_entity(meta: dict, *, now: str) -> str | None:
    """Persist a verified CIK into entities/entity_aliases; warning or None.

    Rows conform to ``resolve_ticker_aliases`` PIT semantics: ticker aliases
    are currently valid (null bounds) and visible from ``known_at``; former
    names keep SEC ``from``/``to`` (null when missing). Extra keys are
    dropped by the warehouse writer, so only dataset columns are sent.
    """
    try:
        from ...storage.parquet import write_rows

        cik = meta.get("cik")
        entity_id = sec_entity_id(cik)
        sic = meta.get("sic")
        write_rows("entities", [{
            "entity_id": entity_id,
            "name": meta.get("name"),
            "entity_type": meta.get("entity_type"),
            "sic": None if sic is None else str(sic),
            "source": SOURCE,
            "known_at": now,
            "retrieved_at": now,
            "content_hash": None,
            "parser_version": PARSER_VERSION,
        }])
        aliases = []
        for ticker in meta.get("tickers") or []:
            if str(ticker).strip():
                aliases.append({
                    "alias_type": "ticker",
                    "alias_value": str(ticker).strip(),
                    "entity_id": entity_id,
                    "security_id": None,
                    "source": SOURCE,
                    "valid_from": None,
                    "valid_to": None,
                    "known_at": now,
                    "retrieved_at": now,
                    "content_hash": None,
                    "parser_version": PARSER_VERSION,
                })
        for entry in meta.get("former_names") or []:
            if isinstance(entry, dict) and (entry.get("name") or "").strip():
                aliases.append({
                    "alias_type": "former_name",
                    "alias_value": str(entry["name"]).strip(),
                    "entity_id": entity_id,
                    "security_id": None,
                    "source": SOURCE,
                    "valid_from": entry.get("from") or None,
                    "valid_to": entry.get("to") or None,
                    "known_at": now,
                    "retrieved_at": now,
                    "content_hash": None,
                    "parser_version": PARSER_VERSION,
                })
        if aliases:
            write_rows("entity_aliases", aliases)
    except Exception as exc:
        return f"entity persistence skipped for CIK {meta.get('cik')}: {exc}"
    return None


def find_sec_entities(query: str, *, as_of=None,
                      exhaustive: bool = True) -> SECSearchResult:
    """Fan out over exact-CIK, exact-ticker, and general legal-name routes.

    General names merge the no-ticker ``cik-lookup-data.txt`` scan with the
    ticker-company index by CIK; each pooled CIK loads submissions once and
    is classified exact/normalized/historical/fuzzy. Ties and fuzzy-only tops
    stay ``ambiguous``; verified candidates persist to the entity store.
    """

    from ..client import (
        find_sec_company,
        get_cik_lookup_candidates,
        get_submissions_metadata,
        resolve_cik,
    )

    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"invalid query: {query!r}")
    as_of = _check_as_of(as_of)
    query = query.strip()
    search_id = uuid.uuid4().hex[:12]
    request = SECSearchRequest(
        query=query, as_of=as_of, exhaustive=exhaustive)
    attempts: list[SearchAttempt] = []
    warnings: list[str] = []
    errors: list[str] = []
    now = _utcnow()

    def _attempt(backend: str, reported: int, retrieved: int,
                 status: str = "complete", error: Exception | None = None,
                 pit_basis: str | None = None) -> None:
        attempts.append(SearchAttempt(
            attempt_id=f"{search_id}-{backend}",
            search_id=search_id,
            backend=backend,
            query=query,
            filters={"as_of": as_of} if as_of else {},
            started_at=now,
            completed_at=now,
            status=status,  # type: ignore[arg-type]
            results_reported=reported,
            results_retrieved=retrieved,
            pages_retrieved=1,
            truncated=False,
            pit_basis=pit_basis,
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        ))

    ranked: list[tuple] = []  # (tier_rank, -score, cik, EntityCandidate)
    pool: dict[int, dict] = {}
    failed = 0

    def _add(cik: object, name: object, tickers: list, source: str) -> None:
        try:
            cik_int = int(str(cik).strip())
        except (TypeError, ValueError, AttributeError):
            return
        slot = pool.setdefault(
            cik_int, {"name": "", "tickers": [], "sources": []})
        if name and not slot["name"]:
            slot["name"] = str(name)
        for ticker in tickers or []:
            if ticker and ticker not in slot["tickers"]:
                slot["tickers"].append(ticker)
        if source not in slot["sources"]:
            slot["sources"].append(source)

    metas: dict[int, dict] = {}
    if query.isdigit():
        backend = "exact-cik"
        try:
            cik_int: int | None = int(query)
        except ValueError:
            cik_int = None
        if cik_int is None:
            _attempt(backend, 0, 0, status="failed")
            failed += 1
        else:
            candidate = verify_sec_entity(cik_int, as_of=as_of)
            ok = candidate.verification_status == "verified"
            _attempt(backend, 1 if ok else 0, 1 if ok else 0,
                     pit_basis="known_at" if as_of else None)
            candidate = replace(candidate, match_source="exact-cik")
            if ok:
                ranked.append((0, -1.0, cik_int, candidate))
                meta = get_submissions_metadata(cik_int)
                if meta is not None:
                    metas[cik_int] = meta
            else:
                ranked.append((5, 0.0, cik_int, candidate))
                errors.append(f"CIK {query} not found in SEC submissions")
    else:
        try:
            ticker_cik = resolve_cik(query)
        except Exception as exc:  # defensive: resolve_cik never raises today
            ticker_cik = None
            errors.append(f"exact-ticker route failed: {exc}")
            _attempt("exact-ticker", 0, 0, status="failed", error=exc)
            failed += 1
        if ticker_cik is None:
            if not any(a.backend == "exact-ticker" for a in attempts):
                _attempt("exact-ticker", 0, 0)
        else:
            _add(ticker_cik, "", [query.strip().upper()], "exact-ticker")
            _attempt("exact-ticker", 1, 1)
        fetch_limit = 50 if exhaustive else 10
        for backend, fetch in (
            ("cik-lookup", lambda: get_cik_lookup_candidates(
                query, limit=fetch_limit)),
            ("company-search", lambda: find_sec_company(
                query, limit=fetch_limit)),
        ):
            try:
                found = fetch()
            except ValueError:
                raise
            except Exception as exc:
                _attempt(backend, 0, 0, status="failed", error=exc)
                errors.append(f"{backend} route failed: {exc}")
                failed += 1
                continue
            for row in found:
                _add(row.get("cik"), row.get("name"),
                     list(row.get("tickers") or []), backend)
            _attempt(backend, len(found), len(found))
        for cik_int, slot in sorted(pool.items()):
            try:
                meta = get_submissions_metadata(cik_int)
            except Exception:
                meta = None
            if meta is None:
                continue
            tickers = tuple(meta.get("tickers") or [])
            if query.strip().upper() in [str(t).strip().upper() for t in tickers]:
                candidate, _w = _verify_against(
                    meta, expected_ticker=query, as_of=as_of)
                candidate = replace(candidate, match_source="+".join(
                    sorted(slot["sources"])) or "exact-ticker")
            else:
                candidate, name_warnings = _verify_against(
                    meta, expected_name=query, as_of=as_of)
                warnings.extend(name_warnings)
                candidate = replace(candidate, match_source="+".join(
                    sorted(slot["sources"])) or "company-search")
            # Conflict (name contradicts authoritative metadata) stays out.
            tier = _TIER_RANK.get(candidate.match_type, 5)
            if candidate.verification_status != "conflict":
                ranked.append((tier, -candidate.match_score, cik_int, candidate))
                metas[cik_int] = meta

    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    entities: list[EntityCandidate] = [item[3] for item in ranked]
    if len(entities) > 1:
        top_tier, top_score = ranked[0][0], ranked[0][1]
        tied = [item for item in ranked
                if item[0] == top_tier and item[1] == top_score]
        if len(tied) > 1:
            tied_ciks = {item[2] for item in tied}
            warnings.append(
                f"{len(tied)} candidates tie at "
                f"{tied[0][3].match_type} score "
                f"{tied[0][3].match_score:.3f}; marking ambiguous")
            entities = [
                replace(item[3], verification_status="ambiguous",
                        entity_id=None)
                if item[2] in tied_ciks else item[3]
                for item in ranked
            ]
    if entities and entities[0].match_type == "fuzzy":
        warnings.append("top match is fuzzy-only; marking ambiguous")
        first = entities[0]
        entities[0] = replace(
            first, verification_status="ambiguous", entity_id=None)
    final_by_cik = {e.cik: e for e in entities}
    for cik_int, meta in metas.items():
        if final_by_cik.get(cik_int, None) is not None and final_by_cik[cik_int].verification_status == "verified":
            note = _persist_entity(meta, now=now)
            if note:
                warnings.append(note)
    if not entities and not errors:
        warnings.append(f"no SEC entity candidates for {query!r}")
    if failed and not entities:
        status: str = "failed"
    elif failed:
        status = "partial"
    else:
        status = "complete"
    sources = tuple(a.backend for a in attempts)
    packet = build_evidence_packet(search_id, entities=tuple(entities))
    return SECSearchResult(
        search_id=search_id,
        request=request,
        entities=tuple(entities),
        coverage=SearchCoverage(
            status=status,  # type: ignore[arg-type]
            sources_attempted=sources,
            sources_completed=tuple(
                a.backend for a in attempts if a.status == "complete"),
            sources_failed=tuple(
                a.backend for a in attempts if a.status == "failed"),
            results_reported=len(pool) if pool else len(entities),
            results_retrieved=len(entities),
        ),
        attempts=tuple(attempts),
        warnings=tuple(warnings),
        errors=tuple(errors),
        retrieval_order=sources,
        evidence_packet_ids=packet,
    )


def resolve_sec_accession(accession_no: str, *, as_of=None) -> SECSearchResult:
    """Exact accession lookup: normalize once, bypass fuzzy discovery.

    Returns filer/form/timestamps, the document inventory, and ``FilingParty``
    rows (carried in ``relationships`` until typed datasets land). An
    accession unknown at ``as_of`` is rejected with ``failed`` coverage —
    never zero-match silence.
    """

    from ..documents import list_sec_documents
    from ..filings import get_sec_filing
    from ..models import pit_of

    as_of = _check_as_of(as_of)
    normalized = normalize_accession_no(accession_no)
    search_id = uuid.uuid4().hex[:12]
    request = SECSearchRequest(
        accession_no=normalized, as_of=as_of, exhaustive=False)
    now = _utcnow()
    try:
        filing = get_sec_filing(normalized, as_of=as_of)
    except ValueError as exc:
        attempt = SearchAttempt(
            attempt_id=f"{search_id}-exact-accession",
            search_id=search_id,
            backend="exact-accession",
            query=normalized,
            filters={"as_of": as_of} if as_of else {},
            started_at=now,
            completed_at=now,
            status="failed",
            error_type="NotFound" if "not known as of" in str(exc)
            else type(exc).__name__,
            error_message=str(exc),
        )
        return SECSearchResult(
            search_id=search_id,
            request=request,
            coverage=SearchCoverage(
                status="failed",
                sources_attempted=("exact-accession",),
                sources_failed=("exact-accession",),
            ),
            attempts=(attempt,),
            errors=(str(exc),),
            retrieval_order=("exact-accession",),
        )
    try:
        documents = list_sec_documents(normalized, as_of=as_of)
    except ValueError as exc:
        documents = []
        doc_warning = (f"document inventory unavailable: {exc}",)
    else:
        doc_warning = ()
    _pit_value, pit_basis = pit_of(filing)
    parties: list[FilingParty] = [FilingParty(
        accession_no=filing.accession_no,
        entity_id=sec_entity_id(filing.filer_cik),
        cik=filing.filer_cik,
        name=filing.filer_name,
        role="filer",
        source=filing.source,
        known_at=filing.known_at,
        parser_version=PARSER_VERSION,
    )]
    if (filing.subject_cik is not None or filing.subject_name) and (
            filing.subject_cik != filing.filer_cik
            or (filing.subject_name or "") != (filing.filer_name or "")):
        parties.append(FilingParty(
            accession_no=filing.accession_no,
            entity_id=sec_entity_id(filing.subject_cik)
            if filing.subject_cik is not None else None,
            cik=filing.subject_cik,
            name=filing.subject_name or "",
            role="subject",
            source=filing.source,
            known_at=filing.known_at,
            parser_version=PARSER_VERSION,
        ))
    filer = EntityCandidate(
        cik=filing.filer_cik,
        name=filing.filer_name,
        tickers=(),
        exchange=None,
        match_source="exact-accession",
        match_score=1.0,
        match_type="exact_cik",
        verification_status="unverified",
        entity_id=sec_entity_id(filing.filer_cik),
    )
    attempt = SearchAttempt(
        attempt_id=f"{search_id}-exact-accession",
        search_id=search_id,
        backend="exact-accession",
        query=normalized,
        filters={"as_of": as_of} if as_of else {},
        started_at=now,
        completed_at=now,
        status="complete",
        results_reported=1,
        results_retrieved=1,
        pages_retrieved=1,
        pit_basis=pit_basis,
    )
    return SECSearchResult(
        search_id=search_id,
        request=request,
        entities=(filer,),
        filings=(filing,),
        documents=tuple(documents),
        relationships=tuple(parties),
        coverage=SearchCoverage(
            status="complete",
            sources_attempted=("exact-accession",),
            sources_completed=("exact-accession",),
            results_reported=1,
            results_retrieved=1,
            forms_covered=(filing.form,),
        ),
        attempts=(attempt,),
        warnings=doc_warning,
        retrieval_order=("exact-accession",),
    )


# Form families only EXPAND routable global queries (person/proxy/13F
# fan-out); unknown form strings always pass through untouched, never rejected.
_PERSON_FORMS = (
    "3", "4", "5", "144",
    "SC 13D", "SC 13G",
    "DEF 14A", "DEFM14A", "PREM14A",
    "13F-HR",
)

_HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "mx", "rev", "hon",
})

# ponytail: at most 8 quarterly partitions per interactive search; deeper
# history is Phase 5 backfill work, never a blocked model-visible call.
_GLOBAL_QUARTER_CAP = 8

# Interactive search never waits for unbounded history: missing partitions
# become bounded quarterly/form backfill jobs in this priority order.
BACKFILL_PRIORITY = (
    "8-K", "10-K", "10-Q", "13D", "13G", "13F-HR",
    "3", "4", "5", "144",
    "S-1", "S-3", "424B", "S-4", "DEF 14A", "D",
)
BACKFILL_SOURCE = "sec-global"
# SEC global quarterly indexes start in 1993; the current quarter always
# comes from the current feed, never a quarterly partition.
SEC_GLOBAL_START = (1993, 1)

_EVIDENCE_MAX_ITEMS = 20
_EVIDENCE_MAX_CHARS = 8000

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _expand_entity_queries(entities, as_of=None):
    """Verified entities -> deterministic text variants for EFTS/global routes.

    Current legal name, legal-name comparison form, ticker(s), and former
    names valid/known for ``as_of``. CIKs route to the filer-submissions
    backend only (callers use ``entity.cik`` directly); they are never
    emitted as text variants.
    """
    as_of = _check_as_of(as_of)
    from ..client import get_submissions_metadata

    variants: list[str] = []
    seen: set[str] = set()

    def _push(value: object) -> None:
        text = str(value or "").strip()
        key = normalize_name(text)
        if text and key and key not in seen:
            seen.add(key)
            variants.append(text)

    for entity in entities or ():
        if getattr(entity, "verification_status", None) != "verified":
            continue
        name = (getattr(entity, "name", "") or "").strip()
        if name:
            _push(name)
            _push(stripped_name_key(name))
        for ticker in getattr(entity, "tickers", None) or ():
            if str(ticker).strip():
                _push(str(ticker).strip().upper())
        cik = getattr(entity, "cik", None)
        if cik is None:
            continue
        try:
            meta = get_submissions_metadata(cik)
        except Exception:
            meta = None
        if not isinstance(meta, dict):
            continue
        _push(meta.get("name") or "")
        for entry in meta.get("former_names") or []:
            if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
                continue
            try:
                valid = _former_valid_at(entry, as_of)
            except Exception:
                valid = True
            if not valid:
                continue
            _push(entry["name"])
            _push(stripped_name_key(entry["name"]))
    return variants


def _expand_person_queries(name: str) -> list[str]:
    """Exact name plus honorific-stripped plus middle-initial-stripped forms.

    Every variant stays a separate attempt with its own provenance; no
    nicknames, no suffix handling, no fuzzy variants.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"invalid person name: {name!r}")
    exact = re.sub(r"\s+", " ", name.strip())
    variants = [exact]
    tokens = exact.split(" ")
    if len(tokens) > 1 and tokens[0].rstrip(".").casefold() in _HONORIFICS:
        variants.append(" ".join(tokens[1:]))
    parts = variants[-1].split(" ")
    middle = [tok for i, tok in enumerate(parts)
              if i == 0 or i == len(parts) - 1
              or not (tok.rstrip(".").isalpha() and len(tok.rstrip(".")) == 1)]
    squashed = " ".join(middle)
    if squashed and squashed not in variants:
        variants.append(squashed)
    return variants


def _expand_domain_queries(domain: str) -> list[str]:
    """Lowercase hostname plus bare/``www.``/literal-``@`` text variants."""
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError(f"invalid domain: {domain!r}")
    raw = domain.strip().lower()
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)
    raw = re.split(r"[/?#]", raw, maxsplit=1)[0]
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]
    raw = raw.split(":")[0].rstrip(".").strip()
    if not raw or "." not in raw or re.search(r"\s", raw):
        raise ValueError(f"invalid domain: {domain!r}")
    bare = raw[4:] if raw.startswith("www.") else raw
    return list(dict.fromkeys((bare, f"www.{bare}", f"@{bare}")))


def _expand_security_queries(identifier: str) -> list[str]:
    """Ticker/CUSIP/ISIN/class-title text variants; never entity identity.

    No identifier resolution happens here: a ``Security`` stays separate from
    its issuer ``Entity``, so expansion yields only case variants and the
    service creates no entity candidate from them.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f"invalid security identifier: {identifier!r}")
    exact = re.sub(r"\s+", " ", identifier.strip())
    upper = exact.upper()
    return [exact] if upper == exact else [exact, upper]


def _quarters_for_range(start_date, end_date, *, cap=_GLOBAL_QUARTER_CAP):
    """(start, end) -> ([(year, quarter)] oldest-first, capped?).

    Empty when unbounded (caller uses the current feed instead of quarterly
    partitions). Partitions before 1993-Q1 are dropped (no global index);
    the current quarter is excluded (the current feed covers it).
    Over-wide ranges keep the most recent ``cap`` quarters.
    """
    if not start_date and not end_date:
        return [], False

    def _parse(value: object, label: str) -> tuple[int, int]:
        if not isinstance(value, str) or not _DATE_RE.match(value):
            raise ValueError(
                f"invalid {label} date: {value!r} (expected YYYY-MM-DD)")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(
                f"invalid {label} date: {value!r} (expected YYYY-MM-DD)") from None
        return parsed.year, (parsed.month - 1) // 3 + 1

    now = datetime.now(timezone.utc)
    current = (now.year, (now.month - 1) // 3 + 1)
    if start_date and end_date:
        low = _parse(start_date, "start")
        high = _parse(end_date, "end")
    elif start_date:
        low = _parse(start_date, "start")
        high = current
    else:
        low = SEC_GLOBAL_START
        high = _parse(end_date, "end")
    if low > high:
        raise ValueError(
            f"invalid date range: {start_date!r}..{end_date!r}")
    if high < SEC_GLOBAL_START:
        return [], False
    if low < SEC_GLOBAL_START:
        low = SEC_GLOBAL_START
    year, quarter = low
    quarters: list[tuple[int, int]] = []
    while (year, quarter) <= high:
        if (year, quarter) != current:
            quarters.append((year, quarter))
        quarter += 1
        if quarter > 4:
            quarter, year = 1, year + 1
    if len(quarters) > cap:
        return quarters[len(quarters) - cap:], True
    return quarters, False


def _quarter_dates(year: int, quarter: int) -> tuple[str, str]:
    """Quarter partition -> (start_date, end_date) YYYY-MM-DD bounds."""
    starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
    ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    return f"{year}-{starts[quarter]}", f"{year}-{ends[quarter]}"


def _partition_for_quarter(year: int, quarter: int) -> str:
    return f"{year}-Q{quarter}"


def _sort_forms_by_priority(forms) -> list[str]:
    """Requested forms first in BACKFILL_PRIORITY order, unknown forms last."""
    order = {name.upper(): i for i, name in enumerate(BACKFILL_PRIORITY)}

    def _key(form: str) -> tuple:
        return (order.get(str(form).upper(), len(order)), str(form).upper())
    return sorted(forms, key=_key)


def _filing_from_local_row(row: dict):
    """Warehouse row -> ``Filing`` for covered-partition local reads."""
    from ..models import Filing

    def _opt_int(value) -> int | None:
        if value is None or str(value) == "":
            return None
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    cik = _opt_int(row.get("filer_cik", row.get("cik")))
    return Filing(
        accession_no=str(row.get("accession")),
        form=str(row.get("form") or ""),
        filer_cik=cik or 0,
        filer_name=str(row.get("filer_name") or row.get("company") or ""),
        filed_at=str(row.get("filed_at") or ""),
        accepted_at=row.get("accepted_at"),
        known_at=str(row.get("known_at") or row.get("filed_at") or ""),
        report_period=row.get("report_period"),
        primary_document=row.get("primary_document"),
        is_amendment=bool(row.get("is_amendment")),
        amendment_of=row.get("amendment_of"),
        source=str(row.get("source_url") or ""),
        subject_cik=_opt_int(row.get("subject_cik", row.get("issuer_cik"))),
        subject_name=row.get("subject_name"),
    )

_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None


def _raw_root_for(data_root) -> object:
    if data_root is None:
        return None
    from pathlib import Path as _Path
    base = _Path(data_root)
    return base / "raw" if base.name != "parquet" else base.parent / "raw"


def run_backfill_job(job: dict, data_root=None) -> bool:
    """Drain one leased job: archive + normalize + coverage + checkpoint."""
    from .. import archive as _archive
    from .. import store as _store
    from ..client import get_current_filings, get_global_filings

    source, form = str(job["source"]), str(job["form"])
    batch_size = max(int(job.get("batch_size") or 50), 1)
    quarters, _ = _quarters_for_range(
        job.get("start_date"), job.get("end_date"), cap=10_000)
    now = datetime.now(timezone.utc)
    current = (now.year, (now.month - 1) // 3 + 1)
    # Index rows carry no document payloads, so no typed relationship parser
    # can run here; relationship forms stay filings-only partial coverage
    # (never speculative parsing).
    filings_only = str(form).strip().upper() in (
        "SC 13D", "SC 13G", "13D", "13G", "3", "4", "5", "13F-HR")
    current_partition = f"{current[0]}-Q{current[1]}"
    try:
        targets = list(quarters) or [None]
        last_key: str | None = None
        total = 0
        for target in targets:
            if target is None:
                partition = f"{current[0]}-Q{current[1]}"
                key = f"{form}/{partition}"
                prior = _store.get_checkpoint(
                    "sec-backfill", source, key, root=data_root)
                if prior and prior.get("status") == "complete":
                    continue
                rows = get_current_filings(form, page_size=batch_size)
                feed_snapshot = True
            else:
                year, quarter = target
                partition = _partition_for_quarter(year, quarter)
                key = f"{form}/{partition}"
                prior = _store.get_checkpoint(
                    "sec-backfill", source, key, root=data_root)
                if prior and prior.get("status") == "complete":
                    if not _store.is_partition_covered(
                            source, form, partition, root=data_root):
                        _store.store_coverage(
                            source, form, partition,
                            "partial" if (
                                filings_only or partition
                                == current_partition) else "complete",
                            coverage_date=job.get("end_date"), root=data_root)
                    continue
                if (year, quarter) == current:
                    rows = get_current_filings(form, page_size=batch_size)
                    feed_snapshot = True
                else:
                    rows = get_global_filings(year, quarter, form=form)
                    feed_snapshot = False
            # Feed snapshots are bounded samples, never full-quarter coverage;
            # quarterly partitions drain fully within the job, so "complete"
            # means the source was exhausted (never claimed after truncation).
            batch = list(rows)[:batch_size] if feed_snapshot else list(rows)
            for filing in batch:
                payload = json.dumps(
                    filing.to_dict(), sort_keys=True, default=str).encode()
                records = _archive.archive_sec_filing(
                    filing, {"submission": payload},
                    url=filing.source or "", root=_raw_root_for(data_root))
                _store.store_filing(
                    filing,
                    raw_submission_path=getattr(
                        records.get("submission"), "payload_path", None),
                    raw_primary_path=getattr(
                        records.get("primary"), "payload_path", None),
                    root=data_root)
                last_key = filing.accession_no
            total += len(batch)
            if feed_snapshot or filings_only:
                _store.store_coverage(
                    source, form, partition, "partial",
                    coverage_date=job.get("end_date"),
                    accession_count=len(batch), last_key=last_key,
                    root=data_root)
                _store.store_checkpoint(
                    "sec-backfill", source, key, "partial",
                    last_key=last_key, record_count=len(batch),
                    totals={"filings_only": filings_only,
                            "feed_snapshot": feed_snapshot},
                    root=data_root)
            else:
                _store.store_coverage(
                    source, form, partition, "complete",
                    coverage_date=job.get("end_date"),
                    accession_count=len(batch), last_key=last_key,
                    root=data_root)
                _store.advance_checkpoint(
                    "sec-backfill", source, key, last_key=last_key,
                    record_count=len(batch), root=data_root)
        _store.complete_job(job["id"], last_key=last_key, root=data_root)
        return True
    except Exception as exc:
        try:
            _store.fail_job(job["id"], str(exc), root=data_root)
        except Exception:
            pass
        try:
            _store.store_checkpoint(
                "sec-backfill", source, f"{form}/{job.get('start_date')}:"
                f"{job.get('end_date')}", "failed", error=str(exc),
                root=data_root)
        except Exception:
            pass
        return False


def drain_backfill_queue(data_root=None, max_jobs: int | None = None) -> dict:
    """Synchronously claim->ingest queued jobs (CLI/resume path)."""
    from .. import store as _store

    _store.recover_stale_jobs(root=data_root)
    done, failed = 0, 0
    while True:
        if max_jobs is not None and done + failed >= max_jobs:
            break
        job = _store.claim_job(root=data_root)
        if job is None:
            break
        if run_backfill_job(job, data_root):
            done += 1
        else:
            failed += 1
    return {"completed": done, "failed": failed}


def ensure_backfill_worker(data_root=None) -> threading.Thread | None:
    """Start the single process-local daemon draining the durable queue."""
    global _WORKER_THREAD
    from .. import store as _store

    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return _WORKER_THREAD
        _store.recover_stale_jobs(root=data_root)

        def _drain() -> None:
            while True:
                try:
                    job = _store.claim_job(root=data_root)
                except Exception:
                    return
                if job is None:
                    return
                try:
                    run_backfill_job(job, data_root)
                except Exception:
                    continue

        # ponytail: one daemon thread per process; later enqueues restart it.
        _WORKER_THREAD = threading.Thread(
            target=_drain, name="sec-backfill", daemon=True)
        _WORKER_THREAD.start()
        return _WORKER_THREAD


def rank_hits(hits, *, verified_ciks=(), verified_names=(),
              relevant_forms=()) -> tuple:
    """Rank after retrieval: identity > phrase > form relevance > recency > score.

    Returns the hits tuple in rank order; nothing is discarded (low-ranked
    structured results stay queryable, the packet alone is bounded).
    """
    ciks = {c for c in verified_ciks or () if c is not None}
    names = {normalize_name(n) for n in verified_names or () if n}
    forms = {str(f).strip().upper() for f in relevant_forms or () if str(f).strip()}

    def _filed(hit) -> str:
        return str(getattr(hit, "filed_at", "") or "")

    def _score(hit) -> float:
        try:
            return float(getattr(hit, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _identity(hit) -> int:
        filer_name = getattr(hit, "filer_name", None) or ""
        return 0 if (getattr(hit, "filer_cik", None) in ciks
                     or (filer_name and normalize_name(filer_name) in names)) else 1

    def _phrase(hit) -> int:
        query = getattr(hit, "query", "") or ""
        filer_name = getattr(hit, "filer_name", None) or ""
        return 0 if (query and filer_name
                     and normalize_name(query) == normalize_name(filer_name)) else 1

    def _relevance(hit) -> int:
        form = str(getattr(hit, "form", "") or "").strip().upper()
        return 0 if (form and form in forms) else 1

    by_recency = sorted(hits or (), key=lambda h: (_filed(h), _score(h)), reverse=True)
    return tuple(sorted(by_recency,
                        key=lambda h: (_identity(h), _phrase(h), _relevance(h))))


def build_evidence_packet(search_id: str, *, entities=(), filings=(),
                          text_hits=(), max_items=_EVIDENCE_MAX_ITEMS,
                          max_chars=_EVIDENCE_MAX_CHARS) -> tuple:
    """Bounded packet IDs (verified entities, ranked hits, filings); lists stay full.

    Only the packet is context-budgeted; the stored search keeps every
    entity/filing/hit queryable.
    """
    ids: list[str] = []
    budget = 0

    def _push(packet_id: str) -> bool:
        nonlocal budget
        # ponytail: flat ~120-char weight per item instead of serializing bodies.
        if len(ids) >= max_items or budget + len(packet_id) + 120 > max_chars:
            return False
        ids.append(packet_id)
        budget += len(packet_id) + 120
        return True

    for entity in entities or ():
        if getattr(entity, "verification_status", None) != "verified":
            continue
        cik = getattr(entity, "cik", None)
        if cik is not None:
            packet_id = f"{search_id}-entity-cik-{cik}"
        else:
            packet_id = (f"{search_id}-entity-"
                         f"{normalize_name(getattr(entity, 'name', ''))[:40]}")
        if not _push(packet_id):
            return tuple(ids)
    for hit in text_hits or ():
        doc = getattr(hit, "matched_document", None) or "primary"
        if not _push(f"{search_id}-hit-{getattr(hit, 'accession_no', '')}-{doc}"):
            return tuple(ids)
    for filing in filings or ():
        if not _push(f"{search_id}-filing-{getattr(filing, 'accession_no', '')}"):
            return tuple(ids)
    return tuple(ids)


class SECDiscoveryService:
    """Cross-source discovery owner: accession, entity, EFTS, submissions,
    global, then local routes; inapplicable routes are explicit attempts."""

    def __init__(self, data_root=None):
        self._data_root = data_root

    def search(self, request: SECSearchRequest, *,
               evidence_max_items: int = _EVIDENCE_MAX_ITEMS,
               evidence_max_chars: int = _EVIDENCE_MAX_CHARS) -> SECSearchResult:
        """Run every applicable route; dedup, rank after retrieval, bound packet."""
        data_root = self._data_root
        if not isinstance(request, SECSearchRequest):
            raise TypeError(
                f"request must be SECSearchRequest, got {type(request).__name__}")
        as_of = _check_as_of(request.as_of)
        search_id = uuid.uuid4().hex[:12]
        now = _utcnow()
        # Interactive bound for global/current-feed reads and backfill batches.
        batch_size = max(int(request.max_results or 50), 1)
        attempts: list[SearchAttempt] = []
        warnings: list[str] = []
        errors: list[str] = []
        entities: dict = {}
        filings: dict = {}
        documents: dict = {}
        relationships: dict = {}
        hits: dict = {}
        retrieval_order: list[str] = []
        pit_gaps = 0
        quarter_capped = False
        pending: list[str] = []
        adopted_limits: list[str] = []
        adopted_not_complete: list[str] = []

        def _record(backend: str, query: str, status: str, *,
                    reported: int = 0, retrieved: int = 0, pages: int = 0,
                    pit_basis: str | None = None, error=None,
                    source_limit: str | None = None,
                    filters: dict | None = None) -> None:
            entry_filters = dict(filters or {})
            if as_of and "as_of" not in entry_filters:
                entry_filters["as_of"] = as_of
            attempts.append(SearchAttempt(
                attempt_id=f"{search_id}-{backend}-{len(attempts) + 1}",
                search_id=search_id,
                backend=backend,
                query=query,
                filters=entry_filters,
                started_at=now,
                completed_at=now,
                status=status,  # type: ignore[arg-type]
                results_reported=reported,
                results_retrieved=retrieved,
                pages_retrieved=pages,
                truncated=status in ("partial", "source_limited"),
                source_limit=source_limit,
                pit_basis=pit_basis,
                error_type=type(error).__name__ if error is not None else None,
                error_message=str(error) if error is not None else None,
            ))
            if backend not in retrieval_order:
                retrieval_order.append(backend)

        def _merge_entity(candidate) -> None:
            key = (candidate.cik if candidate.cik is not None
                   else candidate.entity_id or f"name:{normalize_name(candidate.name)}")
            if key not in entities:
                entities[key] = candidate

        def _keep(record) -> bool:
            nonlocal pit_gaps
            if as_of is None:
                return True
            value, _basis = pit_of(record)
            if value is None or value[:10] > as_of:
                pit_gaps += 1
                return False
            return True

        def _adopt(sub: SECSearchResult, *, route: str | None = None) -> None:
            idmap = {}
            for attempt in sub.attempts:
                aid = f"{search_id}-{attempt.backend}-{len(attempts) + 1}"
                idmap[attempt.attempt_id] = aid
                entry_filters = dict(attempt.filters or {})
                if route is not None:
                    entry_filters.setdefault("route", route)
                attempts.append(replace(
                    attempt, attempt_id=aid, search_id=search_id,
                    filters=entry_filters))
                if attempt.status == "failed" and attempt.error_message:
                    errors.append(
                        f"{attempt.backend} {attempt.query}: {attempt.error_message}")
                if attempt.backend not in retrieval_order:
                    retrieval_order.append(attempt.backend)
            coverage = getattr(sub, "coverage", None)
            if coverage is not None:
                for limit in getattr(coverage, "source_limits", None) or ():
                    if limit not in adopted_limits:
                        adopted_limits.append(limit)
                if getattr(coverage, "status", "complete") != "complete":
                    adopted_not_complete.append(
                        str(getattr(coverage, "status")))
            for warning in sub.warnings:
                if warning not in warnings:
                    warnings.append(warning)
            for candidate in sub.entities:
                _merge_entity(candidate)
            for filing in sub.filings:
                filings.setdefault(filing.accession_no, filing)
            for document in sub.documents:
                documents.setdefault(
                    (document.accession_no, document.document_name), document)
            for party in sub.relationships:
                relationships.setdefault(
                    (party.accession_no, party.role, party.cik, party.name), party)
            for hit in sub.text_hits:
                key = (hit.query, hit.accession_no, hit.matched_document)
                if key not in hits:
                    hits[key] = replace(
                        hit, search_id=search_id,
                        attempt_id=idmap.get(hit.attempt_id, hit.attempt_id))

        # Route 1: exact accession first.
        if request.accession_no:
            try:
                _adopt(resolve_sec_accession(
                    request.accession_no, as_of=as_of), route="accession")
            except ValueError as exc:
                _record("exact-accession", str(request.accession_no),
                        "failed", error=exc)
                errors.append(str(exc))
        else:
            _record("exact-accession", "no accession_no", "not_applicable")

        # Route 2: exact CIK/ticker/name entity routes. Explicit cik/ticker
        # selectors resolve alongside (never silently shadowed by) the text
        # query; the text fallback below still uses entity_query.
        entity_query = (request.query or request.company_name
                        or request.ticker or request.cik)
        entity_selectors: list[str] = []
        for selector in (request.cik, request.ticker):
            text = str(selector).strip() if selector is not None else ""
            if text and all(text != seen for seen in entity_selectors):
                entity_selectors.append(text)
        if entity_query is not None:
            text = str(entity_query).strip()
            if text and all(text != seen for seen in entity_selectors):
                entity_selectors.append(text)
        verified: list = []
        if entity_selectors and request.search_entities:
            for selector in entity_selectors:
                try:
                    _adopt(find_sec_entities(
                        selector, as_of=as_of,
                        exhaustive=request.exhaustive), route="entity")
                except ValueError as exc:
                    _record("entity-discovery", selector,
                            "failed", error=exc)
                    errors.append(str(exc))
            verified = [e for e in entities.values()
                        if e.verification_status == "verified"]
        elif entity_query is None:
            _record("entity-discovery", "no query/ticker/cik/company_name",
                    "not_applicable")
        else:
            _record("entity-discovery", str(entity_query),
                    "not_applicable", filters={"reason": "disabled by request"})

        # Route 3: EFTS text/topic/person/domain/security variants.
        variants: list[tuple[str, str]] = []

        def _add_variants(items, route: str) -> None:
            for item in items or ():
                text = str(item).strip()
                if text and all(text != seen for seen, _ in variants):
                    variants.append((text, route))

        if request.search_documents:
            if entity_query is not None:
                _add_variants(
                    _expand_entity_queries(verified, as_of) if verified else [],
                    "entity")
                _add_variants([str(entity_query).strip()], "text")
            if request.person_name is not None:
                try:
                    _add_variants(
                        _expand_person_queries(request.person_name), "person")
                except ValueError as exc:
                    _record("efts", str(request.person_name), "failed",
                            error=exc, filters={"route": "person"})
                    errors.append(str(exc))
            elif entity_query is None and request.domain is None \
                    and request.security_identifier is None:
                _record("person-search", "no person_name", "not_applicable")
            if request.domain is not None:
                try:
                    domain_variants = _expand_domain_queries(request.domain)
                except ValueError as exc:
                    _record("efts", str(request.domain), "failed",
                            error=exc, filters={"route": "domain"})
                    errors.append(str(exc))
                else:
                    _add_variants(domain_variants, "domain")
                    # Mention-backed only: never verified, never an entity id.
                    _merge_entity(EntityCandidate(
                        cik=None, name=domain_variants[0], tickers=(),
                        exchange=None, match_source="domain-mention",
                        match_score=0.0, match_type="text_mention",
                        verification_status="unverified", entity_id=None))
            elif entity_query is None and request.person_name is None \
                    and request.security_identifier is None:
                _record("domain-search", "no domain", "not_applicable")
            if request.security_identifier is not None:
                try:
                    _add_variants(_expand_security_queries(
                        request.security_identifier), "security")
                except ValueError as exc:
                    _record("efts", str(request.security_identifier), "failed",
                            error=exc, filters={"route": "security"})
                    errors.append(str(exc))
                # Security identity stays separate: no entity candidate.
            elif entity_query is None and request.person_name is None \
                    and request.domain is None:
                _record("security-search", "no security_identifier",
                        "not_applicable")
            # Bounded live EFTS: pages up to the request limit per variant;
            # unretrieved remainder stays partial with the Route 5 quarterly
            # jobs as continuation work (never an unbounded history wait).
            if variants:
                from ..client import search_sec_filings

                forms = list(request.forms) if request.forms else None
                per_variant = request.max_results or 20
                for variant, route in variants:
                    try:
                        _adopt(search_sec_filings(
                            variant, forms=forms,
                            start_date=request.start_date,
                            end_date=request.end_date, limit=per_variant,
                            as_of=as_of), route=route)
                    except Exception as exc:
                        _record("efts", variant, "failed",
                                error=exc, filters={"route": route})
                        errors.append(f"efts {variant!r} failed: {exc}")
            elif not any(a.backend == "efts" for a in attempts):
                _record("efts", "no text/person/domain/security query",
                        "not_applicable")
        else:
            _record("efts", "disabled by request", "not_applicable")
            if request.person_name is not None:
                _record("person-search", "disabled by request", "not_applicable")
            if request.domain is not None:
                _record("domain-search", "disabled by request", "not_applicable")
            if request.security_identifier is not None:
                _record("security-search", "disabled by request",
                        "not_applicable")

        # Route 4: filer submissions for known entities.
        if verified and request.search_documents:
            from ..filings import list_sec_filings

            sub_forms = list(request.forms) if request.forms else None
            for candidate in verified:
                try:
                    rows = list_sec_filings(
                        candidate.cik, forms=sub_forms,
                        start_date=request.start_date, end_date=request.end_date,
                        as_of=as_of, limit=request.max_results or 50)
                except Exception as exc:
                    _record("filer-submissions", str(candidate.cik), "failed",
                            error=exc,
                            pit_basis="known_at" if as_of else None)
                    errors.append(
                        f"filer-submissions {candidate.cik} failed: {exc}")
                    continue
                _record("filer-submissions", str(candidate.cik), "complete",
                        reported=len(rows), retrieved=len(rows), pages=1,
                        pit_basis="known_at" if as_of else None,
                        filters={"forms": sub_forms,
                                 "start_date": request.start_date,
                                 "end_date": request.end_date})
                for filing in rows:
                    filings.setdefault(filing.accession_no, filing)
        elif not verified:
            _record("filer-submissions", "no verified entity", "not_applicable")
        else:
            _record("filer-submissions", "disabled by request", "not_applicable")

        # Route 5: global filing indexes for forms/relationships.
        global_forms: list[str] = []
        for form in request.forms or ():
            text = str(form or "").strip()
            if text and all(text.upper() != seen.upper() for seen in global_forms):
                global_forms.append(text)
        if request.search_relationships and request.person_name:
            for form in _PERSON_FORMS:
                if all(form.upper() != seen.upper() for seen in global_forms):
                    global_forms.append(form)
        if global_forms and request.search_documents:
            try:
                quarters, quarter_capped = _quarters_for_range(
                    request.start_date, request.end_date)
            except ValueError as exc:
                quarters, quarter_capped = [], False
                _record("global-filings", str(global_forms), "failed", error=exc)
                errors.append(str(exc))
            else:
                from .. import store as _backfill_store
                from ..client import get_current_filings

                # Local-first: covered partitions run locally; missing ones
                # become bounded quarterly/form jobs, never a blocked call.
                ordered_forms = _sort_forms_by_priority(global_forms)
                missing: list[tuple] = []
                covered: list[tuple] = []
                for form in ordered_forms:
                    for year, quarter in quarters:
                        partition = _partition_for_quarter(year, quarter)
                        try:
                            is_covered = _backfill_store.is_partition_covered(
                                BACKFILL_SOURCE, form, partition,
                                root=data_root)
                        except Exception:
                            is_covered = False
                        (covered if is_covered else missing).append(
                            (form, year, quarter))
                if quarter_capped:
                    # Discarded older quarters get the same bounded backfill
                    # jobs as missing partitions; never claimed as covered.
                    full_quarters, _ = _quarters_for_range(
                        request.start_date, request.end_date, cap=10_000)
                    recent = set(quarters)
                    for form in ordered_forms:
                        for year, quarter in full_quarters:
                            if (year, quarter) not in recent:
                                missing.append((form, year, quarter))
                if missing:
                    for form, year, quarter in missing:
                        qs, qe = _quarter_dates(year, quarter)
                        partition = _partition_for_quarter(year, quarter)
                        try:
                            job_id = _backfill_store.enqueue_backfill_job(
                                BACKFILL_SOURCE, form, qs, qe,
                                PARSER_VERSION, batch_size=batch_size,
                                root=data_root)
                        except Exception as exc:
                            _record("backfill", f"{form} {partition}",
                                    "failed", error=exc,
                                    filters={"form": form, "year": year,
                                             "quarter": quarter})
                            errors.append(
                                f"backfill enqueue {partition} failed: {exc}")
                            continue
                        pending.append(job_id)
                        _record("backfill", f"{form} {partition}", "partial",
                                filters={"form": form, "year": year,
                                         "quarter": quarter,
                                         "partition": partition,
                                         "job_id": job_id,
                                         "checkpoint":
                                         f"sec-backfill/{BACKFILL_SOURCE}/"
                                         f"{form}/{partition}"})
                    try:
                        ensure_backfill_worker(data_root)
                    except Exception as exc:
                        warnings.append(
                            f"backfill worker failed to start: {exc}")
                    warnings.append(
                        f"{len(missing)} quarterly partition(s) not yet "
                        f"ingested; queued backfill jobs {pending} and "
                        "returned immediately (never waits for history)")
                for form, year, quarter in covered:
                    partition = _partition_for_quarter(year, quarter)
                    qs, qe = _quarter_dates(year, quarter)
                    try:
                        # Bounds apply before the limit: over-fetch, filter to
                        # the partition, then cap the kept rows.
                        fetch_limit = batch_size * 5
                        rows = _backfill_store.query_filings(
                            forms=[form], as_of=as_of, limit=fetch_limit,
                            root=data_root)
                    except Exception as exc:
                        _record("local-filings", f"{form} {partition}",
                                "failed", error=exc,
                                pit_basis="known_at" if as_of else None,
                                filters={"form": form, "partition": partition})
                        errors.append(
                            f"local-filings {partition} failed: {exc}")
                        continue
                    kept_all = []
                    for row in rows:
                        try:
                            filing = _filing_from_local_row(row)
                        except Exception:
                            continue
                        day = (filing.filed_at or filing.known_at or "")[:10]
                        if day and not qs <= day <= qe:
                            continue
                        if _keep(filing):
                            kept_all.append(filing)
                    kept = kept_all[:batch_size]
                    fully_evaluated = (len(rows) < fetch_limit
                                       and len(kept_all) <= batch_size)
                    _record("local-filings", f"{form} {partition}",
                            "complete" if fully_evaluated else "partial",
                            reported=len(rows), retrieved=len(kept), pages=1,
                            pit_basis="known_at" if as_of else None,
                            filters={"form": form, "partition": partition})
                    for filing in kept:
                        filings.setdefault(filing.accession_no, filing)
                # Current quarter always comes from the current feed; skip
                # the live call entirely when the range excludes it.
                _now = datetime.now(timezone.utc)
                _cur = (_now.year, (_now.month - 1) // 3 + 1)
                _cur_qs, _ = _quarter_dates(*_cur)
                if not request.start_date and not request.end_date:
                    want_current = True
                elif request.end_date:
                    want_current = str(request.end_date) >= _cur_qs
                else:
                    want_current = True
                if want_current:
                    for form in ordered_forms:
                        try:
                            rows = get_current_filings(
                                form, page_size=batch_size)
                        except Exception as exc:
                            _record("current-filings", form, "failed",
                                    error=exc,
                                    pit_basis="known_at" if as_of else None,
                                    filters={"form": form})
                            errors.append(
                                f"current-filings {form!r} failed: {exc}")
                            kept = []
                            for filing in rows:
                                day = (filing.filed_at
                                       or filing.known_at or "")[:10]
                                if request.start_date and day \
                                        and day < request.start_date:
                                    continue
                                if request.end_date and day \
                                        and day > request.end_date:
                                    continue
                                if _keep(filing):
                                    kept.append(filing)
                            _record("current-filings", form, "complete",
                                    reported=len(rows), retrieved=len(kept),
                                    pages=1,
                                    pit_basis="known_at" if as_of else None,
                                    filters={"form": form})
                            for filing in kept:
                                filings.setdefault(filing.accession_no, filing)
                else:
                    _record("current-filings",
                            "range excludes current quarter; quarterly "
                            "partitions cover it", "not_applicable")
                if not quarters:
                    if request.start_date or request.end_date:
                        _record("global-filings",
                                "range served by current feed; no quarterly "
                                "partitions", "not_applicable")
                    else:
                        _record("global-filings",
                                "unbounded range uses the current feed",
                                "not_applicable")
                if quarter_capped:
                    warnings.append(
                        f"date range spans more than {_GLOBAL_QUARTER_CAP} "
                        "quarterly partitions; searched the most recent "
                        f"{_GLOBAL_QUARTER_CAP} and queued backfill jobs for "
                        "the older partitions (see backfill attempts)")
        elif not global_forms:
            _record("global-filings", "no forms", "not_applicable")
            _record("current-filings", "no forms", "not_applicable")
        else:
            _record("global-filings", "disabled by request", "not_applicable")
            _record("current-filings", "disabled by request", "not_applicable")

        # Route 6: local relationship/security indexes (Phase 6 typed rows).
        # Covered partitions run locally; missing ones become bounded
        # quarterly/form jobs, never a blocked call. (Phase 7 extends this
        # route with transaction/offering datasets.)
        _REL_FORMS = ("SC 13D", "SC 13G", "3", "4", "5")
        _SEC_FORMS = ("13F-HR",)
        if request.search_relationships:
            from .. import store as _rel_store

            def _cik_int(value):
                try:
                    return int(str(value).strip())
                except Exception:
                    return None

            def _add_party(accession, cik_value, name, role, source,
                           known_at) -> None:
                try:
                    if not accession or not role or not known_at:
                        return
                    cik = _cik_int(cik_value)
                    label = str(name or "").strip() or str(cik_value or "")
                    if not label:
                        return
                    entity_id = None
                    if cik is not None:
                        try:
                            entity_id = sec_entity_id(cik)
                        except Exception:
                            entity_id = None
                    # Phase 7 owns transaction/offering roles; keep Phase 6
                    # projection to ownership/insider/13F evidence only.
                    party = FilingParty(
                        accession_no=str(accession), entity_id=entity_id,
                        cik=cik, name=label, role=role, source=source,
                        known_at=str(known_at), parser_version=PARSER_VERSION)
                    relationships.setdefault(
                        (party.accession_no, party.role, party.cik,
                         party.name), party)
                except Exception:
                    return

            rel_ciks: list[str] = []
            for candidate in verified:
                try:
                    if candidate.cik is not None:
                        text = str(candidate.cik).strip()
                        if text and text not in rel_ciks:
                            rel_ciks.append(text)
                except Exception:
                    continue
            if request.cik is not None:
                try:
                    text = str(request.cik).strip()
                    if text and text not in rel_ciks:
                        rel_ciks.append(text)
                except Exception:
                    pass
            # Missing partitions become bounded backfill jobs when the
            # request carries a date range; unbounded requests query the
            # local typed indexes directly (partial/limited, never complete).
            unbounded_rel = not (request.start_date or request.end_date)
            try:
                quarters, _capped = _quarters_for_range(
                    request.start_date, request.end_date)
            except ValueError:
                quarters = []
            if quarters:
                ordered = _sort_forms_by_priority(
                    list(_REL_FORMS) + list(_SEC_FORMS))
                for form in ordered:
                    for year, quarter in quarters:
                        partition = _partition_for_quarter(year, quarter)
                        try:
                            covered = _rel_store.is_partition_covered(
                                BACKFILL_SOURCE, form, partition,
                                root=data_root)
                        except Exception:
                            covered = False
                        if covered:
                            continue
                        qs, qe = _quarter_dates(year, quarter)
                        try:
                            job_id = _rel_store.enqueue_backfill_job(
                                BACKFILL_SOURCE, form, qs, qe,
                                PARSER_VERSION, batch_size=50,
                                root=data_root)
                        except Exception as exc:
                            _record("backfill", f"{form} {partition}",
                                    "failed", error=exc,
                                    filters={"form": form, "year": year,
                                             "quarter": quarter,
                                             "route": "local-index"})
                            errors.append(
                                f"backfill enqueue {partition} failed: {exc}")
                            continue
                        pending.append(job_id)
                        _record("backfill", f"{form} {partition}", "partial",
                                filters={"form": form, "year": year,
                                         "quarter": quarter,
                                         "partition": partition,
                                         "job_id": job_id,
                                         "route": "local-index",
                                         "checkpoint":
                                         f"sec-backfill/{BACKFILL_SOURCE}/"
                                         f"{form}/{partition}"})
                if pending:
                    try:
                        ensure_backfill_worker(data_root)
                    except Exception as exc:
                        warnings.append(
                            f"backfill worker failed to start: {exc}")
                    warnings.append(
                        "local relationship/security partitions not yet "
                        f"ingested; queued backfill jobs {pending} and "
                        "returned immediately (never waits for history)")
            if rel_ciks:
                rel_found = 0
                try:
                    for cik in rel_ciks:
                        for row in _rel_store.query_beneficial_ownership(
                                subject_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("subject_cik"),
                                row.get("subject_name"), "ownership-subject",
                                "sec-beneficial-ownership",
                                row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("reporter_name") or row.get("filer_name"),
                                "beneficial-owner",
                                "sec-beneficial-ownership",
                                row.get("known_at"))
                            rel_found += 1
                        for row in _rel_store.query_beneficial_ownership(
                                owner_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("subject_cik"),
                                row.get("subject_name"), "ownership-subject",
                                "sec-beneficial-ownership",
                                row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("reporter_name") or row.get("filer_name"),
                                "beneficial-owner",
                                "sec-beneficial-ownership",
                                row.get("known_at"))
                            rel_found += 1
                        for row in _rel_store.query_insider_transactions(
                                issuer_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("issuer_cik"),
                                row.get("issuer_name"), "insider-issuer",
                                "sec-insider", row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("owner_cik"),
                                row.get("owner_name"), "insider-owner",
                                "sec-insider", row.get("known_at"))
                            rel_found += 1
                        for row in _rel_store.query_insider_transactions(
                                owner_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("issuer_cik"),
                                row.get("issuer_name"), "insider-issuer",
                                "sec-insider", row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("owner_cik"),
                                row.get("owner_name"), "insider-owner",
                                "sec-insider", row.get("known_at"))
                            rel_found += 1
                        for row in _rel_store.query_13f_holdings(
                                manager_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("manager_cik"),
                                row.get("manager_name"), "13f-manager",
                                "sec-13f", row.get("known_at"))
                            rel_found += 1
                    if unbounded_rel:
                        warnings.append(
                            "unbounded relationship search covers only "
                            "locally stored rows (partial, limited)")
                    _record("local-relationships",
                            f"{len(rel_ciks)} cik(s)",
                            "partial" if unbounded_rel else "complete",
                            reported=rel_found, retrieved=len(relationships),
                            pages=1, pit_basis="known_at" if as_of else None,
                            filters={"ciks": rel_ciks})
                except Exception as exc:
                    _record("local-relationships", f"{rel_ciks}", "failed",
                            error=exc,
                            pit_basis="known_at" if as_of else None)
                    errors.append(f"local-relationships failed: {exc}")
            else:
                _record("local-relationships", "no entity/cik context",
                        "not_applicable")
            if request.security_identifier is not None:
                try:
                    rows = _rel_store.query_13f_holdings(
                        security=request.security_identifier, as_of=as_of,
                        limit=50, root=data_root)
                except Exception as exc:
                    _record("local-securities",
                            str(request.security_identifier), "failed",
                            error=exc,
                            pit_basis="known_at" if as_of else None)
                    errors.append(f"local-securities failed: {exc}")
                else:
                    for row in rows:
                        _add_party(
                            row.get("accession"), row.get("manager_cik"),
                            row.get("manager_name"), "13f-manager",
                            "sec-13f", row.get("known_at"))
                    _record("local-securities",
                            str(request.security_identifier), "complete",
                            reported=len(rows), retrieved=len(rows), pages=1,
                            pit_basis="known_at" if as_of else None)
            else:
                _record("local-securities", "no security_identifier",
                        "not_applicable")
            # Phase 7: local transaction/offering indexes over stored rows.
            # Missing partitions are queued by the global-filings route when
            # those forms are requested; otherwise this reports stored rows.
            # Roles stay mention-free: only evidenced
            # filer/target/acquirer/registrant links project.
            if rel_ciks:
                txn_found = 0
                try:
                    for cik in rel_ciks:
                        for row in _rel_store.query_transactions(
                                filer_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("filer_name"), "transaction-filer",
                                "sec-transactions", row.get("known_at"))
                            _add_party(
                                row.get("accession"),
                                row.get("target_cik") or row.get("subject_cik"),
                                row.get("target_name") or row.get("subject_name"),
                                "transaction-target",
                                "sec-transactions", row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("acquirer_cik"),
                                row.get("acquirer_name"), "transaction-acquirer",
                                "sec-transactions", row.get("known_at"))
                            txn_found += 1
                        for row in _rel_store.query_transactions(
                                subject_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("filer_name"), "transaction-filer",
                                "sec-transactions", row.get("known_at"))
                            _add_party(
                                row.get("accession"),
                                row.get("target_cik") or row.get("subject_cik"),
                                row.get("target_name") or row.get("subject_name"),
                                "transaction-target",
                                "sec-transactions", row.get("known_at"))
                            _add_party(
                                row.get("accession"), row.get("acquirer_cik"),
                                row.get("acquirer_name"), "transaction-acquirer",
                                "sec-transactions", row.get("known_at"))
                            txn_found += 1
                        for row in _rel_store.query_offerings(
                                filer_cik=cik, as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("filer_name"), "offering-filer",
                                "sec-offerings", row.get("known_at"))
                            _add_party(
                                row.get("accession"),
                                row.get("registrant_cik"),
                                row.get("registrant_name"),
                                "offering-registrant",
                                "sec-offerings", row.get("known_at"))
                            txn_found += 1
                        for row in _rel_store.query_offerings(
                                registrant=str(cik), as_of=as_of, limit=50,
                                root=data_root):
                            _add_party(
                                row.get("accession"), row.get("filer_cik"),
                                row.get("filer_name"), "offering-filer",
                                "sec-offerings", row.get("known_at"))
                            _add_party(
                                row.get("accession"),
                                row.get("registrant_cik"),
                                row.get("registrant_name"),
                                "offering-registrant",
                                "sec-offerings", row.get("known_at"))
                            txn_found += 1
                    _record("local-transactions",
                            f"{len(rel_ciks)} cik(s)", "complete",
                            reported=txn_found, retrieved=len(relationships),
                            pages=1, pit_basis="known_at" if as_of else None,
                            filters={"ciks": rel_ciks})
                except Exception as exc:
                    _record("local-transactions", f"{rel_ciks}", "failed",
                            error=exc,
                            pit_basis="known_at" if as_of else None)
                    errors.append(f"local-transactions failed: {exc}")
            else:
                _record("local-transactions", "no entity/cik context",
                        "not_applicable")
        else:
            _record("local-relationships", "disabled by request",
                    "not_applicable")
            _record("local-securities", "disabled by request",
                    "not_applicable")
            _record("local-transactions", "disabled by request",
                    "not_applicable")

        if pit_gaps:
            warnings.append(
                f"{pit_gaps} global filing(s) excluded by as_of {as_of}")
        ranked = rank_hits(
            tuple(hits.values()),
            verified_ciks=[e.cik for e in verified],
            verified_names=[e.name for e in verified],
            relevant_forms=global_forms)
        packet = build_evidence_packet(
            search_id, entities=tuple(entities.values()),
            filings=tuple(filings.values()), text_hits=ranked,
            max_items=evidence_max_items, max_chars=evidence_max_chars)
        active = [a for a in attempts if a.status != "not_applicable"]
        completed = tuple(dict.fromkeys(
            a.backend for a in attempts if a.status == "complete"))
        failed = tuple(dict.fromkeys(
            a.backend for a in attempts if a.status == "failed"))
        limits: tuple = ()
        if quarter_capped or any(a.status == "source_limited" for a in active):
            limits = ("global-filings:quarter-cap",)
        # Adopted sub-coverages (e.g. caller-limited EFTS partials whose page
        # attempts stay complete) fold in: union source limits, never promote
        # an adopted partial to complete.
        limits = tuple(dict.fromkeys(tuple(limits) + tuple(adopted_limits)))
        if not active or all(a.status == "failed" for a in active):
            status = "failed"
        elif (pending or any(a.status in ("failed", "partial") for a in active)
                or any(s in ("partial", "failed")
                       for s in adopted_not_complete)):
            # Missing partitions queued as bounded backfill jobs: the call
            # returns partial immediately with job IDs, never waits.
            status = "partial"
        elif limits or any(s == "complete_within_source_limits"
                           for s in adopted_not_complete):
            status = "complete_within_source_limits"
        else:
            status = "complete"
        forms_seen = {str(f).strip().upper() for f in global_forms if str(f).strip()}
        forms_seen.update(
            str(f.form).strip().upper() for f in filings.values()
            if str(getattr(f, "form", "") or "").strip())
        forms_seen.update(
            str(h.form).strip().upper() for h in ranked
            if str(getattr(h, "form", "") or "").strip())
        date_coverage = None
        if request.start_date or request.end_date:
            date_coverage = f"{request.start_date or ''}:{request.end_date or ''}"
        try:
            from ..store import persist_search_ledger
            persist_search_ledger(
                search_id=search_id, request=request,
                entities=tuple(entities.values()),
                filings=tuple(filings.values()),
                documents=tuple(documents.values()),
                text_hits=ranked, attempts=tuple(attempts),
                coverage_status=status,
                sources_attempted=tuple(retrieval_order),
                sources_completed=completed, sources_failed=failed,
                source_limits=limits,
                results_reported=sum(a.results_reported for a in active),
                results_retrieved=len(filings) + len(ranked) + len(entities),
                forms_covered=tuple(sorted(forms_seen)),
                pages=sum(a.pages_retrieved for a in active),
                date_coverage=date_coverage,
                warnings=tuple(warnings), errors=tuple(errors),
            )
        except Exception as exc:
            warnings.append(f"search ledger persistence failed: {exc}")
        return SECSearchResult(
            search_id=search_id,
            request=request,
            entities=tuple(entities.values()),
            filings=tuple(filings.values()),
            documents=tuple(documents.values()),
            relationships=tuple(relationships.values()),
            text_hits=ranked,
            coverage=SearchCoverage(
                status=status,  # type: ignore[arg-type]
                sources_attempted=tuple(retrieval_order),
                sources_completed=completed,
                sources_failed=failed,
                source_limits=limits,
                results_reported=sum(a.results_reported for a in active),
                results_retrieved=len(filings) + len(ranked) + len(entities),
                pages=sum(a.pages_retrieved for a in active),
                date_coverage=date_coverage,
                forms_covered=tuple(sorted(forms_seen)),
                pending_backfill_jobs=tuple(pending),
            ),
            attempts=tuple(attempts),
            warnings=tuple(warnings),
            errors=tuple(errors),
            retrieval_order=tuple(retrieval_order),
            evidence_packet_ids=packet,
        )

# --- Phase 8: open-vocabulary relationship search over typed indexes,
# verified/candidate workflow rows, mentions, and EFTS. Results group by
# type/status; mentions never flatten into verified links.

_CIK_RE = re.compile(r"(\d{1,10})\s*$")


def _relationship_ciks(entity: object) -> list[str]:
    """Entity id / CIK / candidate -> bare CIK strings (identity, not text)."""
    ciks: list[str] = []

    def _add(value: object) -> None:
        match = _CIK_RE.search(str(value or ""))
        if match:
            text = str(int(match.group(1)))
            if text not in ciks:
                ciks.append(text)

    if isinstance(entity, str):
        _add(entity)
    else:
        for attr in ("cik", "entity_id"):
            try:
                value = getattr(entity, attr, None)
            except Exception:
                value = None
            if value is not None:
                _add(value)
        if not ciks and isinstance(entity, dict):
            _add(entity.get("cik"))
            _add(entity.get("entity_id"))
    return ciks


def search_sec_relationships(entity: object, relationship_types=None,
                             as_of: str | None = None, data_root=None,
                             limit: int = 50) -> dict:
    """Fan out across typed, workflow, mention, and EFTS routes.

    Returns groups by ``relationship_type`` then status. Typed
    source-encoded roles project as ``verified``; workflow rows keep
    their stored status; text matches stay ``observed`` mentions.
    """
    from ...domain.evidence.relationships import normalize_label
    from .. import store as _store

    if as_of is not None:
        _check_as_of(as_of)
    wanted = None
    if relationship_types is not None:
        wanted = {normalize_label(t) for t in relationship_types}
    ciks = _relationship_ciks(entity)
    attempts: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    typed: list[dict] = []
    workflow: list[dict] = []
    mentions: list[dict] = []

    def _want(label: object) -> bool:
        return wanted is None or normalize_label(label) in wanted

    def _record(backend: str, status: str, **extra) -> None:
        attempts.append({"backend": backend, "status": status, **extra})

    def _emit(label: object, status: str, row: dict) -> None:
        if _want(label):
            typed.append({"relationship_type": normalize_label(label),
                          "status": status, **row})

    # Route 1: typed ownership / holdings / insider indexes, both directions.
    if ciks:
        try:
            for cik in ciks:
                for row in _store.query_beneficial_ownership(
                        subject_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("beneficial_owner", "verified", {
                        "from_entity_id": row.get("filer_cik"),
                        "to_entity_id": row.get("subject_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
                for row in _store.query_beneficial_ownership(
                        owner_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("beneficial_owner", "verified", {
                        "from_entity_id": row.get("filer_cik"),
                        "to_entity_id": row.get("subject_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
                for row in _store.query_insider_transactions(
                        issuer_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("insider_owner", "verified", {
                        "from_entity_id": row.get("owner_cik"),
                        "to_entity_id": row.get("issuer_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
                for row in _store.query_insider_transactions(
                        owner_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("insider_owner", "verified", {
                        "from_entity_id": row.get("owner_cik"),
                        "to_entity_id": row.get("issuer_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
                for row in _store.query_13f_holdings(
                        manager_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("holding_manager", "verified", {
                        "from_entity_id": row.get("manager_cik"),
                        "to_entity_id": row.get("issuer_cik")
                        or row.get("cusip") or row.get("security"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
                for row in _store.query_transactions(
                        filer_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("transaction_party", "verified", {
                        "from_entity_id": row.get("filer_cik"),
                        "to_entity_id": row.get("target_cik")
                        or row.get("subject_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at"),
                        "status": row.get("status") or "unknown"})
                for row in _store.query_transactions(
                        subject_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("transaction_party", "verified", {
                        "from_entity_id": row.get("filer_cik"),
                        "to_entity_id": row.get("target_cik")
                        or row.get("subject_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at"),
                        "status": row.get("status") or "unknown"})
                for row in _store.query_offerings(
                        filer_cik=cik, as_of=as_of, limit=limit,
                        root=data_root):
                    _emit("offering_party", "verified", {
                        "from_entity_id": row.get("filer_cik"),
                        "to_entity_id": row.get("registrant_cik"),
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "known_at": row.get("known_at")})
            _record("local-typed", "complete", ciks=ciks, found=len(typed))
        except Exception as exc:
            _record("local-typed", "failed", ciks=ciks, error=str(exc))
            errors.append(f"local-typed failed: {exc}")
    else:
        _record("local-typed", "not_applicable", reason="no cik context")

    # Route 2: verified/candidate workflow rows keep their stored status.
    try:
        entity_ids: list[str] = []
        for cik in ciks:
            try:
                entity_ids.append(sec_entity_id(int(cik)))
            except Exception:
                entity_ids.append(f"sec:cik:{cik}")
        ev_all: list[dict] = []
        if entity_ids:
            for eid in entity_ids:
                ev_all.extend(_store.query_relationship_evidence(
                    None, entity_id=eid, as_of=as_of, limit=limit,
                    root=data_root))
        else:
            ev_all.extend(_store.query_relationship_evidence(
                None, as_of=as_of, limit=limit, root=data_root))
        by_rel: dict = {}
        for ev in ev_all:
            if ev.get("relationship_id"):
                by_rel.setdefault(ev["relationship_id"], []).append(ev)
        kept = 0
        for rid, ev_rows in by_rel.items():
            label = next((str(e.get("relationship_type") or "").strip()
                          for e in ev_rows if e.get("relationship_type")),
                         "relationship")
            if not _want(label):
                continue
            try:
                rev_rows = _store.query_relationship_revisions(
                    rid, limit=100, root=data_root)
            except Exception:
                rev_rows = []
            # recorded_at has second precision; the revision sequence in
            # "<relationship_id>:rN" breaks same-second ties.
            def _seq(row: dict) -> int:
                try:
                    return int(str(row.get("revision_id") or "").rsplit(":r", 1)[1])
                except (ValueError, IndexError):
                    return -1
            latest_rev = max(rev_rows, key=_seq) if rev_rows else None
            status = str(latest_rev.get("new_status")
                         if latest_rev else "unknown") or "unknown"
            workflow.append({"relationship_id": rid,
                             "relationship_type": label,
                             "status": status,
                             "revision_id": latest_rev.get("revision_id")
                             if latest_rev else None,
                             "evidence": ev_rows})
            kept += 1
        _record("local-workflow", "complete", found=kept)
    except Exception as exc:
        _record("local-workflow", "failed", error=str(exc))
        errors.append(f"local-workflow failed: {exc}")

    # Route 3: local text mentions stay observed, never verified.
    if ciks:
        try:
            for cik in ciks:
                for row in _store.search_document_text(
                        cik, limit=min(limit, 20), as_of=as_of,
                        root=data_root):
                    mentions.append({
                        "relationship_type": "mention",
                        "status": "observed",
                        "accession": row.get("accession"),
                        "document_name": row.get("document_name"),
                        "source_span": (row.get("text") or "")[:280],
                        "known_at": row.get("known_at")})
            _record("local-mentions", "complete", found=len(mentions))
        except Exception as exc:
            _record("local-mentions", "failed", error=str(exc))
            warnings.append(f"local mentions unavailable: {exc}")
    else:
        _record("local-mentions", "not_applicable", reason="no cik context")

    # Route 4: bounded EFTS for uncovered partitions (evidence, not identity).
    if ciks:
        try:
            from ..client import search_sec_filings
            found = 0
            for cik in ciks:
                result = search_sec_filings(
                    cik, limit=min(limit, 20), as_of=as_of)
                for hit in result.text_hits:
                    mentions.append({
                        "relationship_type": "mention",
                        "status": "observed",
                        "accession": hit.accession_no,
                        "document_name": hit.matched_document,
                        "source_span": hit.query,
                        "known_at": None})
                    found += 1
            _record("efts-mentions", "complete", found=found)
        except Exception as exc:
            _record("efts-mentions", "failed", error=str(exc))
            warnings.append(f"efts mentions unavailable: {exc}")
    else:
        _record("efts-mentions", "not_applicable", reason="no cik context")

    # Phase 9: ontology state reorders ranking only. Active types sort first,
    # demoted types sort last; routes, forms, documents, and candidates are
    # never removed, so the ontology cannot prove itself by restricting
    # discovery. State lookup must never break retrieval.
    try:
        from ...domain.evidence.relationship_evaluation import ontology_boost
        _states = get_type_states(data_root=data_root)
        _active = {t for t, s in _states.items() if s == "active"}
        _demoted = {t for t, s in _states.items() if s == "demoted"}
        if _active or _demoted:
            typed.sort(key=lambda e: -ontology_boost(
                e.get("relationship_type"), _active, _demoted))
            workflow.sort(key=lambda e: -ontology_boost(
                e.get("relationship_type"), _active, _demoted))
    except Exception:
        pass
    groups: dict = {}
    for entry in typed + workflow + mentions:
        rtype = entry.get("relationship_type") or "unknown"
        status = entry.get("status") or "unknown"
        if not _want(rtype):
            continue
        groups.setdefault(rtype, {}).setdefault(status, []).append(entry)
    return {"entity": str(entity), "ciks": tuple(ciks),
            "relationship_types": tuple(relationship_types or ()),
            "as_of": as_of, "groups": groups, "typed": typed,
            "relationships": workflow, "mentions": mentions,
            "attempts": attempts, "warnings": warnings, "errors": errors}


# --- Phase 9: walk-forward relationship-type evaluation + ontology state ---

def get_type_states(data_root=None) -> dict:
    """Latest ontology state per normalized type: active/demoted/unevaluated."""
    from ...domain.evidence.relationships import normalize_label
    from .. import store as _store
    try:
        rows = _store.query_relationship_type_evaluations(limit=5000, root=data_root)
    except Exception:
        return {}
    latest: dict = {}
    for row in rows:
        label = normalize_label(row.get("relationship_type"))
        key = (str(row.get("retrieved_at") or ""), str(row.get("window_end") or ""),
               str(row.get("evaluation_id") or ""))
        if label not in latest or key > latest[label][0]:
            latest[label] = (key, str(row.get("new_state") or "unevaluated"))
    return {label: state for label, (_, state) in latest.items()}


def _evaluation_id(relationship_type: str, window: dict, inputs_hash: str) -> str:
    import hashlib
    digest = hashlib.sha256(
        f"{relationship_type}|{window['window_start']}|{window['window_end']}|"
        f"{inputs_hash}".encode("utf-8")).hexdigest()[:16]
    return f"te:{digest}"


def evaluate_and_persist_type(
    relationship_type: str, instances, *, observations=None, benchmark=None,
    windows, horizons=None, data_root=None, actor: str = "evaluation",
    known_at: str | None = None, reason: str | None = None,
) -> dict:
    """Run the pure walk-forward eval and persist one row per window.

    Records the evaluation, inputs hash, per-window/per-horizon metrics,
    threshold decision, and type revision; every prior state stays queryable.
    Deterministic reruns over identical inputs write nothing. A human-set
    state is superseded only by a later qualifying evaluation, which cites
    the superseded evaluation id in its reason.
    """
    from ...domain.evidence import relationship_evaluation as _eval
    from ...domain.evidence.relationships import normalize_label
    from .. import store as _store
    label = normalize_label(relationship_type)
    owned = [dict(it) for it in (instances or [])
             if "relationship_type" not in it
             or normalize_label(it.get("relationship_type")) == label]
    windows = [(str(s), str(e)) for s, e in windows]
    kwargs = {} if horizons is None else {"horizons": tuple(horizons)}
    outcome = _eval.evaluate_type(
        label, owned, observations, benchmark, windows, **kwargs)
    inputs_hash = _eval.hash_inputs({
        "relationship_type": label, "instances": owned,
        "observations": observations, "benchmark": benchmark,
        "windows": windows, "horizons": list(kwargs.get("horizons", _eval.HORIZONS)),
    })
    prev_state, prev_row = _store.latest_type_state(label, root=data_root)
    decision = outcome["decision"]
    new_state = {"activate": "active", "demote": "demoted"}.get(decision, prev_state)
    note = reason or outcome["reason"]
    if (prev_row is not None and prev_row.get("actor") == "human"
            and new_state != prev_state and decision in ("activate", "demote")):
        note = f"supersedes human {prev_row.get('evaluation_id')}: {note}"
    written = 0
    for window in outcome["windows"]:
        written += _store.store_relationship_type_evaluation({
            "evaluation_id": _evaluation_id(label, window, inputs_hash),
            "relationship_type": label,
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "metrics_json": json.dumps(window, sort_keys=True, default=str),
            "decision": decision,
            "inputs_hash": inputs_hash,
            "prev_state": prev_state,
            "new_state": new_state,
            "actor": actor,
            "reason": note,
            "known_at": known_at,
        }, root=data_root)
    outcome.update(inputs_hash=inputs_hash, prev_state=prev_state,
                   new_state=new_state, rows_written=written)
    return outcome


def record_type_decision(
    relationship_type: str, state: str, *, reason: str,
    actor: str = "human", window_start: str | None = None,
    window_end: str | None = None, inputs_hash: str = "",
    data_root=None, known_at: str | None = None,
) -> dict:
    """Persist a manual (default human) type-state decision as a revision row.

    Later qualifying walk-forward evaluations may supersede it with an
    explicit revision citing the superseded evaluation id.
    """
    from ...domain.evidence.relationships import normalize_label
    from .. import store as _store
    if state not in ("active", "demoted"):
        raise ValueError(f"state must be active|demoted, got {state!r}")
    if not str(reason or "").strip():
        raise ValueError("human type decisions require a reason")
    label = normalize_label(relationship_type)
    prev_state, _ = _store.latest_type_state(label, root=data_root)
    row = {
        "evaluation_id": f"te:manual:{uuid.uuid4().hex[:12]}",
        "relationship_type": label,
        "window_start": window_start, "window_end": window_end,
        "metrics_json": json.dumps({"manual": True}, sort_keys=True),
        "decision": "activate" if state == "active" else "demote",
        "inputs_hash": inputs_hash,
        "prev_state": prev_state, "new_state": state,
        "actor": actor, "reason": reason, "known_at": known_at,
    }
    _store.store_relationship_type_evaluation(row, root=data_root)
    return row

def get_sec_search_coverage(*, source=None, form=None, search_id=None,
                            data_root=None, limit: int = 200) -> dict:
    """Persisted coverage + backfill jobs only; never infers from rows.

    Reads ``sec_ingestion_coverage`` / ``sec_searches`` ledgers and the
    durable job queue. Absent ledgers mean unknown coverage, never complete.
    """
    from .. import store as _store
    try:
        coverage = _store.query_coverage(
            source=source, form=form, limit=limit, root=data_root)
    except Exception as exc:
        coverage, coverage_error = [], str(exc)
    else:
        coverage_error = None
    try:
        jobs = _store.list_jobs(limit=limit, root=data_root)
    except Exception as exc:
        jobs, jobs_error = [], str(exc)
    else:
        jobs_error = None
    search = None
    if search_id is not None:
        try:
            search = _store.query_search(str(search_id), root=data_root)
        except Exception:
            search = None
    errors = [e for e in
              ([f"coverage ledger unavailable: {coverage_error}"]
               if coverage_error else [])
              + ([f"job queue unavailable: {jobs_error}"] if jobs_error else [])]
    return {"source": source, "form": form, "search_id": search_id,
            "search": search, "coverage": coverage, "jobs": jobs,
            "errors": errors, "provenance": "persisted-ledgers-only"}
