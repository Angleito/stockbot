import type { Evidence } from "./types";

let counter = 0;

export function resetEvidenceIds(): void {
  counter = 0;
}

export function makeEvidence(
  source: string,
  content: string,
  opts?: { title?: string; url?: string },
): Evidence {
  counter += 1;
  return {
    id: `E${counter}`,
    source,
    title: opts?.title,
    url: opts?.url,
    retrievedAt: new Date().toISOString(),
    content,
  };
}

export function preview(evidence: Evidence): string {
  return evidence.content.slice(0, 160);
}
