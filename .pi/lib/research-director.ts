/** Staged ResearchDirector driver: fetch -> freeze -> trio -> gate -> [waves 2..N, director-set] -> finalize.
 *
 * Pi owns the model loop; this module owns stage order only. Every transition is
 * kernel-gated fail-closed: predicates read research.session.inspect results, at most
 * one transition RPC fires per advance, and kernel errors keep the run entry (nothing
 * is ever invented). The model does SEC dispatch plus evidence/trio authoring through
 * the call_tool verbs named in each prompt. No model invocation, no cli.py, no
 * thresholds, budgets, or wire shapes live here.
 *
 * Restart-safe: runs map runId to sessionId only; all stage state derives from
 * kernel inspect via deriveState, so /research-resume re-attaches a fresh run.
 */

type Json = Record<string, unknown>;
export type BridgeCall = (req: Json) => Promise<Json>;

let bridge: BridgeCall = async () => ({ error: "bridge_unavailable" });
export function setResearchBridge(fn: BridgeCall): void {
 bridge = fn;
}

export type Advance = { done: false; prompt: string } | { done: true; answer: string } | null;
type Stage = "SOURCE_RESEARCH" | "COMMITTEE" | "FINAL";

// ponytail: Mem is session pointer plus a local fetch backstop; stage still derives
// from kernel inspect, but the attempt count itself is in-memory. Ceiling: restart or
// resume resets the count, worst case 3 extra fetch prompts per restart. Terminal
// persists via kernel research.session.cancel, so resume after cancel sees TERMINAL
// and returns done. Upgrade path: kernel-persisted attempt count if restarts mid-fetch matter.
const MAX_FETCH_ATTEMPTS = 3;
interface Mem {
 sessionId: string;
 fetchAttempts: number;
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
// Authoritative staged context: Mem stays {sessionId}; the active job derives
// from the latest inspect cache as the running source_agent for the active
// wave max(current_wave, freeze_ids.length + 1, 1).
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
interface TrioState { fid: string; wave: number; have: number; done: boolean; eligible: Json[]; covered: Set<string> }
function trioState(session: Json, jobs: Json[]): TrioState {
 const freezes = strs(session.freeze_ids);
 const fid = freezes[freezes.length - 1] ?? "";
 const recorded = strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
 const recordedSet = new Set(recorded);
 const byId = new Map(jobs.map((j) => [str(j.job_id), j]));
 const isRole = (t: unknown): boolean => (ROLES as readonly string[]).includes(str(t));
 const runWave = objs(session.committee_runs).find((e) => e.freeze_id === fid)?.wave_id;
 let wave = 1;
 if (typeof runWave === "number" && Number.isInteger(runWave)) wave = runWave;
 else if (typeof session.current_wave === "number" && Number.isInteger(session.current_wave)) wave = session.current_wave;
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
 const covered = new Set<string>(recordedRoles);
 for (const j of eligible) covered.add(str(j.job_type));
 return { fid, wave, have: recordedRoles.size, done: recordedRoles.size === 3, eligible, covered };
}

// Journal fallback: inspect may one day carry events; absence just means undecided.
function journalTypes(session: Json): Set<string> {
 const out = new Set<string>();
 for (const key of ["journal", "events"]) {
  const v = session[key];
  if (!Array.isArray(v)) continue;
  for (const e of v) {
   if (!e || typeof e !== "object") continue;
   const t = (e as Json).event_type ?? (e as Json).type;
   if (typeof t === "string") out.add(t);
  }
 }
 return out;
}

function deriveState(session: Json, jobs: Json[], latestFreeze?: Json | null): {
 stage: Stage; jobId: string; decided: boolean; authorized: boolean;
 baseline: number; activeWave: number; waveFrozen: boolean; targeted: string;
 activeWaveJobId: string; freezeEvidenceIds: string[]; unfrozenEvidenceIds: string[];
 wave2Frozen: boolean; wave2JobId: string; trio: TrioState;
} {
 const sid = str(session.session_id);
 const status = str(session.status);
 const evidence = strs(session.evidence_ids);
 const freezes = strs(session.freeze_ids);
 const journal = journalTypes(session);
 const tq = str(session.targeted_question);
 const td = str(session.targeted_domain);
 const targeted = tq && td ? ` on ${td}: ${tq}` : tq || td;
 const currentWave = typeof session.current_wave === "number" && Number.isInteger(session.current_wave) ? session.current_wave : 0;
 // Active wave N: director drives N>=1 from kernel state; wave 1 auto-fetches SEC, waves 2..N are director-directed.
 const activeWave = Math.max(currentWave, freezes.length + 1, 1);
 const authorized =
  status === "targeted_research" || currentWave >= 2 || targeted.length > 0 ||
  journal.has("wave.authorized");
 // ponytail: "analyzing" is pre-decide (the trio runs there), so it must NOT imply
 // decided — otherwise trio-complete sessions could never reach decide/finalize.
 const decided =
  journal.has("wave.authorized") || journal.has("wave.stopped") ||
  status === "targeted_research" || status === "synthesizing" ||
  status === "completed" || currentWave >= 2 || targeted.length > 0;
 // ponytail: wave frozen derives from the freeze record's wave_id, never the
 // freeze-id shape, so non-synthesized ids still resume to finalize. Absent
 // record (older stubs) falls back to the synthesized id check.
 const freezeWaveId = latestFreeze && typeof latestFreeze.wave_id === "number" && Number.isInteger(latestFreeze.wave_id) ? latestFreeze.wave_id : null;
 const maxFrozenWave = freezeWaveId !== null && freezeWaveId !== undefined ? freezeWaveId : freezes.length;
 // ponytail: active-wave frozen = latest freeze covers N; trio-complete N+1 probe finalizes via the N>2 cap, open trio stays COMMITTEE. Ceiling: none, any N.
 const waveFrozen = maxFrozenWave >= activeWave && freezes.length > 0;
 const wave2Frozen = freezeWaveId !== null && freezeWaveId !== undefined ? freezeWaveId >= 2 : freezes.includes(`${sid}:2:freeze`);
 // Committee/final prompts use the freeze's evidence ids, never the session's
 // broader mutable list; the difference only decides whether the active wave
 // gathered anything past the latest freeze.
 const freezeEvidenceIds = latestFreeze ? strs(latestFreeze.evidence_ids) : evidence;
 const frozenSet = new Set(freezeEvidenceIds);
 const unfrozenEvidenceIds = latestFreeze ? evidence.filter((id) => !frozenSet.has(id)) : [];
 const src1 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 1);
 const activeSrc = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === activeWave);
 const src2 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 2);
 const jobId = str(src1?.job_id ?? jobs[0]?.job_id);
 const activeWaveJobId = str(activeSrc?.job_id);
 const wave2JobId = str(src2?.job_id);
 // ponytail: the true E1 evidence count lives in the freeze store, which TS cannot
 // read via inspect; best-effort current count (freeze decisions key off wave-2
 // job existence instead, so this stays informational).
 const baseline = freezes.length === 0 ? 0 : evidence.length;
 const trio = trioState(session, jobs);
 // Mirrors kernel stage_for_session intent over the real lowercase statuses:
 // trio-complete on the latest freeze is FINAL (wave-2 fetch window re-opens
 // SOURCE); a live freeze with trio incomplete is COMMITTEE; else SOURCE.
 let stage: Stage = "SOURCE_RESEARCH";
 if (trio.done) {
  stage = authorized && !wave2Frozen ? "SOURCE_RESEARCH" : "FINAL";
 } else if (freezes.length > 0 || status === "freezing" || status === "analyzing") {
  stage = "COMMITTEE";
 }
 if (status === "synthesizing" || status === "completed") stage = "FINAL";
 return { stage, jobId, decided, authorized, baseline, activeWave, waveFrozen, targeted, activeWaveJobId, freezeEvidenceIds, unfrozenEvidenceIds, wave2Frozen, wave2JobId, trio };
}

// UX-only mirror of the kernel stage gate (kernel stays authoritative):
// positive canonical control allowlists plus discovery, same reason string,
// fail open otherwise. COMMITTEE/FINAL therefore block every unlisted data tool.
const STAGE_GATE_DISCOVERY: Record<string, true> = { browse_tools: true, search_tools: true, describe_tool: true, list_tool_domains: true, call_tool: true };
const STAGE_GATE_SOURCE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_add_evidence: true, research_submit_source_result: true };
const STAGE_GATE_COMMITTEE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_add_analysis: true };
const STAGE_GATE_FINAL_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_finalize: true };
// Local controls, thesis actions, and dotted bridge ops are never staged data
// dispatches; SOURCE blocks them while unlisted data tools pass.
const STAGE_GATE_NON_DISPATCH: Record<string, true> = { research_start: true, research_add_evidence: true, research_submit_source_result: true, research_add_analysis: true, research_finalize: true, thesis_create: true, thesis_show: true, thesis_refine: true, thesis_watch: true, thesis_journal: true, thesis_status: true, "research.session.inspect": true, "research.session.resume": true, "research.session.finalize": true, "research.job.start": true, "research.job.complete": true, "research.freeze.create": true };
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
const ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher, e.g. SEC>", ` +
 `"source_uri": "<canonical document URL>", "source_record_id": "<filing or record id>", "known_at": "<ISO-8601 timestamp>"}`;
const ANALYSIS_SHAPE =
 `"analysis": {"claims": [{"text": "<finding>", "evidence_ids": ["<allowed evidence id>"]}], "follow_ups": []}`;


function fetchPrompt(sessionId: string, jobId: string, question: string, asOf?: string): string {
 const cutoff = asOf && asOf.length > 0 ? `known_at is required as ISO-8601 on or before the session cutoff ${asOf}; ` : `no session cutoff applies; known_at may be any ISO-8601 or omitted; `;
 return (
  `Research session ${sessionId} created for "${question}". Fetch evidence now: dispatch SEC research with ` +
  `Call call_tool with name="search_sec_filings" (or list_sec_filings / get_sec_document), then record each finding with ` +
  `Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sessionId}", "job_id": "${jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; ${cutoff}` +
  `Out-of-order calls fail closed; up to 3 SEC dispatches, then the driver closes the run as no_questions:empty-wave1 when SEC has no coverage. ` +
  `When source investigation is complete, you MUST call research_submit_source_result exactly once with coverage runs {"useful_for_question": "sufficient"|"insufficient", ...}, evidence_ids, unresolved_questions, then stop. Do not attempt to freeze. Do not keep adding evidence to fill the cap.`
 );
}

function wavePrompt(wave: number, sessionId: string, jobId: string, targeted: string, asOf?: string): string {
 const label = wave <= 1 ? "Wave-1" : `Wave-${wave}`;
 return (
  `${label} authorized for research session ${sessionId}${targeted}. Fetch targeted evidence with ` +
  `Call call_tool with name="search_sec_filings" (or get_material_events / get_sec_document), then record each finding with ` +
  `Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sessionId}", "job_id": "${jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; ` +
  `known_at is required as ISO-8601 on or before the session cutoff ${asOf || "unbounded"}. ` +
  `When source investigation is complete, you MUST call research_submit_source_result exactly once with coverage runs {"useful_for_question": "sufficient"|"insufficient", ...}, evidence_ids, unresolved_questions, then stop. Do not attempt to freeze. Do not keep adding evidence to fill the cap. ` +
  `If evidence is insufficient or you need more direction, include it in unresolved_questions; the director decides the next wave or asks NEED-USER.`
 );
}
function wave2Prompt(sessionId: string, jobId: string, targeted: string, asOf?: string): string {
 return wavePrompt(2, sessionId, jobId, targeted, asOf);
}

function trioJobIdsForFreeze(session: Json, fid: string): string[] {
 return strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
}

function trioPrompt(sessionId: string, freezeId: string, mapping: Json[], allowedIds: string): string {
 const calls = mapping
  .map(
   (j) =>
    `Call call_tool with name="research_add_analysis" and arguments={"session_id": "${sessionId}", "job_id": "${str(j.job_id)}", "role": "${str(j.job_type)}", ${ANALYSIS_SHAPE}}`,
  )
  .join(" ");
 return (
  `Fresh context? Reload first: Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "freeze", "resource_id": "${freezeId}"}, ` +
  `then Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "evidence", "resource_id": "<id>"} for each id you will cite. ` +
  `Committee may READ frozen state; it must NOT fetch new evidence. Then ` +
  `Evidence frozen as ${freezeId}. Author the trio now, one call per role (${calls}). ` +
  `Every claim ref must use these frozen evidence ids: ${allowedIds}. Unknown ids fail closed naming them; follow_ups is an optional list of questions ending with "?".`
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
  `${reload}${note}Evidence frozen as ${freezeId}. Finalize now: ` +
  `Call call_tool with name="research_finalize" and arguments={"session_id": "${sessionId}", "answer": "<final synthesis prose>", ` +
  `"claims": [{"text": "<finding>", "evidence_ids": ["<allowed evidence id>"]}]}. ` +
  `Every claim ref must use these frozen evidence ids: ${allowedIds}. Empty claims are rejected (claims_required); unknown ids fail closed naming them.`
 );
}

// Fail-closed freeze: the kernel rejects open source jobs, so the model must
// end source work via research_submit_source_result first. No force-complete here.
async function freezeWave(sessionId: string, wave: number, dataRoot?: string, asOf?: string): Promise<Json> {
 return rpc("research.freeze.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
}

async function seedTrio(sessionId: string, wave: number, dataRoot?: string, asOf?: string): Promise<Advance> {
 let snapshot: InspectSnapshot;
 try {
  snapshot = await inspect(sessionId, dataRoot, asOf);
 } catch {
  return { done: false, prompt: `Freeze sent for research session ${sessionId}; author the trio with Call call_tool with name="research_add_analysis" and arguments={"session_id": "${sessionId}", "job_id": "<running committee job>", "role": "<stockbot|bullbot|bearbot>", ${ANALYSIS_SHAPE}}. Every claim ref must use frozen evidence ids; unknown ids fail closed naming them.` };
 }
 const d = deriveState(snapshot.session, snapshot.jobs, snapshot.latestFreeze);
 const allowed = d.freezeEvidenceIds.join(", ");
 if (d.trio.done) {
  return { done: false, prompt: finalizePrompt(sessionId, d.trio.fid, allowed, `Trio complete for research session ${sessionId}. `, trioJobIdsForFreeze(snapshot.session, d.trio.fid)) };
 }
 const seeded = d.trio.eligible.slice(0, 3);
 if (seeded.length === 0) {
  const role = ROLES.find((r) => !d.trio.covered.has(r)) ?? "stockbot";
  try {
   const started = await rpc("research.job.start", { session_id: sessionId, type: role, wave_id: wave }, dataRoot, asOf);
   const nid = str(started.job_id);
   if (!nid) throw new Error("research.job.start failed: missing job_id");
   return { done: false, prompt: trioPrompt(sessionId, d.trio.fid, [{ job_id: nid, job_type: role, wave_id: wave }], allowed) };
  } catch (err) {
   return { done: false, prompt: `Committee job (${role}) for research session ${sessionId} failed (${err instanceof Error ? err.message : String(err)}). Keep authoring analyses with Call call_tool with name="research_add_analysis".` };
  }
 }
 return { done: false, prompt: trioPrompt(sessionId, d.trio.fid, seeded, allowed) };
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
 runs.set(runId, { sessionId, fetchAttempts: 0 });
 return { sessionId, prompt: fetchPrompt(sessionId, deriveState(session, jobs, latestFreeze).jobId, question, asOf) };
}

export async function resumeResearch(
 sessionId: string,
 runId: string,
 dataRoot?: string,
 asOf?: string,
): Promise<{ sessionId: string; prompt: string }> {
 runs.set(runId, { sessionId, fetchAttempts: 0 });
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
  return { done: true, answer: str(fr.answer) || answer };
 }
 const d = deriveState(session, jobs, latestFreeze);
 const evidence = strs(session.evidence_ids);
 if (evidence.length === 0) {
  m.fetchAttempts += 1;
  if (m.fetchAttempts >= MAX_FETCH_ATTEMPTS) {
   try {
    await rpc("research.session.cancel", { session_id: sid }, dataRoot, asOf);
   } catch {
    // cancel is best-effort; the run still terminates locally.
   }
   runs.delete(runId);
   return { done: true, answer: `Research session ${sid} closed: no SEC evidence after ${MAX_FETCH_ATTEMPTS} fetch attempts (no_questions:empty-wave1).` };
  }
  return { done: false, prompt: fetchPrompt(sid, d.jobId, str(session.query) || str(session.objective) || sid, asOf) };
 }
 const freezes = strs(session.freeze_ids);
 if (freezes.length === 0) {
  const src1 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 1);
  const src1Status = str(src1?.status);
  if (src1 && (src1Status === "running" || src1Status === "queued"))
   return { done: false, prompt: `Source still running for research session ${sid}: finish fetching with Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sid}", "job_id": "${str(src1.job_id)}", ${ITEM_SHAPE}}, then end source work exactly once with Call call_tool with name="research_submit_source_result" and arguments={"session_id": "${sid}", "job_id": "${str(src1.job_id)}", "coverage": {"useful_for_question": "sufficient"}, "evidence_ids": [<ids>], "unresolved_questions": []}. Do not attempt to freeze.` };
  let frozen: Json;
  try {
   frozen = await freezeWave(sid, 1, dataRoot, asOf);
  } catch (err) {
   return { done: false, prompt: `Freeze for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
  }
  const verb = str((frozen.pending_next_action as Json | undefined)?.verb ?? (latestFreeze?.pending_next_action as Json | undefined)?.verb ?? "");
  const reason = str((frozen.pending_next_action as Json | undefined)?.reason ?? (latestFreeze?.pending_next_action as Json | undefined)?.reason ?? "");
  // ponytail: director owns next-wave choice; insufficient ends the run with an explicit NEED-USER ask (no committee on thin evidence, no auto-loop). Ceiling: policy max_waves + job budgets backstop runaway waves; auto-start caps at wave 2, N>2 finalizes until reply-parsing lands.
  if (verb === "FINALIZE_INSUFFICIENT")
   return { done: true, answer: `SEC insufficient: ${reason || "no sufficient source coverage"}. NEED-USER: reply with the follow-up question or domain for wave 2, or confirm stop.` };
  return seedTrio(sid, 1, dataRoot, asOf);
 }
 if (!d.trio.done) {
  // Reuse running committee work on this exact freeze; start only the next
  // missing role when nothing is running, one transition per advance.
  if (d.trio.eligible.length > 0)
   return { done: false, prompt: trioPrompt(sid, d.trio.fid, d.trio.eligible.slice(0, 3), d.freezeEvidenceIds.join(", ")) };
  const role = ROLES.find((r) => !d.trio.covered.has(r)) ?? "stockbot";
  let nid = "";
  try {
   const started = await rpc("research.job.start", { session_id: sid, type: role, wave_id: d.trio.wave }, dataRoot, asOf);
   nid = str(started.job_id);
   if (!nid) throw new Error("research.job.start failed: missing job_id");
  } catch (err) {
   return { done: false, prompt: `Committee job (${role}) for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Keep authoring analyses with Call call_tool with name="research_add_analysis".` };
  }
  return { done: false, prompt: trioPrompt(sid, d.trio.fid, [{ job_id: nid, job_type: role, wave_id: d.trio.wave }], d.freezeEvidenceIds.join(", ")) };
 }
 let authorized = d.authorized;
 let targeted = d.targeted;
 if (!d.decided && !authorized) {
  try {
   const dec = await rpc("research.wave2.decide", { session_id: sid }, dataRoot, asOf);
   authorized = dec.authorized === true;
   const tq = str(dec.targeted_question);
   const td = str(dec.targeted_domain);
   targeted = tq && td ? ` on ${td}: ${tq}` : tq || td;
  } catch (err) {
   return { done: false, prompt: `Wave-2 gate for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
  }
 }
 if (authorized && !d.waveFrozen) {
  const N = d.activeWave >= 2 ? d.activeWave : 2;
  if (N > 2)
   return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Waves complete for research session ${sid}. `, trioJobIdsForFreeze(session, d.trio.fid)) };
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
  const verbN = str((frozenN.pending_next_action as Json | undefined)?.verb ?? "");
  const reasonN = str((frozenN.pending_next_action as Json | undefined)?.reason ?? "");
  if (verbN === "FINALIZE_INSUFFICIENT")
   return { done: true, answer: `SEC insufficient: ${reasonN || "no sufficient source coverage"}. NEED-USER: reply with the follow-up question or domain for wave ${N + 1}, or confirm stop.` };
  return seedTrio(sid, N, dataRoot, asOf);
 }
 return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), authorized ? `Waves complete for research session ${sid}. ` : `Wave-2 declined for research session ${sid}. `, trioJobIdsForFreeze(session, d.trio.fid)) };
}
