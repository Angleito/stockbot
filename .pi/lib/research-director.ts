/** Staged ResearchDirector driver: fetch -> freeze -> atomic trio -> gate -> [next wave, while the gate authorizes] -> finalize.
 *
 * Pi owns the model loop; this module owns stage order only. Every transition is
 * kernel-gated fail-closed: predicates read research.session.inspect results, at most
 * one transition RPC fires per stage step, and kernel errors keep the run entry (nothing
 * is ever invented). The wave gate alone decides continue vs finalize, for every wave:
 * no wave ceiling lives here. The model does SEC dispatch and evidence authoring
 * through the call_tool verbs named in each prompt, while committee roles are authored
 * in fresh per-role contexts (pi's own provider defaults; STOCKBOT_PI_PROVIDER/
 * STOCKBOT_PI_MODEL override them when configured): one one-shot `pi` process per
 * role — never this conversation — with the frozen evidence text carried in its own
 * prompt. Every spawn, parse, or record failure falls back to authoring in this
 * context, so the staged flow never regresses. No cli.py, no thresholds, and no budgets
 * live here.
 *
 * Restart-safe: runs map runId to sessionId only; all stage state derives from
 * kernel inspect via deriveState, so /research-resume re-attaches a fresh run.
 */

import { spawn } from "node:child_process";

type Json = Record<string, unknown>;
export type BridgeCall = (req: Json) => Promise<Json>;

let bridge: BridgeCall = async () => ({ error: "bridge_unavailable" });
export function setResearchBridge(fn: BridgeCall): void {
 bridge = fn;
}

export type Advance = { done: false; prompt: string } | { done: true; answer: string } | null;
type Stage = "SOURCE_RESEARCH" | "COMMITTEE" | "FINAL";

interface Mem {
 sessionId: string;
}
const runs = new Map<string, Mem>();
export function clearResearchRun(runId: string): void {
 runs.delete(runId);
}
// Last inspect snapshot per session, for the UX-only stage gate in stockbot.ts
// (kernel remains authoritative). Fail open when nothing was seen yet.
interface InspectSnapshot { session: Json; jobs: Json[]; latestFreeze: Json | null }
const lastSeen = new Map<string, InspectSnapshot>();

const ROLES = ["stockbot", "bullbot", "bearbot"] as const;
const TERMINAL: Record<string, true> = { completed: true, failed: true, cancelled: true };

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] =>
 Array.isArray(v) ? v.filter((e): e is string => typeof e === "string") : [];
const objs = (v: unknown): Json[] =>
 Array.isArray(v) ? v.filter((e): e is Json => !!e && typeof e === "object") : [];
// Same-turn delivery renderer: the persisted rich final_result resolves to the
// substantive structured answer (Bottom line through filing refs + SEC scope
// line). Concise when simple — empty sections drop out — but never a bare
// "finalized" status line. Mirrors the kernel render_final_result shape.
function hasFinalSections(fr: Json): boolean {
 if (!fr || typeof fr !== "object") return false;
 return ["executive_summary", "consensus", "impact_channels", "first_order_effects", "second_order_effects", "bull_case", "bear_case", "critical_disagreements", "uncertainties", "evidence_limitations", "grounded_claims", "claims"].some((key) => {
  const value = (fr as Json)[key];
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === "object") return Object.keys(value).length > 0;
  return typeof value === "string" && value.trim().length > 0 && key !== "answer";
 });
}
function renderFinalAnswer(fr: Json): string {
 const idSuffix = (row: Json): string => {
  const raw = row.evidence_ids;
  const ids = Array.isArray(raw) ? raw.filter((e): e is string => typeof e === "string" && e.length > 0).slice(0, 6) : [];
  return ids.length > 0 ? ` [${ids.join(", ")}]` : "";
 };
 const kindSuffix = (row: Json): string => {
  const kind = str(row.claim_type).trim();
  return kind ? ` (${kind})` : "";
 };
 const claimLine = (item: unknown): string => {
  if (!item || typeof item !== "object") return "";
  const row = item as Json;
  const text = str(row.text).trim();
  return text ? `- ${text}${kindSuffix(row)}${idSuffix(row)}` : "";
 };
 const channelLine = (item: unknown): string => {
  if (!item || typeof item !== "object") return "";
  const row = item as Json;
  const name = str(row.name).trim() || "Exposure";
  const severity = str(row.severity).trim() || "direct";
  const explanation = str(row.explanation).trim();
  const detail = explanation && explanation !== name && explanation !== severity ? `: ${explanation}` : "";
  return `- ${name} (${severity})${detail}${idSuffix(row)}`;
 };
 const sideLines = (side: unknown): string[] => {
  if (!side || typeof side !== "object") return [];
  const row = side as Json;
  const summary = str(row.summary).trim();
  return summary ? [`- ${summary}${idSuffix(row)}`] : [];
 };
 const effectLines = (value: unknown): string[] => {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const item of value) {
   if (!item || typeof item !== "object") continue;
   const row = item as Json;
   const text = str(row.text).trim() || str(row.summary).trim() || str(row.name).trim();
   if (text) out.push(`- ${text}${kindSuffix(row)}${idSuffix(row)}`);
  }
  return out;
 };
 const bulleted = (value: unknown): string[] => strs(value).map((line) => `- ${line.trim()}`);
 const section = (header: string, lines: string[]): string[] => (lines.length > 0 ? [`## ${header}`, ...lines] : []);
 const summary = str(fr.executive_summary).trim() || str(fr.answer).trim() || str(fr.content).trim();
 const out: string[] = summary ? [`Bottom line: ${summary}`] : [];
 const consensus = str(fr.consensus).trim();
 if (consensus && consensus.toLowerCase() !== "none stated") out.push(`Consensus: ${consensus}`);
 out.push(...section("Major direct exposures", objs(fr.impact_channels).map(channelLine).filter((line) => line.length > 0)));
 out.push(...section("First-order effects", effectLines(fr.first_order_effects)));
 out.push(...section("Second-order effects", effectLines(fr.second_order_effects)));
 out.push(...section("Bull case", sideLines(fr.bull_case)));
 out.push(...section("Bear case", sideLines(fr.bear_case)));
 out.push(...section("Critical disagreements", bulleted(fr.critical_disagreements)));
 out.push(...section("Uncertainties", bulleted(fr.uncertainties)));
 out.push(...section("What would change the view", bulleted(fr.what_would_change)));
 out.push(...section("SEC-only limitations", bulleted(fr.evidence_limitations)));
 const rawClaims = Array.isArray(fr.grounded_claims) ? fr.grounded_claims : fr.claims;
 out.push(...section("Filing refs", objs(rawClaims).map(claimLine).filter((line) => line.length > 0)));
 const scopeRaw = fr.research_scope && typeof fr.research_scope === "object" ? (fr.research_scope as Json).allowed_sources : undefined;
 const names = Array.isArray(scopeRaw) ? scopeRaw.filter((e): e is string => typeof e === "string" && e.trim().length > 0) : [];
 const allowed = names.length > 0 ? names : ["SEC"];
 out.push(allowed.length === 1 && allowed[0].toUpperCase() === "SEC" ? "Scope: SEC filings only; nothing here draws on non-SEC sources." : `Scope: ${allowed.join(", ")} sources only.`);
 return out.join("\n\n");
}
// Authoritative staged context: Mem stays {sessionId}; the active job derives
// from the latest inspect cache as the running source_agent for the active
export function researchContextForRun(runId: string): { sessionId: string; jobId?: string } | undefined {
 const sid = runs.get(runId)?.sessionId;
 if (!sid) return undefined;
 const seen = lastSeen.get(sid);
 if (!seen) return { sessionId: sid };
 const cw = typeof seen.session.current_wave === "number" && Number.isInteger(seen.session.current_wave) ? (seen.session.current_wave as number) : 0;
 const wave = Math.max(cw, strs(seen.session.freeze_ids).length + 1, 1);
 const jid = str(seen.jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === wave && j.status === "running")?.job_id);
 return jid ? { sessionId: sid, jobId: jid } : { sessionId: sid };
}

async function rpc(op: string, args: Json, dataRoot?: string, asOf?: string): Promise<Json> {
 const req: Json = { op, ...args };
 if (dataRoot) req.data_root = dataRoot;
 if (typeof asOf === "string" && asOf.length > 0) req.as_of = asOf;
 const res = await bridge(req);
 if (typeof res.error === "string")
  throw new Error(`research ${op} failed: ${res.error}${typeof res.detail === "string" ? ` (${res.detail})` : ""}`);
 const result = res.result;
 if (!result || typeof result !== "object") throw new Error(`research ${op} failed: empty result`);
 return result as Json;
}
// Stage predicates read inspect results only: latest freeze, its committee jobs,
// plus running committee jobs not yet recorded for it (sorted for stable prompts).
interface TrioState { fid: string; wave: number; done: boolean; eligible: Json[] }
interface StageState {
 stage: Stage;
 jobId: string;
 targeted: string;
 activeWave: number;
 gateSettled: boolean;
 gateStopped: boolean;
 freezeEvidenceIds: string[];
 unfrozenEvidenceIds: string[];
 trio: TrioState;
}
function trioState(session: Json, jobs: Json[], frozenWave: number): TrioState {
 const freezes = strs(session.freeze_ids);
 const fid = freezes[freezes.length - 1] ?? "";
 const recorded = strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
 const recordedSet = new Set(recorded);
 const byId = new Map(jobs.map((j) => [str(j.job_id), j]));
 const isRole = (t: unknown): boolean => (ROLES as readonly string[]).includes(str(t));
 // Committee jobs must carry the freeze's wave (the kernel rejects a mismatch),
 // so the freeze record wins; the run entry and current_wave are fallbacks for
 // snapshots without a readable freeze record.
 const runWave = objs(session.committee_runs).find((e) => e.freeze_id === fid)?.wave_id;
 const sessionWave = typeof session.current_wave === "number" && Number.isInteger(session.current_wave) ? session.current_wave : 0;
 let wave = 1;
 if (frozenWave >= 1) wave = frozenWave;
 else if (typeof runWave === "number" && Number.isInteger(runWave)) wave = runWave;
 else if (sessionWave >= 1) wave = sessionWave;
 const recordedRoles = new Set<string>();
 for (const id of recorded) {
  const j = byId.get(id);
  if (!j || j.status !== "completed") continue;
  const t = str(j.job_type);
  if (isRole(t)) recordedRoles.add(t);
 }
 const eligible = jobs
  .filter((j) => j.status === "running" && isRole(j.job_type) && !recordedSet.has(str(j.job_id)) && j.wave_id === wave)
  .sort((a, b) => (str(a.job_id) < str(b.job_id) ? -1 : 1));
 return { fid, wave, done: recordedRoles.size === 3, eligible };
}

function deriveState(session: Json, jobs: Json[], latestFreeze?: Json | null): StageState {
 const status = str(session.status);
 const evidence = strs(session.evidence_ids);
 const freezes = strs(session.freeze_ids);
 const tq = str(session.targeted_question);
 const td = str(session.targeted_domain);
 const targeted = tq && td ? ` on ${td}: ${tq}` : tq || td;
 const currentWave = typeof session.current_wave === "number" && Number.isInteger(session.current_wave) ? session.current_wave : 0;
 // Active wave N: director drives N>=1 from kernel state; wave 1 auto-fetches SEC, later waves are gate-authorized.
 const activeWave = Math.max(currentWave, freezes.length + 1, 1);
 // Wave frozen derives from the freeze record's wave_id, never the freeze-id
 // shape, so non-synthesized ids still resume. Absent record (older stubs)
 // falls back to the freeze count.
 const freezeWaveId = latestFreeze && typeof latestFreeze.wave_id === "number" && Number.isInteger(latestFreeze.wave_id) ? latestFreeze.wave_id : null;
 const maxFrozenWave = freezeWaveId !== null && freezeWaveId !== undefined ? freezeWaveId : freezes.length;
 // The gate runs once per freeze: a source job past the latest freeze or a
 // session already waved past it means this freeze's gate authorized; a
 // kernel-stopped status means it declined. Neither re-runs the gate.
 const nextWaveJob = jobs.some((j) => str(j.job_type) === "source_agent" && typeof j.wave_id === "number" && j.wave_id > maxFrozenWave);
 const nextWaveActive = nextWaveJob || currentWave > maxFrozenWave;
 const gateStopped = status === "synthesizing" || Boolean(TERMINAL[status]);
 const gateSettled = nextWaveActive || gateStopped;
 // Committee/final prompts use the freeze's evidence ids, never the session's
 // broader mutable list; the difference only decides whether the active wave
 // gathered anything past the latest freeze.
 const freezeEvidenceIds = latestFreeze ? strs(latestFreeze.evidence_ids) : evidence;
 const frozenSet = new Set(freezeEvidenceIds);
 const unfrozenEvidenceIds = latestFreeze ? evidence.filter((id) => !frozenSet.has(id)) : [];
 const src1 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 1);
 const jobId = str(src1?.job_id ?? jobs[0]?.job_id);
 const trio = trioState(session, jobs, maxFrozenWave);
 // Mirrors kernel stage_for_session intent over the real lowercase statuses:
 // trio-complete on the latest freeze is SOURCE while its next wave is already
 // authorized, FINAL once the gate stops; a live freeze with trio incomplete is
 // COMMITTEE; else SOURCE.
 let stage: Stage = "SOURCE_RESEARCH";
 if (trio.done) {
  stage = nextWaveActive ? "SOURCE_RESEARCH" : "FINAL";
 } else if (freezes.length > 0 || status === "freezing" || status === "analyzing") {
  stage = "COMMITTEE";
 }
 if (status === "synthesizing" || status === "completed") stage = "FINAL";
 return { stage, jobId, targeted, activeWave, gateSettled, gateStopped, freezeEvidenceIds, unfrozenEvidenceIds, trio };
}

// UX-only mirror of the kernel stage gate (kernel stays authoritative):
// positive canonical control allowlists plus discovery, same reason string,
// fail open otherwise. COMMITTEE/FINAL therefore block every unlisted data tool.
const STAGE_GATE_DISCOVERY: Record<string, true> = { browse_tools: true, search_tools: true, describe_tool: true, list_tool_domains: true, call_tool: true };
const STAGE_GATE_SOURCE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_read_search: true, research_cancel: true, research_add_evidence: true, research_submit_source_result: true };
const STAGE_GATE_COMMITTEE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_add_analysis: true };
const STAGE_GATE_FINAL_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_finalize: true };
// Local controls, thesis actions, and dotted bridge ops are never staged data
// dispatches; SOURCE blocks them while unlisted data tools pass.
const STAGE_GATE_NON_DISPATCH: Record<string, true> = { research_start: true, research_add_evidence: true, research_submit_source_result: true, research_read_search: true, research_add_analysis: true, research_finalize: true, thesis_create: true, thesis_show: true, thesis_refine: true, thesis_watch: true, thesis_journal: true, thesis_status: true, "research.session.inspect": true, "research.session.resume": true, "research.session.finalize": true, "research.job.start": true, "research.job.complete": true, "research.freeze.create": true };
function stageBlockReason(stage: Stage, toolName: string): string | undefined {
 if (STAGE_GATE_DISCOVERY[toolName]) return undefined;
 if (stage === "COMMITTEE") return STAGE_GATE_COMMITTEE_EXTRA[toolName] ? undefined : `Stage ${stage} forbids tool '${toolName}'`;
 if (stage === "FINAL") return STAGE_GATE_FINAL_EXTRA[toolName] ? undefined : `Stage ${stage} forbids tool '${toolName}'`;
 if (STAGE_GATE_SOURCE_EXTRA[toolName]) return undefined;
 return STAGE_GATE_NON_DISPATCH[toolName] ? `Stage ${stage} forbids tool '${toolName}'` : undefined;
}
export function blockReasonForRun(runId: string, toolName: string): string | undefined {
 const sid = runs.get(runId)?.sessionId;
 if (!sid) return undefined;
 const seen = lastSeen.get(sid);
 if (!seen) return undefined;
 return stageBlockReason(deriveState(seen.session, seen.jobs, seen.latestFreeze).stage, toolName);
}

async function inspect(sessionId: string, dataRoot?: string, asOf?: string): Promise<InspectSnapshot> {
 const res = await rpc("research.session.inspect", { session_id: sessionId }, dataRoot, asOf);
 if (!res.session || typeof res.session !== "object")
  throw new Error("research.session.inspect failed: missing session");
 const lf = res.latest_freeze;
 const out: InspectSnapshot = { session: res.session as Json, jobs: objs(res.jobs), latestFreeze: lf && typeof lf === "object" ? (lf as Json) : null };
 lastSeen.set(sessionId, out);
 return out;
}
// Evidence item shape. SEC search hits are navigation artifacts: a fact counts
// only when the opened document carries its accession, its name, and a raw
// passage. claim_kind routes the two supported kinds.
const ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher, e.g. SEC>", ` +
 `"source_uri": "<canonical document URL>", "source_record_id": "<SEC accession, e.g. 0000320193-24-000123>", "document_name": "<filing or exhibit you opened>", ` +
 `"matching_passage": "<raw passage quoted from that document>", "known_at": "<ISO-8601 timestamp>", "claim_kind": "observed_fact"}`;
// One source workflow for every wave: navigation-only search, raw-document
// evidence, no limits, coverage submitted structurally.
const SOURCE_WORKFLOW =
 `Work it as a branch map: name the material branches this question needs, search SEC for each, open the filings behind every hit, read the documents/exhibits/passages, and record only raw-document-backed evidence. ` +
 `There is no maximum number of searches, filing reads, document reads, exhibit reads, or waves: keep going while the work is materially useful; the only waste is an exact repeat that adds nothing. ` +
 `Search results are navigation artifacts, never evidence: opening the document is what makes a finding citable. ` +
 `An observed_fact needs source_record_id (the SEC accession), document_name, and a raw passage (matching_passage, or passage/section) — anything less fails closed. ` +
 `An absence_observation instead needs claim_kind "absence_observation" with search_id, query, and the searched coverage (forms, dates, partitions, entities, docs, gaps, pagination_complete, complete), and must not carry an accession. ` +
 `Track entities, forms, exhibits, branches covered/remaining, and the questions still open as you go: sufficiency is coverage of what you checked, never a whole-question answer. `;

function sourceSteps(sessionId: string, jobId: string, asOf?: string): string {
 const cutoff = asOf && asOf.length > 0 ? `known_at must be an ISO-8601 timestamp on or before the session cutoff ${asOf}` : `no session cutoff applies; known_at may be any ISO-8601 timestamp or omitted`;
 return (
  `${SOURCE_WORKFLOW}Record each finding with Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sessionId}", "job_id": "${jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated and ${cutoff}; unprovenanced or out-of-order calls fail closed. ` +
  `When this source investigation is complete, you MUST call research_submit_source_result exactly once with the structured coverage {"useful_for_question": "sufficient"|"insufficient", ...}, evidence_ids, and unresolved_questions, then stop. ` +
  `That ends this source job and returns control to the Director, which owns freezing, waves, and the session; it never freezes, ends a wave, or ends the session. Do not attempt to freeze.`
 );
}

function fetchPrompt(sessionId: string, jobId: string, question: string, asOf?: string): string {
 return (
  `Research session ${sessionId} created for "${question}". Fetch evidence now: dispatch SEC research with ` +
  `Call call_tool with name="search_sec_filings" (or list_sec_filings / get_sec_document), then open what you find. ` +
  sourceSteps(sessionId, jobId, asOf)
 );
}
// Analysis shape: the rich committee envelope, every factual claim cited to a
// frozen evidence id. One contract string serves both authoring paths (the
// in-context research_add_analysis call and the per-role spawn prompt).
const ANALYSIS_ENVELOPE =
 `{"executive_view": "<your read in 1-3 sentences>", "claims": [{"text": "<finding>", "claim_type": "observed_fact"|"inference"|"unknown"|"contradicted", "evidence_ids": ["<frozen evidence id>"]}], ` +
 `"impact_channels": [{"text": "<how it transmits>", "evidence_ids": ["<frozen evidence id>"], "direction": "positive"|"negative"|"mixed"}], ` +
 `"materiality": {"overall": "critical"|"high"|"medium"|"low", "reasoning": "<why>"}, "uncertainties": ["<open question>"], "what_would_change": ["<observable change>"], "follow_ups": ["<question?>"]}`;

function wavePrompt(wave: number, sessionId: string, jobId: string, targeted: string, asOf?: string): string {
 const label = wave <= 1 ? "Wave-1" : `Wave-${wave}`;
 return (
  `${label} authorized for research session ${sessionId}${targeted}. Fetch this wave's evidence with ` +
  `Call call_tool with name="search_sec_filings" (or get_material_events / get_sec_document), then open what you find. ` +
  sourceSteps(sessionId, jobId, asOf) +
  ` If evidence is insufficient, include it in unresolved_questions; the Director decides the next wave.`
 );
}

function trioJobIdsForFreeze(session: Json, fid: string): string[] {
 return strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
}

function trioPrompt(sessionId: string, freezeId: string, mapping: Json[], allowedIds: string, note = ""): string {
 const calls = mapping
  .map(
   (j) =>
    `Call call_tool with name="research_add_analysis" and arguments={"session_id": "${sessionId}", "job_id": "${str(j.job_id)}", "role": "${str(j.job_type)}", "analysis": ${ANALYSIS_ENVELOPE}}`,
  )
  .join(" ");
 return (
  `${note}Fresh context? Reload first: Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "freeze", "resource_id": "${freezeId}"}, ` +
  `then Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "evidence", "resource_id": "<id>"} for each id you will cite. ` +
  `Committee may READ frozen state; it must NOT fetch new evidence. Evidence frozen as ${freezeId}. Author each role now, one call per role (${calls}). ` +
  `Every factual claim must cite the frozen evidence ids it rests on — observed_fact and contradicted need at least one, inference needs at least one, unknown may carry none — and the only allowed ids are: ${allowedIds}. ` +
  `Unknown ids fail closed naming them.`
 );
}

function finalizePrompt(sessionId: string, freezeId: string, allowedIds: string, note = "", trioJobIds: string[] = []): string {
 const freezeRead =
  `Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "freeze", "resource_id": "${freezeId}"}`;
 const jobReads = trioJobIds
  .map(
   (jid) =>
    `Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "job", "resource_id": "${jid}"}`,
  )
  .join(", ");
 const reload =
  jobReads.length > 0
   ? `Fresh context? Reload first: ${freezeRead}, ${jobReads} for persisted committee outputs, and kind "evidence" reads as needed. Then `
   : `Fresh context? Reload first: ${freezeRead}, and kind "evidence" reads as needed. Then `;
 return (
  `${reload}${note}Evidence frozen as ${freezeId}. Finalize now with a substantive structured synthesis: ` +
  `Call call_tool with name="research_finalize" and arguments={"session_id": "${sessionId}", "answer": "<Bottom line plus Major direct exposures / Second-order / Bull / Bear / Uncertainties / SEC-only limitations, every factual claim tied to evidence ids>", ` +
  `"claims": [{"text": "<finding>", "evidence_ids": ["<allowed evidence id>"]}]}. ` +
  `Every claim ref must use these frozen evidence ids: ${allowedIds}. Empty claims are rejected (claims_required); unknown ids fail closed naming them. ` +
  `The kernel persists the rich final_result and the session's final answer is rendered from it once, in this same turn — do not restate it as chat text after the call.`
 );
}

// Fail-closed freeze: the kernel rejects open source jobs, so the model must
// end source work via research_submit_source_result first. No force-complete here.
async function freezeWave(sessionId: string, wave: number, dataRoot?: string, asOf?: string): Promise<Json> {
 return rpc("research.freeze.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
}

// --- fresh per-role contexts -------------------------------------------------
// Kernel role semantics: the same frozen evidence, read three independent ways.
const ROLE_ORDERS: Record<string, string> = {
 stockbot: "You are Stockbot: the balanced base case. State what the frozen evidence most likely implies, weighted as it stands.",
 bullbot: "You are Bullbot: the resilience case. State the strongest limited-damage reading the frozen evidence actually supports.",
 bearbot: "You are Bearbot: the downside case. State the strongest contagion reading the frozen evidence actually supports.",
};
const ROLE_RULES =
 `Each claim is exactly {"text", "claim_type", "evidence_ids"}; claim_type is one of observed_fact / inference / unknown / contradicted, ` +
 `where observed_fact, inference and contradicted need at least one frozen evidence id and unknown may cite none. ` +
 `Each impact channel is {"text", "direction", "evidence_ids"} with at least one frozen evidence id and direction positive, negative or mixed. ` +
 `materiality is {"overall": "critical"|"high"|"medium"|"low", "reasoning": "<why>"}; uncertainties and what_would_change are plain string lists; ` +
 `every follow_up is a question string of 12-500 characters ending in "?". ` +
 `Argue only what the evidence supports: no fabricated optimism, no fabricated pessimism, and mark what the evidence cannot settle as unknown. `;

export type RoleSpawn = (prompt: string) => Promise<string>;
// Production default: the one-shot `pi` process (flag-less unless the env
// override is configured). Tests and embedders set `null`, which declares fresh
// role contexts unavailable — a null seam never spawns anything.
let roleSpawn: RoleSpawn | null = spawnPiRole;
export function setRoleSpawn(fn: RoleSpawn | null): void {
 roleSpawn = fn;
}
// One model call plus process start-up; mirrors the verify harness's 300s run cap.
const ROLE_SPAWN_TIMEOUT_MS = 300_000;

/** Frozen per-role CLI argv; pi resolves the provider/model when no override is set. */
export function roleSpawnCommand(prompt: string): string[] {
 const provider = (process.env.STOCKBOT_PI_PROVIDER ?? "").trim();
 const model = (process.env.STOCKBOT_PI_MODEL ?? "").trim();
 const flags = [...(provider ? ["--provider", provider] : []), ...(model ? ["--model", model] : [])];
 return [
  "pi", "-p", "--no-session", "--no-builtin-tools", "--no-extensions", "--no-skills",
  "--no-prompt-templates", "--no-context-files", ...flags, "--", prompt,
 ];
}

// One-shot role process: stdout only, killed on timeout, "" on any failure (the
// caller falls back to in-context authoring, so nothing throws past it).
function spawnPiRole(prompt: string): Promise<string> {
 const argv = roleSpawnCommand(prompt);
 return new Promise<string>((resolve) => {
  let settled = false;
  let timer: ReturnType<typeof setTimeout>;
  const finish = (out: string): void => {
   if (settled) return;
   settled = true;
   clearTimeout(timer);
   resolve(out);
  };
  const child = spawn(argv[0], argv.slice(1), { stdio: ["ignore", "pipe", "ignore"] });
  let out = "";
  timer = setTimeout(() => {
   try {
    child.kill("SIGKILL");
   } catch {
    // already gone
   }
   finish("");
  }, ROLE_SPAWN_TIMEOUT_MS);
  child.stdout?.on("data", (chunk) => {
   out += String(chunk);
  });
  child.on("error", () => finish(""));
  child.on("close", () => finish(out));
 });
}

function rolePrompt(role: string, question: string, freezeId: string, allowedIds: string, evidenceText: string): string {
 return (
  `${ROLE_ORDERS[role] ?? `You are the ${role} analyst on this committee.`}\n` +
  `Question under research: ${question}\n` +
  `Frozen evidence ${freezeId} is reproduced below in full; it is your only material and you have no tools.\n${evidenceText}\n` +
  `${ROLE_RULES}Cite only these frozen evidence ids: ${allowedIds}. ` +
  `Respond with raw JSON only — no prose, no code fences — in exactly this shape: ${ANALYSIS_ENVELOPE}`
 );
}

// Frozen evidence text for one freeze, read with the same verb the in-context
// prompt names (research_read over the bridge), so a role sees exactly what the
// kernel froze. Any unreadable record fails the whole read (no partial evidence).
async function frozenEvidenceText(sessionId: string, freezeId: string, ids: string[], dataRoot?: string, asOf?: string): Promise<string> {
 const readText = async (kind: string, resourceId: string): Promise<string> => {
  const res = await rpc("tool.invoke", { name: "research_read", arguments: { session_id: sessionId, kind, resource_id: resourceId } }, dataRoot, asOf);
  return str(res.content).trim();
 };
 const freezeText = await readText("freeze", freezeId);
 if (!freezeText) throw new Error(`freeze ${freezeId} unreadable`);
 const evidence = await Promise.all(ids.map((id) => readText("evidence", id)));
 if (evidence.some((text) => text.length === 0)) throw new Error("frozen evidence unreadable");
 return [freezeText, ...evidence].join("\n\n");
}

// Model text at a trust boundary: take the JSON object and let the kernel
// validate the envelope (missing keys and unknown ids fail there, not here).
function parseRoleEnvelope(text: string): Json {
 const body = text.trim();
 const start = body.indexOf("{");
 const end = body.lastIndexOf("}");
 if (start < 0 || end <= start) throw new Error("role context returned no JSON envelope");
 const parsed: unknown = JSON.parse(body.slice(start, end + 1));
 if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("role context returned a non-object envelope");
 return parsed as Json;
}

// Author + record every role in its own context. Null once all three are recorded;
// otherwise the note explaining the fallback, which the caller reports in its prompt.
async function authorTrioInFreshContexts(
 sessionId: string, question: string, freezeId: string, allowedIds: string[], roles: Json[], dataRoot?: string, asOf?: string,
): Promise<string | null> {
 // No seam: fresh role contexts are unavailable by construction, so no frozen
 // evidence is read and nothing can spawn (a null seam never starts a real `pi`).
 const spawnRole = roleSpawn;
 if (!spawnRole) return "Role contexts unavailable (role spawns are disabled); author in this context. ";
 try {
  const evidenceText = await frozenEvidenceText(sessionId, freezeId, allowedIds, dataRoot, asOf);
  const jobs = roles.map((job) => ({ job, prompt: rolePrompt(str(job.job_type), question, freezeId, allowedIds.join(", "), evidenceText) }));
  // Every role process starts before any is awaited: three contexts, one wall clock.
  const envelopes = (await Promise.all(jobs.map((j) => spawnRole(j.prompt)))).map((text) => parseRoleEnvelope(text));
  await Promise.all(
   jobs.map((j, i) =>
    rpc("research.analysis.record", { session_id: sessionId, job_id: str(j.job.job_id), role: str(j.job.job_type), analysis: envelopes[i] }, dataRoot, asOf),
   ),
  );
  return null;
 } catch (err) {
  return `Role contexts unavailable (${err instanceof Error ? err.message : String(err)}); author in this context. `;
 }
}

// The question the roles answer: the session's question plus this wave's target.
function committeeQuestion(session: Json, sessionId: string): string {
 const base = str(session.query) || str(session.objective) || sessionId;
 const targeted = str(session.targeted_question);
 return targeted ? `${base} Follow-up for this wave: ${targeted}` : base;
}

// Which authoring path ran, stated in the prompt the model receives next.
const NOTE_ROLES_AUTHORED = "Committee roles were authored and recorded in three fresh per-role contexts. ";

// Atomic trio: one RPC creates every missing committee job (status RUNNING)
// together, so no role is ever started, awaited, and only then followed by the
// next. A role already running is reused; a recorded role is left alone.
// The driver authors + records the roles in fresh per-role contexts here; on any
// spawn/parse/record failure the returned prompt authors the remaining roles in
// this context.
async function seedTrio(runId: string, sessionId: string, wave: number, dataRoot?: string, asOf?: string, note = ""): Promise<Advance> {
 try {
  await rpc("research.committee.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Committee creation for research session ${sessionId} failed (${err instanceof Error ? err.message : String(err)}). Keep authoring analyses with Call call_tool with name="research_add_analysis".` };
 }
 let snapshot: InspectSnapshot;
 try {
  snapshot = await inspect(sessionId, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Research session ${sessionId} unreadable after committee creation (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
 }
 const d = deriveState(snapshot.session, snapshot.jobs, snapshot.latestFreeze);
 const allowed = d.freezeEvidenceIds.join(", ");
 if (d.trio.done) {
  return { done: false, prompt: finalizePrompt(sessionId, d.trio.fid, allowed, `${note}Trio complete for research session ${sessionId}. `, trioJobIdsForFreeze(snapshot.session, d.trio.fid)) };
 }
 const failure = await authorTrioInFreshContexts(sessionId, committeeQuestion(snapshot.session, sessionId), d.trio.fid, d.freezeEvidenceIds, d.trio.eligible, dataRoot, asOf);
 // Kernel truth decides: roles recorded by the fresh contexts complete the trio,
 // and a partial record leaves only the survivors for in-context authoring.
 const current = await freshState(sessionId, d, dataRoot, asOf);
 if (current.trio.done) {
  // Kernel truth: every role is recorded, so this same advance continues into
  // the kernel's next state (the wave gate, or finalize on a decline).
  const after = await advanceOnAgentEnd(runId, "", dataRoot, asOf);
  return after && !after.done ? { done: false, prompt: `${NOTE_ROLES_AUTHORED}${after.prompt}` } : after;
 }
 // Not every role is recorded: the note says which authoring path ran and the
 // prompt covers exactly the roles the kernel still has open.
 return { done: false, prompt: trioPrompt(sessionId, current.trio.fid, current.trio.eligible, current.freezeEvidenceIds.join(", "), `${note}${failure ?? ""}`) };
}

// Roles recorded by a spawn attempt change the eligible set, so re-read the
// kernel after one; a failed read keeps the snapshot the attempt started from.
async function freshState(sessionId: string, fallback: StageState, dataRoot?: string, asOf?: string): Promise<StageState> {
 try {
  const snapshot = await inspect(sessionId, dataRoot, asOf);
  return deriveState(snapshot.session, snapshot.jobs, snapshot.latestFreeze);
 } catch {
  return fallback;
 }
}

export async function startResearch(
 question: string,
 runId: string,
 dataRoot?: string,
 asOf?: string,
): Promise<{ sessionId: string; prompt: string }> {
 const created = await rpc("research.session.create", { question }, dataRoot, asOf);
 const sessionId = str(created.session_id);
 if (!sessionId) throw new Error("research.session.create failed: missing session_id");
 const { session, jobs, latestFreeze } = await inspect(sessionId, dataRoot, asOf);
 runs.set(runId, { sessionId });
 return { sessionId, prompt: fetchPrompt(sessionId, deriveState(session, jobs, latestFreeze).jobId, question, asOf) };
}

export async function resumeResearch(
 sessionId: string,
 runId: string,
 dataRoot?: string,
 asOf?: string,
): Promise<{ sessionId: string; prompt: string }> {
 runs.set(runId, { sessionId });
 const adv = await advanceOnAgentEnd(runId, "", dataRoot, asOf);
 // Terminal answers reuse the persisted final answer and leave no active run
 // binding (advance deletes it); every other state reuses the advance prompt.
 if (!adv) return { sessionId, prompt: `Resumed research session ${sessionId} is complete.` };
 if (adv.done) return { sessionId, prompt: `Resumed research session ${sessionId} is complete${adv.answer ? `: ${adv.answer}` : "."}` };
 return { sessionId, prompt: adv.prompt };
}

export async function advanceOnAgentEnd(runId: string, answer = "", dataRoot?: string, asOf?: string): Promise<Advance> {
 const m = runs.get(runId);
 if (!m) return null;
 const sid = m.sessionId;
 let snapshot: InspectSnapshot;
 try {
  snapshot = await inspect(sid, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Research session ${sid} unreadable (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
 }
 const { session, jobs, latestFreeze } = snapshot;
 const final = session.final_result;
 if ((final && typeof final === "object") || TERMINAL[str(session.status)]) {
  runs.delete(runId);
  const fr = (final && typeof final === "object" ? final : {}) as Json;
  const rendered = renderFinalAnswer(fr);
  // Same-turn delivery: a rich final_result resolves to the substantive
  // structured answer. A thin stub (answer only, no sections) falls back to
  // the raw kernel answer so stubbed/legacy payloads keep exact text.
  const rich = hasFinalSections(fr) ? rendered : "";
  return { done: true, answer: rich || str(fr.answer) || str(fr.content) || answer };
 }
 const d = deriveState(session, jobs, latestFreeze);
 const evidence = strs(session.evidence_ids);
 if (evidence.length === 0) {
  const activeSrc = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === d.activeWave);
  const activeStatus = str(activeSrc?.status);
  if (!activeSrc || activeStatus === "running" || activeStatus === "queued") {
   return { done: false, prompt: fetchPrompt(sid, d.jobId, str(session.query) || str(session.objective) || sid, asOf) };
  }
  // Active source job closed with no evidence: fall through to freeze;
  // empty-with-limitations freeze succeeds kernel-side and carries limitations forward.
 }
 const freezes = strs(session.freeze_ids);
 if (freezes.length === 0) {
  const src1 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 1);
  const src1Status = str(src1?.status);
  if (src1 && (src1Status === "running" || src1Status === "queued"))
   return { done: false, prompt: `Source still running for research session ${sid}. ${sourceSteps(sid, str(src1.job_id), asOf)}` };
  let frozen: Json;
  try {
   frozen = await freezeWave(sid, 1, dataRoot, asOf);
  } catch (err) {
   return { done: false, prompt: `Freeze for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
  }
  const reason = str((frozen.pending_next_action as Json | undefined)?.reason ?? (latestFreeze?.pending_next_action as Json | undefined)?.reason ?? "");
  const limit = reason ? ` Evidence limitations: ${reason}.` : "";
  return seedTrio(runId, sid, 1, dataRoot, asOf, limit ? `${limit} ` : "");
 }
 if (!d.trio.done) {
  // Committee stage: one atomic RPC guarantees the whole trio exists RUNNING
  // before any authoring prompt (existing role jobs are reused, recorded roles
  // stay out of the prompt); with provider/model configured the roles are
  // authored + recorded in fresh per-role contexts inside seedTrio.
  return seedTrio(runId, sid, d.trio.wave, dataRoot, asOf);
 }
 // Trio complete for the latest freeze: the wave gate alone decides continue vs
 // finalize, for every wave — there is no wave ceiling.
 const trioIds = trioJobIdsForFreeze(session, d.trio.fid);
 if (d.gateStopped) {
  // Restart after a gate stop: the kernel already declined this freeze.
  return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Wave gate stopped research session ${sid}. `, trioIds) };
 }
 let targeted = d.targeted;
 if (!d.gateSettled) {
  let dec: Json;
  try {
   dec = await rpc("research.wave.decide", { session_id: sid }, dataRoot, asOf);
  } catch (err) {
   return { done: false, prompt: `Wave gate for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
  }
  if (dec.authorized !== true)
   return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Wave gate stopped research session ${sid} (${str(dec.stop_reason) || "no further wave"}). `, trioIds) };
  const tq = str(dec.targeted_question);
  const td = str(dec.targeted_domain);
  targeted = tq && td ? ` on ${td}: ${tq}` : tq || td;
 }
 // Authorized: drive the next wave's source job, fetch, freeze, trio, gate.
 const N = d.activeWave;
 const activeSrc = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === N);
 if (!str(activeSrc?.job_id)) {
  try {
   const started = await rpc("research.job.start", { session_id: sid, type: "source_agent", wave_id: N }, dataRoot, asOf);
   const nid = str(started.job_id);
   if (!nid) throw new Error("research.job.start failed: missing job_id");
   try {
    await inspect(sid, dataRoot, asOf);
   } catch {
    // inspect refresh is best-effort; the explicit nid still drives the prompt
   }
   return { done: false, prompt: wavePrompt(N, sid, nid, targeted, asOf) };
  } catch (err) {
   return { done: false, prompt: `Wave-${N} source job for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
  }
 }
 // Wave-N source work is underway: without evidence past the latest freeze the
 // driver keeps fetching; the wave-N freeze fires only once new evidence lands.
 const activeStatus = str(activeSrc?.status);
 if (activeSrc && (activeStatus === "running" || activeStatus === "queued") && d.unfrozenEvidenceIds.length === 0) {
  return { done: false, prompt: wavePrompt(N, sid, str(activeSrc.job_id), targeted, asOf) };
 }
 let frozenN: Json;
 try {
  frozenN = await freezeWave(sid, N, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Freeze for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
 }
 const reasonN = str((frozenN.pending_next_action as Json | undefined)?.reason ?? "");
 const limitN = reasonN ? ` Evidence limitations: ${reasonN}.` : "";
 return seedTrio(runId, sid, N, dataRoot, asOf, limitN ? `${limitN} ` : "");
}
