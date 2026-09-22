import { describe, expect, test } from "bun:test";
import { isTrivialPrompt } from "./entry-router";

describe("entry-router", () => {
  test("trivial prompts short-circuit", () => {
    for (const p of ["hello", "hi", "thanks", "Thank you!", "hey there"]) {
      expect(isTrivialPrompt(p)).toBe(true);
    }
  });

  test("research signals are not trivial", () => {
    for (const p of [
      "What drove NVDA revenue last quarter?",
      "how are you today?",
      "search SEC filings",
      "NVDA stock price",
      "hello, what is NVDA's revenue?",
    ]) {
      expect(isTrivialPrompt(p)).toBe(false);
    }
  });
});
