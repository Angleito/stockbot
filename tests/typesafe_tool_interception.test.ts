import { expect, test } from "bun:test";
import {
	authorizationStore,
	candidateTaskHash,
	consumeAuthorization,
	consumeCandidateBatch,
	consumeFinalizeBundle,
	consumeLaunchForFreeze,
	consumeMatchingAuth,
	consumeOpenRoleAccepts,
	drainCommitteeAccepts,
	drainFinalize,
	drainRoleAccepts,
	finalizeActionHash,
	hashAction,
	hasOpenAuth,
	hasOpenRoleAccepts,
	isExactRepeat,
	issueAuthorization,
	issueRoleAccept,
	launchCommitteeHash,
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

test("ledger-only: committee launch before coverage completes is blocked", async () => {
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
	const ctx = { sessionId: "sess-a", freezeId: "freeze-a", stockbotHash: hashAction({ t: "s" }), bullbotHash: hashAction({ t: "b" }), bearbotHash: hashAction({ t: "r" }) };
	const a = issueAuthorization("run-gate-1", "finalize", finalizeActionHash("draft A", ctx));
	try {
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft B", ctx))).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft A", ctx))).toBe(true);
		expect(consumeMatchingAuth(authorizationStore, "run-gate-1", "finalize", finalizeActionHash("draft A", ctx))).toBe(false);
	} finally {
		authorizationStore.delete(a.id);
	}
});

test("v1-final approval never authorizes the v2 trio", () => {
	const run = `run-v12-${Date.now()}`;
	const freeze = "freeze-v12-1";
	const mkCtx = (suffix: string) => ({ sessionId: "sess-v12", freezeId: freeze, stockbotHash: hashAction({ t: `s-${suffix}` }), bullbotHash: hashAction({ t: "b" }), bearbotHash: hashAction({ t: "r" }) });
	const a = issueAuthorization(run, "finalize", finalizeActionHash("answer", mkCtx("v1")));
	try {
		expect(consumeMatchingAuth(authorizationStore, run, "finalize", finalizeActionHash("answer", mkCtx("v2")))).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, run, "finalize", finalizeActionHash("answer", mkCtx("v1")))).toBe(true);
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
	const task = "fetch exhibits for gap g";
	const a = issueAuthorization(run, "continue_research", candidateTaskHash({ q: "A" }, task));
	try {
		expect(consumeCandidateBatch(authorizationStore, run, [candidateTaskHash({ q: "B" }, task)])).toBe(false);
		expect(consumeCandidateBatch(authorizationStore, run, [candidateTaskHash({ q: "A" }, task)])).toBe(true);
	} finally {
		authorizationStore.delete(a.id);
	}
});

test("candidate+task swap is rejected: same candidate, different task", () => {
	const run = `run-candtask-${Date.now()}`;
	const candidate = { q: "A" };
	const a = issueAuthorization(run, "continue_research", candidateTaskHash(candidate, "fetch exhibits"));
	try {
		expect(consumeCandidateBatch(authorizationStore, run, [candidateTaskHash(candidate, "fetch revenue instead")])).toBe(false);
		expect(consumeCandidateBatch(authorizationStore, run, [candidateTaskHash(candidate, "fetch exhibits")])).toBe(true);
	} finally {
		authorizationStore.delete(a.id);
	}
});

test("candidate batch partial failure burns nothing", () => {
	const run = `run-candbatch-${Date.now()}`;
	const good = candidateTaskHash({ q: "A" }, "task-a");
	const a = issueAuthorization(run, "continue_research", good);
	try {
		expect(consumeCandidateBatch(authorizationStore, run, [good, candidateTaskHash({ q: "B" }, "task-b")])).toBe(false);
		expect(consumeCandidateBatch(authorizationStore, run, [good])).toBe(true);
	} finally {
		authorizationStore.delete(a.id);
	}
});

test("committee skip blocks finalize: committee_accepted is required", () => {
	const run = `run-commskip-${Date.now()}`;
	const sessionId = "sess-commskip";
	const freeze = "freeze-commskip-1";
	const exp = {
		stockbot: { roleJobId: "job-s", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-b", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-r", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const ctx = { sessionId, freezeId: freeze, stockbotHash: exp.stockbot.outputHash, bullbotHash: exp.bullbot.outputHash, bearbotHash: exp.bearbot.outputHash };
	const committeeHash = hashAction({ sessionId, freezeId: freeze });
	const finalizeHash = finalizeActionHash("answer", ctx);
	const roles = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...exp[role] }));
	const fin = issueAuthorization(run, "finalize", finalizeHash);
	try {
		// No committee_accepted issued: the bundle consume must fail and burn nothing.
		expect(consumeFinalizeBundle(authorizationStore, { runId: run, freezeId: freeze, expected: exp, committeeHash, finalizeHash })).toBe(false);
		const comm = issueAuthorization(run, "committee_accepted", committeeHash);
		try {
			expect(hasOpenRoleAccepts(authorizationStore, run, freeze)).toBe(true);
			expect(consumeFinalizeBundle(authorizationStore, { runId: run, freezeId: freeze, expected: exp, committeeHash, finalizeHash })).toBe(true);
		} finally {
			authorizationStore.delete(comm.id);
		}
	} finally {
		for (const a of roles) authorizationStore.delete(a.id);
		authorizationStore.delete(fin.id);
	}
});

test("finalize bundle trio mismatch burns nothing: committee and finalize stay open", () => {
	const run = `run-bundle-${Date.now()}`;
	const sessionId = "sess-bundle";
	const freeze = "freeze-bundle-1";
	const good = {
		stockbot: { roleJobId: "job-s", outputHash: hashAction({ t: "s" }), freezeHash: hashAction(["e1"]) },
		bullbot: { roleJobId: "job-b", outputHash: hashAction({ t: "b" }), freezeHash: hashAction(["e1"]) },
		bearbot: { roleJobId: "job-r", outputHash: hashAction({ t: "r" }), freezeHash: hashAction(["e1"]) },
	};
	const goodCtx = { sessionId, freezeId: freeze, stockbotHash: good.stockbot.outputHash, bullbotHash: good.bullbot.outputHash, bearbotHash: good.bearbot.outputHash };
	const committeeHash = hashAction({ sessionId, freezeId: freeze });
	const goodFinalize = finalizeActionHash("answer", goodCtx);
	const roles = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueRoleAccept(run, { runId: run, freezeId: freeze, role, ...good[role] }));
	const comm = issueAuthorization(run, "committee_accepted", committeeHash);
	const fin = issueAuthorization(run, "finalize", goodFinalize);
	try {
		const wrong = { ...good, bearbot: { ...good.bearbot, outputHash: hashAction({ t: "tampered" }) } };
		const wrongCtx = { sessionId, freezeId: freeze, stockbotHash: wrong.stockbot.outputHash, bullbotHash: wrong.bullbot.outputHash, bearbotHash: wrong.bearbot.outputHash };
		expect(consumeFinalizeBundle(authorizationStore, { runId: run, freezeId: freeze, expected: wrong, committeeHash, finalizeHash: finalizeActionHash("answer", wrongCtx) })).toBe(false);
		expect(hasOpenAuth(authorizationStore, run, "committee_accepted")).toBe(true);
		expect(hasOpenAuth(authorizationStore, run, "finalize")).toBe(true);
		expect(consumeFinalizeBundle(authorizationStore, { runId: run, freezeId: freeze, expected: good, committeeHash, finalizeHash: goodFinalize })).toBe(true);
	} finally {
		for (const a of roles) authorizationStore.delete(a.id);
		authorizationStore.delete(comm.id);
		authorizationStore.delete(fin.id);
	}
});

test("stale committee grades drained on relaunch do not finalize", () => {
	const run = `run-commdrain-${Date.now()}`;
	const comm = issueAuthorization(run, "committee_accepted", hashAction({ sessionId: "s", freezeId: "freeze-commdrain-1" }));
	try {
		expect(hasOpenAuth(authorizationStore, run, "committee_accepted")).toBe(true);
		drainCommitteeAccepts(authorizationStore, run);
		expect(hasOpenAuth(authorizationStore, run, "committee_accepted")).toBe(false);
		expect(consumeMatchingAuth(authorizationStore, run, "committee_accepted", hashAction({ sessionId: "s", freezeId: "freeze-commdrain-1" }))).toBe(false);
	} finally {
		authorizationStore.delete(comm.id);
	}
});

test("allowed committee launch drains stale finalize grades", () => {
	const run = `run-findrain-${Date.now()}`;
	const fin = issueAuthorization(run, "finalize", finalizeActionHash("draft", { sessionId: "s", freezeId: "f", stockbotHash: hashAction({ t: "s" }), bullbotHash: hashAction({ t: "b" }), bearbotHash: hashAction({ t: "r" }) }));
	try {
		expect(hasOpenAuth(authorizationStore, run, "finalize")).toBe(true);
		drainFinalize(authorizationStore, run);
		expect(hasOpenAuth(authorizationStore, run, "finalize")).toBe(false);
	} finally {
		authorizationStore.delete(fin.id);
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
		coverage: async () => ({ objective: "o", branch_map: {}, coverage: {}, claim_states: [], actionable: false, dossierId: "d-1" }),
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
	const out = await runJudgeTool("research_judge_coverage", { research_session_id: "s", freeze_id: "f" }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => "run-fabricated",
		stateLoader: { coverage: async () => { throw new Error("typesafe_untrusted_state"); } },
	});
	expect(out?.isError).toBe(true);
	expect(out?.details.authorization).toBeUndefined();
	expect(hasOpenAuth(authorizationStore, "run-fabricated", "launch_committee")).toBe(false);
});

test("coverage COMPLETE issues launch_committee bound to session/freeze/dossier", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.coverage.map((id) => [id, { p_yes: 0.9, yes: y(0.9) }]));
	const run = `run-covbind-${Date.now()}`;
	const branchMap = { roots: ["ownership"] };
	const coverage = { ownership: "searched" };
	const out = await runJudgeTool("research_judge_coverage", { research_session_id: "sess-cov", freeze_id: "freeze-cov", branch_map_id: "stale-dossier" }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => run,
		stateLoader: {
			coverage: async () => ({ objective: "o", branch_map: branchMap, coverage, claim_states: [], actionable: true, dossierId: "dossier-1" }),
			freeze: async () => { },
		},
	});
	try {
		expect(out?.isError).toBeUndefined();
		expect(out?.details.verdict).toBe("COMPLETE");
		const auth = out?.details.authorization as { authorization_id?: string } | undefined;
		expect(typeof auth?.authorization_id).toBe("string");
		const stored = authorizationStore.get(String(auth?.authorization_id));
		expect(stored?.kind).toBe("launch_committee");
		const parsed = JSON.parse(String(stored?.actionHash)) as Record<string, unknown>;
		expect(parsed.sessionId).toBe("sess-cov");
		expect(parsed.freezeId).toBe("freeze-cov");
		expect(parsed.dossierId).toBe("dossier-1");
		expect(parsed.coverageHash).toBe(hashAction({ branch_map: branchMap, coverage }));
		expect(consumeLaunchForFreeze(authorizationStore, run, { sessionId: "sess-cov", freezeId: "freeze-cov", dossierId: "dossier-1", coverageHash: hashAction({ branch_map: branchMap, coverage }) })).toBe(true);
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
	}
});

test("coverage with a middle-V failure issues no launch_committee", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.coverage.map((id) => [id, { p_yes: id === "V09" ? 0.2 : 0.9, yes: y(id === "V09" ? 0.2 : 0.9) }]));
	const run = `run-covmid-${Date.now()}`;
	const out = await runJudgeTool("research_judge_coverage", { research_session_id: "sess-cov", freeze_id: "freeze-cov", branch_map_id: "stale-dossier" }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => run,
		stateLoader: {
			coverage: async () => ({ objective: "o", branch_map: {}, coverage: {}, claim_states: [], actionable: true, dossierId: "dossier-1" }),
			freeze: async () => { },
		},
	});
	try {
		expect(out?.details.verdict).not.toBe("COMPLETE");
		expect(out?.details.authorization).toBeUndefined();
		expect(hasOpenAuth(authorizationStore, run, "launch_committee")).toBe(false);
	} finally {
		for (const [id, a] of authorizationStore) if (a.runId === run) authorizationStore.delete(id);
	}
});

test("continuation continue carries no authorization", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.continuation.map((id) => [id, { p_yes: ["N01", "N02", "N03", "N04"].includes(id) ? 0.9 : 0.2, yes: y(["N01", "N02", "N03", "N04"].includes(id) ? 0.9 : 0.2) }]));
	const run = `run-contnoauth-${Date.now()}`;
	const out = await runJudgeTool("research_judge_continuation", { research_session_id: "s" }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => run,
		stateLoader: { continuation: async () => ({ objective: "o", coverage: {}, open_questions: [], current_conclusions: [] }) },
	});
	expect(out?.details.decision).toBe("continue");
	expect(out?.details.authorization).toBeUndefined();
	expect(hasOpenAuth(authorizationStore, run, "continue_research")).toBe(false);
});

test("candidate without task is blocked and issues no authorization", async () => {
	const { PACKS: P } = await import("../.stockbot/omp/lib/typesafe/questions.ts");
	const { runJudgeTool } = await import("../.stockbot/omp/tools/research-judge-tools.ts");
	const { FakeEvaluator } = await import("../.stockbot/omp/lib/typesafe/evaluator.ts");
	const { yes: y } = await import("../.stockbot/omp/lib/typesafe/thresholds.ts");
	const judgments = Object.fromEntries(P.candidate.map((id) => [id, { p_yes: 0.9, yes: y(0.9) }]));
	const run = `run-candnotask-${Date.now()}`;
	const out = await runJudgeTool("research_judge_candidate", { research_session_id: "s", gap_id: "g", candidate: { q: "A" } }, {
		evaluator: new FakeEvaluator(judgments),
		getRunId: () => run,
		stateLoader: { candidate: async () => ({ objective: "o", gap: "g" }) },
	});
	expect(out?.isError).toBe(true);
	expect(out?.details.authorization).toBeUndefined();
	expect(hasOpenAuth(authorizationStore, run, "continue_research")).toBe(false);
});

test("launch bound to session/freeze/dossier/coverage: swaps do not consume", () => {
	const run = `run-launchbind-${Date.now()}`;
	const covHash = hashAction({ branch_map: {}, coverage: {} });
	const a = issueAuthorization(run, "launch_committee", launchCommitteeHash({ sessionId: "sess-1", freezeId: "freeze-1", dossierId: "d1", coverageHash: covHash }));
	const good = { sessionId: "sess-1", freezeId: "freeze-1", dossierId: "d1", coverageHash: covHash };
	try {
		expect(consumeLaunchForFreeze(authorizationStore, run, { ...good, freezeId: "freeze-2" })).toBe(false);
		expect(consumeLaunchForFreeze(authorizationStore, run, { ...good, dossierId: "d2" })).toBe(false);
		expect(consumeLaunchForFreeze(authorizationStore, run, { ...good, coverageHash: hashAction({ branch_map: {}, coverage: { other: 1 } }) })).toBe(false);
		expect(consumeLaunchForFreeze(authorizationStore, run, good)).toBe(true);
	} finally {
		authorizationStore.delete(a.id);
	}
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
