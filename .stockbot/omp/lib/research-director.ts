/** Staged ResearchDirector driver: fetch -> freeze -> atomic trio -> gate -> [next wave, while the gate authorizes] -> finalize.
 *
 * OMP owns the model loop; this module owns stage order only. Every transition is
 * kernel-gated fail-closed: predicates read research.session.inspect results, at most
 * one transition RPC fires per stage step, and kernel errors keep the run entry (nothing
 * is ever invented). The wave gate alone decides continue vs finalize, for every wave:
 * no wave ceiling lives here. The model dispatches SEC work as one task batch for
 * sec-agent and the committee as one task batch for the trio, with prompts naming
 * the call_tool verbs and research ids each child must pass through verbatim.
 * No subprocesses, no cli.py, no thresholds, and no budgets live here.
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
const STAGE_GATE_DISCOVERY: Record<string, true> = { browse_tools: true, search_tools: true, describe_tool: true, list_tool_domains: true, call_tool: true, task: true };
const STAGE_GATE_SOURCE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_read_search: true, research_cancel: true, research_add_evidence: true, research_submit_source_result: true };
const STAGE_GATE_COMMITTEE_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true };
const STAGE_GATE_FINAL_EXTRA: Record<string, true> = { research_resume: true, research_status: true, research_read: true, research_cancel: true, research_finalize: true };
// Local controls, thesis actions, and dotted bridge ops are never staged data
// dispatches; SOURCE blocks them while unlisted data tools pass. (SOURCE extras
// live in STAGE_GATE_SOURCE_EXTRA above, so they must not repeat here.)
const STAGE_GATE_NON_DISPATCH: Record<string, true> = { research_start: true, research_add_analysis: true, research_finalize: true, thesis_create: true, thesis_show: true, thesis_refine: true, thesis_watch: true, thesis_journal: true, thesis_status: true, "research.session.inspect": true, "research.session.resume": true, "research.session.finalize": true, "research.job.start": true, "research.job.complete": true, "research.freeze.create": true };
function stageBlockReason(stage: Stage, toolName: string): string | undefined {
 if (STAGE_GATE_DISCOVERY[toolName]) return undefined;
 if (stage === "COMMITTEE") return STAGE_GATE_COMMITTEE_EXTRA[toolName] ? undefined : `Stage ${stage} forbids tool '${toolName}'`;
 if (stage === "FINAL") return STAGE_GATE_FINAL_EXTRA[toolName] ? undefined : `Stage ${stage} forbids tool '${toolName}'`;
 if (STAGE_GATE_SOURCE_EXTRA[toolName]) return undefined;
 return STAGE_GATE_NON_DISPATCH[toolName] ? `Stage ${stage} forbids tool '${toolName}'` : undefined;
}
export function stageBlockReasonForTest(stage: Stage, toolName: string): string | undefined {
 return stageBlockReason(stage, toolName);
}
export function blockReasonForRun(runId: string, toolName: string): string | undefined {
 const sid = runs.get(runId)?.sessionId;
 if (!sid) return undefined;
 const seen = lastSeen.get(sid);
 if (!seen) return undefined;
 return stageBlockReason(deriveState(seen.session, seen.jobs, seen.latestFreeze).stage, toolName);
}

// PIT parity mirror (kernel stays authoritative): pure mirrors of
// app/research/models.py pit_violated/pit_unverified. Export + test only,
// never called in prod paths. DIVERGENCE (deliberate, fail-closed-false):
// Python _coerce_time RAISES on non-ISO input ("" / "garbage" / non-string,
// caught by _ingest_pit_gate as PROVENANCE_FAILURE); coerceTime returns null
// so pitViolated/pitUnverified return false instead. A TS consumer must treat
// false-with-unparseable-input as "no verdict", never as "PIT clean".
const NO_CUTOFF_AS_OF: Record<string, true> = { unbounded: true };
function coerceTime(v: unknown): number | null {
 if (v === null || v === undefined) return null;
 if (v instanceof Date) {
  const ms = v.getTime();
  return Number.isNaN(ms) ? null : ms;
 }
 if (typeof v !== "string") return null;
 const raw = v.trim();
 if (!raw || !/^\d{4}-\d{2}-\d{2}/.test(raw)) return null;
 const text = raw.replace(" ", "T");
 const stamped = /^\d{4}-\d{2}-\d{2}$/.test(text)
  ? `${text}T00:00:00Z`
  : /([zZ]|[+-]\d{2}:?\d{2}(:\d{2})?)$/.test(text)
   ? text
   : `${text}Z`;
 const ms = Date.parse(stamped);
 return Number.isNaN(ms) ? null : ms;
}
function asOfBounded(asOf: unknown): boolean {
 if (asOf === null || asOf === undefined) return false;
 if (typeof asOf === "string") {
  const text = asOf.trim().toLowerCase();
  return text.length > 0 && !NO_CUTOFF_AS_OF[text];
 }
 return asOf instanceof Date;
}
export function pitViolated(asOf: unknown, knownAt: unknown): boolean {
 if (typeof asOf === "string" && NO_CUTOFF_AS_OF[asOf.trim().toLowerCase()]) return false;
 const start = coerceTime(asOf);
 const known = coerceTime(knownAt);
 if (start === null || known === null) return false;
 return known > start;
}
export function pitUnverified(asOf: unknown, knownAt: unknown): boolean {
 if (knownAt !== null && knownAt !== undefined) return false;
 return asOfBounded(asOf);
}
// Accession parity mirror (kernel stays authoritative): pure mirror of
// app/research/evidence.py normalize_accession. Export + test only, never
// called in prod paths. Python RAISES ValueError on non-canonical input;
// this mirror returns null instead ("no verdict", never "valid"). A TS
// consumer must treat null as invalid, never as clean.
const ACCESSION_RE = /^\d{10}-\d{2}-\d{6}$/;
export function normalizeAccession(value: unknown): string | null {
 const text0 = typeof value === "string" ? value.trim() : "";
 const text = /^\d{18}$/.test(text0) ? `${text0.slice(0, 10)}-${text0.slice(10, 12)}-${text0.slice(12)}` : text0;
 return ACCESSION_RE.test(text) ? text : null;
}
// Provenance parity mirror (kernel stays authoritative): pure shape mirror of
// app/research/evidence.py validate_provenance + sec_source_ref/search_run_ref.
// Export + test only, never called in prod paths. Python RAISES
// EvidenceIntegrityError on bad shape; this mirror returns null instead
// ("no verdict", never "valid"). A TS consumer must treat null as invalid.
const PROVENANCE_KINDS: Record<string, true> = { sec_source: true, search_run: true, none: true };
function provStr(v: unknown): string | null {
 return typeof v === "string" && v.trim() ? v : null;
}
export function validateProvenance(value: unknown): Json | null {
 if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) return null;
 const prov = value as Json;
 const keys = Object.keys(prov);
 if (keys.length === 0) return {};
 const kind = prov.kind;
 if (typeof kind !== "string" || !PROVENANCE_KINDS[kind]) return null;
 if (kind === "none") return { kind: "none" };
 if (kind === "search_run") {
  const sid = provStr(prov.search_id);
  const query = provStr(prov.query);
  if (sid === null || query === null) return null;
  return { kind: "search_run", search_id: sid.trim(), query: query.trim() };
 }
 const acc = normalizeAccession(prov.accession_no);
 const doc = provStr(prov.document_name);
 const passage = provStr(prov.passage);
 if (acc === null || doc === null || passage === null) return null;
 const out: Json = { kind: "sec_source", accession_no: acc, document_name: doc.trim(), passage: passage.trim() };
 if (typeof prov.source_uri === "string" && prov.source_uri.trim()) out.source_uri = prov.source_uri.trim();
 else out.source_uri = null;
 return out;
}
// Freeze parity mirror (kernel stays authoritative): pure mirrors of
// app/research/freeze.py freeze_content_hash + verify_freeze id-set/drift
// checks. Export + test only, never called in prod paths. Python RAISES
// FreezeIntegrityError on violation; these return null/error-string instead
// ("no verdict", never "valid"). Hash uses WebCrypto SHA-256 (Bun + OMP
// runtime) to match Python hashlib.sha256 hex.
export interface FreezeRecord { evidence_id: string; content_hash: string }
export async function freezeContentHash(records: FreezeRecord[]): Promise<string> {
 const body = [...records].sort((a, b) => (a.evidence_id < b.evidence_id ? -1 : a.evidence_id > b.evidence_id ? 1 : 0)).map((r) => `${r.evidence_id}:${r.content_hash}`).join("\n");
 const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
 return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
export function checkFreezeDrift(freezeId: string, frozenIds: string[], records: FreezeRecord[]): string | null {
 const have = new Set(records.map((r) => r.evidence_id));
 if (have.size !== records.length) return `freeze ${freezeId}: duplicate evidence ids`;
 if (have.size !== frozenIds.length || frozenIds.some((id) => !have.has(id))) return `freeze ${freezeId}: evidence id set drifted`;
 return null;
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
// only when the citation carries the canonical source_handle get_sec_document
// returned for the window that was read, plus the passage being cited. The
// kernel reloads that window and stores the archive's bytes. claim_kind routes
// the two supported kinds.
const ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher, e.g. SEC>", ` +
 `"source_uri": "<canonical document URL>", "source_record_id": "<SEC accession, e.g. 0000320193-24-000123>", "document_name": "<filing or exhibit you opened>", ` +
 `"source_handle": <the source_handle get_sec_document returned for that window>, "matching_passage": "<passage quoted from that window>", "known_at": "<ISO-8601 timestamp>", "claim_kind": "observed_fact"}`;
// One source workflow for every wave: navigation-only search, raw-document
// evidence, no limits, coverage submitted structurally.
const SOURCE_WORKFLOW =
 `Work it as a branch map: name the material branches this question needs, search SEC for each, open the filings behind every hit, read the documents/exhibits/passages, and record only raw-document-backed evidence. ` +
 `There is no maximum number of searches, filing reads, document reads, exhibit reads, or waves: keep going while the work is materially useful; the only waste is an exact repeat that adds nothing. ` +
 `Search results are navigation artifacts, never evidence: opening the document is what makes a finding citable. ` +
 `Open the filing with get_sec_document and cite what it returned: an observed_fact needs that call's canonical source_handle plus the passage you are citing (matching_passage, or passage/section) — the kernel reloads the window itself, so a hit or a handle-less citation fails ERR_RAW_SOURCE_REQUIRED and a passage the window does not contain fails ERR_PASSAGE_NOT_IN_SOURCE. ` +
 `An absence_observation instead needs claim_kind "absence_observation" with search_id, query, and the searched coverage (forms, dates, partitions, entities, docs, gaps, pagination_complete, complete), and must not carry an accession: it is recorded as a session coverage artifact (what the search did and did not reach), never as citable evidence, and no filing can prove it. ` +
 `Track entities, forms, exhibits, branches covered/remaining, and the questions still open as you go: sufficiency is coverage of what you checked, never a whole-question answer. `;

function sourceSteps(sessionId: string, jobId: string, asOf?: string): string {
 const cutoff = asOf && asOf.length > 0 ? `known_at must be an ISO-8601 timestamp on or before the session cutoff ${asOf}` : `no session cutoff applies; known_at may be any ISO-8601 timestamp or omitted`;
 return (
  `${SOURCE_WORKFLOW}The sec-agent child records evidence with research_add_evidence on session ${sessionId} and job ${jobId} (${ITEM_SHAPE}); ` +
  `its sec-scout batch fans out inside its own session. ` +
  `Provenance is kernel-validated and ${cutoff}; unprovenanced or out-of-order calls fail closed. ` +
  `When this source investigation is complete, the sec-agent MUST call research_submit_source_result exactly once with the structured coverage {"useful_for_question": "sufficient"|"insufficient", ...}, evidence_ids, and unresolved_questions, then stop. ` +
  `That ends this source job and returns control to the Director, which owns freezing, waves, and the session; it never freezes, ends a wave, or ends the session. Do not attempt to freeze.`
 );
}

function fetchPrompt(sessionId: string, jobId: string, question: string, asOf?: string): string {
 return (
  `Research session ${sessionId} created for "${question}". Dispatch SEC research now as one task batch with context and one task for agent sec-agent ` +
  `(task: the research objective plus branch guidance; the batch plans the omp-owned source job). ` +
  sourceSteps(sessionId, jobId, asOf)
 );
}

function wavePrompt(wave: number, sessionId: string, jobId: string, targeted: string, asOf?: string): string {
 const label = wave <= 1 ? "Wave-1" : `Wave-${wave}`;
 return (
  `${label} authorized for research session ${sessionId}${targeted}. Dispatch SEC research now as one task batch with context and one task for agent sec-agent ` +
  `(task: this wave's objective plus branch guidance; the batch plans the omp-owned source job). ` +
  sourceSteps(sessionId, jobId, asOf) +
  ` If evidence is insufficient, include it in unresolved_questions; the Director decides the next wave.`
 );
}

function trioJobIdsForFreeze(session: Json, fid: string): string[] {
 return strs(objs(session.committee_runs).find((e) => e.freeze_id === fid)?.jobs);
}

function trioPrompt(sessionId: string, freezeId: string, mapping: Json[], allowedIds: string, note = ""): string {
 const roles = mapping.map((j) => str(j.job_type)).filter(Boolean).join(", ");
 return (
  `${note}Fresh context? Reload first: Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "freeze", "resource_id": "${freezeId}"}, ` +
  `then Call call_tool with name="research_read" and arguments={"session_id": "${sessionId}", "kind": "evidence", "resource_id": "<id>"} for each id you will cite. ` +
  `Committee may READ frozen state; it must NOT fetch new evidence. Evidence frozen as ${freezeId}. Dispatch the committee now as one task batch with context and tasks for these roles: ${roles}. ` +
  `Each role reads only the frozen evidence and returns exactly the structured envelope (claims with claim_type and evidence_ids, impact channels with direction, materiality with reasoning, uncertainties, what_would_change, follow_ups). ` +
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

// Atomic trio: one RPC creates every missing committee job (status RUNNING)
// together, so no role is ever started, awaited, and only then followed by the
// next. A role already running is reused; a recorded role is left alone. The
// Director then prompts this context to dispatch the trio as one OMP task
// batch (planTaskCall + recordTaskResult); no subprocess ever authors a role.
async function seedTrio(runId: string, sessionId: string, wave: number, dataRoot?: string, asOf?: string, note = ""): Promise<Advance> {
 try {
  await rpc("research.committee.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
 } catch (err) {
  return { done: false, prompt: `Committee creation for research session ${sessionId} failed (${err instanceof Error ? err.message : String(err)}). Dispatch the committee as one task batch.` };
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
 return { done: false, prompt: trioPrompt(sessionId, d.trio.fid, d.trio.eligible, allowed, note) };
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

export async function advanceOnAgentEnd(runId: string, answer = "", dataRoot?: string, asOf?: string, opts?: { keepRun?: boolean }): Promise<Advance> {
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
  if (!opts?.keepRun) runs.delete(runId);
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
  // before any dispatch prompt (existing role jobs are reused, recorded roles
  // stay out of the prompt); the Director then dispatches the trio as one OMP
  // task batch (planTaskCall + recordTaskResult).
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
// --- Director task interception policy (OMP runtime) ---------------------------
// The Director owns every main-session spawn: SOURCE_RESEARCH allows sec-agent
// only, COMMITTEE allows the trio only, FINAL allows nothing. Nested fan-out
// runs inside the sec-agent's own child session, which passes the main-session
// gate through untouched; the kernel spawn policy stays the single owner there.
const num = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);
const DIRECTOR_SPAWNS = ["sec-agent", "stockbot", "bullbot", "bearbot"];
const STAGE_AGENTS: Record<Stage, string[]> = {
 SOURCE_RESEARCH: ["sec-agent"],
 COMMITTEE: ["stockbot", "bullbot", "bearbot"],
 FINAL: [],
};
const JOB_TYPE: Record<string, string> = {
 "sec-agent": "source_agent",
 stockbot: "stockbot",
 bullbot: "bullbot",
 bearbot: "bearbot",
};
export interface TaskPlanContext {
 researchKey: string;
 toolCallId: string;
}
export interface TaskPlan {
 block?: true;
 reason?: string;
 input?: Json;
}
interface PlannedItem {
 sessionId: string;
 jobId: string;
 jobType: string;
 agentType: string;
 wave: number;
 name: string;
}
const plannedByCall = new Map<string, { sessionId: string; items: PlannedItem[] }>();
function shortSuffix(jobId: string): string {
 const cleaned = jobId.replace(/[^a-zA-Z0-9]/g, "");
 return cleaned.slice(-8).toLowerCase() || "job";
}
function researchContextBlock(b: { sessionId: string; jobId: string; wave: number; asOf?: string; freezeId?: string }): string {
 const rows = [
  `research_session_id=${b.sessionId}`,
  `research_job_id=${b.jobId}`,
  `wave_id=${b.wave}`,
  `as_of=${b.asOf && b.asOf.length > 0 ? b.asOf : "-"}`,
  `freeze_id=${b.freezeId && b.freezeId.length > 0 ? b.freezeId : "-"}`,
  `source_domain=SEC`,
 ];
 return `# Research context (kernel-authoritative; pass these ids through verbatim)\n${rows.join("\n")}`;
}
// Pre-record shape guard mirroring the kernel's rich-envelope contract
// (COMMITTEE_REQUIRED_KEYS + typed claims/channels + {overall, reasoning}
// materiality). Structural only — the kernel stays authoritative on freeze
// grounding and follow-up shape. Returns the first defect, or "" when shaped.
function committeeEnvelopeError(data: Json): string {
 for (const key of ["executive_view", "claims", "impact_channels", "materiality", "uncertainties", "what_would_change", "follow_ups"]) {
  if (!(key in data)) return `missing '${key}'`;
 }
 if (typeof data.executive_view !== "string") return "'executive_view' must be a string";
 for (const key of ["claims", "impact_channels", "uncertainties", "what_would_change", "follow_ups"]) {
  if (!Array.isArray(data[key])) return `'${key}' must be a list`;
 }
 for (const [i, claim] of (data.claims as unknown[]).entries()) {
  if (!claim || typeof claim !== "object") return `'claims[${i}]' must be an object`;
  const row = claim as Json;
  if (typeof row.text !== "string" || !row.text.trim()) return `'claims[${i}].text' must be a non-empty string`;
  if (!["observed_fact", "inference", "unknown", "contradicted"].includes(str(row.claim_type))) return `'claims[${i}].claim_type' must be one of observed_fact|inference|unknown|contradicted`;
  if (!Array.isArray(row.evidence_ids)) return `'claims[${i}].evidence_ids' must be a list`;
 }
 for (const [i, channel] of (data.impact_channels as unknown[]).entries()) {
  if (!channel || typeof channel !== "object") return `'impact_channels[${i}]' must be an object`;
  const row = channel as Json;
  if (typeof row.text !== "string" || !row.text.trim()) return `'impact_channels[${i}].text' must be a non-empty string`;
  if (!["positive", "negative", "mixed"].includes(str(row.direction))) return `'impact_channels[${i}].direction' must be one of positive|negative|mixed`;
  if (!Array.isArray(row.evidence_ids)) return `'impact_channels[${i}].evidence_ids' must be a list`;
 }
 const mat = data.materiality;
 if (!mat || typeof mat !== "object") return "'materiality' must be an object";
 if (!["critical", "high", "medium", "low"].includes(str((mat as Json).overall).trim().toLowerCase())) return "'materiality.overall' must be one of critical|high|medium|low";
 if (typeof (mat as Json).reasoning !== "string") return "'materiality.reasoning' must be a string";
 return "";
}
function failureCategory(result: Json): string {
 if (result.retry_failure ?? result.retryFailure) return "provider_error";
 const error = str(result.error);
 if (/timeout|timed out/i.test(error)) return "timeout";
 if (/schema|structured|invalid output/i.test(error)) return "model_output_failure";
 return "tool_error";
}
export async function planTaskCall(ctx: TaskPlanContext, input: Json, dataRoot?: string, asOf?: string): Promise<TaskPlan> {
 const mem = runs.get(ctx.researchKey);
 if (!mem) return {};
 const items = objs(input.tasks);
 if (items.length === 0)
  return { block: true, reason: "Research task calls must use the batch form: provide context and a non-empty tasks array." };
 let snapshot: InspectSnapshot;
 try {
  snapshot = await inspect(mem.sessionId, dataRoot, asOf);
 } catch (err) {
  return { block: true, reason: `Research session ${mem.sessionId} is unreadable (${err instanceof Error ? err.message : String(err)}); the spawn is refused.` };
 }
 const sessionId = mem.sessionId;
 const stage = deriveState(snapshot.session, snapshot.jobs, snapshot.latestFreeze).stage;
 // No parent threading: the Director plans omp-owned top-level jobs only (no
 // parent arg), so every child is a fresh root the kernel owns outright.
 const allowed = DIRECTOR_SPAWNS.filter((a) => (STAGE_AGENTS[stage] ?? []).includes(a));
 for (const item of items) {
  const agentType = str(item.agent);
  if (!allowed.includes(agentType))
   return { block: true, reason: `Director may not spawn '${agentType}' in research session ${sessionId} at stage ${stage}; allowed: ${allowed.join(", ") || "none"}.` };
 }
 try {
  const isCommittee = items.some((item) => (ROLES as readonly string[]).includes(str(item.agent)));
  const roleJobs = new Map<string, string>();
  let freezeId = "";
  const cw = typeof snapshot.session.current_wave === "number" && Number.isInteger(snapshot.session.current_wave) ? (snapshot.session.current_wave as number) : 0;
  let wave = Math.max(cw, strs(snapshot.session.freeze_ids).length + 1, 1);
  if (isCommittee) {
   const freeze = snapshot.latestFreeze;
   if (!freeze)
    return { block: true, reason: `No evidence freeze exists for research session ${sessionId}; freeze the wave before running the committee.` };
   freezeId = str(freeze.freeze_id);
   wave = typeof freeze.wave_id === "number" && Number.isInteger(freeze.wave_id) ? (freeze.wave_id as number) : wave;
   const created = await rpc("research.committee.create", { session_id: sessionId, wave_id: wave }, dataRoot, asOf);
   let freshJobs: Json[] = [];
   try {
    freshJobs = (await inspect(sessionId, dataRoot, asOf)).jobs;
   } catch {
    freshJobs = [];
   }
   for (const role of ROLES) {
    const hit = freshJobs.find((j) => str(j.job_type) === role && j.wave_id === wave);
    const jid = str(hit?.job_id);
    if (jid) roleJobs.set(role, jid);
   }
   if (roleJobs.size < ROLES.length) {
    const returned = strs(created.jobs);
    if (returned.length >= ROLES.length) {
     ROLES.forEach((role, i) => {
      if (!roleJobs.has(role) && returned[i]) roleJobs.set(role, returned[i]);
     });
    } else {
     for (const job of objs(created.jobs)) {
      const t = str(job.job_type);
      const jid2 = str(job.job_id);
      if ((ROLES as readonly string[]).includes(t) && jid2 && !roleJobs.has(t)) roleJobs.set(t, jid2);
     }
    }
   }
   if (roleJobs.size < ROLES.length)
    return { block: true, reason: `Committee creation for research session ${sessionId} returned ${roleJobs.size} of ${ROLES.length} role jobs; the batch is refused.` };
  }
  const planned: PlannedItem[] = [];
  const outItems: Json[] = [];
  for (const item of items) {
   const agentType = str(item.agent);
   const jobType = JOB_TYPE[agentType] ?? agentType;
   let jobId = roleJobs.get(agentType) ?? "";
   let itemWave = wave;
   if (!jobId) {
    const job = await rpc("research.job.start", { session_id: sessionId, type: jobType, wave_id: itemWave, budget: { owner: "omp" } }, dataRoot, asOf);
    jobId = str(job.job_id);
    if (!jobId) return { block: true, reason: `research.job.start returned no job id for '${agentType}'.` };
    const w = num(job.wave_id);
    if (w !== null) itemWave = w;
   }
   const name = `${agentType}-${shortSuffix(jobId)}`;
   planned.push({ sessionId, jobId, jobType, agentType, wave: itemWave, name });
   const taskText = str(item.task);
   outItems.push({
    ...item,
    name,
    task: `${researchContextBlock({ sessionId, jobId, wave: itemWave, asOf, freezeId })}${taskText.length > 0 ? `\n\n${taskText}` : ""}`,
   });
  }
  plannedByCall.set(ctx.toolCallId, { sessionId, items: planned });
  const first = planned[0];
  const contextText = str(input.context);
  const shared = researchContextBlock({ sessionId, jobId: first.jobId, wave: first.wave, asOf, freezeId });
  return {
   input: {
    ...input,
    context: `${shared}\nEach item carries its own research_job_id; use the id in your own item, not this batch-wide one.${contextText.length > 0 ? `\n\n${contextText}` : ""}`,
    tasks: outItems,
   },
  };
 } catch (err) {
  return { block: true, reason: `Research job creation failed (${err instanceof Error ? err.message : String(err)}); the spawn is refused and no child was started.` };
 }
}
export async function recordTaskResult(ctx: TaskPlanContext, details: Json, dataRoot?: string, asOf?: string): Promise<void> {
 const plan = plannedByCall.get(ctx.toolCallId);
 if (!plan) return;
 plannedByCall.delete(ctx.toolCallId);
 const results = objs(details.results);
 for (const [index, item] of plan.items.entries()) {
  const result = results[index] ?? {};
  try {
   try {
    await rpc("research.job.runtime", { job_id: item.jobId, runtime: "omp", runtime_agent_id: item.name, runtime_task_call_id: ctx.toolCallId, runtime_agent_type: item.agentType }, dataRoot, asOf);
   } catch {
    // best-effort runtime attach never throws
   }
   if (result.aborted === true) {
    await rpc("research.job.cancel", { job_id: item.jobId }, dataRoot, asOf);
    continue;
   }
   const exitCode = num(result.exit_code) ?? num(result.exitCode) ?? 0;
   if (exitCode !== 0) {
    await rpc("research.job.fail", { job_id: item.jobId, category: failureCategory(result), message: str(result.stderr) || str(result.error) || "OMP child failed" }, dataRoot, asOf);
    continue;
   }
   const out = (result.structured_output ?? result.structuredOutput) as Json | null | undefined;
   let data: Json | null = null;
   let invalid = false;
   if (out && typeof out === "object") {
    if (str((out as Json).status) === "invalid") invalid = true;
    else {
     const d = (out as Json).data;
     data = d && typeof d === "object" ? (d as Json) : null;
    }
   }
   if (invalid) {
    await rpc("research.job.fail", { job_id: item.jobId, category: "model_output_failure", message: `structured output for '${item.agentType}' failed schema validation` }, dataRoot, asOf);
    continue;
   }
   if ((ROLES as readonly string[]).includes(item.jobType)) {
    if (!data) {
     await rpc("research.job.fail", { job_id: item.jobId, category: "model_output_failure", message: `committee role '${item.jobType}' returned no analysis envelope` }, dataRoot, asOf);
     continue;
    }
    const shapeError = committeeEnvelopeError(data);
    if (shapeError) {
     await rpc("research.job.fail", { job_id: item.jobId, category: "model_output_failure", message: `committee role '${item.jobType}' returned an incomplete analysis envelope (${shapeError})` }, dataRoot, asOf);
     continue;
    }
    await rpc("research.analysis.record", { session_id: plan.sessionId, job_id: item.jobId, role: item.jobType, analysis: data }, dataRoot, asOf);
    continue;
   }
   const after = await rpc("research.session.inspect", { session_id: plan.sessionId }, dataRoot, asOf);
   const open = objs(after.jobs).some((j) => str(j.job_id) === item.jobId && str(j.status) === "running");
   if (open)
    await rpc("research.job.fail", { job_id: item.jobId, category: "model_output_failure", message: `'${item.agentType}' returned without submitting a source result` }, dataRoot, asOf);
  } catch (err) {
   try {
    await rpc("research.job.fail", { job_id: item.jobId, category: "synthesis_failed", message: err instanceof Error ? err.message : String(err) }, dataRoot, asOf);
   } catch {
    // job already terminal; nothing further to record
   }
  }
 }
}
