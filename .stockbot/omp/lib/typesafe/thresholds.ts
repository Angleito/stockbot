export const YES_THRESHOLD = 0.70;

export function yes(p: number): boolean {
 return typeof p === "number" && Number.isFinite(p) && p >= 0 && p <= 1 && p >= YES_THRESHOLD;
}
