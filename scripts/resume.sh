#!/usr/bin/env bash
# Brings Cloud SQL back up. Cloud Run will warm itself when first request hits.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
ENV="${ENV:-dev}"
NAME="arxivlens-$ENV"

DB_NAME="${NAME}-pg"
echo "==> Starting Cloud SQL: $DB_NAME (this takes ~1 min)"
gcloud sql instances patch "$DB_NAME" \
  --activation-policy=ALWAYS \
  --project="$PROJECT_ID" \
  --quiet

# Wait for it to be RUNNABLE
for i in {1..30}; do
  state=$(gcloud sql instances describe "$DB_NAME" \
    --project="$PROJECT_ID" --format='value(state)' 2>/dev/null || echo "PENDING")
  if [[ "$state" == "RUNNABLE" ]]; then
    echo "✅ Database online."
    break
  fi
  echo "  ($state — waiting 10s)"
  sleep 10
done

echo ""
echo "✅ Resumed. Cloud Run will warm on first request."
