"""Command-line entry point. Network-dependent commands are opt-in and future work."""

import typer
from pydantic import ValidationError

from pozor.config import Settings
from pozor.doctor import run_checks
from pozor.logging import configure_logging
from pozor.storage import MigrationError, migrate

app = typer.Typer(help="Локальный Telegram-бот pozor.me", no_args_is_help=True)
db_app = typer.Typer(help="Локальная база данных")
app.add_typer(db_app, name="db")


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


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending local SQLite schema migrations."""
    try:
        settings = Settings()
        applied = migrate(settings.database_path)
    except ValidationError:
        typer.echo("FAIL config: invalid values in environment or .env", err=True)
        raise typer.Exit(code=1) from None
    except (MigrationError, OSError) as exc:
        typer.echo(f"FAIL db migrate: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if applied:
        for name in applied:
            typer.echo(f"OK applied {name}")
    else:
        typer.echo("OK database schema is current")


if __name__ == "__main__":
    app()
