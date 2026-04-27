# ArXivLens — Multi-Modal RAG over Scientific Literature

Production-grade RAG system over ArXiv ML papers with figure, table, and equation understanding. Hybrid retrieval, citation verification, and CI eval gates. Deployed on GCP for under $60.

**[Design doc](./docs/DESIGN.md)** · **[Blog post](./docs/BLOG.md)** · **Live demo:** _coming after deploy_

---

## What this repo contains

```
arxivlens/
├── infra/terraform/    # All GCP resources as code
├── ingestion/          # ArXiv crawler + Marker PDF parser (Cloud Run job)
├── embedding/          # Chunker + embedder (Cloud Run job)
├── retrieval/          # Hybrid retriever + reranker
├── generation/         # FastAPI app — the public API
├── safety/             # Citation verifier (NLI), input guardrails
├── eval/               # RAGAS, golden set, LLM-as-judge
├── frontend/           # Next.js streaming chat UI
├── scripts/            # pause/resume, local dev, eval runners
├── tests/              # pytest
└── .github/workflows/  # CI with eval gate
```

## Prerequisites

- GCP account with $200 free credits → https://cloud.google.com/free
- Local: Python 3.11+, Docker, gcloud CLI, Terraform 1.6+, Node 20+ (for frontend)
- API keys: Google AI Studio (free Gemini), Anthropic (optional, ~$5 for ablations)

## The 4-weekend plan

| Weekend | What you build | Outcome |
|---|---|---|
| 1 | GCP setup + Terraform + ingestion of 1k papers | Parsed corpus in Cloud Storage |
| 2 | Embedding + pgvector + hybrid retrieval | Working `/retrieve` endpoint |
| 3 | Generation + citation verifier + eval | Working `/chat` endpoint with RAGAS scores |
| 4 | Frontend + observability + CI/CD + blog | Live demo + LinkedIn post |

## Quick start

```bash
# 1. One-time GCP setup
./scripts/00-gcp-setup.sh

# 2. Provision infra (~5 min)
cd infra/terraform/envs/dev && terraform init && terraform apply

# 3. Ingest 100 papers (smoke test, ~10 min)
make ingest-smoke

# 4. Embed them
make embed

# 5. Run API locally
make api-local

# 6. Test it
curl localhost:8000/chat -d '{"query":"What is FlashAttention?"}'
```

## Cost control

This is the most important section if you're new to GCP.

```bash
# At end of every dev session:
make pause
# Stops Cloud SQL, scales Cloud Run to 0, deletes idle resources

# Next session:
make resume
```

Budget tracker: `make cost` prints your month-to-date spend.

## Costs (actual, measured)

| Phase | Cost |
|---|---|
| Infra provisioning | $0 (within free tier) |
| Ingest + embed 5k papers | ~$15 (one-time) |
| Eval runs (200 queries × 3 models) | ~$10 |
| Steady-state demo (paused 80% of time) | ~$5/mo |
| **Total for v1** | **~$30–40** |

## License

MIT
