import { getResearchRun, listResearchRuns } from "../../../convex/queries";

export default function SessionTimelinePage({ params }: { params: { id: string } }): JSX.Element {
  const direct = params.id !== "" ? getResearchRun(params.id) : null;
  const run = direct ?? listResearchRuns()[0] ?? null;
  if (run === null) {
    return (
      <main>
        <h1>Session timeline</h1>
        <p>No trace persisted for this session (research://{params.id === "" ? "unselected" : params.id}).</p>
      </main>
    );
  }
  const childrenByParent = new Map<string | null, typeof run.jobs>();
  for (const job of run.jobs) {
    const key = job.parentJobId;
    const group = childrenByParent.get(key);
    if (group === undefined) {
      childrenByParent.set(key, [job]);
    } else {
      group.push(job);
    }
  }
  const roots = childrenByParent.get(null) ?? [];
  const freezeIds = new Set(run.freezes.flatMap((fr) => fr.evidenceIds));
  const recomputed = run.committeeRuns.length > 1 && run.committeeRuns.every((entry) => {
    if (typeof entry !== "object" || entry === null || !("freezeId" in entry)) {
      return false;
    }
    const freezeId = entry.freezeId;
    const lastFreeze = run.freezes[run.freezes.length - 1];
    return typeof freezeId === "string" && lastFreeze !== undefined && freezeId === lastFreeze.freezeId;
  });
  const reused = run.events.filter((evt) => evt.eventType === "scout.reused" || evt.eventType === "source.reused");
  const retried = run.events.filter((evt) => evt.eventType === "scout.retried");
  const failed = run.jobs.filter((job) => job.status === "failed" || job.status === "cancelled");
  return (
    <main>
      <h1>Session timeline</h1>
      <p>
        <a href={`research://${run.sessionId}`}>{run.sessionId}</a> wave {run.waveId} [{run.status}] {run.question}
      </p>
      <p>
        Provider: {run.provider ?? "unknown"} model: {run.model ?? "unknown"}
      </p>
      {run.traceId === null ? (
        <p>Trace missing for this run; showing Job persistence only.</p>
      ) : null}
      {run.status !== "completed" && run.status !== "failed" && run.status !== "cancelled" ? (
        <p>Run in progress [{run.status}]; jobs and events below are partial.</p>
      ) : null}
      {run.status === "failed" ? (
        <p>Run failed; terminal failures listed under Jobs without exceptions.</p>
      ) : null}
      {run.evidence.length === 0 ? (
        <p>Empty evidence for this run.</p>
      ) : null}
      {recomputed ? <p>Committee recomputed on resume against the same freeze.</p> : null}
      {reused.length > 0 ? (
        <p>
          Reused on resume: {reused.map((evt) => String(evt.payload["job_id"] ?? evt.payload["assignment_id"] ?? evt.eventType)).join(", ")}
        </p>
      ) : null}
      {retried.length > 0 ? (
        <p>
          Retried under the same Job without duplicates: {retried.map((evt) => String(evt.payload["job_id"] ?? evt.eventType)).join(", ")}
        </p>
      ) : null}
      <h2>Jobs</h2>
      {roots.length === 0 && run.jobs.length === 0 ? (
        <p>No Jobs persisted.</p>
      ) : null}
      <ul>
        {roots.map((job) => (
          <li key={job.jobId}>
            <a href={`job://${job.jobId}`}>{job.jobId}</a> {job.jobType} [{job.status}]
            {job.failureCategory !== null ? ` ${job.failureCategory}: ${job.failureMessage ?? ""}` : ""}
            <ul>
              {(childrenByParent.get(job.jobId) ?? []).map((child) => (
                <li key={child.jobId}>
                  <a href={`job://${child.jobId}`}>{child.jobId}</a> {child.jobType}
                  {child.assignmentId !== null ? ` ${child.assignmentId}` : ""}
                  {child.role !== null ? ` (${child.role})` : ""} [{child.status}]
                  {child.failureCategory !== null ? ` ${child.failureCategory}: ${child.failureMessage ?? ""}` : ""}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
      <h2>Events</h2>
      <ol>
        {[...run.events]
          .sort((a, b) => a.seq - b.seq)
          .map((evt) => (
            <li key={`${evt.seq}`}>
              [{evt.seq}] {evt.eventType} {JSON.stringify(evt.payload)}
            </li>
          ))}
      </ol>
      <h2>Claims</h2>
      {run.claims.length === 0 ? (
        <p>No validated claims.</p>
      ) : null}
      <ul>
        {run.claims.map((claim, index) => (
          <li key={`${index}`}>
            {claim.text} [
            {claim.evidenceIds.map((eid, inner) => (
              <span key={eid}>
                {inner > 0 ? ", " : ""}
                <a href={`freeze://${eid}`}>{eid}</a>
                {freezeIds.has(eid) ? "" : " (outside freeze)"}
              </span>
            ))}
            ]
          </li>
        ))}
      </ul>
      {failed.length > 0 ? (
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
      ) : null}
      <h2>Conclusion</h2>
      <p>{run.conclusion ?? "No conclusion persisted."}</p>
    </main>
  );
}
