import asyncio
import configparser
import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Tuple

# Third-party imports
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# --- Logging Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Silence httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Configuration Management ---


class ConfigManager:
    def __init__(self, config_file="autoregexbot.cfg", secrets_file="secrets.cfg"):
        self.config_file = config_file
        self.secrets_file = secrets_file

        # Disable interpolation to allow % characters
        self.config = configparser.ConfigParser(interpolation=None)

        # 1. Load Secrets
        self.config.read([secrets_file])
        self.token = os.environ.get(
            "BOT_TOKEN", self.config.get("secrets", "token", fallback=None)
        )

        if not self.token or self.token == "YOUR_BOT_TOKEN":
            logger.critical(
                "No valid bot token found in secrets.cfg or Environment Variables."
            )
            sys.exit(1)

        # 2. Load Dynamic Config
        self._last_mtime = 0
        self.load_config()

    def load_config(self):
        try:
            if not os.path.exists(self.config_file):
                logger.warning(
                    f"Config file {self.config_file} not found. Using defaults."
                )
                return

            self.config.read(self.config_file)

            # Bot Settings
            self.send_as_reply = self.config.getboolean(
                "bot", "send_as_reply", fallback=True
            )
            self.mention_user = self.config.getboolean(
                "bot", "mention_user", fallback=True
            )
            self.enable_delete_button = self.config.getboolean(
                "bot", "enable_delete_button", fallback=True
            )
            self.delete_allowed = self.config.get(
                "bot", "delete_allowed", fallback="sender_or_admin"
            )
            self.cooldown_seconds = self.config.getfloat(
                "bot", "cooldown_seconds", fallback=2.0
            )

            # Access Control
            self.access_policy = self.config.get(
                "access", "access_policy", fallback="off"
            ).lower()
            self.allow_chat_types = self._get_list("access", "allow_chat_types")
            self.deny_chat_types = self._get_list("access", "deny_chat_types")
            self.whitelist_chats = self._get_int_list("access", "whitelist_chats")
            self.blacklist_chats = self._get_int_list("access", "blacklist_chats")
            self.whitelist_users = self._get_int_list("access", "whitelist_users")
            self.blacklist_users = self._get_int_list("access", "blacklist_users")

            # Rules
            self.rules = self._parse_rules()

            self._last_mtime = os.path.getmtime(self.config_file)
            logger.info(f"Configuration loaded. {len(self.rules)} rules active.")

        except Exception as e:
            logger.error(f"Error loading configuration: {e}")

    def check_hot_reload(self):
        try:
            if not os.path.exists(self.config_file):
                return

            current_mtime = os.path.getmtime(self.config_file)
            if current_mtime != self._last_mtime:
                logger.info("Config file change detected. Reloading...")
                self.load_config()
        except Exception as e:
            logger.error(f"Hot reload check failed: {e}")

    def _get_list(self, section, key):
        val = self.config.get(section, key, fallback="")
        return [x.strip().lower() for x in val.split(",")] if val else []

    def _get_int_list(self, section, key):
        val = self.config.get(section, key, fallback="")
        return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]

    def _parse_rules(self) -> List[Tuple[re.Pattern, str]]:
        rules = []
        if not self.config.has_section("substitutions"):
            return rules

        for key, val in self.config.items("substitutions"):
            if not val.startswith("s"):
                continue

            try:
                # Syntax: s@pattern@replacement@flags
                delimiter = val[1]
                parts = val.split(delimiter, 3)

                if len(parts) < 3:
                    continue

                pattern_str = parts[1]
                replacement_str = parts[2]
                flags_str = parts[3] if len(parts) > 3 else ""

                re_flags = 0
                if "i" in flags_str.lower():
                    re_flags |= re.IGNORECASE
                if "m" in flags_str.lower():
                    re_flags |= re.MULTILINE
                if "s" in flags_str.lower():
                    re_flags |= re.DOTALL

                compiled_pattern = re.compile(pattern_str, re_flags)
                rules.append((compiled_pattern, replacement_str))
            except Exception as e:
                logger.error(f"Failed to parse rule '{val}': {e}")

        return rules


cfg = ConfigManager()

# State Tracking
user_cooldowns = {}
processed_messages = set()

# --- Logic Functions ---


def check_access(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False

    # 1. Chat Type Check
    if cfg.allow_chat_types and chat.type not in cfg.allow_chat_types:
        if (
            chat.type == "supergroup"
            and "group" in cfg.allow_chat_types
            and "supergroup" not in cfg.allow_chat_types
        ):
            pass
        elif chat.type not in cfg.allow_chat_types:
            return False

    if cfg.deny_chat_types and chat.type in cfg.deny_chat_types:
        return False

    # 2. Access Policy
    if cfg.access_policy == "whitelist":
        if chat.id not in cfg.whitelist_chats and user.id not in cfg.whitelist_users:
            return False
    elif cfg.access_policy == "blacklist":
        if chat.id in cfg.blacklist_chats or user.id in cfg.blacklist_users:
            return False

    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler."""
    cfg.check_hot_reload()

    if not update.message or not update.message.text:
        return

    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    # Ignore self
    if user.id == context.bot.id:
        return

    # Ignore processed
    if message.message_id in processed_messages:
        return

    # Ignore old messages (>60s)
    if message.date:
        age = (datetime.now(timezone.utc) - message.date).total_seconds()
        if age > 60:
            return

    if not check_access(update):
        return

    # Cooldown
    now_ts = time.time()
    last_time = user_cooldowns.get(user.id, 0)
    if now_ts - last_time < cfg.cooldown_seconds:
        return

    # REGEX LOGIC
    text = message.text
    new_text = text
    matched = False

    for pattern, replacement in cfg.rules:
        if pattern.search(new_text):
            try:
                new_text = pattern.sub(replacement, new_text)
                matched = True
            except re.error as e:
                logger.error(f"Regex error: {e}")

    if not matched or new_text == text:
        return

    processed_messages.add(message.message_id)
    user_cooldowns[user.id] = now_ts

    # SANITIZATION
    safe_user_name = html.escape(user.first_name)
    response_text = new_text

    if cfg.mention_user:
        response_text = (
            f"<a href='tg://user?id={user.id}'>{safe_user_name}</a>: {response_text}"
        )

    reply_markup = None
    if cfg.enable_delete_button:
        callback_data = f"del:{user.id}"
        keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data=callback_data)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    # --- RETRY SEND LOGIC ---
    sent = False
    attempt = 0

    while not sent:
        try:
            attempt += 1
            if cfg.send_as_reply:
                await update.message.reply_text(
                    response_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=response_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
            logger.info(f"Rewrote message {message.message_id} for user {user.id}")
            sent = True

        except (
            NetworkError,
            TimedOut,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
        ) as e:
            logger.warning(
                f"Connection failed (Attempt {attempt}). Retrying in 5s... Error: {e}"
            )
            await asyncio.sleep(5)
            # Infinite loop implies we keep going.
            # If you want to give up eventually, add `if attempt > 10: break` here.

        except Exception as e:
            logger.error(f"Fatal error sending message: {e}")
            break  # Non-network error (e.g., Parsing error), stop retrying.


async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not query.data.startswith("del:"):
        await query.answer("Invalid request.")
        return

    try:
        original_sender_id = int(query.data.split(":")[1])
    except ValueError:
        await query.answer("Error parsing permission data.")
        return

    is_sender = user.id == original_sender_id
    is_admin = False

    if query.message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member = await context.bot.get_chat_member(query.message.chat_id, user.id)
            is_admin = member.status in ["administrator", "creator"]
        except Exception:
            pass

    allowed = False
    if cfg.delete_allowed == "sender" and is_sender:
        allowed = True
    elif cfg.delete_allowed == "admin" and is_admin:
        allowed = True
    elif cfg.delete_allowed == "sender_or_admin" and (is_sender or is_admin):
        allowed = True

    if allowed:
        try:
            await query.message.delete()
            await query.answer("Deleted.")
        except Exception:
            await query.answer(
                "Could not delete message (Bot needs Delete permissions)."
            )
    else:
        await query.answer("⛔ You cannot delete this message.", show_alert=True)


def main():
    if not cfg.token:
        return

    # 1. Configure Request with stable timeouts and HTTP/1.1
    # This forces the bot to use HTTP/1.1 (more stable on some networks) and waits 60s for connections
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60,
        read_timeout=60,
        write_timeout=60,
        http_version="1.1",  # Force HTTP/1.1 to avoid HTTP/2 stream errors
    )

    # 2. Build Application with the custom request
    application = (
        Application.builder()
        .token(cfg.token)
        .request(request)  # Pass the configured request object here
        .build()
    )

    # 3. Add Handlers
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        CallbackQueryHandler(handle_delete_callback, pattern="^del:")
    )

    print("Bot is running. Press Ctrl+C to stop.")

    # 4. Run Polling
    # bootstrap_retries=-1 means it will keep trying to connect forever at startup if internet is down
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
