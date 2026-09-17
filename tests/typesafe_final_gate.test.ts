import { expect, test } from "bun:test";
import { buildAudit } from "../.stockbot/omp/lib/typesafe/audit.ts";
import { reviewFinal } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK, QUESTION_BANK_VERSION } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { YES_THRESHOLD, yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const IDS = [...PACKS.final];
const state = { answer: "Base case holds; downside bounded; see disagreements." };

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
	return { review: reviewFinal(results, IDS.map((id) => QUESTION_BANK[id])), results };
}

test("faithful synthesis passes the final gate", async () => {
	const { review } = await judged(full(0.9));
	expect(review.verdict).toBe("PASS");
	expect(review.failed).toEqual([]);
});

test("overstated conclusion beyond the evidence blocks", async () => {
	expect((await judged(full(0.9, { F10: 0.2 }))).review.verdict).toBe("BLOCK");
});

test("dropped case or hidden uncertainty blocks", async () => {
	expect((await judged(full(0.9, { F09: 0.2 }))).review.verdict).toBe("BLOCK");
	expect((await judged(full(0.9, { F12: 0.2 }))).review.verdict).toBe("BLOCK");
});

test("0.70 boundary decides the final gate", async () => {
	expect((await judged(full(0.9, { F10: 0.7 }))).review.verdict).toBe("PASS");
	expect((await judged(full(0.9, { F10: 0.6999 }))).review.verdict).toBe("BLOCK");
});

test("final gate audit pins bank version, threshold, and raw probabilities", async () => {
	const { review, results } = await judged(full(0.9));
	const audit = buildAudit({ runId: "run-final-1", phase: "final", questions: results, result: review.verdict });
	expect(audit.run_id).toBe("run-final-1");
	expect(audit.question_bank_version).toBe(QUESTION_BANK_VERSION);
	expect(audit.threshold).toBe(YES_THRESHOLD);
	expect(audit.questions["F10"]).toEqual({ p_yes: 0.9, yes: true });
	expect(typeof audit.timestamp).toBe("string");
});
