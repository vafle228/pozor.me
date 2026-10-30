"""Logging setup that redacts configured credentials from messages and tracebacks."""

import logging

from pozor.config import Settings


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


def configure_logging(settings: Settings) -> None:
    """Configure one console handler and redact bot/API credentials."""
    secrets = []
    if settings.bot_token:
        secrets.append(settings.bot_token.get_secret_value())
    if settings.llm_api_key:
        secrets.append(settings.llm_api_key.get_secret_value())

    handler = logging.StreamHandler()
    handler.setFormatter(
        SecretRedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            secrets,
        )
    )
    handler.addFilter(SecretRedactionFilter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
