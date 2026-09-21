import { isTrivialPrompt } from "@/lib/agent/entry-router";
import { reason } from "@/lib/muse/client";
import { kernelRouter, runKernelAgent } from "@/lib/agent/kernel";
import type { AgentEvent } from "@/lib/agent/types";

export async function POST(req: Request): Promise<Response> {
  let prompt: unknown;
  try {
    ({ prompt } = await req.json());
  } catch {
    console.error("[web] [agent-api] invalid JSON");
    return Response.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (typeof prompt !== "string" || !prompt.trim()) {
    console.error("[web] [agent-api] prompt required");
    return Response.json({ error: "prompt required" }, { status: 400 });
  }
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
      // ponytail: trivial chitchat answers direct, never a worker round trip.
      if (isTrivialPrompt(prompt)) {
        await answerDirect();
        controller.close();
        return;
      }
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
      try {
        await runKernelAgent(prompt, send, { signal: req.signal });
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
