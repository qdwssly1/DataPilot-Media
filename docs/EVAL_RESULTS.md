# DataPilot Offline Eval Results

Generated: `2026-09-06T10:01:46.907378+00:00`

## Environment

- Python: `3.13.1`
- Network: disabled by design
- Models: deterministic fakes
- Database: FakeWren with synthetic rows
- Runtime: `0.008955` seconds
- Cases: **30**

## Metrics

| Metric | Value |
|---|---:|
| `planner_intent_accuracy` | `1.000000` |
| `planner_plan_valid_rate` | `1.000000` |
| `sql_execution_success_rate` | `1.000000` |
| `sql_safety_rejection_rate` | `1.000000` |
| `reviewer_decision_accuracy` | `1.000000` |
| `semantic_correction_success_rate` | `1.000000` |
| `analysis_numeric_accuracy` | `1.000000` |
| `follow_up_resolution_accuracy` | `1.000000` |
| `end_to_end_task_success_rate` | `1.000000` |
| `average_technical_retries` | `0.200000` |
| `average_semantic_retries` | `0.100000` |

## Failed Cases

None.

## Known Limitations

- This is an offline application-layer contract eval, not an academic benchmark.
- Fake model outputs test validation and orchestration, not live-model quality.
- FakeWren makes CI deterministic; real Wren/DuckDB validation is recorded separately.
- Token usage and cost are not collected by the current trace model.
- Real LLM evaluation was not run for this sprint.
