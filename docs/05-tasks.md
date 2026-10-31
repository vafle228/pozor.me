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

**Контрольная точка T3 — исправления ревью (2026-10-31):** устранены семь дефектов: потеря импортов при миграции группы; сохранение чужой target-связи при rollback; принятие изменённого автора/даты за дубль; `observed_self` для экспортных цитат; импорт личных чатов; фиктивное подтверждение identity; пропуск epoch invalidation при rollback shared/manual evidence. Миграция `0005_import_provenance.sql` исправляет provenance уже сохранённых экспортных цитат и инвалидирует соответствующие epochs, не меняя live evidence. Apply проверяет live identity в группе назначения внутри транзакции; неизвестные ID остаются в staging. Policy version повышена до 2, старые preview требуют пересоздания. При миграции группы активные batches и source namespaces сохраняются, staged previews истекают и требуют нового chat ID.

Проверено: **105 passed** (77 прежних + 28 новых случаев), включая реальные потоки ijson 3.6.0 для файлов больше 8 MiB, оба формата Desktop JSON, shared-target rollback, атомарный конфликт авторства/даты, обновление БД с миграций 0001–0004 до 0005 и повторное применение миграций. Ruff на изменённых Python-файлах, mypy `src/pozor` и `git diff --check` проходят. Проверки offline на синтетических данных; Telegram и реальные переписки не использовались.

## T4 — локальный inference и retrieval

Результат: httpx adapter, health/model check, non-thinking smoke, token counting, bounded queue, RRF/context packer, prompt/schema, validation/fallback, model manifest. Без Telegram integration можно fake LLM. Runtime completion один, максимум один validation retry в общем deadline.

Приёмка: structured JSON, wrong IDs, hallucinated author, malformed result, timeout, context overflow, injection quoted as data, empty evidence, repetition, unrelated target/context isolation, cancellation; interactive при background занятости. Реальный smoke по желанию оператора, отдельно от CI. Замер отсутствует → нельзя писать «3 секунды» как факт.

**T4 реализован; offline acceptance пройден (2026-10-31):** production httpx adapter для llama.cpp, health/model/slot/context check, opt-in `pozor llm smoke`, tokenizer через apply-template/tokenize, отдельная очередь 8 interactive/16 background с chat round-robin, target coalescing, отменой background HTTP и ожиданием серверного idle slot. Scoped SQLite retrieval: safe FTS/BM25, recent self, mentions/replies, active manual claims, weighted RRF, metadata-aware budget/dedup/diversity. Версионированный system prompt, строгий JSON schema/validator, один validation retry в общем deadline и нейтральный fallback, circuit breaker. Миграция 0006 сохраняет reply source ID для новых live/export сообщений, старые parent links не выдумываются. Убрано дублирующее Hatch force-include, мешавшее сборке wheel.

Проверено: `.venv\Scripts\python -m pytest -q --basetemp=.test-tmp/t4-acceptance` — **144 passed**; Ruff на `src/pozor` и изменённых тестах; mypy `src/pozor`; `uv build --offline --cache-dir .uv-cache --out-dir .test-tmp/dist` — wheel/sdist собраны после загрузки build dependency hatchling. Проверены CLI help, ресурсы wheel, import из распакованного установленного layout и миграции 0001–0006. Исправлена прежняя runtime-зависимость T3 от отсутствующего в wheel `reference.core`: только чистые правила отбора перенесены в `import_rules.py`, conformance с независимым reference проверяется тестом. Windows sandbox блокирует создание asyncio socketpair и PE resources uv; итоговые pytest/build выполнены вне sandbox. HTTP тесты работают через MockTransport; реальные Telegram/LLM, переписки, рассылка и веса не использовались.

Ограничения: реальный GPU smoke, latency, hidden thinking и освобождение настоящего slot не измерялись. `deployment-lock.json` честно unverified, artifact/build/SHA не закреплены. Validator отклоняет выдуманные ID/author fields, но не гарантирует истинность авторских утверждений в prose — human eval в T6. Topic diversity пока использует thread/dossier группы без semantic cards. T5 должен подключить pipeline к сессиям, outbox и sender с повторными revision/epoch/consent guards; T6 добавляет cards/eval. Подробный [контракт передачи T4 → T5](06-t4-inference.md).

## T5 — вертикальная интеграция и доставка

Результат: связывает start→retrieve→generate→validate→outbox→send; reply turns/coalescing, generation revision, evidence epoch, rate limits, stop race, restart recovery. Только теперь `/roast` реально отправляет LLM-панч.

Приёмка: fake pipeline с imported evidence; stale после stop/edit/forget/rollback не отправляется; direct reply продолжает, unrelated message нет; 429 повторяет после актуальности, ambiguous timeout не повторяет; crash после successful send до DB commit не вызывает слепой retry; max turns не превышается. Внешний HTTP уже in-flight при stop отмечается как ограничение, а не скрывается.

**T5 реализован; offline acceptance пройден (2026-10-31):** `DeliveryPipeline` подключён к реальным aiogram/httpx adapters в `pozor run`. Миграция 0007 добавляет durable turns/inputs/outbox, receipts и sender limits. Реализованы scoped reply/mention continuation, coalescing 1.2–3 секунды, 10-секундный интервал, максимум две пересборки inference, revision/epoch/consent/deadline guards перед GPU, outbox и send, отмена stale jobs, независимый health loop, rate-limited busy notices и shutdown/restart recovery. Sender сохраняет sending перед HTTP; 429 повторяется после retry_after и нового guard, ambiguous timeout/crash становится unknown без retry, 403 выключает чат. Миграция группы закрывает активные сессии, сохраняя историю.

Проверено: `.venv\Scripts\python -m pytest -q --basetemp=.test-tmp/t5-acceptance` — **179 passed**, включая 35 новых T5 cases. Ruff на приложении и изменённых тестах, mypy `src/pozor`, `git diff --check`, CLI help, frozen install с dev/imports и wheel/sdist build. Pytest/build выполнялись вне Windows sandbox из-за ограничений asyncio socketpair/PE resources; тесты остаются offline. Для editable install потребовалась загрузка отсутствующего build dependency `editables`, без изменения lock. Реальные Telegram/LLM, переписки, рассылка и веса не использовались.

Ограничения: HTTP уже in-flight при stop может доставить сообщение; exactly-once не обещается. Epoch-контракт forget проверен без реализации `/forget_me` — это T6, как и cards, purge turns/inputs/outbox и human eval. Реальные GPU/Telegram smoke и latency остаются непроверенными. Подробные состояния, failure semantics и передача следующему исполнителю — в [контракте T5](07-t5-delivery.md).

**Контрольная точка T5 — ревью (2026-10-31):** исправлены повтор retrieval после
таймаута, повторное использование inputs отменённой попытки, блокировка других
чатов локальным sender cooldown, обход scope-проверок ранними health notices,
вызов `@bot попусти` без mention entity и сетевой admin lookup перед stop участника.
Подробности — в [контракте T5](07-t5-delivery.md). Добавлено 14 регрессионных cases.
Полный offline suite: **193 passed** (`--basetemp=.test-tmp/t5-review-final`);
Ruff на `src/pozor` и `tests/test_delivery.py`, mypy `src/pozor`, `git diff --check`
проходят. Pytest выполнен вне Windows sandbox из-за зависания asyncio в sandbox.
Реальные Telegram/GPU не запускались; границы T6 сохранены.

## T6 — портрет, удаление, оценка

Результат: quote-backed cards, background watermark, invalidation, TTL cleanup, `/forget_me`, `profile show`, eval suite/benchmark report. Карточки могут быть выключены без поломки direct retrieval.

Приёмка: third-party не повышается до self, 3 соседние реплики не «3 эпизода», отсутствующий source инвалидирует card, import TTL от даты импорта, FTS/inbox/outbox/staging очищаются, opt-out переживает reimport. Оценка 30 кейсов с evidence coverage и latency; качество юмора оценивает человек, не только сама LLM.

**Статус T6 (2026-10-31): автоматическая часть реализована, пакет не закрыт.**
Миграция 0008, extractive quote-backed cards, background watermark/token budget,
invalidation, TTL cleanup, `/forget_me` с tombstone и отменой pending delivery,
`profile show`, 30 синтетических eval cases и offline benchmark report добавлены.
Карточки опциональны, работают через исходные evidence IDs; semantic LLM extractor
сознательно заменён точными цитатами без свободных утверждений о человеке.
Контрольная точка T5 включена в изменения.

Проверено: **215 passed** (`.venv\Scripts\python -m pytest -q --basetemp=.test-tmp/t6-release`),
Ruff `src/pozor` и изменённых тестов, mypy `src/pozor`, `git diff --check`.
Offline CLI benchmark: 30/30 cases, candidate evidence coverage 1.0; это не замер
использования цитат реальной моделью. `uv build --offline --cache-dir .uv-cache
--out-dir .test-tmp/t6-dist` собрал wheel/sdist, проверены ресурсы wheel.
Pytest/build/CLI benchmark выполнялись вне Windows sandbox из-за ограничений
asyncio/socketpair и PE resources. Реальные Telegram/GPU/веса не использовались.
Human humor/safety gate, model latency, concurrency и VRAM остаются непроверенными.
Передача и ограничения: [T6](08-t6-profiles-privacy-eval.md),
[offline report](../reports/t6-benchmark.json). T7 не реализовывался.

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
