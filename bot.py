import telebot
import json
import secrets
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8287739944:AAHp-OIJEpGoIEqt6iBiL1DbKnYYE8Lq3i0"
bot = telebot.TeleBot(TOKEN)

DATA_FILE = "data.json"
upload_sessions = {}


def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


# ===== MAIN MENU =====
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📤 Upload File", callback_data="upload")
    )
    return markup


# ===== START =====
@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()
    data = load_data()

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
    entry["views"] += 1
    save_data(data)

    for file_id in entry["files"]:
        bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=entry["from_chat"],
            message_id=file_id,
            protect_content=True
        )


# ===== CALLBACK HANDLER =====
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == "upload":
        media_id = secrets.token_urlsafe(8)

        upload_sessions[call.from_user.id] = {
            "media_id": media_id,
            "files": [],
            "from_chat": call.message.chat.id
        }

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Finish Upload", callback_data="finish")
        )

        bot.edit_message_text(
            "Send files now.\nWhen finished press Finish.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )

    elif call.data == "finish":
        user_id = call.from_user.id

        if user_id not in upload_sessions:
            return

        session = upload_sessions[user_id]
        data = load_data()

        data[session["media_id"]] = {
            "files": session["files"],
            "views": 0,
            "from_chat": session["from_chat"]
        }

        save_data(data)

        link = f"https://t.me/{bot.get_me().username}?start={session['media_id']}"

        bot.edit_message_text(
            f"✅ Upload Complete!\nViews: 0\n\nLink:\n{link}",
            call.message.chat.id,
            call.message.message_id
        )

        del upload_sessions[user_id]


# ===== HANDLE MEDIA =====
@bot.message_handler(content_types=['photo', 'video', 'document'])
def handle_media(message):
    user_id = message.from_user.id

    if user_id not in upload_sessions:
        return

    upload_sessions[user_id]["files"].append(message.message_id)


print("Bot running...")
bot.infinity_polling()
