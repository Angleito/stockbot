#!/usr/bin/env python3
"""Reproducible research slice: short-interest change + shares-outstanding
change between the two most recent FINRA settlement cycles knowable on or
before --as-of.

Every SEC fact is filtered by known_at <= as_of, so this slice can be
reproduced later from the raw archive without future data.

Usage:
    python scripts/research_slice.py --as-of 2026-08-14 [--limit 25]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import screens

TABLE_HEADERS = (
    "Rank",
    "Ticker",
    "Short now",
    "Short prior",
    "Short chg %",
    "SI % now",
    "SI % prior",
    "PP chg",
    "Shares now",
    "Shares prior",
    "Shares chg %",
)


def _fmt(value: object) -> str:  # object: screen rows are untyped app-side dicts; display-only, never flows back
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD (or ISO timestamp)")
    parser.add_argument("--limit", type=int, default=screens.DEFAULT_LIMIT)
    parser.add_argument("--data-root", default=None, help="data directory (default: project data/)")
    return parser.parse_args(argv)


def fetch_screen(args: argparse.Namespace) -> dict[str, object]:
    return screens.short_interest_change_screen(
        args.as_of, limit=args.limit, data_root=Path(args.data_root) if args.data_root else None
    )


def extract_entries(result: dict[str, object]) -> list[dict[str, object]]:
    entries_raw = result.get("entries")
    if not isinstance(entries_raw, list):
        return []
    return [e for e in entries_raw if isinstance(e, dict)]


def build_rows(entries: list[dict[str, object]]) -> list[tuple[str, ...]]:
    return [
        (
            str(e["rank"]),
            str(e["ticker"]),
            _fmt(e["short_shares_current"]),
            _fmt(e["short_shares_prior"]),
            _fmt(e["short_change_pct"]),
            _fmt(e["short_interest_percent_current"]),
            _fmt(e["short_interest_percent_prior"]),
            _fmt(e["si_pp_change"]),
            _fmt(e["shares_outstanding_current"]),
            _fmt(e["shares_outstanding_prior"]),
            _fmt(e["shares_change_pct"]),
        )
        for e in entries
    ]


def _column_widths(rows: list[tuple[str, ...]]) -> list[int]:
    widths = [len(h) for h in TABLE_HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return widths


def _render_header(result: dict[str, object]) -> list[str]:
    prior = str(result["settlement_prior"] or "-")
    lines: list[str] = [
        f"Short-interest change + shares-outstanding change (calc {result['calculation_version']})",
        f"As of: {result['as_of']} | current settlement: {result['settlement_current']} | prior settlement: {prior}",
        f"Coverage: {result['coverage']}",
    ]
    return lines


def _render_grid(rows: list[tuple[str, ...]], widths: list[int]) -> list[str]:
    lines: list[str] = [
        " | ".join(h.ljust(widths[i]) for i, h in enumerate(TABLE_HEADERS)),
        "-+-".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return lines


def format_table(result: dict[str, object]) -> str:
    rows = build_rows(extract_entries(result))
    return "\n".join(_render_header(result) + _render_grid(rows, _column_widths(rows)))


def format_evidence(entries: list[dict[str, object]]) -> str:
    lines: list[str] = ["", "Evidence links:"]
    for e in entries:
        lines.append(f"  {e['ticker']}:")
        lines.append(f"    FINRA snapshot: {e['finra_source_url']} (settlement {e['settlement_current']})")
        if e["sec_accession_current"]:
            lines.append(
                f"    Shares fact (now): {e['sec_source_url_current']} accession {e['sec_accession_current']} filed {e['sec_filed_at_current']}"
            )
        if e["sec_accession_prior"]:
            lines.append(
                f"    Shares fact (prior): {e['sec_source_url_prior']} accession {e['sec_accession_prior']} filed {e['sec_filed_at_prior']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = fetch_screen(args)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(format_table(result))
    print(format_evidence(extract_entries(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
