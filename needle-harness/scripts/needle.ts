import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { homedir, networkInterfaces } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const HARNESS_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const ROOT_DIR = dirname(HARNESS_DIR);

if (!existsSync(join(HARNESS_DIR, "lib/needle/server.py"))) {
  process.stderr.write(`needle: harness not found at ${HARNESS_DIR}\n`);
  process.exit(1);
}

const DOTENV = join(ROOT_DIR, ".env");
const dotenvLoaded = typeof process.loadEnvFile === "function" && existsSync(DOTENV);
if (dotenvLoaded) process.loadEnvFile(DOTENV);

const VENV_PYTHON = `${homedir()}/.cache/needle-harness/.needle/bin/python`;
const SERVER = join(HARNESS_DIR, "lib/needle/server.py");
const WARMUP_TIMEOUT_MS = 120_000;
const PORT = process.env.PORT ?? "3000";
const CATALOG = process.env.NEEDLE_CATALOG ?? join(HARNESS_DIR, ".needle-catalog.json");

process.stdout.write(`needle [web]: harness ${HARNESS_DIR} → http://localhost:${PORT} (cwd ${process.cwd()})\n`);
for (const n of Object.values(networkInterfaces()).flat()) {
  if (n?.family === "IPv4" && !n.internal) process.stdout.write(`needle [web]: LAN   http://${n.address}:${PORT}\n`);
}
process.stdout.write(dotenvLoaded ? `needle [web]: env loaded ${DOTENV}\n` : `needle [web]: no .env at ${DOTENV}, using shell env\n`);

function fail(msg: string): never {
  process.stderr.write(msg + "\n");
  process.exit(1);
}

if (!existsSync(VENV_PYTHON)) {
  fail("needle venv missing — run: bash scripts/setup-needle.sh");
}
process.stdout.write(`needle [ai]: venv ok ${VENV_PYTHON}\n`);

const pinned = process.env.NEEDLE_WEIGHTS;
const repoBlob = join(ROOT_DIR, "needle3.cact");
if (pinned) {
  const resolved = isAbsolute(pinned) ? pinned : join(HARNESS_DIR, pinned);
  process.env.NEEDLE_WEIGHTS = resolved;
  process.stdout.write(`needle [ai]: weights ${resolved} (${existsSync(resolved) ? "exists" : "missing"})\n`);
} else if (existsSync(repoBlob)) {
  process.stdout.write(`needle [ai]: weights ${repoBlob} (exists)\n`);
} else {
  process.stderr.write("weights not pinned — falling back to HF cache\n");
}

const ALLOWLIST = ["search_web", "find_sec_entities", "search_sec_filings", "get_sec_document"];

type DescribeEntry = { function?: { name?: unknown; description?: unknown; parameters?: unknown } };

async function fetchCatalog(): Promise<void> {
  process.stdout.write("needle [ai]: fetching tool catalog via pi_bridge describe…\n");
  const bridge = spawn(`${ROOT_DIR}/venv/bin/python`, [`${ROOT_DIR}/scripts/pi_bridge.py`], {
    cwd: ROOT_DIR,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  let buf = "";
  let errTail = "";
  const timer = setTimeout(() => reject(new Error("timeout")), WARMUP_TIMEOUT_MS);
  bridge.stdout?.on("data", (d: Buffer) => {
    buf += d.toString();
    const i = buf.indexOf("\n");
    if (i >= 0) {
      clearTimeout(timer);
      resolve(buf.slice(0, i));
    }
  });
  bridge.on("error", reject);
  bridge.on("close", (code) => {
    clearTimeout(timer);
    reject(new Error(`bridge exited ${code ?? "unknown"} before describe reply`));
  });
  bridge.stderr?.on("data", (d: Buffer) => {
    const s = d.toString();
    process.stderr.write(s);
    errTail = (errTail + s).slice(-2000);
  });
  bridge.stdin?.write('{"id":"describe-1","op":"describe"}\n');
  const line = await promise.catch((e: Error) => {
    bridge.kill();
    fail(`needle catalog failed: ${e.message}\nbridge stderr tail: ${errTail || "(empty)"}`);
  });
  bridge.kill();
  let tools: unknown;
  try {
    tools = (JSON.parse(line) as { tools?: unknown }).tools;
  } catch {
    fail(`needle catalog failed: bad JSON ${line}\nbridge stderr tail: ${errTail || "(empty)"}`);
  }
  if (!Array.isArray(tools)) {
    fail(`needle catalog failed: describe tools not an array: ${line}\nbridge stderr tail: ${errTail || "(empty)"}`);
  }
  const catalog: { name: string; description: string; parameters: unknown }[] = [];
  for (const entry of tools as DescribeEntry[]) {
    const fn = entry?.function;
    if (!fn || typeof fn.name !== "string") continue;
    if (!ALLOWLIST.includes(fn.name)) continue;
    catalog.push({
      name: fn.name,
      description: typeof fn.description === "string" ? fn.description : fn.name,
      parameters: fn.parameters && typeof fn.parameters === "object" ? fn.parameters : { type: "object" },
    });
  }
  for (const name of ALLOWLIST) {
    if (!catalog.some((c) => c.name === name)) {
      fail(`needle catalog failed: allowlisted tool "${name}" missing from describe output`);
    }
  }
  catalog.push(
    { name: "fetch_url", description: "Fetch a URL and extract readable text", parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] } },
    { name: "get_current_time", description: "Get the current UTC date and time", parameters: { type: "object", properties: {} } },
  );
  await writeFile(CATALOG, JSON.stringify(catalog, null, 2));
  process.stdout.write(`needle [ai]: catalog ok ${CATALOG} (${catalog.map((c) => c.name).join(",")})\n`);
}

async function warmup(): Promise<void> {
  process.stdout.write("needle [ai]: warmup start (loading Needle 3 weights, first load can take ~60s)…\n");
  const child = spawn(VENV_PYTHON, [SERVER], { cwd: HARNESS_DIR, stdio: ["pipe", "pipe", "pipe"] });
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  let buf = "";
  let errTail = "";
  const timer = setTimeout(() => reject(new Error("timeout")), WARMUP_TIMEOUT_MS);
  child.stdout?.on("data", (d: Buffer) => {
    buf += d.toString();
    const i = buf.indexOf("\n");
    if (i >= 0) {
      clearTimeout(timer);
      resolve(buf.slice(0, i));
    }
  });
  child.on("error", reject);
  child.on("close", (code) => {
    clearTimeout(timer);
    reject(new Error(`bridge exited ${code ?? "unknown"} before warmup reply`));
  });
  child.stderr?.on("data", (d: Buffer) => {
    const s = d.toString();
    process.stderr.write(s);
    errTail = (errTail + s).slice(-2000);
  });
  child.stdin?.write('{"id":"warmup","action":"start","prompt":"What time is it?"}\n');
  const line = await promise.catch((e: Error) => {
    child.kill();
    fail(`needle warmup failed: ${e.message}\nneedle warmup stderr tail: ${errTail || "(empty)"}`);
  });
  child.kill();
  let id: unknown;
  let tool: unknown;
  try {
    ({ id, tool } = JSON.parse(line) as { id?: unknown; tool?: unknown });
  } catch {
    fail(`needle warmup failed: bad JSON ${line}\nneedle warmup stderr tail: ${errTail || "(empty)"}`);
  }
  if (id !== "warmup" || tool !== "get_current_time") {
    fail(`needle warmup failed: unexpected ${line}\nneedle warmup stderr tail: ${errTail || "(empty)"}`);
  }
  process.stdout.write("needle [ai]: warmup ok (get_current_time)\n");
}

await fetchCatalog();
await warmup();

process.stdout.write(`needle [web]: starting next dev (cwd ${HARNESS_DIR}, PORT=${PORT})…\n`);
process.stdout.write("needle [web]: next dev output follows — wait for Ready…\n");
const dev = spawn("bun", ["run", "dev"], {
  cwd: HARNESS_DIR,
  stdio: "inherit",
  env: { ...process.env, PORT },
});
for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => dev.kill(sig));
}
const { promise: exited, resolve: onExit } = Promise.withResolvers<number>();
dev.on("close", onExit);
const code = await exited;
if (code === 0) {
  process.stdout.write(`needle [web]: next dev exited with code ${code}\n`);
} else {
  process.stderr.write(`needle [web]: next dev exited with code ${code}\n`);
}
process.exit(code);
