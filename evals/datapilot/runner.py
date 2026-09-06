"""Repeatable offline runner for DataPilot's synthetic application-layer eval."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datapilot.agent.analyst import Analyst, AnalystError, compare_grouped_metrics
from datapilot.agent.follow_up import (
    FollowUpResolution,
    FollowUpResolver,
    SessionContextExtractor,
)
from datapilot.agent.graph import (
    WorkflowRun,
    commit_session_context,
    prepare_session_turn,
    run_session_turn,
)
from datapilot.agent.planner import Planner, PlannerResult
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import (
    ReviewerResult,
    SQLResult,
    SessionContext,
    TaskItem,
    TimeRangeContext,
    create_initial_state,
)
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector
from evals.datapilot.metrics import EvalObservation, calculate_metrics


CASES_PATH = Path(__file__).with_name("cases.json")


class SequenceModel:
    """Deterministic model boundary; it never performs network I/O."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        self.calls += 1
        if not self.responses:
            raise AssertionError("offline fake model response exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeWren:
    """Small Wren boundary fake with observable planning and query calls."""

    def __init__(self, results: list[WrenQueryResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[str] = []

    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        self.calls.append("fetch_context")
        return {
            "strategy": "full",
            "schema": "orders(quarter, region, category, gmv)",
        }

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        del question, limit
        self.calls.append("recall_queries")
        return []

    def dry_plan(self, sql: str) -> str:
        self.calls.append("dry_plan")
        return sql

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        self.calls.append("query")
        if self.results:
            return self.results.pop(0)
        return WrenQueryResult(
            columns=["gmv"],
            rows=[{"gmv": 125}],
            row_count=1,
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        del nl, sql, tags
        self.calls.append("store_query")


def _task(
    task_id: str,
    description: str,
    task_type: str = "query",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "description": description,
        "task_type": task_type,
        "depends_on": depends_on or [],
        "status": "pending",
    }


def _plan_payload(scenario: str) -> str:
    if scenario == "simple_question":
        tasks = [_task("answer", "Explain the metric.", "response")]
        requires_database = False
    elif scenario == "single_query":
        tasks = [
            _task("query", "Retrieve the requested GMV."),
            _task("answer", "Answer from verified GMV.", "response", ["query"]),
        ]
        requires_database = True
    elif scenario == "multi_step_analysis":
        tasks = [
            _task("q2", "Retrieve Q2 category GMV."),
            _task("q3", "Retrieve Q3 category GMV."),
            _task("analysis", "Compare category GMV.", "analysis", ["q2", "q3"]),
            _task("answer", "Answer from comparison.", "response", ["analysis"]),
        ]
        requires_database = True
    elif scenario == "follow_up":
        tasks = [_task("follow", "Resolve the follow-up.")]
        requires_database = True
    else:
        raise ValueError(f"unsupported plan scenario: {scenario}")
    follow_up = scenario == "follow_up"
    return json.dumps(
        {
            "intent": scenario,
            "reason_summary": "Synthetic offline contract case.",
            "tasks": tasks,
            "requires_database": requires_database,
            "requires_context": follow_up,
            "is_follow_up": follow_up,
        },
        ensure_ascii=False,
    )


def _approve(task_id: str = "query") -> str:
    return json.dumps(
        {
            "decision": "approve",
            "reason_summary": "SQL and result match the current task.",
            "issues": [],
            "retry_instruction": None,
            "confidence": 0.99,
        }
    )


def _retry(issue: str) -> str:
    return json.dumps(
        {
            "decision": "retry",
            "reason_summary": f"Detected {issue}.",
            "issues": [
                {
                    "issue_type": issue,
                    "description": f"Correct the {issue}.",
                }
            ],
            "retry_instruction": "Correct only the current task.",
            "confidence": 0.95,
        }
    )


def _sql(sql: str) -> str:
    return json.dumps({"sql": sql, "summary": "Synthetic read query."})


def _final_answer(source: str) -> str:
    return json.dumps(
        {
            "answer": "The verified synthetic result is complete.",
            "key_findings": ["Used verified task outputs only."],
            "source_task_ids": [source],
        }
    )


def _context_payload() -> str:
    return json.dumps(
        {
            "metrics": ["GMV"],
            "dimensions": ["category"],
            "time_range": {"labels": ["Q2", "Q3"], "start": None, "end": None},
            "filters": {},
            "entities": {},
            "analysis_goal": "Compare synthetic GMV.",
        }
    )


def _previous() -> SessionContext:
    return SessionContext(
        session_id="eval-session",
        turn_index=1,
        metrics=["GMV"],
        dimensions=["category"],
        time_range=TimeRangeContext(labels=["Q2", "Q3"]),
        filters={},
        analysis_goal="Compare category GMV.",
    )


def _resolution_payload(scenario: str) -> str:
    previous = _previous()
    filters: dict[str, list[str]] = {}
    metrics = list(previous.metrics)
    labels = list(previous.time_range.labels)
    inherited = ["metrics", "dimensions", "time_range", "analysis_goal"]
    overridden: list[str] = []
    query = "Compare Q2 and Q3 category GMV."
    can_resolve = True
    missing: list[str] = []
    if scenario == "inherit_add_region":
        filters = {"region": ["华南地区"]}
        inherited.append("entities")
        overridden = ["filters"]
        query = "Compare Q2 and Q3 category GMV in 华南地区."
    elif scenario == "override_time":
        labels = ["Q3"]
        inherited = ["metrics", "dimensions", "filters", "analysis_goal"]
        overridden = ["time_range"]
        query = "Show Q3 category GMV."
    elif scenario == "override_metric":
        metrics = ["订单量"]
        inherited = ["dimensions", "time_range", "filters", "analysis_goal"]
        overridden = ["metrics"]
        query = "Compare Q2 and Q3 category order count."
    elif scenario == "ambiguous":
        can_resolve = False
        missing = ["requested_scope"]
        query = ""
        inherited = []
    return json.dumps(
        {
            "resolved_query": query or None,
            "inherited_fields": inherited,
            "overridden_fields": overridden,
            "missing_fields": missing,
            "can_resolve": can_resolve,
            "reason_summary": "Synthetic follow-up resolution.",
            "metrics": metrics,
            "dimensions": ["category"],
            "time_range": {"labels": labels, "start": None, "end": None},
            "filters": filters,
            "entities": {},
            "analysis_goal": previous.analysis_goal,
        },
        ensure_ascii=False,
    )


def _run_planner(case: dict[str, Any]) -> EvalObservation:
    state = create_initial_state(case["query"])
    trace = TraceCollector(trace_id=state["trace_id"])
    model = SequenceModel([_plan_payload(case["scenario"])])
    result = Planner(model).plan(
        state,
        trace=trace,
    )
    expected = case["expected"]
    intent_correct = result.intent.value == expected["intent"]
    plan_valid = (
        len(result.tasks) == expected["task_count"]
        and len({task.task_id for task in result.tasks}) == len(result.tasks)
        and not state["generated_sql"]
    )
    return EvalObservation(
        case_id=case["id"],
        passed=intent_correct and plan_valid,
        planner_intent_correct=intent_correct,
        planner_plan_valid=plan_valid,
    )


def _run_sql(case: dict[str, Any]) -> EvalObservation:
    task = TaskItem("query", "Retrieve the synthetic metric.", "query")
    state = create_initial_state("Retrieve the synthetic metric.")
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    filters = case.get("filters", {})
    if filters:
        state["was_follow_up"] = True
        state["resolved_query"] = "Retrieve GMV for 华南."
        state["follow_up_resolution"] = FollowUpResolution(
            filters=filters,
            resolved_query=state["resolved_query"],
            overridden_fields=["filters"],
            can_resolve=True,
            reason_summary="Synthetic filter.",
        )
    model = SequenceModel([_sql(sql) for sql in case["sql"]])
    tools = FakeWren()
    trace = TraceCollector(trace_id=state["trace_id"])
    result = SQLAgent(model_client=model, wren_tools=tools).execute_task(
        state,
        task,
        trace=trace,
    )
    expected = case["expected"]
    if expected == "safety_rejection":
        rejected = not result.success and "dry_plan" not in tools.calls
        return EvalObservation(
            case_id=case["id"],
            passed=rejected,
            sql_safety_rejected=rejected,
            technical_retries=result.retry_count,
            semantic_retries=0,
        )
    expected_retries = 1 if expected == "success_after_retry" else 0
    passed = result.success and result.retry_count == expected_retries
    if filters:
        passed = passed and "'华南'" in result.sql and "South China" not in result.sql
    return EvalObservation(
        case_id=case["id"],
        passed=passed,
        sql_execution_success=result.success,
        technical_retries=result.retry_count,
        semantic_retries=0,
    )


def _run_reviewer(case: dict[str, Any]) -> EvalObservation:
    expected = case["expected"]
    task = TaskItem("query", "Retrieve category GMV.", "query", status="executed")
    state = create_initial_state("Retrieve category GMV.")
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    result = SQLResult(
        task_id="query",
        sql="SELECT category, SUM(gmv) AS gmv FROM orders GROUP BY category",
        columns=["category", "gmv"],
        rows=[{"category": "A", "gmv": 100}],
        row_count=1,
    )
    response = (
        _approve()
        if expected["decision"] == "approve"
        else _retry(expected["issue"])
    )
    trace = TraceCollector(trace_id=state["trace_id"])
    review = Reviewer(model_client=SequenceModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )
    issue_types = {issue.issue_type for issue in review.issues}
    correct = review.decision == expected["decision"]
    if expected["issue"]:
        correct = correct and expected["issue"] in issue_types
    return EvalObservation(
        case_id=case["id"],
        passed=correct,
        reviewer_decision_correct=correct,
    )


def _run_analyst(case: dict[str, Any]) -> EvalObservation:
    if case["scenario"] == "ambiguous":
        q2 = TaskItem("q2", "Q2 category GMV.", "query", status="completed")
        q3 = TaskItem("q3", "Q3 category GMV.", "query", status="completed")
        task = TaskItem("analysis", "Compare periods.", "analysis", ["q2", "q3"])
        state = create_initial_state("Compare periods.")
        state["task_plan"] = [q2, q3, task]
        state["pending_tasks"] = [task]
        state["completed_tasks"] = [q2, q3]
        state["sql_results"] = [
            SQLResult(
                "q2",
                "SELECT category, SUM(gmv) AS gmv FROM orders",
                columns=["category", "gmv"],
                rows=[{"category": "A", "gmv": 100}],
                row_count=1,
            ),
            SQLResult(
                "q3",
                "SELECT category, 80 AS q2_gmv, 70 AS q3_gmv FROM orders",
                columns=["category", "q2_gmv", "q3_gmv"],
                rows=[{"category": "A", "q2_gmv": 80, "q3_gmv": 70}],
                row_count=1,
            ),
        ]
        state["review_results"] = [
            ReviewerResult("q2", "approve", "Verified.", confidence=1.0),
            ReviewerResult("q3", "approve", "Verified.", confidence=1.0),
        ]
        trace = TraceCollector(trace_id=state["trace_id"])
        try:
            Analyst(model_client=SequenceModel([])).execute_task(
                state,
                task,
                trace=trace,
            )
        except AnalystError as exc:
            correct = case["expected"]["error"] in str(exc)
        else:
            correct = False
        return EvalObservation(case_id=case["id"], passed=correct)

    left = [
        {"category": key, "gmv": value} for key, value in case["left"].items()
    ]
    right = [
        {"category": key, "gmv": value} for key, value in case["right"].items()
    ]
    derived = compare_grouped_metrics(
        left,
        right,
        dimension_column="category",
        metric_column="gmv",
        left_label="Q2",
        right_label="Q3",
    )
    rows = {row["key"]: row for row in derived["comparison"]}
    correct = True
    for key, expected in case["expected"].items():
        if key == "largest_decline":
            correct = correct and derived["largest_decline"]["key"] == expected
            continue
        correct = correct and all(
            rows[key][name] == value for name, value in expected.items()
        )
    return EvalObservation(
        case_id=case["id"],
        passed=correct,
        analysis_numeric_correct=correct,
    )


def _planner_result(scenario: str, query: str) -> PlannerResult:
    state = create_initial_state(query)
    return Planner(SequenceModel([_plan_payload(scenario)])).plan(
        state,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )


def _run_follow_up(case: dict[str, Any]) -> EvalObservation:
    scenario = case["scenario"]
    previous = _previous()
    correct = False
    if scenario in {"inherit_add_region", "override_time", "override_metric"}:
        result = FollowUpResolver(
            SequenceModel([_resolution_payload(scenario)])
        ).resolve(case["query"], previous, trace=TraceCollector())
        if scenario == "inherit_add_region":
            correct = (
                result.filters == {"region": ["华南"]}
                and result.metrics == previous.metrics
                and result.dimensions == previous.dimensions
                and result.time_range == previous.time_range
            )
        elif scenario == "override_time":
            correct = result.time_range.labels == ["Q3"]
        else:
            correct = result.metrics == ["订单量"] and "GMV" not in result.metrics
    elif scenario == "new_topic":
        store = SessionMemoryStore()
        store.save(previous)
        state = create_initial_state(case["query"], previous_session=previous)
        planning = prepare_session_turn(
            state,
            Planner(SequenceModel([_plan_payload("single_query")])),
            FollowUpResolver(SequenceModel([])),
            store,
            trace=TraceCollector(trace_id=state["trace_id"]),
        )
        correct = planning.can_execute and state["resolved_query"] is None
    elif scenario == "ambiguous_no_context":
        ambiguous = FollowUpResolver(
            SequenceModel([_resolution_payload("ambiguous")])
        ).resolve(case["query"], previous, trace=TraceCollector())
        store = SessionMemoryStore()
        blank = store.create("blank")
        state = create_initial_state(case["query"], previous_session=blank)
        no_context = prepare_session_turn(
            state,
            Planner(SequenceModel([_plan_payload("follow_up")])),
            FollowUpResolver(SequenceModel([])),
            store,
            trace=TraceCollector(trace_id=state["trace_id"]),
        )
        correct = not ambiguous.can_resolve and not no_context.can_execute
    elif scenario == "failed_turn_poisoning":
        store = SessionMemoryStore()
        store.save(previous)
        state = create_initial_state(case["query"], previous_session=previous)
        updated = commit_session_context(
            state,
            _planner_result("single_query", case["query"]),
            WorkflowRun((), (), None),
            store,
            SessionContextExtractor(SequenceModel([])),
            trace=TraceCollector(trace_id=state["trace_id"]),
        )
        after = store.get(previous.session_id)
        correct = not updated and after == previous
    return EvalObservation(
        case_id=case["id"],
        passed=correct,
        follow_up_resolution_correct=correct,
    )


def _e2e_case(case: dict[str, Any]) -> EvalObservation:
    scenario = case["scenario"]
    if scenario == "single_query":
        plan = _plan_payload("single_query")
        sql_responses = [_sql("SELECT SUM(gmv) AS gmv FROM orders")]
        review_responses = [_approve()]
        wren_results = [
            WrenQueryResult(["gmv"], [{"gmv": 125}], 1),
        ]
        answer_source = "query"
    elif scenario == "multi_analysis":
        plan = _plan_payload("multi_step_analysis")
        sql_responses = [
            _sql(
                "SELECT category, SUM(gmv) AS gmv FROM orders "
                "WHERE quarter = 'Q2' GROUP BY category"
            ),
            _sql(
                "SELECT category, SUM(gmv) AS gmv FROM orders "
                "WHERE quarter = 'Q3' GROUP BY category"
            ),
        ]
        review_responses = [_approve(), _approve()]
        wren_results = [
            WrenQueryResult(
                ["category", "gmv"],
                [{"category": "A", "gmv": 100}, {"category": "B", "gmv": 200}],
                2,
            ),
            WrenQueryResult(
                ["category", "gmv"],
                [{"category": "A", "gmv": 80}, {"category": "B", "gmv": 210}],
                2,
            ),
        ]
        answer_source = "analysis"
    else:
        plan = _plan_payload("single_query")
        sql_responses = [
            _sql("SELECT COUNT(*) AS value FROM orders"),
            _sql("SELECT SUM(gmv) AS gmv FROM orders"),
        ]
        review_responses = [_retry("metric_mismatch"), _approve()]
        wren_results = [
            WrenQueryResult(["value"], [{"value": 4}], 1),
            WrenQueryResult(["gmv"], [{"gmv": 125}], 1),
        ]
        answer_source = "query"

    store = SessionMemoryStore()
    session = store.create(f"eval-{scenario}")
    state = create_initial_state(
        "Synthetic end-to-end question.",
        previous_session=session,
    )
    trace = TraceCollector(trace_id=state["trace_id"])
    run = run_session_turn(
        state,
        Planner(SequenceModel([plan])),
        SQLAgent(
            model_client=SequenceModel(sql_responses),
            wren_tools=FakeWren(wren_results),
        ),
        Reviewer(model_client=SequenceModel(review_responses)),
        Analyst(model_client=SequenceModel([_final_answer(answer_source)])),
        FollowUpResolver(SequenceModel([])),
        SessionContextExtractor(SequenceModel([_context_payload()])),
        store,
        trace=trace,
    )
    events = trace.get_events()
    technical = sum(event.event_type is EventType.SQL_RETRY for event in events)
    semantic = sum(
        event.event_type is EventType.SEMANTIC_RETRY_STARTED for event in events
    )
    passed = run.success
    if scenario == "multi_analysis":
        passed = passed and state["analysis_results"][0].derived_values[
            "largest_decline"
        ]["key"] == "A"
    if scenario == "semantic_correction":
        passed = passed and semantic == 1
    return EvalObservation(
        case_id=case["id"],
        passed=passed,
        semantic_correction_success=(
            passed if scenario == "semantic_correction" else None
        ),
        end_to_end_task_success=passed,
        technical_retries=technical,
        semantic_retries=semantic,
    )


_RUNNERS = {
    "planner": _run_planner,
    "sql": _run_sql,
    "reviewer": _run_reviewer,
    "analyst": _run_analyst,
    "follow_up": _run_follow_up,
    "end_to_end": _e2e_case,
}


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    """Load and minimally validate the committed synthetic dataset."""

    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("eval cases must be a JSON array")
    identifiers = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(identifiers) != len(cases) or len(set(identifiers)) != len(cases):
        raise ValueError("eval case ids must be present and unique")
    return cases


def run_eval(path: Path = CASES_PATH) -> dict[str, Any]:
    """Execute all offline cases and return measured results."""

    started = perf_counter()
    observations: list[EvalObservation] = []
    for case in load_cases(path):
        try:
            observation = _RUNNERS[case["component"]](case)
        except Exception as exc:
            observation = EvalObservation(
                case_id=case["id"],
                passed=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        observations.append(observation)
    metrics = calculate_metrics(observations)
    failures = [
        {"case_id": item.case_id, "detail": item.detail or "expectation failed"}
        for item in observations
        if not item.passed
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "network": "disabled by design",
            "models": "deterministic fakes",
            "database": "FakeWren with synthetic rows",
        },
        "case_count": len(observations),
        "metrics": metrics,
        "failed_cases": failures,
        "duration_seconds": round(perf_counter() - started, 6),
        "cases": [asdict(item) for item in observations],
    }


def render_markdown(result: dict[str, Any]) -> str:
    """Render the measured result artifact committed with the freeze sprint."""

    lines = [
        "# DataPilot Offline Eval Results",
        "",
        f"Generated: `{result['generated_at']}`",
        "",
        "## Environment",
        "",
        f"- Python: `{result['environment']['python']}`",
        "- Network: disabled by design",
        "- Models: deterministic fakes",
        "- Database: FakeWren with synthetic rows",
        f"- Runtime: `{result['duration_seconds']:.6f}` seconds",
        f"- Cases: **{result['case_count']}**",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for name, value in result["metrics"].items():
        lines.append(f"| `{name}` | `{value:.6f}` |")
    lines.extend(["", "## Failed Cases", ""])
    if result["failed_cases"]:
        for failure in result["failed_cases"]:
            lines.append(f"- `{failure['case_id']}`: {failure['detail']}")
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "## Known Limitations",
            "",
            "- This is an offline application-layer contract eval, not an "
            "academic benchmark.",
            "- Fake model outputs test validation and orchestration, not "
            "live-model quality.",
            "- FakeWren makes CI deterministic; real Wren/DuckDB validation "
            "is recorded separately.",
            "- Token usage and cost are not collected by the current trace model.",
            "- Real LLM evaluation was not run for this sprint.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_eval()
    if args.output:
        args.output.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
