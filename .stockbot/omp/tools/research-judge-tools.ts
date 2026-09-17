/** Native TypeSafe research-judge tools (evidence/claim/coverage/continuation/candidate).
 *
 * OMP owns runtime; TypeSafe owns semantic judgments. Each tool evaluates one
 * compact structured state pack, persists raw probabilities, and on success
 * issues a one-time Authorization the tool_call gate consumes. All failures
 * are sanitized fail-closed (never leak keys, transcripts, or SDK detail).
 */

import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import { type AuthKind, hashAction, issueAuthorization } from "../lib/research-control.ts";
import { buildAudit } from "../lib/typesafe/audit.ts";
import { authorizeCandidate, judgeContinuation, judgeCoverage, judgeEvidence, resolveClaim } from "../lib/typesafe/decisions.ts";
import { TypeSafeEvaluator } from "../lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../lib/typesafe/questions.ts";
import { YES_THRESHOLD } from "../lib/typesafe/thresholds.ts";
import type { JudgmentMap, JudgeQuestion, SystemOneEvaluator } from "../lib/typesafe/types.ts";

export interface JudgeToolDeps {
 evaluator?: SystemOneEvaluator;
 model?: string;
}

type Json = Record<string, unknown>;

export interface NativeToolResult {
 text: string;
 details: Json;
 isError?: boolean;
}

export const JUDGE_TOOL_NAMES = [
 "research_judge_evidence",
 "research_judge_claim",
 "research_judge_coverage",
 "research_judge_continuation",
 "research_judge_candidate",
];

/** Boundary copy: host-validated tool params into a plain bag without casts. */
export function toolBag(args: unknown): Json {
 const bag: Json = {};
 if (args && typeof args === "object") for (const [k, v] of Object.entries(args)) bag[k] = v;
 return bag;
}

export function runIdOf(bag: Json): string {
 return typeof bag.run_id === "string" && bag.run_id ? bag.run_id : "default";
}

// Raw JSON Schema: the host accepts the same wire documents the bridge passes
// through (index.ts forwards describe parameters untouched), so native tools
// use the same shape instead of a second schema convention.
const nativeSchema = (properties: Json): ToolDefinition["parameters"] =>
 ({ type: "object", properties, additionalProperties: true }) as unknown as ToolDefinition["parameters"];

const anyProp = (description: string): Json => ({ description });

function packQuestions(ids: string[]): JudgeQuestion[] {
 return ids.map((id) => QUESTION_BANK[id]).filter((q): q is JudgeQuestion => !!q);
}

function packResults(full: JudgmentMap, ids: string[]): JudgmentMap {
 const out: JudgmentMap = {};
 for (const id of ids) {
  const e = full[id];
  const raw = e?.p_yes;
  const p = typeof raw === "number" && Number.isFinite(raw) && raw >= 0 && raw <= 1 ? raw : 0;
  out[id] = { p_yes: p, yes: p >= YES_THRESHOLD };
 }
 return out;
}

function authFor(runId: string, kind: AuthKind, fingerprint: unknown): Json {
 const auth = issueAuthorization(runId, kind, hashAction(fingerprint));
 return { authorization_id: auth.id, kind: auth.kind };
}

async function evaluatePack(deps: JudgeToolDeps, state: unknown, pack: string[]): Promise<{ results: JudgmentMap; model?: string }> {
 const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
 const res = await evaluator.evaluate(state, packQuestions(pack));
 return { results: packResults(res.results, pack), model: res.model ?? deps.model };
}

async function evidenceTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const { results, model } = await evaluatePack(deps, { objective: bag.objective, claim: bag.claim, evidence: bag.evidence }, PACKS.evidence);
  const { verdict } = judgeEvidence(results);
  const audit = buildAudit({ runId, phase: "evidence", questions: results, result: verdict, model });
  const yesCount = PACKS.evidence.filter((id) => results[id]?.yes).length;
  return { text: `evidence ${verdict} (${yesCount}/${PACKS.evidence.length} yes at ${YES_THRESHOLD})`, details: { verdict, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: evidence judgment failed. Transition blocked.", details: { error: "evidence_judgment_failed" }, isError: true };
 }
}

async function claimTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const { results, model } = await evaluatePack(deps, { objective: bag.objective, claim: bag.claim, evidence_set: bag.evidence_set }, PACKS.claim);
  const resolution = resolveClaim(results, bag.actionable === true);
  const audit = buildAudit({ runId, phase: "claim", questions: results, result: resolution, model });
  return { text: `claim ${resolution}`, details: { resolution, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: claim judgment failed. Transition blocked.", details: { error: "claim_judgment_failed" }, isError: true };
 }
}

async function coverageTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const { results, model } = await evaluatePack(deps, { objective: bag.objective, branch_map: bag.branch_map, coverage: bag.coverage, claim_states: bag.claim_states }, PACKS.coverage);
  const verdict = judgeCoverage(results, bag.actionable === true);
  const audit = buildAudit({ runId, phase: "coverage", questions: results, result: verdict, model });
  const auth = verdict === "COMPLETE" ? authFor(runId, "launch_committee", { verdict, results }) : undefined;
  return { text: `coverage ${verdict}`, details: { verdict, questions: results, audit, ...(auth ? { authorization: auth } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: coverage judgment failed. Transition blocked.", details: { error: "coverage_judgment_failed" }, isError: true };
 }
}

async function continuationTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const { results, model } = await evaluatePack(deps, { objective: bag.objective, coverage: bag.coverage, open_questions: bag.open_questions, current_conclusions: bag.current_conclusions }, PACKS.continuation);
  const { decision } = judgeContinuation(results, bag.has_candidate === true);
  const audit = buildAudit({ runId, phase: "continuation", questions: results, result: decision, model });
  const auth = decision === "continue" ? authFor(runId, "continue_research", { decision, results }) : undefined;
  return { text: `continuation ${decision}`, details: { decision, questions: results, audit, ...(auth ? { authorization: auth } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: continuation judgment failed. Transition blocked.", details: { error: "continuation_judgment_failed" }, isError: true };
 }
}

async function candidateTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const { results, model } = await evaluatePack(deps, { objective: bag.objective, gap: bag.gap, candidate: bag.candidate }, PACKS.candidate);
  const authorized = authorizeCandidate(results);
  const audit = buildAudit({ runId, phase: "candidate", questions: results, result: authorized ? "authorized" : "rejected", model });
  const auth = authorized ? authFor(runId, "continue_research", { candidate: bag.candidate, results }) : undefined;
  return { text: `candidate ${authorized ? "authorized" : "rejected"}`, details: { authorized, questions: results, audit, ...(auth ? { authorization: auth } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: candidate judgment failed. Transition blocked.", details: { error: "candidate_judgment_failed" }, isError: true };
 }
}

export async function runJudgeTool(name: string, args: unknown, deps: JudgeToolDeps = {}): Promise<NativeToolResult | undefined> {
 switch (name) {
  case "research_judge_evidence": return evidenceTool(args, deps);
  case "research_judge_claim": return claimTool(args, deps);
  case "research_judge_coverage": return coverageTool(args, deps);
  case "research_judge_continuation": return continuationTool(args, deps);
  case "research_judge_candidate": return candidateTool(args, deps);
  default: return undefined;
 }
}

export function registerResearchJudgeTools(pi: ExtensionAPI, deps: JudgeToolDeps = {}): void {
 const tool = (name: string, description: string, properties: Json): void => {
  pi.registerTool({
   name,
   label: name,
   description,
   parameters: nativeSchema(properties),
   async execute(_toolCallId: string, params: unknown) {
    const native = await runJudgeTool(name, params, deps);
    if (!native) return { content: [{ type: "text", text: `Unknown judge tool '${name}'. Transition blocked.` }], details: { error: "unknown_judge_tool" }, isError: true };
    // Durable audit: the audit record rides details.audit into the host
    // tool_result frame, which index.ts persists as a sqlite row (no
    // console/stdout logging: OMP captures stdout as tool chatter, and
    // buildAudit never receives inputs, transcripts, or key material).
    return { content: [{ type: "text", text: native.text }], details: native.details, ...(native.isError === true ? { isError: true } : {}) };
   },
  });
 };
 tool("research_judge_evidence", "Judge one evidence item against its claim (E01-E16). Returns usable/unusable plus raw probabilities.", { objective: anyProp("Research objective under investigation"), claim: anyProp("Claim the evidence supposedly supports"), evidence: anyProp("Single evidence item with source and passage"), run_id: anyProp("OMP run id for audit") });
 tool("research_judge_claim", "Resolve one claim over its evidence set (C01-C16). Returns SUPPORTED/CONTRADICTED/MIXED/UNKNOWN_* plus probabilities.", { objective: anyProp("Research objective under investigation"), claim: anyProp("Claim to resolve"), evidence_set: anyProp("Supporting/contradicting evidence plus unknowns"), actionable: anyProp("Whether further research routes may exist"), run_id: anyProp("OMP run id for audit") });
 tool("research_judge_coverage", "Judge branch-map coverage (V01-V20). Returns COMPLETE/INCOMPLETE_* plus probabilities; COMPLETE authorizes the committee once.", { objective: anyProp("Research objective under investigation"), branch_map: anyProp("Material branches OMP generated"), coverage: anyProp("Investigated/remaining routes"), claim_states: anyProp("Per-claim resolutions"), actionable: anyProp("Whether unsearched routes may help"), run_id: anyProp("OMP run id for audit") });
 tool("research_judge_continuation", "Decide whether another research round is justified (N01-N06). Authorizes continue_research once on continue.", { objective: anyProp("Research objective under investigation"), coverage: anyProp("Current coverage snapshot"), open_questions: anyProp("Material unresolved questions"), current_conclusions: anyProp("Conclusions supportable today"), has_candidate: anyProp("Whether an authorized candidate already exists"), run_id: anyProp("OMP run id for audit") });
 tool("research_judge_candidate", "Authorize one candidate investigation (N07-N12, all critical must pass). Authorizes continue_research once on pass.", { objective: anyProp("Research objective under investigation"), gap: anyProp("Material gap the candidate addresses"), candidate: anyProp("Proposed investigation OMP generated"), run_id: anyProp("OMP run id for audit") });
}
