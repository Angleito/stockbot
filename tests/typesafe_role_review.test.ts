import { expect, test } from "bun:test";
import { addBranch, createResearchState } from "../.stockbot/omp/lib/research-state.ts";
import { reviewRoleOutput } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";
import { runReviewTool } from "../.stockbot/omp/tools/output-review-tools.ts";

const ROLE = [...PACKS.common, ...PACKS.stockbot, ...PACKS.bull, ...PACKS.bear];
const state = { role: "stockbot", output: "Base case: resilient; downside contained." };

function full(v: number, except: Record<string, number> = {}): Record<string, number> {
	return Object.fromEntries(ROLE.map((id) => [id, except[id] ?? v]));
}

async function judged(p: Record<string, number>) {
	const judgments: JudgmentMap = {};
	for (const [id, v] of Object.entries(p)) judgments[id] = { p_yes: v, yes: yes(v) };
	const { results } = await new FakeEvaluator(judgments).evaluate(
		state,
		ROLE.map((id) => QUESTION_BANK[id]),
	);
	return reviewRoleOutput(results, ROLE.map((id) => QUESTION_BANK[id]));
}

test("strong role output is accepted", async () => {
	const r = await judged(full(0.9));
	expect(r.verdict).toBe("ACCEPT");
	expect(r.failed).toEqual([]);
});

test("unsupported claims force revision", async () => {
	const r = await judged(full(0.9, { Q04: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
	expect(r.failed.map((f) => f.id)).toContain("Q04");
});

test("missing contradictory evidence forces revision", async () => {
	const r = await judged(full(0.9, { Q03: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("ignored counterevidence in the synthesis forces revision", async () => {
	const r = await judged(full(0.9, { Q02: 0.2, S01: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("overconfident language beyond the evidence forces revision", async () => {
	const r = await judged(full(0.9, { Q07: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("unsupported leap forces revision with raw probabilities preserved", async () => {
	const r = await judged(full(0.9, { Q15: 0.15 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
	expect(r.failed.find((f) => f.id === "Q15")?.p_yes).toBe(0.15);
});

test("fake-precision magnitude alone is advisory and still accepts", async () => {
	const r = await judged(full(0.9, { Q08: 0.1 }));
	expect(r.verdict).toBe("ACCEPT");
});

test("bull optimism unsupported by evidence forces revision", async () => {
	const r = await judged(full(0.9, { B02: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("bear pessimism unsupported by evidence forces revision", async () => {
	const r = await judged(full(0.9, { R02: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("false balance when evidence favors one side forces revision", async () => {
	const r = await judged(full(0.9, { S02: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("dropped disagreement forces revision", async () => {
	const r = await judged(full(0.9, { S03: 0.2 }));
	expect(r.verdict).toBe("REJECT_FOR_REVISION");
});

test("0.70 boundary on a critical decides accept", async () => {
	expect((await judged(full(0.9, { Q04: 0.7 }))).verdict).toBe("ACCEPT");
	expect((await judged(full(0.9, { Q04: 0.6999 }))).verdict).toBe("REJECT_FOR_REVISION");
});

test("review runner issues accept_role_output auth only on ACCEPT", async () => {
	const ids = [...PACKS.common, ...PACKS.bull];
	const probs = (v: number, except: Record<string, number> = {}): Record<string, { p_yes: number; yes: boolean }> => Object.fromEntries(ids.map((id) => [id, { p_yes: except[id] ?? v, yes: yes(except[id] ?? v) }]));
	const { hashAction } = await import("../.stockbot/omp/lib/research-control.ts");
	const roleLoader = (output: unknown) => async () => ({ role: "bullbot", objective: "o", evidence: [], output, freezeId: "f1", freezeEvidenceIds: ["e1"], freezeEvidenceHash: hashAction(["e1"]) });
	const pass = await runReviewTool("research_review_role_output", { research_session_id: "s", freeze_id: "f1", role_job_id: "job-bull-1" }, { evaluator: new FakeEvaluator(probs(0.9)), getRunId: () => "run-role-1", stateLoader: { role: roleLoader({ text: "ok" }) } });
	const passDetails = pass?.details as { verdict?: unknown; authorization?: { kind?: unknown } };
	expect(passDetails.verdict).toBe("ACCEPT");
	expect(passDetails.authorization?.kind).toBe("accept_role_output");
	const fail = await runReviewTool("research_review_role_output", { research_session_id: "s", freeze_id: "f1", role_job_id: "job-bull-1" }, { evaluator: new FakeEvaluator(probs(0.9, { Q04: 0.1 })), getRunId: () => "run-role-1", stateLoader: { role: roleLoader({ text: "bad" }) } });
	const failDetails = fail?.details as { verdict?: unknown; authorization?: unknown };
	expect(failDetails.verdict).toBe("REJECT_FOR_REVISION");
	expect(failDetails.authorization).toBeUndefined();
});

test("addBranch upserts material branches without duplicating ids", () => {
	const state = createResearchState([{ id: "a", question: "qa", material: true }]);
	addBranch(state, { id: "b", question: "qb", material: false });
	addBranch(state, { id: "a", question: "qa2", material: true });
	expect(state.branches.filter((b) => b.id === "a").length).toBe(1);
	expect(state.branches.find((b) => b.id === "a")?.question).toBe("qa2");
});
