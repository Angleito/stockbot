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


def _coerce_count(value: object) -> str | None:
    """Raw decimal-string-or-null counter; garbage raises _Malformed."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Malformed("count")
    if isinstance(value, int):
        if value < 0:
            raise _Malformed("count")
        return str(value)
    if isinstance(value, str) and _COUNT_RE.fullmatch(value):
        return value
    raise _Malformed("count")


def _pacific_today() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


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


def _reserve(root: Path, kind: str, ceiling: int) -> None:
    """Consume one quota unit under an flock-held lock; raises _QuotaRefused."""
    try:
        import fcntl as _fcntl
    except ImportError:  # pragma: no cover
        raise _QuotaRefused("quota_state_invalid") from None
    try:
        today = _pacific_today()
    except Exception:
        raise _QuotaRefused("quota_state_invalid") from None
    qdir = root / _DIRNAME
    qpath = qdir / _QUOTA_NAME
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        fh = open(qdir / _QUOTA_LOCK, "a+b")  # noqa: PTH123, SIM115
    except OSError:
        raise _QuotaRefused("quota_state_invalid") from None
    with fh:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
            try:
                if not qpath.exists():
                    raw = None
                else:
                    raw_bytes = qpath.read_bytes()
                    if len(raw_bytes) > _BODY_CAP:
                        raise _QuotaRefused("quota_state_invalid")
                    raw = json.loads(raw_bytes.decode("utf-8"))
            except (ValueError, OSError):
                raise _QuotaRefused("quota_state_invalid") from None
            if raw is None:
                days: dict[str, object] = {}
            elif isinstance(raw, dict) and raw.get("version") == 2:
                daymap = raw.get("days")
                if not isinstance(daymap, dict):
                    raise _QuotaRefused("quota_state_invalid")
                days = _validate_days(daymap)
            elif isinstance(raw, dict):
                # Legacy unversioned {UTC-date: count} ledger: validate, then
                # conservatively seed today at both ceilings (one-day pause;
                # surviving counts cannot prove Pacific-day usage).
                total = 0
                for key, value in raw.items():
                    if not isinstance(key, str) or not _is_int(value) or value < 0:
                        raise _QuotaRefused("quota_state_invalid")
                    total += value
                days = {today: {"search": max(_SEARCH_BUCKET, total),
                                "videos": max(_VIDEOS_BUCKET, total)}}
                _store_ledger(qpath, days)
                raise _QuotaRefused("quota_exhausted")
            else:
                raise _QuotaRefused("quota_state_invalid")
            for old in sorted(days)[:-3]:
                del days[old]
            bucket = days.get(today)
            if not isinstance(bucket, dict):
                bucket = {"search": 0, "videos": 0}
                days[today] = bucket
            if bucket[kind] >= ceiling:
                _store_ledger(qpath, days)
                raise _QuotaRefused("quota_exhausted")
            bucket[kind] += 1
            _store_ledger(qpath, days)
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


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
        try:
            body = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, "malformed_response"
        if not isinstance(body, dict):
            return None, "malformed_response"
        return body, None
    finally:
        with contextlib.suppress(Exception):
            resp.close()


def _snippet_row(video_id: str, snip: dict[str, object]) -> dict[str, object]:
    title = snip.get("title")
    channel_id = snip.get("channelId")
    channel_title = snip.get("channelTitle")
    published = snip.get("publishedAt")
    if (not isinstance(title, str) or not title
            or not isinstance(channel_id, str) or not channel_id
            or not isinstance(channel_title, str) or not channel_title
            or not _valid_ts(published)):
        raise _Malformed("metadata")
    live = snip.get("liveBroadcastContent", "none")
    if live is None:
        live = "none"
    if live not in _LIVE_VALUES:
        raise _Malformed("live")
    return {
        "video_id": video_id, "title": title, "channel_id": channel_id,
        "channel_title": channel_title, "published_at": published,
        "live_broadcast_content": live,
        "view_count": None, "like_count": None, "comment_count": None,
    }


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
    video_id = entry.get("id")
    snip = entry.get("snippet")
    if not isinstance(video_id, str) or not video_id or not isinstance(snip, dict):
        raise _Malformed("id")
    row = _snippet_row(video_id, snip)
    stats = entry.get("statistics")
    if stats is None:
        return row
    if not isinstance(stats, dict):
        raise _Malformed("statistics")
    row["view_count"] = _coerce_count(stats.get("viewCount"))
    row["like_count"] = _coerce_count(stats.get("likeCount"))
    row["comment_count"] = _coerce_count(stats.get("commentCount"))
    return row


def _apply_details(rows: list[dict[str, object]], body: dict[str, object]) -> str | None:
    """Merge videos.list into search rows; returns a warning code or None."""
    items = body.get("items")
    if not isinstance(items, list):
        return "details_unavailable"
    by_id = {e["id"]: e for e in items
             if isinstance(e, dict) and isinstance(e.get("id"), str)}
    missing = False
    for row in rows:
        vid = row["video_id"]
        if not isinstance(vid, str):
            missing = True
            continue
        entry = by_id.get(vid)
        if entry is None:
            missing = True
            continue
        stats = entry.get("statistics")
        if stats is None:
            continue
        if not isinstance(stats, dict):
            missing = True
            continue
        try:
            row["view_count"] = _coerce_count(stats.get("viewCount"))
            row["like_count"] = _coerce_count(stats.get("likeCount"))
            row["comment_count"] = _coerce_count(stats.get("commentCount"))
        except _Malformed:
            row["view_count"] = row["like_count"] = row["comment_count"] = None
            missing = True
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


def _topic(root: Path, thesis: Thesis, query: str, region: str, days: int,
           limit: int, key: str, search_ceiling: int) -> dict[str, object]:
    _reserve(root, "search", search_ceiling)
    now = datetime.now(timezone.utc)
    params: dict[str, str | int] = {
        "part": "snippet", "type": "video", "q": query, "order": "viewCount",
        "regionCode": region,
        "publishedAfter": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "publishedBefore": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxResults": limit, "key": key,
        "fields": ("regionCode,items(id/videoId,snippet(title,channelId,"
                   "channelTitle,publishedAt,liveBroadcastContent))"),
    }
    body, err = _http_get(_SEARCH_URL, params)
    if err:
        return _unavailable(err)
    if body is None:
        return _unavailable("source_unavailable")
    items = body.get("items")
    if not isinstance(items, list):
        return _unavailable("malformed_response")
    if "regionCode" in body and body["regionCode"] != region:
        return _unavailable("source_unavailable")
    try:
        rows = [_parse_search_item(item) for item in items]
    except _Malformed:
        return _unavailable("malformed_response")
    warnings: set[str] = set()
    if rows:
        _reserve(root, "videos", _VIDEOS_BUCKET)
        dbody, derr = _http_get(_VIDEOS_URL, {
            "part": "snippet,statistics",
            "id": ",".join(str(r["video_id"]) for r in rows),
            "key": key,
            "fields": ("items(id,snippet(title,channelId,channelTitle,"
                       "publishedAt,liveBroadcastContent),"
                       "statistics(viewCount,likeCount,commentCount))"),
        })
        if derr or dbody is None:
            warnings.add("details_unavailable")
        else:
            warn = _apply_details(rows, dbody)
            if warn:
                warnings.add(warn)
    if any(r["live_broadcast_content"] in ("live", "upcoming") for r in rows):
        warnings.add("live_ordering")
    return _success(thesis, "topic", query, region, days, "viewCount", rows, warnings)


def _popular(root: Path, thesis: Thesis, region: str, limit: int, key: str) -> dict[str, object]:
    _reserve(root, "videos", _VIDEOS_BUCKET)
    body, err = _http_get(_VIDEOS_URL, {
        "part": "snippet,statistics", "chart": "mostPopular",
        "regionCode": region, "maxResults": limit, "key": key,
        "fields": ("items(id,snippet(title,channelId,channelTitle,"
                   "publishedAt,liveBroadcastContent),"
                   "statistics(viewCount,likeCount,commentCount))"),
    })
    if err:
        return _unavailable(err)
    if body is None:
        return _unavailable("source_unavailable")
    items = body.get("items")
    if not isinstance(items, list):
        return _unavailable("malformed_response")
    try:
        rows = [_parse_chart_item(entry) for entry in items]
    except _Malformed:
        return _unavailable("malformed_response")
    warnings: set[str] = set()
    if any(r["live_broadcast_content"] in ("live", "upcoming") for r in rows):
        warnings.add("live_ordering")
    return _success(thesis, "popular", None, region, None, "mostPopular", rows, warnings)


def get_youtube_analytics(*, thesis_id: str, mode: str | None = "topic",
                          query: str | None = None, region: str | None = "US",
                          days: int | None = 30, limit: int | None = 10,
                          data_root: Path | str | None = None) -> dict[str, object]:
    """Thesis-labelled attention metrics; memory-only, fixed-shape errors."""
    if mode is None:
        mode = "topic"
    if region is None:
        region = "US"
    if days is None:
        days = 30
    if limit is None:
        limit = 10
    if not isinstance(thesis_id, str) or not thesis_id.strip():
        return _unavailable("invalid_params")
    if mode not in ("topic", "popular"):
        return _unavailable("invalid_params")
    if not _is_int(limit) or not 1 <= limit <= 20:
        return _unavailable("invalid_params")
    if (not isinstance(region, str) or len(region) != 2
            or not region.isascii() or not region.isalpha()):
        return _unavailable("invalid_params")
    region = region.upper()
    if data_root is not None and not isinstance(data_root, (str, os.PathLike)):
        return _unavailable("invalid_params")
    if mode == "topic":
        if not _is_int(days) or not 1 <= days <= 90:
            return _unavailable("invalid_params")
        if not isinstance(query, str):
            return _unavailable("invalid_params")
        query = query.strip()
        if not query or len(query) > 200:
            return _unavailable("invalid_params")
        if any(ord(c) < 32 or ord(c) == 127 for c in query):
            return _unavailable("invalid_params")
    elif query is not None:
        return _unavailable("invalid_params")
    else:
        query, days = None, None
    if not google_data_enabled():
        return _disabled("google_disabled")
    key = get_google_cloud_api_key()
    if not key:
        return _disabled("missing_key")
    try:
        configured = get_youtube_search_daily_limit()
    except ValueError:
        return _unavailable("invalid_config")
    if not _is_int(configured) or configured <= 0:
        return _unavailable("invalid_config")
    root = Path(data_root) if data_root else get_data_root()
    try:
        thesis = ThesisRepository(root / "thesis").load_thesis(thesis_id)
    except Exception:
        return _unavailable("invalid_thesis")
    if mode == "topic":
        if query is None or days is None:
            return _unavailable("invalid_params")
        if private_pattern_hit(query):
            return _unavailable("private_args_denied")
    gdir = root / _DIRNAME
    for name in (_CACHE_NAME, _CACHE_NAME + ".tmp"):
        probe = gdir / name
        if probe.is_symlink() or probe.exists():
            return _unavailable("legacy_cache_present")
    try:
        if mode == "topic":
            if query is None or days is None:
                return _unavailable("invalid_params")
            return _topic(root, thesis, query, region, days, limit,
                          key, min(configured, _SEARCH_BUCKET))
        return _popular(root, thesis, region, limit, key)
    except _QuotaRefused as refused:
        return _unavailable(refused.code)


def _emit(response: dict[str, object]) -> None:
    try:
        out = json.dumps(response).encode("utf-8")
    except (TypeError, ValueError):
        out = json.dumps(_unavailable("response_too_large")).encode("utf-8")
    if len(out) > _STDOUT_MAX:
        out = json.dumps(_unavailable("response_too_large")).encode("utf-8")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    """Private one-shot worker: one bounded JSON on stdin, one on stdout."""
    try:
        raw = sys.stdin.buffer.read(_STDIN_MAX + 1)
    except Exception:
        raw = b""
    if len(raw) > _STDIN_MAX:
        _emit(_unavailable("response_too_large"))
        return 0
    try:
        request = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _emit(_unavailable("invalid_params"))
        return 0
    if not isinstance(request, dict) or set(request) - _WIRE_FIELDS:
        _emit(_unavailable("invalid_params"))
        return 0
    if request.get("confirmed") is not True:
        _emit(_unavailable("consent_required"))
        return 0
    if "thesis_id" not in request:
        _emit(_unavailable("invalid_params"))
        return 0
    kwargs = {"thesis_id": request.get("thesis_id"), "mode": request.get("mode", "topic"),
              "query": request.get("query"), "region": request.get("region", "US"),
              "days": request.get("days", 30), "limit": request.get("limit", 10)}
    try:
        _emit(get_youtube_analytics(**kwargs))
    except Exception:
        _emit(_unavailable("source_unavailable"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
