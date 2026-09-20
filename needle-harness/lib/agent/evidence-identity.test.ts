import { describe, expect, test } from "bun:test";
import { makeEvidence } from "./evidence";

describe("makeEvidence identity", () => {
  test("same source+handle+content yields same id", () => {
    const a = makeEvidence("s", "hello", { sourceHandle: { a: 1 } });
    const b = makeEvidence("s", "hello", { sourceHandle: { a: 1 } });
    expect(a.id).toBe(b.id);
  });

  test("different handle yields different id", () => {
    const a = makeEvidence("s", "hello", { sourceHandle: { a: 1 } });
    const b = makeEvidence("s", "hello", { sourceHandle: { a: 2 } });
    expect(a.id).not.toBe(b.id);
  });

  test("contents differing only after char 8000 yield different ids and store 8000 chars", () => {
    const head = "x".repeat(8000);
    const a = makeEvidence("s", `${head}a`, { sourceHandle: { a: 1 } });
    const b = makeEvidence("s", `${head}b`, { sourceHandle: { a: 1 } });
    expect(a.id).not.toBe(b.id);
    expect(a.content.length).toBe(8000);
    const c = makeEvidence("s", "x".repeat(9000), { sourceHandle: { a: 1 } });
    expect(c.content.length).toBe(8000);
  });
});
