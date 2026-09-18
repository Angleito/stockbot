/** One-time research authorizations + stable action hashing.
 *
 * OMP owns runtime; this module owns the auth ledger only. No network, no
 * TypeSafe imports, no bridge/Python. Helpers throw on invalid input so the
 * caller blocks fail-closed; consume returns false for unknown or already
 * consumed ids (also a block, without throwing).
 */

export type AuthKind =
 | "continue_research"
 | "launch_committee"
 | "accept_role_output"
 | "committee_accepted"
 | "finalize";

export interface Authorization {
 id: string;
 runId: string;
 kind: AuthKind;
 actionHash: string;
 createdAt: number;
 consumed: boolean;
}

export const authorizationStore = new Map<string, Authorization>();

const KINDS: Record<AuthKind, true> = {
 continue_research: true,
 launch_committee: true,
 accept_role_output: true,
 committee_accepted: true,
 finalize: true,
};

function newId(): string {
 const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
 if (c && typeof c.randomUUID === "function") return c.randomUUID();
 return `${Date.now().toString(36)}-${Math.floor(Math.random() * 0xffffffff).toString(36)}`;
}

export function issueAuthorization(runId: string, kind: AuthKind, actionHash: string): Authorization {
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!kind || !KINDS[kind]) throw new Error("research-control: invalid kind");
 if (typeof actionHash !== "string" || actionHash.length === 0) throw new Error("research-control: invalid actionHash");
 const auth: Authorization = {
  id: newId(),
  runId,
  kind,
  actionHash,
  createdAt: Date.now(),
  consumed: false,
 };
 authorizationStore.set(auth.id, auth);
 return auth;
}

export function consumeAuthorization(store: Map<string, Authorization>, id: string, expected?: { runId?: string; kind?: AuthKind; actionHash?: string }): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof id !== "string" || id.length === 0) throw new Error("research-control: invalid id");
 const auth = store.get(id);
 if (!auth || typeof auth !== "object") return false;
 if (auth.consumed) return false;
 if (expected?.runId !== undefined && auth.runId !== expected.runId) return false;
 if (expected?.kind !== undefined && auth.kind !== expected.kind) return false;
 if (expected?.actionHash !== undefined && auth.actionHash !== expected.actionHash) return false;
 auth.consumed = true;
 return true;
}
// Binding-aware consume: kind-only when actionHash is undefined (waves/
// committee: future task batch unknown at issuance, stored hash is audit),
// hash-bound when provided (finalize: reviewed answer must equal intercepted
// answer). Exact runId match only; no wildcard.
export function consumeMatchingAuth(store: Map<string, Authorization>, runId: string, kind: AuthKind, actionHash?: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!kind || !KINDS[kind]) throw new Error("research-control: invalid kind");
 if (actionHash !== undefined && (typeof actionHash !== "string" || actionHash.length === 0)) throw new Error("research-control: invalid actionHash");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (auth.kind !== kind) continue;
  if (actionHash !== undefined && auth.actionHash !== actionHash) continue;
  if (consumeAuthorization(store, auth.id, { runId: auth.runId, kind, ...(actionHash !== undefined ? { actionHash } : {}) })) return true;
 }
 return false;
}
// Open continue_research for this run (kind-only: the candidate-generation
// round precedes any bound candidate; per-item binding consumes at the gate).
export function hasOpenAuth(store: Map<string, Authorization>, runId: string, kind: AuthKind): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!kind || !KINDS[kind]) throw new Error("research-control: invalid kind");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (auth.kind !== kind) continue;
  return true;
 }
 return false;
}
// Launch binding: session + freeze + dossier + coverage all bound at issuance;
// the gate enforces session/freeze by parse-match, dossier/coverageHash ride
// audit-bound in the stored hash.
export function launchCommitteeHash(fp: { sessionId: string; freezeId: string; dossierId: string; coverageHash: string }): string {
 if (!fp || typeof fp !== "object") throw new Error("research-control: invalid launch binding");
 for (const k of ["sessionId", "freezeId", "dossierId", "coverageHash"] as const) {
  if (typeof fp[k] !== "string" || fp[k].length === 0) throw new Error("research-control: invalid launch binding");
 }
 return hashAction({ sessionId: fp.sessionId, freezeId: fp.freezeId, dossierId: fp.dossierId, coverageHash: fp.coverageHash });
}
export function consumeLaunchForFreeze(store: Map<string, Authorization>, runId: string, sessionId: string, freezeId: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (typeof sessionId !== "string" || sessionId.length === 0) throw new Error("research-control: invalid sessionId");
 if (typeof freezeId !== "string" || freezeId.length === 0) throw new Error("research-control: invalid freezeId");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId || auth.kind !== "launch_committee") continue;
  let b: unknown;
  try { b = JSON.parse(auth.actionHash); } catch { continue; }
  if (!b || typeof b !== "object") continue;
  const row = b as Record<string, unknown>;
  if (row.sessionId !== sessionId || row.freezeId !== freezeId) continue;
  return consumeAuthorization(store, auth.id, { runId: auth.runId, kind: "launch_committee", actionHash: auth.actionHash });
 }
 return false;
}
// Candidate+task binding: the sec-agent item must echo both verbatim.
export function candidateTaskHash(candidate: unknown, task: unknown): string {
 return hashAction({ candidate, task });
}
export function consumeCandidateBatch(store: Map<string, Authorization>, runId: string, hashes: string[]): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!Array.isArray(hashes) || hashes.length === 0) throw new Error("research-control: invalid hashes");
 for (const h of hashes) { if (typeof h !== "string" || h.length === 0) throw new Error("research-control: invalid hashes"); }
 const picked: string[] = [];
 const pickedIds = new Set<string>();
 for (const h of hashes) {
  let found: string | null = null;
  for (const auth of store.values()) {
   if (!auth || typeof auth !== "object" || auth.consumed) continue;
   if (auth.runId !== runId || auth.kind !== "continue_research") continue;
   if (auth.actionHash !== h) continue;
   if (pickedIds.has(auth.id)) continue;
   found = auth.id; break;
  }
  if (!found) return false;
  picked.push(found); pickedIds.add(found);
 }
 for (const id of picked) {
  const auth = store.get(id);
  if (!auth || auth.consumed) return false;
  if (!consumeAuthorization(store, id, { runId: auth.runId, kind: "continue_research", actionHash: auth.actionHash })) return false;
 }
 return true;
}
// Canonical finalize binding: answer plus exact committee under review.
export function finalizeActionHash(answer: unknown, ctx: { sessionId: string; freezeId: string; stockbotHash: string; bullbotHash: string; bearbotHash: string }): string {
 if (typeof answer !== "string" || answer.length === 0) throw new Error("research-control: invalid finalize binding");
 if (!ctx || typeof ctx !== "object") throw new Error("research-control: invalid finalize binding");
 for (const k of ["sessionId", "freezeId", "stockbotHash", "bullbotHash", "bearbotHash"] as const) {
  if (typeof ctx[k] !== "string" || ctx[k].length === 0) throw new Error("research-control: invalid finalize binding");
 }
 return hashAction({ answer, sessionId: ctx.sessionId, freezeId: ctx.freezeId, stockbotHash: ctx.stockbotHash, bullbotHash: ctx.bullbotHash, bearbotHash: ctx.bearbotHash });
}

// Bound trio acceptance (§§15-17 order): role reviews grade AFTER the trio
// runs, so the gate cannot demand ACCEPTs before launch. Each ACCEPT binds one
// recorded output: hashAction({runId, freezeId, role, roleJobId, outputHash,
// freezeHash}). Finalize needs one open ACCEPT per role matching the actual
// committee outputs for that freeze; the finalize auth itself stays hash-bound
// to the reviewed answer. A successful finalize consumes the three role
// ACCEPTs (one-time); a new committee launch drains stales (see index).
export interface RoleAcceptBinding {
 runId: string;
 freezeId: string;
 role: string;
 roleJobId: string;
 outputHash: string;
 freezeHash: string;
}
export type RoleAcceptExpected = Record<"stockbot" | "bullbot" | "bearbot", { roleJobId: string; outputHash: string; freezeHash: string }>;
export function issueRoleAccept(runId: string, binding: RoleAcceptBinding): Authorization {
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!binding || typeof binding !== "object") throw new Error("research-control: invalid role binding");
 for (const k of ["runId", "freezeId", "role", "roleJobId", "outputHash", "freezeHash"] as const) {
  if (typeof binding[k] !== "string" || binding[k].length === 0) throw new Error("research-control: invalid role binding");
 }
 if (binding.role !== "stockbot" && binding.role !== "bullbot" && binding.role !== "bearbot") throw new Error("research-control: invalid role binding");
 if (binding.runId !== runId) throw new Error("research-control: invalid role binding");
 return issueAuthorization(runId, "accept_role_output", hashAction({ runId: binding.runId, freezeId: binding.freezeId, role: binding.role, roleJobId: binding.roleJobId, outputHash: binding.outputHash, freezeHash: binding.freezeHash }));
}
function roleBindingMatches(auth: Authorization, runId: string, freezeId: string, role: string, exp?: { roleJobId: string; outputHash: string; freezeHash: string }): boolean {
 if (auth.runId !== runId || auth.kind !== "accept_role_output") return false;
 let b: unknown;
 try {
  b = JSON.parse(auth.actionHash);
 } catch {
  return false;
 }
 if (!b || typeof b !== "object") return false;
 const row = b as Record<string, unknown>;
 if (row.runId !== runId || row.freezeId !== freezeId || row.role !== role) return false;
 if (exp && (row.roleJobId !== exp.roleJobId || row.outputHash !== exp.outputHash || row.freezeHash !== exp.freezeHash)) return false;
 return true;
}
export function hasOpenRoleAccepts(store: Map<string, Authorization>, runId: string, freezeId: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (typeof freezeId !== "string" || freezeId.length === 0) throw new Error("research-control: invalid freezeId");
 for (const role of ["stockbot", "bullbot", "bearbot"]) {
  let found = false;
  for (const auth of store.values()) {
   if (!auth || typeof auth !== "object" || auth.consumed) continue;
   if (roleBindingMatches(auth, runId, freezeId, role)) { found = true; break; }
  }
  if (!found) return false;
 }
 return true;
}
// One-time trio acceptance: consume one open ACCEPT per role matching the
// actual outputs for the freeze. All-three-or-none: a partial set burns
// nothing (fail-closed, retryable).
export function consumeOpenRoleAccepts(store: Map<string, Authorization>, runId: string, freezeId: string, expected: RoleAcceptExpected): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (typeof freezeId !== "string" || freezeId.length === 0) throw new Error("research-control: invalid freezeId");
 if (!expected || typeof expected !== "object") throw new Error("research-control: invalid expected");
 const ids: string[] = [];
 for (const role of ["stockbot", "bullbot", "bearbot"] as const) {
  const exp = expected[role];
  if (!exp || typeof exp.roleJobId !== "string" || typeof exp.outputHash !== "string" || typeof exp.freezeHash !== "string") throw new Error("research-control: invalid expected");
  let found: string | null = null;
  for (const auth of store.values()) {
   if (!auth || typeof auth !== "object" || auth.consumed) continue;
   if (roleBindingMatches(auth, runId, freezeId, role, exp)) { found = auth.id; break; }
  }
  if (!found) return false;
  ids.push(found);
 }
 for (const id of ids) {
  const auth = store.get(id);
  if (!auth || auth.consumed) return false;
  if (!consumeAuthorization(store, id, { runId: auth.runId, kind: "accept_role_output", actionHash: auth.actionHash })) return false;
 }
 return true;
}
// New trio run invalidates prior ACCEPTs: stale grades must never authorize a
// later finalize (§§15-17 every-output-reviewed). Best-effort drain; a missing
// entry mid-loop fails closed upstream, never throws here.
export function drainRoleAccepts(store: Map<string, Authorization>, runId: string): void {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (auth.kind !== "accept_role_output") continue;
  auth.consumed = true;
 }
}
// Stale committee grades never authorize a later freeze: drained next to
// drainRoleAccepts at allowed committee launch (see index).
export function drainCommitteeAccepts(store: Map<string, Authorization>, runId: string): void {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (auth.kind !== "committee_accepted") continue;
  auth.consumed = true;
 }
}
export function drainFinalize(store: Map<string, Authorization>, runId: string): void {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (auth.kind !== "finalize") continue;
  auth.consumed = true;
 }
}
// All-or-none finalize: one committee_accepted + one finalize + one ACCEPT per
// role, all matching this freeze/committee/answer. Miss burns nothing.
export function consumeFinalizeBundle(store: Map<string, Authorization>, args: { runId: string; freezeId: string; expected: RoleAcceptExpected; committeeHash: string; finalizeHash: string }): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (!args || typeof args !== "object") throw new Error("research-control: invalid finalize bundle");
 const { runId, freezeId, expected, committeeHash, finalizeHash } = args;
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid finalize bundle");
 if (typeof freezeId !== "string" || freezeId.length === 0) throw new Error("research-control: invalid finalize bundle");
 if (typeof committeeHash !== "string" || committeeHash.length === 0) throw new Error("research-control: invalid finalize bundle");
 if (typeof finalizeHash !== "string" || finalizeHash.length === 0) throw new Error("research-control: invalid finalize bundle");
 if (!expected || typeof expected !== "object") throw new Error("research-control: invalid finalize bundle");
 let committeeId: string | null = null;
 let finalizeId: string | null = null;
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (auth.runId !== runId) continue;
  if (!committeeId && auth.kind === "committee_accepted" && auth.actionHash === committeeHash) committeeId = auth.id;
  if (!finalizeId && auth.kind === "finalize" && auth.actionHash === finalizeHash) finalizeId = auth.id;
  if (committeeId && finalizeId) break;
 }
 if (!committeeId || !finalizeId) return false;
 const roleIds: string[] = [];
 for (const role of ["stockbot", "bullbot", "bearbot"] as const) {
  const exp = expected[role];
  if (!exp || typeof exp.roleJobId !== "string" || typeof exp.outputHash !== "string" || typeof exp.freezeHash !== "string") throw new Error("research-control: invalid finalize bundle");
  let found: string | null = null;
  for (const auth of store.values()) {
   if (!auth || typeof auth !== "object" || auth.consumed) continue;
   if (roleBindingMatches(auth, runId, freezeId, role, exp)) { found = auth.id; break; }
  }
  if (!found) return false;
  roleIds.push(found);
 }
 const ids = [committeeId, finalizeId, ...roleIds];
 for (const id of ids) {
  const auth = store.get(id);
  if (!auth || auth.consumed) return false;
 }
 for (const id of ids) {
  const auth = store.get(id)!;
  if (!consumeAuthorization(store, id, { runId: auth.runId, kind: auth.kind, actionHash: auth.actionHash })) return false;
 }
 return true;
}

// Stable stringify with sorted keys and no new dependency. Only plain
// objects are re-keyed; arrays keep order and Dates/class instances pass
// through so JSON semantics (toJSON, undefined/Infinity handling) stay intact.
function sortValue(value: unknown, stack: Set<object>): unknown {
 if (value === null) return value;
 const t = typeof value;
 if (t === "string" || t === "number" || t === "boolean") return value;
 if (t !== "object") return value;
 const obj = value as Record<string, unknown>;
 if (stack.has(obj)) throw new Error("research-control: circular value");
 if (Array.isArray(obj)) {
  stack.add(obj);
  try {
   return obj.map((e) => sortValue(e, stack));
  } finally {
   stack.delete(obj);
  }
 }
 const proto = Object.getPrototypeOf(obj);
 if (proto !== Object.prototype && proto !== null) return value;
 stack.add(obj);
 try {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(obj).sort()) out[k] = sortValue(obj[k], stack);
  return out;
 } finally {
  stack.delete(obj);
 }
}

export function hashAction(value: unknown): string {
 const sorted = sortValue(value, new Set());
 const s = JSON.stringify(sorted);
 if (typeof s !== "string") throw new Error("research-control: unhashable value");
 return s;
}

export function isExactRepeat(seen: Set<string>, hash: string): boolean {
 if (!(seen instanceof Set)) throw new Error("research-control: invalid seen set");
 if (typeof hash !== "string" || hash.length === 0) throw new Error("research-control: invalid hash");
 return seen.has(hash);
}
