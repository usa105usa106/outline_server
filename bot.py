#!/usr/bin/env python3
"""Personal Railway Outline-compatible VPN bot.

Runs a Shadowsocks server in the same Railway container and gives the owner an
Outline-compatible ss:// access key through Telegram.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

VERSION = "101"
DEFAULT_STATE_PATH = "/data/outline_state.json"
SUPPORTED_METHODS = {
    "chacha20-ietf-poly1305",
    "aes-128-gcm",
    "aes-256-gcm",
    "xchacha20-ietf-poly1305",
}


def getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_int_env(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = getenv(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def parse_admin_ids() -> set[int]:
    raw = getenv("ADMIN_TELEGRAM_IDS")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids


@dataclass
class State:
    password: str
    owners: list[int]
    created_at: int
    updated_at: int


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> State:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return State(
                    password=str(data.get("password") or self._new_password()),
                    owners=[int(x) for x in data.get("owners", [])],
                    created_at=int(data.get("created_at") or int(time.time())),
                    updated_at=int(data.get("updated_at") or int(time.time())),
                )
            except Exception:
                # Broken state should not prevent the VPN from starting.
                pass
        now = int(time.time())
        state = State(password=self._new_password(), owners=[], created_at=now, updated_at=now)
        self.state = state
        self.save()
        return state

    @staticmethod
    def _new_password() -> str:
        # URL-safe, high entropy, and safe for Shadowsocks command-line usage.
        return secrets.token_urlsafe(32)

    def save(self) -> None:
        self.state.updated_at = int(time.time())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def rotate_password(self) -> None:
        self.state.password = self._new_password()
        self.save()

    def add_owner(self, user_id: int) -> None:
        if user_id not in self.state.owners:
            self.state.owners.append(user_id)
            self.save()


class ShadowsocksService:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.process: Optional[subprocess.Popen] = None
        self.port = parse_int_env("SS_PORT", 8388, 1, 65535)
        self.timeout = parse_int_env("SS_TIMEOUT", 300, 10, 3600)
        self.method = getenv("SS_METHOD", "chacha20-ietf-poly1305")
        if self.method not in SUPPORTED_METHODS:
            self.method = "chacha20-ietf-poly1305"
        self.enable_udp = getenv("ENABLE_UDP", "false").lower() in {"1", "true", "yes", "on"}

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.is_running():
            return
        cmd = [
            "ss-server",
            "-s",
            "0.0.0.0",
            "-p",
            str(self.port),
            "-m",
            self.method,
            "-k",
            self.store.state.password,
            "-t",
            str(self.timeout),
        ]
        if self.enable_udp:
            # Railway TCP Proxy exposes TCP publicly; UDP is normally useful only on platforms that expose UDP.
            cmd.append("-u")
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.2)
        if self.process.poll() is not None:
            raise RuntimeError("ss-server не запустился. Проверь Railway Logs.")

    def stop(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except Exception:
                    pass
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def rotate_and_restart(self) -> None:
        self.store.rotate_password()
        self.restart()

    def public_endpoint(self) -> tuple[Optional[str], Optional[int]]:
        host = getenv("SS_PUBLIC_HOST") or getenv("RAILWAY_TCP_PROXY_DOMAIN")
        port_raw = getenv("SS_PUBLIC_PORT") or getenv("RAILWAY_TCP_PROXY_PORT")
        if not host:
            return None, None
        try:
            public_port = int(port_raw) if port_raw else self.port
        except ValueError:
            public_port = self.port
        return host, public_port

    def access_key(self) -> str:
        host, port = self.public_endpoint()
        if not host or not port:
            raise RuntimeError(
                "TCP Proxy ещё не настроен. В Railway открой Settings → Networking → TCP Proxy, "
                f"укажи внутренний порт {self.port}, затем сделай Redeploy."
            )
        userinfo = f"{self.method}:{self.store.state.password}".encode("utf-8")
        encoded = base64.urlsafe_b64encode(userinfo).decode("ascii").rstrip("=")
        tag = quote(getenv("SS_KEY_NAME", "Railway Outline VPN"), safe="")
        return f"ss://{encoded}@{host}:{port}/?outline=1#{tag}"


store = StateStore(getenv("STATE_PATH", DEFAULT_STATE_PATH))
ss = ShadowsocksService(store)
ADMIN_IDS = parse_admin_ids()
STARTED_AT = time.time()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 Создать / обновить ключ", callback_data="new_key")],
            [InlineKeyboardButton("📋 Мой Outline-ключ", callback_data="my_key")],
            [InlineKeyboardButton("📊 Статус", callback_data="status"), InlineKeyboardButton("🏓 Ping", callback_data="ping")],
            [InlineKeyboardButton("🔁 Рестарт VPN", callback_data="restart_vpn")],
            [InlineKeyboardButton("♻️ Рестарт контейнера", callback_data="restart_container")],
            [InlineKeyboardButton("❓ Инструкция", callback_data="help")],
        ]
    )


def user_allowed(user_id: int) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    if not store.state.owners:
        store.add_owner(user_id)
        return True
    return user_id in store.state.owners


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if user_allowed(user.id):
        return True
    text = "⛔️ Доступ запрещён. Этот VPN-бот уже привязан к владельцу."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message:
        await update.message.reply_text(text)
    return False


async def send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


def help_text() -> str:
    return (
        "<b>Личный VPN на Railway для Outline Client</b>\n\n"
        "1. В Railway открой <b>Settings → Networking → TCP Proxy</b>.\n"
        f"2. Укажи внутренний порт <code>{ss.port}</code>.\n"
        "3. Сделай redeploy, чтобы появились переменные "
        "<code>RAILWAY_TCP_PROXY_DOMAIN</code> и <code>RAILWAY_TCP_PROXY_PORT</code>.\n"
        "4. Нажми <b>📋 Мой Outline-ключ</b> и вставь ключ в Outline Client.\n\n"
        "Это Outline-compatible Shadowsocks-сервер через Railway TCP Proxy. "
        "Публичный UDP на Railway обычно недоступен, поэтому режим рассчитан на TCP-трафик."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        ss.start()
    except Exception as e:
        await send_or_edit(update, f"⚠️ VPN-сервер не запустился:\n<code>{html.escape(str(e))}</code>", main_keyboard())
        return
    owner_note = "\n\n✅ Ты назначен владельцем этого бота." if not ADMIN_IDS and update.effective_user and update.effective_user.id in store.state.owners else ""
    await send_or_edit(
        update,
        f"✅ <b>Outline-compatible VPN запущен</b>\nВерсия: <code>{VERSION}</code>{owner_note}",
        main_keyboard(),
    )


async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await show_key(update, rotate=False)


async def cmd_new_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await show_key(update, rotate=True)


async def show_key(update: Update, rotate: bool = False) -> None:
    try:
        if rotate:
            ss.rotate_and_restart()
        else:
            ss.start()
        key = ss.access_key()
        title = "🔑 <b>Новый Outline-ключ создан</b>" if rotate else "📋 <b>Твой Outline-ключ</b>"
        await send_or_edit(
            update,
            f"{title}\n\n<code>{html.escape(key)}</code>\n\n"
            "Вставь этот ключ в приложение <b>Outline Client</b>.",
            main_keyboard(),
        )
    except Exception as e:
        await send_or_edit(update, f"⚠️ Не удалось выдать ключ:\n<code>{html.escape(str(e))}</code>", main_keyboard())


async def show_status(update: Update) -> None:
    running = ss.is_running()
    host, port = ss.public_endpoint()
    uptime = int(time.time() - STARTED_AT)
    endpoint = f"{host}:{port}" if host and port else "TCP Proxy ещё не настроен"
    try:
        key_hint = ss.access_key()[:18] + "…"
    except Exception:
        key_hint = "нет"
    text = (
        "📊 <b>Статус VPN</b>\n\n"
        f"Версия: <code>{VERSION}</code>\n"
        f"VPN-процесс: <code>{'работает' if running else 'остановлен'}</code>\n"
        f"Внутренний порт: <code>{ss.port}</code>\n"
        f"Публичный endpoint: <code>{html.escape(endpoint)}</code>\n"
        f"Метод: <code>{html.escape(ss.method)}</code>\n"
        f"Ключ: <code>{html.escape(key_hint)}</code>\n"
        f"Uptime бота: <code>{uptime} сек.</code>"
    )
    await send_or_edit(update, text, main_keyboard())


async def do_ping(update: Update) -> None:
    started = time.perf_counter()
    running = ss.is_running()
    elapsed_ms = (time.perf_counter() - started) * 1000
    text = (
        "🏓 <b>Ping</b>\n\n"
        f"Ответ бота: <code>{elapsed_ms:.2f} ms</code>\n"
        f"Версия: <code>{VERSION}</code>\n"
        f"VPN: <code>{'работает' if running else 'остановлен'}</code>"
    )
    await send_or_edit(update, text, main_keyboard())


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await update.callback_query.answer()
    if not await guard(update):
        return
    data = update.callback_query.data
    if data == "new_key":
        await show_key(update, rotate=True)
    elif data == "my_key":
        await show_key(update, rotate=False)
    elif data == "status":
        await show_status(update)
    elif data == "ping":
        await do_ping(update)
    elif data == "restart_vpn":
        try:
            ss.restart()
            await send_or_edit(update, "🔁 <b>VPN-процесс перезапущен.</b>", main_keyboard())
        except Exception as e:
            await send_or_edit(update, f"⚠️ Не удалось перезапустить VPN:\n<code>{html.escape(str(e))}</code>", main_keyboard())
    elif data == "restart_container":
        await send_or_edit(update, "♻️ <b>Контейнер перезапускается...</b>", None)
        asyncio.create_task(force_container_restart())
    elif data == "help":
        await send_or_edit(update, help_text(), main_keyboard())


async def force_container_restart() -> None:
    await asyncio.sleep(1.0)
    # Non-zero exit makes Railway restart the service instead of treating it as a completed worker.
    os._exit(1)


async def post_init(app: Application) -> None:
    # Start the VPN process when the bot starts, so the TCP proxy is ready even before a button is pressed.
    ss.start()


def main() -> None:
    token = getenv("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN is required", file=sys.stderr)
        sys.exit(1)

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("key", cmd_key))
    app.add_handler(CommandHandler("newkey", cmd_new_key))
    app.add_handler(CallbackQueryHandler(callback))
    print(f"Starting bot version {VERSION}. Shadowsocks internal port: {ss.port}", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
