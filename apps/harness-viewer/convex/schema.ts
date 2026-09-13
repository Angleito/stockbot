// ponytail: projection types only. The authoritative ledger stays in SQLite
// (data/research.sqlite, data/eval_runs.sqlite); these tables are read-only
// projections. Upgrade path: add the convex dep + defineSchema with these
// table names when a live backend lands.
export interface ResearchRun {
  sessionId: string;
  waveId: number;
  question: string;
  status: string;
  asOf: string | null;
  updatedAt: string;
}

export interface EvalRun {
  evalRunId: string;
  model: string;
  provider: string;
  harnessVersion: string;
  promptVersion: string;
  gitSha: string;
  scenarioVersion: string;
  startedAt: string;
  passed: number;
  failed: number;
}

export interface EvalScenarioResult {
  evalRunId: string;
  scenarioName: string;
  passed: boolean;
  violations: string[];
}

export interface Experiment {
  experimentId: string;
  beforeRunId: string;
  afterRunId: string;
  deltaPassed: number;
}

export interface FailureRecord {
  failureId: string;
  evalRunId: string;
  scenarioName: string;
  violation: string;
}

export const PROJECTION_TABLES = [
  "researchRuns",
  "evalRuns",
  "evalScenarioResults",
  "experiments",
  "failureRecords",
] as const;
