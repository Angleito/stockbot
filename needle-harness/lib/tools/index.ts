import type { Tool } from "../agent/types";
import { get_current_time } from "./current-time";
import { fetch_url } from "./fetch-url";
import { invoke } from "./stockbot";

// Gateway-backed entries: parameters stay {} because validation is owned
function gateway(name: string): Tool {
  return {
    description: name,
    parameters: { type: "object", properties: {} },
    async execute(args) {
      const { sessionId, ...rest } = args as Record<string, unknown> & { sessionId?: unknown };
      return invoke(name, rest, typeof sessionId === "string" ? sessionId : "");
    },
  };
}

export const tools: Record<string, Tool> = {
  search_web: gateway("search_web"),
  find_sec_entities: gateway("find_sec_entities"),
  search_sec_filings: gateway("search_sec_filings"),
  get_sec_document: gateway("get_sec_document"),
  fetch_url,
  get_current_time,
};
