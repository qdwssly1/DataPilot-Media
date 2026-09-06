# DataPilot

DataPilot is a multi-step data analysis Agent built on top of WrenAI v0.13.3's
semantic, context, and execution capabilities. It adds an explicit, testable
application layer for planning, SQL orchestration, semantic review, bounded
correction, deterministic analysis, grounded answers, and structured multi-turn
memory.

> Status: resume-freeze candidate. The current checkout is a source-level
> engineering project, not a production service or packaged DataPilot release.

## Overview

DataPilot turns a natural-language analysis request into a dependency-aware task
plan. Query tasks use Wren's context, verified SQL memory, semantic planner, and
database connector; every query result then passes a separate semantic Reviewer.
Only approved results can reach deterministic analysis and the final answer.
Successful turns commit compact session context for follow-up questions.

The ownership boundary is deliberate:

- **DataPilot Agent layer:** state, planning, dispatch, safety, retry, review,
  analysis, answers, session memory, tracing, and offline evaluation.
- **WrenAI layer:** MDL semantic model, context retrieval, SQL memory,
  dry plan, Wren Engine, and connectors.

## Why DataPilot

Ordinary text-to-SQL pipelines often stop when SQL executes. Execution proves
that a database accepted the statement; it does not prove that the selected
metric, dimensions, filters, aggregation, or time range match the user's task.
DataPilot makes those contracts explicit and independently reviewable.

## Architecture

~~~mermaid
flowchart TD
    U[User / multi-turn CLI]

    subgraph DP[DataPilot Agent Layer]
        SM[Structured Session Memory]
        FR[Follow-up Resolver]
        P[Structured Planner]
        D[Task Dispatcher]
        S[SQL Agent + Safety]
        R[Semantic Reviewer]
        A[Deterministic Analyst]
        F[Grounded Final Answer]
        T[Trace Summary + Offline Eval]
    end

    subgraph W[WrenAI Layer]
        WT[Wren Toolkit]
        C[Context Retrieval + SQL Memory]
        E[MDL / Dry Plan / Wren Engine]
        DB[Connector / Database]
    end

    U --> P
    P -- follow_up --> SM --> FR --> P
    P --> D --> S
    S --> WT --> C --> E --> DB
    DB --> R
    R -- one bounded semantic correction --> S
    R -- approve --> A --> F
    F -- success only --> SM
    DP -. observable events .-> T
~~~

## Core Workflow

**Plan → Query → Review → Correct → Analyze → Answer → Remember**

Planner output is strict JSON with unique task IDs and valid dependencies.
The dispatcher runs ready tasks in plan order: query tasks go through SQL
generation and review, analysis tasks consume approved SQL results, and
response tasks write a grounded answer.

## Key Features

- Explicit AgentState, rather than hiding workflow state in chat messages.
- Structured Planner with simple-question, single-query, multi-step-analysis,
  and follow-up intents.
- Dependency-aware query, analysis, and response tasks.
- Wren context retrieval, verified NL-to-SQL recall, semantic planning, and
  connector execution through a thin adapter.
- Read-only SQL safety and authoritative categorical-filter preservation.
- Technical retry and semantic correction with fixed upper bounds.
- Per-task semantic Reviewer and SQL-memory write only after approval.
- Deterministic grouped differences, growth rates, and largest-decline logic.
- Structured session slots with inheritance, override, isolation, reset, and
  poisoning protection.
- In-memory trace events, compact CLI run summaries, and a repeatable offline
  Eval.

## Reliability Design

| Risk | Boundary |
|---|---|
| Invalid model output | Strict JSON fields and one bounded format retry |
| Unsafe SQL | Read-only/single-statement checks before Wren |
| Invalid semantic SQL | Wren dry plan before query execution |
| Transient planning/query failure | SQL Agent has at most two attempts |
| Executable but wrong SQL | Reviewer checks the current Planner task contract |
| Reviewer finds a semantic mismatch | At most one Reviewer → SQL correction |
| LLM arithmetic error | Numeric comparison is deterministic Python |
| Unsupported result shape | Analyst fails closed on ambiguous metric contracts |
| Ungrounded response | Final answer source IDs must reference completed outputs |
| Incorrect translated category | User filter literal is authoritative unless Wren supplies a canonical mapping |
| Failed turn corrupts follow-ups | Session context is committed only after the complete turn succeeds |
| Infinite agent loop | Planner, resolver, output, technical, and semantic retries are bounded |

Token usage and cost are not currently collected; the project does not invent
those metrics.

## Multi-turn Session Example

All values below are synthetic.

**Turn 1**

> 比较 Q2 和 Q3 各商品类别 GMV，并找出下降最大的类别。

| Category | Q2 GMV | Q3 GMV | Change | Growth |
|---|---:|---:|---:|---:|
| A | 250 | 200 | -50 | -20% |
| B | 300 | 370 | +70 | +23.33% |

The deterministic result is **largest decline = A**. After the grounded final
answer, memory contains metric GMV, time Q2/Q3, dimension 商品类别, no filters,
and turn index 1.

**Turn 2**

> 那华南地区呢？

The Planner marks this as a follow-up. The resolver inherits the metric,
dimension, time range, and analysis goal, adds region=华南, and produces a
standalone query before re-planning. The SQL Agent preserves region = '华南';
approved synthetic Wren results are analyzed and the session is committed with
turn index 2.

## Evaluation

The committed offline Eval uses 30 synthetic cases, deterministic fake model
responses, and FakeWren. It makes no network requests.

| Metric | Measured result |
|---|---:|
| Planner intent accuracy | 1.000000 |
| Planner plan valid rate | 1.000000 |
| SQL execution success rate | 1.000000 |
| SQL safety rejection rate | 1.000000 |
| Reviewer decision accuracy | 1.000000 |
| Semantic correction success rate | 1.000000 |
| Analysis numeric accuracy | 1.000000 |
| Follow-up resolution accuracy | 1.000000 |
| End-to-end task success rate | 1.000000 |
| Average technical retries | 0.200000 |
| Average semantic retries | 0.100000 |

These are application-contract results, not a live-model benchmark. See
[the generated Eval artifact](docs/EVAL_RESULTS.md) and
[the Eval design](evals/datapilot/README.md).

## Project Structure

~~~text
datapilot/
├── agent/          state, Planner, SQL Agent, Reviewer, Analyst, workflow
├── llm/            OpenAI-compatible structured-output boundary
├── memory/         in-memory structured session store
├── tools/          thin WrenToolkit adapter
├── tracing/        observable events and run summary
└── cli.py          multi-turn CLI
evals/datapilot/    30-case deterministic offline Eval
tests/datapilot/    unit, contract, workflow, and Eval tests
core/               upstream WrenAI engine and Python runtime
sdk/wren-langchain/ upstream WrenAI LangChain/LangGraph toolkit
~~~

More detail is in [Project Architecture](docs/PROJECT_ARCHITECTURE.md).

## Quick Start

Python 3.11 or newer is required by the Wren packages; this freeze was verified
with Python 3.13.1 on Windows.

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\core\wren
.\.venv\Scripts\python.exe -m pip install -e ".\sdk\wren-langchain[dev]"
Copy-Item .env.example .env
~~~

Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL locally. Point WREN_PROJECT_PATH
at a prepared Wren project. Never commit .env.

~~~powershell
.\.venv\Scripts\python.exe -m datapilot.cli
~~~

The CLI retains one session across questions. Enter reset or clear to discard
its context, and exit or quit to stop.

Run the offline Eval without any LLM credentials:

~~~powershell
.\.venv\Scripts\python.exe evals\datapilot\runner.py
~~~

## Tests

Final freeze regression on Python 3.13.1:

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests\datapilot -v
# 179 passed

.\.venv\Scripts\python.exe -m pytest sdk\wren-langchain\tests -v -m "not slow"
# 116 passed, 2 deselected
~~~

The wren-langchain run reports five existing DuckDB fetch-arrow-table
deprecation warnings.

## WrenAI Attribution

This branch is a secondary development based on the official
[Canner/WrenAI](https://github.com/Canner/WrenAI) v0.13.3 source. WrenAI
provides the MDL semantic layer, Wren Engine, context retrieval, SQL memory,
Toolkit, connectors, MCP support, and related SDKs. DataPilot does not present
those upstream capabilities as original work.

Licensing and attribution are preserved in [LICENSE](LICENSE),
[LICENSE-APACHE-2.0](LICENSE-APACHE-2.0),
[LICENSE-CC-BY-4.0](LICENSE-CC-BY-4.0), and
[LICENSE-AGPL-3.0](LICENSE-AGPL-3.0). The repository license map applies by
path; notably core and sdk are Apache-2.0 and docs is CC BY 4.0.

## Limitations / Future Work

The freeze deliberately leaves these items as future work:

- persistent Session Store or Redis;
- persistent trace backend, distributed tracing, and token/cost accounting;
- broader real connector end-to-end coverage;
- real LanceDB SQL-memory smoke coverage;
- production PII and row-level security policy;
- more complex multi-dimensional deterministic analysis;
- natural-language numeric-claim validation;
- production API and frontend;
- repository pruning.

Real LLM evaluation was not run during this Resume Freeze Sprint. Prior
synthetic-data smoke tests validated the OpenAI-compatible workflow with a real
DeepSeek model and real Wren/DuckDB, but that evidence is separate from the
repeatable offline Eval.
