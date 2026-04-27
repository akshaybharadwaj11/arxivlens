# ArXivLens Makefile
# Run `make help` to see targets.

PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION     ?= us-central1
ENV        ?= dev
NAME       := arxivlens-$(ENV)
REGISTRY   := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(NAME)-images

.PHONY: help
help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ----- One-time setup -----------------------------------------------------------
.PHONY: setup
setup: ## One-time: enable APIs, set budget alert (run after gcloud auth login)
	./scripts/00-gcp-setup.sh

.PHONY: tf-init
tf-init: ## Initialize Terraform
	cd infra/terraform/envs/dev && terraform init

.PHONY: tf-apply
tf-apply: ## Apply Terraform (provision GCP resources)
	cd infra/terraform/envs/dev && terraform apply

.PHONY: tf-destroy
tf-destroy: ## Tear down all infra (CAREFUL — deletes data)
	cd infra/terraform/envs/dev && terraform destroy

.PHONY: db-init
db-init: ## Run init.sql against the Cloud SQL instance
	./scripts/db-init.sh

# ----- Cost control -------------------------------------------------------------
.PHONY: pause
pause: ## Stop Cloud SQL + scale Cloud Run to 0. Run at end of dev session.
	./scripts/pause.sh

.PHONY: resume
resume: ## Start Cloud SQL + bring services back up.
	./scripts/resume.sh

.PHONY: cost
cost: ## Show month-to-date GCP spend
	./scripts/cost.sh

# ----- Build + deploy -----------------------------------------------------------
.PHONY: build-api
build-api: ## Build + push API image
	gcloud builds submit --tag $(REGISTRY)/api:latest -f Dockerfile.api .

.PHONY: build-worker
build-worker: ## Build + push worker image (parser/embedder)
	gcloud builds submit --tag $(REGISTRY)/worker:latest -f Dockerfile.worker .

.PHONY: deploy-api
deploy-api: build-api ## Deploy API to Cloud Run
	./scripts/deploy-api.sh

# ----- Pipeline runs ------------------------------------------------------------
.PHONY: ingest-smoke
ingest-smoke: ## Crawl + parse 100 papers (smoke test, ~10 min)
	python -m ingestion.crawler --max-papers 100

.PHONY: ingest-5k
ingest-5k: ## Crawl 5000 papers — the v1 corpus
	python -m ingestion.crawler --max-papers 5000

.PHONY: parse-local
parse-local: ## Run parser locally on staged papers
	python -m ingestion.parser --subscribe

.PHONY: embed-local
embed-local: ## Embed staged papers locally
	python -m embedding.embedder --backfill

# ----- Local dev ---------------------------------------------------------------
.PHONY: api-local
api-local: ## Run API locally on port 8000
	uvicorn generation.app:app --reload --port 8000

.PHONY: test
test: ## Run tests
	pytest -xvs

.PHONY: lint
lint: ## Lint with ruff
	ruff check .
	ruff format --check .

.PHONY: fmt
fmt: ## Format with ruff
	ruff format .

# ----- Eval --------------------------------------------------------------------
.PHONY: eval
eval: ## Run full eval against golden set
	python -m eval.run_eval --golden eval/golden_set_v1.jsonl --out eval/results.json

.PHONY: eval-gate
eval-gate: ## Compare current eval vs baseline
	python -m eval.gate --current eval/results.json --baseline eval/baseline.json
