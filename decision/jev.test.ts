// decision/jev.test.ts — typed JEV layer contracts. No live calls: stub
// SystemOneFn, temp dirs under os.tmpdir for askDecisions file writes.
//
// Routing: exclusive routing/category/explanation → choice (exactly one
// winner among mutually exclusive labels); independent propositions → noul
// (each id its own probability, no cross-id winner); ordinal judgments →
// score (never deterministic calcs).
import { expect, test } from "bun:test";
import { mkdtemp, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DISPOSITION_OPTIONS,
  EVIDENCE_STATE_OPTIONS,
  MATERIALITY_LEVELS,
  assertAcyclic,
  askDecisions,
  classifyProbability,
  parseChoiceAnswer,
  parseNoulAnswer,
  parseScoreAnswer,
  scopeNodeState,
} from "./jev.ts";

test("noul preserves probability exactly; policy stays in classifyProbability", () => {
  for (const p of [0, 0.5, 0.6999, 0.7, 0.85, 1]) {
    const d = parseNoulAnswer("n", "truth", { type: "noul", noul: p });
    expect(d).toEqual({ kind: "noul", probability: p });
  }
  // Boundary policy is not collapsed into the parse result.
  expect(classifyProbability(0.7)).toBe("yes");
  expect(classifyProbability(0.6999)).toBe("unsure");
  expect(classifyProbability(0.5)).toBe("unsure");
  expect(classifyProbability(0.4999)).toBe("no");
  for (const bad of [-0.1, 1.1, NaN, Infinity, -Infinity])
    expect(() => parseNoulAnswer("n", "truth", { type: "noul", noul: bad })).toThrow("invalid_probability");
  expect(() => parseNoulAnswer("n", "truth", { type: "choice", choice: "x" })).toThrow("malformed");
});

const dispProbs = { analyze: 0.7, gather_evidence: 0.2, reject: 0.1 };
const evProbs = {
  sufficient_support: 0.6,
  sufficient_contradiction: 0.1,
  conflicted: 0.2,
  insufficient: 0.1,
};

test("choice winner must be declared; distribution exactly matches options", () => {
  const d = parseChoiceAnswer("n", "id", {
    type: "choice",
    choice: "analyze",
    probabilities: dispProbs,
    confidence: 0.8,
  }, DISPOSITION_OPTIONS);
  expect(d).toEqual({ kind: "choice", choice: "analyze", probabilities: dispProbs, confidence: 0.8 });
  const e = parseChoiceAnswer("n", "id", {
    type: "choice",
    choice: "conflicted",
    probabilities: evProbs,
    confidence: 0.4,
  }, EVIDENCE_STATE_OPTIONS);
  expect(e.choice).toBe("conflicted");
  expect(e.confidence).toBe(0.4);
  // Unknown winner rejected.
  expect(() => parseChoiceAnswer("n", "id", {
    type: "choice", choice: "bogus", probabilities: dispProbs, confidence: 0.8,
  }, DISPOSITION_OPTIONS)).toThrow("malformed");
  // Key set must be exactly the declared options.
  const missing = { analyze: 0.9, gather_evidence: 0.1 };
  expect(() => parseChoiceAnswer("n", "id", {
    type: "choice", choice: "analyze", probabilities: missing, confidence: 0.8,
  }, DISPOSITION_OPTIONS)).toThrow("malformed");
  const extra = { ...dispProbs, other: 0 };
  expect(() => parseChoiceAnswer("n", "id", {
    type: "choice", choice: "analyze", probabilities: extra, confidence: 0.8,
  }, DISPOSITION_OPTIONS)).toThrow("malformed");
  // Values must be probabilities in [0,1]; confidence too.
  const outOfRange = { ...dispProbs, analyze: 1.5 };
  expect(() => parseChoiceAnswer("n", "id", {
    type: "choice", choice: "analyze", probabilities: outOfRange, confidence: 0.8,
  }, DISPOSITION_OPTIONS)).toThrow("malformed");
  expect(() => parseChoiceAnswer("n", "id", {
    type: "choice", choice: "analyze", probabilities: dispProbs, confidence: NaN,
  }, DISPOSITION_OPTIONS)).toThrow("malformed");
});

test("score keeps finite value; optional distribution/confidence/legend retained", () => {
  expect(parseScoreAnswer("n", "m", { type: "score", score: 3 })).toEqual({ kind: "score", score: 3 });
  const full = parseScoreAnswer("n", "m", {
    type: "score",
    score: 4,
    probabilities: { "0": 0.1, "4": 0.9 },
    confidence: 0.75,
    legend: MATERIALITY_LEVELS,
  });
  expect(full.score).toBe(4);
  expect(full.probabilities).toEqual({ "0": 0.1, "4": 0.9 });
  expect(full.confidence).toBe(0.75);
  expect(full.raw).toEqual(MATERIALITY_LEVELS);
  for (const bad of [
    { type: "score" },
    { type: "score", score: NaN },
    { type: "score", score: Infinity },
    { type: "score", score: "high" },
    { type: "score", score: 2, confidence: "high" },
    { type: "score", score: 2, probabilities: { "2": NaN } },
    { type: "score", score: 2, confidence: 1.5 },
    { type: "score", score: 2, probabilities: { "2": 1.5 } },
  ]) expect(() => parseScoreAnswer("n", "m", bad)).toThrow("malformed");
  expect(() => parseScoreAnswer("n", "m", { type: "score", score: 100 }, 4)).toThrow("malformed");
  expect(() => parseScoreAnswer("n", "m", { type: "score", score: -1 }, 4)).toThrow("malformed");
  expect(parseScoreAnswer("n", "m", { type: "score", score: 3.5 }, 4)).toEqual({ kind: "score", score: 3.5 });
});

test("mixed askDecisions parses noul+choice+score from one shared state", async () => {
  const d = await mkdtemp(join(tmpdir(), "jev-test-"));
  const questions = {
    truth: { type: "noul", instruction: "Do the facts support it?" },
    evidence_state: { type: "choice", instruction: "Evidence state?", criteria: EVIDENCE_STATE_OPTIONS },
    materiality: { type: "score", instruction: "How material?", levels: MATERIALITY_LEVELS },
  };
  const systemOne = async () => ({
    answers: {
      truth: { type: "noul", noul: 0.8 },
      evidence_state: { type: "choice", choice: "sufficient_support", probabilities: evProbs, confidence: 0.7 },
      materiality: { type: "score", score: 3, confidence: 0.6, legend: MATERIALITY_LEVELS },
    },
  });
  const { raw, decisions } = await askDecisions(
    d, "mixed", { state: { objective: "o" }, questions }, systemOne,
    { evidence_state: EVIDENCE_STATE_OPTIONS },
  );
  expect(decisions.truth).toEqual({ kind: "noul", probability: 0.8 });
  expect(decisions.evidence_state?.kind).toBe("choice");
  expect(decisions.materiality?.kind).toBe("score");
  // Out-of-range score against the shared rubric is rejected.
  const oor = async () => ({
    answers: {
      truth: { type: "noul", noul: 0.8 },
      evidence_state: { type: "choice", choice: "sufficient_support", probabilities: evProbs, confidence: 0.7 },
      materiality: { type: "score", score: 100, confidence: 0.6 },
    },
  });
  let oorErr: unknown;
  try {
    await askDecisions(d, "oor", { state: { objective: "o" }, questions }, oor, {
      evidence_state: EVIDENCE_STATE_OPTIONS,
    });
  } catch (e) {
    oorErr = e;
  }
  expect(String(oorErr)).toContain("malformed");
  expect(raw).toHaveProperty("answers");
  const files = await readdir(d);
  for (const f of ["request-mixed.json", "raw-mixed.json", "policy-mixed.json"])
    expect(files).toContain(f);
  const mismatch = async () => ({ answers: { truth: { type: "noul", noul: 0.5 } } });
  let shortErr: unknown;
  try {
    await askDecisions(d, "short", { state: {}, questions }, mismatch);
  } catch (e) {
    shortErr = e;
  }
  expect(String(shortErr)).toContain("do not match");
  const extra = async () => ({
    answers: {
      truth: { type: "noul", noul: 0.5 },
      evidence_state: { type: "choice", choice: "insufficient", probabilities: evProbs, confidence: 0.5 },
      materiality: { type: "score", score: 1 },
      stowaway: { type: "noul", noul: 0.5 },
    },
  });
  let longErr: unknown;
  try {
    await askDecisions(d, "long", { state: {}, questions }, extra, { evidence_state: EVIDENCE_STATE_OPTIONS });
  } catch (e) {
    longErr = e;
  }
  expect(String(longErr)).toContain("do not match");
});

test("assertAcyclic rejects self and indirect cycles, accepts diamonds", () => {
  expect(() => assertAcyclic("s", [{ id: "A", dependsOn: ["A"] }], () => undefined)).toThrow(
    "depends on itself",
  );
  expect(() => assertAcyclic(
    "s",
    [{ id: "A", dependsOn: ["B"] }, { id: "B", dependsOn: ["A"] }],
    () => undefined,
  )).toThrow("cyclic");
  expect(() => assertAcyclic(
    "s",
    [{ id: "A", dependsOn: ["B"] }, { id: "B", dependsOn: ["C"] }, { id: "C", dependsOn: ["A"] }],
    () => undefined,
  )).toThrow("cyclic");
  // Diamond passes.
  assertAcyclic(
    "s",
    [
      { id: "A", dependsOn: ["B", "C"] },
      { id: "B", dependsOn: ["D"] },
      { id: "C", dependsOn: ["D"] },
      { id: "D", dependsOn: [] },
    ],
    () => undefined,
  );
  // Prior-deps callback is consulted: unseen dep resolves through it.
  const seen: string[] = [];
  assertAcyclic("s", [{ id: "A", dependsOn: ["B"] }], (id) => {
    seen.push(id);
    return [];
  });
  expect(seen).toContain("B");
  // Cycle reachable only via prior still throws.
  expect(() => assertAcyclic("s", [{ id: "A", dependsOn: ["B"] }], (id) =>
    id === "B" ? ["A"] : [],
  )).toThrow("cyclic");
});

test("scopeNodeState projects only allowed keys; evidence must be an array", () => {
  const minimal = scopeNodeState({ objective: "o", proposal: "p", evidence: [] }) as Record<string, unknown>;
  expect(Object.keys(minimal).sort()).toEqual(["evidence", "objective", "proposal"]);
  const full = scopeNodeState({
    objective: "o",
    proposal: "p",
    analysis: "a",
    evidence: [{ id: "e1" }],
    dependencyDecisions: [{ proposalId: "q", disposition: "analyze" }],
  }) as Record<string, unknown>;
  expect(Object.keys(full).sort()).toEqual(
    ["analysis", "dependencyDecisions", "evidence", "objective", "proposal"],
  );
  // Nothing smuggled in (e.g. evaluationCriteria) passes through.
  const smuggled = {
    objective: "o",
    proposal: "p",
    evidence: [],
    evaluationCriteria: { foo: 1 },
  } as unknown as Parameters<typeof scopeNodeState>[0];
  expect(scopeNodeState(smuggled) as Record<string, unknown>).not.toHaveProperty("evaluationCriteria");
  expect(() =>
    scopeNodeState({ objective: "o", proposal: "p", evidence: "e1" as unknown as unknown[] }),
  ).toThrow("evidence must be an array");
});
