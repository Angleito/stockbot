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

  test("contents differing only after char 8000 yield different ids and keep 8000-char budget plus marker", () => {
    const head = "x".repeat(8000);
    const a = makeEvidence("s", `${head}a`, { sourceHandle: { a: 1 } });
    const b = makeEvidence("s", `${head}b`, { sourceHandle: { a: 1 } });
    expect(a.id).not.toBe(b.id);
    expect(a.content.startsWith(head)).toBe(true);
    expect(a.content.endsWith("…[truncated]")).toBe(true);
    const c = makeEvidence("s", "x".repeat(9000), { sourceHandle: { a: 1 } });
    expect(c.content.startsWith("x".repeat(8000))).toBe(true);
    expect(c.content.endsWith("…[truncated]")).toBe(true);
  });

  test("content at or under budget has no truncation marker", () => {
    expect(makeEvidence("s", "short").content).toBe("short");
    expect(makeEvidence("s", "x".repeat(8000)).content).toBe("x".repeat(8000));
  });
});
