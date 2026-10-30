"""Command-line entry point. Network-dependent commands are opt-in and future work."""

import typer
from pydantic import ValidationError

from pozor.config import Settings
from pozor.doctor import run_checks
from pozor.logging import configure_logging

app = typer.Typer(help="Локальный Telegram-бот pozor.me", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Inspect configuration and local runtime prerequisites."""


@app.command()
def doctor() -> None:
    """Check configuration, SQLite FTS5, and the local data directory."""
    try:
        settings = Settings()
    except ValidationError:
        typer.echo("FAIL config: invalid values in environment or .env", err=True)
        raise typer.Exit(code=1) from None
    configure_logging(settings)
    checks = run_checks(settings)
    for check in checks:
        typer.echo(f"{'OK' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if not all(check.ok for check in checks):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
