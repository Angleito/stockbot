/** Stockbot Pi extension: RESEARCH-only tools via scripts/pi_bridge.py.
 *
 * Single source of truth stays in Python (app/tools.py TOOLS); tool schemas
 * pass through untouched via Type.Unsafe, and the system prompt comes from
 * the bridge `describe` response (app/prompts.py PI_RESEARCH_PROMPT) plus
 * the raw `.pi/stockbot.yaml` thesis workflow text appended at startup.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { readFileSync, writeFileSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext, Theme } from "@earendil-works/pi-coding-agent";
import { registerYoutubeAnalytics } from "../lib/youtube-analytics.ts";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

export type Json = Record<string, unknown>;

export function toolCallRequest(
 id: string,
 runId: string,
 toolCallId: string,
 name: string,
 params: Json,
 bridgeQueueMs = 0,
 dataRoot?: string,
 asOf?: string,
): Json {
 const req: Json = {
  id,
  op: "tool_call",
  run_id: runId,
  tool_call_id: toolCallId,
  name,
  arguments: params,
  bridge_queue_ms: bridgeQueueMs,
 };
 if (dataRoot) req.data_root = dataRoot;
 if (asOf) req.as_of = asOf;
 return req;
}

export function bridgeModelText(bridge: Json): string {
 const result = bridge.result;
 if (result && typeof result === "object" && typeof (result as Json).content === "string") {
  return (result as Json).content as string;
 }
 return JSON.stringify(bridge);
}

// Absolute bridge paths derived from this file's location: Pi's extension
// host cwd is not the repo root, so relative venv/scripts paths ENOENT.
const ROOT = new URL("../..", import.meta.url).pathname;
const BRIDGE_CMD = `${ROOT}/venv/bin/python`;
const BRIDGE_ARGS = [`${ROOT}/scripts/pi_bridge.py`];
const TOOL_TIMEOUT_MS = 120_000;

// Custom tool cards (step 9). Tools without an entry use Pi's default raw
// JSON card. argKeys feed the call card; the result card shows PIT
// (as_of/known_at) + source labels when the payload carries them.
const CARD_TOOLS: Record<string, { title: string; argKeys: string[] }> = {
 get_fundamentals: { title: "Fundamentals", argKeys: ["ticker"] },
 get_filing_section: { title: "Filing section", argKeys: ["ticker", "form", "accession_number"] },
 get_recent_ownership_filings: { title: "Recent 13D/G", argKeys: ["form_type", "limit"] },
 get_financial_statements: { title: "Financial statements", argKeys: ["ticker", "form"] },
 get_xbrl_facts: { title: "XBRL facts", argKeys: ["ticker", "concept"] },
 get_short_interest: { title: "Short interest", argKeys: ["ticker"] },
 get_short_interest_leaderboard: { title: "Short leaderboard", argKeys: [] },
 get_valuation_metrics: { title: "Valuation", argKeys: ["ticker"] },
 search_web: { title: "Web search", argKeys: ["query"] },
 find_alternative_signals: { title: "Alt signals", argKeys: ["query"] },
 get_trend_evidence: { title: "Trend evidence", argKeys: ["geos"] },
 investigate_social_arbitrage_candidate: { title: "Arbitrage check", argKeys: ["term"] },
 get_macro_context: { title: "Macro context", argKeys: ["geos"] },
 search_company_patents: { title: "Patents", argKeys: ["company_id"] },
};

function short(value: unknown, max = 80): string {
 const s = typeof value === "string" ? value : JSON.stringify(value);
 return s.length > max ? `${s.slice(0, max)}…` : s;
}

// Shallow PIT/source scan: payload shapes vary per tool, so look at the
// top level plus one nesting level instead of per-tool parsers.
export function payloadMeta(details: unknown): { pit: string; sources: string } {
 let pit = "";
 let sources = "";
 // Bridge payload; narrow once, then read known keys.
 const top: Json = details && typeof details === "object" ? (details as Json) : {};
 const inner: Json =
  top.result && typeof top.result === "object" ? (top.result as Json) : top;
 const meta: Json =
  inner.meta && typeof inner.meta === "object" ? (inner.meta as Json) : {};
 for (const obj of [meta, inner, top]) {
  if (!pit && (typeof obj.as_of === "string" || typeof obj.known_at === "string")) {
   pit = String(obj.as_of ?? obj.known_at);
  }
  if (!sources && (obj.source_names ?? obj.sources ?? obj.source)) {
   sources = short(obj.source_names ?? obj.sources ?? obj.source);
  }
 }
 return { pit, sources };
}

function card(title: string, rows: string[], theme: Theme): Text {
 const head = theme.fg("toolTitle", theme.bold(`${title} `));
 return new Text([head, ...rows.map((r) => theme.fg("dim", r))].join("\n"), 0, 0);
}

function rawCard(details: unknown, theme: Theme): Text {
 return new Text(theme.fg("dim", short(details, 500)), 0, 0);
}

// Boundary: bridge `describe` entries are OpenAI-style {function:{...}}.
function describeFn(entry: unknown): { name: string; description: string; parameters: object } | undefined {
 if (!entry || typeof entry !== "object" || !("function" in entry)) return undefined;
 const fn: unknown = entry.function;
 if (!fn || typeof fn !== "object" || !("name" in fn) || typeof fn.name !== "string") {
  return undefined;
 }
 const description =
  "description" in fn && typeof fn.description === "string" ? fn.description : fn.name;
 const parameters =
  "parameters" in fn && fn.parameters && typeof fn.parameters === "object"
   ? (fn.parameters as object)
   : { type: "object" };
 return { name: fn.name, description, parameters };
}

function spawnPythonBridge(): ChildProcessWithoutNullStreams {
 return spawn(BRIDGE_CMD, BRIDGE_ARGS, {
  cwd: ROOT,
  stdio: ["pipe", "pipe", "pipe"],
 });
}

export function createBridgeClient(
 spawnBridge: () => ChildProcessWithoutNullStreams = spawnPythonBridge,
): { callBridge: (req: Json, timeoutMs?: number, fatal?: boolean) => Promise<Json> } {
 // --- bridge process (JSONL stdio; ID-correlated, 4 concurrent tool calls) ---
 let proc: ChildProcessWithoutNullStreams | null = null;
 let buf = "";
 const pending = new Map<string, { resolve: (line: string | null) => void; timer: ReturnType<typeof setTimeout> }>();
 let writeChain: Promise<unknown> = Promise.resolve();
 let permits = 4;
 const permitQueue: Array<() => void> = [];
 const terminatedRuns = new Map<string, Promise<void>>();
 const terminatedMessages = new Map<string, string>();

 function acquire(): Promise<number> {
  if (permits > 0) {
   permits--;
   return Promise.resolve(0);
  }
  const start = Date.now();
  return new Promise<number>((resolve) => {
   permitQueue.push(() => {
    permits--;
    resolve(Date.now() - start);
   });
  });
 }

 function release(): void {
  permits++;
  permitQueue.shift()?.();
 }

 function recycle(child: ChildProcessWithoutNullStreams): void {
  if (child !== proc) return;
  proc = null;
  buf = "";
  const entries = [...pending.values()];
  pending.clear();
  for (const entry of entries) {
   clearTimeout(entry.timer);
   entry.resolve(null);
  }
  writeChain = Promise.resolve();
  try {
   child.stdin.destroy();
  } catch {
   // already gone
  }
  try {
   child.kill("SIGKILL");
  } catch {
   // already exited
  }
 }

 function markDead() {
  proc = null;
  buf = "";
  writeChain = Promise.resolve();
  for (const [, entry] of pending) {
   clearTimeout(entry.timer);
   entry.resolve(null);
  }
  pending.clear();
 }

 function pump(child: ChildProcessWithoutNullStreams) {
  child.stdout.on("data", (chunk) => {
   if (child !== proc) return;
   buf += Buffer.from(chunk).toString("utf8");
   let i: number;
   while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let id: unknown;
    try {
     id = (JSON.parse(line) as Json).id;
    } catch {
     continue;
    }
    if (typeof id !== "string") continue;
    const entry = pending.get(id);
    if (!entry) continue;
    pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve(line);
   }
  });
  // Drain stderr so a chatty bridge can never block on a full pipe.
  child.stderr.on("data", () => {
   // discard
  });
  const dead = () => {
   if (proc === child) markDead();
  };
  child.on("close", dead);
  child.on("error", dead);
 }

 function ensureBridge(): boolean {
  if (proc) return true;
  try {
   proc = spawnBridge();
   buf = "";
   pump(proc);
   return true;
  } catch (err) {
   console.error(`[stockbot] bridge spawn failed: ${String(err)}`);
   markDead();
   return false;
  }
 }

 function writeLine(child: ChildProcessWithoutNullStreams, line: string): Promise<boolean> {
  const p = writeChain.then(async () => {
   try {
    const ok = child.stdin.write(`${line}\n`);
    if (!ok) await once(child.stdin, "drain");
    return true;
   } catch {
    return false;
   }
  });
  writeChain = p.catch(() => undefined);
  return p as Promise<boolean>;
 }

 async function callBridge(req: Json, timeoutMs = TOOL_TIMEOUT_MS, fatal = true): Promise<Json> {
  const fail = (reason: string): Json => {
   // Single loud surface for every fatal bridge failure; pi_event
   // passes fatal=false and stays quiet (observability never breaks research).
   if (fatal) console.error(`[stockbot] bridge ${String(req.op)} failed: ${reason}`);
   return { error: "bridge_unavailable" };
  };
  if (typeof req.id !== "string" || !req.id) req.id = crypto.randomUUID();
  const id = req.id as string;
  const isToolCall = req.op === "tool_call";
  const isAgentEnd = req.op === "pi_event" && req.event === "agent_end";
  if (isAgentEnd && typeof req.run_id === "string" && req.run_id && terminatedRuns.has(req.run_id)) {
   req.status = "failed";
   req.error_type = "tool_timeout";
   const stored = terminatedMessages.get(req.run_id);
   if (stored) req.error_message = stored;
  }
  if (isToolCall && typeof req.run_id === "string" && req.run_id && terminatedRuns.has(req.run_id)) {
   return { error: "run_terminated", error_type: "tool_timeout" };
  }
  let queuedMs = 0;
  let acquired = false;
  try {
   if (isToolCall) {
    queuedMs = await acquire();
    acquired = true;
    if (typeof req.run_id === "string" && req.run_id && terminatedRuns.has(req.run_id)) {
     return { error: "run_terminated", error_type: "tool_timeout" };
    }
    req.bridge_queue_ms = queuedMs;
   }
   if (!ensureBridge() || !proc) return fail("spawn failed");
   const child = proc;
   const { promise, resolve } = Promise.withResolvers<string | null>();
   let timedOut = false;
   let writeFailed = false;
   const timer = setTimeout(() => {
    if (pending.delete(id)) {
     timedOut = true;
     resolve(null);
    }
   }, timeoutMs);
   pending.set(id, { resolve, timer });
   void writeLine(child, JSON.stringify(req)).then((sent) => {
    if (!sent && pending.delete(id)) {
     writeFailed = true;
     clearTimeout(timer);
     resolve(null);
    }
   });
   const line = await promise;
   if (line === null) {
    if (writeFailed) {
     recycle(child);
     return fail("write failed");
    }
    if (timedOut) {
     if (isToolCall && fatal && typeof req.run_id === "string" && req.run_id) {
      const runId = req.run_id as string;
      const message = `Tool call timed out after ${timeoutMs}ms`;
      let term = terminatedRuns.get(runId);
      if (!term) {
       if (!terminatedMessages.has(runId)) terminatedMessages.set(runId, message);
       const stored = terminatedMessages.get(runId) as string;
       term = (async () => {
        const mkAbort = (): Json => ({ id: crypto.randomUUID(), op: "abort_run", run_id: runId, error_type: "tool_timeout", error_message: stored });
        let ack: Json | undefined;
        try {
         ack = await callBridge(mkAbort(), 6_000, false);
        } catch {
         ack = undefined;
        }
        try {
         recycle(child);
        } catch {
         // already gone
        }
        if (!ack || (ack as Json).finalized !== true) {
         try {
          await callBridge(mkAbort(), 6_000, false);
         } catch {
          // best-effort fallback
         }
        }
       })();
       terminatedRuns.set(runId, term);
      }
      try {
       await term;
      } catch {
       // best-effort termination never breaks the timeout result
      }
      return fail(`no response in ${timeoutMs}ms`);
     }
     if (fatal || isAgentEnd) recycle(child);
     return fail(`no response in ${timeoutMs}ms`);
    }
    return fail("bridge reset");
   }
   let parsed: Json;
   try {
    parsed = JSON.parse(line) as Json;
   } catch {
    return fail(`unparseable line: ${line.slice(0, 120)}`);
   }
   if (parsed.error === "tool_drain_timeout") {
    recycle(child);
    return fail("tool drain timeout");
   }
   return parsed;
  } finally {
   if (acquired) release();
  }
 }

 return { callBridge };
}

export default async function stockbotExtension(pi: ExtensionAPI) {
 registerYoutubeAnalytics(pi);
 const { callBridge } = createBridgeClient();

 // --- describe: prompt + RESEARCH tool registry ---
 const describe = await callBridge({ op: "describe" }, 30_000);
 const systemPrompt = typeof describe.system_prompt === "string" ? describe.system_prompt : "";
 const entries = Array.isArray(describe.tools) ? describe.tools : [];
 // Loud handshake: silent 0-tool mode is unreachable. Any describe/doctor
 // failure pins the status bar and turns the agent prompt into a refusal.
 let bridgeDown = false;
 let bridgeDetail = "";
 if (typeof describe.error === "string" || !systemPrompt || entries.length === 0) {
  bridgeDown = true;
  bridgeDetail = typeof describe.error === "string" ? describe.error : "empty prompt/tools";
  console.error(`[stockbot] bridge describe failed: ${bridgeDetail}`);
 } else {
  const doctor = await callBridge({ op: "doctor" }, 30_000);
  if (typeof doctor.error === "string" || doctor.bridge_ok !== true || doctor.tool_count !== entries.length) {
   bridgeDown = true;
   bridgeDetail = typeof doctor.error === "string" ? doctor.error : "doctor not ok";
   console.error(`[stockbot] bridge doctor failed: ${bridgeDetail}`);
  }
 }
 const research = new Set<string>();

for (const entry of bridgeDown ? [] : entries) {
 const fn = describeFn(entry);
 if (!fn) continue;
 research.add(fn.name);
 const cardSpec = CARD_TOOLS[fn.name];
 // Deferred loading: only search_tools carries prompt metadata (Pi rebuilds
 // the system prompt when an active tool carries it) and activates matches.
 const isSearch = fn.name === "search_tools";
 pi.registerTool({
  name: fn.name,
  label: fn.name,
  description: fn.description,
  parameters: Type.Unsafe(fn.parameters),
  ...(isSearch
   ? {
     promptSnippet: "Search for additional tools when the active tools cannot perform the task",
     promptGuidelines: ["Use search_tools when a task requires a capability that is not currently available."],
    }
   : {}),
  async execute(toolCallId, params) {
   toolCalls++;
   const bridge = await callBridge(toolCallRequest(crypto.randomUUID(), runId, toolCallId, fn.name, params as Json, 0, dataRoots.get(runId), asOfs.get(runId)));
   if (isSearch) {
   const inner = bridge.result && typeof bridge.result === "object" ? (bridge.result as Json) : {};
   const meta = inner.meta && typeof inner.meta === "object" ? (inner.meta as Json) : {};
   const raw = (meta.matches ?? inner.matches ?? bridge.matches ?? []) as unknown;
   const matches = (Array.isArray(raw) ? raw : [])
    .map((m) => (typeof m === "string" ? m : m && typeof m === "object" && typeof (m as Json).name === "string" ? ((m as Json).name as string) : ""))
    .filter((n) => research.has(n))
    .slice(0, 4);
    const active = pi.getActiveTools();
    const added = matches.filter((n) => !active.includes(n));
    if (added.length) pi.setActiveTools([...new Set([...active, ...added])]);
    const query = (params as Json).query;
    const text =
     matches.length === 0
      ? `No tools found for: ${typeof query === "string" && query ? query : fn.name}`
      : added.length
        ? `Activated ${added.length} tools: ${added.join(", ")}`
        : `Matching tools already active: ${matches.join(", ")}`;
    refreshStatus(lastCtx);
    return {
     content: [{ type: "text", text }],
     details: bridge,
    };
   }
   refreshStatus(lastCtx);
   return {
    content: [{ type: "text", text: bridgeModelText(bridge) }],
    details: bridge,
   };
  },
   renderCall: cardSpec
    ? (args, theme) => {
     // Pi validates params against the schema before render.
     const bag: Json = args as Json;
     return card(
      cardSpec.title,
      cardSpec.argKeys.map((k) => `${k}=${short(bag[k])}`),
      theme,
     );
    }
    : undefined,
   renderResult: cardSpec
    ? (result, _opts, theme) => {
     try {
      // Details are the bridge result object built in execute above.
      const details: Json =
       result.details && typeof result.details === "object"
        ? (result.details as Json)
        : {};
      const inner: Json =
       details.result && typeof details.result === "object"
        ? (details.result as Json)
        : details;
      if (typeof inner.error === "string") {
       return card(cardSpec.title, [`error: ${short(inner.error)}`], theme);
      }
      const { pit, sources } = payloadMeta(details);
      const rows = ["ok"];
      if (pit) rows.push(`as_of ${pit}`);
      if (sources) rows.push(`sources: ${sources}`);
      rows.push(`${JSON.stringify(details).length} bytes`);
      return card(cardSpec.title, rows, theme);
     } catch {
      return rawCard(result.details, theme);
     }
    }
    : undefined,
  });
 }

 // --- thesis workflow (raw text, never YAML-parsed in TS) ---
 const WORKFLOW_PATH = `${ROOT}/.pi/stockbot.yaml`;
 let workflowText = "";
 let workflowDown = false;
 let workflowDetail = "";
 try {
  workflowText = readFileSync(WORKFLOW_PATH, "utf-8");
  if (!workflowText.trim()) {
   workflowDown = true;
   workflowDetail = `empty workflow file ${WORKFLOW_PATH}`;
   console.error(`[stockbot] thesis workflow unreadable: ${workflowDetail}`);
  }
 } catch (err) {
  workflowDown = true;
  workflowDetail = `cannot read workflow file ${WORKFLOW_PATH}: ${err instanceof Error ? err.message : String(err)}`;
  console.error(`[stockbot] thesis workflow unreadable: ${workflowDetail}`);
 }

 // --- prompt replacement (coding prompt -> research prompt) ---
 pi.on("before_agent_start", async (event) => {
  pendingQuestion = event.prompt;
  if (bridgeDown)
   return {
    systemPrompt:
     `Stockbot research tools are unavailable (${bridgeDetail}). ` +
     "Decline investment-research questions as tool-unavailable; do not answer from model knowledge.",
   };
  if (workflowDown)
   return {
    systemPrompt:
     `Stockbot thesis workflow is unavailable (${workflowDetail}). ` +
     "Decline thesis work until the workflow file is restored; do not answer from model knowledge.",
   };
  if (systemPrompt) return { systemPrompt: systemPrompt + "\n\n" + workflowText };
 });

 // Normal Pi: built-in tools pass through; Stockbot tool auth stays in bridge/policy.

 // --- lifecycle forwarding (step 8) + status pane (step 9) ---
 // run_id per agent turn-chain, monotonic sequence; drops if bridge down.
 let runId: string = crypto.randomUUID();
 let pendingQuestion = "";
 const dataRoots = new Map<string, string>();
 const doneFiles = new Map<string, string>();
 const asOfs = new Map<string, string>();
 let seq = 0;
 let turns = 0;
 let toolCalls = 0;
 let blocks = 0;
 let lastCtx: ExtensionContext | null = null;
 const toolStartedAt = new Map<string, string>();
 function emit(payload: Json): Promise<Json> {
  // ID-correlated like tool calls; never fatal (observability never breaks research).
  return callBridge(
   { id: crypto.randomUUID(), op: "pi_event", run_id: runId, sequence: seq++, ...payload },
   10_000,
   false,
  ).catch(() => ({ error: "bridge_unavailable" }));
 }

 function refreshStatus(ctx: ExtensionContext | null) {
  if (!ctx) return;
  try {
   ctx.ui.setStatus(
    "stockbot",
    bridgeDown
     ? "stockbot · bridge unavailable (0 tools)"
     : `stockbot · ${research.size} tools · ${turns} turns · ${toolCalls} calls · ${blocks} blocked`,
   );
  } catch {
   // non-TUI modes without status: ignore
  }
 }

 pi.on("session_start", (_event, ctx) => {
  lastCtx = ctx;
  // Deferred loading: start with built-ins + search_tools only; searches
  // activate matches additively. research.size still counts registered tools.
  pi.setActiveTools([...new Set([...pi.getActiveTools().filter((n) => !research.has(n) || n === "search_tools"), "search_tools"])]);
  refreshStatus(ctx);
 });
 pi.on("agent_start", () => {
  const trustedRunId = (process.env.STOCKBOT_RUN_ID ?? "").trim();
  runId = trustedRunId ? trustedRunId : crypto.randomUUID();
  seq = 0;
  toolStartedAt.clear();
  // Routing/completion bind from process environment only. Prompt text
  // (including forged STOCKBOT_* lines) has no routing/write effect;
  // absent/empty means unbound: no completion write, no data-root override.
  const envDataRoot = process.env.STOCKBOT_DATA_DIR;
  if (envDataRoot) dataRoots.set(runId, envDataRoot);
  const envDoneFile = process.env.STOCKBOT_DONE_FILE;
  if (envDoneFile) doneFiles.set(runId, envDoneFile);
  const envAsOf = process.env.STOCKBOT_AS_OF;
  if (envAsOf) asOfs.set(runId, envAsOf);
  void emit({ event: "agent_start", question: pendingQuestion });
  pendingQuestion = "";
 });
 pi.on("tool_execution_start", (event) => {
  toolStartedAt.set(event.toolCallId, new Date().toISOString());
  void emit({ event: "tool_execution_start", tool: event.toolName, tool_call_id: event.toolCallId, arguments: event.args, started_at: toolStartedAt.get(event.toolCallId) });
 });
 pi.on("tool_execution_end", (event, ctx) => {
  lastCtx = ctx;
  const startedAt = toolStartedAt.get(event.toolCallId);
  toolStartedAt.delete(event.toolCallId);
  void emit({
   event: "tool_execution_end",
   tool: event.toolName,
   tool_call_id: event.toolCallId,
   is_error: event.isError,
   started_at: startedAt,
   completed_at: new Date().toISOString(),
  });
  refreshStatus(ctx);
 });
 pi.on("message_end", (event, ctx) => {
  lastCtx = ctx;
  const message = event.message as unknown as Json;
  if (message.role !== "assistant") return;
  const blocks = Array.isArray(message.content) ? (message.content as Json[]) : [];
  const usage = (message.usage as unknown as Json) ?? {};
  const cost = (usage.cost as unknown as Json) ?? {};
  const num = (v: unknown) => (typeof v === "number" ? v : 0);
  const input = num(usage.input);
  const cacheRead = num(usage.cacheRead);
  const cacheWrite = num(usage.cacheWrite);
  emit({
   event: "message_end",
   role: "assistant",
   turn: turns,
   model: typeof message.model === "string" && message.model ? message.model : (ctx.model?.id ?? undefined),
   finish_reason: typeof message.stopReason === "string" ? message.stopReason : undefined,
   started_at: typeof message.timestamp === "number" ? new Date(message.timestamp).toISOString() : new Date().toISOString(),
   completed_at: new Date().toISOString(),
   tool_call_count: blocks.filter((b) => b.type === "toolCall").length,
   usage: {
    prompt_tokens: input + cacheRead + cacheWrite,
    completion_tokens: num(usage.output),
    reasoning_tokens: num(usage.reasoning),
    prompt_tokens_details: { cached_tokens: cacheRead },
    total_tokens: num(usage.totalTokens),
    cost: num(cost.total),
   },
  });
 });
 pi.on("turn_end", (event, ctx) => {
  lastCtx = ctx;
  turns++;
  emit({ event: "turn_end", turn: event.turnIndex });
  refreshStatus(ctx);
 });
 pi.on("agent_end", async (event) => {
  const messages = Array.isArray(event.messages) ? (event.messages as unknown as Json[]) : [];
  const assistants = messages.filter((m) => m.role === "assistant");
  const last = assistants[assistants.length - 1] as Json | undefined;
  const blocks = last && Array.isArray(last.content) ? (last.content as Json[]) : [];
  const answer = blocks
   .filter((b) => b.type === "text" && typeof b.text === "string")
   .map((b) => b.text as string)
   .join("\n");
  await emit({ event: "agent_end", status: "completed", answer });
  const doneFile = doneFiles.get(runId);
  if (doneFile) {
   try {
    writeFileSync(doneFile, JSON.stringify({ status: "completed", answer }));
   } catch {
    // observability never breaks research
   }
  }
  doneFiles.delete(runId);
  dataRoots.delete(runId);
  asOfs.delete(runId);
 });
}
