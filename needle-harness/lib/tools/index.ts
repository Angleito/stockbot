import type { Tool } from "../agent/types";
import { fetch_url } from "./fetch-url";
import { get_current_time } from "./current-time";
import { get_sec_filings } from "./sec-filings";
import { web_search } from "./web-search";

export const tools: Record<string, Tool> = {
  web_search,
  fetch_url,
  get_sec_filings,
  get_current_time,
};
