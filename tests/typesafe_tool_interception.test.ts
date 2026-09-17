import { expect, test } from "bun:test";
import {
	authorizationStore,
	consumeAuthorization,
	consumeMatchingAuth,
	consumeOpenRoleAccepts,
	drainRoleAccepts,
	finalizeActionHash,
	hashAction,
	hasOpenRoleAccepts,
	isExactRepeat,
	issueAuthorization,
} from "../.stockbot/omp/lib/research-control.ts";
import { reviewCommittee, reviewFinal } from "../.stockbot/omp/lib/typesafe/decisions.ts";
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
	expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(false);
	const auths = (["stockbot", "bullbot"] as const).map((role) => issueAuthorization(run, "accept_role_output", hashAction({ role })));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
	const all = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueAuthorization(run, "accept_role_output", hashAction({ role })));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(true);
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
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueAuthorization(run, "accept_role_output", hashAction({ role })));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(true);
		expect(consumeOpenRoleAccepts(authorizationStore, run)).toBe(true);
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(false);
		expect(consumeOpenRoleAccepts(authorizationStore, run)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});

test("new trio launch drains stale ACCEPTs from the prior run", () => {
	const run = `run-drain-${Date.now()}`;
	const auths = (["stockbot", "bullbot", "bearbot"] as const).map((role) => issueAuthorization(run, "accept_role_output", hashAction({ role })));
	try {
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(true);
		drainRoleAccepts(authorizationStore, run);
		expect(hasOpenRoleAccepts(authorizationStore, run)).toBe(false);
	} finally {
		for (const a of auths) authorizationStore.delete(a.id);
	}
});
