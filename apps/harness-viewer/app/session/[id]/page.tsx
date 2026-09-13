import { getResearchRun } from "../../../convex/queries";

export default function SessionTimelinePage(): JSX.Element {
  const run = getResearchRun("");
  return (
    <main>
      <h1>Session timeline</h1>
      <p>
        {run === null
          ? "No session selected (projection stub; read researchRuns by sessionId)"
          : `${run.sessionId} wave ${run.waveId} [${run.status}]`}
      </p>
    </main>
  );
}
