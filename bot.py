import telebot
import json
import secrets
import os
import time
import threading
import requests

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

# Set BOT_TOKEN in Railway Variables / Environment.
TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Add your Telegram bot token to the BOT_TOKEN environment variable."
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
# TELEGRAM SEND / RATE LIMIT PROTECTION
# =========================================================

# Serialize file delivery so multiple users cannot trigger
# overlapping media-group requests from different handler threads.
TELEGRAM_SEND_LOCK = threading.Lock()
TELEGRAM_MIN_INTERVAL = 0.15
TELEGRAM_MAX_RETRIES = 5

_last_telegram_send = 0.0
_last_telegram_send_lock = threading.Lock()


def _wait_before_telegram_send():
    """Small global gap between outbound send requests."""
    global _last_telegram_send

    with _last_telegram_send_lock:
        now = time.monotonic()
        wait = TELEGRAM_MIN_INTERVAL - (now - _last_telegram_send)

        if wait > 0:
            time.sleep(wait)

        _last_telegram_send = time.monotonic()


def _get_retry_after(exc):
    """Extract Telegram's retry_after value from a 429 exception."""
    try:
        result_json = getattr(exc, "result_json", None) or {}
        parameters = result_json.get("parameters") or {}
        value = parameters.get("retry_after")
        if value is not None:
            return max(1, int(value))
    except (TypeError, ValueError, AttributeError):
        pass

    # Fallback for library/version differences.
    try:
        text = str(exc)
        marker = "retry after "
        if marker in text:
            value = text.split(marker, 1)[1].split()[0]
            return max(1, int(value))
    except (TypeError, ValueError, IndexError):
        pass

    return None


def telegram_call(method, *args, **kwargs):
    """Call a Telegram API method with 429-aware retry and backoff."""
    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            _wait_before_telegram_send()
            return method(*args, **kwargs)

        except telebot.apihelper.ApiTelegramException as exc:
            retry_after = _get_retry_after(exc)

            if retry_after is not None:
                print(
                    f"Telegram 429: waiting {retry_after}s "
                    f"before retry {attempt}/{TELEGRAM_MAX_RETRIES}."
                )
                time.sleep(retry_after + 1)
                continue

            # Retry other Telegram API errors briefly, but do not spin.
            if attempt < TELEGRAM_MAX_RETRIES:
                delay = min(2 * attempt, 8)
                print(
                    f"Telegram API error on attempt {attempt}/"
                    f"{TELEGRAM_MAX_RETRIES}: {exc}. "
                    f"Retrying in {delay}s."
                )
                time.sleep(delay)
                continue

            raise

        except Exception:
            # Network / unexpected errors: retry with small exponential backoff.
            if attempt < TELEGRAM_MAX_RETRIES:
                delay = min(2 ** (attempt - 1), 8)
                print(
                    f"Telegram send error on attempt {attempt}/"
                    f"{TELEGRAM_MAX_RETRIES}. Retrying in {delay}s."
                )
                time.sleep(delay)
                continue

            raise


def send_media_group_safe(chat_id, media_group):
    """Send one media group with retry protection."""
    return telegram_call(
        bot.send_media_group,
        chat_id,
        media_group,
        protect_content=True
    )


def send_album_safe(chat_id, media_list):
    """Send an entire album sequentially so chunks cannot interleave."""
    with TELEGRAM_SEND_LOCK:
        for i in range(0, len(media_list), 10):
            chunk = media_list[i:i + 10]
            send_media_group_safe(chat_id, chunk)


def send_single_file_safe(chat_id, item):
    """Send a single Telegram file with the same retry protection."""
    with TELEGRAM_SEND_LOCK:
        if item["type"] == "photo":
            return telegram_call(
                bot.send_photo,
                chat_id,
                item["file_id"],
                protect_content=True
            )

        if item["type"] == "video":
            return telegram_call(
                bot.send_video,
                chat_id,
                item["file_id"],
                protect_content=True
            )

        if item["type"] == "document":
            return telegram_call(
                bot.send_document,
                chat_id,
                item["file_id"],
                protect_content=True
            )

        raise ValueError(f"Unsupported file type: {item.get('type')}")


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
            with open(DATA_FILE, "r") as f:
                return json.load(f)

    except Exception:
        return {}


def save_data(data):

    with DATA_LOCK:
        tmp_file = f"{DATA_FILE}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False
            )
        os.replace(tmp_file, DATA_FILE)


def load_force_channels():

    try:

        with open(FORCE_FILE, "r") as f:
            return json.load(f)

    except Exception:

        return []


def increment_view_count(media_id):
    """Atomically increment a media link's view counter."""
    with DATA_LOCK:
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}

        if media_id not in data:
            return None

        data[media_id]["views"] = data[media_id].get("views", 0) + 1

        tmp_file = f"{DATA_FILE}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_file, DATA_FILE)

        return data[media_id]["views"]


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
        media_id
    )


# =========================================================
# SEND FILES
# =========================================================

def send_files(chat_id, media_id):

    data = load_data()

    # -----------------------------------------------------
    # SAFETY CHECK
    # -----------------------------------------------------

    if media_id not in data:

        bot.send_message(
            chat_id,
            "Link not found."
        )

        return

    entry = data[media_id]

    # -----------------------------------------------------
    # VIEW COUNT (ATOMIC)
    # -----------------------------------------------------

    increment_view_count(media_id)

    # Refresh the entry after the atomic update.
    data = load_data()
    entry = data[media_id]

    # -----------------------------------------------------
    # MEDIA LIST
    # -----------------------------------------------------

    media_list = []

    for item in entry["files"]:

        if item["type"] == "photo":

            media_list.append(
                InputMediaPhoto(
                    item["file_id"]
                )
            )

        elif item["type"] == "video":

            media_list.append(
                InputMediaVideo(
                    item["file_id"]
                )
            )

        elif item["type"] == "document":

            media_list.append(
                InputMediaDocument(
                    item["file_id"]
                )
            )

    # -----------------------------------------------------
    # NO FILE
    # -----------------------------------------------------

    if not media_list:

        bot.send_message(
            chat_id,
            "No files available."
        )

        return

    # -----------------------------------------------------
    # SINGLE FILE
    # -----------------------------------------------------

    if len(media_list) == 1:

        item = entry["files"][0]

        try:
            send_single_file_safe(
                chat_id,
                item
            )

        except Exception as e:
            print(
                f"Failed to send single file for media_id={media_id}: {e}"
            )
            telegram_call(
                bot.send_message,
                chat_id,
                "⚠️ Không thể gửi file lúc này. Vui lòng thử lại sau."
            )

    # -----------------------------------------------------
    # MULTIPLE FILES
    # -----------------------------------------------------

    else:

        # Telegram maximum media group = 10.
        # Keep the entire album under one lock so another user's album
        # cannot be inserted between this album's chunks.
        try:
            send_album_safe(
                chat_id,
                media_list
            )

        except Exception as e:
            print(
                f"Failed to send album for media_id={media_id}: {e}"
            )

            try:
                telegram_call(
                    bot.send_message,
                    chat_id,
                    "⚠️ Telegram đang giới hạn tốc độ gửi. "
                    "Vui lòng thử lại sau ít giây."
                )
            except Exception as notify_error:
                print(
                    f"Failed to send error message: {notify_error}"
                )

            return


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
                media_id
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

    WORDPRESS_API_KEY = "EroBookQuickPost_7Cd31JmQ4Wn4Pz21"

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

# ---------------------------------------------------------
# REMOVE WEBHOOK
# ---------------------------------------------------------

print("Deleting webhook...")

try:
    print(
        telegram_call(
            bot.remove_webhook
        )
    )
except Exception as e:
    print(
        f"Webhook remove error: {e}"
    )


# ---------------------------------------------------------
# POLLING
# ---------------------------------------------------------

print("Starting polling...")

bot.infinity_polling(
    skip_pending=True,
    timeout=30,
    long_polling_timeout=30,
    allowed_updates=[
        "message",
        "callback_query"
    ]
)
