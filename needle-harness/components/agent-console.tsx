import type { AgentEvent } from "@/lib/agent/types";
import { AgentEventView } from "./agent-event";

export function AgentConsole({ events, reasoning }: { events: AgentEvent[]; reasoning: boolean }) {
  let answer = "";
  for (const e of events) if (e.type === "answer_delta") answer += e.text;
  const head = events.filter(
    (e) => e.type !== "answer_delta" && e.type !== "done" && e.type !== "error" && e.type !== "reasoning_start",
  );
  const tail = events.filter((e) => e.type === "done" || e.type === "error");
  const streaming = reasoning && tail.length === 0;
  return (
    <div className="flex-1 space-y-2 overflow-y-auto px-4 py-4 text-sm">
      {head.map((e, i) => (
        <AgentEventView key={i} event={e} />
      ))}
      {streaming && <AgentEventView event={{ type: "reasoning_start", model: "muse-spark-1.3-contributor" }} />}
      {answer && <div className="whitespace-pre-wrap pt-2 text-zinc-100">{answer}</div>}
      {tail.map((e, i) => (
        <AgentEventView key={`tail-${i}`} event={e} />
      ))}
    </div>
  );
}
