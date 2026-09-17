/** Native TypeSafe output-review tools (role/committee/final).
 *
 * The committee never self-grades: each role output gets common Q01-Q18 plus
 * its role pack (S/B/R), the committee gets F01-F08, and the final answer gets
 * F09-F12. Critical failures return structured defects for OMP-driven revision;
 * TypeSafe never rewrites. Final PASS issues a one-time finalize authorization.
 */

import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import { type AuthKind, finalizeActionHash, hashAction, issueAuthorization } from "../lib/research-control.ts";
import { buildAudit } from "../lib/typesafe/audit.ts";
import { reviewCommittee, reviewFinal, reviewRoleOutput } from "../lib/typesafe/decisions.ts";
import { TypeSafeEvaluator } from "../lib/typesafe/evaluator.ts";
import { PACKS, QUESTION_BANK } from "../lib/typesafe/questions.ts";
import { YES_THRESHOLD } from "../lib/typesafe/thresholds.ts";
import type { JudgmentMap, JudgeQuestion, SystemOneEvaluator } from "../lib/typesafe/types.ts";
import { runIdOf, toolBag } from "./research-judge-tools.ts";
export interface ReviewToolDeps {
 evaluator?: SystemOneEvaluator;
 model?: string;
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
  const p = typeof raw === "number" && Number.isFinite(raw) && raw >= 0 && raw <= 1 ? raw : 0;
  out[id] = { p_yes: p, yes: p >= YES_THRESHOLD };
 }
 return out;
}

async function roleTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 const role = typeof bag.role === "string" ? bag.role : "";
 const rolePack = ROLE_PACKS[role];
 if (!rolePack) return { text: `Unknown role '${role}'. Expected stockbot|bullbot|bearbot. Revision blocked.`, details: { error: "unknown_role" }, isError: true };
 try {
  const ids = [...PACKS.common, ...rolePack];
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ role, objective: bag.objective, evidence: bag.evidence, output: bag.output }, packQuestions(ids));
  const results = packResults(res.results, ids);
  const { verdict, failed } = reviewRoleOutput(results, packQuestions(ids));
  const audit = buildAudit({ runId, phase: "role_output", questions: results, result: `${role}:${verdict}`, model: res.model ?? deps.model });
  const auth = verdict === "ACCEPT" ? issueAuthorization(runId, "accept_role_output", hashAction({ role, output: bag.output, results })) : undefined;
  const authJson = auth ? { authorization_id: auth.id, kind: auth.kind satisfies AuthKind } : undefined;
  const summary = verdict === "ACCEPT" ? `${role} output ACCEPT` : `${role} output REJECT_FOR_REVISION: ${failed.map((f) => `${f.id}=${f.p_yes}`).join(", ")}`;
  return { text: summary, details: { verdict, failed, questions: results, audit, ...(authJson ? { authorization: authJson } : {}) } };
 } catch {
  return { text: "TypeSafe unavailable: role-output review failed. Revision blocked.", details: { error: "role_review_failed" }, isError: true };
 }
}

async function committeeTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ objective: bag.objective, evidence: bag.evidence, stockbot: bag.stockbot, bullbot: bag.bullbot, bearbot: bag.bearbot }, packQuestions(PACKS.committee));
  const results = packResults(res.results, PACKS.committee);
  const { verdict, failed } = reviewCommittee(results, packQuestions(PACKS.committee));
  const audit = buildAudit({ runId, phase: "committee", questions: results, result: verdict, model: res.model ?? deps.model });
  return { text: verdict === "PASS" ? "committee PASS" : `committee BLOCK: ${failed.map((f) => `${f.id}=${f.p_yes}`).join(", ")}`, details: { verdict, failed, questions: results, audit } };
 } catch {
  return { text: "TypeSafe unavailable: committee review failed. Transition blocked.", details: { error: "committee_review_failed" }, isError: true };
 }
}

async function finalTool(args: unknown, deps: ReviewToolDeps): Promise<NativeToolResult> {
 const bag = toolBag(args);
 const runId = runIdOf(bag);
 try {
  const evaluator = deps.evaluator ?? new TypeSafeEvaluator();
  const res = await evaluator.evaluate({ objective: bag.objective, evidence: bag.evidence, committee: bag.committee, answer: bag.answer }, packQuestions(PACKS.final));
  const results = packResults(res.results, PACKS.final);
  const { verdict, failed } = reviewFinal(results, packQuestions(PACKS.final));
  const audit = buildAudit({ runId, phase: "final", questions: results, result: verdict, model: res.model ?? deps.model });
  const auth = verdict === "PASS" ? issueAuthorization(runId, "finalize", finalizeActionHash(bag.answer)) : undefined;
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
 tool("research_review_role_output", "Review one committee role output (common Q01-Q18 plus role pack S/B/R). ACCEPT issues a one-time accept_role_output authorization; reject returns structured defects.", { role: anyProp("stockbot|bullbot|bearbot"), objective: anyProp("Research objective under investigation"), evidence: anyProp("Frozen material evidence"), output: anyProp("Role output envelope"), run_id: anyProp("OMP run id for audit") });
 tool("research_review_committee", "Review the committee trio together (F01-F08). PASS/BLOCK plus probabilities; no authorization.", { objective: anyProp("Research objective under investigation"), evidence: anyProp("Frozen material evidence"), stockbot: anyProp("Stockbot output"), bullbot: anyProp("Bullbot output"), bearbot: anyProp("Bearbot output"), run_id: anyProp("OMP run id for audit") });
 tool("research_review_final", "Final publication gate (F09-F12). PASS issues a one-time finalize authorization; BLOCK returns failed criteria.", { objective: anyProp("Research objective under investigation"), evidence: anyProp("Frozen material evidence"), committee: anyProp("Committee outputs"), answer: anyProp("Draft final answer"), run_id: anyProp("OMP run id for audit") });
}
