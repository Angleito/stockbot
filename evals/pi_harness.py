"""Pi-harness eval port (step 10): drive 3 eval_set.json cases via the bridge.

Cases: 11 (short-interest), 5 (filing), 29 (valuation).

Each case issues its tool sequence through `scripts/pi_bridge.py` tool_call
ops (subprocess JSONL) and applies the case's own expected_behavior assertions
with the same semantics as evals/run_evals.py had (helper copied, not imported).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _is_ordered_subsequence(required: list, trace: list) -> bool:
    """True if every item in `required` appears in `trace` in order."""
    it = iter(trace)
    return all(any(item == name for name in it) for item in required)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Deterministic per-case drive plan: case id -> ordered (tool, arguments).
PLAN = {
    11: [("get_short_interest", {"ticker": "AAPL"})],
    5: [("list_sec_filings", {"identifier": "MSFT", "forms": ["10-Q"], "limit": 1})],
    29: [("get_valuation_metrics", {"ticker": "AAPL"})],
}


class Bridge:
    def __init__(self):
        python = os.path.join(ROOT, "venv", "bin", "python")
        if not os.path.exists(python):
            python = sys.executable  # ponytail: venv path first, fallback same interpreter
        self.proc = subprocess.Popen(
            [python, os.path.join(ROOT, "scripts", "pi_bridge.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )

    def call(self, name, arguments, session_id):
        self.proc.stdin.write(json.dumps({
            "op": "tool_call", "name": name,
            "arguments": arguments, "session_id": session_id,
        }) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline()).get("result", {})

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()


def check_case(case, detailed, response):
    expected = case.get("expected_behavior", {})
    trace = [t["name"] for t in detailed]
    resp = response.lower()
    must = [s.lower() for s in expected.get("must_contain", [])]
    must_not = [s.lower() for s in expected.get("must_not_contain", [])]
    checks = {
        "expected_tools": (not expected.get("expected_tools")
                           or any(t in trace for t in expected["expected_tools"])),
        "required_tools": all(t in trace for t in expected.get("required_tools", [])),
        "sequence": _is_ordered_subsequence(
            expected.get("required_tool_sequence", []), trace),
        "forbidden_args": all(
            arg not in (call.get("arguments") or {})
            for tool, args in expected.get("forbidden_tool_args", {}).items()
            for arg in args for call in detailed if call["name"] == tool),
        "forbidden_tools": not any(
            t in trace for t in expected.get("forbidden_tools", [])),
        "must_contain": (not must or any(s in resp for s in must)),
        "must_not_contain": not any(s in resp for s in must_not),
    }
    failed = [k for k, ok in checks.items() if not ok]
    return not failed, f"tools called={trace}" + (f" (failed: {failed})" if failed else "")


def main():
    with open(os.path.join(ROOT, "evals", "eval_set.json")) as f:
        cases = {c["id"]: c for c in json.load(f)}
    bridge = Bridge()
    passed = 0
    try:
        for cid, calls in PLAN.items():
            case = cases[cid]
            detailed, results = [], []
            for name, args in calls:
                results.append(bridge.call(name, args, f"eval-{cid}"))
                detailed.append({"name": name, "arguments": args})
            ok, details = check_case(case, detailed, json.dumps(results))
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] Q{cid}: {case['question']}")
            print(f"       Details: {details}")
    finally:
        bridge.close()
    print(f"\nPI-HARNESS EVALS: {passed}/{len(PLAN)} passed")
    sys.exit(0 if passed == len(PLAN) else 1)


if __name__ == "__main__":
    main()
