import { listFailureRecords } from "../../convex/queries";

export default function EvidenceDrillPage(): JSX.Element {
  const failures = listFailureRecords("");
  return (
    <main>
      <h1>Evidence drill</h1>
      <p>{failures.length} untraceable-claim records (projection stub: failureRecords)</p>
      <ul>
        {failures.map((failure) => (
          <li key={failure.failureId}>
            {failure.scenarioName}: {failure.violation}
          </li>
        ))}
      </ul>
    </main>
  );
}
