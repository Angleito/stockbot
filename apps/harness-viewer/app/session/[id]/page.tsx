import { getResearchRun, listResearchRuns } from "../../../convex/queries";
import type { ResearchRun } from "../../../convex/schema";

type Run = ResearchRun;
type RunJob = Run["jobs"][number];

function findRun(sessionId: string): Run | null {
  const direct = sessionId !== "" ? getResearchRun(sessionId) : null;
  return direct ?? listResearchRuns()[0] ?? null;
}

function waveIds(run: Run): number[] {
  const stored = run.waves ?? [];
  if (stored.length !== 0) {
    return [...stored].sort((a, b) => a - b);
  }
  const ids = run.jobs.map((job) => job.waveId);
  const unique = [...new Set(ids.length === 0 ? [run.waveId] : ids)];
  return unique.sort((a, b) => a - b);
}

function jobsForWave(run: Run, wave: number): RunJob[] {
  return run.jobs.filter((job) => job.waveId === wave);
}

function frozenIds(run: Run): string[] {
  return run.freezes.flatMap((fr) => fr.evidenceIds);
}

function deskCounts(run: Run): string[] {
  return (["SEC", "FINRA", "WEB"] as const).map((domain) => {
    const count = run.evidence.filter((ev) => (ev.domain ?? "").toUpperCase() === domain).length;
    return `${domain} ${count}`;
  });
}

function TopHeader({ run }: { run: Run }): JSX.Element {
  const names = run.evidence.map((ev) => ev.sourceName).filter((name) => name !== "");
  const sources = [...new Set(names)];
  return (
    <>
      <h1>Session</h1>
      <p>Question: {run.question}</p>
      <p>
        Status: {run.status} — wave {run.waveId} — waves [{waveIds(run).join(", ")}]
      </p>
      <p>
        Evidence: {run.evidence.length} records — sources: {sources.length} ({sources.join(", ") || "none"})
      </p>
      <p>Evidence by desk: {deskCounts(run).join(", ")}</p>
      <p>
        Provider: {run.provider ?? "unknown"} model: {run.model ?? "unknown"}
      </p>
      <p>
        <a href={`research://${run.sessionId}`}>{run.sessionId}</a> asOf: {run.asOf ?? "unbounded"} updated:{" "}
        {run.updatedAt}
      </p>
      {run.traceId === null ? <p>Trace missing for this run; showing Job persistence only.</p> : null}
      {run.status !== "completed" && run.status !== "failed" && run.status !== "cancelled" ? (
        <p>
          Run in progress [{run.status}]; jobs and events below are partial.
        </p>
      ) : null}
      {run.status === "failed" ? <p>Run failed; terminal failures listed under Jobs without exceptions.</p> : null}
      {run.evidence.length === 0 ? <p>Empty evidence for this run.</p> : null}
    </>
  );
}

function DeskItem({ job }: { job: RunJob }): JSX.Element {
  const detail = [
    job.jobType,
    job.sourceDomain ?? "domain unknown",
    job.role === null ? null : `role ${job.role}`,
    job.assignmentId === null ? null : `assignment ${job.assignmentId}`,
    `[${job.status}]`,
    job.failureCategory === null ? null : `${job.failureCategory}: ${job.failureMessage ?? ""}`,
  ]
    .filter((part) => part !== null)
    .join(" ");
  return (
    <>
      <a href={`job://${job.jobId}`}>{job.jobId}</a> {detail}
    </>
  );
}

function WaveDesks({ run }: { run: Run }): JSX.Element {
  const waves = waveIds(run);
  return (
    <>
      <h2>Waves and desks</h2>
      {waves.map((wave) => (
        <section key={wave}>
          <h3>Wave {wave}</h3>
          {jobsForWave(run, wave).length === 0 ? (
            <p>No jobs persisted for wave {wave}.</p>
          ) : (
            <ul>
              {jobsForWave(run, wave).map((job) => (
                <li key={job.jobId}>
                  <DeskItem job={job} />
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </>
  );
}

function FreezeCommittee({ run }: { run: Run }): JSX.Element {
  const frozen = frozenIds(run);
  const failed = run.jobs.filter((job) => job.status === "failed" || job.status === "cancelled");
  const finalResult = run.finalResult ?? null;
  const disagreements = finalResult === null ? [] : finalResult.disagreements;
  return (
    <>
      <h2>Freeze and committee</h2>
      {run.freezes.length === 0 ? <p>No freeze persisted.</p> : null}
      <ul>
        {run.freezes.map((fr) => (
          <li key={fr.freezeId}>
            <a href={`freeze://${fr.freezeId}`}>{fr.freezeId}</a> [{fr.evidenceIds.join(", ") || "empty"}]
          </li>
        ))}
      </ul>
      <p>Committee runs: {run.committeeRuns.length}</p>
      {disagreements.length === 0 ? (
        <p>No disagreements persisted.</p>
      ) : (
        <>
          <h3>Disagreement</h3>
          <ul>
            {disagreements.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        </>
      )}
      {failed.length === 0 ? null : (
        <>
          <h3>Failures</h3>
          <ul>
            {failed.map((job) => (
              <li key={job.jobId}>
                <a href={`job://${job.jobId}`}>{job.jobId}</a> {job.jobType} [{job.status}]{" "}
                {job.failureCategory ?? "no category"}: {job.failureMessage ?? "no message"}
              </li>
            ))}
          </ul>
        </>
      )}
      <p>Freeze-contained claims only; ids outside [{frozen.join(", ") || "no freeze"}] render as outside freeze.</p>
    </>
  );
}

function NamedList({ title, items }: { title: string; items: string[] }): JSX.Element | null {
  if (items.length === 0) {
    return null;
  }
  return (
    <>
      <h3>{title}</h3>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </>
  );
}

function FinalSection({ run }: { run: Run }): JSX.Element {
  const final = run.finalResult ?? null;
  if (final === null) {
    return (
      <>
        <h2>Final</h2>
        <p>No final persisted.</p>
      </>
    );
  }
  const frozen = frozenIds(run);
  return (
    <>
      <h2>Final</h2>
      <p>{final.answer || "No answer persisted."}</p>
      {final.executiveSummary !== "" && final.executiveSummary !== final.answer ? (
        <p>Bottom line: {final.executiveSummary}</p>
      ) : null}
      {final.consensus !== "" ? <p>Consensus: {final.consensus}</p> : null}
      {final.baseCase !== "" ? <p>Base case: {final.baseCase}</p> : null}
      {final.bullCase !== "" ? <p>Bull case: {final.bullCase}</p> : null}
      {final.bearCase !== "" ? <p>Bear case: {final.bearCase}</p> : null}
      <NamedList title="Positioning" items={final.positioning} />
      <NamedList title="Catalysts" items={final.catalysts} />
      <NamedList title="Uncertainties" items={final.uncertainties} />
      <NamedList title="What changes the view" items={final.whatChangesTheView} />
      <NamedList title="Limitations" items={final.limitations} />
      {final.claims.length === 0 ? (
        <p>No validated claims.</p>
      ) : (
        <>
          <h3>Claims</h3>
          <ul>
            {final.claims.map((claim, index) => (
              <li key={index}>
                {claim.text} [
                {claim.evidenceIds.map((eid, inner) => (
                  <span key={eid}>
                    {inner > 0 ? ", " : ""}
                    <a href={`freeze://${eid}`}>{eid}</a>
                    {frozen.includes(eid) ? "" : " (outside freeze)"}
                  </span>
                ))}
                ]
              </li>
            ))}
          </ul>
        </>
      )}
      {final.sources.length === 0 ? null : (
        <>
          <h3>Sources</h3>
          <ul>
            {final.sources.map((source) => (
              <li key={source.evidenceId}>
                {source.domain}{source.integrityClass ? ` [${source.integrityClass}]` : ""} {source.document} [{source.evidenceId}]
              </li>
            ))}
          </ul>
        </>
      )}
      <p>
        Coverage: {JSON.stringify(final.coverage)} — allowed sources: {final.allowedSources.join(", ") || "unknown"} —
        freeze: {final.freezeId || "unknown"}
      </p>
    </>
  );
}

function AuditLog({ run }: { run: Run }): JSX.Element {
  const events = [...run.events].sort((a, b) => a.seq - b.seq);
  const artifacts = run.coverageArtifacts ?? [];
  return (
    <>
      <h2>Audit log</h2>
      <details>
        <summary>
          {events.length} journal events, {run.jobs.length} jobs, {artifacts.length} coverage artifacts
        </summary>
        <ol>
          {events.map((evt) => (
            <li key={`${evt.seq}`}>
              [{evt.seq}] {evt.eventType} {JSON.stringify(evt.payload)}
            </li>
          ))}
        </ol>
        {artifacts.length === 0 ? null : (
          <ul>
            {artifacts.map((artifact) => (
              <li key={artifact.artifactId}>
                {artifact.artifactId} wave {artifact.waveId} search {artifact.searchId} query {artifact.query}:{" "}
                {artifact.claimText}
              </li>
            ))}
          </ul>
        )}
      </details>
    </>
  );
}

function RunPage({ run }: { run: Run }): JSX.Element {
  return (
    <main>
      <TopHeader run={run} />
      <WaveDesks run={run} />
      <FreezeCommittee run={run} />
      <FinalSection run={run} />
      <AuditLog run={run} />
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
