# Implementation Guide

A step-by-step walkthrough from zero to deployed system. Follow it in order. Each section ends with a ✅ checkpoint — don't move on until that's green.

## Day 0: Prerequisites (15 min)

### Install local tools
```bash
# Python 3.11+
python --version

# Docker
docker --version

# gcloud CLI (see docs/GCP_SETUP.md for install instructions)
gcloud --version

# Terraform
terraform --version  # need 1.6+

# Optional but recommended: uv (fast Python installer)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Clone and install
```bash
git clone <your-repo> arxivlens && cd arxivlens
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in later
```

✅ Checkpoint: `pytest tests/test_smoke.py` passes.

---

## Day 1: GCP Setup + Terraform (1 hour)

### 1. Read `docs/GCP_SETUP.md` end-to-end
This is the most important file in the repo if you're new to GCP. It walks through account creation, the budget alert (do NOT skip this), and CLI setup.

### 2. Authenticate
```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 3. Run the setup script
```bash
make setup
```
This enables ~15 GCP APIs. Takes 1–2 minutes.

### 4. Set the budget alert
Console → Billing → Budgets & alerts → Create. **$50 budget, alerts at 50/75/90/100%**.

### 5. Store the Gemini API key
Get one at https://aistudio.google.com/apikey (free tier).
```bash
echo -n "YOUR_GEMINI_KEY" | gcloud secrets create gemini-api-key --data-file=-
```

### 6. Provision infrastructure
```bash
cp infra/terraform/envs/dev/terraform.tfvars.example infra/terraform/envs/dev/terraform.tfvars
# Edit terraform.tfvars and set project_id

make tf-init
make tf-apply  # type "yes" when prompted; takes ~5 min
```

This creates: 3 Cloud Storage buckets, 2 Pub/Sub topics, 1 Cloud SQL instance with pgvector, an Artifact Registry, a service account.

### 7. Initialize the database schema
```bash
make db-init
```

✅ Checkpoint: `gcloud sql instances list` shows `arxivlens-dev-pg` as RUNNABLE.

### 8. Fill in .env
Use the Terraform outputs:
```bash
cd infra/terraform/envs/dev && terraform output
```
Copy the `raw_bucket`, `parsed_bucket`, `eval_bucket` values into your `.env`.
For `DB_URL`, use:
```bash
gcloud secrets versions access latest --secret=arxivlens-dev-db-url
```

✅ Checkpoint: `python -c "from arxivlens.db import conn; conn().__enter__()"` succeeds.

### 9. **Pause everything before bed**
```bash
make pause
```

---

## Day 2 (Weekend 1): Ingestion + Parsing (3-4 hours)

### Goal
Have 100 papers parsed end-to-end with figures, tables, and equations extracted.

### 1. Resume infra
```bash
make resume
```

### 2. Smoke-test the crawler
```bash
python -m ingestion.crawler --max-papers 10
```

Watch the output — you should see papers being fetched and uploaded to Cloud Storage. Verify:
```bash
gsutil ls gs://arxivlens-dev-raw-YOUR-PROJECT/pdfs/
```

### 3. Test the parser locally
```bash
# Pick one of the arxiv_ids from the crawler output
python -m ingestion.parser --arxiv-id 2403.12345
```

This downloads the PDF from GCS, runs Marker, and writes a parsed manifest. First run will download Marker model weights (~5 GB) — takes 5–10 minutes. Subsequent runs are fast.

Verify:
```bash
gsutil ls gs://arxivlens-dev-parsed-YOUR-PROJECT/manifest/
gsutil cat gs://arxivlens-dev-parsed-YOUR-PROJECT/manifest/2403.12345.json | head
```

### 4. Scale to 100 papers
```bash
python -m ingestion.crawler --max-papers 100
# Then in another terminal:
python -m ingestion.parser --subscribe  # Ctrl+C when queue drains
```

**Important about parsing speed:** Marker on CPU does 1 paper in ~30–60 seconds. 100 papers = ~1 hour. For the v1 5,000-paper run, you'll want to either (a) run it overnight on your laptop, or (b) deploy the parser as a Cloud Run job with concurrency=4. The Day 5 deployment section covers (b).

✅ Checkpoint: ~100 manifests in `gs://arxivlens-dev-parsed-*/manifest/`.

### 5. Pause
```bash
make pause
```

---

## Day 3 (Weekend 2): Embedding + Retrieval (3-4 hours)

### Goal
Working `/retrieve` endpoint returning chunks for any query.

### 1. Resume + embed
```bash
make resume
python -m embedding.embedder --backfill
```

Watch the logs — you should see ~30 chunks per paper getting embedded and inserted. With 100 papers = ~3,000 chunks, this takes ~10 minutes (most of it network I/O).

### 2. Build the HNSW index
After bulk-loading is done, build the vector index for fast search:
```bash
psql "$(gcloud secrets versions access latest --secret=arxivlens-dev-db-url)" <<EOF
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
EOF
```

### 3. Test retrieval locally
```bash
make api-local
```

In another terminal:
```bash
curl -X POST http://localhost:8000/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"query": "What is FlashAttention?", "top_k": 3}' | jq
```

You should see 3 ranked chunks with `rrf_score`.

✅ Checkpoint: Retrieval returns relevant chunks for several test queries.

### 4. Try filters
```bash
curl -X POST http://localhost:8000/retrieve \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "attention mechanism",
    "filters": {"year_min": 2023, "modality": "text"},
    "top_k": 5
  }'
```

---

## Day 4 (Weekend 3): Generation + Eval (3-4 hours)

### 1. Test the full /chat endpoint
```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -N \
  -d '{"query": "What is FlashAttention?", "top_k": 3}'
```

You'll see SSE events streaming: `chunks` → `token` × N → `verification` → `done`.

### 2. Run the eval suite
```bash
make eval
cat eval/results.json | jq '.summary'
```

You'll get retrieval and faithfulness numbers across the 12-question golden set. Expect:
- recall@10: 0.5–0.8 with only 100 papers (low because golden set asks about specific papers that may not be in your subset)
- faithfulness: 0.7–0.9

### 3. Set the baseline
```bash
cp eval/results.json eval/baseline.json
git add eval/baseline.json && git commit -m "Initial eval baseline"
```

Now any PR that regresses recall or faithfulness by >2pp will fail CI.

### 4. Expand the golden set
The shipped `golden_set_v1.jsonl` has 12 entries. To get to 200, add hand-crafted entries based on papers actually in your corpus. Pattern:
1. Browse `gs://arxivlens-dev-parsed-*/manifest/` to find interesting papers.
2. Write 5 questions per paper covering different query types.
3. For each, fill `gold_paper_ids`. Where you can, add `gold_chunk_ids` by inspecting the manifest.

✅ Checkpoint: `eval/baseline.json` committed; eval gate runs locally.

---

## Day 5 (Weekend 4): Deploy + Frontend + Blog (4-6 hours)

### 1. Build and push images
```bash
make build-api
make build-worker
```

### 2. Deploy the API
```bash
make deploy-api
```

Outputs a URL like `https://arxivlens-dev-api-abc123.run.app`. Test:
```bash
curl https://arxivlens-dev-api-abc123.run.app/health
```

### 3. Deploy the parser as a Cloud Run job
For ongoing ingestion. Run once for backfill, then on a schedule.
```bash
gcloud run jobs create arxivlens-dev-parser \
  --image=us-central1-docker.pkg.dev/$PROJECT_ID/arxivlens-dev-images/worker:latest \
  --region=us-central1 \
  --service-account=arxivlens-dev-sa@$PROJECT_ID.iam.gserviceaccount.com \
  --memory=4Gi --cpu=2 --task-timeout=3600 --max-retries=2 \
  --set-env-vars="PROJECT_ID=$PROJECT_ID,ENV=dev,..." \
  --set-secrets="DB_URL=arxivlens-dev-db-url:latest"

# Run it
gcloud run jobs execute arxivlens-dev-parser --region=us-central1
```

### 4. Scale up the corpus
With the parser deployed as a Cloud Run job, you can confidently ingest 5,000 papers:
```bash
python -m ingestion.crawler --max-papers 5000  # Run from your laptop, ~2 hours
# Parser job consumes the Pub/Sub queue automatically
```

### 5. Re-run eval on the full corpus
```bash
make eval
```

### 6. Add observability
- Console → Cloud Trace: latency breakdown per /chat request
- Console → Logs Explorer: query `resource.type="cloud_run_revision"` to see logs
- Optional: sign up at langfuse.com for hosted LLM tracing (free tier, 50k events/mo)

### 7. Write the blog
The design doc (`docs/DESIGN.md`) is structured to map 1:1 to blog sections. Lift each section, add code snippets from the repo, and add the architecture diagram. Target 2,500–3,500 words.

### 8. Frontend (optional but high-leverage)
A streaming Next.js chat UI is in `frontend/`. Deploy with:
```bash
cd frontend && vercel  # or another Cloud Run service
```

✅ Final checkpoint: A live URL, a published blog, and a LinkedIn post.

---

## Troubleshooting

### "Permission denied" from Terraform
Console → IAM → your email → grant "Owner" role on the project.

### Cloud SQL takes forever
First-time provisioning is 5–10 min. Subsequent stop/start is ~1 min. If stuck >15 min, check the operations log: Console → SQL → instance → Operations.

### Marker fails to load models on first run
It downloads ~5 GB to `~/.cache/huggingface/`. On Cloud Run, set `HF_HOME=/tmp` and increase the timeout to 600s.

### Embeddings are empty / 401 errors
Vertex AI requires the SA to have `roles/aiplatform.user`. Terraform grants this; verify with:
```bash
gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" \
  --filter="bindings.members:arxivlens-dev-sa@*"
```

### "vector type does not exist" when running queries
You forgot to run `make db-init`. The pgvector extension must be created in each database.

### Cloud Run cold starts are slow
First request after idle is 5–15s (model load). Set `min-instances=1` for $5/mo if you want zero cold starts. For demo use, cold start is fine.

---

## Daily routine (after initial build)

```bash
make resume       # at start of session
# ... develop ...
make test
make eval         # periodic, before pushing
git push          # CI runs the eval gate on the PR
make pause        # at end of session
```

That's it. The pause/resume habit is what keeps you under $40 total.
