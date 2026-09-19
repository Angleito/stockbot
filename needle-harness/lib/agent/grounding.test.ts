import { describe, expect, test } from "bun:test";
import { groundingError } from "./grounding";

const PROMPT = "What will happen to NVDA should Anthropic IPO fail?";
const opts = (evidenceText = "", urls: string[] = []) => ({
  prompt: PROMPT,
  evidenceText,
  seenUrls: new Set(urls),
});

describe("groundingError", () => {
  test("rejects ungrounded download query", () => {
    expect(
      groundingError("search_web", { query: "download file download file download" }, opts()),
    ).not.toBeNull();
  });

  test("passes grounded NVDA/Anthropic query", () => {
    expect(
      groundingError("search_web", { query: "NVDA Anthropic investment exposure" }, opts()),
    ).toBeNull();
  });

  test("rejects triple-repeated token", () => {
    expect(
      groundingError("search_web", { query: "NVDA NVDA NVDA earnings" }, opts()),
    ).not.toBeNull();
  });

  test("rejects unseen fetch_url, passes evidence URL", () => {
    expect(
      groundingError("fetch_url", { url: "https://evil.example/x" }, opts("", ["https://good.example/y"])),
    ).not.toBeNull();
    expect(
      groundingError("fetch_url", { url: "https://good.example/y" }, opts("", ["https://good.example/y"])),
    ).toBeNull();
  });

  test("rejects accession absent from evidence, passes when present", () => {
    const acc = "0000320193-25-000079";
    expect(
      groundingError("get_sec_document", { accession_no: acc }, opts("no filings here")),
    ).not.toBeNull();
    expect(
      groundingError("get_sec_document", { accession_no: acc }, opts(`filing ${acc} found`)),
    ).toBeNull();
  });

  test("rejects malformed accession", () => {
    expect(
      groundingError("get_sec_document", { accession_no: "NVDA-10K" }, opts("NVDA-10K")),
    ).not.toBeNull();
  });

  test("get_current_time always passes", () => {
    expect(groundingError("get_current_time", {}, opts())).toBeNull();
  });

  test("unknown tool rejected", () => {
    expect(groundingError("web_search", { query: "NVDA" }, opts())).not.toBeNull();
  });
});
