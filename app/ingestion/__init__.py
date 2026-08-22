"""Connector -> raw archive -> normalizer -> Parquet ingestion pipelines.

Each pipeline is deterministic: identical source payloads archive once,
normalize once, and checkpoint once.  See app/ingestion/base.py for the
shared contract.
"""