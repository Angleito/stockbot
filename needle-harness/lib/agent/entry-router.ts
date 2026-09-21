// lib/agent/entry-router.ts — pure trivial-prompt gate, no JEV imports.
// JEV routing lives in the persistent kernel worker (op:route); this module
// only short-circuits obvious chitchat without any worker round trip.
// ponytail: regex only; per-domain adapters land when OTHER tools need them.
const TRIVIAL_PATTERNS = [
  /^(hi|hello|hey|yo|sup|howdy|hiya|greetings)( there)?$/,
  /^good (morning|afternoon|evening|day)$/,
  /^(thanks|thank you|thx|many thanks)( (very much|so much|a lot))?$/,
  /^(ok|okay|cool|great|nice|got it)$/,
  /^(bye|goodbye|good night|see you)$/,
];

// Question words / research nouns: never trivial, even inside a short prompt.
const RESEARCH_SIGNAL_RE =
  /\b(what|who|when|where|why|how|which|whose|whom|explain|analy[sz]e|compare|summari[sz]e|search|find|look ?up|fetch|filing|sec|stock|price|market|report|ticker|url|http)\b/;

export function isTrivialPrompt(prompt: string): boolean {
  const trimmed = prompt.trim();
  if (!trimmed || trimmed.length > 60) return false;
  const n = trimmed
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!n || RESEARCH_SIGNAL_RE.test(n)) return false;
  return TRIVIAL_PATTERNS.some((re) => re.test(n));
}
