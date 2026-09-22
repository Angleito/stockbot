import { prewarmKernel } from "@/lib/agent/kernel";

// Node.js-only startup hook (see instrumentation.ts): prewarms the kernel
// worker so request #1 == request #500. Never throws; prewarm failure only logs.
export async function register(): Promise<void> {
  try {
    await prewarmKernel();
    process.env.KERNEL_PREWARMED = "1";
    console.log("[web] prewarm kernel ready");
  } catch (e) {
    console.error(`[web] kernel prewarm failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
