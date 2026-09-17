import { getResearchRun, listResearchRuns } from "../../../convex/queries";
import type { ResearchRun } from "../../../convex/schema";

type Run = ResearchRun;
type RunJob = Run["jobs"][number];
type RunEvent = Run["events"][number];
type JobMap = Map<string | null, Run["jobs"]>;

function addJobToGroup(childrenByParent: JobMap, job: RunJob): void {
  const prior = childrenByParent.get(job.parentJobId);
  if (prior === undefined) {
    childrenByParent.set(job.parentJobId, [job]);
  } else {
    childrenByParent.set(job.parentJobId, [...prior, job]);
  }
}

function groupJobsByParent(run: Run): JobMap {
  const childrenByParent: JobMap = new Map();
  for (const job of run.jobs) {
    addJobToGroup(childrenByParent, job);
  }
  return childrenByParent;
}

function isRecomputed(run: Run): boolean {
  return run.committeeRuns.length > 1 && run.committeeRuns.every((entry) => isSameFreezeEntry(run, entry));
}

function isObject(entry: unknown): entry is Record<string, unknown> {
  return typeof entry === "object" && entry !== null;
}

function freezeIdProp(entry: Record<string, unknown>): string | null {
  const value = entry["freezeId"];
  if (typeof value !== "string") {
    return null;
  }
  return value;
}

function entryFreezeId(entry: unknown): string | null {
  if (!isObject(entry)) {
    return null;
  }
  return freezeIdProp(entry);
}

function lastFreezeId(run: Run): string | null {
  const last = run.freezes.at(-1);
  if (last === undefined) {
    return null;
  }
  return last.freezeId;
}

function freezeIdsMatch(a: string | null, b: string | null): boolean {
  if (a === null) {
    return false;
  }
  return a === b;
}

function isSameFreezeEntry(run: Run, entry: unknown): boolean {
  return freezeIdsMatch(entryFreezeId(entry), lastFreezeId(run));
}

function TraceNotice({ run }: { run: Run }): JSX.Element | null {
  return run.traceId !== null ? null : <p>Trace missing for this run; showing Job persistence only.</p>;
}

function ProgressNotice({ run }: { run: Run }): JSX.Element | null {
  const done = ["completed", "failed", "cancelled"].includes(run.status);
  return done ? null : <p>Run in progress [{run.status}]; jobs and events below are partial.</p>;
}

function FailedNotice({ run }: { run: Run }): JSX.Element | null {
  return run.status !== "failed" ? null : <p>Run failed; terminal failures listed under Jobs without exceptions.</p>;
}

function EmptyEvidenceNotice({ run }: { run: Run }): JSX.Element | null {
  return run.evidence.length !== 0 ? null : <p>Empty evidence for this run.</p>;
}

function StatusNotices({ run }: { run: Run }): JSX.Element {
  return (
    <>
      <TraceNotice run={run} />
      <ProgressNotice run={run} />
      <FailedNotice run={run} />
      <EmptyEvidenceNotice run={run} />
    </>
  );
}

function isReusedEvent(evt: RunEvent): boolean {
  return evt.eventType === "scout.reused" || evt.eventType === "source.reused";
}

function eventLabel(evt: RunEvent, extra?: string): string {
  const fallback = extra === undefined ? null : evt.payload[extra];
  return String(evt.payload["job_id"] ?? fallback ?? evt.eventType);
}

function RecomputedNote({ show }: { show: boolean }): JSX.Element | null {
  return show ? <p>Committee recomputed on resume against the same freeze.</p> : null;
}

function ReusedNote({ events }: { events: Run["events"] }): JSX.Element | null {
  return events.length === 0 ? null : (
    <p>Reused on resume: {events.map((e) => eventLabel(e, "assignment_id")).join(", ")}</p>
  );
}

function RetriedNote({ events }: { events: Run["events"] }): JSX.Element | null {
  return events.length === 0 ? null : (
    <p>Retried under the same Job without duplicates: {events.map((e) => eventLabel(e)).join(", ")}</p>
  );
}

function ResumeNotices({ run, recomputed }: { run: Run; recomputed: boolean }): JSX.Element {
  return (
    <>
      <RecomputedNote show={recomputed} />
      <ReusedNote events={run.events.filter(isReusedEvent)} />
      <RetriedNote events={run.events.filter((evt) => evt.eventType === "scout.retried")} />
    </>
  );
}

function RunNotices({ run, recomputed }: { run: Run; recomputed: boolean }): JSX.Element {
  return (
    <>
      <StatusNotices run={run} />
      <ResumeNotices run={run} recomputed={recomputed} />
    </>
  );
}

function failureSuffix(failureCategory: string | null, failureMessage: string | null): string {
  return failureCategory === null ? "" : ` ${failureCategory}: ${failureMessage ?? ""}`;
}

function assignmentSuffix(assignmentId: string | null): string {
  return assignmentId === null ? "" : ` ${assignmentId}`;
}

function ChildRow({ child }: { child: RunJob }): JSX.Element {
  const assignment = assignmentSuffix(child.assignmentId);
  const role = child.role !== null ? ` (${child.role})` : "";
  return (
    <>
      <a href={`job://${child.jobId}`}>{child.jobId}</a> {child.jobType}
      {`${assignment}${role} [${child.status}]`}
      {failureSuffix(child.failureCategory, child.failureMessage)}
    </>
  );
}

function RootRow({ job, kids }: { job: RunJob; kids: Run["jobs"] }): JSX.Element {
  return (
    <>
      <a href={`job://${job.jobId}`}>{job.jobId}</a> {job.jobType} [{job.status}]
      {failureSuffix(job.failureCategory, job.failureMessage)}
      <ul>
        {kids.map((child) => (
          <li key={child.jobId}>
            <ChildRow child={child} />
          </li>
        ))}
      </ul>
    </>
  );
}

function hasJobs(run: Run, roots: Run["jobs"]): boolean {
  return roots.length !== 0 || run.jobs.length !== 0;
}

function EmptyJobs({ run, roots }: { run: Run; roots: Run["jobs"] }): JSX.Element | null {
  return hasJobs(run, roots) ? null : <p>No Jobs persisted.</p>;
}

function JobsSection({ run, childrenByParent }: { run: Run; childrenByParent: JobMap }): JSX.Element {
  const roots = childrenByParent.get(null) ?? [];
  return (
    <>
      <h2>Jobs</h2>
      <EmptyJobs run={run} roots={roots} />
      <ul>
        {roots.map((job) => (
          <li key={job.jobId}>
            <RootRow job={job} kids={childrenByParent.get(job.jobId) ?? []} />
          </li>
        ))}
      </ul>
    </>
  );
}

function EventsSection({ run }: { run: Run }): JSX.Element {
  const events = [...run.events].sort((a, b) => a.seq - b.seq);
  return (
    <>
      <h2>Events</h2>
      <ol>
        {events.map((evt) => (
          <li key={`${evt.seq}`}>
            [{evt.seq}] {evt.eventType} {JSON.stringify(evt.payload)}
          </li>
        ))}
      </ol>
    </>
  );
}

function ClaimRow({ text, ids, freezeIds }: { text: string; ids: string[]; freezeIds: Set<string> }): JSX.Element {
  return (
    <>
      {text} [
      {ids.map((eid, inner) => (
        <span key={eid}>
          {inner > 0 ? ", " : ""}
          <a href={`freeze://${eid}`}>{eid}</a>
          {freezeIds.has(eid) ? "" : " (outside freeze)"}
        </span>
      ))}
      ]
    </>
  );
}

function ClaimsSection({ run, freezeIds }: { run: Run; freezeIds: Set<string> }): JSX.Element {
  return (
    <>
      <h2>Claims</h2>
      {run.claims.length === 0 ? <p>No validated claims.</p> : null}
      <ul>
        {run.claims.map((claim, index) => (
          <li key={`${index}`}>
            <ClaimRow text={claim.text} ids={claim.evidenceIds} freezeIds={freezeIds} />
          </li>
        ))}
      </ul>
    </>
  );
}

function FailuresSection({ run }: { run: Run }): JSX.Element | null {
  const failed = run.jobs.filter((job) => job.status === "failed" || job.status === "cancelled");
  if (failed.length === 0) {
    return null;
  }
  return (
    <>
      <h2>Failures</h2>
      <ul>
        {failed.map((job) => (
          <li key={job.jobId}>
            <a href={`job://${job.jobId}`}>{job.jobId}</a> {job.jobType} [{job.status}]{" "}
            {job.failureCategory ?? "no category"}: {job.failureMessage ?? "no message"}
          </li>
        ))}
      </ul>
    </>
  );
}

function findRun(sessionId: string): Run | null {
  const direct = sessionId !== "" ? getResearchRun(sessionId) : null;
  return direct ?? listResearchRuns()[0] ?? null;
}

function RunHead({ run }: { run: Run }): JSX.Element {
  return (
    <>
      <h1>Session timeline</h1>
      <p>
        <a href={`research://${run.sessionId}`}>{run.sessionId}</a> wave {run.waveId} [{run.status}] {run.question}
      </p>
      <p>
        Provider: {run.provider ?? "unknown"} model: {run.model ?? "unknown"}
      </p>
    </>
  );
}

function RunPage({ run }: { run: Run }): JSX.Element {
  return (
    <main>
      <RunHead run={run} />
      <RunNotices run={run} recomputed={isRecomputed(run)} />
      <JobsSection run={run} childrenByParent={groupJobsByParent(run)} />
      <EventsSection run={run} />
      <ClaimsSection run={run} freezeIds={new Set(run.freezes.flatMap((fr) => fr.evidenceIds))} />
      <FailuresSection run={run} />
      <h2>Conclusion</h2>
      <p>{run.conclusion ?? "No conclusion persisted."}</p>
    </main>
  );
}

function missingSessionLabel(sessionId: string): string {
  return sessionId === "" ? "unselected" : sessionId;
}

export default function SessionTimelinePage({ params }: { params: { id: string } }): JSX.Element {
  const run = findRun(params.id);
  if (run === null) {
    return (
      <main>
        <h1>Session timeline</h1>
        <p>No trace persisted for this session (research://{missingSessionLabel(params.id)}).</p>
      </main>
    );
  }
  return <RunPage run={run} />;
}
