import telebot
import json
import secrets
import time

TOKEN = "8287739944:AAHp-OIJEpGoIEqt6iBiL1DbKnYYE8Lq3i0"

bot = telebot.TeleBot(TOKEN)

DATA_FILE = "data.json"
temp_albums = {}  # lưu album tạm thời


def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


# ===== START =====
@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()
    data = load_data()

    if len(args) == 1:
        bot.reply_to(message, "Send /upload to upload files.")
        return

    media_id = args[1]

    if media_id not in data:
        bot.reply_to(message, "Link not found.")
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


# ===== UPLOAD =====
@bot.message_handler(commands=['upload'])
def upload(message):
    media_id = secrets.token_urlsafe(8)
    bot.reply_to(message, "Send files now. Type DONE when finished.")
    bot.register_next_step_handler(message, handle_files, media_id, [])


def handle_files(message, media_id, files):

    if message.text == "DONE":
        data = load_data()

        data[media_id] = {
            "files": files,
            "views": 0,
            "from_chat": message.chat.id
        }

        save_data(data)

        link = f"https://t.me/{bot.get_me().username}?start={media_id}"

        bot.send_message(
            message.chat.id,
            f"Upload complete!\nViews: 0\nLink:\n{link}"
        )
        return

    # ===== MEDIA GROUP HANDLING =====
    if message.media_group_id:
        group_id = message.media_group_id

        if group_id not in temp_albums:
            temp_albums[group_id] = []

        temp_albums[group_id].append(message.message_id)

        # đợi 1 chút để Telegram gửi hết album
        time.sleep(1)

        files.extend(temp_albums[group_id])
        temp_albums.pop(group_id, None)

        bot.reply_to(message, "Album saved.")
    else:
        if message.content_type in ["photo", "video", "document"]:
            files.append(message.message_id)
            bot.reply_to(message, "Saved.")
        else:
            bot.reply_to(message, "Send media only.")

    bot.register_next_step_handler(message, handle_files, media_id, files)


print("Bot running...")
bot.infinity_polling()
