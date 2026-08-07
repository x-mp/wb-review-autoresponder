from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_access_token: str = Field(default="change-me-to-at-least-32-random-characters")
    database_url: str = "postgresql://wb_assistant:change-me@localhost:5432/wb_assistant"
    artifact_root: Path = Path("data/raw")
    wb_api_token: str = ""
    wb_api_base: str = "https://feedbacks-api.wildberries.ru"
    wb_content_api_base: str = "https://content-api.wildberries.ru"
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-5-mini"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "WB Review Assistant"
    llm_mode: str = "mock"
    auto_publish: bool = False
    sync_interval_seconds: int = 300
    log_level: str = "INFO"

    @field_validator("app_access_token")
    @classmethod
    def validate_access_token(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("APP_ACCESS_TOKEN must contain at least 32 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
