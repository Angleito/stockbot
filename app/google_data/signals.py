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
from datetime import datetime, timezone
from operator import itemgetter
from pathlib import Path
from typing import Optional

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
    return datetime.now(timezone.utc).isoformat()


def _resolve_root(data_root: Optional[Path | str] = None) -> Path:
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


def _rank_improvement(present: list[dict[str, object]]) -> Optional[int]:
    groups: dict[tuple[object, object, object], list[tuple[str, int]]] = {}
    for item in present:
        rank = _pick(item, _RANK_KEYS)
        period = _pick(item, _PERIOD_KEYS)
        if rank is None or period is None:
            continue
        try:
            rank = as_int(rank, what="rank")
        except (TypeError, ValueError):
            continue
        key = (item.get("table") or item.get("source_table"),
               item.get("list_kind") or item.get("list"),
               _pick(item, _GEO_KEYS))
        groups.setdefault(key, []).append((str(period), rank))
    best = None
    for rows in groups.values():
        rows.sort(key=itemgetter(0))
        if len(rows) >= 2 and (best is None or len(rows) > len(best)):
            best = rows
    if not best:
        return None
    return best[-2][1] - best[-1][1]


CALC_VERSION = "2"

# Informational thresholds only; candidates remain status="candidate" regardless.
RULES = {"velocity_min": None, "persistence_min": 0.5, "diffusion_min": 0.25}


def _score_of(item: dict[str, object]) -> Optional[float]:
    score = item.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    metrics = item.get("metrics")
    if isinstance(metrics, dict):
        mscore = metrics.get("score")
        if isinstance(mscore, (int, float)) and not isinstance(mscore, bool):
            return float(mscore)
    return None


def compute_candidate_features(
    observations: Optional[dict[str, object] | Sequence[dict[str, object]]],
    periods_covered: Optional[Sequence[str]] = None,
    geos_covered: Optional[Sequence[str]] = None,
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
    if isinstance(observations, dict):
        blob = observations
        if periods_covered is None:
            _pc = blob.get("periods_covered") or blob.get("covered_periods")
            if _pc is not None:
                periods_covered = as_str_list(_pc, what="periods_covered")
        if geos_covered is None:
            _gc = blob.get("geos_covered") or blob.get("covered_geos")
            if _gc is not None:
                geos_covered = as_str_list(_gc, what="geos_covered")
        _obs_list = as_list(blob.get("observations", []), what="observations")
        _filtered: list[dict[str, object]] = []
        for _o in _obs_list:
            if isinstance(_o, dict):
                _filtered.append(as_dict(_o, what="observations"))
        observations = _filtered
    rows: list[dict[str, object]] = [o for o in (observations or []) if isinstance(o, dict)]
    for row in rows:
        if periods_covered is None and isinstance(row.get("periods_covered"), list):
            periods_covered = as_str_list(row.get("periods_covered"), what="periods_covered")
        if geos_covered is None and isinstance(row.get("geos_covered"), list):
            geos_covered = as_str_list(row.get("geos_covered"), what="geos_covered")
    covered_periods = (list(periods_covered) if periods_covered
                       else sorted({str(_pick(o, _PERIOD_KEYS)) for o in rows if _pick(o, _PERIOD_KEYS) is not None}))
    covered_geos = (list(geos_covered) if geos_covered
                    else sorted({str(_pick(o, _GEO_KEYS)) for o in rows if _pick(o, _GEO_KEYS) is not None}))
    present = [o for o in rows if _is_present(o)]
    hit_periods = {str(_pick(o, _PERIOD_KEYS)) for o in present if _pick(o, _PERIOD_KEYS) is not None}
    hit_geos = {str(_pick(o, _GEO_KEYS)) for o in present if _pick(o, _GEO_KEYS) is not None}
    missing: dict[str, str] = {}
    scored_pairs: set[tuple[str, float]] = set()
    for o in present:
        _period = _pick(o, _PERIOD_KEYS)
        _score = _score_of(o)
        if _period is not None and _score is not None:
            scored_pairs.add((str(_period), _score))
    scored = sorted(scored_pairs, key=itemgetter(0))
    n_periods = len(covered_periods)
    if not covered_periods:
        missing["velocity"] = "no coverage"
        velocity = None
    elif len(scored) < 2:
        missing["velocity"] = "missing score history"
        velocity = None
    else:
        velocity = (scored[-1][1] - scored[0][1]) / n_periods
    if len(scored) < 3 or n_periods < 2:
        if "velocity" not in missing:
            missing["acceleration"] = "missing score history"
        acceleration = None
    else:
        cut = max(1, n_periods // 2)
        prior = [s for p, s in scored if p < covered_periods[min(cut, n_periods - 1)]]
        recent = [s for p, s in scored if p >= covered_periods[min(cut, n_periods - 1)]]
        if len(prior) < 2 or len(recent) < 2:
            missing["acceleration"] = "missing score history"
            acceleration = None
        else:
            acceleration = ((recent[-1] - recent[0]) / len(recent)
                            - (prior[-1] - prior[0]) / len(prior))
    if not scored:
        missing["percentile"] = "missing score history"
        percentile = None
    else:
        last = scored[-1][1]
        percentile = sum(1 for _, s in scored if s <= last) / len(scored)
    latest = max(covered_periods) if covered_periods else None
    first_seen = min(hit_periods) if hit_periods else None
    new_entry = bool(hit_periods) and first_seen == latest
    values: dict[str, object] = {
        "persistence": (len(hit_periods) / len(covered_periods)) if covered_periods else 0.0,
        "diffusion": (len(hit_geos) / len(covered_geos)) if covered_geos else 0.0,
        "rank_improvement": _rank_improvement(present),
        "velocity": velocity,
        "acceleration": acceleration,
        "percentile": percentile,
        "new_entry": new_entry,
    }
    values["rules"] = {name: {"value": value, "rule": f"trend_{name}_v{CALC_VERSION}",
                              "calc_version": CALC_VERSION}
                       for name, value in values.items()}
    values["coverage"] = {"periods_covered": covered_periods, "geos_covered": covered_geos,
                          "missing": missing}
    return values


def normalize_candidate(record: Optional[dict[str, object]] = None, *, table: object = None,
                        period: object = None, geo: object = None,
                        term: object = None, list_kind: object = None, rank: object = None,
                        signal_type: object = "trend", source: object = "trends",
                        source_record_id: object = None, observed_at: object = None,
                        retrieved_at: object = None, known_at: object = None,
                        entities: object = None,
                        metrics: object = None, evidence: object = None,
                        features: object = None, data_root: Optional[Path | str] = None,
                        persist: bool = True) -> dict[str, object]:
    """Build (and by default store) one candidate; identity is stable.

    Accepts either a record dict (keys table/period/geo/term/list_kind/rank)
    or the same fields as keywords; explicit keywords win.
    """
    record = dict(record or {})
    for key, value in (("table", table), ("period", period), ("geo", geo),
                       ("term", term), ("list_kind", list_kind), ("rank", rank)):
        if value is not None:
            record[key] = value
    missing = [k for k in ("table", "geo", "term", "list_kind")
               if record.get(k) is None]
    if missing:
        raise TypeError(f"normalize_candidate missing required fields: {missing}")
    table, geo, term = record["table"], record["geo"], record["term"]
    list_kind = record["list_kind"]
    period = record.get("period") or record.get("observed_at")
    rank = record.get("rank")
    signal_id = hashlib.sha256(
        f"{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()
    now = _now_iso()
    merged_metrics: dict[str, object] = dict(as_dict(record.get("metrics", None) or metrics or {}, what="metrics"))
    if rank is not None and "rank" not in merged_metrics:
        merged_metrics["rank"] = rank
    candidate = {
        "signal_id": signal_id, "status": "candidate",
        "signal_type": record.get("signal_type", signal_type),
        "source": record.get("source", source),
        "source_record_id": record.get("source_record_id") or source_record_id or signal_id,
        "observed_at": record.get("observed_at") or observed_at or period,
        "known_at": known_at or record.get("known_at") or now,
        "retrieved_at": retrieved_at or record.get("retrieved_at") or now,
        "term": term, "geo": geo, "table": table,
        "list_kind": list_kind, "period": period,
        "metrics": merged_metrics,
        "entities": as_list(record.get("entities", None) if record.get("entities") is not None else entities or [], what="entities"),
        "evidence": as_list(record.get("evidence", None) if record.get("evidence") is not None else evidence or [], what="evidence"),
        "features": record.get("features", None) if record.get("features") is not None else features,
    }
    if persist:
        _append_version(candidate, data_root)
    return candidate


def _same_content(old: dict[str, object], new: dict[str, object]) -> bool:
    skip = {"known_at", "retrieved_at"}
    keys = (set(old) | set(new)) - skip
    return all(old.get(k) == new.get(k) for k in keys)


def _append_version(candidate: dict[str, object],
                    data_root: Optional[Path | str] = None) -> dict[str, object]:
    root = _resolve_root(data_root) / "google_data"
    root.mkdir(parents=True, exist_ok=True)
    path = root / STORE_NAME
    prior: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                prior.append(json.loads(line))
    same = [d for d in prior if d.get("signal_id") == candidate["signal_id"]]
    if same:
        latest = max(same, key=_known_at_str)
        if _same_content(latest, candidate):
            candidate["known_at"] = latest.get("known_at", candidate["known_at"])
            return candidate  # idempotent recollect: no duplicate line
    with path.open("a") as handle:
        handle.write(json.dumps(candidate) + "\n")
    return candidate


def _as_key(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _signal_id_for(table: object, period: object, geo: object, term: object,
                   list_kind: object) -> str:
    return hashlib.sha256(
        f"{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()


def _warehouse_records(data_root: Optional[Path | str] = None) -> list[dict[str, object]]:
    try:
        from ..storage import parquet as _parquet
    except ImportError:
        return []
    root = _resolve_root(data_root) / "parquet"
    try:
        return _parquet.read_table("google_observations", root).to_pylist()
    except Exception:
        return []


def migrate_jsonl_once(data_root: Optional[Path | str] = None) -> int:
    """Import legacy signals.jsonl rows into the warehouse, preserving known_at.

    Runs once per data root (marker file); warehouse dedup makes reruns free.
    JSONL is not extended afterwards; it stays as a read-only legacy trail.
    """
    root = _resolve_root(data_root) / "google_data"
    store = root / STORE_NAME
    marker = root / _MIGRATED_MARKER
    try:
        from ..storage import parquet as _parquet
    except ImportError:
        try:
            from . import trends as _trends
        except ImportError:
            return 0
        try:
            _trends.backfill_legacy_feature_rows(data_root)
        except Exception:
            pass
        return 0
    if marker.exists() or not store.exists():
        try:
            from . import trends as _trends2
        except ImportError:
            return 0
        try:
            _trends2.backfill_legacy_feature_rows(data_root)
        except Exception:
            pass
        return 0
    rows: list[dict[str, object]] = []
    feature_rows: list[dict[str, object]] = []
    for line in store.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        metrics: dict[str, object] = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        evidence: list[object] = record.get("evidence") if isinstance(record.get("evidence"), list) else []
        raw_features = record.get("features")
        features = dict(raw_features) if isinstance(raw_features, dict) else None
        table = record.get("table") or "trends"
        period = record.get("period") or record.get("observed_at") or ""
        geo, term = record.get("geo") or "", record.get("term") or ""
        list_kind = record.get("list_kind") or "top"
        source = record.get("source") or "trends"
        content_hash = hashlib.sha256(json.dumps(
            {"metrics": metrics, "evidence": evidence, "collector_version": "1"},
            sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        observation_id = hashlib.sha256(
            f"{source}|{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()
        known_at = record.get("known_at") or record.get("retrieved_at") or _now_iso()
        retrieved_at = record.get("retrieved_at") or record.get("known_at") or _now_iso()
        rows.append({
            "observation_id": observation_id,
            "source": source, "table": table, "term": term, "geo": geo,
            "list_kind": list_kind, "period": period,
            "observed_at": record.get("observed_at") or period,
            "known_at": known_at,
            "retrieved_at": retrieved_at,
            "source_record_id": record.get("source_record_id") or "",
            "content_hash": content_hash, "collector_version": "1",
            "calc_version": "1",
            "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
            "features_json": None,
            "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
            "source_url": f"bq://{table}",
        })
        if isinstance(features, dict):
            feature_rows.append({
                "observation_id": observation_id,
                "feature_scope_hash": "legacy-unknown",
                "feature_scope_json": json.dumps(
                    {"legacy": True, "reason": "pre-scope-backfill"}),
                "features_json": json.dumps(features, sort_keys=True, default=str),
                "calc_version": "1",
                "calculated_at": known_at,
                "inputs_hash": "",
            })
    written = 0
    if rows:
        try:
            written = _parquet.write_rows(
                "google_observations", rows, root=_resolve_root(data_root) / "parquet")
        except Exception:
            return 0
    if feature_rows:
        try:
            _parquet.write_rows(
                "google_signal_features", feature_rows,
                root=_resolve_root(data_root) / "parquet")
        except Exception:
            pass
    try:
        from . import trends as _trends3
    except ImportError:
        _trends3 = None
    if _trends3 is not None:
        try:
            _trends3.backfill_legacy_feature_rows(data_root)
        except Exception:
            pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("migrated\n")
    except OSError:
        pass
    return written


def _record_to_signal(row: dict[str, object]) -> dict[str, object]:
    table = row.get("table") or "trends"
    period = row.get("period") or ""
    geo, term = row.get("geo") or "", row.get("term") or ""
    list_kind = row.get("list_kind") or "top"
    metrics: dict[str, object] = as_dict(json_from_text(row.get("metrics_json"), what="metrics_json"), what="metrics")
    evidence: list[object] = as_list(json_from_text(row.get("evidence_json"), what="evidence_json"), what="evidence")
    collector_version = str(row.get("collector_version") or "1")
    try:
        source_hash = hashlib.sha256(json.dumps(
            {"metrics": metrics, "evidence": evidence,
             "collector_version": collector_version},
            sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    except (TypeError, ValueError):
        source_hash = ""
    return {
        "signal_id": _signal_id_for(table, period, geo, term, list_kind),
        "status": "candidate", "signal_type": "trend",
        "source": row.get("source") or "trends",
        "source_record_id": row.get("source_record_id") or "",
        "observation_id": row.get("observation_id") or "",
        "observed_at": row.get("observed_at") or period,
        "known_at": row.get("known_at") or "",
        "retrieved_at": row.get("retrieved_at") or "",
        "term": term, "geo": geo, "table": table,
        "list_kind": list_kind, "period": period,
        "metrics": metrics, "entities": [],
        "evidence": evidence, "features": None,
        "collector_version": collector_version, "_source_hash": source_hash,
    }


def query_signals(query: Optional[str] = None, geo: Optional[str] = None,
                  as_of: Optional[str] = None, limit: Optional[int] = None,
                  data_root: Optional[Path | str] = None,
                  feature_scope_hash: Optional[str] = None) -> list[dict[str, object]]:
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
    latest: dict[str, dict[str, object]] = {}
    keys: dict[str, tuple[str, str, str, str]] = {}
    for row in _warehouse_records(data_root):
        if not isinstance(row, dict):
            continue
        record = _record_to_signal(row)
        known = str(record.get("known_at", ""))
        if cutoff is not None and known > cutoff:
            continue
        _mraw: object = record.get("metrics") or {}
        _mdict: dict[str, object] = _mraw if isinstance(_mraw, dict) else {}
        key = (str(_mdict.get("refresh_date") or ""), known,
               str(record.get("_source_hash") or ""), str(row.get("source_record_id") or ""))
        sid = str(record.get("signal_id") or "")
        if sid not in latest or key > keys[sid]:
            latest[sid] = record
            keys[sid] = key
    feature_rows: list[dict[str, object]] = []
    try:
        from ..storage import parquet as _parquet_q
    except ImportError:
        _parquet_q = None
    if _parquet_q is not None:
        try:
            feature_rows = _parquet_q.read_table(
                "google_signal_features",
                _resolve_root(data_root) / "parquet").to_pylist()
        except Exception:
            feature_rows = []
    try:
        from . import trends as _trends_q
    except ImportError:
        _trends_q = None
    shaped: dict[str, dict[str, object]] = {}
    for _sid, _rec in latest.items():
        _metrics = _rec.get("metrics")
        shaped[_sid] = {
            "observation_id": str(_rec.get("observation_id") or ""),
            "content_hash": str(_rec.get("_source_hash") or ""),
            "table": str(_rec.get("table") or ""),
            "period": str(_rec.get("period") or ""),
            "geo": str(_rec.get("geo") or ""),
            "term": str(_rec.get("term") or ""),
            "list_kind": str(_rec.get("list_kind") or ""),
            "metrics": _metrics if isinstance(_metrics, dict) else {},
        }
    candidates = list(shaped.values())
    def _pick(_rows: list[dict[str, object]]) -> dict[str, object]:
        _peak = max(str(_r.get("calculated_at") or "") for _r in _rows)
        return min((_r for _r in _rows if str(_r.get("calculated_at") or "") == _peak),
                   key=_features_json_str)
    for _sid, record in latest.items():
        oid = str(record.get("observation_id") or "")
        by_scope: dict[str, list[dict[str, object]]] = {}
        if _trends_q is not None:
            basis = _trends_q._series_basis(shaped[_sid])
            for frow in feature_rows:
                if not isinstance(frow, dict):
                    continue
                if str(frow.get("observation_id") or "") != oid:
                    continue
                if str(frow.get("calc_version") or "") != str(CALC_VERSION):
                    continue
                if feature_scope_hash is not None:
                    if str(frow.get("feature_scope_hash") or "") != feature_scope_hash:
                        continue
                if cutoff is not None and str(frow.get("calculated_at") or "") > cutoff:
                    continue
                scope = json_from_text(frow.get("feature_scope_json"), what="feature_scope_json")
                if not isinstance(scope, dict):
                    continue
                expected = _trends_q.expected_inputs_hash(
                    scope, str(record.get("term") or ""),
                    str(record.get("table") or ""),
                    str(record.get("list_kind") or ""), basis, str(record.get("geo") or ""), candidates)
                if str(frow.get("inputs_hash") or "") != expected:
                    continue
                by_scope.setdefault(str(frow.get("feature_scope_hash") or ""), []).append(frow)
        flat = [r for rows in by_scope.values() for r in rows]
        best: Optional[dict[str, object]] = None
        if flat and (feature_scope_hash is not None or len(by_scope) == 1):
            best = _pick(flat)
        available: list[dict[str, object]] = []
        for _hash in sorted(by_scope):
            rep = _pick(by_scope[_hash])
            _scope = json_from_text(rep.get("feature_scope_json"), what="feature_scope_json")
            if not isinstance(_scope, dict):
                continue
            available.append({
                "feature_scope": _scope,
                "feature_scope_hash": str(rep.get("feature_scope_hash") or ""),
                "feature_calculated_at": str(rep.get("calculated_at") or ""),
                "calc_version": str(rep.get("calc_version") or ""),
                "inputs_hash": str(rep.get("inputs_hash") or ""),
            })
        if best is not None:
            decoded = json_from_text(best.get("features_json"), what="features_json")
            record["features"] = decoded if isinstance(decoded, dict) else None
            scope_decoded = json_from_text(best.get("feature_scope_json"), what="feature_scope_json")
            record["feature_scope"] = scope_decoded
            record["feature_scope_hash"] = best.get("feature_scope_hash")
            record["feature_calculated_at"] = best.get("calculated_at")
        else:
            record["features"] = None
            record["feature_scope"] = None
            record["feature_scope_hash"] = None
            record["feature_calculated_at"] = None
        record["available_feature_scopes"] = available
    rows = sorted(latest.values(), key=_signal_sort_key)
    if query is not None:
        rows = [r for r in rows if query.lower() in str(r.get("term", "")).lower()]
    if geo is not None:
        rows = [r for r in rows if str(r.get("geo")) == geo]
    if limit is not None:
        try:
            rows = rows[:max(0, limit)]
        except (TypeError, ValueError):
            pass
    return rows
