# Telemetry-сервер StepBot

Принимает крэш-репорты со всех клиентских установок StepBot: агрегирует ошибки по fingerprint, показывает дашборд, шлёт алерты в Telegram про новые ошибки и регрессии. Один файл `server.py`, SQLite, никакой внешней БД.

## Деплой

```bash
cd telemetry-server
cat > .env <<'EOF'
TS_DASH_USER=admin
TS_DASH_PASS=поменяй-меня
TS_INGEST_TOKEN=длинный-случайный-токен
EOF
docker compose up -d --build
```

Проверка: `curl http://127.0.0.1:8088/health` → `{"ok":true}`. База лежит в `./data/telemetry.db`.

## Переменные окружения

| Переменная | Обязательна | Что делает |
|---|---|---|
| `TS_DASH_USER` / `TS_DASH_PASS` | да (для дашборда) | Basic Auth на `/`. Без них дашборд отдаёт 503 |
| `TS_INGEST_TOKEN` | **да** | `POST /ingest` требует заголовок `X-Telemetry-Token` с этим значением. Без токена endpoint отвечает 503 (fail-closed). Обойти можно только явным `TS_ALLOW_ANONYMOUS=1` (не рекомендуется) |
| `TS_TG_BOT_TOKEN` + `TS_TG_CHAT_ID` | нет | Алерты в Telegram: 🆕 новая ошибка, ♻️ регрессия решённой |
| `TS_DB_PATH` | нет | Путь к SQLite, в контейнере уже `/data/telemetry.db` |

## nginx для errors.<домен>

```nginx
server {
    server_name errors.example.com;
    listen 80;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Required: the per-IP rate limit reads X-Forwarded-For's first hop. Without it
        # every install shares one 127.0.0.1 bucket and legit telemetry gets 429'd.
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 2m;
    }
}
```

HTTPS: `certbot --nginx -d errors.example.com`.

## Дашборд

`https://errors.example.com/` под Basic Auth. Сверху счётчики open/resolved/installs, ниже таблица ошибок (свежие сверху). Клик по `traceback / context / events` в строке раскрывает трейс, контекст и последние события. Кнопка `resolve` закрывает ошибку — она уходит из списка, но вернётся сама (и придёт алерт про регрессию), если снова прилетит с установок. `/?all=1` показывает и решённые.

## Telegram-алерты

Создай бота у @BotFather, добавь его в чат/канал, возьми chat_id (`getUpdates` или @userinfobot). В `.env`:

```bash
TS_TG_BOT_TOKEN=123456:AA...
TS_TG_CHAT_ID=-1001234567890
```

`docker compose up -d` после правки `.env`. Алерты приходят только на новые fingerprint и регрессии, не на каждое событие — спама не будет.
