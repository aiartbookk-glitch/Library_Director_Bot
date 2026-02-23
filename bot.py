import telebot
import json
import os
import uuid
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument
)

# ====== CONFIG ======
TOKEN = "YOUR_BOT_TOKEN_HERE"
BOT_USERNAME = "YOUR_BOT_USERNAME"
DATA_FILE = "data.json"

bot = telebot.TeleBot(TOKEN)

# ====== LOAD DATA ======
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

# ====== MAIN MENU ======
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📂 Create Link", callback_data="create"),
        InlineKeyboardButton("📊 My Links", callback_data="mylinks")
    )
    return markup

# ====== START ======
@bot.message_handler(commands=['start'])
def start_handler(message):
    args = message.text.split()

    # Nếu có link
    if len(args) > 1:
        media_id = args[1]
        data = load_data()

        if media_id not in data:
            bot.send_message(message.chat.id, "❌ Link not found.")
            return

        info = data[media_id]

        # Tăng view
        info["views"] += 1
        save_data(data)

        files = info["files"]

        # Nếu 1 file
        if len(files) == 1:
            item = files[0]
            send_single_file(message.chat.id, item)

        # Nếu nhiều file
        else:
            send_album(message.chat.id, files)

        return

    bot.send_message(
        message.chat.id,
        "🔥 Welcome!\nChoose an option:",
        reply_markup=main_menu()
    )

# ====== SEND SINGLE ======
def send_single_file(chat_id, item):
    if item["type"] == "photo":
        bot.send_photo(chat_id, item["file_id"], protect_content=True)

    elif item["type"] == "video":
        bot.send_video(chat_id, item["file_id"], protect_content=True)

    elif item["type"] == "document":
        bot.send_document(chat_id, item["file_id"], protect_content=True)

# ====== SEND ALBUM (FIX >10 FILES) ======
def send_album(chat_id, files):
    media_list = []

    for item in files:
        if item["type"] == "photo":
            media_list.append(InputMediaPhoto(item["file_id"]))
        elif item["type"] == "video":
            media_list.append(InputMediaVideo(item["file_id"]))
        elif item["type"] == "document":
            media_list.append(InputMediaDocument(item["file_id"]))

    # Telegram giới hạn 10 mỗi lần
    for i in range(0, len(media_list), 10):
        chunk = media_list[i:i+10]
        bot.send_media_group(chat_id, chunk, protect_content=True)

# ====== CALLBACK ======
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):

    if call.data == "create":
        msg = bot.send_message(call.message.chat.id, "📤 Send me files (album supported).")
        bot.register_next_step_handler(msg, handle_files)

    elif call.data == "mylinks":
        show_my_links(call)

    elif call.data == "reset_links":
        reset_links(call)

    elif call.data == "back_menu":
        bot.edit_message_text(
            "Main Menu:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu()
        )

# ====== HANDLE FILES ======
def handle_files(message):

    if not message.content_type in ["photo", "video", "document"]:
        bot.send_message(message.chat.id, "❌ Only photo/video/document allowed.")
        return

    data = load_data()

    media_id = str(uuid.uuid4())
    user_id = message.from_user.id

    files = []

    # Nếu là album
    if message.media_group_id:

        media_group_id = message.media_group_id
        files.append(extract_file(message))

        @bot.message_handler(func=lambda m: m.media_group_id == media_group_id)
        def collect_album(m):
            files.append(extract_file(m))

    else:
        files.append(extract_file(message))

    # Lưu sau 2s để đảm bảo đủ album
    bot.send_message(message.chat.id, "✏ Send name for this link:")
    bot.register_next_step_handler(message, lambda m: save_link(m, media_id, files, user_id))

# ====== EXTRACT FILE ======
def extract_file(message):

    if message.photo:
        return {
            "type": "photo",
            "file_id": message.photo[-1].file_id
        }

    elif message.video:
        return {
            "type": "video",
            "file_id": message.video.file_id
        }

    elif message.document:
        return {
            "type": "document",
            "file_id": message.document.file_id
        }

# ====== SAVE LINK ======
def save_link(message, media_id, files, user_id):

    data = load_data()

    data[media_id] = {
        "name": message.text,
        "owner": user_id,
        "views": 0,
        "files": files
    }

    save_data(data)

    link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

    bot.send_message(
        message.chat.id,
        f"✅ Link created!\n\n🔗 {link}",
        disable_web_page_preview=True
    )

# ====== SHOW MY LINKS ======
def show_my_links(call):

    data = load_data()
    user_id = call.from_user.id

    text = "📊 Your Links:\n\n"
    found = False

    for media_id, info in data.items():
        if info.get("owner") == user_id:
            found = True
            link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

            text += f"📛 {info['name']}\n"
            text += f"🔗 {link}\n"
            text += f"👁 Views: {info['views']}\n"
            text += f"📁 Files: {len(info['files'])}\n\n"

    markup = InlineKeyboardMarkup()

    if found:
        markup.add(
            InlineKeyboardButton("🗑 Reset All My Links", callback_data="reset_links")
        )
    else:
        text = "You have no links."

    markup.add(
        InlineKeyboardButton("⬅ Back", callback_data="back_menu")
    )

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        disable_web_page_preview=True
    )

# ====== RESET ======
def reset_links(call):

    data = load_data()
    user_id = call.from_user.id

    new_data = {
        media_id: info
        for media_id, info in data.items()
        if info.get("owner") != user_id
    }

    deleted = len(data) - len(new_data)

    save_data(new_data)

    bot.edit_message_text(
        f"🗑 Reset complete!\nDeleted {deleted} link(s).",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=main_menu()
    )

# ====== RUN ======
bot.infinity_polling()
