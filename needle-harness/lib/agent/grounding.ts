// Harness-side grounding gate: ungrounded tool args fail closed before execution.
// ponytail: token-overlap heuristic, paraphrase false-rejects possible; add entity-alias overlap if benchmark false-reject rate is high, never lower threshold to zero.
const STOPWORDS: Record<string, true> = {
  what: true, will: true, should: true, when: true, does: true, that: true,
  this: true, with: true, from: true, have: true, has: true, are: true,
  for: true, and: true, the: true,
};

const ALLOWLIST: Record<string, true> = {
  search_web: true,
  find_sec_entities: true,
  search_sec_filings: true,
  get_sec_document: true,
  fetch_url: true,
  get_current_time: true,
};

const ACCESSION_RE = /^\d{10}-\d{2}-\d{6}$/;

export type GroundingOpts = {
  prompt: string;
  evidenceText: string;
  seenUrls: Set<string>;
};

function contentTokens(text: string): Set<string> {
  const out = new Set<string>();
  for (const raw of text.match(/[A-Za-z0-9]+/g) ?? []) {
    const lower = raw.toLowerCase();
    if (STOPWORDS[lower]) continue;
    const isCapsTicker = raw.length >= 2 && /^[A-Z0-9]+$/.test(raw) && /[A-Z]/.test(raw);
    if (lower.length >= 3 || isCapsTicker) out.add(lower);
  }
  return out;
}

function repetitionError(tokens: string[]): string | null {
  const counts = new Map<string, number>();
  for (const t of tokens) counts.set(t, (counts.get(t) ?? 0) + 1);
  for (const [t, n] of counts) {
    if (n >= 3) return `ungrounded: token "${t}" repeats ${n}x`;
  }
  if (tokens.length > 0 && counts.size / tokens.length < 0.4) {
    return "ungrounded: query lacks lexical diversity";
  }
  return null;
}

export function groundingError(
  name: string,
  args: Record<string, unknown>,
  opts: GroundingOpts,
): string | null {
  if (!ALLOWLIST[name]) return `ungrounded: unknown tool "${name}"`;

  if (name === "get_current_time") return null;

  if (name === "fetch_url") {
    const url = args.url;
    if (typeof url !== "string" || !opts.seenUrls.has(url)) {
      return `ungrounded: fetch_url target not in evidence: ${String(url ?? "(missing)").slice(0, 120)}`;
    }
    return null;
  }

  if (name === "get_sec_document") {
    const acc = args.accession_no;
    if (typeof acc !== "string" || !ACCESSION_RE.test(acc)) {
      return `ungrounded: accession_no must match NNNNNNNNNN-NN-NNNNNN, got ${String(acc ?? "(missing)").slice(0, 60)}`;
    }
    if (!opts.evidenceText.includes(acc)) {
      return `ungrounded: accession_no ${acc} absent from evidence`;
    }
    const refine = ["query", "section"]
      .filter((k) => typeof args[k] === "string")
      .map((k) => args[k] as string)
      .join(" ");
    if (refine.trim()) {
      const rep = repetitionError(refine.toLowerCase().match(/[a-z0-9]+/g) ?? []);
      if (rep) return rep;
    }
    return null;
  }

  // Search family: join string-valued args.
  const text = Object.values(args)
    .filter((v): v is string => typeof v === "string")
    .join(" ");
  const tokens = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
  if (!text.trim() || tokens.length === 0) return "ungrounded: search query is empty";
  const rep = repetitionError(tokens);
  if (rep) return rep;
  const querySet = contentTokens(text);
  const ctxSet = contentTokens(`${opts.prompt}\n${opts.evidenceText}`);
  for (const t of querySet) {
    if (ctxSet.has(t)) return null;
  }
  return "ungrounded: query shares no content token with prompt or evidence";
}
