/** Trusted-state loader for TypeSafe judge/review tools.
 *
 * Tools grade kernel records, never model-supplied stories: every loader pulls
 * its evaluator state through the research bridge (inspect + tool.invoke
 * research_read) and throws Error("typesafe_untrusted_state") on anything
 * missing, unreadable, or mismatched. Tools catch that into an isError block
 * with no auth, so failure always blocks fail-closed. Error text is constant:
 * keys and transcripts never leak into errors.
 * Reasoning calls are OMP-native (@typesafe-ai/sdk), while trusted research state still travels through the Python research bridge (inspect + research_read).
 */

import { hashAction } from "../research-control.ts";
import { getResearchBridge, type BridgeCall } from "../research-director.ts";

type Json = Record<string, unknown>;

export interface StateLoaderDeps {
 bridge?: BridgeCall;
 dataRoot?: string;
 asOf?: string;
}

export interface EvidenceState { objective: string; claim: string; evidence: Json }
export interface ClaimState { objective: string; claim: string; evidence_set: Json[]; actionable: boolean }
export interface CoverageState {
 objective: string;
 branch_map: Json;
 coverage: Json;
 claim_states: { text: string; evidence_ids: string[] }[];
 actionable: boolean;
}
export interface ContinuationState {
 objective: string;
 coverage: Json;
 open_questions: string[];
 current_conclusions: { text: string; evidence_ids: string[] }[];
}
export interface CandidateGap { objective: string; gap: string }
export interface RoleOutputState {
 role: string;
 objective: string;
 evidence: Json[];
 output: Json;
 freezeId: string;
 freezeEvidenceIds: string[];
 freezeEvidenceHash: string;
}
export interface CommitteeState {
 objective: string;
 evidence: Json[];
 stockbot: Json;
 bullbot: Json;
 bearbot: Json;
 roleJobIds: { stockbot: string; bullbot: string; bearbot: string };
 freezeHash: string;
}
export interface FinalState {
 objective: string;
 evidence: Json[];
 committee: { stockbot: Json; bullbot: Json; bearbot: Json };
}

// Residual coverage lists that keep research actionable (kernel submit keys).
const RESIDUAL_KEYS = ["material_open_questions", "major_entities_missing", "remaining_branches", "routes_unsearched", "unresolved"];

const bad = (): Error => new Error("typesafe_untrusted_state");

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const obj = (v: unknown): Json | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : null);
const objs = (v: unknown): Json[] =>
 Array.isArray(v) ? v.filter((e): e is Json => !!e && typeof e === "object" && !Array.isArray(e)) : [];

// Strict string list: mistyped kernel fields block instead of silently shrinking hashes.
function strList(v: unknown): string[] {
 if (!Array.isArray(v) || v.some((e) => typeof e !== "string")) throw bad();
 return v as string[];
}

function nonEmptyId(v: unknown): string {
 if (typeof v !== "string" || v.length === 0) throw bad();
 return v;
}

function routed(deps: StateLoaderDeps): { bridge: BridgeCall; dataRoot?: string; asOf?: string } {
 const out: { bridge: BridgeCall; dataRoot?: string; asOf?: string } = { bridge: deps.bridge ?? getResearchBridge() };
 if (deps.dataRoot) out.dataRoot = deps.dataRoot;
 if (deps.asOf) out.asOf = deps.asOf;
 return out;
}

async function call(bridge: BridgeCall, req: Json): Promise<Json> {
 let res: Json;
 try {
  res = await bridge(req);
 } catch {
  throw bad();
 }
 if (!res || typeof res !== "object" || typeof res.error === "string") throw bad();
 return res;
}

async function inspectSession(sessionId: string, deps: StateLoaderDeps): Promise<Json> {
 const { bridge, dataRoot, asOf } = routed(deps);
 const req: Json = { op: "research.session.inspect", session_id: sessionId };
 if (dataRoot) req.data_root = dataRoot;
 if (asOf) req.as_of = asOf;
 const res = await call(bridge, req);
 const session = obj((obj(res.result) ?? {}).session);
 if (!session || session.session_id !== sessionId) throw bad();
 return session;
}

async function readRecord(sessionId: string, kind: string, resourceId: string, deps: StateLoaderDeps, freezeId?: string): Promise<Json> {
 const { bridge, dataRoot, asOf } = routed(deps);
 const args: Json = { session_id: sessionId, kind, resource_id: resourceId };
 if (freezeId) args.freeze_id = freezeId;
 const req: Json = { op: "tool.invoke", name: "research_read", arguments: args };
 if (dataRoot) req.data_root = dataRoot;
 if (asOf) req.as_of = asOf;
 const res = await call(bridge, req);
 const result = obj(res.result);
 if (!result || typeof result.error === "string") throw bad();
 const record = obj(result.record);
 if (!record) throw bad();
 return record;
}

function requireId(record: Json, sessionId: string, idKey: string, id: string): void {
 if (record.session_id !== sessionId || record[idKey] !== id) throw bad();
}

function objectiveOf(session: Json): string {
 const o = str(session.objective);
 if (!o) throw bad();
 return o;
}

// ponytail: sequential kernel reads; waves are small, parallelism buys nothing here.
async function loadFrozenEvidence(sessionId: string, freezeId: string, ids: string[], deps: StateLoaderDeps): Promise<Json[]> {
 const out: Json[] = [];
 for (const id of ids) {
  // Freeze-scoped read: the kernel rejects evidence outside the freeze; ids are still verified below.
  const rec = await readRecord(sessionId, "evidence", id, deps, freezeId);
  requireId(rec, sessionId, "evidence_id", id);
  out.push(rec);
 }
 return out;
}

async function loadFreezeIds(sessionId: string, freezeId: string, deps: StateLoaderDeps): Promise<string[]> {
 const freeze = await readRecord(sessionId, "freeze", freezeId, deps);
 requireId(freeze, sessionId, "freeze_id", freezeId);
 return strList(freeze.evidence_ids);
}

export async function requireFreeze(sessionId: string, freezeId: string, deps: StateLoaderDeps = {}): Promise<void> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 await inspectSession(sessionId, deps);
 const rec = await readRecord(sessionId, "freeze", freezeId, deps);
 requireId(rec, sessionId, "freeze_id", freezeId);
}

function dossierFindings(d: Json): { text: string; evidence_ids: string[] }[] {
 const raw = d.findings;
 if (!Array.isArray(raw)) throw bad();
 return raw.map((f) => {
  const row = obj(f);
  const text = row ? str(row.text) : "";
  if (!text.trim() || !row) throw bad();
  return { text, evidence_ids: strList(row.evidence_ids) };
 });
}

async function loadSessionDossiers(sessionId: string, session: Json, deps: StateLoaderDeps): Promise<Json[]> {
 const out: Json[] = [];
 for (const id of strList(session.dossier_ids ?? [])) {
  const rec = await readRecord(sessionId, "dossier", id, deps);
  requireId(rec, sessionId, "dossier_id", id);
  out.push(rec);
 }
 return out;
}

// Actionable derives from trusted remaining routes only (dossier residuals +
// session unresolved questions), never from model args.
function actionableFrom(coverage: unknown, session: Json): boolean {
 const cov = obj(coverage);
 if (cov) {
  for (const k of RESIDUAL_KEYS) {
   const v = cov[k];
   if (Array.isArray(v) && v.some((e) => typeof e === "string" && e.trim())) return true;
  }
 }
 const uq = session.unresolved_questions;
 return Array.isArray(uq) && uq.some((e) => typeof e === "string" && e.trim());
}

export async function loadEvidenceState(
 sessionId: string,
 freezeId: string,
 evidenceId: string,
 deps: StateLoaderDeps = {},
): Promise<EvidenceState> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 nonEmptyId(evidenceId);
 const session = await inspectSession(sessionId, deps);
 const rec = await readRecord(sessionId, "evidence", evidenceId, deps, freezeId);
 requireId(rec, sessionId, "evidence_id", evidenceId);
 const claim = str(rec.claim_text);
 if (!claim) throw bad();
 return { objective: objectiveOf(session), claim, evidence: rec };
}

export async function loadClaimState(
 sessionId: string,
 freezeId: string,
 claimId: string,
 deps: StateLoaderDeps = {},
): Promise<ClaimState> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 nonEmptyId(claimId);
 const session = await inspectSession(sessionId, deps);
 const freezeIds = await loadFreezeIds(sessionId, freezeId, deps);
 const evidence = await loadFrozenEvidence(sessionId, freezeId, freezeIds, deps);
 // claim_id is the claim text itself: it must exactly match kernel-grounded
 // text (freeze evidence claim_text or dossier finding text), else throw.
 let matched = evidence.some((r) => r.claim_text === claimId);
 let latest: Json | null = null;
 if (!matched) {
  const dossiers = await loadSessionDossiers(sessionId, session, deps);
  latest = dossiers.length > 0 ? dossiers[dossiers.length - 1] : null;
  matched = dossiers.some((d) => dossierFindings(d).some((f) => f.text === claimId));
 } else {
  const dossiers = await loadSessionDossiers(sessionId, session, deps);
  latest = dossiers.length > 0 ? dossiers[dossiers.length - 1] : null;
 }
 if (!matched) throw bad();
 // The evidence set is the whole frozen set: selective subsets could cherry-pick.
 return { objective: objectiveOf(session), claim: claimId, evidence_set: evidence, actionable: actionableFrom(latest ? obj(latest.coverage) : null, session) };
}

export function coverageHash(branchMap: unknown, coverage: unknown): string {
 return hashAction({ branch_map: branchMap, coverage });
}

export async function loadCoverageForFreeze(
 sessionId: string,
 freezeId: string,
 deps: StateLoaderDeps = {},
): Promise<CoverageState & { dossierId: string }> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 const session = await inspectSession(sessionId, deps);
 const freeze = await readRecord(sessionId, "freeze", freezeId, deps);
 requireId(freeze, sessionId, "freeze_id", freezeId);
 const wave = freeze.wave_id;
 if (typeof wave !== "number" || !Number.isInteger(wave)) throw bad();
 const dossiers = await loadSessionDossiers(sessionId, session, deps);
 const match = [...dossiers].reverse().find((d) => d.wave_id === wave) ?? null;
 if (!match) throw bad();
 const dossierId = str(match.dossier_id);
 if (!dossierId) throw bad();
 const coverage = obj(match.coverage);
 if (!coverage) throw bad();
 const findings = dossierFindings(match);
 const branch_map: Json = {
  dossier_id: dossierId,
  covered_branches: strList(coverage.covered_branches ?? []),
  relationships: objs(match.relationships ?? []),
  findings,
 };
 return { objective: objectiveOf(session), branch_map, coverage, claim_states: findings, actionable: actionableFrom(coverage, session), dossierId };
}

export async function loadContinuationState(sessionId: string, deps: StateLoaderDeps = {}): Promise<ContinuationState> {
 nonEmptyId(sessionId);
 const session = await inspectSession(sessionId, deps);
 const dossiers = await loadSessionDossiers(sessionId, session, deps);
 const latest = dossiers.length > 0 ? dossiers[dossiers.length - 1] : null;
 if (!latest) throw bad();
 const coverage = obj(latest.coverage);
 if (!coverage) throw bad();
 const open_questions = [...new Set([...strList(latest.open_questions ?? []), ...strList(session.unresolved_questions ?? [])])];
 return { objective: objectiveOf(session), coverage, open_questions, current_conclusions: dossierFindings(latest) };
}

export async function loadCandidateGap(sessionId: string, gapId: string, deps: StateLoaderDeps = {}): Promise<CandidateGap> {
 nonEmptyId(sessionId);
 nonEmptyId(gapId);
 const session = await inspectSession(sessionId, deps);
 // gap_id is either a coverage artifact_id (direct read) or exact gap text
 // (dossier open_question / session unresolved_question); else throw.
 try {
  const rec = await readRecord(sessionId, "coverage", gapId, deps);
  requireId(rec, sessionId, "artifact_id", gapId);
  const gap = str(rec.claim_text);
  if (!gap) throw bad();
  return { objective: objectiveOf(session), gap };
 } catch {
  const dossiers = await loadSessionDossiers(sessionId, session, deps);
  const known = [...dossiers.flatMap((d) => strList(d.open_questions ?? [])), ...strList(session.unresolved_questions ?? [])];
  if (!known.includes(gapId)) throw bad();
  return { objective: objectiveOf(session), gap: gapId };
 }
}

export async function loadRoleOutput(
 sessionId: string,
 freezeId: string,
 roleJobId: string,
 deps: StateLoaderDeps = {},
): Promise<RoleOutputState> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 nonEmptyId(roleJobId);
 const session = await inspectSession(sessionId, deps);
 const job = await readRecord(sessionId, "job", roleJobId, deps);
 requireId(job, sessionId, "job_id", roleJobId);
 const role = str(job.job_type);
 if (role !== "stockbot" && role !== "bullbot" && role !== "bearbot") throw bad();
 const output = obj(job.result);
 if (!output || Object.keys(output).length === 0) throw bad();
 const freeze = await readRecord(sessionId, "freeze", freezeId, deps);
 requireId(freeze, sessionId, "freeze_id", freezeId);
 const freezeEvidenceIds = strList(freeze.evidence_ids);
 // The job must belong to this freeze: matching wave plus committee-run membership.
 if (typeof freeze.wave_id === "number" && typeof job.wave_id === "number" && freeze.wave_id !== job.wave_id) throw bad();
 const entry = objs(session.committee_runs).find((e) => e.freeze_id === freezeId);
 if (!entry || !strList(entry.jobs).includes(roleJobId)) throw bad();
 const evidence = await loadFrozenEvidence(sessionId, freezeId, freezeEvidenceIds, deps);
 // Sorted before hashing so the freeze binding is order-stable.
 const sorted = [...freezeEvidenceIds].sort();
 return { role, objective: objectiveOf(session), evidence, output, freezeId, freezeEvidenceIds: sorted, freezeEvidenceHash: hashAction(sorted) };
}

async function loadTrio(
 sessionId: string,
 freezeId: string,
 session: Json,
 deps: StateLoaderDeps,
): Promise<{ byRole: Record<string, { roleJobId: string; output: Json }>; freezeEvidenceIds: string[] }> {
 const entry = objs(session.committee_runs).find((e) => e.freeze_id === freezeId);
 if (!entry) throw bad();
 const byRole: Record<string, { roleJobId: string; output: Json }> = {};
 for (const jid of strList(entry.jobs)) {
  const job = await readRecord(sessionId, "job", jid, deps);
  requireId(job, sessionId, "job_id", jid);
  const role = str(job.job_type);
  if (role !== "stockbot" && role !== "bullbot" && role !== "bearbot") throw bad();
  if (str(job.status) !== "completed") continue;
  const output = obj(job.result);
  if (!output || Object.keys(output).length === 0) throw bad();
  byRole[role] = { roleJobId: jid, output };
 }
 if (!byRole.stockbot || !byRole.bullbot || !byRole.bearbot) throw bad();
 return { byRole, freezeEvidenceIds: await loadFreezeIds(sessionId, freezeId, deps) };
}

export async function loadCommitteeState(sessionId: string, freezeId: string, deps: StateLoaderDeps = {}): Promise<CommitteeState> {
 nonEmptyId(sessionId);
 nonEmptyId(freezeId);
 const session = await inspectSession(sessionId, deps);
 const { byRole, freezeEvidenceIds } = await loadTrio(sessionId, freezeId, session, deps);
 const evidence = await loadFrozenEvidence(sessionId, freezeId, freezeEvidenceIds, deps);
 const sorted = [...freezeEvidenceIds].sort();
 return {
  objective: objectiveOf(session),
  evidence,
  stockbot: byRole.stockbot.output,
  bullbot: byRole.bullbot.output,
  bearbot: byRole.bearbot.output,
  roleJobIds: { stockbot: byRole.stockbot.roleJobId, bullbot: byRole.bullbot.roleJobId, bearbot: byRole.bearbot.roleJobId },
  freezeHash: hashAction(sorted),
 };
}

export async function loadFinalState(sessionId: string, freezeId: string, deps: StateLoaderDeps = {}): Promise<FinalState> {
 const full = await loadCommitteeState(sessionId, freezeId, deps);
 return {
  objective: full.objective,
  evidence: full.evidence,
  committee: { stockbot: full.stockbot, bullbot: full.bullbot, bearbot: full.bearbot },
 };
}
