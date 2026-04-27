#!/usr/bin/env bash
# Run init.sql against the Cloud SQL instance via Cloud SQL Proxy or direct.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
ENV="${ENV:-dev}"
NAME="arxivlens-$ENV"

# Get the DB URL from Secret Manager
DB_URL=$(gcloud secrets versions access latest \
  --secret="${NAME}-db-url" \
  --project="$PROJECT_ID")

echo "==> Initializing schema..."
psql "$DB_URL" -f infra/sql/init.sql
echo "✅ Schema created."

echo ""
echo "==> Verifying pgvector extension..."
psql "$DB_URL" -c "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm');"
