"""Memory-only YouTube attention analytics, bound to a local thesis label.

Gated by GOOGLE_DATA_ENABLED + GOOGLE_CLOUD_API_KEY. No disk cache: titles and
counts live only in the returned dict (labelled with a 15-minute expiry) and
are never written to the data root, logs, or tool evidence. Only quota
counters (google_data/youtube_quota.json) persist locally.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

import requests

from ..config import (
    get_data_root,
    get_google_cloud_api_key,
    get_youtube_search_daily_limit,
    google_data_enabled,
)
from ..security.action_policy import private_pattern_hit
from ..thesis.models import Thesis
from ..thesis.repository import ThesisRepository

SOURCE = "youtube"
_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_TIMEOUT = (5, 15)
_BODY_CAP = 1024 * 1024
_SEARCH_BUCKET = 100  # provider search.list budget; effective ceiling is min(configured, 100)
_VIDEOS_BUCKET = 10000  # provider shared videos.list budget
_DIRNAME = "google_data"
_QUOTA_NAME = "youtube_quota.json"
_QUOTA_LOCK = "youtube_quota.lock"
_CACHE_NAME = "youtube_cache.json"
_STDIN_MAX = 8192
_STDOUT_MAX = 262144
_EXPIRY_MINUTES = 15
_COUNT_RE = re.compile(r"[0-9]{1,20}")  # ASCII digits only, never \d
_QUERY_BAD_RE = re.compile(r"[\x00-\x1f\x7f]")  # control chars + DEL
_LIVE_VALUES = ("none", "live", "upcoming")
_WARNING_ORDER = ("details_unavailable", "details_missing", "live_ordering")
_WIRE_FIELDS = frozenset({"thesis_id", "mode", "query", "region", "days", "limit", "confirmed"})


class _Malformed(Exception):
    """One response (or counter) broke the fixed-shape contract."""


class _QuotaRefused(Exception):
    """Ledger reservation refused; carries the fixed error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _disabled(reason: str) -> dict[str, object]:
    return {"status": "disabled", "source": SOURCE, "reason": reason}


def _unavailable(code: str) -> dict[str, object]:
    return {"status": "unavailable", "source": SOURCE, "error_type": code, "error": code}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_ts(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _coerce_int_count(value: int) -> str:
    if value < 0:
        raise _Malformed("count")
    return str(value)


def _coerce_str_count(value: str) -> str:
    if _COUNT_RE.fullmatch(value):
        return value
    raise _Malformed("count")


def _pacific_today() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _coerce_count(value: object) -> str | None:
    """Raw decimal-string-or-null counter; garbage raises _Malformed."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Malformed("count")
    if isinstance(value, int):
        return _coerce_int_count(value)
    if isinstance(value, str):
        return _coerce_str_count(value)
    raise _Malformed("count")


def _store_ledger(path: Path, days: dict[str, object]) -> None:
    payload = {"version": 2, "days": days}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path.parent / (path.name + ".tmp"))
        raise _QuotaRefused("quota_state_invalid")


def _validate_days(days: dict[str, object]) -> dict[str, object]:
    for day, bucket in days.items():
        if not isinstance(day, str):
            raise _QuotaRefused("quota_state_invalid")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise _QuotaRefused("quota_state_invalid") from None
        if not isinstance(bucket, dict):
            raise _QuotaRefused("quota_state_invalid")
        for key in ("search", "videos"):
            used = bucket.get(key)
            if not isinstance(used, int) or isinstance(used, bool) or used < 0:
                raise _QuotaRefused("quota_state_invalid")
    return days


def _open_lock(qdir: Path) -> BinaryIO:
    """Open (creating) the quota lock file; OSError becomes quota_state_invalid."""
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        return open(qdir / _QUOTA_LOCK, "a+b")  # noqa: PTH123, SIM115
    except OSError:
        raise _QuotaRefused("quota_state_invalid") from None


def _read_raw(qpath: Path) -> object:
    """Ledger payload or None when absent; corrupt/oversize refuses."""
    try:
        if not qpath.exists():
            return None
        raw_bytes = qpath.read_bytes()
        if len(raw_bytes) > _BODY_CAP:
            raise _QuotaRefused("quota_state_invalid")
        parsed: object = json.loads(raw_bytes.decode("utf-8"))
        return parsed
    except (ValueError, OSError):
        raise _QuotaRefused("quota_state_invalid") from None


def _legacy_total(raw: dict[str, object]) -> int:
    total = 0
    for key, value in raw.items():
        if not isinstance(key, str) or not _is_int(value):
            raise _QuotaRefused("quota_state_invalid")
        count = int(str(value))
        if count < 0:
            raise _QuotaRefused("quota_state_invalid")
        total += count
    return total


def _migrate_legacy(raw: dict[str, object], today: str, qpath: Path) -> dict[str, object]:
    """Validate a legacy unversioned ledger, seed today at both ceilings, pause."""
    total = _legacy_total(raw)
    days: dict[str, object] = {today: {"search": max(_SEARCH_BUCKET, total),
                                       "videos": max(_VIDEOS_BUCKET, total)}}
    _store_ledger(qpath, days)
    raise _QuotaRefused("quota_exhausted")


def _load_days(raw: object, today: str, qpath: Path) -> dict[str, object]:
    """Versioned day buckets for the raw ledger; legacy migrates, else refuses."""
    if raw is None:
        return {}
    if isinstance(raw, dict) and raw.get("version") == 2:
        daymap = raw.get("days")
        if not isinstance(daymap, dict):
            raise _QuotaRefused("quota_state_invalid")
        return _validate_days(daymap)
    if isinstance(raw, dict):
        return _migrate_legacy(raw, today, qpath)
    raise _QuotaRefused("quota_state_invalid")


def _today_bucket(days: dict[str, object], today: str) -> dict[str, object]:
    """Today's quota bucket, created when absent (returned live, never a copy)."""
    bucket = days.get(today)
    if not isinstance(bucket, dict):
        fresh: dict[str, object] = {"search": 0, "videos": 0}
        days[today] = fresh
        return fresh
    search = bucket.get("search")
    videos = bucket.get("videos")
    if isinstance(search, bool) or not isinstance(search, int):
        raise _QuotaRefused("quota_state_invalid")
    if isinstance(videos, bool) or not isinstance(videos, int):
        raise _QuotaRefused("quota_state_invalid")
    return bucket


def _consume(days: dict[str, object], today: str, kind: str,
             ceiling: int, qpath: Path) -> None:
    """Consume one unit for kind today; exhausted/over-ceiling refuses."""
    for old in sorted(days)[:-3]:
        del days[old]
    bucket = _today_bucket(days, today)
    count = bucket[kind]
    if isinstance(count, bool) or not isinstance(count, int):
        raise _QuotaRefused("quota_state_invalid")
    if count >= ceiling:
        _store_ledger(qpath, days)
        raise _QuotaRefused("quota_exhausted")
    bucket[kind] = count + 1
    _store_ledger(qpath, days)


def _reserve_locked(fh: BinaryIO, qpath: Path, kind: str, ceiling: int, today: str) -> None:
    """Load, consume, and persist quota under the held lock."""
    import fcntl as _fcntl

    _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
    try:
        days = _load_days(_read_raw(qpath), today, qpath)
        _consume(days, today, kind, ceiling, qpath)
    finally:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def _reserve(root: Path, kind: str, ceiling: int) -> None:
    """Consume one quota unit under an flock-held lock; raises _QuotaRefused."""
    try:
        import fcntl as _fcntl  # noqa: F401
    except ImportError:  # pragma: no cover
        raise _QuotaRefused("quota_state_invalid") from None
    try:
        today = _pacific_today()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        raise _QuotaRefused("quota_state_invalid") from None
    qdir = root / _DIRNAME
    qpath = qdir / _QUOTA_NAME
    fh = _open_lock(qdir)
    with fh:
        _reserve_locked(fh, qpath, kind, ceiling, today)


def _http_body(chunks: list[bytes]) -> tuple[dict[str, object] | None, str | None]:
    try:
        body = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "malformed_response"
    if not isinstance(body, dict):
        return None, "malformed_response"
    return body, None


def _http_chunks(resp: requests.Response) -> tuple[list[bytes] | None, str | None]:
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > _BODY_CAP:
                return None, "response_too_large"
            chunks.append(chunk)
    except requests.RequestException:
        return None, "source_unavailable"
    return chunks, None


def _http_get(url: str, params: dict[str, str | int]) -> tuple[dict[str, object] | None, str | None]:
    """Bounded GET returning (body, error_code); never raises, never logs."""
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT,
                            allow_redirects=False, stream=True)
    except requests.RequestException:
        return None, "source_unavailable"
    try:
        if resp.status_code != 200:
            return None, "source_unavailable"
        chunks, err = _http_chunks(resp)
        if err is not None or chunks is None:
            return None, err
        return _http_body(chunks)
    finally:
        with contextlib.suppress(Exception):
            resp.close()


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _narrow_str(value: object) -> str | None:
    """str value or None when absent/blank."""
    return value if isinstance(value, str) and bool(value) else None


def _snippet_text(snip: dict[str, object]) -> tuple[str, str, str, str]:
    title = _narrow_str(snip.get("title"))
    channel_id = _narrow_str(snip.get("channelId"))
    channel_title = _narrow_str(snip.get("channelTitle"))
    published = snip.get("publishedAt")
    if (title is None or channel_id is None
            or channel_title is None or not _valid_ts(published)):
        raise _Malformed("metadata")
    if not isinstance(published, str):
        raise _Malformed("metadata")
    return title, channel_id, channel_title, published


def _snippet_live(snip: dict[str, object]) -> str:
    raw_live: object = snip.get("liveBroadcastContent", "none")
    live = "none" if raw_live is None else raw_live
    if not isinstance(live, str) or live not in _LIVE_VALUES:
        raise _Malformed("live")
    return live


def _snippet_row(video_id: str, snip: dict[str, object]) -> dict[str, object]:
    title, channel_id, channel_title, published = _snippet_text(snip)
    live = _snippet_live(snip)
    return {
        "video_id": video_id, "title": title, "channel_id": channel_id,
        "channel_title": channel_title, "published_at": published,
        "live_broadcast_content": live,
        "view_count": None, "like_count": None, "comment_count": None,
    }


def _chart_ids(entry: dict[str, object]) -> tuple[str, dict[str, object]]:
    video_id = entry.get("id")
    snip = entry.get("snippet")
    if not isinstance(video_id, str) or not video_id or not isinstance(snip, dict):
        raise _Malformed("id")
    return video_id, snip


def _chart_stats(row: dict[str, object], stats: object) -> dict[str, object]:
    if stats is None:
        return row
    if not isinstance(stats, dict):
        raise _Malformed("statistics")
    row["view_count"] = _coerce_count(stats.get("viewCount"))
    row["like_count"] = _coerce_count(stats.get("likeCount"))
    row["comment_count"] = _coerce_count(stats.get("commentCount"))
    return row


def _parse_search_item(item: dict[str, object]) -> dict[str, object]:
    if not isinstance(item, dict):
        raise _Malformed("item")
    ident = item.get("id")
    video_id = ident.get("videoId") if isinstance(ident, dict) else None
    snip = item.get("snippet")
    if not isinstance(video_id, str) or not video_id or not isinstance(snip, dict):
        raise _Malformed("id")
    return _snippet_row(video_id, snip)


def _parse_chart_item(entry: dict[str, object]) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise _Malformed("item")
    video_id, snip = _chart_ids(entry)
    row = _snippet_row(video_id, snip)
    return _chart_stats(row, entry.get("statistics"))


def _details_index(body: dict[str, object]) -> dict[str, object] | None:
    """videos.list items keyed by id, or None when the shape is wrong."""
    items = body.get("items")
    if not isinstance(items, list):
        return None
    return {e["id"]: e for e in items
            if isinstance(e, dict) and isinstance(e.get("id"), str)}


def _merge_stats(row: dict[str, object], stats: object) -> bool:
    """Merge one statistics payload into a row; True means details missing."""
    if stats is None:
        return False
    if not isinstance(stats, dict):
        return True
    try:
        row["view_count"] = _coerce_count(stats.get("viewCount"))
        row["like_count"] = _coerce_count(stats.get("likeCount"))
        row["comment_count"] = _coerce_count(stats.get("commentCount"))
    except _Malformed:
        row["view_count"] = row["like_count"] = row["comment_count"] = None
        return True
    return False


def _merge_row(row: dict[str, object], by_id: dict[str, object]) -> bool:
    """Merge details for one row; True means details missing."""
    vid = row["video_id"]
    if not isinstance(vid, str):
        return True
    entry = by_id.get(vid)
    if entry is None:
        return True
    if not isinstance(entry, dict):
        return True
    return _merge_stats(row, entry.get("statistics"))


def _apply_details(rows: list[dict[str, object]], body: dict[str, object]) -> str | None:
    """Merge videos.list into search rows; returns a warning code or None."""
    by_id = _details_index(body)
    if by_id is None:
        return "details_unavailable"
    missing = any(_merge_row(row, by_id) for row in rows)
    return "details_missing" if missing else None


def _success(thesis: Thesis, mode: str, query: str | None, region: str, days: int | None,
             order: str, videos: list[dict[str, object]],
             warnings: set[str]) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "status": "partial" if warnings - {"live_ordering"} else "ok",
        "source": SOURCE,
        "thesis": {"thesis_id": thesis.thesis_id, "slug": thesis.slug},
        "mode": mode, "query": query, "region": region, "days": days,
        "order": order,
        "retrieved_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=_EXPIRY_MINUTES)).isoformat(),
        "videos": videos,
        "warnings": [w for w in _WARNING_ORDER if w in warnings],
    }


def _search_params(query: str, region: str, days: int, limit: int,
                   key: str) -> dict[str, str | int]:
    """Bounded search.list params for the trailing-days window."""
    now = datetime.now(timezone.utc)
    return {
        "part": "snippet", "type": "video", "q": query, "order": "viewCount",
        "regionCode": region,
        "publishedAfter": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "publishedBefore": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxResults": limit, "key": key,
        "fields": ("regionCode,items(id/videoId,snippet(title,channelId,"
                   "channelTitle,publishedAt,liveBroadcastContent))"),
    }


def _search_rows(body: dict[str, object],
                 region: str) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """(error, rows): validated search rows or a fixed-shape error."""
    items = body.get("items")
    if not isinstance(items, list):
        return _unavailable("malformed_response"), []
    if "regionCode" in body and body["regionCode"] != region:
        return _unavailable("source_unavailable"), []
    try:
        return None, [_parse_search_item(item) for item in items]
    except _Malformed:
        return _unavailable("malformed_response"), []


def _details_params(rows: list[dict[str, object]], key: str) -> dict[str, str | int]:
    """Bounded videos.list params for detail enrichment."""
    return {
        "part": "snippet,statistics",
        "id": ",".join(str(r["video_id"]) for r in rows),
        "key": key,
        "fields": ("items(id,snippet(title,channelId,channelTitle,"
                   "publishedAt,liveBroadcastContent),"
                   "statistics(viewCount,likeCount,commentCount))"),
    }


def _enrich(rows: list[dict[str, object]], key: str,
            root: Path, warnings: set[str]) -> None:
    """Reserve videos quota and merge details; failures become warnings."""
    _reserve(root, "videos", _VIDEOS_BUCKET)
    dbody, derr = _http_get(_VIDEOS_URL, _details_params(rows, key))
    if derr or dbody is None:
        warnings.add("details_unavailable")
        return
    warn = _apply_details(rows, dbody)
    if warn:
        warnings.add(warn)


def _with_live_warning(rows: list[dict[str, object]], warnings: set[str]) -> None:
    """Flag viewCount ordering when live/upcoming videos are present."""
    if any(r["live_broadcast_content"] in ("live", "upcoming") for r in rows):
        warnings.add("live_ordering")


def _topic(root: Path, thesis: Thesis, query: str, region: str, days: int,
           limit: int, key: str, search_ceiling: int) -> dict[str, object]:
    _reserve(root, "search", search_ceiling)
    body, err = _http_get(_SEARCH_URL, _search_params(query, region, days, limit, key))
    if err:
        return _unavailable(err)
    if body is None:
        return _unavailable("source_unavailable")
    failed, rows = _search_rows(body, region)
    if failed is not None:
        return failed
    warnings: set[str] = set()
    if rows:
        _enrich(rows, key, root, warnings)
    _with_live_warning(rows, warnings)
    return _success(thesis, "topic", query, region, days, "viewCount", rows, warnings)


def _popular_params(region: str, limit: int, key: str) -> dict[str, str | int]:
    return {
        "part": "snippet,statistics", "chart": "mostPopular",
        "regionCode": region, "maxResults": limit, "key": key,
        "fields": ("items(id,snippet(title,channelId,channelTitle,"
                   "publishedAt,liveBroadcastContent),"
                   "statistics(viewCount,likeCount,commentCount))"),
    }


def _popular_rows(body: dict[str, object]) -> list[dict[str, object]]:
    items = body.get("items")
    if not isinstance(items, list):
        raise _Malformed("items")
    return [_parse_chart_item(entry) for entry in items]


def _popular(root: Path, thesis: Thesis, region: str, limit: int, key: str) -> dict[str, object]:
    _reserve(root, "videos", _VIDEOS_BUCKET)
    body, err = _http_get(_VIDEOS_URL, _popular_params(region, limit, key))
    if err:
        return _unavailable(err)
    if body is None:
        return _unavailable("source_unavailable")
    try:
        rows = _popular_rows(body)
    except _Malformed:
        return _unavailable("malformed_response")
    warnings: set[str] = set()
    _with_live_warning(rows, warnings)
    return _success(thesis, "popular", None, region, None, "mostPopular", rows, warnings)


def _defaults(mode: str | None, region: str | None, days: int | None,
              limit: int | None) -> tuple[str, str, int | None, int | None]:
    """Caller params with documented defaults applied."""
    return mode or "topic", region or "US", 30 if days is None else days, 10 if limit is None else limit


def _limit_value(limit: object) -> int | None:
    """int limit in the 1..20 analytics window, else None."""
    if isinstance(limit, bool):
        return None
    if isinstance(limit, int) and 1 <= limit <= 20:
        return limit
    return None


def _limit_ok(limit: object) -> bool:
    """True when limit is an int in the 1..20 analytics window."""
    return _limit_value(limit) is not None


def _mode_error(mode: str) -> dict[str, object] | None:
    """Invalid-params error for unknown modes, else None."""
    if mode not in ("topic", "popular"):
        return _unavailable("invalid_params")
    return None


def _region_error(region: object) -> tuple[dict[str, object] | None, str]:
    """(error, UPPER region) for two-letter ASCII region codes."""
    if (not isinstance(region, str) or len(region) != 2
            or not region.isascii() or not region.isalpha()):
        return _unavailable("invalid_params"), ""
    return None, region.upper()


def _root_error(data_root: Path | str | None) -> dict[str, object] | None:
    """Invalid-params error for non-path data roots, else None."""
    if data_root is not None and not isinstance(data_root, (str, os.PathLike)):
        return _unavailable("invalid_params")
    return None


def _topic_params(query: object,
                  days: object) -> tuple[dict[str, object] | None, str | None]:
    """(error, clean query) for topic-mode params."""
    if (not isinstance(days, int) or isinstance(days, bool)
            or not 1 <= days <= 90):
        return _unavailable("invalid_params"), None
    clean = query.strip() if isinstance(query, str) else ""
    if not 1 <= len(clean) <= 200 or _QUERY_BAD_RE.search(clean):
        return _unavailable("invalid_params"), None
    return None, clean


def _topic_shape(region: str, query: str | None, days: int | None,
                 limit: object) -> tuple[dict[str, object] | None, str, str | None,
                                        int | None, int]:
    """Validated topic-mode shape or (error, region, query, days, 0)."""
    limit_value = _limit_value(limit)
    if limit_value is None:
        return _unavailable("invalid_params"), region, query, days, 0
    topic_err, clean_query = _topic_params(query, days)
    if topic_err is not None:
        return topic_err, region, query, days, 0
    return None, region, clean_query, days, limit_value


def _popular_shape(region: str, query: str | None,
                   limit: object) -> tuple[dict[str, object] | None, str, str | None,
                                          int | None, int]:
    """Validated popular-mode shape or (error, region, query, None, 0)."""
    limit_value = _limit_value(limit)
    if limit_value is None:
        return _unavailable("invalid_params"), region, query, None, 0
    if query is not None:
        return _unavailable("invalid_params"), region, query, None, 0
    return None, region, None, None, limit_value


def _shape_error(mode: str, query: str | None, region: object,
                 days: int | None, limit: object,
                 data_root: Path | str | None) -> tuple[dict[str, object] | None, str, str | None,
                                                       int | None, int]:
    """(error, region, query, days, limit) for fixed-shape param validation."""
    mode_err = _mode_error(mode)
    if mode_err is not None:
        return mode_err, "", query, days, 0
    region_err, clean_region = _region_error(region)
    if region_err is not None:
        return region_err, "", query, days, 0
    root_err = _root_error(data_root)
    if root_err is not None:
        return root_err, clean_region, query, days, 0
    if mode == "topic":
        return _topic_shape(clean_region, query, days, limit)
    return _popular_shape(clean_region, query, limit)


def _gate(root: Path | str | None) -> tuple[dict[str, object] | None, str, Path, int]:
    """(error, key, root, ceiling) for feature flag, API key, and quota config."""
    if not google_data_enabled():
        return _disabled("google_disabled"), "", Path("."), 0
    key = get_google_cloud_api_key()
    if not key:
        return _disabled("missing_key"), "", Path("."), 0
    try:
        configured = get_youtube_search_daily_limit()
    except ValueError:
        return _unavailable("invalid_config"), "", Path("."), 0
    if not _is_int(configured) or configured <= 0:
        return _unavailable("invalid_config"), "", Path("."), 0
    resolved = Path(root) if root else get_data_root()
    return None, key, resolved, configured


def _load_thesis(root: Path, thesis_id: str) -> tuple[dict[str, object] | None, Thesis | None]:
    """(error, thesis) for the local thesis label."""
    try:
        return None, ThesisRepository(root / "thesis").load_thesis(thesis_id)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return _unavailable("invalid_thesis"), None


def _thesis_id_error(thesis_id: str) -> dict[str, object] | None:
    """Invalid-params error for blank thesis ids, else None."""
    if not isinstance(thesis_id, str) or not thesis_id.strip():
        return _unavailable("invalid_params")
    return None


def _topic_guard(mode: str, query: str | None,
                 days: int | None) -> dict[str, object] | None:
    """Topic-mode guard for cleared params and private patterns."""
    if mode != "topic":
        return None
    if query is None or days is None:
        return _unavailable("invalid_params")
    if private_pattern_hit(query):
        return _unavailable("private_args_denied")
    return None


def _legacy_cache_hit(root: Path) -> bool:
    """True when a legacy disk cache file is still present."""
    gdir = root / _DIRNAME
    for name in (_CACHE_NAME, _CACHE_NAME + ".tmp"):
        probe = gdir / name
        if probe.is_symlink() or probe.exists():
            return True
    return False


def _resolve_thesis(root: Path, thesis_id: str) -> tuple[dict[str, object] | None, Thesis | None]:
    """Loaded thesis or (fixed-shape error, None)."""
    thesis_err, thesis = _load_thesis(root, thesis_id)
    if thesis_err is not None or thesis is None:
        return thesis_err or _unavailable("invalid_thesis"), None
    return None, thesis


def _dispatch(root: Path, thesis: Thesis, mode: str, query: str | None,
              region: str, days: int | None, limit: int, key: str,
              configured: int) -> dict[str, object]:
    """Run the selected mode; quota refusals become fixed-shape errors."""
    try:
        if mode == "topic":
            if query is None or days is None:
                return _unavailable("invalid_params")
            if private_pattern_hit(query):
                return _unavailable("private_args_denied")
            return _topic(root, thesis, query, region, days, limit,
                          key, min(configured, _SEARCH_BUCKET))
        return _popular(root, thesis, region, limit, key)
    except _QuotaRefused as refused:
        return _unavailable(refused.code)


def get_youtube_analytics(*, thesis_id: str, mode: str | None = "topic",
                          query: str | None = None, region: str | None = "US",
                          days: int | None = 30, limit: int | None = 10,
                          data_root: Path | str | None = None) -> dict[str, object]:
    """Thesis-labelled attention metrics; memory-only, fixed-shape errors."""
    default_mode, default_region, default_days, default_limit = _defaults(mode, region, days, limit)
    thesis_err = _thesis_id_error(thesis_id)
    if thesis_err is not None:
        return thesis_err
    shape_err, clean_region, clean_query, clean_days, clean_limit = _shape_error(
        default_mode, query, region, default_days, default_limit, data_root)
    if shape_err is not None:
        return shape_err
    gate_err, key, root, configured = _gate(data_root)
    if gate_err is not None:
        return gate_err
    resolve_err, thesis = _resolve_thesis(root, thesis_id)
    if resolve_err is not None or thesis is None:
        return resolve_err or _unavailable("invalid_thesis")
    guard = _topic_guard(default_mode, clean_query, clean_days)
    if guard is not None:
        return guard
    if _legacy_cache_hit(root):
        return _unavailable("legacy_cache_present")
    return _dispatch(root, thesis, default_mode, clean_query, clean_region, clean_days, clean_limit, key, configured)


def _emit(response: dict[str, object]) -> None:
    try:
        out = json.dumps(response).encode("utf-8")
    except (TypeError, ValueError):
        out = json.dumps(_unavailable("response_too_large")).encode("utf-8")
    if len(out) > _STDOUT_MAX:
        out = json.dumps(_unavailable("response_too_large")).encode("utf-8")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()


def _decode_request(raw: bytes) -> tuple[dict[str, object] | None, str | None]:
    try:
        request = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "invalid_params"
    if not isinstance(request, dict) or set(request) - _WIRE_FIELDS:
        return None, "invalid_params"
    return request, None


def _read_request() -> tuple[dict[str, object] | None, str | None]:
    """(request, error_code) for the bounded stdin worker payload."""
    try:
        raw = sys.stdin.buffer.read(_STDIN_MAX + 1)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        raw = b""
    if len(raw) > _STDIN_MAX:
        return None, "response_too_large"
    return _decode_request(raw)


def _narrow_opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _narrow_opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _narrow_path(value: object) -> Path | str | None:
    if value is None or isinstance(value, (str, Path)):
        return value
    return None

def _main_action(request: dict[str, object]) -> dict[str, object]:
    """Dispatch one validated worker request to the analytics entry point."""
    if request.get("confirmed") is not True:
        return _unavailable("consent_required")
    thesis_raw = request.get("thesis_id")
    if not isinstance(thesis_raw, str) or not thesis_raw.strip():
        return _unavailable("invalid_params")
    try:
        return get_youtube_analytics(thesis_id=thesis_raw, mode=_narrow_opt_str(request.get("mode")),
                                     query=_narrow_opt_str(request.get("query")),
                                     region=_narrow_opt_str(request.get("region")),
                                     days=_narrow_opt_int(request.get("days")),
                                     limit=_narrow_opt_int(request.get("limit")),
                                     data_root=_narrow_path(request.get("data_root")))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return _unavailable("source_unavailable")


def main(argv: list[str] | None = None) -> int:
    """Private one-shot worker: one bounded JSON on stdin, one on stdout."""
    request, err = _read_request()
    if err is not None or request is None:
        _emit(_unavailable(err or "invalid_params"))
        return 0
    _emit(_main_action(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
