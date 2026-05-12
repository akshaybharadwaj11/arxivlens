"""Configuration loaded from environment variables.

In Cloud Run, secrets like DB_URL and GEMINI_API_KEY are injected as env vars
from Secret Manager. Locally, they come from a .env file.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP
    project_id: str = Field(..., alias="PROJECT_ID")
    region: str = Field(default="us-central1", alias="REGION")
    env: str = Field(default="dev", alias="ENV")

    # Storage
    raw_bucket: str = Field(default="", alias="RAW_BUCKET")
    parsed_bucket: str = Field(default="", alias="PARSED_BUCKET")
    eval_bucket: str = Field(default="", alias="EVAL_BUCKET")

    # Pub/Sub
    parse_topic: str = Field(default="", alias="PARSE_TOPIC")
    embed_topic: str = Field(default="", alias="EMBED_TOPIC")

    # Database
    db_url: str = Field(default="", alias="DB_URL")

    # API keys (loaded from Secret Manager in prod, .env locally)
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Models
    embedding_model: str = "text-embedding-005"
    generation_model: str = "gemini-2.5-flash"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    nli_model: str = "cross-encoder/nli-deberta-v3-base"

    # Retrieval
    top_k_dense: int = 30
    top_k_sparse: int = 30
    top_k_rerank: int = 5
    rrf_k: int = 60

    # Limits
    max_query_length: int = 1000
    max_chunks_per_paper: int = 100


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
