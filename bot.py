import telebot
import json
import secrets
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument
)

TOKEN = "8287739944:AAHp-OIJEpGoIEqt6iBiL1DbKnYYE8Lq3i0"
bot = telebot.TeleBot(TOKEN)

DATA_FILE = "data.json"
upload_sessions = {}


# ================= DATA =================

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


# ================= MENU =================

def main_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📤 Upload File", callback_data="upload")
    )
    return markup


# ================= START =================

@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()
    data = load_data()

    # Mở bot bình thường
    if len(args) == 1:
        bot.send_message(
            message.chat.id,
            "Welcome!",
            reply_markup=main_menu()
        )
        return

    # Mở link file
    media_id = args[1]

    if media_id not in data:
        bot.send_message(message.chat.id, "Link not found.")
        return

    entry = data[media_id]
    entry["views"] += 1
    save_data(data)

    media_list = []

    for item in entry["files"]:
        if item["type"] == "photo":
            media_list.append(InputMediaPhoto(item["file_id"]))
        elif item["type"] == "video":
            media_list.append(InputMediaVideo(item["file_id"]))
        elif item["type"] == "document":
            media_list.append(InputMediaDocument(item["file_id"]))

    if len(media_list) == 1:
        item = entry["files"][0]
        if item["type"] == "photo":
            bot.send_photo(message.chat.id, item["file_id"])
        elif item["type"] == "video":
            bot.send_video(message.chat.id, item["file_id"])
        elif item["type"] == "document":
            bot.send_document(message.chat.id, item["file_id"])
    else:
        bot.send_media_group(message.chat.id, media_list)


# ================= CALLBACK =================

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == "upload":

        media_id = secrets.token_urlsafe(8)

        upload_sessions[call.from_user.id] = {
            "media_id": media_id,
            "files": []
        }

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Finish Upload", callback_data="finish")
        )

        bot.edit_message_text(
            "Send photos / videos / documents now.\nPress Finish when done.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )

    elif call.data == "finish":

        user_id = call.from_user.id

        if user_id not in upload_sessions:
            return

        session = upload_sessions[user_id]

        if not session["files"]:
            bot.answer_callback_query(call.id, "No files uploaded.")
            return

        data = load_data()

        data[session["media_id"]] = {
            "files": session["files"],
            "views": 0
        }

        save_data(data)

        link = f"https://t.me/{bot.get_me().username}?start={session['media_id']}"

        bot.edit_message_text(
            f"✅ Upload Complete!\nViews: 0\n\nLink:\n{link}",
            call.message.chat.id,
            call.message.message_id
        )

        del upload_sessions[user_id]


# ================= HANDLE MEDIA =================

@bot.message_handler(content_types=['photo', 'video', 'document'])
def handle_media(message):

    user_id = message.from_user.id

    if user_id not in upload_sessions:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        upload_sessions[user_id]["files"].append({
            "type": "photo",
            "file_id": file_id
        })

    elif message.video:
        file_id = message.video.file_id
        upload_sessions[user_id]["files"].append({
            "type": "video",
            "file_id": file_id
        })

    elif message.document:
        file_id = message.document.file_id
        upload_sessions[user_id]["files"].append({
            "type": "document",
            "file_id": file_id
        })


print("Bot running...")
bot.infinity_polling()
