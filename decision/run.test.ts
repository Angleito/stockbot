// Focused tests for decision/run.ts. No live calls: OpenCode fetch and the real
// TypeSafe SDK adapter (with stub fetch) are exercised. Each run writes to a temp
// dir; no .env reads.
import { expect, test } from "bun:test";
import { mkdtemp, readdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { classifyProbability, createSystemOne, runScenario } from "./run.ts";
import { resetTypeSafeClientForTest } from "../.stockbot/omp/lib/typesafe/client.ts";
import { scenarios } from "./scenarios.ts";

const OBJ = scenarios[0].objective.id;
const P = (suffix: string) => `${OBJ}-${suffix}`;

test("classifyProbability boundaries match approved policy", () => {
  expect(classifyProbability(1)).toBe("yes");
  expect(classifyProbability(0.7)).toBe("yes");
  expect(classifyProbability(0.6999)).toBe("unsure");
  expect(classifyProbability(0.5)).toBe("unsure");
  expect(classifyProbability(0.4999)).toBe("no");
  expect(classifyProbability(0)).toBe("no");
  for (const bad of [NaN, -0.1, 1.1, Infinity]) expect(() => classifyProbability(bad)).toThrow("invalid_probability");
});

// Lockstep helper: Responses API payload shape consumed by parseOpenCodeOutput.
function responsesPayload(obj: unknown): Response {
  return new Response(
    JSON.stringify({ output: [{ type: "message", content: [{ type: "output_text", text: JSON.stringify(obj) }] }] }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

const PROPOSALS = [
  { id: P("repayment"), objectiveId: OBJ, question: "Does repayment history support the loan?", dependsOn: [], whyItMatters: "Repayment predicts default risk." },
  { id: P("collateral"), objectiveId: OBJ, question: "Is the accelerator collateral adequate?", dependsOn: [P("repayment")], whyItMatters: "Collateral bounds loss given default." },
  { id: P("lunch"), objectiveId: OBJ, question: "Where does the CEO eat lunch?", dependsOn: [], whyItMatters: "Fictional control: irrelevant." },
];

async function successDir(secret: string): Promise<string> {
  process.env.OPENCODE_API_KEY = secret;
  process.env.OPENCODE_MODEL = "test-model";
  const prevTypesafeKey = process.env.TYPESAFE_API_KEY;
  process.env.TYPESAFE_API_KEY = "sk-test-typesafe-dummy";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const calls: string[] = [];
  const fetchFn = (async () => {
    const step = calls.length;
    calls.push(step === 0 ? "decompose" : step === 1 ? "analyze" : "expand");
    if (step === 0) return responsesPayload({ proposals: PROPOSALS });
    if (step === 1) {
      return responsesPayload({
        analyses: [
          { nodeId: P("repayment"), objectiveId: OBJ, interpretation: "Repayment history is strong.", evidenceRefs: ["s1-ev3"] },
        ],
        evidenceRequests: [
          { nodeId: P("collateral"), objectiveId: OBJ, missingEvidence: "Independent valuation of the accelerator units." },
        ],
      });
    }
    return responsesPayload({
      proposals: [
        { id: P("packwell"), objectiveId: OBJ, question: "What if Packwell packaging fails?", dependsOn: [P("collateral")], whyItMatters: "Indirect exposure via sole supplier." },
      ],
      evidenceRequests: [
        { nodeId: P("packwell"), objectiveId: OBJ, missingEvidence: "Packwell supply terms." },
      ],
    });
  }) as unknown as typeof fetch;
  const probs: Record<string, number> = {
    [P("repayment")]: 0.85, [P("collateral")]: 0.6, [P("lunch")]: 0.1, [P("packwell")]: 0.8,
  };
  const systemOneFetch = (async (url: unknown, init?: { body?: unknown }) => {
    const body = JSON.parse(String((init?.body as string) ?? "{}")) as { questions?: Record<string, unknown> };
    const answers = Object.fromEntries(
      Object.keys(body.questions ?? {}).map((id) => [
        id, { type: "noul", noul: probs[id] ?? 0.9, instructions: "stubbed" },
      ]),
    );
    return new Response(JSON.stringify({ answers, model: "stub-jev" }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  const systemOne = createSystemOne(systemOneFetch);
  let out: string;
  try {
    out = await runScenario(scenarios[0].id, { fetchFn, systemOne, outDir: dir });
  } finally {
    resetTypeSafeClientForTest();
    if (prevTypesafeKey === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = prevTypesafeKey;
  }
  expect(out).toBe(dir);
  return dir;
}

test("full sequence: relevance, analysis, adjudication, expansion, artifacts", async () => {
  const dir = await successDir("sk-test-success-key");
  const final = JSON.parse(await readFile(join(dir, "final.json"), "utf8"));
  expect(final.proposals.map((p: { id: string }) => p.id)).toEqual(
    [P("repayment"), P("collateral"), P("lunch"), P("packwell")],
  );
  expect(final.proposals.map((p: { decision: string }) => p.decision)).toEqual(["yes", "unsure", "no", "yes"]);
  expect(final.reasoning).toHaveLength(1);
  expect(final.reasoning[0].nodeId).toBe(P("repayment"));
  expect(final.reasoning[0].decision).toBe("yes");
  expect(final.unresolved).toEqual([P("collateral")]);
  expect(final.evidenceRequests).toHaveLength(2);
  expect(final.jev.relevance).toBeDefined();
  expect(final.policy.decide[P("repayment")].decision).toBe("yes");
  // evaluationCriteria never sent: request bodies carry no such field.
  for (const name of ["request-decompose.json", "request-relevance.json", "request-analyze.json", "request-decide.json", "request-expand.json", "request-relevance-expanded.json"]) {
    const body = await readFile(join(dir, name), "utf8");
    expect(body).not.toContain("evaluationCriteria");
  }
  const evalCtx = JSON.parse(await readFile(join(dir, "eval-context.json"), "utf8"));
  expect(evalCtx.evaluationCriteria).toEqual(scenarios[0].evaluationCriteria);
});

test("unsure proposal analysis is rejected", async () => {
  process.env.OPENCODE_API_KEY = "sk-test-unsure-analysis-key";
  const prevTypesafeKey = process.env.TYPESAFE_API_KEY;
  process.env.TYPESAFE_API_KEY = "sk-test-typesafe-dummy";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const calls: string[] = [];
  const fetchFn = (async () => {
    const step = calls.length;
    calls.push(step === 0 ? "decompose" : "analyze");
    if (step === 0) return responsesPayload({ proposals: PROPOSALS });
    return responsesPayload({
      analyses: [
        { nodeId: P("collateral"), objectiveId: OBJ, interpretation: "Must not analyze unsure ids.", evidenceRefs: ["s1-ev2"] },
      ],
      evidenceRequests: [
        { nodeId: P("collateral"), objectiveId: OBJ, missingEvidence: "Independent valuation of the accelerator units." },
      ],
    });
  }) as unknown as typeof fetch;
  const probs: Record<string, number> = { [P("repayment")]: 0.85, [P("collateral")]: 0.6, [P("lunch")]: 0.1 };
  const systemOneFetch = (async (_url: unknown, init?: { body?: unknown }) => {
    const body = JSON.parse(String((init?.body as string) ?? "{}")) as { questions?: Record<string, unknown> };
    return new Response(JSON.stringify({
      answers: Object.fromEntries(Object.keys(body.questions ?? {}).map((id) => [id, { type: "noul", noul: probs[id] ?? 0.9 }])),
      model: "stub-jev",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
  let err: unknown = null;
  try {
    await runScenario(scenarios[0].id, { fetchFn, systemOne: createSystemOne(systemOneFetch), outDir: dir });
  } catch (e) {
    err = e;
  } finally {
    resetTypeSafeClientForTest();
    if (prevTypesafeKey === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = prevTypesafeKey;
  }
  expect(String(err)).toContain("must not be analyzed");
});

test("unsure proposal without missing evidence is rejected", async () => {
  process.env.OPENCODE_API_KEY = "sk-test-unsure-missing-key";
  const prevTypesafeKey = process.env.TYPESAFE_API_KEY;
  process.env.TYPESAFE_API_KEY = "sk-test-typesafe-dummy";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const calls: string[] = [];
  const fetchFn = (async () => {
    const step = calls.length;
    calls.push(step === 0 ? "decompose" : "analyze");
    if (step === 0) return responsesPayload({ proposals: PROPOSALS });
    return responsesPayload({
      analyses: [
        { nodeId: P("repayment"), objectiveId: OBJ, interpretation: "Repayment history is strong.", evidenceRefs: ["s1-ev3"] },
      ],
      evidenceRequests: [],
    });
  }) as unknown as typeof fetch;
  const probs: Record<string, number> = { [P("repayment")]: 0.85, [P("collateral")]: 0.6, [P("lunch")]: 0.1 };
  const systemOneFetch = (async (_url: unknown, init?: { body?: unknown }) => {
    const body = JSON.parse(String((init?.body as string) ?? "{}")) as { questions?: Record<string, unknown> };
    return new Response(JSON.stringify({
      answers: Object.fromEntries(Object.keys(body.questions ?? {}).map((id) => [id, { type: "noul", noul: probs[id] ?? 0.9 }])),
      model: "stub-jev",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
  let err: unknown = null;
  try {
    await runScenario(scenarios[0].id, { fetchFn, systemOne: createSystemOne(systemOneFetch), outDir: dir });
  } catch (e) {
    err = e;
  } finally {
    resetTypeSafeClientForTest();
    if (prevTypesafeKey === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = prevTypesafeKey;
  }
  expect(String(err)).toContain("missing evidence request");
});

test("malformed OpenCode response fails the chain without final.json", async () => {
  process.env.OPENCODE_API_KEY = "sk-test-malformed-key";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const fetchFn = (() => Promise.resolve(new Response(JSON.stringify({ output: [] }), { status: 200 }))) as unknown as typeof fetch;
  const systemOne = (() => Promise.resolve({ answers: {} })) as unknown as never;
  let err: unknown = null;
  try {
    await runScenario(scenarios[0].id, { fetchFn, systemOne, outDir: dir });
  } catch (e) {
    err = e;
  }
  expect(String(err)).toContain("malformed_opencode_response");
  let finalErr: unknown = null;
  try {
    await readFile(join(dir, "final.json"), "utf8");
  } catch (e) {
    finalErr = e;
  }
  expect(finalErr).not.toBeNull();
});

test("duplicate proposal ids are rejected", async () => {
  process.env.OPENCODE_API_KEY = "sk-test-duplicate-key";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const dupFetch = (() => Promise.resolve(responsesPayload({ proposals: [PROPOSALS[0], PROPOSALS[0]] }))) as unknown as typeof fetch;
  const emptyOne = (() => Promise.resolve({ answers: {} })) as unknown as never;
  let err: unknown = null;
  try {
    await runScenario(scenarios[0].id, { fetchFn: dupFetch, systemOne: emptyOne, outDir: dir });
  } catch (e) {
    err = e;
  }
  expect(String(err)).toContain("duplicate proposal id");
});

test("self-dependency is rejected", async () => {
  process.env.OPENCODE_API_KEY = "sk-test-selfdep-key";
  const dir = await mkdtemp(join(tmpdir(), "run-test-"));
  const bad = [{ ...PROPOSALS[0], dependsOn: [PROPOSALS[0].id] }];
  const badFetch = (() => Promise.resolve(responsesPayload({ proposals: bad }))) as unknown as typeof fetch;
  const emptyOne = (() => Promise.resolve({ answers: {} })) as unknown as never;
  let err: unknown = null;
  try {
    await runScenario(scenarios[0].id, { fetchFn: badFetch, systemOne: emptyOne, outDir: dir });
  } catch (e) {
    err = e;
  }
  expect(String(err)).toContain("depends on itself");
});

test("no secrets leak into artifacts", async () => {
  const secret = `sk-test-secret-${Date.now()}`;
  const dir = await successDir(secret);
  for (const name of await readdir(dir)) {
    const text = await readFile(join(dir, name), "utf8");
    expect(text).not.toContain(secret);
    expect(text).not.toContain("Authorization");
  }
});
