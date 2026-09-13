import { listResearchRuns } from "../convex/queries";

export default function RunsPage(): JSX.Element {
  const runs = listResearchRuns();
  return (
    <main>
      <h1>Runs</h1>
      <p>
        {runs.length} sessions (projection stub; wire a Convex client to populate researchRuns)
      </p>
      <ul>
        {runs.map((run) => (
          <li key={run.sessionId}>
            {run.sessionId} wave {run.waveId} [{run.status}] {run.question}
          </li>
        ))}
      </ul>
    </main>
  );
}
