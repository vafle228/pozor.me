# T5: вертикальная интеграция и доставка

Файлы T5: `src/pozor/delivery.py`, `src/pozor/runtime.py`, `src/pozor/commands.py`,
`src/pozor/ingestion.py`, `src/pozor/migrations/0007_delivery.sql`,
`tests/test_delivery.py`, `tests/test_commands.py`, `tests/test_importing.py`,
`README.md`, `docs/04-runbook.md`, `docs/05-tasks.md`, `docs/06-t4-inference.md`
и этот документ. Незакоммиченные изменения T3/T4, присутствовавшие до начала T5,
сохранены и включены в общий коммит по указанию владельца.

`pozor run` связывает реальные aiogram Bot API и httpx llama.cpp adapters через
`DeliveryPipeline`. Тесты по умолчанию offline: синтетические экспорты, временная
SQLite, fake Telegram и httpx MockTransport. Команда `run` в acceptance не запускалась.

## Состояние и отмена

Миграция 0007 добавляет `turns`, `turn_inputs`, `outbox`, `delivery_limits` и
`interaction_receipts`. Номер хода уникален в сессии; все включённые trigger message IDs
сохраняются отдельно. Переходы: pending → queued → generating → ready → sending → sent.
Cancelled/failed/unknown не дают права на отправку. Revision относится к попытке хода,
epoch берётся вместе с evidence из retrieval snapshot. Deadline задаётся до retrieval
и ожидания очереди; coalescing/cooldown происходят до начала этого deadline.

Перед GPU factory, созданием outbox и каждым send проверяются session ID, revision,
epoch, следующий номер хода, TTL/тишина, deadline, allowlist, enabled, consent/opt_out.
Повторный прямой ответ target обновляет pending turn; во время inference отменяет
старый HTTP и повышает revision. Очередь T4 ждёт idle slot перед следующей completion.
Debounce — 1.2 секунды, максимум 3 секунды, с ожиданием минимального интервала
10 секунд после предыдущей доставки. Максимум две пересборки начавшегося inference;
после этого нужен следующий прямой вызов. Обычный разговор и чужая тема не продолжают сессию.

Адресация: reply на сохранённый Telegram ID доставленного панча текущей сессии или
entity-backed упоминание username бота. `/roast` поддерживает прежние формы и
`@bot попусти @target ...`. Target может остановить сессию словами «стоп», «хватит»,
«отстань», «не продолжай» при адресации; инициатор — отдельной фразой
«всё, доминация» в reply. Slash-команды и кнопки сохраняют проверки T2.

Stop/opt-out/disable немедленно закрывают сессию и меняют revision. Проверка jobs
и outbox выполняется при обработке события и sender loop каждые 100 мс.
Edit входной реплики меняет revision; edit evidence, retention и import rollback
обнаруживаются также по epoch, включая изменения из отдельного локального CLI.
Миграция группы сохраняет историю и закрывает активные сессии `chat_migrated`,
чтобы прежний scoped context не отправился в новую группу.

## Доставка и восстановление

Sender один на процесс. В короткой `BEGIN IMMEDIATE` транзакции проверяет guards,
резервирует лимиты и меняет ready → sending; затем сразу начинает HTTP, без await
между commit и вызовом sender. Сеть никогда не выполняется с открытой транзакцией.
Панчи отправляются plain text (`parse_mode=None`), с reply_parameters и stop/finish
кнопками с ID сессии. Собственные панчи — только session memory, не evidence профиля.

Локальные консервативные лимиты панчей: одна попытка в секунду глобально,
одна в три секунды на чат; next_at сохраняются в SQLite. Продуктовый интервал сессии —
10 секунд. Это выбранные ограничения приложения, не обещание конкретных лимитов Telegram.
ACK команд остаются отдельными ответами T2; уведомления «занят/недоступно» объединены
одним 30-секундным cooldown на чат.

- Явный 429 возвращает запись в ready с next_attempt_at=now+retry_after. Повтор
  разрешён только после нового guard, до исходного deadline.
- 403 выключает чат и закрывает все его сессии.
- Timeout/сетевой сбой с возможной доставкой → unknown, сессия закрывается
  `delivery_unknown`; автоматического повтора нет. Прочие ошибки Telegram также
  трактуются консервативно, без слепого retry.
- Перед HTTP сохраняется sending. Crash после успешного send до DB commit оставляет
  sending; при restart это unknown. Старые ready/pending/queued/generating отменяются,
  active sessions закрываются restart. Сохранённые sent не повторяются.

HTTP уже in-flight в момент stop может завершиться доставкой. Успех фиксируется
даже для уже закрытой сессии, но следующего хода не будет. Exactly-once не обещается:
возможен потерянный ответ. Ingestion и jobs не составляют единую сетевую транзакцию;
durable receipts предотвращают повтор хода, а restart намеренно закрывает сессии.
Сбой до постановки job может потерять раунд; возобновлять его после restart нельзя.

Health проверяется отдельной задачей каждые 5 секунд с deadline 3 секунды. До первого
успешного check новые сессии не открываются; polling, consent и stop продолжают работать.
Circuit breaker/валидация/нейтральный fallback принадлежат T4. Фоновые runtime задачи
контролируются вместе с poller: отказ sender прекращает процесс вместо тихой потери доставки.

## Приёмка и передача в T6

`tests/test_delivery.py` проверяет import → retrieval → generator → validator → outbox →
sender, в том числе настоящий httpx adapter через MockTransport; commands/polling с
занятой GPU; stop/edit/rollback/opt-out/disable/deadline в inference и outbox;
epoch-контракт forget; продолжения, coalescing, cooldown, revision replacement,
ограничение пересборок и 4 ходов; 429/recheck, ambiguous timeout, 403, crash,
restart, конкурирующий claim и сохранённые rate limits.

`/forget_me`, cards, profile show, очистка чувствительных turns/turn_inputs/outbox,
watermarks и human eval остаются T6. Forget должен в одной транзакции поднять
target epoch и удалить соответствующие тексты, включая новые таблицы T5; удаление
turn каскадно удаляет inputs/outbox. Тест T5 использует epoch-инвалидацию, а не
подставной finished forget adapter. Opt-out не является удалением.

Реальные Telegram/GPU/latency/slot release не проверялись. Проверить их отдельно
с явным запуском в тестовой группе, готовыми весами и закреплённым deployment-lock.
Не делать выводов о качестве шуток и времени ответа по offline suite.
