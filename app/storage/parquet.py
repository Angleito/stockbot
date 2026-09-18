"""Versioned normalized datasets for point-in-time records.

DuckDB tables are the canonical store (see ``app.storage.duckdb``); this
module owns the dataset schemas plus ``write_rows``/``read_table`` shims that
delegate to the warehouse. Appending, dedup, and point-in-time reads all run
inside DuckDB transactions, so re-running an ingestion job writes no
duplicates. Every record carries provenance: source, source record/URL,
retrieved time, period/effective date, ``known_at``, content hash, and parser
version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from ..config import get_data_root
DEFAULT_PARQUET_ROOT = get_data_root() / "parquet"

TEXT = pa.string()
DOUBLE = pa.float64()
INTEGER = pa.int64()


@dataclass(frozen=True)
class Dataset:
    name: str
    schema: pa.Schema
    unique_keys: tuple[str, ...]
    partition_field: str | None = None


def _fields(*pairs: tuple[str, pa.DataType]) -> list[pa.Field]:
    return [pa.field(name, type_) for name, type_ in pairs]


DATASETS: dict[str, Dataset] = {
    "entities": Dataset(
        name="entities",
        schema=pa.schema(
            _fields(
                ("entity_id", TEXT),
                ("name", TEXT),
                ("entity_type", TEXT),
                ("sic", TEXT),
                ("source", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
                ("cik", TEXT),
                ("accession", TEXT),
                ("document_name", TEXT),
                ("source_url", TEXT),
                ("raw_archive_path", TEXT),
            )
        ),
        unique_keys=("entity_id",),
    ),
    "entity_aliases": Dataset(
        name="entity_aliases",
        schema=pa.schema(
            _fields(
                ("alias_type", TEXT),
                ("alias_value", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("source", TEXT),
                ("valid_from", TEXT),
                ("valid_to", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
                ("cik", TEXT),
                ("accession", TEXT),
                ("source_url", TEXT),
            )
        ),
        unique_keys=("alias_type", "alias_value", "entity_id", "source", "valid_from"),
    ),
    "securities": Dataset(
        name="securities",
        schema=pa.schema(
            _fields(
                ("security_id", TEXT),
                ("entity_id", TEXT),
                ("security_type", TEXT),
                ("ticker", TEXT),
                ("exchange", TEXT),
                ("source", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
                ("cik", TEXT),
                ("accession", TEXT),
                ("source_url", TEXT),
                ("raw_archive_path", TEXT),
                ("cusip", TEXT),
                ("isin", TEXT),
                ("class_title", TEXT),
            )
        ),
        unique_keys=("security_id", "security_type", "known_at", "retrieved_at", "content_hash"),
    ),
    "documents": Dataset(
        name="documents",
        schema=pa.schema(
            _fields(
                ("doc_id", TEXT),
                ("source", TEXT),
                ("kind", TEXT),
                ("key", TEXT),
                ("source_url", TEXT),
                ("accession", TEXT),
                ("sha256", TEXT),
                ("retrieved_at", TEXT),
                ("published_at", TEXT),
                ("known_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
                ("document_name", TEXT),
                ("file_type", TEXT),
                ("file_description", TEXT),
                ("location", TEXT),
                ("filer_cik", TEXT),
                ("filer_name", TEXT),
                ("form", TEXT),
                ("filed_at", TEXT),
                ("accepted_at", TEXT),
                ("raw_archive_path", TEXT),
            )
        ),
        unique_keys=("doc_id",),
    ),
    "financial_facts": Dataset(
        name="financial_facts",
        schema=pa.schema(
            _fields(
                ("fact_id", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("concept", TEXT),
                ("original_concept", TEXT),
                ("value", DOUBLE),
                ("unit", TEXT),
                ("duration_type", TEXT),
                ("period_end", TEXT),
                ("period_start", TEXT),
                ("fiscal_year", INTEGER),
                ("fiscal_period", TEXT),
                ("filed_at", TEXT),
                ("accession", TEXT),
                ("frame", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("source_url", TEXT),
                ("source_record_id", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("fact_id",),
        partition_field="period_end",
    ),
    "short_interest": Dataset(
        name="short_interest",
        schema=pa.schema(
            _fields(
                ("row_id", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("symbol_code", TEXT),
                ("issue_name", TEXT),
                ("settlement_date", TEXT),
                ("short_position", DOUBLE),
                ("prev_position", DOUBLE),
                ("avg_daily_volume", DOUBLE),
                ("days_to_cover", DOUBLE),
                ("source_url", TEXT),
                ("source_record_id", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("row_id",),
        partition_field="settlement_date",
    ),
    "short_sale_volume": Dataset(
        name="short_sale_volume",
        schema=pa.schema(
            _fields(
                ("row_id", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("symbol_code", TEXT),
                ("trade_date", TEXT),
                ("facility", TEXT),
                ("short_volume", DOUBLE),
                ("exempt_volume", DOUBLE),
                ("total_volume", DOUBLE),
                ("source_url", TEXT),
                ("source_record_id", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("row_id",),
        partition_field="trade_date",
    ),
    "screen_runs": Dataset(
        name="screen_runs",
        schema=pa.schema(
            _fields(
                ("run_id", TEXT),
                ("screen", TEXT),
                ("settlement_date", TEXT),
                ("as_of", TEXT),
                ("created_at", TEXT),
                ("calc_version", TEXT),
                ("finra_rows", INTEGER),
                ("eligible_rows", INTEGER),
                ("valid_short_interest_rows", INTEGER),
                ("mapped_rows", INTEGER),
                ("unambiguous_rows", INTEGER),
                ("common_equity_rows", INTEGER),
                ("shares_outstanding_rows", INTEGER),
                ("exclusions_json", TEXT),
                ("environment", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("run_id",),
        partition_field="settlement_date",
    ),
    "screen_entries": Dataset(
        name="screen_entries",
        schema=pa.schema(
            _fields(
                ("run_id", TEXT),
                ("rank", INTEGER),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("ticker", TEXT),
                ("issue_name", TEXT),
                ("short_shares", DOUBLE),
                ("shares_outstanding", DOUBLE),
                ("short_interest_percent", DOUBLE),
                ("sec_shares_as_of", TEXT),
                ("sec_filed_at", TEXT),
                ("sec_accession", TEXT),
                ("sec_source_url", TEXT),
            )
        ),
        unique_keys=("run_id", "rank"),
    ),
    "ingestion_checkpoints": Dataset(
        name="ingestion_checkpoints",
        schema=pa.schema(
            _fields(
                ("pipeline", TEXT),
                ("source", TEXT),
                ("key", TEXT),
                ("payload_hash", TEXT),
                ("status", TEXT),
                ("record_count", INTEGER),
                ("started_at", TEXT),
                ("finished_at", TEXT),
                ("parser_version", TEXT),
                ("last_key", TEXT),
                ("error", TEXT),
                ("totals_json", TEXT),
            )
        ),
        unique_keys=("pipeline", "source", "key", "payload_hash", "status"),
    ),
    "portfolio_snapshots": Dataset(
        name="portfolio_snapshots",
        schema=pa.schema(
            _fields(
                ("snapshot_id", TEXT),
                ("broker", TEXT),
                ("created_at", TEXT),
                ("cash", pa.decimal128(38, 14)),
                ("invested_value", pa.decimal128(38, 14)),
                ("total_value", pa.decimal128(38, 14)),
                ("account_count", INTEGER),
                ("position_count", INTEGER),
                ("priced_position_count", INTEGER),
                ("unresolved_position_count", INTEGER),
                ("source", TEXT),
                ("parser_version", TEXT),
                ("calculation_version", TEXT),
            )
        ),
        unique_keys=("snapshot_id",),
    ),
    "portfolio_positions": Dataset(
        name="portfolio_positions",
        schema=pa.schema(
            _fields(
                ("snapshot_id", TEXT),
                ("position_id", TEXT),
                ("account_id", TEXT),
                ("security_id", TEXT),
                ("entity_id", TEXT),
                ("ticker", TEXT),
                ("quantity", pa.decimal128(38, 8)),
                ("average_cost", pa.decimal128(38, 6)),
                ("market_price", pa.decimal128(38, 6)),
                ("price_type", TEXT),
                ("market_value", pa.decimal128(38, 14)),
                ("unrealized_gain", pa.decimal128(38, 14)),
                ("unrealized_gain_pct", pa.decimal128(38, 28)),
                ("portfolio_weight", pa.decimal128(38, 28)),
                ("source", TEXT),
                ("quote_retrieved_at", TEXT),
                ("asset_type", TEXT),
            )
        ),
        unique_keys=("position_id",),
    ),
    "portfolio_accounts": Dataset(
        name="portfolio_accounts",
        schema=pa.schema(
            _fields(
                ("snapshot_id", TEXT),
                ("account_id", TEXT),
            )
        ),
        unique_keys=("snapshot_id", "account_id"),
    ),
    "sector_mappings": Dataset(
        name="sector_mappings",
        schema=pa.schema(
            _fields(
                ("entity_id", TEXT),
                ("sector", TEXT),
                ("source", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("entity_id", "sector", "source", "known_at"),
    ),
    "events": Dataset(
        name="events",
        schema=pa.schema(
            _fields(
                ("event_id", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("ticker", TEXT),
                ("event_type", TEXT),
                ("amount_billions", DOUBLE),
                ("certainty", TEXT),
                ("status", TEXT),
                ("revenue_matched", pa.bool_()),
                ("default_triggered", pa.bool_()),
                ("fiscal_year", TEXT),
                ("schedule_json", TEXT),
                ("payment_timing_json", TEXT),
                ("filed_at", TEXT),
                ("known_at", TEXT),
                ("retrieved_at", TEXT),
                ("accession", TEXT),
                ("source", TEXT),
                ("source_url", TEXT),
                ("content_hash", TEXT),
                ("parser_version", TEXT),
                ("agreement_key", TEXT),
                ("lifecycle_event", TEXT),
                ("schedule_component", pa.bool_()),
                ("headline_type", TEXT),
            )
        ),
        unique_keys=("event_id",),
        partition_field="filed_at",
    ),
    "evidence": Dataset(
        name="evidence",
        schema=pa.schema(
            _fields(
                ("evidence_id", TEXT),
                ("event_id", TEXT),
                ("source_type", TEXT),
                ("archive_key", TEXT),
                ("content_hash", TEXT),
                ("excerpt", TEXT),
                ("span_start", INTEGER),
                ("span_end", INTEGER),
                ("retrieved_at", TEXT),
                ("parser_version", TEXT),
            )
        ),
        unique_keys=("evidence_id",),
        partition_field="retrieved_at",
    ),
    "evidence_claims": Dataset(
        name="evidence_claims",
        schema=pa.schema(
            _fields(
                ("claim_id", TEXT),
                ("entity_id", TEXT),
                ("security_id", TEXT),
                ("ticker", TEXT),
                ("subject_name", TEXT),
                ("claim_type", TEXT),
                ("object_entity_id", TEXT),
                ("object_name", TEXT),
                ("text", TEXT),
                ("event_at", TEXT),
                ("published_at", TEXT),
                ("retrieved_at", TEXT),
                ("source_url", TEXT),
                ("source_domain", TEXT),
                ("publisher", TEXT),
                ("source_tier", TEXT),
                ("integrity", TEXT),
                ("evidence_summary", TEXT),
                ("confidence", TEXT),
                ("content_hash", TEXT),
                ("reported_ticker", TEXT),
                ("subject_resolution", TEXT),
                ("object_resolution", TEXT),
            )
        ),
        unique_keys=("claim_id",),
        partition_field="retrieved_at",
    ),
}
DATASETS["capital_events"] = Dataset(
    name="capital_events", schema=DATASETS["events"].schema, unique_keys=("event_id",), partition_field="filed_at"
)
DATASETS["dividend_events"] = Dataset(
    name="dividend_events",
    schema=pa.schema(
        _fields(
            ("dividend_event_id", TEXT),
            ("entity_id", TEXT),
            ("security_id", TEXT),
            ("ticker", TEXT),
            ("amount_per_share", DOUBLE),
            ("currency", TEXT),
            ("dividend_type", TEXT),
            ("declaration_date", TEXT),
            ("record_date", TEXT),
            ("payment_date", TEXT),
            ("ex_dividend_date", TEXT),
            ("ex_dividend_date_source", TEXT),
            ("status", TEXT),
            ("source_form", TEXT),
            ("accession", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("source_url", TEXT),
            ("source_concept", TEXT),
            ("source_type", TEXT),
            ("evidence_excerpt", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("dividend_event_id",),
    partition_field="filed_at",
)
DATASETS["sec_filings"] = Dataset(
    name="sec_filings",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("form", TEXT),
            ("cik", TEXT),
            ("company", TEXT),
            ("filer_cik", TEXT),
            ("filer_name", TEXT),
            ("subject_cik", TEXT),
            ("subject_name", TEXT),
            ("filed_at", TEXT),
            ("accepted_at", TEXT),
            ("known_at", TEXT),
            ("report_period", TEXT),
            ("primary_document", TEXT),
            ("is_amendment", pa.bool_()),
            ("amendment_of", TEXT),
            ("issuer_cik", TEXT),
            ("source_url", TEXT),
            ("raw_submission_path", TEXT),
            ("raw_primary_path", TEXT),
            ("retrieved_at", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
            ("document_name", TEXT),
            ("document_location", TEXT),
            ("raw_archive_path", TEXT),
        )
    ),
    unique_keys=("accession",),
    partition_field="filed_at",
)
DATASETS["filing_parties"] = Dataset(
    name="filing_parties",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("role", TEXT),
            ("entity_id", TEXT),
            ("cik", TEXT),
            ("name", TEXT),
            ("source", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("document_name", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("accession", "role", "entity_id", "cik", "name"),
    partition_field="known_at",
)
DATASETS["document_text"] = Dataset(
    name="document_text",
    schema=pa.schema(
        _fields(
            ("doc_id", TEXT),
            ("content_hash", TEXT),
            ("accession", TEXT),
            ("document_name", TEXT),
            ("text", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("source_content_hash", TEXT),
            ("source_representation", TEXT),
            ("location", TEXT),
            ("file_type", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("doc_id", "content_hash"),
    partition_field="filed_at",
)
DATASETS["sec_searches"] = Dataset(
    name="sec_searches",
    schema=pa.schema(
        _fields(
            ("search_id", TEXT),
            ("request_json", TEXT),
            ("coverage_status", TEXT),
            ("sources_attempted_json", TEXT),
            ("sources_completed_json", TEXT),
            ("sources_failed_json", TEXT),
            ("results_reported", INTEGER),
            ("results_retrieved", INTEGER),
            ("pages", INTEGER),
            ("date_coverage", TEXT),
            ("forms_covered_json", TEXT),
            ("pending_jobs_json", TEXT),
            ("warnings_json", TEXT),
            ("errors_json", TEXT),
            ("evidence_packet_ids_json", TEXT),
            ("dedup_counts_json", TEXT),
            # Retrieval truth: paging drained every route; the source itself had
            # no caps/limits left. Independent of the display packet bound.
            ("pagination_complete", pa.bool_()),
            ("source_exhausted", pa.bool_()),
            ("retrieved_at", TEXT),
            ("known_at", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("search_id",),
    partition_field="retrieved_at",
)
DATASETS["sec_search_attempts"] = Dataset(
    name="sec_search_attempts",
    schema=pa.schema(
        _fields(
            ("attempt_id", TEXT),
            ("search_id", TEXT),
            ("backend", TEXT),
            ("query", TEXT),
            ("filters_json", TEXT),
            ("status", TEXT),
            ("results_reported", INTEGER),
            ("results_retrieved", INTEGER),
            ("pages_retrieved", INTEGER),
            ("truncated", pa.bool_()),
            ("source_limit", TEXT),
            ("pit_basis", TEXT),
            ("error_type", TEXT),
            ("error_message", TEXT),
            ("started_at", TEXT),
            ("completed_at", TEXT),
            ("retrieved_at", TEXT),
        )
    ),
    unique_keys=("attempt_id",),
    partition_field="retrieved_at",
)
DATASETS["sec_text_hits"] = Dataset(
    name="sec_text_hits",
    schema=pa.schema(
        _fields(
            ("hit_id", TEXT),
            ("search_id", TEXT),
            ("attempt_id", TEXT),
            ("query", TEXT),
            ("accession", TEXT),
            ("filer_cik", TEXT),
            ("filer_name", TEXT),
            ("form", TEXT),
            ("filed_at", TEXT),
            ("matched_document", TEXT),
            ("file_type", TEXT),
            ("file_description", TEXT),
            ("items_json", TEXT),
            ("sic", TEXT),
            ("location", TEXT),
            ("state", TEXT),
            ("inc_state", TEXT),
            ("snippet", TEXT),
            ("score", DOUBLE),
            ("source_url", TEXT),
            ("page", INTEGER),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
            ("raw_archive_path", TEXT),
        )
    ),
    unique_keys=("search_id", "query", "accession", "matched_document"),
    partition_field="filed_at",
)
DATASETS["sec_ingestion_coverage"] = Dataset(
    name="sec_ingestion_coverage",
    schema=pa.schema(
        _fields(
            ("source", TEXT),
            ("form", TEXT),
            ("family", TEXT),
            ("date_partition", TEXT),
            ("coverage_date", TEXT),
            ("status", TEXT),
            ("accession_count", INTEGER),
            ("last_key", TEXT),
            ("parser_version", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
        )
    ),
    # ponytail: status in key so partial->complete upgrades append instead of deduping away
    unique_keys=("source", "form", "date_partition", "parser_version", "status"),
    partition_field="coverage_date",
)
DATASETS["sec_beneficial_ownership"] = Dataset(
    name="sec_beneficial_ownership",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("document_name", TEXT),
            ("subject_cik", TEXT),
            ("subject_name", TEXT),
            ("filer_cik", TEXT),
            ("filer_name", TEXT),
            ("reporter_name", TEXT),
            ("shares", DOUBLE),
            ("percent", DOUBLE),
            ("voting_power", TEXT),
            ("dispositive_power", TEXT),
            ("purpose", TEXT),
            ("form", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("accession", "subject_cik", "filer_cik", "reporter_name"),
    partition_field="filed_at",
)
DATASETS["sec_13f_holdings"] = Dataset(
    name="sec_13f_holdings",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("document_name", TEXT),
            ("manager_cik", TEXT),
            ("manager_name", TEXT),
            ("report_period", TEXT),
            ("issuer_name", TEXT),
            ("entity_id", TEXT),
            ("security_id", TEXT),
            ("class_title", TEXT),
            ("cusip", TEXT),
            ("isin", TEXT),
            ("shares", DOUBLE),
            ("value", DOUBLE),
            ("put_call", TEXT),
            ("discretion", TEXT),
            ("other_manager", TEXT),
            ("shares_prn_type", TEXT),
            ("source_row", INTEGER),
            ("holding_id", TEXT),
            ("voting", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("holding_id",),
    partition_field="filed_at",
)
DATASETS["sec_insider_transactions"] = Dataset(
    name="sec_insider_transactions",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("document_name", TEXT),
            ("form", TEXT),
            ("issuer_cik", TEXT),
            ("issuer_name", TEXT),
            ("owner_cik", TEXT),
            ("owner_name", TEXT),
            ("is_director", pa.bool_()),
            ("is_officer", pa.bool_()),
            ("is_ten_percent", pa.bool_()),
            ("is_other", pa.bool_()),
            ("role_title", TEXT),
            ("security_title", TEXT),
            ("transaction_code", TEXT),
            ("transaction_date", TEXT),
            ("shares", DOUBLE),
            ("price", DOUBLE),
            ("holdings", DOUBLE),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=(
        "accession",
        "owner_cik",
        "transaction_code",
        "transaction_date",
        "security_title",
        "shares",
        "price",
        "holdings",
        "content_hash",
    ),
    partition_field="filed_at",
)
DATASETS["sec_offerings"] = Dataset(
    name="sec_offerings",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("document_name", TEXT),
            ("form", TEXT),
            ("filer_cik", TEXT),
            ("filer_name", TEXT),
            ("registrant_cik", TEXT),
            ("registrant_name", TEXT),
            ("security_title", TEXT),
            ("amount", DOUBLE),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("accession", "document_name", "security_title"),
    partition_field="filed_at",
)
DATASETS["sec_transactions"] = Dataset(
    name="sec_transactions",
    schema=pa.schema(
        _fields(
            ("accession", TEXT),
            ("document_name", TEXT),
            ("form", TEXT),
            ("filer_cik", TEXT),
            ("filer_name", TEXT),
            ("subject_cik", TEXT),
            ("subject_name", TEXT),
            ("target_cik", TEXT),
            ("target_name", TEXT),
            ("acquirer_cik", TEXT),
            ("acquirer_name", TEXT),
            ("status", TEXT),
            ("filed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("accession", "form", "target_cik", "acquirer_cik"),
    partition_field="filed_at",
)
DATASETS["relationship_evidence"] = Dataset(
    name="relationship_evidence",
    schema=pa.schema(
        _fields(
            ("evidence_id", TEXT),
            ("relationship_id", TEXT),
            ("relationship_type", TEXT),
            ("from_entity_id", TEXT),
            ("to_entity_id", TEXT),
            ("accession", TEXT),
            ("document_name", TEXT),
            ("source_span", TEXT),
            ("extraction_method", TEXT),
            ("confidence", DOUBLE),
            ("is_counterevidence", pa.bool_()),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_url", TEXT),
            ("raw_archive_path", TEXT),
            ("content_hash", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("evidence_id",),
    partition_field="known_at",
)
DATASETS["relationship_revisions"] = Dataset(
    name="relationship_revisions",
    schema=pa.schema(
        _fields(
            ("revision_id", TEXT),
            ("relationship_id", TEXT),
            ("previous_status", TEXT),
            ("new_status", TEXT),
            ("actor", TEXT),
            ("reason", TEXT),
            ("recorded_at", TEXT),
            ("superseded_revision_id", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("revision_id",),
    partition_field="recorded_at",
)
DATASETS["relationship_type_evaluations"] = Dataset(
    name="relationship_type_evaluations",
    schema=pa.schema(
        _fields(
            ("evaluation_id", TEXT),
            ("relationship_type", TEXT),
            ("window_start", TEXT),
            ("window_end", TEXT),
            ("metrics_json", TEXT),
            ("decision", TEXT),
            ("inputs_hash", TEXT),
            ("prev_state", TEXT),
            ("new_state", TEXT),
            ("actor", TEXT),
            ("reason", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("parser_version", TEXT),
        )
    ),
    unique_keys=("evaluation_id",),
    partition_field="window_end",
)


DATASETS["google_observations"] = Dataset(
    name="google_observations",
    schema=pa.schema(
        _fields(
            ("observation_id", TEXT),
            ("source", TEXT),
            ("table", TEXT),
            ("term", TEXT),
            ("geo", TEXT),
            ("list_kind", TEXT),
            ("period", TEXT),
            ("observed_at", TEXT),
            ("known_at", TEXT),
            ("retrieved_at", TEXT),
            ("source_record_id", TEXT),
            ("content_hash", TEXT),
            ("collector_version", TEXT),
            ("calc_version", TEXT),
            ("metrics_json", TEXT),
            ("features_json", TEXT),
            ("evidence_json", TEXT),
            ("source_url", TEXT),
        )
    ),
    unique_keys=("observation_id", "content_hash"),
    partition_field="known_at",
)

DATASETS["google_signal_features"] = Dataset(
    name="google_signal_features",
    schema=pa.schema(
        _fields(
            ("observation_id", TEXT),
            ("feature_scope_hash", TEXT),
            ("feature_scope_json", TEXT),
            ("features_json", TEXT),
            ("calc_version", TEXT),
            ("calculated_at", TEXT),
            ("inputs_hash", TEXT),
        )
    ),
    unique_keys=("observation_id", "feature_scope_hash", "calc_version", "inputs_hash"),
    partition_field="calculated_at",
)


def dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown parquet dataset: {name}") from exc


def dataset_names() -> list[str]:
    return sorted(DATASETS)


def _duckdb_data_root(root: Path | None) -> Path | None:
    """A ``.../parquet`` root addresses the warehouse under its parent."""
    if root is None:
        return None
    resolved = Path(root)
    return resolved.parent if resolved.name == "parquet" else resolved


def read_table(name: str, root: Path | None = None, columns: list[str] | None = None) -> pa.Table:
    """Read a full dataset from the warehouse as a pyarrow Table.

    Timestamp columns come back exactly as written (date-only stays date-only,
    midnight instants keep their full instant); rows predating raw capture
    fall back to UTC ISO formatting. The dataset schema is otherwise
    preserved.
    """
    from . import duckdb as _duckdb

    ds = dataset(name)
    wanted = list(columns) if columns else [field.name for field in ds.schema]
    unknown = [column for column in wanted if column not in ds.schema.names]
    if unknown:
        raise ValueError(f"Unknown column for {name}: {unknown[0]}")

    def _ts_select(column: str) -> str:
        normalized = f'"{column}" AT TIME ZONE \'UTC\''
        return (
            f"CASE WHEN \"{column}\" IS NULL THEN NULL "
            f"WHEN date_trunc('day', \"{column}\") = \"{column}\" "
            f"THEN strftime({normalized}, '%Y-%m-%d') "
            f"WHEN date_trunc('second', \"{column}\") = \"{column}\" "
            f"THEN strftime({normalized}, '%Y-%m-%dT%H:%M:%SZ') "
            f"ELSE strftime({normalized}, '%Y-%m-%dT%H:%M:%S.%fZ') END AS \"{column}\""
        )

    select = ", ".join(
        _ts_select(column) if column in _duckdb.TIMESTAMP_COLS else f'"{column}"' for column in wanted
    )
    rows = _duckdb.query(
        f'SELECT {select}, "_tsraw" AS "_tsraw" FROM "{name}" ORDER BY rowid',
        data_root=_duckdb_data_root(root),
    )
    schema = ds.schema if columns is None else pa.schema([ds.schema.field(column) for column in wanted])
    if not rows:
        return schema.empty_table()
    for row in rows:
        raw_json = row.pop("_tsraw", None)
        if not raw_json or not isinstance(raw_json, str):
            continue
        try:
            raw = json.loads(raw_json)
        except ValueError:
            continue
        if isinstance(raw, dict):
            for column, value in raw.items():
                if column in row and isinstance(value, str):
                    row[column] = value
    return pa.Table.from_pylist(rows, schema=schema)


def write_rows(name: str, rows: list[dict[str, object]], root: Path | None = None) -> int:
    """Insert rows deduplicated by the dataset's unique key; returns the
    number of rows actually written (0 on a deterministic rerun)."""
    from . import duckdb as _duckdb

    dataset(name)
    return _duckdb.insert_ignore(name, rows, data_root=_duckdb_data_root(root))


def count_rows(name: str, root: Path | None = None) -> int:
    return read_table(name, root).num_rows
