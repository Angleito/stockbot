import { expect, test } from "bun:test";
import { authorizeCandidate, judgeContinuation } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const CONT = [...PACKS.continuation];
const CAND = [...PACKS.candidate];
const state = { unresolved: ["supplier exposure"], candidate: "search exhibits" };

function full(ids: string[], v: number, except: Record<string, number> = {}): Record<string, number> {
	return Object.fromEntries(ids.map((id) => [id, except[id] ?? v]));
}

async function judgedCont(p: Record<string, number>) {
	const judgments: JudgmentMap = {};
	for (const [id, v] of Object.entries(p)) judgments[id] = { p_yes: v, yes: yes(v) };
	const { results } = await new FakeEvaluator(judgments).evaluate(
		state,
		CONT.map((id) => QUESTION_BANK[id]),
	);
	return judgeContinuation(results);
}

async function judgedCand(p: Record<string, number>) {
	const judgments: JudgmentMap = {};
	for (const [id, v] of Object.entries(p)) judgments[id] = { p_yes: v, yes: yes(v) };
	const { results } = await new FakeEvaluator(judgments).evaluate(
		state,
		CAND.map((id) => QUESTION_BANK[id]),
	);
	return authorizeCandidate(results);
}

test("unresolved and improvable continues", async () => {
	expect((await judgedCont(full(CONT, 0.2, { N01: 0.9, N02: 0.9, N03: 0.85, N04: 0.9 }))).decision).toBe("continue");
});

test("honest stop with no candidate stops", async () => {
	expect((await judgedCont(full(CONT, 0.2, { N05: 0.9, N06: 0.9 }))).decision).toBe("stop");
});

test("N02-no flips a would-be continue to stop", async () => {
	expect((await judgedCont(full(CONT, 0.2, { N01: 0.9, N02: 0.2, N03: 0.9, N04: 0.9 }))).decision).toBe("stop");
});

test("unresolved but not improvable stops", async () => {
	expect((await judgedCont(full(CONT, 0.2, { N01: 0.9, N02: 0.9, N03: 0.2, N04: 0.9 }))).decision).toBe("stop");
});

test("missing judgments fail closed to stop", async () => {
	const { results } = await new FakeEvaluator({}).evaluate(
		state,
		CONT.map((id) => QUESTION_BANK[id]),
	);
	expect(judgeContinuation(results).decision).toBe("stop");
});

test("0.70 boundary on N01-N04 decides continue", async () => {
	expect((await judgedCont(full(CONT, 0.2, { N01: 0.7, N02: 0.7, N03: 0.7, N04: 0.7 }))).decision).toBe("continue");
	expect((await judgedCont(full(CONT, 0.2, { N01: 0.7, N02: 0.7, N03: 0.6999, N04: 0.7 }))).decision).toBe("stop");
});

test("targeted, novel, likely-useful candidate is authorized", async () => {
	expect(await judgedCand(full(CAND, 0.9))).toBe(true);
});

test("off-target candidate is not authorized", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N07: 0.2 }))).toBe(false);
});

test("duplicate of completed research is not authorized", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N09: 0.2 }))).toBe(false);
});

test("untargeted candidate is not authorized", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N12: 0.2 }))).toBe(false);
});

test("scope-divergent task is not authorized", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N13: 0.2 }))).toBe(false);
});

test("advisory N11 alone never blocks authorization", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N11: 0.1 }))).toBe(true);
});

test("0.70 boundary on every gating candidate question", async () => {
	expect(await judgedCand(full(CAND, 0.9, { N07: 0.7, N08: 0.7, N09: 0.7, N10: 0.7, N12: 0.7, N13: 0.7 }))).toBe(true);
	expect(await judgedCand(full(CAND, 0.9, { N10: 0.6999 }))).toBe(false);
	expect(await judgedCand(full(CAND, 0.9, { N13: 0.6999 }))).toBe(false);
});
