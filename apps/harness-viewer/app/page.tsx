import { listResearchRuns } from "../convex/queries";

export default function RunsPage(): JSX.Element {
  const runs = listResearchRuns();
  return (
    <main>
      <h1>Runs</h1>
      {runs.length === 0 ? (
        <p>No research runs persisted yet. Run a live research session, then re-run scripts/export_harness_viewer.py.</p>
      ) : (
        <p>
          {runs.length} sessions (read-only projections from trace persistence)
        </p>
      )}
      <ul>
        {runs.map((run) => (
          <li key={run.sessionId}>
            <a href={`research://${run.sessionId}`}>{run.sessionId}</a> wave {run.waveId} [{run.status}] {run.question}
          </li>
        ))}
      </ul>
    </main>
  );
}
