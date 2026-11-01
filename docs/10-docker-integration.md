# T7: Docker и live-проверки

Контейнерный запуск: NVIDIA GPU, Docker Desktop с WSL2/GPU support (Windows)
либо Docker Engine с NVIDIA Container Toolkit (Linux). Веса Qwen3-8B Q4_K_M
скачиваются при сборке отдельного LLM-образа, входят в его слой и проверяются
по SHA256. Требуется место для 5 ГБ весов, build cache и runtime images.
В Git веса не входят. Ревизия модели и checksum заданы в
`ops/docker/download_model.py`; runtime и Python/uv images закреплены по digest.
Обновлять их нужно явно с повторной приёмкой.

```powershell
docker compose build
docker compose up -d llm
docker compose run --rm --no-deps bot llm smoke
docker compose run --rm --no-deps bot telegram smoke
# Одно тихое синтетическое сообщение в первой включённой группе реестра, затем удаление:
docker compose run --rm --no-deps bot telegram smoke --send
docker compose up -d bot
docker compose ps
```

Не запускайте одновременно второй poller через `pozor run` или старый supervisor.
Compose передаёт `.env` только боту во время запуска. Никогда не публикуйте
`docker compose config` без `--quiet`, полный `docker inspect` контейнера бота,
дампы окружения или `.env`. Docker administrators могут читать environment
контейнера; runtime-файлы и доступ к Docker должны оставаться приватными.
В build context действует allowlist; `.env`, data и локальные файлы исключены.

База сохраняется в `./data/pozor.sqlite3`; путь внутри контейнера переопределён
на `/data/pozor.sqlite3`. Имеющуюся БД из другого DATABASE_PATH нужно переносить
отдельно через backup/restore, а не копировать работающую SQLite.
LLM слушает 8080 внутри сети Compose, с хоста доступен на 127.0.0.1:18080.
Контекст 8192, один slot, thinking отключён. `LLM_API_KEY` для этого локального
сервера не требуется. Log rotation: три файла по 10 МБ на сервис.

```powershell
docker compose exec bot pozor status
docker compose exec bot pozor db backup --directory /data/backups
docker compose stop bot
# Новый destination, исходная база остаётся нетронутой:
docker compose run --rm --no-deps bot db restore --source /data/backups/<snapshot>.sqlite3 --destination /data/recovery.sqlite3
docker compose down
```

`restart: unless-stopped` перезапускает упавший процесс, но unhealthy сам по себе
не вызывает restart. Бот отдельно проверяет готовность LLM. Docker Desktop должен
стартовать после входа пользователя; конфигурация запуска ОС здесь не меняется.
Backup запускается явно; периодический backup требует отдельного расписания.

Официальное описание runtime: [llama.cpp Docker](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md).
Контейнерный runtime проверяется отдельно от Windows executable в старом
`deployment-lock.json`; поле `llama_cpp_artifact` нельзя заполнять digest образа
вместо SHA256 исполняемого файла.

## Исправления контрольной точки

- Backup rotation проверяет точный формат имени и пропускает каталоги.
- Live benchmark учитывает generation deadline из Settings и возвращает failure
  при fallback модели даже при успешном retrieval.
- Offline pytest отключает загрузку `.env` и удаляет настройки приложения из
  окружения тестов. Реальные интеграции запускаются отдельными командами.
- Telegram smoke проверяет авторизацию, отсутствие webhook, доступ к группам,
  опционально отправку и удаление сообщения. Ошибки не раскрывают API payload.
- Ошибка Telegram API при старте бота завершается краткой диагностикой без traceback.
- Doctor использует уникальный временный файл вместо перезаписи фиксированного имени.
- `reasoning_format=none` приводил к HTTP 400 при structured output Qwen в
  b11718; adapter использует `auto`, thinking отключён, проверка отсутствия
  reasoning content/tokens сохранена. В Compose используется `--reasoning off`.
- Реальные aiogram.Update сериализуются с `by_alias=True`: иначе `from_user`
  не попадал в ожидаемое поле `from`, и команды молча игнорировались. Регрессии
  используют настоящие Update с reply/callback и проходят через poller → pipeline.
- Модель иногда включала @упоминания, что закономерно отклонял валидатор.
  Адресация команды удалена из caller hint; system prompt и validation retry
  явно требуют ответа без упоминаний. Строгий валидатор сохранён. Причины retry
  и fallback логируются кодами, без prompt, ответов модели или Telegram IDs.

T6 human quality и полный интерактивный Telegram-сценарий остаются отдельными
критериями: доступность API и структурированный ответ LLM их не заменяют.

## Фактические результаты 2026-10-31

- Windows / Docker Desktop Linux / RTX 5060 Ti 16 ГБ, CUDA runtime b11718-547208dca.
- Образы собраны; GGUF 5 027 783 488 байт скачан и SHA256 проверен внутри build.
- Telegram getMe/getWebhookInfo/getChat/getChatMember и sendMessage/deleteMessage:
  успешно с хоста и из контейнера после исправления пользователем allowlist.
- Structured non-thinking smoke: успешно с хоста и из контейнера.
- [Live benchmark](../reports/t7-live-benchmark.json): 30 synthetic cases,
  75 генераций, без fallback; p50 612.52 мс, p95 794.84 мс, candidate и generated
  evidence-ID coverage 1.0. Совпадение ID не доказывает фактологическую точность текста.
- [Очередь/GPU](../reports/t7-gpu-queue.json): 2/4 одновременно, без fallback;
  четыре запроса завершились за 2375 мс, максимальное ожидание 1891 мс.
  Отмена ограниченного production-запроса: busy наблюдался, slot свободен через 610 мс.
- Отдельный запрос на 2048 токена после disconnect не освободил slot за 8 с:
  non-streaming HTTP cancellation не гарантирует немедленной остановки GPU.
  Production использует 180 токенов и wait-idle перед следующим заданием.
- GPU device виден в контейнере; суммарная занятая VRAM около 7241 МиБ
  (включая рабочий стол, это не точный per-process peak).
- Контейнерная БД: migration, status, online backup и restore в новый путь прошли.
- `.env` и `.git` отсутствуют в образе; `docker compose config --quiet` проходит.
- Полный pytest: **248 passed**; Ruff и mypy проходят. Тесты не читают `.env`.
- Пользовательский сценарий `/enable_chat` → `/roast_on` → `/roast` прошёл после
  исправления aiogram aliases и @упоминаний: настоящий ответ модели отправлен.
  `/stop` закрыл сессию как `explicit`. После restart/backup/restore число sent
  не выросло, pending turns не возобновлялись. Тексты и IDs в отчёт не включены.
- Расширенная групповая приёмка (несколько людей/тем, `/stop` именно во время
  inference) и T6 human scoring ещё не проведены; offline-regressions их не заменяют.

Воспроизведение измерений с хоста (без чтения `.env` для queue probe):

```powershell
$env:LLM_BASE_URL='http://127.0.0.1:18080'
uv run pozor llm smoke
uv run pozor benchmark --suite tests/fixtures/eval.jsonl --output reports/t7-live-benchmark.json --live --repeat 3
uv run python ops/docker/check_gpu.py http://127.0.0.1:18080 reports/t7-gpu-queue.json
```
