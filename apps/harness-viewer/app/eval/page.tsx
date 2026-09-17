import { listEvalRuns, listEvalScenarioResults, listExperiments } from "../../convex/queries";

export default function EvalComparePage(): JSX.Element {
  const runs = listEvalRuns();
  const experiments = listExperiments();
  const results = runs.length > 0 ? listEvalScenarioResults(runs[0].evalRunId) : [];
  return (
    <main>
      <h1>Eval compare</h1>
      <p>
        {runs.length} eval runs, {experiments.length} experiments
        (projections: evalRuns, evalScenarioResults, experiments)
      </p>
      <ul>
        {results.map((result) => (
          <li key={result.scenarioName}>
            {result.passed ? "PASS" : "FAIL"} {result.scenarioName}
          </li>
        ))}
      </ul>
    </main>
  );
}
