# DataPilot Resume Evidence

This file is the factual source for resume and interview claims. It separates
upstream WrenAI capabilities, DataPilot secondary development, deterministic
regression, and manually executed real integration evidence.

## Project positioning

DataPilot is a multi-step data analysis Agent application built on the official
WrenAI v0.13.3 source. Its contribution is explicit orchestration and
correctness boundaries around Wren, not a new semantic engine or connector
stack.

## Capability evidence

| Capability | Implementation | Evidence | Test or metric | Main files |
|---|---|---|---|---|
| Explicit state | Turn-scoped AgentState and dataclass contracts | Mutable state isolation and serialization | DataPilot regression | datapilot/agent/state.py |
| Structured Planner | Validated intent/task DAG; Planner cannot emit SQL | Invalid JSON, schema, duplicate ID, dependency, and SQL-field tests | Planner intent/plan Eval = 1.0/1.0 | datapilot/agent/planner.py |
| SQL orchestration | Context, recall, generation, safety, dry plan, query | Fake and real Wren paths; bounded failure tests | SQL execution Eval = 1.0 | datapilot/agent/sql_agent.py |
| Authoritative filters | Structured filters reach generation; mismatch blocked before dry plan | 华南 is preserved unless Wren provides a canonical mapping | SQL filter Eval case passes | datapilot/agent/sql_agent.py |
| Semantic Reviewer | Per-task approve/retry/fail decision | Metric/dimension/time/aggregation mismatch cases | Reviewer decision Eval = 1.0 | datapilot/agent/reviewer.py |
| Semantic correction | One Reviewer → SQL Agent correction | Corrected SQL is reviewed again; task scope is preserved | Correction Eval = 1.0; average semantic retries = 0.1 | datapilot/agent/graph.py |
| Deterministic Analyst | Difference, growth, largest decline | Expected values compared to structured derived values | Numeric Eval = 1.0 | datapilot/agent/analyst.py |
| Grounded answer | Completed task outputs and valid source task IDs only | Invented source IDs are rejected | DataPilot regression | datapilot/agent/analyst.py |
| Session Memory | Compact slots, isolation, reset, success-only commit | Failed-turn poisoning and cross-session tests | Follow-up Eval = 1.0 | datapilot/memory/session_memory.py |
| Follow-up resolution | Planner detection, context merge, standalone query, one re-plan | “那华南地区呢？” inherits and adds region=华南 | Follow-up workflow tests | datapilot/agent/follow_up.py |
| Observability Lite | In-memory events and derived Run Summary | Success, retry, failure, unknown metric, follow-up tests | 5 Trace Summary tests | datapilot/tracing/ |
| Offline Eval | 30 synthetic deterministic cases | Runner-generated artifact; no network | All nine rates = 1.0; no failures | evals/datapilot/ |

## Upstream WrenAI capabilities

The following are WrenAI capabilities and must not be described as original
DataPilot work:

- MDL semantic layer and Wren Engine;
- Python runtime and database connectors;
- WrenToolkit and wren-langchain integration;
- schema/context retrieval;
- verified NL-to-SQL memory and optional LanceDB provider;
- dry planning and semantic SQL translation;
- MCP and the other upstream SDKs.

DataPilot's WrenToolAdapter normalizes these existing APIs for its own workflow.

## DataPilot contributions

- explicit AgentState and dependency-aware task contracts;
- structured Planner and sequential task dispatcher;
- SQL safety, bounded technical retries, and authoritative-filter checks;
- independent semantic Reviewer and one bounded correction;
- deterministic numerical Analyst and grounded final answer;
- structured SessionContext, follow-up inheritance/override, session isolation,
  and poisoning protection;
- Trace Summary and 30-case offline application Eval.

## Architecture decisions

1. **Execution is not correctness.** Wren dry plan/query and semantic review are
   separate gates.
2. **Tasks are contracts.** Reviewer feedback and correction remain within the
   current Planner task.
3. **LLMs do not judge arithmetic.** Supported comparisons use deterministic
   Python and fail closed on ambiguous shapes.
4. **Memory has two meanings.** Wren SQL Memory stores verified SQL examples;
   DataPilot Session Memory stores compact conversational business slots.
5. **Commit memory after success.** Failed turns cannot replace authoritative
   session context.
6. **Every loop is bounded.** Default correction budgets are zero or one retry.
7. **Measure only observable facts.** Current traces do not claim token usage,
   cost, or LLM call count.

## Real validation evidence

### Real LLM

Before this freeze, the OpenAI-compatible adapter was exercised with a real
DeepSeek endpoint using only synthetic prompts and schema. The final Phase 7
two-turn acceptance used model deepseek-v4-flash. It completed 15 LLM calls
with zero bounded retries in the successful run.

This was a smoke test, not the offline Eval and not a general model-quality
benchmark. No real LLM Eval was run during the Resume Freeze Sprint.

### Real Wren and DuckDB

The final Phase 7 acceptance used a temporary Wren project and DuckDB database
with synthetic orders data:

- Turn 1 Q2: A=250, B=300;
- Turn 1 Q3: A=200, B=370;
- deterministic result: A change -50 and -20%; B change +70 and about +23.33%;
- largest decline: A;
- SessionContext turn_index: 1.

Turn 2 raw query was “那华南地区呢？”. The resolver returned can_resolve=true,
added filters.region=华南, and preserved the prior metric, dimension, time range,
and analysis goal. Re-planning succeeded. The executed SQL used the literal
region = '华南'; real Wren returned the synthetic South-region rows. Reviewer,
Analyst, and Final Answer completed, then SessionContext committed turn_index=2
with the region filter retained.

Temporary database/project resources and credentials were not committed.

## Frozen automated evidence

Environment:

- Windows;
- Python 3.13.1;
- new runtime dependencies in the Resume Freeze Sprint: 0.

Regression:

- DataPilot: **179 passed**;
- wren-langchain non-slow regression: **116 passed, 2 deselected**;
- wren-langchain warnings: five existing DuckDB fetch-arrow-table deprecation
  warnings.

Offline Eval:

| Metric | Result |
|---|---:|
| planner_intent_accuracy | 1.000000 |
| planner_plan_valid_rate | 1.000000 |
| sql_execution_success_rate | 1.000000 |
| sql_safety_rejection_rate | 1.000000 |
| reviewer_decision_accuracy | 1.000000 |
| semantic_correction_success_rate | 1.000000 |
| analysis_numeric_accuracy | 1.000000 |
| follow_up_resolution_accuracy | 1.000000 |
| end_to_end_task_success_rate | 1.000000 |
| average_technical_retries | 0.200000 |
| average_semantic_retries | 0.100000 |

Case count: **30**. Failed cases: **none**. These are deterministic
application-contract results using fake model responses and FakeWren.

## Important commits

| Commit | Evidence |
|---|---|
| 0210eeb | WrenAI v0.13.3 baseline |
| 9da4edf | Original WrenAI architecture analysis |
| 2e76442 | Repository prune plan |
| c69e529 | AgentState and CLI skeleton |
| 693c8e3 | Structured Planner |
| 07624af | SQL Agent and Wren integration |
| 7630297 | Semantic Reviewer and correction loop |
| 1174fe2 | Analyst and multi-step workflow |
| 1b6ae87 | Structured Session Memory |
| 86ec4cf | Trace Summary and offline Eval framework |

## Known limitations

- Session Memory is in-process only; there is no Redis or durable store.
- Trace events are in-memory; there is no distributed backend or dashboard.
- Tokens and cost are not collected.
- The offline Eval uses deterministic fakes and is not a live-model benchmark.
- Real connector coverage is limited; the acceptance path used DuckDB.
- Real LanceDB SQL Memory was not smoke-tested in this freeze.
- Production PII, access-control, and row-level security policy are not part of
  the DataPilot layer.
- Deterministic analysis supports a bounded set of grouped comparisons.
- There is no production API or frontend.
- Repository pruning remains a plan, not an executed change.
