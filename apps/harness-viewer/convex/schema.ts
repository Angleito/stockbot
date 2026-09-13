// ponytail: projection types only. The authoritative ledger stays in SQLite
// (data/research.sqlite, data/eval_runs.sqlite); these tables are read-only
// projections. Upgrade path: add the convex dep + defineSchema with these
// table names when a live backend lands.
export interface TraceJob {
  jobId: string;
  parentJobId: string | null;
  jobType: string;
  owner: string;
  waveId: number;
  status: string;
  failureCategory: string | null;
  failureMessage: string | null;
  assignmentId: string | null;
  role: string | null;
}

export interface TraceEvent {
  seq: number;
  eventType: string;
  payload: Record<string, string | number | boolean | null>;
}

export interface GroundedClaimView {
  text: string;
  evidenceIds: string[];
}

export interface ResearchRun {
  sessionId: string;
  waveId: number;
  question: string;
  status: string;
  asOf: string | null;
  updatedAt: string;
  traceId: string | null;
  conclusion: string | null;
  traceStatus: string | null;
  provider: string | null;
  model: string | null;
  jobs: TraceJob[];
  events: TraceEvent[];
  claims: GroundedClaimView[];
  evidence: { evidenceId: string; subject: string; knownAt: string | null; sourceName: string; sourceUri: string | null }[];
  freezes: { freezeId: string; evidenceIds: string[] }[];
  dossiers: { dossierId: string; findings: GroundedClaimView[] }[];
  committeeRuns: unknown[];
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
