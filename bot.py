import html
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import telebot
from telebot import types

VERSION = "103-ws"
METHOD = "chacha20-ietf-poly1305"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

PUBLIC_HOST = (
    os.getenv("WS_PUBLIC_HOST")
    or os.getenv("PUBLIC_HOST")
    or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    or ""
).strip()

PUBLIC_HOST = (
    PUBLIC_HOST
    .replace("https://", "")
    .replace("http://", "")
    .rstrip("/")
)

PUBLIC_PORT = int(os.getenv("PORT", "8080"))
OUTLINE_WS_PORT = int(os.getenv("OUTLINE_WS_PORT", "9000"))

STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))
if not STATE_DIR.exists():
    STATE_DIR = Path("/tmp")

STATE_FILE = STATE_DIR / "vpn_state_v103_ws.json"
SERVER_CONFIG = Path("/tmp/outline-ss-server-ws.yaml")
ACCESS_YAML = Path("/tmp/outline-access.yaml")
NGINX_CONFIG = Path("/tmp/nginx-outline-ws.conf")
SS_LOG = Path("/tmp/outline-ss-server.log")
NGINX_LOG = Path("/tmp/nginx-outline.log")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

ss_proc: Optional[subprocess.Popen] = None
nginx_proc: Optional[subprocess.Popen] = None


def _rand_password() -> str:
    return secrets.token_urlsafe(24)


def _rand_path() -> str:
    return secrets.token_urlsafe(24).replace("-", "_")


def load_state() -> Dict[str, str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    changed = False

    if not data.get("password"):
        data["password"] = _rand_password()
        changed = True

    if not data.get("secret_path"):
        data["secret_path"] = _rand_path()
        changed = True

    data["mode"] = "websocket"

    if changed:
        save_state(data)

    return data


def save_state(data: Dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def build_server_yaml(state: Dict[str, str]) -> str:
    secret = state["secret_path"]
    password = state["password"]

    return f"""web:
  servers:
    - id: railway_ws
      listen:
        - "127.0.0.1:{OUTLINE_WS_PORT}"

services:
  - listeners:
      - type: websocket-stream
        web_server: railway_ws
        path: "/{secret}/tcp"
      - type: websocket-packet
        web_server: railway_ws
        path: "/{secret}/udp"
    keys:
      - id: user-1
        cipher: {METHOD}
        secret: "{password}"
"""


def build_client_yaml(state: Dict[str, str]) -> str:
    secret = state["secret_path"]
    password = state["password"]
    host = PUBLIC_HOST or "SET_RAILWAY_PUBLIC_DOMAIN"

    return f"""transport:
  $type: tcpudp
  tcp:
    $type: shadowsocks
    endpoint:
      $type: websocket
      url: wss://{host}/{secret}/tcp
    cipher: {METHOD}
    secret: "{password}"
  udp:
    $type: shadowsocks
    endpoint:
      $type: websocket
      url: wss://{host}/{secret}/udp
    cipher: {METHOD}
    secret: "{password}"
"""


def dynamic_key(state: Dict[str, str]) -> str:
    host = PUBLIC_HOST or "SET_RAILWAY_PUBLIC_DOMAIN"
    return f"ssconf://{host}/{state['secret_path']}/access.yaml"


def write_runtime_files() -> None:
    state = load_state()
    secret = state["secret_path"]

    SERVER_CONFIG.write_text(build_server_yaml(state), "utf-8")
    ACCESS_YAML.write_text(build_client_yaml(state), "utf-8")

    nginx = f"""
worker_processes 1;
daemon off;
pid /tmp/nginx-outline.pid;

events {{
  worker_connections 1024;
}}

http {{
  access_log /dev/stdout;
  error_log /dev/stderr info;

  map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
  }}

  server {{
    listen 0.0.0.0:{PUBLIC_PORT};
    server_name _;

    location = /healthz {{
      default_type text/plain;
      return 200 "ok\\n";
    }}

    location = / {{
      default_type text/plain;
      return 200 "Outline WS server v{VERSION} OK\\n";
    }}

    location = /{secret}/access.yaml {{
      default_type text/yaml;
      add_header Access-Control-Allow-Origin "*" always;
      add_header Cache-Control "no-store" always;
      alias {ACCESS_YAML};
    }}

    location = /{secret}/tcp {{
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto https;
      proxy_read_timeout 900s;
      proxy_send_timeout 900s;
      proxy_pass http://127.0.0.1:{OUTLINE_WS_PORT};
    }}

    location = /{secret}/udp {{
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto https;
      proxy_read_timeout 900s;
      proxy_send_timeout 900s;
      proxy_pass http://127.0.0.1:{OUTLINE_WS_PORT};
    }}
  }}
}}
"""

    NGINX_CONFIG.write_text(nginx, "utf-8")


def stop_proc(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def start_services() -> None:
    global ss_proc, nginx_proc

    write_runtime_files()

    stop_proc(ss_proc)
    stop_proc(nginx_proc)

    SS_LOG.write_text("", "utf-8")
    NGINX_LOG.write_text("", "utf-8")

    ss_log = open(SS_LOG, "ab", buffering=0)
    nginx_log = open(NGINX_LOG, "ab", buffering=0)

    ss_proc = subprocess.Popen(
        ["outline-ss-server", f"-config={SERVER_CONFIG}"],
        stdout=ss_log,
        stderr=subprocess.STDOUT,
    )

    nginx_proc = subprocess.Popen(
        ["nginx", "-c", str(NGINX_CONFIG), "-p", "/tmp"],
        stdout=nginx_log,
        stderr=subprocess.STDOUT,
    )

    time.sleep(1)


def ensure_services() -> None:
    global ss_proc, nginx_proc

    if ss_proc is None or ss_proc.poll() is not None:
        start_services()
        return

    if nginx_proc is None or nginx_proc.poll() is not None:
        start_services()
        return


def rotate_key() -> None:
    state = load_state()
    state["password"] = _rand_password()
    state["secret_path"] = _rand_path()
    save_state(state)
    start_services()


def tail(path: Path, n: int = 25) -> str:
    if not path.exists():
        return "нет лога"

    lines = path.read_text("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]) if lines else "лог пуст"


def keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🌐 Мой WS-ключ")
    kb.row("📋 YAML", "📊 Статус")
    kb.row("🛠 Debug", "🔁 Рестарт WS")
    kb.row("🔑 Создать / обновить ключ")
    kb.row("❓ Инструкция")
    return kb


def code(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_services()

    text = (
        "🌐 <b>Outline WebSocket VPN</b>\n\n"
        f"Версия: {VERSION}\n"
        "Режим: Shadowsocks-over-WebSocket\n\n"
        "TCP идёт через:\n"
        "wss://.../tcp\n\n"
        "UDP/DNS идёт через:\n"
        "wss://.../udp\n\n"
        "Нажми <b>🌐 Мой WS-ключ</b> и добавь его в Outline Client."
    )

    bot.send_message(message.chat.id, text, reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "🌐 Мой WS-ключ")
def ws_key(message):
    ensure_services()

    state = load_state()
    key = dynamic_key(state)

    warn = ""
    if not PUBLIC_HOST:
        warn = (
            "\n\n⚠️ Нет RAILWAY_PUBLIC_DOMAIN.\n"
            "В Railway включи Settings → Networking → Public Networking → Generate Domain."
        )

    text = (
        "🌐 <b>Dynamic Outline key</b>\n\n"
        + code(key)
        + "\nДобавь этот ключ через <b>+</b> в Outline Client.\n"
        "Нужен Outline Client 1.15.0+."
        + warn
    )

    bot.send_message(message.chat.id, text, reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "📋 YAML")
def show_yaml(message):
    ensure_services()

    yaml_text = build_client_yaml(load_state())
    bot.send_message(
        message.chat.id,
        "📋 <b>Client YAML</b>\n\n" + code(yaml_text),
        reply_markup=keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📊 Статус")
def status(message):
    ensure_services()

    state = load_state()

    ss_status = "работает" if ss_proc and ss_proc.poll() is None else "остановлен"
    nginx_status = "работает" if nginx_proc and nginx_proc.poll() is None else "остановлен"

    host = PUBLIC_HOST or "HOST"

    text = f"""📊 <b>Статус VPN</b>

Версия: {VERSION}
Режим: Shadowsocks-over-WebSocket

Public host: {PUBLIC_HOST or 'нет'}
Public port: {PUBLIC_PORT}
outline-ss-server: {ss_status}
nginx: {nginx_status}

TCP endpoint:
wss://{host}/{state['secret_path']}/tcp

UDP/DNS endpoint:
wss://{host}/{state['secret_path']}/udp

Dynamic key:
{dynamic_key(state)}
"""

    bot.send_message(message.chat.id, text, reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "🛠 Debug")
def debug(message):
    ensure_services()

    state = load_state()

    ss_pid = ss_proc.pid if ss_proc and ss_proc.poll() is None else "нет"
    nginx_pid = nginx_proc.pid if nginx_proc and nginx_proc.poll() is None else "нет"

    debug_text = f"""🛠 Debug

VERSION={VERSION}
MODE=websocket

PUBLIC_HOST={PUBLIC_HOST or 'нет'}
PORT={PUBLIC_PORT}
OUTLINE_WS_PORT={OUTLINE_WS_PORT}

RAILWAY_PUBLIC_DOMAIN={os.getenv('RAILWAY_PUBLIC_DOMAIN', 'нет')}
RAILWAY_TCP_PROXY_DOMAIN={os.getenv('RAILWAY_TCP_PROXY_DOMAIN', 'нет')}
RAILWAY_TCP_PROXY_PORT={os.getenv('RAILWAY_TCP_PROXY_PORT', 'нет')}

secret_path=/{state['secret_path']}
dynamic_key={dynamic_key(state)}

ss_pid={ss_pid}
nginx_pid={nginx_pid}

--- outline-ss-server log ---
{tail(SS_LOG, 25)}

--- nginx log ---
{tail(NGINX_LOG, 25)}
"""

    bot.send_message(message.chat.id, code(debug_text), reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "🔁 Рестарт WS")
def restart(message):
    start_services()
    bot.send_message(message.chat.id, "🔁 WebSocket VPN перезапущен", reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "🔑 Создать / обновить ключ")
def update_key(message):
    rotate_key()

    state = load_state()
    key = dynamic_key(state)

    text = (
        "🔑 Новый WS-ключ создан.\n\n"
        "Старый сервер в Outline нужно удалить и добавить этот ключ заново:\n\n"
        + code(key)
    )

    bot.send_message(message.chat.id, text, reply_markup=keyboard())


@bot.message_handler(func=lambda m: m.text == "❓ Инструкция")
def instructions(message):
    text = """❓ Инструкция

1. Railway → Settings → Networking → Public Networking → Generate Domain.
2. Дождись, пока Railway создаст публичный домен.
3. Сделай Redeploy.
4. В Telegram нажми /start.
5. Нажми 🌐 Мой WS-ключ.
6. В Outline Client удали старый Railway Outline VPN.
7. Нажми + и вставь ssconf:// ключ.
8. Подключись.

Важно:
- TCP Proxy в этом режиме не используется.
- Нужен Outline Client 1.15.0+.
- Весь трафик идёт через wss:// по TCP/HTTPS.
- Если нажал “Создать / обновить ключ”, старый сервер в Outline надо удалить и добавить новый ключ.
"""

    bot.send_message(message.chat.id, html.escape(text), reply_markup=keyboard())


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, "Выбери действие на клавиатуре 👇", reply_markup=keyboard())


if __name__ == "__main__":
    start_services()
    print(f"Bot v{VERSION} started. Public host={PUBLIC_HOST or 'missing'}")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)