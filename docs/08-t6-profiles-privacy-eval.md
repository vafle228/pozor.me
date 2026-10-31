# T6 — карточки, удаление, оценка

Дата: 2026-10-31. **Пакет не закрыт:** автоматическая часть реализована, ручной
quality gate и latency локальной модели пока не измерены. Результаты контрольной
точки T5 из исходного рабочего дерева включены без потери регрессионных тестов.

## Реализация

- Миграция 0008: карточки, source hashes, watermark, минимальный tombstone,
  `imports.retained_until`. Миграции 0001–0007 не изменялись.
  У старых active imports до 0008 отсутствовала дата apply: миграция использует
  `created_at + 90 days` как консервативный срок; только новые apply знают точную дату.
- `profiles.py`: extractive v1, только точные цитаты, до пяти источников. Self pattern
  означает повтор нормализованной цитаты в ≥3 эпизодах с разрывами ≥30 минут.
  Разные авторы и attribution не объединяются. Imported quote не становится observed
  self; third-party остаётся third-party; manual claims вообще не идут в карточки.
  Нет семантических тем или непроверяемых выводов о личности.
- Опциональный runtime scheduler: 40 новых сообщений либо 10 минут при наличии
  новых данных; background через очередь T4, до 1500 tokens настоящего tokenizer,
  шесть карточек за транзакцию. Вызова генерации профиля нет. Commit повторно
  проверяет epoch, hash, авторство и entitlement. Watermark только после commit;
  слишком большой одиночный source фиксируется как пропуск, не блокирует архив.
- `PROFILE_CARDS_ENABLED=false` по умолчанию. Карточки повышают rank исходных
  цитат с прежними IDs/attribution; не подменяют direct retrieval.
- Edit/delete удаляют всю зависимую карточку; epoch invalidation при rollback,
  изменении consent и purge удаляет target-карточки и сбрасывает watermark.
  При миграции группы карточки сбрасываются, tombstone переносится.
- `/forget_me` действует только от самого участника, только в текущей разрешённой
  группе; anonymous sender не может менять consent или удалять данные. Удаляются
  authored/subject-linked сообщения целиком, в том числе shared текст, FTS, manual
  items target, карточки, aliases и личные labels. Membership остаётся как ID с
  consent=0/opt_out=1. Tombstone не снимается импортом или новым live сообщением.
  Только успешный явный `/roast_on` разрешает новый сбор/импорт.
- Purge намеренно закрывает **все сессии группы** и удаляет их turns/inputs/outbox,
  command receipts и inbox группы: там могут быть цитаты удалённого участника.
  Epoch затронутых target меняется в той же транзакции. In-memory остаток poll batch
  повторно проверяет существование inbox row, не исполняется после purge.
  GPU jobs отменяются через очередь, поздние результаты не могут восстановить turn.
- `privacy.cleanup`: live TTL по policy группы, import/manual TTL от **apply**
  (90 дней по умолчанию), staging 24 часа, inbox и session text максимум сутки.
  Runtime запускает cleanup каждый час; `pozor db cleanup` — offline запуск вручную.
  Retrieval и guards дополнительно проверяют TTL до работы cleanup.

## Команды

Изменённые файлы:

- Новые runtime-модули: `src/pozor/profiles.py`, `src/pozor/privacy.py`,
  `src/pozor/evaluation.py`, `src/pozor/migrations/0008_profiles_privacy.sql`.
- Интеграция: `src/pozor/cli.py`, `commands.py`, `config.py`, `context.py`,
  `delivery.py`, `importing.py`, `ingestion.py`, `runtime.py` (в том же каталоге).
- Проверки: `tests/test_profiles_privacy.py`, `tests/test_delivery.py`,
  `tests/test_importing.py`, `tests/test_intelligence.py`, `tests/fixtures/eval.jsonl`.
- Конфигурация/документы: `.env.example`, `README.md`, `docs/03-intelligence.md`,
  `docs/04-runbook.md`, `docs/05-tasks.md`, `docs/07-t5-delivery.md`, этот документ,
  `reports/t6-benchmark.json`.

```powershell
uv run pozor db migrate
uv run pozor db cleanup
uv run pozor profile show --chat -1001234567890 --target 123456789
uv run pozor benchmark --suite tests/fixtures/eval.jsonl --output reports/t6-benchmark.json
# Только при готовом локальном сервере и весах; Telegram не используется:
uv run pozor benchmark --suite tests/fixtures/eval.jsonl --output data/model-eval.json --live --repeat 3
```

## Оценка

Заморожены 30 синтетических кейсов: 10 callbacks, 5 без истории, 5 identity,
5 adversarial, 5 control (stop, epoch edit, forget, deadline, opt-out).
Offline отчёт: [reports/t6-benchmark.json](../reports/t6-benchmark.json).
Он проверяет production retrieval и production guard до/после control mutation.
Edit control проверяет epoch-контракт; настоящие ingestion edits и import rollback
покрыты отдельными regression tests. Это маленький baseline, не оценка русского
semantic recall: candidate evidence coverage измерена **до token packing**.
Отдельные поля для generated evidence coverage и ручной оценки не заполняются
искусственными результатами.

Offline latency в отчёте включает создание БД/миграции и retrieval. Это **не GPU
latency**. Opt-in live benchmark использует production LlamaClient/Generator,
3 warm-ups и не менее 50 вызовов (25 model cases × минимум 2 повтора); control
cases не запускают генерацию. Сохраняются lengths, fallback, evidence IDs, текст
синтетического ответа и p50/p95. Queue wait, concurrency и VRAM не измеряются этим
командным путём и обозначены как непроверенные. Шутки оценивает человек по rubric
из runbook: relevance/callback/brevity/humor 0–2, отдельно attribution/safety/stop.
Gate ≥6/8, ≥80% relevant evidence и ноль критических нарушений ещё не подтверждён.

## Ограничения и передача

Проверки: полный offline suite **215 passed**; Ruff на приложении и изменённых
тестах, mypy приложения, `git diff --check` — успешно. Wheel/sdist собраны через
`uv build --offline --cache-dir .uv-cache --out-dir .test-tmp/t6-dist`; в wheel
проверены миграция 0008, новые модули и prompt. Команда полного тестирования:
`.venv\Scripts\python -m pytest -q --basetemp=.test-tmp/t6-release`.

- Реальные Telegram, GPU, веса, рассылки и переписки не использовались.
- Уже in-flight Telegram HTTP при purge/stop может доставиться; его нельзя отозвать
  локальной транзакцией. Отправленное в Telegram команда не удаляет.
- Удаление логическое: следы SQLite/WAL и резервных копий не стираются мгновенно.
  Входные operator exports и manifests по произвольным пользовательским путям
  остаются файлами оператора. Автоматически удаляются только manifests внутри
  `<database parent>/staging`; previews в SQL становятся неприменимыми независимо
  от удаления файла. При forget очищаются и application-owned active manifests
  группы, поскольку они могут содержать shared текст. Оригинальные экспорты нужно
  удалить/ротировать самостоятельно; старый backup может содержать удалённые данные.
- Extractive matcher не объединяет синонимы/перефразировки и не распознаёт темы;
  отсутствие карточек не мешает непосредственному поиску цитат.
- Следующий исполнитель должен завершить T6 human/model eval, сохранить exact
  deployment-lock/hardware с измерениями и ручными оценками. Только затем решать
  о закрытии T6 и переходить к эксплуатационному пакету T7. Нельзя считать
  offline candidate coverage доказательством качества юмора или отсутствия
  смысловых hallucinations в prose.
