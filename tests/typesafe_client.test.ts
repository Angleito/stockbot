import { afterAll, afterEach, expect, test } from "bun:test";
import { getTypeSafeClient, resetTypeSafeClientForTest } from "../.stockbot/omp/lib/typesafe/client.ts";

const KEY = "TYPESAFE_API_KEY";
const prevKey = process.env[KEY];
const realFetch = globalThis.fetch;
let fetchCalls = 0;

globalThis.fetch = ((..._args: unknown[]) => {
 fetchCalls++;
 throw new Error("network disabled in unit tests");
}) as unknown as typeof fetch;

afterEach(() => {
 fetchCalls = 0;
 resetTypeSafeClientForTest();
 if (prevKey === undefined) delete process.env[KEY];
 else process.env[KEY] = prevKey;
});

afterAll(() => {
 globalThis.fetch = realFetch;
});

test("singleton: same instance until reset", () => {
 process.env[KEY] = "dummy-test-key";
 const a = getTypeSafeClient();
 expect(getTypeSafeClient()).toBe(a);
 resetTypeSafeClientForTest();
 expect(getTypeSafeClient()).not.toBe(a);
});

test("construction performs zero network calls", () => {
 process.env[KEY] = "dummy-test-key";
 resetTypeSafeClientForTest();
 getTypeSafeClient();
 expect(fetchCalls).toBe(0);
});

test("missing key fails closed without leaking key material", () => {
 process.env[KEY] = "SENTINEL-KEY-ABCDE";
 resetTypeSafeClientForTest();
 delete process.env[KEY];
 let threw: unknown = null;
 try {
  getTypeSafeClient();
 } catch (e) {
  threw = e;
 } finally {
  resetTypeSafeClientForTest();
 }
 expect(fetchCalls).toBe(0);
 if (threw !== null) expect(String(threw)).not.toContain("SENTINEL-KEY-ABCDE");
});
