import { expect, test } from "bun:test";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { QUESTION_BANK_VERSION } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { YES_THRESHOLD } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import { runJudgeTool } from "../.stockbot/omp/tools/research-judge-tools.ts";
import { runReviewTool } from "../.stockbot/omp/tools/output-review-tools.ts";
import type { JudgmentMap } from "../.stockbot/omp/lib/typesafe/types.ts";

function fake(ids: string[], v: number, except: Record<string, number> = {}): FakeEvaluator {
	const judgments: JudgmentMap = {};
	for (const id of ids) judgments[id] = { p_yes: except[id] ?? v, yes: (except[id] ?? v) >= YES_THRESHOLD };
	return new FakeEvaluator(judgments);
}

test("judge tool details.audit persists raw probabilities, bank version, threshold, model", async () => {
	const { PACKS } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const out = await runJudgeTool(
		"research_judge_evidence",
		{ research_session_id: "s", freeze_id: "f", evidence_id: "e1" },
		{ evaluator: fake(PACKS.evidence, 0.9, { E02: 0.2 }), model: "system-one-x", getRunId: () => "run-log-1", stateLoader: { evidence: async () => ({ objective: "o", claim: "c", evidence: { evidence_id: "e1" } }) } },
	);
	const audit = out?.details.audit as Record<string, unknown>;
	expect(out?.details.verdict).toBe("unusable");
	expect(audit.run_id).toBe("run-log-1");
	expect(audit.question_bank_version).toBe(QUESTION_BANK_VERSION);
	expect(audit.threshold).toBe(YES_THRESHOLD);
	expect(audit.model).toBe("fake");
	expect((audit.questions as Record<string, unknown>)["E01"]).toEqual({ p_yes: 0.9, yes: true });
});

test("persisted audit never carries key material even when params do", async () => {
	const { PACKS } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const out = await runJudgeTool(
		"research_judge_evidence",
		{
			research_session_id: "s",
			freeze_id: "f",
			evidence_id: "e1",
			TYPESAFE_API_KEY: "sk-live-SENTINEL",
			api_key: "sk-live-SENTINEL",
			passage: "raw secret passage",
		},
		{ evaluator: fake(PACKS.evidence, 0.9), getRunId: () => "run-log-2", stateLoader: { evidence: async () => ({ objective: "o", claim: "c", evidence: { evidence_id: "e1" } }) } },
	);
	const line = JSON.stringify(out?.details.audit);
	expect(line).not.toContain("sk-live-SENTINEL");
	expect(line).not.toContain("raw secret passage");
	expect(line).not.toContain("TYPESAFE_API_KEY");
});

test("final review details.audit pins BLOCK with failing probabilities", async () => {
	const { PACKS } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const out = await runReviewTool(
		"research_review_final",
		{ research_session_id: "s", freeze_id: "f", answer: "draft" },
		{ evaluator: fake(PACKS.final_extended, 0.9, { F10: 0.2 }), getRunId: () => "run-log-3", stateLoader: { final: async () => ({ objective: "o", evidence: [], committee: { stockbot: {}, bullbot: {}, bearbot: {} } }) } },
	);
	const audit = out?.details.audit as Record<string, unknown>;
	expect(audit.phase).toBe("final");
	expect(audit.result).toBe("BLOCK");
	expect((audit.questions as Record<string, unknown>)["F10"]).toEqual({ p_yes: 0.2, yes: false });
});
