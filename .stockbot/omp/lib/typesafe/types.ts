export type Phase =
 | "evidence"
 | "claim"
 | "coverage"
 | "continuation"
 | "candidate"
 | "role_output"
 | "committee"
 | "final";

export type Kind = "pass_when_yes" | "trigger_when_yes";

export interface JudgeQuestion {
 id: string;
 phase: Phase;
 instruction: string;
 kind: Kind;
 critical: boolean;
}

interface JudgmentEntry {
 p_yes: number;
 yes: boolean;
}

export type JudgmentMap = Record<string, JudgmentEntry>;

export interface JudgmentResults {
 results: JudgmentMap;
 model?: string;
}

export interface SystemOneEvaluator {
 evaluate(state: unknown, questions: JudgeQuestion[]): Promise<JudgmentResults>;
}
