import telebot
import json
import secrets
import os
import requests
import time
import threading
import queue
from pathlib import Path

BUILD_VERSION = "PHASE2-FIXED-2026-08-15"

from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    BotCommand,
    BotCommandScopeChat
)


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Add your Telegram bot token to Railway Variables."
    )

# =========================================================
# ADMIN USER ID
# =========================================================
# Thay 123456789 bằng Telegram User ID của bạn.
#
# Có thể thêm nhiều admin:
#
# ADMIN_IDS = {
#     123456789,
#     987654321,
# }
#
# =========================================================

ADMIN_IDS = {
    7799631196,
}


# =========================================================
# BOT
# =========================================================

bot = telebot.TeleBot(TOKEN)


# =========================================================
# PHASE 1 - DOWNLOAD QUEUE / RATE LIMIT / ANTI-SPAM
# =========================================================

DOWNLOAD_QUEUE_MAX = 50
DOWNLOAD_QUEUE = queue.Queue(maxsize=DOWNLOAD_QUEUE_MAX)

DOWNLOAD_STATE_LOCK = threading.Lock()
ACTIVE_DOWNLOAD_USERS = set()
LAST_DOWNLOAD_ACCEPTED = {}
DOWNLOAD_COOLDOWN_SECONDS = 2.0

# Conservative pacing for outbound file-send API calls.
FILE_SEND_MIN_INTERVAL = 0.35
FILE_SEND_MAX_RETRIES = 5
_last_file_send = 0.0
_last_file_send_lock = threading.Lock()


def _wait_before_file_send():
    global _last_file_send

    with _last_file_send_lock:
        now = time.monotonic()
        wait = FILE_SEND_MIN_INTERVAL - (now - _last_file_send)

        if wait > 0:
            time.sleep(wait)

        _last_file_send = time.monotonic()


def _get_retry_after(exc):
    """Extract Telegram retry_after from a 429 exception."""
    try:
        result_json = getattr(exc, "result_json", None) or {}
        parameters = result_json.get("parameters") or {}
        retry_after = parameters.get("retry_after")
        if retry_after is not None:
            return max(1, int(retry_after))
    except (TypeError, ValueError, AttributeError):
        pass

    try:
        text = str(exc)
        marker = "retry after "
        if marker in text:
            value = text.split(marker, 1)[1].split()[0]
            return max(1, int(value))
    except (TypeError, ValueError, IndexError):
        pass

    return None


def send_file_api(method, *args, **kwargs):
    """Send a file/media-group with pacing, retry and 429 handling."""
    for attempt in range(1, FILE_SEND_MAX_RETRIES + 1):
        try:
            _wait_before_file_send()
            return method(*args, **kwargs)

        except telebot.apihelper.ApiTelegramException as exc:
            retry_after = _get_retry_after(exc)

            if retry_after is not None:
                print(
                    f"Telegram 429: retry_after={retry_after}s "
                    f"(attempt {attempt}/{FILE_SEND_MAX_RETRIES})"
                )
                time.sleep(retry_after + 1)
                continue

            if attempt < FILE_SEND_MAX_RETRIES:
                delay = min(2 ** (attempt - 1), 8)
                print(
                    f"Telegram API error while sending file: {exc}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
                continue

            raise

        except Exception as exc:
            if attempt < FILE_SEND_MAX_RETRIES:
                delay = min(2 ** (attempt - 1), 8)
                print(
                    f"Telegram send/network error: {exc}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
                continue

            raise


def safe_download_message(chat_id, text):
    """Best-effort status message for queued downloads."""
    try:
        bot.send_message(chat_id, text)
    except Exception as exc:
        print(f"Could not send queue status to {chat_id}: {exc}")



# =========================================================
# BOT USERNAME / SESSIONS
# =========================================================

BOT_USERNAME = bot.get_me().username

upload_sessions = {}
force_setup_mode = set()

# =========================================================
# PHASE 2 - SQLITE / HISTORY / STATISTICS / BACKUP
# =========================================================

import sqlite3
from datetime import datetime, timezone, timedelta
from zipfile import ZipFile, ZIP_DEFLATED

DATA_DIR = "/data"
DB_FILE = os.path.join(DATA_DIR, "bot.db")
LEGACY_DATA_FILE = os.path.join(DATA_DIR, "data.json")
FORCE_FILE = os.path.join(DATA_DIR, "force_channels.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

DB_LOCK = threading.RLock()

BACKUP_KEEP_COUNT = 14
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_START_DELAY_SECONDS = 30
DB_BUSY_TIMEOUT_MS = 30_000
LINK_NOT_FOUND_MESSAGE = "❌ Link này không còn tồn tại hoặc dữ liệu đã bị xóa."


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    return conn


def db_init():
    with DB_LOCK:
        conn = db_connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS links (
                    media_id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    views INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    file_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    FOREIGN KEY(media_id)
                        REFERENCES links(media_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_files_media_id
                    ON files(media_id);

                CREATE INDEX IF NOT EXISTS idx_links_owner_id
                    ON links(owner_id);

                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    download_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_history_media_id
                    ON download_history(media_id);

                CREATE INDEX IF NOT EXISTS idx_history_user_id
                    ON download_history(user_id);

                CREATE INDEX IF NOT EXISTS idx_history_created_at
                    ON download_history(created_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def migrate_legacy_json():
    """One-time, additive migration from legacy data.json to SQLite.

    This NEVER deletes or overwrites existing SQLite records. It only adds
    legacy links/files that do not already exist. The marker prevents a stale
    legacy data.json from resurrecting links after the admin intentionally
    deletes them later.
    """
    marker = os.path.join(DATA_DIR, ".json_migrated_to_sqlite")

    if os.path.exists(marker):
        print("Legacy JSON migration already completed.")
        return False

    if not os.path.exists(LEGACY_DATA_FILE):
        print("Legacy JSON file not found; migration skipped.")
        return False

    try:
        with open(LEGACY_DATA_FILE, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except Exception as exc:
        print(f"Legacy migration skipped: cannot read JSON: {exc}")
        return False

    if not isinstance(legacy, dict) or not legacy:
        print("Legacy migration skipped: JSON is empty or invalid.")
        return False

    migrated_links = 0
    added_files = 0

    with DB_LOCK:
        conn = db_connect()
        try:
            for media_id, entry in legacy.items():
                if not isinstance(entry, dict):
                    continue

                media_id = str(media_id).strip()
                if not media_id:
                    continue

                try:
                    owner_id = int(entry.get("owner", 0))
                except (TypeError, ValueError):
                    owner_id = 0

                name = str(entry.get("name", media_id)).strip() or media_id

                try:
                    views = int(entry.get("views", 0) or 0)
                except (TypeError, ValueError):
                    views = 0

                now = utc_now()

                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO links
                    (media_id, owner_id, name, views, created_at, updated_at)
                    VALUES (:media_id, :owner_id, :name, :views, :created_at, :updated_at)
                    """,
                    {
                        "media_id": media_id,
                        "owner_id": owner_id,
                        "name": name,
                        "views": views,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                if cur.rowcount > 0:
                    migrated_links += 1

                files = entry.get("files", [])
                if not isinstance(files, list):
                    continue

                for position, item in enumerate(files):
                    if not isinstance(item, dict):
                        continue

                    file_type = str(item.get("type", "")).strip()
                    file_id = str(item.get("file_id", "")).strip()
                    if not file_type or not file_id:
                        continue

                    exists = conn.execute(
                        """
                        SELECT 1 FROM files
                        WHERE media_id = :media_id
                          AND position = :position
                          AND file_id = :file_id
                        LIMIT 1
                        """,
                        {
                            "media_id": media_id,
                            "position": position,
                            "file_id": file_id,
                        }
                    ).fetchone()

                    if exists:
                        continue

                    conn.execute(
                        """
                        INSERT INTO files
                        (media_id, position, file_type, file_id)
                        VALUES (:media_id, :position, :file_type, :file_id)
                        """,
                        {
                            "media_id": media_id,
                            "position": position,
                            "file_type": file_type,
                            "file_id": file_id,
                        }
                    )
                    added_files += 1

            conn.commit()

            Path(marker).write_text(utc_now(), encoding="utf-8")

            print(
                "SQLite legacy migration completed: "
                f"{migrated_links} links, {added_files} files."
            )
            return True

        except Exception as exc:
            conn.rollback()
            print(f"Legacy migration failed and was rolled back: {exc}")
            return False
        finally:
            conn.close()


def get_link(media_id):
    media_id = str(media_id or "").strip()
    if not media_id:
        return None

    with DB_LOCK:
        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT media_id, owner_id, name, views, created_at, updated_at
                FROM links
                WHERE media_id = :media_id
                LIMIT 1
                """,
                {"media_id": media_id}
            ).fetchone()

            if not row:
                return None

            file_rows = conn.execute(
                """
                SELECT position, file_type, file_id
                FROM files
                WHERE media_id = :media_id
                ORDER BY position ASC, id ASC
                """,
                {"media_id": media_id}
            ).fetchall()

            return {
                "media_id": row["media_id"],
                "owner": row["owner_id"],
                "name": row["name"],
                "views": row["views"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "files": [
                    {
                        "type": item["file_type"],
                        "file_id": item["file_id"]
                    }
                    for item in file_rows
                ]
            }
        finally:
            conn.close()


def create_link(media_id, owner_id, name, file_list):
    media_id = str(media_id or "").strip()
    name = str(name or "").strip()

    if not media_id:
        raise ValueError("media_id is empty")
    if not name:
        raise ValueError("name is empty")
    if not isinstance(file_list, list) or not file_list:
        raise ValueError("file_list is empty")

    now = utc_now()

    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO links
                (media_id, owner_id, name, views, created_at, updated_at)
                VALUES (:media_id, :owner_id, :name, 0, :created_at, :updated_at)
                """,
                {
                    "media_id": media_id,
                    "owner_id": int(owner_id),
                    "name": name,
                    "created_at": now,
                    "updated_at": now,
                }
            )

            inserted_files = 0
            for position, item in enumerate(file_list):
                if not isinstance(item, dict):
                    continue
                file_type = str(item.get("type", "")).strip()
                file_id = str(item.get("file_id", "")).strip()
                if not file_type or not file_id:
                    continue

                conn.execute(
                    """
                    INSERT INTO files
                    (media_id, position, file_type, file_id)
                    VALUES (:media_id, :position, :file_type, :file_id)
                    """,
                    {
                        "media_id": media_id,
                        "position": position,
                        "file_type": file_type,
                        "file_id": file_id,
                    }
                )
                inserted_files += 1

            if inserted_files == 0:
                raise ValueError("No valid files in upload")

            conn.commit()
            return inserted_files

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def record_download(media_id, user_id, chat_id, file_count):
    """Atomically record a download.

    Returns True when recorded, False when media_id no longer exists.
    The stale-link guard is important because users may open very old links
    after an administrator has deleted the underlying record.
    """
    media_id = str(media_id or "").strip()
    if not media_id:
        return False

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        user_id = 0

    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        chat_id = 0

    try:
        file_count = max(0, int(file_count))
    except (TypeError, ValueError):
        file_count = 0

    now = utc_now()

    with DB_LOCK:
        conn = db_connect()
        try:
            # Guard against old/stale deep links.
            exists = conn.execute(
                "SELECT 1 FROM links WHERE media_id = :media_id LIMIT 1",
                {"media_id": media_id}
            ).fetchone()

            if not exists:
                print(f"Download ignored: media_id not found: {media_id}")
                return False

            conn.execute(
                """
                UPDATE links
                SET views = views + 1, updated_at = :updated_at
                WHERE media_id = :media_id
                """,
                {
                    "updated_at": now,
                    "media_id": media_id,
                }
            )

            conn.execute(
                """
                INSERT INTO download_history
                (media_id, user_id, chat_id, file_count, created_at)
                VALUES (:media_id, :user_id, :chat_id, :file_count, :created_at)
                """,
                {
                    "media_id": media_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "file_count": file_count,
                    "created_at": now,
                }
            )

            conn.execute(
                """
                INSERT INTO users
                (user_id, first_seen, last_seen, download_count)
                VALUES (:user_id, :first_seen, :last_seen, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    download_count = users.download_count + 1
                """,
                {
                    "user_id": user_id,
                    "first_seen": now,
                    "last_seen": now,
                }
            )

            conn.commit()
            return True

        except Exception as exc:
            conn.rollback()
            print(
                "record_download failed: "
                f"media_id={media_id}, user_id={user_id}, "
                f"chat_id={chat_id}, file_count={file_count}, "
                f"error={exc!r}"
            )
            raise
        finally:
            conn.close()


def get_owner_links(owner_id):
    with DB_LOCK:
        conn = db_connect()
        try:
            return conn.execute(
                """
                SELECT
                    l.media_id,
                    l.name,
                    l.views,
                    l.created_at,
                    COUNT(f.id) AS file_count
                FROM links l
                LEFT JOIN files f
                    ON f.media_id = l.media_id
                WHERE l.owner_id = ?
                GROUP BY
                    l.media_id,
                    l.name,
                    l.views,
                    l.created_at
                ORDER BY l.created_at DESC
                """,
                (owner_id,)
            ).fetchall()

        finally:
            conn.close()


def delete_owner_links(owner_id):
    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                "DELETE FROM links WHERE owner_id = ?",
                (owner_id,)
            )
            conn.commit()
        finally:
            conn.close()


def get_statistics():
    with DB_LOCK:
        conn = db_connect()
        try:
            total_links = conn.execute(
                "SELECT COUNT(*) AS c FROM links"
            ).fetchone()["c"]

            total_files = conn.execute(
                "SELECT COUNT(*) AS c FROM files"
            ).fetchone()["c"]

            total_downloads = conn.execute(
                "SELECT COUNT(*) AS c FROM download_history"
            ).fetchone()["c"]

            unique_users = conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            today = datetime.now(
                timezone.utc
            ).date().isoformat()

            last_7_days = (
                datetime.now(timezone.utc)
                - timedelta(days=7)
            ).isoformat()

            downloads_today = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM download_history
                WHERE substr(created_at, 1, 10) = ?
                """,
                (today,)
            ).fetchone()["c"]

            downloads_7_days = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM download_history
                WHERE created_at >= ?
                """,
                (last_7_days,)
            ).fetchone()["c"]

            top_links = conn.execute(
                """
                SELECT
                    l.media_id,
                    l.name,
                    COUNT(h.id) AS downloads
                FROM links l
                LEFT JOIN download_history h
                    ON h.media_id = l.media_id
                GROUP BY
                    l.media_id,
                    l.name,
                    l.created_at
                ORDER BY
                    downloads DESC,
                    l.created_at DESC
                LIMIT 10
                """
            ).fetchall()

            return {
                "total_links": total_links,
                "total_files": total_files,
                "total_downloads": total_downloads,
                "unique_users": unique_users,
                "downloads_today": downloads_today,
                "downloads_7_days": downloads_7_days,
                "top_links": top_links
            }

        finally:
            conn.close()


def create_full_backup_zip():
    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d_%H-%M-%S")

    zip_path = os.path.join(
        BACKUP_DIR,
        f"bot_backup_{timestamp}.zip"
    )

    snapshot_path = os.path.join(
        BACKUP_DIR,
        f".snapshot_{timestamp}.db"
    )

    # SQLite online backup gives a consistent snapshot.
    with DB_LOCK:
        source = db_connect()
        destination = sqlite3.connect(
            snapshot_path
        )

        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    try:
        manifest = {
            "backup_created_at_utc": utc_now(),
            "version": "phase2",
            "files": [
                "bot.db"
            ]
        }

        with ZipFile(
            zip_path,
            "w",
            compression=ZIP_DEFLATED
        ) as archive:

            archive.write(
                snapshot_path,
                arcname="bot.db"
            )

            if os.path.exists(
                LEGACY_DATA_FILE
            ):
                archive.write(
                    LEGACY_DATA_FILE,
                    arcname="data.json"
                )
                manifest["files"].append(
                    "data.json"
                )

            if os.path.exists(
                FORCE_FILE
            ):
                archive.write(
                    FORCE_FILE,
                    arcname="force_channels.json"
                )
                manifest["files"].append(
                    "force_channels.json"
                )

            marker = os.path.join(
                DATA_DIR,
                ".json_migrated_to_sqlite"
            )

            if os.path.exists(marker):
                archive.write(
                    marker,
                    arcname=".json_migrated_to_sqlite"
                )
                manifest["files"].append(
                    ".json_migrated_to_sqlite"
                )

            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2
                )
            )

        return zip_path

    finally:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass


def cleanup_old_backups():
    try:
        backups = list(
            Path(BACKUP_DIR).glob(
                "bot_backup_*.zip"
            )
        )

        backups.sort(
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        for old_path in backups[
            BACKUP_KEEP_COUNT:
        ]:
            try:
                old_path.unlink()
            except OSError:
                pass

    except Exception as exc:
        print(
            f"Backup cleanup error: {exc}"
        )


def database_health_check():
    """Validate the SQLite file at startup without modifying user data."""
    with DB_LOCK:
        conn = db_connect()
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {}
            for table in ("links", "files", "download_history", "users"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]

            print(
                "Database health: "
                f"integrity={integrity}; "
                f"links={counts['links']}; "
                f"files={counts['files']}; "
                f"history={counts['download_history']}; "
                f"users={counts['users']}"
            )
            return integrity == "ok"
        except Exception as exc:
            print(f"Database health check failed: {exc!r}")
            return False
        finally:
            conn.close()


def automatic_backup_worker():
    time.sleep(
        BACKUP_START_DELAY_SECONDS
    )

    while True:
        try:
            path = create_full_backup_zip()
            cleanup_old_backups()

            print(
                f"Automatic backup created: "
                f"{path}"
            )

        except Exception as exc:
            print(
                f"Automatic backup error: "
                f"{exc}"
            )

        time.sleep(
            BACKUP_INTERVAL_SECONDS
        )


db_init()
# One-time legacy migration. Never deletes SQLite data.
migrate_legacy_json()
database_health_check()

if not os.path.exists(
    FORCE_FILE
):
    with open(
        FORCE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            [],
            f,
            ensure_ascii=False
        )

backup_thread = threading.Thread(
    target=automatic_backup_worker,
    name="database-backup-worker",
    daemon=True
)
backup_thread.start()


def load_force_channels():
    try:
        with open(
            FORCE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)
    except Exception:
        return []


def save_force_channels(data):
    tmp_file = f"{FORCE_FILE}.tmp"

    with open(
        tmp_file,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False
        )

    os.replace(
        tmp_file,
        FORCE_FILE
    )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_only(message):
    if not is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "🚫 Bạn không có quyền sử dụng chức năng này."
        )
        return False

    return True


def admin_callback_only(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "🚫 Bạn không có quyền sử dụng chức năng này.",
            show_alert=True
        )
        return False

    return True


# =========================================================
# BOT COMMANDS
# =========================================================

# Normal users only see /start.
bot.set_my_commands([
    BotCommand(
        "start",
        "Open bot"
    )
])

# Admin users also get management/statistics/backup commands.
for admin_id in ADMIN_IDS:
    try:
        bot.set_my_commands(
            [
                BotCommand(
                    "start",
                    "Open bot"
                ),
                BotCommand(
                    "setforce",
                    "Add force join channel"
                ),
                BotCommand(
                    "listforce",
                    "Show force channels"
                ),
                BotCommand(
                    "removeforce",
                    "Remove force channel"
                ),
                BotCommand(
                    "data",
                    "Download backup ZIP"
                ),
                BotCommand(
                    "backup",
                    "Create backup ZIP"
                ),
                BotCommand(
                    "stats",
                    "Show bot statistics"
                )
            ],
            scope=BotCommandScopeChat(
                admin_id
            )
        )
    except Exception as exc:
        print(
            f"Could not set admin commands "
            f"for {admin_id}: {exc}"
        )


# =========================================================
# FORCE MANAGEMENT
# =========================================================

@bot.message_handler(commands=['setforce'])
def enable_force_setup(message):

    # ADMIN ONLY
    if not admin_only(message):
        return

    force_setup_mode.add(
        message.from_user.id
    )

    bot.send_message(
        message.chat.id,
        "Forward a message from the channel to add."
    )


@bot.message_handler(
    func=lambda m:
        m.from_user.id in force_setup_mode
        and m.forward_from_chat is not None
)
def save_force_channel(message):

    # ADMIN ONLY
    if not is_admin(message.from_user.id):

        force_setup_mode.discard(
            message.from_user.id
        )

        bot.send_message(
            message.chat.id,
            "🚫 You don't have permission."
        )

        return

    if message.forward_from_chat.type != "channel":

        bot.send_message(
            message.chat.id,
            "Forward from a channel only."
        )

        return

    channel_id = message.forward_from_chat.id

    channels = load_force_channels()

    if channel_id not in channels:

        channels.append(channel_id)

        save_force_channels(channels)

    force_setup_mode.discard(
        message.from_user.id
    )

    bot.send_message(
        message.chat.id,
        f"✅ Added force channel:\n{channel_id}"
    )


# =========================================================
# LIST FORCE
# =========================================================

@bot.message_handler(commands=['listforce'])
def list_force(message):

    # ADMIN ONLY
    if not admin_only(message):
        return

    channels = load_force_channels()

    if not channels:

        bot.send_message(
            message.chat.id,
            "No force channels set."
        )

        return

    text = "📢 Force Channels:\n\n"

    for ch in channels:

        try:

            chat = bot.get_chat(ch)

            text += (
                f"{chat.title} ({ch})\n"
            )

        except Exception:

            text += (
                f"Invalid Channel ({ch})\n"
            )

    bot.send_message(
        message.chat.id,
        text
    )


# =========================================================
# REMOVE FORCE
# =========================================================

@bot.message_handler(commands=['removeforce'])
def remove_force(message):

    # ADMIN ONLY
    if not admin_only(message):
        return

    args = message.text.split()

    if len(args) != 2:

        bot.send_message(
            message.chat.id,
            "Usage: /removeforce CHANNEL_ID"
        )

        return

    try:

        channel_id = int(args[1])

    except ValueError:

        bot.send_message(
            message.chat.id,
            "Channel ID must be a number."
        )

        return

    channels = load_force_channels()

    if channel_id in channels:

        channels.remove(channel_id)

        save_force_channels(channels)

        bot.send_message(
            message.chat.id,
            "✅ Xóa."
        )

    else:

        bot.send_message(
            message.chat.id,
            "Channel not found."
        )


# =========================================================
# SAFE CHECK JOIN
# =========================================================

def is_joined(user_id):

    channels = load_force_channels()

    updated = []

    all_joined = True

    for ch in channels:

        try:

            member = bot.get_chat_member(
                ch,
                user_id
            )

            if member.status in [
                "left",
                "kicked"
            ]:

                all_joined = False

            updated.append(ch)

        except Exception:

            print(
                "Removed invalid force channel:",
                ch
            )

    if len(updated) != len(channels):

        save_force_channels(updated)

    return all_joined


# =========================================================
# FORCE JOIN BUTTON
# =========================================================

def join_required_markup(media_id):

    channels = load_force_channels()

    markup = InlineKeyboardMarkup()

    for ch in channels:

        try:

            chat = bot.get_chat(ch)

            invite = (
                chat.invite_link
                or bot.export_chat_invite_link(ch)
            )

            markup.add(
                InlineKeyboardButton(
                    f"📢 Join {chat.title}",
                    url=invite
                )
            )

        except Exception:

            continue

    markup.add(
        InlineKeyboardButton(
            "✅ Kiểm tra đã tham gia đủ",
            callback_data=f"check_{media_id}"
        )
    )

    return markup


# =========================================================
# MENU
# =========================================================

def main_menu(user_id):

    markup = InlineKeyboardMarkup()

    # =====================================================
    # CHỈ ADMIN MỚI CÓ MENU QUẢN TRỊ
    # =====================================================

    if is_admin(user_id):

        markup.add(
            InlineKeyboardButton(
                "📤 Upload File",
                callback_data="upload"
            )
        )

        markup.add(
            InlineKeyboardButton(
                "📊 My Links",
                callback_data="mylinks"
            )
        )

    # USER BÌNH THƯỜNG:
    # Không có nút admin nào.

    return markup


# =========================================================
# START
# =========================================================

@bot.message_handler(commands=['start'])
def start(message):

    text = message.text

    media_id = None

    # -----------------------------------------------------
    # /start MEDIA_ID
    # -----------------------------------------------------

    if text.startswith("/start "):

        media_id = text.replace(
            "/start ",
            "",
            1
        ).strip()

    # -----------------------------------------------------
    # /start bình thường
    # -----------------------------------------------------

    if not media_id:

        bot.send_message(
            message.chat.id,
            "Welcome!",
            reply_markup=main_menu(
                message.from_user.id
            )
        )

        return

    # -----------------------------------------------------
    # MEDIA NOT FOUND
    # -----------------------------------------------------

    if get_link(media_id) is None:

        bot.send_message(
            message.chat.id,
            LINK_NOT_FOUND_MESSAGE
        )

        return

    # -----------------------------------------------------
    # FORCE JOIN
    # -----------------------------------------------------

    if not is_joined(
        message.from_user.id
    ):

        bot.send_message(
            message.chat.id,
            "🚫 You must join required channels.",
            reply_markup=join_required_markup(
                media_id
            )
        )

        return

    # -----------------------------------------------------
    # SEND FILES
    # -----------------------------------------------------

    send_files(
        message.chat.id,
        media_id,
        message.from_user.id
    )



# =========================================================
# SEND FILES - PHASE 1 CONCURRENT DELIVERY
# =========================================================

DOWNLOAD_WORKERS = 4

DOWNLOAD_QUEUE_MAX = 50
DOWNLOAD_QUEUE = queue.Queue(
    maxsize=DOWNLOAD_QUEUE_MAX
)

DOWNLOAD_STATE_LOCK = threading.Lock()
ACTIVE_DOWNLOAD_USERS = set()
LAST_DOWNLOAD_ACCEPTED = {}

DOWNLOAD_COOLDOWN_SECONDS = 2.0

FILE_SEND_MIN_INTERVAL = 0.35
FILE_SEND_MAX_RETRIES = 5

_last_file_send = 0.0
_last_file_send_lock = threading.Lock()


def _wait_before_file_send():
    global _last_file_send

    with _last_file_send_lock:

        now = time.monotonic()

        wait = (
            FILE_SEND_MIN_INTERVAL
            - (now - _last_file_send)
        )

        if wait > 0:
            time.sleep(wait)

        _last_file_send = (
            time.monotonic()
        )


def _get_retry_after(exc):
    try:
        result_json = getattr(
            exc,
            "result_json",
            None
        ) or {}

        parameters = (
            result_json.get("parameters")
            or {}
        )

        retry_after = (
            parameters.get("retry_after")
        )

        if retry_after is not None:
            return max(
                1,
                int(retry_after)
            )

    except (
        TypeError,
        ValueError,
        AttributeError
    ):
        pass

    text = str(exc)
    marker = "retry after "

    if marker in text:
        try:
            value = (
                text.split(
                    marker,
                    1
                )[1]
                .split()[0]
            )

            return max(
                1,
                int(value)
            )

        except (
            TypeError,
            ValueError,
            IndexError
        ):
            pass

    return None


def send_file_api(
    method,
    *args,
    **kwargs
):
    for attempt in range(
        1,
        FILE_SEND_MAX_RETRIES + 1
    ):

        try:

            _wait_before_file_send()

            return method(
                *args,
                **kwargs
            )

        except telebot.apihelper.ApiTelegramException as exc:

            retry_after = _get_retry_after(
                exc
            )

            if retry_after is not None:

                print(
                    "Telegram 429: "
                    f"retry_after={retry_after}s "
                    f"(attempt "
                    f"{attempt}/"
                    f"{FILE_SEND_MAX_RETRIES})"
                )

                time.sleep(
                    retry_after + 1
                )

                continue

            if attempt < FILE_SEND_MAX_RETRIES:

                delay = min(
                    2 ** (attempt - 1),
                    8
                )

                print(
                    f"Telegram API error: "
                    f"{exc}. "
                    f"Retrying in {delay}s..."
                )

                time.sleep(delay)
                continue

            raise

        except Exception as exc:

            if attempt < FILE_SEND_MAX_RETRIES:

                delay = min(
                    2 ** (attempt - 1),
                    8
                )

                print(
                    f"Telegram send/network "
                    f"error: {exc}. "
                    f"Retrying in {delay}s..."
                )

                time.sleep(delay)
                continue

            raise


def safe_download_message(
    chat_id,
    text
):
    try:
        bot.send_message(
            chat_id,
            text
        )
    except Exception as exc:
        print(
            f"Could not send queue status "
            f"to {chat_id}: {exc}"
        )


def _build_media_list(files):
    result = []

    for item in files:

        file_type = item.get("type")
        file_id = item.get("file_id")

        if not file_id:
            continue

        if file_type == "photo":
            result.append(
                InputMediaPhoto(
                    file_id
                )
            )

        elif file_type == "video":
            result.append(
                InputMediaVideo(
                    file_id
                )
            )

        elif file_type == "document":
            result.append(
                InputMediaDocument(
                    file_id
                )
            )

    return result


def _deliver_download_job(job):
    chat_id = job["chat_id"]
    media_id = job["media_id"]
    files = job["files"]

    media_list = _build_media_list(
        files
    )

    if not media_list:

        safe_download_message(
            chat_id,
            "No files available."
        )

        return

    try:

        if len(media_list) == 1:

            item = next(
                (
                    item
                    for item in files
                    if item.get("file_id")
                ),
                None
            )

            if not item:
                raise ValueError(
                    "No valid file."
                )

            file_type = item["type"]
            file_id = item["file_id"]

            if file_type == "photo":

                send_file_api(
                    bot.send_photo,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            elif file_type == "video":

                send_file_api(
                    bot.send_video,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            elif file_type == "document":

                send_file_api(
                    bot.send_document,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            else:

                raise ValueError(
                    f"Unsupported file type: "
                    f"{file_type}"
                )

            return

        # Telegram media groups max out at 10.
        for i in range(
            0,
            len(media_list),
            10
        ):

            chunk = media_list[
                i:i + 10
            ]

            send_file_api(
                bot.send_media_group,
                chat_id,
                chunk,
                protect_content=True
            )

    except Exception as exc:

        print(
            f"Download failed: "
            f"chat_id={chat_id}, "
            f"media_id={media_id}, "
            f"error={exc}"
        )

        safe_download_message(
            chat_id,
            "⚠️ Telegram đang bận hoặc "
            "giới hạn tốc độ gửi. "
            "Vui lòng thử lại sau ít giây."
        )


def _download_worker(
    worker_id
):
    print(
        f"Download worker "
        f"{worker_id} started."
    )

    while True:

        job = DOWNLOAD_QUEUE.get()

        try:
            _deliver_download_job(
                job
            )

        except Exception as exc:

            print(
                f"Download worker "
                f"{worker_id} error: "
                f"{exc}"
            )

        finally:

            with DOWNLOAD_STATE_LOCK:
                ACTIVE_DOWNLOAD_USERS.discard(
                    job.get("user_id")
                )

            DOWNLOAD_QUEUE.task_done()


def enqueue_download(
    chat_id,
    user_id,
    media_id
):
    entry = get_link(
        media_id
    )

    if entry is None:

        bot.send_message(
            chat_id,
            LINK_NOT_FOUND_MESSAGE
        )

        return False

    files = entry.get(
        "files",
        []
    )

    if not files:

        bot.send_message(
            chat_id,
            "No files available."
        )

        return False

    now = time.monotonic()

    with DOWNLOAD_STATE_LOCK:

        if (
            user_id
            in ACTIVE_DOWNLOAD_USERS
        ):

            safe_download_message(
                chat_id,
                "⏳ Bạn đang có một lượt "
                "tải đang xử lý. "
                "Vui lòng chờ lượt hiện tại "
                "hoàn tất."
            )

            return False

        last_accepted = (
            LAST_DOWNLOAD_ACCEPTED.get(
                user_id,
                0.0
            )
        )

        elapsed = (
            now - last_accepted
        )

        if (
            elapsed
            < DOWNLOAD_COOLDOWN_SECONDS
        ):

            remaining = (
                DOWNLOAD_COOLDOWN_SECONDS
                - elapsed
            )

            safe_download_message(
                chat_id,
                "⏳ Vui lòng chờ "
                f"{max(1, int(remaining + 0.99))} "
                "giây rồi thử lại."
            )

            return False

        if DOWNLOAD_QUEUE.full():

            safe_download_message(
                chat_id,
                "⚠️ Bot đang có quá "
                "nhiều lượt tải. "
                "Vui lòng thử lại sau."
            )

            return False

        job = {
            "chat_id": chat_id,
            "user_id": user_id,
            "media_id": media_id,
            "files": [
                dict(item)
                for item in files
            ]
        }

        ACTIVE_DOWNLOAD_USERS.add(
            user_id
        )

        LAST_DOWNLOAD_ACCEPTED[
            user_id
        ] = now

        try:

            DOWNLOAD_QUEUE.put_nowait(
                job
            )

        except queue.Full:

            ACTIVE_DOWNLOAD_USERS.discard(
                user_id
            )

            safe_download_message(
                chat_id,
                "⚠️ Bot đang có quá "
                "nhiều lượt tải. "
                "Vui lòng thử lại sau."
            )

            return False

        queue_position = (
            DOWNLOAD_QUEUE.qsize()
        )

    # A request is counted when accepted. If the link disappeared between
    # get_link() and this point, silently skip history instead of generating
    # a database error for the user.
    try:
        recorded = record_download(
            media_id=media_id,
            user_id=user_id,
            chat_id=chat_id,
            file_count=len(files)
        )
        if not recorded:
            print(f"Download record skipped for stale media_id={media_id}")
            ACTIVE_DOWNLOAD_USERS.discard(user_id)
            return False

    except Exception as exc:
        print(
            f"Could not record download for {media_id}: {exc!r}"
        )
        # The actual download can still proceed even if statistics fail.

    if queue_position > 1:

        safe_download_message(
            chat_id,
            "⏳ Yêu cầu đã được nhận "
            "và đang chờ xử lý."
        )

    return True


def send_files(
    chat_id,
    media_id,
    user_id
):
    return enqueue_download(
        chat_id,
        user_id,
        media_id
    )


download_worker_threads = []

for worker_id in range(
    1,
    DOWNLOAD_WORKERS + 1
):

    thread = threading.Thread(
        target=_download_worker,
        args=(worker_id,),
        name=(
            f"download-worker-"
            f"{worker_id}"
        ),
        daemon=True
    )

    thread.start()

    download_worker_threads.append(
        thread
    )
# =========================================================
# CALLBACK
# =========================================================

@bot.callback_query_handler(
    func=lambda call: True
)
def callback(call):

    # =====================================================
    # CHECK FORCE JOIN
    # =====================================================

    if call.data.startswith("check_"):

        media_id = call.data.split(
            "_",
            1
        )[1]

        if is_joined(
            call.from_user.id
        ):

            try:

                bot.delete_message(
                    call.message.chat.id,
                    call.message.message_id
                )

            except Exception:

                pass

            send_files(
                call.message.chat.id,
                media_id,
                call.from_user.id
            )

        else:

            bot.answer_callback_query(
                call.id,
                "Join all channels first.",
                show_alert=True
            )

        return

    # =====================================================
    # UPLOAD
    # =====================================================

    elif call.data == "upload":

        # ADMIN ONLY
        if not admin_callback_only(call):
            return

        media_id = secrets.token_urlsafe(8)

        upload_sessions[
            call.from_user.id
        ] = {

            "media_id": media_id,
            "files": []

        }

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "✅ Finish Upload",
                callback_data="finish"
            )
        )

        bot.edit_message_text(
            "Send files now.\nPress Finish when done.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )

        return

    # =====================================================
    # FINISH UPLOAD
    # =====================================================

    elif call.data == "finish":

        # ADMIN ONLY
        if not admin_callback_only(call):
            return

        user_id = call.from_user.id

        if (
            user_id not in upload_sessions
            or not upload_sessions[user_id]["files"]
        ):

            bot.answer_callback_query(
                call.id,
                "No files uploaded."
            )

            return

        upload_sessions[
            user_id
        ]["waiting_name"] = True

        bot.edit_message_text(
            "Enter name for this link:",
            call.message.chat.id,
            call.message.message_id
        )

        return

    # =====================================================
    # MY LINKS
    # =====================================================

    elif call.data == "mylinks":

        if not admin_callback_only(call):
            return

        rows = get_owner_links(
            call.from_user.id
        )

        text = "📊 Your Links:\n\n"
        found = False

        for row in rows:

            found = True

            link = (
                f"https://t.me/"
                f"{BOT_USERNAME}"
                f"?start={row['media_id']}"
            )

            text += (
                f"{row['name']}\n"
                f"{link}\n"
                f"Views: {row['views']}\n"
                f"Files: {row['file_count']}\n\n"
            )

        markup = InlineKeyboardMarkup()

        if found:

            markup.add(
                InlineKeyboardButton(
                    "🗑 Reset All",
                    callback_data="reset_all"
                )
            )

        else:

            text = (
                "You have no links yet."
            )

        markup.add(
            InlineKeyboardButton(
                "⬅ Back",
                callback_data="back_menu"
            )
        )

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            disable_web_page_preview=True
        )

        return


    # =====================================================
    # RESET ALL
    # =====================================================

    elif call.data == "reset_all":

        # ADMIN ONLY
        if not admin_callback_only(call):
            return

        user_id = call.from_user.id

        delete_owner_links(
            user_id
        )

        bot.edit_message_text(
            "🗑 All links deleted.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu(
                user_id
            )
        )

        return

    # =====================================================
    # BACK MENU
    # =====================================================

    elif call.data == "back_menu":

        bot.edit_message_text(
            "Welcome!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu(
                call.from_user.id
            )
        )

        return


# =========================================================
# RECEIVE NAME
# =========================================================

@bot.message_handler(
    func=lambda m:
        m.from_user.id in upload_sessions
        and upload_sessions[
            m.from_user.id
        ].get("waiting_name")
)
def receive_name(message):

    # ADMIN ONLY
    if not is_admin(message.from_user.id):

        upload_sessions.pop(
            message.from_user.id,
            None
        )

        bot.send_message(
            message.chat.id,
            "🚫 You don't have permission."
        )

        return

    user_id = message.from_user.id

    session = upload_sessions[user_id]

    link_name = message.text.strip()

    # -----------------------------------------------------
    # EMPTY NAME
    # -----------------------------------------------------

    if not link_name:

        bot.send_message(
            message.chat.id,
            "Please enter a name."
        )

        return

    media_id = session["media_id"]

    create_link(
        media_id=media_id,
        owner_id=user_id,
        name=link_name,
        file_list=session["files"]
    )

    # =====================================================
    # CREATE TELEGRAM LINK
    # =====================================================

    link = f"https://t.me/{BOT_USERNAME}?start={media_id}"


    # =====================================================
    # CREATE WORDPRESS QUICK NOTE
    # =====================================================

    WORDPRESS_URL = "https://erobooks.online/wp-json/quick-post/v1/create"

    WORDPRESS_API_KEY = os.getenv("WORDPRESS_API_KEY", "").strip()

    wordpress_data = {
        "title": link_name,
        "url": link,
        "media_id": media_id
    }

    wordpress_success = False
    wordpress_post_url = None

    try:

        response = requests.post(
            WORDPRESS_URL,
            json=wordpress_data,
            headers={
                "X-Quick-Post-Key": WORDPRESS_API_KEY
            },
            timeout=20
        )

        if response.status_code == 200:

            result = response.json()

            if result.get("success"):

                wordpress_success = True

                wordpress_post_url = result.get("post_url")

                print(
                    "WordPress post created:",
                    wordpress_post_url
                )

            else:

                print(
                    "WordPress API error:",
                    result
                )

        else:

            print(
                "WordPress HTTP error:",
                response.status_code,
                response.text
            )

    except Exception as e:

        print(
            "WordPress connection error:",
            e
        )


    # =====================================================
    # CREATE GETLINK
    # =====================================================

    getlink_url = None

    if wordpress_success and wordpress_post_url:

        GETLINK_API_KEY = os.getenv("GETLINK_API_KEY")

        GETLINK_API_URL = "https://vuotlink.xyz/api/"

        getlink_params = {
            "api": GETLINK_API_KEY,
            "url": wordpress_post_url,
            "alias": link_name
        }

        try:

            getlink_response = requests.get(
    GETLINK_API_URL,
    params=getlink_params,
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36"
    },
    timeout=20
)

            if getlink_response.status_code == 200:

                getlink_result = getlink_response.json()

                if getlink_result.get("status") == "success":

                    getlink_url = getlink_result.get(
                        "shortenedUrl"
                    )

                    print(
                        "GetLink created:",
                        getlink_url
                    )

                else:

                    print(
                        "GetLink API error:",
                        getlink_result
                    )

            else:

                print(
                    "GetLink HTTP error:",
                    getlink_response.status_code,
                    getlink_response.text
                )

        except Exception as e:

            print(
                "GetLink connection error:",
                e
            )


    # =====================================================
    # SEND TELEGRAM RESULT
    # =====================================================

    if getlink_url:

        bot.send_message(
            message.chat.id,
            f"✅ Upload Complete!\n"
            f"🔗 {getlink_url}",
            disable_web_page_preview=True
        )

    elif wordpress_success:

        bot.send_message(
            message.chat.id,
            f"✅ WordPress Note created!\n"
            f"🔗 {wordpress_post_url}\n\n"
            f"⚠️ GetLink could not be created.",
            disable_web_page_preview=True
        )

    else:

        bot.send_message(
            message.chat.id,
            f"✅ Upload Complete!\n"
            f"{link}\n\n"
            f"⚠️ WordPress post could not be created.",
            disable_web_page_preview=True
        )


    del upload_sessions[user_id]

# =========================================================
# HANDLE MEDIA
# =========================================================

@bot.message_handler(
    content_types=[
        'photo',
        'video',
        'document'
    ]
)
def handle_media(message):

    user_id = message.from_user.id

    # -----------------------------------------------------
    # CHỈ ADMIN ĐƯỢC UPLOAD
    # -----------------------------------------------------

    if not is_admin(user_id):
        return

    # -----------------------------------------------------
    # CHƯA BẮT ĐẦU UPLOAD
    # -----------------------------------------------------

    if user_id not in upload_sessions:
        return

    # -----------------------------------------------------
    # PHOTO
    # -----------------------------------------------------

    if message.photo:

        upload_sessions[user_id]["files"].append(
            {
                "type": "photo",
                "file_id": message.photo[-1].file_id
            }
        )

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    elif message.video:

        upload_sessions[user_id]["files"].append(
            {
                "type": "video",
                "file_id": message.video.file_id
            }
        )

    # -----------------------------------------------------
    # DOCUMENT
    # -----------------------------------------------------

    elif message.document:

        upload_sessions[user_id]["files"].append(
            {
                "type": "document",
                "file_id": message.document.file_id
            }
        )



# =========================================================
# STATISTICS
# =========================================================

@bot.message_handler(commands=['stats'])
def stats(message):

    if not admin_only(message):
        return

    try:

        data = get_statistics()

        text = (
            "📊 Bot Statistics\n\n"
            f"🔗 Total links: "
            f"{data['total_links']}\n"
            f"📦 Total files: "
            f"{data['total_files']}\n"
            f"⬇️ Total downloads: "
            f"{data['total_downloads']}\n"
            f"👤 Unique users: "
            f"{data['unique_users']}\n"
            f"📅 Downloads today: "
            f"{data['downloads_today']}\n"
            f"📈 Downloads last 7 days: "
            f"{data['downloads_7_days']}\n"
        )

        if data["top_links"]:

            text += (
                "\n🔥 Top downloads:\n"
            )

            for index, row in enumerate(
                data["top_links"],
                start=1
            ):

                text += (
                    f"{index}. "
                    f"{row['name']} — "
                    f"{row['downloads']}\n"
                )

        bot.send_message(
            message.chat.id,
            text
        )

    except Exception as exc:

        print(
            f"Statistics error: {exc}"
        )

        bot.send_message(
            message.chat.id,
            "❌ Cannot load statistics."
        )


# =========================================================
# BACKUP
# =========================================================

def send_backup_to_admin(
    chat_id
):
    try:

        path = (
            create_full_backup_zip()
        )

        cleanup_old_backups()

        size_mb = (
            os.path.getsize(path)
            / (1024 * 1024)
        )

        with open(
            path,
            "rb"
        ) as f:

            bot.send_document(
                chat_id,
                f,
                caption=(
                    "✅ Full bot backup created.\n"
                    f"📦 Size: {size_mb:.2f} MB\n"
                    "Includes: bot.db, data.json "
                    "(if present), force_channels.json "
                    "and manifest.json."
                )
            )

        return True

    except Exception as exc:

        print(
            f"Backup error: {exc}"
        )

        bot.send_message(
            chat_id,
            "❌ Cannot create backup."
        )

        return False


@bot.message_handler(
    commands=['backup']
)
def backup(message):

    if not admin_only(message):
        return

    send_backup_to_admin(
        message.chat.id
    )


# =========================================================
# /data = BACKUP ALIAS
# =========================================================

@bot.message_handler(
    commands=['data']
)
def view_data(message):

    if not admin_only(message):
        return

    send_backup_to_admin(
        message.chat.id
    )


# =========================================================
# START BOT
# =========================================================

print(f"{BUILD_VERSION}")
print("Bot running...")
print(f"SQLite database: {DB_FILE}")
print(f"Backup directory: {BACKUP_DIR}")

try:
    bot.remove_webhook()
    print("Webhook removed.")
except Exception as e:
    print(f"Webhook remove error: {e}")

print("Starting polling...")

bot.infinity_polling(
    skip_pending=True,
    timeout=20,
    long_polling_timeout=20,
    allowed_updates=[
        "message",
        "callback_query"
    ]
)
