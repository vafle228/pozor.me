# Pozor.me

- New domain logic, humor policies and use cases belong in `src/pozor/core`. Core must not import infrastructure, presentation, application, Telegram/HTTP/SQLite clients or environment configuration. Import-linter enforces this.
- SQL executes only under `src/pozor/infra/sqlite`; `tests/test_architecture.py` enforces this. Message retention and entitlement must use `v_entitled_messages`.
- Declare settings in the typed registry `infra/config/registry.py`; generate `.env.example` with `pozor config template`. Do not add independent environment readers.
- The application container owns the inference queue. All interactive and background model work, including admin imports, uses that queue. Keep the durable send guard immediately before Telegram delivery.
- Preserve opt-out, forgetting, source hashes, scope, cancellation, outbox ambiguity and migration checksums when changing workflows. Keep compatibility facades small.
- Before completion run `uv run poe check`, `uv run pozor config template --check`, `uv run pozor deployment check --metadata-only`. Humor fixtures are synthetic contract tests; human acceptance requires 40+ blind pairs and at least 60% wins.
- Never commit secrets, real chat histories, staging files, sessions, database dumps or model weights. Live Telegram publication requires explicit instruction.
