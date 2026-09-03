"""Response-side DLP: strip flagged spans from final answers. stdlib only.

Leaked spans (credentials, .env paths, account identifiers) are removed
before the answer is returned and recorded; each strip is logged locally as
a response_stripped security event carrying only the span's sha256 and
length — never the leaked content itself."""

from __future__ import annotations

import hashlib
import re

from ..redact import _ACCOUNT_ID_RE, _BEARER_RE, _JWT_RE, _SK_OR_V1_RE
from ..storage.runs import get_current_recorder
from .context import RunSecurityContext

_ENV_PATH_RE = re.compile(r"(?:^|\s)\.env(?:\b|/)")

# Always stripped: credentials and secret paths.
_ALWAYS_STRIP = (
    ("bearer", _BEARER_RE),
    ("sk_or_v1", _SK_OR_V1_RE),
    ("jwt", _JWT_RE),
    ("env_path", _ENV_PATH_RE),
)

FALLBACK_RESPONSE = "I couldn't generate a response that meets safety checks."


def _find_spans(text: str, run_security: RunSecurityContext) -> list[tuple[str, str]]:
    spans = [
        (name, match.group(0))
        for name, pattern in _ALWAYS_STRIP
        for match in pattern.finditer(text)
    ]
    if not run_security.authorization.portfolio_read:
        # Defense in depth: account identifiers are portfolio content, only
        # legitimate with an explicit session grant.
        spans.extend(
            ("account_id", match.group(0))
            for match in _ACCOUNT_ID_RE.finditer(text)
        )
    return spans


def guard_response(text: str, run_security: RunSecurityContext, run_id: str) -> str:
    """Strip flagged spans from a final answer; record each strip locally."""
    if not isinstance(text, str) or not text.strip():
        return text
    spans = _find_spans(text, run_security)
    if not spans:
        return text
    cleaned = text
    for _name, span in spans:
        cleaned = cleaned.replace(span, "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    if not cleaned.strip():
        cleaned = FALLBACK_RESPONSE
    recorder = get_current_recorder()
    if recorder is not None:
        for name, span in spans:
            recorder.record_security_event(
                source="response",
                sha256=hashlib.sha256(span.encode()).hexdigest(),
                score=None,
                verdict=None,
                rule_ids=[name],
                decision="response_stripped",
                reason=None,
                span_length=len(span),
            )
    return cleaned
