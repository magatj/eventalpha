"""Application configuration loaded from environment variables (never hardcoded)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DataMode = Literal["demo", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "EventAlpha"
    api_version: str = "1.0.0"
    data_mode: DataMode = "demo"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://eventalpha:eventalpha_local_dev@localhost:5432/eventalpha"
    # On AWS, ECS injects these separately (password from the RDS-managed Secrets Manager
    # secret) and DATABASE_URL is assembled here, so the password never sits in a task definition.
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "eventalpha"
    db_user: str = ""
    db_password: str = ""
    db_sslmode: str = "require"

    # Massive (formerly Polygon.io)
    massive_api_key: str = ""
    massive_base_url: str = "https://api.massive.com"
    massive_requests_per_minute: int = 5
    massive_timeout_seconds: float = 20.0

    # SEC EDGAR
    sec_user_agent: str = "EventAlpha personal-research contact@example.com"
    sec_requests_per_second: float = 5.0
    sec_timeout_seconds: float = 30.0

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_input_price_per_m: float = 0.25
    openai_output_price_per_m: float = 2.00
    ai_monthly_budget_usd: float = 10.0
    ai_cache_ttl_hours: int = 24
    ai_max_sources: int = 20

    # Forecasting assumptions
    risk_free_rate: float = 0.0425
    equity_risk_premium: float = 0.05
    pre_tax_cost_of_debt: float = 0.055
    tax_rate: float = 0.21

    # Discovery
    discovery_top_n: int = 20
    discovery_max_candidates: int = 60
    discovery_ai_top_n: int = 10
    min_avg_dollar_volume: float = 20_000_000.0

    # Scheduler / worker
    scheduler_enabled: bool = True
    news_ingest_interval_minutes: int = 60
    embedded_worker: bool = False
    worker_poll_seconds: float = 2.0

    # Analysis cache freshness
    analysis_cache_ttl_hours: int = 12

    rss_feeds: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://www.sec.gov/news/pressreleases.rss",
            "https://www.federalreserve.gov/feeds/press_all.xml",
            "https://www.bls.gov/feed/bls_latest.rss",
        ]
    )

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    @field_validator("cors_origins", "rss_feeds", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _assemble_database_url(self) -> Settings:
        if self.db_host and self.db_user:
            self.database_url = (
                f"postgresql+psycopg://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode={self.db_sslmode}"
            )
        return self

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def massive_configured(self) -> bool:
        return bool(self.massive_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
