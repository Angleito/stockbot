"""Evaluator-only structured quality judge (first live-eval slice).

Uses an OpenAI-compatible ``/chat/completions`` endpoint over stdlib
``urllib`` only. The judge sees question/as_of, accepted evidence and
source metadata, known limitations, the final answer, and the rubric --
never hidden worker reasoning (nodes, tool executions, sessions, jobs,
model/prompt metadata). Three temperature-zero calls by default; the
median rating per dimension wins.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

FIRST_SLICE_DIMENSIONS: tuple[str, ...] = (
    "factual_correctness",
    "evidence_entailment",
    "task_coverage",
    "causal_reasoning",
    "uncertainty",
    "question_fidelity",
    "decision_usefulness",
)

_DIMENSION_RUBRIC: dict[str, str] = {
    "factual_correctness": "claims are supported by accepted evidence; no invented facts or numbers.",
    "evidence_entailment": "material claims follow from cited evidence text rather than stretching it.",
    "task_coverage": "the answer addresses the question asked, including explicit subtasks where given.",
    "causal_reasoning": "causal links state a valid mechanism with proportionate confidence.",
    "uncertainty": "limits, unknowns, and confidence are stated honestly; no overclaiming.",
    "question_fidelity": "no scope drift: answers this question, not a nearby easier one.",
    "decision_usefulness": "a reader can act on the conclusion given the evidence and limits.",
}

JUDGE_BASE_URL_ENV = "STOCKBOT_EVAL_JUDGE_BASE_URL"
JUDGE_API_KEY_ENV = "STOCKBOT_EVAL_JUDGE_API_KEY"
JUDGE_MODEL_ENV = "STOCKBOT_EVAL_JUDGE_MODEL"

_TIMEOUT_S = 60.0
_EVIDENCE_CHARS = 2000

# Raw model content: either inline JSON text or an already-decoded object.
JudgeContent = str | dict[str, object]
# Injected offline stand-in: (system_prompt, user_prompt) -> raw content.
JudgeClientFn = Callable[[str, str], JudgeContent]


class JudgeError(RuntimeError):
    """Any judge failure: misconfiguration, transport, or malformed output."""


@dataclass(frozen=True)
class MaterialIssue:
    type: str
    description: str
    claim: str

    def as_dict(self) -> dict[str, object]:
        return {"type": self.type, "description": self.description, "claim": self.claim}


@dataclass(frozen=True)
class JudgeRun:
    scores: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    issues: tuple[MaterialIssue, ...] = ()
    raw: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "scores": dict(self.scores),
            "reasons": dict(self.reasons),
            "material_issues": [i.as_dict() for i in self.issues],
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class JudgeAggregate:
    case_id: str
    scores: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    issues: tuple[MaterialIssue, ...] = ()
    runs: tuple[JudgeRun, ...] = ()
    disagreement: float = 0.0
    model: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "scores": dict(self.scores),
            "reasons": dict(self.reasons),
            "material_issues": [i.as_dict() for i in self.issues],
            "disagreement": self.disagreement,
            "runs": [r.as_dict() for r in self.runs],
        }


def _judge_url(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _envelope_content(envelope: object) -> JudgeContent:
    if not isinstance(envelope, dict):
        raise JudgeError("judge returned unexpected envelope shape")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise JudgeError("judge returned unexpected envelope shape")
    first = choices[0]
    if not isinstance(first, dict):
        raise JudgeError("judge returned unexpected envelope shape")
    message = first.get("message")
    if not isinstance(message, dict):
        raise JudgeError("judge returned unexpected envelope shape")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        out: dict[str, object] = {}
        for k, v in content.items():
            if isinstance(k, str):
                out[k] = v
        return out
    raise JudgeError("judge returned unexpected envelope shape")


def _http_complete(system: str, user: str) -> JudgeContent:
    """Default client: POST env-configured model at temperature 0, JSON mode."""
    base = os.environ.get(JUDGE_BASE_URL_ENV, "").strip()
    model = os.environ.get(JUDGE_MODEL_ENV, "").strip()
    if not base:
        raise JudgeError(f"judge misconfigured: {JUDGE_BASE_URL_ENV} is not set")
    if not model:
        raise JudgeError(f"judge misconfigured: {JUDGE_MODEL_ENV} is not set")
    key = os.environ.get(JUDGE_API_KEY_ENV, "").strip()
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key  # never logged or echoed
    req = urllib.request.Request(_judge_url(base), data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - transport boundary; type only, never key material
        raise JudgeError(f"judge transport failed: {type(exc).__name__}") from exc
    try:
        envelope: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge returned invalid JSON envelope: {exc}") from exc
    return _envelope_content(envelope)


def _narrow_content(value: object) -> JudgeContent:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if isinstance(k, str):
                out[k] = v
        return out
    raise JudgeError(f"judge client returned unsupported content type: {type(value).__name__}")


def _coerce_client(client: object) -> JudgeClientFn:
    if client is None:
        return _http_complete
    target = getattr(client, "complete", None)
    if target is None and callable(client):
        target = client
    if not callable(target):
        raise JudgeError("judge client must be callable or expose complete(system, user)")

    def _call(system: str, user: str) -> JudgeContent:
        return _narrow_content(target(system, user))

    return _call


def _case_get(case: object, name: str) -> object:
    if isinstance(case, Mapping):
        return case.get(name)
    return getattr(case, name, None)


def _evidence_items(trace: Mapping[str, object]) -> list[dict[str, str]]:
    raw: list[object] = []
    for key in ("accepted_evidence", "acceptedEvidence", "evidenceRecords", "evidence_records", "evidence"):
        val = trace.get(key)
        if isinstance(val, list):
            raw = val
            break
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        eid = ""
        for ikey in ("id", "evidence_id", "evidenceId"):
            ival = entry.get(ikey)
            if isinstance(ival, str) and ival.strip():
                eid = ival.strip()
                break
        source = ""
        for skey in ("source", "source_name"):
            sval = entry.get(skey)
            if isinstance(sval, str) and sval.strip():
                source = sval.strip()
                break
        if not source:
            prov = entry.get("provenance")
            if isinstance(prov, str) and prov.strip():
                source = prov.strip()
            elif isinstance(prov, Mapping):
                for pkey in ("source", "source_name", "domain"):
                    pval = prov.get(pkey)
                    if isinstance(pval, str) and pval.strip():
                        source = pval.strip()
                        break
        content = ""
        for ckey in ("content", "text", "passage", "fact", "body"):
            cval = entry.get(ckey)
            if isinstance(cval, str) and cval.strip():
                content = cval
                break
        title = entry.get("title", "")
        items.append(
            {
                "id": eid,
                "source": source,
                "title": title if isinstance(title, str) else "",
                "content": content,
            }
        )
    return items


def _limitations(trace: Mapping[str, object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: object) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        elif isinstance(value, Mapping):
            for key in ("text", "description", "limitation"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    _add(item)
                    break
        elif isinstance(value, list):
            for item in value:
                _add(item)

    for key in ("known_limitations", "limitations"):
        raw = trace.get(key)
        if isinstance(raw, list) and raw:
            for item in raw:
                _add(item)
            if out:
                return out
    for key in ("dossiers", "claims", "coverageArtifacts", "coverage_artifacts"):
        val = trace.get(key)
        if isinstance(val, list):
            for entry in val:
                if isinstance(entry, Mapping):
                    for lkey in ("limitations", "limitation", "source_limitations", "gaps"):
                        _add(entry.get(lkey))
                    coverage = entry.get("coverage")
                    if isinstance(coverage, Mapping):
                        for ckey in ("source_limitations", "gaps", "limitations"):
                            _add(coverage.get(ckey))
    if out:
        return out
    for key in (
        "unresolved",
        "unresolved_questions",
        "unresolvedQuestions",
        "open_questions",
        "openQuestions",
        "unknowns",
    ):
        val = trace.get(key)
        if isinstance(val, list):
            for item in val:
                _add(item)
    return out


def _build_prompts(
    question: str,
    as_of: object,
    evidence: list[dict[str, str]],
    limitations: list[str],
    answer: str,
    dimensions: list[str],
) -> tuple[str, str]:
    rubric = "\n".join(f"- {d}: {_DIMENSION_RUBRIC[d]}" for d in dimensions)
    system = (
        "You are an evaluator-only judge of investment-research answers. "
        f"Score ONLY these {len(dimensions)} dimensions: {', '.join(dimensions)}. "
        "Scale: 0 = absent/failed; 1 = weak; 2 = adequate; 3 = strong; 4 = exemplary. "
        "Use only the question, as-of date, accepted evidence, known limitations, "
        "and final answer below. Do not use outside knowledge. "
        "Respond with EXACTLY one JSON object and nothing else (no prose, no markdown fences): "
        '{"scores": {dimension: 0-4 integer}, '
        '"reasons": {dimension: one concise sentence}, '
        '"material_issues": [{"type": short slug, "description": one sentence, '
        '"claim": quoted or paraphrased claim}]}. '
        f"Rubric:\n{rubric}"
    )
    lines = [f"Question: {question}", f"As of: {as_of if as_of else 'none'}"]
    lines.append(f"Accepted evidence ({len(evidence)} items):")
    for i, item in enumerate(evidence, 1):
        content = item["content"]
        if len(content) > _EVIDENCE_CHARS:
            content = content[:_EVIDENCE_CHARS] + " [truncated]"
        lines.append(f"[{i}] id={item['id']} source={item['source']}")
        if item["title"]:
            lines.append(f"Title: {item['title']}")
        lines.append(content)
    lines.append("Known limitations:")
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- none stated")
    lines.append("Final answer:")
    lines.append(answer)
    return system, "\n".join(lines)


def _parse_content(raw: JudgeContent) -> dict[str, object]:
    data: object
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise JudgeError("judge returned empty content")
        if "```" in text:
            raise JudgeError("judge returned markdown-fenced content instead of raw JSON")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"judge returned invalid JSON: {exc}") from exc
    else:
        raise JudgeError(f"judge returned unsupported content type: {type(raw).__name__}")
    if not isinstance(data, dict):
        raise JudgeError("judge top-level JSON must be an object")
    clean: dict[str, object] = {}
    for k, v in data.items():
        if isinstance(k, str):
            clean[k] = v
    return clean


def _validate_issue(item: object) -> MaterialIssue:
    if not isinstance(item, Mapping):
        raise JudgeError("judge material issue must be an object")
    values: dict[str, str] = {}
    for key in ("type", "description", "claim"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise JudgeError(f"judge material issue needs nonempty string {key!r}")
        values[key] = value.strip()
    return MaterialIssue(type=values["type"], description=values["description"], claim=values["claim"])


def _validate_run(data: dict[str, object], dimensions: list[str]) -> JudgeRun:
    extra_keys = sorted(set(data) - {"scores", "reasons", "material_issues"})
    if extra_keys:
        raise JudgeError(f"judge returned unexpected top-level keys: {extra_keys}")
    scores = data.get("scores")
    reasons = data.get("reasons")
    if not isinstance(scores, dict) or not isinstance(reasons, dict):
        raise JudgeError("judge JSON needs 'scores' and 'reasons' objects")
    for name, mapping in (("scores", scores), ("reasons", reasons)):
        keys = [k for k in mapping if isinstance(k, str)]
        missing = sorted(set(dimensions) - set(keys))
        extra = sorted(set(keys) - set(dimensions))
        if missing or extra:
            raise JudgeError(f"judge {name} dimensions mismatch: missing={missing} extra={extra}")
    clean_scores: dict[str, int] = {}
    for dim in dimensions:
        score = scores[dim]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 4:
            raise JudgeError(f"judge score for {dim!r} must be an integer 0..4")
        clean_scores[dim] = score
    clean_reasons: dict[str, str] = {}
    for dim in dimensions:
        reason = reasons[dim]
        if not isinstance(reason, str) or not reason.strip():
            raise JudgeError(f"judge reason for {dim!r} must be a nonempty string")
        clean_reasons[dim] = reason.strip()
    raw_issues: object = data.get("material_issues", [])
    if not isinstance(raw_issues, list):
        raise JudgeError("judge 'material_issues' must be a list")
    return JudgeRun(
        scores=clean_scores,
        reasons=clean_reasons,
        issues=tuple(_validate_issue(item) for item in raw_issues),
        raw=dict(data),
    )


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def judge_answer(
    case: object,
    answer: str,
    trace: Mapping[str, object] | None = None,
    client: object = None,
    runs: int = 3,
) -> JudgeAggregate:
    """Judge one answer; median per dimension over ``runs`` independent calls."""
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ValueError("runs must be a positive int")
    case_id = _case_get(case, "id")
    question = _case_get(case, "question")
    as_of = _case_get(case, "as_of")
    raw_dims = _case_get(case, "applicable_dimensions")
    if not isinstance(case_id, str) or not case_id:
        raise JudgeError("case needs a nonempty string id")
    if not isinstance(question, str) or not question.strip():
        raise JudgeError("case needs a nonempty string question")
    dimensions: list[str] = list(raw_dims) if isinstance(raw_dims, (list, tuple)) else []
    if not dimensions or any(d not in _DIMENSION_RUBRIC for d in dimensions):
        raise JudgeError(f"case applicable_dimensions must be a nonempty subset of {sorted(_DIMENSION_RUBRIC)}")
    if not isinstance(answer, str):
        raise JudgeError("answer must be a string")
    trace_map: Mapping[str, object] = trace if isinstance(trace, Mapping) else {}
    complete = _coerce_client(client)
    system, user = _build_prompts(
        question.strip(), as_of, _evidence_items(trace_map), _limitations(trace_map), answer, dimensions
    )
    validated: list[JudgeRun] = []
    for _ in range(runs):
        try:
            raw = complete(system, user)
        except JudgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - injected-client boundary; type only, never key material
            raise JudgeError(f"judge client failed: {type(exc).__name__}") from exc
        validated.append(_validate_run(_parse_content(raw), dimensions))
    scores = {d: _median([r.scores[d] for r in validated]) for d in dimensions}
    reasons = {d: next(r.reasons[d] for r in validated if r.scores[d] == scores[d]) for d in dimensions}
    seen: set[tuple[str, str, str]] = set()
    merged: list[MaterialIssue] = []
    for run in validated:
        for issue in run.issues:
            key = (issue.type.strip(), issue.description.strip(), issue.claim.strip())
            if key not in seen:
                seen.add(key)
                merged.append(issue)
    diffs = sum(1 for d in dimensions if len({r.scores[d] for r in validated}) > 1)
    disagreement = diffs / len(dimensions)
    model = os.environ.get(JUDGE_MODEL_ENV, "").strip()
    if not model:
        attr = getattr(client, "model", "")
        model = attr if isinstance(attr, str) and attr else "injected-client"
    return JudgeAggregate(
        case_id=case_id,
        scores=scores,
        reasons=reasons,
        issues=tuple(merged),
        runs=tuple(validated),
        disagreement=disagreement,
        model=model,
    )
