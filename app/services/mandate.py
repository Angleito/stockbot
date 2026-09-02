"""Mandate file adapter: JSON mandate file → validated domain Mandate."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.risk.mandate import Mandate, parse_mandate


def load_mandate_file(path: Path) -> Mandate:
    """Load and validate a JSON mandate file.

    Raises ``ValueError`` with a clear message on any malformed or
    unsupported configuration; ``FileNotFoundError`` when the file is
    missing.  Unknown extra keys are ignored.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return parse_mandate(data)
