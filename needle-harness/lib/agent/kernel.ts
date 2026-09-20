import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { reason, type MuseUsage } from "../muse/client";
import { redactArgs } from "./types";
import type { AgentEvent, Evidence, FailureCategory, Metrics } from "./types";

// ponytail: one-shot worker per request (no persistent bridge); spawn/exit/stderr handling mirrors lib/needle/client.ts.
const MUSE_MODEL = "muse-spark-1.3-contributor";
const WORKER_TIMEOUT_MS = 10 * 60 * 1000;

const ROOT = process.env.STOCKBOT_REPO_ROOT ?? process.cwd();
const WORKER = `${ROOT}/app/research/kernel_worker.py`;
const VENV_PYTHON = `${homedir()}/.cache/needle-harness/.needle/bin/python`;

export type KernelEvidence = {
  id: string;
  content: string;
  source?: string;
  title?: string;
  url?: string;
  retrievedAt?: string;
};

export type KernelNeedleDecision = {
  step: number;
  tool: string | null;
  arguments: Record<string, unknown>;
  confidence: number | null;
  reasoning?: string;
};

export type KernelToolCall = {
  tool: string;
  ok: boolean;
  evidenceId?: string;
  error?: string;
  category?: FailureCategory;
};

export type KernelResponse = {
  id?: string;
  evidence: KernelEvidence[];
  needleDecisions: KernelNeedleDecision[];
  toolCalls: KernelToolCall[];
  failures: Record<string, number>;
  escalations: number;
  escalated: boolean;
  error?: string;
  terminal?: { category: FailureCategory; message: string };
};

export type KernelChild = {
  stdin: { write: (data: string, cb?: (err?: Error | null) => void) => void } | null;
  stdout: { on: (event: "data", listener: (chunk: Buffer) => void) => void } | null;
  stderr: { on: (event: "data", listener: (chunk: Buffer) => void) => void } | null;
  on: (event: "error" | "exit", listener: (arg?: unknown) => void) => void;
  kill: (signal?: string) => void;
  readonly exitCode: number | null;
};

export type KernelSpawn = (cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv }) => KernelChild;

export type RunKernelDeps = {
  spawnFn?: KernelSpawn;
  reason?: typeof reason;
  python?: string;
  workerPath?: string;
  timeoutMs?: number;
};

function defaultSpawn(cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv }): KernelChild {
  return spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], env: opts.env }) as unknown as KernelChild;
}

function callWorker(
  body: Record<string, unknown>,
  opts: { signal?: AbortSignal; timeoutMs: number; python: string; workerPath: string; spawnFn: KernelSpawn },
): Promise<KernelResponse> {
  return new Promise<KernelResponse>((resolve, reject) => {
    let child: KernelChild;
    try {
      child = opts.spawnFn(opts.python, [opts.workerPath], {
        env: { ...process.env, NEEDLE_TELEMETRY: "0", DO_NOT_TRACK: "1" },
      });
    } catch (err) {
      reject(err);
      return;
    }
    console.error(`[ai] kernel worker spawn ${opts.python} (${opts.workerPath})`);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stderrTail = "";
    let buf = "";
    let settled = false;
    const done = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
      fn();
    };
    const fail = (err: Error): void => {
      done(() => {
        try {
          child.kill();
        } catch {
          // Already gone; rejection below carries the error.
        }
        reject(err);
      });
    };
    const onAbort = (): void => fail(new Error("worker aborted"));
    timer = setTimeout(() => fail(new Error(`kernel worker timeout; stderr tail: ${stderrTail || "(empty)"}`)), opts.timeoutMs);
    if (opts.signal?.aborted) {
      fail(new Error("worker aborted"));
      return;
    }
    opts.signal?.addEventListener("abort", onAbort, { once: true });
    child.stderr?.on("data", (d: Buffer) => {
      const s = d.toString();
      stderrTail = (stderrTail + s).slice(-2000);
      process.stderr.write(s.startsWith("[kernel-worker]") ? s : `[kernel-worker] ${s}`);
    });
    child.on("error", (err) => fail(err instanceof Error ? err : new Error(String(err))));
    child.on("exit", () => fail(new Error(`kernel worker exited; stderr tail: ${stderrTail || "(empty)"}`)));
    child.stdout?.on("data", (d: Buffer) => {
      buf += d.toString();
      let i = buf.indexOf("\n");
      while (i >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) {
          let msg: unknown;
          try {
            msg = JSON.parse(line);
          } catch {
            // Non-JSON bridge noise; keep scanning.
          }
          if (typeof msg === "object" && msg !== null && (!("id" in msg) || msg.id === body.id)) {
            // boundary: JSONL line from our own worker; every field is defaulted where read below.
            const res: KernelResponse = msg as KernelResponse;
            done(() => {
              try {
                child.kill();
              } catch {
                // One-shot worker; nothing to reuse.
              }
              resolve(res);
            });
            return;
          }
        }
        i = buf.indexOf("\n");
      }
    });
    if (!child.stdin) {
      fail(new Error("kernel worker spawn failed"));
      return;
    }
    child.stdin.write(`${JSON.stringify(body)}\n`, (err) => {
      if (err) fail(err instanceof Error ? err : new Error(String(err)));
    });
  });
}

export async function runKernelAgent(
  prompt: string,
  emit: (e: AgentEvent) => void,
  opts?: { signal?: AbortSignal; deadlineMs?: number; deps?: RunKernelDeps },
): Promise<void> {
  const t0 = performance.now();
  const deps = opts?.deps ?? {};
  const spawnFn = deps.spawnFn ?? defaultSpawn;
  const reasonFn = deps.reason ?? reason;
  const timeoutMs = deps.timeoutMs ?? opts?.deadlineMs ?? WORKER_TIMEOUT_MS;
  const python = deps.python ?? (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3");
  const workerPath = deps.workerPath ?? WORKER;

  const buildMetrics = (
    decisions: number,
    calls: number,
    ev: Evidence[],
    workerMs: number,
    escalations: number,
    fl: Partial<Record<FailureCategory, number>>,
    muse: Metrics["muse"],
  ): Metrics => ({
    totalMs: performance.now() - t0,
    // ponytail: kernel reports no routing/execution split yet; worker wall time sits on tools, TS routing is 0.
    needle: { calls: decisions, totalMs: 0, escalations },
    tools: { calls, totalMs: workerMs },
    muse,
    evidence: { count: ev.length, characters: ev.reduce((n, e) => n + e.content.length, 0) },
    failures: fl,
  });

  emit({ type: "agent_start", prompt });

  let res: KernelResponse;
  const workerStart = performance.now();
  try {
    res = await callWorker({ id: "1", op: "run", prompt, deadlineMs: timeoutMs }, { signal: opts?.signal, timeoutMs, python, workerPath, spawnFn });
  } catch (err) {
    // ponytail: needle-down shape — worker failure still ends at done, never throws to the route.
    const msg = err instanceof Error ? err.message : String(err);
    emit({ type: "tool_failed", tool: "needle", category: "provider_error", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: msg.slice(0, 160) });
    emit({ type: "done", metrics: buildMetrics(0, 0, [], 0, 1, { provider_error: 1 }, { calls: 0, totalMs: 0 }) });
    return;
  }
  const workerMs = performance.now() - workerStart;
  const decisions = Array.isArray(res.needleDecisions) ? res.needleDecisions : [];
  const calls = Array.isArray(res.toolCalls) ? res.toolCalls : [];
  const evidence: Evidence[] = (Array.isArray(res.evidence) ? res.evidence : []).map((e) => ({
    id: String(e.id),
    source: e.source ?? "kernel",
    ...(e.title !== undefined ? { title: e.title } : {}),
    ...(e.url !== undefined ? { url: e.url } : {}),
    retrievedAt: e.retrievedAt ?? new Date().toISOString(),
    content: String(e.content ?? ""),
  }));
  const byId: Record<string, Evidence> = {};
  for (const e of evidence) byId[e.id] = e;
  const failures: Partial<Record<FailureCategory, number>> = {};
  if (res.failures && typeof res.failures === "object") {
    for (const [k, v] of Object.entries(res.failures)) if (typeof v === "number") failures[k as FailureCategory] = v;
  }
  const countFailure = (c: FailureCategory): void => {
    failures[c] = (failures[c] ?? 0) + 1;
  };

  if (typeof res.error === "string" && res.error) {
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "needle", category: "provider_error", preview: res.error.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: res.error.slice(0, 160) });
    emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, (res.escalations ?? 0) + 1, failures, { calls: 0, totalMs: 0 }) });
    return;
  }

  // ponytail: index-aligned replay — worker lists decisions and calls in execution order, so calls[i] belongs to decisions[i]; a trailing escalate decision has no call.
  const termCategory = res.terminal && typeof res.terminal === "object" ? res.terminal.category : undefined;
  let toolFailedEmitted = false;
  const emitCall = (c: KernelToolCall): void => {
    emit({ type: "tool_start", tool: c.tool });
    if (c.ok) {
      const ev = c.evidenceId ? byId[c.evidenceId] : undefined;
      emit({ type: "tool_result", tool: c.tool, evidenceId: c.evidenceId, preview: (ev?.content ?? "").slice(0, 160) });
    } else {
      const preview = (c.error ?? "tool failed").slice(0, 160);
      emit({ type: "tool_result", tool: c.tool, preview });
      emit({ type: "tool_failed", tool: c.tool, category: c.category ?? termCategory ?? "tool_error", preview });
      toolFailedEmitted = true;
    }
  };
  for (let i = 0; i < decisions.length; i++) {
    const d = decisions[i];
    emit({ type: "needle_decision", step: d.step ?? i, tool: d.tool ?? null, arguments: redactArgs(d.arguments ?? {}), confidence: d.confidence ?? null });
    if (i < calls.length) emitCall(calls[i]);
  }
  for (let i = decisions.length; i < calls.length; i++) emitCall(calls[i]);

  if (res.terminal && typeof res.terminal === "object") {
    const { category, message } = res.terminal;
    countFailure(category);
    if (!toolFailedEmitted) {
      emit({ type: "tool_failed", tool: calls.length > 0 ? calls[calls.length - 1].tool : "worker", category, preview: message.slice(0, 160) });
    }
    emit({ type: "failed", category, message: message.slice(0, 160) });
    emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, (res.escalations ?? 0) + 1, failures, { calls: 0, totalMs: 0 }) });
    return;
  }

  emit({ type: "reasoning_start", model: MUSE_MODEL });
  const tm = performance.now();
  let usage: MuseUsage;
  try {
    const r = await reasonFn({
      prompt,
      evidence,
      escalated: res.escalated ?? false,
      direct: (res.escalated ?? false) && evidence.length === 0,
      onDelta: (text) => emit({ type: "answer_delta", text }),
    });
    usage = r.usage;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "muse", category: "provider_error", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: msg.slice(0, 160) });
    emit({ type: "error", message: msg });
    return;
  }
  emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, res.escalations ?? 0, failures, { calls: 1, totalMs: performance.now() - tm, ...usage }) });
}
