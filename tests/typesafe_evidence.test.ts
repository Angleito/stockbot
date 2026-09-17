import { expect, test } from "bun:test";
import { judgeEvidence } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const E = [...PACKS.evidence];
const state = {
	claim: "Revenue grew 20% year over year.",
	evidence: "10-K: revenue $10.0B versus $8.3B in the prior year.",
};

function probs(pairs: Record<string, number>): JudgmentMap {
	const out: JudgmentMap = {};
	for (const [id, p] of Object.entries(pairs)) out[id] = { p_yes: p, yes: yes(p) };
	return out;
}

function all(v: number, except: Record<string, number> = {}): Record<string, number> {
	return Object.fromEntries(E.map((id) => [id, except[id] ?? v]));
}

async function judged(p: Record<string, number>) {
	const fake = new FakeEvaluator(probs(p));
	const { results } = await fake.evaluate(
		state,
		E.map((id) => QUESTION_BANK[id]),
	);
	return judgeEvidence(results);
}

test("strong evidence is usable", async () => {
	expect((await judged(all(0.9))).verdict).toBe("usable");
});

test("weak evidence with critical failures is unusable", async () => {
	expect((await judged(all(0.9, { E01: 0.2, E02: 0.1 }))).verdict).toBe("unusable");
});

test("topic-only mention (E02 no) is unusable", async () => {
	expect((await judged(all(0.9, { E02: 0.2 }))).verdict).toBe("unusable");
});

test("out-of-context use (E14 no) is unusable", async () => {
	expect((await judged(all(0.9, { E14: 0.2 }))).verdict).toBe("unusable");
});

test("duplicate adding nothing new (E10 no, E16 no) is unusable", async () => {
	expect((await judged(all(0.9, { E10: 0.2, E16: 0.2 }))).verdict).toBe("unusable");
});

test("0.70 boundary on criticals decides usability", async () => {
	expect((await judged(all(0.9, { E01: 0.7, E02: 0.7, E14: 0.7, E16: 0.7 }))).verdict).toBe("usable");
	expect((await judged(all(0.9, { E01: 0.6999 }))).verdict).toBe("unusable");
});

test("missing ids fail closed to unusable", async () => {
	const fake = new FakeEvaluator(probs({ E01: 0.9 }));
	const { results } = await fake.evaluate(
		state,
		E.map((id) => QUESTION_BANK[id]),
	);
	expect(results["E02"]).toEqual({ p_yes: 0, yes: false });
	expect(judgeEvidence(results).verdict).toBe("unusable");
});

test("invalid evaluator values clamp to p_yes 0 and block, never throw or audit fake confidence", async () => {
	const fake = {
		evaluate: async () => ({
			results: {
				E01: { p_yes: NaN, yes: false },
				E02: { p_yes: Infinity, yes: false },
				E14: { p_yes: -0.5, yes: false },
				E16: { p_yes: 2, yes: false },
			},
			model: "fake-invalid",
		}),
	};
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const out = await runJudgeTool("research_judge_evidence", { objective: "o", claim: "c", evidence: "e", run_id: "run-invalid" }, { evaluator: fake });
	expect(out?.details.verdict).toBe("unusable");
	const questions = out?.details.questions as Record<string, { p_yes: number; yes: boolean }>;
	for (const id of ["E01", "E02", "E14", "E16"]) {
		expect(questions[id].yes).toBe(false);
		expect(Number.isFinite(questions[id].p_yes)).toBe(true);
	}
});
