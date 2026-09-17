import { expect, test } from "bun:test";
import fc from "fast-check";
import { YES_THRESHOLD, yes } from "../.stockbot/omp/lib/typesafe/thresholds.ts";

test("threshold constant is exactly 0.70", () => {
	expect(YES_THRESHOLD).toBe(0.7);
});

test("boundary pins: 0.6999 NO, 0.70 YES", () => {
	expect(yes(0.0)).toBe(false);
	expect(yes(0.6999)).toBe(false);
	expect(yes(0.7)).toBe(true);
	expect(yes(0.7001)).toBe(true);
	expect(yes(1.0)).toBe(true);
});

test("fail-closed: NaN, non-finite, out-of-range", () => {
	for (const bad of [NaN, Infinity, -Infinity, -0.1, 1.1, -1, 2]) expect(yes(bad)).toBe(false);
});

test("fail-closed: wrong-type probes return false, never throw", () => {
	const nonNumbers: unknown[] = [undefined, null, "0.9", {}, []];
	for (const bad of nonNumbers) {
		const probe: number = bad as never;
		expect(yes(probe)).toBe(false);
	}
});

test("property: yes(p) iff finite p within [0.70, 1]", () => {
	fc.assert(
		fc.property(fc.double(), (p) => yes(p) === (Number.isFinite(p) && p >= 0 && p <= 1 && p >= 0.7)),
		{ seed: 42 },
	);
});
