terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "env" {
  type    = string
  default = "dev"
}

locals {
  name_prefix = "arxivlens-${var.env}"
  labels = {
    project = "arxivlens"
    env     = var.env
    managed = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Cloud Storage — raw PDFs, parsed content, eval artifacts
# -----------------------------------------------------------------------------
resource "google_storage_bucket" "raw" {
  name                        = "${local.name_prefix}-raw-${var.project_id}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # so `terraform destroy` works in dev

  lifecycle_rule {
    condition { age = 30 }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
  labels = local.labels
}

resource "google_storage_bucket" "parsed" {
  name                        = "${local.name_prefix}-parsed-${var.project_id}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels
}

resource "google_storage_bucket" "eval" {
  name                        = "${local.name_prefix}-eval-${var.project_id}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels
}

# -----------------------------------------------------------------------------
# Pub/Sub — async pipeline backbone
# -----------------------------------------------------------------------------
resource "google_pubsub_topic" "papers_to_parse" {
  name   = "${local.name_prefix}-papers-to-parse"
  labels = local.labels
}

resource "google_pubsub_topic" "papers_to_embed" {
  name   = "${local.name_prefix}-papers-to-embed"
  labels = local.labels
}

resource "google_pubsub_subscription" "parse_sub" {
  name  = "${local.name_prefix}-parse-sub"
  topic = google_pubsub_topic.papers_to_parse.name

  ack_deadline_seconds       = 600
  message_retention_duration = "604800s" # 7 days

  retry_policy {
    minimum_backoff = "60s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription" "embed_sub" {
  name  = "${local.name_prefix}-embed-sub"
  topic = google_pubsub_topic.papers_to_embed.name

  ack_deadline_seconds       = 300
  message_retention_duration = "604800s"
}

# -----------------------------------------------------------------------------
# Cloud SQL Postgres — chunks, metadata, pgvector, BM25
# -----------------------------------------------------------------------------
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "main" {
  name             = "${local.name_prefix}-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = "db-f1-micro" # ~$10/mo running, ~$1/mo stopped
    activation_policy = "ALWAYS"
    disk_size         = 20
    disk_autoresize   = true

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    backup_configuration {
      enabled    = true
      start_time = "02:00"
    }

    ip_configuration {
      ipv4_enabled = true
      authorized_networks {
        name  = "all"
        value = "0.0.0.0/0" # dev only — tighten for prod
      }
    }

    user_labels = local.labels
  }

  deletion_protection = false # dev only
}

resource "google_sql_database" "main" {
  name     = "arxivlens"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "app"
  instance = google_sql_database_instance.main.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "db_url" {
  secret_id = "${local.name_prefix}-db-url"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = "postgresql://app:${random_password.db.result}@${google_sql_database_instance.main.public_ip_address}:5432/arxivlens"
}

# -----------------------------------------------------------------------------
# Artifact Registry — for our container images
# -----------------------------------------------------------------------------
resource "google_artifact_registry_repository" "main" {
  location      = var.region
  repository_id = "${local.name_prefix}-images"
  format        = "DOCKER"
  labels        = local.labels
}

# -----------------------------------------------------------------------------
# Service account — used by all Cloud Run services and jobs
# -----------------------------------------------------------------------------
resource "google_service_account" "app" {
  account_id   = "${local.name_prefix}-sa"
  display_name = "ArXivLens app service account"
}

# Grant the SA the permissions it needs
resource "google_project_iam_member" "sa_roles" {
  for_each = toset([
    "roles/storage.objectAdmin",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/cloudsql.client",
    "roles/secretmanager.secretAccessor",
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app.email}"
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------
output "project_id"          { value = var.project_id }
output "region"              { value = var.region }
output "raw_bucket"          { value = google_storage_bucket.raw.name }
output "parsed_bucket"       { value = google_storage_bucket.parsed.name }
output "eval_bucket"         { value = google_storage_bucket.eval.name }
output "parse_topic"         { value = google_pubsub_topic.papers_to_parse.name }
output "embed_topic"         { value = google_pubsub_topic.papers_to_embed.name }
output "db_instance"         { value = google_sql_database_instance.main.name }
output "db_connection_name"  { value = google_sql_database_instance.main.connection_name }
output "db_public_ip"        { value = google_sql_database_instance.main.public_ip_address }
output "db_url_secret"       { value = google_secret_manager_secret.db_url.secret_id }
output "artifact_registry"   { value = google_artifact_registry_repository.main.name }
output "service_account"     { value = google_service_account.app.email }
output "registry_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.main.repository_id}"
}
