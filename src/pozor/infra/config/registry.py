"""Validated application settings; secret values are excluded from representations."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pozor.infra.config.paths import config_home


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    def __init__(self, **values: Any) -> None:
        values.setdefault("_env_file", config_home() / ".env")
        super().__init__(**values)
        if not self.pozor_data_dir.is_absolute():
            object.__setattr__(self, "pozor_data_dir", config_home() / self.pozor_data_dir)
        if 'database_path' not in self.model_fields_set:
            object.__setattr__(self, "database_path", self.pozor_data_dir / 'pozor.sqlite3')
        if not self.database_path.is_absolute():
            object.__setattr__(self, "database_path", config_home() / self.database_path)

    @classmethod
    def settings_customise_sources(cls, settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource, env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource, file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, dotenv_settings, file_secret_settings,
                TomlConfigSettingsSource(settings_cls, config_home() / "pozor.toml"))

    pozor_data_dir: Path = Field(default=Path('./data'),description='Каталог SQLite, секретов и сессий; на Windows ограничьте ACL')
    admin_password: SecretStr | None = Field(default=None, repr=False,description='Пароль входа в админку, минимум 16 символов; значение не показывается')
    admin_host: str = "127.0.0.1"
    admin_port: int = Field(default=8090, ge=0, le=65535)
    admin_secure_cookie: bool = False
    admin_login_attempts: int = Field(default=5, ge=1, le=100)
    admin_login_window_seconds: int = Field(default=600, ge=1, le=86400)
    humor_v2_enabled: bool = True
    roast_level: int = Field(default=2, ge=1, le=3)
    target_daily_round_limit: int = Field(default=12, ge=1, le=100)
    temperature: float = Field(default=.9, ge=0, le=2)
    top_p: float = Field(default=.95, gt=0, le=1)
    min_p: float = Field(default=.05, ge=0, le=1)
    presence_penalty: float = Field(default=.4, ge=-2, le=2)
    hook_temperature: float = Field(default=.3, ge=0, le=2)
    retrieval_min_terms: int = Field(default=2, ge=1, le=12)
    recent_token_budget: int = Field(default=900, ge=0, le=8192)
    session_token_budget: int = Field(default=600, ge=0, le=8192)
    conversation_token_budget: int = Field(default=800, ge=0, le=8192)
    dossier_token_budget: int = Field(default=900, ge=0, le=8192)
    token_reserve: int = Field(default=512, ge=0, le=8192)
    queue_capacity: int = Field(default=8, ge=1, le=128)
    background_queue_capacity: int = Field(default=16, ge=1, le=128)
    background_timeout_seconds: float = Field(default=60, gt=0, le=300)
    prompt_version: Literal["v2"] = "v2"
    bot_token: SecretStr | None = Field(default=None, repr=False,description='Токен Telegram-бота от BotFather; обязателен для serve')
    owner_ids: list[int] = Field(default_factory=list)
    database_path: Path = Path("./data/pozor.sqlite3")
    llm_base_url: str = "http://127.0.0.1:8080"
    llm_model: str = "pozor-local"
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    context_size: int = Field(default=8192, ge=512, le=131072)
    max_output_tokens: int = Field(default=600, ge=1, le=4096)
    generation_deadline_seconds: float = Field(default=30, gt=0, le=300)
    historical_token_budget: int = Field(default=1200, ge=0, le=8192)
    per_topic_limit: int = Field(default=2, ge=1, le=40)
    retrieval_weights: tuple[float, float, float, float] = (1.0, .8, .5, .6)
    live_retention_days: int = Field(default=30, ge=1, le=3650)
    import_retention_days: int = Field(default=90, ge=1, le=3650)
    profile_cards_enabled: bool = False
    semantic_profiles_enabled: bool = True
    telegram_api_id: int | None = Field(default=None, gt=0)
    telegram_api_hash: SecretStr | None = Field(default=None, repr=False)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def empty_telegram_id(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("retrieval_weights")
    @classmethod
    def valid_weights(cls, value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        import math

        if any(not math.isfinite(weight) or weight < 0 for weight in value):
            raise ValueError("retrieval weights must be finite and nonnegative")
        return value

    @field_validator("owner_ids")
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


BOOTSTRAP = {"pozor_data_dir", "database_path", "llm_base_url", "llm_model", "admin_host", "admin_port", "log_level", "owner_ids", "admin_secure_cookie"}
SECRETS = {"bot_token", "admin_password", "llm_api_key"}
CREDENTIALS = {"telegram_api_id", "telegram_api_hash"}


def owner(name: str) -> str:
    if name in BOOTSTRAP:
        return "bootstrap"
    if name in SECRETS:
        return "secret"
    if name in CREDENTIALS:
        return "credentials"
    return "runtime"


def registry() -> list[dict[str, Any]]:
    return [{"name": name, "owner": owner(name), "restart_required": owner(name) != "runtime" and name!='admin_password',
             "description": field.description or name.replace("_", " "),
             "schema": Settings.model_json_schema()["properties"][name]}
            for name, field in Settings.model_fields.items()]
