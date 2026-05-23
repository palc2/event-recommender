from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    nimble_api_key: str = ""
    nimble_base_url: str = "https://sdk.nimbleway.com/v1"

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8443
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "event_scheduler"
    clickhouse_secure: bool = True

    llm_api_key: str = ""
    llm_base_url: str = "https://opencode.ai/zen/v1"
    llm_parse_model: str = "deepseek-v4-flash-free"
    llm_rerank_model: str = "deepseek-v4-flash-free"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""

    feedback_api_base_url: str = "http://localhost:8000"

    ingest_batch_size: int = 50
    parser_batch_size: int = 10
    recommender_top_k: int = 20
    recommendation_threshold: float = 0.3


settings = Settings()
