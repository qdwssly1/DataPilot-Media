# DataPilot Interview Guide

These answers describe the frozen implementation. They intentionally avoid
claiming production features or upstream WrenAI capabilities as original work.

## 1. Why build DataPilot?

WrenAI already provides a strong semantic/context/execution layer, but its
reference Agent flow did not provide the explicit multi-step application state,
per-task semantic review, deterministic analysis, or conversational business
memory needed for this project. DataPilot explores those application-layer
engineering problems without replacing Wren.

## 2. Why is it not ordinary text-to-SQL?

Text-to-SQL usually maps one question to one statement. DataPilot can decompose
a request into dependent query, analysis, and response tasks; review each query
against its task; correct it once; combine approved results deterministically;
and carry structured semantic context into a follow-up.

## 3. What does the Planner do?

It classifies intent and returns strict JSON containing a dependency-aware task
plan. It validates task IDs, types, dependencies, database/context flags, and
follow-up status. It is forbidden to generate SQL, preserving separation
between planning and execution.

## 4. Why use an explicit AgentState?

Messages alone cannot reliably express which task is active, which dependencies
are approved, how many retries occurred, or which outputs may ground an answer.
AgentState makes those transitions inspectable and testable while keeping
cross-turn memory in a separate store.

## 5. Why decompose tasks?

A question such as comparing Q2 and Q3 can require two independent queries, one
comparison, and one answer. Task decomposition exposes dependencies, allows
each SQL result to be reviewed independently, and prevents a correction for one
period from silently changing its sibling task.

## 6. How does the SQL Agent work?

It asks Wren for context and verified SQL examples, supplies the current task
and authoritative filters to the model, validates structured output and
read-only SQL, checks filter consistency, calls Wren dry plan, then executes
through Wren. The returned SQLResult records rows, timing, and retries.

## 7. What does WrenAI provide?

WrenAI provides the MDL semantic layer, Wren Engine, context retrieval, SQL
memory, dry planning, connectors, WrenToolkit, MCP, and SDK integrations.
DataPilot owns the orchestration around those primitives. This boundary is
visible in the thin WrenToolAdapter.

## 8. Why is dry plan necessary?

It catches semantic-planning and translation failures before the physical query
runs. It is a cheaper validation boundary, but it is not sufficient by itself:
successful planning still does not prove the SQL answers the user's task.

## 9. How is SQL safety enforced?

The SQL Agent accepts one read-only statement, permits SELECT and SELECT-backed
CTEs, rejects writes or multiple statements, and applies the check before dry
plan/query. It also checks that authoritative categorical literals are not
translated without a canonical mapping from Wren context.

## 10. What is the difference between technical and semantic retry?

A technical retry addresses invalid structured output, safety/consistency
failure, dry-plan failure, or query failure inside the SQL Agent. A semantic
retry starts only after SQL executes but the Reviewer finds a task mismatch.
Both have at most one retry by default and are traced separately.

## 11. Why does the Reviewer exist?

SQL can be syntactically valid and executable while using the wrong metric,
dimension, time range, filter, join, or aggregation. The Reviewer is an
independent structured gate between execution and downstream analysis.

## 12. Why does SQL execution success not mean task success?

The database checks whether a statement can run, not whether it implemented the
user's intent. For example, COUNT rows can execute successfully when the task
asked for SUM GMV. DataPilot requires Reviewer approval before completing the
query task.

## 13. How does review preserve the Planner task contract?

The Reviewer prompt receives the current task and relevant evidence, and its
retry instruction is validated against that task. Correction is passed back to
the SQL Agent for the same task only; sibling task descriptions are not merged
into the retry.

## 14. Why prefer deterministic Python in the Analyst?

Differences, growth rates, and extrema are precise computations. Implementing
them as Python functions makes behavior repeatable, directly testable, and
independent of model phrasing.

## 15. How are LLM math errors avoided?

Approved SQL rows are converted into structured comparisons by deterministic
code. Tests compare derived values directly with expected numbers. If metric or
period columns are ambiguous, the Analyst fails closed rather than asking the
LLM to guess.

## 16. How do SQL Memory and Session Memory differ?

Wren SQL Memory stores verified natural-language-to-SQL examples for later SQL
generation. DataPilot Session Memory stores compact business slots such as
metric, dimensions, time range, filters, and goal for the current conversation.
Session Memory is not stored in LanceDB or mixed into SQL Memory.

## 17. What happens for “那华南地区呢？”?

The initial Planner marks follow-up. The workflow loads the same session, the
resolver inherits GMV, Q2/Q3, 商品类别, and the goal while adding region=华南,
then writes a standalone resolved query. The Planner runs once more, after
which SQL Agent → Wren → Reviewer → Analyst → Final Answer proceeds normally.
Only success commits turn index 2.

## 18. Why does a failed turn not update Session Memory?

An invalid field or failed analysis should not become authoritative context for
the next question. commit_session_context checks the whole workflow, completed
tasks, successful analysis, and final answer before replacing the stored
context. Otherwise the prior verified turn remains.

## 19. How are infinite loops prevented?

Planner, SQL generation, Reviewer output, Analyst output, context extraction,
and follow-up resolution each allow at most one retry by default. Semantic
correction is limited to one, and a follow-up gets one resolution plus one
re-plan; if the re-plan is still follow-up, execution stops safely.

## 20. How is the Eval designed?

The offline Eval has 30 synthetic cases across Planner, SQL, Reviewer, Analyst,
follow-up/session, and complete workflows. Fixed structured responses and
FakeWren remove network and model variance while exercising actual component
APIs. Numeric cases compare exact derived values instead of using an LLM judge.

## 21. How can DataPilot's effectiveness be demonstrated?

The frozen evidence is: 179 passing DataPilot tests; 116 passing non-slow
wren-langchain tests with 2 deselected; 30 offline Eval cases with no failures;
nine measured success/accuracy rates of 1.0; and a prior real DeepSeek +
Wren + DuckDB two-turn smoke test over synthetic data. Each claim is scoped to
what that validation can prove.

## 22. Which work is original and which comes from WrenAI?

DataPilot adds state, Planner, dispatcher, SQL orchestration/safety, semantic
review/correction, deterministic analysis, grounded answer, Session Memory,
follow-up resolution, tracing summary, and its offline Eval. WrenAI supplies
the semantic model, Engine, context and SQL memory, Toolkit, dry plan,
connectors, MCP, and upstream SDKs.

## 23. What is still needed for production?

Durable/session-distributed storage, persistent and distributed tracing,
token/cost accounting, more real connector tests, real LanceDB SQL-memory
smoke coverage, production PII/RLS policy, broader deterministic analysis,
natural-language numeric-claim validation, an API/frontend, deployment and
load testing, and an executed repository-pruning decision remain future work.
