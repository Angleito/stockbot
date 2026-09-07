"""Deterministic monitoring + duplicate-safe ticks (stdlib + PyYAML only).

A tick queries canonical Stockbot sources from persisted checkpoints, normalizes
hits to stable canonical events, persists one pending trigger per new meaningful
event, then runs each pending trigger oldest-first through Pi. The gateway is
only ever called via ``run_trigger``; ticks with nothing new make zero Pi calls.

No broker, price, Greeks, or options monitoring exists here on purpose: rules
without a reliable canonical backing stay ``enabled: false``/``unsupported``
and are never queried. Never fake data: production services with no resolvable
targets return no events instead of inventing any.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.thesis.models import Checkpoint, WatchRule
from app.thesis.runner import run_trigger
from app.thesis.yaml import load_raw_yaml, thesis_lock

_MAX_DUP_MARKERS = 1_000
# ponytail: recent-hash markers capped at newest 1_000; stable per-source
# cursors are the primary resume path, hashes only cover cursor-less sources.
_DEEP_REVIEW_DAYS = 30
# ponytail: FINRA "material" = metrics-payload change between settlement cycles
# (magnitude-blind); add a size bar here when Stockbot analytics grows one.


@dataclass(frozen=True)
class CanonicalEvent:
    """One normalized source hit: stable IDs, PIT timestamp, deterministic summary."""

    event_id: str
    canonical_ref: str
    source: str
    known_at: str
    entity: str = ""
    summary: str = ""
    content_hash: str = ""
    cycle: str | None = None
    cursor: str | None = None
    file_id: str | None = None
    metadata: dict = field(default_factory=dict)


class SourceService(Protocol):
    """Injectable canonical query surface (tests use fakes; CLI wires real ones)."""

    name: str

    def query_since(self, checkpoint: dict, *, known_at: str) -> list[CanonicalEvent]:
        ...


@dataclass
class TickResult:
    thesis_id: str
    triggers_created: list = field(default_factory=list)  # trigger_id strings
    runs: list = field(default_factory=list)  # RunOutcome objects
    no_op: bool = False
    no_op_reason: str = ""
    checkpoint: dict = field(default_factory=dict)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(canonical_ref: str, summary: str, known_at: str) -> str:
    return hashlib.sha256(f"{canonical_ref}\n{summary}\n{known_at}".encode()).hexdigest()


def _as_dt(value: str) -> datetime | None:
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)  # bare dates are UTC days
    return out


def _le(a: str, b: str) -> bool:
    """Point-in-time ``a <= b``; unparseable values compare as raw strings."""
    da, db = _as_dt(a), _as_dt(b)
    if da is not None and db is not None:
        return da <= db
    return str(a) <= str(b)


def _day(ts: str) -> str:
    ts = str(ts or "")
    return ts[:10] if len(ts) >= 10 else ts


_TICKER_RE = re.compile(r"^[A-Za-z]{1,5}$")


def targets_for_thesis(thesis: Any) -> tuple[str, ...]:
    """Ticker-ish tokens from the thesis scope (``unknown`` yields nothing)."""
    out: list[str] = []
    for tok in re.split(r"[^A-Za-z]+", getattr(thesis, "scope", "") or ""):
        if _TICKER_RE.match(tok or ""):
            t = tok.upper()
            if t not in out:
                out.append(t)
    return tuple(out)


def _thesis_blob(thesis: Any) -> str:
    parts = [getattr(thesis, "user_thesis", "") or "", getattr(thesis, "scope", "") or ""]
    parts.extend(getattr(thesis, "assumptions", None) or [])
    parts.extend(getattr(thesis, "invalidators", None) or [])
    parts.extend(getattr(thesis, "unknowns", None) or [])
    for c in getattr(thesis, "claims", None) or []:
        parts.append(getattr(c, "statement", "") or "")
    for e in getattr(thesis, "expressions", None) or []:
        parts.append(getattr(e, "intent", "") or "")
        parts.append(getattr(e, "instrument", "") or "")
        parts.append(getattr(e, "structure", "") or "")
    return "\n".join(str(p) for p in parts).lower()


def _relevant(event: CanonicalEvent, targets: tuple[str, ...], blob: str) -> bool:
    """Keep events that name the thesis; drop clearly irrelevant entities.

    An empty entity carries no signal either way, so it stays (Pi decides).
    """
    ent = (event.entity or "").strip()
    if not ent:
        return True
    if ent.upper() in {t.upper() for t in targets}:
        return True
    toks = [t for t in re.split(r"[^a-z0-9]+", ent.lower()) if len(t) >= 3]
    return any(t in blob for t in toks)


def _importance(rule_type: str) -> str:
    return {"explicit_thesis_invalidator": "high", "scheduled_deep_review": "low"}.get(rule_type, "medium")


# -- production source services (thin, PIT-gated, empty-safe) -----------------


class SecFilingsService:
    """New/amended filings via SEC discovery, ``as_of``-gated to ``known_at``."""

    name = "sec_filings"

    def __init__(self, targets: tuple[str, ...] = (), *, since_default: str | None = None) -> None:
        self.targets = tuple(targets)
        self.since_default = since_default

    def query_since(self, checkpoint: dict, *, known_at: str) -> list[CanonicalEvent]:
        if not self.targets:
            return []
        from app.sec.filings import list_sec_filings

        since = _day(checkpoint.get("cursor") or "") or _day(self.since_default or "") or None
        out: list[CanonicalEvent] = []
        for target in self.targets:
            for f in list_sec_filings(target, start_date=since, as_of=_day(known_at), limit=50):
                ka = f.known_at or f.filed_at or known_at
                if ka and known_at and not _le(ka, known_at):
                    continue
                ref = f"sec:{f.accession_no}"
                summary = f"{f.form} filed by {f.filer_name or target} (filed {f.filed_at or 'unknown date'})"
                amend = bool(getattr(f, "is_amendment", False) or str(f.form or "").endswith("/A"))
                out.append(CanonicalEvent(
                    event_id=ref, canonical_ref=ref, source=self.name, known_at=ka,
                    entity=target, summary=summary, content_hash=_digest(ref, summary, ka),
                    cursor=_day(ka) or None, file_id=f.accession_no,
                    metadata={"amendment": amend, "form": f.form},
                ))
        return out


class MaterialEventsService:
    """8-K material events via the SEC material-event path, PIT-gated."""

    name = "material_events"

    def __init__(self, targets: tuple[str, ...] = (), *, since_default: str | None = None) -> None:
        self.targets = tuple(targets)
        self.since_default = since_default

    def query_since(self, checkpoint: dict, *, known_at: str) -> list[CanonicalEvent]:
        if not self.targets:
            return []
        from app.sec.material import get_material_events

        since = _day(checkpoint.get("cursor") or "") or _day(self.since_default or "") or "2000-01-01"
        out: list[CanonicalEvent] = []
        for target in self.targets:
            for e in get_material_events(target, since, as_of=_day(known_at), limit=50):
                ka = e.known_at or known_at
                if ka and known_at and not _le(ka, known_at):
                    continue
                accs = list(getattr(e, "source_accessions", None) or [])
                summary = (f"{e.event_type} ({e.issuer}) effective {e.effective_date or 'unknown date'} "
                           f"severity {getattr(e, 'severity', 'routine')}")
                out.append(CanonicalEvent(
                    event_id=e.event_id, canonical_ref=e.event_id, source=self.name, known_at=ka,
                    entity=target, summary=summary, content_hash=_digest(e.event_id, summary, ka),
                    cursor=_day(ka) or None, file_id=accs[0] if accs else None,
                    metadata={"event_type": e.event_type, "issuer": e.issuer},
                ))
        return out


class FinraShortInterestService:
    """Latest FINRA short-interest settlement cycle per target (no backfill).

    Cycle identity is the briefing ``as_of_date``; trend text rides along in
    metadata so handlers can tell a new cycle from a changed one.
    """

    name = "finra_short_interest"

    def __init__(self, targets: tuple[str, ...] = ()) -> None:
        self.targets = tuple(targets)

    def query_since(self, checkpoint: dict, *, known_at: str) -> list[CanonicalEvent]:
        if not self.targets:
            return []
        from app.finra_client import get_short_interest

        out: list[CanonicalEvent] = []
        errors: list[str] = []
        for target in self.targets:
            try:
                res = get_short_interest(target)
            except Exception as exc:
                errors.append(f"{target}: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(res, dict) or res.get("error"):
                errors.append(f"{target}: {(res or {}).get('error', 'unknown FINRA error')}")
                continue
            cycle = str(res.get("as_of_date") or "")
            if not cycle:
                continue  # no orderable cycle: never invent one
            if not _le(cycle, _day(known_at)):
                continue
            trends = [str(t) for t in (res.get("trends") or [])]
            ref = f"finra-si:{target}:{cycle}"
            summary = f"short-interest settlement {cycle} for {target}" + (
                f": {'; '.join(trends)}" if trends else "")
            out.append(CanonicalEvent(
                event_id=ref, canonical_ref=ref, source=self.name, known_at=cycle,
                entity=target, summary=summary,
                content_hash=_digest(ref, summary, "|".join(trends)),
                cycle=cycle, cursor=cycle,
                metadata={"ticker": target, "cycle": cycle, "trends": trends},
            ))
        if not out and errors:
            raise RuntimeError(f"<monitor>: FINRA short-interest query failed: {errors[0]}")
        return out


# -- supported handlers: rule_type -> fn --------------------------------------
# Each handler maps already-queried source events (or local checkpoint state)
# to candidate CanonicalEvents plus its new per-source cursor/detail state.

def _cursor_state(events: list[CanonicalEvent], known_at: str) -> dict:
    days = [_day(e.cursor or e.known_at or "") for e in events]
    days = [d for d in days if d]
    return {"cursor": max(days) if days else _day(known_at)}


def _handle_new_filing(*, rule: WatchRule, thesis: Any, source_events: dict,
                       sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    evs = [e for e in source_events.get("sec_filings", [])
           if not (e.metadata or {}).get("amendment")]
    return evs, _cursor_state(source_events.get("sec_filings", []), known_at)


def _handle_filing_change(*, rule: WatchRule, thesis: Any, source_events: dict,
                          sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    evs = [e for e in source_events.get("sec_filings", [])
           if (e.metadata or {}).get("amendment")]
    return evs, _cursor_state(source_events.get("sec_filings", []), known_at)


def _handle_material_event(*, rule: WatchRule, thesis: Any, source_events: dict,
                           sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    evs = list(source_events.get("material_events", []))
    return evs, _cursor_state(evs, known_at)


def _finra_state(all_evs: list[CanonicalEvent], prev: dict, known_at: str) -> dict:
    tickers: dict[str, dict] = {}
    for e in all_evs:
        meta = e.metadata or {}
        if meta.get("cycle"):
            tickers[str(meta.get("ticker") or e.entity)] = {
                "cycle": meta["cycle"], "trends": list(meta.get("trends") or [])}
    cursor = _cursor_state(all_evs, known_at)["cursor"]
    if prev.get("cursor"):
        cursor = max(str(prev["cursor"]), cursor)
    return {"cursor": cursor, "tickers": tickers}


def _handle_si_cycle(*, rule: WatchRule, thesis: Any, source_events: dict,
                     sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    prev = dict(sources.get("finra_short_interest") or {})
    cursor = str(prev.get("cursor") or "")
    evs = [e for e in source_events.get("finra_short_interest", [])
           if (e.metadata or {}).get("cycle") and (not cursor or str((e.metadata or {})["cycle"]) > cursor)]
    return evs, _finra_state(source_events.get("finra_short_interest", []), prev, known_at)


def _handle_si_material(*, rule: WatchRule, thesis: Any, source_events: dict,
                        sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    prev = dict(sources.get("finra_short_interest") or {})
    cursor = str(prev.get("cursor") or "")
    seen = prev.get("tickers") or {}
    out = []
    for e in source_events.get("finra_short_interest", []):
        meta = e.metadata or {}
        cycle = str(meta.get("cycle") or "")
        ticker = str(meta.get("ticker") or e.entity)
        if not cycle or (cursor and cycle <= cursor):
            continue
        prior = (seen.get(ticker) or {})
        if prior.get("trends") is not None and list(prior.get("trends") or []) != list(meta.get("trends") or []):
            out.append(e)
    return out, _finra_state(source_events.get("finra_short_interest", []), prev, known_at)

def _handle_external(*, rule: WatchRule, thesis: Any, source_events: dict,
                     sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    # No deterministic production evidence path exists; an injected service may
    # still supply events (tests), otherwise this stays quiet but supported.
    evs = list(source_events.get("external_evidence", []))
    return evs, _cursor_state(evs, known_at)


def _handle_invalidator(*, rule: WatchRule, thesis: Any, source_events: dict,
                        sources: dict, known_at: str,
                        stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    # Deterministic semantic match, no source query: invalidator keywords from
    # the thesis against this tick's events plus already-stored canonical refs
    # (evidence files, earlier triggers). Refs stay stable, so a repeat tick
    # dedups via the persisted trigger instead of refiring.
    toks = {w for inv in (getattr(thesis, "invalidators", None) or [])
            for w in re.split(r"[^a-z0-9]+", str(inv).lower()) if len(w) >= 5}
    if not toks:
        return [], {}
    pool = [e for evs in source_events.values() for e in evs] + list(stored)
    out, matched = [], set()
    for e in pool:
        if e.canonical_ref in matched:
            continue
        hay = f"{e.summary} {e.canonical_ref} {e.entity}".lower()
        if any(t in hay for t in toks):
            matched.add(e.canonical_ref)
            ref = f"invalidator:{rule.rule_id}:{hashlib.sha256(e.canonical_ref.encode()).hexdigest()[:8]}"
            summary = f"possible invalidator ({rule.rule_type}): {e.summary}"
            out.append(CanonicalEvent(
                event_id=ref, canonical_ref=ref, source="watch",
                known_at=e.known_at or known_at, entity=e.entity, summary=summary,
                content_hash=_digest(ref, summary, e.known_at or known_at)))
    return out, {}


def _handle_deep_review(*, rule: WatchRule, thesis: Any, source_events: dict,
                        sources: dict, known_at: str, stored: tuple = ()) -> tuple[list[CanonicalEvent], dict]:
    last = str((sources.get("scheduled") or {}).get("cursor") or "")
    today = _day(known_at)
    if last:
        dl, dt = _as_dt(last), _as_dt(today)
        # ponytail: date subtraction covers it; unparseable cursors fall back
        # to lexicographic compare (ISO dates order lexically).
        due = ((dt - dl).days >= _DEEP_REVIEW_DAYS if dl is not None and dt is not None
               else today > last)
    else:
        due = True
    if not due:
        return [], {}
    ref = f"review:{thesis.thesis_id}:{today}"
    summary = f"scheduled deep review ({thesis.slug}); last reviewed {last or 'never'}"
    return [CanonicalEvent(event_id=ref, canonical_ref=ref, source="watch", known_at=known_at,
                           entity=thesis.slug, summary=summary,
                           content_hash=_digest(ref, summary, known_at), cursor=today)], {"cursor": today}


SUPPORTED_HANDLERS = {
    "new_filing": _handle_new_filing,
    "filing_change": _handle_filing_change,
    "new_material_event": _handle_material_event,
    "new_short_interest_cycle": _handle_si_cycle,
    "material_short_interest_change": _handle_si_material,
    "new_external_evidence": _handle_external,
    "explicit_thesis_invalidator": _handle_invalidator,
    "scheduled_deep_review": _handle_deep_review,
}

_SOURCE_FOR_RULE = {
    "new_filing": "sec_filings",
    "filing_change": "sec_filings",
    "new_material_event": "material_events",
    "new_short_interest_cycle": "finra_short_interest",
    "material_short_interest_change": "finra_short_interest",
    "new_external_evidence": "external_evidence",
    "explicit_thesis_invalidator": None,
    "scheduled_deep_review": None,
}

_STATE_KEY = {
    "new_filing": "sec_filings",
    "filing_change": "sec_filings",
    "new_material_event": "material_events",
    "new_short_interest_cycle": "finra_short_interest",
    "material_short_interest_change": "finra_short_interest",
    "new_external_evidence": "external_evidence",
    "explicit_thesis_invalidator": None,
    "scheduled_deep_review": "scheduled",
}


def tick(repository: Any, thesis_id: str, source_services: Any = None,
         gateway: Any = None, request_context: Any = None, *,
         known_at: str | None = None) -> TickResult:
    """Run one deterministic monitor tick; see module docstring for the order."""
    known_at = known_at or _utcnow()
    services = dict(source_services or {})

    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    thesis_dir = repository.root / thesis.slug
    with thesis_lock(thesis_dir):
        status = repository.load_thesis(tid).status
    if status == "closed":
        raise ValueError(f"{thesis_dir}: thesis {tid!r} is closed; cannot tick")
    if status == "paused":
        cp = repository.load_checkpoint(tid)
        return TickResult(thesis_id=tid, no_op=True, no_op_reason="paused",
                          checkpoint=cp.to_dict())
    repository.normalize_watch(tid)

    rules = repository.load_watch_rules(tid)
    targets = targets_for_thesis(thesis)
    blob = _thesis_blob(thesis)
    active_claims = {c.claim_id for c in thesis.claims if c.status != "invalidated"}
    active_exprs = {e.expression_id for e in thesis.expressions if e.status != "closed"}
    live = [r for r in sorted(rules, key=lambda r: (r.rule_type, r.rule_id))
            if r.enabled and r.support_status == "supported"
            and r.rule_type in SUPPORTED_HANDLERS
            and ((set(r.claim_ids) & active_claims) or (set(r.expression_ids) & active_exprs))]

    checkpoint = repository.load_checkpoint(tid)
    sources = dict(checkpoint.sources or {})

    # (2) query each needed source once, from its checkpoint up to known_at.
    needed = sorted({_SOURCE_FOR_RULE[r.rule_type] for r in live} - {None})
    source_events: dict[str, list[CanonicalEvent]] = {}
    queried_ok: set[str] = set()
    for key in needed:
        svc = services.get(key)
        if svc is None:
            continue  # unwired source: never queried, checkpoint untouched
        query = getattr(svc, "query_since", None) or svc
        try:
            seen = query(dict(sources.get(key) or {}), known_at=known_at)
        except Exception:
            continue  # failed source: checkpoint never advances
        source_events[key] = [e for e in (seen or []) if isinstance(e, CanonicalEvent)]
        queried_ok.add(key)

    # (3) per-rule handlers + global dedup filters.
    existing = repository.load_triggers(tid)  # created-time order already
    # ponytail: inbox scan is O(n) per tick; index by canonical ref past thousands.
    seen_refs = {ref for t in existing for ref in t.canonical_refs}
    seen_hashes = set(checkpoint.recent_hashes or {})
    for t in existing:
        if isinstance(getattr(t, "metadata", None), dict) and t.metadata.get("content_hash"):
            seen_hashes.add(t.metadata["content_hash"])
    pending_states: dict[str, dict] = {}
    grouped: dict[str, dict] = {}
    # Stored canonical refs (evidence files PIT-filtered + earlier triggers)
    # give the invalidator handler a query-free candidate pool.
    stored: list[CanonicalEvent] = []
    evdir = repository.root / thesis.slug / "evidence"
    if evdir.is_dir():
        for f in sorted(evdir.glob("*.yaml")):
            try:
                raw = load_raw_yaml(f)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("thesis_id") != tid:
                continue
            ref = str(raw.get("canonical_ref") or "")
            ka = str(raw.get("known_at") or "")
            summary = str(raw.get("summary") or "")
            if not ref or not ka or not _le(ka, known_at):
                continue  # missing or future-known evidence is never visible
            stored.append(CanonicalEvent(event_id=ref, canonical_ref=ref, source="stored",
                                         known_at=ka, summary=summary,
                                         content_hash=_digest(ref, summary, ka)))
    for t in existing:
        if t.created_at and not _le(t.created_at, known_at):
            continue  # future-known trigger: never visible at this tick
        for ref in t.canonical_refs:
            stored.append(CanonicalEvent(
                event_id=ref, canonical_ref=ref, source="stored", known_at=t.created_at,
                summary=t.summary or "", content_hash=_digest(ref, t.summary or "", t.created_at)))
    stored_t = tuple(stored)
    for rule in live:
        evs, state = SUPPORTED_HANDLERS[rule.rule_type](
            rule=rule, thesis=thesis, source_events=source_events,
            sources=sources, known_at=known_at, stored=stored_t)
        skey = _STATE_KEY[rule.rule_type]
        if skey is not None and (skey == "scheduled" or skey in queried_ok):
            pending_states[skey] = state
        for e in evs:
            if not e.canonical_ref:
                continue
            if e.known_at and known_at and not _le(e.known_at, known_at):
                continue  # future-known evidence is never visible
            digest = e.content_hash or _digest(e.canonical_ref, e.summary, e.known_at)
            if digest in seen_hashes or e.canonical_ref in seen_refs:
                continue  # checkpoint / processed-marker / existing-trigger dup
            if e.canonical_ref in grouped and digest in grouped[e.canonical_ref]["digests"]:
                continue  # identical content within this tick
            if not _relevant(e, targets, blob):
                continue
            g = grouped.setdefault(e.canonical_ref, {
                "rule": rule, "event": e, "digest": digest,
                "claims": set(), "exprs": set(), "digests": set()})
            g["claims"] |= set(rule.claim_ids)
            g["exprs"] |= set(rule.expression_ids)
            g["digests"].add(digest)

    # (4) one trigger per canonical event; zero Pi calls when there are none.
    created = []
    new_hashes = []
    for ref in sorted(grouped):
        g = grouped[ref]
        trig = repository.create_trigger(
            tid, trigger_type=g["rule"].rule_type, importance=_importance(g["rule"].rule_type),
            claim_ids=sorted(g["claims"]), expression_ids=sorted(g["exprs"]),
            canonical_refs=[ref], summary=g["event"].summary,
            metadata={"event_id": g["event"].event_id, "source": g["event"].source,
                      "content_hash": g["digest"], "entity": g["event"].entity,
                      "event_known_at": g["event"].known_at,
                      **{k: v for k, v in {"cycle": g["event"].cycle, "cursor": g["event"].cursor,
                                           "file_id": g["event"].file_id}.items() if v is not None},
                      **(g["event"].metadata or {})},
        )
        created.append(trig)
        seen_refs.add(ref)
        seen_hashes.add(g["digest"])
        new_hashes.append(g["digest"])

    # (5) run each pending trigger oldest-first; a failure leaves that trigger
    # pending and halts the tick so later evidence cannot overtake earlier.
    pending = [t for t in repository.load_triggers(tid) if t.status == "pending"]
    runs = []
    for t in pending:
        try:
            runs.append(run_trigger(repository, tid, t.trigger_id, gateway,
                                    request_context, known_at=known_at))
        except Exception:
            break

    # (6) advance each successfully queried source (never failed ones); dup
    # markers bounded to the newest _MAX_DUP_MARKERS.
    for key, state in pending_states.items():
        old = dict(sources.get(key) or {})
        if isinstance(state.get("tickers"), dict):
            old["tickers"] = {**(old.get("tickers") or {}), **state["tickers"]}
            state = {k: v for k, v in state.items() if k != "tickers"}
        old.update(state)
        old["updated_at"] = known_at
        sources[key] = old
    recent = (list(checkpoint.recent_hashes or []) + new_hashes)[- _MAX_DUP_MARKERS:]
    fresh = Checkpoint(thesis_id=tid, sources=sources, recent_hashes=recent)
    if fresh.to_dict() != checkpoint.to_dict():
        checkpoint = repository.save_checkpoint(tid, fresh)

    no_op = not created and not runs
    return TickResult(thesis_id=tid, triggers_created=[t.trigger_id for t in created],
                      runs=runs, no_op=no_op, no_op_reason="" if not no_op else "",
                      checkpoint=checkpoint.to_dict())
