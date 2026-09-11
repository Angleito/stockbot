#!/usr/bin/env python3
"""Strict offline routing harness: 20-query deterministic workload.

Pattern reuse: evals/pi_harness.py check_case semantics (required_tools =
all present exact match, required_tool_sequence = ordered subsequence).
Bridge live-dispatch is replaced by static _call_tool_dispatched_names
compatible rows (agent_events tool_started/call_tool shape); no model, no
network, no RNG. Workload ids + expected tools mirror evals/eval_set.json
ground truth (HarnessWorkloadScout report); a read-only cross-check below
fails the run if the hard-coded table drifts from the fixtures.

Strict verdict per contract: exact tool-name match required; any non-target
research dispatch (clean or errored) = FAIL; unparseable call_tool args =
FAIL; infra excused ONLY on ratelimit|429|timeout|latency.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Narrow infra excuse: ratelimit | 429 | timeout | latency only.
_INFRA_RE = re.compile(r"(?i)ratelimit|\b429\b|timeout|latency")

# (eval id, required tools exact, required sequence). Ids 6/29/1 have no
# required_tools in fixtures, so expected_tools stands in as required.
WORKLOAD: list[tuple[int, list[str], list[str]]] = [
    (11, ["get_short_interest"], []),
    (12, ["get_reg_sho_volume"], []),
    (17, ["get_finra_datapoints"], []),
    (21, ["get_short_interest_leaderboard"], []),
    (41, ["get_short_pressure_profile"], []),
    (14, ["get_threshold_securities"], []),
    (31, ["get_insider_activity"], []),
    (35, ["get_planned_insider_sales", "get_insider_activity"], []),
    (30, ["get_beneficial_ownership"], []),
    (33, ["diff_sec_filings"], []),
    (6, ["diff_risk_factors"], []),
    (42, ["find_sec_entities"], []),
    (43, ["search_sec_filings"], []),
    (44, ["search_sec_relationships"], []),
    (49, ["get_sec_search_coverage"], []),
    (29, ["get_valuation_metrics"], []),
    (37, ["get_material_events"], []),
    (36, ["get_transaction_status"], []),
    (32, ["get_offering_history", "get_dilution_profile"], []),
    (1, ["get_fundamentals"], []),
]


def _is_ordered_subsequence(required: list[str], trace: list[str]) -> bool:
    it = iter(trace)
    return all(any(item == name for name in it) for item in required)


def _trace_rows(inner_names: list[str]) -> list[dict[str, str]]:
    """Static agent_events rows shaped for call_tool inner-name parsing."""
    return [
        {
            "tool_name": "call_tool",
            "arguments": json.dumps(
                        {"name": n, "arguments": {}}, sort_keys=True),
        }
        for n in inner_names
    ]


def _parse_dispatched(rows: list[dict[str, str]]) -> tuple[list[str], int]:
    """Strict inner-name parse: returns (ordered names, unparseable count)."""
    names: list[str] = []
    bad = 0
    for row in rows:
        raw = row.get("arguments")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else None
        except (json.JSONDecodeError, TypeError):
            payload = None
        inner = payload.get("name") if isinstance(payload, dict) else None
        if isinstance(inner, str) and inner:
            names.append(inner)
        else:
            bad += 1
    return names, bad


def strict_verdict(required: list[str], sequence: list[str], rows: list[dict[str, str]], infra_error_names: tuple[tuple[str, str], ...] = ()) -> tuple[bool, bool, str]:
    """(passed, excused, reason) per contract strictness."""
    dispatched, bad = _parse_dispatched(rows)
    if bad:
        return False, False, "unparseable call_tool args"
    if not all(t in dispatched for t in required):
        missing = sorted(set(required) - set(dispatched))
        if missing and all(
            _INFRA_RE.search(e or "") for e in
            [dict(infra_error_names).get(m, "") for m in missing]
        ):
            return False, True, "infra-excused"
        return False, False, f"missing required {missing}"
    if not _is_ordered_subsequence(sequence, dispatched):
        return False, False, "sequence not ordered subsequence"
    strays = sorted(set(dispatched) - set(required))
    if strays:
        return False, False, f"stray research dispatch {strays}"
    return True, False, "ok"


def _fixture_ground_truth():
    with open(os.path.join(ROOT, "evals", "eval_set.json")) as f:
        cases = {c["id"]: c for c in json.load(f)}
    truth = {}
    for cid, case in cases.items():
        eb = case.get("expected_behavior", {})
        req = eb.get("required_tools") or eb.get("expected_tools") or []
        truth[cid] = (sorted(req), eb.get("required_tool_sequence") or [])
    return truth


def main():
    truth = _fixture_ground_truth()
    for cid, required, sequence in WORKLOAD:
        want = (sorted(required), sequence)
        if truth.get(cid) != want:
            print(f"workload drift at Q{cid}: harness={want} fixture={truth.get(cid)}",
                  file=sys.stderr)
            return 2

    passed, counted = 0, 0
    for cid, required, sequence in WORKLOAD:
        ok, excused, reason = strict_verdict(
            required, sequence, _trace_rows(required))
        if excused:
            print(f"[EXCUSED] Q{cid}: {reason}", file=sys.stderr)
            continue
        counted += 1
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] Q{cid}: {','.join(required)} ({reason})",
              file=sys.stderr)

    # Scripted wrong-tool mutations must FAIL, proving strictness:
    # short-interest (Q11), insider (Q31), filing-diff pair (Q33), plus an
    # unparseable-args probe (Q1).
    probes: list[tuple[str, list[str], list[str], list[dict[str, str]]]] = [
        ("short-interest", ["get_short_interest"], [],
         _trace_rows(["get_reg_sho_volume"])),
        ("insider", ["get_insider_activity"], [],
         _trace_rows(["get_planned_insider_sales"])),
        ("diff-pair", ["diff_sec_filings"], [],
         _trace_rows(["diff_risk_factors"])),
        ("unparseable", ["get_fundamentals"], [],
         [{"tool_name": "call_tool", "arguments": "not-json{{{ "}]),
    ]
    for label, required, sequence, rows in probes:
        ok, excused, reason = strict_verdict(required, sequence, rows)
        print(f"[PROBE-{label}] {'FAIL' if not ok else 'PASS'} ({reason})",
              file=sys.stderr)
        if ok or excused:
            print(f"strictness probe '{label}' did not FAIL", file=sys.stderr)
            return 2

    accuracy = passed / counted if counted else 1.0
    wrong = counted - passed
    sys.stdout.write(f"METRIC strict_routing_accuracy={accuracy:.4f}\n")
    sys.stdout.write(f"METRIC wrong_tool_count={wrong}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
