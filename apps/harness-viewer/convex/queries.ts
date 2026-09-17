import { PROJECTION } from "./projection";
import type {
  EvalRun,
  EvalScenarioResult,
  Experiment,
  FailureRecord,
  ResearchRun,
} from "./schema";

export function listResearchRuns(): ResearchRun[] {
  return PROJECTION.researchRuns.map((row) => ({
    sessionId: row.sessionId,
    waveId: row.waveId,
    question: row.question,
    status: row.status,
    asOf: row.asOf,
    updatedAt: row.updatedAt,
    traceId: row.traceId,
    conclusion: row.conclusion,
    traceStatus: row.traceStatus,
    provider: row.provider,
    model: row.model,
    jobs: [...row.jobs],
    events: [...row.events],
    claims: [...row.claims],
    evidence: [...row.evidence],
    freezes: [...row.freezes],
    dossiers: [...row.dossiers],
    committeeRuns: [...row.committeeRuns],
  }));
}

export function getResearchRun(sessionId: string): ResearchRun | null {
  const found = PROJECTION.researchRuns.find((row) => row.sessionId === sessionId);
  if (found === undefined) {
    return null;
  }
  return {
    sessionId: found.sessionId,
    waveId: found.waveId,
    question: found.question,
    status: found.status,
    asOf: found.asOf,
    updatedAt: found.updatedAt,
    traceId: found.traceId,
    conclusion: found.conclusion,
    traceStatus: found.traceStatus,
    provider: found.provider,
    model: found.model,
    jobs: [...found.jobs],
    events: [...found.events],
    claims: [...found.claims],
    evidence: [...found.evidence],
    freezes: [...found.freezes],
    dossiers: [...found.dossiers],
    committeeRuns: [...found.committeeRuns],
  };
}

export function listEvalRuns(): EvalRun[] {
  return PROJECTION.evalRuns.map((row) => ({ ...row }));
}

export function listEvalScenarioResults(evalRunId: string): EvalScenarioResult[] {
  return PROJECTION.evalScenarioResults
    .filter((row) => row.evalRunId === evalRunId)
    .map((row) => ({ ...row }));
}

export function listExperiments(): Experiment[] {
  return PROJECTION.experiments.map((row) => ({ ...row }));
}

export function listFailureRecords(evalRunId: string): FailureRecord[] {
  const rows = evalRunId === "" ? PROJECTION.failureRecords : PROJECTION.failureRecords.filter((row) => row.evalRunId === evalRunId);
  return rows.map((row) => ({ ...row }));
}
