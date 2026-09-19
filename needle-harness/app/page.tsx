"use client";

import { useState } from "react";
import { AgentConsole } from "@/components/agent-console";
import { PromptBox } from "@/components/prompt-box";
import type { AgentEvent } from "@/lib/agent/types";

export default function Home() {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const reasoning = events.some((e) => e.type === "reasoning_start");

  async function ask(prompt: string) {
    setBusy(true);
    setEvents([{ type: "agent_start", prompt }]);
    try {
      const res = await fetch("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      if (!res.ok || !res.body) {
        setEvents((ev) => [...ev, { type: "error", message: `request failed: ${res.status}` }]);
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (; ;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of frame.split("\n")) {
            const t = line.trim();
            if (!t.startsWith("data:")) continue;
            try {
              const ev = JSON.parse(t.slice(5)) as AgentEvent;
              if (ev.type !== "agent_start") setEvents((prev) => [...prev, ev]);
            } catch {
              // Partial frame; next chunk completes it.
            }
          }
        }
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-4 py-2 text-sm">
        <span className="font-bold tracking-widest">NEEDLE</span>
        <span className="text-zinc-500">
          LOCAL <span className="text-green-500">●</span>
        </span>
      </header>
      <AgentConsole events={events} reasoning={reasoning} />
      <PromptBox onSubmit={ask} busy={busy} />
    </main>
  );
}
