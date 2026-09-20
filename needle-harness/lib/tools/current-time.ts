import { makeEvidence } from "../agent/evidence";
import type { Tool } from "../agent/types";

export const get_current_time: Tool = {
  description: "Get the current UTC date and time",
  parameters: { type: "object", properties: {} },
  async execute() {
    try {
      return {
        ok: true,
        evidence: makeEvidence("system-clock", new Date().toISOString(), {
          title: "Current UTC time",
        }),
      };
    } catch (e) {
      return { ok: false as const, error: String(e), category: "tool_error" as const, retryable: false };
    }
  },
};
