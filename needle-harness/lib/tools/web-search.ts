import { makeEvidence } from "../agent/evidence";
import type { Tool } from "../agent/types";

export const web_search: Tool = {
  description: "Search the web for current information",
  parameters: {
    type: "object",
    properties: {
      query: { type: "string" },
      limit: { type: "integer", minimum: 1, maximum: 5, default: 5 },
    },
    required: ["query"],
  },
  async execute(args) {
    try {
      const query = String(args.query ?? "");
      if (!query) return { ok: false, error: "query required" };
      if (!/^(1|true|yes|on)$/i.test(process.env.EXA_ENABLED ?? "") || !process.env.EXA_API_KEY) {
        return { ok: false, error: "web_search disabled (EXA_ENABLED/EXA_API_KEY)" };
      }
      const limit = typeof args.limit === "number" ? args.limit : 5;
      const res = await fetch("https://api.exa.ai/search", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.EXA_API_KEY,
        },
        body: JSON.stringify({ query, numResults: limit }),
        signal: AbortSignal.timeout(10000),
      });
      if (!res.ok) return { ok: false, error: `exa search failed: ${res.status}` };
      const data = (await res.json()) as {
        results?: { title?: string; url?: string; text?: string; snippet?: string }[];
      };
      const lines = (data.results ?? []).map(
        (r) => `- ${r.title ?? "untitled"} (${r.url ?? "no-url"}): ${r.text ?? r.snippet ?? ""}`,
      );
      const content = lines.join("\n").slice(0, 8000);
      return {
        ok: true,
        evidence: makeEvidence("exa", content, { title: `Web search: ${query}` }),
      };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },
};
