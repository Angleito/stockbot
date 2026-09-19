import { describe, expect, test } from "bun:test";
import { acquireNeedle } from "./client";

describe("acquireNeedle", () => {
  test("concurrent holders serialize", async () => {
    const order: string[] = [];
    const first = await acquireNeedle();
    order.push("A-enter");
    let bEntered = false;
    const bGrant = acquireNeedle();
    const bWait = bGrant.then(async (releaseB) => {
      bEntered = true;
      order.push("B-enter");
      order.push("B-exit");
      releaseB();
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(bEntered).toBe(false);
    order.push("A-exit");
    first();
    await bWait;
    expect(order).toEqual(["A-enter", "A-exit", "B-enter", "B-exit"]);
  });

  test("throw still grants next holder", async () => {
    const first = await acquireNeedle();
    try {
      throw new Error("boom");
    } catch {
      // Fall through; release must still run.
    } finally {
      first();
    }
    const second = await acquireNeedle();
    try {
      expect(true).toBe(true);
    } finally {
      second();
    }
  });
});
