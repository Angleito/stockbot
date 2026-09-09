/** Ephemeral YouTube attention-metrics panel.
 *
 * Operator-only `/youtube-analytics` command. Worker output stays in process
 * memory inside an overlay component: never sent to the model, never saved,
 * never logged. Quota counts alone persist (owned by the Python worker).
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type {
 ExtensionAPI,
 ExtensionCommandContext,
 Theme,
} from "@earendil-works/pi-coding-agent";
import {
 type Component,
 type KeybindingsManager,
 stripTerminalSequences,
 truncateToWidth,
 type TUI,
 wrapTextWithAnsi,
} from "@earendil-works/pi-tui";

export type Json = Record<string, unknown>;

export interface YoutubeAnalyticsRequest {
 thesis_id: string;
 mode: "topic" | "popular";
 query: string | null;
 region: string;
 days: number | null;
 limit: number;
 confirmed: boolean;
}

/** Code-only testing seam: builds the private worker child for one JSON input. */
export type SpawnWorker = (input: string) => ChildProcessWithoutNullStreams;

export const YT_TIMEOUT_MS = 45_000;
export const YT_STDOUT_CAP = 256 * 1024;
const YT_LIMIT = 10;

const ROOT = new URL("../..", import.meta.url).pathname;

export function spawnYoutubeWorker(input: string): ChildProcessWithoutNullStreams {
 const child = spawn(`${ROOT}/venv/bin/python`, ["-m", "app.google_data.youtube"], {
  shell: false,
  cwd: ROOT,
  stdio: ["pipe", "pipe", "pipe"],
  windowsHide: true,
 });
 child.stdin.on("error", () => { });
 child.stdin.write(input);
 child.stdin.end();
 return child;
}

function clientFailure(code: string): Json {
 return { status: "unavailable", source: "youtube", error_type: code, error: code };
}

/** One bounded stdin request in, one bounded JSON response out. Rejects only on abort. */
export function fetchYoutubeAnalytics(
 request: YoutubeAnalyticsRequest,
 signal?: AbortSignal,
 spawnWorker: SpawnWorker = spawnYoutubeWorker,
 timeoutMs: number = YT_TIMEOUT_MS,
): Promise<Json> {
 signal?.throwIfAborted();
 const input = JSON.stringify(request);
 let child: ChildProcessWithoutNullStreams;
 try {
  child = spawnWorker(input);
 } catch {
  return Promise.resolve(clientFailure("source_unavailable"));
 }
 // Drain stderr without forwarding: child bytes never reach logs, chat, or errors.
 child.stderr.resume();
 child.stderr.on("data", () => { });
 return new Promise<Json>((resolve, reject) => {
  let settled = false;
  let out = Buffer.alloc(0);
  const cleanup = () => {
   clearTimeout(timer);
   signal?.removeEventListener("abort", onAbort);
   // Reap stdio fds now so process exit never races a dying child (bun EPIPE teardown noise).
   for (const s of [child.stdin, child.stdout, child.stderr]) {
    try {
     s.removeAllListeners();
     s.on("error", () => { });
     s.destroy();
    } catch { }
   }
   child.removeAllListeners();
  };
  const finish = (value: Json) => {
   if (settled) return;
   settled = true;
   cleanup();
   resolve(value);
  };
  const onAbort = () => {
   if (settled) return;
   settled = true;
   cleanup();
   try {
    child.kill();
   } catch { }
   reject(signal?.reason ?? new DOMException("aborted", "AbortError"));
  };
  // ponytail: kill-then-settle; a hung SIGTERM still resolves here, late events ignored by settled flag
  const timer = setTimeout(() => {
   try {
    child.kill();
   } catch { }
   finish(clientFailure("source_unavailable"));
  }, timeoutMs);
  child.stdout.on("data", (chunk: Buffer) => {
   if (settled) return;
   out = Buffer.concat([out, Buffer.from(chunk)]);
   if (out.length > YT_STDOUT_CAP) {
    try {
     child.kill();
    } catch { }
    out = Buffer.alloc(0);
    finish(clientFailure("response_too_large"));
   }
  });
  child.on("error", () => finish(clientFailure("source_unavailable")));
  child.on("close", (code) => {
   if (settled) return;
   if (code !== 0) {
    out = Buffer.alloc(0);
    finish(clientFailure("source_unavailable"));
    return;
   }
   let parsed: unknown;
   try {
    parsed = JSON.parse(out.toString("utf8"));
   } catch {
    parsed = undefined;
   }
   out = Buffer.alloc(0);
   if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    finish(clientFailure("malformed_response"));
    return;
   }
   finish(parsed as Json);
  });
  signal?.addEventListener("abort", onAbort, { once: true });
 });
}

const TOPIC_LABEL = "Thesis-topic performance";
const POPULAR_LABEL = "Regional most-popular";
const DAY_OPTIONS = ["7 days", "30 days (default)", "90 days"];

const WARNING_LABELS: Record<string, string> = {
 details_unavailable: "some video details unavailable; affected rows show snippet data only",
 details_missing: "some videos missing from details; affected rows show snippet data only",
 live_ordering: "provider may rank live broadcasts by concurrent viewers, not lifetime views",
};

function failureNotice(response: Json): string | undefined {
 if (response.status === "disabled") {
  return response.reason === "missing_key"
   ? "YouTube analytics unavailable: no API key is configured."
   : "YouTube analytics unavailable: Google data is disabled.";
 }
 if (response.status !== "unavailable") return undefined;
 const reasons: Record<string, string> = {
  quota_exhausted: "daily quota exhausted; resumes at next Pacific midnight",
  consent_required: "confirmation is required",
  invalid_thesis: "unknown thesis",
  invalid_params: "invalid request",
  invalid_config: "invalid configuration",
  private_args_denied: "query withheld by privacy screen; retry with a public topic",
  legacy_cache_present: "legacy cache present; operator cleanup required",
  quota_state_invalid: "quota ledger invalid",
  source_unavailable: "source request failed",
  malformed_response: "source response unreadable",
  response_too_large: "response exceeded size cap",
 };
 const reason =
  typeof response.error_type === "string" && reasons[response.error_type]
   ? reasons[response.error_type]
   : "request failed";
 return `YouTube analytics unavailable: ${reason}.`;
}

interface VideoRow {
 video_id: string;
 title: string;
 channel_title: string;
 published_at: string;
 live_broadcast_content: string;
 view_count: string | null;
 like_count: string | null;
 comment_count: string | null;
}

interface PanelMeta {
 slug: string;
 mode: string;
 query: string | null;
 region: string;
 days: number | null;
 retrievedAt: string;
 expiresAt: string;
 warnings: string[];
}

/** Strip control/escape sequences at render time; words otherwise unchanged. */
function safeText(value: unknown): string {
 return stripTerminalSequences(String(value ?? ""))
  .replace(/[\r\n]+/g, " ")
  .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");
}

function asVideo(v: unknown): VideoRow {
 const o = (v ?? {}) as Json;
 const str = (k: string) => (typeof o[k] === "string" ? (o[k] as string) : "");
 const count = (k: string): string | null =>
  typeof o[k] === "string" || o[k] === null ? (o[k] as string | null) : null;
 return {
  video_id: str("video_id"),
  title: str("title"),
  channel_title: str("channel_title"),
  published_at: str("published_at"),
  live_broadcast_content: str("live_broadcast_content") || "none",
  view_count: count("view_count"),
  like_count: count("like_count"),
  comment_count: count("comment_count"),
 };
}

function confirmMessage(thesisArg: string, request: YoutubeAnalyticsRequest): string {
 const clean = (s: string) => s.replace(/[\r\n]+/g, " ");
 const scope =
  request.mode === "topic"
   ? `topic query "${clean(request.query ?? "")}" · window ${request.days}d · region ${request.region}`
   : `regional chart · region ${request.region} (not filtered to thesis)`;
 return [
  `Fetch YouTube analytics for local thesis "${clean(thesisArg)}"?`,
  scope,
  "",
  "Stockbot uses YouTube API Services. Only these public search parameters and the project API key go to Google; never the thesis ID or body.",
  "Titles and counts stay in process memory: not sent to the model, not saved by Stockbot. Quota counts alone are stored locally.",
  "Closing the view or 15-minute expiry discards it. Terminal recordings, screenshots, or OS copies are outside Stockbot control.",
  "https://www.youtube.com/t/terms · https://policies.google.com/privacy · https://developers.google.com/youtube/terms/developer-policies",
  "Using this feature is subject to YouTube terms and this notice.",
 ].join("\n");
}

export function registerYoutubeAnalytics(
 pi: ExtensionAPI,
 fetchAnalytics: typeof fetchYoutubeAnalytics = fetchYoutubeAnalytics,
): void {
 let panelOpen = false;
 pi.registerCommand("youtube-analytics", {
  description: "Ephemeral YouTube attention metrics for a thesis (private view; never saved).",
  handler: async (args: string, ctx: ExtensionCommandContext): Promise<void> => {
   if (ctx.mode !== "tui" || !ctx.isIdle() || panelOpen) {
    ctx.ui.notify(
     "YouTube analytics is available only in an idle TUI session with no panel open.",
     "info",
    );
    return;
   }
   const thesisArg = args.trim();
   if (!thesisArg) {
    ctx.ui.notify("Usage: /youtube-analytics <thesis-id-or-slug>", "info");
    return;
   }
   // Claim the single-panel slot synchronously: handler awaits below would
   // otherwise let a concurrent invocation slip past the gate and hang.
   panelOpen = true;
   try {
    const choice = await ctx.ui.select("YouTube analytics", [TOPIC_LABEL, POPULAR_LABEL]);
    if (choice === undefined) return;
    const topic = choice === TOPIC_LABEL;
    let query: string | null = null;
    let days: number | null = null;
    if (topic) {
     const rawQuery = await ctx.ui.input("Public topic query", "e.g. solid-state batteries");
     if (rawQuery === undefined || !rawQuery.trim()) return;
     query = rawQuery.trim();
     const rawDays = await ctx.ui.select("Publication window", DAY_OPTIONS);
     if (rawDays === undefined) return;
     days = rawDays.startsWith("7") ? 7 : rawDays.startsWith("90") ? 90 : 30;
    }
    const rawRegion = await ctx.ui.input("Region (2 letters, blank = US)", "US");
    if (rawRegion === undefined) return;
    const region = (rawRegion.trim() || "US").toUpperCase();
    if (!/^[A-Z]{2}$/.test(region)) {
     ctx.ui.notify("YouTube analytics cancelled: region must be two letters.", "info");
     return;
    }
    const request: YoutubeAnalyticsRequest = {
     thesis_id: thesisArg,
     mode: topic ? "topic" : "popular",
     query,
     region,
     days,
     limit: YT_LIMIT,
     confirmed: true,
    };
    if (!(await ctx.ui.confirm("Confirm YouTube request", confirmMessage(thesisArg, request))))
     return;
    let failure: Json | null = null;
    await ctx.ui.custom<void>(
     (tui: TUI, _theme: Theme, _keys: KeybindingsManager, done: (v: void) => void) => {
      let closed = false;
      let doneCalled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const aborter = new AbortController();
      let meta: PanelMeta | null = null;
      let rows: VideoRow[] = [];
      let page = 0;
      // ponytail: fixed 5-row pages keep the close/help/status footer visible without terminal-height plumbing
      const ROWS_PER_PAGE = 5;
      const finish = () => {
       if (doneCalled) return;
       doneCalled = true;
       done(undefined);
      };
      const cleanup = () => {
       if (closed) return;
       closed = true;
       clearTimeout(timer);
       timer = undefined;
       try {
        aborter.abort();
       } catch { }
       rows = [];
       meta = null;
       try {
        tui.requestRender();
       } catch { }
       finish();
      };
      const expired = () => {
       if (!meta) return false;
       const ms = Date.parse(meta.expiresAt);
       return Number.isFinite(ms) && Date.now() >= ms;
      };
      const fit = (lines: string[], width: number) =>
       lines.map((l) => truncateToWidth(l, Math.max(width, 8)));
      const renderRows = (width: number): string[] => {
       const lines: string[] = [];
       const head =
        meta!.mode === "topic"
         ? `YouTube topic analytics · ${safeText(meta!.slug)}`
         : `YouTube most-popular chart — ${safeText(meta!.region)}; not filtered to thesis`;
       lines.push(head);
       lines.push(
        `query: ${meta!.query === null ? "—" : safeText(meta!.query)} · region: ${safeText(meta!.region)} · source: youtube`,
       );
       lines.push(`fetched ${safeText(meta!.retrievedAt)} · expires ${safeText(meta!.expiresAt)}`);
       if (meta!.mode === "topic") {
        lines.push("counts are lifetime totals for recently published videos, not views gained in the window");
        lines.push("region controls viewability, not viewer location");
       }
       for (const w of meta!.warnings) {
        if (WARNING_LABELS[w]) lines.push(`note: ${WARNING_LABELS[w]}`);
       }
       lines.push("");
       const pages = Math.max(1, Math.ceil(rows.length / ROWS_PER_PAGE));
       if (page > pages - 1) page = pages - 1;
       const count = (c: string | null) => c ?? "unavailable";
       rows.slice(page * ROWS_PER_PAGE, (page + 1) * ROWS_PER_PAGE).forEach((r, i) => {
        for (const w of wrapTextWithAnsi(
         `#${page * ROWS_PER_PAGE + i + 1} ${safeText(r.title)}`,
         width,
        ))
         lines.push(w);
        const live = r.live_broadcast_content === "none" ? "" : ` · ${r.live_broadcast_content}`;
        lines.push(
         `  ${safeText(r.channel_title)} · views ${count(r.view_count)} · likes ${count(r.like_count)} · comments ${count(r.comment_count)} · ${safeText(r.published_at)}${live}`,
        );
       });
       lines.push("");
       lines.push(
        `[esc/q] close · [j/k] page · page ${page + 1}/${pages} · ${rows.length} videos`,
       );
       return fit(lines, width);
      };
      const component: Component & { dispose?(): void } = {
       render: (width: number) => {
        if (closed) return fit(["YouTube analytics view closed."], width);
        if (expired()) {
         cleanup();
         return fit(["YouTube analytics view expired."], width);
        }
        if (!meta)
         return fit(
          [
           "Loading YouTube analytics…",
           "",
           "Private view: not saved, not sent to the model. [esc/q] close",
          ],
          width,
         );
        return renderRows(width);
       },
       handleInput: (data: string) => {
        if (closed) return;
        if (expired()) {
         cleanup();
         return;
        }
        if (data === "\x1b" || data === "q" || data === "Q") cleanup();
        else if (data === "j" || data === " " || data === "\x1b[B") {
         page++;
         try {
          tui.requestRender();
         } catch { }
        } else if (data === "k" || data === "\x1b[A") {
         if (page > 0) page--;
         try {
          tui.requestRender();
         } catch { }
        }
       },
       invalidate: () => { },
       dispose: () => cleanup(),
      };
      void (async () => {
       let response: Json;
       try {
        response = await fetchAnalytics(request, aborter.signal);
       } catch {
        if (closed || aborter.signal.aborted) return;
        failure = clientFailure("source_unavailable");
        cleanup();
        return;
       }
       if (closed || aborter.signal.aborted) return;
       if (response.status === "ok" || response.status === "partial") {
        const r = response as Json;
        const thesis = (r.thesis ?? {}) as Json;
        meta = {
         slug: typeof thesis.slug === "string" ? thesis.slug : thesisArg,
         mode: typeof r.mode === "string" ? r.mode : request.mode,
         query: typeof r.query === "string" || r.query === null ? (r.query as string | null) : request.query,
         region: typeof r.region === "string" ? r.region : request.region,
         days: typeof r.days === "number" || r.days === null ? (r.days as number | null) : request.days,
         retrievedAt: typeof r.retrieved_at === "string" ? r.retrieved_at : "",
         expiresAt: typeof r.expires_at === "string" ? r.expires_at : "",
         warnings: Array.isArray(r.warnings)
          ? (r.warnings as unknown[]).filter((w): w is string => typeof w === "string")
          : [],
        };
        rows = Array.isArray(r.videos) ? (r.videos as unknown[]).map(asVideo) : [];
        const ms = Date.parse(meta.expiresAt);
        if (Number.isFinite(ms)) timer = setTimeout(cleanup, Math.max(0, ms - Date.now()));
        try {
         tui.requestRender();
        } catch { }
       } else if (response.status === "disabled" || response.status === "unavailable") {
        failure = response;
        cleanup();
       } else {
        failure = clientFailure("malformed_response");
        cleanup();
       }
      })();
      return component;
     },
     { overlay: true },
    );
    if (failure) {
     const note = failureNotice(failure);
     if (note) ctx.ui.notify(note, "info");
    }
   } finally {
    panelOpen = false;
   }
  },
 });
}
