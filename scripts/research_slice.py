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

from app.analytics import screens  # noqa: E402


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD (or ISO timestamp)")
    parser.add_argument("--limit", type=int, default=screens.DEFAULT_LIMIT)
    parser.add_argument("--data-root", default=None, help="data directory (default: project data/)")
    args = parser.parse_args()

    result = screens.short_interest_change_screen(
        args.as_of, limit=args.limit, data_root=Path(args.data_root) if args.data_root else None
    )
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    print(f"Short-interest change + shares-outstanding change (calc {result['calculation_version']})")
    print(f"As of: {result['as_of']} | current settlement: {result['settlement_current']} | prior settlement: {result['settlement_prior'] or '-'}")
    print(f"Coverage: {result['coverage']}")
    headers = ("Rank", "Ticker", "Short now", "Short prior", "Short chg %", "SI % now", "SI % prior", "PP chg", "Shares now", "Shares prior", "Shares chg %")
    widths = [len(h) for h in headers]
    rows = [
        (
            str(e["rank"]), e["ticker"], _fmt(e["short_shares_current"]), _fmt(e["short_shares_prior"]),
            _fmt(e["short_change_pct"]), _fmt(e["short_interest_percent_current"]),
            _fmt(e["short_interest_percent_prior"]), _fmt(e["si_pp_change"]),
            _fmt(e["shares_outstanding_current"]), _fmt(e["shares_outstanding_prior"]),
            _fmt(e["shares_change_pct"]),
        )
        for e in result["entries"]
    ]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print("\nEvidence links:")
    for e in result["entries"]:
        print(f"  {e['ticker']}:")
        print(f"    FINRA snapshot: {e['finra_source_url']} (settlement {e['settlement_current']})")
        if e["sec_accession_current"]:
            print(f"    Shares fact (now): {e['sec_source_url_current']} accession {e['sec_accession_current']} filed {e['sec_filed_at_current']}")
        if e["sec_accession_prior"]:
            print(f"    Shares fact (prior): {e['sec_source_url_prior']} accession {e['sec_accession_prior']} filed {e['sec_filed_at_prior']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())