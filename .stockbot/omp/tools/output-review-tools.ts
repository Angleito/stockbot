/** Native TypeSafe output-review tools (role/committee/final).
 *
 * The committee never self-grades: each role output gets common Q01-Q18 plus
 * its role pack (S/B/R), the committee gets F01-F08, and the final answer gets
 * the extended pack (Q04-Q18 subset + F09-F12). Critical failures return
 * structured defects for OMP-driven revision; TypeSafe never rewrites.
 * Role ACCEPT issues a bound accept_role_output authorization, committee PASS
 * issues committee_accepted, final PASS issues a one-time finalize
 * authorization. Tools grade trusted kernel state only (identifier-only
 * schemas + injected loader); run identity comes from injected getRunId.
 */

import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import { type AuthKind, finalizeActionHash, hashAction, issueAuthorization, issueRoleAccept } from "../lib/research-control.ts";
import { buildAudit } from "../lib/typesafe/audit.ts";
import { reviewCommittee, reviewFinal, reviewRoleOutput } from "../lib/typesafe/decisions.ts";
import { TypeSafeEvaluator } from "../lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../lib/typesafe/questions.ts";
import { loadCommitteeState, loadFinalState, loadRoleOutput } from "../lib/typesafe/state.ts";
import { YES_THRESHOLD } from "../lib/typesafe/thresholds.ts";
import type { JudgmentMap, JudgeQuestion, SystemOneEvaluator } from "../lib/typesafe/types.ts";
import { toolBag } from "./research-judge-tools.ts";
export interface ReviewToolDeps {
 evaluator?: SystemOneEvaluator;
 model?: string;
 getRunId?: () => string;
 stateLoader?: {
  role?: (sessionId: string, freezeId: string, roleJobId: string) => Promise<{ role: string; objective: string; evidence: unknown; output: unknown; freezeId: string; freezeEvidenceIds: string[]; freezeEvidenceHash: string }>;
  committee?: (sessionId: string, freezeId: string) => Promise<{ objective: string; evidence: unknown; stockbot: unknown; bullbot: unknown; bearbot: unknown; freezeHash: string }>;
  final?: (sessionId: string, freezeId: string) => Promise<{ objective: string; evidence: unknown; committee: unknown }>;
  freeze?: (sessionId: string, freezeId: string) => Promise<void>;
 };
}

type Json = Record<string, unknown>;

export interface NativeToolResult {
 text: string;
 details: Json;
 isError?: boolean;
}

export const REVIEW_TOOL_NAMES = ["research_review_role_output", "research_review_committee", "research_review_final"];

const ROLE_PACKS: Record<string, string[]> = { stockbot: PACKS.stockbot, bullbot: PACKS.bull, bearbot: PACKS.bear };

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

function requireRunId(deps: ReviewToolDeps): string {
 const runId = deps.getRunId?.();
 if (typeof runId !== "string" || runId.length === 0) throw new Error("typesafe_missing_run");
 return runId;
}

function idOf(bag: Json, key: string): string {
 const v = bag[key];
 if (typeof v !== "string" || v.length === 0) throw new Error("typesafe_missing_id");
 return v;
}

async function roleTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const load = deps.stateLoader?.role ?? loadRoleOutput;
  const st = await load(idOf(bag, "research_session_id"), idOf(bag, "freeze_id"), idOf(bag, "role_job_id"));
  const rolePack = ROLE_PACKS[st.role];
  if (!rolePack) return { text: `Unknown role '${st.role}'. Revision blocked.`, details: { error: "unknown_role" }, isError: true };
  const ids = [...PACKS.common, ...rolePack];
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ role: st.role, objective: st.objective, evidence: st.evidence, output: st.output }, packQuestions(ids));
  const results = packResults(res.results, ids);
  const { verdict, failed } = reviewRoleOutput(results, packQuestions(ids));
  const audit = buildAudit({ runId, phase: "role_output", questions: results, result: `${st.role}:${verdict}`, model: res.model ?? deps.model });
  // Bound ACCEPT: the gate verifies this binding against the actual trio for the freeze.
  const auth = verdict === "ACCEPT"
   ? issueRoleAccept(runId, { runId, freezeId: st.freezeId, role: st.role, roleJobId: idOf(bag, "role_job_id"), outputHash: hashAction(st.output), freezeHash: st.freezeEvidenceHash })
   : undefined;
  const authJson = auth ? { authorization_id: auth.id, kind: auth.kind satisfies AuthKind } : undefined;
  const summary = verdict === "ACCEPT" ? `${st.role} output ACCEPT` : `${st.role} output REJECT_FOR_REVISION: ${failed.map((f) => `${f.id}=${f.p_yes}`).join(", ")}`;
  return { text: summary, details: { verdict, failed, questions: results, audit, ...(authJson ? { authorization: authJson } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: role-output review failed. Revision blocked.", details: { error: "role_review_failed" }, isError: true };
 }
}

async function committeeTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const sessionId = idOf(bag, "research_session_id");
  const freezeId = idOf(bag, "freeze_id");
  const load = deps.stateLoader?.committee ?? loadCommitteeState;
  const st = await load(sessionId, freezeId);
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ objective: st.objective, evidence: st.evidence, stockbot: st.stockbot, bullbot: st.bullbot, bearbot: st.bearbot }, packQuestions(PACKS.committee));
  const results = packResults(res.results, PACKS.committee);
  const { verdict, failed } = reviewCommittee(results, packQuestions(PACKS.committee));
  const audit = buildAudit({ runId, phase: "committee", questions: results, result: verdict, model: res.model ?? deps.model });
  const auth = verdict === "PASS" ? issueAuthorization(runId, "committee_accepted", hashAction({ sessionId, freezeId })) : undefined;
  const authJson = auth ? { authorization_id: auth.id, kind: auth.kind satisfies AuthKind } : undefined;
  return { text: verdict === "PASS" ? "committee PASS" : `committee BLOCK: ${failed.map((f) => `${f.id}=${f.p_yes}`).join(", ")}`, details: { verdict, failed, questions: results, audit, ...(authJson ? { authorization: authJson } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: committee review failed. Transition blocked.", details: { error: "committee_review_failed" }, isError: true };
 }
}

async function finalTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 try {
  const runId = requireRunId(deps);
  const bag = toolBag(args);
  const answer = bag.answer;
  if (typeof answer !== "string" || answer.length === 0) throw new Error("typesafe_missing_id");
  const sessionId = idOf(bag, "research_session_id");
  const freezeId = idOf(bag, "freeze_id");
  const load = deps.stateLoader?.final ?? loadFinalState;
  const st = await load(sessionId, freezeId);
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ objective: st.objective, evidence: st.evidence, committee: st.committee, answer }, packQuestions(PACKS.final_extended));
  const results = packResults(res.results, PACKS.final_extended);
  const { verdict, failed } = reviewFinal(results, packQuestions(PACKS.final_extended));
  const audit = buildAudit({ runId, phase: "final", questions: results, result: verdict, model: res.model ?? deps.model });
  const trio = st.committee as { stockbot: unknown; bullbot: unknown; bearbot: unknown };
  const auth = verdict === "PASS" ? issueAuthorization(runId, "finalize", finalizeActionHash(answer, { sessionId, freezeId, stockbotHash: hashAction(trio.stockbot), bullbotHash: hashAction(trio.bullbot), bearbotHash: hashAction(trio.bearbot) })) : undefined;
  const authJson = auth ? { authorization_id: auth.id, kind: auth.kind satisfies AuthKind } : undefined;
  return { text: verdict === "PASS" ? "final PASS: answer may be published" : `final BLOCK: ${failed.map((f) => `${f.id}=${f.p_yes}`).join(", ")}`, details: { verdict, failed, questions: results, audit, ...(authJson ? { authorization: authJson } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: final review failed. Publication blocked.", details: { error: "final_review_failed" }, isError: true };
 }
}

export async function runReviewTool(name: string, args: unknown, deps: ReviewToolDeps = {}): Promise<NativeToolResult | undefined> {
 switch (name) {
  case "research_review_role_output": return roleTool(args, deps);
  case "research_review_committee": return committeeTool(args, deps);
  case "research_review_final": return finalTool(args, deps);
  default: return undefined;
 }
}

export function registerOutputReviewTools(pi: ExtensionAPI, deps: ReviewToolDeps = {}): void {
 const tool = (name: string, description: string, properties: Json): void => {
  pi.registerTool({
   name,
   label: name,
   description,
   parameters: nativeSchema(properties),
   async execute(_toolCallId: string, params: unknown) {
    const native = await runReviewTool(name, params, deps);
    if (!native) return { content: [{ type: "text", text: `Unknown review tool '${name}'. Transition blocked.` }], details: { error: "unknown_review_tool" }, isError: true };
    // Durable audit: same details.audit -> tool_result -> sqlite contract.
    return { content: [{ type: "text", text: native.text }], details: native.details, ...(native.isError === true ? { isError: true } : {}) };
   },
  });
 };
 tool("research_review_role_output", "Review one committee role output (common Q01-Q18 plus role pack S/B/R). ACCEPT issues a bound accept_role_output authorization; reject returns structured defects.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id"), role_job_id: anyProp("Committee role job id") });
 tool("research_review_committee", "Review the committee trio together (F01-F08). PASS issues committee_accepted; BLOCK plus probabilities otherwise.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id") });
 tool("research_review_final", "Final publication gate (extended Q+F pack). PASS issues a one-time finalize authorization; BLOCK returns failed criteria.", { research_session_id: anyProp("Research session id"), freeze_id: anyProp("Evidence freeze id"), answer: anyProp("Draft final answer") });
}
