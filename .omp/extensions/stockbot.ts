/** Stockbot Pi extension: RESEARCH-only tools via scripts/pi_bridge.py.
 *
 * Single source of truth stays in Python (app/tools.py TOOLS); tool schemas
 * pass through as raw JSON documents, and the system prompt comes from
 * the bridge `describe` response (app/prompts.py PI_RESEARCH_PROMPT) plus
 * the raw `.omp/thesis-workflow.yaml` thesis workflow text appended at startup.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { readFileSync, writeFileSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext, Theme, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import { type Advance, advanceOnAgentEnd, blockReasonForRun, clearResearchRun, planTaskCall, recordTaskResult, researchContextForRun, resumeResearch, setResearchBridge, startResearch } from "../lib/research-director.ts";
import { TASK_SUBAGENT_LIFECYCLE_CHANNEL, type SubagentLifecyclePayload } from "@oh-my-pi/pi-coding-agent/task";
import { registerYoutubeAnalytics } from "../lib/youtube-analytics.ts";
import { Text, type AutocompleteProvider } from "@oh-my-pi/pi-tui";

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
 researchContext?: { sessionId: string; jobId?: string },
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
 if (researchContext?.sessionId) req.active_research_session_id = researchContext.sessionId;
 if (researchContext?.jobId) req.active_research_job_id = researchContext.jobId;
 return req;
}

export function bridgeModelText(bridge: Json): string {
 const result = bridge.result;
 if (result && typeof result === "object") {
  // Tool results carry prose only. The session's final answer is rendered once
  // by the driver (renderFinalAnswer) at agent_end; the research_finalize card
  // stays a short confirmation, never a second copy of the answer.
  const content = (result as Json).content;
  if (typeof content === "string" && content) return content;
 }
 return JSON.stringify(bridge);
}
// Stable visible grammar: the model permanently sees only DISCOVERY_TOOLS
// plus pre-existing host tools. Every other registered research schema stays
// registered passthrough but inactive behind call_tool; the roster is never
// mutated after discovery.
export const DISCOVERY_TOOLS = ["browse_tools", "call_tool", "search_tools"];

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
): { callBridge: (req: Json, timeoutMs?: number, fatal?: boolean) => Promise<Json>; close: () => void } {
 // --- bridge process (JSONL stdio; ID-correlated, 4 concurrent tool calls) ---
 let proc: ChildProcessWithoutNullStreams | null = null;
 let buf = "";
 const pending = new Map<string, { resolve: (line: string | null) => void; timer: ReturnType<typeof setTimeout> }>();
 let writeChain: Promise<unknown> = Promise.resolve();
 let permits = 4;
 const permitQueue: Array<() => void> = [];
 const terminatedRuns = new Map<string, Promise<void>>();
 const terminatedMessages = new Map<string, string>();
 let closed = false;

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
 function close(): void {
  if (closed) return;
  closed = true;
  const child = proc;
  if (child) recycle(child);
  permits = 4;
  const queued = permitQueue.splice(0);
  for (const resume of queued) resume();
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
  if (closed) return false;
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

 return { callBridge, close };
}
// Module-level Director identity: factory re-runs per child session (fresh
// eventBus, fresh ExtensionAPI, fresh runner per session), so each binding gets
// its own closure. Identity must live here: the first session id seen on
// session_start wins. String id (not object identity): getSessionId is stable
// across moveTo; a wrapped/cloned manager would break ===.
let mainSessionId: string | null = null;
export function resetMainSessionIdentity(): void {
 mainSessionId = null;
}
function sessionIdOf(ctx: ExtensionContext | undefined | null): string | null {
 try {
  const mgr: unknown = ctx?.sessionManager;
  if (mgr && typeof mgr === "object") {
   if ("getSessionId" in mgr && typeof mgr.getSessionId === "function") {
    const id: unknown = mgr.getSessionId();
    if (typeof id === "string" && id) return id;
   }
   // Test doubles carry a bare string id; production always has getSessionId().
   if ("id" in mgr && typeof mgr.id === "string" && mgr.id) return mgr.id;
  }
 } catch {
  return null;
 }
 return null;
}
function isMainSession(ctx: ExtensionContext): boolean {
 // Manager-less contexts (tests, non-session callers) keep legacy behavior.
 const id = sessionIdOf(ctx);
 if (!id) return true;
 if (mainSessionId === null) {
  mainSessionId = id;
  return true;
 }
 return mainSessionId === id;
}
// Slash-menu filter: core "/" rows disabled via settings still list with a
// "disabled in settings" description; hide those rows so users never pick a
// dead command. Disabled behavior itself is untouched — only the listing.
function isDisabledInSettings(description: unknown): boolean {
 return typeof description === "string" && /disabled in settings/i.test(description);
}
// Bare "/" scores every command equally; pin our single entry point first
// without touching typed-prefix match order.
function pinResearchFirst<T extends { value: string }>(items: T[], prefix: string): T[] {
 if (!/^\s*\/$/.test(prefix)) return items;
 const idx = items.findIndex((item) => item.value === "research");
 if (idx <= 0) return items;
 const found = items[idx];
 if (found === undefined) return items;
 return [found, ...items.slice(0, idx), ...items.slice(idx + 1)];
}
// Items without a string description are always kept.
// Wrap the current autocomplete provider, dropping disabled-in-settings rows.
// Every other member stays a bound passthrough (never spread: a class
// instance may hold #private fields that spread would drop).
function wrapAutocompleteProvider(current: AutocompleteProvider): AutocompleteProvider {
 return new Proxy(current, {
  get(target, prop, _receiver) {
   if (prop === "getSuggestions") {
    return async (...args: Parameters<AutocompleteProvider["getSuggestions"]>) => {
     const result = await target.getSuggestions(...args);
     if (result === null) return null;
     const items = result.items.filter((item) => !isDisabledInSettings(item.description));
     return { prefix: result.prefix, items: pinResearchFirst(items, result.prefix) };
    };
   }
   if (prop === "trySyncSlashCompletion") {
    const sync = target.trySyncSlashCompletion;
    if (typeof sync !== "function") return undefined;
    return (textBeforeCursor: string) => {
     const result = sync.call(target, textBeforeCursor);
     if (result === null) return null;
     const items = result.items.filter((item) => !isDisabledInSettings(item.description));
     if (items.length === 0) return null;
     return { prefix: result.prefix, items: pinResearchFirst(items, result.prefix) };
    };
   }
   const value = Reflect.get(target, prop, target);
   if (typeof value === "function") return value.bind(target);
   return value;
  },
 });
}

export default async function stockbotExtension(pi: ExtensionAPI, spawnBridge?: () => ChildProcessWithoutNullStreams) {
 registerYoutubeAnalytics(pi);
 // Per-binding bridge process: every session (main + each child) re-runs this
 // factory, so each binding owns a private callBridge closure. The Director
 // seam is module-global: only the main binding claims it, on first main
 // session_start — a child overwrite would reroute Director RPCs mid-turn.
 // Tests calling handlers without session_start must fire main session_start
 // first (prod always does before any command or task frame).
 const { callBridge, close } = createBridgeClient(spawnBridge ?? spawnPythonBridge);
 let bridgeClaimed = false;
 const claimBridgeForMain = (): void => {
  if (bridgeClaimed) return;
  bridgeClaimed = true;
  setResearchBridge((req) => callBridge(req));
 };
 // (lifecycle subscription lives below emit/runId/seq init: subscribing at
 // factory top would close over runId/seq in TDZ during the describe/doctor
 // handshake, throwing ReferenceError on any early frame.)

 // --- describe: prompt + RESEARCH tool registry ---
 const [describe, doctor] = await Promise.all([callBridge({ op: "describe" }, 30_000), callBridge({ op: "doctor" }, 30_000)]);
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
 } else if (typeof doctor.error === "string" || doctor.bridge_ok !== true || doctor.tool_count !== entries.length) {
  bridgeDown = true;
  bridgeDetail = typeof doctor.error === "string" ? doctor.error : "doctor not ok";
  console.error(`[stockbot] bridge doctor failed: ${bridgeDetail}`);
 }
 const registeredResearch = new Set<string>();
 for (const entry of bridgeDown ? [] : entries) {
  const fn = describeFn(entry);
  if (!fn) continue;
  registeredResearch.add(fn.name);
 }

 for (const entry of bridgeDown ? [] : entries) {
  const fn = describeFn(entry);
  if (!fn || !registeredResearch.has(fn.name)) continue;
  const cardSpec = CARD_TOOLS[fn.name];
  const isDiscoveryTool = (DISCOVERY_TOOLS as string[]).includes(fn.name);
  const isDirectTool = !isDiscoveryTool && fn.name !== "call_tool";
  pi.registerTool({
   name: fn.name,
   label: fn.name,
   description: fn.description,
   parameters: fn.parameters as ToolDefinition["parameters"],
   async execute(_toolCallId: string, params: unknown) {
    toolCalls++;
    if (fn.name === "browse_tools" || fn.name === "search_tools") routing.discoveryCalls++;
    if (fn.name === "call_tool") routing.callToolCount++;
    if (isDirectTool) routing.directToolCalls++;
    // call_tool research target: params.name when it names a registered
    // non-discovery tool; otherwise this call is not a research attempt.
    const callTarget = fn.name === "call_tool" && typeof (params as Json).name === "string" ? ((params as Json).name as string) : undefined;
    const researchTarget = isDirectTool ? fn.name : callTarget && registeredResearch.has(callTarget) && !(DISCOVERY_TOOLS as string[]).includes(callTarget) ? callTarget : undefined;
    if (researchTarget) {
     routing.researchCalls++;
     routing.researchToolNames.push(researchTarget);
    }
    // Transparent query assistance: ticker-only/short searches cannot rank.
    // Native full-question passes stay untouched; assisted calls are tagged
    // in the returned text and counted separately (bridge only persists
    // routing_metrics/agent tool rows, so no separate event is emitted).
    let effParams = (params ?? {}) as Json;
    let searchAssisted = false;
    let searchOriginalQuery = "";
    if (fn.name === "search_tools") {
     const bag = params as Json;
     const q: unknown = typeof bag === "object" && bag !== null && "query" in bag ? bag.query : undefined;
     const stored = runQuestions.get(runId) ?? "";
     if (isShortQuery(q) && stored.length > q.trim().length + 10) {
      searchAssisted = true;
      searchOriginalQuery = q.trim();
      effParams = { ...(bag as Record<string, unknown>), query: stored };
      routing.assistedSearchCalls++;
     }
    }
    const bridge = await callBridge(toolCallRequest(crypto.randomUUID(), runId, _toolCallId, fn.name, effParams, 0, dataRoots.get(runId), asOfs.get(runId), researchContextForRun(runId)));
    const inner = bridge.result && typeof bridge.result === "object" ? (bridge.result as Json) : undefined;
    const failed = typeof bridge.error === "string" || (inner !== undefined && typeof inner.error === "string");
    const invalid = inner !== undefined && (inner.error_type === "unknown_tool" || inner.error_type === "invalid_tool_arguments");
    if (researchTarget) {
     if (failed) routing.failedResearchCalls++;
     if (invalid) {
      routing.invalidToolCalls++;
      routing.invalidToolNames.push(researchTarget);
     }
     if (!failed && !invalid) routing.successfulResearchToolNames.push(researchTarget);
    } else if (invalid) {
     routing.invalidToolCalls++;
     const suspect = fn.name === "call_tool" && typeof (params as Json).name === "string" ? ((params as Json).name as string) : fn.name;
     routing.invalidToolNames.push(suspect);
    }
    if (fn.name === "search_tools" && !failed && !invalid) {
     const meta = inner !== undefined && inner.meta && typeof inner.meta === "object" ? (inner.meta as Json) : undefined;
     const rawMatches = meta !== undefined && Array.isArray(meta.matches) ? (meta.matches as unknown[]) : [];
     const matches = rawMatches
      .map((m) => (typeof m === "string" ? m : m && typeof m === "object" ? ((m as Json).name as unknown) : undefined))
      .filter((n): n is string => typeof n === "string" && registeredResearch.has(n));
     for (const m of matches) routing.discoveredTools.add(m);
     if (searchAssisted) for (const m of matches) routing.assistedDiscoveredTools.push(m);
     routing.discoveryHadMatches = matches.length > 0;
    }
    if (fn.name === "browse_tools" && !failed && !invalid) {
     const meta = inner !== undefined && inner.meta && typeof inner.meta === "object" ? (inner.meta as Json) : undefined;
     const rawTools = meta !== undefined && Array.isArray(meta.tools) ? (meta.tools as unknown[]) : [];
     const surfaced = rawTools
      .map((m) => (typeof m === "string" ? m : m && typeof m === "object" ? ((m as Json).name as unknown) : undefined))
      .filter((n): n is string => typeof n === "string" && registeredResearch.has(n));
     for (const m of surfaced) routing.discoveredTools.add(m);
     if (surfaced.length > 0) routing.discoveryHadMatches = true;
    }
    refreshStatus(lastCtx);
    return {
     content: [{ type: "text", text: bridgeModelText(bridge) + (searchAssisted ? `\n\nStockbot note: short search query "${searchOriginalQuery}" was expanded with the session question for this call (assisted search; native passes stay unassisted).` : "") }],
     details: bridge,
    };
   },
   renderCall: cardSpec
    ? (args: unknown, _options: unknown, theme: Theme) => {
     // The host validates params against the schema before render.
     const bag: Json = args as Json;
     return card(
      cardSpec.title,
      cardSpec.argKeys.map((k) => `${k}=${short(bag[k])}`),
      theme,
     );
    }
    : undefined,
   renderResult: cardSpec
    ? (result, _opts, theme, _args) => {
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
 const WORKFLOW_PATH = `${ROOT}/.omp/thesis-workflow.yaml`;
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
 pi.on("before_agent_start", async (event, ctx) => {
  // Child sessions run their agent-definition prompts; only the Director gets the research prompt.
  if (!isMainSession(ctx)) return;
  // Independent prompt boundary: reset routing. A queued routing
  // continuation reuses its state instead.
  if (!routing.continuationPending) {
   resetRouting();
  }
  pendingQuestion = event.prompt;
  if (bridgeDown)
   return {
    systemPrompt: [
     `Stockbot research tools are unavailable (${bridgeDetail}). ` +
     "Decline investment-research questions as tool-unavailable; do not answer from model knowledge.",
    ],
   };
  if (workflowDown)
   return {
    systemPrompt: [
     `Stockbot thesis workflow is unavailable (${workflowDetail}). ` +
     "Decline thesis work until the workflow file is restored; do not answer from model knowledge.",
    ],
   };
  // OMP replaces policy as a string[]; one entry keeps the exact Pi text.
  if (systemPrompt) return { systemPrompt: [systemPrompt + "\n\n" + workflowText] };
 });

 // --- RESEARCH gate: registered tools only, and only while active ---
 // Inactive direct schemas stay uncallable (hallucinated direct calls
 // block); call_tool remains the active fallback. Non-research host tools
 // keep the pre-existing RESEARCH-only block.
 pi.on("tool_call", async (event, ctx) => {
  // Main task interception first: the Director owns every main-session spawn
  // (stage-gated sec-agent/trio). sec-agent -> sec-scout fan-out runs inside
  // the sec-agent child session, which passes through untouched; the kernel
  // spawn policy still enforces there.
  if (event.toolName === "task") {
   if (!isMainSession(ctx)) return;
   try {
    const taskInput = (((event as unknown as Json).input ?? {}) as Json);
    const plan = await planTaskCall({ researchKey: runId, toolCallId: event.toolCallId }, taskInput, dataRoots.get(runId), asOfs.get(runId));
    if (plan.block) {
     emit({ event: "security_block", tool: event.toolName, reason: plan.reason ?? "task spawn refused" });
     blocks++;
     return { block: true, reason: plan.reason ?? "Stockbot task gate: spawn refused" };
    }
    await emit({ event: "task_planned", tool: event.toolName, tool_call_id: event.toolCallId });
    if (plan.input) return { input: plan.input as Record<string, unknown> };
    return;
   } catch (err) {
    const reason = `Stockbot task gate failed (${err instanceof Error ? err.message : String(err)})`;
    emit({ event: "security_block", tool: event.toolName, reason });
    blocks++;
    return { block: true, reason };
   }
  }
  // Director-only gate for everything else: the main session owns research
  // routing, prompt policy, and roster. Child bindings run agent-definition
  // prompts with their own tools (research_read/research_add_evidence) and
  // must never hit the RESEARCH roster or per-binding routing counters.
  if (!isMainSession(ctx)) return;
  let isActive = false;
  try {
   isActive = pi.getActiveTools().includes(event.toolName);
  } catch {
   isActive = false;
  }
  if (!registeredResearch.has(event.toolName) || !isActive) {
   emit({ event: "security_block", tool: event.toolName, reason: "not a RESEARCH tool" });
   blocks++;
   return { block: true, reason: `Stockbot RESEARCH-only: '${event.toolName}' is not enabled` };
  }
  // Stage gate (UX-only; the kernel gate is authoritative): staged research runs
  // may only use stage-appropriate tools. Unknown stages fail open.
  try {
   const rawArgs: unknown = (event as unknown as Json).input;
   const inner: unknown = event.toolName === "call_tool" && rawArgs && typeof rawArgs === "object" ? (rawArgs as Json).name : undefined;
   const target = typeof inner === "string" && inner ? inner : event.toolName;
   const reason = blockReasonForRun(runId, target);
   if (reason) {
    emit({ event: "security_block", tool: target, reason });
    blocks++;
    return { block: true, reason };
   }
  } catch {
   // fail open; the kernel gate still enforces.
  }
 });
 pi.on("tool_result", async (event, ctx) => {
  if (!isMainSession(ctx)) return;
  if (event.toolName !== "task") return;
  try {
   await recordTaskResult({ researchKey: runId, toolCallId: event.toolCallId }, ((event.details ?? {}) as unknown as Json), dataRoots.get(runId), asOfs.get(runId));
   await emit({ event: "task_result", tool: event.toolName, tool_call_id: event.toolCallId, is_error: event.isError });
  } catch (err) {
   console.error(`[stockbot] task result recording failed: ${err instanceof Error ? err.message : String(err)}`);
  }
 });

 // --- lifecycle forwarding (step 8) + status pane (step 9) ---
 // run_id per agent turn-chain, monotonic sequence; drops if bridge down.
 let runId: string = crypto.randomUUID();
 let pendingQuestion = "";
 const runQuestions = new Map<string, string>();
 function baseQuestion(prompt: string): string {
  // Harness natural prompts append routing guidance after the user wording;
  // direct runs carry the bare question. Either way the leading text is the
  // user's own question.
  const i = prompt.indexOf(" To answer,");
  return (i < 0 ? prompt : prompt.slice(0, i)).trim();
 }
 function isShortQuery(q: unknown): q is string {
  if (typeof q !== "string" || !q.trim()) return false;
  return q.trim().split(/\s+/).length <= 2;
 }
 const dataRoots = new Map<string, string>();
 const stagedResearchRuns = new Set<string>();
 const doneFiles = new Map<string, string>();
 const asOfs = new Map<string, string>();
 let seq = 0;
 let turns = 0;
 let toolCalls = 0;
 let blocks = 0;
 let lastCtx: ExtensionContext | null = null;
 // Non-Stockbot tools active in the host: preserved across activation slices.
 let hostTools: string[] = [];
 // One request's discover -> execute -> answer tracking, reset per prompt.
 const routing = {
  discoveredTools: new Set<string>(),
  activeDiscoveredTools: [] as string[],
  researchToolNames: [] as string[],
  successfulResearchToolNames: [] as string[],
  invalidToolNames: [] as string[],
  discoveryCalls: 0,
  researchCalls: 0,
  failedResearchCalls: 0,
  invalidToolCalls: 0,
  callToolCount: 0,
  directToolCalls: 0,
  assistedSearchCalls: 0,
  assistedDiscoveredTools: [] as string[],
  continuationInjected: false,
  continuationPending: false,
  discoveryHadMatches: false,
  prematureStopDetected: false,
 };
 function resetRouting(): void {
  routing.discoveredTools.clear();
  routing.activeDiscoveredTools = [];
  routing.researchToolNames = [];
  routing.successfulResearchToolNames = [];
  routing.invalidToolNames = [];
  routing.discoveryCalls = 0;
  routing.researchCalls = 0;
  routing.failedResearchCalls = 0;
  routing.invalidToolCalls = 0;
  routing.callToolCount = 0;
  routing.directToolCalls = 0;
  routing.assistedSearchCalls = 0;
  routing.assistedDiscoveredTools = [];
  routing.continuationInjected = false;
  routing.continuationPending = false;
  routing.discoveryHadMatches = false;
  routing.prematureStopDetected = false;
 }

 const toolStartedAt = new Map<string, string>();
 function emit(payload: Json): Promise<Json> {
  // ID-correlated like tool calls; never fatal (observability never breaks research).
  return callBridge(
   { id: crypto.randomUUID(), op: "pi_event", run_id: runId, sequence: seq++, ...payload },
   10_000,
   false,
  ).catch(() => ({ error: "bridge_unavailable" }));
 }
 // OMP lifecycle -> kernel trace: task spawns publish on the session bus, so a
 // started frame names the child before its tool_result lands and a settled
 // frame closes it. Each binding subscribes its own bus; the handler emits via
 // this binding's runId. Synchronous spawns also emit tool_result, which stays
 // the authoritative per-job record; unknown payloads are ignored.
 const unsubLifecycle = pi.events?.on(TASK_SUBAGENT_LIFECYCLE_CHANNEL, (raw: unknown) => {
  const payload = (raw ?? {}) as Partial<SubagentLifecyclePayload>;
  if (typeof payload.id !== "string" || !payload.id) return;
  if (typeof payload.agent !== "string" || !payload.agent) return;
  if (payload.status === "started") {
   void emit({ event: "subagent_started", runtime_id: payload.id, agent: payload.agent, parent_tool_call_id: payload.parentToolCallId, session_file: payload.sessionFile });
   return;
  }
  if (payload.status === "completed" || payload.status === "failed" || payload.status === "aborted") {
   void emit({ event: "subagent_finished", runtime_id: payload.id, agent: payload.agent, status: payload.status, parent_tool_call_id: payload.parentToolCallId, session_file: payload.sessionFile });
  }
 });
 pi.on("session_shutdown", () => {
  // Each binding closes only its own bridge proc; createBridgeClient.close is
  // idempotent and per-binding, so a child shutdown cannot kill the parent.
  try {
   close();
  } catch {
   // already closed
  }
  try {
   unsubLifecycle?.();
  } catch {
   // already unsubscribed
  }
 });
 function refreshStatus(ctx: ExtensionContext | null) {
  if (!ctx) return;
  try {
   let activeResearch = 0;
   try {
    activeResearch = pi.getActiveTools().filter((n) => registeredResearch.has(n)).length;
   } catch {
    activeResearch = 0;
   }
   ctx.ui.setStatus(
    "stockbot",
    bridgeDown
     ? "stockbot · bridge unavailable (0 tools)"
     : `stockbot · ${registeredResearch.size} registered · ${activeResearch} research active · ${toolCalls} calls · ${blocks} blocked`,
   );
  } catch {
   // non-TUI modes without status: ignore
  }
 }

 let menuFilterInstalled = false;

 pi.on("session_start", async (_event, ctx) => {
  // Slash-menu filter first: hide core "/" rows disabled via settings. Once
  // per binding and independent of the Director gate below (every TUI session
  // gets it); headless/test contexts without ui stay a silent no-op.
  if (!menuFilterInstalled) {
   try {
    const ui = ctx?.ui;
    if (typeof ui?.addAutocompleteProvider === "function") {
     ui.addAutocompleteProvider((current) => wrapAutocompleteProvider(current));
     menuFilterInstalled = true;
    }
   } catch {
    // non-TUI modes without autocomplete: ignore
   }
  }
  // Director-only roster pin: child bindings share this module but own their
  // per-binding hostTools/lastCtx; only the first session pins the roster.
  // OMP applies activation asynchronously, so await it before refreshStatus.
  if (!isMainSession(ctx)) return;
  // First main session_start owns Director RPCs: claim the module seam now so
  // a later child factory re-run can never reroute it mid-turn.
  claimBridgeForMain();
  lastCtx = ctx;
  // Capture non-Stockbot host tools once and pin the stable visible grammar:
  // host tools plus the permanent discovery set. Direct research schemas stay
  // registered but inactive behind call_tool; the roster never changes after
  // discovery, keeping the provider prompt cache stable.
  hostTools = pi.getActiveTools().filter((n) => !registeredResearch.has(n));
  const permanent = DISCOVERY_TOOLS.filter((n) => registeredResearch.has(n));
  await pi.setActiveTools([...new Set([...hostTools, ...permanent])]);
  refreshStatus(lastCtx);
 });

 // --- operator slash commands ---
 // /research delegates to the staged ResearchDirector driver (internal create +
 // follow-up prompts); /research-status only phrases the tool call and lets the
 // agent run it. No policy or synthesis lives here.
 pi.registerCommand("research", {
  description: 'Start a persisted research session: /research <question> (staged driver: create, fetch, freeze, trio, finalize)',
  handler: async (args) => {
   const question = args.trim();
   if (!question) {
    await pi.sendMessage(
     {
      customType: "stockbot-research-command",
      content: 'Ask the operator for a research question, then call call_tool with name="research_start" and arguments={"question": ...}.',
      display: true,
     },
     { triggerTurn: true, deliverAs: "followUp" },
    );
    return;
   }
   // Establish the trusted run before staging: the follow-up turn's
   // agent_start must preserve (not rotate) this id and its data root.
   clearResearchRun(runId);
   stagedResearchRuns.delete(runId);
   const stagedId = (process.env.STOCKBOT_RUN_ID ?? "").trim();
   runId = stagedId ? stagedId : crypto.randomUUID();
   const stagedRoot = (process.env.STOCKBOT_DATA_DIR ?? "").trim();
   if (stagedRoot) dataRoots.set(runId, stagedRoot);
   else dataRoots.delete(runId);
   const stagedAsOf = (process.env.STOCKBOT_AS_OF ?? "").trim();
   if (stagedAsOf) asOfs.set(runId, stagedAsOf);
   else asOfs.delete(runId);
   stagedResearchRuns.add(runId);
   try {
    const { prompt } = await startResearch(question, runId, dataRoots.get(runId), asOfs.get(runId));
    await pi.sendMessage(
     { customType: "stockbot-research-command", content: prompt, display: true },
     { triggerTurn: true, deliverAs: "followUp" },
    );
   } catch (err) {
    await pi.sendMessage(
     {
      customType: "stockbot-research-command",
      content: `Research start failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only.`,
      display: true,
     },
     { triggerTurn: true, deliverAs: "followUp" },
    );
   }
  },
 });
 pi.registerCommand("research-status", {
  description: 'Show a research session: /research-status <session_id> (Call call_tool with name="research_status")',
  handler: async (args) => {
   const sessionId = args.trim();
   await pi.sendMessage(
    {
     customType: "stockbot-research-status-command",
     content: sessionId
      ? `Call call_tool with name="research_status" and arguments={"session_id": "${sessionId}"}.`
      : 'Ask the operator for a research session id, then call call_tool with name="research_status" and arguments={"session_id": ...}.',
     display: true,
    },
    { triggerTurn: true, deliverAs: "followUp" },
   );
  },
 });
 pi.registerCommand("research-resume", {
  description: 'Re-attach to a persisted research session: /research-resume <session_id> (staged driver resumes: fetch, trio, or final prompt)',
  handler: async (args) => {
   const sessionId = args.trim();
   if (!sessionId) {
    await pi.sendMessage(
     {
      customType: "stockbot-research-resume-command",
      content: 'Ask the operator for a research session id, then run /research-resume <session_id>.',
      display: true,
     },
     { triggerTurn: true, deliverAs: "followUp" },
    );
    return;
   }
   // Same trusted-run setup as /research: the follow-up turn's agent_start
   // must preserve (not rotate) this id and its data root.
   clearResearchRun(runId);
   stagedResearchRuns.delete(runId);
   const stagedId = (process.env.STOCKBOT_RUN_ID ?? "").trim();
   runId = stagedId ? stagedId : crypto.randomUUID();
   const stagedRoot = (process.env.STOCKBOT_DATA_DIR ?? "").trim();
   if (stagedRoot) dataRoots.set(runId, stagedRoot);
   else dataRoots.delete(runId);
   const stagedAsOf = (process.env.STOCKBOT_AS_OF ?? "").trim();
   if (stagedAsOf) asOfs.set(runId, stagedAsOf);
   else asOfs.delete(runId);
   stagedResearchRuns.add(runId);
   try {
    const { prompt } = await resumeResearch(sessionId, runId, dataRoots.get(runId), asOfs.get(runId));
    await pi.sendMessage(
     { customType: "stockbot-research-resume-command", content: prompt, display: true },
     { triggerTurn: true, deliverAs: "followUp" },
    );
   } catch (err) {
    await pi.sendMessage(
     {
      customType: "stockbot-research-resume-command",
      content: `Research resume failed (${err instanceof Error ? err.message : String(err)}). Reply with model text only.`,
      display: true,
     },
     { triggerTurn: true, deliverAs: "followUp" },
    );
   }
  },
 });

 pi.on("agent_start", () => {
  // A queued routing continuation reuses the same run, recorder/session,
  // active tools, and routing state instead of starting a second bridge run.
  if (routing.continuationPending) {
   routing.continuationPending = false;
   return;
  }
  resetRouting();
  if (!stagedResearchRuns.has(runId)) {
   clearResearchRun(runId);
   const trustedRunId = (process.env.STOCKBOT_RUN_ID ?? "").trim();
   runId = trustedRunId ? trustedRunId : crypto.randomUUID();
  }
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
  runQuestions.set(runId, baseQuestion(pendingQuestion));
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
    reasoning_tokens: num(usage.reasoningTokens),
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
  // One-shot routing continuation: discovery found tools but none ran.
  // Flags are set before queueing so re-entry cannot loop. The first end
  // returns without bridge teardown; the continuation turn reuses the run.
  if (routing.discoveryHadMatches && routing.researchCalls === 0 && !routing.continuationInjected) {
   routing.prematureStopDetected = true;
   routing.continuationInjected = true;
   routing.continuationPending = true;
   const candidates = [...routing.discoveredTools];
   await emit({ event: "routing_continuation", reason: "discovery_without_research", discovered_tools: candidates, continuation_number: 1 });
   const content =
    "Stockbot routing continuation:\n\nTool discovery found relevant research tools, but no research tool has been executed yet.\n\nAvailable candidates:\n" +
    candidates.map((c) => `- ${c}`).join("\n") +
    "\n\nUse one of the discovered tools now. Discovery results are not evidence and are not a completed answer.\n\nDo not explain which tool you intend to call. Call it.\n\nIf none of the discovered tools can actually answer the request, state that after evaluating them.";
   try {
    pi.sendMessage(
     { customType: "stockbot-routing-continuation", content, display: false, details: { discovered_tools: candidates, continuation_number: 1 } },
     { triggerTurn: true, deliverAs: "followUp" },
    );
    return;
   } catch {
    routing.continuationPending = false;
    await emit({ event: "routing_continuation_failed" });
   }
  }
  const messages = Array.isArray(event.messages) ? (event.messages as unknown as Json[]) : [];
  const assistants = messages.filter((m) => m.role === "assistant");
  const last = assistants[assistants.length - 1] as Json | undefined;
  const blocks = last && Array.isArray(last.content) ? (last.content as Json[]) : [];
  let answer = blocks
   .filter((b) => b.type === "text" && typeof b.text === "string")
   .map((b) => b.text as string)
   .join("\n");
  // Staged ResearchDirector: null when no /research run is staged for this
  // run id, so non-driver turns fall through untouched. A stage prompt queues
  // one follow-up turn (mirroring the routing continuation above); completion
  // falls through with the authoritative kernel answer.
  let driver: Advance = null;
  try {
   driver = await advanceOnAgentEnd(runId, answer, dataRoots.get(runId), asOfs.get(runId));
  } catch (err) {
   console.error(`[stockbot] research director advance failed: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (driver && !driver.done) {
   try {
    pi.sendMessage(
     { customType: "stockbot-research-stage", content: driver.prompt, display: false },
     { triggerTurn: true, deliverAs: "followUp" },
    );
   } catch {
    // best-effort follow-up; the next turn re-drives the same stage
   }
   return;
  }
  // Single user-facing render: the staged driver renders the persisted
  // final_result exactly once (renderFinalAnswer) and its answer replaces the
  // model's chat text here. The finalize card is a short confirmation, so the
  // answer reaches the user/bridge/done-file once — never as a second render
  // alongside a "finalized" note. Non-research turns keep the model text.
  if (driver && driver.done && driver.answer) answer = driver.answer;
  const hasEvidence = routing.successfulResearchToolNames.length > 0;
  await emit({
   event: "routing_metrics",
   discovery_calls: routing.discoveryCalls,
   discovered_tool_count: routing.discoveredTools.size,
   discovered_tools: [...routing.discoveredTools],
   research_tools: [...routing.researchToolNames],
   successful_research_tools: [...routing.successfulResearchToolNames],
   invalid_tool_names: [...routing.invalidToolNames],
   research_calls: routing.researchCalls,
   failed_research_calls: routing.failedResearchCalls,
   call_tool_count: routing.callToolCount,
   direct_tool_calls: routing.directToolCalls,
   assisted_search_calls: routing.assistedSearchCalls,
   assisted_discovered_tools: [...routing.assistedDiscoveredTools],
   invalid_tool_calls: routing.invalidToolCalls,
   premature_stop_detected: routing.prematureStopDetected,
   continuation_injected: routing.continuationInjected,
   continuation_succeeded: routing.continuationInjected && hasEvidence,
   unrelated_research_calls:
    routing.discoveredTools.size === 0
     ? 0
     : routing.successfulResearchToolNames.filter((n) => !routing.discoveredTools.has(n)).length,
   final_answer_after_evidence: answer.trim().length > 0 && hasEvidence,
  });
  await emit({ event: "agent_end", status: "completed", answer });
  const doneFile = doneFiles.get(runId);
  if (doneFile) {
   try {
    writeFileSync(doneFile, JSON.stringify({ status: "completed", answer }));
   } catch {
    // observability never breaks research
   }
  }
  stagedResearchRuns.delete(runId);
  doneFiles.delete(runId);
  dataRoots.delete(runId);
  asOfs.delete(runId);
  runQuestions.delete(runId);
 });
}
