import { makeEvidence } from "../agent/evidence";
import type { Tool } from "../agent/types";

export const get_sec_filings: Tool = {
  description: "List recent SEC EDGAR filings for a US stock ticker",
  parameters: {
    type: "object",
    properties: {
      ticker: { type: "string", description: "Stock ticker symbol, e.g. NVDA (not the company name)" },
      forms: { type: "array", items: { type: "string" } },
      limit: { type: "integer", minimum: 1, maximum: 10, default: 5 },
    },
    required: ["ticker"],
  },
  async execute(args) {
    try {
      const ticker = String(args.ticker ?? "").toUpperCase();
      if (!/^[A-Z]{1,5}$/.test(ticker)) {
        return { ok: false, error: "ticker required (1-5 uppercase letters)" };
      }
      const identity = process.env.SEC_EDGAR_IDENTITY;
      if (!identity) return { ok: false, error: "SEC_EDGAR_IDENTITY missing" };
      const headers = {
        "User-Agent": identity,
        "Accept-Encoding": "gzip",
        Accept: "application/json",
      };
      const limit = typeof args.limit === "number" ? args.limit : 5;
      const forms = Array.isArray(args.forms)
        ? (args.forms as unknown[]).map((f) => String(f).toUpperCase())
        : null;

      const tickersRes = await fetch("https://www.sec.gov/files/company_tickers.json", {
        headers,
        signal: AbortSignal.timeout(10000),
      });
      if (!tickersRes.ok) return { ok: false, error: `ticker lookup failed: ${tickersRes.status}` };
      const tickers = (await tickersRes.json()) as Record<
        string,
        { ticker?: string; cik_str?: number }
      >;
      const entry = Object.values(tickers).find((t) => t.ticker === ticker);
      if (!entry?.cik_str) return { ok: false, error: `unknown ticker: ${ticker}` };
      const cik10 = String(entry.cik_str).padStart(10, "0");
      const cikNoLead = String(entry.cik_str);
      const submissionsUrl = `https://data.sec.gov/submissions/CIK${cik10}.json`;

      const subRes = await fetch(submissionsUrl, {
        headers,
        signal: AbortSignal.timeout(10000),
      });
      if (!subRes.ok) return { ok: false, error: `submissions fetch failed: ${subRes.status}` };
      const subs = (await subRes.json()) as {
        filings?: {
          recent?: {
            form?: string[];
            filingDate?: string[];
            accessionNumber?: string[];
            primaryDocument?: string[];
          };
        };
      };
      const recent = subs.filings?.recent;
      const n = Math.min(recent?.form?.length ?? 0, 100);
      const lines: string[] = [];
      let taken = 0;
      let firstAcc = "";
      for (let i = 0; i < n && taken < limit; i++) {
        const form = recent!.form![i];
        if (forms && !forms.includes(form)) continue;
        const date = recent!.filingDate![i];
        const acc = recent!.accessionNumber![i];
        const doc = recent!.primaryDocument![i];
        if (!firstAcc) firstAcc = acc;
        lines.push(`- ${form} filed ${date}: ${acc} (${doc})`);
        taken++;
      }

      // Best-effort: index listing for the first filing; failure is not fatal.
      try {
        if (firstAcc) {
          const accNoDashes = firstAcc.replace(/-/g, "");
          const indexUrl = `https://www.sec.gov/Archives/edgar/data/${cikNoLead}/${accNoDashes}/index.json`;
          const idxRes = await fetch(indexUrl, {
            headers,
            signal: AbortSignal.timeout(10000),
          });
          if (idxRes.ok) {
            const idx = (await idxRes.json()) as {
              directory?: { item?: { name?: string }[] };
            };
            const names = (idx.directory?.item ?? [])
              .map((it) => it.name ?? "")
              .filter(Boolean)
              .slice(0, 10);
            if (names.length) lines.push(`index: ${names.join(", ")}`);
          }
        }
      } catch {
        // ignore index failures
      }

      const content = lines.join("\n").slice(0, 8000);
      return {
        ok: true,
        evidence: makeEvidence("sec-edgar", content, {
          title: `SEC filings: ${ticker}`,
          url: submissionsUrl,
        }),
      };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },
};
