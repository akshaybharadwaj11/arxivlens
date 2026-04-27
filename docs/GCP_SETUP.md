# GCP Setup Guide for ArXivLens

If you've never used GCP before, do every step in this file in order before touching any other code. This is a one-time setup that takes about 30 minutes.

## 1. Create a GCP account and project

1. Go to https://cloud.google.com/free and sign up. You get $300 credits valid for 90 days. (Plus your $200 = effectively $500 — but we'll target $40 spend.)
2. Set up a billing account when prompted. **You will not be charged unless you explicitly upgrade**, even if you go over the free credits — GCP pauses your services instead.
3. Once in the console (https://console.cloud.google.com), click the project dropdown at the top → "New Project."
4. Name it `arxivlens-dev`. Note the **Project ID** that gets generated (looks like `arxivlens-dev-123456`). You'll use this everywhere.

## 2. Install the gcloud CLI

```bash
# macOS
brew install --cask google-cloud-sdk

# Linux
curl https://sdk.cloud.google.com | bash
exec -l $SHELL

# Windows: download installer from https://cloud.google.com/sdk/docs/install
```

Then authenticate:

```bash
gcloud auth login
gcloud auth application-default login   # for Terraform
gcloud config set project YOUR_PROJECT_ID
```

## 3. Set a hard budget alert (do this first, before anything else)

This is the single most important step for cost control.

1. Console → Billing → Budgets & alerts → Create Budget.
2. Name: `arxivlens-budget`. Amount: **$50**.
3. Alert thresholds: 50%, 75%, 90%, 100% of budget.
4. **Check "Send email alerts"** to your address.
5. (Optional but recommended) Set up a Pub/Sub-triggered Cloud Function that *automatically caps your spending* by disabling billing on the project at 100%. Tutorial: https://cloud.google.com/billing/docs/how-to/notify

If you skip this step and a bug causes a runaway loop, you can lose hundreds of dollars in hours. Don't skip it.

## 4. Enable the APIs we need

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  sqladmin.googleapis.com \
  pubsub.googleapis.com \
  storage.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  monitoring.googleapis.com \
  cloudtrace.googleapis.com \
  logging.googleapis.com \
  servicenetworking.googleapis.com
```

This takes 1–2 minutes.

## 5. Get your API keys

You need two:

### Google AI Studio (for Gemini — FREE)

1. Go to https://aistudio.google.com/apikey
2. Click "Create API key" → use your `arxivlens-dev` project.
3. Copy the key. We'll store it in Secret Manager, not in your code.

**Why we use AI Studio's API instead of Vertex AI's Gemini for now:** AI Studio has a generous free tier (1500 req/day for Gemini 2.5 Flash). Vertex AI bills per request from token #1.

### Anthropic (optional, for ablations)

1. https://console.anthropic.com/ → API Keys → Create Key.
2. Add ~$5 of credit. We use Claude only for the ablation comparison and as the LLM judge.

## 6. Store secrets in Secret Manager

```bash
# Set your project ID as an env var so you don't have to retype it
export PROJECT_ID=$(gcloud config get-value project)

# Store the Gemini key
echo -n "YOUR_GEMINI_KEY" | gcloud secrets create gemini-api-key \
  --data-file=- --project=$PROJECT_ID

# Store the Anthropic key (optional)
echo -n "YOUR_ANTHROPIC_KEY" | gcloud secrets create anthropic-api-key \
  --data-file=- --project=$PROJECT_ID

# Verify
gcloud secrets list
```

## 7. Set up local environment

```bash
# Clone your repo, then:
cd arxivlens
cp .env.example .env

# Edit .env to set:
# PROJECT_ID=your-project-id
# REGION=us-central1
# (Optional) Add API keys here for local dev only — never commit this file
```

## 8. The cost-control tools you must understand

GCP has three primitives that control your bill. Memorize these:

| Primitive | What it does | Where to set it |
|---|---|---|
| **Min instances = 0** | Cloud Run service scales to zero when idle. You pay $0 when no one is using it. | Terraform: `min_instance_count = 0` |
| **Stop instance** | Cloud SQL has a "stop" button. Pays only for storage (~$1/mo) instead of compute (~$10/mo). | Console → SQL → instance → Stop, or `gcloud sql instances patch` |
| **Lifecycle policies** | Cloud Storage auto-moves old objects to cheaper tiers | Terraform: `lifecycle_rule` |

**Rule of thumb:** at the end of every dev session, run `make pause`. This script (created later) stops your SQL instance and confirms Cloud Run is at zero. Without this, you'll bleed ~$0.50/day even when not using the system.

## 9. Verify your setup

```bash
# Should print your project ID
gcloud config get-value project

# Should list the APIs you enabled
gcloud services list --enabled | head -20

# Should list your secrets
gcloud secrets list

# Should print "Active"
gcloud auth list
```

If all four commands work, you're ready to run `terraform apply` in the next step.

## Common gotchas (read these or you'll hit them)

1. **"Permission denied" from Terraform**: you need `roles/owner` or a long list of granular roles. For a personal project, give your account Owner: Console → IAM → your email → Edit → add "Owner."
2. **"Quota exceeded" on Cloud Run / Cloud SQL**: new GCP accounts have low quotas. If you hit one, request an increase in Console → IAM → Quotas. Usually approved in minutes.
3. **Cloud SQL takes 5–10 minutes to provision** the first time. This is normal. Subsequent stops/starts take ~1 minute.
4. **Vertex AI text embeddings**: even though we use them, they're billed *per request*. Our 5k-paper ingest costs ~$3. Watch the dashboard if you scale up.
5. **Egress costs**: pulling data *out* of GCP costs more than pulling it in. Keep all storage in one region (`us-central1`).
6. **Don't run `terraform destroy` casually** — it deletes your Cloud SQL data. Use `make pause` instead for daily on/off.

## What to read next

- `infra/terraform/README.md` — what each Terraform module provisions
- `docs/DESIGN.md` — the system design doc
- `docs/RUNBOOK.md` — operating the system day-to-day
