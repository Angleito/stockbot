import { reason } from "@/lib/muse/client";
import { kernelRouter, runKernelAgent } from "@/lib/agent/kernel";
import { needleRouter } from "@/lib/needle/client";
import { tools } from "@/lib/tools";
import { endSession, invoke, newSessionId } from "@/lib/tools/stockbot";
import { redactArgs, type AgentEvent, type Evidence, type FailureCategory } from "@/lib/agent/types";

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
      // ponytail: JEV owns routing; this answers direct with zero session/DB.
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
      // ponytail: JEV-selected exact tool — Needle args-only, one execution, Muse direct.
      const answerSingleShot = async (toolName: string): Promise<boolean> => {
        const t0 = performance.now();
        send({ type: "agent_start", prompt });
        let args: Record<string, unknown> = {};
        let needleCalls = 0;
        try {
          const gen = await needleRouter.generateArguments({ tool: toolName, schema: tools[toolName]?.parameters, objective: prompt });
          if (gen.arguments && typeof gen.arguments === "object") args = gen.arguments;
          needleCalls = 1;
          send({ type: "needle_decision", step: 0, tool: toolName, arguments: redactArgs(args), confidence: gen.confidence ?? null });
        } catch {
          // Needle down — execute once with empty args.
        }
        send({ type: "tool_start", tool: toolName });
        const toolT0 = performance.now();
        const sessionId = newSessionId();
        let evidence: Evidence[] = [];
        let failure: FailureCategory | null = null;
        try {
          const entry = tools[toolName];
          const res = entry?.execute ? await entry.execute(args, { sessionId }) : await invoke(toolName, args, sessionId);
          if (res.ok) {
            evidence = [res.evidence];
            send({ type: "tool_result", tool: toolName, evidenceId: res.evidence.id, preview: res.evidence.content.slice(0, 160) });
          } else {
            failure = res.category;
            send({ type: "tool_result", tool: toolName, preview: res.error.slice(0, 160) });
            send({ type: "tool_failed", tool: toolName, category: res.category, preview: res.error.slice(0, 160) });
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          failure = "tool_error";
          send({ type: "tool_failed", tool: toolName, category: "tool_error", preview: msg.slice(0, 160) });
        } finally {
          await endSession(sessionId);
        }
        const failures: Partial<Record<FailureCategory, number>> = {};
        if (failure) failures[failure] = 1;
        send({ type: "reasoning_start", model: "muse-spark-1.3-contributor" });
        try {
          const m0 = performance.now();
          const r = await reason({ prompt, evidence, escalated: false, direct: evidence.length === 0, onDelta: (text) => send({ type: "answer_delta", text }) });
          send({ type: "done", metrics: { totalMs: performance.now() - t0, needle: { calls: needleCalls, totalMs: 0, escalations: 0 }, tools: { calls: 1, totalMs: performance.now() - toolT0 }, muse: { calls: 1, totalMs: performance.now() - m0, ...r.usage }, evidence: { count: evidence.length, characters: evidence.reduce((n, e) => n + e.content.length, 0) }, failures } });
        } catch (err) {
          send({ type: "error", message: err instanceof Error ? err.message : String(err) });
        }
        return true;
      };
      try {
        // ponytail: JEV-first entry — one route round; reason/research/tool winners branch here.
        const routed = await kernelRouter.call({ op: "route", prompt }, { signal: req.signal });
        const winner = typeof routed.route === "string" ? routed.route : "research_required";
        if (winner === "reasoning_required") {
          await answerDirect();
          controller.close();
          return;
        }
        // ponytail: worker output is untrusted — only identifier-shaped names single-shot; anything else researches.
        if (winner !== "research_required" && /^[A-Za-z_][A-Za-z0-9_]*$/.test(winner)) {
          await answerSingleShot(winner);
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
