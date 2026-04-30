# Личный Outline-compatible VPN на Railway + Telegram-бот

Это шаблон для Railway, который запускает:

- Shadowsocks-сервер внутри Railway-контейнера;
- Telegram-бота, который выдаёт `ss://` ключ для Outline Client;
- кнопки для генерации ключа, показа ключа, статуса, ping и рестарта.

> Важно: это не классический WireGuard/OpenVPN и не полный Outline Server Manager с REST API. Это Outline-compatible Shadowsocks через Railway TCP Proxy. Для личного использования в Outline Client этого обычно достаточно для TCP-трафика. Публичный UDP на Railway через TCP Proxy не работает.

## 1. Создай Telegram-бота

1. Открой `@BotFather`.
2. Создай бота командой `/newbot`.
3. Скопируй токен и добавь его в Railway Variables как `BOT_TOKEN`.

## 2. Разверни на Railway

1. Создай GitHub-репозиторий из этих файлов.
2. В Railway создай новый Project → Deploy from GitHub repo.
3. Добавь переменные:

```env
BOT_TOKEN=токен_от_BotFather
ADMIN_TELEGRAM_IDS=твой_telegram_id
SS_PORT=8388
SS_METHOD=chacha20-ietf-poly1305
SS_TIMEOUT=300
SS_KEY_NAME=Railway Outline VPN
ENABLE_UDP=false
STATE_PATH=/data/outline_state.json
```

`ADMIN_TELEGRAM_IDS` можно оставить пустым. Тогда первый пользователь, который нажмёт `/start`, станет владельцем бота.

## 3. Включи TCP Proxy в Railway

1. Открой Railway → твой Service → Settings → Networking.
2. Нажми **TCP Proxy**.
3. Введи внутренний порт:

```text
8388
```

4. Railway выдаст внешний адрес примерно такого вида:

```text
roundhouse.proxy.rlwy.net:11105
```

5. Сделай **Redeploy**, чтобы переменные `RAILWAY_TCP_PROXY_DOMAIN` и `RAILWAY_TCP_PROXY_PORT` появились внутри контейнера.

Если бот всё равно пишет, что TCP Proxy не настроен, добавь вручную:

```env
SS_PUBLIC_HOST=roundhouse.proxy.rlwy.net
SS_PUBLIC_PORT=11105
```

## 4. Получи ключ в Telegram

1. Открой своего Telegram-бота.
2. Нажми `/start`.
3. Нажми **📋 Мой Outline-ключ**.
4. Скопируй `ss://...`.
5. Вставь его в **Outline Client**.

## Кнопки бота

- **🔑 Создать / обновить ключ** — создаёт новый пароль, перезапускает VPN. Старый ключ перестаёт работать.
- **📋 Мой Outline-ключ** — показывает текущий ключ.
- **📊 Статус** — показывает порт, endpoint, метод, uptime.
- **🏓 Ping** — показывает отклик бота и версию `101`.
- **🔁 Рестарт VPN** — перезапускает только Shadowsocks-процесс.
- **♻️ Рестарт контейнера** — завершает процесс, Railway поднимает контейнер заново.
- **❓ Инструкция** — краткая инструкция внутри Telegram.

## Максимальная скорость

Рекомендуемые настройки:

```env
SS_METHOD=chacha20-ietf-poly1305
SS_TIMEOUT=300
ENABLE_UDP=false
```

Для максимальной скорости выбери Railway-регион ближе к себе и не включай лишние плагины/обфускацию. Скорость будет зависеть от региона Railway, маршрута до `*.proxy.rlwy.net`, лимитов твоего тарифного плана и сети клиента.

## Сохранение ключа после redeploy

По умолчанию Railway filesystem может быть временным. Чтобы ключ не менялся после redeploy:

1. Добавь Railway Volume в настройках Railway.
2. Примонтируй его в `/data`.
3. Оставь переменную:

```env
STATE_PATH=/data/outline_state.json
```

В Dockerfile специально нет команды `VOLUME`, потому что Railway её не поддерживает. Используй только Railway Volumes через интерфейс Railway.

## Проверка без Telegram

В логах Railway должна появиться строка:

```text
Starting bot version 101. Shadowsocks internal port: 8388
```

Если `ss-server` не стартует, смотри Railway Logs.
