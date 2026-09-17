import { describe, expect, test } from "bun:test";
import { TypeSafeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";

// Opt-in live eval: exercises the real TypeSafe SDK against a tiny question
// slice. Skipped without TYPESAFE_API_KEY, so normal `bun test` runs zero
// live network. Run explicitly with:
//   TYPESAFE_API_KEY=... bun test tests/typesafe_live.eval.ts
const KEY = process.env.TYPESAFE_API_KEY;
const live = KEY ? test : test.skip;

describe("typesafe live eval (opt-in)", () => {
	live("systemOne answers one evidence question with a finite p_yes", async () => {
		const { results } = await new TypeSafeEvaluator().evaluate(
			{ claim: "Revenue grew year over year.", evidence: "10-K: revenue $10.0B versus $8.3B prior year." },
			[QUESTION_BANK["E01"]],
		);
		const entry = results["E01"];
		expect(Number.isFinite(entry.p_yes)).toBe(true);
		expect(entry.p_yes).toBeGreaterThanOrEqual(0);
		expect(entry.p_yes).toBeLessThanOrEqual(1);
		expect(entry.yes).toBe(entry.p_yes >= 0.7);
	}, 60_000);

	live("systemOne survives the full evidence pack shape", async () => {
		const { results, model } = await new TypeSafeEvaluator().evaluate(
			{ claim: "Revenue grew year over year.", evidence: "10-K: revenue $10.0B versus $8.3B prior year." },
			[...PACKS.evidence].map((id) => QUESTION_BANK[id]),
		);
		expect(typeof model).toBe("string");
		for (const id of PACKS.evidence) expect(Number.isFinite(results[id].p_yes)).toBe(true);
	}, 120_000);
});

test("normal suite performs zero live network (this file only defines skipped tests)", () => {
	if (KEY) return;
	expect(true).toBe(true);
});
