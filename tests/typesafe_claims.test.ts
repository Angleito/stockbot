import { expect, test } from "bun:test";
import { resolveClaim } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

const IDS = [...PACKS.claim];
const state = { claim: "Margin expansion is durable.", evidence: "10-K segment margins, three-year trend." };

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
	return resolveClaim(results, actionable);
}

test("supported claim resolves SUPPORTED", async () => {
	expect(await judged(full(0.8, { C02: 0.1 }), true)).toBe("SUPPORTED");
});

test("contradicted claim resolves CONTRADICTED", async () => {
	expect(await judged(full(0.8, { C01: 0.1 }), true)).toBe("CONTRADICTED");
});

test("support plus contradiction resolves MIXED, never picks a side", async () => {
	expect(await judged(full(0.85), true)).toBe("MIXED");
});

test("ignored contradiction still surfaces as MIXED", async () => {
	expect(await judged(full(0.2, { C01: 0.9, C02: 0.85, C13: 0.1 }), true)).toBe("MIXED");
});

test("unsupported claim stays UNKNOWN_ACTIONABLE while routes remain", async () => {
	expect(await judged(full(0.2), true)).toBe("UNKNOWN_ACTIONABLE");
});

test("missing evidence with no routes left is UNKNOWN_EXHAUSTED", async () => {
	expect(await judged(full(0.2), false)).toBe("UNKNOWN_EXHAUSTED");
});

test("unsupported magnitude keeps direction but is visible in raw p_yes", async () => {
	const judgments: JudgmentMap = { C09: { p_yes: 0.2, yes: false } };
	const { results } = await new FakeEvaluator(judgments).evaluate(state, [QUESTION_BANK["C09"]]);
	expect(results["C09"]).toEqual({ p_yes: 0.2, yes: false });
	expect(await judged(full(0.8, { C02: 0.1, C09: 0.2 }), true)).toBe("SUPPORTED");
});

test("causality gap without support is UNKNOWN_ACTIONABLE", async () => {
	expect(await judged(full(0.2, { C07: 0.1 }), true)).toBe("UNKNOWN_ACTIONABLE");
});

test("overconfident fact framing cannot rescue an unsupported claim", async () => {
	expect(await judged(full(0.2, { C04: 0.95 }), true)).toBe("UNKNOWN_ACTIONABLE");
});

test("0.70 boundary on C01 decides SUPPORTED", async () => {
	expect(await judged(full(0.2, { C01: 0.7 }), true)).toBe("SUPPORTED");
	expect(await judged(full(0.2, { C01: 0.6999 }), true)).toBe("UNKNOWN_ACTIONABLE");
});
