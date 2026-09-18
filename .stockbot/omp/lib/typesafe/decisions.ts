import type { JudgmentMap, JudgeQuestion } from "./types.ts";

const isYes = (results: JudgmentMap, id: string): boolean => results[id]?.yes === true;

export function judgeEvidence(results: JudgmentMap): { verdict: "usable" | "unusable" } {
 const usable = isYes(results, "E01") && isYes(results, "E02") && isYes(results, "E14") && isYes(results, "E16");
 return { verdict: usable ? "usable" : "unusable" };
}

export type ClaimResolution = "SUPPORTED" | "CONTRADICTED" | "MIXED" | "UNKNOWN_ACTIONABLE" | "UNKNOWN_EXHAUSTED";

export function resolveClaim(results: JudgmentMap, actionable: boolean): ClaimResolution {
 const supports = isYes(results, "C01");
 const contradicts = isYes(results, "C02");
 if (supports && contradicts) return "MIXED";
 if (contradicts) return "CONTRADICTED";
 if (supports && isYes(results, "C03") && !isYes(results, "C06") && isYes(results, "C13")) return "SUPPORTED";
 return actionable ? "UNKNOWN_ACTIONABLE" : "UNKNOWN_EXHAUSTED";
}

export type CoverageVerdict = "COMPLETE" | "INCOMPLETE_ACTIONABLE" | "INCOMPLETE_NONACTIONABLE";

export function judgeCoverage(results: JudgmentMap, actionable: boolean): CoverageVerdict {
 const complete = Array.from({ length: 20 }, (_, i) => `V${String(i + 1).padStart(2, "0")}`).every((id) => isYes(results, id));
 if (complete) return "COMPLETE";
 return actionable ? "INCOMPLETE_ACTIONABLE" : "INCOMPLETE_NONACTIONABLE";
}

export function judgeContinuation(results: JudgmentMap): { decision: "continue" | "stop" } {
 if (isYes(results, "N01") && isYes(results, "N02") && isYes(results, "N03") && isYes(results, "N04")) return { decision: "continue" };
 return { decision: "stop" };
}

export function authorizeCandidate(results: JudgmentMap): boolean {
 return ["N07", "N08", "N09", "N10", "N12"].every((id) => isYes(results, id));
}

export interface FailedQuestion {
 id: string;
 p_yes: number;
 reason: string;
}

function collectFailures(results: JudgmentMap, questions: JudgeQuestion[]): FailedQuestion[] {
 const failed: FailedQuestion[] = [];
 for (const q of questions) {
  if (!q.critical || isYes(results, q.id)) continue;
  const raw = results[q.id]?.p_yes;
  failed.push({ id: q.id, p_yes: typeof raw === "number" ? raw : 0, reason: q.instruction });
 }
 return failed;
}

export function reviewRoleOutput(
 results: JudgmentMap,
 questions: JudgeQuestion[],
): { verdict: "ACCEPT" | "REJECT_FOR_REVISION"; failed: FailedQuestion[] } {
 const failed = collectFailures(results, questions);
 return failed.length > 0 ? { verdict: "REJECT_FOR_REVISION", failed } : { verdict: "ACCEPT", failed };
}

export function reviewCommittee(
 results: JudgmentMap,
 questions: JudgeQuestion[],
): { verdict: "PASS" | "BLOCK"; failed: FailedQuestion[] } {
 const failed = collectFailures(results, questions);
 return failed.length > 0 ? { verdict: "BLOCK", failed } : { verdict: "PASS", failed };
}

export function reviewFinal(
 results: JudgmentMap,
 questions: JudgeQuestion[],
): { verdict: "PASS" | "BLOCK"; failed: FailedQuestion[] } {
 const failed = collectFailures(results, questions);
 return failed.length > 0 ? { verdict: "BLOCK", failed } : { verdict: "PASS", failed };
}
