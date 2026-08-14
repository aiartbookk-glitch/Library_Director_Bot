import telebot
import json
import secrets
import os
import requests
import time
import threading
import queue

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
# PHASE 1 - PARALLEL DOWNLOADS / RATE LIMIT / ANTI-SPAM
# =========================================================

from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------
# DOWNLOAD CONCURRENCY
# ---------------------------------------------------------
# Users do NOT have to wait for other users' albums to finish.
# Up to this many download jobs can run concurrently.
# Telegram API calls inside those jobs are still rate-limited
# globally and per chat.
DOWNLOAD_WORKERS = 4
DOWNLOAD_QUEUE_MAX = 50
DOWNLOAD_QUEUE = queue.Queue(maxsize=DOWNLOAD_QUEUE_MAX)
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=DOWNLOAD_WORKERS,
    thread_name_prefix="download-worker"
)

DOWNLOAD_STATE_LOCK = threading.Lock()
ACTIVE_DOWNLOAD_USERS = set()
LAST_DOWNLOAD_ACCEPTED = {}
DOWNLOAD_COOLDOWN_SECONDS = 2.0

# ---------------------------------------------------------
# TELEGRAM FILE-SEND RATE LIMITER
# ---------------------------------------------------------
# This is deliberately conservative. If Telegram still returns
# 429, the retry_after value becomes a temporary global cooldown.
FILE_SEND_MIN_INTERVAL = 0.12
MEDIA_GROUP_MIN_INTERVAL_PER_CHAT = 1.05
FILE_SEND_MAX_RETRIES = 5

FILE_RATE_CONDITION = threading.Condition()
NEXT_FILE_SEND_AT = 0.0
GLOBAL_TELEGRAM_COOLDOWN_UNTIL = 0.0

# Last successful file-send time per destination chat.
CHAT_LAST_MEDIA_SEND = {}


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
        text = str(exc).lower()
        marker = "retry after "
        if marker in text:
            value = text.split(marker, 1)[1].split()[0]
            return max(1, int(value))
    except (TypeError, ValueError, IndexError):
        pass

    return None


def _acquire_file_send_slot(chat_id, method_name):
    """Wait until a file-send API call is allowed globally and per chat."""
    global NEXT_FILE_SEND_AT

    with FILE_RATE_CONDITION:
        while True:
            now = time.monotonic()

            global_wait = max(
                0.0,
                GLOBAL_TELEGRAM_COOLDOWN_UNTIL - now
            )

            global_interval_wait = max(
                0.0,
                NEXT_FILE_SEND_AT - now
            )

            chat_wait = 0.0

            # Telegram can be stricter when repeatedly sending into the
            # same destination chat. Apply per-chat pacing mainly to albums.
            if method_name == "send_media_group":
                last_chat_send = CHAT_LAST_MEDIA_SEND.get(chat_id, 0.0)
                chat_wait = max(
                    0.0,
                    MEDIA_GROUP_MIN_INTERVAL_PER_CHAT
                    - (now - last_chat_send)
                )

            wait_for = max(
                global_wait,
                global_interval_wait,
                chat_wait
            )

            if wait_for <= 0:
                send_time = time.monotonic()
                NEXT_FILE_SEND_AT = (
                    send_time + FILE_SEND_MIN_INTERVAL
                )
                return

            FILE_RATE_CONDITION.wait(wait_for)


def _set_global_telegram_cooldown(seconds):
    """Pause all file sends after Telegram explicitly reports 429."""
    global GLOBAL_TELEGRAM_COOLDOWN_UNTIL

    with FILE_RATE_CONDITION:
        GLOBAL_TELEGRAM_COOLDOWN_UNTIL = max(
            GLOBAL_TELEGRAM_COOLDOWN_UNTIL,
            time.monotonic() + max(1, seconds)
        )
        FILE_RATE_CONDITION.notify_all()


def _mark_file_send_success(chat_id, method_name):
    if method_name == "send_media_group":
        with FILE_RATE_CONDITION:
            CHAT_LAST_MEDIA_SEND[chat_id] = time.monotonic()
            FILE_RATE_CONDITION.notify_all()


def send_file_api(method, chat_id, *args, **kwargs):
    """Send a file/media-group with pacing, retry, and adaptive 429 handling."""
    method_name = getattr(method, "__name__", "telegram_method")

    for attempt in range(1, FILE_SEND_MAX_RETRIES + 1):
        _acquire_file_send_slot(chat_id, method_name)

        try:
            result = method(
                chat_id,
                *args,
                **kwargs
            )

            _mark_file_send_success(
                chat_id,
                method_name
            )

            return result

        except telebot.apihelper.ApiTelegramException as exc:
            retry_after = _get_retry_after(exc)

            if retry_after is not None:
                # Telegram itself tells us how long to back off. Because
                # this limit is shared by the bot, pause the file sender
                # globally for that interval rather than hammering again.
                print(
                    f"Telegram 429 on {method_name}: "
                    f"retry_after={retry_after}s "
                    f"(attempt {attempt}/{FILE_SEND_MAX_RETRIES})"
                )

                _set_global_telegram_cooldown(
                    retry_after + 1
                )

                if attempt < FILE_SEND_MAX_RETRIES:
                    continue

            if attempt < FILE_SEND_MAX_RETRIES:
                delay = min(
                    2 ** (attempt - 1),
                    8
                )
                print(
                    f"Telegram API error on {method_name}: {exc}. "
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
                    f"Telegram send/network error on {method_name}: "
                    f"{exc}. Retrying in {delay}s..."
                )
                time.sleep(delay)
                continue

            raise


def safe_download_message(chat_id, text):
    """Best-effort status message for downloads."""
    try:
        bot.send_message(
            chat_id,
            text
        )
    except Exception as exc:
        print(
            f"Could not send status message to {chat_id}: {exc}"
        )

# =========================================================
# FILES
# =========================================================

if not os.path.exists("/data/data.json"):
    with open("/data/data.json", "w") as f:
        f.write("{}")

if not os.path.exists("/data/force_channels.json"):
    with open("/data/force_channels.json", "w") as f:
        f.write("[]")


# =========================================================
# BOT USERNAME
# =========================================================

BOT_USERNAME = bot.get_me().username


# =========================================================
# DATA PATH
# =========================================================

DATA_FILE = "/data/data.json"
FORCE_FILE = "/data/force_channels.json"


# =========================================================
# SESSIONS
# =========================================================

upload_sessions = {}
force_setup_mode = set()
DATA_LOCK = threading.Lock()


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_only(message):
    """
    Kiểm tra user có phải admin không.
    Dùng cho các command admin.
    """

    if not is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "🚫 Bạn không có quyền sử dụng chức năng này."
        )
        return False

    return True


def admin_callback_only(call):
    """
    Kiểm tra quyền admin cho callback.
    """

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

# User bình thường chỉ nhìn thấy /start
bot.set_my_commands([
    BotCommand(
        "start",
        "Open bot"
    )
])


# Admin sẽ nhìn thấy thêm các command quản trị
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
                    "Download data.json backup"
                )
            ],
            scope=BotCommandScopeChat(admin_id)
        )

    except Exception as e:

        print(
            f"Could not set admin commands for {admin_id}: {e}"
        )


# =========================================================
# DATA
# =========================================================

def load_data():

    try:
        with DATA_LOCK:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {}


def save_data(data):

    with DATA_LOCK:
        tmp_file = f"{DATA_FILE}.tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False
            )

        os.replace(tmp_file, DATA_FILE)


def increment_view_count(media_id):
    """Atomically increment one link's view counter."""
    with DATA_LOCK:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False

        if media_id not in data:
            return False

        data[media_id]["views"] = data[media_id].get("views", 0) + 1

        tmp_file = f"{DATA_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        os.replace(tmp_file, DATA_FILE)
        return True


def load_force_channels():

    try:

        with open(FORCE_FILE, "r") as f:
            return json.load(f)

    except Exception:

        return []


def save_force_channels(data):

    with open(FORCE_FILE, "w") as f:
        json.dump(data, f)


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

    data = load_data()

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

    if media_id not in data:

        bot.send_message(
            message.chat.id,
            "Link not found."
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
# SEND FILES
# =========================================================

def _build_media_list(files):
    media_list = []

    for item in files:
        item_type = item.get("type")

        if item_type == "photo":
            media_list.append(
                InputMediaPhoto(item["file_id"])
            )

        elif item_type == "video":
            media_list.append(
                InputMediaVideo(item["file_id"])
            )

        elif item_type == "document":
            media_list.append(
                InputMediaDocument(item["file_id"])
            )

    return media_list


def _deliver_download_job(job):
    """Deliver one user's album. Jobs themselves may run concurrently."""
    chat_id = job["chat_id"]
    media_id = job["media_id"]
    files = job["files"]

    media_list = _build_media_list(files)

    if not media_list:
        safe_download_message(
            chat_id,
            "No files available."
        )
        return

    try:
        if len(media_list) == 1:
            item = files[0]
            item_type = item.get("type")
            file_id = item["file_id"]

            if item_type == "photo":
                send_file_api(
                    bot.send_photo,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            elif item_type == "video":
                send_file_api(
                    bot.send_video,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            elif item_type == "document":
                send_file_api(
                    bot.send_document,
                    chat_id,
                    file_id,
                    protect_content=True
                )

            else:
                raise ValueError(
                    f"Unsupported file type: {item_type}"
                )

            return

        # Telegram maximum media group size = 10.
        # Each group remains atomic, while different users can have
        # their jobs running concurrently.
        for i in range(0, len(media_list), 10):
            chunk = media_list[i:i + 10]

            send_file_api(
                bot.send_media_group,
                chat_id,
                chunk,
                protect_content=True
            )

    except Exception as exc:
        print(
            f"Download failed: chat_id={chat_id}, "
            f"media_id={media_id}, error={exc}"
        )

        safe_download_message(
            chat_id,
            "⚠️ Telegram đang bận hoặc giới hạn tốc độ gửi. "
            "Vui lòng thử lại sau ít giây."
        )


def _download_dispatcher():
    """Move queued jobs into a bounded concurrent worker pool."""
    print(
        f"Download dispatcher started with "
        f"{DOWNLOAD_WORKERS} workers."
    )

    while True:
        job = DOWNLOAD_QUEUE.get()

        try:
            DOWNLOAD_EXECUTOR.submit(
                _run_download_job,
                job
            )
        except Exception as exc:
            print(
                f"Could not submit download job: {exc}"
            )

            with DOWNLOAD_STATE_LOCK:
                ACTIVE_DOWNLOAD_USERS.discard(
                    job.get("user_id")
                )

            safe_download_message(
                job["chat_id"],
                "⚠️ Không thể bắt đầu lượt tải. "
                "Vui lòng thử lại sau."
            )

        finally:
            DOWNLOAD_QUEUE.task_done()


def _run_download_job(job):
    try:
        _deliver_download_job(job)
    except Exception as exc:
        print(
            f"Download worker unexpected error: {exc}"
        )
    finally:
        with DOWNLOAD_STATE_LOCK:
            ACTIVE_DOWNLOAD_USERS.discard(
                job.get("user_id")
            )


def enqueue_download(chat_id, user_id, media_id):
    """Accept a download without forcing users to wait for other users."""
    data = load_data()

    if media_id not in data:
        bot.send_message(
            chat_id,
            "Link not found."
        )
        return False

    entry = data[media_id]
    files = entry.get("files", [])

    if not files:
        bot.send_message(
            chat_id,
            "No files available."
        )
        return False

    now = time.monotonic()

    with DOWNLOAD_STATE_LOCK:
        if user_id in ACTIVE_DOWNLOAD_USERS:
            safe_download_message(
                chat_id,
                "⏳ Bạn đang có một lượt tải đang xử lý. "
                "Vui lòng chờ lượt hiện tại hoàn tất."
            )
            return False

        last_accepted = LAST_DOWNLOAD_ACCEPTED.get(
            user_id,
            0.0
        )

        if now - last_accepted < DOWNLOAD_COOLDOWN_SECONDS:
            remaining = (
                DOWNLOAD_COOLDOWN_SECONDS
                - (now - last_accepted)
            )

            safe_download_message(
                chat_id,
                f"⏳ Vui lòng chờ "
                f"{max(1, int(remaining + 0.99))} "
                f"giây rồi thử lại."
            )
            return False

        if DOWNLOAD_QUEUE.full():
            safe_download_message(
                chat_id,
                "⚠️ Hệ thống đang xử lý quá nhiều lượt tải. "
                "Vui lòng thử lại sau."
            )
            return False

        # Snapshot the file list so a later admin action does not mutate
        # an already accepted download job.
        job = {
            "chat_id": chat_id,
            "user_id": user_id,
            "media_id": media_id,
            "files": [dict(item) for item in files]
        }

        ACTIVE_DOWNLOAD_USERS.add(user_id)
        LAST_DOWNLOAD_ACCEPTED[user_id] = now

        try:
            DOWNLOAD_QUEUE.put_nowait(job)
        except queue.Full:
            ACTIVE_DOWNLOAD_USERS.discard(user_id)

            safe_download_message(
                chat_id,
                "⚠️ Hệ thống đang xử lý quá nhiều lượt tải. "
                "Vui lòng thử lại sau."
            )
            return False

        queued_jobs = DOWNLOAD_QUEUE.qsize()

    # Count a view only after the job has actually been accepted.
    increment_view_count(media_id)

    # Only show queue information when there are more pending jobs than
    # the concurrent worker capacity. Otherwise the user starts shortly.
    if queued_jobs > DOWNLOAD_WORKERS:
        safe_download_message(
            chat_id,
            "⏳ Yêu cầu của bạn đã được nhận và đang chờ xử lý."
        )

    return True


def send_files(chat_id, media_id, user_id):
    return enqueue_download(
        chat_id,
        user_id,
        media_id
    )


# Start the dispatcher once, after all download functions exist.
download_dispatcher_thread = threading.Thread(
    target=_download_dispatcher,
    name="download-dispatcher",
    daemon=True
)
download_dispatcher_thread.start()

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

        # ADMIN ONLY
        if not admin_callback_only(call):
            return

        data = load_data()

        user_id = call.from_user.id

        text = "📊 Your Links:\n\n"

        found = False

        for media_id, info in data.items():

            if info.get("owner") == user_id:

                found = True

                link = (
                    f"https://t.me/"
                    f"{BOT_USERNAME}"
                    f"?start={media_id}"
                )

                text += (
                    f"{info.get('name')}\n"
                    f"{link}\n"
                    f"Views: {info.get('views', 0)}\n"
                    f"Files: {len(info.get('files', []))}\n\n"
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

            text = "You have no links yet."

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

        data = load_data()

        user_id = call.from_user.id

        new_data = {
            k: v
            for k, v in data.items()
            if v.get("owner") != user_id
        }

        save_data(new_data)

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

    data = load_data()

    data[media_id] = {

        "owner": user_id,

        "name": link_name,

        "files": session["files"],

        "views": 0

    }

    save_data(data)

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
# DOWNLOAD DATA
# =========================================================

@bot.message_handler(commands=['data'])
def view_data(message):

    # =====================================================
    # ADMIN ONLY
    # =====================================================

    if not admin_only(message):
        return

    try:

        with open(
            DATA_FILE,
            "rb"
        ) as f:

            bot.send_document(
                message.chat.id,
                f
            )

    except Exception as e:

        bot.send_message(
            message.chat.id,
            f"❌ Cannot read data file:\n{e}"
        )


# =========================================================
# START BOT
# =========================================================

print("Bot running...")

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
    allowed_updates=["message", "callback_query"]
)
