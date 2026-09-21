# DataPilot-Media domain

This directory is the deterministic Media domain used by DataPilot-Media. It
contains the Wren semantic project, synthetic fixture generator, domain
knowledge, four read-only tools, and the stdio MCP adapter. It does not process
audio/video bytes or connect to a production monitoring system.

## Models and windows

| Model | Grain | Main fields |
|---|---|---|
| `stream_sessions` | one playback attempt | region, CDN, device, startup time, buffer duration, play success |
| `alarm_events` | one operational alarm | region, CDN, error code, severity, status, message |
| `log_events` | one structured log | region, CDN, service, level, error code, trace ID, message |
| `transcode_jobs` | one synthetic job | region, codec, status, error code, GPU pool |

| Window | Time range | Role |
|---|---|---|
| `prior_year` | 2025-09-01 11:00–12:00 | year-over-year candidate baseline |
| `previous_day` | 2026-08-31 11:00–12:00 | day-over-day candidate baseline |
| `previous_window` | 2026-09-01 10:00–11:00 | previous-hour baseline |
| `current_window` | 2026-09-01 11:00–12:00 | anomaly window |

Each window contains 180 playback sessions. The deliberate incident is 华南 /
CDN-B in `current_window`:

- Overall 华南 playback success: 93.33% → 71.67%.
- CDN-A: 95% → 85%.
- CDN-B: 95% → 50%.
- CDN-C: 90% → 80%.
- Three high-severity E302 alarms affect CDN-B, with one `open`, one
  `investigating`, and one `resolved` state.
- Structured logs provide bounded upstream-timeout examples.

These facts support correlation and a troubleshooting hypothesis; they do not
prove causality.

## Wren design

The project defines four models, two views, one QoE cube, and no row-grain
relationship between sessions and alarms. `hourly_alarm_correlation` aggregates
both facts by hour, region, and CDN before joining them, preventing alarm
multiplicity from distorting playback metrics.

`target/mdl.json` and `data/media.duckdb` are generated artifacts. YAML, CSV,
knowledge Markdown, and `generate_data.py` are the committed sources of truth.

## Build

Run from the repository root in PowerShell:

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

The profile uses `${MEDIA_DUCKDB_DIR}` rather than a machine-specific absolute
path. Wren's DuckDB connector expects the directory that contains
`media.duckdb`.

## Media tools

The registry in `runtime/tools.py` exposes:

1. `query_qoe_metrics`
2. `get_alarm_events`
3. `query_logs`
4. `get_transcode_status`

Inputs are strict structured contracts, unknown fields are rejected, queries
are read-only, and all outputs contain synthetic evidence. Tool evidence is
preserved when a Reviewer-approved SQL correction adds more evidence.

Run the data-only MCP server after setting `WREN_PROJECT_PATH`, `WREN_PROFILE`,
`WREN_HOME`, and `MEDIA_DUCKDB_DIR`:

```powershell
.\.venv\Scripts\python.exe -m domains.media.runtime.mcp_server
```

The MCP process does not need LLM credentials.

## Knowledge corpus

`knowledge/` contains:

- QoE metric definitions and aggregation rules;
- E302 and other synthetic error-code descriptions;
- CDN and startup-latency troubleshooting SOPs;
- codec and transcoding reference material;
- retrieval aliases and deterministic reranking configuration.

The semantic retrieval mode is local TF-IDF feature hashing with domain concept
aliases. It is not a pretrained sentence-embedding model.

## Demo

```powershell
$env:WREN_PROJECT_PATH = (Resolve-Path .\domains\media).Path
$env:WREN_PROFILE = "datapilot_media_duckdb"
.\.venv\Scripts\python.exe -m datapilot.cli
```

Suggested question:

> 华南播放成功率下降并出现 E302，结合告警、日志和知识库分析原因，并给出排查建议。

Reference SQL for simpler domain checks is in `data/demo_queries.sql`. The full
evaluation commands and metric definitions are documented in
`../../evals/media/README.md`.
