import { expect, test } from "bun:test";
import { judgeCoverage } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { branchStatus, createResearchState, researchComplete, setBranchResult } from "../.stockbot/omp/lib/research-state.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";
import type { JudgmentMap, JudgeQuestion } from "../.stockbot/omp/lib/typesafe/types.ts";
import { authorizationStore, hasOpenAuth, hashAction, launchCommitteeHash } from "../.stockbot/omp/lib/research-control.ts";
import { coverageHash, loadCoverageForFreeze } from "../.stockbot/omp/lib/typesafe/state.ts";
import { runJudgeTool } from "../.stockbot/omp/tools/research-judge-tools.ts";
import { setResearchBridge } from "../.stockbot/omp/lib/research-director.ts";

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

type Req = Record<string, unknown>;

function freezeBridge(session: Req, records: Record<string, Req>): (req: Req) => Promise<Req> {
	return async (req: Req): Promise<Req> => {
		if (req.op === "research.session.inspect") return { result: { session } };
		if (req.op === "tool.invoke" && req.name === "research_read") {
			const args = (req.arguments ?? {}) as Req;
			const rec = records[`${String(args.kind)}:${String(args.resource_id)}`];
			if (rec) return { result: { record: rec } };
		}
		return { error: "unknown_op" };
	};
}

const SID = "sess-cov-load";
const dossierRec = (id: string, wave: number, tag: string): Req => ({
	session_id: SID,
	dossier_id: id,
	wave_id: wave,
	coverage: { covered_branches: [tag], marker: tag },
	findings: [{ text: `finding ${tag}`, evidence_ids: [] }],
	relationships: [],
});
const loadSession = (dossierIds: string[], sid: string = SID): Req => ({
	session_id: sid,
	objective: "objective o",
	dossier_ids: dossierIds,
	unresolved_questions: [],
});

test("loadCoverageForFreeze derives the wave-2 dossier", async () => {
	setResearchBridge(freezeBridge(loadSession(["d-1", "d-2"]), {
		"freeze:fz-2": { session_id: SID, freeze_id: "fz-2", wave_id: 2 },
		"dossier:d-1": dossierRec("d-1", 1, "w1"),
		"dossier:d-2": dossierRec("d-2", 2, "w2"),
	}));
	const st = await loadCoverageForFreeze(SID, "fz-2");
	expect(st.dossierId).toBe("d-2");
	expect(st.objective).toBe("objective o");
	expect(st.coverage).toMatchObject({ marker: "w2" });
});

test("loadCoverageForFreeze rejects with no wave-matching dossier", async () => {
	setResearchBridge(freezeBridge(loadSession(["d-1"]), {
		"freeze:fz-9": { session_id: SID, freeze_id: "fz-9", wave_id: 9 },
		"dossier:d-1": dossierRec("d-1", 1, "w1"),
	}));
	let threw = "";
	try {
		await loadCoverageForFreeze(SID, "fz-9");
	} catch (e) {
		threw = String((e as Error)?.message ?? e);
	}
	expect(threw).toBe("typesafe_untrusted_state");
});

test("coverageHash is stable on equal inputs, differs on differing coverage", () => {
	const branchMap = { dossier_id: "d-2", covered_branches: ["a"] };
	expect(coverageHash(branchMap, { m: "w2" })).toBe(hashAction({ branch_map: branchMap, coverage: { m: "w2" } }));
	expect(coverageHash(branchMap, { m: "w2" })).toBe(
		coverageHash({ dossier_id: "d-2", covered_branches: ["a"] }, { m: "w2" }),
	);
	expect(coverageHash(branchMap, { m: "w2" })).not.toBe(coverageHash(branchMap, { m: "other" }));
});

const covJudgments = (): JudgmentMap =>
	Object.fromEntries(PACKS.coverage.map((id) => [id, { p_yes: 0.9, yes: yes(0.9) }]));

test("coverage COMPLETE binds the derived dossier even with a stale branch_map_id", async () => {
	const run = `run-covderived-${Date.now()}`;
	const branchMap = { dossier_id: "d-derived", covered_branches: ["a"] };
	const coverage = { marker: "w2" };
	const out = await runJudgeTool(
		"research_judge_coverage",
		{ research_session_id: "sess-cov", freeze_id: "fz-2", branch_map_id: "stale-dossier" },
		{
			evaluator: new FakeEvaluator(covJudgments()),
			getRunId: () => run,
			stateLoader: {
				coverage: async () => ({ objective: "o", branch_map: branchMap, coverage, claim_states: [], actionable: true, dossierId: "d-derived" }),
				freeze: async () => { },
			},
		},
	);
	try {
		expect(out?.isError).toBeUndefined();
		expect(out?.details.verdict).toBe("COMPLETE");
		const auth = out?.details.authorization as { authorization_id?: string } | undefined;
		expect(typeof auth?.authorization_id).toBe("string");
		const stored = authorizationStore.get(String(auth?.authorization_id));
		expect(stored?.kind).toBe("launch_committee");
		const parsed = JSON.parse(String(stored?.actionHash)) as Req;
		expect(parsed.sessionId).toBe("sess-cov");
		expect(parsed.freezeId).toBe("fz-2");
		expect(parsed.dossierId).toBe("d-derived");
		expect(parsed.coverageHash).toBe(coverageHash(branchMap, coverage));
		expect(String(stored?.actionHash)).toBe(
			launchCommitteeHash({ sessionId: "sess-cov", freezeId: "fz-2", dossierId: "d-derived", coverageHash: coverageHash(branchMap, coverage) }),
		);
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
	}
});

test("candidate with N13 low is rejected with no authorization", async () => {
	const judgments = Object.fromEntries(
		PACKS.candidate.map((id) => [id, { p_yes: id === "N13" ? 0.2 : 0.9, yes: yes(id === "N13" ? 0.2 : 0.9) }]),
	);
	const run = `run-candn13-${Date.now()}`;
	const out = await runJudgeTool(
		"research_judge_candidate",
		{ research_session_id: "s", gap_id: "g", candidate: { q: "A" }, task: "do X that drifts" },
		{
			evaluator: new FakeEvaluator(judgments),
			getRunId: () => run,
			stateLoader: { candidate: async () => ({ objective: "o", gap: "g" }) },
		},
	);
	expect(out?.isError).toBeUndefined();
	expect(out?.details.authorized).toBe(false);
	expect(out?.details.authorization).toBeUndefined();
	expect(hasOpenAuth(authorizationStore, run, "continue_research")).toBe(false);
});

test("candidate judgment receives the task in evaluated state", async () => {
	const seen: { state?: Req } = {};
	const judgments = Object.fromEntries(PACKS.candidate.map((id) => [id, { p_yes: 0.9, yes: yes(0.9) }]));
	const capturing = {
		async evaluate(state: unknown, questions: JudgeQuestion[]) {
			seen.state = state as Req;
			const results: JudgmentMap = {};
			for (const q of questions) {
				const p = (judgments[q.id] as { p_yes: number } | undefined)?.p_yes ?? 0.9;
				results[q.id] = { p_yes: p, yes: yes(p) };
			}
			return { results, model: "capture" };
		},
	};
	const run = `run-candtask-${Date.now()}`;
	try {
		const out = await runJudgeTool(
			"research_judge_candidate",
			{ research_session_id: "s", gap_id: "g", candidate: { q: "A" }, task: "faithful task" },
			{
				evaluator: capturing,
				getRunId: () => run,
				stateLoader: { candidate: async () => ({ objective: "o", gap: "g" }) },
			},
		);
		expect(out?.details.authorized).toBe(true);
		expect(seen.state?.task).toBe("faithful task");
		expect(seen.state?.candidate).toEqual({ q: "A" });
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
	}
});
test("INCOMPLETE coverage records gaps and minted gap_id authorizes a candidate", async () => {
	const sessionId = `sess-gap-${Date.now()}`;
	const freezeId = "fz-gap";
	const run = `run-gap-${Date.now()}`;
	const covJudgments = Object.fromEntries(PACKS.coverage.map((id) => [id, { p_yes: id === "V14" ? 0.2 : 0.9, yes: yes(id === "V14" ? 0.2 : 0.9) }]));
	const out = await runJudgeTool(
		"research_judge_coverage",
		{ research_session_id: sessionId, freeze_id: freezeId },
		{
			evaluator: new FakeEvaluator(covJudgments),
			getRunId: () => run,
			stateLoader: {
				coverage: async () => ({ objective: "o", branch_map: {}, coverage: {}, claim_states: [], actionable: true, dossierId: "d-gap" }),
				freeze: async () => { },
			},
		},
	);
	try {
		expect(out?.details.verdict).not.toBe("COMPLETE");
		expect((out?.details.failed as { id: string }[]).map((f) => f.id)).toEqual(["V14"]);
		setResearchBridge(freezeBridge(loadSession([], sessionId), {
			[`freeze:${freezeId}`]: { session_id: sessionId, freeze_id: freezeId, wave_id: 1 },
		}));
		const { loadCandidateGap } = await import("../.stockbot/omp/lib/typesafe/state.ts");
		const gapId = `typesafe:coverage:${freezeId}:V14`;
		const gap = await loadCandidateGap(sessionId, gapId);
		expect(gap.gap).toBe(QUESTION_BANK.V14.instruction);
		let threw = "";
		try {
			await loadCandidateGap(sessionId, `typesafe:coverage:${freezeId}:V09`);
		} catch (e) {
			threw = String((e as Error)?.message ?? e);
		}
		expect(threw).toBe("typesafe_untrusted_state");
		const candJudgments = Object.fromEntries(PACKS.candidate.map((id) => [id, { p_yes: 0.9, yes: yes(0.9) }]));
		const cand = await runJudgeTool(
			"research_judge_candidate",
			{ research_session_id: sessionId, gap_id: gapId, candidate: { q: "A" }, task: "faithful task" },
			{ evaluator: new FakeEvaluator(candJudgments), getRunId: () => run, stateLoader: { candidate: async () => gap } },
		);
		expect(cand?.details.authorized).toBe(true);
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
		setResearchBridge(async () => ({ error: "bridge_unavailable" }));
	}
});
test("minted gap rejects in a fresh session until coverage re-runs", async () => {
	const run = `run-gapfresh-${Date.now()}`;
	const covJudgments = Object.fromEntries(PACKS.coverage.map((id) => [id, { p_yes: id === "V09" ? 0.2 : 0.9, yes: yes(id === "V09" ? 0.2 : 0.9) }]));
	const out = await runJudgeTool(
		"research_judge_coverage",
		{ research_session_id: "sess-gap-a", freeze_id: "fz-gap" },
		{
			evaluator: new FakeEvaluator(covJudgments),
			getRunId: () => run,
			stateLoader: {
				coverage: async () => ({ objective: "o", branch_map: {}, coverage: {}, claim_states: [], actionable: true, dossierId: "d-gap" }),
				freeze: async () => { },
			},
		},
	);
	try {
		expect((out?.details.failed as { id: string }[]).map((f) => f.id)).toEqual(["V09"]);
		setResearchBridge(freezeBridge(loadSession([], "sess-gap-fresh"), {
			"freeze:fz-gap": { session_id: "sess-gap-fresh", freeze_id: "fz-gap", wave_id: 1 },
		}));
		const { loadCandidateGap } = await import("../.stockbot/omp/lib/typesafe/state.ts");
		let threw = "";
		try {
			await loadCandidateGap("sess-gap-fresh", "typesafe:coverage:fz-gap:V09");
		} catch (e) {
			threw = String((e as Error)?.message ?? e);
		}
		expect(threw).toBe("typesafe_untrusted_state");
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
		setResearchBridge(async () => ({ error: "bridge_unavailable" }));
	}
});
