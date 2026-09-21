# DataPilot-Media evaluation

This directory contains the reproducible synthetic evaluations for the Media
domain. Offline runners do not call an external LLM. Real-model artifacts are
reported separately and must not be interpreted as offline benchmark accuracy.

## Evaluation inventory

| Evaluation | Runner or artifact | Scope |
|---|---|---|
| Tool Golden Set | `run_tool_eval.py` / `tool_cases.json` | 18 selection, argument, and execution cases |
| Original Retrieval Eval | `run_retrieval_eval.py` / `retrieval_cases.json` | 15 synthetic knowledge queries retained as the original small set |
| Expanded Retrieval Eval | `run_retrieval_eval.py --expanded` / `expanded_retrieval_cases.json` | 22 separately annotated knowledge-expansion queries |
| Offline Agent Eval | `run_tool_offline_cases.py` | deterministic tools plus six Planner comparison shapes |
| MCP smoke | `run_mcp_smoke.py` | real stdio client/server protocol and seven checks |
| Real DeepSeek E2E | `phase3_real_e2e_result.json` | one authorized complete synthetic workflow |
| Phase 2 history | `PHASE2_FINAL_REPORT.md` and `phase2_verification_results.json` | retained earlier retrieval/agent evidence |

## Reproduce the offline suite

Generate the fixture and prepare Wren first; see
`../../domains/media/README.md`. Then run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m evals.media.run_tool_eval
.\.venv\Scripts\python.exe -m evals.media.run_retrieval_eval
.\.venv\Scripts\python.exe -m evals.media.run_retrieval_eval --expanded
.\.venv\Scripts\python.exe -m evals.media.run_tool_offline_cases
.\.venv\Scripts\python.exe -m evals.media.run_mcp_smoke
```

The Tool, Retrieval, and Offline Agent runners are deterministic. MCP smoke
starts the local Media MCP server and talks to it over stdio; it does not pass
LLM credentials to the child process.

## Tool Golden Set

The 18 cases cover valid Tool selection, strict argument extraction, execution,
expected SQL fallback, and invalid selection. Reported `1.0` values mean all 18
synthetic golden cases passed the corresponding contract; they are not a
production accuracy claim.

Latest verified result:

- Selection accuracy: 1.0
- Argument accuracy: 1.0
- Execution success: 1.0
- Expected SQL fallback: 3/18 cases (rate 1/6)
- Invalid selection: 0
- Tool Router model calls: 0

## Retrieval Eval

The 14-document corpus is split into 86 retrievable chunks by Markdown
headings. Source `References` remain in each Markdown document for human
traceability but are excluded from retrieval chunks, so citation lists do not
compete with diagnostic content. Retrieval modes are:

- `lexical`: BM25 with domain alias expansion;
- `semantic`: local deterministic TF-IDF feature-hash vectors plus concept
  aliases;
- `hybrid`: reciprocal-rank fusion of lexical and semantic ranks;
- `hybrid_rerank`: hybrid candidates plus deterministic domain-aware reranking.

The semantic mode is not a pretrained neural embedding model. Recall is macro
average over annotated relevant chunks. MRR is calculated over the returned top
three chunks. The unrelated query is excluded from recall/MRR and evaluated as
no-result accuracy.

Latest verified original 15-case results on the expanded corpus:

| Mode | Recall@1 | Recall@3 | MRR@3 | Unrelated no-result |
|---|---:|---:|---:|---:|
| Lexical / BM25 | 0.571429 | 1.000000 | 0.785714 | 1.000000 |
| Semantic | 0.428571 | 0.678571 | 0.607143 | 1.000000 |
| Hybrid RRF | 0.500000 | 0.785714 | 0.666667 | 1.000000 |
| Hybrid + Rerank | 0.642857 | 0.928571 | 0.821429 | 1.000000 |

The original annotations intentionally remain unchanged. The added corpus now
contains relevant replacement chunks that this older ground truth does not
label, so these values are a regression signal rather than a complete relevance
judgment.

Latest verified expanded 22-case results (19 positive and 3 out-of-domain):

| Mode | Recall@1 | Recall@3 | MRR@3 | Unrelated no-result |
|---|---:|---:|---:|---:|
| Lexical / BM25 | 0.631579 | 0.842105 | 0.728070 | 0.333333 |
| Semantic | 0.315789 | 0.631579 | 0.464912 | 0.333333 |
| Hybrid RRF | 0.631579 | 0.894737 | 0.736842 | 0.333333 |
| Hybrid + Rerank | 0.736842 | 0.947368 | 0.842105 | 0.333333 |

On both small synthetic development sets, Hybrid + Rerank has the strongest
metrics. Plain Hybrid does not uniformly outperform BM25, so the results must
not be generalized beyond these sets. The expanded failure analysis also shows
that the current retriever has no calibrated relevance threshold: two of three
new out-of-domain paraphrases return results because of generic token overlap.

## Offline Agent Eval

The runner exercises direct tools and these comparison shapes:

- single-task baseline/current;
- single-task multi-baseline;
- split baseline/current tasks;
- multi-baseline with explicit primary baseline;
- multi-baseline without a primary baseline;
- the overlapping real-run shape used for canonical deduplication.

The overlapping fixture normalizes 24 derived comparison facts into eight
canonical facts—two overall and six CDN comparisons—while retaining all 24
provenance sources. It also verifies fail-closed conflict handling. These are
deterministic fixture results, not live-model success rates.

## Real DeepSeek E2E

`phase3_real_e2e_result.json` is the bounded summary of one explicitly
authorized synthetic run. It contains task metadata, bounded Tool/evidence
summaries, claims, guard decisions, the deterministic final answer, and timing.
It does not contain API keys, Authorization headers, `.env` contents, complete
prompts, raw model responses, real user data, or real business data.

Latest result:

- complete workflow: PASS, 7/7 tasks;
- LLM calls: 14;
- wall latency: approximately 126.76 seconds;
- technical / semantic / structured retry: 0 / 3 / 0;
- Projection Guard: PASS;
- Bundle Guard: PASS;
- Full-Pack Validator: `valid=true`, `violations=[]`;
- Deterministic Renderer: PASS;
- Session Memory: committed after full success.

The Real E2E runner requires the explicit
`--confirm-external-synthetic-data` acknowledgement. Do not run it without a
separate authorization for the exact external-data scope. A failed run must be
kept as evidence rather than silently rerun.

## Reporting rules

- Always identify the named case count and synthetic scope.
- Keep offline deterministic results separate from Real DeepSeek results.
- Do not describe the project as production-grade or zero-hallucination.
- Do not claim that Hybrid always beats BM25.
- Do not convert bounded samples into statements about all events.
- Preserve correlation-versus-causation limitations.
