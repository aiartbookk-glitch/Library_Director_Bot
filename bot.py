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

# Lấy username 1 lần duy nhất (fix deep link)
BOT_USERNAME = bot.get_me().username

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
    markup.add(
        InlineKeyboardButton("📊 My Links", callback_data="mylinks")
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

    media_id = args[1]

    if media_id not in data:
        bot.send_message(message.chat.id, "Link not found.")
        return

    entry = data[media_id]

    # Tăng view
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

    # Nếu chỉ 1 file
    if len(media_list) == 1:
        item = entry["files"][0]
        if item["type"] == "photo":
            bot.send_photo(message.chat.id, item["file_id"])
        elif item["type"] == "video":
            bot.send_video(message.chat.id, item["file_id"])
        elif item["type"] == "document":
            bot.send_document(message.chat.id, item["file_id"])

    # Nếu nhiều file -> chia nhóm 10
    else:
        for i in range(0, len(media_list), 10):
            chunk = media_list[i:i+10]
            bot.send_media_group(message.chat.id, chunk)


# ================= CALLBACK =================

@bot.callback_query_handler(func=lambda call: True)
def callback(call):

    # UPLOAD
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

    # FINISH
    elif call.data == "finish":

        user_id = call.from_user.id

        if user_id not in upload_sessions:
            return

        if not upload_sessions[user_id]["files"]:
            bot.answer_callback_query(call.id, "No files uploaded.")
            return

        upload_sessions[user_id]["waiting_name"] = True

        bot.edit_message_text(
            "Enter name for this link:",
            call.message.chat.id,
            call.message.message_id
        )

    # MY LINKS
    elif call.data == "mylinks":

        data = load_data()
        user_id = call.from_user.id

        text = "📊 Your Links:\n\n"
        found = False

        for media_id, info in data.items():
            if info.get("owner") == user_id:
                found = True
                link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

                text += f"📛 {info.get('name','No name')}\n"
                text += f"🔗 {link}\n"
                text += f"👁 Views: {info['views']}\n"
                text += f"📁 Files: {len(info['files'])}\n\n"

        if not found:
            text = "You have no links yet."

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            disable_web_page_preview=True
        )


# ================= RECEIVE NAME =================

@bot.message_handler(func=lambda m: m.from_user.id in upload_sessions and upload_sessions[m.from_user.id].get("waiting_name"))
def receive_name(message):

    user_id = message.from_user.id
    session = upload_sessions[user_id]

    link_name = message.text.strip()
    media_id = session["media_id"]

    data = load_data()

    data[media_id] = {
        "owner": user_id,
        "name": link_name,
        "files": session["files"],
        "views": 0
    }

    save_data(data)

    link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

    bot.send_message(
        message.chat.id,
        f"✅ Upload Complete!\n\n"
        f"📛 Name: {link_name}\n"
        f"👁 Views: 0\n"
        f"🔗 {link}",
        disable_web_page_preview=True
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
