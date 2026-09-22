import { isTrivialPrompt } from "@/lib/agent/entry-router";
import { reason } from "@/lib/muse/client";
import { kernelRouter, runKernelAgent } from "@/lib/agent/kernel";
import type { AgentEvent } from "@/lib/agent/types";

export async function POST(req: Request): Promise<Response> {
  let prompt: unknown;
  let asOf: unknown;
  let includeTrace: unknown;
  let evalSeedEvidence: unknown;
  try {
    ({ prompt, asOf, includeTrace, evalSeedEvidence } = await req.json());
  } catch {
    console.error("[web] [agent-api] invalid JSON");
    return Response.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (typeof prompt !== "string" || !prompt.trim()) {
    console.error("[web] [agent-api] prompt required");
    return Response.json({ error: "prompt required" }, { status: 400 });
  }
  if (includeTrace) {
    const configured = process.env.STOCKBOT_EVAL_TOKEN ?? "";
    const presented = req.headers.get("authorization") ?? "";
    if (!configured.trim() || presented !== `Bearer ${configured}`) {
      console.error("[web] [agent-api] evaluation trace forbidden");
      return Response.json({ error: "forbidden" }, { status: 403 });
    }
  }
  // Eval-only hostile seed: honored ONLY with a valid eval token + includeTrace; never the production path.
  type SeedIn = { id: unknown; source: unknown; content: unknown; title?: unknown };
  const rawSeed: SeedIn[] = includeTrace && Array.isArray(evalSeedEvidence)
    ? (evalSeedEvidence as SeedIn[]).filter((s) => !!s && typeof s === "object" && !Array.isArray(s))
    : [];
  const seedEvidence = rawSeed
    .filter((s) => typeof s.id === "string" && s.id.trim() && typeof s.content === "string" && s.content)
    .slice(0, 20)
    .map((s) => ({
      id: String(s.id),
      // Marked untrusted non-company fixture data; never trusted as company disclosure.
      source: typeof s.source === "string" && s.source.trim() ? `untrusted-seed:${s.source.trim()}` : "untrusted-seed",
      ...(typeof s.title === "string" && s.title.trim() ? { title: s.title.trim().slice(0, 200) } : {}),
      content: String(s.content).slice(0, 8000),
    }));
  // Eval mode must always reach runKernelAgent so exactly-one-trace holds.
  const evalMode = Boolean(includeTrace);
  console.log(`[web] [agent-api] POST prompt_chars=${prompt.length}`);
  const stream = new ReadableStream({
    async start(controller) {
      const enc = new TextEncoder();
      const send = (e: AgentEvent) => controller.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`));
      const answerDirect = async (): Promise<boolean> => {
        const t0 = performance.now();
        send({ type: "agent_start", prompt });
        send({ type: "reasoning_start", model: "muse-spark-1.3-contributor" });
        try {
          const r = await reason({ prompt, evidence: [], escalated: false, direct: true, onDelta: (text) => send({ type: "answer_delta", text }) });
          send({ type: "done", metrics: { totalMs: performance.now() - t0, needle: { calls: 0, totalMs: 0, escalations: 0 }, tools: { calls: 0, totalMs: 0 }, muse: { calls: 1, totalMs: performance.now() - t0, ...r.usage }, evidence: { count: 0, characters: 0 }, failures: {} } });
        } catch (err) {
          send({ type: "error", message: err instanceof Error ? err.message : String(err) });
        }
        return true;
      };
      // ponytail: trivial chitchat answers direct, never a worker round trip (production only; eval needs a trace).
      if (!evalMode && isTrivialPrompt(prompt)) {
        await answerDirect();
        controller.close();
        return;
      }
      if (!evalMode) {
        try {
          // ponytail: JEV-before-kernel — one route round, zero session/DB on reasoning.
          const routed = await kernelRouter.call({ op: "route", prompt }, { signal: req.signal });
          if (routed.route === "reasoning_required") {
            await answerDirect();
            controller.close();
            return;
          }
        } catch {
          // Route unavailable — fail open to research below.
        }
      }
      // Production fast paths live above (evalMode-guarded); research path below.
      try {
        await runKernelAgent(prompt, send, {
          signal: req.signal,
          ...(typeof asOf === "string" ? { asOf } : asOf === null ? { asOf: null } : {}),
          ...(includeTrace ? { includeTrace: true } : {}),
          ...(seedEvidence.length > 0 ? { seedEvidence } : {}),
        });
      } catch (err) {
        console.error(`[web] [agent-api] runKernelAgent error: ${err instanceof Error ? err.message : String(err)}`);
        send({ type: "error", message: err instanceof Error ? err.message : String(err) });
      }
      controller.close();
      console.log("[web] [agent-api] stream close");
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" },
  });
}
