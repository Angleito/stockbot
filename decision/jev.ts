// decision/jev.ts — typed JEV decision layer (API-only, no OMP/Pi runtime).
//
// choice = exclusive routing/category/explanation: exactly one winner among
// mutually exclusive labels (use DISPOSITION_OPTIONS / EVIDENCE_STATE_OPTIONS).
// noul = independent propositions: each id gets its own probability, no
// cross-id winner. score = ordinal judgments only, never deterministic calcs.
// Raw vs policy: parsers return raw probabilities/choices/scores unchanged;
// classifyProbability is the separate policy step callers apply to noul
// probabilities (yes/unsure/no), never collapsed inside a parser.
// Lifecycle vs decision: stage/proposal admission (which nodes exist, which
// stage they belong to) is lifecycle; disposition/truth/evidenceState/
// materiality are the semantic decisions JEV owns per node.
import { writeFile } from "node:fs/promises";
import { join } from "node:path";

export type Decision = "yes" | "unsure" | "no";

// Approved policy: >=.70 yes; >=.50 and <.70 unsure; <.50 no.
export function classifyProbability(p: number): Decision {
  if (!Number.isFinite(p) || p < 0 || p > 1) throw new Error("invalid_probability");
  if (p >= 0.7) return "yes";
  if (p >= 0.5) return "unsure";
  return "no";
}

export type NoulDecision = { kind: "noul"; probability: number };
export type ChoiceDecision = {
  kind: "choice";
  choice: string;
  probabilities: Record<string, number>;
  confidence: number;
};
export type ScoreDecision = {
  kind: "score";
  score: number;
  probabilities?: Record<string, number>;
  confidence?: number;
  raw?: unknown;
};
export type DecisionResult = NoulDecision | ChoiceDecision | ScoreDecision;

export const DISPOSITION_OPTIONS: Record<"analyze" | "gather_evidence" | "reject", string> = {
  analyze: "The question materially contributes to resolving the objective and is ready to analyze.",
  gather_evidence: "The question matters, but available state is insufficient to analyze it.",
  reject: "The question does not materially contribute to resolving the user's objective.",
};

export const EVIDENCE_STATE_OPTIONS: Record<
  "sufficient_support" | "sufficient_contradiction" | "conflicted" | "insufficient",
  string
> = {
  sufficient_support: "The cited evidence sufficiently supports the claim.",
  sufficient_contradiction: "The cited evidence sufficiently contradicts the claim.",
  conflicted: "The evidence both supports and contradicts the claim; do not resolve by guessing.",
  insufficient: "The evidence is missing or too weak to support or contradict the claim.",
};
// Ordinal materiality rubric for score questions (never deterministic calcs).
export const MATERIALITY_LEVELS = ["Immaterial", "Low", "Moderate", "High", "Critical"] as const;

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isProb(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1;
}

export function parseNoulAnswer(name: string, id: string, ans: unknown): NoulDecision {
  if (!isObj(ans) || ans.type !== "noul" || typeof ans.noul !== "number")
    throw new Error(`${name}: malformed_typesafe_answer for ${id}`);
  const probability = ans.noul;
  if (!isProb(probability)) throw new Error("invalid_probability");
  return { kind: "noul", probability };
}

export function parseChoiceAnswer(
  name: string,
  id: string,
  ans: unknown,
  options: Record<string, string>,
): ChoiceDecision {
  const bad = (): Error => new Error(`${name}: malformed_typesafe_answer for ${id}`);
  if (!isObj(ans) || ans.type !== "choice") throw bad();
  if (typeof ans.choice !== "string" || !(ans.choice in options)) throw bad();
  if (!isObj(ans.probabilities) || typeof ans.confidence !== "number") throw bad();
  if (!isProb(ans.confidence)) throw bad();
  const want = Object.keys(options).sort();
  const got = Object.keys(ans.probabilities).sort();
  if (got.length !== want.length || got.some((k, i) => k !== want[i])) throw bad();
  const probabilities: Record<string, number> = {};
  for (const k of want) {
    const v = (ans.probabilities as Record<string, unknown>)[k];
    if (!isProb(v)) throw bad();
    probabilities[k] = v;
  }
  return { kind: "choice", choice: ans.choice as string, probabilities, confidence: ans.confidence as number };
}

export function parseScoreAnswer(name: string, id: string, ans: unknown, maxScore?: number): ScoreDecision {
  const bad = (): Error => new Error(`${name}: malformed_typesafe_answer for ${id}`);
  if (!isObj(ans) || ans.type !== "score" || typeof ans.score !== "number") throw bad();
  const s = ans.score as number;
  if (!Number.isFinite(s)) throw bad();
  if (maxScore !== undefined) {
    if (!Number.isFinite(maxScore) || s < 0 || s > maxScore) throw bad();
  }
  const out: ScoreDecision = { kind: "score", score: s };
  if (ans.confidence !== undefined) {
    if (!isProb(ans.confidence)) throw bad();
    out.confidence = ans.confidence as number;
  }
  if (ans.probabilities !== undefined) {
    if (!isObj(ans.probabilities)) throw bad();
    const probs: Record<string, number> = {};
    for (const [k, v] of Object.entries(ans.probabilities)) {
      if (!isProb(v)) throw bad();
      probs[k] = v;
    }
    out.probabilities = probs;
  }
  if (isObj(ans) && "legend" in ans) out.raw = ans.legend;
  return out;
}

export function assertAcyclic(
  stage: string,
  proposals: { id: string; dependsOn: string[] }[],
  prior: (id: string) => string[] | undefined,
): void {
  const edges = new Map(proposals.map((p) => [p.id, p.dependsOn]));
  for (const p of proposals)
    if (p.dependsOn.includes(p.id)) throw new Error(`${stage}: proposal ${p.id} depends on itself`);
  const state = new Map<string, number>();
  const visit = (id: string): void => {
    const s = state.get(id);
    if (s === 2) return;
    if (s === 1) throw new Error(`${stage}: cyclic dependency involving ${id}`);
    state.set(id, 1);
    const deps = edges.has(id) ? (edges.get(id) as string[]) : (prior(id) ?? []);
    for (const d of deps) visit(d);
    state.set(id, 2);
  };
  for (const p of proposals) visit(p.id);
}

export function scopeNodeState(args: {
  objective: unknown;
  proposal: unknown;
  analysis?: unknown;
  evidence: unknown[];
  dependencyDecisions?: unknown;
}): unknown {
  if (!Array.isArray(args.evidence)) throw new Error("scopeNodeState: evidence must be an array");
  const out: Record<string, unknown> = {
    objective: args.objective,
    proposal: args.proposal,
    evidence: args.evidence,
  };
  if (args.analysis !== undefined) out.analysis = args.analysis;
  if (args.dependencyDecisions !== undefined) out.dependencyDecisions = args.dependencyDecisions;
  return out;
}

export type SystemOneFn = (req: { state: unknown; questions: Record<string, unknown> }) => Promise<unknown>;

function scoreMaxFromQuestion(q: unknown): number | undefined {
  if (!isObj(q)) return undefined;
  const criteria = (q as Record<string, unknown>).criteria;
  if (Array.isArray(criteria)) return criteria.length - 1;
  const levels = (q as Record<string, unknown>).levels;
  if (Array.isArray(levels)) return levels.length - 1;
  return undefined;
}
function optionsFromQuestion(q: unknown): Record<string, string> {
  if (isObj(q) && isObj(q.criteria))
    return Object.fromEntries(Object.keys(q.criteria).map((k) => [k, k]));
  return {};
}

// Typed ask: preserves raw SDK output plus the parsed DecisionResult map, and
// writes request/raw/policy files like the existing sequential caller. Policy
// (the decisions map) stays separate from raw; noul yes/unsure/no is derived
// by callers via classifyProbability, never collapsed here.
export async function askDecisions(
  dir: string,
  name: string,
  req: { state: unknown; questions: Record<string, unknown> },
  systemOne: SystemOneFn,
  choiceOptions?: Record<string, Record<string, string>>,
): Promise<{ raw: unknown; decisions: Record<string, DecisionResult> }> {
  await writeFile(join(dir, `request-${name}.json`), JSON.stringify(req, null, 2) + "\n");
  let raw: unknown;
  try {
    raw = await systemOne(req);
  } catch (e) {
    throw new Error(`${name}: typesafe_request_failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  await writeFile(join(dir, `raw-${name}.json`), JSON.stringify(raw, null, 2) + "\n");
  const ids = Object.keys(req.questions).sort();
  if (!isObj(raw) || !isObj(raw.answers)) throw new Error(`${name}: malformed_typesafe_response`);
  const got = Object.keys(raw.answers).sort();
  if (got.length !== ids.length || got.some((k, i) => k !== ids[i]))
    throw new Error(`${name}: typesafe answers do not match questions`);
  const decisions: Record<string, DecisionResult> = {};
  for (const id of ids) {
    const ans = (raw.answers as Record<string, unknown>)[id];
    const q = req.questions[id];
    const qType = isObj(q) ? q.type : undefined;
    const aType = isObj(ans) ? ans.type : undefined;
    const kind = qType === "choice" || qType === "score" ? qType : aType;
    if (kind === "choice") {
      decisions[id] = parseChoiceAnswer(name, id, ans, choiceOptions?.[id] ?? optionsFromQuestion(q));
    } else if (kind === "score") {
      decisions[id] = parseScoreAnswer(name, id, ans, scoreMaxFromQuestion(q));
    } else {
      decisions[id] = parseNoulAnswer(name, id, ans);
    }
  }
  await writeFile(join(dir, `policy-${name}.json`), JSON.stringify(decisions, null, 2) + "\n");
  return { raw, decisions };
}
