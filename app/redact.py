"""Redaction of credentials and secrets in observability records.

Position/security identifiers (``position_id``, ``provider_instrument_id``,
accession numbers, content hashes) are NOT redacted: they are structural
research data, not credentials.
"""

from __future__ import annotations

import json
import re

# Normalized (lowercase, punctuation-stripped) dict keys whose values are
# always replaced with "[REDACTED]" — e.g. account_number, accountNumber,
# access token all normalize to the same norm.
SENSITIVE_KEY_NORMS = frozenset({
    "accesstoken",
    "refreshtoken",
    "token",
    "authorization",
    "auth",
    "clientsecret",
    "clientid",
    "secret",
    "password",
    "apikey",
    "cookie",
    "crumb",
    "accountnumber",
    "accountid",
})

_REDACTED = "[REDACTED]"

# Applied in order in redact_text.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SK_OR_V1_RE = re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _norm_key(key: object) -> str:
    """Normalize a dict key for sensitive-name matching (case/punctuation-insensitive)."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def redact_value(value: object) -> object:
    """Recursively redact sensitive values; structural research data passes through.

    Dict keys matching SENSITIVE_KEY_NORMS have their values replaced with
    "[REDACTED]" (keys are never rewritten). Lists/tuples are redacted
    item-wise (as a list); strings go through redact_text; other scalars
    (int/float/bool/None) pass through unchanged.
    """
    if isinstance(value, dict):
        return {
            key: _REDACTED if _norm_key(key) in SENSITIVE_KEY_NORMS else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Mask bearer tokens, sk-or-v1 API keys, and JWTs in free text."""
    if not isinstance(text, str):
        text = str(text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SK_OR_V1_RE.sub("sk-or-v1-[REDACTED]", text)
    text = _JWT_RE.sub("eyJ[REDACTED JWT]", text)
    return text


def redact_json(text: str) -> str:
    """Redact a JSON document in place; fall back to text redaction if unparseable."""
    try:
        return json.dumps(redact_value(json.loads(text)))
    except (TypeError, ValueError):
        return redact_text(text)
