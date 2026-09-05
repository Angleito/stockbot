import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	bridgeModelText,
	payloadMeta,
	toolCallRequest,
	type Json,
} from "../.pi/extensions/stockbot.ts";

const ROOT = new URL("..", import.meta.url).pathname;

function makeReader(proc: ReturnType<typeof Bun.spawn>) {
	const reader = proc.stdout.getReader();
	const decoder = new TextDecoder();
	let buf = "";
	return async (): Promise<Json> => {
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
}

test("uuid protocol carries checked search_tools text", async () => {
	const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
	const proc = Bun.spawn([`${ROOT}/venv/bin/python`, `${ROOT}/scripts/pi_bridge.py`], {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "ignore",
		env: { ...process.env, RUNS_DB_PATH: join(dir, "runs.sqlite") },
	});
	const read = makeReader(proc);
	const send = async (obj: Json): Promise<Json> => {
		const stdin = proc.stdin;
		if (!stdin) throw new Error("bridge stdin unavailable");
		stdin.write(`${JSON.stringify(obj)}\n`);
		stdin.flush();
		return read();
	};
	try {
		const runId = crypto.randomUUID();
		expect(await send({ op: "pi_event", run_id: runId, event: "agent_start" })).toEqual({
			ok: true,
		});
		const req = toolCallRequest(runId, "search_tools", { query: "insider sale" });
		expect(req.run_id).toBe(runId);
		expect("session_id" in req).toBe(false);
		const res = await send(req);
		expect(res.error).toBeUndefined();
		const result = res.result as Json;
		expect(typeof result.content).toBe("string");
		expect(bridgeModelText(res)).toBe(result.content);
		expect(await send({ op: "pi_event", run_id: runId, event: "agent_end" })).toEqual({
			ok: true,
		});
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
