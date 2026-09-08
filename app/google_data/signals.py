"""Normalized Google-discovery candidate signals (local evidence only).

A candidate is a discovery pointer, never an investment thesis: it carries
source identity, provenance timestamps and evidence references. Features are
list-based only (persistence/diffusion/rank moves); no search-volume velocity,
no materiality scores. Missing observations are missing, never zero.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from ..storage import parquet as _parquet
except ImportError:  # pragma: no cover
    try:
        from app.storage import parquet as _parquet  # type: ignore
    except ImportError:
        _parquet = None  # type: ignore

STORE_NAME = "signals.jsonl"
_MIGRATED_MARKER = ".signals_jsonl_migrated"

_PERIOD_KEYS = ("source_period", "period", "week", "observed_at", "date")
_GEO_KEYS = ("geo", "geography")
_RANK_KEYS = ("rank", "position")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_root(data_root=None) -> Path:
    if data_root:
        return Path(data_root)
    try:
        from .. import config as _config  # type: ignore
        return Path(_config.get_data_root())
    except Exception:
        try:
            from app import config as _config2  # type: ignore
            return Path(_config2.get_data_root())
        except Exception:
            return Path(os.getenv("STOCKBOT_DATA_DIR", "data"))


def _pick(item: dict, keys, default=None):
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def _is_present(item: dict) -> bool:
    if not item.get("present", True):
        return False
    if item.get("absent", False) or item.get("missing", False):
        return False
    for key in ("term", "topic", "query", "keyword"):
        if key in item and item[key] is None:
            return False
    return True


def _rank_improvement(present: list) -> int | None:
    groups: dict = {}
    for item in present:
        rank = _pick(item, _RANK_KEYS)
        period = _pick(item, _PERIOD_KEYS)
        if rank is None or period is None:
            continue
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        key = (item.get("table") or item.get("source_table"),
               item.get("list_kind") or item.get("list"),
               _pick(item, _GEO_KEYS))
        groups.setdefault(key, []).append((str(period), rank))
    best = None
    for rows in groups.values():
        rows.sort(key=lambda pair: pair[0])
        if len(rows) >= 2 and (best is None or len(rows) > len(best)):
            best = rows
    if not best:
        return None
    return best[-2][1] - best[-1][1]


CALC_VERSION = "1"

# Informational thresholds only; candidates remain status="candidate" regardless.
RULES = {"velocity_min": None, "persistence_min": 0.5, "diffusion_min": 0.25}


def _score_of(item: dict):
    for key in ("score", "metrics"):
        if key == "metrics" and isinstance(item.get("metrics"), dict):
            value = item["metrics"].get("score")
        elif key == "score":
            value = item.get("score")
        else:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def compute_candidate_features(observations, periods_covered=None, geos_covered=None) -> dict:
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
        periods_covered = periods_covered or blob.get("periods_covered") or blob.get("covered_periods")
        geos_covered = geos_covered or blob.get("geos_covered") or blob.get("covered_geos")
        observations = blob.get("observations", [])
    rows = [o for o in (observations or []) if isinstance(o, dict)]
    for row in rows:
        if periods_covered is None and isinstance(row.get("periods_covered"), list):
            periods_covered = row["periods_covered"]
        if geos_covered is None and isinstance(row.get("geos_covered"), list):
            geos_covered = row["geos_covered"]
    covered_periods = (list(periods_covered) if periods_covered
                       else sorted({str(_pick(o, _PERIOD_KEYS)) for o in rows if _pick(o, _PERIOD_KEYS) is not None}))
    covered_geos = (list(geos_covered) if geos_covered
                    else sorted({str(_pick(o, _GEO_KEYS)) for o in rows if _pick(o, _GEO_KEYS) is not None}))
    present = [o for o in rows if _is_present(o)]
    hit_periods = {str(_pick(o, _PERIOD_KEYS)) for o in present if _pick(o, _PERIOD_KEYS) is not None}
    hit_geos = {str(_pick(o, _GEO_KEYS)) for o in present if _pick(o, _GEO_KEYS) is not None}
    missing: dict = {}
    scored = sorted(
        {(str(_pick(o, _PERIOD_KEYS)), _score_of(o)) for o in present
         if _pick(o, _PERIOD_KEYS) is not None and _score_of(o) is not None},
        key=lambda pair: pair[0])
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
    values = {
        "persistence": (len(hit_periods) / len(covered_periods)) if covered_periods else 0.0,
        "diffusion": (len(hit_geos) / len(covered_geos)) if covered_geos else 0.0,
        "rank_improvement": _rank_improvement(present),
        "velocity": velocity,
        "acceleration": acceleration,
        "percentile": percentile,
        "new_entry": new_entry,
    }
    values["rules"] = {name: {"value": value, "rule": f"trend_{name}_v1",
                              "calc_version": CALC_VERSION}
                       for name, value in values.items()}
    values["coverage"] = {"periods_covered": covered_periods, "geos_covered": covered_geos,
                          "missing": missing}
    return values


def normalize_candidate(record=None, *, table=None, period=None, geo=None,
                        term=None, list_kind=None, rank=None,
                        signal_type="trend", source="trends",
                        source_record_id=None, observed_at=None,
                        retrieved_at=None, known_at=None, entities=None,
                        metrics=None, evidence=None, features=None, data_root=None,
                        persist=True) -> dict:
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
    merged_metrics = dict(record.get("metrics", None) or metrics or {})
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
        "entities": record.get("entities", None) if record.get("entities") is not None else (entities or []),
        "evidence": record.get("evidence", None) if record.get("evidence") is not None else (evidence or []),
        "features": record.get("features", None) if record.get("features") is not None else features,
    }
    if persist:
        _append_version(candidate, data_root)
    return candidate


def _same_content(old: dict, new: dict) -> bool:
    skip = {"known_at", "retrieved_at"}
    keys = (set(old) | set(new)) - skip
    return all(old.get(k) == new.get(k) for k in keys)


def _append_version(candidate: dict, data_root=None) -> dict:
    root = _resolve_root(data_root) / "google_data"
    root.mkdir(parents=True, exist_ok=True)
    path = root / STORE_NAME
    prior = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                prior.append(json.loads(line))
    same = [d for d in prior if d.get("signal_id") == candidate["signal_id"]]
    if same:
        latest = max(same, key=lambda d: str(d.get("known_at", "")))
        if _same_content(latest, candidate):
            candidate["known_at"] = latest.get("known_at", candidate["known_at"])
            return candidate  # idempotent recollect: no duplicate line
    with path.open("a") as handle:
        handle.write(json.dumps(candidate) + "\n")
    return candidate


def _as_key(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _signal_id_for(table, period, geo, term, list_kind) -> str:
    return hashlib.sha256(
        f"{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()


def _warehouse_records(data_root=None) -> list:
    if _parquet is None:
        return []
    root = _resolve_root(data_root) / "parquet"
    try:
        return _parquet.read_table("google_observations", root).to_pylist()
    except Exception:
        return []


def migrate_jsonl_once(data_root=None) -> int:
    """Import legacy signals.jsonl rows into the warehouse, preserving known_at.

    Runs once per data root (marker file); warehouse dedup makes reruns free.
    JSONL is not extended afterwards; it stays as a read-only legacy trail.
    """
    root = _resolve_root(data_root) / "google_data"
    store = root / STORE_NAME
    marker = root / _MIGRATED_MARKER
    if marker.exists() or not store.exists() or _parquet is None:
        return 0
    rows = []
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
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        evidence = record.get("evidence") if isinstance(record.get("evidence"), list) else []
        table = record.get("table") or "trends"
        period = record.get("period") or record.get("observed_at") or ""
        geo, term = record.get("geo") or "", record.get("term") or ""
        list_kind = record.get("list_kind") or "top"
        source = record.get("source") or "trends"
        content_hash = hashlib.sha256(json.dumps(
            {"metrics": metrics, "evidence": evidence},
            sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        rows.append({
            "observation_id": hashlib.sha256(
                f"{source}|{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest(),
            "source": source, "table": table, "term": term, "geo": geo,
            "list_kind": list_kind, "period": period,
            "observed_at": record.get("observed_at") or period,
            "known_at": record.get("known_at") or record.get("retrieved_at") or _now_iso(),
            "retrieved_at": record.get("retrieved_at") or record.get("known_at") or _now_iso(),
            "source_record_id": record.get("source_record_id") or "",
            "content_hash": content_hash, "collector_version": "1",
            "calc_version": "1",
            "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
            "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
            "source_url": f"bq://{table}",
        })
    written = 0
    if rows:
        try:
            written = _parquet.write_rows(
                "google_observations", rows, root=_resolve_root(data_root) / "parquet")
        except Exception:
            return 0
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("migrated\n")
    except OSError:
        pass
    return written


def _record_to_signal(row: dict) -> dict:
    table = row.get("table") or "trends"
    period = row.get("period") or ""
    geo, term = row.get("geo") or "", row.get("term") or ""
    list_kind = row.get("list_kind") or "top"
    try:
        metrics = json.loads(row.get("metrics_json") or "{}")
    except ValueError:
        metrics = {}
    try:
        evidence = json.loads(row.get("evidence_json") or "[]")
    except ValueError:
        evidence = []
    return {
        "signal_id": _signal_id_for(table, period, geo, term, list_kind),
        "status": "candidate", "signal_type": "trend",
        "source": row.get("source") or "trends",
        "source_record_id": row.get("source_record_id") or "",
        "observed_at": row.get("observed_at") or period,
        "known_at": row.get("known_at") or "",
        "retrieved_at": row.get("retrieved_at") or "",
        "term": term, "geo": geo, "table": table,
        "list_kind": list_kind, "period": period,
        "metrics": metrics, "entities": [],
        "evidence": evidence,
    }


def query_signals(query=None, geo=None, as_of=None, limit=None, data_root=None) -> list:
    """Latest known version per signal_id, excluding anything known after as_of.

    Reads the ``google_observations`` warehouse (migrating legacy JSONL once);
    optional substring match on term (``query``), exact match on ``geo``,
    and a row ``limit`` cap.
    """
    migrate_jsonl_once(data_root)
    cutoff = _as_key(as_of)
    latest: dict = {}
    keys: dict = {}
    for row in _warehouse_records(data_root):
        if not isinstance(row, dict):
            continue
        record = _record_to_signal(row)
        known = str(record.get("known_at", ""))
        if cutoff is not None and known > cutoff:
            continue
        if query is not None and str(query).lower() not in str(record.get("term", "")).lower():
            continue
        if geo is not None and str(record.get("geo")) != str(geo):
            continue
        metrics = record.get("metrics") or {}
        key = (known, str(metrics.get("refresh_date") or ""),
               str(row.get("content_hash") or ""), str(row.get("source_record_id") or ""))
        sid = record.get("signal_id")
        if sid not in latest or key > keys[sid]:
            latest[sid] = record
            keys[sid] = key
    rows = sorted(latest.values(),
                  key=lambda d: (str(d.get("known_at", "")), str(d.get("signal_id"))))
    if limit is not None:
        try:
            rows = rows[:max(0, int(limit))]
        except (TypeError, ValueError):
            pass
    return rows
