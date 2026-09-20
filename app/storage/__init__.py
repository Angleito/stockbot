"""Ops storage: immutable raw archive + run ledger + id helpers.

Live reads come from providers through ``app.data_sources``; the
portfolio snapshot store lives in ``app.services.portfolio_sync``
(``portfolio.sqlite``). Nothing here is an analytical warehouse.
"""
