"""Evaluation runner: evaluates eval_set.json across one or more OpenRouter models."""

import argparse
import json
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import run_chat
from app.config import get_default_model, get_local_chat_policy
from app.policy import LOCAL_CONTEXT


def _is_ordered_subsequence(required: list, trace: list) -> bool:
    """True if every item in `required` appears in `trace` in order."""
    it = iter(trace)
    return all(any(item == name for name in it) for item in required)


def run_evals(eval_file: str, models: list[str]) -> dict:
    with open(eval_file, "r") as f:
        cases = json.load(f)

    # results: {model: {case_id: (passed: bool, details: str)}}
    results = {}

    for model in models:
        print(f"\n==================================================")
        print(f"Running evals for model: {model}")
        print(f"==================================================")
        model_results = {}
        passed_count = 0

        for case in cases:
            cid = case["id"]
            question = case["question"]
            expected = case.get("expected_behavior", {})
            expected_tools = expected.get("expected_tools", [])
            required_tools = expected.get("required_tools", [])
            required_sequence = expected.get("required_tool_sequence", [])
            forbidden_tool_args = expected.get("forbidden_tool_args", {})
            must_contain = [s.lower() for s in expected.get("must_contain", [])]
            must_not_contain = [s.lower() for s in expected.get("must_not_contain", [])]

            try:
                response, detailed = run_chat(
                    [{"role": "user", "content": question}],
                    model=model,
                    context=LOCAL_CONTEXT,
                    policy=get_local_chat_policy(),
                    return_detailed_trace=True,
                )
                trace = [t["name"] for t in detailed]
                resp_lower = response.lower()

                # 1a. Legacy any-of check
                if expected_tools:
                    any_ok = any(t in trace for t in expected_tools)
                else:
                    any_ok = True

                # 1b. Every required tool must appear
                if required_tools:
                    all_ok = all(t in trace for t in required_tools)
                else:
                    all_ok = True

                # 1c. Required tools must appear in order (as a subsequence)
                if required_sequence:
                    seq_ok = _is_ordered_subsequence(required_sequence, trace)
                else:
                    seq_ok = True

                # 1d. Forbidden argument names per tool
                if forbidden_tool_args:
                    forbid_ok = all(
                        arg not in (call.get("arguments") or {})
                        for tool, args in forbidden_tool_args.items()
                        for arg in args
                        for call in detailed
                        if call["name"] == tool
                    )
                else:
                    forbid_ok = True

                # 2. Check must_contain (if any match is sufficient when multiple alternatives provided)
                if must_contain:
                    contain_ok = any(item in resp_lower for item in must_contain)
                else:
                    contain_ok = True

                # 3. Check must_not_contain
                not_contain_ok = not any(item in resp_lower for item in must_not_contain)

                passed = (
                    any_ok and all_ok and seq_ok and forbid_ok
                    and contain_ok and not_contain_ok
                )
                if passed:
                    passed_count += 1

                status = "PASS" if passed else "FAIL"
                details = f"tools called={trace}"
                if not any_ok:
                    details += f" (expected one of {expected_tools})"
                if not all_ok:
                    missing = [t for t in required_tools if t not in trace]
                    details += f" (missing required tools: {missing})"
                if not seq_ok:
                    details += f" (required order not followed: {required_sequence})"
                if not forbid_ok:
                    details += f" (called with forbidden args: {forbidden_tool_args})"
                if not contain_ok:
                    details += f" (missing one of required substrings: {must_contain})"
                if not not_contain_ok:
                    matched_forbidden = [item for item in must_not_contain if item in resp_lower]
                    details += f" (contained forbidden substrings: {matched_forbidden})"

                print(f"[{status}] Q{cid}: {question}")
                print(f"       Details: {details}")
                if not passed:
                    print(f"       Response snippet: {response[:300]}...")
                model_results[cid] = (passed, details)

            except Exception as e:
                print(f"[ERROR] Q{cid}: {question} -> {e}")
                model_results[cid] = (False, f"Exception: {e}")

        results[model] = {
            "score": f"{passed_count}/{len(cases)}",
            "passed_count": passed_count,
            "total": len(cases),
            "cases": model_results,
        }

    # Summary table
    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY TABLE")
    print("=" * 60)
    header = f"{'Question ID':<15}" + "".join(f"{m[:20]:<22}" for m in models)
    print(header)
    print("-" * len(header))

    for case in cases:
        cid = case["id"]
        row = f"Q{cid:<14}"
        for m in models:
            passed, _ = results[m]["cases"].get(cid, (False, "N/A"))
            row += f"{'PASS' if passed else 'FAIL':<22}"
        print(row)

    print("-" * len(header))
    total_row = f"{'Total Passed':<15}"
    for m in models:
        total_row += f"{results[m]['score']:<22}"
    print(total_row)
    print("=" * 60)

    return results


def main():
    parser = argparse.ArgumentParser(description="Run eval suite against LLM models via OpenRouter")
    parser.add_argument(
        "--models",
        nargs="+",
        default=[get_default_model()],
        help="One or more OpenRouter model strings to evaluate",
    )
    parser.add_argument(
        "--eval-set",
        default=os.path.join(os.path.dirname(__file__), "eval_set.json"),
        help="Path to eval_set.json",
    )
    args = parser.parse_args()
    run_evals(args.eval_set, args.models)


if __name__ == "__main__":
    main()
