# Данные и импорт в V1

## 1. Два входных формата

**Telegram Desktop JSON:** экспорт конкретного чата, только текст, выбранный диапазон дат; поддержать также общий экспорт `chats.list` с обязательным выбором ровно одного чата. JSON-экспорт поддерживается самим Telegram. [Экспорт](https://telegram.org/blog/export-and-more), [реализация сериализатора](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/export/output/export_output_json.cpp).

**Ручное досье:** JSON версии 1 с заметками/цитатами. Нужен для «600 дней в Reddit» ещё до накопления истории. Произвольный TXT не парсить в факты моделью: пользователь копирует фрагменты в явные поля. Не требовать загрузить весь архив, если нужны три цитаты.

```json
{
  "schema_version": 1,
  "target_user_id": 123456789,
  "items": [
    {
      "id": "reddit-streak",
      "kind": "operator_note",
      "text": "600 дней подряд заходит в Reddit",
      "occurred_at": null,
      "source_label": "заметка инициатора",
      "source_url": null
    },
    {
      "id": "game-quote",
      "kind": "pasted_quote",
      "text": "И я устрою полный трахау",
      "occurred_at": "2026-10-30T23:37:00+05:00",
      "source_label": "вставленная вручную цитата Макса",
      "source_url": null
    }
  ]
}
```

Оба вида — `manual_claim`, пока не связаны с наблюдавшимся live message. Даже JSON-экспорт редактируемый: статус `imported_quote`, а не «криптографически проверенный факт». Ссылка не подтверждает содержимое и автоматически не открывается. JSON не может задавать consent, роли, системный промпт, TTL, owner_id или исполняемые команды.

Готовая заготовка: `examples/maxwell.dossier.json`. ID 123456789 — заглушка, заменить на подтверждённый числовой ID; не использовать этот пример как реальные данные без проверки. Время из примера пользователя сохранено с явно указанным UTC+05:00.

## 2. Сценарий оператора

1. Target написал боту/в группе хотя бы один раз; `/whois` reply показывает числовой ID и chat ID. Если ещё неизвестен — импорт можно подготовить в staging по явно заданному ID, использовать только после подтверждения live identity. Не сопоставлять по display name.
2. В Telegram Desktop экспортировать нужный чат в JSON. Не экспортировать медиа. Локально передать файл CLI; в V1 не делать Telegram upload произвольных архивов.
3. Запустить будущую команду preview:

```powershell
uv run pozor import preview --file .\imports\result.json --format telegram-desktop --source-chat 123456 --chat -1001234567890 --target 123456789 --from 2026-09-01 --until 2026-11-01 --timezone Asia/Yekaterinburg
```

`--source-chat` — ID внутри экспорта, `--chat` — разрешённая группа назначения. Это **разные пространства ID**; запрещено угадывать сопоставление добавлением `-100`. Даты: полуинтервал [from, until), локальные границы переводятся в UTC. По умолчанию выбрать все подходящие сообщения target в диапазоне, явные упоминания и ответы ему; соседи не включаются без `--neighbors 1`.

4. Preview ничего не добавляет в рабочую память: создаёт staging manifest с SHA256 файла, ID источника/target/destination, диапазоном, версиями parser/policy, ID выбранных сообщений, причинами включения и hash итогового нормализованного payload. Показывает счётчики self/mention/reply/context/unknown, пропущенные медиа/ошибки, несколько примеров и предупреждение о ручной привязке.
5. Можно добавить `--message-ids 51,52,60` для пересечения с выбранным диапазоном или подготовить ручное досье. Упоминания устаревших usernames включать только через явно утверждённые aliases с диапазоном дат; совпадение «Макс» не считать связью. Не включать чужие приватные чаты в групповую память: назначение источника подтверждает оператор, для V1 разрешены только экспорты этой группы и ручные заметки.
6. `uv run pozor import apply --manifest .\data\staging\<batch>.json` проверяет hash, ID, allowlist, identity и policy заново, затем транзакционно применяет. При изменении файла нужен новый preview. Apply реализует идемпотентность; повторный запуск не удваивает строки. Большой import — staging chunks, атомарная активация batch только после полной загрузки.
7. `uv run pozor profile show --chat ... --target ...` показывает доступные темы с происхождением. Профиль не обязан ждать LLM: импортированные цитаты доступны retrieval сразу. До первого вызова желательно закончить фоновую индексацию.
8. `uv run pozor import rollback --batch <id>` удаляет привязки batch и производные карточки; общие сообщения из других batch/live не уничтожает.

Ручной ввод: `import preview --format dossier-v1 --file .\imports\maxwell.json --chat ... --target ...`. ID в файле должен совпасть с CLI. Изменение досье = новая ревизия batch, не незаметное обновление старого факта.

## 3. Критический алгоритм отбора

Порядок: проверить формат/размер → выбрать source chat → разобрать сообщения → построить карту message_id → определить ID авторов → выбрать диапазон и target → классифицировать связи → при необходимости добавить ограниченных соседей → preview → apply.

- `text` бывает строкой или массивом строк/объектов `{type,text}`. Склеить **один раз**, не прибавлять `text_entities` повторно. Не читать пути media/file из экспорта.
- `from_id=user123` соответствует user ID 123; `channel...`, anonymous и отсутствующий автор не превращаются в человека.
- Self: автор target. Mention: `mention_name` с numeric user_id либо явное `@username` из утверждённых aliases для даты сообщения. Reply: родитель точно authored by target в том же source chat. Reply показывает адресата, не гарантирует, что текст описывает его.
- Соседние сообщения — только `context`, никогда не приписывать target. Только в том же source chat и в пределах 5 минут; не переходить через разные topics, если metadata позволяет их различить. Если экспорт не даёт topic, он не участвует в live recent-topic buffer.
- Forward — это слова пересланного автора, не автоматически автора-сендера. В V1 исключить forwarded content из self evidence; можно хранить как context. Service/media-only исключить.
- Использовать `date_unixtime`; если нет — timezone-aware ISO, а naive ISO требует явную timezone. Ошибка даты/ID у выбранного сообщения — ошибка preview, а не тихая утрата.
- Команды, prompt injections и прошлые панчи бота из импорта не выполняются. Импорт никогда не проходит через live router.
- Точный дубль: namespace + source_message_id + content_hash. Тот же source key с другим текстом → conflict в preview. Live edit авторитетнее старого экспорта только при подтверждённом source mapping. Не перезаписывать автоматически.

Эталонная функция отбора находится в `reference/core.py`. Это чистое ядро для уже выбранного одного чата, не готовый безопасный загрузчик файлов. Полный adapter/validation/staging нужны в задаче T3.

## 4. Таблицы и индексы — контракт миграций

| Таблица | Ключи и важные поля |
|---|---|
| `chats` | chat_id PK, enabled, policy_version, retention_days |
| `members` | PK(chat_id,user_id), display_name, current_username, last_seen, consent, consent_version, opt_out |
| `aliases` | chat_id,user_id,username,valid_from,valid_to,source; неоднозначные интервалы не резолвить |
| `inbox` | update_id PK, normalized payload, received_at, processed_at; краткое хранение |
| `poller_state` | bot_id PK, next_offset |
| `source_chats` | namespace PK, export_chat_id, destination_chat_id, mapping_status; ручная проверка |
| `messages` | internal id PK, scope_chat_id, source_namespace, source_message_id, thread_id nullable, author_id nullable, text, hash, timestamps, origin; UNIQUE(namespace,source_message_id) |
| `message_subjects` | PK(message_id,target_id,relation), attribution; доказательство связи |
| `imports` | batch_id PK, manifest_hash UNIQUE, status(staged/active/rolled_back), operator_id, target_id, chat_id, created_at |
| `import_items` | PK(batch_id,message_id), relation, selected; сообщения могут быть в нескольких batch |
| `manual_items` | item PK, batch_id, target_id, text, kind, source_label; ревизии без перезаписи |
| `profile_cards` | id, chat_id,target_id,topic,summary,status,profile_version; не хранить без evidence |
| `card_evidence` | PK(card_id,message_id), source_text_hash; cascade invalidation |
| `sessions` | id, chat_id,thread_id,target_id,initiator_id,state,revision,turn_count,created_at,last_sent_at,closed_reason |
| `turns` | PK(session_id,turn_no), trigger IDs, revision, deadline, evidence_epoch, status, text, evidence IDs |
| `outbox` | PK(session_id,turn_no), status, next_attempt_at, telegram_message_id, error_code |
| `data_epochs` | PK(chat_id,target_id), epoch; повышать при purge/edit/import rollback/consent change |

`messages_fts`: FTS5 по нормализованному тексту с external content + INSERT/UPDATE/DELETE triggers или управляемым rebuild в той же транзакции. Политика единая и покрыта тестами. FTS без SQL scope-фильтра применять запрещено. Индексы `(scope_chat_id,author_id,occurred_at)`, `(scope_chat_id,thread_id,occurred_at)`, subjects(target_id,message_id), active sessions partial UNIQUE(chat_id,thread_id) WHERE state='active'. Foreign keys ON на каждом соединении; WAL, busy_timeout=5000, короткие транзакции; один writer coordinator.

## 5. Хранение и удаление

Defaults: live raw text 30 дней; выбранные import/manual материалы 90 дней **от импорта**, с отображением срока в preview. Profile cards не переживают свои источники. Staging 24 часа; логи 7 дней и без текстов/токенов. Продление только явной операцией владельца. Старый годовой экспорт допустим — историческая дата не равна дате истечения хранения импорта.

`/forget_me` удаляет в текущей группе self evidence, все subject links/manual items о target, карточки, turns с его содержимым, pending jobs, staged imports target и FTS entries. Для сообщений, где target упомянут наряду с другими, V1 выбирает простое полное удаление текста, а не ненадёжную частичную редакцию. Отмена pending отправок через epoch. `/roast_off` не равен удалению; справка объясняет разницу. Не забыть текстовые payload inbox/outbox и backup retention. Физическое удаление следов SQLite/WAL/backups не обещать мгновенным: backup rotation + checkpoint/secure_delete политика отдельно, доступ к диску ограничить.

Импорт после opt-out не снимает запрет. Для `/forget_me` сохраняется минимальный tombstone ID+policy, чтобы архив не воскресил удалённые материалы; повторная загрузка возможна только после явного изменения выбора участником. Ни профиль, ни retrieval, ни память модели не общие между чатами. KV cache модели не заменяет БД и не даёт право смешивать contexts.
