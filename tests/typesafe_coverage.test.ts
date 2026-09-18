import { expect, test } from "bun:test";
import { judgeCoverage } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { branchStatus, createResearchState, researchComplete, setBranchResult } from "../.stockbot/omp/lib/research-state.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const IDS = [...PACKS.coverage];
const state = { branches: ["ownership", "revenue", "supplier"], searched: ["ownership"] };

function full(v: number, except: Record<string, number> = {}): Record<string, number> {
	return Object.fromEntries(IDS.map((id) => [id, except[id] ?? v]));
}

async function judged(p: Record<string, number>, actionable: boolean) {
	const judgments: JudgmentMap = {};
	for (const [id, v] of Object.entries(p)) judgments[id] = { p_yes: v, yes: yes(v) };
	const { results } = await new FakeEvaluator(judgments).evaluate(
		state,
		IDS.map((id) => QUESTION_BANK[id]),
	);
	return judgeCoverage(results, actionable);
}

test("full branch map is COMPLETE", async () => {
	expect(await judged(full(0.9), true)).toBe("COMPLETE");
});

test("missing branch with routes left is INCOMPLETE_ACTIONABLE", async () => {
	expect(await judged(full(0.9, { V01: 0.2 }), true)).toBe("INCOMPLETE_ACTIONABLE");
});

test("missing branch with no routes left is INCOMPLETE_NONACTIONABLE", async () => {
	expect(await judged(full(0.9, { V01: 0.2 }), false)).toBe("INCOMPLETE_NONACTIONABLE");
});

test("unsearched routes that could change the answer stay actionable", async () => {
	expect(await judged(full(0.9, { V19: 0.2 }), true)).toBe("INCOMPLETE_ACTIONABLE");
});

test("insufficient coverage for an honest answer is not COMPLETE", async () => {
	expect(await judged(full(0.9, { V20: 0.2 }), true)).toBe("INCOMPLETE_ACTIONABLE");
});

test("middle-question V09-no flips a would-be COMPLETE to actionable", async () => {
	expect(await judged(full(0.9, { V09: 0.2 }), true)).toBe("INCOMPLETE_ACTIONABLE");
});

test("0.70 boundary on every coverage question decides COMPLETE", async () => {
	expect(await judged(full(0.7), true)).toBe("COMPLETE");
	expect(await judged(full(0.9, { V09: 0.6999 }), true)).toBe("INCOMPLETE_ACTIONABLE");
});

test("branchStatus: uninvestigated material stays open, resolved closes", () => {
	const branch = { id: "b1", question: "Supplier exposure?", material: true };
	expect(branchStatus(branch, false, "UNKNOWN_ACTIONABLE")).toBe("OPEN_ACTIONABLE");
	expect(branchStatus(branch, true, "UNKNOWN_ACTIONABLE")).toBe("OPEN_ACTIONABLE");
	expect(branchStatus(branch, true, "SUPPORTED")).toBe("CLOSED");
	expect(branchStatus(branch, true, "MIXED")).toBe("CLOSED");
	expect(branchStatus(branch, true, "UNKNOWN_EXHAUSTED")).toBe("CLOSED_UNKNOWN");
	expect(branchStatus({ ...branch, material: false }, false, "UNKNOWN_ACTIONABLE")).toBe("CLOSED");
});

test("researchComplete tracks open material branches only", () => {
	const rs = createResearchState([
		{ id: "b1", question: "q1", material: true },
		{ id: "b2", question: "q2", material: false },
	]);
	expect(researchComplete(rs)).toBe(false);
	setBranchResult(rs, "b1", true, "SUPPORTED");
	expect(researchComplete(rs)).toBe(true);
});
