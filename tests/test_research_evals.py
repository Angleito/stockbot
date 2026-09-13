from app.research.evals.evaluators import EvalInput, evaluate


def test_crashed_fails_scenario_crashed():
    inp = EvalInput(scenario_name="crashed", answer_text="", scenario_crashed=True)
    result = evaluate(inp)
    assert result.violations == ("scenario-crashed",)
    assert not result.passed
    assert result.metrics.failed_count == 0
    assert result.metrics.recovered_count == 0


def test_unrecovered_fails_execution_failed():
    inp = EvalInput(scenario_name="unrecovered", answer_text="", failed_count=2, recovered_count=0)
    result = evaluate(inp)
    assert result.violations == ("scenario-execution-failed",)
    assert not result.passed
    assert result.metrics.failed_count == 2
    assert result.metrics.recovered_count == 0


def test_fully_recovered_passes():
    inp = EvalInput(scenario_name="recovered", answer_text="", failed_count=2, recovered_count=2)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 2
    assert result.metrics.recovered_count == 2


def test_over_recovery_passes():
    inp = EvalInput(scenario_name="over", answer_text="", failed_count=1, recovered_count=5)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 1
    assert result.metrics.recovered_count == 5


def test_no_failures_passes():
    inp = EvalInput(scenario_name="clean", answer_text="", failed_count=0, recovered_count=0)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 0
    assert result.metrics.recovered_count == 0


def test_empty_answer_still_fails():
    inp = EvalInput(scenario_name="empty", answer_text="", evidence_ids=(), failed_count=1, requires_evidence=False)
    result = evaluate(inp)
    assert not result.passed
    assert result.violations == ("scenario-execution-failed",)
