import asyncio
import configparser
import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from typing import List, Tuple
from zoneinfo import ZoneInfo

# Third-party imports
import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
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
    def __init__(
        self,
        config_file="autoregexbot.cfg",
        example_file="autoregexbot.cfg.example",
        secrets_file="secrets.cfg",
    ):
        self.config_file = config_file
        self.example_file = example_file
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
        self._last_example_mtime = 0
        self.load_config()

    def load_config(self):
        try:
            # Load example defaults first, then local config overrides
            files_to_read = []
            if os.path.exists(self.example_file):
                files_to_read.append(self.example_file)
                self._last_example_mtime = os.path.getmtime(self.example_file)

            if os.path.exists(self.config_file):
                files_to_read.append(self.config_file)
                self._last_mtime = os.path.getmtime(self.config_file)

            if not files_to_read:
                logger.warning("No configuration files found. Using hardcoded defaults.")
                return

            self.config.read(files_to_read)

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
            self.remind_include_link = self.config.getboolean(
                "bot", "remind_include_link", fallback=True
            )
            self.process_whole_message = self.config.getboolean(
                "bot", "process_whole_message", fallback=False
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

            logger.info(f"Configuration loaded. {len(self.rules)} rules active.")

        except Exception as e:
            logger.error(f"Error loading configuration: {e}")

    def set_and_save(self, section, key, value):
        """Updates a setting in memory and saves it to the local config file."""
        try:
            if not self.config.has_section(section):
                self.config.add_section(section)
            
            # Convert bool to string for configparser
            val_str = str(value).lower() if isinstance(value, bool) else str(value)
            self.config.set(section, key, val_str)

            with open(self.config_file, "w") as f:
                self.config.write(f)
            
            # Re-sync local variables
            self.load_config()
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False

    def check_hot_reload(self):
        try:
            reload_needed = False
            
            if os.path.exists(self.example_file):
                if os.path.getmtime(self.example_file) != self._last_example_mtime:
                    reload_needed = True
            
            if os.path.exists(self.config_file):
                if os.path.getmtime(self.config_file) != self._last_mtime:
                    reload_needed = True

            if reload_needed:
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
    matched = False
    response_text = ""

    if cfg.process_whole_message:
        # 1. Process whole message
        new_text = text
        for pattern, replacement in cfg.rules:
            if pattern.search(new_text):
                try:
                    new_text = pattern.sub(replacement, new_text)
                    matched = True
                except re.error as e:
                    logger.error(f"Regex error: {e}")
        response_text = new_text
    else:
        # 2. Only process URLs and send them
        # Extract things that look like URLs
        urls = re.findall(r"https?://[^\s]+", text)
        processed_urls = []

        for url in urls:
            new_url = url
            url_matched = False
            for pattern, replacement in cfg.rules:
                if pattern.search(new_url):
                    try:
                        new_url = pattern.sub(replacement, new_url)
                        url_matched = True
                        matched = True
                    except re.error as e:
                        logger.error(f"Regex error on URL {url}: {e}")
            
            if url_matched:
                processed_urls.append(new_url)
        
        if processed_urls:
            response_text = "\n".join(processed_urls)

    if not matched or response_text == text or not response_text:
        return

    processed_messages.add(message.message_id)
    user_cooldowns[user.id] = now_ts

    # SANITIZATION
    safe_user_name = html.escape(user.first_name)

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


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /version command."""
    if not check_access(update):
        return

    # Try ENV first (for CalVer), then metadata, then default to Unknown
    pkg_version = os.environ.get("BOT_VERSION")
    if not pkg_version:
        try:
            pkg_version = metadata.version("telegram-autoregexbot")
        except metadata.PackageNotFoundError:
            pkg_version = "Unknown"

    commit_sha = os.environ.get("VERSION", "Unknown")

    response = "<b>Telegram AutoRegex Bot</b>\n"
    response += f"Version: <code>{pkg_version}</code>\n"
    response += f"Commit: <code>{commit_sha}</code>"

    await update.message.reply_text(response, parse_mode=ParseMode.HTML)


def parse_duration(duration_str: str) -> int:
    """Parses a duration string (e.g., 2h, 15m, 1d) and returns total seconds."""
    total_seconds = 0
    pattern = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
    matches = pattern.findall(duration_str)

    if not matches:
        return 0

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}

    for amount, unit in matches:
        total_seconds += int(amount) * multipliers[unit.lower()]

    return total_seconds


async def schedule_reminder(
    context: ContextTypes.DEFAULT_TYPE, seconds: int, job_data: dict
):
    """Background task to wait and send the reminder."""
    await asyncio.sleep(seconds)

    chat_id = job_data["chat_id"]
    user_id = job_data["user_id"]
    message_id = job_data["message_id"]
    reason = job_data["reason"]
    link = job_data.get("link")

    mention = f"<a href='tg://user?id={user_id}'>Reminder</a>"
    text = f"🔔 {mention}"
    if reason:
        text += f": {reason}"

    if link:
        text += f"\n\n🔗 <a href='{link}'>Original Message</a>"

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=message_id,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /remindme command."""
    if not check_access(update):
        return

    message = update.effective_message
    args_str = " ".join(context.args)

    if not args_str:
        await message.reply_text(
            "Usage: /remindme [time] (reason)\nExample: /remindme 2h (laundry)"
        )
        return

    # Extract reason in parentheses
    reason = ""
    reason_match = re.search(r"\((.*)\)", args_str)
    if reason_match:
        reason = reason_match.group(1)
        # Remove reason from args to parse duration
        duration_part = args_str.replace(reason_match.group(0), "").strip()
    else:
        duration_part = args_str

    seconds = parse_duration(duration_part)

    if seconds <= 0:
        await message.reply_text(
            "❌ Invalid time format. Use something like 30m, 2h, 1d."
        )
        return

    remind_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    # Prepare data for the task
    job_data = {
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id,
        "message_id": message.message_id,
        "reason": reason,
    }

    if cfg.remind_include_link and message.reply_to_message:
        reply = message.reply_to_message
        if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            # Construct link for groups
            chat_id_str = str(update.effective_chat.id).replace("-100", "")
            link = f"https://t.me/c/{chat_id_str}/{reply.message_id}"
            job_data["link"] = link

    # Launch background task
    asyncio.create_task(schedule_reminder(context, seconds, job_data))

    # Confirmation
    iso_time_utc = remind_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Calculate IST time (defaulting to IST as user timezone isn't provided by Telegram API)
    ist_time = remind_time.astimezone(ZoneInfo("Asia/Kolkata"))
    ist_str = ist_time.strftime("%Y-%m-%d %H:%M:%S")

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds_left = divmod(remainder, 60)
    t_minus = f"{int(hours):02}:{int(minutes):02}:{int(seconds_left):02}"

    confirm_text = (
        "✅⏰\n"
        f"I'll remind you at <code>{iso_time_utc}</code> (UTC)\n"
        f"Local time (IST): <code>{ist_str}</code>\n"
        f"T-minus: <code>{t_minus}</code>"
    )
    await message.reply_text(confirm_text, parse_mode=ParseMode.HTML)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the settings menu."""
    if not check_access(update):
        return

    # Only allow whitelisted users or admins to change settings
    user = update.effective_user
    if user.id not in cfg.whitelist_users:
        # Check if admin if in group
        is_admin = False
        if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
            is_admin = member.status in ["administrator", "creator"]
        
        if not is_admin:
            await update.message.reply_text("⛔ Access denied. Only whitelisted users can change settings.")
            return

    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if cfg.send_as_reply else '❌'} Reply to original",
                callback_data="set:bot:send_as_reply",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if cfg.mention_user else '❌'} Mention User",
                callback_data="set:bot:mention_user",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if cfg.process_whole_message else '❌'} Process Whole Msg",
                callback_data="set:bot:process_whole_message",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if cfg.enable_delete_button else '❌'} Delete Button",
                callback_data="set:bot:enable_delete_button",
            )
        ],
        [InlineKeyboardButton("Close", callback_data="set:close")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("<b>Bot Settings</b>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles settings button clicks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "set:close":
        await query.message.delete()
        return

    if not data.startswith("set:"):
        return

    # Format: set:section:key
    parts = data.split(":")
    section = parts[1]
    key = parts[2]

    # Get current value
    current_val = getattr(cfg, key, None)
    if isinstance(current_val, bool):
        new_val = not current_val
        if cfg.set_and_save(section, key, new_val):
            # Refresh keyboard
            await settings_command(update, context)
            # Remove the old settings message to avoid clutter
            await query.message.delete()


async def post_init(application: Application):
    """Sets the bot commands for autocomplete."""
    commands = [
        BotCommand("version", "Show bot version and commit hash"),
        BotCommand("remindme", "Set a reminder. Usage: /remindme 2h (reason)"),
        BotCommand("settings", "Configure bot settings (Whitelisted only)"),
    ]
    await application.bot.set_my_commands(commands)


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
        .request(request)
        .post_init(post_init)
        .build()
    )

    # 3. Add Handlers
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("remindme", remind_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        CallbackQueryHandler(handle_delete_callback, pattern="^del:")
    )
    application.add_handler(
        CallbackQueryHandler(handle_settings_callback, pattern="^set:")
    )

    print("Bot is running. Press Ctrl+C to stop.")

    # 4. Run Polling
    # bootstrap_retries=-1 means it will keep trying to connect forever at startup if internet is down
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
