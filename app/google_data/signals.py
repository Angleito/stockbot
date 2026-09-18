"""Normalized Google-discovery candidate signals (local evidence only).

A candidate is a discovery pointer, never an investment thesis: it carries
source identity, provenance timestamps and evidence references. Features are
list-based only (persistence/diffusion/rank moves); no search-volume velocity,
no materiality scores. Missing observations are missing, never zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path
from typing import Protocol

from ._guards import as_dict, as_int, as_list, as_str_list, json_from_text
from ._lazy_config import get_data_root_or_cwd

STORE_NAME = "signals.jsonl"
_MIGRATED_MARKER = ".signals_jsonl_migrated"


def _known_at_str(row: dict[str, object]) -> str:
    return str(row.get("known_at", ""))


def _features_json_str(row: dict[str, object]) -> str:
    return str(row.get("features_json") or "")


def _signal_sort_key(row: dict[str, object]) -> tuple[str, str]:
    return (str(row.get("known_at", "")), str(row.get("signal_id")))


_PERIOD_KEYS = ("source_period", "period", "week", "observed_at", "date")
_GEO_KEYS = ("geo", "geography")
_RANK_KEYS = ("rank", "position")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_root(data_root: Path | str | None = None) -> Path:
    if data_root:
        return Path(data_root)
    return get_data_root_or_cwd()


def _pick(item: dict[str, object], keys: Sequence[str], default: object = None) -> object:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def _is_present(item: dict[str, object]) -> bool:
    if not item.get("present", True):
        return False
    if item.get("absent", False) or item.get("missing", False):
        return False
    for key in ("term", "topic", "query", "keyword"):
        if key in item and item[key] is None:
            return False
    return True


def _rank_group_key(item: dict[str, object]) -> tuple[object, object, object]:
    return (
        item.get("table") or item.get("source_table"),
        item.get("list_kind") or item.get("list"),
        _pick(item, _GEO_KEYS),
    )


def _rank_row(item: dict[str, object]) -> tuple[str, int] | None:
    rank = _pick(item, _RANK_KEYS)
    period = _pick(item, _PERIOD_KEYS)
    if rank is None or period is None:
        return None
    try:
        rank = as_int(rank, what="rank")
    except TypeError, ValueError:
        return None
    return (str(period), rank)


def _best_rank_series(
    groups: dict[tuple[object, object, object], list[tuple[str, int]]],
) -> list[tuple[str, int]] | None:
    best = None
    for rows in groups.values():
        rows.sort(key=itemgetter(0))
        if len(rows) >= 2 and (best is None or len(rows) > len(best)):
            best = rows
    return best


def _rank_improvement(present: list[dict[str, object]]) -> int | None:
    groups: dict[tuple[object, object, object], list[tuple[str, int]]] = {}
    for item in present:
        parsed = _rank_row(item)
        if parsed is None:
            continue
        groups.setdefault(_rank_group_key(item), []).append(parsed)
    best = _best_rank_series(groups)
    if not best:
        return None
    return best[-2][1] - best[-1][1]


CALC_VERSION = "2"

# Informational thresholds only; candidates remain status="candidate" regardless.
RULES = {"velocity_min": None, "persistence_min": 0.5, "diffusion_min": 0.25}


def _score_of(item: dict[str, object]) -> float | None:
    score = item.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    metrics = item.get("metrics")
    if isinstance(metrics, dict):
        mscore = metrics.get("score")
        if isinstance(mscore, (int, float)) and not isinstance(mscore, bool):
            return float(mscore)
    return None


def _blob_coverage(blob: dict[str, object], primary: str, fallback: str, what: str) -> list[str] | None:
    raw = blob.get(primary) or blob.get(fallback)
    if raw is None:
        return None
    return as_str_list(raw, what=what)


def _blob_observations(blob: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in as_list(blob.get("observations", []), what="observations"):
        if isinstance(raw, dict):
            rows.append(as_dict(raw, what="observations"))
    return rows


def _narrow_blob_coverage(
    blob: dict[str, object], periods_covered: Sequence[str] | None, geos_covered: Sequence[str] | None
) -> tuple[Sequence[str] | None, Sequence[str] | None, list[dict[str, object]]]:
    if periods_covered is None:
        parsed = _blob_coverage(blob, "periods_covered", "covered_periods", "periods_covered")
        if parsed is not None:
            periods_covered = parsed
    if geos_covered is None:
        parsed = _blob_coverage(blob, "geos_covered", "covered_geos", "geos_covered")
        if parsed is not None:
            geos_covered = parsed
    return periods_covered, geos_covered, _blob_observations(blob)


def _row_coverage(
    rows: list[dict[str, object]], periods_covered: Sequence[str] | None, geos_covered: Sequence[str] | None
) -> tuple[Sequence[str] | None, Sequence[str] | None]:
    for row in rows:
        if periods_covered is None and isinstance(row.get("periods_covered"), list):
            periods_covered = as_str_list(row.get("periods_covered"), what="periods_covered")
        if geos_covered is None and isinstance(row.get("geos_covered"), list):
            geos_covered = as_str_list(row.get("geos_covered"), what="geos_covered")
    return periods_covered, geos_covered


def _coerce_rows(
    observations: dict[str, object] | Sequence[dict[str, object]] | None,
    periods_covered: Sequence[str] | None,
    geos_covered: Sequence[str] | None,
) -> tuple[list[dict[str, object]], Sequence[str] | None, Sequence[str] | None]:
    if isinstance(observations, dict):
        periods_covered, geos_covered, observations = _narrow_blob_coverage(observations, periods_covered, geos_covered)
    rows: list[dict[str, object]] = [o for o in (observations or []) if isinstance(o, dict)]
    return (rows, *_row_coverage(rows, periods_covered, geos_covered))


def _distinct_sorted(rows: list[dict[str, object]], keys: Sequence[str]) -> list[str]:
    return sorted({str(_pick(o, keys)) for o in rows if _pick(o, keys) is not None})


def _covered_lists(
    rows: list[dict[str, object]], periods_covered: Sequence[str] | None, geos_covered: Sequence[str] | None
) -> tuple[list[str], list[str]]:
    covered_periods = list(periods_covered) if periods_covered else _distinct_sorted(rows, _PERIOD_KEYS)
    covered_geos = list(geos_covered) if geos_covered else _distinct_sorted(rows, _GEO_KEYS)
    return covered_periods, covered_geos


def _scored_history(present: list[dict[str, object]]) -> list[tuple[str, float]]:
    pairs: set[tuple[str, float]] = set()
    for o in present:
        period = _pick(o, _PERIOD_KEYS)
        score = _score_of(o)
        if period is not None and score is not None:
            pairs.add((str(period), score))
    return sorted(pairs, key=itemgetter(0))


def _velocity_of(scored: list[tuple[str, float]], covered_periods: list[str], missing: dict[str, str]) -> float | None:
    if not covered_periods:
        missing["velocity"] = "no coverage"
        return None
    if len(scored) < 2:
        missing["velocity"] = "missing score history"
        return None
    return (scored[-1][1] - scored[0][1]) / len(covered_periods)


def _score_halves(scored: list[tuple[str, float]], covered_periods: list[str]) -> tuple[list[float], list[float]]:
    n_periods = len(covered_periods)
    edge = covered_periods[min(max(1, n_periods // 2), n_periods - 1)]
    prior = [s for p, s in scored if p < edge]
    recent = [s for p, s in scored if p >= edge]
    return prior, recent


def _acceleration_of(
    scored: list[tuple[str, float]], covered_periods: list[str], missing: dict[str, str]
) -> float | None:
    if len(scored) < 3 or len(covered_periods) < 2:
        if "velocity" not in missing:
            missing["acceleration"] = "missing score history"
        return None
    prior, recent = _score_halves(scored, covered_periods)
    if len(prior) < 2 or len(recent) < 2:
        missing["acceleration"] = "missing score history"
        return None
    return (recent[-1] - recent[0]) / len(recent) - (prior[-1] - prior[0]) / len(prior)


def _percentile_of(scored: list[tuple[str, float]], missing: dict[str, str]) -> float | None:
    if not scored:
        missing["percentile"] = "missing score history"
        return None
    last = scored[-1][1]
    return sum(1 for _, s in scored if s <= last) / len(scored)


def _assemble_values(
    covered_periods: list[str],
    covered_geos: list[str],
    hit_periods: set[str],
    hit_geos: set[str],
    present: list[dict[str, object]],
    velocity: float | None,
    acceleration: float | None,
    percentile: float | None,
    missing: dict[str, str],
) -> dict[str, object]:
    latest = max(covered_periods) if covered_periods else None
    first_seen = min(hit_periods) if hit_periods else None
    values: dict[str, object] = {
        "persistence": (len(hit_periods) / len(covered_periods)) if covered_periods else 0.0,
        "diffusion": (len(hit_geos) / len(covered_geos)) if covered_geos else 0.0,
        "rank_improvement": _rank_improvement(present),
        "velocity": velocity,
        "acceleration": acceleration,
        "percentile": percentile,
        "new_entry": bool(hit_periods) and first_seen == latest,
    }
    values["rules"] = {
        name: {"value": value, "rule": f"trend_{name}_v{CALC_VERSION}", "calc_version": CALC_VERSION}
        for name, value in values.items()
    }
    values["coverage"] = {"periods_covered": covered_periods, "geos_covered": covered_geos, "missing": missing}
    return values


def compute_candidate_features(
    observations: dict[str, object] | Sequence[dict[str, object]] | None,
    periods_covered: Sequence[str] | None = None,
    geos_covered: Sequence[str] | None = None,
) -> dict[str, object]:
    """persistence = distinct hit periods / covered periods; diffusion likewise.

    Coverage may be passed explicitly, ride inside the batch
    (``periods_covered``/``geos_covered`` keys), or fall back to the distinct
    periods/geos seen. Absent markers (present=False/absent/missing/term=None)
    count toward coverage but never toward hits. Rank improvement needs two
    ranked observations in the same table/list/geo, else None. Score-based
    metrics (velocity/acceleration/percentile) yield None plus a coverage
    reason when history is missing, never zero. No composite score.
    """
    rows, periods_covered, geos_covered = _coerce_rows(observations, periods_covered, geos_covered)
    covered_periods, covered_geos = _covered_lists(rows, periods_covered, geos_covered)
    present = [o for o in rows if _is_present(o)]
    hit_periods = {str(_pick(o, _PERIOD_KEYS)) for o in present if _pick(o, _PERIOD_KEYS) is not None}
    hit_geos = {str(_pick(o, _GEO_KEYS)) for o in present if _pick(o, _GEO_KEYS) is not None}
    missing: dict[str, str] = {}
    scored = _scored_history(present)
    velocity = _velocity_of(scored, covered_periods, missing)
    acceleration = _acceleration_of(scored, covered_periods, missing)
    percentile = _percentile_of(scored, missing)
    return _assemble_values(
        covered_periods, covered_geos, hit_periods, hit_geos, present, velocity, acceleration, percentile, missing
    )


def _apply_candidate_overrides(
    record: dict[str, object],
    *,
    table: object = None,
    period: object = None,
    geo: object = None,
    term: object = None,
    list_kind: object = None,
    rank: object = None,
) -> dict[str, object]:
    for key, value in (
        ("table", table),
        ("period", period),
        ("geo", geo),
        ("term", term),
        ("list_kind", list_kind),
        ("rank", rank),
    ):
        if value is not None:
            record[key] = value
    return record


def _candidate_identity(record: dict[str, object]) -> tuple[object, object, object, object, object, object, str]:
    missing = [k for k in ("table", "geo", "term", "list_kind") if record.get(k) is None]
    if missing:
        raise TypeError(f"normalize_candidate missing required fields: {missing}")
    table, geo, term = record["table"], record["geo"], record["term"]
    list_kind = record["list_kind"]
    period = record.get("period") or record.get("observed_at")
    rank = record.get("rank")
    signal_id = hashlib.sha256(f"{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()
    return table, geo, term, list_kind, period, rank, signal_id


def _merged_candidate_metrics(record: dict[str, object], metrics: object, rank: object) -> dict[str, object]:
    merged: dict[str, object] = dict(as_dict(record.get("metrics", None) or metrics or {}, what="metrics"))
    if rank is not None and "rank" not in merged:
        merged["rank"] = rank
    return merged


def _candidate_list_field(record: dict[str, object], name: str, fallback: object, what: str) -> list[object]:
    value = record.get(name, None)
    if value is not None:
        return as_list(value, what=what)
    return as_list(fallback or [], what=what)


def _first_truthy(*values: object) -> object:
    for value in values:
        if value:
            return value
    return values[-1] if values else None


def _candidate_features_field(record: dict[str, object], features: object) -> object:
    if record.get("features", None) is not None:
        return record.get("features", None)
    return features


def _build_candidate(
    record: dict[str, object],
    table: object,
    geo: object,
    term: object,
    list_kind: object,
    period: object,
    signal_id: str,
    signal_type: object,
    source: object,
    source_record_id: object,
    observed_at: object,
    retrieved_at: object,
    known_at: object,
    now: str,
    merged_metrics: dict[str, object],
    entities_list: list[object],
    evidence_list: list[object],
    features_value: object,
) -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "status": "candidate",
        "signal_type": record.get("signal_type", signal_type),
        "source": record.get("source", source),
        "source_record_id": _first_truthy(record.get("source_record_id"), source_record_id, signal_id),
        "observed_at": _first_truthy(record.get("observed_at"), observed_at, period),
        "known_at": _first_truthy(known_at, record.get("known_at"), now),
        "retrieved_at": _first_truthy(retrieved_at, record.get("retrieved_at"), now),
        "term": term,
        "geo": geo,
        "table": table,
        "list_kind": list_kind,
        "period": period,
        "metrics": merged_metrics,
        "entities": entities_list,
        "evidence": evidence_list,
        "features": features_value,
    }


def normalize_candidate(
    record: dict[str, object] | None = None,
    *,
    table: object = None,
    period: object = None,
    geo: object = None,
    term: object = None,
    list_kind: object = None,
    rank: object = None,
    signal_type: object = "trend",
    source: object = "trends",
    source_record_id: object = None,
    observed_at: object = None,
    retrieved_at: object = None,
    known_at: object = None,
    entities: object = None,
    metrics: object = None,
    evidence: object = None,
    features: object = None,
    data_root: Path | str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    """Build (and by default store) one candidate; identity is stable.

    Accepts either a record dict (keys table/period/geo/term/list_kind/rank)
    or the same fields as keywords; explicit keywords win.
    """
    record = _apply_candidate_overrides(
        dict(record or {}), table=table, period=period, geo=geo, term=term, list_kind=list_kind, rank=rank
    )
    table, geo, term, list_kind, period, rank, signal_id = _candidate_identity(record)
    candidate = _build_candidate(
        record,
        table,
        geo,
        term,
        list_kind,
        period,
        signal_id,
        signal_type,
        source,
        source_record_id,
        observed_at,
        retrieved_at,
        known_at,
        _now_iso(),
        _merged_candidate_metrics(record, metrics, rank),
        _candidate_list_field(record, "entities", entities, "entities"),
        _candidate_list_field(record, "evidence", evidence, "evidence"),
        _candidate_features_field(record, features),
    )
    if persist:
        _append_version(candidate, data_root)
    return candidate


def _same_content(old: dict[str, object], new: dict[str, object]) -> bool:
    skip = {"known_at", "retrieved_at"}
    keys = (set(old) | set(new)) - skip
    return all(old.get(k) == new.get(k) for k in keys)


def _read_prior_versions(path: Path) -> list[dict[str, object]]:
    prior: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                prior.append(json.loads(line))
    return prior


def _known_version(prior: list[dict[str, object]], candidate: dict[str, object]) -> dict[str, object] | None:
    same = [d for d in prior if d.get("signal_id") == candidate["signal_id"]]
    if not same:
        return None
    latest = max(same, key=_known_at_str)
    return latest if _same_content(latest, candidate) else None


def _append_version(candidate: dict[str, object], data_root: Path | str | None = None) -> dict[str, object]:
    root = _resolve_root(data_root) / "google_data"
    root.mkdir(parents=True, exist_ok=True)
    path = root / STORE_NAME
    known = _known_version(_read_prior_versions(path), candidate)
    if known is not None:
        candidate["known_at"] = known.get("known_at", candidate["known_at"])
        return candidate  # idempotent recollect: no duplicate line
    with path.open("a") as handle:
        handle.write(json.dumps(candidate) + "\n")
    return candidate


def _as_key(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _signal_id_for(table: object, period: object, geo: object, term: object, list_kind: object) -> str:
    return hashlib.sha256(f"{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()


def _warehouse_records(data_root: Path | str | None = None) -> list[dict[str, object]]:
    try:
        from ..storage import duckdb as _duckdb
    except ImportError:
        return []
    try:
        return _duckdb.query("SELECT * EXCLUDE (_dedup, _tsraw) FROM google_observations", data_root=_resolve_root(data_root))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _backfill_legacy_features(data_root: Path | str | None) -> None:
    try:
        from . import trends as _trends
    except ImportError:
        return
    try:
        _trends.backfill_legacy_feature_rows(data_root)
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass


def _migration_store_paths(data_root: Path | str | None) -> tuple[Path, Path, Path]:
    root = _resolve_root(data_root) / "google_data"
    return root, root / STORE_NAME, root / _MIGRATED_MARKER


def _parse_jsonl_record(line: str) -> dict[str, object] | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def _migration_payload(record: dict[str, object]) -> tuple[dict[str, object], list[object], dict[str, object] | None]:
    metrics = record.get("metrics")
    evidence = record.get("evidence")
    raw_features = record.get("features")
    return (
        metrics if isinstance(metrics, dict) else {},
        evidence if isinstance(evidence, list) else [],
        dict(raw_features) if isinstance(raw_features, dict) else None,
    )


def _migration_defaults(record: dict[str, object]) -> tuple[object, object, object, object, object, object]:
    return (
        record.get("table") or "trends",
        record.get("period") or record.get("observed_at") or "",
        record.get("geo") or "",
        record.get("term") or "",
        record.get("list_kind") or "top",
        record.get("source") or "trends",
    )


def _migration_content_hash(metrics: dict[str, object], evidence: list[object]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"metrics": metrics, "evidence": evidence, "collector_version": "1"},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _migration_observation_id(
    source: object, table: object, period: object, geo: object, term: object, list_kind: object
) -> str:
    return hashlib.sha256(f"{source}|{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()


def _migration_row(record: dict[str, object]) -> tuple[dict[str, object], dict[str, object] | None]:
    metrics, evidence, features = _migration_payload(record)
    table, period, geo, term, list_kind, source = _migration_defaults(record)
    observation_id = _migration_observation_id(source, table, period, geo, term, list_kind)
    known_at = record.get("known_at") or record.get("retrieved_at") or _now_iso()
    retrieved_at = record.get("retrieved_at") or record.get("known_at") or _now_iso()
    row = {
        "observation_id": observation_id,
        "source": source,
        "table": table,
        "term": term,
        "geo": geo,
        "list_kind": list_kind,
        "period": period,
        "observed_at": record.get("observed_at") or period,
        "known_at": known_at,
        "retrieved_at": retrieved_at,
        "source_record_id": record.get("source_record_id") or "",
        "content_hash": _migration_content_hash(metrics, evidence),
        "collector_version": "1",
        "calc_version": "1",
        "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
        "features_json": None,
        "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
        "source_url": f"bq://{table}",
    }
    feature_row: dict[str, object] | None = None
    if isinstance(features, dict):
        feature_row = {
            "observation_id": observation_id,
            "feature_scope_hash": "legacy-unknown",
            "feature_scope_json": json.dumps({"legacy": True, "reason": "pre-scope-backfill"}),
            "features_json": json.dumps(features, sort_keys=True, default=str),
            "calc_version": "1",
            "calculated_at": known_at,
            "inputs_hash": "",
        }
    return row, feature_row


def _collect_migration_rows(store: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    feature_rows: list[dict[str, object]] = []
    for line in store.read_text().splitlines():
        record = _parse_jsonl_record(line)
        if record is None:
            continue
        row, feature_row = _migration_row(record)
        rows.append(row)
        if feature_row is not None:
            feature_rows.append(feature_row)
    return rows, feature_rows


def _persist_migration_rows(
    rows: list[dict[str, object]],
    feature_rows: list[dict[str, object]],
    data_root: Path | str | None,
    _writer: _ParquetWriter,
) -> int | None:
    written = 0
    if rows:
        try:
            written = _writer.insert_ignore("google_observations", rows, data_root=_resolve_root(data_root))
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return None
    if feature_rows:
        try:
            _writer.insert_ignore("google_signal_features", feature_rows, data_root=_resolve_root(data_root))
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
    return written


def _write_migration_marker(root: Path, marker: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("migrated\n")
    except OSError:
        pass


def migrate_jsonl_once(data_root: Path | str | None = None) -> int:
    """Import legacy signals.jsonl rows into the warehouse, preserving known_at.

    Runs once per data root (marker file); warehouse dedup makes reruns free.
    JSONL is not extended afterwards; it stays as a read-only legacy trail.
    """
    try:
        from ..storage import duckdb as _store
    except ImportError:
        _backfill_legacy_features(data_root)
        return 0
    root, store, marker = _migration_store_paths(data_root)
    if marker.exists() or not store.exists():
        _backfill_legacy_features(data_root)
        return 0
    rows, feature_rows = _collect_migration_rows(store)
    written = _persist_migration_rows(rows, feature_rows, data_root, _store)
    if written is None:
        return 0
    _backfill_legacy_features(data_root)
    _write_migration_marker(root, marker)
    return written


def _signal_identity(row: dict[str, object]) -> tuple[object, object, object, object, object]:
    table = row.get("table") or "trends"
    period = row.get("period") or ""
    geo, term = row.get("geo") or "", row.get("term") or ""
    list_kind = row.get("list_kind") or "top"
    return table, period, geo, term, list_kind


def _signal_payload(row: dict[str, object]) -> tuple[dict[str, object], list[object], str]:
    metrics_raw, evidence_raw = row.get("metrics_json"), row.get("evidence_json")
    metrics: dict[str, object] = as_dict(
        json_from_text(metrics_raw if isinstance(metrics_raw, str) else str(metrics_raw), what="metrics_json"),
        what="metrics",
    )
    evidence: list[object] = as_list(
        json_from_text(evidence_raw if isinstance(evidence_raw, str) else str(evidence_raw), what="evidence_json"),
        what="evidence",
    )
    return metrics, evidence, str(row.get("collector_version") or "1")


def _source_hash_for(metrics: dict[str, object], evidence: list[object], collector_version: str) -> str:
    try:
        return hashlib.sha256(
            json.dumps(
                {"metrics": metrics, "evidence": evidence, "collector_version": collector_version},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
    except TypeError, ValueError:
        return ""


def _record_to_signal(row: dict[str, object]) -> dict[str, object]:
    table, period, geo, term, list_kind = _signal_identity(row)
    metrics, evidence, collector_version = _signal_payload(row)
    return {
        "signal_id": _signal_id_for(table, period, geo, term, list_kind),
        "status": "candidate",
        "signal_type": "trend",
        "source": row.get("source") or "trends",
        "source_record_id": row.get("source_record_id") or "",
        "observation_id": row.get("observation_id") or "",
        "observed_at": row.get("observed_at") or period,
        "known_at": row.get("known_at") or "",
        "retrieved_at": row.get("retrieved_at") or "",
        "term": term,
        "geo": geo,
        "table": table,
        "list_kind": list_kind,
        "period": period,
        "metrics": metrics,
        "entities": [],
        "evidence": evidence,
        "features": None,
        "collector_version": collector_version,
        "_source_hash": _source_hash_for(metrics, evidence, collector_version),
    }


def _version_key(record: dict[str, object], row: dict[str, object]) -> tuple[str, str, str, str]:
    raw: object = record.get("metrics") or {}
    metrics: dict[str, object] = raw if isinstance(raw, dict) else {}
    return (
        str(metrics.get("refresh_date") or ""),
        str(record.get("known_at", "")),
        str(record.get("_source_hash") or ""),
        str(row.get("source_record_id") or ""),
    )


def _latest_versions(data_root: Path | str | None, cutoff: str | None) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    keys: dict[str, tuple[str, str, str, str]] = {}
    for row in _warehouse_records(data_root):
        if not isinstance(row, dict):
            continue
        record = _record_to_signal(row)
        known = str(record.get("known_at", ""))
        if cutoff is not None and _parse_instant(known) > _parse_instant(str(cutoff)):
            continue
        key = _version_key(record, row)
        sid = str(record.get("signal_id") or "")
        if sid not in latest or key > keys[sid]:
            latest[sid] = record
            keys[sid] = key
    return latest


def _load_feature_rows(data_root: Path | str | None) -> list[dict[str, object]]:
    try:
        from ..storage import duckdb as _duckdb_q
    except ImportError:
        return []
    try:
        return _duckdb_q.query("SELECT * EXCLUDE (_dedup, _tsraw) FROM google_signal_features", data_root=_resolve_root(data_root))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _shape_candidate(record: dict[str, object]) -> dict[str, object]:
    metrics = record.get("metrics")
    return {
        "observation_id": str(record.get("observation_id") or ""),
        "content_hash": str(record.get("_source_hash") or ""),
        "table": str(record.get("table") or ""),
        "period": str(record.get("period") or ""),
        "geo": str(record.get("geo") or ""),
        "term": str(record.get("term") or ""),
        "list_kind": str(record.get("list_kind") or ""),
        "metrics": metrics if isinstance(metrics, dict) else {},
    }


def _shape_candidates(
    latest: dict[str, dict[str, object]],
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    shaped = {sid: _shape_candidate(record) for sid, record in latest.items()}
    return shaped, list(shaped.values())


def _pick_feature_row(rows: list[dict[str, object]]) -> dict[str, object]:
    peak = max(str(r.get("calculated_at") or "") for r in rows)
    return min((r for r in rows if str(r.get("calculated_at") or "") == peak), key=_features_json_str)


def _frow_identity_ok(frow: object, oid: str) -> bool:
    if not isinstance(frow, dict):
        return False
    return str(frow.get("observation_id") or "") == oid


def _frow_version_ok(frow: object) -> bool:
    return isinstance(frow, dict) and str(frow.get("calc_version") or "") == str(CALC_VERSION)


def _frow_scope_ok(frow: object, feature_scope_hash: str | None) -> bool:
    if feature_scope_hash is None:
        return True
    return isinstance(frow, dict) and str(frow.get("feature_scope_hash") or "") == feature_scope_hash


def _parse_instant(value: str) -> tuple[float, str]:
    """(epoch seconds, raw) for cutoff compare; unparseable sorts by raw text."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (float("inf"), value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment.timestamp(), value)


def _frow_cutoff_ok(frow: object, cutoff: str | None) -> bool:
    if cutoff is None:
        return True
    if not isinstance(frow, dict):
        return False
    stamp = str(frow.get("calculated_at") or "")
    return _parse_instant(stamp) <= _parse_instant(str(cutoff))


def _feature_row_matches(frow: object, oid: str, cutoff: str | None, feature_scope_hash: str | None) -> bool:
    if not _frow_identity_ok(frow, oid):
        return False
    if not _frow_version_ok(frow):
        return False
    if not _frow_scope_ok(frow, feature_scope_hash):
        return False
    return _frow_cutoff_ok(frow, cutoff)


def _frow_scope_text(frow: dict[str, object]) -> dict[str, object] | None:
    raw = frow.get("feature_scope_json")
    scope = json_from_text(raw if isinstance(raw, str) else str(raw), what="feature_scope_json")
    return scope if isinstance(scope, dict) else None


class _ParquetWriter(Protocol):
    """Structural seam: storage.duckdb module or test double with insert_ignore."""

    def insert_ignore(
        self, table: str, rows: list[dict[str, object]], data_root: Path | None = None
    ) -> int: ...


class _TrendsInputs(Protocol):
    """Structural seam: trends module or test double with expected_inputs_hash."""

    def expected_inputs_hash(
        self,
        scope: dict[str, object],
        term: str,
        table: str,
        list_kind: str,
        basis: str,
        geo: str,
        candidates: list[dict[str, object]],
    ) -> str: ...


def _frow_inputs_ok(
    frow: dict[str, object],
    scope: dict[str, object],
    record: dict[str, object],
    basis: object,
    candidates: list[dict[str, object]],
    _trends_q: _TrendsInputs,
) -> bool:
    basis_text = basis if isinstance(basis, str) else str(basis) if basis is not None else ""
    expected = _trends_q.expected_inputs_hash(
        scope,
        str(record.get("term") or ""),
        str(record.get("table") or ""),
        str(record.get("list_kind") or ""),
        basis_text,
        str(record.get("geo") or ""),
        candidates,
    )
    return str(frow.get("inputs_hash") or "") == expected


def _scoped_candidate(
    frow: dict[str, object], oid: str, cutoff: str | None, feature_scope_hash: str | None
) -> dict[str, object] | None:
    if not _feature_row_matches(frow, oid, cutoff, feature_scope_hash):
        return None
    return _frow_scope_text(frow)


def _scoped_feature_rows(
    feature_rows: list[dict[str, object]],
    oid: str,
    record: dict[str, object],
    basis: object,
    candidates: list[dict[str, object]],
    cutoff: str | None,
    feature_scope_hash: str | None,
    _trends_q: _TrendsInputs,
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for frow in feature_rows:
        scope = _scoped_candidate(frow, oid, cutoff, feature_scope_hash)
        if scope is None:
            continue
        if not _frow_inputs_ok(frow, scope, record, basis, candidates, _trends_q):
            continue
        grouped.setdefault(str(frow.get("feature_scope_hash") or ""), []).append(frow)
    return grouped


def _available_scopes(by_scope: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    available: list[dict[str, object]] = []
    for scope_hash in sorted(by_scope):
        rep = _pick_feature_row(by_scope[scope_hash])
        raw = rep.get("feature_scope_json")
        scope = json_from_text(raw if isinstance(raw, str) else str(raw), what="feature_scope_json")
        if not isinstance(scope, dict):
            continue
        available.append(
            {
                "feature_scope": scope,
                "feature_scope_hash": str(rep.get("feature_scope_hash") or ""),
                "feature_calculated_at": str(rep.get("calculated_at") or ""),
                "calc_version": str(rep.get("calc_version") or ""),
                "inputs_hash": str(rep.get("inputs_hash") or ""),
            }
        )
    return available


def _best_scoped_row(
    flat: list[dict[str, object]], by_scope: dict[str, list[dict[str, object]]], feature_scope_hash: str | None
) -> dict[str, object] | None:
    if flat and (feature_scope_hash is not None or len(by_scope) == 1):
        return _pick_feature_row(flat)
    return None


def _attach_features(
    record: dict[str, object], by_scope: dict[str, list[dict[str, object]]], feature_scope_hash: str | None
) -> None:
    flat = [r for rows in by_scope.values() for r in rows]
    best = _best_scoped_row(flat, by_scope, feature_scope_hash)
    if best is not None:
        raw = best.get("features_json")
        decoded = json_from_text(raw if isinstance(raw, str) else str(raw), what="features_json")
        record["features"] = decoded if isinstance(decoded, dict) else None
        scope_raw = best.get("feature_scope_json")
        scope_decoded = json_from_text(
            scope_raw if isinstance(scope_raw, str) else str(scope_raw), what="feature_scope_json"
        )
        record["feature_scope"] = scope_decoded
        record["feature_scope_hash"] = best.get("feature_scope_hash")
        record["feature_calculated_at"] = best.get("calculated_at")
    else:
        record["features"] = None
        record["feature_scope"] = None
        record["feature_scope_hash"] = None
        record["feature_calculated_at"] = None
    record["available_feature_scopes"] = _available_scopes(by_scope)


def _apply_signal_features(
    latest: dict[str, dict[str, object]],
    shaped: dict[str, dict[str, object]],
    candidates: list[dict[str, object]],
    feature_rows: list[dict[str, object]],
    cutoff: str | None,
    feature_scope_hash: str | None,
) -> None:
    try:
        from . import trends as _trends_q
    except ImportError:
        _trends_q = None
    for sid, record in latest.items():
        if _trends_q is None:
            by_scope: dict[str, list[dict[str, object]]] = {}
        else:
            basis = _trends_q._series_basis(shaped[sid])
            by_scope = _scoped_feature_rows(
                feature_rows,
                str(record.get("observation_id") or ""),
                record,
                basis,
                candidates,
                cutoff,
                feature_scope_hash,
                _trends_q,
            )
        _attach_features(record, by_scope, feature_scope_hash)


def _filter_signals(
    rows: list[dict[str, object]], query: str | None, geo: str | None, limit: int | None
) -> list[dict[str, object]]:
    if query is not None:
        rows = [r for r in rows if query.lower() in str(r.get("term", "")).lower()]
    if geo is not None:
        rows = [r for r in rows if str(r.get("geo")) == geo]
    if limit is not None:
        try:
            rows = rows[: max(0, limit)]
        except TypeError, ValueError:
            pass
    return rows


def query_signals(
    query: str | None = None,
    geo: str | None = None,
    as_of: str | None = None,
    limit: int | None = None,
    data_root: Path | str | None = None,
    feature_scope_hash: str | None = None,
) -> list[dict[str, object]]:
    """Latest known version per signal_id, excluding anything known after as_of.

    Reads the ``google_observations`` warehouse (migrating legacy JSONL once);
    optional substring match on term (``query``), exact match on ``geo``,
    and a row ``limit`` cap.
    Features are scope-transparent derived data: only PIT-valid v2 rows at
    current CALC_VERSION attach (stored inputs_hash must equal the hash
    recomputed over the selected observations); unscoped reads attach
    features only when exactly one valid scope matches, otherwise null
    with ``available_feature_scopes`` listed.
    """
    migrate_jsonl_once(data_root)
    cutoff = _as_key(as_of)
    latest = _latest_versions(data_root, cutoff)
    feature_rows = _load_feature_rows(data_root)
    shaped, candidates = _shape_candidates(latest)
    _apply_signal_features(latest, shaped, candidates, feature_rows, cutoff, feature_scope_hash)
    return _filter_signals(sorted(latest.values(), key=_signal_sort_key), query, geo, limit)
