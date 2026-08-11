# Деплой на VPS (systemd + nginx)

Альтернатива Docker-установке (`./scripts/install.sh`) для тех, кто хочет запускать
процессы напрямую через systemd. Проверено на Ubuntu 24.04, 1 vCPU / 1 GB RAM
(на 1 GB обязателен swap 2 GB).

| Что | Где |
|---|---|
| Админ-кабинет | `https://your-domain/admin/` (логин/пароль — `ADMIN__USERNAME`/`ADMIN__PASSWORD` из `.env`) |
| Мини-аппа | `https://your-domain/app/` (превью тем: `?mock=1&variant=a..h&mode=dark`) |
| Admin API | `https://your-domain/api/admin/…` |
| Cabinet API | `https://your-domain/api/cabinet/…` (auth `tma <initData>`) |
| Бот | long polling, menu button → мини-аппа |
| Мок-панель Remnawave | `127.0.0.1:3010` (сабки публично: `https://your-domain/sub/<short>`) |

## Раскладка на сервере

- `/opt/vpnshop/app` — код (+ `.venv`, `.env` с секретами, `admin/dist` — собранная SPA)
- `/opt/vpnshop/compose.dev.yml` — Postgres 16 + Redis 7 (docker, localhost-only)
- systemd: `vpnshop-web` (uvicorn :8000), `vpnshop-bot`, `vpnshop-worker`,
  `vpnshop-scheduler`, `vpnshop-mockpanel` (:3010) — все с `MemoryMax` под размер RAM
- nginx: 443 (LE-серт, авто-обновление certbot) → `/` → :8000, `/sub/` → :3010
  - **важно:** в server-блоке нужен `client_max_body_size 200m;` — иначе загрузка
    дампов на экране «Миграция» (.sql/.db) упирается в дефолтный лимит nginx 1 МБ и
    падает с «Failed to fetch». Приложение принимает до 200 МБ.

## Деплой обновлений

```bash
./scripts/deploy.sh user@your-server https://your-domain
# rsync + uv sync + alembic + restart + health
```

На сервер уходит rsync рабочей копии (git на сервере не нужен).
SPA собирается локально (`npm run build --prefix admin`) — Node на сервере не нужен.

## Замена мок-панели на живую Remnawave

В `/opt/vpnshop/app/.env`:

```
REMNAWAVE__BASE_URL=https://panel.example.com
REMNAWAVE__TOKEN=<api token>
```

и `systemctl restart vpnshop-web vpnshop-worker vpnshop-bot`. Больше ничего не меняется —
мок реализует те же эндпоинты/схемы, что использует клиент.

После первого запуска владелец может изменить подключение без SSH: откройте админку →
**Серверы** → **Подключение Remnawave**. URL, тип авторизации, токен и webhook-secret
сохраняются в зашифрованном виде в БД и применяются без перезапуска. Кнопка «Сбросить к
.env» возвращает значения из файла окружения.

## Логи

```bash
journalctl -u vpnshop-web -f      # api
journalctl -u vpnshop-bot -f      # бот
journalctl -u vpnshop-worker -f   # рассылки/бэкапы/синк
```
