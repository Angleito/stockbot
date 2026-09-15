"""Dividend lifecycle analysis from paid dividend events.

Pure functions only: no storage, no network. CAGR math is reused from
:app:func:`app.edgar_client._dividend_growth` (never re-implemented here).
Yearly regular totals are plain sums pre-CAGR, so
:func:`app.edgar_client._dividend_annual_history` does not apply (it keeps the
latest fact per year instead of summing).
"""

import datetime as _dt
import statistics as _statistics
from collections.abc import Mapping, Sequence

from app.edgar_client import _dividend_growth

_CADENCE_WINDOWS = (
    ("monthly", 25, 36),
    ("quarterly", 75, 110),
    ("semiannual", 150, 215),
    ("annual", 330, 400),
)
_SPECIAL_TYPES = ("special", "supplemental")


def _parse_day(value: object) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None

def _series_day(entry: tuple[_dt.date, float]) -> _dt.date:
    """Sort key: regular-series payment day (stable; ties keep insertion order)."""
    return entry[0]


def _regular_series(paid_events: Sequence[Mapping[str, object]] | None) -> list[tuple[_dt.date, float]]:
    """(payment_date, amount) for regular paid events, ascending by date."""
    series: list[tuple[_dt.date, float]] = []
    for event in paid_events or []:
        if not isinstance(event, dict) or event.get("dividend_type") != "regular":
            continue
        day = _parse_day(event.get("payment_date"))
        raw_amount = event.get("amount_per_share")
        if not isinstance(raw_amount, (int, float, str)):
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        if day is None:
            continue
        series.append((day, amount))
    series.sort(key=_series_day)
    return series


def _median_gap(paid_events: Sequence[Mapping[str, object]] | None) -> tuple[list[int], float | None]:
    """Sorted payment gaps plus their median (None when fewer than 2 dates)."""
    days = [day for day, _ in _regular_series(paid_events)]
    gaps = [(later - earlier).days for earlier, later in zip(days, days[1:])]
    if not gaps:
        return [], None
    return gaps, _statistics.median(gaps)


def _cadence_match(median_gap: float) -> tuple[str, tuple[int, int] | None]:
    """Cadence name + window bounds for a median gap (unknown when unmatched)."""
    for name, low, high in _CADENCE_WINDOWS:
        if low <= median_gap <= high:
            return name, (low, high)
    return "unknown", None


def _unknown_cadence(median_gap: float | None) -> dict[str, object]:
    """Unknown-cadence payload (keeps the median for downstream suspension math)."""
    return {
        "payment_cadence": "unknown",
        "cadence_confidence": None,
        "cadence_basis": "payment_dates",
        "median_interval_days": median_gap,
    }


def cadence_from_events(paid_events: Sequence[Mapping[str, object]] | None) -> dict[str, object]:
    """Median payment gap mapped onto a cadence window.

    >=3 gaps in one window -> high confidence, 2 gaps -> medium,
    otherwise (unknown, None).
    """
    gaps, median_gap = _median_gap(paid_events)
    if median_gap is None:
        return _unknown_cadence(None)
    cadence, bounds = _cadence_match(median_gap)
    in_window = sum(1 for gap in gaps if bounds[0] <= gap <= bounds[1]) if bounds else 0
    if cadence == "unknown" or in_window < 2:
        return _unknown_cadence(median_gap)
    return {
        "payment_cadence": cadence,
        "cadence_confidence": "high" if in_window >= 3 else "medium",
        "cadence_basis": "payment_dates",
        "median_interval_days": median_gap,
    }


def _cadence_context(paid_events: Sequence[Mapping[str, object]] | None) -> tuple[dict[str, object], list[tuple[_dt.date, float]], float, bool]:
    """Cadence + regular series + gap days + observed flag for lifecycle math."""
    cadence = cadence_from_events(paid_events)
    median_gap = cadence["median_interval_days"]
    gap_days = median_gap if isinstance(median_gap, (int, float)) else 0
    observed = cadence["payment_cadence"] != "unknown" and median_gap
    return cadence, _regular_series(paid_events), gap_days, bool(observed)


def _latest_change(series: Sequence[tuple[_dt.date, float]]) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """(increase, cut) from the last two regular payments."""
    if len(series) < 2:
        return None, None
    (prior_date, prior), (new_date, new) = series[-2], series[-1]
    if new > prior:
        return ({"pct": _change_pct(prior, new), "amount": round(new, 4),
                 "date": new_date.isoformat()}, None)
    if new < prior:
        return (None, {"pct": _change_pct(prior, new), "prior": round(prior, 4),
                       "new": round(new, 4), "date": new_date.isoformat()})
    return None, None


def _freeze_run(series: Sequence[tuple[_dt.date, float]], cadence: Mapping[str, object], observed: bool) -> dict[str, object] | None:
    """Frozen-dividend marker once equal payments span the cadence threshold."""
    if not series or not observed:
        return None
    run = 1
    for (_, older), (_, newer) in zip(reversed(series[:-1]), reversed(series[1:])):
        if newer != older:
            break
        run += 1
    threshold = 4 if cadence["payment_cadence"] in ("monthly", "quarterly") else 2
    if run < threshold:
        return None
    return {"amount": round(series[-1][1], 4), "count": run}


def _event_amount(event: Mapping[str, object]) -> float | None:
    """Numeric amount of one event (None when unparsable)."""
    raw_amount = event.get("amount_per_share")
    if not isinstance(raw_amount, (int, float, str)):
        return None
    try:
        return float(raw_amount)
    except (TypeError, ValueError):
        return None


def _special_totals(paid_events: Sequence[Mapping[str, object]] | None) -> tuple[list[dict[str, object]], float, float, float]:
    """(specials list, regular total, special total, grand total)."""
    specials: list[dict[str, object]] = []
    regular_total = special_total = total = 0.0
    for event in paid_events or []:
        if not isinstance(event, dict):
            continue
        amount = _event_amount(event)
        if amount is None:
            continue
        total += amount
        dtype = event.get("dividend_type")
        if dtype == "regular":
            regular_total += amount
        elif dtype in _SPECIAL_TYPES:
            special_total += amount
            specials.append({"amount": round(amount, 4), "payment_date": event.get("payment_date")})
    return specials, regular_total, special_total, total


def _possible_suspension(observed: bool, series: Sequence[tuple[_dt.date, float]], gap_days: float, as_of: _dt.date | str | None) -> bool:
    """True when the last payment is >3 median gaps before as_of."""
    as_of_day = _parse_day(as_of) if as_of is not None else None
    return bool(
        observed and series and as_of_day is not None
        and (as_of_day - series[-1][0]).days > 3 * gap_days
    )


def _reinstatement(observed: bool, series: Sequence[tuple[_dt.date, float]], gap_days: float) -> dict[str, object] | None:
    """First post-gap resumption date once any inter-payment gap exceeds 3x median."""
    if not observed or len(series) < 2:
        return None
    for (prev_day, _), (day, _) in zip(series[:-1], series[1:]):
        if (day - prev_day).days > 3 * gap_days:
            return {"date": day.isoformat()}
    return None


def _change_pct(prior: float, new: float) -> float | None:
    if prior is None or prior <= 0:
        return None
    return round((new - prior) / prior, 4)


def lifecycle_from_events(paid_events: Sequence[Mapping[str, object]] | None, *, as_of: _dt.date | str | None = None) -> dict[str, object]:
    """Increase/cut/freeze/specials/suspension/reinstatement from paid events."""
    cadence, series, gap_days, observed = _cadence_context(paid_events)
    increase, cut = _latest_change(series)
    freeze = _freeze_run(series, cadence, observed)
    specials, regular_total, special_total, total = _special_totals(paid_events)
    return {
        **cadence,
        "increase": increase,
        "cut": cut,
        "freeze": freeze,
        "specials": specials,
        "total_paid_per_share": round(total, 4),
        "regular_paid_per_share": round(regular_total, 4),
        "special_paid_per_share": round(special_total, 4),
        "possible_suspension": _possible_suspension(observed, series, gap_days, as_of),
        "reinstatement": _reinstatement(observed, series, gap_days),
    }


def _regular_annual_totals(paid_events: Sequence[Mapping[str, object]] | None) -> dict[int, float]:
    totals: dict[int, float] = {}
    for day, amount in _regular_series(paid_events):
        totals[day.year] = round(totals.get(day.year, 0.0) + amount, 4)
    return totals


def _has_consecutive_run(years: list[int], length: int = 5) -> bool:
    ordered = sorted(set(years))
    run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if cur == prev + 1 else 1
        if run >= length:
            return True
    return False

def analyze_dividends(*, paid_events: Sequence[Mapping[str, object]] | None, as_of: _dt.date | str | None = None, ttm_dps: float | None = None, growth: Mapping[str, object] | None = None,
                      annual_history: Sequence[Mapping[str, object]] | None = None) -> dict[str, object]:
    """Full lifecycle + growth-trend analysis.

    ``growth``/``annual_history`` arrive total-aggregate basis from the caller;
    ``ttm_dps``/``annual_history`` anchor the wiring shape and are otherwise
    unused here. Regular-basis growth keys are added alongside (never swapped)
    once paid regular events cover >=5 consecutive calendar years.
    """
    result = lifecycle_from_events(paid_events, as_of=as_of)
    growth = growth or {}
    growth_1y = growth.get("growth_1y")
    growth_5y = growth.get("growth_5y_cagr")
    if not isinstance(growth_1y, (int, float)) or not isinstance(growth_5y, (int, float)):
        trend = "stable_or_unknown"
    elif growth_1y < growth_5y:
        trend = "decelerating"
    elif growth_1y > growth_5y:
        trend = "accelerating"
    else:
        trend = "stable_or_unknown"
    annual_regular = _regular_annual_totals(paid_events)
    result.update({
        "growth_trend": trend,
        "growth_basis": "total_aggregates",
        "regular_basis_growth": (
            _dividend_growth(annual_regular)
            if _has_consecutive_run(list(annual_regular), 5)
            else None
        ),
    })
    return result
