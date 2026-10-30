# Пакеты работ для кодовых моделей

## Общие правила

Каждый пакет — отдельная небольшая итерация, без самостоятельного redesign. Сначала прочитать README, соответствующий документ и reference tests. Не выдавать fake integration за готовый бот. Если нужно изменить контракт, описать конкретный конфликт и обновить документацию вместе с тестом. Не пересылать кодовой модели реальные переписки: использовать синтетические fixtures.

Dependency graph: **T0 → T1 → T2 → T3 → T4 → T5 → T6 → T7**. Некоторые чистые модули можно писать независимо, но интеграцию сдавать в этом порядке. T3 (импорт) — обязательная часть V1, не «после релиза». После T5 уже должен работать вертикальный сценарий: импорт → вызов → контекстный ответ → stop. T6 улучшает качество, T7 делает запуск воспроизводимым.

## T0 — каркас и конфигурация

Результат: pyproject/uv.lock, src/pozor, CLI, .env.example, settings, logging redaction, ruff/mypy/pytest, doctor. Точный Python baseline выбрать 3.12 (reference проверяется также локальным 3.13). Doctor проверяет FTS5, writable data directory, config; network checks отдельно флагом.

**T0 завершён (2026-10-31):** добавлены метаданные и `uv.lock`; пакет `src/pozor`; настройки из окружения/`.env` с маскировкой секретов; `.env.example`; CLI `--help` и `doctor`; проверки FTS5, каталога данных и наличия токена; redaction токенов в логах и traceback. `tests/test_application_basics.py` проверяет defaults/валидацию настроек, локальные проверки doctor и отсутствие секретов в выводе и traceback. Общий offline suite проходит: 31 тест; Ruff для приложения и новых тестов, mypy для пакета, чистая установка `uv sync --frozen --extra dev`, `pozor --help` и doctor с тестовой конфигурацией проверены. Реальные Telegram, сеть и GPU не требуются.

Приёмка: чистый install по lock, `pozor --help`, missing token понятен без печати token; unit suite работает offline. Не реализовывать Telegram, LLM или schema «на глаз» в этом пакете.

## T1 — БД и durable ingestion

**Готовая часть T1 — SQLite-схема и checksum-защищённые миграции (2026-10-31):** создана первая миграция для чатов, участников/aliases, inbox/checkpoint, нормализованных сообщений, связей subjects, data epochs и FTS5 с триггерами. Добавлена настройка SQLite-соединения (foreign keys, WAL, busy timeout) и команда `pozor db migrate`; повторный запуск идемпотентен, изменение уже применённого SQL отклоняется. Durable receiver, транзакционная обработка inbox, allowlist, edits и lifecycle обработки остаются открытыми частями T1.

**Готовая часть T1 — durable polling и обработка inbox (2026-10-31):** добавлен poller поверх Bot API `getUpdates` с durable offset и retry backoff. Батч update-ов и следующий offset сохраняются атомарно; затем inbox транзакционно превращается в сообщения. Повторный update идемпотентен, сбой обработки откатывает сообщение и marker, обработанные payload очищаются, неизвестные/выключенные чаты отбрасываются до записи текста. Live message сохраняет author/thread/date и content hash; edit обновляет строку и FTS в той же транзакции, затронутые subject epochs инвалидируются. Добавлены offline fake-source проверки duplicate, allowlist, edit/FTS, очистки payload и rollback. Проверено: полный pytest suite — 37 passed; Ruff на изменённых файлах и mypy пакета.

**Готовая часть T1 — нормализация live subject-связей (2026-10-31):** ingestion создаёт `self/observed_self` для автора, `mention/third_party` из Telegram `text_mention` и однозначно разрешённых username aliases на дату сообщения, `reply/third_party` только по автору родительского сообщения из той же live-группы. UTF-16 offsets Telegram учитываются при чтении username mention; неоднозначный или устаревший alias не разрешается. Edit пересобирает связи и повышает epochs для затронутых target-ов; FTS и subjects остаются в одной транзакции. Offline-тесты покрывают author, UTF-16 mention, прямой text mention, alias ambiguity, reply и удаление связи при edit. Проверено: полный pytest suite — 40 passed; Ruff для изменённых файлов и mypy пакета.

**T1 завершён (2026-10-31):** receiver подключён к opt-in `pozor run` на реальном aiogram Bot API client; до polling приложение применяет миграции и синхронизирует `CHAT_ALLOWLIST` в отдельный флаг `allowed`, не включая новые чаты (`enabled` управляется командами T2). Обработаны `migrate_to_chat_id` события: members/aliases, messages/FTS, epochs и chat policy переносятся транзакционно; пустой destination placeholder из обновлённого allowlist объединяется, заполненный scope отклоняется с операторской ошибкой. Добавлена очистка истёкших live-сообщений по chat retention и обработанных inbox payload старше 7 дней; очистка удаляет FTS-строки и повышает epochs. Webhook/второй poller conflict прекращает запуск с инструкцией, webhook автоматически не удаляется.

Проверено offline: полный pytest suite — 48 passed; Ruff для изменённых модулей и ingestion tests; mypy для `src/pozor`; CLI help и миграция на временной SQLite-БД. Fake-source проверки покрывают checkpoint до processing, restart drain, duplicate, allowlist, edit/FTS, rollback, group migration/collision/placeholder, cleanup, conflict и retry backoff. Реальный Telegram token, webhook и группа не использовались; поведение внешнего Bot API остаётся непроверенным.

Результат: миграции/репозитории по doc02, inbox/checkpoint transaction, message normalization, FTS triggers, receiver через aiogram client, allowlist, edit handling, restart behavior. Anonymous sender не привязывается к target. Chat migration group→supergroup требует атомарного переноса scope/aliases/consent и индексов либо явной блокировки с инструкцией миграции — не терять identity молча.

Приёмка: повтор update не дублирует message; crash before commit получает update заново; crash after commit не теряет processing; edit заменяет FTS и инвалидирует cards; сообщения чужого чата недоступны; receiver не ждёт GPU. Mock getUpdates проверяет offsets, webhook conflict и retry backoff. Старые сообщения сохраняются, но не исполняют команды.

## T2 — команды, ID и сессии

Результат: /enable_chat, /disable_chat, /help, /whois, /roast_on, /roast_off, /roast, /stop, /finish; парсер mention/reply/UTF16, local aliases; state machine, лимиты, scheduler и pure clock. Применить invariants reference, не копировать его как готовый session manager.

Приёмка: emoji перед @mention, rename/reused username, unknown user, target bot, anonymous admin, conflicting multiple targets; target не включил участие; другой участник нажал target-only кнопку; две темы; два одновременных start; стоп при pending; silence и max-turns; случайная фраза «стоп» в чужом тексте не завершает чужую сессию. Кнопки содержат session ID, проверяется actor ID; callback не даёт права сам по себе.

**T2 реализован (2026-10-31):** добавлены реальные команды aiogram/SQLite для allowlist-scoped включения чата (OWNER_IDS или проверенный `getChatMember` admin status), `/whois` по reply, добровольного opt-in/out, разрешения известных username aliases и старта/остановки сессий. Username alias интервалы обновляются при наблюдаемом rename; ambiguous/reused username не разрешается. Username, numeric `text_mention` и reply ID разрешаются только из Telegram entities/наблюдаемого локального scope; conflicting targets отклоняются. В сессиях enforced одна активная на `(chat, thread)`, consent, 10-минутный target cooldown, 30-секундный initiator cooldown, 4-turn limit, 5-минутный TTL и 90-секундная тишина; clock проверяется каждые 5 секунд, а процесс restart закрывает активные сессии и увеличивает revision. Inline stop/finish callback несёт session ID и повторно проверяет chat/thread/actor.

Архитектурное уточнение относительно T1: если оставлять inbox фильтр только по `enabled`, отключённый чат не сможет получить `/enable_chat`. Теперь в разрешённом `allowed` чате до включения сохраняются только Telegram bot-command events и callback events; обычные сообщения отбрасываются до записи в inbox. После обработки control-update inbox payload очищается. `enabled` остаётся отдельным операторским флагом, а consent — отдельным opt-in участника.

Проверено offline: полный suite — 66 passed; Ruff и mypy без ошибок; CLI help и применение миграций 0001–0003 к временной SQLite-БД. Fake Bot API проверяет admin lookup/отказ anonymous admin и buttons/callback actor guard. Реальный Telegram, admin status lookup и рассылка не запускались. `/roast` только создаёт bounded session и контрольное сообщение; генерация и ответы на продолжения остаются T4–T5.

## T3 — импорт первой версии

Результат: Desktop single-chat и chats.list adapter, dossier-v1 schema, даты/aliases, выбор диапазона/message IDs, preview manifests, atomic apply, idempotency, conflicts, rollback, staged expiry. Local CLI, ограничение file size 100 MiB, streaming для больших файлов; никакого чтения вложенных file paths/архивов/remote URLs. Неверный source mapping не применяется.

Приёмка: строковый/составной text; deleted/unknown author; replies на отсутствующего родителя; одинаковый message ID в двух source chats; username исторически принадлежал другому; forward; диапазон UTC; scalar/list malformed JSON; повторный import; изменённый после preview файл; rollback одного из двух batch; manual claim не становится self fact; imported `/roast` ничего не запускает. Показать реальный dry-run на синтетическом fixture и query, находящий импортированную цитату.

**T3 реализован (2026-10-31):** добавлены миграция `0004_imports`, Desktop single-chat и `chats.list` adapters, dossier-v1 validation, UTC/ISO date normalization, explicit source/destination IDs, approved alias interval/ambiguity filtering, optional message ID intersection and context neighbors, preview manifests с source/payload hashes и 24h expiry, atomic/idempotent apply, conflict rejection, FTS triggers и batch rollback с сохранением shared evidence. Ручные items хранятся отдельно как claims и не попадают в message FTS или live command router. CLI: `pozor import preview|apply|rollback`. Файлы до 8 MiB работают со stdlib; для больших файлов нужен optional `imports` extra с ijson; общий жёсткий предел — 100 MiB.

Проверено offline: полный suite — 77 passed; Ruff на новых/изменённых Python-модулях T3; mypy `src/pozor`; CLI dry-run/apply на синтетическом экспорте и FTS query для импортированной цитаты. Применение миграций 0001–0004 выполняется в acceptance fixtures. Секреты, Telegram, реальные чаты, рассылка и веса не использовались. На Windows IANA timezones кроме UTC требуют установленного `tzdata`; пакет не попал в lock, поскольку офлайн-резолвер не имел кэша зависимостей.

## T4 — локальный inference и retrieval

Результат: httpx adapter, health/model check, non-thinking smoke, token counting, bounded queue, RRF/context packer, prompt/schema, validation/fallback, model manifest. Без Telegram integration можно fake LLM. Runtime completion один, максимум один validation retry в общем deadline.

Приёмка: structured JSON, wrong IDs, hallucinated author, malformed result, timeout, context overflow, injection quoted as data, empty evidence, repetition, unrelated target/context isolation, cancellation; interactive при background занятости. Реальный smoke по желанию оператора, отдельно от CI. Замер отсутствует → нельзя писать «3 секунды» как факт.

## T5 — вертикальная интеграция и доставка

Результат: связывает start→retrieve→generate→validate→outbox→send; reply turns/coalescing, generation revision, evidence epoch, rate limits, stop race, restart recovery. Только теперь `/roast` реально отправляет LLM-панч.

Приёмка: fake pipeline с imported evidence; stale после stop/edit/forget/rollback не отправляется; direct reply продолжает, unrelated message нет; 429 повторяет после актуальности, ambiguous timeout не повторяет; crash после successful send до DB commit не вызывает слепой retry; max turns не превышается. Внешний HTTP уже in-flight при stop отмечается как ограничение, а не скрывается.

## T6 — портрет, удаление, оценка

Результат: quote-backed cards, background watermark, invalidation, TTL cleanup, `/forget_me`, `profile show`, eval suite/benchmark report. Карточки могут быть выключены без поломки direct retrieval.

Приёмка: third-party не повышается до self, 3 соседние реплики не «3 эпизода», отсутствующий source инвалидирует card, import TTL от даты импорта, FTS/inbox/outbox/staging очищаются, opt-out переживает reimport. Оценка 30 кейсов с evidence coverage и latency; качество юмора оценивает человек, не только сама LLM.

## T7 — эксплуатация и финальный smoke

Результат: проверенный runbook Windows, варианты GPU backend, deployment-lock, backup/restore, supervisor samples, final status. Тестовая группа и локальная GPU используются только при наличии токена/весов; скачивание крупной модели не скрывать как часть unit test.

Приёмка: установка с нуля по README, импорт выбранной истории, контекстный ответ, продолжение/stop, restart без повторного панча, backup restore; итог содержит реально проверенное и непроверенное. Не требуются deploy в облако, commit или PR без указания владельца.

## Готовый промпт для исполнителя

```text
Ты реализуешь пакет T<N> проекта pozor.me. Рабочая папка — текущий репозиторий.
Прочитай README.md, docs/05-tasks.md, документы по этому пакету,
reference/core.py и tests/test_reference.py. Сначала проверь текущий код и git diff.
Сохрани границы пакета и контракты. Реализуй production-модуль, миграции и
проверки приёмки; reference — эталон отдельных правил, не готовое приложение.
Не выполняй работу будущих пакетов и не заменяй реальные adapter-ы заглушками,
которые выглядят как завершённый продукт. Тесты должны быть offline по умолчанию.
Не запускай реальную рассылку, не используй реальные переписки в fixtures,
не печатай секреты, не скачивай веса без явной необходимости.
В конце: изменённые файлы, команды/результаты проверок, ограничения и
что нужно следующему исполнителю. Если acceptance не прошёл — не помечай
пакет завершённым. Архитектурные расхождения объясни до изменения контракта.
```

## Что проверить дорогой моделью только на контрольных точках

После T3: ID mapping, import scope, provenance и rollback. После T5: отмена/идемпотентность/отправка и разделение чатов. После T7: сверка acceptance и эксплуатационных обещаний. Остальную работу можно делегировать по одному пакету без повторного полного проектирования.
