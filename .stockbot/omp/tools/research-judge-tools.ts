/** Native TypeSafe research-judge tools (evidence/claim/coverage/continuation/candidate).
 *
 * OMP owns runtime; TypeSafe owns semantic judgments. Each tool evaluates one
 * compact structured state pack, persists raw probabilities, and on success
 * issues a one-time Authorization the tool_call gate consumes. All failures
 * are sanitized fail-closed (never leak keys, transcripts, or SDK detail).
 *
 * Tools grade trusted kernel state only: every schema is identifier-only
 * (session/freeze/evidence/claim/branch-map/gap ids) resolved through an
 * injected state loader. Model args never reach the evaluator except `answer`
 * (final, what is being reviewed) and `candidate` (OMP generates, TypeSafe
 * evaluates; the auth binds its hash). Run identity comes from the injected
 * getRunId, never from model params: absent run id blocks.
 */

import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import { type AuthKind, candidateTaskHash, hashAction, issueAuthorization, launchCommitteeHash } from "../lib/research-control.ts";
import { buildAudit } from "../lib/typesafe/audit.ts";
import { authorizeCandidate, judgeContinuation, judgeCoverage, judgeEvidence, resolveClaim } from "../lib/typesafe/decisions.ts";
import { TypeSafeEvaluator } from "../lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../lib/typesafe/questions.ts";
import { loadCandidateGap, loadClaimState, loadContinuationState, loadCoverageState, loadEvidenceState, requireFreeze } from "../lib/typesafe/state.ts";
import { YES_THRESHOLD } from "../lib/typesafe/thresholds.ts";
import type { JudgmentMap, JudgeQuestion, SystemOneEvaluator } from "../lib/typesafe/types.ts";

export interface JudgeToolDeps {
 evaluator?: SystemOneEvaluator;
 model?: string;
 getRunId?: () => string;
 stateLoader?: {
  evidence?: (sessionId: string, freezeId: string, evidenceId: string) => Promise<{ objective: string; claim: string; evidence: unknown }>;
  claim?: (sessionId: string, freezeId: string, claimId: string) => Promise<{ objective: string; claim: string; evidence_set: unknown; actionable: boolean }>;
  coverage?: (sessionId: string, branchMapId: string) => Promise<{ objective: string; branch_map: unknown; coverage: unknown; claim_states: unknown; actionable: boolean }>;
  continuation?: (sessionId: string) => Promise<{ objective: string; coverage: unknown; open_questions: unknown; current_conclusions: unknown }>;
  candidate?: (sessionId: string, gapId: string) => Promise<{ objective: string; gap: string }>;
  freeze?: (sessionId: string, freezeId: string) => Promise<void>;
 };
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

function requireRunId(deps: JudgeToolDeps): string {
 const runId = deps.getRunId?.();
 if (typeof runId !== "string" || runId.length === 0) throw new Error("typesafe_missing_run");
 return runId;
}

function idOf(bag: Json, key: string): string {
 const v = bag[key];
 if (typeof v !== "string" || v.length === 0) throw new Error("typesafe_missing_id");
 return v;
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
  const raw = full[id]?.p_yes;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0 || raw > 1) throw new Error("typesafe_invalid_response");
  out[id] = { p_yes: raw, yes: raw >= YES_THRESHOLD };
 }
 return out;
}

function authFor(runId: string, kind: AuthKind, actionHash: string): Json {
 const auth = issueAuthorization(runId, kind, actionHash);
 return { authorization_id: auth.id, kind: auth.kind };
}

async function evaluatePack(deps: JudgeToolDeps, state: unknown, pack: string[]): Promise<{ results: JudgmentMap; model?: string }> {
 const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
 const res = await evaluator.evaluate(state, packQuestions(pack));
 return { results: packResults(res.results, pack), model: res.model ?? deps.model };
}

async function evidenceTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const load = deps.stateLoader?.evidence ?? loadEvidenceState;
  const st = await load(idOf(bag, "research_session_id"), idOf(bag, "freeze_id"), idOf(bag, "evidence_id"));
  const { results, model } = await evaluatePack(deps, { objective: st.objective, claim: st.claim, evidence: st.evidence }, PACKS.evidence);
  const { verdict } = judgeEvidence(results);
  const audit = buildAudit({ runId, phase: "evidence", questions: results, result: verdict, model });
  const yesCount = PACKS.evidence.filter((id) => results[id]?.yes).length;
  return { text: `evidence ${verdict} (${yesCount}/${PACKS.evidence.length} yes at ${YES_THRESHOLD})`, details: { verdict, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: evidence judgment failed. Transition blocked.", details: { error: "evidence_judgment_failed" }, isError: true };
 }
}

async function claimTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const load = deps.stateLoader?.claim ?? loadClaimState;
  const st = await load(idOf(bag, "research_session_id"), idOf(bag, "freeze_id"), idOf(bag, "claim_id"));
  const { results, model } = await evaluatePack(deps, { objective: st.objective, claim: st.claim, evidence_set: st.evidence_set }, PACKS.claim);
  const resolution = resolveClaim(results, st.actionable);
  const audit = buildAudit({ runId, phase: "claim", questions: results, result: resolution, model });
  return { text: `claim ${resolution}`, details: { resolution, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: claim judgment failed. Transition blocked.", details: { error: "claim_judgment_failed" }, isError: true };
 }
}

async function coverageTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const sessionId = idOf(bag, "research_session_id");
  const freezeId = idOf(bag, "freeze_id");
  const branchMapId = idOf(bag, "branch_map_id");
  const load = deps.stateLoader?.coverage ?? loadCoverageState;
  const st = await load(sessionId, branchMapId);
  const freeze = deps.stateLoader?.freeze ?? requireFreeze;
  await freeze(sessionId, freezeId);
  const { results, model } = await evaluatePack(deps, { objective: st.objective, branch_map: st.branch_map, coverage: st.coverage, claim_states: st.claim_states }, PACKS.coverage);
  const verdict = judgeCoverage(results, st.actionable);
  const audit = buildAudit({ runId, phase: "coverage", questions: results, result: verdict, model });
  const auth = verdict === "COMPLETE" ? issueAuthorization(runId, "launch_committee", launchCommitteeHash({ sessionId, freezeId, dossierId: branchMapId, coverageHash: hashAction({ branch_map: st.branch_map, coverage: st.coverage }) })) : undefined;
  const authJson = auth ? { authorization_id: auth.id, kind: auth.kind satisfies AuthKind } : undefined;
  return { text: `coverage ${verdict}`, details: { verdict, questions: results, audit, ...(authJson ? { authorization: authJson } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: coverage judgment failed. Transition blocked.", details: { error: "coverage_judgment_failed" }, isError: true };
 }
}

async function continuationTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const load = deps.stateLoader?.continuation ?? loadContinuationState;
  const st = await load(idOf(bag, "research_session_id"));
  const { results, model } = await evaluatePack(deps, { objective: st.objective, coverage: st.coverage, open_questions: st.open_questions, current_conclusions: st.current_conclusions }, PACKS.continuation);
  const { decision } = judgeContinuation(results);
  const audit = buildAudit({ runId, phase: "continuation", questions: results, result: decision, model });
  return { text: `continuation ${decision}`, details: { decision, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: continuation judgment failed. Transition blocked.", details: { error: "continuation_judgment_failed" }, isError: true };
 }
}

async function candidateTool(args: unknown, deps: JudgeToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const load = deps.stateLoader?.candidate ?? loadCandidateGap;
  const st = await load(idOf(bag, "research_session_id"), idOf(bag, "gap_id"));
  const candidate = bag.candidate;
  const task = bag.task;
  if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) throw new Error("typesafe_missing_id");
  if (typeof task !== "string" || task.length === 0) throw new Error("typesafe_missing_id");
  const { results, model } = await evaluatePack(deps, { objective: st.objective, gap: st.gap, candidate }, PACKS.candidate);
  const authorized = authorizeCandidate(results);
  const audit = buildAudit({ runId, phase: "candidate", questions: results, result: authorized ? "authorized" : "rejected", model });
  const auth = authorized ? authFor(runId, "continue_research", candidateTaskHash(candidate, task)) : undefined;
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
 tool("research_judge_evidence", "Judge one evidence item against its claim (E01-E16). Returns usable/unusable plus raw probabilities.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id"), evidence_id: anyProp("Evidence record id") });
 tool("research_judge_claim", "Resolve one claim over its evidence set (C01-C16). Returns SUPPORTED/CONTRADICTED/MIXED/UNKNOWN_* plus probabilities.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id"), claim_id: anyProp("Claim text: must exactly match kernel-grounded claim text") });
 tool("research_judge_coverage", "Judge branch-map coverage (V01-V20). Requires research_session_id, freeze_id, and branch_map_id; COMPLETE issues a launch_committee authorization bound to session/freeze/dossier.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id"), branch_map_id: anyProp("Dossier id carrying the branch map") });
 tool("research_judge_continuation", "Decide whether another research round is justified (N01-N04 all required). Advisory only; issues no authorization — call research_judge_candidate to authorize.", { research_session_id: anyProp("Research session id") });
 tool("research_judge_candidate", "Authorize one candidate investigation (N07-N12, all critical must pass). Authorizes continue_research once on pass. Post-first-round sec-agent task items must echo the approved candidate and full task text verbatim under item.candidate and item.task; rephrased candidates or altered instructions need a fresh candidate judgment.", { research_session_id: anyProp("Research session id"), gap_id: anyProp("Material gap id or exact gap text"), candidate: anyProp("Proposed investigation OMP generated"), task: anyProp("Full sec-agent task text the item must echo verbatim") });
}
