# Deploy System

Дашборд (FastAPI) + автобот, который берёт задачи из Jira, подбирает ПК в Jira Insight,
обновляет ansible-инвентарь и запускает `ansible-playbook`.

## Запуск

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить токены, DASH_USER / DASH_PASS
uvicorn mm_server:app --host 127.0.0.1 --port 8000 --workers 1
```

* **Ровно один worker.** Блокировка «один деплой за раз» и планировщик живут в памяти процесса.
* Логин/пароль дашборда — `DASH_USER` / `DASH_PASS` (по умолчанию `admin` / `changeme`; при слабом пароле сервис стартует, но пишет предупреждение в лог).
* Выставляйте сервис наружу только через reverse proxy с HTTPS (nginx/caddy) — Basic Auth по HTTP передаёт пароль открытым текстом.
* Готовые примеры: [deploy/deploy-system.service](deploy/deploy-system.service), [Dockerfile](Dockerfile).
* Бот включается тумблером в настройках дашборда; вручную один тик: `python bot.py`.

## Безопасность

* Все изменяющие запросы требуют CSRF-токен (форма или заголовок `X-CSRF-Token`).
* Имена ПК проверяются regex `^A\d{2,6}$`, теги — `^[\w-]+(,[\w-]+)*$`,
  настраиваемые аргументы Ansible — белый список (`--tags`, `--skip-tags`, `-f`). См. [validation.py](validation.py).
* 5 неудачных входов с одного IP → блокировка на 5 минут (`AUTH_MAX_FAILURES`, `AUTH_LOCKOUT_SECONDS`).
* Аудит: в таблице `deploy_logs` сохраняются `triggered_by` и итоговая `command`.

## Наблюдаемость

* `GET /health` — liveness, `GET /metrics` — метрики Prometheus (под Basic Auth).
* Watchdog раз в 5 минут шлёт алерт в Mattermost о деплоях, висящих дольше `ANSIBLE_TIMEOUT + WATCHDOG_GRACE_SECONDS`.
* Лог приложения ротируется (10 МБ × 5).

## Структура

| Файл | Назначение |
|------|-----------|
| `mm_server.py` | HTTP-слой: роуты, шаблоны, планировщик |
| `bot.py` | Логика обработки задач Jira (сценарии A/B/C) |
| `deploy_service.py` | Единый запуск деплоя: lock, сборка команд, записи в БД |
| `ansible_runner.py` | Запуск/отмена/восстановление процессов ansible |
| `inventory_manager.py` | Атомарная правка ansible-инвентаря (flock) |
| `validation.py`, `security.py`, `statuses.py`, `settings_store.py` | Валидация, auth/CSRF, статусы, настройки |
| `config.py` | Все настройки из `.env` (проект Jira, эпики, поля, GPU и т.д.) |

## Разработка

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```
