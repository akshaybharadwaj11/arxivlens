#!/usr/bin/env bash
# One-time GCP setup: enables APIs, creates the budget alert.
# Run AFTER `gcloud auth login` and `gcloud config set project YOUR_PROJECT`.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
if [[ -z "$PROJECT_ID" ]]; then
  echo "❌ No project set. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

echo "==> Project: $PROJECT_ID"
echo "==> Enabling required APIs (this takes 1-2 min)..."
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
  servicenetworking.googleapis.com \
  --project="$PROJECT_ID"

echo "==> APIs enabled."
echo ""
echo "⚠️  IMPORTANT: Set up your budget alert manually:"
echo "    https://console.cloud.google.com/billing/budgets?project=$PROJECT_ID"
echo "    Recommended: \$50 budget, alerts at 50/75/90/100%"
echo ""
echo "==> Next: store your API keys in Secret Manager"
echo "    echo -n 'YOUR_GEMINI_KEY' | gcloud secrets create gemini-api-key --data-file=-"
echo ""
echo "==> Then run: make tf-init && make tf-apply"
