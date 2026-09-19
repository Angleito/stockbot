import { makeEvidence } from "../agent/evidence";
import type { Tool } from "../agent/types";

let allowed: Set<string> | null = null;

export function setAllowedFetchUrls(urls: string[]): void {
  allowed = new Set(urls);
}

export const fetch_url: Tool = {
  description: "Fetch a URL and extract readable text",
  parameters: {
    type: "object",
    properties: { url: { type: "string" } },
    required: ["url"],
  },
  async execute(args) {
    try {
      const url = String(args.url ?? "");
      if (!/^https?:\/\//.test(url)) {
        return { ok: false, error: "fetch_url requires http(s) url" };
      }
      if (allowed !== null && !allowed.has(url)) {
        return { ok: false, error: "fetch_url target not in evidence" };
      }
      const res = await fetch(url, { signal: AbortSignal.timeout(10000) });
      if (!res.ok) return { ok: false, error: `fetch failed: ${res.status}` };
      const ct = res.headers.get("content-type") ?? "";
      if (!ct.includes("text/") && !ct.includes("application/json")) {
        return { ok: false, error: `unsupported content-type: ${ct || "unknown"}` };
      }
      const raw = (await res.text()).slice(0, 500_000);
      const text = raw
        .replace(/<script[\s\S]*?<\/script>/gi, " ")
        .replace(/<style[\s\S]*?<\/style>/gi, " ")
        .replace(/<[^>]+>/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 8000);
      return {
        ok: true,
        evidence: makeEvidence("web-fetch", text, { title: `Fetched ${url}`, url }),
      };
    } catch (e) {
      return { ok: false, error: `fetch failed: ${e instanceof Error ? e.message : String(e)}` };
    }
  },
};
