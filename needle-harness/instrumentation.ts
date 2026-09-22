// Next.js startup hook. Empty by design: prewarm moved out of
// instrumentation because Turbopack traces every import in this file
// (literal AND computed) into the Edge Instrumentation build, where
// node:child_process/fs/os/path (kernel.ts) errors. Prewarm now runs
// lazily on first kernel call instead. Never throws; nothing to log.
export async function register(): Promise<void> { }
