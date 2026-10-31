# T7 — эксплуатация и финальный smoke

2026-10-31. **Не закрыт.** Offline-операции реализованы; T6 human/model gate и
реальный Telegram/GPU/supervisor smoke не выполнены. Наличие токена или файлов
модели не считается разрешением на отправку в группу. В этой итерации сеть
Telegram, запуск llama.cpp, регистрация задач и скачивание весов не выполнялись.

## Контракт и реализация

`operations.py`: online SQLite backup API с committed WAL, deadline 30 секунд,
integrity_check, foreign_key_check, FTS external-content integrity и точная сверка
ledger миграций с ресурсами приложения. Источник не мигрируется. Нет новой
миграции: схема T0–T6 не меняется. Несовместимая версия/изменённый checksum
отклоняются; сначала используйте соответствующую версию приложения. Старую схему
обновляйте отдельной операцией с сохранённой исходной копией.

Backup публикуется только после проверки в новом файле. Windows использует
same-directory rename с отказом при существующем destination, POSIX — hard link.
Локальный SSD/NTFS предпочтителен; сетевые/облачные каталоги не проверены.
Ротация затрагивает только `pozor-backup-<UTC>-<UUID>.sqlite3` в выбранном каталоге,
старше retention по mtime, только после успешного snapshot. Это не фоновый таймер:
без запуска backup старые файлы сами не исчезнут. Произвольные экспорты, manifests,
конфигурация, lock, веса, логи и сторонние копии в backup не входят.

Restore принимает только **новый** destination без SQLite sidecars. Временная
копия проверяется, production `recover_delivery` закрывает active sessions как
`restart`, ready/pending отменяются, sending становится unknown, sent сохраняется.
Checkpoint, consent, tombstones, import provenance, FTS и receipts входят в snapshot.
Источник и действующая БД не заменяются. Ошибка не публикует частичную БД.

Откат к backup теряет всё после его даты, включая новые opt-out/forget и записи о
доставках. Snapshot не может доказать отсутствие последующих отправок в Telegram.
Перед сетевым запуском восстановите актуальные запреты участников и оцените
неоднозначные доставки; exactly-once после отката не обещается. Старые backups
содержат удалённые данные до ротации. Forget не стирает внешние файлы оператора.

`status` читает БД без создания отсутствующего файла: schema, inbox backlog,
active sessions, outbox по состояниям, количество messages и логический размер
страниц БД. Это не физический расход WAL/диска, health probe или export переписки.
В вывод не включены IDs людей, тексты, prompt, токены и credentials.

## Backup / restore Windows

Все команды — из корня checkout; используйте тот же private account, что и бот.
До первой записи защитите каталог данных/backup. Пример для нового каталога:

```powershell
New-Item -ItemType Directory -Path data/backups -Force
$ownerIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls data/backups /inheritance:r /grant:r "${ownerIdentity}:(OI)(CI)F"
uv run --frozen pozor db backup --directory data/backups --retention-days 7
uv run --frozen pozor db restore --source data/backups/<snapshot>.sqlite3 --destination data/recovery/pozor.sqlite3
```

`icacls` проверьте вручную: ранее заданные explicit ACE не удаляются этой командой.
Private ACL нужны также checkout с `.env`, data, recovery и операторским экспортам.
На POSIX используйте private каталог с mode 0700; snapshot создаётся через 0600.
В этих проверках ACL не изменялись, DPAPI/encryption не реализованы.

Проверить restore, не включая polling:

```powershell
$env:DATABASE_PATH = (Join-Path (Get-Location) 'data/recovery/pozor.sqlite3')
uv run --frozen pozor status
uv run --frozen pozor db migrate  # ожидается schema is current
uv run --frozen pozor profile show --chat <chat-id> --target <user-id>
Remove-Item Env:DATABASE_PATH
```

Для действительного переключения: остановить Bot task/процесс, исключить второй
poller, сохранить старую БД backup API, восстановить на новый путь, сверить
актуальные consent/forget, поменять `DATABASE_PATH` в private `.env`, затем запуск.
Не копируйте живую `.sqlite3` отдельно от WAL и не переименовывайте файлы поверх
работающей БД. Pending jobs после restart не возобновляются. Сохранённые sent в
snapshot повторно не отправляются. Staging preview после restore нужно пересоздать:
manifests/exports не входят в snapshot и не являются журналом восстановления.

## Модель и deployment-lock

Lock остаётся `unverified_baseline`; null не заменены предположениями. Оператор
заполняет model repo, **commit revision**, реальное имя GGUF и SHA256; точную
llama.cpp build/release/commit, имя server executable и SHA256, GPU, backend,
результаты benchmark. Дополнительные DLL/backend libraries закрепите в локальном
manifest дистрибутива: CLI хеширует только GGUF и executable, не все DLL/драйверы.
Пример получения SHA без загрузки модели:

```powershell
Get-FileHash models/Qwen3-8B-Q4_K_M.gguf -Algorithm SHA256
Get-FileHash vendor/llama.cpp/llama-server.exe -Algorithm SHA256
uv run --frozen pozor deployment check --model models/Qwen3-8B-Q4_K_M.gguf --server vendor/llama.cpp/llama-server.exe
```

Хеши в lock — lowercase. Проверка сравнивает alias/context/output с Settings,
требует один slot и non-thinking; она не запускает binary и не удостоверяет его
происхождение. Успешный checksum не закрывает live/safety gates. В исходном lock
команда ожидаемо завершается FAIL, пока реальные поля не заполнены.

Backend выбирается отдельно для целевого устройства. NVIDIA: CUDA-сборка;
альтернативный Vulkan проверяется отдельно. CPU допустим для диагностики, но
должен отмечаться degraded. Для собственной сборки официальные CMake options:
`-DGGML_CUDA=ON` либо `-DGGML_VULKAN=ON`. Сначала выбрать точный commit, затем
собирать по его документации с нужными SDK/toolchain. Проверить offload в server
logs, выделенную VRAM, драйвер и отсутствие скрытого CPU fallback.
Основание: [llama.cpp build](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)
и [server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
сверены 2026-10-31. Конкретная сборка здесь не проверена; её `--help` имеет приоритет
над примером flags в runbook. Inference слушает только 127.0.0.1.

## Supervisor samples

`ops/windows/Start-Pozor.ps1` запускает Bot, Llm или Backup из правильного working
directory через подготовленную `.venv`. Llm сначала проверяет local artifacts;
context/alias берёт из lock, slots=1/non-thinking. Не выполняет install/download.
`Register-PozorTasks.ps1` — **пример для просмотра и ручного запуска**:

```powershell
.\ops\windows\Register-PozorTasks.ps1 -Repository (Get-Location).Path -TaskPrefix Pozor
# Лишь после проверки конфигурации оператор явно запускает задачи в UI Task Scheduler.
```

Он регистрирует три задачи, сам их не запускает: Bot/Llm при sign-in текущего
пользователя, Backup ежедневно 03:00 по локальному времени машины. Hidden окна,
IgnoreNew, максимум три restart с интервалом минута; существующие имена отклоняются.
Для режима без входа пользователя/при boot нужен отдельный service account и
проверенная политика GPU-доступа: этот sample такой режим не обещает. Stop-ScheduledTask
может убить процесс принудительно; crash recovery обязателен и проверен offline.
Для штатного завершения ручного запуска используйте Ctrl+C. После смены .env или
lock перезапустите обе задачи; не запускайте одновременно manual poller и task.
Bot health-loop терпит отсутствие LLM, команды stop/ingestion работают отдельно.

Проверка на машине оператора: task history/LastTaskResult, отсутствие второго
процесса, отказ сервера/бота и restart с backoff, перезагрузка/sign-in, backup по
расписанию. Логи не перенаправляются в файлы sample-ом: используйте защищённый
log sink с retention 7 дней, без токенов и текстов. `status` не заменяет task history.
Samples `ops/systemd/*.service` показывают Linux-вариант с отдельным пользователем,
UMask=0077, restart/backoff и localhost. Они не устанавливались и не проверялись
на Linux; After/Wants задают порядок, а не readiness — её проверяет приложение.

## Финальная приёмка и передача

Offline проверка Windows: frozen install в отдельную пустую venv, CLI help,
миграция с нуля/повтор, doctor с synthetic token, pytest, Ruff/mypy, wheel/sdist и
ресурсы wheel; результаты команд фиксируются ниже. Это не установка на чистую ОС.
`test_operations.py` проверяет WAL/uncommitted writes, restore recovery,
checksum/schema/FK/FTS rejects, rotation после успеха, safe CLI и artifact hashes.
`test_delivery.py` расширяет import → реальный HTTP adapter с MockTransport →
production sender → backup → restore → no resend/сохранённый FTS. Fake Telegram и
ответы HTTP существуют только в тестах. Continuation/stop, pending/crash/restart
также покрыты T5 suite. Физическая GPU/Telegram этим не проверяются.

Следующему исполнителю с разрешённой тестовой группой и готовыми весами:

1. Завершить T6 human evaluation (≥6/8, evidence ≥80%, ноль critical violations),
   не считать offline candidate coverage результатом модели.
2. Заполнить exact deployment-lock и hardware; выполнить `deployment check`,
   `llm smoke`, live benchmark `--live --repeat 3`; сохранить p50/p95, output lengths,
   queue wait, concurrency 2/4, VRAM, slot release после отмены/background activity.
3. Включить allowlist/test chat, `/enable_chat`, `/roast_on`. Импортировать выбранную
   **синтетическую** историю через preview/apply. Проверить контекстный callback,
   target continuation, `/stop` во время inference, отсутствие позднего панча,
   неверный target и изоляцию тем. Не использовать рассылку.
4. Перезапустить процесс после отправки и в pending состоянии: нет повторного
   панча/исполнения старого trigger. Выполнить backup/restore smoke на отдельный путь;
   проверить imported evidence, consent/tombstone и manual rollback.
5. Проверить supervisor на целевой машине и private ACL/retention. Зафиксировать
   реальные результаты и только тогда решать о закрытии T6/T7.

На момент коммита final acceptance **не пройден** из-за невыполненных live/human
gates. Контракты T0–T6, реальные aiogram/httpx adapters и эталон reference сохранены.

## Фактические проверки этой итерации

Windows, Python 3.12.9, uv 0.11.7. Начальная ветка `codex/t3-import`, HEAD
`f0a4987`, рабочее дерево чистое. Изменения T7 — в `codex/t7-operations`.

| Команда / проверка | Результат |
| --- | --- |
| `$env:UV_PROJECT_ENVIRONMENT='.test-tmp/t7-clean'; uv sync --frozen --offline --cache-dir .uv-cache --extra dev --extra imports` | Первый проход отказал: mypy отсутствовал в локальном кэше |
| Та же команда без `--offline` | Успешно, отдельная новая venv, 45 pinned packages; только Python dependencies, без весов |
| `.test-tmp\t7-clean\Scripts\python -m pytest -q --basetemp=.test-tmp/t7-clean-tests` | **228 passed**, 37.09 с |
| `.venv\Scripts\python -m pytest -q --basetemp=.test-tmp/t7-final` | **228 passed**, 39.91 с |
| `.test-tmp\t7-clean\Scripts\ruff check src/pozor tests/test_operations.py tests/test_delivery.py` | All checks passed |
| `.test-tmp\t7-clean\Scripts\mypy src/pozor` | Success, 20 source files |
| `pozor --help`, `doctor` из новой venv | OK; временный DATABASE_PATH и synthetic BOT_TOKEN, сети нет |
| `pozor db migrate`, повтор, `status`, `db backup` из новой venv | 0001–0008 применены, повтор current, status/backup OK |
| `pozor benchmark --suite tests/fixtures/eval.jsonl --output .test-tmp/t7-offline-benchmark.json` | 30 cases, retrieval_pass=true, candidate coverage=1.0; human/model pending |
| `uv build --offline --cache-dir .uv-cache --out-dir .test-tmp/t7-dist` | Wheel/sdist собраны |
| Проверка архивов через zipfile/tarfile | В wheel T7 modules/0008/prompt; в sdist supervisor samples; `.env` и локальные caches исключены |
| PowerShell AST parser для обоих `ops/windows/*.ps1` | Синтаксис OK; скрипты не исполнялись |
| `git diff --check` | OK (Git предупреждает о Windows LF→CRLF conversion) |

Полный pytest, CLI benchmark и build выполнены с разрешённым выходом из Windows
sandbox из-за ограничений asyncio/socketpair/PE resources. Это не live smoke.
Единственный новый сетевой install — закреплённые Python packages через uv.

Изменённые файлы: `README.md`, `deployment-lock.json`, `docs/04-runbook.md`,
`docs/05-tasks.md`, этот документ; `src/pozor/cli.py`, `operations.py`,
`deployment.py`; `tests/test_operations.py`, `tests/test_delivery.py`;
`ops/windows/Start-Pozor.ps1`, `Register-PozorTasks.ps1`;
`ops/systemd/pozor-bot.service`, `pozor-llm.service`.
