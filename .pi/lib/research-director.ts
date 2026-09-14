/** Staged ResearchDirector driver: fetch -> freeze -> trio -> gate -> [wave-2] -> finalize.
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

export type Json = Record<string, unknown>;
export type BridgeCall = (req: Json) => Promise<Json>;

let bridge: BridgeCall = async () => ({ error: "bridge_unavailable" });
export function setResearchBridge(fn: BridgeCall): void {
 bridge = fn;
}

export type Advance = { done: false; prompt: string } | { done: true; answer: string } | null;
export type Stage = "SOURCE_RESEARCH" | "COMMITTEE" | "FINAL";

// ponytail: Mem is just the session pointer; everything else derives from kernel
// inspect so a restart plus resumeResearch recovers the full driver state.
interface Mem {
 sessionId: string;
}
const runs = new Map<string, Mem>();
export function clearResearchRun(runId: string): void {
 runs.delete(runId);
}
export function sessionIdForRun(runId: string): string | undefined {
 return runs.get(runId)?.sessionId;
}
// Last inspect snapshot per session, for the UX-only stage gate in stockbot.ts
// (kernel remains authoritative). Fail open when nothing was seen yet.
export interface InspectSnapshot { session: Json; jobs: Json[]; latestFreeze: Json | null }
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
export interface TrioState { fid: string; wave: number; have: number; done: boolean; eligible: Json[]; covered: Set<string> }
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

export function deriveState(session: Json, jobs: Json[], latestFreeze?: Json | null): {
 stage: Stage; jobId: string; decided: boolean; authorized: boolean;
 baseline: number; wave2Frozen: boolean; targeted: string;
 wave2JobId: string; freezeEvidenceIds: string[]; unfrozenEvidenceIds: string[];
 trio: TrioState;
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
 const authorized =
  status === "targeted_research" || currentWave === 2 || targeted.length > 0 ||
  journal.has("wave.authorized");
 // ponytail: "analyzing" is pre-decide (the trio runs there), so it must NOT imply
 // decided — otherwise trio-complete sessions could never reach decide/finalize.
 const decided =
  journal.has("wave.authorized") || journal.has("wave.stopped") ||
  status === "targeted_research" || status === "synthesizing" ||
  status === "completed" || currentWave === 2 || targeted.length > 0;
 // ponytail: wave-2 frozen derives from the freeze record's wave_id, never the
 // freeze-id shape, so non-synthesized ids still resume to finalize. Absent
 // record (older stubs) falls back to the synthesized id check.
 const freezeWaveId = latestFreeze && typeof latestFreeze.wave_id === "number" && Number.isInteger(latestFreeze.wave_id) ? latestFreeze.wave_id : null;
 const wave2Frozen = freezeWaveId !== null && freezeWaveId !== undefined ? freezeWaveId >= 2 : freezes.includes(`${sid}:2:freeze`);
 // Committee/final prompts use the freeze's evidence ids, never the session's
 // broader mutable list; the difference only decides whether the active wave
 // gathered anything past the latest freeze.
 const freezeEvidenceIds = latestFreeze ? strs(latestFreeze.evidence_ids) : evidence;
 const frozenSet = new Set(freezeEvidenceIds);
 const unfrozenEvidenceIds = latestFreeze ? evidence.filter((id) => !frozenSet.has(id)) : [];
 const src1 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 1);
 const src2 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 2);
 const jobId = str(src1?.job_id ?? jobs[0]?.job_id);
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
 return { stage, jobId, decided, authorized, baseline, wave2Frozen, targeted, wave2JobId, freezeEvidenceIds, unfrozenEvidenceIds, trio };
}

// UX-only mirror of the kernel stage gate (kernel stays authoritative):
// positive canonical control allowlists plus discovery, same reason string,
// fail open otherwise. COMMITTEE/FINAL therefore block every unlisted data tool.
const STAGE_GATE_DISCOVERY: Record<string, true> = { browse_tools: true, search_tools: true, describe_tool: true, list_tool_domains: true, call_tool: true };
const STAGE_GATE_SOURCE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_add_evidence: true };
const STAGE_GATE_COMMITTEE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_add_analysis: true };
const STAGE_GATE_FINAL_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_finalize: true };
// Local controls, thesis actions, and dotted bridge ops are never staged data
// dispatches; SOURCE blocks them while unlisted data tools pass.
const STAGE_GATE_NON_DISPATCH: Record<string, true> = { research_start: true, research_add_evidence: true, research_add_analysis: true, research_finalize: true, thesis_create: true, thesis_show: true, thesis_refine: true, thesis_watch: true, thesis_journal: true, thesis_status: true, "research.session.inspect": true, "research.session.resume": true, "research.session.finalize": true, "research.job.start": true, "research.job.complete": true, "research.freeze.create": true };
export function stageBlockReason(stage: Stage, toolName: string): string | undefined {
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
  `Call call_tool with name="search_web" (or get_fundamentals / get_short_interest), then record each finding with ` +
  `Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sessionId}", "job_id": "${jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; ${cutoff}` +
  `Out-of-order calls fail closed; keep fetching until evidence shows.`
 );
}

function wave2Prompt(sessionId: string, jobId: string, targeted: string, asOf?: string): string {
 return (
  `Wave-2 authorized for research session ${sessionId}${targeted}. Fetch targeted evidence with ` +
  `Call call_tool with name="search_web", then record each finding with ` +
  `Call call_tool with name="research_add_evidence" and arguments={"session_id": "${sessionId}", "job_id": "${jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; ` +
  `known_at is required as ISO-8601 on or before the session cutoff ${asOf || "unbounded"}.`
 );
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

// Best-effort: complete the wave's running source jobs so the kernel
// terminal-before-freeze guard passes; queued work never ran, is left untouched,
// and the kernel freeze_session open-jobs guard exposes it. Failures (already
// terminal, stub without the op) are ignored; freeze.create stays fail-closed below.
async function freezeWave(sessionId: string, wave: number, jobs: Json[], dataRoot?: string, asOf?: string): Promise<void> {
 for (const j of jobs) {
  const t = str(j.job_type);
  const s = str(j.status);
  if ((t === "source_agent" || t === "scout") && j.wave_id === wave && (s === "running")) {
   try {
    await rpc("research.job.complete", { job_id: str(j.job_id), outcome: { status: "done" } }, dataRoot, asOf);
   } catch {
    // already terminal: proceed to the authoritative freeze call.
   }
  }
 }
 await rpc("research.freeze.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
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
  return { done: true, answer: str(fr.answer) || answer };
 }
 const d = deriveState(session, jobs, latestFreeze);
 const evidence = strs(session.evidence_ids);
 if (evidence.length === 0) {
  return { done: false, prompt: fetchPrompt(sid, d.jobId, str(session.query) || str(session.objective) || sid, asOf) };
 }
 const freezes = strs(session.freeze_ids);
 if (freezes.length === 0) {
  try {
   await freezeWave(sid, 1, jobs, dataRoot, asOf);
  } catch (err) {
   return { done: false, prompt: `Freeze for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
  }
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
 if (authorized && !d.wave2Frozen) {
  if (!d.wave2JobId) {
   try {
    const started = await rpc("research.job.start", { session_id: sid, type: "source_agent", wave_id: 2 }, dataRoot, asOf);
    const nid = str(started.job_id);
    if (!nid) throw new Error("research.job.start failed: missing job_id");
    try {
     await inspect(sid, dataRoot, asOf);
    } catch {
     // inspect refresh is best-effort; the explicit nid still drives the prompt
    }
    return { done: false, prompt: wave2Prompt(sid, nid, targeted, asOf) };
   } catch (err) {
    return { done: false, prompt: `Wave-2 source job for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
   }
  }
  // Wave-2 source work is underway: without evidence past the latest freeze the
  // driver keeps fetching; the wave-2 freeze fires only once new evidence lands.
  if (d.unfrozenEvidenceIds.length === 0) {
   const runningW2 = jobs.find((j) => str(j.job_type) === "source_agent" && j.wave_id === 2 && str(j.status) === "running");
   return { done: false, prompt: wave2Prompt(sid, str(runningW2?.job_id) || d.wave2JobId, targeted, asOf) };
  }
  try {
   await freezeWave(sid, 2, jobs, dataRoot, asOf);
  } catch (err) {
   return { done: false, prompt: `Freeze for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
  }
  return seedTrio(sid, 2, dataRoot, asOf);
 }
 return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), authorized ? `Wave-2 complete for research session ${sid}. ` : `Wave-2 declined for research session ${sid}. `, trioJobIdsForFreeze(session, d.trio.fid)) };
}
