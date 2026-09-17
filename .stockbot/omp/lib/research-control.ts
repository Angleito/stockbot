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
// answer). "default" runId stays a single-run/test wildcard.
export function consumeMatchingAuth(store: Map<string, Authorization>, runId: string, kind: AuthKind, actionHash?: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 if (!kind || !KINDS[kind]) throw new Error("research-control: invalid kind");
 if (actionHash !== undefined && (typeof actionHash !== "string" || actionHash.length === 0)) throw new Error("research-control: invalid actionHash");
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (!(auth.runId === runId || auth.runId === "default")) continue;
  if (auth.kind !== kind) continue;
  if (actionHash !== undefined && auth.actionHash !== actionHash) continue;
  if (consumeAuthorization(store, auth.id, { runId: auth.runId, kind, ...(actionHash !== undefined ? { actionHash } : {}) })) return true;
 }
 return false;
}
// Canonical finalize binding: the answer prose the final gate reviewed must
// equal the answer the model publishes. Both sides hash {answer} only;
// session/claims stay kernel-validated (freeze ids, claims_required).
export function finalizeActionHash(answer: unknown): string {
 return hashAction({ answer });
}

// Trio acceptance at finalize (§§15-17 order): role reviews grade AFTER the
// trio runs, so the gate cannot demand ACCEPTs before launch. Finalize needs
// one open accept_role_output per role; the finalize auth itself stays
// hash-bound to the reviewed answer. A successful finalize consumes the three
// role ACCEPTs (one-time); a new committee launch drains stales (see index).
export function hasOpenRoleAccepts(store: Map<string, Authorization>, runId: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 const roles = new Set<string>();
 for (const auth of store.values()) {
  if (!auth || typeof auth !== "object" || auth.consumed) continue;
  if (!(auth.runId === runId || auth.runId === "default")) continue;
  if (auth.kind !== "accept_role_output") continue;
  try {
   const role = (JSON.parse(auth.actionHash) as { role?: unknown }).role;
   if (role === "stockbot" || role === "bullbot" || role === "bearbot") roles.add(role);
  } catch {
   continue;
  }
 }
 return roles.size >= 3;
}
// One-time trio acceptance: consume one open ACCEPT per role for the run.
// All-three-or-none: a partial set burns nothing (fail-closed, retryable).
export function consumeOpenRoleAccepts(store: Map<string, Authorization>, runId: string): boolean {
 if (!(store instanceof Map)) throw new Error("research-control: invalid store");
 if (typeof runId !== "string" || runId.length === 0) throw new Error("research-control: invalid runId");
 const ids: string[] = [];
 for (const role of ["stockbot", "bullbot", "bearbot"]) {
  let found: string | null = null;
  for (const auth of store.values()) {
   if (!auth || typeof auth !== "object" || auth.consumed) continue;
   if (!(auth.runId === runId || auth.runId === "default")) continue;
   if (auth.kind !== "accept_role_output") continue;
   let r: unknown = null;
   try {
    r = (JSON.parse(auth.actionHash) as { role?: unknown }).role;
   } catch {
    continue;
   }
   if (r === role) { found = auth.id; break; }
  }
  if (!found) return false;
  ids.push(found);
 }
 for (const id of ids) {
  const auth = store.get(id);
  if (!auth || auth.consumed) return false;
  if (!consumeAuthorization(store, id, { runId: auth.runId, kind: "accept_role_output" })) return false;
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
  if (!(auth.runId === runId || auth.runId === "default")) continue;
  if (auth.kind !== "accept_role_output") continue;
  auth.consumed = true;
 }
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
