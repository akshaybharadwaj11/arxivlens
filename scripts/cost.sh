#!/usr/bin/env bash
# Print month-to-date GCP spend.
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)

echo "==> Month-to-date spend for $PROJECT_ID"
echo ""
echo "Open the billing console:"
echo "  https://console.cloud.google.com/billing/reports?project=$PROJECT_ID"
echo ""
echo "Or query via BigQuery if billing export is set up."
echo "(Setting up billing export to BigQuery is in docs/COST_TRACKING.md)"
