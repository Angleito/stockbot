import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

export type NeedleRouteResult = {
  tool: string | null;
  arguments: Record<string, unknown>;
  confidence: number | null;
  reasoning: string;
};

export type NeedleDecision = NeedleRouteResult & {
  escalate: boolean;
};

export const TOOL_TIMEOUT_MS = 120_000;
// Runtime gate mirroring server.py validate_needle_tool: Needle output must
// invoke the exact JEV-selected tool. JEV owns selection; Needle never
// selects, chains, or judges sufficiency. Throws on mismatch (incl. null).
export function validateNeedleTool(jevTool: string, needleTool: string | null): string {
  if (!jevTool) throw new Error("validateNeedleTool: jevTool must be a nonempty tool name");
  if (needleTool !== jevTool)
    throw new Error(`needle tool mismatch: jev selected ${JSON.stringify(jevTool)}, needle emitted ${JSON.stringify(needleTool)}`);
  return needleTool;
}

const ROOT = process.cwd();
const SERVER = `${ROOT}/lib/needle/server.py`;
const VENV_PYTHON = `${homedir()}/.cache/needle-harness/.needle/bin/python`;

type Pending = {
  resolve: (v: NeedleRouteResult) => void;
  reject: (e: Error) => void;
  cancel: () => void;
};
// ponytail: exclusive lock serializes Needle routing; per-session Needle state if Herdr panes need concurrency
let needleTail: Promise<void> = Promise.resolve();

export async function acquireNeedle(): Promise<() => void> {
  const { promise: grant, resolve: release } = Promise.withResolvers<void>();
  const prev = needleTail;
  needleTail = prev.then(() => grant);
  await prev;
  return () => release();
}


export class NeedleRouter {
  private child: ChildProcess | null = null;
  private pending: Record<string, Pending> = {};
  private buf = "";
  private nextId = 0;
  private exited = false;
  private bridgeStderrTail = "";

  private spawn(): ChildProcess {
    const python = existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3";
    this.exited = false;
    this.buf = "";
    const child = spawn(python, [SERVER], {
      stdio: ["pipe", "pipe", "pipe"] as const,
      env: { ...process.env, NEEDLE_TELEMETRY: "0", DO_NOT_TRACK: "1" },
    });
    this.child = child;
    child.stdout?.on("data", (d: Buffer) => this.onData(d.toString()));
    child.stderr?.on("data", (d: Buffer) => {
      const s = d.toString();
      this.bridgeStderrTail = (this.bridgeStderrTail + s).slice(-2000);
      process.stderr.write(s.startsWith("[needle-bridge]") ? s : `[needle-bridge] ${s}`);
    });
    child.on("error", (err) => this.failAll(err));
    child.on("exit", () => {
      this.exited = true;
      if (Object.keys(this.pending).length > 0) {
        this.failAll(new Error(`needle child exited; stderr tail: ${this.bridgeStderrTail || "(empty)"}`));
      }
      this.child = null;
    });
    console.error(`[ai] needle bridge spawn ${python} (${SERVER})`);
    return child;
  }

  private ensure(): ChildProcess {
    const running = this.child;
    if (running && !this.exited && running.exitCode === null) return running;
    try {
      running?.kill();
    } catch {
      // Already gone; fresh spawn below replaces it.
    }
    const fresh = this.spawn();
    if (!fresh.stdin || !fresh.stdout) {
      this.child = null;
      throw new Error("needle spawn failed");
    }
    return fresh;
  }

  private onLine(line: string): void {
    let msg: unknown;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    if (typeof msg !== "object" || msg === null || !("id" in msg)) return;
    const id: unknown = msg.id;
    if (typeof id !== "string") return;
    const p = this.pending[id];
    if (!p) return;
    delete this.pending[id];
    p.cancel();
    if ("error" in msg && typeof msg.error === "string") {
      p.reject(new Error(msg.error));
      return;
    }
    const tool = "tool" in msg && (typeof msg.tool === "string" || msg.tool === null) ? msg.tool : null;
    const args =
      "arguments" in msg && typeof msg.arguments === "object" && msg.arguments !== null
        ? (msg.arguments as Record<string, unknown>)
        : {};
    const confidence = "confidence" in msg && typeof msg.confidence === "number" ? msg.confidence : null;
    const reasoning = "reasoning" in msg && typeof msg.reasoning === "string" ? msg.reasoning : "";
    p.resolve({ tool, arguments: args, confidence, reasoning });
  }

  private onData(chunk: string): void {
    this.buf += chunk;
    let i = this.buf.indexOf("\n");
    while (i >= 0) {
      const line = this.buf.slice(0, i).trim();
      this.buf = this.buf.slice(i + 1);
      if (line.length > 0) this.onLine(line);
      i = this.buf.indexOf("\n");
    }
  }

  private failAll(err: Error): void {
    for (const id of Object.keys(this.pending)) {
      const p = this.pending[id];
      if (p) {
        delete this.pending[id];
        p.cancel();
        p.reject(err);
      }
    }
  }

  private call(body: Record<string, unknown>): Promise<NeedleRouteResult> {
    const child = this.ensure();
    if (!child.stdin) throw new Error("needle spawn failed");
    const id = String((this.nextId += 1));
    return new Promise<NeedleRouteResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        delete this.pending[id];
        child.kill();
        this.child = null;
        reject(new Error(`needle route timeout; stderr tail: ${this.bridgeStderrTail || "(empty)"}`));
      }, TOOL_TIMEOUT_MS);
      this.pending[id] = { resolve, reject, cancel: () => clearTimeout(timer) };
      child.stdin?.write(JSON.stringify({ id, ...body }) + "\n", (err) => {
        if (err) {
          const p = this.pending[id];
          if (p) {
            delete this.pending[id];
            p.cancel();
            p.reject(err);
          }
        }
      });
    });
  }

  // confidence stays on the record for observability but never gates: tuned
  // weights report None, so any floor only pretends to protect.
  // Legacy non-kernel path: kernel path uses generateArguments only; JEV owns transitions (kept until loop.ts cutover).
  async start(prompt: string): Promise<NeedleDecision> {
    const result = await this.call({ action: "start", prompt });
    return { ...result, escalate: result.tool === null };
  }

  async step(result: unknown): Promise<NeedleDecision> {
    const r = await this.call({ action: "step", result });
    return { ...r, escalate: r.tool === null };
  }

  // Narrow execution worker op: generate arguments for the exact JEV-selected
  // tool only. Single request->response (no Needle-owned conversation).
  // Enforces emitted tool == requested tool; caller treats rejection as
  // retryable and returns to JEV with the full registry again.
  async generateArguments(req: {
    tool: string;
    schema?: unknown;
    objective?: unknown;
    node?: unknown;
    context?: unknown;
  }): Promise<NeedleRouteResult> {
    if (!req.tool) throw new Error("generateArguments: tool must be a nonempty tool name");
    const r = await this.call({ action: "arguments.generate", ...req });
    validateNeedleTool(req.tool, r.tool);
    return r;
  }
  async close(): Promise<void> {
    const child = this.child;
    this.child = null;
    this.failAll(new Error("needle router closed"));
    if (!child || child.exitCode !== null) return;
    child.kill();
    try {
      await once(child, "exit");
    } catch {
      // Already gone; nothing to wait for.
    }
  }
}

export const needleRouter = new NeedleRouter();
