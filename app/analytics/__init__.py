"""Analytics: deterministic point-in-time screens and derived features.

Screens consume only normalized data through the DuckDB layer and enforce
``known_at <= as_of`` on every historical join (see app/analytics/screens.py).
"""