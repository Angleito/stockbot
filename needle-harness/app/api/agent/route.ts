import { runKernelAgent } from "@/lib/agent/kernel";
import type { AgentEvent } from "@/lib/agent/types";

export async function POST(req: Request): Promise<Response> {
  let prompt: unknown;
  let asOf: unknown;
  let includeTrace: unknown;
  try {
    ({ prompt, asOf, includeTrace } = await req.json());
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
  console.log(`[web] [agent-api] POST prompt_chars=${prompt.length}`);
  const stream = new ReadableStream({
    async start(controller) {
      const enc = new TextEncoder();
      const send = (e: AgentEvent) => controller.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`));
      try {
        await runKernelAgent(prompt, send, {
          signal: req.signal,
          ...(typeof asOf === "string" ? { asOf } : asOf === null ? { asOf: null } : {}),
          ...(includeTrace ? { includeTrace: true } : {}),
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
