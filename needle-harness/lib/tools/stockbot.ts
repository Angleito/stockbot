import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { makeEvidence } from "../agent/evidence";
import type { ToolResult } from "../agent/types";

const ROOT = process.cwd().endsWith("needle-harness")
  ? process.cwd().replace(/\/needle-harness$/, "")
  : process.cwd();
const BRIDGE_CMD = `${ROOT}/venv/bin/python`;
const BRIDGE_ARGS = [`${ROOT}/scripts/tool_bridge.py`];

export type BridgeReply = {
  id?: unknown;
  result?: { content?: unknown; error?: unknown };
  error?: unknown;
};

type Pending = {
  resolve: (v: BridgeReply) => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};

// Persistent runtime-neutral tool bridge; replies correlate by id.
class StockbotBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private buf = "";
  private nextId = 0;
  private pending = new Map<string, Pending>();
  private bridgeStderrTail = "";

  private spawnChild(): ChildProcessWithoutNullStreams {
    const child = spawn(BRIDGE_CMD, BRIDGE_ARGS, { cwd: ROOT, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    this.buf = "";
    child.stdout.on("data", (chunk: Buffer) => this.onData(chunk.toString("utf8")));
    child.stderr.on("data", (chunk: Buffer) => {
      this.bridgeStderrTail = (this.bridgeStderrTail + chunk.toString("utf8")).slice(-2000);
    });
    child.on("exit", () => {
      if (this.child === child) this.child = null;
      for (const [, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error(`stockbot bridge exited; stderr tail: ${this.bridgeStderrTail || "(empty)"}`));
      }
      this.pending.clear();
    });
    return child;
  }

  private ensure(): ChildProcessWithoutNullStreams {
    if (this.child && this.child.exitCode === null) return this.child;
    return this.spawnChild();
  }

  private onData(chunk: string): void {
    this.buf += chunk;
    let i: number;
    while ((i = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, i).trim();
      this.buf = this.buf.slice(i + 1);
      if (!line) continue;
      let msg: unknown;
      try {
        msg = JSON.parse(line) as unknown;
      } catch {
        continue;
      }
      if (!msg || typeof msg !== "object" || !("id" in msg)) continue;
      const reply = msg as BridgeReply;
      if (typeof reply.id !== "string") continue;
      const p = this.pending.get(reply.id);
      if (!p) continue;
      this.pending.delete(reply.id);
      clearTimeout(p.timer);
      p.resolve(reply);
    }
  }

  call(req: Record<string, unknown>, timeoutMs = 120_000): Promise<BridgeReply> {
    const child = this.ensure();
    const id = `sb-${(this.nextId += 1)}`;
    return new Promise<BridgeReply>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`stockbot bridge timeout; stderr tail: ${this.bridgeStderrTail || "(empty)"}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      child.stdin.write(JSON.stringify({ id, ...req }) + "\n", (err) => {
        if (err) {
          const p = this.pending.get(id);
          if (p) {
            this.pending.delete(id);
            clearTimeout(p.timer);
            p.reject(err);
          }
        }
      });
    });
  }

  close(): void {
    const child = this.child;
    this.child = null;
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new Error("stockbot bridge closed"));
    }
    this.pending.clear();
    try {
      child?.kill();
    } catch {
      // Already gone; nothing to kill.
    }
  }
}

const bridge = new StockbotBridge();

export async function invoke(name: string, args: Record<string, unknown>, sessionId: string): Promise<ToolResult> {
  const msg = await bridge.call({ op: "tool.invoke", name, arguments: args, session_id: sessionId });
  if (msg.result && typeof msg.result === "object" && typeof msg.result.error === "string" && msg.result.error) {
    return { ok: false, error: msg.result.error };
  }
  if (msg.error !== undefined && msg.error !== null && msg.error !== "") {
    return { ok: false, error: typeof msg.error === "string" ? msg.error : JSON.stringify(msg.error) };
  }
  const result = msg.result ?? {};
  const content =
    "content" in result && typeof result.content === "string"
      ? result.content
      : JSON.stringify("content" in result ? result.content ?? result : result).slice(0, 8000);
  return {
    ok: true,
    evidence: makeEvidence(name, content.slice(0, 8000), { title: name }),
  };
}

export async function endSession(sessionId: string): Promise<void> {
  try {
    await bridge.call({ op: "tool.session.end", session_id: sessionId }, 10_000);
  } catch {
    // Best-effort; loop finally awaits directly and must never throw.
  }
}

export function closeBridge(): void {
  bridge.close();
}

export function newSessionId(): string {
  return randomUUID();
}
