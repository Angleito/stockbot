"""Dividend lifecycle analysis from paid dividend events.

Pure functions only: no storage, no network. CAGR math is reused from
:app:func:`app.edgar_client._dividend_growth` (never re-implemented here).
Yearly regular totals are plain sums pre-CAGR, so
:func:`app.edgar_client._dividend_annual_history` does not apply (it keeps the
latest fact per year instead of summing).
"""

import datetime as _dt
import statistics as _statistics

from app.edgar_client import _dividend_growth

_CADENCE_WINDOWS = (
    ("monthly", 25, 36),
    ("quarterly", 75, 110),
    ("semiannual", 150, 215),
    ("annual", 330, 400),
)
_SPECIAL_TYPES = ("special", "supplemental")


def _parse_day(value) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _regular_series(paid_events) -> list[tuple[_dt.date, float]]:
    """(payment_date, amount) for regular paid events, ascending by date."""
    series = []
    for event in paid_events or []:
        if not isinstance(event, dict) or event.get("dividend_type") != "regular":
            continue
        day = _parse_day(event.get("payment_date"))
        try:
            amount = float(event["amount_per_share"])
        except (KeyError, TypeError, ValueError):
            continue
        if day is None:
            continue
        series.append((day, amount))
    series.sort(key=lambda item: item[0])
    return series

def cadence_from_events(paid_events) -> dict:
    """Median payment gap mapped onto a cadence window.

    >=3 gaps in one window -> high confidence, 2 gaps -> medium,
    otherwise (unknown, None).
    """
    days = [day for day, _ in _regular_series(paid_events)]
    gaps = [(later - earlier).days for earlier, later in zip(days, days[1:])]
    if not gaps:
        return {
            "payment_cadence": "unknown",
            "cadence_confidence": None,
            "cadence_basis": "payment_dates",
            "median_interval_days": None,
        }
    median_gap = _statistics.median(gaps)
    bounds = next(
        ((low, high) for name, low, high in _CADENCE_WINDOWS if low <= median_gap <= high),
        None,
    )
    cadence = next(
        (name for name, low, high in _CADENCE_WINDOWS if low <= median_gap <= high),
        "unknown",
    )
    in_window = sum(1 for gap in gaps if bounds[0] <= gap <= bounds[1]) if bounds else 0
    if cadence == "unknown" or in_window < 2:
        return {
            "payment_cadence": "unknown",
            "cadence_confidence": None,
            "cadence_basis": "payment_dates",
            "median_interval_days": median_gap,
        }
    return {
        "payment_cadence": cadence,
        "cadence_confidence": "high" if in_window >= 3 else "medium",
        "cadence_basis": "payment_dates",
        "median_interval_days": median_gap,
    }


def _change_pct(prior: float, new: float) -> float | None:
    if prior is None or prior <= 0:
        return None
    return round((new - prior) / prior, 4)


def lifecycle_from_events(paid_events, *, as_of=None) -> dict:
    """Increase/cut/freeze/specials/suspension/reinstatement from paid events."""
    cadence = cadence_from_events(paid_events)
    median_gap = cadence["median_interval_days"]
    observed = cadence["payment_cadence"] != "unknown" and median_gap
    series = _regular_series(paid_events)

    increase = cut = None
    if len(series) >= 2:
        (prior_date, prior), (new_date, new) = series[-2], series[-1]
        if new > prior:
            increase = {"pct": _change_pct(prior, new), "amount": round(new, 4),
                        "date": new_date.isoformat()}
        elif new < prior:
            cut = {"pct": _change_pct(prior, new), "prior": round(prior, 4),
                   "new": round(new, 4), "date": new_date.isoformat()}

    freeze = None
    if series and observed:
        run = 1
        for (_, older), (_, newer) in zip(reversed(series[:-1]), reversed(series[1:])):
            if newer != older:
                break
            run += 1
        threshold = 4 if cadence["payment_cadence"] in ("monthly", "quarterly") else 2
        if run >= threshold:
            freeze = {"amount": round(series[-1][1], 4), "count": run}

    specials = []
    regular_total = special_total = total = 0.0
    for event in paid_events or []:
        if not isinstance(event, dict):
            continue
        try:
            amount = float(event["amount_per_share"])
        except (KeyError, TypeError, ValueError):
            continue
        total += amount
        dtype = event.get("dividend_type")
        if dtype == "regular":
            regular_total += amount
        elif dtype in _SPECIAL_TYPES:
            special_total += amount
            specials.append({"amount": round(amount, 4), "payment_date": event.get("payment_date")})

    as_of_day = _parse_day(as_of) if as_of is not None else None
    possible_suspension = bool(
        observed and series and as_of_day is not None
        and (as_of_day - series[-1][0]).days > 3 * median_gap
    )

    reinstatement = None
    if observed and len(series) >= 2:
        for (prev_day, _), (day, _) in zip(series[:-1], series[1:]):
            if (day - prev_day).days > 3 * median_gap:
                reinstatement = {"date": day.isoformat()}
                break

    return {
        **cadence,
        "increase": increase,
        "cut": cut,
        "freeze": freeze,
        "specials": specials,
        "total_paid_per_share": round(total, 4),
        "regular_paid_per_share": round(regular_total, 4),
        "special_paid_per_share": round(special_total, 4),
        "possible_suspension": possible_suspension,
        "reinstatement": reinstatement,
    }


def _regular_annual_totals(paid_events) -> dict[int, float]:
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


def analyze_dividends(*, paid_events, as_of=None, ttm_dps=None, growth=None,
                      annual_history=None) -> dict:
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
    if growth_1y is None or growth_5y is None:
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
