import { runKernelAgent } from "@/lib/agent/kernel";
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
