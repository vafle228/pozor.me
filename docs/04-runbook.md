# Запуск и эксплуатация

## 1. Что понадобится

- Постоянно включённая машина, SSD, Python 3.12+, ориентировочно 16–32 ГБ RAM, GPU 16 ГБ VRAM. Свободное место для весов и экспортов проверять отдельно.
- Для NVIDIA — подходящий драйвер и CUDA-сборка llama.cpp; для другой GPU сначала smoke Vulkan/backend. Автоматическое падение на CPU считать degraded, а не успешной проверкой скорости.
- Bot token от BotFather; username вроде `@pozor_me_bot`; numeric OWNER_IDS и CHAT_ALLOWLIST.
- Telegram Desktop для первоначального экспорта JSON. В проекте нет необходимости логиниться личным аккаунтом через Telethon.

## 2. Закрепление зависимостей

Исполнитель создаёт `pyproject.toml`, `uv.lock`. Runtime: aiogram 3, aiosqlite, httpx, pydantic-settings, typer; для больших экспортов ijson. Dev: pytest, pytest-asyncio, ruff, mypy. Не требуются LangChain/LlamaIndex, torch в процессе бота, Redis, PostgreSQL. SQL-миграции нумерованные с checksum; откат версии приложения с несовместимой схемой запрещён.

Пиновать проверенные версии, llama.cpp build SHA/release, model repo+revision+filename+SHA256 в `deployment-lock.json`. Не выдумывать lockfile в плане; собрать и проверить в T0/T4. Не скачивать случайные GGUF «uncensored» как решение качества.

## 3. Последовательность будущего запуска

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Заполнить .env локально, не присылать токен в чат и не коммитить.
uv run pozor doctor
uv run pozor db migrate
```

`.env.example` должен содержать:

```dotenv
BOT_TOKEN=replace_me
OWNER_IDS=[123456789]
CHAT_ALLOWLIST=[-1001234567890]
DATABASE_PATH=./data/pozor.sqlite3
LLM_BASE_URL=http://127.0.0.1:8080
LLM_MODEL=pozor-local
LLM_API_KEY=
CONTEXT_SIZE=8192
MAX_OUTPUT_TOKENS=180
GENERATION_DEADLINE_SECONDS=8
LIVE_RETENTION_DAYS=30
IMPORT_RETENTION_DAYS=90
LOG_LEVEL=INFO
```

Пример **планируемого** запуска бинарника llama-server в PowerShell; закреплённая сборка обязана пройти `--help` и smoke, т.к. флаги меняются:

```powershell
$env:LLAMA_ARG_CHAT_TEMPLATE_KWARGS = '{"enable_thinking":false}'
.\vendor\llama.cpp\llama-server.exe -m .\models\Qwen3-8B-Q4_K_M.gguf -ngl 99 -c 8192 -np 1 --jinja --host 127.0.0.1 --port 8080 --alias pozor-local
```

Файл модели должен реально существовать под этим локальным именем; repo/revision/checksum хранит deployment-lock. Не подразумевается, что точное имя remote artifact такое же. Проверить offload всех слоёв, `/health`, `/v1/models`, structured completion и отсутствие thinking в сыром ответе/расходе токенов. Само отсутствие `<think>` в отображаемом тексте ещё не доказывает отсутствие скрытых вычислений. [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

```powershell
uv run pozor llm smoke
uv run pozor benchmark --suite tests/fixtures/eval.jsonl --repeat 3
uv run pozor run
```

Сейчас реализованы `pozor --help`, локальный `pozor doctor`, SQLite-миграции `pozor db migrate` и `pozor run`. `run` — opt-in сетевая команда: она синхронизирует `CHAT_ALLOWLIST` в отдельный признак `allowed`, сохраняет `enabled` выключенным для новых чатов, затем использует aiogram `getUpdates`. До включения чата в inbox проходят только control-команды и callback-кнопки из allowlist; обычный текст не сохраняется. Команды T2 включают/выключают чат, регистрируют согласие, разрешают ID и создают ограниченную сессию. `/roast` пока не генерирует реплику; LLM, доставка панча и продолжение диалога — T4–T5. Конфликт активного webhook или другого poller завершается понятной ошибкой; webhook автоматически не удаляется. `llm smoke` и `benchmark` относятся к будущим пакетам. Запуск и диагностика не скачивают веса автоматически.

## 4. Подключение группы и импорт

1. Создать бота, отключить privacy mode. Проверить получение обычного текста, а не только slash-команд, в отдельной группе.
2. Добавить chat ID в allowlist, выполнить `/enable_chat` от администратора. Авторизация проверяет Telegram user ID и актуальные admin rights. OWNER_IDS управляют локальным развёртыванием; group admin — только своей группой.
3. Target включает `/roast_on`; `/whois` reply даёт ID для импорта. Пока участия нет, вызов возвращает одну инструкцию о включении и не создаёт session.
4. Экспорт → preview → apply → `profile show`, как в документе 02. Первая память работает до долгого сбора новых сообщений.
5. `/roast` reply создаёт сессию с ограничениями и кнопками stop/finish. Проверить continuation и генерацию ответа после реализации T5.

## 5. Постоянная работа

Windows: два отдельных процесса под Планировщиком заданий или service wrapper; запуск при старте, restart on failure с backoff, скрытые окна. Linux: две systemd services с зависимостью готовности LLM и Restart=on-failure. Для V1 предпочесть native запуск на машине с GPU; Docker Compose необязателен, чтобы не добавлять проблемы GPU passthrough на Windows.

llama-server слушает localhost. Если позже приложение в контейнере — localhost контейнера не хост: адрес и firewall задать явно. Не открывать inference в интернет. Telegram требует исходящий интернет; локальная LLM означает отсутствие внешнего AI API, а не полностью offline-бота.

Graceful shutdown: остановить intake → сохранить checkpoint → закрыть pending сессии/отменить jobs → ограниченно дождаться sender → закрыть DB/HTTP. После рестарта старые active sessions закрыть `restart`, не продолжать вчерашнюю прожарку. Updates старше 30 секунд архивировать при соблюдении retention, но команды/продолжения из них не запускать. Время future >30 секунд — reject trigger как clock anomaly; message всё равно можно сохранить после проверки даты.

Backup через SQLite backup API, не копированием живого одного .sqlite без WAL. Ротация 7 дней, owner-only ACL, restore smoke на отдельный путь. Экспорты не помещать в обычные логи/репозиторий. Следить за ростом DB, удалением staging, свободным диском.

## 6. Наблюдаемость

Логировать request/session/batch IDs, event kind, durations, counts и error codes. Не текст переписки, token, prompt или полный response. Debug dump только отдельным явным offline режимом на синтетических данных.

Метрики: received/processed updates, update lag, inbox backlog, import rejects, retrieval ms, input/output tokens, queue wait, time to final text, stale results dropped, LLM errors, outbox unknown, rate limits, active sessions, SQLite busy, retention deletes. На первом этапе structured logs + `pozor status` достаточно, Prometheus/Grafana не обязательны.

## 7. Проверка качества и приёмка

Автотесты без Telegram/GPU: fake clock, fake Bot API, fake LLM, temporary DB. Затем отдельный opt-in live smoke с настоящими credentials. CI не должен скачивать веса.

Замороженный набор минимум 30 синтетических кейсов: 10 с хорошими callback-ами, 5 без истории, 5 с похожими именами/ложными упоминаниями, 5 adversarial/injection, 5 с остановкой и задержками. Не зашивать единственную ожидаемую формулировку шутки.

Ручные оценки 0–2: связь с текущей репликой, уместный callback, краткость, юмор; отдельно pass/fail: атрибуция, отсутствие выдуманных фактов/секретов, остановка. Gate: все identity/isolation/stop tests pass; ноль критических нарушений на наборе; ≥80% сценариев с релевантной историей получают правильный evidence; средняя ручная оценка ≥6/8. Если не прошёл — менять retrieval/prompt/model, не ослаблять provenance.

Latency: 3 warm-up, затем минимум 30 запросов на фиксированном prompt suite, output lengths сохранить; p50/p95, queue wait, generation, VRAM, GPU/backend, exact model/build. Отдельно 2 и 4 concurrent requests и background import/profile. Цель одиночного запроса из документа 03; concurrent backlog не скрывать средним. Импорт не должен блокировать polling/stop более 200 мс в локальном stress test; file parsing вынести в thread/process или читать потоково с yielding.

Готовность к использованию: новый чат получает новый message, импорт находит конкретную старую цитату, неправильный target отвергается, шутка действительно использует контекст, `/stop` во время inference отменяет отправку, restart не дублирует update, rollback очищает память, token отсутствует в логах. Проверить вручную в тестовой группе; отсутствие реальной проверки явно отметить.
