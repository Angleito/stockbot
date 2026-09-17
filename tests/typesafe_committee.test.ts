import { expect, test } from "bun:test";
import { reviewCommittee } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const IDS = [...PACKS.committee];
const state = { stockbot: "base case", bullbot: "upside", bearbot: "downside" };

function full(v: number, except: Record<string, number> = {}): Record<string, number> {
	return Object.fromEntries(IDS.map((id) => [id, except[id] ?? v]));
}

async function judged(p: Record<string, number>) {
	const judgments: JudgmentMap = {};
	for (const [id, v] of Object.entries(p)) judgments[id] = { p_yes: v, yes: yes(v) };
	const { results } = await new FakeEvaluator(judgments).evaluate(
		state,
		IDS.map((id) => QUESTION_BANK[id]),
	);
	return reviewCommittee(results, IDS.map((id) => QUESTION_BANK[id]));
}

test("aligned committee on the same evidence passes", async () => {
	const r = await judged(full(0.9));
	expect(r.verdict).toBe("PASS");
	expect(r.failed).toEqual([]);
});

test("members on different evidence sets block", async () => {
	expect((await judged(full(0.9, { F01: 0.2 }))).verdict).toBe("BLOCK");
});

test("uncovered material claim blocks", async () => {
	expect((await judged(full(0.9, { F02: 0.2 }))).verdict).toBe("BLOCK");
});

test("hidden disagreement blocks", async () => {
	expect((await judged(full(0.9, { F04: 0.2 }))).verdict).toBe("BLOCK");
});

test("missing channel coverage blocks", async () => {
	expect((await judged(full(0.9, { F07: 0.2 }))).verdict).toBe("BLOCK");
});

test("advisory F08 alone never blocks", async () => {
	const r = await judged(full(0.9, { F08: 0.1 }));
	expect(r.verdict).toBe("PASS");
});

test("0.70 boundary on a critical decides pass", async () => {
	expect((await judged(full(0.9, { F01: 0.7 }))).verdict).toBe("PASS");
	expect((await judged(full(0.9, { F01: 0.6999 }))).verdict).toBe("BLOCK");
});
