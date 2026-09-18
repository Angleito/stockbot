import { expect, test } from "bun:test";
import { PACKS, QUESTION_BANK, QUESTION_BANK_VERSION } from "../.stockbot/omp/lib/typesafe/questions.ts";
import type { Phase } from "../.stockbot/omp/lib/typesafe/types.ts";

const SIZES: Record<string, number> = {
	evidence: 16,
	claim: 16,
	coverage: 20,
	continuation: 6,
	candidate: 6,
	common: 18,
	stockbot: 6,
	bull: 6,
	bear: 6,
	committee: 8,
	final: 4,
	final_extended: 16,
};

test("bank version is 2", () => {
	expect(QUESTION_BANK_VERSION).toBe("2");
});

test("pack sizes match the plan", () => {
	for (const [pack, n] of Object.entries(SIZES)) expect(PACKS[pack]).toHaveLength(n);
});

test("bank holds >=100 distinct questions covering every pack id", () => {
	expect(Object.keys(QUESTION_BANK).length).toBeGreaterThanOrEqual(100);
	for (const pack of Object.values(PACKS)) for (const id of pack) expect(QUESTION_BANK[id]).toBeDefined();
});

test("every entry carries phase/kind/critical with a non-empty instruction", () => {
	for (const [id, q] of Object.entries(QUESTION_BANK)) {
		expect(q.id).toBe(id);
		expect(q.instruction.length).toBeGreaterThan(0);
		expect(["pass_when_yes", "trigger_when_yes"]).toContain(q.kind);
		expect(typeof q.critical).toBe("boolean");
	}
});

test("phases match pack membership", () => {
	const phaseOf: Record<string, Phase> = {
		evidence: "evidence",
		claim: "claim",
		coverage: "coverage",
		continuation: "continuation",
		candidate: "candidate",
		common: "role_output",
		stockbot: "role_output",
		bull: "role_output",
		bear: "role_output",
		committee: "committee",
		final: "final",
	};
	for (const [pack, phase] of Object.entries(phaseOf)) {
		for (const id of PACKS[pack]) expect(QUESTION_BANK[id].phase).toBe(phase);
	}
});

test("critical sets match the plan", () => {
	const crit = (ids: readonly string[]) => ids.filter((id) => QUESTION_BANK[id].critical).sort();
	expect(crit(PACKS.evidence)).toEqual(["E01", "E02", "E14", "E16"]);
	expect(crit(PACKS.claim)).toEqual(["C01", "C02", "C03", "C06", "C13"]);
	expect(crit(PACKS.coverage)).toEqual(["V01", "V19", "V20"]);
	expect(crit(PACKS.continuation)).toHaveLength(6);
	expect(crit(PACKS.candidate)).toEqual(["N07", "N08", "N09", "N10", "N12"]);
	expect(crit(PACKS.common)).toEqual([
		"Q01", "Q02", "Q03", "Q04", "Q05", "Q06", "Q07", "Q13", "Q14", "Q15", "Q18",
	]);
	for (const pack of [PACKS.stockbot, PACKS.bull, PACKS.bear]) {
		expect(pack.filter((id) => !QUESTION_BANK[id].critical)).toEqual([]);
	}
	expect(crit(PACKS.committee)).toEqual(["F01", "F02", "F03", "F04", "F05", "F06", "F07"]);
	expect(crit(PACKS.final)).toHaveLength(4);
});

test("trigger kinds match the plan", () => {
	const trig = (ids: readonly string[]) =>
		ids.filter((id) => QUESTION_BANK[id].kind === "trigger_when_yes").sort();
	expect(trig(PACKS.claim)).toEqual(["C02", "C06", "C08"]);
	expect(trig(PACKS.continuation)).toEqual(["N01", "N02", "N03", "N04"]);
	for (const pack of [
		PACKS.evidence, PACKS.coverage, PACKS.candidate, PACKS.common,
		PACKS.stockbot, PACKS.bull, PACKS.bear, PACKS.committee, PACKS.final,
	]) {
		expect(trig([...pack])).toEqual([]);
	}
});

test("spot wording probes (bank owns the strings)", () => {
	expect(QUESTION_BANK["E01"].instruction).toContain("directly bears on the claim");
	expect(QUESTION_BANK["C01"].instruction).toContain("materially supports");
	expect(QUESTION_BANK["F12"].instruction).toContain("without hiding material evidence");
});
