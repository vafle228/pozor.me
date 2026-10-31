"""Command-line entry point. Network-dependent polling is explicitly opt-in."""

import asyncio
import uuid
from pathlib import Path

import typer
from pydantic import ValidationError

from pozor.config import Settings
from pozor.context import ContextPack, Evidence
from pozor.doctor import run_checks
from pozor.importing import ImportErrorSafe, apply_import, preview_import, rollback_import
from pozor.ingestion import ChatMigrationConflict, PollingConflictError
from pozor.llm import Generator, InferenceError, LlamaClient
from pozor.logging import configure_logging
from pozor.runtime import run_telegram
from pozor.storage import MigrationError, migrate

app = typer.Typer(help="Локальный Telegram-бот pozor.me", no_args_is_help=True)
db_app = typer.Typer(help="Локальная база данных")
app.add_typer(db_app, name="db")
import_app = typer.Typer(help="Локальный preview и применение импорта")
app.add_typer(import_app, name="import")
llm_app = typer.Typer(help="Opt-in диагностика локального llama.cpp")
app.add_typer(llm_app, name="llm")


@llm_app.command("smoke")
def llm_smoke() -> None:
    """Check local server/model, tokenizer and non-thinking structured completion.

    Uses synthetic input only. Does not download weights or contact Telegram.
    """
    async def smoke(settings: Settings) -> None:
        deadline = asyncio.get_running_loop().time() + settings.generation_deadline_seconds
        async with LlamaClient(settings) as client:
            await client.check(deadline)
            await client.wait_idle(deadline)
            pack = ContextPack(
                chat_id=-1, thread_id=0, target_id=1, target_label="Synthetic",
                trigger=Evidence(evidence_id="trigger:1", chat_id=-1, allowed_target_id=1,
                                 source_namespace="request", relation="trigger",
                                 attribution="caller_hint", text="Обещаю выиграть в игре"),
            )
            result = await Generator(client).generate(pack, deadline)
            if result.fallback:
                raise InferenceError(result.reason)
            typer.echo(f"OK structured non-thinking smoke; prompt tokens={result.token_count}")
            typer.echo("GPU offload, hidden thinking and latency still require operator verification.")
    try:
        settings = Settings()
        configure_logging(settings)
        asyncio.run(smoke(settings))
    except (ValidationError, InferenceError, TimeoutError):
        typer.echo("FAIL llm smoke: check local server, model alias, template and deadline", err=True)
        raise typer.Exit(code=1) from None


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


@app.command()
def run() -> None:
    """Run the Telegram long poller and durable inbox processor."""
    try:
        settings = Settings()
    except ValidationError:
        typer.echo("FAIL config: invalid values in environment or .env", err=True)
        raise typer.Exit(code=1) from None
    configure_logging(settings)
    if settings.bot_token is None:
        typer.echo("FAIL bot_token: BOT_TOKEN is not configured", err=True)
        raise typer.Exit(code=1)
    try:
        asyncio.run(run_telegram(settings))
    except (PollingConflictError, ChatMigrationConflict) as exc:
        typer.echo(f"FAIL telegram polling: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, MigrationError) as exc:
        typer.echo(f"FAIL startup: {exc}", err=True)
        raise typer.Exit(code=1) from None


@import_app.command("preview")
def import_preview(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, readable=True),  # noqa: B008
    format: str = typer.Option(..., "--format"),
    chat: int = typer.Option(..., "--chat"),
    target: int = typer.Option(..., "--target"),
    manifest: Path | None = typer.Option(None, "--manifest"),  # noqa: B008
    source_chat: int | None = typer.Option(None, "--source-chat"),
    from_date: str | None = typer.Option(None, "--from"),
    until_date: str | None = typer.Option(None, "--until"),
    timezone: str = typer.Option("UTC", "--timezone"),
    message_ids: str | None = typer.Option(None, "--message-ids"),
    neighbors: int = typer.Option(0, "--neighbors", min=0, max=1),
    operator_id: int | None = typer.Option(None, "--operator-id"),
) -> None:
    """Validate a local Telegram Desktop export or dossier and write a preview manifest."""
    settings = Settings()
    if manifest is None:
        manifest = settings.database_path.parent / "staging" / f"preview-{uuid.uuid4().hex}.json"
    selected: set[int] | None = None
    if message_ids is not None:
        try:
            selected = {int(value.strip()) for value in message_ids.split(",")}
        except ValueError:
            typer.echo("FAIL import preview: --message-ids must be comma-separated integers", err=True)
            raise typer.Exit(code=1) from None
        if not selected or any(value <= 0 for value in selected):
            typer.echo("FAIL import preview: --message-ids must contain positive IDs", err=True)
            raise typer.Exit(code=1)
    try:
        result = preview_import(
            settings.database_path, file_path=file, format=format, chat_id=chat,
            target_id=target, manifest_path=manifest, source_chat_id=source_chat,
            from_date=from_date, until_date=until_date, timezone=timezone,
            selected_ids=selected, neighbors=neighbors, operator_id=operator_id,
        )
    except (ImportErrorSafe, OSError) as exc:
        typer.echo(f"FAIL import preview: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"OK preview {result.batch_id}: {result.manifest_path}")
    typer.echo(f"Counts: {result.counts}")
    typer.echo("Warning: imported evidence is operator-confirmed, not cryptographically verified.")
    for example in result.examples:
        text = example.get("text", "")
        typer.echo(f"Example {example.get('message_id', example.get('id'))}: {str(text)[:160]}")


@import_app.command("apply")
def import_apply(manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False)) -> None:  # noqa: B008
    """Revalidate and atomically activate one staged import."""
    settings = Settings()
    try:
        batch_id, count = apply_import(settings.database_path, manifest)
    except (ImportErrorSafe, OSError) as exc:
        typer.echo(f"FAIL import apply: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"OK applied {batch_id}: {count} records")


@import_app.command("rollback")
def import_rollback(batch: str = typer.Option(..., "--batch")) -> None:
    """Remove one import batch while preserving evidence shared by other batches."""
    settings = Settings()
    try:
        removed = rollback_import(settings.database_path, batch)
    except (ImportErrorSafe, OSError) as exc:
        typer.echo(f"FAIL import rollback: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"OK rolled back {batch}: {removed} messages removed")


if __name__ == "__main__":
    app()
