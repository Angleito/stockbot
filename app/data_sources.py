"""Live provider seam: thin per-run gateway over authoritative providers.

Providers (EdgarTools/SEC, FINRA) are authoritative. This facade delegates
to the existing provider modules, standardizes point-in-time/provenance,
and caches fetch-once-per-run in memory only (never persisted). No
business logic lives here; resolution, assembly, and screening stay with
callers and their existing helpers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from . import finra_client as _finra
from . import normalization as _norm
from .domain.market.securities import TickerAlias
from .sec import client as _sec
from .sec import filings as _filings
from .sec.models import Filing, SECSearchResult, pit_of

_SHORT_FIELDS = (
    "symbolCode",
    "issueName",
    "settlementDate",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
)

_EMPTY_FACTS: dict[str, object] = {
    "documents": [],
    "financial_facts": [],
    "securities": [],
    "dividend_events": [],
}


def _known_as_of(row: object, as_of: str) -> bool:
    """True when a normalized row's PIT instant is knowable on ``as_of``."""
    value, _basis = pit_of(row)
    return value is not None and value[:10] <= as_of


class SourceGateway:
    def __init__(self):
        self._cache: dict[tuple[object, ...], object] = {}

    def company_facts(self, cik: int, *, as_of: str | None = None) -> dict[str, object]:
        """Normalized companyfacts for one CIK, PIT-filtered to ``as_of``."""
        cik_int = int(cik)
        key = ("company_facts", cik_int)
        cached: object = self._cache.get(key)
        if cached is None:
            _sec.ensure_identity()
            from edgar.entity.entity_facts import download_company_facts_from_sec

            try:
                raw = download_company_facts_from_sec(cik_int)
            except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                raw = None
            if not isinstance(raw, dict):
                cached = dict(_EMPTY_FACTS)
            else:
                retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                from edgar.urls import build_company_facts_url

                source_url = build_company_facts_url(cik_int)
                record_id = f"cik{cik_int:010d}"
                content_hash = hashlib.sha256(
                    json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
                ).hexdigest()
                normalized: dict[str, list[dict[str, object]]] = _norm.normalize_sec_company_facts(
                    raw,
                    retrieved_at=retrieved_at,
                    content_hash=content_hash,
                    source_url=source_url,
                    source_record_id=record_id,
                )
                cached = {name: list(rows) for name, rows in normalized.items()}
            self._cache[key] = cached
        assert isinstance(cached, dict)
        if as_of is None:
            return dict(cached)
        out = dict(cached)
        for _name, rows in cached.items():
            if isinstance(rows, list):
                out[_name] = [row for row in rows if _known_as_of(row, as_of)]
        return out

    def search_filings(
        self,
        *,
        query: str,
        forms: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        as_of: str | None = None,
    ) -> SECSearchResult:
        """EDGAR full-text search; provider packet returned unchanged."""
        return _sec.search_sec_filings(
            query,
            forms=forms,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            as_of=as_of,
        )

    def get_filing(self, accession: str, *, as_of: str | None = None) -> Filing:
        """Exact accession lookup; PIT enforced per call over the cached filing."""
        key = ("filing", str(accession))
        cached: object = self._cache.get(key)
        if not isinstance(cached, Filing):
            cached = _filings.get_sec_filing(accession)
            self._cache[key] = cached
        if as_of is not None:
            value, _basis = pit_of(cached)
            if value is None or value[:10] > as_of:
                raise ValueError(f"filing {accession!r} not known as of {as_of!r}")
        return cached

    def short_interest(self, symbol: str, *, as_of: str | None = None) -> list[dict[str, object]]:
        """Normalized short-interest rows, newest first; ``as_of`` gates knowledge time (``known_at``)."""
        sym = str(symbol).strip().upper()
        key = ("short_interest", sym)
        cached: object = self._cache.get(key)
        if cached is None:
            result = _finra.get_finra_datapoints(
                "otcMarket/consolidatedShortInterest",
                fields=list(_SHORT_FIELDS),
                ticker=sym,
                sort_fields=["-settlementDate"],
                limit=25,
            )
            records = result.get("records") if isinstance(result, dict) else None
            if not isinstance(records, list):
                cached_list: list[dict[str, object]] = []
                cached = cached_list
            else:
                cached = self._normalized_short_rows([r for r in records if isinstance(r, dict)])
            self._cache[key] = cached
        assert isinstance(cached, list)
        if as_of is None:
            return list(cached)
        return [row for row in cached if isinstance(row, dict) and _known_as_of(row, as_of)]

    def _normalized_short_rows(self, records: list[dict]) -> list[dict[str, object]]:
        """Exact FINRA records grouped by settlement date through the normalizer."""
        from .config import finra_use_mock

        name = "consolidatedShortInterest" + ("Mock" if finra_use_mock() else "")
        url = f"{_finra.FINRA_API_BASE}/data/group/otcMarket/name/{name}"
        retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        grouped: dict[str, list[dict]] = {}
        for record in records:
            day = str(record.get("settlementDate") or "")
            if day:
                grouped.setdefault(day, []).append(record)
        out: list[dict[str, object]] = []
        for day in sorted(grouped, reverse=True):
            group = grouped[day]
            content_hash = hashlib.sha256(
                json.dumps(group, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            normalized = _norm.normalize_finra_short_interest(
                group,
                settlement_date=day,
                retrieved_at=retrieved_at,
                content_hash=content_hash,
                source_url=url,
                source_record_id=f"otcMarket/consolidatedShortInterest:{day}",
            )
            short_rows = normalized.get("short_interest")
            if isinstance(short_rows, list):
                out.extend(short_rows)
        return out

    def ticker_candidates(self, ticker: str, as_of: datetime) -> list[TickerAlias]:
        """Live company-tickers aliases for one ticker (current knowledge only).

        Rows are now-stamped (``known_at == retrieved_at``), so a historical
        ``as_of`` view resolves ``unresolved`` in ``resolve_ticker_aliases``;
        intended, never papered over. PIT stays with the resolver (the caller).
        """
        want = str(ticker).strip().upper()
        key = ("company_tickers",)
        cached: object = self._cache.get(key)
        if cached is None:
            _sec.ensure_identity()
            from edgar.httprequests import get_with_retry, inspect_response
            from edgar.urls import build_company_tickers_url

            url = build_company_tickers_url()
            resp = get_with_retry(url)
            inspect_response(resp)
            payload = bytes(resp.content)
            retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            content_hash = hashlib.sha256(payload).hexdigest()
            raw = json.loads(payload)
            datasets = _norm.normalize_sec_tickers(
                raw, retrieved_at=retrieved_at, content_hash=content_hash
            )
            alias_rows = datasets.get("entity_aliases")
            built: list[TickerAlias] = []
            if isinstance(alias_rows, list):
                for row in alias_rows:
                    if not isinstance(row, dict):
                        continue
                    built.append(
                        TickerAlias(
                            alias_type=str(row.get("alias_type")),
                            alias_value=str(row.get("alias_value")),
                            entity_id=str(row.get("entity_id")),
                            security_id=str(row.get("security_id"))
                            if row.get("security_id")
                            else None,
                            source=str(row.get("source")),
                            valid_from=str(row.get("valid_from"))
                            if row.get("valid_from")
                            else None,
                            valid_to=str(row.get("valid_to")) if row.get("valid_to") else None,
                            known_at=str(row.get("known_at")) if row.get("known_at") else None,
                            retrieved_at=str(row.get("retrieved_at"))
                            if row.get("retrieved_at")
                            else None,
                        )
                    )
            cached = built
            self._cache[key] = cached
        assert isinstance(cached, list)
        return [alias for alias in cached if alias.alias_value == want]
