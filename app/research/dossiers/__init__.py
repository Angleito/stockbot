"""Canonical dossiers over frozen evidence."""

from .sec import (
    COVERAGE_KEYS,
    DossierIntegrityError,
    SECDossier,
    create_dossier,
    default_coverage,
    validate_dossier,
)

__all__ = [
    "COVERAGE_KEYS",
    "DossierIntegrityError",
    "SECDossier",
    "create_dossier",
    "default_coverage",
    "validate_dossier",
]
