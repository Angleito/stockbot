/** Deterministic research branch state (Decision-tree-B).
 *
 * OMP owns runtime; this module owns branch bookkeeping only. Pure and
 * total where it matters: branchStatus/researchComplete never throw, so the
 * director can call them on any inspect-derived snapshot. Mutators throw on
 * invalid input so the caller blocks fail-closed.
 */

export interface Branch {
 id: string;
 question: string;
 material: boolean;
}

export type BranchStatus = "OPEN_ACTIONABLE" | "CLOSED" | "CLOSED_UNKNOWN";

// Mirrors the resolveClaim verdict union (duplicated to avoid coupling to
// lib/typesafe/*, which this module never imports).
export type ClaimStatus =
 | "SUPPORTED"
 | "CONTRADICTED"
 | "MIXED"
 | "UNKNOWN_ACTIONABLE"
 | "UNKNOWN_EXHAUSTED";

export interface ResearchState {
 branches: Branch[];
 investigated: Record<string, boolean>;
 claims: Record<string, ClaimStatus>;
}

export function createResearchState(branches: Branch[] = []): ResearchState {
 if (!Array.isArray(branches)) throw new Error("research-state: invalid branches");
 for (const b of branches) assertBranch(b);
 return { branches: [...branches], investigated: {}, claims: {} };
}

export function addBranch(state: ResearchState, branch: Branch): void {
 assertState(state);
 assertBranch(branch);
 const i = state.branches.findIndex((b) => b.id === branch.id);
 if (i >= 0) state.branches[i] = branch;
 else state.branches.push(branch);
}

export function setBranchResult(
 state: ResearchState,
 id: string,
 investigated: boolean,
 claimStatus: ClaimStatus,
): void {
 assertState(state);
 if (typeof id !== "string" || id.length === 0) throw new Error("research-state: invalid id");
 if (investigated !== true && investigated !== false) throw new Error("research-state: invalid investigated");
 assertClaimStatus(claimStatus);
 state.investigated[id] = investigated;
 state.claims[id] = claimStatus;
}

// Decision-tree-B: uninvestigated material -> OPEN_ACTIONABLE; investigated
// with no unresolved claim -> CLOSED; investigated with unresolved but
// improvable claim -> OPEN_ACTIONABLE; else CLOSED_UNKNOWN. Immaterial
// branches are always CLOSED. Unknown material fails closed to OPEN_ACTIONABLE.
export function branchStatus(
 branch: Branch | null | undefined,
 investigated: unknown,
 claimStatus: unknown,
): BranchStatus {
 if (!branch || typeof branch !== "object") return "OPEN_ACTIONABLE";
 if (branch.material === false) return "CLOSED";
 if (branch.material !== true) return "OPEN_ACTIONABLE";
 if (investigated !== true) return "OPEN_ACTIONABLE";
 if (claimStatus === "SUPPORTED" || claimStatus === "CONTRADICTED" || claimStatus === "MIXED") return "CLOSED";
 if (claimStatus === "UNKNOWN_ACTIONABLE") return "OPEN_ACTIONABLE";
 return "CLOSED_UNKNOWN";
}

// True when no material branch is OPEN_ACTIONABLE. Invalid state fails
// closed to false (never report complete on a bad snapshot).
export function researchComplete(state: ResearchState | null | undefined): boolean {
 if (!state || typeof state !== "object" || !Array.isArray(state.branches)) return false;
 const investigated = state.investigated && typeof state.investigated === "object" ? state.investigated : {};
 const claims = state.claims && typeof state.claims === "object" ? state.claims : {};
 for (const b of state.branches) {
  if (branchStatus(b, (investigated as Record<string, unknown>)[(b as Branch).id], (claims as Record<string, unknown>)[(b as Branch).id]) === "OPEN_ACTIONABLE") return false;
 }
 return true;
}

function assertBranch(branch: Branch): void {
 if (!branch || typeof branch !== "object") throw new Error("research-state: invalid branch");
 if (typeof branch.id !== "string" || branch.id.length === 0) throw new Error("research-state: invalid branch id");
 if (typeof branch.question !== "string") throw new Error("research-state: invalid branch question");
 if (branch.material !== true && branch.material !== false) throw new Error("research-state: invalid branch material");
}

function assertClaimStatus(claimStatus: ClaimStatus): void {
 if (
  claimStatus !== "SUPPORTED" &&
  claimStatus !== "CONTRADICTED" &&
  claimStatus !== "MIXED" &&
  claimStatus !== "UNKNOWN_ACTIONABLE" &&
  claimStatus !== "UNKNOWN_EXHAUSTED"
 ) throw new Error("research-state: invalid claim status");
}

function assertState(state: ResearchState): void {
 if (!state || typeof state !== "object") throw new Error("research-state: invalid state");
 if (!Array.isArray(state.branches)) throw new Error("research-state: invalid state branches");
 if (!state.investigated || typeof state.investigated !== "object") throw new Error("research-state: invalid state investigated");
 if (!state.claims || typeof state.claims !== "object") throw new Error("research-state: invalid state claims");
}
