import { listResearchRuns } from "../../convex/queries";

export default function EvidenceDrillPage(): JSX.Element {
  const runs = listResearchRuns();
  const rows = runs.flatMap((run) =>
    run.evidence.map((ev) => ({ run, ev })),
  );
  return (
    <main>
      <h1>Evidence drill</h1>
      {rows.length === 0 ? (
        <p>No freeze-contained evidence persisted yet.</p>
      ) : (
        <p>{rows.length} evidence records with freeze-contained claim links below.</p>
      )}
      <ul>
        {rows.map(({ run, ev }) => (
          <li key={`${run.sessionId}:${ev.evidenceId}`}>
            <a href={`evidence://${ev.evidenceId}`}>{ev.evidenceId}</a> [{ev.domain ?? "SEC"}] {ev.subject} [
            {ev.knownAt ?? "known_at unknown"}] {ev.sourceName} <a href={`research://${run.sessionId}`}>{run.sessionId}</a>
          </li>
        ))}
      </ul>
    </main>
  );
}
