import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import type { JSONSchema } from "../agent/types";

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

const ROOT = process.cwd();
const SERVER = `${ROOT}/lib/needle/server.py`;
const VENV_PYTHON = `${homedir()}/.cache/needle-harness/.needle/bin/python`;

export const TOOL_SCHEMAS: Record<string, JSONSchema> = {
  web_search: {
    type: "object",
    properties: {
      query: { type: "string" },
      limit: { type: "integer", minimum: 1, maximum: 5, default: 5 },
    },
    required: ["query"],
  },
  fetch_url: {
    type: "object",
    properties: { url: { type: "string" } },
    required: ["url"],
  },
  get_sec_filings: {
    type: "object",
    properties: {
      ticker: { type: "string", description: "Stock ticker symbol, e.g. NVDA (not the company name)" },
      forms: { type: "array", items: { type: "string" } },
      limit: { type: "integer", minimum: 1, maximum: 10, default: 5 },
    },
    required: ["ticker"],
  },
  get_current_time: { type: "object", properties: {} },
};

type Pending = {
  resolve: (v: NeedleRouteResult) => void;
  reject: (e: Error) => void;
  cancel: () => void;
};

function floor(): number {
  const raw = process.env.CONFIDENCE_FLOOR ?? process.env.NEEDLE_CONFIDENCE_FLOOR ?? "0.5";
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0.5;
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

  async route({ prompt, context }: { prompt: string; context: string }): Promise<NeedleDecision> {
    const child = this.ensure();
    if (!child.stdin) throw new Error("needle spawn failed");
    const id = String((this.nextId += 1));
    const result = await new Promise<NeedleRouteResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        delete this.pending[id];
        child.kill();
        this.child = null;
        reject(new Error(`needle route timeout; stderr tail: ${this.bridgeStderrTail || "(empty)"}`));
      }, TOOL_TIMEOUT_MS);
      this.pending[id] = { resolve, reject, cancel: () => clearTimeout(timer) };
      child.stdin?.write(JSON.stringify({ id, action: "route", prompt, context }) + "\n", (err) => {
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
    const f = floor();
    return {
      ...result,
      escalate: result.tool === null || (result.confidence !== null && result.confidence < f),
    };
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
