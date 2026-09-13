/** Staged ResearchDirector driver: fetch -> freeze -> trio -> gate -> [wave-2] -> finalize.
 *
 * Pi owns the model loop; this module owns stage order only. Every transition is
 * kernel-gated fail-closed: predicates read research.session.inspect results, at most
 * one transition RPC fires per advance, and kernel errors keep the run entry (nothing
 * is ever invented). The model does SEC dispatch plus evidence/trio authoring through
 * the call_tool verbs named in each prompt. No model invocation, no cli.py, no
 * thresholds, budgets, or wire shapes live here.
 */

export type Json = Record<string, unknown>;
export type BridgeCall = (req: Json) => Promise<Json>;

let bridge: BridgeCall = async () => ({ error: "bridge_unavailable" });
export function setResearchBridge(fn: BridgeCall): void {
 bridge = fn;
}

export type Advance = { done: false; prompt: string } | { done: true; answer: string } | null;

interface Mem {
 sessionId: string;
 jobId: string;
 question: string;
 decided: boolean;
 authorized: boolean;
 baseline: number;
 wave2Frozen: boolean;
 targeted: string;
 wave2JobId: string;
}
const runs = new Map<string, Mem>();
export function clearResearchRun(runId: string): void {
 runs.delete(runId);
}

const ROLES = ["stockbot", "bullbot", "bearbot"] as const;
const TERMINAL: Record<string, true> = { completed: true, failed: true, cancelled: true };

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] =>
 Array.isArray(v) ? v.filter((e): e is string => typeof e === "string") : [];
const objs = (v: unknown): Json[] =>
 Array.isArray(v) ? v.filter((e): e is Json => !!e && typeof e === "object") : [];

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
function trioState(session: Json, jobs: Json[]): { fid: string; wave: number; have: number; done: boolean; eligible: Json[]; covered: Set<string> } {
 const freezes = strs(session.freeze_ids);
 const fid = freezes[freezes.length - 1] ?? "";
 const recorded = strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
 const recordedSet = new Set(recorded);
 const byId = new Map(jobs.map((j) => [str(j.job_id), j]));
 const isRole = (t: unknown): boolean => (ROLES as readonly string[]).includes(str(t));
 const recordedRoles = new Set<string>();
 for (const id of recorded) {
  const j = byId.get(id);
  if (!j || j.status !== "completed") continue;
  const t = str(j.job_type);
  if (isRole(t)) recordedRoles.add(t);
 }
 const eligible = jobs
  .filter((j) => j.status === "running" && isRole(j.job_type) && !recordedSet.has(str(j.job_id)))
  .sort((a, b) => (str(a.job_id) < str(b.job_id) ? -1 : 1));
 const covered = new Set<string>(recordedRoles);
 for (const j of eligible) covered.add(str(j.job_type));
 const runWave = objs(session.committee_runs).find((e) => e.freeze_id === fid)?.wave_id;
 let wave = 1;
 if (typeof runWave === "number" && Number.isInteger(runWave)) wave = runWave;
 else if (typeof session.current_wave === "number" && Number.isInteger(session.current_wave)) wave = session.current_wave;
 return { fid, wave, have: recordedRoles.size, done: recordedRoles.size === 3, eligible, covered };
}

async function inspect(sessionId: string, dataRoot?: string, asOf?: string): Promise<{ session: Json; jobs: Json[] }> {
 const res = await rpc("research.session.inspect", { session_id: sessionId }, dataRoot, asOf);
 if (!res.session || typeof res.session !== "object")
  throw new Error("research.session.inspect failed: missing session");
 return { session: res.session as Json, jobs: objs(res.jobs) };
}
const ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher, e.g. SEC>", ` +
 `"source_uri": "<canonical document URL>", "source_record_id": "<filing or record id>", "known_at": "<ISO-8601 timestamp>"}`;
const ANALYSIS_SHAPE =
 `"analysis": {"claims": [{"text": "<finding>", "evidence_ids": ["<allowed evidence id>"]}], "follow_ups": []}`;

function fetchPrompt(m: Mem, asOf?: string): string {
 const cutoff = asOf && asOf.length > 0 ? `known_at is required as ISO-8601 on or before the session cutoff ${asOf}; ` : `no session cutoff applies; known_at may be any ISO-8601 or omitted; `;
 return (
  `Research session ${m.sessionId} created for "${m.question}". Fetch evidence now: dispatch SEC research with ` +
  `Call call_tool with name="search_web" (or get_fundamentals / get_short_interest), then record each finding with ` +
  `Call call_tool with name="research_add_evidence" and arguments={"session_id": "${m.sessionId}", "job_id": "${m.jobId}", ${ITEM_SHAPE}}. ` +
  `Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; ${cutoff}` +
  `Out-of-order calls fail closed; keep fetching until evidence shows.`
 );
}

function trioPrompt(m: Mem, freezeId: string, mapping: Json[], allowedIds: string): string {
 const calls = mapping
  .map(
   (j) =>
    `Call call_tool with name="research_add_analysis" and arguments={"session_id": "${m.sessionId}", "job_id": "${str(j.job_id)}", "role": "${str(j.job_type)}", ${ANALYSIS_SHAPE}}`,
  )
  .join(" ");
 return (
  `Evidence frozen as ${freezeId}. Author the trio now, one call per role (${calls}). ` +
  `Every claim ref must use these frozen evidence ids: ${allowedIds}. Unknown ids fail closed naming them; follow_ups is an optional list of questions ending with "?".`
 );
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
 const { jobs } = await inspect(sessionId, dataRoot, asOf);
 const m: Mem = {
  sessionId,
  jobId: str(jobs[0]?.job_id),
  question,
  decided: false,
  authorized: false,
  baseline: 0,
  wave2Frozen: false,
  targeted: "",
  wave2JobId: "",
 };
 runs.set(runId, m);
 return { sessionId, prompt: fetchPrompt(m, asOf) };
}

export async function advanceOnAgentEnd(runId: string, answer = "", dataRoot?: string, asOf?: string): Promise<Advance> {
 const m = runs.get(runId);
 if (!m) return null;
 let session: Json;
 let jobs: Json[];
 try {
  ({ session, jobs } = await inspect(m.sessionId, dataRoot, asOf));
 } catch (err) {
  return { done: false, prompt: `Research session ${m.sessionId} unreadable (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
 }
 const final = session.final_result;
 if ((final && typeof final === "object") || TERMINAL[str(session.status)]) {
  runs.delete(runId);
  const fr = (final && typeof final === "object" ? final : {}) as Json;
  return { done: true, answer: str(fr.answer) || answer };
 }
 const evidence = strs(session.evidence_ids);
 if (evidence.length === 0) return { done: false, prompt: fetchPrompt(m, asOf) };
 const freezes = strs(session.freeze_ids);
 const trio = trioState(session, jobs);
 if (freezes.length === 0 || (m.authorized && !m.wave2Frozen && evidence.length > m.baseline)) {
  const wave = freezes.length === 0 ? 1 : 2;
  try {
   await rpc("research.freeze.create", { session_id: m.sessionId, wave_id: wave }, dataRoot, asOf);
   if (wave === 2) m.wave2Frozen = true;
  } catch (err) {
   return { done: false, prompt: `Freeze for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Add or repair evidence with Call call_tool with name="research_add_evidence", then continue.` };
  }
  try {
   ({ session, jobs } = await inspect(m.sessionId, dataRoot, asOf));
  } catch {
   return { done: false, prompt: `Freeze sent for research session ${m.sessionId}; author the trio with Call call_tool with name="research_add_analysis" and arguments={"session_id": "${m.sessionId}", "job_id": "<running committee job>", "role": "<stockbot|bullbot|bearbot>", ${ANALYSIS_SHAPE}}. Every claim ref must use these frozen evidence ids: ${strs(session.evidence_ids).join(", ")}. Unknown ids fail closed naming them.` };
  }
  const fresh = trioState(session, jobs);
  const seeded = fresh.eligible.slice(0, 3);
  if (seeded.length === 0) {
   const role = ROLES.find((r) => !fresh.covered.has(r)) ?? "stockbot";
   try {
    const started = await rpc("research.job.start", { session_id: m.sessionId, type: role, wave_id: wave }, dataRoot, asOf);
    const nid = str(started.job_id);
    if (!nid) throw new Error("research.job.start failed: missing job_id");
    return { done: false, prompt: trioPrompt(m, fresh.fid, [{ job_id: nid, job_type: role, wave_id: wave }], strs(session.evidence_ids).join(", ")) };
   } catch (err) {
    return { done: false, prompt: `Committee job (${role}) for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Keep authoring analyses with Call call_tool with name="research_add_analysis".` };
   }
  }
  return { done: false, prompt: trioPrompt(m, fresh.fid, seeded, strs(session.evidence_ids).join(", ")) };
 }
 if (!trio.done) {
  const missing = ROLES.find((r) => !trio.covered.has(r));
  if (!missing) return { done: false, prompt: trioPrompt(m, trio.fid, trio.eligible.slice(0, 3), strs(session.evidence_ids).join(", ")) };
  const role = missing;
  let nid = "";
  try {
   const started = await rpc("research.job.start", { session_id: m.sessionId, type: role, wave_id: trio.wave }, dataRoot, asOf);
   nid = str(started.job_id);
   if (!nid) throw new Error("research.job.start failed: missing job_id");
  } catch (err) {
   return { done: false, prompt: `Committee job (${role}) for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Keep authoring analyses with Call call_tool with name="research_add_analysis".` };
  }
  return { done: false, prompt: trioPrompt(m, trio.fid, [...trio.eligible, { job_id: nid, job_type: role, wave_id: trio.wave }].slice(0, 3), strs(session.evidence_ids).join(", ")) };
 }
 if (!m.decided) {
  try {
   const d = await rpc("research.wave2.decide", { session_id: m.sessionId }, dataRoot, asOf);
   m.decided = true;
   m.authorized = d.authorized === true;
   m.baseline = evidence.length;
   const tq = str(d.targeted_question);
   const td = str(d.targeted_domain);
   m.targeted = tq && td ? ` on ${td}: ${tq}` : tq || td;
  } catch (err) {
   return { done: false, prompt: `Wave-2 gate for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
  }
  if (!m.authorized)
   return { done: false, prompt: `Wave-2 declined for research session ${m.sessionId}. Write the final synthesis as plain assistant text (no tool call); it is recorded on the next turn.` };
 }
 if (m.authorized && !m.wave2Frozen) {
  if (!m.wave2JobId) {
   try {
    const started = await rpc("research.job.start", { session_id: m.sessionId, type: "source_agent", wave_id: 2 }, dataRoot, asOf);
    const nid = str(started.job_id);
    if (!nid) throw new Error("research.job.start failed: missing job_id");
    m.wave2JobId = nid;
   } catch (err) {
    return { done: false, prompt: `Wave-2 source job for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
   }
  }
  return { done: false, prompt: `Wave-2 authorized for research session ${m.sessionId}${m.targeted}. Fetch targeted evidence with Call call_tool with name="search_web", then record each finding with Call call_tool with name="research_add_evidence" and arguments={"session_id": "${m.sessionId}", "job_id": "${m.wave2JobId || m.jobId}", ${ITEM_SHAPE}}. Provenance is kernel-validated: one of source_uri/source_record_id is required and content or claim_text is required; known_at is required as ISO-8601 on or before the session cutoff ${asOf || "unbounded"}.` };
 }
 try {
  await rpc("research.session.finalize", { session_id: m.sessionId, answer, claims: [] }, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Finalize for research session ${m.sessionId} failed (${err instanceof Error ? err.message : String(err)}). Write the final synthesis as plain assistant text (no tool call); it is recorded on the next turn.` };
 }
 const snap = await inspect(m.sessionId, dataRoot, asOf).catch(() => null);
 runs.delete(runId);
 const fr = (snap?.session.final_result && typeof snap.session.final_result === "object" ? snap.session.final_result : {}) as Json;
 return { done: true, answer: str(fr.answer) || answer };
}
