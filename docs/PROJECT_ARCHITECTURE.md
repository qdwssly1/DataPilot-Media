# DataPilot Project Architecture

## 1. Positioning and ownership

DataPilot is the Agent application layer added to a WrenAI v0.13.3 source
baseline. It does not replace or reimplement Wren's semantic engine.

| Layer | Responsibilities | Ownership in this repository |
|---|---|---|
| DataPilot | State, planning, task dispatch, SQL orchestration, safety, review, bounded correction, analysis, answers, session memory, tracing, Eval | Secondary development |
| WrenAI | MDL, semantic SQL planning, context retrieval, verified SQL memory, Toolkit, Engine, connectors, MCP and SDKs | Upstream WrenAI |
| Data source | Stores and executes physical queries | External system; DuckDB used in integration evidence |

The adapter in datapilot/tools/wren_tools.py is intentionally thin. It calls
WrenToolkit for schema context, query recall, dry planning, execution, and
verified-query storage. It does not copy Wren Engine or Memory internals.

## 2. End-to-end lifecycle

~~~mermaid
flowchart TD
    Q[Original user query] --> P1[Initial Planner]
    P1 --> FU{Follow-up?}
    FU -- no --> D[Task Dispatcher]
    FU -- yes --> L[Load SessionContext]
    L --> R[FollowUpResolver]
    R --> C{Resolvable?}
    C -- no --> X[Fail safely / clarification]
    C -- yes --> P2[Re-plan standalone query]
    P2 --> D

    D --> T{Task type}
    T -- query --> S[SQL Agent]
    S --> W[Wren context / memory / dry plan / query]
    W --> V[Semantic Reviewer]
    V -- one correction --> S
    V -- approve --> D
    T -- analysis --> A[Deterministic Analyst]
    A --> D
    T -- response --> F[Grounded Final Answer]
    F --> OK{Whole turn successful?}
    OK -- yes --> M[Commit compact SessionContext]
    OK -- no --> N[Keep last verified SessionContext]
~~~

The dispatcher is a small sequential Python workflow in
datapilot/agent/graph.py. It executes ready tasks in Planner order and does not
introduce another Agent framework.

## 3. AgentState

AgentState represents one turn, not the complete chat history. Its main groups
are:

- input: original_query, resolved_query, was_follow_up;
- plan: current_task, task_plan, pending_tasks, completed_tasks;
- SQL: relevant_tables, business_context, generated_sql, sql_results;
- review: review_result and review_results;
- analysis/answer: analysis_results, final_answer, final_answer_result;
- session reference: session_context;
- observability: trace_id.

Dataclasses define task, SQL, review, analysis, answer, and session contracts.
Mutable defaults are created per state, and a previous session is deep-copied
before a new turn begins.

## 4. Planner and task dispatcher

The Planner accepts an effective user query and returns strict structured JSON:

- intent;
- short reason summary;
- tasks with unique task IDs;
- task type and dependency IDs;
- database/context requirements;
- follow-up indicator.

Validation rejects missing or unexpected fields, duplicate IDs, invalid
dependencies, invalid task types, and Planner-generated SQL. Structured-output
retry is bounded to one retry by default.

The dispatcher supports three task types:

1. query — SQL Agent, Wren, and Reviewer;
2. analysis — Analyst over approved dependency results;
3. response — final answer over completed sources.

A task runs only after all declared dependencies are complete.

## 5. SQL Agent and Wren boundary

For each query task the SQL Agent:

1. fetches Wren schema/business context;
2. recalls verified NL-to-SQL examples when Wren SQL Memory is enabled;
3. adds the current task contract and authoritative structured filters;
4. requests strict structured SQL output;
5. enforces one read-only statement;
6. checks that filter literals are preserved unless Wren supplies an explicit
   canonical mapping;
7. calls Wren dry plan;
8. executes through Wren and records a bounded SQLResult.

The default is two technical attempts, so there can be at most one technical
retry. Unverified SQL is never written to Wren SQL Memory.

## 6. Semantic Reviewer and correction

The Reviewer evaluates the current task, SQL, columns, bounded sample rows, and
prior feedback. It returns approve, retry, or fail plus typed issues. The retry
instruction is constrained to the current Planner task contract; it cannot
merge sibling tasks or enlarge scope.

Execution success and semantic correctness are different gates. Wren proves
that SQL can be planned and executed; the Reviewer checks whether it answered
the requested metric, dimensions, time range, filters, joins, and aggregation.

The workflow permits zero or one semantic correction. Approved SQL may then be
stored in Wren SQL Memory. Rejected or failed SQL is not stored.

## 7. Analyst and final answer

The Analyst consumes only dependency results that have an approve decision.
Supported grouped comparisons are computed in deterministic Python:

- per-group difference;
- growth rate with explicit zero-base behavior;
- largest decline.

Column/period contracts are inferred conservatively. Ambiguous metric shapes
fail closed instead of silently selecting a column. The final response writer
receives bounded, verified task outputs and must return source task IDs that
refer to completed work.

## 8. Structured Session Memory

SessionContext is compact business context:

- session_id and turn_index;
- metrics and dimensions;
- time_range labels/start/end;
- filters and entities;
- analysis_goal;
- last user/resolved query, intent, short answer summary, and source task IDs;
- updated_at.

SessionMemoryStore is an in-memory, session-keyed, copy-on-read store. It does
not use LanceDB and must not be confused with Wren SQL Memory:

| Memory | Content | Purpose |
|---|---|---|
| Wren SQL Memory | Verified natural language → SQL examples | SQL generation recall |
| DataPilot Session Memory | Current session's semantic slots | Resolve follow-up intent |

Only a complete workflow with a successful final answer updates the
authoritative session. Failed turns leave the last verified context unchanged.
This is the memory-poisoning boundary.

## 9. Follow-up resolution

The initial Planner is the follow-up detector. There is no keyword-based
shortcut. For a follow-up:

1. load the same session ID;
2. stop safely if no verified business context exists;
3. resolve inheritance and overrides into a strict FollowUpResolution;
4. preserve original_query and set a standalone resolved_query;
5. re-run the Planner once;
6. stop if the second plan is still a follow-up.

For “那华南地区呢？”, GMV, Q2/Q3, 商品类别, and the analysis goal are
inherited while region=华南 is added. A later “只看 Q3” overrides the time
range without restoring Q2. A clearly new question is planned without old
slots being injected.

## 10. Trace and Observability Lite

TraceCollector stores observable in-memory events only. Events cover Planner,
Wren/SQL stages, review/correction, analysis/answer, and session lifecycle.
They exclude hidden reasoning, credentials, connection strings, and large
result rows.

summarize_trace derives stage durations when emitted, task outcomes, query and
review counts, retry counts, follow-up count, total duration, and success. CLI
runs print a compact Run Summary. LLM call count, tokens, and cost remain
unknown because the current event model does not collect them.

## 11. Offline Eval

evals/datapilot contains 30 synthetic cases for Planner, SQL, Reviewer,
Analyst, session/follow-up, and end-to-end workflows. SequenceModel and
FakeWren keep it deterministic and network-free while exercising actual
DataPilot component APIs. Numeric expected values are compared directly,
without an LLM judge.

The generated results are in EVAL_RESULTS.md. This Eval measures application
contracts; it does not replace real-model, real-engine, connector, security, or
load testing.

## 12. Bounded failure model

| Boundary | Default maximum |
|---|---:|
| Planner structured-output retry | 1 |
| SQL technical retry | 1 |
| Reviewer structured-output retry | 1 |
| Reviewer semantic correction | 1 |
| Analyst/final-output retry | 1 |
| Follow-up resolver retry | 1 |
| Follow-up resolution and re-plan cycle | 1 |

No component is allowed an unbounded retry loop.
