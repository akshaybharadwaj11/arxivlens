#!/usr/bin/env bash
# Stops Cloud SQL and scales Cloud Run services to 0 to minimize cost.
# Run this at the end of every dev session.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
ENV="${ENV:-dev}"
NAME="arxivlens-$ENV"

echo "==> Pausing $NAME resources in $PROJECT_ID..."

# 1. Stop Cloud SQL (~$10/mo running, ~$1/mo stopped)
DB_NAME="${NAME}-pg"
echo "==> Stopping Cloud SQL: $DB_NAME"
if gcloud sql instances describe "$DB_NAME" --project="$PROJECT_ID" &>/dev/null; then
  gcloud sql instances patch "$DB_NAME" \
    --activation-policy=NEVER \
    --project="$PROJECT_ID" \
    --quiet || echo "  (already stopped or not found)"
else
  echo "  (instance not found — skipping)"
fi

# 2. Cloud Run services already scale to 0 with min-instances=0,
#    but we explicitly set min-instances=0 just to be safe.
echo "==> Verifying Cloud Run services scale to 0..."
for service in api reranker; do
  if gcloud run services describe "${NAME}-${service}" \
       --region=us-central1 --project="$PROJECT_ID" &>/dev/null; then
    gcloud run services update "${NAME}-${service}" \
      --region=us-central1 \
      --min-instances=0 \
      --project="$PROJECT_ID" \
      --quiet || true
  fi
done

echo ""
echo "✅ Paused. Estimated idle cost: ~\$1.50/mo (storage only)."
echo "   To resume: make resume"
