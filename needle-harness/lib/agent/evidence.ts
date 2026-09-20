import { createHash } from "node:crypto";
import type { Evidence } from "./types";

export function makeEvidence(
  source: string,
  content: string,
  opts?: { title?: string; url?: string },
): Evidence {
  const body = content.slice(0, 8000);
  const id = `ev:${createHash("sha256").update(body).digest("hex").slice(0, 16)}`;
  return {
    id,
    source,
    title: opts?.title,
    url: opts?.url,
    retrievedAt: new Date().toISOString(),
    content: body,
  };
}

export function preview(evidence: Evidence): string {
  return evidence.content.slice(0, 160);
}
