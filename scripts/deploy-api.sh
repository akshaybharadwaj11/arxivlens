#!/usr/bin/env bash
# Deploy the API service to Cloud Run.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
REGION="${REGION:-us-central1}"
ENV="${ENV:-dev}"
NAME="arxivlens-$ENV"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME}-images"
SA="${NAME}-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Pull values from Terraform output (or hardcode if you prefer)
DB_URL_SECRET="${NAME}-db-url"

echo "==> Deploying ${NAME}-api to Cloud Run"

gcloud run deploy "${NAME}-api" \
  --image="${REGISTRY}/api:latest" \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="${SA}" \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=1 \
  --max-instances=3 \
  --timeout=300 \
  --concurrency=20 \
  --set-env-vars="PROJECT_ID=${PROJECT_ID},REGION=${REGION},ENV=${ENV}" \
  --set-env-vars="RAW_BUCKET=${NAME}-raw-${PROJECT_ID}" \
  --set-env-vars="PARSED_BUCKET=${NAME}-parsed-${PROJECT_ID}" \
  --set-env-vars="EVAL_BUCKET=${NAME}-eval-${PROJECT_ID}" \
  --set-env-vars="PARSE_TOPIC=${NAME}-papers-to-parse" \
  --set-env-vars="EMBED_TOPIC=${NAME}-papers-to-embed" \
  --add-cloudsql-instances=arxivlens-dev:us-central1:arxivlens-dev-pg \
  --set-secrets="DB_URL=${DB_URL_SECRET}:latest,GEMINI_API_KEY=gemini-api-key:latest" \
  --project="${PROJECT_ID}"

URL=$(gcloud run services describe "${NAME}-api" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url)')

echo ""
echo "✅ Deployed: $URL"
echo ""
echo "Smoke test:"
echo "  curl $URL/health"
echo "  curl -X POST $URL/retrieve -H 'Content-Type: application/json' \\"
echo "    -d '{\"query\":\"What is FlashAttention?\",\"top_k\":3}'"
