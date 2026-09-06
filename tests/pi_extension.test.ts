import { expect, test } from "bun:test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	createBridgeClient,
	bridgeModelText,
	payloadMeta,
	toolCallRequest,
	type Json,
} from "../.pi/extensions/stockbot.ts";

const ROOT = new URL("..", import.meta.url).pathname;

function makeBridge(proc: ReturnType<typeof Bun.spawn>) {
	const reader = proc.stdout.getReader();
	const decoder = new TextDecoder();
	let buf = "";
	const readLine = async (): Promise<Json> => {
		for (; ;) {
			const i = buf.indexOf("\n");
			if (i >= 0) {
				const line = buf.slice(0, i).trim();
				buf = buf.slice(i + 1);
				if (line) return JSON.parse(line) as Json;
				continue;
			}
			const { done, value } = await reader.read();
			if (done) throw new Error("bridge closed");
			buf += decoder.decode(value, { stream: true });
		}
	};
	const send = (obj: Json): void => {
		const stdin = proc.stdin;
		if (!stdin) throw new Error("bridge stdin unavailable");
		stdin.write(`${JSON.stringify(obj)}\n`);
		stdin.flush();
	};
	return { send, readLine };
}

test("uuid protocol carries checked search_tools text", async () => {
	const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
	const proc = Bun.spawn([`${ROOT}/venv/bin/python`, `${ROOT}/scripts/pi_bridge.py`], {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "ignore",
		env: { ...process.env, RUNS_DB_PATH: join(dir, "runs.sqlite") },
	});
	const { send, readLine } = makeBridge(proc);
	try {
		const runId = crypto.randomUUID();
		const startId = crypto.randomUUID();
		send({ id: startId, op: "pi_event", run_id: runId, event: "agent_start" });
		const started = await readLine();
		expect(started.id).toBe(startId);
		expect(started.ok).toBe(true);
		const req = toolCallRequest(
			crypto.randomUUID(),
			runId,
			"call-1",
			"search_tools",
			{ query: "insider sale" },
		);
		expect(req.run_id).toBe(runId);
		expect(req.tool_call_id).toBe("call-1");
		expect(typeof req.id).toBe("string");
		expect("session_id" in req).toBe(false);
		send(req);
		const res = await readLine();
		expect(res.id).toBe(req.id);
		expect(res.error).toBeUndefined();
		const result = res.result as Json;
		expect(typeof result.content).toBe("string");
		expect(bridgeModelText(res)).toBe(result.content);
		const endId = crypto.randomUUID();
		send({ id: endId, op: "pi_event", run_id: runId, event: "agent_end" });
		const ended = await readLine();
		expect(ended.id).toBe(endId);
		expect(ended.ok).toBe(true);
	} finally {
		proc.stdin.end();
		await proc.exited;
	}
});

test("concurrent tool calls correlate by id, not arrival order", async () => {
	const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
	const proc = Bun.spawn([`${ROOT}/venv/bin/python`, `${ROOT}/scripts/pi_bridge.py`], {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "ignore",
		env: { ...process.env, RUNS_DB_PATH: join(dir, "runs.sqlite") },
	});
	const { send, readLine } = makeBridge(proc);
	try {
		const runId = crypto.randomUUID();
		send({ id: crypto.randomUUID(), op: "pi_event", run_id: runId, event: "agent_start" });
		expect((await readLine()).ok).toBe(true);
		const reqs = [0, 1, 2].map((n) =>
			toolCallRequest(crypto.randomUUID(), runId, `call-${n}`, "search_tools", {
				query: `concurrency probe ${n}`,
			}),
		);
		for (const req of reqs) send(req);
		const byId = new Map<string, Json>();
		for (let i = 0; i < reqs.length; i++) {
			const res = await readLine();
			expect(typeof res.id).toBe("string");
			byId.set(res.id as string, res);
		}
		for (const req of reqs) {
			const res = byId.get(req.id as string);
			expect(res).toBeDefined();
			expect(res!.error).toBeUndefined();
			expect(typeof (res!.result as Json).content).toBe("string");
		}
		send({ id: crypto.randomUUID(), op: "pi_event", run_id: runId, event: "agent_end" });
		expect((await readLine()).ok).toBe(true);
	} finally {
		proc.stdin.end();
		await proc.exited;
	}
});

test("error envelope stays visible; nested meta yields card values", () => {
	const err: Json = { error: "boom" };
	expect(bridgeModelText(err)).toBe(JSON.stringify(err));
	const meta = payloadMeta({
		result: { content: "SAFE", meta: { source: "sec", as_of: "2026-09-05" } },
	});
	expect(meta.pit).toBe("2026-09-05");
	expect(meta.sources).toBe("sec");
});

const HEALTHY_SCRIPT =
	"let b='';process.stdin.on('data',c=>{b+=c.toString();let i;while((i=b.indexOf('\\n'))>=0){const l=b.slice(0,i).trim();b=b.slice(i+1);if(!l)continue;try{const o=JSON.parse(l);process.stdout.write(JSON.stringify({id:o.id,ok:true})+'\\n');}catch{}}});";
const DRAIN_SCRIPT =
	"let b='';process.stdin.on('data',c=>{b+=c.toString();let i;while((i=b.indexOf('\\n'))>=0){const l=b.slice(0,i).trim();b=b.slice(i+1);if(!l)continue;try{const o=JSON.parse(l);process.stdout.write(JSON.stringify({id:o.id,error:'tool_drain_timeout'})+'\\n');}catch{}}});";
const SILENT_SCRIPT = "process.stdin.resume();process.stdin.on('data',()=>{});setTimeout(()=>{},5000);";
const NEVER_READ_SCRIPT = "setTimeout(()=>{},5000);";

function spawnScript(script: string): ChildProcessWithoutNullStreams {
	const child = spawn(process.execPath, ["-e", script], { stdio: ["pipe", "pipe", "pipe"] });
	child.stderr.resume();
	return child;
}

// Real clock: these tests recycle real child processes on real 50ms timeouts; fake timers cannot kill a process.
function deadline(ms: number): Promise<never> {
	return new Promise((_, reject) => setTimeout(() => reject(new Error("outer deadline exceeded")), ms));
}

async function settledKilled(child: ChildProcessWithoutNullStreams): Promise<boolean> {
	for (let i = 0; i < 20 && !child.killed && child.exitCode === null && child.signalCode === null; i++) {
		await new Promise((r) => setTimeout(r, 10));
	}
	return child.killed || child.exitCode !== null || child.signalCode !== null;
}

test("blocked stdin write still recycles child and releases permits", async () => {
	const kids: ChildProcessWithoutNullStreams[] = [];
	let spawns = 0;
	const { callBridge } = createBridgeClient(() => {
		spawns++;
		const child = spawnScript(spawns === 1 ? NEVER_READ_SCRIPT : HEALTHY_SCRIPT);
		kids.push(child);
		return child;
	});
	try {
		const big = "x".repeat(200_000);
		const stuck = await Promise.race([
			Promise.all(
				[0, 1, 2, 3].map(() => callBridge({ op: "tool_call", tool: "probe", blob: big }, 50, true)),
			),
			deadline(1000),
		]);
		for (const res of stuck) expect(res).toEqual({ error: "bridge_unavailable" });
		expect(await settledKilled(kids[0])).toBe(true);
		const healthy = await Promise.race([
			Promise.all([0, 1, 2, 3].map(() => callBridge({ op: "tool_call", tool: "probe" }, 500, true))),
			deadline(1000),
		]);
		for (const res of healthy) expect(res.ok).toBe(true);
		expect(spawns).toBe(2);
	} finally {
		for (const kid of kids) {
			try {
				kid.kill("SIGKILL");
			} catch {
				// already exited
			}
		}
	}
});

test("agent_end tool_drain_timeout recycles and respawns", async () => {
	const kids: ChildProcessWithoutNullStreams[] = [];
	let spawns = 0;
	const { callBridge } = createBridgeClient(() => {
		spawns++;
		const child = spawnScript(spawns === 1 ? DRAIN_SCRIPT : HEALTHY_SCRIPT);
		kids.push(child);
		return child;
	});
	try {
		const drained = await Promise.race([
			callBridge({ op: "pi_event", event: "agent_end", run_id: "r" }, 500, false),
			deadline(1000),
		]);
		expect(drained).toEqual({ error: "bridge_unavailable" });
		expect(await settledKilled(kids[0])).toBe(true);
		const next = await Promise.race([
			callBridge({ op: "pi_event", event: "agent_start", run_id: "r" }, 500, false),
			deadline(1000),
		]);
		expect(next.ok).toBe(true);
		expect(spawns).toBe(2);
	} finally {
		for (const kid of kids) {
			try {
				kid.kill("SIGKILL");
			} catch {
				// already exited
			}
		}
	}
});

test("agent_end timeout recycles even when fatal=false", async () => {
	const kids: ChildProcessWithoutNullStreams[] = [];
	let spawns = 0;
	const { callBridge } = createBridgeClient(() => {
		spawns++;
		const child = spawnScript(spawns === 1 ? SILENT_SCRIPT : HEALTHY_SCRIPT);
		kids.push(child);
		return child;
	});
	try {
		const timedOut = await Promise.race([
			callBridge({ op: "pi_event", event: "agent_end", run_id: "r" }, 50, false),
			deadline(1000),
		]);
		expect(timedOut).toEqual({ error: "bridge_unavailable" });
		expect(await settledKilled(kids[0])).toBe(true);
		const next = await Promise.race([
			callBridge({ op: "pi_event", event: "agent_start", run_id: "r" }, 500, false),
			deadline(1000),
		]);
		expect(next.ok).toBe(true);
		expect(spawns).toBe(2);
	} finally {
		for (const kid of kids) {
			try {
				kid.kill("SIGKILL");
			} catch {
				// already exited
			}
		}
	}
});
