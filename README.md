# DataPilot-Media

DataPilot-Media is a multi-step analysis agent for audio/video quality analysis
and incident troubleshooting. It extends the DataPilot runtime with a synthetic
Media domain and combines schema-grounded planning, read-only Tool Calling,
Knowledge RAG, evidence validation, and deterministic rendering to produce
traceable answers.

> Project status: completed experimental prototype. The current evaluation uses
> deterministic synthetic Media data and a limited number of real-model runs; it
> is not a production monitoring platform.

## Why this project

Executable SQL is not enough for incident analysis. A query can run while using
the wrong metric, time window, filter, aggregation, or evidence scope. A language
model can also turn correlation into an unsupported causal claim. DataPilot-Media
makes those boundaries explicit:

- the Planner sees a lightweight schema/capability context before decomposing a
  request;
- deterministic Media tools and Wren/DuckDB provide read-only data evidence;
- the Reviewer can request one bounded SQL correction without discarding valid
  Tool evidence;
- Knowledge RAG supplies metric definitions, error-code meaning, and SOP steps;
- evidence is normalized, deduplicated, scoped, and validated before rendering;
- Session Memory is committed only after the complete workflow succeeds.

## Features

- Schema-grounded planning over Media models, dimensions, metrics, and tools.
- Wren semantic modeling backed by a deterministic DuckDB fixture.
- Four read-only Media tools: QoE metrics, alarms, structured logs, and transcode
  status.
- Deterministic Tool Router with bounded SQL fallback/correction.
- Stdio MCP exposure of the same validated Tool contracts.
- Heading-aware Media knowledge corpus with BM25, local concept-aware feature
  hashing, hybrid reciprocal-rank fusion, and deterministic reranking.
- Paired baseline/current normalization across multiple Planner shapes.
- Canonical comparison deduplication with provenance retention and fail-closed
  conflict handling.
- Full Evidence Pack as the local canonical source of truth.
- Compact Evidence Projection and deterministic scope-aware bundles for the
  Final Claims LLM.
- Typed claims: observation, knowledge, correlation, hypothesis,
  recommendation, and causal claim.
- Projection, bundle, numeric, causal, mixed-status, bounded-sample, scope, and
  stable-control guards.
- Deterministic final renderer with DATA EVIDENCE, KNOWLEDGE EVIDENCE,
  INFERENCE, and LIMITATION sections.

## Architecture

```mermaid
flowchart TD
    U[User Question] --> P[Schema-Grounded Planner]
    P --> D[Task Dispatcher]
    D --> R[Deterministic Tool Router]
    R --> MT[Media Tools]
    R --> SQL[SQL Agent / bounded fallback]
    MT --> REV[Semantic Reviewer]
    SQL --> W[Wren dry-plan / DuckDB]
    W --> REV
    REV --> KR[Knowledge Retrieval]
    KR --> EN[Evidence Normalization]
    EN --> CD[Canonical Evidence Dedup]
    CD --> FP[Full Evidence Pack]
    FP --> CP[Compact Evidence Projection]
    CP --> SB[Scope-Aware Bundles]
    SB --> SC[Typed Structured Claims]
    SC --> PG[Projection Visibility Guard]
    PG --> BG[Bundle Guard]
    BG --> FV[Full-Pack Validator]
    FV --> DR[Deterministic Renderer]
    DR --> FA[Final Answer]
    FA --> SM[Success-only Session Memory]
    FP -. local canonical source of truth .-> FV
```

The Full Evidence Pack remains local. The Final Claims LLM sees only the
bounded Compact Projection and bundle metadata; the final Validator checks its
claims against the Full Evidence Pack.

## Media domain

The project under `domains/media/` contains four synthetic models:

| Model | Purpose |
|---|---|
| `stream_sessions` | Playback attempts and QoE measurements |
| `alarm_events` | Operational alarms with mixed lifecycle states |
| `log_events` | Structured CDN/origin log evidence |
| `transcode_jobs` | Synthetic transcode status records |

The fixture includes `prior_year`, `previous_day`, `previous_window`, and
`current_window`. Its deliberate incident is a 华南 / CDN-B playback-success
drop accompanied by three high-severity E302 alarms whose states are `open`,
`investigating`, and `resolved`.

## Quick start

Python 3.11 or newer is required. The commands below are for PowerShell and
must be run from the repository root.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\core\wren
.\.venv\Scripts\python.exe -m pip install -e ".\sdk\wren-langchain[dev]"
Copy-Item .env.example .env
```

Keep credentials only in the ignored `.env`. Generate the deterministic data,
register the environment-variable-based DuckDB profile, and build the Wren
project:

```powershell
.\.venv\Scripts\python.exe .\domains\media\data\generate_data.py
$env:MEDIA_DUCKDB_DIR = (Resolve-Path .\domains\media\data).Path
$env:WREN_HOME = (Resolve-Path .\domains\media\.wren).Path
.\.venv\Scripts\wren.exe profile add datapilot_media_duckdb `
  --from-file .\domains\media\profile.yml
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\wren.exe context build --path .\domains\media
.\.venv\Scripts\wren.exe context validate --path .\domains\media
```

Point DataPilot at the Media project and start the CLI:

```powershell
$env:WREN_PROJECT_PATH = (Resolve-Path .\domains\media).Path
$env:WREN_PROFILE = "datapilot_media_duckdb"
.\.venv\Scripts\python.exe -m datapilot.cli
```

Demo question:

> 华南播放成功率下降并出现 E302，结合告警、日志和知识库分析原因，并给出排查建议。

## Evaluation snapshot

Metrics are scoped to the named synthetic evaluation set. They are not
production accuracy claims.

| Evaluation | Result |
|---|---:|
| `tests/media` | 219 passed |
| `tests/datapilot` | 190 passed |
| MCP smoke | 7/7 passed |
| 18-case Tool Golden Set | selection 1.0 / arguments 1.0 / execution 1.0 |
| 15-case Retrieval Eval, Hybrid + Rerank | Recall@1 0.9286 / Recall@3 1.0 / MRR@3 1.0 |
| Single authorized Real DeepSeek E2E | complete workflow passed |

The successful Real E2E used 14 LLM calls and completed in approximately
126.76 seconds. It had zero technical retries, three bounded semantic
corrections, and zero structured-output retries. The evidence context was
compressed from 32,710 characters to 13,282 characters; the final prompt used
18,782 of the 20,000-character budget.

Reproduce the offline evaluations without calling an external LLM:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\media
.\.venv\Scripts\python.exe -m pytest tests\datapilot
.\.venv\Scripts\python.exe -m evals.media.run_tool_eval
.\.venv\Scripts\python.exe -m evals.media.run_retrieval_eval
.\.venv\Scripts\python.exe -m evals.media.run_mcp_smoke
```

See the [final project report](docs/MEDIA_AGENT_FINAL_REPORT.md) for the full
evaluation matrix and the [Media Eval README](evals/media/README.md) for metric
definitions and runner boundaries.

## Safety and reliability boundaries

| Risk | Boundary |
|---|---|
| Hallucinated schema | Lightweight planning context constrains available entities and fields |
| Unsafe SQL | Read-only, single-statement checks plus Wren dry-plan |
| Executable but wrong query | Independent semantic Reviewer and one bounded correction |
| Tool/correction evidence loss | Additive evidence merge with source lineage |
| Invalid comparison | Explicit metric, unit, aggregation, scope, group, and window bindings |
| Duplicate evidence | Canonical identity deduplication; conflicting facts fail closed |
| Context overflow | Deterministic P0/P1/P2 projection under a 20,000-character hard limit |
| Cross-scope claim | Scope-aware bundles and Bundle Guard |
| Unsupported fact or cause | Numeric, causal, mixed-status, bounded-sample, and evidence-ID guards |
| Free-form final hallucination | Structured claims, Full-Pack validation, deterministic renderer |
| Failed turn poisoning memory | Session Memory commits only after complete success |

Retries and correction loops remain bounded. No runner persists API keys,
Authorization headers, complete prompts, or raw model responses.

## Project structure

```text
datapilot/
├── agent/              Planner, workflow, Reviewer, Analyst, evidence pipeline
├── retrieval/          heading-aware corpus loading and local hybrid retrieval
├── tools/              contracts, discovery, routing, integration, Wren adapter
├── memory/             structured success-only session memory
└── tracing/            bounded trace and run summaries
domains/media/
├── models/ views/ cubes/  Wren Media semantic project
├── data/                   deterministic fixture generator and CSV sources
├── knowledge/              QoE, error-code, codec, and SOP corpus
└── runtime/                four Media tools and MCP server
evals/media/                 Tool, retrieval, MCP, offline-agent, and Real E2E evals
tests/media/                 Media contracts, guards, workflow, and regression tests
docs/MEDIA_AGENT_FINAL_REPORT.md
```

## Known limitations

- The Media dataset and all operational evidence are synthetic.
- The Tool and Retrieval golden sets are small development evaluations, not
  held-out production benchmarks.
- Real-model coverage is intentionally limited; the reported Phase 3 result is
  one authorized Real DeepSeek E2E run.
- The semantic retrieval path is deterministic local TF-IDF feature hashing
  with domain aliases, not a pretrained sentence-embedding model.
- Real E2E latency is high, and Reviewer/SQL correction increases LLM calls.
- The project does not process audio/video bytes, run FFmpeg, operate production
  monitoring infrastructure, implement user permissions, or provide a frontend.

## WrenAI attribution

This branch is secondary development based on the official
[Canner/WrenAI](https://github.com/Canner/WrenAI) source. Wren supplies the MDL
semantic layer, Wren Engine, context and memory facilities, connectors, CLI,
MCP foundations, and SDKs. DataPilot-Media adds an application/domain layer; it
does not claim to have reimplemented the WrenAI engine.

The repository's existing licenses and path-specific attribution remain in
effect. See `LICENSE`, `LICENSE-APACHE-2.0`, `LICENSE-CC-BY-4.0`, and
`LICENSE-AGPL-3.0`.
