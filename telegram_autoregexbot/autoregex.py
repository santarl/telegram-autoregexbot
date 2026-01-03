import asyncio
import configparser
import html
import logging
import os
import re
import sqlite3
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

# --- Database Management ---


class DatabaseManager:
    def __init__(self, db_path="reminders.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    user_name TEXT,
                    message_id INTEGER,
                    remind_time TIMESTAMP,
                    reason TEXT,
                    link TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.commit()

    def set_state(self, key, value):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()

    def get_state(self, key):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def clear_state(self, key):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM state WHERE key = ?", (key,))
            conn.commit()

    def add_reminder(self, chat_id, user_id, user_name, message_id, remind_time, reason, link):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO reminders (chat_id, user_id, user_name, message_id, remind_time, reason, link)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, user_name, message_id, remind_time.isoformat(), reason, link),
            )
            conn.commit()
            return cursor.lastrowid

    def delete_reminder(self, reminder_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            conn.commit()

    def get_pending_reminders(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reminders")
            return cursor.fetchall()

    def get_user_reminders(self, chat_id, user_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM reminders WHERE chat_id = ? AND user_id = ? ORDER BY remind_time ASC",
                (chat_id, user_id),
            )
            return cursor.fetchall()

    def get_chat_reminders(self, chat_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM reminders WHERE chat_id = ? ORDER BY remind_time ASC",
                (chat_id,),
            )
            return cursor.fetchall()


db = DatabaseManager()

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
            self.disabled_rules = self._get_list("bot", "disabled_rules")

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

    def toggle_rule(self, rule_key):
        """Toggles a rule between enabled and disabled."""
        current_disabled = self._get_list("bot", "disabled_rules")
        if rule_key in current_disabled:
            current_disabled.remove(rule_key)
        else:
            current_disabled.append(rule_key)
        
        return self.set_and_save("bot", "disabled_rules", ",".join(current_disabled))

    def add_rule(self, key, value):
        """Adds or updates a substitution rule."""
        return self.set_and_save("substitutions", key, value)

    def delete_rule(self, key):
        """Removes a substitution rule."""
        try:
            if self.config.has_option("substitutions", key):
                self.config.remove_option("substitutions", key)
                
                # Also clean up from disabled_rules if it was there
                current_disabled = self._get_list("bot", "disabled_rules")
                if key in current_disabled:
                    current_disabled.remove(key)
                    self.config.set("bot", "disabled_rules", ",".join(current_disabled))

                with open(self.config_file, "w") as f:
                    self.config.write(f)
                self.load_config()
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete rule: {e}")
            return False

    def reset_to_defaults(self):
        """Resets the local config by copying from the example file."""
        try:
            import shutil
            if os.path.exists(self.example_file):
                shutil.copy(self.example_file, self.config_file)
                self.load_config()
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to reset config: {e}")
            return False

    def get_all_substitution_keys(self):
        """Returns all keys in the substitutions section that look like rules."""
        if not self.config.has_section("substitutions"):
            return []
        return [
            key
            for key, val in self.config.items("substitutions")
            if val.startswith("s")
        ]

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

            if key in self.disabled_rules:
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

    # --- RESTORE CONFIG LOGIC ---
    if update.message and update.message.document and context.user_data.get("awaiting_config"):
        doc = update.message.document
        if doc.file_name == "autoregexbot.cfg" or doc.file_name.endswith(".cfg"):
            if not check_access(update):
                return
            
            # Check for whitelist/admin
            user = update.effective_user
            is_whitelisted = user.id in cfg.whitelist_users
            is_admin = False
            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
                is_admin = member.status in ["administrator", "creator"]
            
            if not is_whitelisted and not is_admin:
                return

            try:
                new_file = await context.bot.get_file(doc.file_id)
                await new_file.download_to_drive(cfg.config_file)
                cfg.load_config()
                context.user_data["awaiting_config"] = False
                await update.message.reply_text("✅ <b>Configuration restored successfully.</b>", parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                await update.message.reply_text(f"❌ <b>Failed to restore configuration:</b> {e}", parse_mode=ParseMode.HTML)
                return

    if not update.message or not update.message.text:
        return

    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    # --- SETTINGS INPUT LOGIC ---
    if context.user_data.get("awaiting_rule"):
        # Check if it's the same user who initiated
        text = message.text.strip()
        if "=" in text:
            try:
                key, val = text.split("=", 1)
                key = key.strip()
                val = val.strip()
                if val.startswith("s"):
                    if cfg.add_rule(key, val):
                        await message.reply_text(f"✅ Rule <code>{key}</code> added/updated.", parse_mode=ParseMode.HTML)
                        context.user_data["awaiting_rule"] = False
                        return
                
                await message.reply_text("❌ Invalid format. Use: <code>name = s@pattern@replacement@flags</code>", parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                await message.reply_text(f"❌ Error: {e}")
                return
        
        await message.reply_text("❌ Invalid format. Use: <code>name = s@pattern@replacement@flags</code>")
        return

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

    reminder_id = job_data.get("reminder_id")
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
        if reminder_id:
            db.delete_reminder(reminder_id)
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /remindme command."""
    if not check_access(update):
        return

    message = update.effective_message
    user = update.effective_user
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

    # Prepare link
    link = None
    if cfg.remind_include_link and message.reply_to_message:
        reply = message.reply_to_message
        if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            chat_id_str = str(update.effective_chat.id).replace("-100", "")
            link = f"https://t.me/c/{chat_id_str}/{reply.message_id}"

    # Save to Database
    reminder_id = db.add_reminder(
        chat_id=update.effective_chat.id,
        user_id=user.id,
        user_name=user.first_name,
        message_id=message.message_id,
        remind_time=remind_time,
        reason=reason,
        link=link,
    )

    # Prepare data for the task
    job_data = {
        "reminder_id": reminder_id,
        "chat_id": update.effective_chat.id,
        "user_id": user.id,
        "message_id": message.message_id,
        "reason": reason,
        "link": link,
    }

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
        f"I'll try to remind you at <code>{iso_time_utc}</code> (UTC)\n"
        f"Local time (IST): <code>{ist_str}</code>\n"
        f"T-minus: <code>{t_minus}</code>"
    )
    await message.reply_text(confirm_text, parse_mode=ParseMode.HTML)


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows pending reminders for the user in the current chat."""
    if not check_access(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    rows = db.get_user_reminders(chat_id, user_id)
    if not rows:
        await update.message.reply_text("You have no pending reminders in this chat.")
        return

    text = "<b>Your Pending Reminders:</b>\n\n"
    for row in rows:
        remind_time = datetime.fromisoformat(row["remind_time"]).astimezone(ZoneInfo("Asia/Kolkata"))
        time_str = remind_time.strftime("%Y-%m-%d %H:%M:%S")
        reason = f" ({row['reason']})" if row["reason"] else ""
        text += f"• <code>{time_str}</code>{reason}\n"

    keyboard = [[InlineKeyboardButton("🗑 Manage / Delete", callback_data="rem:manage")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def reminders_manage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a list of user reminders with delete buttons."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    rows = db.get_user_reminders(chat_id, user_id)
    if not rows:
        text = "You have no pending reminders."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    keyboard = []
    for row in rows:
        remind_time = datetime.fromisoformat(row["remind_time"]).astimezone(ZoneInfo("Asia/Kolkata"))
        time_str = remind_time.strftime("%d/%m %H:%M")
        reason = row["reason"][:15] + ".." if row["reason"] and len(row["reason"]) > 15 else (row["reason"] or "No reason")
        
        keyboard.append([
            InlineKeyboardButton(
                f"🗑 {time_str} - {reason}",
                callback_data=f"rem:del:{row['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("Close", callback_data="rem:close")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "<b>Manage Your Reminders</b>\nTap a reminder to delete it:"
    
    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles reminder management callbacks."""
    query = update.callback_query
    
    data = query.data
    if data == "rem:close":
        await query.answer("Menu closed.")
        await query.message.delete()
        return
    
    if data == "rem:manage":
        await query.answer("Loading reminders...")
        await reminders_manage_menu(update, context)
        return
        
    if data.startswith("set:"): # Guard for safety if needed, but pattern handles it
        await query.answer()
        return

    # rem:del:id
    parts = data.split(":")
    if len(parts) == 3 and parts[1] == "del":
        reminder_id = int(parts[2])
        db.delete_reminder(reminder_id)
        await query.answer("🗑 Reminder deleted!")
        # Refresh the menu
        await reminders_manage_menu(update, context)
    else:
        await query.answer()


async def reminders_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all pending reminders in the current chat."""
    if not check_access(update):
        return

    chat_id = update.effective_chat.id
    
    rows = db.get_chat_reminders(chat_id)
    if not rows:
        await update.message.reply_text("There are no pending reminders in this chat.")
        return

    text = "<b>All Pending Reminders:</b>\n\n"
    for row in rows:
        remind_time = datetime.fromisoformat(row["remind_time"]).astimezone(ZoneInfo("Asia/Kolkata"))
        time_str = remind_time.strftime("%Y-%m-%d %H:%M:%S")
        reason = f" ({row['reason']})" if row["reason"] else ""
        text += f"• {row['user_name']}: <code>{time_str}</code>{reason}\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the settings menu."""
    if not check_access(update):
        return

    # Only allow whitelisted users or admins to change settings
    user = update.effective_user
    query = update.callback_query

    if user.id not in cfg.whitelist_users:
        # Check if admin if in group
        is_admin = False
        if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
            is_admin = member.status in ["administrator", "creator"]
        
        if not is_admin:
            msg = "⛔ Access denied. Only whitelisted users can change settings."
            if query:
                await query.answer(msg, show_alert=True)
            else:
                await update.message.reply_text(msg)
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
        [
            InlineKeyboardButton(
                "📂 Individual Rules (Substitutions)",
                callback_data="set:menu:subs",
            )
        ],
        [
            InlineKeyboardButton(
                "📤 Backup Config",
                callback_data="set:action:backup",
            ),
            InlineKeyboardButton(
                "📥 Restore Config",
                callback_data="set:action:restore_prompt",
            )
        ],
        [
            InlineKeyboardButton(
                "⚠️ Reset to Defaults",
                callback_data="set:menu:reset_confirm",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Restart Bot",
                callback_data="set:menu:restart_confirm",
            )
        ],
        [InlineKeyboardButton("Close", callback_data="set:close")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "<b>Bot Settings</b>"
    
    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def restart_confirmation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a confirmation menu before restarting the container."""
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🔄 YES, RESTART NOW", callback_data="set:action:restart_do")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="set:menu:main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "<b>🔄 RESTART BOT</b>\n\n"
        "This will terminate the current process. Docker will automatically restart the container.\n\n"
        "The bot will be offline for a few seconds. Proceed?"
    )
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def reset_confirmation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a scary confirmation menu before resetting."""
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🔥 YES, RESET EVERYTHING", callback_data="set:action:reset_do")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="set:menu:main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "<b>⚠️ WARNING: RESET TO DEFAULTS</b>\n\n"
        "This will delete ALL your custom rules and settings in <code>autoregexbot.cfg</code> "
        "and restore everything from the example file.\n\n"
        "<b>This cannot be undone.</b> Are you sure?"
    )
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def substitutions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the menu to toggle individual regex rules."""
    query = update.callback_query
    
    # Check if we are in delete mode
    is_delete_mode = context.user_data.get("delete_mode", False)
    
    keys = cfg.get_all_substitution_keys()
    keyboard = []
    
    for key in keys:
        if is_delete_mode:
            btn_text = f"🗑 {key}"
            callback = f"set:delrule:{key}"
        else:
            is_enabled = key not in cfg.disabled_rules
            status_icon = "✅" if is_enabled else "❌"
            btn_text = f"{status_icon} {key}"
            callback = f"set:rule:{key}"
            
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
    
    # Add Control Buttons
    control_row = []
    if is_delete_mode:
        control_row.append(InlineKeyboardButton("✅ Done Deleting", callback_data="set:menu:subs_normal"))
    else:
        control_row.append(InlineKeyboardButton("➕ Add Rule", callback_data="set:rule:add_prompt"))
        control_row.append(InlineKeyboardButton("🗑 Delete Mode", callback_data="set:menu:subs_delete"))
    
    keyboard.append(control_row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="set:menu:main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if is_delete_mode:
        text = "<b>Delete Rules</b>\nTap a rule to permanently remove it."
    else:
        text = "<b>Toggle Individual Rules</b>\n<i>Changes are applied instantly.</i>"
    
    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles settings button clicks."""
    query = update.callback_query
    
    data = query.data
    if data == "set:close":
        await query.answer("Menu closed.")
        await query.message.delete()
        return

    if not data.startswith("set:"):
        await query.answer()
        return

    # Sub-menu navigation
    if data == "set:menu:subs":
        await query.answer("Loading rules...")
        await substitutions_menu(update, context)
        return
    if data == "set:menu:reset_confirm":
        await query.answer("Warning: Reset requested.")
        await reset_confirmation_menu(update, context)
        return
    if data == "set:action:reset_do":
        if cfg.reset_to_defaults():
            await query.answer("✅ Settings reset to defaults!", show_alert=True)
            await query.edit_message_text("✅ <b>Configuration has been reset to defaults.</b>", parse_mode=ParseMode.HTML)
            # Send fresh settings after a short delay
            await asyncio.sleep(2)
            await settings_command(update, context)
            await query.message.delete()
        else:
            await query.answer("❌ Failed to reset configuration.", show_alert=True)
        return
    if data == "set:menu:restart_confirm":
        await query.answer("Restart requested.")
        await restart_confirmation_menu(update, context)
        return
    if data == "set:action:restart_do":
        await query.answer("🔄 Restarting bot...", show_alert=True)
        await query.edit_message_text("🔄 <b>Restarting...</b> The bot will be back online in a few seconds.", parse_mode=ParseMode.HTML)
        logger.info(f"Restart initiated by user {update.effective_user.id}")
        
        # Save state for announcement after restart
        db.set_state("restart_chat_id", update.effective_chat.id)
        
        # Small delay to allow the message to be sent before shutdown
        await asyncio.sleep(1)
        sys.exit(0)
    if data == "set:menu:subs_delete":
        await query.answer("Delete mode enabled.")
        context.user_data["delete_mode"] = True
        await substitutions_menu(update, context)
        return
    if data == "set:menu:subs_normal":
        await query.answer("Delete mode disabled.")
        context.user_data["delete_mode"] = False
        await substitutions_menu(update, context)
        return
    if data == "set:menu:main":
        await query.answer("Back to main settings.")
        context.user_data["delete_mode"] = False
        await settings_command(update, context)
        return

    # Format: set:section:key OR set:rule:key OR set:delrule:key
    parts = data.split(":")
    type_ = parts[1]
    key = parts[2]

    if type_ == "rule":
        if key == "add_prompt":
            await query.answer("Please send the new rule.")
            context.user_data["awaiting_rule"] = True
            await query.message.reply_text(
                "➕ <b>Adding a new Rule</b>\n"
                "Please send the rule in the following format:\n"
                "<code>name = s@pattern@replacement@flags</code>\n\n"
                "Example:\n<code>twitter = s@twitter.com@fxtwitter.com@i</code>",
                parse_mode=ParseMode.HTML
            )
            return
        
        if cfg.toggle_rule(key):
            is_enabled = key not in cfg.disabled_rules
            await query.answer(f"Rule '{key}' {'enabled' if is_enabled else 'disabled'}")
            await substitutions_menu(update, context)
        return

    if type_ == "delrule":
        if cfg.delete_rule(key):
            await query.answer(f"✅ Rule '{key}' deleted!")
            await substitutions_menu(update, context)
        return

    # Original boolean toggle logic
    section = type_
    current_val = getattr(cfg, key, None)
    if isinstance(current_val, bool):
        new_val = not current_val
        if cfg.set_and_save(section, key, new_val):
            await query.answer(f"{key.replace('_', ' ').capitalize()} set to {'ON' if new_val else 'OFF'}")
            # Refresh UI in place
            await settings_command(update, context)
    else:
        await query.answer()


async def post_init(application: Application):
    """Sets the bot commands for autocomplete and recovers pending reminders."""
    # 1. Autocomplete Commands
    commands = [
        BotCommand("version", "Show bot version and commit hash"),
        BotCommand("remindme", "Set a reminder. Usage: /remindme 2h (reason)"),
        BotCommand("reminders", "See your pending reminders in this chat"),
        BotCommand("remindersall", "See all pending reminders in this chat"),
        BotCommand("settings", "Configure bot settings (Whitelisted only)"),
    ]
    await application.bot.set_my_commands(commands)

    # 2. Recover Reminders from DB
    pending = db.get_pending_reminders()
    now = datetime.now(timezone.utc)
    count = 0

    for row in pending:
        remind_time = datetime.fromisoformat(row["remind_time"])
        if remind_time.tzinfo is None:
            remind_time = remind_time.replace(tzinfo=timezone.utc)

        seconds = (remind_time - now).total_seconds()
        
        job_data = {
            "reminder_id": row["id"],
            "chat_id": row["chat_id"],
            "user_id": row["user_id"],
            "message_id": row["message_id"],
            "reason": row["reason"],
            "link": row["link"],
        }

        if seconds <= 0:
            # Overdue while bot was down, send immediately
            asyncio.create_task(send_reminder_from_recovery(application.bot, job_data))
        else:
            # Re-schedule
            context_stub = type('obj', (object,), {'bot': application.bot})
            asyncio.create_task(schedule_reminder(context_stub, int(seconds), job_data))
        
        count += 1
    
    if count > 0:
        logger.info(f"Recovered {count} reminders from database.")

    # 3. Check for restart announcement
    restart_chat_id = db.get_state("restart_chat_id")
    if restart_chat_id:
        try:
            await application.bot.send_message(
                chat_id=int(restart_chat_id),
                text="✅ <b>Bot has restarted successfully!</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to send restart announcement: {e}")
        finally:
            db.clear_state("restart_chat_id")


async def send_reminder_from_recovery(bot, data):
    """Helper to send overdue reminders on startup."""
    mention = f"<a href='tg://user?id={data['user_id']}'>Missed Reminder</a>"
    text = f"🔔 {mention} (Was scheduled for earlier)"
    if data["reason"]:
        text += f": {data['reason']}"
    if data.get("link"):
        text += f"\n\n🔗 <a href='{data['link']}'>Original Message</a>"

    try:
        await bot.send_message(
            chat_id=data["chat_id"],
            text=text,
            reply_to_message_id=data["message_id"],
            parse_mode=ParseMode.HTML,
        )
        db.delete_reminder(data["reminder_id"])
    except Exception as e:
        logger.error(f"Failed to send recovered reminder: {e}")


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
    application.add_handler(CommandHandler("reminders", reminders_command))
    application.add_handler(CommandHandler("remindersall", reminders_all_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.Document.FileExtension("cfg")) & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        CallbackQueryHandler(handle_delete_callback, pattern="^del:")
    )
    application.add_handler(
        CallbackQueryHandler(handle_settings_callback, pattern="^set:")
    )
    application.add_handler(
        CallbackQueryHandler(handle_reminder_callback, pattern="^rem:")
    )

    print("Bot is running. Press Ctrl+C to stop.")

    # 4. Run Polling
    # bootstrap_retries=-1 means it will keep trying to connect forever at startup if internet is down
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
