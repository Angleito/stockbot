// Next.js startup hook: prewarm the kernel worker at app startup so
// request #1 == request #500. Never throws; prewarm failure only logs.
export async function register(): Promise<void> {
  try {
    // Dynamic import, not static: keeps the kernel singleton (spawns a python
    // worker on ensure) out of this module's graph so a kernel load failure
    // can't break startup; failure is caught below.
    const m = await import("@/lib/agent/kernel");
    await m.prewarmKernel?.();
    process.env.KERNEL_PREWARMED = "1";
    console.log("[web] prewarm kernel ready");
  } catch (e) {
    console.error(`[web] kernel prewarm failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
