"""Builds the run's model message list behind the context gateway."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..storage.runs import get_current_recorder
from ..tool_render import render_tool_result
from . import quarantine_reader
from .context import RunSecurityContext, Sensitivity
from .context_gateway import (
    QuarantinedContext,
    envelope_for_tool,
    prepare_context,
)


class ContextBuilder:
    """Assembles the run's messages; every tool result passes the gateway.

    System/user/assistant messages bypass scanning (they were normalized by
    the chat policy). Tool results are labeled, scanned, and silently
    omitted from model context when quarantined or blocked; each decision is
    recorded as a security event against the active recorder (if any).
    """

    def __init__(self, run_security: RunSecurityContext, model: str, data_root: Path | None = None) -> None:
        self.run_security = run_security
        self.model = model
        self._data_root = data_root
        self.messages: list[dict] = []
        # The exact text of the most recently appended tool message (used by
        # the agent loop for evidence accounting).
        self.last_appended_text: str | None = None

    def add_system(self, text: str) -> None:
        self.messages.append({"role": "system", "content": text})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, message: str | dict) -> None:
        """Append an assistant message: a content string or a raw message
        dict (the loop appends model messages that carry tool_calls)."""
        if isinstance(message, str):
            self.messages.append({"role": "assistant", "content": message})
        else:
            self.messages.append(dict(message))

    def add_tool_result(
        self, name: str, result: dict, rendered: str, tool_call_id: str = ""
    ) -> bool:
        """Label and scan one tool result; append it only when allowed.

        Returns True when the rendered text entered model context. Web
        search results are first converted to structured claims by the
        quarantined reader.
        """
        envelope = envelope_for_tool(name, result)
        _enriched_evidence: list[dict] | None = None
        if name == "search_web":
            transformed = quarantine_reader.process_web_evidence(self.model, result)
            if transformed.get("claims_processed") and isinstance(transformed.get("evidence"), list) and transformed["evidence"]:
                from ..services.evidence_claims import build_evidence_claims, claim_to_enriched_dict
                from ..storage import parquet

                _now = datetime.now(timezone.utc)
                _fallback = transformed.get("retrieved_at") or result.get("retrieved_at") or _now.isoformat()
                _reader_items = transformed["evidence"]
                try:
                    _claims = build_evidence_claims(
                        reader_items=_reader_items,
                        as_of=_now,
                        data_root=self._data_root,
                        retrieved_fallback=_fallback,
                    )
                    transformed["evidence"] = [claim_to_enriched_dict(c) for c in _claims]
                    _enriched_evidence = transformed["evidence"]
                except Exception as exc:
                    self._record(
                        source="exa",
                        sha256=hashlib.sha256(f"{type(exc).__name__}:{len(_reader_items)}".encode()).hexdigest(),
                        score=None,
                        verdict=None,
                        rule_ids=["exa", "ontology", "enrichment_failed"],
                        decision="ontology_enrichment_failed",
                        reason=f"{type(exc).__name__} enriching {len(_reader_items)} items",
                    )
            envelope = replace(envelope, content=transformed)
            rendered = render_tool_result(transformed)
        outcome = prepare_context(envelope, rendered)
        if isinstance(outcome, QuarantinedContext):
            self.run_security.quarantined_items += 1
            self._record(
                source=envelope.source,
                sha256=hashlib.sha256(rendered.encode()).hexdigest(),
                score=outcome.score,
                verdict=outcome.verdict,
                rule_ids=list(outcome.rule_ids),
                decision=(
                    "quarantined" if outcome.verdict == "QUARANTINE" else "blocked"
                ),
                reason="; ".join(outcome.reasons) if outcome.reasons else None,
            )
            # Keep the tool-call protocol intact: the model's tool_call_id
            # gets a fixed placeholder response instead of being silently
            # dropped. last_appended_text stays untouched so no evidence row
            # is created for the withheld content.
            self.messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": (
                    "Tool result withheld by Stockbot security gateway. "
                    "No usable evidence was provided."
                ),
            })
            return False
        if _enriched_evidence:
            try:
                parquet.write_rows("evidence_claims", _enriched_evidence, root=self._data_root / "parquet" if self._data_root is not None else None)
            except Exception as exc:
                self._record(
                    source="exa",
                    sha256=hashlib.sha256(f"{type(exc).__name__}:{len(_enriched_evidence)}".encode()).hexdigest(),
                    score=None,
                    verdict=None,
                    rule_ids=["exa", "ontology", "enrichment_failed"],
                    decision="ontology_persist_failed",
                    reason=f"{type(exc).__name__} persisting {len(_enriched_evidence)} items",
                )
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": outcome.text}
        )
        self.last_appended_text = outcome.text
        if name == "search_web":
            self.run_security.data_labels.add("external")
        if envelope.sensitivity is Sensitivity.PRIVATE:
            self.run_security.data_labels.add("private")
        self._record(
            source=envelope.source,
            sha256=hashlib.sha256(outcome.text.encode()).hexdigest(),
            score=None,
            verdict=None,
            rule_ids=[
                envelope.source,
                envelope.sensitivity.value,
                envelope.integrity.value,
            ],
            decision="allowed",
            reason=None,
        )
        return True

    def _record(
        self,
        *,
        source: str,
        sha256: str,
        score: int | None,
        verdict: str | None,
        rule_ids: list[str],
        decision: str,
        reason: str | None,
    ) -> None:
        event = {
            "source": source,
            "sha256": sha256,
            "score": score,
            "verdict": verdict,
            "rule_ids": rule_ids,
            "decision": decision,
            "reason": reason,
        }
        self.run_security.security_events.append(event)
        recorder = get_current_recorder()
        if recorder is not None:
            recorder.record_security_event(
                source=source,
                sha256=sha256,
                score=score,
                verdict=verdict,
                rule_ids=rule_ids,
                decision=decision,
                reason=reason,
            )

    def render_for_model(self) -> list[dict]:
        return self.messages
