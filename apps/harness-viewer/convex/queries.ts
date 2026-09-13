// ponytail: stub queries over the projection tables; wire a Convex client
// here when the backend lands. Never migrate the authoritative ledger.
import type {
  EvalRun,
  EvalScenarioResult,
  Experiment,
  FailureRecord,
  ResearchRun,
} from "./schema";

export function listResearchRuns(): ResearchRun[] {
  return [];
}

export function getResearchRun(_sessionId: string): ResearchRun | null {
  return null;
}

export function listEvalRuns(): EvalRun[] {
  return [];
}

export function listEvalScenarioResults(_evalRunId: string): EvalScenarioResult[] {
  return [];
}

export function listExperiments(): Experiment[] {
  return [];
}

export function listFailureRecords(_evalRunId: string): FailureRecord[] {
  return [];
}
