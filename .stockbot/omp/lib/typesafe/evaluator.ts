import { noul, type EntryType, type NoulQuestion } from "@typesafe-ai/sdk";
import { getTypeSafeClient } from "./client.ts";
import { yes } from "./thresholds.ts";
import type { JudgmentMap, JudgmentResults, JudgeQuestion, SystemOneEvaluator } from "./types.ts";

export class FakeEvaluator implements SystemOneEvaluator {
 constructor(private readonly judgments: JudgmentMap) { }

 async evaluate(_state: unknown, questions: JudgeQuestion[]): Promise<JudgmentResults> {
  const results: JudgmentMap = {};
  for (const q of questions) {
   const p = this.judgments[q.id]?.p_yes;
   const raw = typeof p === "number" ? p : 0;
   results[q.id] = { p_yes: raw, yes: yes(raw) };
  }
  return { results, model: "fake" };
 }
}

export class TypeSafeEvaluator implements SystemOneEvaluator {
 async evaluate(state: unknown, questions: JudgeQuestion[]): Promise<JudgmentResults> {
  const qs: Record<string, NoulQuestion> = {};
  for (const q of questions) qs[q.id] = noul(q.instruction);
  let res;
  try {
   res = await getTypeSafeClient().systemOne({ state: state as EntryType, questions: qs });
  } catch {
   throw new Error("typesafe_evaluation_failed");
  }
  const results: JudgmentMap = {};
  const answers = res.answers as Record<string, { noul?: unknown }>;
  for (const q of questions) {
   const raw = answers[q.id]?.noul;
   if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0 || raw > 1) throw new Error("typesafe_invalid_response");
   results[q.id] = { p_yes: raw, yes: yes(raw) };
  }
  return { results, model: res.model };
 }
}
