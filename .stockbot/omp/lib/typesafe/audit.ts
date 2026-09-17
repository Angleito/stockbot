import { QUESTION_BANK_VERSION } from "./questions.ts";
import { YES_THRESHOLD } from "./thresholds.ts";
import type { JudgmentMap, Phase } from "./types.ts";

export interface AuditRecord {
 timestamp: string;
 run_id: string;
 phase: Phase;
 question_bank_version: string;
 threshold: number;
 model?: string;
 questions: JudgmentMap;
 result: string;
}

export function buildAudit(args: {
 runId: string;
 phase: Phase;
 questions: JudgmentMap;
 result: string;
 model?: string;
 timestamp?: string;
}): AuditRecord {
 const record: AuditRecord = {
  timestamp: args.timestamp ?? new Date().toISOString(),
  run_id: args.runId,
  phase: args.phase,
  question_bank_version: QUESTION_BANK_VERSION,
  threshold: YES_THRESHOLD,
  questions: args.questions,
  result: args.result,
 };
 if (args.model !== undefined) record.model = args.model;
 return record;
}
