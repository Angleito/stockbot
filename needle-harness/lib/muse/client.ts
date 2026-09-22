import type { Evidence } from "../agent/types";

export type MuseUsage = {
  inputTokens?: number;
  outputTokens?: number;
  cachedTokens?: number;
  cost?: number;
};

const MODEL = "muse-spark-1.3-contributor";
const BASE = "https://opencode.ai/zen/go/v1";
// User-provided Muse Spark 1.3 Contributor per-1M-token rates.
const INPUT_PER_M = 0.1;
const OUTPUT_PER_M = 0.2;
const CACHE_PER_M = 0.002;

const SYSTEM = `Answer only from the EVIDENCE below. Structure your answer in three parts: sourced facts (cite [E1] ids), inference, uncertainty. If the evidence is thin, say what is missing.`;

// Truncate oldest-first to fit the Muse request budget.
function formatEvidence(evidence: Evidence[]): string {
  const parts = evidence.map(
    (e) =>
      `[${e.id}]\nsource: ${e.source}\n${e.title ? `title: ${e.title}\n` : ""}${e.url ? `url: ${e.url}\n` : ""}content: ${e.content}`,
  );
  const kept: string[] = [];
  let total = 0;
  for (let i = parts.length - 1; i >= 0; i--) {
    if (total + parts[i].length > 24000) break;
    kept.unshift(parts[i]);
    total += parts[i].length;
  }
  return kept.join("\n\n");
}

function harvest(ev: unknown, acc: { text: string; usage: MuseUsage }, onDelta: (t: string) => void): void {
  if (!ev || typeof ev !== "object") return;
  const rec = ev as Record<string, unknown>;
  if (typeof rec["delta"] === "string" && /delta/i.test(typeof rec["type"] === "string" ? rec["type"] : "")) {
    acc.text += rec["delta"] as string;
    onDelta(rec["delta"] as string);
  }
  const first = Array.isArray(rec["choices"]) ? rec["choices"][0] : undefined;
  const delta = first && typeof first === "object" ? (first as Record<string, unknown>)["delta"] : undefined;
  const content = delta && typeof delta === "object" ? (delta as Record<string, unknown>)["content"] : undefined;
  if (typeof content === "string" && content) {
    acc.text += content;
    onDelta(content);
  }
  const response = rec["response"];
  const nested = response && typeof response === "object" ? (response as Record<string, unknown>)["usage"] : undefined;
  const rawUsage = rec["usage"] ?? nested;
  if (!rawUsage || typeof rawUsage !== "object") return;
  const u = rawUsage as Record<string, unknown>;
  if (typeof u["input_tokens"] === "number") acc.usage.inputTokens = u["input_tokens"];
  if (typeof u["output_tokens"] === "number") acc.usage.outputTokens = u["output_tokens"];
  const inDetails = u["input_tokens_details"];
  const promptDetails = u["prompt_tokens_details"];
  const inCached = inDetails && typeof inDetails === "object" ? (inDetails as Record<string, unknown>)["cached_tokens"] : undefined;
  const promptCached = promptDetails && typeof promptDetails === "object" ? (promptDetails as Record<string, unknown>)["cached_tokens"] : undefined;
  const cached = u["cached_tokens"] ?? inCached ?? promptCached;
  if (typeof cached === "number") acc.usage.cachedTokens = cached;
  if (typeof u["prompt_tokens"] === "number") acc.usage.inputTokens ??= u["prompt_tokens"];
  if (typeof u["completion_tokens"] === "number") acc.usage.outputTokens ??= u["completion_tokens"];
}

async function readSse(
  res: Response,
  onDelta: (t: string) => void,
): Promise<{ text: string; usage: MuseUsage }> {
  const acc = { text: "", usage: {} as MuseUsage };
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (; ;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        const data = t.slice(5).trim();
        if (!data || data === "[DONE]") continue;
        try {
          harvest(JSON.parse(data), acc, onDelta);
        } catch {
          // Partial SSE frame; next chunk completes it.
        }
      }
    }
  }
  const u = acc.usage;
  if (u.inputTokens !== undefined || u.outputTokens !== undefined) {
    u.cost =
      (u.inputTokens ?? 0) / 1e6 * INPUT_PER_M +
      (u.outputTokens ?? 0) / 1e6 * OUTPUT_PER_M +
      (u.cachedTokens ?? 0) / 1e6 * CACHE_PER_M;
  }
  return acc;
}

async function post(
  apiKey: string,
  session: string,
  path: "/responses" | "/chat/completions",
  body: unknown,
  onDelta: (t: string) => void,
): Promise<{ text: string; usage: MuseUsage }> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "User-Agent": "needle-harness/0.1",
      "x-opencode-session": session,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`muse ${path} ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return readSse(res, onDelta);
}

export async function reason(opts: {
  prompt: string;
  evidence: Evidence[];
  escalated: boolean;
  direct?: boolean;
  // Optional graph state (kernel.ts always sends these; direct callers omit them).
  // incompleteGuard/unresolved force the missing-evidence line even with evidence present.
  objective?: string;
  nodes?: unknown[];
  decisions?: unknown[];
  unresolved?: string[];
  incompleteGuard?: boolean;
  onDelta: (text: string) => void;
}): Promise<{ text: string; usage: MuseUsage; missingEvidence?: string }> {
  const apiKey = process.env.OPENCODE_API_KEY;
  if (!apiKey) throw new Error("OPENCODE_API_KEY missing");
  const session = crypto.randomUUID();
  const needsGapLine =
    opts.escalated || opts.incompleteGuard === true || (opts.unresolved !== undefined && opts.unresolved.length > 0);
  const system = opts.direct
    ? "You are the Reasoner (Muse), not Needle. Answer the user directly and briefly from the supplied context. Needle is the constrained argument executor and never answers directly. If the request is unclear, ask what they mean and say what you can look up: the time, SEC filings, a URL to fetch, or a web search."
    : SYSTEM + (needsGapLine ? "\nRetrieval escalated with no usable evidence. End your answer with a line: Missing-Evidence: <what is needed>." : "");
  const user = opts.direct ? `USER REQUEST\n${opts.prompt}` : `USER REQUEST\n${opts.prompt}\n\nEVIDENCE\n${formatEvidence(opts.evidence)}`;
  const input = [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
  try {
    const r = await post(apiKey, session, "/responses", { model: MODEL, input, stream: true }, opts.onDelta);
    return { ...r, missingEvidence: /^Missing-Evidence:\s*(.+)$/m.exec(r.text)?.[1] };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (!/(404|405|500)/.test(msg)) throw err;
    const r = await post(
      apiKey,
      session,
      "/chat/completions",
      { model: MODEL, messages: input.map((m) => ({ role: m.role, content: m.content })), stream: true },
      opts.onDelta,
    );
    return { ...r, missingEvidence: /^Missing-Evidence:\s*(.+)$/m.exec(r.text)?.[1] };
  }
}
