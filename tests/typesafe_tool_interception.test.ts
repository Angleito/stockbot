import { expect, test } from "bun:test";
import {
	authorizationStore,
	consumeAuthorization,
	consumeMatchingAuth,
	consumeOpenRoleAccepts,
	drainCommitteeAccepts,
	drainRoleAccepts,
	finalizeActionHash,
	hashAction,
	hasOpenAuth,
	hasOpenRoleAccepts,
	isExactRepeat,
	issueAuthorization,
	issueRoleAccept,
} from "../.stockbot/omp/lib/research-control.ts";
import { reviewCommittee, reviewFinal } from "../.stockbot/omp/lib/typesafe/decisions.ts";
import {
	peekTaskFingerprint,
	planTaskCall,
	recordTaskResult,
	resumeResearch,
	setResearchBridge,
} from "../.stockbot/omp/lib/research-director.ts";
import { FakeEvaluator } from "../.stockbot/omp/lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../.stockbot/omp/lib/typesafe/questions.ts";
import { yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";

// Models the index.ts TypeSafe gate per the frozen Contract: one
// Authorization = one protected action; missing, reused, or wrong-kind auth
// blocks fail-closed. These tests pin research-control consume semantics,
// which is the entire interception mechanism the gate delegates to.
const runId = "run-gate-1";

function judged(ids: string[], full: Record<string, number>) {
	return new FakeEvaluator(
		Object.fromEntries(Object.entries(full).map(([id, p_yes]) => [id, { p_yes, yes: yes(p_yes) }])),
	).evaluate(
		{},
		ids.map((id) => QUESTION_BANK[id]),
	);
}

function gate(store: Map<string, { consumed: boolean }>, authId: string | undefined): string | undefined {
	if (!authId) return "Stockbot TypeSafe: blocked (no authorization)";
	const auth = store.get(authId);
	if (!auth || auth.consumed) return "Stockbot TypeSafe: blocked (authorization invalid or consumed)";
	return undefined;
}

test("no-auth second source wave is blocked", () => {
	expect(gate(new Map(), undefined)).toContain("blocked");
});

test("authed second wave is allowed exactly once, then reuse is blocked", () => {
	const auth = issueAuthorization(runId, "continue_research", hashAction({ wave: 2 }));
	try {
		expect(gate(authorizationStore, auth.id)).toBeUndefined();
		expect(consumeAuthorization(authorizationStore, auth.id)).toBe(true);
		expect(consumeAuthorization(authorizationStore, auth.id)).toBe(false);
		expect(gate(authorizationStore, auth.id)).toContain("blocked");
	} finally {
		authorizationStore.delete(auth.id);
	}
});

test("wrong-kind or wrong-action auth does not authorize", () => {
	const waveHash = hashAction({ wave: 2 });
	const otherHash = hashAction({ wave: 3 });
	const kindAuth = issueAuthorization(runId, "launch_committee", waveHash);
	const hashAuth = issueAuthorization(runId, "continue_research", otherHash);
	try {
		expect(consumeAuthorization(authorizationStore, kindAuth.id, { runId, kind: "continue_research", actionHash: waveHash })).toBe(false);
		expect(consumeAuthorization(authorizationStore, hashAuth.id, { runId, kind: "continue_research", actionHash: waveHash })).toBe(false);
		expect(consumeAuthorization(authorizationStore, hashAuth.id, { runId, kind: "continue_research", actionHash: otherHash })).toBe(true);
	} finally {
		authorizationStore.delete(kindAuth.id);
		authorizationStore.delete(hashAuth.id);
	}
});

test("committee launch before coverage completes is blocked", async () => {
	const store = new Map<string, { consumed: boolean }>();
	expect(gate(store, undefined)).toContain("blocked");
	const { results } = await judged([...PACKS.committee], { F01: 0.2 });
	const review: { verdict: string } = reviewCommittee(
		results,
		[...PACKS.committee].map((id) => QUESTION_BANK[id]),
	);
	expect(review.verdict).toBe("BLOCK");
	const auth = issueAuthorization(runId, "launch_committee", hashAction({ freeze: "F1" }));
	try {
		expect(gate(authorizationStore, auth.id)).toBeUndefined();
		expect(consumeAuthorization(authorizationStore, auth.id)).toBe(true);
	} finally {
		authorizationStore.delete(auth.id);
	}
});

test("finalize before the final gate passes is blocked", async () => {
	expect(gate(new Map(), undefined)).toContain("blocked");
	const { results } = await judged([...PACKS.final], { F10: 0.2 });
	const review: { verdict: string } = reviewFinal(
		results,
		[...PACKS.final].map((id) => QUESTION_BANK[id]),
	);
	expect(review.verdict).toBe("BLOCK");
});

test("TypeSafe-down blocks: evaluator throw means no auth is ever issued", async () => {
	const down = {
		evaluate(): Promise<never> {
			throw new Error("typesafe_evaluation_failed");
		},
	};
	let threw: unknown = null;
	try {
		await down.evaluate();
	} catch (e) {
		threw = e;
	}
	expect(threw).toBeDefined();
	expect(String(threw)).not.toContain("TYPESAFE_API_KEY");
	expect(gate(new Map(), undefined)).toContain("blocked");
});

test("duplicate action hash is an exact repeat and needs no new wave", () => {
	const seen = new Set<string>();
	const h = hashAction({ wave: 2, freeze: "F1" });
	expect(isExactRepeat(seen, h)).toBe(false);
	seen.add(h);
	expect(isExactRepeat(seen, h)).toBe(true);
});

test("blocked reasons never carry key material or transcripts", () => {
	const reason = gate(new Map(), undefined) ?? "";
	expect(reason).not.toContain("TYPESAFE_API_KEY");
	expect(reason).not.toContain("transcript");
});

test("finalize binds to the reviewed answer: draft B needs its own PASS", () => {
	const a = issueAuthorization("run-gate-1", "finalize", finalizeActionHash("draft A"));
	try {
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft B"))).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft A"))).toBe(true);
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft A"))).toBe(false);
	} finally {
		authorizationStore.delete(a.id);
	}
});
test("finalize requires ACCEPTs on all three roles, not just the final PASS", () => {
	const run = `run-roles-${Date.now()}`;
	const freeze = "freeze-roles-1";
	const mkExpected = (suffix: string) => ({
		stockbot: { roleJobId: `job-stock-${suffix}`, outputHash: hashAction({ t: `stock-${suffix}` }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: `job-bull-${suffix}`, outputHash: hashAction({ t: `bull-${suffix}` }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: `job-bear-${suffix}`, outputHash: hashAction({ t: `bear-${suffix}` }), freezeHash: hashAction(["e1"]) },
	});
	expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(false);
	const auths = (["stockbot", "bullbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, roleJobId: `job-${role}-x`, outputHash: hashAction({ t: role }), freezeHash: hashAction(["e1"]) }));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
	const exp = mkExpected("a");
	const all = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, roleJobId: exp[role].roleJobId, outputHash: exp[role].outputHash, freezeHash: exp[role].freezeHash }));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
	} finally {
		for (const a of all) authorizationStore.delete(a.id);
	}
});

test("identical task batch is an exact repeat after first execution", () => {
	const seen = new Set<string>();
	const batch = [{ agent: "sec-agent", task: "fetch ownership" }];
	const h = hashAction(batch);
	expect(isExactRepeat(seen, h)).toBe(false);
	seen.add(h);
	expect(isExactRepeat(seen, hashAction([{ agent: "sec-agent", task: "fetch ownership" }]))).toBe(true);
	expect(isExactRepeat(seen, hashAction([{ agent: "sec-agent", task: "fetch revenue" }]))).toBe(false);
});

test("role ACCEPTs are one-time: successful finalize consumes all three", () => {
	const run = `run-once-${Date.now()}`;
	const freeze = "freeze-once-1";
	const exp = {
		stockbot: { roleJobId: "job-stock-1", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-bull-1", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-bear-1", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...exp[role] }));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
		expect(consumeOpenRoleAccepts(authorizationStore, run, freeze, exp)).toBe(true);
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(false);
		expect(consumeOpenRoleAccepts(authorizationStore, run, freeze, exp)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});

test("new trio launch drains stale ACCEPTs from the prior run", () => {
	const run = `run-drain-${Date.now()}`;
	const freeze = "freeze-drain-1";
	const exp = {
		stockbot: { roleJobId: "job-stock-1", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-bull-1", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-bear-1", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...exp[role] }));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
		drainRoleAccepts(authorizationStore, run);
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});

test("stale ACCEPTs for a prior freeze do not authorize a later freeze", () => {
	const run = `run-stale-${Date.now()}`;
	const oldF = "freeze-old";
	const newF = "freeze-new";
	const oldExp = {
		stockbot: { roleJobId: "job-s", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-b", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-r", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: oldF, role, ...oldExp[role] }));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run, newF)).toBe(false);
		expect(consumeOpenRoleAccepts(authorizationStore, run, newF, { stockbot: { ...oldExp.stockbot }, bullbot: { ...oldExp.bullbot }, bearbot: { ...oldExp.bearbot } })).toBe(false);
		expect(hasOpenRoleAccepts(authorizationStore, run, oldF)).toBe(true);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});

test("mismatched output hash does not consume: partial sets burn nothing", () => {
	const run = `run-mismatch-${Date.now()}`;
	const freeze = "freeze-mismatch-1";
	const good = {
		stockbot: { roleJobId: "job-s", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-b", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-r", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...good[role] }));
	try {
		const wrong = { ...good, bearbot: { ...good.bearbot, outputHash: hashAction({ t: "tampered" }) } };
		expect(consumeOpenRoleAccepts(authorizationStore, run, freeze, wrong)).toBe(false);
		expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
		expect(consumeOpenRoleAccepts(authorizationStore, run, freeze, good)).toBe(true);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});

test("candidate-A auth does not authorize candidate-B", () => {
	const run = `run-cand-${Date.now()}`;
	const a = issueAuthorization(run, "continue_research", hashAction({ candidate: { q: "A" } }));
	try {
		expect(consumeMatchingAuth(authorizationStore, run, "continue_research", hashAction({ candidate: { q: "B" } }))).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, run, "continue_research", hashAction({ candidate: { q: "A" } }))).toBe(true);
	} finally {
		authorizationStore.delete(a.id);
	}
});

test("committee skip blocks finalize: committee_accepted is required", () => {
	const run = `run-commskip-${Date.now()}`;
	const freeze = "freeze-commskip-1";
	const exp = {
		stockbot: { roleJobId: "job-s", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-b", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-r", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const roles = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...exp[role] }));
	const fin = issueAuthorization(run, "finalize", finalizeActionHash("answer"));
	try {
		// No committee_accepted issued: the gate's committee consume must fail.
		expect(consumeMatchingAuth(authorizationStore, run, "committee_accepted", hashAction({ freezeId: freeze }))).toBe(false);
		const comm = issueAuthorization(run, "committee_accepted", hashAction({ freezeId: freeze }));
		try {
			expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
			expect(consumeMatchingAuth(authorizationStore, run, "committee_accepted", hashAction({ freezeId: freeze }))).toBe(true);
			expect(consumeMatchingAuth(authorizationStore, run, "finalize", finalizeActionHash("answer"))).toBe(true);
			expect(consumeOpenRoleAccepts(authorizationStore, run, freeze, exp)).toBe(true);
		} finally {
			authorizationStore.delete(comm.id);
		}
	} finally {
		for (const a of roles) authorizationStore.delete(a.id);
		authorizationStore.delete(fin.id);
	}
});

test("stale committee grades drained on relaunch do not finalize", () => {
	const run = `run-commdrain-${Date.now()}`;
	const freeze = "freeze-commdrain-1";
	const comm = issueAuthorization(run, "committee_accepted", hashAction({ freezeId: freeze }));
	try {
		expect(hasOpenAuth(authorizationStore, run, "committee_accepted")).toBe(true);
		drainCommitteeAccepts(authorizationStore, run);
		expect(hasOpenAuth(authorizationStore, run, "committee_accepted")).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, run, "committee_accepted", hashAction({ freezeId: freeze }))).toBe(false);
	} finally {
		authorizationStore.delete(comm.id);
	}
});

test("omitted run_id blocks every judge and review tool", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { runReviewTool } = await import("../.stockbot/omp/tools/output-review-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.evidence.map((id) => [id, { p_yes: 0.9, yes: y(0.9) }]));
	const fakeLoader = {
		evidence: async () => ({ objective: "o", claim: "c", evidence: { evidence_id: "e1" } }),
		claim: async () => ({ objective: "o", claim: "c", evidence_set: [], actionable: false }),
		coverage: async () => ({ objective: "o", branch_map: {}, coverage: {}, claim_states: [], actionable: false }),
		continuation: async () => ({ objective: "o", coverage: {}, open_questions: [], current_conclusions: [] }),
		candidate: async () => ({ objective: "o", gap: "g" }),
	};
	const out = await runJudgeTool("research_judge_evidence", { research_session_id: "s", freeze_id: "f", evidence_id: "e1" }, { evaluator: new FakeEvaluator(judgments), stateLoader: fakeLoader });
	expect(out?.isError).toBe(true);
	expect(out?.details.error).toBe("evidence_judgment_failed");
	// A model-supplied run_id is ignored: without the injected run context the tool still blocks.
	const spoofed = await runJudgeTool("research_judge_evidence", { research_session_id: "s", freeze_id: "f", evidence_id: "e1", run_id: "spoofed" }, { evaluator: new FakeEvaluator(judgments), stateLoader: fakeLoader });
	expect(spoofed?.isError).toBe(true);
	const roleJudgments = Object.fromEntries([...P.common, ...P.stockbot].map((id) => [id, { p_yes: 0.9, yes: y(0.9) }]));
	const role = await runReviewTool("research_review_role_output", { research_session_id: "s", freeze_id: "f", role_job_id: "j" }, {
		evaluator: new FakeEvaluator(roleJudgments),
		stateLoader: { role: async () => ({ role: "stockbot", objective: "o", evidence: [], output: { ok: 1 }, freezeId: "f", freezeEvidenceIds: [], freezeEvidenceHash: hashAction([]) }) },
	});
	expect(role?.isError).toBe(true);
	expect(role?.details.error).toBe("role_review_failed");
});

test("fabricated coverage cannot yield launch_committee", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.coverage.map((id) => [id, { p_yes: 0.9, yes: y(0.9) }]));
	// Loader throws (untrusted/missing record) -> blocked, no auth ever issued.
	const out = await runJudgeTool("research_judge_coverage", { research_session_id: "s", branch_map_id: "made-up" }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => "run-fabricated",
		stateLoader: { coverage: async () => { throw new Error("typesafe_untrusted_state"); } },
	});
	expect(out?.isError).toBe(true);
	expect(out?.details.authorization).toBeUndefined();
	expect(hasOpenAuth(authorizationStore, "run-fabricated", "launch_committee")).toBe(false);
});

test("retry after failure with identical text is allowed; only successful settles mark repeats", async () => {
	const SID = `rs:retry-${Date.now()}`;
	let status = "running";
	setResearchBridge(async (req: Record<string, unknown>) => {
		if (req.op === "research.session.inspect")
			return { result: { session: { session_id: SID, status: "researching", query: "q", evidence_ids: [], freeze_ids: [], committee_runs: [], final_result: null, current_wave: 1, targeted_question: "", targeted_domain: "" }, jobs: [{ job_id: "job:src-1", job_type: "source_agent", wave_id: 1, status }], pending_next_action: null, latest_freeze: null } };
		if (req.op === "research.job.start") return { result: { job_id: "job:src-1", session_id: SID, status: "running", wave_id: 1, job_type: "source_agent" } };
		if (req.op === "research.job.runtime") return { result: {} };
		if (req.op === "research.job.fail") return { result: {} };
		if (req.op === "research.job.cancel") return { result: {} };
		return { error: "unknown_op" };
	});
	const tasks = [{ agent: "sec-agent", task: "fetch ownership" }];
	const runA = `run-retry-${Date.now()}-a`;
	await resumeResearch(SID, runA);
	const h1 = await peekTaskFingerprint(runA, tasks);
	expect(h1.length).toBeGreaterThan(0);
	expect(isExactRepeat(new Set<string>(), h1)).toBe(false);
	await planTaskCall({ researchKey: runA, toolCallId: "call-retry-fail" }, { tasks });
	const failed = await recordTaskResult({ researchKey: runA, toolCallId: "call-retry-fail" }, { results: [{ exit_code: 1, stderr: "boom" }] });
	expect(failed.settledHashes).toEqual([]);
	expect(failed.settledAgents).toEqual([]);
	// Same text after failure: composite unchanged (no new evidence), so a
	// retry is a legitimate re-plan, not a blocked duplicate.
	const runB = `run-retry-${Date.now()}-b`;
	await resumeResearch(SID, runB);
	const h2 = await peekTaskFingerprint(runB, tasks);
	expect(h2).toBe(h1);
	await planTaskCall({ researchKey: runB, toolCallId: "call-retry-ok" }, { tasks });
	status = "completed";
	const ok = await recordTaskResult({ researchKey: runB, toolCallId: "call-retry-ok" }, { results: [{ exit_code: 0 }] });
	expect(ok.settledHashes).toEqual([h2]);
	expect(ok.settledAgents).toEqual(["sec-agent"]);
});

test("same task text with new evidence is not a repeat", async () => {
	const SID = `rs:evchange-${Date.now()}`;
	let evidence: string[] = [];
	setResearchBridge(async (req: Record<string, unknown>) => {
		if (req.op === "research.session.inspect")
			return { result: { session: { session_id: SID, status: "researching", query: "q", evidence_ids: evidence, freeze_ids: [], committee_runs: [], final_result: null, current_wave: 1, targeted_question: "", targeted_domain: "" }, jobs: [], pending_next_action: null, latest_freeze: null } };
		return { error: "unknown_op" };
	});
	const run = `run-evchange-${Date.now()}`;
	await resumeResearch(SID, run);
	const tasks = [{ agent: "sec-agent", task: "fetch ownership" }] as unknown as Record<string, unknown>[];
	const before = await peekTaskFingerprint(run, tasks);
	evidence = [`${SID}:ev:1`];
	const after = await peekTaskFingerprint(run, tasks);
	expect(after).not.toBe(before);
});
