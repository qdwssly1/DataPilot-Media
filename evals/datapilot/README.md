# DataPilot Offline Eval

This directory contains DataPilot's deterministic, application-layer evaluation.
It validates orchestration contracts without calling a network service or an
external database.

Run it from the repository root:

```powershell
.\.venv\Scripts\python.exe evals\datapilot\runner.py --output docs\EVAL_RESULTS.md
```

The committed 30-case dataset uses only synthetic questions, schemas, SQL, and
rows. `SequenceModel` supplies fixed structured model responses and `FakeWren`
exercises the same DataPilot component boundaries used by the workflow. Numeric
analysis is compared directly against deterministic expected values.

Measured rates cover Planner intent and plan validity, SQL execution and safety,
Reviewer decisions, semantic correction, Analyst numeric results, follow-up
resolution, and end-to-end task completion. Retry averages are calculated from
eligible SQL and end-to-end cases; they are not estimates.

This eval does not measure live-model quality, production connector behavior,
token usage, cost, latency under load, or Wren Engine correctness. Those claims
must come from separate real integration evidence. Real LLM evaluation is
optional and is not run by the offline runner.
