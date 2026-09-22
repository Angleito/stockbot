// Next.js startup hook. Dynamic import is required here, never static:
// Turbopack traces every import in this file into the Edge Instrumentation
// build, where node:child_process/fs/os/path (kernel.ts) errors. Node-only
// prewarm lives in ./instrumentation.node, loaded on the nodejs runtime only.
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "edge") return;
  try {
    await import("./instrumentation.node").then((m) => m.register());
  } catch (e) {
    console.error(`[web] prewarm load failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
