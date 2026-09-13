#!/usr/bin/env python3
"""Deterministic agent-scenario verification: fixtures + static checks, no network, no model calls.

Usage:
    python scripts/verify_agent_scenarios.py [--scenario NAME] [--model LABEL]
        [--provider LABEL] [--prompt-version VER] [--fixtures-dir DIR] [--json]
    python scripts/verify_agent_scenarios.py --list

With --model the run is persisted to data/eval_runs.sqlite stamped with the
model label, harness version, prompt version, git SHA, timestamp, and
scenario version (see app/research/evals/evaluators.py). Without --model it
is a dry run. Exit 0 when every scenario passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.evals.evaluators import EvalInput, evaluate, eval_input_from_fixture, run_eval_suite  # noqa: E402
from app.research.evals.regression import list_fixtures, load_fixture, run_deterministic_validators  # noqa: E402
from app.research.evals.scenarios import list_scenarios  # noqa: E402


def _check_static(name: str, answer: str = "") -> EvalInput:
    """Definition-level outcome for scenarios with no promoted fixture yet."""
    for scenario in list_scenarios():
        if scenario.name == name:
            return EvalInput(
                scenario_name=name,
                answer_text=answer,
                tool_calls=scenario.expected_tools,
                as_of=scenario.as_of,
                requires_evidence=scenario.requires_evidence,
            )
    raise KeyError(f"unknown scenario {name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    parser.add_argument("--scenario", default=None, help="run one scenario (default: all)")
    parser.add_argument("--model", default=None,
                        help="model label for the persisted eval run (default: dry run, no persistence)")
    parser.add_argument("--provider", default="unknown", help="provider label (default unknown)")
    parser.add_argument("--prompt-version", default="v1", help="prompt version stamp (default v1)")
    parser.add_argument("--fixtures-dir", default=None, help="fixtures dir (default: evals/fixtures/agent_scenarios)")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    args = parser.parse_args()

    if args.list:
        for scenario in list_scenarios():
            print(f"{scenario.name} [{scenario.family.value}]")
        return 0

    fixtures_dir = Path(args.fixtures_dir) if args.fixtures_dir else None
    names = [args.scenario] if args.scenario else [s.name for s in list_scenarios()]
    saved = set(list_fixtures(fixtures_dir))
    outcomes: list[EvalInput] = []
    static: dict[str, bool] = {}
    for name in names:
        if name in saved:
            try:
                fixture = load_fixture(name, fixtures_dir)
            except (OSError, ValueError) as exc:
                print(f"FAIL {name}: unreadable fixture ({exc})")
                return 1
            problems = run_deterministic_validators(fixture)
            if problems:
                print(f"FAIL {name}: {', '.join(problems)}")
                return 1
            outcomes.append(eval_input_from_fixture(fixture))
        else:
            static[name] = True
            outcomes.append(_check_static(name))

    results = [evaluate(inp) for inp in outcomes]
    failed = [r for r in results if not r.passed]
    for result in results:
        suffix = " (static; no fixture)" if result.scenario_name in static else ""
        if result.passed:
            print(f"PASS {result.scenario_name}{suffix}")
        else:
            print(f"FAIL {result.scenario_name}{suffix}: {', '.join(result.violations)}")

    summary: dict[str, object] = {
        "scenarios": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": len(failed),
        "failures": {r.scenario_name: list(r.violations) for r in failed},
    }
    if args.model:
        suite = run_eval_suite(
            model=args.model,
            provider=args.provider,
            prompt_version=args.prompt_version,
            outcomes=outcomes,
        )
        summary["eval_run_id"] = suite.eval_run_id
        summary["model"] = suite.model
        if not args.json:
            print(f"eval run {suite.eval_run_id}:"
                  f" {suite.passed_count}/{suite.scenario_count} passed (model={suite.model})")
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
