// decision/run.ts — minimal sequential caller for the API-only prompt-graph experiment.
//
// Owns: OpenCode Responses calls, TypeSafe/JEV relevance + adjudication, strict
// contract validation, per-run artifacts under decision/results.
// Consumes sibling-owned decision/scenarios.ts + decision/prompts.ts; evaluationCriteria
// is never sent to any provider.
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { getTypeSafeClient } from "../.stockbot/omp/lib/typesafe/client.ts";
import { TypeSafeClient, noul } from "@typesafe-ai/sdk";
import { scenarios } from "./scenarios.ts";
import { analyzePrompt, decomposePrompt, expandPrompt } from "./prompts.ts";
import type { Analysis, EvidenceRequest, Proposal } from "./prompts.ts";

const OPENCODE_URL = "https://opencode.ai/zen/v1/responses";
const DEFAULT_MODEL = "muse-spark-1.3-contributor";
const RESULTS_ROOT = "decision/results";

export type Decision = "yes" | "unsure" | "no";

// Approved policy: >=.70 yes; >=.50 and <.70 unsure; <.50 no.
export function classifyProbability(p: number): Decision {
  if (!Number.isFinite(p) || p < 0 || p > 1) throw new Error("invalid_probability");
  if (p >= 0.7) return "yes";
  if (p >= 0.5) return "unsure";
  return "no";
}

type SystemOneFn = (req: { state: unknown; questions: Record<string, unknown> }) => Promise<unknown>;

export function createSystemOne(fetchImpl?: typeof fetch): SystemOneFn {
  return (req) => {
    const client = fetchImpl ? new TypeSafeClient({ fetch: fetchImpl }) : getTypeSafeClient();
    return client.systemOne(req as never, { retry: { maxRetries: 0 } } as never);
  };
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function exactKeys(stage: string, o: Record<string, unknown>, keys: string[]): void {
  const actual = Object.keys(o).sort();
  if (actual.length !== keys.length || actual.some((k, i) => k !== keys[i]))
    throw new Error(`${stage}: unexpected fields [${actual.join(",")}]`);
}

function nonEmpty(stage: string, v: unknown, what: string): string {
  if (typeof v !== "string" || v.length === 0) throw new Error(`${stage}: ${what} must be a nonempty string`);
  return v;
}

// Proposed IDs are objective-scoped, nonempty, unique; deps reference existing or
// co-proposed IDs, never self. Expand stage additionally rejects IDs reusing old ones.
function checkProposals(stage: string, value: unknown, objectiveId: string, priorIds: Set<string>): Proposal[] {
  if (!Array.isArray(value)) throw new Error(`${stage}: proposals must be an array`);
  const ids = new Set<string>();
  for (const item of value) {
    if (!isObj(item)) throw new Error(`${stage}: proposal must be an object`);
    exactKeys(stage, item, ["dependsOn", "id", "objectiveId", "question", "whyItMatters"]);
    const id = nonEmpty(stage, item.id, "proposal.id");
    if (!id.startsWith(`${objectiveId}-`)) throw new Error(`${stage}: proposal id ${id} must start with ${objectiveId}-`);
    if (item.objectiveId !== objectiveId) throw new Error(`${stage}: proposal ${id} references wrong objective`);
    nonEmpty(stage, item.question, "proposal.question");
    nonEmpty(stage, item.whyItMatters, "proposal.whyItMatters");
    if (ids.has(id)) throw new Error(`${stage}: duplicate proposal id ${id}`);
    if (priorIds.has(id)) throw new Error(`${stage}: reused proposal id ${id}`);
    ids.add(id);
  }
  const refs = new Set([...priorIds, ...ids]);
  for (const item of value) {
    const r = item as Record<string, unknown>;
    const id = r.id as string;
    if (!Array.isArray(r.dependsOn) || r.dependsOn.some((d) => typeof d !== "string"))
      throw new Error(`${stage}: proposal ${id} dependsOn must be string[]`);
    for (const d of r.dependsOn as string[]) {
      if (d === id) throw new Error(`${stage}: proposal ${id} depends on itself`);
      if (!refs.has(d)) throw new Error(`${stage}: proposal ${id} references unknown id ${d}`);
    }
  }
  return value as Proposal[];
}

function checkAnalyses(
  stage: string,
  value: unknown,
  objectiveId: string,
  proposalIds: Set<string>,
  evidenceIds: Set<string>,
): Analysis[] {
  if (!Array.isArray(value)) throw new Error(`${stage}: analyses must be an array`);
  const seen = new Set<string>();
  for (const item of value) {
    if (!isObj(item)) throw new Error(`${stage}: analysis must be an object`);
    exactKeys(stage, item, ["evidenceRefs", "interpretation", "nodeId", "objectiveId"]);
    const nodeId = nonEmpty(stage, item.nodeId, "analysis.nodeId");
    if (!proposalIds.has(nodeId)) throw new Error(`${stage}: analysis references unknown proposal ${nodeId}`);
    if (item.objectiveId !== objectiveId) throw new Error(`${stage}: analysis ${nodeId} references wrong objective`);
    nonEmpty(stage, item.interpretation, "analysis.interpretation");
    if (!Array.isArray(item.evidenceRefs) || item.evidenceRefs.some((e) => typeof e !== "string"))
      throw new Error(`${stage}: analysis ${nodeId} evidenceRefs must be string[]`);
    for (const e of item.evidenceRefs as string[])
      if (!evidenceIds.has(e)) throw new Error(`${stage}: analysis ${nodeId} references unknown evidence ${e}`);
    if (seen.has(nodeId)) throw new Error(`${stage}: duplicate analysis for ${nodeId}`);
    seen.add(nodeId);
  }
  return value as Analysis[];
}

function checkEvidenceRequests(
  stage: string,
  value: unknown,
  objectiveId: string,
  nodeIds: Set<string>,
): EvidenceRequest[] {
  if (!Array.isArray(value)) throw new Error(`${stage}: evidenceRequests must be an array`);
  for (const item of value) {
    if (!isObj(item)) throw new Error(`${stage}: evidenceRequest must be an object`);
    exactKeys(stage, item, ["missingEvidence", "nodeId", "objectiveId"]);
    const nodeId = nonEmpty(stage, item.nodeId, "evidenceRequest.nodeId");
    if (!nodeIds.has(nodeId)) throw new Error(`${stage}: evidenceRequest references unknown node ${nodeId}`);
    if (item.objectiveId !== objectiveId)
      throw new Error(`${stage}: evidenceRequest for ${nodeId} references wrong objective`);
    nonEmpty(stage, item.missingEvidence, "evidenceRequest.missingEvidence");
  }
  return value as EvidenceRequest[];
}

// Extract concatenated output_text from a Responses API payload, then parse the
// exact contract object. Raw payload is saved by the caller before this runs.
function parseOpenCodeOutput(stage: string, raw: unknown, keys: string[]): Record<string, unknown> {
  const output = isObj(raw) ? raw.output : undefined;
  if (!Array.isArray(output)) throw new Error(`${stage}: malformed_opencode_response`);
  let text = "";
  for (const item of output) {
    if (!isObj(item) || item.type !== "message" || !Array.isArray(item.content)) continue;
    for (const part of item.content)
      if (isObj(part) && part.type === "output_text" && typeof part.text === "string") text += part.text;
  }
  if (!text) throw new Error(`${stage}: malformed_opencode_response`);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`${stage}: malformed_opencode_json`);
  }
  if (!isObj(parsed)) throw new Error(`${stage}: malformed_opencode_json`);
  exactKeys(stage, parsed, keys);
  return parsed;
}

// POST {model, input} — no tools, no streaming. Only the body is saved; the
// Authorization header is never written to disk. Any failure throws, so a failed
// request can never report a successful chain.
async function postOpenCode(
  dir: string,
  name: string,
  input: string,
  model: string,
  apiKey: string,
  fetchFn: typeof fetch,
): Promise<unknown> {
  const body = { model, input };
  await writeFile(join(dir, `request-${name}.json`), JSON.stringify(body, null, 2) + "\n");
  let res: Response;
  try {
    res = await fetchFn(OPENCODE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error(`${name}: opencode_request_failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  if (!res.ok) throw new Error(`${name}: opencode_request_failed: ${res.status}`);
  const raw: unknown = await res.json();
  await writeFile(join(dir, `raw-${name}.json`), JSON.stringify(raw, null, 2) + "\n");
  return raw;
}

// ponytail: raw here is the SDK-resolved JSON saved before our policy validation;
// the SDK owns transport bytes, so byte-level capture would need a wrapping fetch.
async function askSystemOne(
  dir: string,
  name: string,
  req: { state: unknown; questions: Record<string, unknown> },
  systemOne: SystemOneFn,
): Promise<{ raw: unknown; policy: Record<string, { probability: number; decision: Decision }> }> {
  await writeFile(join(dir, `request-${name}.json`), JSON.stringify(req, null, 2) + "\n");
  let raw: unknown;
  try {
    raw = await systemOne(req);
  } catch (e) {
    throw new Error(`${name}: typesafe_request_failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  await writeFile(join(dir, `raw-${name}.json`), JSON.stringify(raw, null, 2) + "\n");
  const ids = Object.keys(req.questions).sort();
  if (!isObj(raw) || !isObj(raw.answers)) throw new Error(`${name}: malformed_typesafe_response`);
  const got = Object.keys(raw.answers).sort();
  if (got.length !== ids.length || got.some((k, i) => k !== ids[i]))
    throw new Error(`${name}: typesafe answers do not match questions`);
  const policy: Record<string, { probability: number; decision: Decision }> = {};
  for (const id of ids) {
    const ans = (raw.answers as Record<string, unknown>)[id];
    if (!isObj(ans) || ans.type !== "noul" || typeof ans.noul !== "number")
      throw new Error(`${name}: malformed_typesafe_answer for ${id}`);
    const probability = ans.noul as number;
    policy[id] = { probability, decision: classifyProbability(probability) };
  }
  await writeFile(join(dir, `policy-${name}.json`), JSON.stringify(policy, null, 2) + "\n");
  return { raw, policy };
}

function promptText(stage: string, build: (ctx: unknown) => string, ctx: unknown): string {
  const text = build(ctx);
  if (typeof text !== "string" || text.length === 0) throw new Error(`${stage}: malformed_prompt_builder`);
  return text;
}

export async function runScenario(
  scenarioId: string,
  opts?: { fetchFn?: typeof fetch; systemOne?: SystemOneFn; outDir?: string; model?: string },
): Promise<string> {
  const scenario = scenarios.find((s) => s.id === scenarioId);
  if (!scenario) throw new Error(`unknown_scenario: ${scenarioId} (known: ${scenarios.map((s) => s.id).join(", ")})`);
  const model = opts?.model ?? process.env.OPENCODE_MODEL ?? DEFAULT_MODEL;
  const apiKey = process.env.OPENCODE_API_KEY;
  if (!apiKey) throw new Error("opencode_unavailable: missing OPENCODE_API_KEY");
  const fetchFn = opts?.fetchFn ?? fetch;
  const systemOne = opts?.systemOne ?? createSystemOne();
  const dir = opts?.outDir ?? join(RESULTS_ROOT, `${scenario.id}-${new Date().toISOString().replace(/[:.]/g, "-")}`);
  await mkdir(dir, { recursive: true });

  const objectiveId = scenario.objective.id;
  const evidenceIds = new Set(scenario.evidence.map((e) => e.id));
  const base = { objective: scenario.objective, fictional: scenario.fictional, evidence: scenario.evidence };

  // 1. decompose
  const decomposeRaw = await postOpenCode(
    dir, "decompose", promptText("decompose", decomposePrompt, base), model, apiKey, fetchFn,
  );
  const proposals = checkProposals(
    "decompose", parseOpenCodeOutput("decompose", decomposeRaw, ["proposals"]).proposals, objectiveId, new Set(),
  );

  // 2. relevance of initial proposals (yes -> analysis; unsure -> evidence needs, not
  // admitted; no -> retained artifact only)
  const relevanceQuestions: Record<string, unknown> = {};
  for (const p of proposals)
    relevanceQuestions[p.id] = noul(`Is proposal ${p.id} relevant to objective ${objectiveId} and worth analyzing?`);
  const relevance = proposals.length
    ? await askSystemOne(dir, "relevance", { state: { ...base, proposals }, questions: relevanceQuestions }, systemOne)
    : { raw: null, policy: {} as Record<string, { probability: number; decision: Decision }> };
  const decideOf = (id: string): Decision => relevance.policy[id]?.decision ?? "no";
  const probOf = (policy: Record<string, { probability: number; decision: Decision }>, id: string): number | null =>
    policy[id]?.probability ?? null;
  const yesOnly = proposals.filter((p) => decideOf(p.id) === "yes");
  const unsureIds = new Set(proposals.filter((p) => decideOf(p.id) === "unsure").map((p) => p.id));
  const relevanceList = proposals.map((p) => ({
    proposalId: p.id, probability: probOf(relevance.policy, p.id), decision: decideOf(p.id),
  }));

  // 3. analyze yes-only; unsure gets missing-evidence requests only
  const yesIds = new Set(yesOnly.map((p) => p.id));
  const analyzeCtx = {
    ...base, proposals: yesOnly, unsureProposalIds: [...unsureIds], relevance: relevanceList, jev: relevance.raw,
    jevResults: relevance.raw, policyResults: relevanceList,
    priorRefs: { evidenceIds: [...evidenceIds], proposalIds: proposals.map((p) => p.id) },
  };
  const analyzeRaw = await postOpenCode(
    dir, "analyze", promptText("analyze", analyzePrompt, analyzeCtx), model, apiKey, fetchFn,
  );
  const analyzeOut = parseOpenCodeOutput("analyze", analyzeRaw, ["analyses", "evidenceRequests"]);
  const analyzableIds = new Set([...yesIds, ...unsureIds]);
  const analyses = checkAnalyses("analyze", analyzeOut.analyses, objectiveId, analyzableIds, evidenceIds);
  const analyzeRequests = checkEvidenceRequests("analyze", analyzeOut.evidenceRequests, objectiveId, new Set([...yesIds, ...unsureIds]));
  for (const a of analyses)
    if (unsureIds.has(a.nodeId))
      throw new Error(`analyze: unsure proposal ${a.nodeId} must not be analyzed`);
  for (const id of unsureIds)
    if (!analyzeRequests.some((r) => r.nodeId === id))
      throw new Error(`analyze: missing evidence request for unsure proposal ${id}`);
  // 4. adjudicate yes-only analyses
  const decideQuestions: Record<string, unknown> = {};
  for (const a of analyses)
    decideQuestions[a.nodeId] = noul(`Should analysis of ${a.nodeId} for objective ${objectiveId} be accepted on the cited evidence?`);
  const decide = analyses.length
    ? await askSystemOne(
      dir, "decide", { state: { ...base, proposals: yesOnly, analyses }, questions: decideQuestions }, systemOne,
    )
    : { raw: null, policy: {} as Record<string, { probability: number; decision: Decision }> };
  const analysisDecisions = analyses.map((a) => ({
    nodeId: a.nodeId, probability: probOf(decide.policy, a.nodeId), decision: decide.policy[a.nodeId]?.decision ?? "no",
  }));
  // Missing evidence becomes requests/artifacts only.
  const priorIds = new Set(proposals.map((p) => p.id));
  const unresolved = [...new Set([
    ...relevanceList.filter((r) => r.decision === "unsure").map((r) => r.proposalId),
    ...analysisDecisions.filter((d) => d.decision === "unsure").map((d) => d.nodeId),
  ])];
  const expandCtx = {
    ...base, proposals, relevance: relevanceList, analyses, analysisDecisions,
    evidenceRequests: analyzeRequests, unresolved, jev: { relevance: relevance.raw, decide: decide.raw },
    adjudication: { relevance: relevanceList, analysisDecisions },
    priorIds: [...priorIds], priorRefs: { proposalIds: [...priorIds] },
  };
  const expandRaw = await postOpenCode(
    dir, "expand", promptText("expand", expandPrompt, expandCtx), model, apiKey, fetchFn,
  );
  const expandOut = parseOpenCodeOutput("expand", expandRaw, ["evidenceRequests", "proposals"]);
  const expanded = checkProposals("expand", expandOut.proposals, objectiveId, priorIds);
  const allNodeIds = new Set([...priorIds, ...expanded.map((p) => p.id)]);
  const expandRequests = checkEvidenceRequests("expand", expandOut.evidenceRequests, objectiveId, allNodeIds);

  // 6. relevance of expanded proposals
  const expandedQuestions: Record<string, unknown> = {};
  for (const p of expanded)
    expandedQuestions[p.id] = noul(`Is expanded proposal ${p.id} relevant to objective ${objectiveId}?`);
  const expandedRelevance = expanded.length
    ? await askSystemOne(
      dir, "relevance-expanded", { state: { ...base, proposals: expanded }, questions: expandedQuestions }, systemOne,
    )
    : { raw: null, policy: {} as Record<string, { probability: number; decision: Decision }> };
  const expDecideOf = (id: string): Decision => expandedRelevance.policy[id]?.decision ?? "no";

  const final = {
    scenarioId: scenario.id,
    model,
    objective: scenario.objective,
    fictional: scenario.fictional,
    evidence: scenario.evidence,
    proposals: [
      ...proposals.map((p) => ({ ...p, stage: "initial", probability: probOf(relevance.policy, p.id), decision: decideOf(p.id) })),
      ...expanded.map((p) => ({ ...p, stage: "expanded", probability: probOf(expandedRelevance.policy, p.id), decision: expDecideOf(p.id) })),
    ],
    reasoning: analyses.map((a) => ({
      ...a, probability: probOf(decide.policy, a.nodeId), decision: decide.policy[a.nodeId]?.decision ?? "no",
    })),
    jev: { relevance: relevance.raw, decide: decide.raw, expandedRelevance: expandedRelevance.raw },
    policy: { relevance: relevance.policy, decide: decide.policy, expandedRelevance: expandedRelevance.policy },
    evidenceRequests: [
      ...analyzeRequests.map((r) => ({ ...r, stage: "analyze" })),
      ...expandRequests.map((r) => ({ ...r, stage: "expand" })),
    ],
    unresolved,
  };
  await writeFile(join(dir, "final.json"), JSON.stringify(final, null, 2) + "\n");
  await writeFile(
    join(dir, "evaluation-criteria.json"),
    JSON.stringify({ scenarioId: scenario.id, evaluationCriteria: scenario.evaluationCriteria }, null, 2) + "\n",
  );
  await writeFile(
    join(dir, "eval-context.json"),
    JSON.stringify({
      scenarioId: scenario.id, objective: scenario.objective, fictional: scenario.fictional,
      evidence: scenario.evidence, proposals: [...proposals, ...expanded], analyses,
      evidenceRequests: [...analyzeRequests, ...expandRequests], evaluationCriteria: scenario.evaluationCriteria,
    }, null, 2) + "\n",
  );
  return dir;
}

if (import.meta.main) {
  const args = Bun.argv.slice(2);
  try {
    if (args.includes("--list")) {
      for (const s of scenarios) console.log(`${s.id}\t${s.objective.id}\t${s.objective.prompt}`);
    } else {
      const id = args.find((a) => !a.startsWith("-"));
      if (!id) {
        console.error("usage: bun decision/run.ts --list | bun decision/run.ts <scenario-id>");
        process.exit(2);
      }
      const dir = await runScenario(id);
      console.log(`saved ${dir}/final.json`);
    }
  } catch (e) {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  }
}
