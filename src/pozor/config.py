"""Validated application settings; secret values are excluded from representations."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    bot_token: SecretStr | None = Field(default=None, repr=False)
    owner_ids: list[int] = Field(default_factory=list)
    chat_allowlist: list[int] = Field(default_factory=list)
    database_path: Path = Path("./data/pozor.sqlite3")
    llm_base_url: str = "http://127.0.0.1:8080"
    llm_model: str = "pozor-local"
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    context_size: int = Field(default=8192, ge=512, le=131072)
    max_output_tokens: int = Field(default=180, ge=1, le=4096)
    generation_deadline_seconds: float = Field(default=8, gt=0, le=300)
    live_retention_days: int = Field(default=30, ge=1, le=3650)
    import_retention_days: int = Field(default=90, ge=1, le=3650)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("owner_ids", "chat_allowlist")
    @classmethod
    def ids_must_be_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("IDs must not contain duplicates")
        return value

    @field_validator("bot_token")
    @classmethod
    def ignore_example_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().strip().lower() in {"", "replace_me"}:
            return None
        return value
