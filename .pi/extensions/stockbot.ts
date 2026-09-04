/** Stockbot Pi extension: RESEARCH-only tools via scripts/pi_bridge.py.
 *
 * Single source of truth stays in Python (app/tools.py TOOLS); tool schemas
 * pass through untouched via Type.Unsafe, and the system prompt comes from
 * the bridge `describe` response (app/prompts.py PI_RESEARCH_PROMPT).
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import type { ExtensionAPI, ExtensionContext, Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

type Json = Record<string, unknown>;

// Absolute bridge paths derived from this file's location: Pi's extension
// host cwd is not the repo root, so relative venv/scripts paths ENOENT.
const ROOT = new URL("../..", import.meta.url).pathname;
const BRIDGE_CMD = `${ROOT}/venv/bin/python`;
const BRIDGE_ARGS = [`${ROOT}/scripts/pi_bridge.py`];
const SESSION_ID = "pi";
const TOOL_TIMEOUT_MS = 120_000;

// Custom tool cards (step 9). Tools without an entry use Pi's default raw
// JSON card. argKeys feed the call card; the result card shows PIT
// (as_of/known_at) + source labels when the payload carries them.
const CARD_TOOLS: Record<string, { title: string; argKeys: string[] }> = {
	get_fundamentals: { title: "Fundamentals", argKeys: ["ticker"] },
	get_filing_section: { title: "Filing section", argKeys: ["ticker", "form", "accession_number"] },
	get_financial_statements: { title: "Financial statements", argKeys: ["ticker", "form"] },
	get_xbrl_facts: { title: "XBRL facts", argKeys: ["ticker", "concept"] },
	get_short_interest: { title: "Short interest", argKeys: ["ticker"] },
	get_short_interest_leaderboard: { title: "Short leaderboard", argKeys: [] },
	get_valuation_metrics: { title: "Valuation", argKeys: ["ticker"] },
	search_web: { title: "Web search", argKeys: ["query"] },
};

function short(value: unknown, max = 80): string {
	const s = typeof value === "string" ? value : JSON.stringify(value);
	return s.length > max ? `${s.slice(0, max)}…` : s;
}

// Shallow PIT/source scan: payload shapes vary per tool, so look at the
// top level plus one nesting level instead of per-tool parsers.
function payloadMeta(details: unknown): { pit: string; sources: string } {
	let pit = "";
	let sources = "";
	// Bridge payload; narrow once, then read known keys.
	const top: Json = details && typeof details === "object" ? (details as Json) : {};
	const inner: Json =
		top.result && typeof top.result === "object" ? (top.result as Json) : top;
	for (const obj of [inner, top]) {
		if (!pit && (typeof obj.as_of === "string" || typeof obj.known_at === "string")) {
			pit = String(obj.as_of ?? obj.known_at);
		}
		if (!sources && (obj.source_names ?? obj.sources)) {
			sources = short(obj.source_names ?? obj.sources);
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

export default async function stockbotExtension(pi: ExtensionAPI) {
	// --- bridge process (JSONL stdio; serialized calls, FIFO responses) ---
	// ponytail: one in-flight queue, not a request map; parallel Pi calls
	// resolve in order. Upgrade only if tool latency stacking shows in traces.
	let proc: ChildProcessWithoutNullStreams | null = null;
	let buf = "";
	const waiters: Array<(line: string | null) => void> = [];
	let tail: Promise<unknown> = Promise.resolve();

	function markDead() {
		proc = null;
		while (waiters.length) waiters.shift()?.(null);
	}

	function pump(child: ChildProcessWithoutNullStreams) {
		child.stdout.on("data", (chunk) => {
			buf += Buffer.from(chunk).toString("utf8");
			let i: number;
			while ((i = buf.indexOf("\n")) >= 0) {
				const line = buf.slice(0, i).trim();
				buf = buf.slice(i + 1);
				if (line) waiters.shift()?.(line);
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
			proc = spawn(BRIDGE_CMD, BRIDGE_ARGS, {
				cwd: ROOT,
				stdio: ["pipe", "pipe", "pipe"],
			});
			buf = "";
			pump(proc);
			return true;
		} catch (err) {
			console.error(`[stockbot] bridge spawn failed: ${String(err)}`);
			markDead();
			return false;
		}
	}

	function readLine(timeoutMs: number): Promise<string | null> {
		const { promise, resolve } = Promise.withResolvers<string | null>();
		const timer = setTimeout(() => {
			const i = waiters.indexOf(done);
			if (i >= 0) waiters.splice(i, 1);
			resolve(null);
		}, timeoutMs);
		function done(line: string | null) {
			clearTimeout(timer);
			resolve(line);
		}
		waiters.push(done);
		return promise;
	}

	function callBridge(req: Json, timeoutMs = TOOL_TIMEOUT_MS, fatal = true): Promise<Json> {
		const run = async (): Promise<Json> => {
			const fail = (reason: string): Json => {
				// Single loud surface for every fatal bridge failure; pi_event
				// passes fatal=false and stays quiet (observability never breaks research).
				if (fatal) console.error(`[stockbot] bridge ${String(req.op)} failed: ${reason}`);
				return { error: "bridge_unavailable" };
			};
			if (!ensureBridge() || !proc) return fail("spawn failed");
			try {
				const ok = proc.stdin.write(`${JSON.stringify(req)}\n`);
				if (!ok) await once(proc.stdin, "drain");
			} catch {
				markDead();
				return fail("stdin write/flush failed");
			}
			const line = await readLine(timeoutMs);
			if (line === null) {
				if (fatal) markDead(); // timeout/closed pipe: respawn on next call
				return fail(`no response in ${timeoutMs}ms`);
			}
			try {
				return JSON.parse(line) as Json;
			} catch {
				return fail(`unparseable line: ${line.slice(0, 120)}`);
			}
		};
		const p = tail.then(run, run);
		tail = p.catch(() => undefined);
		return p;
	}

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
		pi.registerTool({
			name: fn.name,
			label: fn.name,
			description: fn.description,
			parameters: Type.Unsafe(fn.parameters),
			async execute(toolCallId, params) {
				toolCalls++;
				const result = await callBridge({
					op: "tool_call",
					name: fn.name,
					arguments: params,
					session_id: SESSION_ID,
				});
				refreshStatus(lastCtx);
				return {
					content: [{ type: "text", text: JSON.stringify(result) }],
					details: result,
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

	// --- prompt replacement (coding prompt -> research prompt) ---
	pi.on("before_agent_start", async () => {
		if (bridgeDown)
			return {
				systemPrompt:
					`Stockbot research tools are unavailable (${bridgeDetail}). ` +
					"Decline investment-research questions as tool-unavailable; do not answer from model knowledge.",
			};
		if (systemPrompt) return { systemPrompt };
	});

	// --- RESEARCH gate: block anything the bridge did not register ---
	// (portfolio/broker shapes + builtins when --no-builtin-tools is dropped)
	pi.on("tool_call", (event) => {
		if (!research.has(event.toolName)) {
			emit({ event: "security_block", tool: event.toolName, reason: "not a RESEARCH tool" });
			blocks++;
			return { block: true, reason: `Stockbot RESEARCH-only: '${event.toolName}' is not enabled` };
		}
	});

	// --- lifecycle forwarding (step 8) + status pane (step 9) ---
	// run_id per agent turn-chain, monotonic sequence; drops if bridge down.
	let runId = crypto.randomUUID();
	let seq = 0;
	let turns = 0;
	let toolCalls = 0;
	let blocks = 0;
	let lastCtx: ExtensionContext | null = null;

	function emit(payload: Json) {
		// Queued like tool calls so the bridge's {"ok":true} replies are
		// consumed in order; never fatal (observability never breaks research).
		void callBridge(
			{ op: "pi_event", run_id: runId, sequence: seq++, ...payload },
			10_000,
			false,
		).catch(() => undefined);
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
		refreshStatus(ctx);
	});
	pi.on("agent_start", () => {
		runId = crypto.randomUUID();
		seq = 0;
		emit({ event: "agent_start" });
	});
	pi.on("tool_execution_start", (event) => {
		emit({ event: "tool_execution_start", tool: event.toolName, tool_call_id: event.toolCallId });
	});
	pi.on("tool_execution_end", (event, ctx) => {
		lastCtx = ctx;
		emit({
			event: "tool_execution_end",
			tool: event.toolName,
			tool_call_id: event.toolCallId,
			is_error: event.isError,
		});
		refreshStatus(ctx);
	});
	pi.on("message_end", (event, ctx) => {
		lastCtx = ctx;
		const message: unknown = event.message;
		const role =
			message && typeof message === "object" && "role" in message && typeof message.role === "string"
				? message.role
				: "";
		emit({
			event: "message_end",
			role,
			turn: turns,
			model: ctx.model?.id ?? undefined,
		});
	});
	pi.on("turn_end", (event, ctx) => {
		lastCtx = ctx;
		turns++;
		emit({ event: "turn_end", turn: event.turnIndex });
		refreshStatus(ctx);
	});
	pi.on("agent_end", () => {
		emit({ event: "agent_end", status: "completed" });
	});
}
