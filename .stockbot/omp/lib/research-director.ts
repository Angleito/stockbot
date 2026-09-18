/** Staged ResearchDirector driver: fetch -> freeze -> atomic trio -> gate -> [next wave, while the gate authorizes] -> finalize.
 *
 * OMP owns the model loop; this module owns stage order only. Every transition is
 * kernel-gated fail-closed: predicates read research.session.inspect results, at most
 * one transition RPC fires per stage step, and kernel errors keep the run entry (nothing
 * is ever invented). The wave gate alone decides continue vs finalize, for every wave:
 * no wave ceiling lives here. Wave-1 dispatches one task batch for every allowed
 * material desk (sec-agent/finra-agent/exa-agent per session source_policy), and
 * targeted waves dispatch only requested desks; the committee runs as one task
 * batch for the trio, with prompts naming the call_tool verbs and research ids
 * each child must pass through verbatim.
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
const JOB_TERMINAL: Record<string, true> = { completed: true, failed: true, cancelled: true, timed_out: true };
const SOURCE_AGENTS = ["sec-agent", "finra-agent", "exa-agent"] as const;
const DOMAIN_FOR_AGENT: Record<string, string> = { "sec-agent": "SEC", "finra-agent": "FINRA", "exa-agent": "WEB" };
const AGENT_FOR_DOMAIN: Record<string, string> = { SEC: "sec-agent", FINRA: "finra-agent", WEB: "exa-agent" };
const KNOWN_DOMAINS = ["SEC", "FINRA", "WEB"];

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] =>
 Array.isArray(v) ? v.filter((e): e is string => typeof e === "string") : [];
const objs = (v: unknown): Json[] =>
 Array.isArray(v) ? v.filter((e): e is Json => !!e && typeof e === "object") : [];
// Same-turn delivery renderer: the persisted rich final_result resolves to the
// substantive structured answer (Bottom line through evidence refs + scope
// line). Concise when simple — empty sections drop out — but never a bare
// "finalized" status line. Mirrors the kernel render_final_result shape.
function hasFinalSections(fr: Json): boolean {
 if (!fr || typeof fr !== "object") return false;
 return ["executive_summary", "consensus", "base_case", "major_evidence", "impact_channels", "first_order_effects", "second_order_effects", "bull_case", "bear_case", "critical_disagreements", "disagreements", "positioning", "catalysts", "uncertainties", "evidence_limitations", "coverage", "sources", "grounded_claims", "claims"].some((key) => {
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
 const strList = (value: unknown): string[] => strs(value).map((line) => line.trim()).filter((line) => line.length > 0);
 const mergedStrs = (...values: unknown[]): string[] => {
  const seen: Record<string, true> = {};
  const out: string[] = [];
  for (const v of values) for (const line of strList(v)) if (!seen[line]) { seen[line] = true; out.push(line); }
  return out;
 };
 const section = (header: string, lines: string[]): string[] => (lines.length > 0 ? [`## ${header}`, ...lines] : []);
 const coverageVerdictKeys: Record<string, true> = { source_domain: true, source_sufficiency: true, useful_for_question: true, complete: true, detail: true, summary: true };
 const coverageLines = (value: unknown): string[] => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const cov = value as Json;
  const lines: string[] = [];
  for (const key of ["sec", "finra", "web"]) {
   const secRaw = cov[key];
   if (!secRaw || typeof secRaw !== "object" || Array.isArray(secRaw)) continue;
   const sec = secRaw as Json;
   const parts: string[] = [];
   for (const field of Object.keys(sec).sort()) {
    if (coverageVerdictKeys[field]) continue;
    const items = strList(sec[field]);
    if (items.length > 0) parts.push(`${field}: ${items.join(", ")}`);
   }
   const detail = str(sec.detail).trim() || str(sec.summary).trim();
   const head = key.toUpperCase();
   if (parts.length > 0 || detail) lines.push(`- ${head} — ${parts.join("; ")}${detail && parts.length > 0 ? ` — ${detail}` : detail}`);
   else lines.push(`- ${head}`);
  }
  const gaps = strList(cov.gaps);
  if (gaps.length > 0) lines.push(`- gaps: ${gaps.join(", ")}`);
  return lines;
 };
 const sourceLine = (item: unknown): string => {
  if (!item || typeof item !== "object") return "";
  const row = item as Json;
  const evidenceId = str(row.evidence_id).trim();
  const domain = str(row.domain).trim().toUpperCase() || "SOURCE";
  const integrity = str(row.integrity_class ?? row.integrity).trim().toUpperCase();
  const document = str(row.document).trim() || str(row.source_name).trim();
  const label = integrity ? `${domain} [${integrity}]` : domain;
  const head = [label, document].filter((p) => p.length > 0).join(" ") || evidenceId || "source";
  return evidenceId && head !== evidenceId ? `- ${head} [${evidenceId}]` : `- ${head}`;
 };
 const summary = str(fr.executive_summary).trim() || str(fr.answer).trim() || str(fr.content).trim();
 const out: string[] = summary ? [`Bottom line: ${summary}`] : [];
 const consensus = str(fr.consensus).trim();
 if (consensus && consensus.toLowerCase() !== "none stated") out.push(`Consensus: ${consensus}`);
 const baseCase = str(fr.base_case).trim();
 if (baseCase) out.push(...section("Base case", [`- ${baseCase}`]));
 out.push(...section("Major direct exposures", objs(fr.impact_channels).map(channelLine).filter((line) => line.length > 0)));
 const majorRaw = Array.isArray(fr.major_evidence) ? fr.major_evidence : fr.direct_evidence;
 out.push(...section("Major evidence", effectLines(majorRaw)));
 out.push(...section("First-order effects", effectLines(fr.first_order_effects)));
 out.push(...section("Second-order effects", effectLines(fr.second_order_effects)));
 out.push(...section("Bull case", sideLines(fr.bull_case)));
 out.push(...section("Bear case", sideLines(fr.bear_case)));
 out.push(...section("Critical disagreements", mergedStrs(fr.critical_disagreements, fr.disagreements).map((line) => `- ${line}`)));
 out.push(...section("Positioning", mergedStrs(fr.positioning).map((line) => `- ${line}`)));
 out.push(...section("Catalysts", mergedStrs(fr.catalysts).map((line) => `- ${line}`)));
 out.push(...section("Uncertainties", mergedStrs(fr.uncertainties, fr.absence_observations).map((line) => `- ${line}`)));
 out.push(...section("What would change the view", mergedStrs(fr.what_would_change, fr.what_changes_the_view).map((line) => `- ${line}`)));
 out.push(...section("Limitations", mergedStrs(fr.evidence_limitations, fr.limitations).map((line) => `- ${line}`)));
 out.push(...section("Coverage", coverageLines(fr.coverage)));
 out.push(...section("Sources", objs(fr.sources).map(sourceLine).filter((line) => line.length > 0)));
 const rawClaims = Array.isArray(fr.grounded_claims) ? fr.grounded_claims : fr.claims;
 out.push(...section("Evidence refs", objs(rawClaims).map(claimLine).filter((line) => line.length > 0)));
 const filingRefs = mergedStrs(fr.filing_references, fr.evidence_refs);
 if (filingRefs.length > 0) out.push(`Evidence references: ${filingRefs.join(", ")}`);
 const scopeObj = fr.research_scope && typeof fr.research_scope === "object" ? (fr.research_scope as Json) : {};
 const scopeRaw = Array.isArray(scopeObj.allowed_sources) ? scopeObj.allowed_sources : scopeObj.allowed;
 const names = Array.isArray(scopeRaw) ? scopeRaw.filter((e): e is string => typeof e === "string" && e.trim().length > 0) : [];
 const allowed = names.length > 0 ? names : ["SEC"];
 out.push(allowed.length === 1 && allowed[0].toUpperCase() === "SEC" ? "Scope: SEC sources only." : `Scope: ${allowed.join(", ")} sources only.`);
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
interface DeskState { domain: string; agent: string; status: string; jobId: string }
interface StageState {
 stage: Stage;
 jobId: string;
 targeted: string;
 targetedDomain: string;
 allowedDesks: string[];
 activeWave: number;
 gateSettled: boolean;
 gateStopped: boolean;
 freezeEvidenceIds: string[];
 unfrozenEvidenceIds: string[];
 waveDesks: DeskState[];
 waveTerminal: boolean;
 trio: TrioState;
}
const jobStatus = (j: Json): string => str(j.status);
const jobWave = (j: Json): number => (typeof j.wave_id === "number" && Number.isInteger(j.wave_id) ? (j.wave_id as number) : 0);
function sessionAllowedDesks(session: Json): string[] {
 const policy = session.source_policy;
 const raw = policy && typeof policy === "object" ? (policy as Json).allowed : undefined;
 // Legacy sessions predate the policy column: jobs without a source_domain are
 // SEC rows, so absent/empty policy means SEC-only (single sec-agent desk).
 const names = Array.isArray(raw) ? raw.filter((e): e is string => typeof e === "string" && e.trim().length > 0).map((e) => e.trim().toUpperCase()) : [];
 const allowed = (names.length > 0 ? names : ["SEC"]).filter((d) => KNOWN_DOMAINS.includes(d));
 return [...SOURCE_AGENTS].filter((a) => allowed.includes(DOMAIN_FOR_AGENT[a]));
}
function targetedDesks(allowedDesks: string[], targetedDomain: string): string[] {
 const want = targetedDomain.trim().toUpperCase();
 if (!want) return allowedDesks;
 const agent = AGENT_FOR_DOMAIN[want];
 if (!agent) return [];
 return allowedDesks.includes(agent) ? [agent] : [];
}
function waveDescriptors(jobs: Json[], wave: number): DeskState[] {
 // Legacy rows predate source_domain: an unmarked source_agent job is the SEC desk.
 const rows = jobs.filter((j) => str(j.job_type) === "source_agent" && jobWave(j) === wave);
 return rows
  .map((j) => {
   const source = str(j.source_domain).trim().toUpperCase() || "SEC";
   return { domain: source, agent: AGENT_FOR_DOMAIN[source] ?? "sec-agent", status: jobStatus(j), jobId: str(j.job_id) };
  })
  .sort((a, b) => (a.agent < b.agent ? -1 : 1));
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
 const targetedDomain = td.trim().toUpperCase();
 const currentWave = typeof session.current_wave === "number" && Number.isInteger(session.current_wave) ? session.current_wave : 0;
 // Active wave N: director drives N>=1 from kernel state; wave 1 auto-fetches every
 // allowed desk, later waves are gate-authorized for requested desks only.
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
 const allowedDesks = sessionAllowedDesks(session);
 const wanted = activeWave === 1 ? allowedDesks : targetedDesks(allowedDesks, targetedDomain);
 const waveDesks = waveDescriptors(jobs, activeWave);
 const terminalByAgent: Record<string, DeskState> = {};
 for (const d of waveDesks) terminalByAgent[d.agent] = d;
 // One kernel source job per domain: freeze only when every selected desk is
 // terminal (sufficient/insufficient/failed-with-limitation alike); a missing
 // desk still needs starting. Infra corruption (no job row survives) restarts
 // the desk instead of freezing.
 const waveTerminal = wanted.length > 0 && wanted.every((a) => terminalByAgent[a] !== undefined && JOB_TERMINAL[terminalByAgent[a].status] === true);
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
 return { stage, jobId, targeted, targetedDomain, allowedDesks, activeWave, gateSettled, gateStopped, freezeEvidenceIds, unfrozenEvidenceIds, waveDesks, waveTerminal, trio };
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
const PROVENANCE_KINDS: Record<string, true> = { sec_source: true, finra_record: true, web_source: true, search_run: true, none: true };
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
 if (kind === "finra_record") {
  const tool = provStr(prov.tool_name);
  const identity = provStr(prov.record_identity);
  if (tool === null || identity === null) return null;
  const out: Json = { kind: "finra_record", tool_name: tool.trim(), record_identity: identity.trim() };
  for (const k of ["dataset", "source_uri", "known_at"]) {
   const raw = prov[k];
   if (typeof raw === "string" && raw.trim()) out[k] = raw.trim();
  }
  return out;
 }
 if (kind === "web_source") {
  const url = provStr(prov.url);
  const excerpt = provStr(prov.excerpt);
  if (url === null || excerpt === null) return null;
  const out: Json = { kind: "web_source", url: url.trim(), excerpt: excerpt.trim() };
  for (const k of ["title", "domain", "published_at", "retrieved_at"]) {
   const raw = prov[k];
   if (typeof raw === "string" && raw.trim()) out[k] = raw.trim();
  }
  return out;
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
// Evidence item shapes per desk. SEC cites the reloaded filing handle +
// passage; FINRA/WEB cite the persisted tool result id + the record values /
// highlight the kernel stored. A bare number/URL with no persisted result id
// fails ERR_RAW_SOURCE_REQUIRED; a citation the persisted bytes do not
// reproduce fails ERR_PASSAGE_NOT_IN_SOURCE.
const SEC_ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher, e.g. SEC>", ` +
 `"source_uri": "<canonical document URL>", "source_record_id": "<SEC accession, e.g. 0000320193-24-000123>", "document_name": "<filing or exhibit you opened>", ` +
 `"source_handle": <the source_handle get_sec_document returned for that window>, "matching_passage": "<passage quoted from that window>", "known_at": "<ISO-8601 timestamp>", "claim_kind": "observed_fact"}`;
const FINRA_ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<dataset publisher, e.g. FINRA>", ` +
 `"tool_result_id": "<the tool_result_id the kernel returned for the FINRA tool response you read>", "record_identity": "<dataset row/briefing values you are citing>", ` +
 `"matching_passage": "<same record values>", "known_at": "<ISO-8601 dataset public-knowledge time, never retrieval time>", "claim_kind": "observed_fact"}`;
const WEB_ITEM_SHAPE =
 `"item": {"content": "<observed fact>", "claim_text": "<single claim>", "subject": "<ticker>", "source_name": "<publisher>", ` +
 `"tool_result_id": "<the tool_result_id the kernel returned for the search_web response you read>", "source_record_id": "<result URL you are citing>", ` +
 `"excerpt": "<highlight text you are citing>", "matching_passage": "<same highlight text>", "known_at": "<ISO-8601 publication time, never retrieval time>", "claim_kind": "observed_fact"}`;
const ITEM_SHAPES: Record<string, string> = { "sec-agent": SEC_ITEM_SHAPE, "finra-agent": FINRA_ITEM_SHAPE, "exa-agent": WEB_ITEM_SHAPE };
// One source workflow per desk: navigation-only search, raw-document evidence,
// no limits, coverage submitted structurally. The desk name (sec/finra/exa)
// selects the branch map; the submit/freeze contract is identical per desk.
const DESK_WORKFLOWS: Record<string, string> = {
 "sec-agent":
  `Work it as a branch map: name the material branches this question needs, search SEC for each, open the filings behind every hit, read the documents/exhibits/passages, and record only raw-document-backed evidence. ` +
  `There is no maximum number of searches, filing reads, document reads, exhibit reads, or waves: keep going while the work is materially useful; the only waste is an exact repeat that adds nothing. ` +
  `Search results are navigation artifacts, never evidence: opening the document is what makes a finding citable. ` +
  `Open the filing with get_sec_document and cite what it returned: an observed_fact needs that call's canonical source_handle plus the passage you are citing (matching_passage, or passage/section) — the kernel reloads the window itself, so a hit or a handle-less citation fails ERR_RAW_SOURCE_REQUIRED and a passage the window does not contain fails ERR_PASSAGE_NOT_IN_SOURCE. ` +
  `An absence_observation instead needs claim_kind "absence_observation" with search_id, query, and the searched coverage (forms, dates, partitions, entities, docs, gaps, pagination_complete, complete), and must not carry an accession: it is recorded as a session coverage artifact (what the search did and did not reach), never as citable evidence, and no filing can prove it. ` +
  `Track entities, forms, exhibits, branches covered/remaining, and the questions still open as you go: sufficiency is coverage of what you checked, never a whole-question answer. `,
 "finra-agent":
  `Work it as a branch map: name the material FINRA branches this question needs (tickers, datasets, short-interest vs short-volume vs threshold status, settlement/date windows), query the FINRA datasets for each, and record only dataset-row-backed evidence. ` +
  `There is no maximum number of dataset reads, queries, rows, or waves: keep going while the work is materially useful; the only waste is an exact repeat that adds nothing. ` +
  `Dataset rows are navigation only: the recorded finra_record is what makes a finding citable. ` +
  `Track tickers, datasets, windows, branches covered/remaining, and the questions still open as you go: sufficiency is coverage of what you checked, never a whole-question answer. `,
 "exa-agent":
  `Work it as a branch map: name the material semantic branches this question needs (announcements, catalysts, market reaction, industry developments, commentary, counterevidence), run web searches for each, inspect the results, and record only inspected-result-backed evidence. ` +
  `There is no maximum number of searches or waves: keep going while the work is materially useful; the only waste is an exact repeat that adds nothing. ` +
  `Search highlights are qualitative context only: the recorded web_source is what makes a finding citable. Never use web results for SEC or FINRA canonical facts. ` +
  `Track branches covered/remaining and the questions still open as you go: sufficiency is coverage of what you checked, never a whole-question answer. `,
};
const DESK_SCOUTS: Record<string, string> = { "sec-agent": "sec-scout", "finra-agent": "finra-scout", "exa-agent": "exa-scout" };
function deskWorkflow(agent: string): string {
 return DESK_WORKFLOWS[agent] ?? DESK_WORKFLOWS["sec-agent"];
}
function sourceSteps(sessionId: string, jobId: string, agent: string, domain: string, objective: string, asOf?: string): string {
 const cutoff = asOf && asOf.length > 0 ? `known_at must be an ISO-8601 timestamp on or before the session cutoff ${asOf}` : `no session cutoff applies; known_at may be any ISO-8601 timestamp or omitted`;
 const scout = DESK_SCOUTS[agent] ?? "sec-scout";
 const brief = objective.trim().length > 0 ? ` Objective: ${objective.trim()}` : "";
 return (
  `${deskWorkflow(agent)}The ${agent} child (source_domain=${domain}) records evidence with research_add_evidence on session ${sessionId} and job ${jobId} (${ITEM_SHAPES[agent] ?? SEC_ITEM_SHAPE}); ` +
  `its ${scout} batch fans out inside its own session.${brief} ` +
  `Provenance is kernel-validated (sec_source|finra_record|web_source|search_run|none) and ${cutoff}; unprovenanced or out-of-order calls fail closed. ` +
  `When this source investigation is complete, the ${agent} MUST call research_submit_source_result exactly once with the structured coverage {"useful_for_question": "sufficient"|"insufficient", ...}, evidence_ids, and unresolved_questions, then stop. ` +
  `That ends this source job and returns control to the Director, which owns freezing, waves, and the session; it never freezes, ends a wave, or ends the session. Do not attempt to freeze.`
 );
}
function deskListText(agents: string[]): string {
 return agents.map((a) => `${a} (${DOMAIN_FOR_AGENT[a] ?? "SEC"})`).join(", ");
}
function followUpText(followUps: Json[]): string {
 const rows = followUps
  .map((f) => {
   const question = str(f.question).trim();
   if (!question) return "";
   const why = str(f.why_material || f.why_it_matters).trim();
   const domain = str(f.requested_source_domain || f.suggested_source).trim().toUpperCase();
   const gain = str(f.expected_gain).trim().toLowerCase();
   const bits = [`question: ${question}`];
   if (why) bits.push(`why: ${why}`);
   if (domain) bits.push(`requested_source_domain: ${domain}`);
   if (gain) bits.push(`expected_gain: ${gain}`);
   return `\n  - follow-up {${bits.join("; ")}}`;
  })
  .filter((line) => line.length > 0);
 return rows.length > 0 ? `\nFollow-ups (carry question/why/requested_source_domain/expected_gain through to the desk):${rows.join("")}` : "";
}
function legacyFetchPrompt(sessionId: string, jobId: string, question: string, asOf?: string): string {
 return (
  `Research session ${sessionId} created for "${question}". Dispatch SEC research now as one task batch with context and one task for agent sec-agent ` +
  `(task: the research objective plus branch guidance; the batch plans the omp-owned source job). ` +
  sourceSteps(sessionId, jobId, "sec-agent", "SEC", question, asOf)
 );
}
function waveBatchPrompt(wave: number, sessionId: string, agents: string[], jobs: { agent: string; jobId: string }[], objective: string, targeted: string, asOf?: string, followUps: Json[] = []): string {
 const label = wave <= 1 ? "Wave-1" : `Wave-${wave}`;
 const lines = jobs.map((j) => `- ${j.agent} (source_domain=${DOMAIN_FOR_AGENT[j.agent] ?? "SEC"}): job ${j.jobId} — ${sourceSteps(sessionId, j.jobId, j.agent, DOMAIN_FOR_AGENT[j.agent] ?? "SEC", objective, asOf)}`);
 return (
  `${label} authorized for research session ${sessionId}${targeted}. Dispatch source research now as ONE task batch with context and one task per requested desk: ${deskListText(agents)} ` +
  `(each task: its desk objective plus branch guidance; the batch plans one omp-owned source job per domain). ` +
  `${lines.join("\n")}${followUpText(followUps)}` +
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
  `Authority: SEC PRIMARY_DOCUMENT outranks FINRA CANONICAL_STRUCTURED outranks WEB EXTERNAL_SOURCE; web context never overrides a canonical record and never becomes a company-reported fact. ` +
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
  `Call call_tool with name="research_finalize" and arguments={"session_id": "${sessionId}", "answer": "<Bottom line plus Major direct exposures / Second-order / Bull / Bear / Uncertainties / Limitations, every factual claim tied to evidence ids>", ` +
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
 const created = await rpc("research.session.create", { question, policy: { research_sources: { mode: "allowlist", sources: ["SEC", "FINRA", "WEB"] } } }, dataRoot, asOf);
 const sessionId = str(created.session_id);
 if (!sessionId) throw new Error("research.session.create failed: missing session_id");
 runs.set(runId, { sessionId });
 const adv = await advanceOnAgentEnd(runId, "", dataRoot, asOf, { keepRun: true });
 if (!adv) return { sessionId, prompt: `Research session ${sessionId} created for "${question}".` };
 return { sessionId, prompt: adv.done ? adv.answer : adv.prompt };
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
 const question = str(session.query) || str(session.objective) || sid;
 const objective = str(session.objective) || question;
 const wantedWave1 = d.allowedDesks;
 if (evidence.length === 0) {
  const legacyOpen = jobs.filter((j) => str(j.job_type) === "source_agent" && jobWave(j) === d.activeWave && (jobStatus(j) === "running" || jobStatus(j) === "queued") && !str(j.source_domain).trim());
  const openWave1 = jobs.filter((j) => str(j.job_type) === "source_agent" && jobWave(j) === d.activeWave && (jobStatus(j) === "running" || jobStatus(j) === "queued"));
  const legacyOnly = legacyOpen.length === 1 && openWave1.length === 1 && wantedWave1.length === 1 && wantedWave1[0] === "sec-agent";
  if (legacyOnly) {
   return { done: false, prompt: legacyFetchPrompt(sid, str(legacyOpen[0].job_id) || d.jobId, question, asOf) };
  }
  if (openWave1.length > 0 || d.waveDesks.length < wantedWave1.length) {
   const started: { agent: string; jobId: string }[] = [];
   try {
    for (const agent of wantedWave1) {
     const domain = DOMAIN_FOR_AGENT[agent] ?? "SEC";
     const existing = d.waveDesks.find((x) => x.agent === agent);
     if (existing && existing.jobId) {
      started.push({ agent, jobId: existing.jobId });
      continue;
     }
     const created = await rpc("research.job.start", { session_id: sid, type: "source_agent", source: domain, wave_id: d.activeWave, budget: { owner: "omp" } }, dataRoot, asOf);
     const nid = str(created.job_id);
     if (!nid) throw new Error(`research.job.start failed: missing job_id for '${agent}'`);
     started.push({ agent, jobId: nid });
    }
   } catch (err) {
    return { done: false, prompt: `Wave-1 source jobs for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
   }
   try {
    await inspect(sid, dataRoot, asOf);
   } catch {
    // inspect refresh is best-effort; explicit ids still drive the prompt
   }
   return { done: false, prompt: waveBatchPrompt(1, sid, wantedWave1, started, objective, "", asOf) };
  }
  // Active source jobs closed with no evidence: fall through to freeze;
  // empty-with-limitations freeze succeeds kernel-side and carries limitations forward.
 }
 const freezes = strs(session.freeze_ids);
 if (freezes.length === 0) {
  const legacyStill = jobs.find((j) => str(j.job_type) === "source_agent" && jobWave(j) === 1 && (jobStatus(j) === "running" || jobStatus(j) === "queued") && !str(j.source_domain).trim());
  const open = d.waveDesks.filter((x) => x.status === "running" || x.status === "queued");
  if (legacyStill && open.length === 1 && wantedWave1.length === 1 && wantedWave1[0] === "sec-agent") {
   return { done: false, prompt: `Source still running for research session ${sid}. ${sourceSteps(sid, str(legacyStill.job_id), "sec-agent", "SEC", objective, asOf)}` };
  }
  if (open.length > 0 || !d.waveTerminal) {
   const lines = wantedWave1.map((a) => {
    const hit = d.waveDesks.find((x) => x.agent === a);
    return hit && hit.jobId ? `- ${a} (${hit.domain}): job ${hit.jobId} — ${sourceSteps(sid, hit.jobId, a, hit.domain, objective, asOf)}` : `- ${a}: awaiting its source job`;
   });
   return { done: false, prompt: `Source still running for research session ${sid} (one kernel source job per domain: ${deskListText(wantedWave1)}). Dispatch the missing desks now as ONE task batch; running desks keep fetching.\n${lines.join("\n")}` };
  }
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
 let targetedDomain = d.targetedDomain;
 let gateFollowUps: Json[] = [];
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
  targetedDomain = td.trim().toUpperCase();
  gateFollowUps = objs(dec.follow_ups);
  // Next wave dispatches ONLY requested desks: an empty/unknown request set stops.
  const requested = targetedDesks(d.allowedDesks, targetedDomain);
  if (requested.length === 0)
   return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Wave gate stopped research session ${sid} (no requested desk). `, trioIds) };
 }
 // Authorized: drive the next wave's source jobs (one per requested desk), fetch, freeze, trio, gate.
 const N = d.activeWave;
 const wanted = N === 1 ? d.allowedDesks : targetedDesks(d.allowedDesks, targetedDomain || str(session.targeted_domain));
 if (wanted.length === 0)
  return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Wave gate stopped research session ${sid} (no requested desk). `, trioIds) };
 const haveByAgent: Record<string, DeskState> = {};
 for (const x of waveDescriptors(jobs, N)) haveByAgent[x.agent] = x;
 const missing = wanted.filter((a) => !haveByAgent[a]?.jobId);
 if (missing.length > 0) {
  const started: { agent: string; jobId: string }[] = [];
  try {
   for (const agent of wanted) {
    const hit = haveByAgent[agent];
    if (hit?.jobId) {
     started.push({ agent, jobId: hit.jobId });
     continue;
    }
    const domain = DOMAIN_FOR_AGENT[agent] ?? "SEC";
    const created = await rpc("research.job.start", { session_id: sid, type: "source_agent", source: domain, wave_id: N, budget: { owner: "omp" } }, dataRoot, asOf);
    const nid = str(created.job_id);
    if (!nid) throw new Error(`research.job.start failed: missing job_id for '${agent}'`);
    started.push({ agent, jobId: nid });
   }
  } catch (err) {
   return { done: false, prompt: `Wave-${N} source jobs for research session ${sid} failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only; the run stays staged.` };
  }
  try {
   await inspect(sid, dataRoot, asOf);
  } catch {
   // inspect refresh is best-effort; the explicit ids still drive the prompt
  }
  return { done: false, prompt: waveBatchPrompt(N, sid, wanted, started, objective, targeted, asOf, gateFollowUps) };
 }
 // Wave-N freeze fires only once every requested desk is terminal; novelty
 // decides finalize vs freeze, never fetch vs freeze.
 const openWave = wanted.map((a) => haveByAgent[a]).filter((x) => x && (x.status === "running" || x.status === "queued"));
 const allTerminal = wanted.every((a) => haveByAgent[a] && JOB_TERMINAL[haveByAgent[a].status] === true);
 if (openWave.length > 0 || !allTerminal) {
  // source work underway: keep fetching regardless of novelty
  const started = wanted.map((a) => ({ agent: a, jobId: haveByAgent[a].jobId }));
  return { done: false, prompt: waveBatchPrompt(N, sid, wanted, started, objective, targeted, asOf, gateFollowUps) };
 }
 // Stop when covered + no material request + no routes + no novelty (+repeat-block):
 // the gate already declined those states; a terminal wave with nothing past the
 // freeze and no new requested desk finalizes instead of re-freezing.
 if (d.unfrozenEvidenceIds.length === 0 && gateFollowUps.length === 0 && wanted.every((a) => haveByAgent[a] && JOB_TERMINAL[haveByAgent[a].status] === true)) {
  return { done: false, prompt: finalizePrompt(sid, d.trio.fid, d.freezeEvidenceIds.join(", "), `Wave gate stopped research session ${sid} (covered, no material follow-up). `, trioIds) };
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
// The Director owns every main-session spawn: SOURCE_RESEARCH allows the allowed
// material desks only (sec-agent/finra-agent/exa-agent per session source_policy;
// targeted waves narrow to requested desks), COMMITTEE allows the trio only,
// FINAL allows nothing. Nested fan-out runs inside each desk agent's own child
// session, which passes the main-session gate through untouched; the kernel
// spawn policy stays the single owner there.
const num = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);
const DIRECTOR_SPAWNS = ["sec-agent", "finra-agent", "exa-agent", "stockbot", "bullbot", "bearbot"];
function stageAgents(stage: Stage, allowedDesks: string[], targetedDomain: string, activeWave: number): string[] {
 if (stage === "COMMITTEE") return ["stockbot", "bullbot", "bearbot"];
 if (stage !== "SOURCE_RESEARCH") return [];
 if (activeWave !== 1) {
  const requested = targetedDesks(allowedDesks, targetedDomain);
  return requested.length > 0 ? requested : [];
 }
 return allowedDesks;
}
const JOB_TYPE: Record<string, string> = {
 "sec-agent": "source_agent",
 "finra-agent": "source_agent",
 "exa-agent": "source_agent",
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
 domain: string;
 wave: number;
 name: string;
}
const plannedByCall = new Map<string, { sessionId: string; items: PlannedItem[] }>();
function shortSuffix(jobId: string): string {
 const cleaned = jobId.replace(/[^a-zA-Z0-9]/g, "");
 return cleaned.slice(-8).toLowerCase() || "job";
}
function researchContextBlock(b: { sessionId: string; jobId: string; wave: number; asOf?: string; freezeId?: string; domain?: string; question?: string; objective?: string }): string {
 const rows = [
  `research_session_id=${b.sessionId}`,
  `research_job_id=${b.jobId}`,
  `wave_id=${b.wave}`,
  `as_of=${b.asOf && b.asOf.length > 0 ? b.asOf : "-"}`,
  `freeze_id=${b.freezeId && b.freezeId.length > 0 ? b.freezeId : "-"}`,
  `source_domain=${b.domain && b.domain.length > 0 ? b.domain : "SEC"}`,
  `question=${b.question && b.question.length > 0 ? b.question : "-"}`,
  `objective=${b.objective && b.objective.length > 0 ? b.objective : "-"}`,
  `cutoff=${b.asOf && b.asOf.length > 0 ? b.asOf : "-"}`,
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
 const d = deriveState(snapshot.session, snapshot.jobs, snapshot.latestFreeze);
 const stage = d.stage;
 const question = str(snapshot.session.query) || str(snapshot.session.objective) || sessionId;
 const objective = str(snapshot.session.objective) || question;
 // No parent threading: the Director plans omp-owned top-level jobs only (no
 // parent arg), so every child is a fresh root the kernel owns outright.
 const allowed = DIRECTOR_SPAWNS.filter((a) => stageAgents(stage, d.allowedDesks, d.targetedDomain || str(snapshot.session.targeted_domain), d.activeWave).includes(a));
 for (const item of items) {
  const agentType = str(item.agent);
  if (!allowed.includes(agentType))
   return { block: true, reason: `Director may not spawn '${agentType}' in research session ${sessionId} at stage ${stage}; allowed: ${allowed.join(", ") || "none"}.` };
 }
 try {
  const isCommittee = items.some((item) => (ROLES as readonly string[]).includes(str(item.agent)));
  const isSource = items.some((item) => (SOURCE_AGENTS as readonly string[]).includes(str(item.agent)));
  if (isCommittee && isSource)
   return { block: true, reason: `Research session ${sessionId}: source desks and committee roles never share one task batch.` };
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
  if (isSource) {
   // Targeted waves dispatch ONLY requested desks: a batch naming an
   // unrequested desk is refused before any kernel job starts.
   const wanted = wave === 1 ? d.allowedDesks : targetedDesks(d.allowedDesks, d.targetedDomain || str(snapshot.session.targeted_domain));
   const named = items.map((item) => str(item.agent));
   const extra = named.filter((a) => !wanted.includes(a));
   if (extra.length > 0)
    return { block: true, reason: `Research session ${sessionId} wave-${wave} requests only ${wanted.join(", ") || "no desk"}; refusing unrequested ${extra.join(", ")}.` };
   const missing = wanted.filter((a) => !named.includes(a));
   if (wave === 1 && missing.length > 0)
    return { block: true, reason: `Research session ${sessionId} Wave-1 needs one task per allowed desk (${wanted.join(", ")}); missing ${missing.join(", ")}.` };
  }
  // Explicit-domain reuse: startResearch/advance own wave-1 starts on the
  // real seam; the plan step must not start a second job for the same
  // wave-domain lane. Legacy seed rows carry no source_domain, so the
  // `director plans sec-agent` seed still starts fresh; a stub row that
  // names the domain reuses it.
  const reuseByDomain: Record<string, string> = {};
  for (const j of snapshot.jobs) {
   const st = jobStatus(j);
   if (str(j.job_type) !== "source_agent" || (st !== "running" && st !== "queued")) continue;
   if (jobWave(j) !== wave) continue;
   const dom = str(j.source_domain).trim().toUpperCase();
   if (!dom || reuseByDomain[dom]) continue;
   const jid = str(j.job_id);
   if (jid) reuseByDomain[dom] = jid;
  }
  const planned: PlannedItem[] = [];
  const outItems: Json[] = [];
  for (const item of items) {
   const agentType = str(item.agent);
   const jobType = JOB_TYPE[agentType] ?? agentType;
   const domain = DOMAIN_FOR_AGENT[agentType] ?? "";
   const reuseKey = domain.trim().toUpperCase();
   let jobId = roleJobs.get(agentType) ?? (reuseKey ? (reuseByDomain[reuseKey] ?? "") : "");
   let itemWave = wave;
   if (!jobId) {
    const startArgs: Json = { session_id: sessionId, type: jobType, wave_id: itemWave, budget: { owner: "omp" } };
    if (domain) startArgs.source = domain;
    const job = await rpc("research.job.start", startArgs, dataRoot, asOf);
    jobId = str(job.job_id);
    if (!jobId) return { block: true, reason: `research.job.start returned no job id for '${agentType}'.` };
    const w = num(job.wave_id);
    if (w !== null) itemWave = w;
   }
   const name = `${agentType}-${shortSuffix(jobId)}`;
   planned.push({ sessionId, jobId, jobType, agentType, domain, wave: itemWave, name });
   const taskText = str(item.task);
   const withObjective = taskText.length > 0 ? taskText : ((SOURCE_AGENTS as readonly string[]).includes(agentType) ? objective : question);
   outItems.push({
    ...item,
    name,
    task: `${researchContextBlock({ sessionId, jobId, wave: itemWave, asOf, freezeId, domain, question, objective })}${withObjective.length > 0 ? `\n\n${withObjective}` : ""}`,
   });
  }
  plannedByCall.set(ctx.toolCallId, { sessionId, items: planned });
  const first = planned[0];
  const contextText = str(input.context);
  const shared = researchContextBlock({ sessionId, jobId: first.jobId, wave: first.wave, asOf, freezeId, domain: first.domain, question, objective });
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
   // Per-desk failure is a limitation, not fatal: the failing desk fails closed
   // while sibling desks keep their terminal verdicts; the freeze carries the
   // limitation forward. Only infra corruption (unreadable session) is fatal.
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
