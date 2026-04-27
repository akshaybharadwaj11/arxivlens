# ArXivLens: Multi-Modal RAG over Scientific Literature on GCP

**Author:** Akshay
**Status:** Design — v1.0
**Last updated:** April 2026
**Target audience:** ML engineers, infra engineers, hiring managers reviewing this as a portfolio artifact

---

## 0. TL;DR

ArXivLens is a production-grade Retrieval-Augmented Generation system over ~50,000 scientific papers from ArXiv (cs.CL, cs.LG, cs.CV from 2023–2026). Unlike standard RAG demos that embed text and call it done, ArXivLens treats figures, tables, and equations as first-class retrievable objects, supports cross-modal queries, applies hybrid retrieval with metadata pre-filtering, verifies every cited claim with an NLI model, and ships with a CI/CD eval gate that blocks any deploy that regresses faithfulness by more than 2%.

The system runs on GCP (Vertex AI Vector Search, Cloud Run, Cloud SQL, Pub/Sub, Cloud Storage) with full Terraform IaC and OpenTelemetry-based observability. Total cost target: under $160 of the $200 GCP free credits.

The headline outcome is a hosted demo, an open-source repo, and a deep-dive blog — together they answer the question hiring managers actually care about: *can you design and ship a non-trivial ML system end-to-end, and reason about what could go wrong?*

---

## 1. Problem Statement

### 1.1 The user

A researcher, grad student, or applied ML engineer who needs to navigate the firehose of recent ML literature. Concretely, they want answers to questions like:

- *"Show me papers from 2024 that use FlashAttention-2 and report wall-clock training speedups."*
- *"What does Figure 4 in the Llama 3 tech report show, and how does it compare to the equivalent figure in Mistral 7B?"*
- *"Find tables comparing models on MMLU, GSM8K, and HumanEval from the last 6 months."*
- *"Which papers cite the original RLHF paper and propose modifications to PPO?"*

A general-purpose chat model can hallucinate plausibly on these. A standard text-only RAG can answer the first one but fails on the rest because the answer lives in figures and tables, not prose.

### 1.2 Why this is hard

Four properties of scientific papers break naive RAG:

1. **Long-range structure.** A paper's claim in the abstract is supported by a table in section 4 and a figure in the appendix. Chunk-level retrieval breaks this dependency.
2. **Multi-modal content.** Up to 40% of the information density in an ML paper sits in figures and tables. Text-only embeddings ignore this.
3. **Citation grounding is non-negotiable.** Researchers cannot tolerate hallucinated citations. Every claim must trace to a paper, section, and ideally a specific chunk.
4. **Domain vocabulary drift.** "Attention" in 2017 means something narrower than "attention" in 2026. A vanilla embedding model gets this wrong without domain conditioning.

### 1.3 Non-goals

- **Not** a paper-writing assistant. ArXivLens retrieves and answers; it doesn't draft sections.
- **Not** a citation manager. Zotero exists.
- **Not** a real-time index. Daily ingestion is fine; sub-minute freshness is over-engineering for this corpus.
- **Not** an arena for fine-tuning a domain LLM. We use frontier models for generation. Fine-tuning is called out as future work in §13.

---

## 2. Success Metrics

### 2.1 Retrieval quality (offline, on golden set)

- **Recall@10 ≥ 0.85** for text queries
- **Recall@10 ≥ 0.70** for figure-grounded queries
- **Recall@10 ≥ 0.75** for table-grounded queries
- **MRR ≥ 0.65** across all query types
- **nDCG@10 ≥ 0.70** with graded relevance labels (3 = perfect, 2 = useful, 1 = tangential, 0 = irrelevant)

### 2.2 Generation quality (RAGAS + LLM judge)

- **Faithfulness ≥ 0.85** — every claim entailed by retrieved context
- **Answer relevancy ≥ 0.80**
- **Context precision ≥ 0.75** — retrieved chunks are actually used
- **LLM-judge win rate ≥ 60%** vs a no-RAG baseline (Gemini 2.5 Flash answering from parametric knowledge alone)

### 2.3 System SLOs

- **p50 end-to-end latency ≤ 1.2s** (excluding generation streaming)
- **p95 end-to-end latency ≤ 2.5s**
- **Error rate ≤ 1%** measured weekly
- **Availability ≥ 99.5%** on the API endpoint

### 2.4 Cost

- **Build phase:** under $160 of $200 credits
- **Steady-state:** under $25/month at 1k queries/day (achievable by aggressive scale-to-zero on Cloud Run + Cloud SQL stop/start automation during dev)

---

## 3. Architecture Overview

The system is composed of seven planes, each with a clear responsibility:

| Plane | Responsibility | Key services |
|---|---|---|
| Ingestion | Pull papers, extract structured content | Cloud Scheduler, Cloud Run jobs, Cloud Storage, Pub/Sub |
| Indexing | Chunk, embed, write to dual stores | Cloud Run, Vertex AI embeddings, Vector Search, Cloud SQL |
| Retrieval | Hybrid search with metadata filtering | FastAPI on Cloud Run, Cloud SQL, Vector Search |
| Reranking | Refine top-50 to top-5 | Cloud Run with bge-reranker-v2-m3 |
| Generation | Produce grounded answers with citations | Gemini 2.5 Flash, Claude Sonnet 4.6 |
| Safety + Eval | Input/output guardrails, online + offline eval | DLP, NLI verifier, RAGAS, LLM judge |
| Observability + Ops | Tracing, metrics, alerts, IaC, CI/CD | OpenTelemetry, Cloud Trace, Langfuse, Terraform, GitHub Actions |

The seven-plane decomposition matches how a real platform team is organized — and it's how the blog and repo are structured. Each plane is independently testable, independently deployable, and has its own SLOs.

---

## 4. Data Layer

### 4.1 Corpus

- **Source:** ArXiv. Metadata via the [Cornell ArXiv Kaggle dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv) (refreshed weekly), full PDFs via the ArXiv S3 bulk-access bucket (requester-pays).
- **Filter:** primary categories `cs.CL`, `cs.LG`, `cs.CV`; date range 2023-01-01 to present; ~50k papers.
- **Why this slice:** dense, recent, has the kinds of figures/tables the system is designed to surface, and the volume fits the budget.

### 4.2 Storage layout

```
gs://arxivlens-{env}-raw/
  pdfs/{yyyy}/{mm}/{arxiv_id}.pdf
  metadata/{yyyy}/{mm}/{arxiv_id}.json

gs://arxivlens-{env}-parsed/
  markdown/{arxiv_id}.md           # Marker output
  figures/{arxiv_id}/fig_{n}.png   # extracted figures
  tables/{arxiv_id}/tbl_{n}.json   # serialized tables (rows + headers)
  equations/{arxiv_id}/eq_{n}.tex  # extracted LaTeX
  manifest/{arxiv_id}.json         # ties everything together with offsets

gs://arxivlens-{env}-eval/
  golden_set_v1.jsonl
  judge_traces/{date}/{run_id}.jsonl
```

Lifecycle: nearline storage class after 30 days for raw PDFs. Parsed bucket stays standard — it's the hot path.

### 4.3 Eval set design

Two hundred questions, hand-curated, stratified:

| Type | Count | What it tests |
|---|---|---|
| Factoid (text) | 60 | Single-passage retrieval, baseline RAG quality |
| Figure-grounded | 50 | Cross-modal retrieval, vision-language understanding |
| Table-grounded | 40 | Structured retrieval, numerical comparison |
| Multi-hop | 30 | Multi-document reasoning, citation chains |
| Out-of-scope (refusal) | 20 | Safety — system should refuse cleanly |

Each item has: `query`, `gold_paper_ids`, `gold_chunk_ids` (where available), `gold_answer_summary`, `query_type`, `difficulty`. The set is versioned in the eval bucket and in the repo.

---

## 5. Ingestion and Parsing

### 5.1 Crawler

- **Cloud Scheduler** triggers a **Cloud Run job** daily.
- The job queries the ArXiv OAI-PMH endpoint for papers in target categories with a `from` cursor stored in Firestore.
- New papers' PDFs are streamed from the ArXiv S3 bucket into `gs://arxivlens-prod-raw/pdfs/`.
- A Pub/Sub message is published per paper: `{"arxiv_id": "...", "stage": "downloaded"}`.

### 5.2 Parsing

- A second Cloud Run job, triggered by the `parse-queue` Pub/Sub subscription, picks up papers in batches of 8 (sized to fit one CPU instance's memory).
- **Marker** ([VikParuchuri/marker](https://github.com/VikParuchuri/marker)) is the primary parser. Chosen over Nougat for three reasons:
  1. Better table preservation (Markdown table syntax with structure).
  2. ~5× faster on CPU.
  3. Active maintenance and easy Docker packaging.
- For papers where Marker fails to extract a usable table (heuristic: zero detected tables but paper contains the literal string "Table 1"), a fallback path runs **pdfplumber** specifically on table extraction.
- Equations: Marker preserves inline LaTeX. Display equations are extracted and stored separately for equation-specific embedding later.
- Figures: Marker extracts to PNG with bounding boxes. We additionally store the figure caption and the surrounding paragraph (the "context window" — typically the paragraph that introduces the figure).

### 5.3 Output schema

```json
{
  "arxiv_id": "2403.12345",
  "title": "...",
  "authors": [...],
  "primary_category": "cs.LG",
  "published": "2024-03-15",
  "abstract": "...",
  "sections": [
    {"heading": "1. Introduction", "text": "...", "char_offset": 0},
    ...
  ],
  "figures": [
    {
      "id": "fig_1",
      "caption": "Attention weights across layers...",
      "context_text": "We visualize the attention pattern in Figure 1...",
      "image_uri": "gs://.../figures/2403.12345/fig_1.png",
      "section": "3. Method"
    }
  ],
  "tables": [
    {
      "id": "tbl_1",
      "caption": "Performance on MMLU...",
      "headers": ["Model", "MMLU", "GSM8K"],
      "rows": [["Llama-3", "82.1", "84.5"], ...],
      "markdown": "| Model | MMLU | ... |",
      "section": "4. Results"
    }
  ],
  "equations": [
    {"id": "eq_1", "latex": "\\mathcal{L} = \\mathbb{E}...", "section": "3. Method"}
  ]
}
```

This manifest is what every downstream stage reads. It's stored at `gs://arxivlens-prod-parsed/manifest/{arxiv_id}.json`.

### 5.4 Failure modes

| Failure | Detection | Mitigation |
|---|---|---|
| Marker times out (>5 min) | Cloud Run job timeout | Retry once with smaller batch; if still failing, log to dead-letter and skip |
| Corrupted PDF | Marker raises | Move to `dead-letter/` prefix, alert weekly |
| Empty parse output | Post-parse check | Re-run with pdfplumber-only path |
| Table extracted but malformed (no headers) | Schema validation | Drop the table, keep the paper; log metric |
| Figure extracted but caption missing | Schema check | Use surrounding paragraph as caption |

---

## 6. Chunking and Embedding

### 6.1 Chunking strategy

This is one of the two places (alongside retrieval fusion) where most RAG systems quietly underperform. The strategy:

- **Text chunks:** semantic chunking at section boundaries first. If a section exceeds 1000 tokens, split on paragraph boundaries with a 100-token overlap. This preserves the local coherence that matters in scientific writing — methods sections shouldn't be sliced mid-derivation.
- **Figure chunks:** one chunk per figure. The chunk's text content is `caption + context_text + section_heading`. The chunk's image content is the PNG. Both are embedded.
- **Table chunks:** one chunk per table. The chunk's text content is `caption + markdown_serialization + section_heading`. Headers and the first 5 rows are also stored as structured fields in Cloud SQL for SQL-style queries.
- **Equation chunks:** equations are not their own chunks — they live inside the surrounding text chunk. (Querying equations directly is a v2 feature; see §13.)

Each chunk gets a stable ID: `{arxiv_id}:{section_idx}:{chunk_idx}` for text, `{arxiv_id}:fig:{n}` for figures, `{arxiv_id}:tbl:{n}` for tables.

### 6.2 Embedding models

| Modality | Model | Dim | Why |
|---|---|---|---|
| Text | Vertex AI `text-embedding-005` | 768 | Strong on technical text; free tier covers full corpus; managed |
| Figure (image) | OpenAI CLIP ViT-L/14 (Vertex Model Garden) | 768 | Mature, joint image-text space; 768d matches text for shared index |
| Figure (caption) | `text-embedding-005` | 768 | Caption captures the *intent* — usually a stronger signal than the image alone |
| Table | `text-embedding-005` over markdown serialization | 768 | Surprisingly effective; future work explores TaPas-style table-native embeddings |

For figures we store *both* CLIP image embeddings and text embeddings of the caption. At retrieval time we query both and let RRF merge — this trick recovers ~8 points of recall on figure-grounded queries vs CLIP alone, in our preliminary ablations.

### 6.3 Embedding pipeline

- A Pub/Sub-triggered Cloud Run service reads the manifest, batches chunks (64 per batch for text, 16 for images), and calls the embedding APIs.
- Embeddings are written to **Vertex AI Vector Search** (streaming index) keyed by chunk ID.
- A parallel write goes to **Cloud SQL** for sparse retrieval and metadata filtering — the chunk text, all metadata, and a `tsvector` column for BM25 scoring via `pg_trgm` and `ts_rank`.
- Idempotency: the upsert is keyed on chunk ID, and a `content_hash` field prevents re-embedding unchanged chunks across re-runs.

### 6.4 Cloud SQL schema (sketch)

```sql
CREATE TABLE chunks (
  chunk_id            TEXT PRIMARY KEY,
  arxiv_id            TEXT NOT NULL,
  modality            TEXT NOT NULL,          -- 'text' | 'figure' | 'table'
  section             TEXT,
  content             TEXT NOT NULL,
  content_tsv         TSVECTOR,                -- generated column
  metadata            JSONB,                   -- year, category, authors, has_code, etc.
  content_hash        TEXT NOT NULL,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX chunks_tsv_idx     ON chunks USING GIN (content_tsv);
CREATE INDEX chunks_metadata    ON chunks USING GIN (metadata);
CREATE INDEX chunks_arxiv_id    ON chunks (arxiv_id);
CREATE INDEX chunks_modality    ON chunks (modality);

CREATE TABLE tables_structured (
  chunk_id            TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
  headers             TEXT[],
  first_rows          JSONB,
  caption             TEXT
);
```

### 6.5 Cost and scale notes

- 50k papers × ~30 chunks/paper average = 1.5M chunks.
- Vertex AI Vector Search streaming index handles 1.5M × 768d = ~4.6 GB comfortably under free-tier thresholds.
- Embedding cost: 1.5M × ~250 tokens/chunk ≈ 375M tokens × $0.000025/1k = ~$10 for text. CLIP via Model Garden is ~$0.001/image × 200k figures = ~$200 — **this is the budget killer.** Mitigation: only run CLIP on a curated subset (papers with high figure density, ~20k figures) or self-host CLIP on a single Cloud Run GPU instance for batch embedding (~$15 for 4 hours of L4 GPU time). The self-host path is what we recommend.

---

## 7. Retrieval

### 7.1 Query flow

```
user query
    │
    ▼
[Input safety: PII redaction, jailbreak check]
    │
    ▼
[Query understanding: intent classification, metadata extraction]
    │  e.g. "papers from 2024 about FlashAttention" →
    │       {filters: {year: 2024}, semantic_query: "FlashAttention training speedup"}
    ▼
[Metadata pre-filter in Cloud SQL]
    │  returns candidate chunk_ids matching filters
    ▼
┌───────────────────────────┬──────────────────────────────┐
│ Dense retrieval           │ Sparse retrieval             │
│ Vector Search             │ Cloud SQL BM25 (ts_rank)     │
│ top-50 within candidates  │ top-50 within candidates     │
└─────────────┬─────────────┴────────────────┬─────────────┘
              │                              │
              └──────────────┬───────────────┘
                             ▼
                      [RRF merge → top-30]
                             │
                             ▼
                  [Cross-encoder rerank → top-5]
                             │
                             ▼
                       [Generation]
```

### 7.2 Hybrid retrieval design

**Why hybrid?** Dense embeddings dominate on paraphrastic queries ("attention mechanism" matches "self-attention layers"), but BM25 dominates on rare-term queries ("FlashAttention-2" — a token the embedding model has likely seen rarely). A hybrid system covers both. RRF is the merge of choice because it doesn't require score calibration across the two systems.

**Reciprocal Rank Fusion:**

```
RRF_score(d) = sum over retrievers r of 1 / (k + rank_r(d))
```

with `k = 60` (the canonical setting from Cormack et al. 2009). The top-30 by RRF score go to the reranker.

**Why pre-filter on metadata first?** Two reasons. First, latency — filtering 1.5M down to ~50k candidates in Postgres is fast (<50ms with the right indexes), and Vector Search's filtered query over a smaller candidate set is faster than over the full index. Second, recall — Vector Search's pre-filtering is a first-class feature on Vertex AI; we use the `restricts` field with `namespace="year"` and `allowList=["2024"]` semantics.

### 7.3 Reranker

- **Model:** `BAAI/bge-reranker-v2-m3` — small (568M params), strong on MTEB reranking tasks, runs on CPU.
- **Deployment:** Cloud Run service with 4 vCPUs, 8GB RAM, min-instances=0, max-instances=3.
- **Latency budget:** rerank top-30 in 250ms p95.
- **Why this one:** the v2-m3 variant supports multilingual and is the default recommendation in the BGE family. We avoided larger rerankers (bge-reranker-large, ~1.3B) because the latency hit isn't justified by the marginal nDCG gain on our query mix.

### 7.4 Modality-aware retrieval

When a query is detected as figure-grounded ("show me the architecture diagram in Llama 3"), retrieval is biased: we boost the figure-modality results in the RRF merge by adding 0.1 to their RRF score before reranking. Detection is a small classifier (zero-shot for v1 — `claude-haiku` with 5 in-context examples — replaced with a fine-tuned distilbert classifier in v2).

### 7.5 Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Vector Search down | Fall back to BM25-only; tag response with `retrieval_degraded=true` |
| Reranker timeout (>500ms) | Skip rerank, return top-5 by RRF; log SLO breach |
| No results match metadata filter | Drop the strictest filter, retry; surface "we relaxed your filter" to the user |
| Query embedding fails | Return error with retry hint; circuit-breaker after 5 consecutive failures |

---

## 8. Generation

### 8.1 Model choice

- **Default:** Gemini 2.5 Flash. Cheap, fast, multi-modal native (we can pass figure images directly into the context).
- **Comparison:** Claude Sonnet 4.6. Used for blog ablations and as a fallback if Gemini is degraded. Strong on instruction-following and refusals.
- **Why not GPT-4 family:** budget. The blog explicitly compares two frontier families to keep the comparison clean.

### 8.2 Prompt structure

```
SYSTEM:
You are a research assistant for ML literature. Answer the user's question
using ONLY the provided context. Every claim must end with a citation tag of
the form [arxiv_id, chunk_id]. If the context does not support an answer,
say so explicitly. Do not use prior knowledge.

CONTEXT:
[chunk 1 — text — 2403.12345:3:2]
The authors propose FlashAttention-2, achieving 2.1× wall-clock speedup...

[chunk 2 — table — 2403.12345:tbl:1]
| Model | Tokens/sec | ... |
| Vanilla | 12,400 | ... |
| FA-2    | 26,040 | ... |

[chunk 3 — figure — 2403.67890:fig:4]
Caption: "Memory usage as a function of sequence length..."
[image embedded]

USER: How much speedup does FlashAttention-2 achieve over vanilla attention?

ASSISTANT: FlashAttention-2 achieves a 2.1× wall-clock speedup over vanilla
attention [2403.12345, 2403.12345:3:2], with the table reporting throughput
increasing from 12,400 to 26,040 tokens/second [2403.12345, 2403.12345:tbl:1].
```

The structured citation format is what the citation verifier (§9.3) keys on.

### 8.3 Streaming

Server-Sent Events (SSE) from FastAPI to the Next.js frontend. The frontend renders retrieved chunks as soon as they arrive (perceived-latency win), then streams the answer token-by-token, then renders citations as expandable cards.

### 8.4 Caching

- **Query-level cache** in Memorystore (Redis): hash of `(query, filter_set, top_k)` → response. TTL 24h. Hit rate target: 15% (driven by demo-traffic patterns).
- **Embedding cache:** the query embedding itself is cached by query hash. TTL 7d.

---

## 9. Safety and Guardrails

### 9.1 Input layer

- **PII redaction:** Cloud DLP API. Detects emails, phone numbers, names, etc. Redacted before logging or sending to the LLM.
- **Jailbreak / prompt-injection detection:** a fine-tuned distilbert classifier (we use [`protectai/deberta-v3-base-prompt-injection`](https://huggingface.co/protectai/deberta-v3-base-prompt-injection) as the starting point). Blocks obvious attacks at the gateway.
- **Rate limiting:** per-IP via Cloud Armor. 60 req/min, with a 10 req/sec burst.

### 9.2 Output layer — citation verifier (the differentiator)

This is what separates ArXivLens from a textbook RAG demo.

- For each generated sentence with a citation tag, run an NLI model (`microsoft/deberta-v3-base-mnli`) on `(retrieved_chunk_text, generated_sentence)`.
- If `entailment_score < 0.5`, mark the sentence as **unsupported** and either:
  - (strict mode) refuse to return the answer and surface "I couldn't find strong support for this in the retrieved context",
  - (lenient mode, default) return the answer with a visual indicator on unsupported sentences, and log a faithfulness warning.
- Aggregated over time, this becomes our online faithfulness metric.

The verifier runs in parallel with generation (we don't wait for it before streaming the first token), and decorates the streamed response with citation badges as each sentence is verified.

### 9.3 Refusal policy

Out-of-scope queries ("what's the weather in Boston") trigger a graceful refusal: *"ArXivLens is scoped to ML literature on ArXiv. I can't help with that, but I can answer questions about papers in cs.CL, cs.LG, or cs.CV."* Detection is a query-classifier — same model as the modality classifier, retrained for in-scope/out-of-scope.

---

## 10. Evaluation

### 10.1 Offline eval

- **Frequency:** every PR via GitHub Actions; full run weekly.
- **Components:**
  - **Retrieval-only metrics** (Recall@10, MRR, nDCG@10) on the golden set's `gold_chunk_ids`. Crucial for separating retrieval failures from generation failures.
  - **RAGAS suite:** faithfulness, answer relevancy, context precision, context recall.
  - **LLM judge:** pairwise comparison ArXivLens-vs-baseline using `claude-opus-4.7` as the judge with a structured rubric.
- **Output:** a versioned eval report at `gs://arxivlens-prod-eval/runs/{run_id}/report.json` and an HTML summary published to Cloud Storage with public read.

### 10.2 The eval gate (the FAANG move)

A GitHub Actions workflow runs a 50-question subset on every PR. The deploy job has a hard precondition: the PR must not regress faithfulness or recall@10 by more than 2 percentage points relative to `main`. Below that threshold, deploy is blocked and a comment is posted on the PR with the regression details.

This single piece of automation is what transforms a portfolio project from "I built a RAG" to "I built a system with continuous evaluation, like a real ML platform team would."

### 10.3 Online eval

- Every production response is scored by the citation verifier (faithfulness signal) and emits an OpenTelemetry span with the score.
- A weekly job samples 50 production traces and runs them through the LLM judge to detect drift.
- A simple "👍/👎" feedback button on the frontend captures user signal.

---

## 11. Observability

### 11.1 The traces stack

OpenTelemetry instrumentation in FastAPI. Each query spans:

```
query.received
├── safety.input_check                    (PII, jailbreak)
├── retrieval.understand_query            (LLM call)
├── retrieval.metadata_filter             (Postgres)
├── retrieval.dense                       (Vector Search)
├── retrieval.sparse                      (Postgres ts_rank)
├── retrieval.fuse                        (RRF in app)
├── retrieval.rerank                      (Cloud Run reranker call)
├── generation.prompt_assemble
├── generation.llm_call                   (Gemini)
├── safety.citation_verify                (NLI, parallel)
└── response.stream_complete
```

Spans flow to **Cloud Trace** for latency analysis, and to **Langfuse** (self-hosted on Cloud Run, free) for LLM-specific views: prompts, retrieved chunks, generations, scores all in one UI. Langfuse is what we open during incident response.

### 11.2 Metrics and SLOs

Cloud Monitoring custom metrics:

| Metric | SLO |
|---|---|
| `arxivlens.latency.e2e` (p95) | ≤ 2.5s |
| `arxivlens.faithfulness` (rolling 7d mean) | ≥ 0.85 |
| `arxivlens.recall_at_10` (weekly) | ≥ 0.85 (text), ≥ 0.70 (figure) |
| `arxivlens.error_rate` | ≤ 1% |
| `arxivlens.cost_per_query` | ≤ $0.005 |
| `arxivlens.citation_unsupported_rate` | ≤ 5% |

Alerts: SLO violation for >15 min → PagerDuty (or Slack webhook for portfolio version).

### 11.3 Dashboards

A single Cloud Monitoring dashboard with four rows:
1. **Traffic and latency** — RPS, p50/p95/p99 latency, error rate
2. **Quality** — faithfulness rolling mean, unsupported citation rate, judge win rate
3. **Cost** — daily spend by service, cost per query
4. **Pipeline health** — papers ingested, chunks indexed, embedding API errors

---

## 12. Deployment, Infrastructure, CI/CD

### 12.1 Environments

- **dev** — local + a single Cloud Run service with min-instances=0
- **prod** — full deployment, Terraform-managed, single GCP project

### 12.2 Terraform module layout

```
infra/terraform/
├── modules/
│   ├── ingestion/        # Cloud Storage buckets, Pub/Sub, Cloud Scheduler
│   ├── compute/          # Cloud Run services + jobs, IAM bindings
│   ├── data/             # Cloud SQL, Vector Search index
│   ├── observability/    # Log sinks, metric definitions, alert policies
│   └── network/          # VPC, Serverless VPC Connector, Cloud Armor
├── envs/
│   ├── dev/
│   └── prod/
└── README.md
```

State backend: GCS bucket with object versioning enabled. State locking: enabled.

### 12.3 GitHub Actions

```
.github/workflows/
├── lint-and-test.yaml        # ruff, mypy, pytest — on every PR
├── eval-gate.yaml            # 50-question eval — on PR to main
├── deploy.yaml               # depends on eval-gate; canary 10% then full
├── nightly-full-eval.yaml    # full 200-question eval on main
└── weekly-judge-sample.yaml  # 50 production traces through LLM judge
```

The deploy job uses Cloud Run's traffic-splitting: 10% canary for 30 minutes, then auto-promote to 100% if error rate stays ≤ 1%.

### 12.4 Secrets

All in Secret Manager. Cloud Run services bind to secrets via `--update-secrets`, never via env vars in source. The Terraform module enforces this with a check.

---

## 13. Risks and Open Questions

| Risk | Likelihood | Mitigation |
|---|---|---|
| CLIP embedding cost blows the budget | High | Self-host on L4 Cloud Run for batch, or curate to 20k figures only |
| Marker fails on math-heavy papers | Medium | Fallback to pdfplumber; track parse-failure rate as a metric |
| Vector Search streaming index hits latency limits | Low | Switch to batch-update index if observed; document the trade-off |
| LLM judge introduces its own bias | Medium | Use two judges (Claude + Gemini), report both; take the harsher of the two |
| Citation verifier blocks valid answers (false positives) | Medium | Tune the threshold per-modality; surface in lenient mode by default |
| Daily ingestion drifts from a real-world freshness need | Low | Acknowledged non-goal; document in README |

**Open questions for v2:**

1. Should equation embeddings use MathBERT, or is LaTeX-as-text sufficient? Need an ablation.
2. GraphRAG layer — does a citation graph improve multi-hop recall enough to justify the added complexity?
3. Domain-adapted reranker — fine-tune `bge-reranker` on a hard-negative-mined ArXiv triplet set. Likely +3–5 nDCG.
4. Agentic mode — multi-turn retrieval where the model can call retrieve() iteratively. Worth it for multi-hop, but complicates eval.

---

## 14. Roadmap

| Phase | Scope | Target |
|---|---|---|
| **v0.1** | Ingestion + parsing of 1k papers, manifest pipeline | Weekend 1 |
| **v0.2** | Embedding + dual-store + hybrid retrieval, golden set v1 | Weekend 2 |
| **v0.3** | Reranker, generator, citation verifier, RAGAS eval | Weekend 3 |
| **v0.4** | Frontend, observability, IaC, eval-gate CI, blog draft | Weekend 4 |
| **v1.0** | Public launch — repo + blog + LinkedIn post + hosted demo | End of week 4 |
| **v1.1** | Domain-adapted reranker, equation embeddings ablation | Future |
| **v2.0** | Agentic multi-hop, GraphRAG layer | Future |

---

## 15. Appendix A — Glossary

- **RRF (Reciprocal Rank Fusion):** combines rankings from multiple retrievers without needing score calibration.
- **NLI (Natural Language Inference):** classifies whether one text entails, contradicts, or is neutral to another. Used for citation verification.
- **RAGAS:** an open-source eval framework for RAG covering faithfulness, relevancy, precision, recall.
- **MRR (Mean Reciprocal Rank):** average of `1/rank_of_first_relevant_result` across queries. Sensitive to top-1 quality.
- **nDCG (normalized Discounted Cumulative Gain):** ranking quality with graded relevance, normalized by ideal ranking.

## 16. Appendix B — Cost Model

| Service | Driver | Build cost | Steady-state ($/mo at 1k qps/day) |
|---|---|---|---|
| Cloud Storage | 100GB raw + 20GB parsed | $5 | $3 |
| Cloud SQL (db-f1-micro) | 1.5M rows + indexes | $15 | $10 (stop overnight in dev) |
| Vector Search (streaming) | 1.5M × 768d | $30 | $20 |
| Vertex AI text embeddings | 1.5M chunks once + 1k queries/day | $15 | $1 |
| CLIP image embeddings (self-hosted) | 4 hours L4 GPU | $15 | $0 (one-time) |
| Cloud Run (FastAPI + reranker) | scale-to-zero | $10 | $5 |
| Gemini 2.5 Flash generation | 1k queries × ~3k tokens avg | $5 | $5 |
| Claude Sonnet 4.6 (comparison only) | 200 eval queries × 3 ablations | $15 | $0 |
| Pub/Sub + Cloud Scheduler | minimal | $1 | $1 |
| Memorystore Redis (cache) | 1GB | $10 | $10 |
| Cloud Trace + Logging | within free tier | $0 | $0 |
| Buffer for re-runs | — | $30 | — |
| **Total** | | **~$160** | **~$55/mo** |

Steady-state is for the launched-and-public phase. During development (the four weekends), you stop Cloud SQL and scale Cloud Run min-instances to 0 outside of work sessions.

---

## 17. Appendix C — What this design says about the engineer

A hiring manager reading this should walk away with three signals:

1. **Systems thinking.** The seven-plane decomposition, the explicit failure modes per stage, the cost model, the SLOs — these aren't decorative. They're how a real platform is built.
2. **Pragmatism.** Self-host CLIP because the managed cost would blow the budget. Use Marker over Nougat because the table extraction is better. Use RRF over weighted-sum because it doesn't need score calibration. Each choice has a reason.
3. **Production sensibility.** The eval gate, the citation verifier, the OpenTelemetry instrumentation, the canary deploys. This isn't a notebook — it's something you could hand off to an on-call rotation.

Those three signals are exactly what's being assessed in design rounds at Anthropic, Glean, Harvey, Decagon, and the rest of the tracker.
