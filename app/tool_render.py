"""Render tool results as compact plain text/Markdown for the main model.

Internal results stay structured Python dicts/JSON (caching, validation,
deterministic metrics, the small FINRA analysis model's validated prose).
Only the tool message sent to the main chat model is rendered to compact
text. Rendering enforces a fixed UTF-8 byte budget and reduces the structure
(rows first, then verbose sections, then individual text values) before
rendering — it never slices serialized JSON or rendered text blindly.
"""

from __future__ import annotations

from typing import Any, Optional

MAX_TOOL_MESSAGE_BYTES = 64 * 1024

TRUNCATED_MARKER = "... [Tool output truncated]"

_TEXT_KEYS = ("text", "diff", "summary")


def render_tool_result(
    result: Any, max_bytes: int = MAX_TOOL_MESSAGE_BYTES
) -> str:
    """Render a tool result as compact text within the byte budget.

    Always returns a non-empty string of at most max_bytes UTF-8 bytes.
    """
    if not isinstance(result, dict):
        result = {"result": result}
    if "error" in result:
        return _render_error(result, max_bytes)
    if "records" in result and "fields" in result:
        text = _render_datapoints(result, max_bytes)
    elif "coverage" in result and "metrics" in result:
        text = _render_briefing(result, max_bytes)
    elif _is_text_result(result):
        text = _render_text_result(result, max_bytes)
    else:
        text = _render_generic(result, max_bytes)
    if _utf8_size(text) <= max_bytes:
        return text
    return _minimal(result, max_bytes)


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_bytes(text: str, max_bytes: int, marker: str = TRUNCATED_MARKER) -> str:
    """Byte-safe prefix truncation with an explicit marker appended."""
    if _utf8_size(text) <= max_bytes:
        return text
    room = max_bytes - _utf8_size(marker)
    if room < 0:
        return marker[: max(0, max_bytes)]
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf8_size(text[:mid]) <= room:
            lo = mid
        else:
            hi = mid - 1
    prefix = text[:lo].rstrip()
    return prefix + marker


def _fit_lines(lines: list[str], max_bytes: int) -> tuple[list[str], int]:
    """Keep as many complete lines as fit; returns (kept, omitted)."""
    kept: list[str] = []
    omitted = 0
    used = 0
    for line in lines:
        cost = _utf8_size(line) + 1
        if cost > max_bytes:
            kept.append(_truncate_bytes(line, max_bytes - used))
            return kept, omitted
        if used + cost > max_bytes:
            omitted += 1
            continue
        kept.append(line)
        used += cost
    return kept, omitted


def _cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return s.strip()


_MAX_TABLE_CELL_CHARS = 200


def _table_cell(value: Any) -> str:
    """Single table cell; oversized values are truncated with a marker so
    one huge field cannot balloon the whole table."""
    s = _cell(value)
    if len(s) <= _MAX_TABLE_CELL_CHARS:
        return s
    return s[:_MAX_TABLE_CELL_CHARS].rstrip() + f"... [{len(s)} chars]"


def _minimal(result: dict, max_bytes: int) -> str:
    source = result.get("source") or result.get("dataset_id") or "tool result"
    text = f"Source: {source} | {TRUNCATED_MARKER}"
    return _truncate_bytes(text, max_bytes, marker="")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _render_error(result: dict, max_bytes: int) -> str:
    msg = str(result.get("error") or "Unknown error").strip()
    lines = [f"Error: {msg}"]
    for key in ("dataset", "dataset_id", "source", "request_purpose"):
        value = result.get(key)
        if value:
            lines.append(f"{key}: {value}")
    if result.get("http_status") is not None:
        lines.append(f"http_status: {result['http_status']}")
    if result.get("finra_response"):
        body = _cell(result["finra_response"])
        lines.append(f"finra_response: {body}")
    if result.get("environment"):
        lines.append(f"environment: {result['environment']}")
    return _truncate_bytes("\n".join(lines), max_bytes, marker="")


# ---------------------------------------------------------------------------
# get_finra_datapoints: compact Markdown table of only the selected fields
# ---------------------------------------------------------------------------


def _render_datapoints(result: dict, max_bytes: int) -> str:
    fields = [str(f) for f in (result.get("fields") or [])]
    if not fields:
        return _render_generic(result, max_bytes)
    records = result.get("records") or []

    header = "| " + " | ".join(_table_cell(f) for f in fields) + " |"
    sep = "|" + "|".join("---" for _ in fields) + "|"
    stale_banner = _datapoints_stale_banner(result)
    footer = _datapoints_footer(result)
    if footer:
        footer = "\n" + footer
    reserved = (
        _utf8_size(TRUNCATED_MARKER + "\n" + "Omitted rows: 99999\n" + footer)
        + 32
    )
    if stale_banner:
        reserved += _utf8_size(stale_banner) + 2

    if reserved > max_bytes:
        return _minimal(result, max_bytes)

    used = _utf8_size(header + "\n" + sep + "\n")
    out = [header, sep]
    if stale_banner:
        out.append(stale_banner)
        used += _utf8_size(stale_banner) + 1
    omitted = 0
    for row in records:
        if not isinstance(row, dict):
            line = _table_cell(row)
        else:
            line = "| " + " | ".join(_table_cell(row.get(f)) for f in fields) + " |"
        if used + _utf8_size(line) + 1 + reserved > max_bytes:
            omitted += 1
            continue
        out.append(line)
        used += _utf8_size(line) + 1

    text = "\n".join(out) + footer
    if omitted:
        text = (
            "\n".join(out)
            + f"\n{TRUNCATED_MARKER}\nOmitted rows: {omitted}"
            + footer
        )
    return text


def _datapoints_footer(result: dict) -> str:
    parts = []
    if result.get("source"):
        parts.append(f"Source: {result['source']}")
    returned = result.get("returned_count")
    total = result.get("total_records")
    if total is not None:
        pag = f"{returned if returned is not None else '?'} returned of {total} total"
    else:
        pag = f"{returned if returned is not None else '?'} returned (Record-Total absent)"
    if result.get("pagination_source"):
        pag += f", {result['pagination_source']}"
    if result.get("may_have_more") is not None:
        pag += f", more pages: {'yes' if result['may_have_more'] else 'no'}"
    if result.get("next_offset") is not None:
        pag += f", next_offset {result['next_offset']}"
    parts.append("Pagination: " + pag)
    if result.get("as_of_date"):
        parts.append(
            f"As of: {result['as_of_date']} "
            f"(freshness: {result.get('data_freshness') or 'unknown'})"
        )
    if result.get("environment"):
        parts.append(f"Environment: {result['environment']}")
    warnings = [str(w) for w in (result.get("warnings") or []) if str(w).strip()]
    non_stale = [w for w in warnings if "STALE" not in w]
    if non_stale:
        parts.append("Warnings: " + "; ".join(non_stale))
    return "\n".join(parts)


def _datapoints_stale_banner(result: dict) -> str:
    if result.get("data_freshness") == "stale" and result.get("as_of_date"):
        return (
            f"!! STALE/HISTORICAL DATA !! Newest record is "
            f"{result['as_of_date']} (over 90 days old); this is historical "
            "data, NOT current market data."
        )
    return ""


# ---------------------------------------------------------------------------
# FINRA analysis briefing
# ---------------------------------------------------------------------------


def _render_briefing(result: dict, max_bytes: int) -> str:
    cov = result.get("coverage") or {}
    query = result.get("query") or {}

    name = result.get("name") or result.get("dataset") or result.get("dataset_id")
    ticker = query.get("ticker") or result.get("ticker")
    title = f"FINRA: {name}" + (f" — {ticker}" if ticker else "")

    forced = [title]
    if result.get("source"):
        forced.append(f"Source: {result['source']}")

    cover = []
    if cov.get("rows_analyzed") is not None:
        cover.append(f"{cov['rows_analyzed']} rows analyzed")
    if cov.get("rows_matched") is not None:
        cover.append(f"{cov['rows_matched']} rows returned")
    if cov.get("first_date") and cov.get("last_date"):
        cover.append(f"{cov['first_date']} to {cov['last_date']}")
    statuses = []
    for flag in ("page_complete", "query_complete", "analysis_complete"):
        value = cov.get(flag)
        if value is True:
            status = "yes"
        elif value is False:
            status = "no"
        else:
            status = "unknown"
        statuses.append(f"{flag.replace('_', ' ')}: {status}")
    if statuses:
        cover.append("; ".join(statuses))
    if cover:
        forced.append("Coverage: " + ", ".join(cover))

    query_parts = []
    if ticker:
        query_parts.append(f"ticker {ticker}")
    if query.get("start_date") or query.get("end_date"):
        query_parts.append(
            f"{query.get('start_date') or '…'}..{query.get('end_date') or '…'}"
        )
    if query.get("limit") is not None:
        query_parts.append(f"limit {query['limit']}")
    if query.get("offset"):
        query_parts.append(f"offset {query['offset']}")
    if query_parts:
        forced.append("Query: " + ", ".join(query_parts))

    warnings = _render_warnings(result)
    if warnings:
        forced.append("Warnings:\n" + "\n".join("  - " + w for w in warnings))
    pagination = _render_pagination(result)
    if pagination:
        forced.append("Pagination: " + pagination)

    if result.get("as_of_date"):
        forced.append(
            f"As of: {result['as_of_date']} "
            f"(freshness: {result.get('data_freshness') or 'unknown'})"
        )
    if result.get("environment"):
        forced.append(f"Environment: {result['environment']}")

    used = _utf8_size("\n".join(forced))
    if used > max_bytes:
        return _minimal(result, max_bytes)

    out = list(forced)
    optional: list[tuple[str, list[str]]] = []

    metrics = _render_metrics(result)
    if metrics:
        optional.append(("Key metrics", metrics))
    briefing = _render_briefing_prose(result)
    if briefing:
        optional.append(("Briefing", briefing))
    trends = [str(t) for t in (result.get("trends") or [])]
    if trends:
        optional.append(("Trends", trends))
    categorical = _render_categorical(result)
    if categorical:
        optional.append(("Categorical", categorical))

    for header, lines in optional:
        block = header + "\n" + "\n".join("  - " + l for l in lines)
        cost = _utf8_size(block) + 1
        if used + cost <= max_bytes:
            out.append(block)
            used += cost
            continue
        kept, omitted = _fit_lines(
            ["  - " + l for l in lines], max_bytes - used - _utf8_size(header) - 4
        )
        if kept:
            out.append(header)
            out.extend(kept)
            used += _utf8_size(header) + _utf8_size("\n".join(kept)) + 4
        if omitted:
            out.append(f"{TRUNCATED_MARKER} (Omitted rows: {omitted})")
            break

    return "\n".join(out)


def _render_metrics(result: dict) -> list[str]:
    metrics = result.get("metrics") or {}
    lines: list[str] = []
    for entry in metrics.get("latest_vs_prior") or []:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field", "?")
        latest = entry.get("latest")
        prior = entry.get("prior")
        change = entry.get("change")
        pct = entry.get("change_percent")
        base = f"{field}: latest {latest}"
        if prior is not None:
            base += f" vs prior {prior}"
        if change is not None:
            base += f" (change {change:+,}"
            if pct is not None:
                base += f", {pct:+.2f}%"
            base += ")"
        lines.append(base)
    for name in sorted(metrics.get("fields") or {}):
        stats = metrics["fields"][name]
        if not isinstance(stats, dict):
            continue
        parts = [f"{key} {stats[key]}" for key in ("min", "max", "mean", "median", "sum") if key in stats]
        if "missing" in stats:
            parts.append(f"missing {stats['missing']}")
        lines.append(f"{name}: {', '.join(parts)}")
    return lines


def _render_briefing_prose(result: dict) -> list[str]:
    briefing = result.get("briefing")
    if not isinstance(briefing, dict) or not briefing.get("summary"):
        return []
    lines = [str(briefing["summary"])]
    for finding in briefing.get("key_findings") or []:
        if isinstance(finding, str) and finding.strip():
            lines.append(finding.strip())
    return lines


def _render_categorical(result: dict) -> list[str]:
    breakdowns = (result.get("metrics") or {}).get("categorical") or {}
    lines: list[str] = []
    for field, counts in breakdowns.items():
        if not isinstance(counts, dict):
            continue
        top = ", ".join(f"{k} {v}" for k, v in list(counts.items())[:8])
        lines.append(f"{field}: {top}")
    return lines


def _render_warnings(result: dict) -> list[str]:
    return [str(w) for w in (result.get("warnings") or []) if str(w).strip()]


def _render_pagination(result: dict) -> str:
    total = result.get("total_records")
    source = result.get("pagination_source")
    parts = []
    if total is not None:
        parts.append(f"{total} total records")
    if source:
        parts.append(source)
    if result.get("may_have_more") is not None:
        parts.append(
            f"more pages: {'yes' if result['may_have_more'] else 'no'}"
        )
    if result.get("next_offset") is not None:
        parts.append(f"next_offset {result['next_offset']}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# SEC filing-style results: header + byte-safe truncated text
# ---------------------------------------------------------------------------


def _is_text_result(result: dict) -> bool:
    for key in _TEXT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and len(value) > 400:
            return True
    return False


def _render_text_result(result: dict, max_bytes: int) -> str:
    header_lines = []
    ticker = result.get("ticker")
    if ticker:
        header_lines.append(f"Ticker: {ticker}")
    for key in ("form_type", "item", "statement_type", "concept_searched"):
        if result.get(key):
            header_lines.append(f"{key}: {result[key]}")
    if result.get("filed"):
        header_lines.append(f"Filed: {result['filed']}")
    if result.get("source"):
        header_lines.append(f"Source: {result['source']}")
    header = "\n".join(header_lines)
    header_cost = _utf8_size(header) + 1
    if header_cost > max_bytes:
        return _minimal(result, max_bytes)

    body_keys = [k for k in _TEXT_KEYS if isinstance(result.get(k), str)]
    budget_for_text = max_bytes - header_cost - 2
    if budget_for_text <= 0:
        return header
    body_lines: list[str] = []
    for key in body_keys:
        text = result[key]
        if not text.strip():
            continue
        label = "" if key == "text" and len(body_keys) == 1 else f"{key}: "
        body_lines.append(label + text)
    body = "\n\n".join(body_lines)
    if body:
        body = _truncate_bytes(body, budget_for_text)
    return header + "\n\n" + body if body else header


# ---------------------------------------------------------------------------
# Generic structured results (fundamentals, XBRL facts, catalog, etc.)
# ---------------------------------------------------------------------------


def _render_generic(result: dict, max_bytes: int) -> str:
    lines: list[str] = []
    used = 0
    omitted: list[str] = []

    def _add(text: str) -> bool:
        nonlocal used
        cost = _utf8_size(text) + 1
        if used + cost > max_bytes:
            return False
        lines.append(text)
        used += cost
        return True

    for key in ("ticker", "source", "concept_searched"):
        value = result.get(key)
        if value is not None:
            _add(f"{key}: {_cell(value)}")

    for key, value in result.items():
        if value is None or key in ("ticker", "source", "concept_searched"):
            continue
        if isinstance(value, list):
            kept = 0
            for item in value:
                line = "  - " + _cell(item if not isinstance(item, dict) else _summarize_dict(item))
                if not _add(line):
                    omitted.append(key)
                    break
                kept += 1
            if kept == 0 and not value:
                _add(f"{key}: none")
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}: {_cell(v)}" for k, v in value.items())
            if not _add(f"{key}: {rendered}"):
                _add(f"{key}: {TRUNCATED_MARKER}")
        else:
            if not _add(f"{key}: {_cell(value)}"):
                _add(f"{key}: {TRUNCATED_MARKER}")

    if omitted:
        _add(f"{TRUNCATED_MARKER} (Omitted rows: {len(omitted)} in {', '.join(omitted)})")
    if not lines:
        return _minimal(result, max_bytes)
    return "\n".join(lines)


def _summarize_dict(item: dict) -> str:
    parts = []
    for key in ("dataset", "group", "name", "description", "concept", "value", "period_end"):
        if key in item and item[key] not in (None, ""):
            parts.append(f"{key} {_cell(item[key])}")
    if not parts:
        return ", ".join(f"{k} {_cell(v)}" for k, v in list(item.items())[:6])
    return ", ".join(parts)